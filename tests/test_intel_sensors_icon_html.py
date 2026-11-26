# -*- coding: utf-8 -*-
"""Tests unitaires : icône SVG Ubuntu, capteurs Intel xpu-smi/xe-hwmon, et onglets HTML (zéro doublon Débit/RAM)."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET
from unittest import mock

from core import gpu_stats, intel_gpu
from exporters import html_report as hr

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestUbuntuIconAndDesktop(unittest.TestCase):
    def test_svg_icon_exists_and_is_valid_xml(self):
        svg_path = os.path.join(ROOT, "assets", "io.benchsuite.app.svg")
        self.assertTrue(os.path.isfile(svg_path))
        tree = ET.parse(svg_path)
        self.assertTrue(tree.getroot().tag.endswith("svg"))

    def test_install_and_shortcuts_reference_app_icon_and_wmclass(self):
        for fname in ("install.sh", "create_shortcuts.sh"):
            content = open(os.path.join(ROOT, fname), encoding="utf-8").read()
            self.assertIn("Icon=io.benchsuite.app", content, fname)
            self.assertIn("StartupWMClass=io.benchsuite.app", content, fname)
            self.assertIn("io.benchsuite.app.svg", content, fname)


class TestIntelXpuSensors(unittest.TestCase):
    def test_parse_stats_text_table_full(self):
        sample = """
+-----------------------------+---------------------------------------------+
| Device ID                   | 0                                           |
+-----------------------------+---------------------------------------------+
| Average % utilization of    | 98                                          |
| EU Array Active (%)         | 92.5                                        |
| EU Array Stall (%)          | 4.2                                         |
| Compute Engine Util (%)     | 99; Engine 0: 99                            |
| Render Engine Util (%)      | 12; Engine 0: 12                            |
| Media Engine Util (%)       | 0                                           |
| Copy Engine Util (%)        | 5; Engine 0: 5                              |
+-----------------------------+---------------------------------------------+
| Reset                       | 0                                           |
| Programming Errors          | 0                                           |
| Driver Errors               | 1                                           |
| Cache Errors Correctable    | 2                                           |
| Cache Errors Uncorrectable  | 0                                           |
| Mem Errors Correctable      | 3                                           |
| Mem Errors Uncorrectable    | 0                                           |
+-----------------------------+---------------------------------------------+
| GPU Power (W)               | 255.4                                       |
| GPU Frequency (MHz)         | 2400                                        |
| GPU Memory Frequency (MHz)  | 1200                                        |
| Media Engine Freq (MHz)     | 1200                                        |
| GPU Core Temperature (C)    | 68                                          |
| GPU Memory Temperature (C)  | 74                                          |
| GPU Memory Read (kB/s)      | 512000                                      |
| GPU Memory Write (kB/s)     | 256000                                      |
| GPU Memory Bandwidth (%)    | 64                                          |
| GPU Memory Used (MiB)       | 14200                                       |
| GPU Memory Util (%)         | 86.5                                        |
| PCIe Read (kB/s)            | 12000                                       |
| PCIe Write (kB/s)           | 8000                                        |
+-----------------------------+---------------------------------------------+
"""
        st = intel_gpu._parse_stats(sample)
        self.assertAlmostEqual(st["power"], 255.4)
        self.assertEqual(st["freq_graphics"], 2400.0)
        self.assertEqual(st["freq_memory"], 1200.0)
        self.assertEqual(st["freq_media"], 1200.0)
        self.assertEqual(st["temp"], 68.0)
        self.assertEqual(st["temp_vram"], 74.0)
        self.assertEqual(st["util_compute"], 99.0)
        self.assertEqual(st["util_render"], 12.0)
        self.assertEqual(st["util_copy"], 5.0)
        self.assertEqual(st["util_media"], 0.0)
        self.assertAlmostEqual(st["eu_active"], 92.5)
        self.assertAlmostEqual(st["eu_stall"], 4.2)
        self.assertEqual(st["mem_bw"], 64.0)
        self.assertEqual(st["mem_read_kbps"], 512000.0)
        self.assertEqual(st["mem_write_kbps"], 256000.0)
        self.assertEqual(st["pcie_read_kbps"], 12000.0)
        self.assertEqual(st["pcie_write_kbps"], 8000.0)
        self.assertEqual(st["mem_used_mib"], 14200.0)
        self.assertAlmostEqual(st["mem_util"], 86.5)
        self.assertEqual(st["ras_reset"], 0.0)
        self.assertEqual(st["ras_driver_errors"], 1.0)
        self.assertEqual(st["ras_cache_corr"], 2.0)
        self.assertEqual(st["ras_mem_corr"], 3.0)
        self.assertEqual(st["ras_mem_uncorr"], 0.0)

    def test_health_thresholds_parsing(self):
        health_out = """
| 1. GPU Core Temperature      | Status: Ok                                  |
|                              | Throttle Threshold: 105 Celsius Degree      |
| 2. GPU Memory Temperature    | Status: Ok                                  |
|                              | Throttle Threshold: 85 Celsius Degree       |
| 3. GPU Power                 | Status: Ok                                  |
|                              | Throttle Threshold: 260 watts               |
"""
        with mock.patch.object(intel_gpu, "_run", return_value=health_out):
            hl = intel_gpu.health(0)
        self.assertEqual(hl["thermal_limit_c"], 105.0)
        self.assertEqual(hl["vram_thermal_limit_c"], 85.0)
        self.assertEqual(hl["power_throttle_w"], 260.0)

    def test_xe_freqs_sysfs_and_request_frequency(self):
        self.assertEqual(intel_gpu.classify("GPU Request Frequency (MHz)"), "freq_request")
        self.assertEqual(intel_gpu.classify("Media Engine Freq (MHz)"), "freq_media")
        self.assertEqual(intel_gpu.classify("Media Engine Request Freq (MHz)"), "freq_media_request")
        self.assertEqual(intel_gpu.classify("GPU Memory Frequency (MHz)"), "freq_memory")
        self.assertEqual(intel_gpu.classify("GPU Min Frequency(MHz)"), "")
        self.assertEqual(intel_gpu.classify("GPU Max Frequency(MHz)"), "")
        with tempfile.TemporaryDirectory() as td:
            bdf = "0000:43:00.0"
            gt0 = os.path.join(td, bdf, "tile0", "gt0", "freq0")
            th0 = os.path.join(gt0, "throttle")
            gt1 = os.path.join(td, bdf, "tile0", "gt1", "freq0")
            os.makedirs(th0)
            os.makedirs(gt1)
            for p, val in (
                (os.path.join(gt0, "act_freq"), "2450\n"),
                (os.path.join(gt0, "cur_freq"), "2600\n"),
                (os.path.join(th0, "reason_thermal"), "1\n"),
                (os.path.join(gt1, "act_freq"), "1200\n"),
                (os.path.join(gt1, "cur_freq"), "1350\n"),
            ):
                with open(p, "w", encoding="utf-8") as f:
                    f.write(val)
            xf = intel_gpu.xe_freqs(bdf, sysfs_pci_root=td)
        self.assertEqual(xf["freq_graphics"], 2450.0)
        self.assertEqual(xf["freq_request"], 2600.0)
        self.assertEqual(xf["freq_media"], 1200.0)
        self.assertEqual(xf["freq_media_request"], 1350.0)
        self.assertEqual(xf["throttle_thermal"], 1.0)

    def test_gpu_load_metrics_aggregates_new_intel_sensors(self):
        series = {
            "t": [0.0, 5.0, 12.0, 15.0, 20.0],
            "gpu0_temp": [40, 60, 70, 72, 71],
            "gpu0_power": [30, 250, 255, 256, 254],
            "gpu0_util": [5, 99, 100, 100, 99],
            "gpu0_util_compute": [0, 98, 99, 100, 99],
            "gpu0_fan_rpm": [1200, 2100, 2400, 2500, 2500],
            "gpu0_fan2_rpm": [1100, 2050, 2350, 2450, 2450],
            "gpu0_voltage_v": [0.75, 1.02, 1.04, 1.05, 1.04],
            "gpu0_mem_bw": [10, 62, 65, 66, 64],
            "gpu0_ras_mem_corr": [0, 0, 0, 1, 1],
            "gpu0_ras_mem_uncorr": [0, 0, 0, 0, 0],
        }
        m = gpu_stats.gpu_load_metrics(series, 0)
        self.assertEqual(m["gpu0_fan_rpm_max"], 2500.0)
        self.assertEqual(m["gpu0_fan2_rpm_max"], 2450.0)
        self.assertAlmostEqual(m["gpu0_voltage_max_v"], 1.05, places=3)
        self.assertEqual(m["gpu0_util_compute_max_pct"], 100.0)
        self.assertEqual(m["gpu0_mem_bw_max_pct"], 66.0)
        self.assertEqual(m["gpu0_ras_mem_corr"], 1)
        self.assertEqual(m["gpu0_ras_errors_total"], 1)


@unittest.skipUnless(shutil.which("node"), "Node.js requis pour tester le JS du rapport HTML")
class TestHtmlReportMetricTabs(unittest.TestCase):
    def test_no_debit_ram_duplicate_and_intel_tabs_present(self):
        fake_res = {
            "name": "GPU — Burn OpenCL puissance constante",
            "category": "GPU",
            "status": "completed",
            "duration_s": 30.0,
            "metrics": {
                "gpu0_mem_used_moy_mib": 12000.0,
                "gpu0_mem_used_max_mib": 12050.0,
                "oclburn_dev0_mem_gbs": 380.5,
                "gpu0_ras_mem_uncorr": 0,
                "series": {
                    "t": [0.0, 5.0, 10.0, 15.0],
                    "gpu0_temp": [45.0, 65.0, 70.0, 71.0],
                    "gpu0_power": [40.0, 250.0, 255.0, 254.0],
                    "gpu0_freq_graphics": [600.0, 2400.0, 2400.0, 2400.0],
                    "gpu0_util": [10.0, 99.0, 100.0, 100.0],
                    "gpu0_util_compute": [0.0, 98.0, 99.0, 100.0],
                    "gpu0_mem_util": [12.0, 80.0, 80.0, 80.0],
                    "gpu0_mem_used_mib": [1500.0, 12000.0, 12050.0, 12050.0],
                    "gpu0_fan_rpm": [1200.0, 2200.0, 2400.0, 2400.0],
                    "gpu0_voltage_v": [0.75, 1.03, 1.04, 1.04],
                },
            },
        }
        from core.benchmarks import BenchmarkResult
        br = BenchmarkResult(
            name=fake_res["name"],
            category=fake_res["category"],
            status=fake_res["status"],
            duration_s=fake_res["duration_s"],
            score=0.0,
            unit="",
            raw_output="",
            metrics=fake_res["metrics"],
        )
        with tempfile.TemporaryDirectory() as td:
            hpath = hr.export_html([br], td)
            with open(hpath, encoding="utf-8") as f:
                html = f.read()
            scripts = re.findall(r"<script(?![^>]*type=)[^>]*>(.*?)</script>", html, re.S)
            main_js = scripts[-1]
            m = re.search(r"(const METRIC_GROUPS=.*?function groupLabel\(result,g\)\{.*?\n)", main_js, re.S)
            self.assertIsNotNone(m)
            js_test = (
                "const payload = " + json.dumps({"system": {}, "results": [fake_res]}) + ";\n"
                + m.group(1) + "\n"
                + """
const r = payload.results[0];
const out = {
  tabs: applicableMetrics(r),
  ram_label: groupLabel(r, 'ram'),
  ram_series: groupSeries(r, 'ram').series.map(s => s.key),
  tput_series: groupSeries(r, 'throughput').series.map(s => s.key),
  tput_scalars: groupScalars(r, 'throughput').map(s => s.key),
  gpu_util_series: groupSeries(r, 'gpu_util').series.map(s => s.key),
  fan_series: groupSeries(r, 'fan').series.map(s => s.key),
  volt_series: groupSeries(r, 'voltage').series.map(s => s.key),
  err_scalars: groupScalars(r, 'errors').map(s => s.key),
};
console.log(JSON.stringify(out));
"""
            )
            p = os.path.join(td, "check.js")
            with open(p, "w", encoding="utf-8") as f:
                f.write(js_test)
            proc = subprocess.run(["node", p], capture_output=True, text=True, check=True)
            res = json.loads(proc.stdout)
        self.assertEqual(res["ram_label"], "VRAM utilisée")
        self.assertIn("series.gpu0_mem_used_mib", res["ram_series"])
        self.assertNotIn("series.gpu0_mem_used_mib", res["tput_series"])
        self.assertIn("oclburn_dev0_mem_gbs", res["tput_scalars"])
        self.assertIn("series.gpu0_util", res["gpu_util_series"])
        self.assertIn("series.gpu0_util_compute", res["gpu_util_series"])
        self.assertIn("series.gpu0_fan_rpm", res["fan_series"])
        self.assertIn("series.gpu0_voltage_v", res["volt_series"])
        self.assertIn("gpu0_ras_mem_uncorr", res["err_scalars"])


if __name__ == "__main__":
    unittest.main()
