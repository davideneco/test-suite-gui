"""Tests unitaires et fonctionnels (outils factices) pour l'intégration Axelera AI (Metis AIPU)."""

import json
import os
import stat
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import axelera_aipu as ax
from core.bench_axelera import bench_axelera_stress, build_axelera_command, parse_axrunmodel_output, parse_inference_output
from core.fault_monitor import classify_line
from core.result import BenchmarkResult
from core.system_info import _is_gpu_pci_line
from core.verdict import FAIL, PASS, WARN, compute_verdict
from exporters.common import _series_for_result


class TestAxeleraParsing(unittest.TestCase):
    def test_parse_axdevice_single_and_multi(self):
        out = (
            "Device 0: metis-0:1:0 130p 4GiB flver=1.3.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%\n"
            "Device 1: metis-0:6c:0 part of 150p 4GiB flver=1.3.0 bcver=1.4 clock=600MHz(0-3:600MHz) mvm=0:100%,1-3:50%\n"
            "Device 2: metis-0:2:0 113m std 8GiB flver=1.6.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%\n"
        )
        devs = ax.parse_axdevice_output(out)
        self.assertEqual(len(devs), 3)
        self.assertEqual(devs[0]["model"], "130p")
        self.assertEqual(devs[0]["mem_mb"], 4096)
        self.assertEqual(devs[0]["bdf"], "0000:01:00.0")
        self.assertEqual(devs[0]["clock_mhz"], 800)
        self.assertEqual(devs[0]["mvm_pct"], 100.0)
        self.assertEqual(devs[1]["model"], "part of 150p")
        self.assertEqual(devs[1]["bdf"], "0000:6c:00.0")
        self.assertEqual(devs[1]["clock_mhz"], 600)
        self.assertEqual(devs[1]["mvm_pct"], 62.5)
        self.assertEqual(devs[2]["model"], "113m std")
        self.assertEqual(devs[2]["mem_mb"], 8192)

    def test_parse_axmonitor_and_axlogdevice(self):
        mon = (
            "=== DEV0 (metis-0:1:0) ===\n"
            "Core Temp: 54.5 C\n"
            "Power: 11.4 W\n"
            "Clock: 800 MHz\n"
            "MVM utilization: 97.5 %\n"
            "Total KPS: 642.0\n"
            "DDR: 1024 MB / 4096 MB\n"
            "PCIe DMA: 1850.5 MB/s\n"
        )
        parsed = ax.parse_axmonitor_print(mon)
        self.assertIn(0, parsed)
        self.assertAlmostEqual(parsed[0]["temp_c"], 54.5)
        self.assertAlmostEqual(parsed[0]["power_w"], 11.4)
        self.assertEqual(parsed[0]["clock_mhz"], 800.0)
        self.assertAlmostEqual(parsed[0]["mvm_pct"], 97.5)
        self.assertAlmostEqual(parsed[0]["kps"], 642.0)
        self.assertAlmostEqual(parsed[0]["ddr_used_mb"], 1024.0)
        self.assertAlmostEqual(parsed[0]["pcie_bw_mbs"], 1850.5)

        log_pt = ax.parse_axlogdevice_line("[04:58:54.012,603] <inf> collector: core_temps=[35,41,39,40,38]")
        self.assertEqual(log_pt["temp_c"], 41.0)
        self.assertEqual(log_pt["board_temp_c"], 35.0)

    def test_parse_axrunmodel_and_inference(self):
        r1 = parse_axrunmodel_output("Device FPS: 1245.50\nHost FPS: 980.10\nSystem FPS: 975.40\n")
        self.assertEqual(r1["device_fps"], 1245.5)
        self.assertEqual(r1["host_fps"], 980.1)
        self.assertEqual(r1["system_fps"], 975.4)

        r2 = parse_inference_output("INFO : Core Temp : 48.5°C\nSystem FPS: 181.2\nDevice FPS: 220.0\nCPU usage: 14.5%\n")
        self.assertEqual(r2["core_temp_c"], 48.5)
        self.assertEqual(r2["system_fps"], 181.2)
        self.assertEqual(r2["cpu_usage_pct"], 14.5)

    def test_pci_detection_and_fault_monitor(self):
        self.assertTrue(_is_gpu_pci_line("01:00.0 Processing accelerators [1200]: Axelera AI Metis AIPU [1f9d:1100] (rev 02)"))
        self.assertFalse(_is_gpu_pci_line("00:01.0 Co-processor [0b40]: Intel Corporation QAT [8086:4940]"))

        ev_dma = classify_line("axl 0000:01:00.0: Fail to dma alloc 400000")
        self.assertIsNotNone(ev_dma)
        self.assertEqual(ev_dma["severity"], "critical")

        ev_sd = classify_line("metis 0000:01:00.0: thermal shutdown triggered at 120C")
        self.assertIsNotNone(ev_sd)
        self.assertEqual(ev_sd["severity"], "critical")

        ev_w = classify_line("axl 0000:01:00.0: thermal warning threshold reached")
        self.assertIsNotNone(ev_w)
        self.assertEqual(ev_w["severity"], "warning")


class TestAxeleraFunctionalMock(unittest.TestCase):
    def test_functional_run_with_fake_sdk_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            model_json = os.path.join(tmp, "model.json")
            with open(model_json, "w", encoding="utf-8") as f:
                json.dump({"name": "yolov8s-test"}, f)

            def _mk_script(name: str, body: str) -> str:
                p = os.path.join(bindir, name)
                with open(p, "w", encoding="utf-8") as f:
                    f.write("#!/usr/bin/env bash\n" + body)
                os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
                return p

            _mk_script(
                "axdevice",
                'echo "Device 0: metis-0:1:0 130p 4GiB flver=1.3.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%"\n',
            )
            _mk_script(
                "axmonitor",
                'echo "=== DEV0 ==="\necho "Core Temp: 62.0 C"\necho "Power: 14.2 W"\necho "MVM: 99%"\necho "KPS: 810"\n',
            )
            _mk_script(
                "axrunmodel",
                'echo "Running fake axrunmodel args: $@"\nsleep 1.4\n'
                'echo "Device FPS: 910.25"\necho "Host FPS: 820.10"\necho "System FPS: 805.50"\n',
            )

            with patch.dict(os.environ, {"PATH": bindir + os.pathsep + os.environ.get("PATH", "")}):
                ax.forget_cache()
                cmd, _cwd, err = build_axelera_command(
                    "axrunmodel", model_json, time_s=2, aipu_cores=4, target_devices="0"
                )
                self.assertEqual(err, "")
                self.assertIn("--seconds", cmd)
                self.assertIn("2", cmd)
                self.assertIn("-d", cmd)
                self.assertIn("0", cmd)
                self.assertIn("--aipu-cores", cmd)

                with patch("core.bench_axelera.LOG_PRE_SECONDS", 0.0), \
                     patch("core.bench_axelera.LOG_POST_SECONDS", 0.0):
                    res = bench_axelera_stress(
                        model_path=model_json,
                        engine="axrunmodel",
                        time_s=2,
                        aipu_cores=4,
                        target_devices="0",
                    )

            self.assertEqual(res.status, "ok")
            self.assertAlmostEqual(res.score, 805.50)
            self.assertEqual(res.unit, "FPS")
            self.assertAlmostEqual(res.metrics["device_fps"], 910.25)
            self.assertIn("axrunmodel", res.command)
            self.assertGreaterEqual(res.metrics.get("temp_max_c", 0), 62.0)

            # Vérifie que les séries sont exploitables par les exports
            extracted = _series_for_result(res)
            self.assertIsNotNone(extracted)
            keys = [k for k, _lbl, _v in extracted["series"]]
            self.assertIn("series_temp", keys)
            self.assertIn("series_power", keys)

    def test_verdict_axelera_thresholds(self):
        r_ok = BenchmarkResult("Axelera AIPU (m)", "Axelera AI", 500.0, "FPS", 10.0, "",
                               metrics={"temp_max_c": 68.0}, status="ok")
        v_ok = compute_verdict([r_ok], {})
        self.assertEqual(v_ok["status"], "CONFORME")

        r_warn = BenchmarkResult("Axelera AIPU (m)", "Axelera AI", 450.0, "FPS", 10.0, "",
                                 metrics={"temp_max_c": 97.0}, status="ok")
        v_warn = compute_verdict([r_warn], {})
        self.assertEqual(v_warn["status"], "A_VERIFIER")

        r_fail = BenchmarkResult("Axelera AIPU (m)", "Axelera AI", 200.0, "FPS", 10.0, "",
                                 metrics={"temp_max_c": 120.5}, status="ok")
        v_fail = compute_verdict([r_fail], {})
        self.assertEqual(v_fail["status"], "NON_CONFORME")

    def test_multi_sdk_selection_prefers_tools_and_newer_version(self):
        with tempfile.TemporaryDirectory() as home:
            sdk_old = os.path.join(home, "voyager-sdk")
            sdk_new = os.path.join(home, "voyager-sdk-1.9.1")
            os.makedirs(sdk_old)
            with open(os.path.join(sdk_old, "VERSION.TXT"), "w") as f:
                f.write("1.5.2\n")
            os.makedirs(os.path.join(sdk_new, "venv", "bin"))
            with open(os.path.join(sdk_new, "VERSION.TXT"), "w") as f:
                f.write("1.9.1\n")
            axdev = os.path.join(sdk_new, "venv", "bin", "axdevice")
            with open(axdev, "w") as f:
                f.write("#!/bin/sh\necho 'libaxl missing' >&2\nexit 1\n")
            os.chmod(axdev, 0o755)

            with patch("core.axelera_aipu.get_config", return_value={}), \
                 patch("core.axelera_aipu._candidate_homes", return_value=[home]), \
                 patch("shutil.which", return_value=None):
                ax.forget_cache()
                self.assertEqual(ax.find_sdk_dir(), sdk_new)
                sdks = ax.list_detected_sdks()
                self.assertEqual(len(sdks), 2)
                self.assertEqual(sdks[0]["path"], sdk_new)
                self.assertTrue(sdks[0]["has_tools"])
                self.assertEqual(ax.find_tool("axdevice"), axdev)

                with patch("core.axelera_aipu.pci_devices", return_value=[
                    {"bdf": "0000:03:00.0", "vendor_id": "1f9d", "device_id": "1100",
                     "desc": "Metis AIPU", "pcie_gen": "3", "pcie_width": "4", "pcie_speed": "8.0 GT/s", "driver": ""}
                ]):
                    devs = ax.list_devices(timeout=2.0)
                    st = ax.sdk_status()
                    self.assertEqual(len(devs), 1)
                    self.assertFalse(devs[0]["axdevice_ok"])
                    self.assertIn("libaxl missing", st["axdevice_error"])

    def test_opt_runtime_bin_and_ld_library_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            rt_old = os.path.join(tmp, "runtime-1.5.2-1")
            rt_new = os.path.join(tmp, "runtime-1.9.1-1")
            os.makedirs(os.path.join(rt_old, "bin"))
            os.makedirs(os.path.join(rt_new, "bin"))
            os.makedirs(os.path.join(rt_new, "lib"))
            axdev = os.path.join(rt_new, "bin", "axdevice")
            with open(axdev, "w") as f:
                f.write("#!/bin/sh\nexit 0\n")
            os.chmod(axdev, 0o755)

            with patch("core.axelera_aipu._opt_runtime_dirs", return_value=[rt_new, rt_old]), \
                 patch("core.axelera_aipu.find_sdk_dir", return_value=""), \
                 patch("core.axelera_aipu._candidate_sdk_dirs", return_value=[]), \
                 patch("core.axelera_aipu._candidate_homes", return_value=[]), \
                 patch("shutil.which", return_value=None):
                ax.forget_cache()
                self.assertEqual(ax.find_tool("axdevice"), axdev)
                env = ax.sdk_env()
                self.assertIn(os.path.join(rt_new, "bin"), env["PATH"])
                self.assertIn(os.path.join(rt_new, "lib"), env.get("LD_LIBRARY_PATH", ""))


if __name__ == "__main__":
    unittest.main()
