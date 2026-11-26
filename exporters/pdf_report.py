# -*- coding: utf-8 -*-
"""Export des rapports BenchSuite au format PDF (ReportLab)."""

import html as _html
import json
import math
import os
from typing import List

from core.result import BenchmarkResult
from exporters.common import (
    _GPU_EXTRA_KEYS,
    _GPU_METRIC_RE,
    _electrical_rows,
    _fio_matrix,
    _fmt_num,
    _gpu_extra_text,
    _gpu_labels,
    _gpu_list_from,
    _gpu_rows_for_result,
    _gpu_table,
    _hardware_fault_alerts,
    _metric_rows,
    _non_gpu_metric_items,
    _observation_alerts,
    _pcie_alerts,
    _pcie_link_rows,
    _report_payload,
    _safe_text,
    _seq_elec_enabled,
    _sequence_entity,
    _series_for_result,
    _system_sections,
    _ts,
    unique_result_names,
)


def _thermal_line_chart(metrics: dict, series_keys: list,
                        series_labels: list, series_colors: list,
                        title: str, width_pts: float, height_pts: float):
    """
    Graphique générique de séries temporelles (Temp, Power/Freq, Load…).
    series_keys   = ["series_temp", "series_power", "series_freq_graphics"]
    series_labels = ["Temp (°C)", "Power (W)", "GPU MHz"]
    series_colors = ["#f38ba8", "#f97316", "#22d3ee"]
    """
    try:
        from reportlab.graphics.shapes import Drawing, Rect, String, Line, PolyLine
        from reportlab.lib import colors
    except ImportError:
        return None

    t_vals = metrics.get("series_t", [])
    all_series = [metrics.get(k, []) for k in series_keys]

    if not t_vals or len(t_vals) < 2:
        return None

    def _nice_num(x: float, do_round: bool) -> float:
        # Standard "nice number" helper for readable tick spacing.
        if x <= 0 or not math.isfinite(x):
            return 1.0
        expv = math.floor(math.log10(x))
        f = x / (10 ** expv)
        if do_round:
            if f < 1.5:
                nf = 1.0
            elif f < 3.0:
                nf = 2.0
            elif f < 7.0:
                nf = 5.0
            else:
                nf = 10.0
        else:
            if f <= 1.0:
                nf = 1.0
            elif f <= 2.0:
                nf = 2.0
            elif f <= 5.0:
                nf = 5.0
            else:
                nf = 10.0
        return nf * (10 ** expv)

    def _nice_bounds(vmin_raw: float, vmax_raw: float, ticks: int = 4):
        if not (math.isfinite(vmin_raw) and math.isfinite(vmax_raw)):
            return 0.0, 1.0, 1.0/3.0
        if vmax_raw == vmin_raw:
            pad = max(1.0, abs(vmax_raw) * 0.05)
            vmin_raw -= pad
            vmax_raw += pad

        rng = vmax_raw - vmin_raw
        pad = rng * 0.05
        vmin_raw -= pad
        vmax_raw += pad
        rng = vmax_raw - vmin_raw
        
        intervals = max(1, ticks - 1)
        step = _nice_num(rng / intervals, do_round=True)
        vmin = math.floor(vmin_raw / step) * step
        vmax = vmin + intervals * step
        
        while vmax < vmax_raw:
            step = _nice_num(step * 1.5, do_round=True)
            vmin = math.floor(vmin_raw / step) * step
            vmax = vmin + intervals * step

        return vmin, vmax, step

    COL_BG     = colors.HexColor("#1e1e2e")
    COL_PANEL  = colors.HexColor("#181825")
    COL_BORDER = colors.HexColor("#313244")
    COL_GREY   = colors.HexColor("#6c7086")
    COL_TEXT   = colors.HexColor("#cdd6f4")
    COL_TITLE  = colors.HexColor("#f97316")

    n_panels  = len(series_keys)
    margin    = 8
    label_w   = 42
    top_gap   = 38
    full_h    = height_pts + top_gap
    panel_h   = (full_h - top_gap - margin * (n_panels + 1)) / n_panels

    d = Drawing(width_pts, full_h)
    d.add(Rect(0, 0, width_pts, full_h, fillColor=COL_BG, strokeColor=None))
    d.add(String(width_pts / 2, full_h - 14, title,
                 fontSize=11, fontName="Helvetica-Bold",
                 fillColor=COL_TITLE, textAnchor="middle"))

    # Légende
    lx = margin + label_w
    for i, (lbl, hex_col) in enumerate(zip(series_labels, series_colors)):
        col = colors.HexColor(hex_col)
        d.add(Rect(lx + i * 85, full_h - 28, 12, 7, fillColor=col, strokeColor=None))
        d.add(String(lx + i * 85 + 15, full_h - 21, lbl,
                     fontSize=8, fillColor=COL_TEXT, textAnchor="start"))

    t_min = t_vals[0]
    t_max = max(t_vals[-1], t_min + 1)

    for panel_i, (vals, lbl, hex_col) in enumerate(zip(all_series, series_labels, series_colors)):
        if not vals: continue
        num_vals = [float(v) for v in vals if isinstance(v, (int, float)) and math.isfinite(float(v))]
        if not num_vals: continue
        col   = colors.HexColor(hex_col)
        py    = margin + (n_panels - 1 - panel_i) * (panel_h + margin)
        vmin_data, vmax_data = min(num_vals), max(num_vals)
        vmin, vmax, v_step = _nice_bounds(vmin_data, vmax_data, ticks=4)

        d.add(Rect(margin, py, width_pts - 2*margin, panel_h,
                   fillColor=COL_PANEL, strokeColor=COL_BORDER, strokeWidth=0.5))
        d.add(String(margin + 4, py + panel_h - 10, lbl,
                     fontSize=7, fontName="Helvetica-Bold",
                     fillColor=COL_GREY, textAnchor="start"))

        px    = margin + label_w
        pw    = width_pts - 2*margin - label_w - 4
        plot_y = py + 12
        ph    = panel_h - 22
        v_rng = vmax - vmin if vmax != vmin else 1.0

        for gi in range(4):
            gy = plot_y + ph * (1 - gi / 3)
            d.add(Line(px, gy, px + pw, gy, strokeColor=COL_BORDER, strokeWidth=0.3))
            gv = vmax - v_step * gi
            d.add(String(px - 3, gy - 3, f"{gv:.0f}",
                         fontSize=6, fillColor=COL_GREY, textAnchor="end"))

        pts_xy = []
        for t, v in zip(t_vals, vals):
            if not isinstance(v, (int, float)) or not math.isfinite(float(v)):
                continue
            pts_xy.extend([
                px + pw * (t - t_min) / (t_max - t_min),
                plot_y + ph * (float(v) - vmin) / v_rng,
            ])
        if len(pts_xy) >= 4:
            d.add(PolyLine(pts_xy, strokeColor=col, strokeWidth=1.5, fillColor=None))

        # Axe temps sur le panneau du bas
        if panel_i == n_panels - 1:
            for ti in range(5):
                tv = t_min + (t_max - t_min) * ti / 4
                x  = px + pw * ti / 4
                d.add(String(x, plot_y - 8, f"{tv:.0f}s",
                             fontSize=6, fillColor=COL_GREY, textAnchor="middle"))

    return d

_PDF_GPU_COL_MM = {"GPU": 28, "TFLOPS": 15, "VRAM GB/s": 13, "Cœur max/moy °C": 17, "Hotspot max/moy °C": 18, "VRAM max °C": 13,
                   "dT hotspot moy (P95) °C": 22, "Puiss. moy W": 12, "Fréq. MHz min–max": 19, "Throttling": 14,
                   "Activité & capteurs": 52}


def _pdf_gpu_widths(headers: list, total_mm: float = 182.0) -> list:
    """Largeurs de colonnes (points) de la table GPU du PDF, proportionnelles aux colonnes présentes, total = `total_mm`."""
    from reportlab.lib.units import mm
    w = [_PDF_GPU_COL_MM.get(h, 15) for h in headers]
    k = total_mm / float(sum(w))
    return [x * k * mm for x in w]

GPU_PALETTE = ["#ff6600", "#3b82f6", "#22c55e", "#e879f9", "#facc15", "#22d3ee", "#f43f5e", "#a3a3a3",
               "#8b5cf6", "#14b8a6", "#84cc16", "#ec4899", "#06b6d4", "#fb7185", "#fdba74", "#d4d4d4"]


def _pdf_multi_gpu_charts(result: BenchmarkResult, width_pts: float, height_pts: float) -> list:
    """Graphiques PDF multi-GPU (un par grandeur : cœur, hotspot, VRAM, puissance, fréquence)."""
    try:
        from reportlab.graphics.shapes import Drawing, Rect, Line, String, PolyLine
        from reportlab.lib import colors
    except Exception:
        return []
    m = result.metrics or {}
    ser = m.get("series") if isinstance(m.get("series"), dict) else {}
    t = ser.get("t") or m.get("series_t") or []
    if not isinstance(t, list) or len(t) < 2:
        return []
    specs = [("temp", "Température cœur", "°C"), ("temp_hotspot", "Température hotspot", "°C"),
             ("temp_vram", "Température VRAM", "°C"), ("power", "Puissance", "W"),
             ("freq_graphics", "Fréquence GPU", "MHz")]
    charts = []
    for suffix, title, unit in specs:
        lines = {}
        for k, v in ser.items():
            mt = _GPU_METRIC_RE.match(str(k))
            if mt and mt.group(2) == suffix and isinstance(v, list) and len(v) >= 2:
                lines[int(mt.group(1))] = [None if x is None else float(x) for x in v]
        if not lines:
            continue
        n = len(t)
        step = max(1, n // 300)
        allv = [x for v in lines.values() for x in v if x is not None and math.isfinite(x)]
        if not allv:
            continue
        lo, hi = min(allv), max(allv)
        raw_step = max((hi - lo) / 4.0, 1.0)
        mag = 10 ** int(math.floor(math.log10(raw_step)))
        tick = next(c * mag for c in (1, 2, 2.5, 5, 10) if c * mag >= raw_step)
        lo, hi = math.floor(lo / tick) * tick, math.ceil(hi / tick) * tick
        if hi - lo < tick * 2:
            hi = lo + tick * 2
        if suffix == "power" and lo < 0:
            lo = 0.0
        n_ticks = int(round((hi - lo) / tick))
        left, right, top, bottom = 38, 8, 26, 16
        d = Drawing(width_pts, height_pts)
        d.add(Rect(0, 0, width_pts, height_pts, fillColor=colors.HexColor("#14141c"), strokeColor=colors.HexColor("#2a2a3a")))
        d.add(String(8, height_pts - 12, f"{title} ({unit})", fontName="Helvetica-Bold", fontSize=8,
                     fillColor=colors.HexColor("#ff8c33")))
        # légende compacte
        lx = width_pts - right
        for idx in sorted(lines, reverse=True):
            label = f"GPU {idx}"
            lx -= 34
            d.add(Rect(lx, height_pts - 13, 7, 4, fillColor=colors.HexColor(GPU_PALETTE[idx % len(GPU_PALETTE)]), strokeColor=None))
            d.add(String(lx + 10, height_pts - 13, label, fontName="Helvetica", fontSize=6, fillColor=colors.HexColor("#c8c8d8")))
        pw, ph = width_pts - left - right, height_pts - top - bottom
        t0, t1 = float(t[0]), float(t[-1])
        span_t = max(t1 - t0, 1e-6)
        for g in range(n_ticks + 1):
            gy = bottom + ph * g / n_ticks
            d.add(Line(left, gy, left + pw, gy, strokeColor=colors.HexColor("#2a2a3a"), strokeWidth=0.4))
            d.add(String(left - 4, gy - 2, f"{lo + tick * g:g}", fontName="Helvetica", fontSize=6,
                         fillColor=colors.HexColor("#9a9ab0"), textAnchor="end"))
        for g in range(6):
            gx = left + pw * g / 5
            d.add(String(gx, 6, f"{t0 + span_t * g / 5:.0f}s", fontName="Helvetica", fontSize=6,
                         fillColor=colors.HexColor("#9a9ab0"), textAnchor="middle"))
        for idx in sorted(lines):
            pts = []
            v = lines[idx]
            for j in range(0, min(n, len(v)), step):
                if v[j] is None:
                    continue
                pts += [left + pw * (float(t[j]) - t0) / span_t, bottom + ph * (v[j] - lo) / (hi - lo)]
            if len(pts) >= 4:
                d.add(PolyLine(pts, strokeColor=colors.HexColor(GPU_PALETTE[idx % len(GPU_PALETTE)]), strokeWidth=0.8))
        charts.append(d)
    return charts


def _simple_pdf_chart(result: BenchmarkResult, width_pts: float, height_pts: float):
    data = _series_for_result(result)
    if not data:
        return None
    keys = [x[0] for x in data["series"][:3]]
    labels = [x[1] for x in data["series"][:3]]
    colors_ = ["#f38ba8", "#f97316", "#22d3ee"][:len(keys)]
    # Réinjecter sous noms attendus par l'ancien helper.
    metrics = {"series_t": data["t"]}
    for item in data["series"][:3]:
        metrics[item[0]] = item[2]
    try:
        return _thermal_line_chart(metrics, keys, labels, colors_, f"Graphiques - {result.name}", width_pts, height_pts)
    except Exception:
        return None


def export_pdf(results: List[BenchmarkResult], out_dir: str,
               system_info_data: dict = None, report_meta: dict = None) -> str:
    results = unique_result_names(results)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"benchsuite_report_{_ts()}.pdf")
    payload = _report_payload(results, report_meta, system_info_data)
    try:
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib import colors
        from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, PageBreak, HRFlowable
        from reportlab.lib.enums import TA_CENTER
    except ImportError:
        txt = path.replace(".pdf", ".txt")
        with open(txt, "w", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False, indent=2))
        return txt

    W, H = A4
    doc = SimpleDocTemplate(path, pagesize=A4, topMargin=14*mm, bottomMargin=14*mm, leftMargin=14*mm, rightMargin=14*mm)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("BSTitle", parent=styles["Title"], textColor=colors.HexColor("#1e1e2e"), fontSize=24, alignment=TA_CENTER)
    h2 = ParagraphStyle("BSH2", parent=styles["Heading2"], textColor=colors.HexColor("#f97316"), fontSize=14, spaceBefore=8, spaceAfter=5)
    normal = ParagraphStyle("BSNormal", parent=styles["Normal"], fontSize=8, leading=10)
    story = []

    def table(rows, widths=None, repeat_rows=1):
        cooked = []
        head_style = ParagraphStyle("BSHead", parent=normal, fontName="Helvetica-Bold", fontSize=6.5, leading=7.5,
                                    textColor=colors.white)
        for i, row in enumerate(rows):
            if i == 0:
                cooked.append([Paragraph(_html.escape(_safe_text(cell)), head_style) for cell in row])
                continue
            cooked.append([Paragraph(_html.escape(_safe_text(cell)), normal) for cell in row])
        t = Table(cooked, colWidths=widths, repeatRows=repeat_rows)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,0), colors.HexColor("#313244")),
            ("TEXTCOLOR", (0,0), (-1,0), colors.white),
            ("FONTNAME", (0,0), (-1,0), "Helvetica-Bold"),
            ("FONTSIZE", (0,0), (-1,-1), 7),
            ("GRID", (0,0), (-1,-1), 0.25, colors.lightgrey),
            ("ROWBACKGROUNDS", (0,1), (-1,-1), [colors.white, colors.HexColor("#f5f5f5")]),
            ("VALIGN", (0,0), (-1,-1), "TOP"),
        ]))
        return t

    sys_only = not results          # rapport « informations système » seul : aucune page de test
    story.append(Paragraph("BenchSuite - Informations système" if sys_only else "BenchSuite - Rapport de validation", title))
    story.append(Paragraph(f"Système testé : <b>{_html.escape(payload['system']['name'])}</b>", styles["Heading2"]))
    meta = payload.get("metadata", {})
    operator = meta.get("operator", "")
    client = meta.get("client", "")
    meta_text = ""
    if meta.get("model"): meta_text += f"Modèle : <b>{_html.escape(meta['model'])}</b> &nbsp; "
    if meta.get("serial"): meta_text += f"N° de série : <b>{_html.escape(meta['serial'])}</b> &nbsp; "
    if meta.get("order_no"): meta_text += f"Commande : <b>{_html.escape(meta['order_no'])}</b> &nbsp; "
    if operator: meta_text += f"Opérateur : <b>{_html.escape(operator)}</b> &nbsp; "
    if client: meta_text += f"Client : <b>{_html.escape(client)}</b> &nbsp; "
    ambient = meta.get("ambient_temp", "")
    fan = meta.get("fan_speed", "")
    if ambient: meta_text += f"T. ambiante : <b>{_html.escape(ambient)}</b> &nbsp; "
    if fan: meta_text += f"Ventilation : <b>{_html.escape(fan)}</b>"
    
    if meta_text:
        story.append(Paragraph(meta_text.strip(" &nbsp;"), normal))
    story.append(Paragraph(f"Export : {payload['exported_at']} - Version : {payload['benchsuite_version']}", normal))
    story.append(HRFlowable(width="100%", thickness=1.5, color=colors.HexColor("#f97316")))
    story.append(Spacer(1, 5*mm))

    v = payload.get("verdict")
    if v:
        vcol = {"CONFORME": "#15803d", "A_VERIFIER": "#a16207", "NON_CONFORME": "#b91c1c", "NON_EVALUE": "#475569"}[v["status"]]
        vbg = {"CONFORME": "#dcfce7", "A_VERIFIER": "#fef9c3", "NON_CONFORME": "#fee2e2", "NON_EVALUE": "#e2e8f0"}[v["status"]]
        c = v.get("counts", {})
        vrows = [[Paragraph(f"<b><font color='{vcol}' size='13'>VERDICT : {_html.escape(v['label'])}</font></b><br/>"
                            f"<font size='7'>{c.get('pass', 0)} conforme(s) · {c.get('warn', 0)} à vérifier · {c.get('fail', 0)} non conforme(s) · "
                            f"{c.get('na', 0)} non évalué(s) — sources : {_html.escape(', '.join(v.get('sources', [])))}</font>", normal)]]
        for r in v["rules"]:
            if r["level"] in ("fail", "warn"):
                mark = "X" if r["level"] == "fail" else "!"
                det = f" — {_html.escape(r['detail'])}" if r.get("detail") else ""
                vrows.append([Paragraph(f"<b>[{mark}]</b> {_html.escape(r['label'])}{det}", normal)])
        vt = Table(vrows, colWidths=[182*mm])
        vt.setStyle(TableStyle([("BACKGROUND", (0,0), (-1,-1), colors.HexColor(vbg)), ("BOX", (0,0), (-1,-1), 1, colors.HexColor(vcol)),
                                ("PADDING", (0,0), (-1,-1), 5)]))
        story.append(vt)
        story.append(Spacer(1, 4*mm))

    # Audit matériel pour affichage dans le PDF
    from core.system_info import run_hardware_audit
    alerts = []
    if system_info_data:
        try:
            alerts = run_hardware_audit(system_info_data)
        except Exception:
            pass
    alerts = list(alerts) + _hardware_fault_alerts(results) + _observation_alerts(results, report_meta) + _pcie_alerts(results)

    diag_style = ParagraphStyle("BSDiag", parent=normal, fontSize=8, leading=10, textColor=colors.HexColor("#333333"))
    
    if alerts:
        has_error = any(a["type"] == "error" for a in alerts)
        has_warning = any(a["type"] == "warning" for a in alerts)
        
        if has_error:
            bg_color = colors.HexColor("#fee2e2") # light red
            border_color = colors.HexColor("#ef4444")
            title_text = "ALERTE : Anomalies critiques détectées dans la configuration matérielle !"
            title_text_color = "#b91c1c"
        elif has_warning:
            bg_color = colors.HexColor("#fef9c3") # light yellow
            border_color = colors.HexColor("#eab308")
            title_text = "ATTENTION : Quelques points matériels à vérifier sur le système"
            title_text_color = "#a16207"
        else:
            bg_color = colors.HexColor("#dbeafe") # light blue
            border_color = colors.HexColor("#3b82f6")
            title_text = "INFORMATION : Recommandations d'optimisation système"
            title_text_color = "#1d4ed8"

        diag_content = [
            [Paragraph(f"<b><font color='{title_text_color}'>{title_text}</font></b>", ParagraphStyle("BSDiagTitle", parent=normal, fontSize=9, leading=11, fontName="Helvetica-Bold"))]
        ]
        
        for a in alerts:
            atype = a.get("type", "warning")
            if atype == "error":
                prefix = "•"
                color = "#b91c1c"
            elif atype == "warning":
                prefix = "•"
                color = "#a16207"
            else:
                prefix = "•"
                color = "#1d4ed8"
                
            text = _pdf_safe(f"<b>{prefix} <font color='{color}'>{_html.escape(a['category'])}</font></b> — <b>{_html.escape(a['title'])}</b><br/>{_html.escape(a['desc'])}")
            if a.get("devices"):
                text += f"<br/><i>Concernés : {_html.escape(' ; '.join(a['devices']))}</i>"
            diag_content.append([Paragraph(text, diag_style)])
            
        diag_table = Table(diag_content, colWidths=[182*mm])
        diag_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), bg_color),
            ("BOX", (0,0), (-1,-1), 1, border_color),
            ("PADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        
        story.append(Paragraph("Synthèse du diagnostic matériel", h2))
        story.append(diag_table)
        story.append(Spacer(1, 5*mm))
    else:
        # Aucun problème détecté!
        bg_color = colors.HexColor("#d1fae5") # light green
        border_color = colors.HexColor("#10b981")
        
        diag_content = [
            [Paragraph("<b><font color='#065f46'>Diagnostic Matériel : Système entièrement opérationnel</font></b>", ParagraphStyle("BSDiagTitle", parent=normal, fontSize=9, leading=11, fontName="Helvetica-Bold"))],
            [Paragraph("Toutes les barrettes de RAM détectées sont homogènes et tous les périphériques PCIe clés (cartes graphiques, stockage, interfaces réseau) fonctionnent dans leur configuration et bande passante maximales théoriques.", diag_style)]
        ]
        
        diag_table = Table(diag_content, colWidths=[182*mm])
        diag_table.setStyle(TableStyle([
            ("BACKGROUND", (0,0), (-1,-1), bg_color),
            ("BOX", (0,0), (-1,-1), 1, border_color),
            ("PADDING", (0,0), (-1,-1), 6),
            ("BOTTOMPADDING", (0,0), (-1,-1), 4),
            ("TOPPADDING", (0,0), (-1,-1), 4),
            ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ]))
        
        story.append(Paragraph("Synthèse du diagnostic matériel", h2))
        story.append(diag_table)
        story.append(Spacer(1, 5*mm))

    story.append(Paragraph("Informations système au départ", h2))
    snap = payload["system"]["snapshot_at_start"]
    for sec_title, headers, sec_rows in _system_sections(snap, _gpu_list_from(system_info_data, payload)):
        story.append(Paragraph(_html.escape(str(sec_title)), styles["Heading4"]))
        if headers:
            n = len(headers)
            widths = [182 * mm / n] * n
            if sec_title.startswith("Cartes graphiques"):
                widths = [7*mm, 52*mm, 26*mm, 28*mm, 26*mm, 12*mm, 16*mm, 15*mm]
            elif sec_title.startswith("Périphériques PCIe"):
                widths = [25*mm, 17*mm, 33*mm, 17*mm, 15*mm, 15*mm, 30*mm, 13*mm, 17*mm]
            story.append(table([headers] + sec_rows, widths))
        else:
            story.append(table([["Information", "Valeur"]] + sec_rows, [55*mm, 127*mm]))
        story.append(Spacer(1, 3*mm))
    story.append(PageBreak())

    labels = _gpu_labels(_gpu_list_from(system_info_data, payload))
    tel = payload.get("telemetry_summary", {}).get("gpus", [])
    if tel:
        story.append(Paragraph("Synthèse thermique GPU (en charge stabilisée)", h2))
        with_hs = any(g.get("temp_hotspot_max_c") is not None or g.get("delta_hotspot_moy_c") is not None for g in tel)
        with_ex = any(any(g.get(k) is not None for k in _GPU_EXTRA_KEYS) for g in tel)
        head = ["GPU", "Cœur max °C"] + (["Hotspot max °C"] if with_hs else []) + ["VRAM max °C"] \
            + (["dT hotspot moy (P95)", "Statut dT"] if with_hs else []) + ["Fréq. min MHz", "Throttling"] \
            + (["Activité & capteurs"] if with_ex else [])
        trow = [head]
        for g in tel:
            d = f"+{_fmt_num(g.get('delta_hotspot_moy_c'))}" if g.get("delta_hotspot_moy_c") is not None else "—"
            if g.get("delta_hotspot_p95_c") is not None:
                d += f" ({_fmt_num(g['delta_hotspot_p95_c'])})"
            line = [g.get("label") or f"GPU {g['gpu_index']}", _fmt_num(g.get("temp_core_max_c"))]
            if with_hs:
                line.append(_fmt_num(g.get("temp_hotspot_max_c")))
            line.append(_fmt_num(g.get("temp_vram_max_c")))
            if with_hs:
                line += [d, {"critique": "> 25 °C", "avertissement": "> 20 °C", "optimal": "normal"}.get(g.get("delta_hotspot_status") or "", "—")]
            line += [_fmt_num(g.get("freq_min_mhz"), 0), f"oui ({g['throttle_samples']})" if g.get("throttle_detected") else "non"]
            if with_ex:
                line.append(_gpu_extra_text(g) or "—")
            trow.append(line)
        _w = {"GPU": 36, "Cœur max °C": 16, "Hotspot max °C": 18, "VRAM max °C": 16, "dT hotspot moy (P95)": 26, "Statut dT": 20,
              "Fréq. min MHz": 16, "Throttling": 16, "Activité & capteurs": 60}
        _k = 182.0 / sum(_w[h] for h in head)
        story.append(table(trow, [_w[h] * _k * mm for h in head]))
        story.append(Paragraph(("dT = hotspot - coeur. Seuils : &gt; 20 °C avertissement, &gt; 25 °C critique (moyenne en charge). " if with_hs else "")
                               + "Les valeurs de repos et la montée en charge (10 premières secondes) sont exclues.", normal))
        story.append(Spacer(1, 4*mm))

    if not sys_only:
        story.append(Paragraph("Résumé des benchmarks", h2))
        bench_rows = [["Benchmark", "Catégorie", "Statut", "Score", "Durée"]]
        for r in results:
            bench_rows.append([r.name, r.category, r.status, f"{r.score if r.score is not None else ''} {r.unit}", f"{r.duration_s:.2f}s"])
        story.append(table(bench_rows, [52*mm, 32*mm, 22*mm, 36*mm, 25*mm]))
        story.append(Spacer(1, 6*mm))
    camp = payload.get("inventory_campaign")
    if isinstance(camp, dict):
        story.append(Paragraph("Inventaire matériel — départ / fin de campagne", h2))
        story.append(Paragraph(f"Départ : {_html.escape(_pdf_safe(str(camp.get('before', ''))))}<br/>"
                               f"Fin : {_html.escape(_pdf_safe(str(camp.get('after', ''))))}", normal))
        rows_c = [[{"fail": "Anomalie", "warn": "À vérifier"}.get(f["level"], f["level"]), _pdf_safe(f["label"]),
                   _pdf_safe(f.get("detail", ""))] for f in camp.get("findings", [])]
        story.append(table([["Niveau", "Constat", "Détail"]] + rows_c, [22*mm, 80*mm, 80*mm]) if rows_c else
                     Paragraph("Identique : aucun périphérique perdu, aucun lien dégradé, aucune erreur apparue.", normal))
        if camp.get("sel_new"):
            story.append(table([["Journal BMC (SEL) — nouveaux événements"]] + [[_pdf_safe(l)] for l in camp["sel_new"][:40]], [182*mm]))
        story.append(Spacer(1, 6*mm))

    seq = payload.get("sequence_series") if _seq_elec_enabled() else None
    if isinstance(seq, dict) and seq.get("channels"):
        story.append(Paragraph("Courbes de la séquence — température et puissance", h2))
        for panel, ttl in (("temp", "Température (°C)"), ("power", "Puissance (W)")):
            ch = _sequence_pdf_chart(seq, panel, ttl, 182*mm, 70*mm)
            if ch:
                story.append(ch); story.append(Spacer(1, 3*mm))
        story.append(Paragraph("Une courbe par GPU, CPU, RAM et disque ; traits pointillés : hotspot GPU ; repères jaunes : début des étapes.", normal))
        rows_s = [["Courbe", "Type", "Min", "Moy.", "Max", "Dernier"]] + [
            [c["label"], "Température" if c["panel"] == "temp" else "Puissance", f"{c['min']:.0f} {c['unit']}", f"{c['mean']:.0f} {c['unit']}",
             f"{c['max']:.0f} {c['unit']}", f"{c['last']:.0f} {c['unit']}"] for c in seq["channels"]]
        story.append(table(rows_s, [50*mm, 28*mm, 26*mm, 26*mm, 26*mm, 26*mm]))
        story.append(Spacer(1, 4*mm))

    for i, r in enumerate(results, 1):
        if i > 1 or (isinstance(seq, dict) and seq.get("channels")):
            story.append(PageBreak())
        story.append(Paragraph(f"{i}. {_html.escape(str(r.name))}", h2))
        info_rows = [["Champ", "Valeur"], ["Catégorie", r.category], ["Statut", r.status],
                     ["Score", f"{r.score if r.score is not None else ''} {r.unit}"], ["Durée", f"{r.duration_s:.1f} s"],
                     ["Horodatage", r.timestamp[:19].replace("T", " ")]]
        if getattr(r, "command", ""):
            info_rows.append(["Commande", r.command])
        story.append(table(info_rows, [35*mm, 147*mm]))
        story.append(Spacer(1, 4*mm))
        gpu_rows = _gpu_rows_for_result(r, labels)
        if gpu_rows:
            story.append(Paragraph("Synthèse par GPU (en charge)", styles["Heading3"]))
            g_head, g_cells, g_vram = _gpu_table(gpu_rows)
            story.append(table([g_head] + g_cells, _pdf_gpu_widths(g_head)))
            story.append(Spacer(1, 4*mm))
        others = _non_gpu_metric_items(r.metrics)
        others.pop("command", None)
        fm = _fio_matrix(r.metrics)
        if fm:
            fheads, frows, fused = fm
            story.append(Paragraph("Résultats par test (fio)", styles["Heading3"]))
            nco = len(fheads) - 1
            wcol = max(11, min(20, int(120 / max(1, nco))))
            story.append(table([[_pdf_safe(h) for h in fheads]] + [[_pdf_safe(c) for c in row] for row in frows],
                               [(182 - wcol * nco) * mm] + [wcol * mm] * nco))
            story.append(Spacer(1, 3*mm))
            others = {k: v for k, v in others.items() if k not in fused}
        if others:
            story.append(Paragraph("Paramètres et métriques", styles["Heading3"]))
            story.append(table([["Métrique", "Valeur"]] + _metric_rows(others, 60), [65*mm, 117*mm]))
        el = r.metrics.get("electrical") if (_seq_elec_enabled() and isinstance(r.metrics.get("electrical"), dict)) else {}
        if el.get("sensors"):
            story.append(Paragraph("Électronique — relevé pendant le test", styles["Heading3"]))
            story.append(Paragraph(_html.escape(_pdf_safe(f"{el.get('sensor_count', 0)} capteur(s), sources : {', '.join(el.get('sources', []))}. "
                                                          "Relevé passif ; tolérance ATX ±5 % vérifiée seulement pour les mesures du BMC.")), normal))
            rows_e = [[_pdf_safe(str(c)) for c in row] for row in _electrical_rows(el)[:60]]
            story.append(table([["Capteur", "Source", "Type", "Min", "Moy.", "Max", "Unité", "Remarque"]] + rows_e,
                               [46*mm, 16*mm, 22*mm, 15*mm, 15*mm, 15*mm, 11*mm, 42*mm]))
            story.append(Spacer(1, 3*mm))
        inv = r.metrics.get("inventory_diff") if isinstance(r.metrics.get("inventory_diff"), dict) else {}
        if inv:
            story.append(Paragraph("Inventaire matériel avant / après", styles["Heading3"]))
            story.append(Paragraph(f"Avant : {_html.escape(_pdf_safe(str(inv.get('before', ''))))}<br/>"
                                   f"Après : {_html.escape(_pdf_safe(str(inv.get('after', ''))))}", normal))
            rows_i = [[{"fail": "Anomalie", "warn": "À vérifier"}.get(f["level"], f["level"]), _pdf_safe(f["label"]),
                       _pdf_safe(f.get("detail", ""))] for f in inv.get("findings", [])]
            if rows_i:
                story.append(table([["Niveau", "Constat", "Détail"]] + rows_i, [22*mm, 80*mm, 80*mm]))
            else:
                story.append(Paragraph("Identique : aucun périphérique perdu, aucun lien dégradé, aucune erreur apparue.", normal))
            if inv.get("sel_new"):
                story.append(Paragraph("Journal BMC (SEL) — nouveaux événements", styles["Heading4"]))
                story.append(table([["Événement"]] + [[_pdf_safe(l)] for l in inv["sel_new"][:40]], [182*mm]))
            story.append(Spacer(1, 3*mm))
        pl = r.metrics.get("pcie_links") if isinstance(r.metrics.get("pcie_links"), dict) else {}
        if pl.get("periphériques"):
            story.append(Paragraph("Liens PCIe pendant le test", styles["Heading3"]))
            story.append(Paragraph(_pdf_safe(f"{pl.get('au_maximum', 0)} périphérique(s) au maximum de leur lien, "
                                             f"{pl.get('anomalies_warn', 0)} à vérifier (relevé toutes les ~2 s)."), normal))
            rows_p = [[_pdf_safe(str(c))[:70] for c in row] for row in _pcie_link_rows(pl, True)[:30]]
            if rows_p:
                story.append(table([["Périphérique", "Type", "Maximum", "Liens observés", "Au max.", "Remarque"]] + rows_p,
                                   [50*mm, 18*mm, 20*mm, 40*mm, 12*mm, 42*mm]))
            story.append(Spacer(1, 3*mm))
        charts = _pdf_multi_gpu_charts(r, 182*mm, 46*mm) if gpu_rows else []
        if charts:
            story.append(PageBreak())
            story.append(Paragraph(f"Graphiques — {_html.escape(str(r.name))}", h2))
            for ch in charts:
                story.append(ch); story.append(Spacer(1, 3*mm))
        else:
            ch = _simple_pdf_chart(r, 182*mm, 75*mm)
            if ch:
                story.append(Spacer(1, 5*mm)); story.append(ch)
    doc.build(story)
    return path


def _pdf_safe(text: str) -> str:
    """Remplace les symboles absents de la police Helvetica du PDF (flèches, coches, ΔT…)."""
    for a, b in (("→", "->"), ("✓", "OK"), ("✗", "X"), ("⚠", "!"), ("Δ", "d"), ("≈", "~")):
        text = text.replace(a, b)
    return text

def _sequence_pdf_chart(seq: dict, panel: str, title: str, width_pts: float, height_pts: float):
    """Graphique PDF multi-courbes (une ligne par GPU/CPU/RAM/disque) avec repères d'étapes ; None si pas de données."""
    try:
        from reportlab.graphics.shapes import Drawing, Rect, String, Line, PolyLine
        from reportlab.lib import colors
    except ImportError:
        return None
    chans = [c for c in (seq or {}).get("channels", []) if c["panel"] == panel and len(c["v"]) >= 2]
    if not chans:
        return None
    pal = ["#ff6600", "#3b82f6", "#22c55e", "#e879f9", "#facc15", "#22d3ee", "#f43f5e", "#a3a3a3", "#8b5cf6", "#14b8a6", "#84cc16", "#ec4899"]
    ents = {}
    for c in seq["channels"]:
        ents.setdefault(_sequence_entity(c["label"]), pal[len(ents) % len(pal)])
    vals = [v for c in chans for v in c["v"]]
    vmin_d, vmax_d = min(vals), max(vals)
    lo0 = 0.0 if panel == "power" else max(0.0, vmin_d - 3)
    step = next((st for st in (1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 150, 200, 250, 500, 1000)
                 if (0.0 if panel == "power" else float(int(lo0 // st) * st)) + 4 * st >= vmax_d), 1000)
    lo = 0.0 if panel == "power" else float(int(lo0 // step) * step)
    hi = lo + 4 * step
    t_max = max(seq.get("duration", 1.0), max(c["t"][-1] for c in chans), 1.0)
    d = Drawing(width_pts, height_pts)
    d.add(Rect(0, 0, width_pts, height_pts, fillColor=colors.HexColor("#1e1e2e"), strokeColor=None))
    d.add(String(8, height_pts - 12, title, fontSize=9, fontName="Helvetica-Bold", fillColor=colors.HexColor("#f97316")))
    lx = 150
    for c in chans:
        if lx > width_pts - 70:
            break
        txt = f"{c['label']} {c['v'][-1]:.0f}{c['unit']}"
        d.add(Rect(lx, height_pts - 13, 10, 5, fillColor=colors.HexColor(ents[_sequence_entity(c['label'])]), strokeColor=None))
        d.add(String(lx + 13, height_pts - 12, txt, fontSize=6.5, fillColor=colors.HexColor("#cdd6f4")))
        lx += 20 + len(txt) * 3.6
    px, py, pw, ph = 34, 16, width_pts - 34 - 10, height_pts - 16 - 22
    for gi in range(5):
        y = py + ph * gi / 4
        d.add(Line(px, y, px + pw, y, strokeColor=colors.HexColor("#45475a"), strokeWidth=0.3))
        d.add(String(px - 3, y - 2, f"{lo + (hi - lo) * gi / 4:.0f}", fontSize=6, fillColor=colors.HexColor("#6c7086"), textAnchor="end"))
    for ti in range(6):
        d.add(String(px + pw * ti / 5, 5, f"{t_max * ti / 5 / 60:.0f} min" if t_max >= 120 else f"{t_max * ti / 5:.0f} s",
                     fontSize=6, fillColor=colors.HexColor("#6c7086"), textAnchor="middle"))
    for i, m in enumerate(seq.get("marks", [])):
        x = px + pw * min(1.0, m["t"] / t_max)
        d.add(Line(x, py, x, py + ph, strokeColor=colors.HexColor("#f9e2af"), strokeWidth=0.5, strokeDashArray=[3, 2]))
        d.add(String(x + 2, py + ph - 7, str(i + 1), fontSize=6, fillColor=colors.HexColor("#f9e2af")))
    for c in chans:
        pts = []
        for t, v in zip(c["t"], c["v"]):
            pts.extend([px + pw * t / t_max, py + ph * (v - lo) / (hi - lo)])
        d.add(PolyLine(pts, strokeColor=colors.HexColor(ents[_sequence_entity(c['label'])]), strokeWidth=1.1,
                       strokeDashArray=[3, 2] if c.get("dash") else None, fillColor=None))
    return d
