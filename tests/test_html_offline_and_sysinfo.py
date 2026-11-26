# -*- coding: utf-8 -*-
"""Tests unitaires pour Chart.js hors ligne (HTML) et les sections incomplètes (Infos système)."""

import os
import re
import tempfile
import unittest
from unittest import mock

from tests._mock_deps import ensure_psutil_stub

ensure_psutil_stub()

import core.system_info as si
import exporters.html_report as hr
from core.result import BenchmarkResult


class TestHtmlOfflineChartJs(unittest.TestCase):
    """Vérifie l'intégration locale de Chart.js et du plugin zoom dans les rapports HTML."""

    def test_export_html_embeds_chartjs_and_zoom_offline(self):
        res = BenchmarkResult(
            name="CPU stress",
            category="CPU",
            score=100.0,
            unit="bogo ops/s",
            duration_s=5.0,
            status="ok",
            raw_output="ok",
            metrics={"series": {"t": [0, 1, 2], "cpu_temp_c": [50.0, 55.0, 58.0]}},
        )
        with tempfile.TemporaryDirectory() as td:
            html_path = hr.export_html([res], td, report_meta={"system_name": "AirgapSrv"})
            self.assertTrue(os.path.isfile(html_path))
            with open(html_path, encoding="utf-8") as f:
                txt = f.read()

            # Les bibliothèques doivent être embarquées en local, sans URL CDN Chart.js/zoom
            self.assertNotIn("cdnjs.cloudflare.com/ajax/libs/Chart.js", txt)
            self.assertNotIn("cdn.jsdelivr.net/npm/hammerjs", txt)
            self.assertNotIn("cdn.jsdelivr.net/npm/chartjs-plugin-zoom", txt)
            self.assertIn("Chart.register", txt)
            self.assertIn('id="payload"', txt)
            self.assertIn("limits:{x:{min:'original',max:'original'", txt)
            self.assertIn("onZoomStart", txt)
            self.assertIn("threshold:5", txt)

            # Le ré-export doit aussi conserver Chart.js embarqué hors ligne
            reexp_path = os.path.join(td, "reexp.html")
            hr.reexport_html(html_path, reexp_path)
            with open(reexp_path, encoding="utf-8") as f:
                re_txt = f.read()
            self.assertNotIn("cdnjs.cloudflare.com/ajax/libs/Chart.js", re_txt)
            self.assertIn('id="payload"', re_txt)

    def test_chart_scripts_fallback_to_cdn_when_local_missing(self):
        with mock.patch.object(hr, "_CHARTJS_SCRIPTS_CACHE", None), \
             mock.patch("os.path.isfile", return_value=False):
            tags = hr._chart_scripts_html()
        self.assertIn("https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js", tags)
        self.assertIn("https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js", tags)
        self.assertEqual(len(re.findall(r"<script src=", tags)), 3)


class TestSystemInfoIncompleteAudit(unittest.TestCase):
    """Vérifie la remontée des sections incomplètes dans l'audit matériel."""

    def test_run_hardware_audit_flags_incomplete_sections(self):
        data = {
            "ram": {},
            "gpus": [],
            "stockage": [],
            "pcie": [],
            "sections_incompletes": [{"section": "pmbus", "budget_s": 25}, {"section": "bmc", "budget_s": 45}],
        }
        alerts = si.run_hardware_audit(data)
        titles = [a.get("title", "") for a in alerts]
        self.assertTrue(any("pmbus" in t and "25 s" in t for t in titles))
        self.assertTrue(any("bmc" in t and "45 s" in t for t in titles))


class TestCsv2Html(unittest.TestCase):
    """Vérifie la conversion CSV vers HTML par csv2html.py."""

    def test_csv2html_generates_interactive_report(self):
        import csv2html
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "capteurs.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("Time;Puissance;Température\n0;120 W;45 °C\n1;150 W;52 °C\n2;180 W;60 °C\n")
            rc = csv2html.main([csv_path, "-o", td, "-s", "Serveur Test"])
            self.assertEqual(rc, 0)
            target = os.path.join(td, "capteurs_rapport.html")
            self.assertTrue(os.path.isfile(target))
            with open(target, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("Serveur Test", content)
            self.assertIn("Puissance", content)

    def test_csv2html_autodetect_and_psu_synthesis(self):
        import csv2html
        orig_cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as td:
            pin_file = os.path.join(td, "pin_20260825T012513.csv")
            pout_file = os.path.join(td, "pout_20260825T012513.csv")
            temp_file = os.path.join(td, "temp1_20260825T012513.csv")
            with open(pin_file, "w", encoding="utf-8") as f:
                f.write("Time,Value\n0,500.0\n1,505.0\n2,502.0\n")
            with open(pout_file, "w", encoding="utf-8") as f:
                f.write("Time,Value\n0,470.0\n1,475.0\n2,472.0\n")
            with open(temp_file, "w", encoding="utf-8") as f:
                f.write("Time,Value\n0,42.0\n1,43.5\n2,44.0\n")
            try:
                os.chdir(td)
                # Test sans argument (détection automatique de *.csv dans le dossier courant)
                rc = csv2html.main([])
                self.assertEqual(rc, 0)
                html_files = [f for f in os.listdir(td) if f.endswith(".html")]
                self.assertEqual(len(html_files), 1)
                with open(os.path.join(td, html_files[0]), encoding="utf-8") as f:
                    html = f.read()
                self.assertIn("Synthèse par alimentation", html)
                self.assertIn("Pin", html)
                self.assertIn("Pout", html)
            finally:
                os.chdir(orig_cwd)

    def test_csv2html_standalone_export(self):
        import csv2html
        res = BenchmarkResult(
            name="Alimentation — Synthèse globale",
            category="Alimentation & Redondance",
            score=500.0,
            unit="W",
            duration_s=10.0,
            raw_output="",
            metrics={
                "series": {"t": [0, 1, 2], "power_c1": [500.0, 502.0, 501.0]},
                "series_labels": {"power_c1": "Pin"},
                "pin_max_w": 502.0,
                "pin_moy_w": 501.0,
            },
            status="ok",
        )
        with tempfile.TemporaryDirectory() as td:
            out_file = csv2html.export_html_standalone([res], td, {"system_name": "TestStandAlone"}, [res])
            self.assertTrue(os.path.isfile(out_file))
            with open(out_file, encoding="utf-8") as f:
                txt = f.read()
            self.assertIn("TestStandAlone", txt)
            self.assertIn("Chart.register", txt)
            self.assertIn("mainChart", txt)

        # Vérifie le repli CDN lorsque third_party/chartjs n'est pas à côté de csv2html.py (cas Windows isolé)
        with mock.patch.object(csv2html, "_CHARTJS_SCRIPTS_CACHE", None), \
             mock.patch("os.path.isfile", return_value=False):
            tags = csv2html._chart_scripts_html()
        self.assertIn("Chart.js/4.4.1/chart.umd.min.js", tags)
        self.assertIn("hammerjs@2.0.8", tags)
        self.assertIn("chartjs-plugin-zoom@2.0.1", tags)

    def test_csv2html_zoom_and_copy_features(self):
        import csv2html
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "zoom_test.csv")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("Time,Pin,Temp\n0,100,40\n1,200,45\n2,300,50\n3,250,48\n")
            rc = csv2html.main([csv_path, "-o", td, "-s", "ZoomSrv"])
            self.assertEqual(rc, 0)
            target = os.path.join(td, "zoom_test_rapport.html")
            self.assertTrue(os.path.isfile(target))
            with open(target, encoding="utf-8") as f:
                html = f.read()
            for marker in (
                "resetZoomBtn",
                "copyChartBtn",
                "chartStatsGrid",
                "updateZoomStats",
                "resetChartZoom",
                "copyChartImage",
                "legendRow",
                "chartQuickFilters",
                "applyLegendVisibility",
                "qfAll",
                "qfPower",
                "qfTemp",
                "qfNone",
                "limits:",
                "minZoomSpan",
                "threshold: 5",
                "onZoomStart",
                "onZoomComplete",
                "onPanComplete",
                "modifierKey: \"ctrl\"",
                "ondblclick",
                "chartjs-plugin-zoom v2.0.1",
            ):
                self.assertIn(marker, html)

    def test_csv2html_large_dataset_decimation(self):
        import csv2html
        with tempfile.TemporaryDirectory() as td:
            csv_path = os.path.join(td, "large_psu.csv")
            n = 50000
            # Génération d'un CSV de 50 000 lignes avec pic et creux
            lines = ["Time,Pin,Pout"]
            for i in range(n):
                val_in = 500.0
                val_out = 460.0
                if i == 12345:
                    val_in = 1234.5  # Pic de surtension/puissance
                elif i == 34567:
                    val_in = 120.0   # Creux de tension/puissance
                lines.append(f"{i},{val_in},{val_out}")
            with open(csv_path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")

            rc = csv2html.main([csv_path, "-o", td, "-s", "Serveur BigData", "--max-points", "1200"])
            self.assertEqual(rc, 0)
            target = os.path.join(td, "large_psu_rapport.html")
            self.assertTrue(os.path.isfile(target))

            # Vérifier la taille du fichier (ultra-léger < 400 Ko malgré 50 000 échantillons)
            sz_kb = os.path.getsize(target) / 1024
            self.assertLess(sz_kb, 400, f"Le fichier HTML est trop lourd ({sz_kb:.1f} Ko) pour un affichage fluide")

            with open(target, encoding="utf-8") as f:
                txt = f.read()

            # Vérifier que les statistiques exactes sont conservées
            self.assertIn("1 234.5", txt)  # Pin max exact
            self.assertIn("120.0", txt)    # Pin min exact
            self.assertIn("50 000", txt)   # Nombre total d'échantillons exact

            # Vérifier dans le payload que la décimation est effective tout en préservant les pics
            import json, re
            m = re.search(r'<script type="application/json" id="payload">(.*?)</script>', txt, re.S)
            self.assertIsNotNone(m)
            data = json.loads(m.group(1))
            res = data["results"][0]
            pts = len(res["metrics"]["series"]["t"])
            self.assertLessEqual(pts, 1500)
            pin_series = res["metrics"]["series"]["power_c2"]
            self.assertEqual(max(pin_series), 1234.5)
            self.assertEqual(min(pin_series), 120.0)


class TestReportFolderNaming(unittest.TestCase):
    """Vérifie le format standardisé des dossiers de résultats créés après les tests."""

    def test_format_report_dir_name_and_export_all(self):
        from datetime import datetime
        import exporters.export as ex
        from core import campaign as cp

        dt = datetime(2026, 11, 19, 1, 22, 33)
        name = ex.format_report_dir_name({"system_name": "Mon Serveur 01"}, dt=dt)
        self.assertEqual(name, f"BenchSuite_v{ex.APP_VERSION}_2026-11-19_01-22-33_Mon_Serveur_01")

        # Vérifie que export_all et make_run_dir utilisent bien ce format
        res = BenchmarkResult(
            name="CPU stress",
            category="CPU",
            score=100.0,
            unit="bogo ops/s",
            duration_s=1.0,
            status="ok",
            raw_output="ok",
        )
        with tempfile.TemporaryDirectory() as td:
            paths = ex.export_all([res], td, report_meta={"system_name": "Srv-Test"})
            folder_name = os.path.basename(paths["dossier"])
            self.assertRegex(
                folder_name,
                rf"^BenchSuite_v{re.escape(ex.APP_VERSION)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}_Srv-Test$",
            )
            run_dir = cp.make_run_dir(td, {"system_name": "Srv-Test", "serial": "SN42"}, "sequence")
            self.assertRegex(
                os.path.basename(run_dir),
                rf"^BenchSuite_v{re.escape(ex.APP_VERSION)}_\d{{4}}-\d{{2}}-\d{{2}}_\d{{2}}-\d{{2}}-\d{{2}}_Srv-Test_sequence$",
            )


if __name__ == "__main__":
    unittest.main()
