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


if __name__ == "__main__":
    unittest.main()
