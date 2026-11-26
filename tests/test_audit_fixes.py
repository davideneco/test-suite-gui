"""Tests unitaires de non-régression pour les corrections issues de la revue de code complète."""

import os
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import tests._mock_deps  # noqa: F401 — assure le stub psutil si absent


class TestExecutionAndSafetyFixes(unittest.TestCase):
    """Vérifie les corrections critiques sur GPU, réseau, FIO et stats."""

    def test_gpu_fryer_has_fault_watch_and_respects_stop_and_rc(self):
        from core import bench_nvidia
        # Décoré par @with_fault_watch
        self.assertTrue(hasattr(bench_nvidia.bench_gpu_fryer, "__wrapped__"))

        with tempfile.TemporaryDirectory() as tmp:
            # 1. Faux gpu-fryer qui échoue avec rc=1 sans message NVML
            fail_bin = os.path.join(tmp, "gpu-fryer-fail")
            with open(fail_bin, "w", encoding="utf-8") as fh:
                fh.write(f"#!{sys.executable}\nimport sys; print('CUDA error'); sys.exit(1)\n")
            os.chmod(fail_bin, 0o755)

            with mock.patch.object(bench_nvidia, "_find_tool_path", return_value=fail_bin), \
                 mock.patch.object(bench_nvidia, "_poll_gpu_sensors", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "_log_window_wait", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "LOG_PRE_SECONDS", 0), \
                 mock.patch.object(bench_nvidia, "LOG_POST_SECONDS", 0), \
                 mock.patch("core.telemetry_aggregator.start_background_telemetry", lambda *a, **kw: None):
                r_err = bench_nvidia.bench_gpu_fryer.__wrapped__(time_s=5)
                self.assertEqual(r_err.status, "error")

            # 2. Faux gpu-fryer arrêté par kill_event → status="stopped"
            slow_bin = os.path.join(tmp, "gpu-fryer-slow")
            with open(slow_bin, "w", encoding="utf-8") as fh:
                fh.write(f"#!{sys.executable}\nimport time; time.sleep(30)\n")
            os.chmod(slow_bin, 0o755)

            ev = threading.Event()
            def stop_soon():
                import time
                time.sleep(0.25)
                ev.set()
            threading.Thread(target=stop_soon, daemon=True).start()

            with mock.patch.object(bench_nvidia, "_find_tool_path", return_value=slow_bin), \
                 mock.patch.object(bench_nvidia, "_poll_gpu_sensors", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "_log_window_wait", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "LOG_PRE_SECONDS", 0), \
                 mock.patch.object(bench_nvidia, "LOG_POST_SECONDS", 0), \
                 mock.patch("core.telemetry_aggregator.start_background_telemetry", lambda *a, **kw: None):
                r_stop = bench_nvidia.bench_gpu_fryer.__wrapped__(time_s=30, kill_event=ev)
                self.assertEqual(r_stop.status, "stopped")

    def test_nvbandwidth_large_output_no_pipe_deadlock_and_sets_command(self):
        from core import bench_nvidia
        with tempfile.TemporaryDirectory() as tmp:
            fake_nvb = os.path.join(tmp, "nvbandwidth")
            # Émet > 120 Ko (> tampon pipe 64 Ko) avant de sortir
            with open(fake_nvb, "w", encoding="utf-8") as fh:
                fh.write(
                    f"#!{sys.executable}\n"
                    "import json\n"
                    "print('#' * 130000)\n"
                    "print(json.dumps({'testcases': [{'name': 'device_local_copy', 'results': {'average': 912.5}}]}))\n"
                )
            os.chmod(fake_nvb, 0o755)

            with mock.patch.object(bench_nvidia, "_find_tool_path", return_value=fake_nvb), \
                 mock.patch.object(bench_nvidia, "_poll_gpu_sensors", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "_log_window_wait", lambda *a, **kw: None), \
                 mock.patch.object(bench_nvidia, "LOG_PRE_SECONDS", 0), \
                 mock.patch.object(bench_nvidia, "LOG_POST_SECONDS", 0):
                res = bench_nvidia.bench_gpu_nvbandwidth.__wrapped__(iterations=1)
                self.assertEqual(res.status, "ok")
                self.assertEqual(res.score, 912.5)
                self.assertTrue(bool(res.command))
                self.assertIn("nvbandwidth", res.metrics.get("command", ""))

    def test_iperf3_large_output_no_pipe_deadlock(self):
        from core import bench_net
        rc, out, err, killed = bench_net._iperf3_run(
            [sys.executable, "-c", "print('A' * 130000)"],
            time_s=5,
        )
        self.assertEqual(rc, 0)
        self.assertFalse(killed)
        self.assertGreaterEqual(len(out), 130000)

    def test_vram_bandwidth_decorated_and_oclburn_errors_fail(self):
        from core import bench_compute
        self.assertTrue(hasattr(bench_compute.bench_gpu_vram_bandwidth, "__wrapped__"))

        # Simulation oclburn avec erreurs de calcul → status doit être "error"
        fake_out = (
            "START dev=0 name=FakeGPU vram_pct=90\n"
            "RESULT dev=0 name=FakeGPU fp32_gflops=10000 compute_errors=3 memory_errors=0\n"
        )
        with mock.patch.object(bench_compute, "_oclburn_binary", return_value=("/bin/true", "")), \
             mock.patch.object(bench_compute, "_opencl_icd_present", return_value=True), \
             mock.patch.object(bench_compute, "_oclburn_devices", return_value=[{"idx": 0, "name": "FakeGPU", "pci": ""}]), \
             mock.patch.object(bench_compute, "_stream_peak_tool", return_value=(0, fake_out, {"t": [0, 1]}, 2.0, False)):
            res = bench_compute.bench_gpu_oclburn.__wrapped__(time_s=10)
            self.assertEqual(res.status, "error")
            self.assertEqual(res.metrics.get("compute_errors"), 3)

    def test_parse_fio_cli_directory_and_spaced_options(self):
        from core import fio_runner
        # --directory=/ doit être extrait dans targets
        p1 = fio_runner._parse_fio_cli("fio --name=j --directory=/ --rw=write --size=1G")
        self.assertEqual(p1["error"], "")
        self.assertIn("/", p1["targets"])
        self.assertTrue(p1["writes"])

        # --directory / (séparé par espace) et options espacées --bs 4k --size 1G
        p2 = fio_runner._parse_fio_cli("fio --name j --bs 4k --size 1G --directory /mnt/data --rw randwrite")
        self.assertEqual(p2["error"], "")
        self.assertEqual(p2["targets"], ["/mnt/data"])
        self.assertTrue(p2["writes"])

        # Fichier de job avec directory=/
        with tempfile.NamedTemporaryFile("w", delete=False, encoding="utf-8") as jf:
            jf.write("[job]\ndirectory=/\nrw=write\n")
            jf_path = jf.name
        try:
            p3 = fio_runner._parse_fio_cli(f"fio {jf_path}")
            self.assertEqual(p3["error"], "")
            self.assertIn("/", p3["targets"])
            self.assertTrue(p3["writes"])
        finally:
            os.remove(jf_path)

    def test_gpu_stats_handles_all_none_series(self):
        from core.gpu_stats import gpu_load_metrics
        series = {
            "t": [0, 1, 2],
            "gpu0_temp": [None, "N/A", None],
            "gpu0_temp_hotspot": [None, None, None],
            "gpu0_temp_vram": [None, None, None],
            "gpu0_power": [None, None, None],
        }
        out = gpu_load_metrics(series, 0)
        self.assertIsInstance(out, dict)
        self.assertNotIn("gpu0_temp_max_c", out)


class TestExportsCliAndViewerFixes(unittest.TestCase):
    """Vérifie les corrections HTML, CLI, Fleet, Windows Viewer, csv2html et .bat."""

    def test_html_light_theme_preserves_closing_head(self):
        from exporters import html_report
        tpl = "<html><head><style>\nbody { color: #fff; }\n</style></head><body>OK</body></html>"
        out = html_report._add_light_theme(tpl)
        self.assertIn("</head>", out)
        self.assertIn('id="lightCss"', out)
        self.assertLess(out.index('id="lightCss"'), out.index("</head>"))

    def test_latest_session_finds_new_and_legacy_dirs(self):
        from clitui.run import latest_session
        from core import cli_session as cs
        with tempfile.TemporaryDirectory() as tmp:
            sdir = os.path.join(tmp, "BenchSuite_v1.0.19_2026-11-26_12-00-00_srv_session")
            os.makedirs(sdir)
            cs.save(sdir, [], None, {"dir": "run1"})
            found = latest_session(tmp)
            self.assertEqual(found, sdir)

    def test_fleet_finds_session_report_json(self):
        import json
        from core.fleet import _latest_reports
        with tempfile.TemporaryDirectory() as tmp:
            sdir = os.path.join(tmp, "BenchSuite_v1.0.19_2026-11-26_srv_session")
            os.makedirs(sdir)
            rpath = os.path.join(sdir, "benchsuite_report.json")
            with open(rpath, "w", encoding="utf-8") as fh:
                json.dump({"exported_at": "2026-11-26T12:00:00", "metadata": {"serial": "SN-01"}}, fh)
            found = _latest_reports(tmp)
            self.assertEqual(found.get("SN-01"), rpath)

    def test_clitui_choices_include_oclburn_power(self):
        from clitui import model
        self.assertIn("power", model._CHOICES["mode"])
        self.assertIn("power", model._CHOICES["verification"])
        self.assertNotIn("tensor", model._CHOICES["precision"])

    def test_windows_viewer_js_syntax_and_sequence_channel_key(self):
        from windows_viewer.benchsuite_viewer import build_viewer_html
        html = build_viewer_html()
        self.assertIn("c.v || c.values", html)
        self.assertIn("function escJs", html)
        # Vérification syntaxe JS réelle avec node --check si présent
        scripts = re.findall(r"<script>(.*?)</script>", html, re.S)
        self.assertTrue(scripts)
        js_code = "\n".join(scripts)
        res = subprocess.run(["node", "--check"], input=js_code, text=True, capture_output=True, timeout=10)
        self.assertEqual(res.returncode, 0, f"Erreur JS windows_viewer : {res.stderr}")

    def test_csv2html_zero_syntax_warning_and_bat_for_d(self):
        res = subprocess.run(
            [sys.executable, "-W", "error::SyntaxWarning", "-m", "py_compile", os.path.join(ROOT, "csv2html.py")],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(res.returncode, 0, f"SyntaxWarning dans csv2html.py : {res.stderr}")

        for rel in ("Lancer_csv2html.bat", "windows_viewer/Lancer_csv2html.bat", "windows_viewer/Lancer_BenchSuite_Viewer.bat"):
            txt = Path(ROOT, rel).read_text(encoding="utf-8")
            self.assertIn("for /D %%D in", txt, f"for /D manquant dans {rel}")


if __name__ == "__main__":
    unittest.main()
