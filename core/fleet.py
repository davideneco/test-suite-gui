"""
fleet.py — Synthèse d'un lot de machines à partir de leurs rapports JSON.

Parcourt un dossier de résultats (ex. ~/BenchmarkResults/<client>/<série>/<date>/), garde le DERNIER
rapport de chaque machine, et produit un tableau comparatif (CSV + HTML) où les machines qui sortent
du lot (score ou température vs médiane du lot) sont mises en évidence. Sans dépendance externe.
"""

import csv
import glob
import html
import json
import os
import statistics
from datetime import datetime
from typing import Dict, List, Optional

SCORE_TOL_PCT = 5.0     # un score à plus de 5 % sous la médiane du lot est signalé
TEMP_TOL_C = 5.0        # un hotspot à plus de 5 °C au-dessus de la médiane du lot est signalé


def _latest_reports(root: str) -> Dict[str, str]:
    """{clé machine: chemin du rapport JSON le plus récent}."""
    best: Dict[str, tuple] = {}
    paths = (glob.glob(os.path.join(root, "**", "benchsuite_report_*.json"), recursive=True)
             + glob.glob(os.path.join(root, "**", "benchsuite_report.json"), recursive=True))
    for path in set(paths):
        try:
            with open(path, encoding="utf-8") as f:
                head = json.load(f)
            meta = head.get("metadata", {})
            key = meta.get("serial") or meta.get("system_name") or os.path.basename(os.path.dirname(path))
            stamp = head.get("exported_at", "") or ""
            if key not in best or stamp > best[key][0]:
                best[key] = (stamp, path)
        except Exception:
            continue
    return {k: v[1] for k, v in best.items()}


def _gpu_medians(tel: dict) -> Dict[str, Optional[float]]:
    gpus = (tel or {}).get("gpus", [])

    def med(key):
        vals = [g[key] for g in gpus if g.get(key) is not None]
        return round(statistics.median(vals), 1) if vals else None
    return {"gpu_count": len(gpus), "hotspot_max_c": max([g["temp_hotspot_max_c"] for g in gpus if g.get("temp_hotspot_max_c") is not None], default=None),
            "delta_moy_c": med("delta_hotspot_moy_c"), "power_moy_w": med("power_moy_w"), "freq_min_mhz": med("freq_min_mhz")}


def collect_fleet(root: str) -> List[Dict]:
    """Une ligne par machine : identité, verdict, score de chaque test, médianes GPU."""
    rows = []
    for key, path in sorted(_latest_reports(root).items()):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        meta = d.get("metadata", {})
        row = {"machine": key, "model": meta.get("model", ""), "client": meta.get("client", ""),
               "order": meta.get("order_no", ""), "date": (d.get("exported_at") or "")[:16].replace("T", " "),
               "verdict": (d.get("verdict") or {}).get("label", "—"), "report": path,
               "tests": {r["name"]: r.get("score") for r in d.get("results", []) if r.get("status") == "ok" and r.get("score") is not None}}
        row.update(_gpu_medians(d.get("telemetry_summary")))
        rows.append(row)
    return rows


def flag_outliers(rows: List[Dict]) -> None:
    """Ajoute row['flags'] : liste des écarts vs médiane du lot (≥ 3 machines nécessaires)."""
    for r in rows:
        r["flags"] = []
    if len(rows) < 3:
        return
    names = sorted({n for r in rows for n in r["tests"]})
    for n in names:
        vals = [r["tests"][n] for r in rows if n in r["tests"]]
        if len(vals) < 3:
            continue
        med = statistics.median(vals)
        for r in rows:
            v = r["tests"].get(n)
            if v is not None and med and 100.0 * (med - v) / med > SCORE_TOL_PCT:
                r["flags"].append(f"{n} : {v:g} ({-100.0 * (med - v) / med:+.1f} % vs lot)")
    hs = [r["hotspot_max_c"] for r in rows if r.get("hotspot_max_c") is not None]
    if len(hs) >= 3:
        med = statistics.median(hs)
        for r in rows:
            if r.get("hotspot_max_c") is not None and r["hotspot_max_c"] - med > TEMP_TOL_C:
                r["flags"].append(f"hotspot max {r['hotspot_max_c']:.0f} °C (+{r['hotspot_max_c'] - med:.0f} °C vs lot)")


def write_fleet(rows: List[Dict], out_dir: str) -> Dict[str, str]:
    """Écrit fleet_summary.csv et fleet_summary.html ; retourne les chemins."""
    os.makedirs(out_dir, exist_ok=True)
    names = sorted({n for r in rows for n in r["tests"]})
    cols = ["machine", "model", "client", "order", "date", "verdict"] + names + \
           ["gpu_count", "hotspot_max_c", "delta_moy_c", "power_moy_w", "freq_min_mhz", "flags", "report"]
    csv_path = os.path.join(out_dir, "fleet_summary.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in rows:
            w.writerow([r.get(c, "") if c not in names else r["tests"].get(c, "") for c in cols[:-2]]
                       + [" | ".join(r.get("flags", [])), r["report"]])
    th = "".join(f"<th>{html.escape(c)}</th>" for c in cols[:-1])
    body = ""
    for r in rows:
        flagged = bool(r.get("flags"))
        cells = "".join(f"<td>{html.escape(str(r.get(c, '') if c not in names else r['tests'].get(c, '')))}</td>" for c in cols[:-2])
        cells += f"<td>{html.escape(' | '.join(r.get('flags', [])))}</td>"
        body += f"<tr style='{'background:#3a1a1a' if flagged else ''}'>{cells}</tr>"
    page = (f"<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'><title>BenchSuite — Synthèse du lot</title>"
            "<style>body{background:#0d0d0d;color:#eee;font:13px system-ui;padding:24px}table{border-collapse:collapse;width:100%}"
            "th,td{border-bottom:1px solid #2a2a2a;padding:6px 8px;text-align:left}th{background:#141414;color:#ff8c33}</style></head><body>"
            f"<h1>Synthèse du lot — {len(rows)} machine(s)</h1><p>Généré le {datetime.now():%Y-%m-%d %H:%M}. "
            f"Lignes rouges : machine hors lot (score &gt; {SCORE_TOL_PCT:g} % sous la médiane ou hotspot &gt; +{TEMP_TOL_C:g} °C).</p>"
            f"<table><thead><tr>{th}</tr></thead><tbody>{body}</tbody></table></body></html>")
    html_path = os.path.join(out_dir, "fleet_summary.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)
    return {"csv": csv_path, "html": html_path}
