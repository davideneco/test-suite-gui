# -*- coding: utf-8 -*-
"""exporters/export.py — Façade d'export BenchSuite (CSV, JSON, PDF, HTML, dmesg, historique)."""

import csv
import json
import os
from datetime import datetime
from typing import List

from core.result import BenchmarkResult, collect_dmesg_log


def get_hardware_report():
    """Fiche matérielle brute (import paresseux depuis core.benchmarks)."""
    try:
        from core.benchmarks import get_hardware_report as _ghr
        return _ghr()
    except Exception:
        return {}

APP_VERSION = "1.0.21"

# Rapports : onglet « Séquence & électronique » (courbes de la séquence, relevés de tensions/ventilateurs), sections PDF et lignes CSV
# correspondantes. Désactivé : les relevés restent collectés et présents dans le JSON, mais ne s'affichent plus dans HTML / PDF / CSV.
# Passer à True pour les réafficher.
EXPORT_SEQUENCE_ELECTRONICS = False

from exporters.common import (  # noqa: E402, F401
    RAW_OUTPUT_JSON_MAX,
    _ELEC_CAT_LABELS,
    _FIO_COLS,
    _FIO_KEY_RE,
    _GPU_EXTRA_KEYS,
    _GPU_METRIC_RE,
    _GPU_TABLE_HEADERS,
    _HIDDEN_METRIC_KEYS,
    _PCIE_MAIN_CLASSES,
    _SYS_LABELS,
    _build_telemetry_summary,
    _collect_report_alerts,
    _dedupe_alerts,
    _dur_label,
    _dumps_compact_json,
    _electrical_rows,
    _fio_matrix,
    _flatten_system_rows,
    _fmt_cell,
    _fmt_num,
    _gpu_col,
    _gpu_extra_text,
    _gpu_labels,
    _gpu_list_from,
    _gpu_rows_for_result,
    _gpu_table,
    _gpu_table_cells,
    _hardware_fault_alerts,
    _is_compute_gpu,
    _metric_rows,
    _non_gpu_metric_items,
    _observation_alerts,
    _observations,
    _pci_label,
    _pcie_alerts,
    _pcie_link_rows,
    _report_payload,
    _report_system_name,
    _safe_folder_part,
    _safe_text,
    _sequence_entity,
    _series_for_result,
    _system_sections,
    _trim_payload_for_json,
    _ts,
    _verdict_for,
    compact_raw_output,
    format_report_dir_name,
    load_report_meta,
    save_report_meta,
    unique_result_names,
)
from exporters.history import (  # noqa: E402, F401
    HISTORY_FILE,
    HISTORY_MAX_ENTRIES,
    HISTORY_RAW_MAX,
    _write_history,
    clear_history,
    delete_history_indices,
    history_result,
    load_history,
    save_to_history,
)
from exporters.pdf_report import (  # noqa: E402, F401
    GPU_PALETTE,
    _PDF_GPU_COL_MM,
    _pdf_gpu_widths,
    _pdf_multi_gpu_charts,
    _pdf_safe,
    _sequence_pdf_chart,
    _simple_pdf_chart,
    _thermal_line_chart,
    export_pdf,
)
from exporters.html_report import (  # noqa: E402, F401
    HTML_MAX_POINTS,
    _VERDICT_COLORS,
    _add_light_theme,
    _decimate_series,
    _default_theme,
    _electronics_chart_data,
    _electronics_html,
    _pcie_links_html,
    _round_series,
    _sequence_html,
    _slim_html_payload,
    _stop_card_html,
    _verdict_html,
    export_html,
    reexport_html,
)


def export_json(results: List[BenchmarkResult], out_dir: str, report_meta: dict = None,
                system_info_data: dict = None) -> str:
    results = unique_result_names(results)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"benchsuite_report_{_ts()}.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(_dumps_compact_json(_trim_payload_for_json(_report_payload(results, report_meta, system_info_data))))
    return path


def export_csv(results: List[BenchmarkResult], out_dir: str, report_meta: dict = None,
               system_info_data: dict = None, condensed: bool = False) -> str:
    results = unique_result_names(results)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"benchsuite_report_{_ts()}.csv")
    payload = _report_payload(results, report_meta, system_info_data)
    rows = []
    rows.append({"section":"metadata", "item":"system_name", "key":"", "value":payload["system"]["name"]})
    rows.append({"section":"metadata", "item":"exported_at", "key":"", "value":payload["exported_at"]})
    rows.append({"section":"metadata", "item":"benchsuite_version", "key":"", "value":payload["benchsuite_version"]})
    for mk in ("model", "serial", "order_no", "operator", "client", "ambient_temp", "fan_speed"):
        if payload["metadata"].get(mk):
            rows.append({"section":"metadata", "item":mk, "key":"", "value":payload["metadata"][mk]})

    v = payload.get("verdict")
    if v:
        rows.append({"section":"verdict", "item":"status", "key":"", "value":v["label"]})
        for r in v["rules"]:
            if r["level"] != "pass":
                rows.append({"section":"verdict", "item":r["level"], "key":r["label"], "value":r.get("detail", "")})

    # Snapshot système : valeurs vides ignorées
    for k, v in _flatten_system_rows("", payload["system"]["snapshot_at_start"]):
        if v not in ("", None):
            rows.append({"section":"system", "item":k, "key":"", "value":v})

    for g in payload.get("telemetry_summary", {}).get("gpus", []):
        gid = g.get("gpu_index", 0)
        for k, v in g.items():
            if k == "gpu_index":
                continue
            rows.append({"section":"telemetry_summary", "item":f"GPU_{gid}", "key":k, "value":_safe_text(v)})

    seq = payload.get("sequence_series") if EXPORT_SEQUENCE_ELECTRONICS else None
    if isinstance(seq, dict):
        for c in seq.get("channels", []):
            rows.append({"section": "sequence_courbes", "item": c["panel"], "key": c["label"],
                         "value": f"min {c['min']} / moy {c['mean']} / max {c['max']} / dernier {c['last']} {c['unit']}"})
        for i, m in enumerate(seq.get("marks", []), 1):
            rows.append({"section": "sequence_etapes", "item": i, "key": f"t+{m['t']} s", "value": m["label"]})
    camp = payload.get("inventory_campaign")
    if isinstance(camp, dict):
        rows.append({"section": "inventory_campaign", "item": "", "key": "resultat",
                     "value": "identique" if camp.get("identical") else f"{len(camp.get('findings', []))} écart(s)"})
        for f in camp.get("findings", []):
            rows.append({"section": "inventory_campaign", "item": "", "key": f["level"], "value": f["label"] + (f" — {f['detail']}" if f.get("detail") else "")})
        for l in camp.get("sel_new", []):
            rows.append({"section": "bmc_sel", "item": "campagne", "key": "", "value": l})

    for idx, r in enumerate(results, 1):
        base = r.to_dict()
        rows.append({"section":"benchmark", "item":idx, "key":"name", "value":base.get("name", "")})
        rows.append({"section":"benchmark", "item":idx, "key":"category", "value":base.get("category", "")})
        rows.append({"section":"benchmark", "item":idx, "key":"status", "value":base.get("status", "")})
        rows.append({"section":"benchmark", "item":idx, "key":"score", "value":base.get("score", "")})
        rows.append({"section":"benchmark", "item":idx, "key":"unit", "value":base.get("unit", "")})
        rows.append({"section":"benchmark", "item":idx, "key":"duration_s", "value":round(base.get("duration_s", 0) or 0, 2)})
        rows.append({"section":"benchmark", "item":idx, "key":"timestamp", "value":base.get("timestamp", "")})
        if base.get("notes"):
            rows.append({"section":"benchmark", "item":idx, "key":"notes", "value":base.get("notes", "")})
        bm = base.get("metrics") or {}
        for row in _electrical_rows(bm.get("electrical") if (EXPORT_SEQUENCE_ELECTRONICS and isinstance(bm.get("electrical"), dict)) else {}):
            rows.append({"section": "electrical", "item": idx, "key": f"{row[0]} [{row[1]}]",
                         "value": f"min {row[3]} / moy {row[4]} / max {row[5]} {row[6]}" + (f" — {row[7]}" if row[7] else "")})
        inv = bm.get("inventory_diff") if isinstance(bm.get("inventory_diff"), dict) else {}
        if inv:
            rows.append({"section": "inventory", "item": idx, "key": "resultat",
                         "value": "identique" if inv.get("identical") else f"{len(inv.get('findings', []))} écart(s)"})
            for f in inv.get("findings", []):
                rows.append({"section": "inventory", "item": idx, "key": f["level"], "value": f["label"] + (f" — {f['detail']}" if f.get("detail") else "")})
            for l in inv.get("sel_new", []):
                rows.append({"section": "bmc_sel", "item": idx, "key": "", "value": l})
        for prow in _pcie_link_rows(bm.get("pcie_links") if isinstance(bm.get("pcie_links"), dict) else {}):
            rows.append({"section": "pcie_links", "item": idx, "key": prow[0],
                         "value": f"max {prow[2]} · observé {prow[3]} · au max {prow[4]}" + (f" — {prow[5]}" if prow[5] else "")})
        if not condensed:
            for mk, mv in (base.get("metrics") or {}).items():
                if mk in ("electrical", "inventory_diff", "pcie_links"):
                    continue
                if mk == "jobs_details" and isinstance(mv, list) and len(mv) == 1:
                    continue         # un seul disque : doublon des métriques de tête (anciens rapports) ; JSON complet inchangé
                if isinstance(mv, list) and mv and all(isinstance(x, dict) for x in mv):
                    # liste de dictionnaires (ex. jobs_details, un par disque) : valeurs simples ligne par ligne, séries laissées au JSON/HTML
                    # (une cellule de plusieurs centaines de Ko dépasserait la limite d'Excel : 32 767 caractères)
                    for i_d, dd in enumerate(mv):
                        for dk, dv in dd.items():
                            if str(dk).startswith("series") or isinstance(dv, (list, dict)) or dv in ("", None):
                                continue
                            rows.append({"section":"metric", "item":idx, "key":f"{mk}[{i_d}].{dk}", "value":_safe_text(dv)})
                elif isinstance(mv, list) and len(mv) > 20:
                    rows.append({"section":"metric", "item":idx, "key":mk, "value":f"{len(mv)} points - voir JSON/HTML"})
                elif isinstance(mv, dict) and (mk == "series" or len(json.dumps(mv, default=str)) > 2000):
                    # Les séries complètes (plusieurs centaines de Ko) dépassent la limite d'une cellule
                    # Excel (32 767 caractères) : on n'exporte qu'un résumé, le détail est dans JSON/HTML.
                    rows.append({"section":"metric", "item":idx, "key":mk, "value":f"{len(mv)} séries - voir JSON/HTML"})
                else:
                    rows.append({"section":"metric", "item":idx, "key":mk, "value":_safe_text(mv)})
    with open(path, "w", newline="", encoding="utf-8") as f:
        fields = ["section", "item", "key", "value"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(rows)
    return path

def export_dmesg_file(out_dir: str, report_meta: dict = None, lines: int = 300) -> str:
    """Écrit dmesg dans un fichier séparé, sans l'injecter aux exports."""
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"dmesg_{_ts()}.txt")
    log = collect_dmesg_log(lines=lines)
    with open(path, "w", encoding="utf-8") as f:
        f.write("BenchSuite - dmesg\n")
        f.write(f"Système: {_report_system_name(report_meta)}\n")
        f.write(f"Capture: {log.get('captured_at', '')}\n")
        f.write(f"Commande: {log.get('command', '')}\n")
        f.write(f"Statut: {log.get('status', '')}\n")
        f.write(f"Code retour: {log.get('returncode', '')}\n")
        f.write(f"Lignes dmesg analysees: {log.get('line_count', 0)}\n")
        f.write(f"Erreurs dmesg detectees: {log.get('error_line_count', 0)}\n")
        if log.get("note"):
            f.write(f"Note: {log.get('note')}\n")
        if log.get("error"):
            f.write("\n--- erreur ---\n")
            f.write(str(log.get("error", "")).rstrip() + "\n")
        if log.get("status") != "ok":
            f.write("\n--- dmesg non capture ---\n")
            f.write("Aucun journal kernel n'a ete lu car la commande dmesg a echoue.\n")
        elif log.get("error_lines"):
            f.write("\n--- dmesg erreurs uniquement ---\n")
            f.write(str(log.get("error_lines", "")).rstrip() + "\n")
            f.write("\nNote: des erreurs ont ete detectees, le dmesg complet n'est pas recopie ici pour garder le fichier lisible.\n")
        else:
            f.write("\n--- dmesg complet recent (aucune erreur detectee) ---\n")
            f.write(str(log.get("output", "")).rstrip() + "\n")
    return path


def export_all(results: List[BenchmarkResult], out_dir: str, report_meta: dict = None,
               system_info_data: dict = None) -> dict:
    """Exporte tous les formats dans un dossier unique avec dmesg séparé."""
    base_name = format_report_dir_name(report_meta)
    report_dir = os.path.join(out_dir, base_name)
    os.makedirs(report_dir, exist_ok=True)
    return {
        "dossier": report_dir,
        "csv": export_csv(results, report_dir, report_meta, system_info_data),
        "json": export_json(results, report_dir, report_meta, system_info_data),
        "pdf": export_pdf(results, report_dir, system_info_data=system_info_data, report_meta=report_meta),
        "html": export_html(results, report_dir, report_meta, system_info_data),
        "dmesg": export_dmesg_file(report_dir, report_meta),
    }
