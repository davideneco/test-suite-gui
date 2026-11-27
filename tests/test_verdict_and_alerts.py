# -*- coding: utf-8 -*-
"""Tests de déduplication du verdict et des alertes matérielles, et d'édition interactive HTML."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from core.result import BenchmarkResult
from core import verdict
from exporters.common import (
    _collect_report_alerts,
    _dedupe_alerts,
    _hardware_fault_alerts,
    _observation_alerts,
    _pcie_alerts,
)
from exporters.html_report import export_html, reexport_html


def _make_pcie_result(name: str, bus: str = "0000:01:00.0", dev_name: str = "NVIDIA Corporation Device [10de:233b]",
                      anom_label: str = "largeur x8 au lieu de x16", code: str = "largeur",
                      category: str = "GPU", inv_findings: list = None, hw_events: list = None):
    metrics = {
        "pcie_links": {
            "duree_s": 60.0,
            "au_maximum": 0,
            "non_evalues": 0,
            "periphériques": [
                {
                    "bus": bus,
                    "nom": dev_name,
                    "type": "GPU",
                    "classe": "0302",
                    "gen_max": 5,
                    "largeur_max": 16,
                    "liens": [{"gen": 5, "largeur": 8, "pct": 100.0}],
                    "pct_au_maximum": 0.0,
                    "anomalies": [{"level": "warn", "code": code, "label": anom_label}],
                }
            ],
        }
    }
    if inv_findings is not None:
        metrics["inventory_diff"] = {"identical": not inv_findings, "findings": inv_findings}
    if hw_events is not None:
        metrics["hardware_events"] = hw_events
    return BenchmarkResult(name=name, category=category, score=0.0, unit="", duration_s=60.0,
                           raw_output="", status="ok", metrics=metrics)


class TestVerdictAndAlertsDeduplication(unittest.TestCase):
    def test_verdict_dedupe_rules_concise_for_many_tests(self):
        results = [_make_pcie_result(f"Test {i}") for i in range(1, 8)]
        v = verdict.compute_verdict(results, snapshot={})
        pcie_rules = [r for r in v["rules"] if r["id"] == "pcie_link"]
        self.assertEqual(len(pcie_rules), 1)
        self.assertEqual(pcie_rules[0]["detail"], "relevé sur 7 tests")

    def test_verdict_dedupe_rules_one_and_two_tests(self):
        v1 = verdict.compute_verdict([_make_pcie_result("Stress CPU")], snapshot={})
        r1 = [r for r in v1["rules"] if r["id"] == "pcie_link"]
        self.assertEqual(len(r1), 1)
        self.assertEqual(r1[0]["detail"], "test « Stress CPU »")

        v2 = verdict.compute_verdict([_make_pcie_result("Stress CPU"), _make_pcie_result("GPU Burn")], snapshot={})
        r2 = [r for r in v2["rules"] if r["id"] == "pcie_link"]
        self.assertEqual(len(r2), 1)
        self.assertEqual(r2[0]["detail"], "tests « Stress CPU », « GPU Burn »")

    def test_pcie_alerts_single_entry_across_seven_tests(self):
        results = [_make_pcie_result(f"Test {i}") for i in range(1, 8)]
        alerts = _pcie_alerts(results)
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["category"], "Liens PCIe")
        self.assertIn("0000:01:00.0", alerts[0]["title"])
        self.assertIn("relevé sur 7 tests", alerts[0]["desc"])

    def test_dedupe_alerts_suppresses_pcie_duplicate_covered_by_hardware_audit(self):
        hw_audit_alert = {
            "type": "warning",
            "category": "Carte graphique (NVIDIA H200 NVL)",
            "title": "Liaison PCIe physiquement dégradée",
            "desc": "La carte graphique fonctionne avec un bus bridé : x8 au lieu de x16 (Largeur réduite).",
            "devices": ["GPU 0 (01:00.0)"],
        }
        results = [_make_pcie_result(f"Test {i}") for i in range(1, 8)]
        combined = _dedupe_alerts([hw_audit_alert] + _pcie_alerts(results))
        self.assertEqual(len(combined), 1)
        self.assertEqual(combined[0]["category"], "Carte graphique (NVIDIA H200 NVL)")

    def test_dedupe_alerts_keeps_distinct_width_vs_idle_speed(self):
        hw_speed_only = {
            "type": "warning",
            "category": "Carte graphique (NVIDIA H200 NVL)",
            "title": "Vitesse de liaison PCIe réduite au repos",
            "desc": "Le bus PCIe du GPU fonctionne en Gen 1 au lieu de Gen 5 (Vitesse réduite).",
            "devices": ["GPU 0 (01:00.0)"],
        }
        results = [_make_pcie_result("GPU Burn", anom_label="largeur x8 au lieu de x16", code="largeur")]
        combined = _dedupe_alerts([hw_speed_only] + _pcie_alerts(results))
        self.assertEqual(len(combined), 2)

    def test_observation_and_fault_alerts_deduplicated(self):
        inv = [{"level": "fail", "label": "Disque disparu", "detail": "/dev/nvme0n1"}]
        ev = [{"severity": "critical", "category": "MCE", "t": 12, "line": "Machine check error", "device": "CPU 0"}]
        results = [
            _make_pcie_result("Test 1", inv_findings=inv, hw_events=ev),
            _make_pcie_result("Test 2", inv_findings=inv, hw_events=ev),
        ]
        meta = {"inventory_diff": {"identical": False, "findings": inv}}
        obs = _observation_alerts(results, meta)
        self.assertEqual(len(obs), 1)
        self.assertEqual(obs[0]["type"], "error")
        self.assertEqual(obs[0]["title"], "Disque disparu")

        faults = _hardware_fault_alerts(results)
        self.assertEqual(len(faults), 1)
        self.assertIn("2 événements", faults[0]["title"])
        self.assertIn("relevé sur 2 tests", faults[0]["desc"])

    def test_collect_report_alerts_integration_single_gpu_alert(self):
        fake_audit = [{
            "type": "warning",
            "category": "Carte graphique (NVIDIA H200 NVL)",
            "title": "Liaison PCIe physiquement dégradée",
            "desc": "La carte graphique fonctionne avec un bus bridé : x8 au lieu de x16 (Largeur réduite).",
            "devices": ["GPU 0 (01:00.0)"],
        }]
        results = [_make_pcie_result(f"Test {i}") for i in range(1, 8)]
        with patch("core.system_info.run_hardware_audit", return_value=fake_audit):
            alerts = _collect_report_alerts(results, system_info_data={"gpus": [{"pci_bus": "0000:01:00.0"}]})
        self.assertEqual(len(alerts), 1)


class TestHtmlInteractiveVerdictAndAlerts(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="benchsuite_verdict_html_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_html_report_single_alert_interactive_controls_and_valid_js(self):
        fake_audit = [{
            "type": "warning",
            "category": "Carte graphique (NVIDIA H200 NVL)",
            "title": "Liaison PCIe physiquement dégradée",
            "desc": "La carte graphique fonctionne avec un bus bridé : x8 au lieu de x16 (Largeur réduite).",
            "devices": ["GPU 0 (01:00.0)"],
        }]
        results = [_make_pcie_result(f"Test {i}") for i in range(1, 8)]
        with patch("core.system_info.run_hardware_audit", return_value=fake_audit):
            html_path = export_html(results, self.tmpdir, report_meta={}, system_info_data={"gpus": [{"pci_bus": "0000:01:00.0"}]})

        with open(html_path, encoding="utf-8") as f:
            doc = f.read()

        # Structure et déduplication
        self.assertIn("</head>", doc)
        self.assertIn('id="verdictBox"', doc)
        self.assertIn('id="diagnosticBox"', doc)
        self.assertIn('id="manualConformeForm"', doc)
        self.assertIn("Passer en CONFORME", doc)
        self.assertIn("Enregistrer HTML modifié", doc)
        self.assertIn("@media print", doc)
        self.assertIn('data-alert-idx="0"', doc)
        self.assertNotIn('data-alert-idx="1"', doc)
        self.assertIn("relevé sur 7 tests", doc)

        # Payload embarque diagnostic_alerts dédupliqué (1 seule alerte)
        m_payload = re.search(r'<script[^>]*id="payload"[^>]*>(.*?)</script>', doc, re.S)
        self.assertIsNotNone(m_payload)
        payload = json.loads(m_payload.group(1).replace("<\\/", "</"))
        self.assertEqual(len(payload.get("diagnostic_alerts", [])), 1)

        # Validation syntaxique JS avec node --check si node est disponible
        if shutil.which("node"):
            scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", doc, re.S)
            # Le dernier <script> non-JSON est le script applicatif principal
            app_js = [s for s in scripts if "initVerdictAndAlerts" in s]
            self.assertEqual(len(app_js), 1)
            js_file = os.path.join(self.tmpdir, "check_report.js")
            with open(js_file, "w", encoding="utf-8") as jf:
                jf.write(app_js[0])
            proc = subprocess.run(["node", "--check", js_file], capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, msg=f"Erreur syntaxe JS :\n{proc.stderr}")

    def test_reexport_html_preserves_manual_conforme_and_dismissed_alerts(self):
        results = [_make_pcie_result("Test 1")]
        v = verdict.compute_verdict(results, snapshot={})
        v["manual_override"] = {"status": "CONFORME", "reason": "Câblage x8 prévu par le châssis", "at": "2026-11-27T12:00:00Z"}
        v["status"] = "CONFORME"
        v["label"] = "CONFORME (validé manuellement)"

        src = export_html(results, self.tmpdir, report_meta={"verdict": v, "diagnostic_alerts": []}, system_info_data={})
        dst = os.path.join(self.tmpdir, "reexported.html")
        reexport_html(src, dst)

        with open(dst, encoding="utf-8") as f:
            doc = f.read()
        self.assertIn("CONFORME (validé manuellement)", doc)
        self.assertIn("Câblage x8 prévu par le châssis", doc)
        self.assertIn("Système entièrement opérationnel", doc)
        m_payload = re.search(r'<script[^>]*id="payload"[^>]*>(.*?)</script>', doc, re.S)
        payload = json.loads(m_payload.group(1).replace("<\\/", "</"))
        self.assertEqual(payload["diagnostic_alerts"], [])
        self.assertEqual(payload["verdict"]["status"], "CONFORME")

    def test_js_interactive_behavior_in_node(self):
        if not shutil.which("node"):
            self.skipTest("node indisponible")
        inv_fail = [{"level": "fail", "label": "Disque disparu", "detail": "/dev/nvme0n1"}]
        results = [_make_pcie_result("Test 1", inv_findings=inv_fail)]
        html_path = export_html(results, self.tmpdir, report_meta={}, system_info_data={})
        with open(html_path, encoding="utf-8") as f:
            doc = f.read()

        m_payload = re.search(r'<script[^>]*id="payload"[^>]*>(.*?)</script>', doc, re.S)
        scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", doc, re.S)
        app_js = [s for s in scripts if "initVerdictAndAlerts" in s][0]
        # Extraire uniquement la partie gestion verdict/alertes + helpers (jusqu'à bindNoteEditors)
        end_idx = app_js.index("function currentNoteMap")
        snippet = app_js[app_js.index("let payload={}"):end_idx]
        payload_js_str = json.dumps(m_payload.group(1).replace("<\\/", "</"))

        harness = f"""
const assert = require('assert');
function makeEl(id) {{
  return {{
    id, textContent: '', innerHTML: '', value: '', style: {{}}, dataset: {{}},
    attrs: {{}},
    setAttribute(k, v) {{ this.attrs[k] = v; }},
    getAttribute(k) {{ return this.attrs[k]; }},
    removeAttribute(k) {{ delete this.attrs[k]; }},
    hasAttribute(k) {{ return k in this.attrs; }},
    addEventListener() {{}},
    focus() {{}},
    querySelectorAll() {{ return []; }},
  }};
}}
const ids = ['payload','jsonDump','verdictBox','verdictTitle','verdictManualNote','verdictMeta',
  'btnForceConforme','btnCancelConforme','btnResetEdits','verdictRulesMain','verdictRulesRest',
  'manualConformeForm','manualConformeReason','diagnosticBox','diagTitle','diagAlertsList',
  'btnDismissAllDiag','btnResetDiagOnly'];
const els = Object.fromEntries(ids.map(i => [i, makeEl(i)]));
els.payload.textContent = {payload_js_str};
const store = {{}};
global.localStorage = {{
  getItem: k => (k in store ? store[k] : null),
  setItem: (k, v) => {{ store[k] = String(v); }},
  removeItem: k => {{ delete store[k]; }},
}};
global.window = {{ __theme: 'dark' }};
global.document = {{
  getElementById: id => els[id] || null,
  body: {{ insertAdjacentHTML() {{}} }},
}};
function escapeHtml(s) {{ return String(s).replace(/[&<>"']/g, c => ({{'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}}[c])); }}
function themeIsLight() {{ return false; }}
function fixInline() {{}}

{snippet}

// 1. État initial : NON_CONFORME (inv_fail + pcie_link warn), 2 alertes diagnostic
assert.strictEqual(payload.verdict.status, 'NON_CONFORME');
assert.strictEqual(payload.diagnostic_alerts.length, 2);

// 2. Passer en CONFORME manuellement avec motif
els.manualConformeReason.value = 'Validé après contrôle banc';
confirmManualConforme();
assert.strictEqual(payload.verdict.status, 'CONFORME');
assert.strictEqual(payload.verdict.label, 'CONFORME (validé manuellement)');
assert.strictEqual(payload.verdict.auto_status, 'NON_CONFORME');
assert(els.verdictManualNote.textContent.includes('Validé après contrôle banc'));

// 3. Annuler la validation manuelle -> retour NON_CONFORME
cancelManualConforme();
assert.strictEqual(payload.verdict.status, 'NON_CONFORME');

// 4. Supprimer la règle fail puis la règle warn -> passage automatique en CONFORME
const failIdx = payload.verdict.rules.findIndex(r => r.level === 'fail');
assert(failIdx >= 0);
dismissVerdictRule(failIdx);
assert.strictEqual(payload.verdict.status, 'A_VERIFIER');

const warnIdx = payload.verdict.rules.findIndex(r => r.level === 'warn');
assert(warnIdx >= 0);
dismissVerdictRule(warnIdx);
assert.strictEqual(payload.verdict.status, 'CONFORME');

// 5. Supprimer une alerte diagnostic puis toutes -> encart opérationnel vert
dismissDiagAlert(0);
assert.strictEqual(payload.diagnostic_alerts.length, 1);
dismissAllDiagAlerts();
assert.strictEqual(payload.diagnostic_alerts.length, 0);
assert(els.diagTitle.textContent.includes('entièrement opérationnel'));

// 6. Réinitialiser -> tout revient à l'état d'origine
resetVerdictAndAlerts();
assert.strictEqual(payload.verdict.status, 'NON_CONFORME');
assert.strictEqual(payload.diagnostic_alerts.length, 2);
console.log('NODE_INTERACTIVE_OK');
"""
        runner = os.path.join(self.tmpdir, "test_interactive.js")
        with open(runner, "w", encoding="utf-8") as rf:
            rf.write(harness)
        proc = subprocess.run(["node", runner], capture_output=True, text=True, timeout=15)
        self.assertEqual(proc.returncode, 0, msg=f"Échec test JS Node :\n{proc.stderr}\n{proc.stdout}")
        self.assertIn("NODE_INTERACTIVE_OK", proc.stdout)


class TestReportParamsAndCommands(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="benchsuite_params_")

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_fio_multidisk_propagates_command_and_params_to_all_disks(self):
        from core.fio_runner import _disk_metrics
        shared = {
            "command": "fio --rw=rw --rwmixread=70 --bs=1m --numjobs=1 --iodepth=16 --ioengine=io_uring --runtime=60",
            "mode": "rw",
            "rwmixread": 70,
            "taille_bloc": "1m",
            "iodepth": 16,
            "jobs": 1,
            "moteur_io": "io_uring",
            "runtime_s": 60,
            "pcie_links": {"periphériques": []},
        }
        jm0 = {"name": "job0", "lecture_MiB_s": 129.0, "ecriture_MiB_s": 130.5}
        jm1 = {"name": "job1", "lecture_MiB_s": 56.3, "ecriture_MiB_s": 56.1}
        jm2 = {"name": "job2", "lecture_MiB_s": 113.0, "ecriture_MiB_s": 111.4}

        m0 = _disk_metrics(jm0, shared, first=True)
        m1 = _disk_metrics(jm1, shared, first=False)
        m2 = _disk_metrics(jm2, shared, first=False)

        for m in (m0, m1, m2):
            self.assertEqual(m["command"], shared["command"])
            self.assertEqual(m["mode"], "rw")
            self.assertEqual(m["taille_bloc"], "1m")
            self.assertEqual(m["moteur_io"], "io_uring")
            self.assertEqual(m["rwmixread"], 70)
            self.assertNotIn("name", m)
        self.assertIn("pcie_links", m0)
        self.assertNotIn("pcie_links", m1)
        self.assertNotIn("pcie_links", m2)

    def test_html_storage_and_gpu_nvbandwidth_params_and_commands(self):
        from core.fio_runner import _disk_metrics
        shared_hdd = {
            "command": "fio --rw=rw --rwmixread=70 --bs=1m --numjobs=1 --iodepth=16 --ioengine=io_uring --runtime=60",
            "mode": "rw",
            "rwmixread": 70,
            "taille_bloc": "1m",
            "iodepth": 16,
            "jobs": 1,
            "moteur_io": "io_uring",
            "runtime_s": 60,
        }
        r_sda = BenchmarkResult(
            name="fio — Benchmark HDD — Mixte Séquentiel (/dev/sda)", category="Stockage",
            score=129.7, unit="MiB/s", duration_s=74.5, raw_output="", status="ok",
            command=shared_hdd["command"],
            metrics=_disk_metrics({"name": "job0", "lecture_MiB_s": 129.0, "ecriture_MiB_s": 130.5}, shared_hdd, True),
        )
        r_sdb = BenchmarkResult(
            name="fio — Benchmark HDD — Mixte Séquentiel (/dev/sdb)", category="Stockage",
            score=56.2, unit="MiB/s", duration_s=74.5, raw_output="", status="ok",
            command=shared_hdd["command"],
            metrics=_disk_metrics({"name": "job1", "lecture_MiB_s": 56.3, "ecriture_MiB_s": 56.1}, shared_hdd, False),
        )
        # Ancien rapport multi-disques sans command dans metrics mais avec system_info['test_params']
        r_legacy = BenchmarkResult(
            name="fio — Benchmark HDD — Mixte Séquentiel (/dev/sdc)", category="Stockage",
            score=112.2, unit="MiB/s", duration_s=74.5, raw_output="", status="ok",
            metrics={"lecture_MiB_s": 113.0, "ecriture_MiB_s": 111.4},
            system_info={"disk_dev": "/dev/sdc", "test_params": "{'rw_mode': 'rw', 'bs': '1m', 'iodepth': 16, 'numjobs': 1, 'io_engine': 'io_uring', 'runtime_s': 60}"},
        )
        # Test GPU nvbandwidth avec télémétrie GPU et testcases
        r_nv = BenchmarkResult(
            name="GPU nvbandwidth", category="GPU",
            score=285.4, unit="GB/s", duration_s=18.2, raw_output="", status="ok",
            command="/opt/nvbandwidth/nvbandwidth --bufferSize 512 --testSamples 3 -t host_to_device_memcpy_ce device_to_host_memcpy_ce",
            metrics={
                "command": "/opt/nvbandwidth/nvbandwidth --bufferSize 512 --testSamples 3 -t host_to_device_memcpy_ce device_to_host_memcpy_ce",
                "buffer_size_mb": 512,
                "iterations": 3,
                "extra_args": "-t host_to_device_memcpy_ce device_to_host_memcpy_ce",
                "nvbandwidth_tests": ["host_to_device_memcpy_ce", "device_to_host_memcpy_ce"],
                "nvbandwidth_data": [
                    {"name": "host_to_device_memcpy_ce", "results": {"average": 280.1}},
                    {"name": "device_to_host_memcpy_ce", "results": {"average": 290.7}},
                ],
                "gpu0_temp_max_c": 52.0,
                "gpu0_power_moy_w": 95.0,
            },
        )
        # Ancien test GPU — Bande passante VRAM sans command explicite
        r_vram = BenchmarkResult(
            name="GPU — Bande passante VRAM", category="GPU",
            score=3100.0, unit="GB/s", duration_s=12.4, raw_output="", status="ok",
            metrics={"vram_bw_method": "nvbandwidth", "vram_bw_source": "nvbandwidth", "gpu0_vram_bw_gbs": 3100.0},
        )

        html_path = export_html([r_sda, r_sdb, r_legacy, r_nv, r_vram], self.tmpdir, report_meta={}, system_info_data={})
        with open(html_path, encoding="utf-8") as f:
            doc = f.read()

        # Section Stockage : les 3 disques ont bien paramètres + commande (pas de "—")
        m_st = re.search(r'<section id="storage"[^>]*>(.*?)</section>', doc, re.S)
        self.assertIsNotNone(m_st)
        st_html = m_st.group(1)
        self.assertEqual(st_html.count("Engine: io_uring"), 3)
        self.assertEqual(st_html.count("<code class='cmd-inline'"), 3)
        self.assertEqual(st_html.count("fio --rw=rw"), 6)
        self.assertNotIn("<td><span class='muted'>—</span></td>", st_html)

        # Section GPU : nvbandwidth et Bande passante VRAM affichent le bench, les tests et la commande
        m_gpu = re.search(r'<section id="gpu"[^>]*>(.*?)</section>', doc, re.S)
        self.assertIsNotNone(m_gpu)
        gpu_html = m_gpu.group(1)
        self.assertIn("Bench: nvbandwidth", gpu_html)
        self.assertIn("Tests: host_to_device_memcpy_ce, device_to_host_memcpy_ce", gpu_html)
        self.assertIn("/opt/nvbandwidth/nvbandwidth --bufferSize 512 --testSamples 3 -t host_to_device_memcpy_ce", gpu_html)
        self.assertIn("285.4 GB/s", gpu_html)
        self.assertIn("device_local_copy", gpu_html)
        self.assertNotIn("<td><span class='muted'>—</span></td>", gpu_html)

        # Validation syntaxique JS
        if shutil.which("node"):
            scripts = re.findall(r"<script(?:\s[^>]*)?>(.*?)</script>", doc, re.S)
            app_js = [s for s in scripts if "renderNvbandwidthTab" in s][0]
            js_file = os.path.join(self.tmpdir, "check_nv_report.js")
            with open(js_file, "w", encoding="utf-8") as jf:
                jf.write(app_js)
            proc = subprocess.run(["node", "--check", js_file], capture_output=True, text=True, timeout=15)
            self.assertEqual(proc.returncode, 0, msg=f"Erreur syntaxe JS :\n{proc.stderr}")


if __name__ == "__main__":
    unittest.main()
