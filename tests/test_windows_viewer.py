# -*- coding: utf-8 -*-
"""Tests unitaires pour l'outil compagnon Windows (windows_viewer/benchsuite_viewer.py)."""

import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from windows_viewer.benchsuite_viewer import (
    DB_SCHEMA_VERSION,
    VIEWER_VERSION,
    BenchSuiteDB,
    export_static_catalog_html,
    make_handler,
)


def _sample_report(system_name: str, serial: str, verdict_status: str, legacy_gpu_key: bool = False) -> dict:
    gpu_block = [{"index": 0, "nom": "NVIDIA H200"}]
    snap = {
        "os": {"distro": "Ubuntu 24.04"},
        "cpu": {"modele": "AMD EPYC 9654"},
        "ram": {"total_go": 256.0},
        "sections_incompletes": [{"section": "pmbus", "budget_s": 30}] if verdict_status == "A_VERIFIER" else [],
    }
    snap["gpu" if legacy_gpu_key else "gpus"] = gpu_block
    return {
        "benchsuite_version": "1.0.4",
        "exported_at": "2026-11-16T10:15:00",
        "metadata": {
            "system_name": system_name,
            "model": "SuperServer 4U",
            "serial": serial,
            "client": "ClientTest",
            "order_no": "CMD-42",
            "operator": "OpA",
        },
        "system": {"name": system_name, "snapshot_at_start": snap},
        "summary": {"bench_count": 2, "ok": 1, "failed": 1 if verdict_status == "NON_CONFORME" else 0, "stopped": 0, "skipped": 0},
        "telemetry_summary": {
            "gpus": [
                {
                    "gpu_index": 0,
                    "label": "GPU 0",
                    "temp_core_max_c": 74.0,
                    "temp_hotspot_max_c": 88.5,
                    "temp_vram_max_c": 78.0,
                    "throttle_detected": verdict_status == "A_VERIFIER",
                    "throttle_samples": 4 if verdict_status == "A_VERIFIER" else 0,
                }
            ]
        },
        "verdict": {
            "status": verdict_status,
            "label": "Non conforme" if verdict_status == "NON_CONFORME" else "À vérifier",
            "rules": [{"level": "fail" if verdict_status == "NON_CONFORME" else "warn", "label": "Test règle", "detail": "Détail test"}],
        },
        "results": [
            {
                "name": "GPU gpu-burn (30 min)",
                "category": "gpu",
                "status": "error" if verdict_status == "NON_CONFORME" else "ok",
                "score": 32000.0,
                "unit": "Gflop/s",
                "duration_s": 1800.0,
                "timestamp": "2026-11-16T10:00:00",
                "notes": "Xid détecté" if verdict_status == "NON_CONFORME" else "",
                "metrics": {
                    "command": "gpu-burn 1800",
                    "hardware_events": [{"severity": "critical", "category": "GPU NVIDIA Xid", "line": "NVRM: Xid 79"}]
                    if verdict_status == "NON_CONFORME"
                    else [],
                    "series": {"t": [0, 1, 2], "gpu0_temp": [50.0, 62.0, 74.0]},
                },
            }
        ],
    }


class TestWindowsViewerDBAndServer(unittest.TestCase):
    """Vérifie l'indexation SQLite incrémentale, le multi-format et le serveur HTTP local."""

    def test_scan_db_companions_and_corrupt_tolerance(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d1 = root / "srv1"
            d1.mkdir()
            (d1 / "benchsuite_report_20261116_101500.json").write_text(
                json.dumps(_sample_report("SRV-01", "SN-001", "NON_CONFORME")), encoding="utf-8"
            )
            (d1 / "benchsuite_report_20261116_101500.html").write_text("<html><body>OK</body></html>", encoding="utf-8")
            (d1 / "benchsuite_report_20261116_101500.pdf").write_bytes(b"%PDF-1.4\n")
            (d1 / "benchsuite_report_20261116_101500.csv").write_bytes(
                "section;item;key;value\nmetadata;system_name;;SRV-01\nTempérature;;max;88,5 °C\n".encode("cp1252")
            )
            (d1 / "dmesg_20261116_101500.txt").write_text(
                "BenchSuite - dmesg\nErreurs dmesg detectees: 1\n[12.3] NVRM: Xid (PCI:0000:07:00): 79\n", encoding="utf-8"
            )
            # Ancien format ("gpu" au lieu de "gpus") + JSON corrompu ignoré sans crash
            d2 = root / "srv2"
            d2.mkdir()
            (d2 / "benchsuite_report_20261115_090000.json").write_text(
                json.dumps(_sample_report("SRV-02", "SN-002", "A_VERIFIER", legacy_gpu_key=True)), encoding="utf-8"
            )
            (d2 / "benchsuite_report_broken.json").write_text("{json cassé", encoding="utf-8")
            (root / "fleet_summary.csv").write_text("machine;verdict\nSRV-01;NON_CONFORME\n", encoding="utf-8")

            db = BenchSuiteDB(root)
            s1 = db.scan()
            self.assertEqual(s1["updated_reports"], 2)
            self.assertEqual(s1["skipped_corrupt"], ["srv2/benchsuite_report_broken.json"])

            ov = db.get_overview()
            self.assertEqual(ov["stats"]["total_reports"], 2)
            self.assertEqual(ov["stats"]["non_conforme"], 1)
            self.assertEqual(ov["stats"]["a_verifier"], 1)
            self.assertEqual(ov["stats"]["total_files"], 8)

            srv1 = [r for r in ov["reports"] if r["system_name"] == "SRV-01"][0]
            self.assertEqual(srv1["critical_faults_count"], 1)
            self.assertEqual(srv1["html_rel_path"], "srv1/benchsuite_report_20261116_101500.html")
            self.assertEqual(srv1["pdf_rel_path"], "srv1/benchsuite_report_20261116_101500.pdf")
            self.assertEqual(srv1["csv_rel_path"], "srv1/benchsuite_report_20261116_101500.csv")
            self.assertEqual(srv1["dmesg_rel_path"], "srv1/dmesg_20261116_101500.txt")

            # Deuxième scan sans modification : 0 rapport ré-indexé (incrémental)
            s2 = db.scan()
            self.assertEqual(s2["updated_reports"], 0)
            self.assertEqual(s2["updated_files"], 0)

            # Vérifier la BDD SQLite directement
            with sqlite3.connect(str(db.db_path)) as conn:
                cnt_b = conn.execute("SELECT COUNT(*) FROM benchmarks").fetchone()[0]
                self.assertEqual(cnt_b, 2)
                meta_rows = dict(conn.execute("SELECT key, value FROM meta_db").fetchall())
                self.assertEqual(meta_rows.get("schema_version"), str(DB_SCHEMA_VERSION))
                self.assertEqual(meta_rows.get("viewer_version"), VIEWER_VERSION)

            # Catalogue HTML autonome
            cat_html = export_static_catalog_html(db)
            self.assertIn("SRV-01", cat_html)
            self.assertIn("SN-002", cat_html)
            self.assertIn(f"Viewer v{VIEWER_VERSION}", cat_html)
            self.assertIn(f"BDD schéma v{DB_SCHEMA_VERSION}", cat_html)

    def test_http_server_endpoints_and_path_traversal_blocked(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            outside = root.parent / "secret_outside.txt"
            outside.write_text("SECRET", encoding="utf-8")
            try:
                (root / "benchsuite_report_1.json").write_text(
                    json.dumps(_sample_report("SRV-HTTP", "SN-99", "NON_CONFORME")), encoding="utf-8"
                )
                (root / "rapport.csv").write_bytes("col1;col2\nTempérature;75 °C\n".encode("cp1252"))

                db = BenchSuiteDB(root)
                db.scan()

                server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(db))
                port = server.server_address[1]
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                try:
                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/") as r:
                        html_doc = r.read().decode("utf-8")
                        self.assertIn("BenchSuite Viewer", html_doc)
                        self.assertIn(f"v{VIEWER_VERSION} · BDD v{DB_SCHEMA_VERSION}", html_doc)
                        # Vérifie l'intégration Chart.js locale si third_party/chartjs est présent
                        self.assertIn("new Chart(", html_doc)

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/overview?q=SN-99") as r:
                        ov = json.loads(r.read().decode("utf-8"))
                        self.assertEqual(ov["viewer_version"], VIEWER_VERSION)
                        self.assertEqual(ov["schema_version"], DB_SCHEMA_VERSION)
                        self.assertEqual(len(ov["reports"]), 1)
                        self.assertEqual(len(ov["machines"]), 1)
                        rid = ov["reports"][0]["id"]
                        mkey = ov["machines"][0]["machine_key"]

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/machine?key={mkey}") as r:
                        mdet = json.loads(r.read().decode("utf-8"))
                        self.assertEqual(mdet["machine"]["system_name"], "SRV-HTTP")
                        self.assertTrue(all(f["machine_key"] == mkey or f["report_id"] == rid for f in mdet["files"]))

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/report/{rid}") as r:
                        det = json.loads(r.read().decode("utf-8"))
                        self.assertEqual(det["report"]["serial"], "SN-99")
                        self.assertEqual(len(det["benchmarks"]), 1)

                    with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/preview?path=rapport.csv") as r:
                        prev = json.loads(r.read().decode("utf-8"))
                        self.assertEqual(prev["mode"], "csv")
                        self.assertEqual(prev["delimiter"], ";")
                        self.assertEqual(prev["rows"][0][0], "Température")

                    # Protection traversée de chemin hors dossier racine
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(f"http://127.0.0.1:{port}/api/file?path=../secret_outside.txt")
                    self.assertEqual(ctx.exception.code, 404)
                finally:
                    server.shutdown()
                    server.server_close()
            finally:
                if outside.exists():
                    outside.unlink()

    def test_strict_file_isolation_between_machines_in_flat_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Deux machines exportées dans le même dossier plat avec timestamps distincts
            (root / "benchsuite_report_20260520_143600.json").write_text(
                json.dumps(_sample_report("test", "", "NON_EVALUE")), encoding="utf-8"
            )
            (root / "benchsuite_report_20260520_143600.html").write_text("<html>test</html>", encoding="utf-8")
            (root / "dmesg_20260520_143600.txt").write_text("dmesg test", encoding="utf-8")

            (root / "benchsuite_report_20260521_091900.json").write_text(
                json.dumps(_sample_report("NodeB slot du haut", "", "NON_EVALUE")), encoding="utf-8"
            )
            (root / "benchsuite_report_20260521_091900.html").write_text("<html>nodeb</html>", encoding="utf-8")
            (root / "dmesg_20260521_091900.txt").write_text("dmesg nodeb", encoding="utf-8")

            db = BenchSuiteDB(root)
            db.scan()

            det_test = db.get_machine_detail("test")
            det_nodeb = db.get_machine_detail("NodeB slot du haut")
            self.assertIsNotNone(det_test)
            self.assertIsNotNone(det_nodeb)

            test_names = {f["file_name"] for f in det_test["files"]}
            nodeb_names = {f["file_name"] for f in det_nodeb["files"]}
            self.assertEqual(
                test_names,
                {"benchsuite_report_20260520_143600.json", "benchsuite_report_20260520_143600.html", "dmesg_20260520_143600.txt"},
            )
            self.assertEqual(
                nodeb_names,
                {"benchsuite_report_20260521_091900.json", "benchsuite_report_20260521_091900.html", "dmesg_20260521_091900.txt"},
            )
            self.assertTrue(test_names.isdisjoint(nodeb_names))

    def test_one_report_per_row_with_shared_chassis_serial(self):
        """Plusieurs rapports partageant le même n° de série (ex. SN-CHASSIS-01) doivent rester 1 rapport par ligne."""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 2 rapports dans un même dossier plat + 2 rapports dans des sous-dossiers distincts,
            # tous avec serial="SN-CHASSIS-01"
            specs = [
                (root, "20261113_110800", "2026-11-13T11:08:00", "nodeb gauche chassis erreur ssd2", "A_VERIFIER"),
                (root, "20261113_121500", "2026-11-13T12:15:00", "nodeb droit chassis", "CONFORME"),
                (root / "slot_haut", "20261114_090000", "2026-11-14T09:00:00", "NodeB slot du haut", "CONFORME"),
                (root / "slot_bas", "20261115_163000", "2026-11-15T16:30:00", "NodeB slot du bas", "NON_CONFORME"),
            ]
            for folder, ts, exp_at, sys_name, verdict in specs:
                folder.mkdir(parents=True, exist_ok=True)
                rep = _sample_report(sys_name, "SN-CHASSIS-01", verdict)
                rep["exported_at"] = exp_at
                (folder / f"benchsuite_report_{ts}.json").write_text(json.dumps(rep), encoding="utf-8")
                (folder / f"benchsuite_report_{ts}.html").write_text(f"<html>{sys_name}</html>", encoding="utf-8")
                (folder / f"benchsuite_report_{ts}.pdf").write_bytes(b"%PDF-1.4\n")
                (folder / f"benchsuite_report_{ts}.csv").write_text("k;v\na;1\n", encoding="utf-8")
                (folder / f"dmesg_{ts}.txt").write_text(f"dmesg {sys_name}", encoding="utf-8")

            db = BenchSuiteDB(root)
            db.scan()
            ov = db.get_overview()

            # 4 rapports distincts (jamais fusionnés en 1 ligne), triés par date décroissante
            self.assertEqual(len(ov["reports"]), 4)
            self.assertEqual(
                [r["system_name"] for r in ov["reports"]],
                [
                    "NodeB slot du bas",
                    "NodeB slot du haut",
                    "nodeb droit chassis",
                    "nodeb gauche chassis erreur ssd2",
                ],
            )

            seen_file_sets = []
            for r in ov["reports"]:
                self.assertEqual(r["file_count"], 5)
                det = db.get_report_detail(r["id"])
                self.assertIsNotNone(det)
                rels = {f["rel_path"] for f in det["files"]}
                self.assertEqual(len(rels), 5)
                for prev_rels in seen_file_sets:
                    self.assertTrue(rels.isdisjoint(prev_rels))
                seen_file_sets.append(rels)


if __name__ == "__main__":
    unittest.main()
