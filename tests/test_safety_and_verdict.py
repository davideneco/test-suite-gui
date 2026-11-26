# -*- coding: utf-8 -*-
"""Tests unitaires pour la sécurité disque (core/safety.py), les logs noyau et le verdict."""

import os
import tempfile
import unittest
from unittest import mock

from tests._mock_deps import ensure_psutil_stub

ensure_psutil_stub()

import core.dmesg_monitor as dm
import core.safety as safety
import core.verdict as verdict
from core.result import BenchmarkResult


class TestSafetyDiskProtection(unittest.TestCase):
    """Vérifie la protection contre l'écriture destructive sur disque OS ou monté."""

    def test_is_write_mode(self):
        self.assertFalse(safety.is_write_mode("read"))
        self.assertFalse(safety.is_write_mode("randread"))
        self.assertTrue(safety.is_write_mode("write"))
        self.assertTrue(safety.is_write_mode("randwrite"))
        self.assertTrue(safety.is_write_mode("randrw"))
        self.assertTrue(safety.is_write_mode(""))

    def test_block_raw_write_on_os_disk(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(safety, "disks_for_target", return_value={"nvme0n1"}), \
             mock.patch.object(safety, "os_disks", return_value={"nvme0n1"}):
            lvl, msg = safety.check_write_target("/dev/nvme0n1", raw_device=True, writes=True)
        self.assertEqual(lvl, "block")
        self.assertIn("disque système", msg)

    def test_block_raw_write_on_mounted_data_disk(self):
        with mock.patch("os.path.exists", return_value=True), \
             mock.patch.object(safety, "disks_for_target", return_value={"nvme1n1"}), \
             mock.patch.object(safety, "os_disks", return_value={"nvme0n1"}), \
             mock.patch.object(safety, "mounted_partitions_of", return_value=["/mnt/data"]):
            lvl, msg = safety.check_write_target("/dev/nvme1n1", raw_device=True, writes=True, allow_mounted=False)
        self.assertEqual(lvl, "block")
        self.assertIn("partitions montées", msg)

    def test_allow_raw_read_even_on_os_disk(self):
        with mock.patch("os.path.exists", return_value=True):
            lvl, msg = safety.check_write_target("/dev/nvme0n1", raw_device=True, writes=False)
        self.assertEqual(lvl, "ok")
        self.assertEqual(msg, "")

    def test_warn_file_write_on_os_disk(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(safety, "disks_for_target", return_value={"nvme0n1"}), \
             mock.patch.object(safety, "os_disks", return_value={"nvme0n1"}):
            lvl, msg = safety.check_write_target(td, raw_device=False, writes=True)
        self.assertEqual(lvl, "warn")
        self.assertIn("disque système", msg)

    def test_ok_file_write_on_data_disk(self):
        with tempfile.TemporaryDirectory() as td, \
             mock.patch.object(safety, "disks_for_target", return_value={"nvme1n1"}), \
             mock.patch.object(safety, "os_disks", return_value={"nvme0n1"}):
            lvl, msg = safety.check_write_target(td, raw_device=False, writes=True)
        self.assertEqual(lvl, "ok")
        self.assertEqual(msg, "")


class TestDmesgMonitor(unittest.TestCase):
    """Vérifie la lecture et le filtrage des journaux noyau."""

    def test_parse_and_filter_kernel_logs(self):
        sample = "\n".join([
            "2026-11-16T10:00:00+0000 srv kernel: nvme nvme0: pci function 0000:01:00.0",
            "2026-11-16T10:00:01+0000 srv kernel: NVRM: Xid (PCI:0000:01:00): 79, status=0x00000001 failed",
            "2026-11-16T10:00:02+0000 srv kernel: cpufreq: thermal throttled on cpu 0",
        ])
        fake_res = mock.Mock(returncode=0, stdout=sample, stderr="")
        with mock.patch.object(dm, "_tool", side_effect=lambda name: name == "journalctl"), \
             mock.patch("core.dmesg_monitor.subprocess.run", return_value=fake_res):
            all_logs = dm.get_kernel_logs(max_lines=50, level_filter="all")
            err_logs = dm.get_kernel_logs(max_lines=50, level_filter="error")
            hw_logs = dm.get_kernel_logs(max_lines=50, level_filter="hardware", keyword_filter="nvme")

        self.assertEqual(all_logs["total_count"], 3)
        self.assertEqual(all_logs["error_count"], 1)
        self.assertEqual(all_logs["warning_count"], 1)
        self.assertEqual(len(err_logs["entries"]), 1)
        self.assertEqual(len(hw_logs["entries"]), 1)
        self.assertIn("nvme0", hw_logs["entries"][0]["content"])


class TestVerdictFaultRules(unittest.TestCase):
    """Vérifie l'impact des événements matériels dans le verdict automatique."""

    def test_critical_fault_forces_fail(self):
        res = BenchmarkResult(
            name="GPU burn",
            category="GPU",
            score=100.0,
            unit="Gflop/s",
            duration_s=10.0,
            status="error",
            raw_output="",
            metrics={
                "hardware_fault_critical": 1,
                "hardware_events": [{"severity": "critical", "category": "NVIDIA Xid 79", "device": "PCI:0000:01:00"}],
            },
        )
        v = verdict.compute_verdict([res], snapshot={})
        self.assertEqual(v["status"], "NON_CONFORME")
        self.assertTrue(any(r["id"] == "hw_fault" and r["level"] == verdict.FAIL for r in v["rules"]))

    def test_warning_fault_gives_warn_and_clean_gives_pass(self):
        warn_res = BenchmarkResult(
            name="NVMe",
            category="Stockage",
            score=500.0,
            unit="MiB/s",
            duration_s=10.0,
            status="ok",
            raw_output="",
            metrics={
                "hardware_events": [{"severity": "warning", "category": "Problème NVMe", "device": ""}],
            },
        )
        v_warn = verdict.compute_verdict([warn_res], snapshot={})
        self.assertEqual(v_warn["status"], "A_VERIFIER")
        self.assertTrue(any(r["id"] == "hw_events" and r["level"] == verdict.WARN for r in v_warn["rules"]))

        clean_res = BenchmarkResult(
            name="CPU", category="CPU", score=100.0, unit="ops/s",
            duration_s=5.0, status="ok", raw_output="",
        )
        v_ok = verdict.compute_verdict([clean_res], snapshot={})
        self.assertEqual(v_ok["status"], "CONFORME")
        self.assertTrue(any(r["id"] == "hw_clean" and r["level"] == verdict.PASS for r in v_ok["rules"]))


if __name__ == "__main__":
    unittest.main()
