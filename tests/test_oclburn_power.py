# -*- coding: utf-8 -*-
"""Tests unitaires pour le mode puissance constante (-M power) de oclburn et sa suite dédiée."""

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tests._mock_deps import ensure_psutil_stub

ensure_psutil_stub()

import core.bench_compute as bc
from core.runner import load_suite


class TestOclburnPowerMode(unittest.TestCase):
    """Vérifie la compilation d'oclburn, le mode -M power et les métriques de stabilité."""

    def test_oclburn_source_compiles_cleanly(self):
        gcc = shutil.which("gcc") or shutil.which("cc")
        if not gcc:
            self.skipTest("gcc absent de l'environnement")
        with tempfile.TemporaryDirectory() as td:
            out_bin = os.path.join(td, "oclburn_test")
            p = subprocess.run(
                [gcc, "-Wall", "-Wextra", "-O2", "-pthread", bc._OCLBURN_SRC, "-o", out_bin, "-ldl"],
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(p.returncode, 0, f"Échec compilation oclburn.c : {p.stderr}")
            self.assertEqual(p.stderr.strip(), "", f"Avertissements gcc inattendus : {p.stderr}")
            self.assertTrue(os.path.isfile(out_bin))

    def test_bench_gpu_oclburn_power_mode_and_stability_metrics(self):
        fake_devs = [{"idx": "0", "name": "Intel(R) Arc(TM) Pro B70 Graphics", "pci": "0000:43:00.0"}]
        fake_out = (
            'START dev=0 name="Intel(R) Arc(TM) Pro B70 Graphics" mode=power duration=30 vram_pct=90 pci=0000:43:00.0\n'
            'STAT t=5.0 dev=0 fp32_gflops=21500.0 fp16_gflops=0.0 giops=0.0 mem_gbs=0.0 compute_errors=0 memory_errors=0\n'
            'STAT t=10.0 dev=0 fp32_gflops=21480.0 fp16_gflops=0.0 giops=0.0 mem_gbs=0.0 compute_errors=0 memory_errors=0\n'
            'STAT t=15.0 dev=0 fp32_gflops=21510.0 fp16_gflops=0.0 giops=0.0 mem_gbs=0.0 compute_errors=0 memory_errors=0\n'
            'STAT t=20.0 dev=0 fp32_gflops=21495.0 fp16_gflops=0.0 giops=0.0 mem_gbs=0.0 compute_errors=0 memory_errors=0\n'
            'RESULT dev=0 name="Intel(R) Arc(TM) Pro B70 Graphics" t=30.0 fp32_gflops=21500.0 fp16_gflops=0.0 '
            'giops=0.0 mem_gbs=0.0 mem_tested_mb=0 compute_errors=0 memory_errors=0 launches=200 passes=0\n'
            'SUMMARY status=PASS errors=0 devices=1 duration=30.0\n'
        )
        fake_series = {
            "t": [0, 5, 10, 12, 14, 16, 18, 20],
            "gpu0_power": [60.0, 290.0, 292.0, 293.0, 291.5, 292.5, 292.0, 293.0],
            "gpu0_temp": [35.0, 55.0, 60.0, 61.0, 61.5, 62.0, 62.0, 62.0],
        }
        fake_gpu_info = [{"index": "0", "pci_bus": "0000:43:00.0", "puissance_max_w": "290"}]

        captured_cmd = []

        def fake_stream(cmd, *args, **kwargs):
            captured_cmd.extend(cmd)
            return 0, fake_out, fake_series, 30.0, False

        with mock.patch.dict(os.environ, {"BENCHSUITE_NO_FAULT_WATCH": "1"}), \
             mock.patch.object(bc, "_oclburn_binary", return_value=("/fake/oclburn", "")), \
             mock.patch.object(bc, "_opencl_icd_present", return_value=True), \
             mock.patch.object(bc, "_oclburn_devices", return_value=fake_devs), \
             mock.patch.object(bc, "_stream_peak_tool", side_effect=fake_stream), \
             mock.patch.object(bc, "_gpu_power_limit_w", return_value=290.0), \
             mock.patch("core.system_info.get_gpu_info", return_value=fake_gpu_info), \
             mock.patch.object(bc, "get_system_info", return_value={}):
            res = bc.bench_gpu_oclburn(time_s=30, mode="power")

        self.assertEqual(res.status, "ok")
        self.assertEqual(res.name, "GPU — Burn OpenCL puissance constante")
        self.assertIn("-M", captured_cmd)
        self.assertEqual(captured_cmd[captured_cmd.index("-M") + 1], "power")
        self.assertEqual(res.metrics["compute_errors"], 0)
        self.assertEqual(res.metrics["oclburn_mode"], "power")
        self.assertEqual(res.metrics["gpu0_power_cap_w"], 290.0)
        self.assertAlmostEqual(res.metrics["gpu0_power_med_w"], 292.2, delta=0.5)
        self.assertEqual(res.metrics["gpu0_power_min_load_w"], 291.5)
        self.assertLess(res.metrics["gpu0_power_variation_pct"], 1.0)

    def test_suite_burn_gpu_puissance_stable_valid(self):
        suite = load_suite("burn_gpu_puissance_stable")
        self.assertEqual(suite["steps"][0]["bench"], "gpu_oclburn")
        self.assertEqual(suite["steps"][0]["params"]["mode"], "power")


if __name__ == "__main__":
    unittest.main()
