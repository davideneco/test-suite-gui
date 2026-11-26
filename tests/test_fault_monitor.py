# -*- coding: utf-8 -*-
"""Tests unitaires pour core/fault_monitor.py."""

import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import core.fault_monitor as fm
from core.result import BenchmarkResult


class TestClassifyLine(unittest.TestCase):
    """Vérifie la classification des lignes du journal noyau."""

    def test_nvidia_xid_critical_and_warning(self):
        crit = fm.classify_line("NVRM: Xid (PCI:0000:01:00): 79, pid=123, GPU has fallen off the bus.")
        self.assertIsNotNone(crit)
        self.assertEqual(crit["severity"], "critical")
        self.assertEqual(crit["xid"], 79)
        self.assertEqual(crit["device"], "PCI:0000:01:00")
        self.assertIn("Xid 79", crit["category"])

        warn = fm.classify_line("NVRM: Xid (PCI:0000:02:00): 31, Ch 00000010, MMU Fault")
        self.assertIsNotNone(warn)
        self.assertEqual(warn["severity"], "warning")
        self.assertEqual(warn["xid"], 31)

    def test_mce_and_aer_priority_uncorrected_before_corrected(self):
        mce_fat = fm.classify_line("mce: [Hardware Error]: Machine check events logged; uncorrected fatal")
        self.assertEqual(mce_fat["severity"], "critical")
        mce_cor = fm.classify_line("mce: [Hardware Error]: CPU 0: Machine Check: 0 Bank 4")
        self.assertEqual(mce_cor["severity"], "warning")

        aer_unc = fm.classify_line("pcieport 0000:00:01.0: AER: Uncorrected (Fatal) Error message received")
        self.assertEqual(aer_unc["severity"], "critical")
        aer_cor = fm.classify_line("pcieport 0000:00:01.0: AER: Corrected error received: 0000:01:00.0")
        self.assertEqual(aer_cor["severity"], "warning")

    def test_modern_nvme_and_io_error_patterns(self):
        nvme_modern = fm.classify_line("nvme nvme0: I/O tag 12 QID 1 timeout, reset controller")
        self.assertIsNotNone(nvme_modern)
        self.assertEqual(nvme_modern["severity"], "warning")
        self.assertIn("NVMe", nvme_modern["category"])

        nvme_down = fm.classify_line("nvme0: controller is down; will reset")
        self.assertIsNotNone(nvme_down)
        self.assertEqual(nvme_down["severity"], "warning")

        io_modern = fm.classify_line("I/O error, dev nvme0n1, sector 2048 op 0x1:(WRITE)")
        self.assertIsNotNone(io_modern)
        self.assertEqual(io_modern["severity"], "warning")

    def test_edac_amdgpu_thermal_oom_and_benign(self):
        self.assertEqual(fm.classify_line("EDAC MC0: 1 UE memory read error")["severity"], "critical")
        self.assertEqual(fm.classify_line("EDAC MC0: 2 CE memory read error")["severity"], "warning")
        self.assertEqual(fm.classify_line("amdgpu 0000:03:00.0: ring gfx_0.0.0 timeout")["severity"], "critical")
        self.assertEqual(fm.classify_line("thermal thermal_zone0: critical temperature reached, shutting down")["severity"], "critical")
        self.assertEqual(fm.classify_line("Out of memory: Killed process 4242 (stress-ng)")["severity"], "critical")
        self.assertEqual(fm.classify_line("NVLink: fatal error on link 2")["severity"], "warning")
        self.assertIsNone(fm.classify_line("usb 1-1: new high-speed USB device number 3"))


class TestFaultMonitorEventsAndEcc(unittest.TestCase):
    """Vérifie la gestion des événements, la saturation MAX_EVENTS et le polling ECC."""

    def test_critical_event_preserved_when_max_events_full_of_warnings(self):
        abort = threading.Event()
        with mock.patch.object(fm, "nvidia_smi_usable", return_value=False):
            mon = fm.FaultMonitor(abort_event=abort)
        mon._t0 = time.time()
        for i in range(fm.MAX_EVENTS + 10):
            mon._add({"severity": "warning", "category": "Warn", "line": f"w{i}"})
        self.assertEqual(len(mon.events), fm.MAX_EVENTS)
        self.assertFalse(mon.critical)
        self.assertFalse(abort.is_set())

        # Un événement critique doit chasser un warning, ne pas être perdu et déclencher abort_event
        mon._add({"severity": "critical", "category": "OOM", "line": "Out of memory: Killed process 99"})
        self.assertEqual(len(mon.events), fm.MAX_EVENTS)
        self.assertTrue(mon.critical)
        self.assertTrue(abort.is_set())
        crits = [e for e in mon.events if e["severity"] == "critical"]
        self.assertEqual(len(crits), 1)
        self.assertEqual(crits[0]["category"], "OOM")
        summary = mon.stop()
        self.assertTrue(summary["critical"])
        self.assertEqual(summary["critical_count"], 1)

    def test_ecc_once_baseline_increment_and_retired_dedup(self):
        abort = threading.Event()
        with mock.patch.object(fm, "nvidia_smi_usable", return_value=True):
            mon = fm.FaultMonitor(abort_event=abort)
        mon._t0 = time.time()

        r1 = mock.Mock(returncode=0, stdout="0, 2, Yes\n1, [N/A], No\n")
        r2 = mock.Mock(returncode=0, stdout="0, 2, Yes\n1, [N/A], No\n")
        r3 = mock.Mock(returncode=0, stdout="0, 5, Yes\n1, [N/A], No\n")

        with mock.patch("core.fault_monitor.subprocess.run", side_effect=[r1, r2, r3]):
            mon._ecc_once()
            mon._ecc_once()
            # retired_pages.pending=Yes ne doit être signalé qu'une seule fois
            self.assertEqual(len(mon.events), 1)
            self.assertEqual(mon.events[0]["severity"], "warning")
            self.assertFalse(abort.is_set())

            mon._ecc_once()
            self.assertEqual(len(mon.events), 2)
            crits = [e for e in mon.events if e["severity"] == "critical"]
            self.assertEqual(len(crits), 1)
            self.assertIn("2 → 5", crits[0]["line"])
            self.assertTrue(abort.is_set())


class TestFaultMonitorLifecycle(unittest.TestCase):
    """Vérifie la lecture flux temps réel (y compris octets invalides) et l'arrêt du sous-processus."""

    def test_streaming_non_utf8_and_clean_stop(self):
        script = (
            "import sys, time\n"
            "sys.stdout.buffer.write(b'normal line\\n')\n"
            "sys.stdout.buffer.write(b'bad\\xff\\xfe NVRM: Xid (PCI:0000:01:00): 79, fallen off bus\\n')\n"
            "sys.stdout.buffer.flush()\n"
            "time.sleep(30)\n"
        )
        abort = threading.Event()
        with mock.patch.object(fm, "nvidia_smi_usable", return_value=False):
            mon = fm.FaultMonitor(abort_event=abort)
        with mock.patch.object(mon, "_candidates", return_value=[[sys.executable, "-c", script]]):
            mon.start()
            self.assertTrue(abort.wait(timeout=4.0), "FaultMonitor aurait dû déclencher abort_event")
            t_start = time.time()
            summary = mon.stop()
            elapsed = time.time() - t_start
        self.assertLess(elapsed, 3.0)
        self.assertTrue(summary["critical"])
        self.assertGreaterEqual(summary["critical_count"], 1)
        self.assertEqual(mon._proc.poll() is not None, True)

    def test_attach_summary_no_empty_parens_without_device(self):
        res = BenchmarkResult(
            name="CPU", category="CPU", score=1.0, unit="u",
            duration_s=1.0, status="stopped", raw_output="",
        )
        summary = {
            "source": "journalctl -k -f",
            "critical": True,
            "count": 1,
            "critical_count": 1,
            "events": [{"severity": "critical", "category": "Manque de mémoire (OOM killer)", "line": "oom-kill"}],
        }
        fm._attach_summary(res, summary)
        self.assertEqual(res.status, "error")
        self.assertNotIn("()", res.notes)
        self.assertIn("Manque de mémoire", res.notes)
        self.assertEqual(res.metrics["hardware_fault_critical"], 1)

    def test_early_exiting_candidate_and_stubborn_process_termination(self):
        # Premier candidat meurt aussitôt (ex. journalctl sans droits), second reste vivant et ignore SIGTERM
        stubborn = (
            "import signal, sys, time\n"
            "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "sys.stdout.write('ok\\n'); sys.stdout.flush()\n"
            "time.sleep(30)\n"
        )
        abort = threading.Event()
        with mock.patch.object(fm, "nvidia_smi_usable", return_value=False):
            mon = fm.FaultMonitor(abort_event=abort)
        with mock.patch.object(
            mon, "_candidates",
            return_value=[[sys.executable, "-c", "import sys; sys.exit(1)"], [sys.executable, "-c", stubborn]],
        ):
            mon.start()
            t0 = time.time()
            summary = mon.stop()
            elapsed = time.time() - t0
        self.assertNotEqual(summary["source"], "indisponible")
        self.assertLess(elapsed, 3.0)
        self.assertIsNotNone(mon._proc.poll())


class TestWithFaultWatchDecorator(unittest.TestCase):
    """Vérifie la robustesse du décorateur with_fault_watch."""

    def test_kill_event_and_depth_restored_on_exception(self):
        fm._DEPTH.n = 0

        class BrokenObs:
            def start(self):
                return self

            def stop(self):
                raise RuntimeError("observer boom")

        @fm.with_fault_watch
        def failing_test(kill_event=None):
            raise ValueError("test failed")

        with mock.patch.object(fm, "_Observers", BrokenObs), \
             mock.patch.object(fm, "nvidia_smi_usable", return_value=False), \
             mock.patch.object(fm.FaultMonitor, "_open_source", return_value=False):
            with self.assertRaises(ValueError):
                failing_test()
        self.assertEqual(getattr(fm._DEPTH, "n", 0), 0)

    def test_stop_flag_fn_propagation(self):
        fm._DEPTH.n = 0
        seen = []

        @fm.with_fault_watch
        def worker(stop_flag_fn=None):
            seen.append(stop_flag_fn())
            return BenchmarkResult(
                name="FIO", category="Stockage", score=10.0,
                unit="MiB/s", duration_s=1.0, status="ok", raw_output="",
            )

        with mock.patch.dict(os.environ, {"BENCHSUITE_NO_ELECTRICAL": "1", "BENCHSUITE_NO_PCIE": "1", "BENCHSUITE_NO_INVENTORY": "1"}), \
             mock.patch.object(fm, "nvidia_smi_usable", return_value=False), \
             mock.patch.object(fm.FaultMonitor, "_open_source", return_value=False):
            res = worker(stop_flag_fn=lambda: False)
        self.assertEqual(seen, [False])
        self.assertEqual(res.status, "ok")
        self.assertEqual(getattr(fm._DEPTH, "n", 0), 0)

    def test_bypass_env_var(self):
        called = []

        @fm.with_fault_watch
        def quick():
            called.append(True)
            return "ok"

        with mock.patch.dict(os.environ, {"BENCHSUITE_NO_FAULT_WATCH": "1"}), \
             mock.patch.object(fm, "FaultMonitor") as mock_fm:
            out = quick()
        self.assertEqual(out, "ok")
        self.assertEqual(called, [True])
        mock_fm.assert_not_called()


if __name__ == "__main__":
    unittest.main()
