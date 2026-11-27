# -*- coding: utf-8 -*-
"""Export des rapports BenchSuite au format HTML interactif (Chart.js, comparaison, thèmes)."""

import html as _html
import json
import os
import re
from typing import List

from core.result import BenchmarkResult
from exporters.common import (
    _GPU_METRIC_RE,
    _HIDDEN_METRIC_KEYS,
    _collect_report_alerts,
    _electrical_rows,
    _fio_matrix,
    _gpu_col,
    _gpu_labels,
    _gpu_list_from,
    _gpu_rows_for_result,
    _gpu_table,
    _observations,
    _pcie_link_rows,
    _report_payload,
    _safe_text,
    _seq_elec_enabled,
    _system_sections,
    _ts,
    compact_raw_output,
    unique_result_names,
)


_VERDICT_COLORS = {"CONFORME": ("#a6e3a1", "rgba(166,227,161,0.12)"), "A_VERIFIER": ("#f9e2af", "rgba(249,226,175,0.12)"),
                   "NON_CONFORME": ("#f38ba8", "rgba(243,139,168,0.14)"), "NON_EVALUE": ("#94a3b8", "rgba(148,163,184,0.12)")}


def _verdict_html(v: dict) -> str:
    """Bandeau de verdict interactif pour le tableau de bord HTML (suppression d'alertes + validation manuelle CONFORME)."""
    if not v:
        return ""
    col, bg = _VERDICT_COLORS.get(v.get("status"), _VERDICT_COLORS["NON_EVALUE"])
    sym = {"fail": "✗", "warn": "⚠", "pass": "✓", "na": "·"}
    lvl_col = {"fail": "#f38ba8", "warn": "#f9e2af", "pass": "#a6e3a1", "na": "#94a3b8"}
    c = v.get("counts", {})
    mo = v.get("manual_override") if isinstance(v.get("manual_override"), dict) else None
    mo_html = ""
    if mo:
        reason = str(mo.get("reason") or "").strip()
        mo_html = (
            '<div id="verdictManualNote" class="verdict-manual-note">✓ Validé manuellement en CONFORME'
            + (f' — Motif : {_html.escape(reason)}' if reason else '')
            + '</div>'
        )
    else:
        mo_html = '<div id="verdictManualNote" class="verdict-manual-note" style="display:none"></div>'

    rules = v.get("rules") or []

    def row_issue(idx, r):
        det = f' <span style="color:#94a3b8">— {_html.escape(r["detail"])}</span>' if r.get("detail") else ""
        return (
            f'<div class="verdict-rule-row" data-rule-idx="{idx}">'
            f'<span style="color:{lvl_col.get(r["level"], "#94a3b8")}">{sym.get(r["level"], "·")} '
            f'<span style="color:#e2e8f0">{_html.escape(r["label"])}</span>{det}</span>'
            f'<button type="button" class="alert-del-btn no-print" title="Supprimer cette alerte du verdict" '
            f'onclick="dismissVerdictRule({idx})">✕</button></div>'
        )

    def row_ok(r):
        det = f' <span style="color:#94a3b8">— {_html.escape(r["detail"])}</span>' if r.get("detail") else ""
        return (f'<div style="margin:3px 0;color:{lvl_col.get(r["level"], "#94a3b8")}">{sym.get(r["level"], "·")} '
                f'<span style="color:#e2e8f0">{_html.escape(r["label"])}</span>{det}</div>')

    main = "".join(row_issue(i, r) for i, r in enumerate(rules) if r.get("level") in ("fail", "warn"))
    rest = "".join(row_ok(r) for r in rules if r.get("level") in ("pass", "na"))
    show_force = (v.get("status") != "CONFORME") and not mo
    show_cancel = bool(mo)
    show_reset = bool(mo or v.get("dismissed_rules"))
    actions = (
        '<div class="verdict-actions no-print" id="verdictActions">'
        + f'<button type="button" class="verdict-btn verdict-btn-ok" id="btnForceConforme" onclick="openManualConforme()" style="display:{"inline-flex" if show_force else "none"}">✓ Passer en CONFORME</button>'
        + f'<button type="button" class="verdict-btn" id="btnCancelConforme" onclick="cancelManualConforme()" style="display:{"inline-flex" if show_cancel else "none"}">↩ Annuler validation manuelle</button>'
        + f'<button type="button" class="verdict-btn" id="btnResetEdits" onclick="resetVerdictAndAlerts()" style="display:{"inline-flex" if show_reset else "none"}">↺ Réinitialiser</button>'
        + '</div>'
    )
    manual_form = (
        '<div id="manualConformeForm" class="manual-form no-print" style="display:none">'
        '<input type="text" id="manualConformeReason" class="search" '
        'placeholder="Motif de validation manuelle (optionnel, ex. câblage x8 prévu par le châssis)…">'
        '<button type="button" class="verdict-btn verdict-btn-ok" onclick="confirmManualConforme()">✓ Confirmer CONFORME</button>'
        '<button type="button" class="verdict-btn" onclick="closeManualConforme()">Annuler</button>'
        '</div>'
    )
    return (
        f'<div id="verdictBox" style="background:{bg};border:1px solid {col};border-radius:12px;padding:16px 20px;margin-bottom:20px">'
        f'<div style="display:flex;justify-content:space-between;align-items:flex-start;flex-wrap:wrap;gap:10px">'
        f'<div><div id="verdictTitle" style="font-size:22px;font-weight:800;color:{col}">VERDICT : {_html.escape(v["label"])}</div>{mo_html}</div>'
        f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:8px">'
        f'<div id="verdictMeta" style="font-size:12px;color:#94a3b8;text-align:right">{c.get("pass", 0)} conforme(s) · {c.get("warn", 0)} à vérifier · '
        f'{c.get("fail", 0)} non conforme(s) · {c.get("na", 0)} non évalué(s)<br>Sources : {_html.escape(", ".join(v.get("sources", [])))}</div>'
        f'{actions}</div></div>'
        f'{manual_form}'
        f'<div id="verdictRulesMain" style="margin-top:10px;font-size:13px;{"" if main else "display:none;"}">{main}</div>'
        f'<div id="verdictRulesRest">'
        + (f'<details style="margin-top:8px"><summary style="color:#94a3b8;cursor:pointer">Détail des contrôles réussis / non évalués ({c.get("pass", 0) + c.get("na", 0)})</summary>'
           f'<div style="margin-top:6px;font-size:12px">{rest}</div></details>' if rest else "")
        + '</div></div>'
    )


def _stop_card_html(summary: dict) -> str:
    """Carte « Arrêtés » (tests interrompus / ignorés) : affichée seulement s'il y en a, pour ne pas les compter comme échecs."""
    n_stop, n_skip = int(summary.get("stopped", 0) or 0), int(summary.get("skipped", 0) or 0)
    if not (n_stop or n_skip):
        return ""
    sub = " · ".join(x for x in (f"{n_stop} arrêté(s)" if n_stop else "", f"{n_skip} ignoré(s)" if n_skip else "") if x)
    return ('<div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:#f59e0b;background:#f59e0b"></span>'
            f'Interrompus</div><div class="stat-val">{n_stop + n_skip}</div><div class="stat-range"><span>{sub}</span>'
            '<span>pas des échecs</span></div></div>')

def _pcie_links_html(r) -> str:
    """Bloc HTML « Liens PCIe pendant le test » d'un résultat ('' si aucun relevé)."""
    pl = (r.metrics or {}).get("pcie_links") if isinstance(getattr(r, "metrics", None), dict) else None
    if not isinstance(pl, dict) or not pl.get("periphériques"):
        return ""

    def table(rows):
        body = ""
        for row in rows:
            bad = bool(row[5]) and ("largeur" in row[5] or "au repos" not in row[5])   # remarque sans réserve « au repos » = à vérifier
            body += ("<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in row[:5]) +
                     f'<td class="{"st-warn" if bad else "muted"}">{_html.escape(row[5])}</td></tr>')
        return ("<table class='gpu-table'><thead><tr><th>Périphérique</th><th>Type</th><th>Lien maximum</th><th>Liens observés (part du temps)</th>"
                f"<th>Au maximum</th><th>Remarque</th></tr></thead><tbody>{body}</tbody></table>")

    cat = getattr(r, "category", "") or ""
    main, other = _pcie_link_rows(pl, True, cat), _pcie_link_rows(pl, False, cat)
    # Même règle que le verdict : une vitesse sous le maximum est « à vérifier » pour le périphérique sollicité par ce test
    # (avant : « 0 à vérifier » affiché alors que le verdict signalait ces mêmes liens).
    from core.verdict import pcie_speed_is_warn
    n_check = n_below = 0
    for d in pl.get("periphériques", []):
        an = d.get("anomalies") or []
        if not an:
            continue
        if any(f.get("level") == "warn" or (f.get("code") == "vitesse" and pcie_speed_is_warn(cat, d.get("classe", ""))) for f in an):
            n_check += 1
        else:
            n_below += 1
    head = (f"{pl.get('au_maximum', 0)} périphérique(s) au maximum de leur lien, {n_check} à vérifier"
            + (f", {n_below} sous leur vitesse maximale (probablement au repos)" if n_below else "")
            + (f", {pl['non_evalues']} non évalué(s) (maximum inconnu)" if pl.get("non_evalues") else ""))
    out = (f'<h4 style="margin:10px 0 4px">Liens PCIe pendant le test</h4><p class="muted">{_html.escape(head)} · relevé toutes les ~2 s '
           f'sur {pl.get("duree_s", 0):.0f} s. Un périphérique peut baisser sa vitesse quand il est au repos : '
           f'seule la largeur sous le maximum est une anomalie ; la vitesse est jugée surtout pour le périphérique sollicité par le test.</p>')
    out += table(main) if main else ""
    if other:
        out += f"<details><summary>Autres périphériques ({len(other)})</summary>{table(other)}</details>"
    return out

def _sequence_html(seq: dict) -> str:
    """Courbes température / puissance de toute la séquence + tableau récapitulatif ('' si aucune donnée)."""
    if not seq or not seq.get("channels"):
        return ""
    rows = ""
    for c in seq["channels"]:
        rows += (f"<tr><td>{_html.escape(c['label'])}</td><td>{'Température' if c['panel'] == 'temp' else 'Puissance'}</td>"
                 f"<td>{c['min']:.0f}</td><td>{c['mean']:.0f}</td><td>{c['max']:.0f}</td><td>{c['last']:.0f}</td><td>{_html.escape(c['unit'])}</td></tr>")
    dur = seq.get("duration", 0)
    return ('<div class="card" style="margin-bottom:18px"><h2>Courbes de la séquence — température et puissance</h2>'
            f'<p class="muted">Durée {dur / 60:.0f} min · une ligne par GPU, CPU, RAM et disque · traits pointillés : hotspot GPU · '
            'repères verticaux : début de chaque étape.</p>'
            '<div class="mini-chart" style="height:300px"><canvas id="seqChartTemp"></canvas></div>'
            '<div class="mini-chart" style="height:300px"><canvas id="seqChartPower"></canvas></div>'
            '<table><thead><tr><th>Courbe</th><th>Type</th><th>Min</th><th>Moy.</th><th>Max</th><th>Dernier</th><th>Unité</th></tr></thead>'
            f'<tbody>{rows}</tbody></table></div>')

def _electronics_html(results, meta: dict = None) -> str:
    """Contenu de l'onglet « Séquence &amp; électronique » ('' si aucune donnée)."""
    blocks = []
    lvl_col = {"fail": "#f38ba8", "warn": "#f9e2af", "info": "#94a3b8"}
    for bi, (title, el, inv, evs) in enumerate(_observations(results, meta)):
        parts = [f'<div class="card" style="margin-bottom:18px"><h2>{_html.escape(title)}</h2>']
        if el:
            parts.append(f'<h4 style="margin:10px 0 4px">Électronique — relevé pendant le test</h4>'
                         f'<p class="muted">{el.get("sensor_count", 0)} capteur(s) · sources : {_html.escape(", ".join(el.get("sources", [])))} '
                         f'· durée {el.get("duration_s", 0):.0f} s. Relevé passif (pas un test) ; tolérance ATX ±5 % vérifiée '
                         f'seulement pour les mesures du BMC.</p>')
            if (el.get("series") or {}).get("t"):
                parts.append(f'<div class="elec-charts" id="elecCharts{bi}" data-elec-index="{bi}" '
                             f'style="display:grid;grid-template-columns:repeat(auto-fit,minmax(420px,1fr));gap:12px"></div>')
            body = ""
            for row in _electrical_rows(el):
                warn = "hors tolérance" in row[7]
                body += "<tr>" + "".join(f"<td>{_html.escape(str(c))}</td>" for c in row[:7]) + \
                        f'<td class="{"st-warn" if warn else "muted"}">{_html.escape(row[7])}</td></tr>'
            parts.append("<table><thead><tr><th>Capteur</th><th>Source</th><th>Type</th><th>Min</th><th>Moy.</th>"
                         f"<th>Max</th><th>Unité</th><th>Remarque</th></tr></thead><tbody>{body}</tbody></table>")
        if inv:
            parts.append(f'<h4 style="margin:14px 0 4px">Inventaire matériel avant / après</h4>'
                         f'<p class="muted">Avant ({_html.escape(inv.get("taken_before", ""))}) : {_html.escape(inv.get("before", ""))}<br>'
                         f'Après ({_html.escape(inv.get("taken_after", ""))}) : {_html.escape(inv.get("after", ""))}</p>')
            if inv.get("identical"):
                parts.append('<p class="st-ok">✓ Identique : aucun périphérique perdu, aucun lien dégradé, aucune erreur apparue.</p>')
            for f in inv.get("findings", []):
                det = f' <span class="muted">— {_html.escape(f["detail"])}</span>' if f.get("detail") else ""
                parts.append(f'<div style="margin:3px 0;color:{lvl_col.get(f["level"], "#94a3b8")}">'
                             f'{"✗" if f["level"] == "fail" else "⚠"} {_html.escape(f["label"])}{det}</div>')
            if inv.get("sel_new"):
                parts.append('<h4 style="margin:12px 0 4px">Journal BMC (SEL) — nouveaux événements</h4><pre>'
                             + _html.escape("\n".join(inv["sel_new"])) + "</pre>")
        if evs:
            parts.append('<h4 style="margin:12px 0 4px">Journal noyau — événements matériels pendant le test</h4><pre>'
                         + _html.escape("\n".join(f"t+{e.get('t', '?')} s [{e.get('severity', '')}] {e.get('category', '')} : "
                                                    f"{e.get('line', '')}" for e in evs[:200])) + "</pre>")
        parts.append("</div>")
        blocks.append("".join(parts))
    return "".join(blocks)


def _electronics_chart_data(results, meta: dict = None) -> list:
    """Séries électroniques pour les graphiques HTML, dans l'ordre des blocs de l'onglet."""
    data = []
    for _, el, _, _ in _observations(results, meta):
        ser = (el or {}).get("series") or {}
        data.append({"series": ser, "labels": (el or {}).get("series_labels", {})} if ser.get("t") else None)
    return data


# Nombre maximal de points par courbe dans le rapport HTML (les séries complètes restent dans le fichier JSON exporté) :
# 8 GPU × 12 capteurs × 1 h à 1 Hz = plusieurs dizaines de Mo de JSON à analyser par le navigateur. 0 = aucune réduction.
HTML_MAX_POINTS = int(os.environ.get("BENCHSUITE_HTML_MAX_POINTS", "800") or 0)


def _decimate_series(values: list, factor: int, is_time: bool = False) -> list:
    """Réduit une série d'un facteur `factor` en gardant les extrêmes : un seul échantillon par paquet, celui qui s'écarte le plus de
    la moyenne du paquet (les pics et creux de température / puissance / fréquence restent visibles) ; le temps prend la moyenne du paquet."""
    out = []
    for i in range(0, len(values), factor):
        chunk = values[i:i + factor]
        nums = [x for x in chunk if isinstance(x, (int, float)) and not isinstance(x, bool)]
        if not nums:
            out.append(chunk[0])
            continue
        mean = sum(nums) / len(nums)
        out.append(round(mean, 1) if is_time else max(nums, key=lambda x: abs(x - mean)))
    return out


def _round_series(values: list, nd: int = 2) -> list:
    """Arrondit les flottants d'une série (53.76225189252946 → 53.76) : moins d'octets, aucune perte visible sur un graphique."""
    return [round(x, nd) if isinstance(x, float) else x for x in values]


def _slim_html_payload(payload: dict, max_points: int = None) -> int:
    """Allège EN PLACE la copie du payload destinée au rapport HTML (le JSON exporté reste complet) :
    séries réduites à ≈ `max_points` points (extrêmes conservés), doublons plats `series_*` retirés, flottants arrondis.
    Traite les séries imbriquées (`metrics.series`, multi-GPU) comme les anciennes séries plates (`series_t`, `series_temp`…).
    Retourne le facteur de réduction appliqué (1 = aucune)."""
    max_points = HTML_MAX_POINTS if max_points is None else max_points
    factor_max = 1
    for item in payload.get("results", []) or []:
        m = item.get("metrics") if isinstance(item, dict) else None
        if not isinstance(m, dict):
            continue
        # Capteur absent : les anciens rapports contiennent des séries de température / puissance entièrement à 0 (et « 0.0 °C »).
        # Elles ne sont pas affichées (le JSON exporté garde les données brutes).
        for k in list(m):
            v = m[k]
            if isinstance(v, list) and v and re.match(r"^series_(temp(_s\d+)?|ram_temp|ram_power|power(_s\d+)?)$", str(k)) \
                    and not any(isinstance(x, (int, float)) and not isinstance(x, bool) and x > 0 for x in v):
                del m[k]
            elif isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0 and re.match(r"^(cpu\d+_)?temp_(max|moy)_c$", str(k)):
                del m[k]
        ser = m.get("series") if isinstance(m.get("series"), dict) else None
        if ser is not None and "t" in ser and any(re.match(r"^gpu\d+_", str(k)) for k in ser):
            for k in ("series_t", "series_temp", "series_power", "series_freq_graphics", "series_freq_memory"):
                m.pop(k, None)               # doublons des séries gpu0_* déjà présentes dans `series`
        t = (ser or {}).get("t") or m.get("series_t") or []
        n = len(t) if isinstance(t, list) else 0
        factor = -(-n // max_points) if (max_points and n > max_points) else 1

        def slim(v, is_time=False, own=False):
            f = factor
            if own and max_points and len(v) > max_points:       # liste longue sans axe de temps connu
                f = -(-len(v) // max_points)
            if f > 1:
                v = _decimate_series(v, f, is_time=is_time)
            return _round_series(v), f

        def numeric_list(v):
            return isinstance(v, list) and len(v) > 8 and all(isinstance(x, (int, float, type(None))) and not isinstance(x, bool) for x in v[:20])

        if ser is not None:
            for k, v in list(ser.items()):
                if isinstance(v, list):
                    ser[k], f = slim(v, is_time=(k == "t"))
                    factor_max = max(factor_max, f)
        for k, v in list(m.items()):
            if k != "series" and numeric_list(v) and (str(k).startswith("series_") or len(v) > (max_points or 10 ** 9)):
                m[k], f = slim(v, is_time=(k == "series_t"), own=(n == 0))
                factor_max = max(factor_max, f)
    if factor_max > 1:
        payload["series_reduction"] = {"facteur": factor_max, "points_max": max_points}
    return factor_max


def reexport_html(src_html: str, dst_html: str = None) -> str:
    """Régénère un rapport HTML existant (même ancien) avec l'exporteur actuel : séries allégées, JavaScript rapide.

    Les données sont relues dans le bloc JSON du fichier (résultats, métriques, notes, verdict, inventaire, fiche système) ; rien n'est
    relancé. Écrit `<nom>_optimise.html` à côté du fichier si `dst_html` n'est pas donné. Lève ValueError si le fichier n'est pas un rapport BenchSuite."""
    with open(src_html, encoding="utf-8", errors="replace") as f:
        raw = f.read()
    m = re.search(r'<script[^>]*id="payload"[^>]*>(.*?)</script>', raw, re.S)
    if not m:
        raise ValueError("bloc de données BenchSuite introuvable : ce fichier n'est pas un rapport HTML BenchSuite")
    payload = json.loads(m.group(1).replace("<\\/", "</"))
    import html as _h
    # sorties brutes : absentes du bloc JSON (déjà affichées dans les cartes) → relues dans les <pre> des cartes d'origine
    raws, scores = {}, {}
    for cm in re.finditer(r'<section class="bench-card" data-bench-card="(\d+)">(.*?)(?=<section class="bench-card"|</section>\s*<section id=|\Z)', raw, re.S):
        pm = re.search(r'<details[^>]*>.*?<pre[^>]*>(.*?)</pre>', cm.group(2), re.S)
        if pm:
            raws[int(cm.group(1))] = _h.unescape(pm.group(1))
        sm = re.search(r'Score : (-?[\d.]+)', cm.group(2))          # le score n'est pas dans le bloc JSON : relu dans le badge de la carte
        if sm:
            try:
                scores[int(cm.group(1))] = float(sm.group(1))
            except ValueError:
                pass
    results = []
    for idx, it in enumerate(payload.get("results", [])):
        results.append(BenchmarkResult(
            name=it.get("name", ""), category=it.get("category", ""), score=scores.get(idx, it.get("score")), unit=it.get("unit", ""),
            duration_s=it.get("duration_s", 0) or 0, raw_output=raws.get(idx, ""), metrics=it.get("metrics") or {}, status=it.get("status", "ok"),
            timestamp=it.get("timestamp", ""), system_info=it.get("system_info") or {}, notes=it.get("notes", ""), command=it.get("command", "")))
    meta = dict(payload.get("metadata") or {})
    if isinstance(payload.get("verdict"), dict):
        meta["verdict"] = payload["verdict"]                       # verdict d'origine (critères / référence éventuels)
    if isinstance(payload.get("diagnostic_alerts"), list):
        meta["diagnostic_alerts"] = payload["diagnostic_alerts"]   # alertes d'origine (y compris suppressions éventuelles)
    if isinstance(payload.get("inventory_campaign"), dict):
        meta["inventory_diff"] = payload["inventory_campaign"]
    snapshot = (payload.get("system") or {}).get("snapshot_at_start") or {}
    out_dir = os.path.dirname(os.path.abspath(dst_html or src_html))
    path = export_html(results, out_dir, meta, snapshot)           # nom horodaté, renommé ci-dessous
    target = dst_html or (os.path.splitext(os.path.abspath(src_html))[0] + "_optimise.html")
    # Pour les anciens rapports sans diagnostic_alerts dans le payload : reprendre l'en-tête du tableau de bord tel quel,
    # car l'audit matériel d'origine dépendait de la machine d'export (lecture sysfs en direct).
    if "diagnostic_alerts" not in payload:
        marker_start, marker_end = '<section id="overview" class="view active">', '<div class="stat-grid">'
        with open(path, encoding="utf-8") as f:
            new_html = f.read()
        os_, oe_ = raw.find(marker_start), raw.find(marker_end, raw.find(marker_start))
        ns_, ne_ = new_html.find(marker_start), new_html.find(marker_end, new_html.find(marker_start))
        if min(os_, oe_, ns_, ne_) >= 0:
            new_html = new_html[:ns_] + raw[os_:oe_] + new_html[ne_:]
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_html)
    os.replace(path, target)
    return target


def _default_theme() -> str:
    """Thème d'ouverture du rapport : celui choisi dans le logiciel (sombre par défaut)."""
    try:
        from core import theme
        return theme.get_mode()
    except Exception:
        return "dark"


def _add_light_theme(template: str) -> str:
    """Ajoute au gabarit HTML une feuille de style CLAIRE (la feuille sombre avec la luminosité de chaque couleur inversée), désactivée par défaut
    (`media="not all"`) : le bouton « thème » du rapport l'active (`media="all"`). Aucune règle à maintenir en double. Gabarit sans feuille : inchangé."""
    try:
        from core import theme
        m = re.search(r"(<style>\n)(.*?)(</style></head>)", template, re.S)
        if not m:
            return template
        light = theme.invert_css(m.group(2)) + theme.HTML_LIGHT_EXTRA
        return template[:m.end(2) + len("</style>")] + '<style id="lightCss" media="not all">\n' + light + "</style></head>" + template[m.end(3):]
    except Exception:
        return template


_CHARTJS_ASSETS = (
    ("chart.umd.min.js", "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"),
    ("hammer.min.js", "https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"),
    ("chartjs-plugin-zoom.min.js", "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"),
)
_CHARTJS_SCRIPTS_CACHE = None


def _chart_scripts_html() -> str:
    """Retourne les balises <script> Chart.js + zoom embarquées depuis third_party/chartjs/ (mode hors ligne),
    avec repli vers le CDN si un fichier local est absent."""
    global _CHARTJS_SCRIPTS_CACHE
    if _CHARTJS_SCRIPTS_CACHE is not None:
        return _CHARTJS_SCRIPTS_CACHE
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    asset_dir = os.path.join(root, "third_party", "chartjs")
    tags = []
    for fname, cdn_url in _CHARTJS_ASSETS:
        fpath = os.path.join(asset_dir, fname)
        content = ""
        if os.path.isfile(fpath):
            try:
                with open(fpath, encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                content = ""
        if content.strip():
            # Sécurité parser HTML : empêcher toute fermeture prématurée de balise </script>
            safe_js = re.sub(r"</script", r"<\/script", content, flags=re.IGNORECASE)
            tags.append(f"<script>\n{safe_js}\n</script>")
        else:
            tags.append(f'<script src="{cdn_url}"></script>')
    _CHARTJS_SCRIPTS_CACHE = "\n".join(tags)
    return _CHARTJS_SCRIPTS_CACHE


def export_html(results: List[BenchmarkResult], out_dir: str, report_meta: dict = None,
                system_info_data: dict = None) -> str:
    results = unique_result_names(results)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, f"benchsuite_report_{_ts()}.html")
    payload = _report_payload(results, report_meta, system_info_data)

    # Audit matériel et alertes dédupliquées (ou reprises telles quelles si fournies par un ré-export)
    if isinstance((report_meta or {}).get("diagnostic_alerts"), list):
        alerts = list(report_meta["diagnostic_alerts"])
    else:
        alerts = _collect_report_alerts(results, system_info_data, report_meta)

    html_payload = json.loads(json.dumps(payload, ensure_ascii=False))
    html_payload["diagnostic_alerts"] = alerts
    _slim_html_payload(html_payload)        # séries réduites + arrondies pour l'affichage (le JSON exporté reste complet)
    for item in html_payload.get("results", []):
        item.pop("score", None)
        item["raw_output"] = ""   # déjà affichée dans la carte du benchmark (évite ~190 Ko de doublon)
        el = (item.get("metrics") or {}).get("electrical")
        if isinstance(el, dict):
            el.pop("series", None)   # courbes fournies à part (elecData) pour l'onglet Électronique
    data_json = json.dumps(html_payload, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")

    if _seq_elec_enabled():
        electronics_html = _sequence_html(payload.get("sequence_series")) + _electronics_html(results, report_meta)
        elec_json = json.dumps(_electronics_chart_data(results, report_meta), ensure_ascii=False).replace("</", "<\\/")
    else:
        electronics_html, elec_json = "", "[]"      # onglet retiré du rapport (voir EXPORT_SEQUENCE_ELECTRONICS)

    failed_count = sum(1 for r in results if r.status in ("failed", "error"))
    has_error = any(a.get("type") == "error" for a in alerts) if alerts else False
    has_warning = any(a.get("type") == "warning" for a in alerts) if alerts else False


    tel_gpus = (payload.get("telemetry_summary") or {}).get("gpus", [])
    thermal_card = ""
    if tel_gpus:
        def _pill(text, color, bg):
            return (f'<span style="background: {bg}; border: 1px solid {color}; color: {color}; '
                    f'padding: 5px 12px; border-radius: 8px; font-size: 13px; font-weight: 600;">{text}</span>')

        def _g(g):
            return _html.escape(g.get("label") or f"GPU {g['gpu_index']}")

        def _max_of(key):
            c = [g for g in tel_gpus if g.get(key) is not None]
            return max(c, key=lambda g: g[key]) if c else None

        core_g, hs_g, vram_g = _max_of("temp_core_max_c"), _max_of("temp_hotspot_max_c"), _max_of("temp_vram_max_c")
        with_delta = [g for g in tel_gpus if g.get("delta_hotspot_moy_c") is not None]
        worst = max(with_delta, key=lambda g: g["delta_hotspot_moy_c"]) if with_delta else None
        n_crit = sum(1 for g in with_delta if g.get("delta_hotspot_status") == "critique")
        n_warn = sum(1 for g in with_delta if g.get("delta_hotspot_status") == "avertissement")
        temp_bits = []
        if core_g: temp_bits.append(f"Cœur max <b>{core_g['temp_core_max_c']:.0f} °C</b> ({_g(core_g).split(' · ')[0]})")
        if hs_g: temp_bits.append(f"Hotspot max <b>{hs_g['temp_hotspot_max_c']:.0f} °C</b> ({_g(hs_g).split(' · ')[0]})")
        if vram_g: temp_bits.append(f"VRAM max <b>{vram_g['temp_vram_max_c']:.0f} °C</b> ({_g(vram_g).split(' · ')[0]})")

        delta_badge = ""
        if worst:
            if n_crit:
                col, bg, lab = "#f38ba8", "rgba(243,139,168,0.15)", f"{n_crit} GPU avec écart &gt; 25 °C"
            elif n_warn:
                col, bg, lab = "#f9e2af", "rgba(249,226,175,0.15)", f"{n_warn} GPU avec écart &gt; 20 °C"
            else:
                col, bg, lab = "#a6e3a1", "rgba(166,227,161,0.15)", "Écart optimal"
            delta_badge = _pill(f'ΔT hotspot − cœur (en charge) : pire <b>+{worst["delta_hotspot_moy_c"]:.1f} °C</b> '
                                f'({_g(worst).split(" · ")[0]}) · {lab}', col, bg)
        th_gpus = [g for g in tel_gpus if g.get("throttle_detected")]
        if th_gpus:
            txt = ", ".join(_g(g).split(" · ")[0] for g in th_gpus)
            n_s = sum(int(g.get("throttle_samples") or 0) for g in th_gpus)
            th_badge = _pill(f"⚠️ Throttling thermique détecté ({txt} · {n_s} éch.)", "#f38ba8", "rgba(243,139,168,0.15)")
        else:
            th_badge = _pill("🛡️ Aucun throttling thermique signalé par le pilote", "#a6e3a1", "rgba(166,227,161,0.15)")
        thermal_card = (
            f'<div style="background: rgba(255,102,0,0.08); border: 1px solid rgba(255,102,0,0.4); '
            f'border-radius: 10px; padding: 12px 16px; margin-top: 14px; display: flex; '
            f'justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;">'
            f'<div><strong style="color: #ff6600; font-size: 14px;">🔥 Synthèse thermique &amp; fréquences GPU</strong>'
            f'<p style="color: #cbd5e1; font-size: 13px; margin-top: 4px;">{" · ".join(temp_bits)}</p></div>'
            f'<div style="display: flex; gap: 8px; flex-wrap: wrap; align-items: center;">{delta_badge}{th_badge}</div>'
            f'</div>'
        )


    thermal_slot = f'<div id="thermalCardSlot">{thermal_card}</div>' if thermal_card else '<div id="thermalCardSlot"></div>'
    diagnostic_html = ""
    if alerts:
        if has_error:
            box_style = "background-color: #2d191e; border: 1px solid #ef4444; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;"
            title_text = "ALERTE : Anomalies critiques détectées dans la configuration matérielle !"
            title_color = "#fca5a5"
        elif has_warning:
            box_style = "background-color: #2b2210; border: 1px solid #f59e0b; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;"
            title_text = "ATTENTION : Quelques points matériels à vérifier sur le système"
            title_color = "#fef08a"
        else:
            box_style = "background-color: #112233; border: 1px solid #3b82f6; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;"
            title_text = "INFORMATION : Recommandations d'optimisation système"
            title_color = "#93c5fd"

        items_html = ""
        for a_idx, a in enumerate(alerts):
            atype = a.get("type", "warning")
            if atype == "error":
                lbl_color = "#fca5a5"
            elif atype == "warning":
                lbl_color = "#fef08a"
            else:
                lbl_color = "#93c5fd"

            items_html += (
                f'<div class="diag-alert-item" data-alert-idx="{a_idx}">'
                f'<div class="diag-alert-body">'
                f'<strong style="color: {lbl_color};">• {_html.escape(str(a["category"]))}</strong> — <strong>{_html.escape(str(a["title"]))}</strong>'
                f'<p style="color: #cbd5e1; font-size: 13px; margin-top: 4px; line-height: 1.4;">{_html.escape(str(a["desc"]))}</p>'
                + (f'<p style="color: #94a3b8; font-size: 12px; margin-top: 2px;">Concernés : {_html.escape(" ; ".join(a["devices"]))}</p>' if a.get("devices") else "")
                + '</div>'
                f'<button type="button" class="alert-del-btn no-print" title="Supprimer cette alerte" '
                f'onclick="dismissDiagAlert({a_idx})">✕</button>'
                f'</div>'
            )

        clear_btn = (
            '<button type="button" class="verdict-btn no-print" id="btnDismissAllDiag" '
            'onclick="dismissAllDiagAlerts()" title="Supprimer toutes les alertes matérielles">✕ Tout supprimer</button>'
        )
        diagnostic_html = (
            f'<div id="diagnosticBox" style="{box_style}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">'
            f'<h3 id="diagTitle" style="color: {title_color}; font-size: 16px; font-weight: 700; margin: 0;">{title_text}</h3>'
            f'<div class="verdict-actions no-print" id="diagActions">{clear_btn}'
            f'<button type="button" class="verdict-btn no-print" id="btnResetDiagOnly" onclick="resetVerdictAndAlerts()" style="display:none">↺ Rétablir les alertes</button>'
            f'</div></div>'
            f'<hr style="border: 0; border-top: 1px solid rgba(255,255,255,0.1); margin: 8px 0;"/>'
            f'<div id="diagAlertsList">{items_html}</div>'
            f'{thermal_slot}'
            f'</div>'
        )
    else:
        box_style = "background-color: #112615; border: 1px solid #10b981; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;"
        diagnostic_html = (
            f'<div id="diagnosticBox" style="{box_style}">'
            f'<div style="display:flex;justify-content:space-between;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:8px;">'
            f'<h3 id="diagTitle" style="color: #a7f3d0; font-size: 16px; font-weight: 700; margin: 0;">Diagnostic Matériel : Système entièrement opérationnel</h3>'
            f'<div class="verdict-actions no-print" id="diagActions">'
            f'<button type="button" class="verdict-btn no-print" id="btnDismissAllDiag" onclick="dismissAllDiagAlerts()" style="display:none">✕ Tout supprimer</button>'
            f'<button type="button" class="verdict-btn no-print" id="btnResetDiagOnly" onclick="resetVerdictAndAlerts()" style="display:none">↺ Rétablir les alertes</button>'
            f'</div></div>'
            f'<hr style="border: 0; border-top: 1px solid rgba(255,255,255,0.1); margin: 8px 0;"/>'
            f'<div id="diagAlertsList"><p style="color: #cbd5e1; font-size: 13px; line-height: 1.4;">Toutes les barrettes de RAM détectées sont homogènes et tous les périphériques PCIe clés (cartes graphiques, stockage, interfaces réseau) fonctionnent dans leur configuration et bande passante maximales théoriques.</p></div>'
            f'{thermal_slot}'
            f'</div>'
        )

    def _has_nested_gpu_series(metrics: dict) -> bool:
        nested = (metrics or {}).get("series")
        if not isinstance(nested, dict):
            return False
        return any(re.match(r"^gpu\d+_", str(k)) and isinstance(v, list) and v
                   for k, v in nested.items())

    def _hide_html_metric_key(metrics: dict, key: str) -> bool:
        if key == "jobs_details" and isinstance(metrics.get(key), list) and len(metrics[key]) == 1:
            return True        # un seul disque : mêmes chiffres que les métriques de tête (anciens rapports)
        # Toujours masquer les tableaux de séries temporelles car
        # Chart.js s'en occupe et les afficher en texte brut casse le HTML.
        if key in {
            "series",
            "electrical",
            "inventory_diff",
            "pcie_links",
            "series_t",
            "series_temp",
            "series_power",
            "series_freq_graphics",
            "series_freq_memory",
        }:
            return True
            
        if "thermal_limit" in key or "seuil_throttling" in key or "limite_thermique" in key:
            return True

        if not _has_nested_gpu_series(metrics):
            return False

        return key in {
            "t", "time", "secs"
        }

    system_snapshot = payload["system"]["snapshot_at_start"] or {}
    gpu_inventory = system_snapshot.get("gpu")
    if not isinstance(gpu_inventory, list):
        gpu_inventory = system_snapshot.get("gpus") if isinstance(system_snapshot.get("gpus"), list) else []

    def _gpu_display_name(idx: int) -> str:
        gpu = None
        for item in gpu_inventory:
            if isinstance(item, dict) and str(item.get("index", item.get("id", ""))) == str(idx):
                gpu = item
                break
        if gpu is None and 0 <= idx < len(gpu_inventory) and isinstance(gpu_inventory[idx], dict):
            gpu = gpu_inventory[idx]
        if isinstance(gpu, dict):
            pci = gpu.get("pci_bus") or ""
            if pci.startswith("00000000:"):
                pci = "0000:" + pci[9:]
            elif pci.startswith("0000:"):
                pci = pci[5:]
            mcio = gpu.get("mcio") or ""
            parts = []
            if pci:
                parts.append(pci)
            if mcio:
                parts.append(mcio)
            extra = f" [{' · '.join(parts)}]" if parts else ""
            return f"GPU {idx}{extra}"
        return f"GPU {idx}"

    def _metric_display_label(key: str) -> str:
        m = re.match(r"^gpu(\d+)_(.+)$", str(key))
        if not m:
            return str(key)
        idx = int(m.group(1))
        gpu_name = _gpu_display_name(idx)
        raw_suffix = m.group(2).lower()
        sensor_map = {
            "temp": "Cœur",
            "temp_c": "Cœur",
            "temp_max_c": "Cœur max",
            "temp_moy_c": "Cœur moy",
            "temp_hotspot": "Hotspot",
            "temp_hotspot_max_c": "Hotspot max",
            "temp_hotspot_moy_c": "Hotspot moy",
            "hotspot": "Hotspot",
            "temp_vram": "VRAM",
            "temp_vram_max_c": "VRAM max",
            "temp_vram_moy_c": "VRAM moy",
            "vram": "VRAM",
            "power": "Puissance",
            "power_max_w": "Puissance max",
            "power_moy_w": "Puissance moy",
            "freq_graphics": "Fréq. GPU",
            "freq_graphics_max_mhz": "Fréq. GPU max",
            "freq_memory": "Fréq. VRAM",
            "freq_memory_max_mhz": "Fréq. VRAM max",
        }
        suffix = sensor_map.get(raw_suffix, raw_suffix.replace("_", " "))
        return f"{gpu_name} — {suffix}"

    def _get_benchmark_command(res: BenchmarkResult) -> str:
        cmd = getattr(res, "command", "") or ""
        if cmd:
            return str(cmd).strip()
        m = res.metrics or {}
        if m.get("command"):
            return str(m["command"]).strip()
        # Fallback intelligent de reconstruction pour les anciens rapports
        name_lower = (res.name or "").lower()
        cat_lower = (res.category or "").lower()
        target_gpus = m.get("target_gpus")
        gpu_env = f"CUDA_VISIBLE_DEVICES={target_gpus} " if target_gpus else ""
        if "gpu-fryer" in name_lower:
            t = m.get("time_s", 30)
            dtype = m.get("dtype", "float32")
            bf16 = "true" if dtype == "bfloat16" else "false"
            return f"{gpu_env}gpu-fryer {t} --use-bf16 {bf16}"
        elif "gpu-burn" in name_lower:
            t = m.get("time_s", 30)
            prec = " -d" if m.get("precision") == "double" else ""
            return f"{gpu_env}./gpu_burn {t}{prec}"
        elif "fio" in name_lower:
            if not (m.get("rw_mode") and m.get("bs")):
                return ""        # commande non enregistrée (anciens rapports) : on n'en invente pas une avec des valeurs par défaut
            rw = m.get("rw_mode", "readwrite")
            bs = m.get("bs", "128k")
            sz = m.get("size") or (f"{m.get('size_mb', 512)}M")
            rt = m.get("runtime_s", 20)
            iod = m.get("iodepth", 1)
            eng = m.get("io_engine", "sync")
            return f"fio --name=bench --rw={rw} --bs={bs} --size={sz} --runtime={rt} --iodepth={iod} --ioengine={eng} --time_based --group_reporting"
        elif "nvbandwidth" in name_lower:
            buf = m.get("buffer_size_mb", 512)
            it = m.get("iterations", 3)
            return f"{gpu_env}nvbandwidth --bufferSize {buf} --testSamples {it}"
        elif "iperf" in name_lower:
            h = m.get("host", "127.0.0.1")
            p = m.get("port", 5201)
            t = m.get("time_s", 10)
            par = m.get("parallel", 1)
            u = " -u" if "udp" in name_lower else ""
            bw = f" -b {m['bandwidth']}" if m.get("bandwidth") else ""
            return f"iperf3{u} -c {h} -p {p} -t {t}{bw} -P {par}"
        elif "sysbench" in name_lower and cat_lower in ("cpu", "cpu + ram"):
            th = m.get("threads", 4)
            t = m.get("time_s", 10)
            pr = m.get("max_prime", 20000)
            return f"sysbench cpu --threads={th} --time={t} --cpu-max-prime={pr} run"
        elif "ram" in name_lower or cat_lower == "ram":
            sz = m.get("size_mb", 256)
            vw = m.get("vm_workers") or 1
            t = m.get("time_s", 30)
            return f"stress-ng --vm {vw} --vm-bytes {sz}M --timeout {t}s"
        elif "stress-ng" in name_lower or cat_lower in ("cpu", "cpu + ram"):
            w = m.get("threads") or m.get("cpu_workers") or 4
            s = m.get("stressor") or m.get("cpu_stressor") or "cpu"
            t = m.get("time_s", 10)
            return f"stress-ng --{s} {w} --timeout {t}s --metrics-brief"
        elif "clpeak" in name_lower:
            mode = f" --{m['test_mode']}" if m.get("test_mode") and m['test_mode'] != "all-tests" else ""
            return f"{gpu_env}clpeak{mode}"
        elif "vkpeak" in name_lower:
            return f"{gpu_env}vkpeak"
        elif "dd" in name_lower:
            bs = m.get("bs", "1M")
            sz = m.get("size_mb", 512)
            conv = m.get("conv", "dsync")
            return f"dd if=/dev/zero of=_bench.tmp bs={bs} count={sz} oflag={conv} && dd if=_bench.tmp of=/dev/null bs={bs}"
        elif "glmark2" in name_lower:
            off = " --off-screen" if m.get("off_screen", True) else ""
            rend = f" {m['renderer']}" if m.get("renderer") else ""
            return f"glmark2{off}{rend}"
        return ""

    def _format_bench_params(res: BenchmarkResult) -> str:
        m = res.metrics or {}
        parts = []
        name_lower = (res.name or "").lower()
        cat_lower = (res.category or "").lower()
        is_cpu_ram = cat_lower in ("cpu", "cpu + ram")
        is_ram_test = cat_lower == "ram" or (is_cpu_ram and "ram" in name_lower)
        if m.get("profile_name") and is_cpu_ram:
            # Profil stress-ng : le nom du résultat est déjà celui du profil
            if m.get("cpu_workers"): parts.append(f"CPU: {m['cpu_workers']}")
            if m.get("cpu_stressor"): parts.append(f"Stressor: {m['cpu_stressor']}")
            if m.get("vm_workers"):
                vb = f" × {m['vm_bytes']}" if m.get("vm_bytes") else ""
                parts.append(f"VM: {m['vm_workers']}{vb}")
            if m.get("cpu_load_pct"): parts.append(f"Load: {m['cpu_load_pct']}%")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
            return " · ".join(parts) if parts else "—"
        if "sysbench" in name_lower and is_cpu_ram and not is_ram_test:
            if "threads" in m: parts.append(f"Threads: {m['threads']}")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
            if "max_prime" in m: parts.append(f"Prime: {m['max_prime']}")
        elif is_cpu_ram and not is_ram_test:
            w = m.get("cpu_workers") or m.get("threads")
            if w: parts.append(f"CPU: {w}")
            s = m.get("cpu_stressor") or m.get("stressor")
            if s: parts.append(f"Stressor: {s}")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
            if m.get("vm_workers"): parts.append(f"VM: {m['vm_workers']}")
            if m.get("cpu_load_pct"): parts.append(f"Load: {m['cpu_load_pct']}%")
        elif "sysbench" in name_lower and is_ram_test:
            if "rw_mode" in m: parts.append(f"Op: {m['rw_mode']}")
            if "access_mode" in m: parts.append(f"Accès: {m['access_mode']}")
            if "block_size" in m: parts.append(f"BS: {m['block_size']}")
        elif is_ram_test:
            if "size_mb" in m: parts.append(f"Taille: {m['size_mb']}M")
            if "pattern" in m: parts.append(f"Pat: {m['pattern']}")
            if "passes" in m: parts.append(f"Passes: {m['passes']}")
            if "vm_method" in m: parts.append(f"VM: {m['vm_method']}")
            if m.get("vm_workers"): parts.append(f"VM workers: {m['vm_workers']}")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
        elif "fio" in name_lower:
            if "rw_mode" in m: parts.append(f"Mode: {m['rw_mode']}")
            if "bs" in m: parts.append(f"BS: {m['bs']}")
            if "size" in m or "size_mb" in m:
                fio_size = m.get("size") or f"{m.get('size_mb')}M"
                parts.append(f"Taille: {fio_size}")
            if "iodepth" in m: parts.append(f"IODepth: {m['iodepth']}")
            if "numjobs" in m: parts.append(f"Jobs: {m['numjobs']}")
            if "io_engine" in m: parts.append(f"Engine: {m['io_engine']}")
        elif "dd" in name_lower:
            if "size_mb" in m: parts.append(f"Taille: {m['size_mb']}M")
            if "bs" in m: parts.append(f"BS: {m['bs']}")
            if "conv" in m: parts.append(f"Conv: {m['conv']}")
        elif "nvbandwidth" in name_lower:
            if "buffer_size_mb" in m: parts.append(f"Buf: {m['buffer_size_mb']}M")
            if "iterations" in m: parts.append(f"Iter: {m['iterations']}")
            if "target_gpus" in m and m["target_gpus"]: parts.append(f"GPUs: {m['target_gpus']}")
        elif "gpu-burn" in name_lower:
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
            if "precision" in m: parts.append(f"Prec: {m['precision']}")
            if "target_gpus" in m and m["target_gpus"]: parts.append(f"GPUs: {m['target_gpus']}")
        elif "gpu-fryer" in name_lower:
            if "dtype" in m: parts.append(f"Dtype: {m['dtype']}")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
            if "target_gpus" in m and m["target_gpus"]: parts.append(f"GPUs: {m['target_gpus']}")
        elif "clpeak" in name_lower:
            if "test_mode" in m: parts.append(f"Test: {m['test_mode']}")
            if "target_gpus" in m and m["target_gpus"]: parts.append(f"GPUs: {m['target_gpus']}")
        elif "vkpeak" in name_lower:
            if "target_gpus" in m and m["target_gpus"]: parts.append(f"GPUs: {m['target_gpus']}")
        elif "iperf" in name_lower:
            if "host" in m: parts.append(f"Host: {m['host']}")
            if "parallel" in m: parts.append(f"Flux: {m['parallel']}")
            if "bandwidth" in m: parts.append(f"BW: {m['bandwidth']}")
            if "time_s" in m: parts.append(f"Temps: {m['time_s']}s")
        if not parts:
            for k in ("threads", "time_s", "size_mb", "duration_s"):
                if k in m:
                    label = k.replace("_", " ").capitalize()
                    parts.append(f"{label}: {m[k]}")
        return " · ".join(parts) if parts else "—"

    bench_sections = []
    category_rows = {"cpu": [], "storage": [], "gpu": [], "network": []}
    category_labels = {
        "cpu": "CPU + RAM", "storage": "Stockage",
        "gpu": "GPU", "network": "Réseau",
    }
    category_colors = {
        "cpu": "#f97316", "storage": "#e879f9",
        "gpu": "#a78bfa", "network": "#34d399",
    }
    def _bucket_for_result(res: BenchmarkResult) -> str:
        cat = (res.category or "").strip().lower()
        for key, bucket in (("cpu", "cpu"), ("ram", "cpu"), ("stock", "storage"), ("gpu", "gpu"),
                            ("réseau", "network"), ("reseau", "network"), ("network", "network")):
            if key in cat:
                return bucket
        name = (res.name or "").lower()
        hay = f"{cat} {name}"
        if "gpu" in hay or "glmark" in hay:
            return "gpu"
        if "stock" in hay or "fio" in hay or re.search(r"\bdd\b", hay):
            return "storage"
        if "réseau" in hay or "reseau" in hay or "network" in hay or "iperf" in hay:
            return "network"
        return "cpu"
    def format_detailed_value(k, v):
        if isinstance(v, list):
            if not v:
                return "série vide"
            try:
                numeric_v = []
                for x in v:
                    if x is not None:
                        if isinstance(x, (int, float)):
                            numeric_v.append(float(x))
                        else:
                            try:
                                numeric_v.append(float(x))
                            except ValueError:
                                pass
                if numeric_v:
                    min_val = min(numeric_v)
                    max_val = max(numeric_v)
                    avg_val = sum(numeric_v) / len(numeric_v)
                    
                    unit = ""
                    k_lower = str(k).lower()
                    if "temp" in k_lower:
                        unit = " °C"
                    elif "power" in k_lower or "watt" in k_lower or "puissance" in k_lower:
                        unit = " W"
                    elif "freq" in k_lower or "clock" in k_lower or "mhz" in k_lower:
                        unit = " MHz"
                    elif "gb_s" in k_lower or "bandwidth" in k_lower or "throughput" in k_lower or "mb_s" in k_lower or "rate" in k_lower:
                        unit = " MB/s"
                    
                    return f"série {len(v)} points (Min: {min_val:.1f}{unit}, Max: {max_val:.1f}{unit}, Moy: {avg_val:.1f}{unit})"
            except Exception:
                pass
            return f"série {len(v)} points"
        if isinstance(v, dict):
            return json.dumps(v, ensure_ascii=False)[:300]
        return _safe_text(v)

    def get_all_rows(metrics_dict):
        flat_rows = []
        for k, v in metrics_dict.items():
            if _hide_html_metric_key(metrics_dict, str(k)):
                continue
            if k == "series" and isinstance(v, dict):
                for sk, sv in v.items():
                    if sk in ("t", "time", "secs"):
                        continue
                    flat_rows.append((sk, sv))
            elif isinstance(v, dict):
                for sk, sv in v.items():
                    flat_rows.append((f"{k}.{sk}", sv))
            else:
                flat_rows.append((k, v))
        return flat_rows

    gpu_labels_map = _gpu_labels(_gpu_list_from(system_info_data, payload))
    for idx, r in enumerate(results):
        notes = (getattr(r, "notes", "") or "").strip()
        note_html = (
            f'<div class="bench-notes"><div class="note-head"><div class="stat-label">Notes</div>'
            f'<span class="muted">modifiable dans ce HTML</span></div>'
            f'<div class="note-editor" contenteditable="true" data-note-index="{idx}" '
            f'data-placeholder="Ajouter une note pour ce benchmark...">{_html.escape(notes)}</div></div>'
        )
        flat_metrics = get_all_rows(r.metrics or {})
        cmd_str = _get_benchmark_command(r)
        card_rows_list = []
        fio_m = _fio_matrix(r.metrics or {})
        fio_used = fio_m[2] if fio_m else set()
        if r.score is not None:
            # Suite fio : le « score » est le meilleur débit d'un sous-test (lecture ou écriture), pas une note globale
            card_rows_list.append(("Meilleur débit d'un sous-test" if fio_m else "Score global", f"{r.score} {r.unit}"))
        card_rows_list.append(("Durée d'exécution", f"{r.duration_s:.2f} s"))
        if cmd_str and "\n" not in cmd_str:        # suite : plusieurs commandes, déjà lisibles dans le cadre de commande ci-dessus
            card_rows_list.append(("Ligne de commande", cmd_str))
        has_gpu_table = bool(_gpu_rows_for_result(r))
        mm_ = r.metrics or {}
        for k, v in flat_metrics:
            if isinstance(v, list) and len(v) > 2500:
                continue
            if k in fio_used:
                continue                # affiché dans le tableau « Résultats par test »
            if str(k) == "command":
                continue                # déjà affichée (« Ligne de commande » / cadre de commande)
            if v is None or v == "" or v == [] or v == {}:
                continue                # pas de ligne vide (options non renseignées : target_gpus, extra_args…)
            if str(k) in _HIDDEN_METRIC_KEYS:
                continue                # interne à BenchSuite (ex. dossier de travail : chemin local sans intérêt dans un rapport)
            if str(k) == "time_s" and mm_.get("duration_s") == v:
                continue                # doublon exact de « duration_s »
            if str(k) == "stress_ng_command" and v == cmd_str:
                continue                # doublon de la ligne de commande
            if has_gpu_table and _GPU_METRIC_RE.match(str(k)) and not isinstance(v, (list, dict)):
                continue   # déjà présent dans la table « Synthèse par GPU »
            if fio_m and str(k) in ("lecture_MiB_s", "ecriture_MiB_s"):
                continue   # simple copie du meilleur sous-test, déjà dans la ligne « Meilleur débit » et le tableau ci-dessous
            label = "Durée demandée (s)" if str(k) == "duration_s" else _metric_display_label(str(k))
            card_rows_list.append((label, format_detailed_value(k, v)))
        
        rows = ''.join(
            f"<tr><td style='font-weight: 600; width: 35%;'>{_html.escape(str(name))}</td><td>{_html.escape(str(val))}</td></tr>"
            for name, val in card_rows_list
        ) or "<tr><td>-</td><td>-</td></tr>"

        fio_table_html = ""
        if fio_m:
            fio_table_html = ('<h4 style="margin:10px 0 6px">Résultats par test (fio)</h4><div style="overflow-x:auto"><table class="gpu-table"><thead><tr>'
                              + "".join(f"<th>{_html.escape(h)}</th>" for h in fio_m[0]) + "</tr></thead><tbody>"
                              + "".join("<tr>" + f"<td style='font-weight:600'>{_html.escape(row[0])}</td>"
                                        + "".join(f"<td style='text-align:right'>{_html.escape(c)}</td>" for c in row[1:]) + "</tr>" for row in fio_m[1])
                              + "</tbody></table></div>")
        raw = _html.escape(compact_raw_output(r.raw_output or '', 30000))
        key_metrics = []
        metric_priority = re.compile(
            r"(temp|max|moy|avg|power|freq|charge|cpu|ram|read|write|download|upload|iops|lat|ping|jitter|perte|loss|error|erreur|mbit|mib)",
            re.I,
        )
        numeric_items = [(mk, mv) for mk, mv in (r.metrics or {}).items() if isinstance(mv, (int, float))]
        for mk, mv in numeric_items:
            if metric_priority.search(str(mk)):
                key_metrics.append(f"{_metric_display_label(str(mk))}={mv}")
            if len(key_metrics) >= 6:
                break
        if not key_metrics:
            for mk, mv in numeric_items[:4]:
                key_metrics.append(f"{_metric_display_label(str(mk))}={mv}")
        key_txt = " · ".join(key_metrics) if key_metrics else "—"
        gpu_rows_r = _gpu_rows_for_result(r, gpu_labels_map)
        key_cell_html = _html.escape(key_txt)
        gpu_table_html = ""
        if gpu_rows_r:
            def _st_cls(st):
                return {"critique": "st-crit", "avertissement": "st-warn", "optimal": "st-ok"}.get(st or "", "")
            g_head, g_cells, g_vram = _gpu_table(gpu_rows_r)
            i_dt, i_th = _gpu_col(g_head, "dT hotspot"), _gpu_col(g_head, "Throttling")
            body_rows = ""
            for row, cells in zip(gpu_rows_r, g_cells):
                if i_dt >= 0:
                    cells[i_dt] = f'<span class="{_st_cls(row.get("delta_status"))}">{_html.escape(cells[i_dt])}</span>'
                if i_th >= 0:
                    cells[i_th] = f'<span class="{"st-crit" if row["throttle"] else "st-ok"}">{_html.escape(cells[i_th])}</span>'
                cells = [c if i in (i_dt, i_th) else _html.escape(c) for i, c in enumerate(cells)]
                body_rows += "<tr>" + "".join(f"<td>{c}</td>" for c in cells) + "</tr>"
            gpu_table_html = ("<table class='gpu-table'><thead><tr>" + "".join(f"<th>{_html.escape(h.replace('dT ', 'ΔT '))}</th>" for h in g_head)
                              + f"</tr></thead><tbody>{body_rows}</tbody></table>"
                              "<p class='muted' style='font-size:11px;margin-top:4px'>Valeurs moyennes en charge stabilisée (repos et 10 premières secondes exclus). ΔT = hotspot − cœur ; ! élevé (&gt; 20 °C), !! très élevé (&gt; 25 °C).</p>")
            key_cell_html = "<span class='muted'>Synthèse par GPU ci-dessous ↓</span>"
        bucket = _bucket_for_result(r)
        params_txt = _format_bench_params(r)
        params_badge = f"<span class='nv-badge'>{_html.escape(params_txt)}</span>" if params_txt != "—" else ""
        cmd_inline = f"<div style='margin-top:4px;'><code class='cmd-inline' title='{_html.escape(cmd_str)}'>{_html.escape(cmd_str)}</code></div>" if cmd_str else ""
        cell_params = f"{params_badge}{cmd_inline}" if (params_badge or cmd_inline) else "<span class='muted'>—</span>"
        category_rows[bucket].append(
            f"<tr><td>{idx+1}</td><td>{_html.escape(r.name)}</td><td>{_html.escape(str(r.status))}</td>"
            f"<td>{r.duration_s:.2f}s</td><td>{cell_params}</td><td>{key_cell_html}</td></tr>"
            + (f"<tr><td colspan='6'>{gpu_table_html}</td></tr>" if gpu_table_html else "")
        )
        
        score_badge = ""
        if r.score is not None:
            score_badge = f'<span class="badge" style="border-color: #f97316; color: #f97316; background: rgba(249, 115, 22, 0.1);">Score : {r.score} {r.unit}</span>'

        cmd_box_html = ""
        if cmd_str:
            cmd_box_html = (
                f'<div class="bench-cmd-box">'
                f'<div class="cmd-label">⌨️ Commande exécutée :</div>'
                f'<div class="cmd-row">'
                f'<code class="cmd-text">{_html.escape(cmd_str)}</code>'
                f'<button class="action-btn cmd-copy-btn" onclick="copyCommand(this, {_html.escape(json.dumps(cmd_str))})">📋 Copier</button>'
                f'</div></div>'
            )

        bench_sections.append(f"""
          <section class="bench-card" data-bench-card="{idx}">
            <div class="bench-head">
              <div>
                <h3>{_html.escape(r.name)}</h3>
                <p class="muted">{_html.escape(r.category)} · {_html.escape(r.timestamp or '')}</p>
              </div>
              <div class="bench-badges">
                {score_badge}
                <span class="badge status-{_html.escape(str(r.status).lower())}">{_html.escape(str(r.status))}</span>
                <span class="badge">{r.duration_s:.2f}s</span>
              </div>
            </div>
            {cmd_box_html}
            {('<h4 style="margin:10px 0 6px">Synthèse par GPU (en charge)</h4>' + gpu_table_html) if gpu_table_html else ''}
            {note_html}
            {_pcie_links_html(r)}
            <div class="table-actions" style="display:flex;justify-content:flex-end;gap:8px;margin:6px 0 -4px"><button class="action-btn" title="Copie le graphique comme image (Ctrl+V pour coller ailleurs)" onclick="chartShot('mini',this,false,{idx})">📷 Copier le graphique</button><button class="action-btn" onclick="chartShot('mini',this,true,{idx})">💾 PNG</button></div>
            <div class="mini-chart"><canvas id="benchChart{idx}" aria-label="Graphique du benchmark {idx}"></canvas></div>
            <div class="table-actions" data-html2canvas-ignore="true" style="margin-top:14px;margin-bottom:6px;display:flex;justify-content:flex-end;">
              <button class="action-btn" onclick="copyTableToImage(this, this.parentElement.nextElementSibling)">📸 Image</button>
              <button class="action-btn" onclick="copyTableToExcel(this, this.parentElement.nextElementSibling)">📋 Excel</button>
            </div>
            {fio_table_html}
            <table><thead><tr><th>Métrique</th><th>Valeur</th></tr></thead><tbody>{rows}</tbody></table>
            <details><summary>Sortie brute</summary><pre>{raw}</pre></details>
          </section>
        """)

    def _rows_html(bucket: str) -> str:
        rows = "".join(category_rows.get(bucket, []))
        if rows:
            return rows
        return "<tr><td colspan='6'>Aucun test dans cette catégorie</td></tr>"

    cpu_rows = _rows_html("cpu")
    storage_rows = _rows_html("storage")
    gpu_rows = _rows_html("gpu")
    network_rows = _rows_html("network")
    category_cards = ''.join(
        f'<div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:{category_colors[k]};background:{category_colors[k]}"></span>{category_labels[k]}</div>'
        f'<div class="stat-val">{len(category_rows[k])}</div><div class="stat-range"><span>tests</span><span>{sum(1 for row in category_rows[k] if "status-ok" in row or ">ok<" in row.lower())} OK</span></div></div>'
        for k in ("cpu", "storage", "gpu", "network") if category_rows[k]
    )
    covered_txt = ", ".join(category_labels[k] for k in ("cpu", "storage", "gpu", "network") if category_rows[k])
    covered_names = sorted({r.name for r in results})
    covered_sentence = (f"Tests du rapport : {_html.escape(', '.join(covered_names))}. " if covered_names else "")

    sys_rows = ""
    for sec_title, headers, sec_rows in _system_sections(payload["system"]["snapshot_at_start"], _gpu_list_from(system_info_data, payload)):
        ncols = len(headers) if headers else 2
        sys_rows += f"<tr><td colspan='{ncols}' class='sys-sec'>{_html.escape(sec_title)}</td></tr>"
        if headers:
            sys_rows += "<tr>" + "".join(f"<th>{_html.escape(h)}</th>" for h in headers) + "</tr>"
        for row in sec_rows:
            sys_rows += "<tr>" + "".join(f"<td>{_html.escape(_safe_text(c))}</td>" for c in row) + "</tr>"
    if not sys_rows:
        sys_rows = "<tr><td>info</td><td>non disponible</td></tr>"

    html_template = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BenchSuite — %%TITLE%%</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@500&display=swap" rel="stylesheet">
%%CHART_SCRIPTS%%
<style>
:root{--bg-base:#0d0d0d;--bg-surface:#141414;--bg-card:#141414;--border-color:#2a2a2a;--text-main:#f0f0f0;--text-muted:#888888;--accent:#ff6600;--ok:#4caf50;--bad:#ff3333;--warn:#ff9966}
*{margin:0;padding:0;box-sizing:border-box}body{background:var(--bg-base);color:var(--text-main);font-family:'Inter',system-ui,-apple-system,sans-serif;font-size:14px;min-height:100vh;padding:36px 30px;-webkit-font-smoothing:antialiased}header{margin-bottom:28px}h1{font-size:26px;font-weight:700;margin-bottom:6px;letter-spacing:-.03em}h2{font-size:18px;margin:0 0 16px}h3{font-size:16px;margin-bottom:4px}.subtitle,.muted{color:var(--text-muted);font-size:13px}.topbar{display:flex;justify-content:space-between;gap:18px;align-items:flex-start;flex-wrap:wrap}.tabs{display:inline-flex;gap:8px;background:var(--bg-surface);padding:6px;border-radius:10px;border:1px solid var(--border-color);margin:20px 0 28px;flex-wrap:wrap}.subtabs{margin:0 0 18px}.tab{padding:8px 18px;border:none;background:transparent;color:var(--text-muted);border-radius:6px;cursor:pointer;font-size:13px;font-weight:600;font-family:inherit;transition:all .2s ease}.tab:hover:not(.active){color:var(--text-main);background:rgba(255,255,255,.05)}.tab.active{background:var(--accent);color:#fff;box-shadow:0 2px 4px rgba(0,0,0,.2)}.view{display:none}.view.active{display:block}.stat-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:28px}.stat-card,.card,.bench-card{background:linear-gradient(145deg,var(--bg-surface),var(--bg-card));border:1px solid var(--border-color);border-radius:12px;padding:18px 20px;box-shadow:0 4px 10px -2px rgba(0,0,0,.18)}.stat-card{display:flex;flex-direction:column;min-height:130px;transition:transform .2s ease,box-shadow .2s ease}.stat-card:hover{transform:translateY(-2px);box-shadow:0 8px 16px -4px rgba(0,0,0,.25)}.stat-label{font-size:12px;color:var(--text-muted);font-weight:600;display:flex;align-items:center;gap:8px;margin-bottom:12px}.stat-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;box-shadow:0 0 8px currentColor}.stat-val{font-size:28px;font-weight:700;font-family:'Fira Code',monospace;margin-bottom:6px}.stat-unit{font-size:13px;color:var(--text-muted);font-family:'Inter',sans-serif;font-weight:500}.stat-range{display:flex;justify-content:space-between;gap:8px;color:var(--text-muted);font-size:12px;margin-top:auto}.stat-range span{background:rgba(255,255,255,.06);padding:3px 7px;border-radius:5px}.chart-container{background:var(--bg-surface);border:1px solid var(--border-color);border-radius:12px;padding:24px;box-shadow:0 4px 10px -2px rgba(0,0,0,.18);margin-bottom:28px}.chart-toolbar{display:flex;justify-content:space-between;gap:14px;flex-wrap:wrap;align-items:center;margin-bottom:18px}.select,.search{background:#0b1220;color:var(--text-main);border:1px solid var(--border-color);border-radius:8px;padding:9px 11px;font:inherit}.legend{display:flex;flex-wrap:wrap;gap:18px;margin-bottom:18px}.legend-item{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:600;color:var(--text-muted);cursor:pointer;user-select:none;transition:.2s}.legend-item:hover{color:var(--text-main)}.legend-item.muted{opacity:.4;filter:grayscale(100%)}.legend-line{width:24px;height:4px;border-radius:2px;flex-shrink:0;display:inline-block;vertical-align:middle;}.legend-line.dashed{border-top:3px dashed;height:0!important;background:transparent!important;}.legend-line.dotted{border-top:3px dotted;height:0!important;background:transparent!important;}.quick-filters{display:inline-flex;gap:6px;flex-wrap:wrap;align-items:center;}.filter-btn{background:#0b1220;color:var(--text-muted);border:1px solid var(--border-color);border-radius:6px;padding:5px 10px;font-size:11px;font-weight:700;cursor:pointer;transition:all .15s ease;display:inline-flex;align-items:center;gap:4px;}.filter-btn:hover{color:var(--text-main);border-color:var(--accent);background:rgba(255,102,0,0.1);}.filter-btn.active{background:rgba(255,102,0,0.2);color:var(--accent);border-color:var(--accent);}.chart-wrap{position:relative;width:100%;height:440px}.compare-wrap{height:500px}table{width:100%;border-collapse:collapse;font-size:13px;margin-top:10px}th,td{border-bottom:1px solid var(--border-color);padding:8px 9px;text-align:left;vertical-align:top}th{color:#fff;background:rgba(15,23,42,.7);position:sticky;top:0}.bench-card{margin-bottom:18px}.bench-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap;margin-bottom:12px}.bench-notes{background:#0b1220;border:1px solid var(--border-color);border-radius:10px;padding:12px 14px;margin:6px 0 14px}.note-head{display:flex;justify-content:space-between;gap:10px;align-items:center}.note-editor{min-height:62px;white-space:pre-wrap;color:#dbeafe;line-height:1.5;outline:none;border:1px solid transparent;border-radius:8px;padding:8px}.note-editor:focus{border-color:var(--accent);background:#0d0d0d}.note-editor:empty:before{content:attr(data-placeholder);color:#64748b}.top-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}.note-btn{background:#0b1220;color:var(--text-main);border:1px solid var(--border-color);border-radius:8px;padding:8px 10px;font:inherit;font-weight:700;cursor:pointer}.note-btn:hover{border-color:var(--accent)}.bench-badges{display:flex;gap:8px;flex-wrap:wrap}.badge{font-size:12px;font-weight:700;border:1px solid var(--border-color);border-radius:999px;padding:5px 9px;background:rgba(255,255,255,.06);color:var(--text-main)}.status-ok{border-color:#10b98166;color:#86efac;background:#10b9811a}.status-failed,.status-error{border-color:#ef444466;color:#fca5a5;background:#ef44441a}.mini-chart{height:230px;background:#0b1220;border:1px solid var(--border-color);border-radius:12px;padding:10px;margin:10px 0 14px}.mini-chart canvas{width:100%;height:100%}pre{white-space:pre-wrap;background:#0b1220;border:1px solid var(--border-color);border-radius:10px;padding:12px;color:#a7f3d0;max-height:420px;overflow:auto;margin-top:10px;font-family:'Fira Code',monospace;font-size:12px}details{margin-top:12px}summary{cursor:pointer;color:var(--text-muted);font-weight:700}.toolbar-row{display:flex;gap:10px;flex-wrap:wrap;align-items:center;margin-bottom:14px}.report-name-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:10px;margin:12px 0 18px}.report-name-card{display:flex;align-items:center;gap:10px;background:#0b1220;border:1px solid var(--border-color);border-radius:10px;padding:10px}.color-swatch{width:14px;height:14px;border-radius:50%;box-shadow:0 0 10px currentColor;flex-shrink:0}.report-name-card input{width:100%;background:#111827;color:var(--text-main);border:1px solid var(--border-color);border-radius:8px;padding:8px 10px;font:inherit}.report-badge{display:inline-flex;align-items:center;gap:7px;font-weight:700}.report-badge .color-swatch{width:11px;height:11px}.nv-badge{display:inline-block;font-size:11px;background:rgba(167, 139, 250, 0.15);color:#c084fc;padding:2px 6px;border-radius:4px;font-family:"Fira Code",monospace;font-weight:500}
.stat-sub{font-size:11px;color:var(--text-muted);margin:-6px 0 8px;line-height:1.3}
.gpu-table{width:100%;font-size:12px;margin:6px 0}.gpu-table th{font-size:11px;white-space:normal}.gpu-table td{white-space:normal}.st-crit{color:#f38ba8;font-weight:700}.st-warn{color:#f9e2af;font-weight:700}.st-ok{color:#a6e3a1}.sys-sec{background:#1a1a1a;color:#ff8c33;font-weight:700;padding-top:12px}
.bench-cmd-box{background:#090d16;border:1px solid #1e293b;border-radius:8px;padding:10px 14px;margin:8px 0 14px;}
.cmd-label{font-size:11px;font-weight:700;color:var(--accent);text-transform:uppercase;letter-spacing:0.05em;margin-bottom:6px;display:flex;align-items:center;gap:6px;}
.cmd-row{display:flex;align-items:center;justify-content:space-between;gap:12px;}
.cmd-text{font-family:'Fira Code',monospace;font-size:12px;color:#38bdf8;word-break:break-all;white-space:pre-wrap;background:transparent;padding:0;border:none;}
.cmd-inline{font-family:'Fira Code',monospace;font-size:11px;background:#090d16;color:#38bdf8;padding:3px 7px;border-radius:5px;border:1px solid #1e293b;display:inline-block;max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;vertical-align:middle;}
.cmd-copy-btn{flex-shrink:0;font-size:12px;padding:5px 10px;}
.card-header,.table-header-flex{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;flex-wrap:wrap;gap:10px}.card-header h2,.table-header-flex h4{margin:0}.table-actions{display:flex;gap:8px}.action-btn{background:#0b1220;color:var(--text-main);border:1px solid var(--border-color);border-radius:6px;padding:5px 10px;font-size:11px;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:4px;transition:all .15s ease}.action-btn:hover{border-color:var(--accent);background:rgba(255,102,0,0.1)}.action-btn.success{background:rgba(76,175,80,0.15);border-color:var(--ok);color:#81c784}.action-btn.loading{background:rgba(255,153,102,0.15);border-color:var(--warn);color:#ffb74d}
.verdict-actions{display:flex;gap:6px;flex-wrap:wrap;align-items:center;justify-content:flex-end}
.verdict-btn{background:rgba(11,18,32,0.75);color:var(--text-main);border:1px solid var(--border-color);border-radius:7px;padding:5px 10px;font:inherit;font-size:12px;font-weight:600;cursor:pointer;display:inline-flex;align-items:center;gap:5px;transition:all .15s ease}
.verdict-btn:hover{border-color:var(--accent);background:rgba(255,102,0,0.12)}
.verdict-btn-ok{border-color:#a6e3a1;color:#a6e3a1;background:rgba(166,227,161,0.12)}
.verdict-btn-ok:hover{border-color:#a6e3a1;background:rgba(166,227,161,0.24)}
.verdict-rule-row{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin:4px 0;padding:4px 8px;border-radius:7px;background:rgba(0,0,0,0.14)}
.diag-alert-item{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;margin-top:10px;padding:8px 10px;border-radius:8px;background:rgba(0,0,0,0.14)}
.diag-alert-body{flex:1;min-width:0}
.alert-del-btn{flex-shrink:0;background:transparent;color:#94a3b8;border:1px solid rgba(148,163,184,0.28);border-radius:6px;padding:2px 7px;font-size:12px;font-weight:700;cursor:pointer;line-height:1.3;transition:all .15s ease}
.alert-del-btn:hover{color:#f38ba8;border-color:#f38ba8;background:rgba(243,139,168,0.15)}
.manual-form{display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:10px;padding:10px 12px;border-radius:9px;background:rgba(0,0,0,0.22);border:1px solid rgba(166,227,161,0.35)}
.manual-form input{flex:1;min-width:220px;padding:7px 10px;font-size:13px}
.verdict-manual-note{font-size:12px;color:#a6e3a1;margin-top:4px;font-weight:600}
@media print{.no-print,.alert-del-btn,.verdict-actions,.top-actions,.table-actions,.cmd-copy-btn,.manual-form{display:none!important}}
</style></head><body>
<script type="application/json" id="payload">%%PAYLOAD%%</script>
<header><div class="topbar"><div><h1>BenchSuite — Rapport de validation</h1><p class="subtitle">Système testé : <b>%%SYSTEM%%</b> %%META_INFO%%· Export : %%EXPORTED%% · Version : %%VERSION%%</p><div class="top-actions"><button class="note-btn" id="downloadEditedHtml" type="button" title="Enregistre ce rapport HTML en conservant vos notes, alertes supprimées et validation manuelle">💾 Enregistrer HTML modifié</button><button class="note-btn" id="themeBtn" type="button" onclick="cycleTheme()" title="Thème : sombre → clair → automatique (suit le système), mémorisé dans ce navigateur">🌙 Sombre</button></div></div></div><div class="tabs" id="mainTabs"><button class="tab active" data-view="overview">Tableau de bord</button><button class="tab" data-view="cpu">CPU + RAM</button><button class="tab" data-view="storage">Stockage</button><button class="tab" data-view="gpu">GPU</button><button class="tab" data-view="network">Réseau</button><button class="tab" data-view="nvbandwidth" id="nvbandwidthTab" style="display:none">NVIDIA nvbandwidth</button><button class="tab" data-view="charts">Graphique</button><button class="tab" data-view="electronics">Séquence &amp; électronique</button><button class="tab" data-view="system">Système</button><button class="tab" data-view="benches">Résultats détaillés</button><button class="tab" data-view="compare">Comparaison HTML</button><button class="tab" data-view="json">JSON brut</button></div></header>
<main><section id="overview" class="view active">%%DIAGNOSTIC_BOX%%<div class="stat-grid"><div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:#3b82f6;background:#3b82f6"></span>Benchmarks</div><div class="stat-val">%%COUNT%%</div><div class="stat-range"><span>Total</span><span>rapport</span></div></div><div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:#10b981;background:#10b981"></span>OK</div><div class="stat-val">%%OK%%</div><div class="stat-range"><span>Validés</span><span>sans erreur</span></div></div><div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:#ef4444;background:#ef4444"></span>Échecs</div><div class="stat-val">%%FAILED%%</div><div class="stat-range"><span>À vérifier</span><span>logs inclus</span></div></div>%%STOP_CARD%%<div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:#a855f7;background:#a855f7"></span>Système</div><div class="stat-val" style="font-size:18px">%%SYSTEM%%</div><div class="stat-range"><span>snapshot</span><span>départ</span></div></div></div><div class="card"><h2>Tests couverts</h2><p class="muted">%%COVERED%%Les graphiques utilisent les séries temporelles quand elles existent, sinon les métriques numériques du test.</p><div class="stat-grid" style="margin-top:16px;margin-bottom:0">%%CATEGORY_CARDS%%</div></div></section>
<section id="cpu" class="view"><div class="card"><div class="card-header"><h2>Résultats CPU + RAM</h2><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest('.card').querySelector('table'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest('.card').querySelector('table'))">📋 Excel</button></div></div><table><thead><tr><th>#</th><th>Test</th><th>Statut</th><th>Durée</th><th>Paramètres</th><th>Métriques clés</th></tr></thead><tbody>%%CPU_ROWS%%</tbody></table></div></section>
<section id="storage" class="view"><div class="card"><div class="card-header"><h2>Résultats Stockage (fio / dd)</h2><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest('.card').querySelector('table'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest('.card').querySelector('table'))">📋 Excel</button></div></div><table><thead><tr><th>#</th><th>Test</th><th>Statut</th><th>Durée</th><th>Paramètres</th><th>Métriques clés</th></tr></thead><tbody>%%STORAGE_ROWS%%</tbody></table></div></section>
<section id="gpu" class="view"><div class="card"><div class="card-header"><h2>Résultats GPU</h2><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest('.card').querySelector('table'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest('.card').querySelector('table'))">📋 Excel</button></div></div><table><thead><tr><th>#</th><th>Test</th><th>Statut</th><th>Durée</th><th>Paramètres</th><th>Métriques clés</th></tr></thead><tbody>%%GPU_ROWS%%</tbody></table></div></section>
<section id="network" class="view"><div class="card"><div class="card-header"><h2>Résultats Réseau</h2><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest('.card').querySelector('table'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest('.card').querySelector('table'))">📋 Excel</button></div></div><table><thead><tr><th>#</th><th>Test</th><th>Statut</th><th>Durée</th><th>Paramètres</th><th>Métriques clés</th></tr></thead><tbody>%%NETWORK_ROWS%%</tbody></table></div></section>
<section id="nvbandwidth" class="view"><div class="card"><h2>Matrice &amp; Résultats détaillés NVIDIA nvbandwidth</h2><p class="muted">Mesures de bande passante et de latence inter-GPU (PCIe/NVLink) et CPU-GPU.</p><div id="nvbandwidthContent" style="margin-top:20px;"></div></div></section>
<section id="charts" class="view"><div class="chart-container"><div class="chart-toolbar"><div><h2>Analyse par benchmark</h2><p class="subtitle">Choisis d'abord le test, les métriques disponibles s'adaptent ensuite.</p></div><select class="select" id="benchSelect"></select></div></div><div class="tabs subtabs" id="metricTabs"></div><div class="stat-grid" id="statGrid"></div><div class="chart-container"><div class="chart-toolbar"><div><h2 id="chartTitle">Graphique</h2><p class="subtitle" id="chartSubtitle">Séries temporelles détectées automatiquement (Clic-gauche glisser pour zoomer, Double-clic pour réinitialiser)</p></div><div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;"><div id="chartQuickFilters" class="quick-filters" style="display:none;"></div><button id="resetZoomBtn" class="note-btn" style="display:none; font-size:12px; padding:6px 10px;">Réinitialiser le zoom</button><button class="note-btn" style="font-size:12px;padding:6px 10px" title="Copie le graphique (titre, légende, courbes) comme image : collez-la dans Word, Teams, un mail…" onclick="chartShot('main',this)">📷 Copier le graphique</button><button class="note-btn" style="font-size:12px;padding:6px 10px" title="Enregistre le graphique en PNG" onclick="chartShot('main',this,true)">💾 PNG</button></div></div><div class="legend" id="legendRow"></div><div class="chart-wrap"><canvas id="mainChart" role="img" aria-label="Graphique BenchSuite"></canvas></div></div></section>
<section id="electronics" class="view"><p class="muted" style="margin-bottom:14px">Relevés passifs pris pendant les tests : capteurs électroniques (tensions, courants, puissances, ventilateurs, températures de carte), inventaire matériel avant / après et journaux (BMC, noyau).</p>%%ELECTRONICS%%</section>
<script type="application/json" id="elecData">%%ELEC_DATA%%</script>
<section id="system" class="view"><input class="search" placeholder="Filtrer les infos système..." oninput="filterTable(this,'systemTable')"><div class="card"><div class="card-header"><h2>Informations système au départ</h2><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest('.card').querySelector('table'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest('.card').querySelector('table'))">📋 Excel</button></div></div><table id="systemTable"><thead></thead><tbody>%%SYS_ROWS%%</tbody></table></div></section>
<section id="benches" class="view"><input class="search" placeholder="Filtrer les benchmarks par nom ou catégorie..." oninput="filterBenches(this)" style="margin-bottom: 18px; width: 100%;">%%BENCH_SECTIONS%%</section>
<section id="compare" class="view"><div class="card"><h2>Comparer les graphiques avec d'autres rapports HTML BenchSuite</h2><p class="muted">Charge un ou plusieurs fichiers HTML générés par BenchSuite. Les courbes sont superposées par benchmark et par métrique. Couleur = rapport/fichier, style de ligne = série détectée. Tu peux renommer chaque rapport ci-dessous.</p><div class="toolbar-row"><input class="search" type="file" id="compareFiles" multiple accept=".html,text/html"><select class="select" id="compareBenchSelect"></select></div><div id="compareReportNames" class="report-name-grid"></div><div class="tabs subtabs" id="compareMetricTabs"></div><div class="chart-container"><div class="chart-toolbar"><div><h2 id="compareTitle">Comparaison</h2><p class="subtitle" id="compareSubtitle">Rapport courant inclus automatiquement (Clic-gauche glisser pour zoomer, Double-clic pour réinitialiser)</p></div><div style="display:flex;gap:10px;flex-wrap:wrap"><button id="compareResetZoomBtn" class="note-btn" style="display:none; font-size:12px; padding:6px 10px;">Réinitialiser le zoom</button><button class="note-btn" style="font-size:12px;padding:6px 10px" onclick="chartShot('compare',this)">📷 Copier le graphique</button><button class="note-btn" style="font-size:12px;padding:6px 10px" onclick="chartShot('compare',this,true)">💾 PNG</button></div></div><div class="legend" id="compareLegend"></div><div class="chart-wrap compare-wrap"><canvas id="compareChart" role="img" aria-label="Comparaison graphique BenchSuite"></canvas></div></div><div id="compareOutput"></div></div></section>
<section id="json" class="view"><pre id="jsonDump"></pre></section></main>
<script>
function copyCommand(btn, cmd){
  if(navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(cmd).then(()=>{
      const orig = btn.textContent;
      btn.textContent = '✓ Copié !';
      setTimeout(()=>{ btn.textContent = orig; }, 2000);
    }).catch(()=>{ prompt('Commande :', cmd); });
  } else {
    prompt('Commande :', cmd);
  }
}
async function loadHtml2Canvas(){
  if(window.html2canvas)return window.html2canvas;
  return new Promise((resolve,reject)=>{
    const s=document.createElement('script');
    s.src="https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js";
    s.onload=()=>resolve(window.html2canvas);
    s.onerror=()=>reject(new Error("Impossible de charger html2canvas"));
    document.head.appendChild(s);
  });
}
async function copyTableToImage(btn,table){
  if(!table){console.error("Tableau non trouvé");return}
  const origText=btn.innerHTML;
  btn.innerHTML='<span>⏳</span> Génération...';
  btn.className='action-btn loading';
  btn.disabled=true;
  try{
    const h2c=await loadHtml2Canvas();
    const canvas=await h2c(table,{backgroundColor:'#162032',scale:2,logging:false,useCORS:true});
    canvas.toBlob(async blob=>{
      try{
        if(!blob)throw new Error("Blob vide");
        await navigator.clipboard.write([new ClipboardItem({[blob.type]:blob})]);
        btn.innerHTML='<span>✅</span> Copié !';
        btn.className='action-btn success';
      }catch(err){
        const a=document.createElement('a');
        a.href=canvas.toDataURL('image/png');
        a.download='tableau_export.png';
        a.click();
        btn.innerHTML='<span>💾</span> Téléchargé !';
        btn.className='action-btn success';
      }
      setTimeout(()=>{btn.innerHTML=origText;btn.className='action-btn';btn.disabled=false},2000);
    },'image/png');
  }catch(err){
    console.error(err);
    alert("Erreur lors de la capture d'image : "+err.message+"\nNote : Vous devez être connecté à Internet lors de la première utilisation pour charger la bibliothèque de capture, ou utiliser l'export Excel.");
    btn.innerHTML='<span>❌</span> Échec';
    btn.className='action-btn';
    btn.disabled=false;
    setTimeout(()=>{btn.innerHTML=origText},2000);
  }
}
function copyTableToExcel(btn,table){
  if(!table){console.error("Tableau non trouvé");return}
  const origText=btn.innerHTML;
  btn.innerHTML='<span>⏳</span> Copie...';
  btn.className='action-btn loading';
  btn.disabled=true;
  try{
    let tsv="";
    const rows=table.querySelectorAll('tr');
    rows.forEach(tr=>{
      const cols=tr.querySelectorAll('th, td');
      const rowData=[];
      cols.forEach(col=>{
        if(col.hasAttribute('data-html2canvas-ignore')||col.closest('[data-html2canvas-ignore]'))return;
        let txt=col.innerText.trim().replace(/\r?\n|\r/g," ");
        rowData.push(txt);
      });
      if(rowData.length>0)tsv+=rowData.join("\t")+"\n";
    });
    navigator.clipboard.writeText(tsv).then(()=>{
      btn.innerHTML='<span>✅</span> Copié (Excel) !';
      btn.className='action-btn success';
      setTimeout(()=>{btn.innerHTML=origText;btn.className='action-btn';btn.disabled=false},2000);
    }).catch(err=>{throw err});
  }catch(err){
    console.error(err);
    alert("Erreur lors de la copie des données : "+err.message);
    btn.innerHTML='<span>❌</span> Échec';
    btn.className='action-btn';
    btn.disabled=false;
    setTimeout(()=>{btn.innerHTML=origText},2000);
  }
}
let payload={};try{payload=JSON.parse(document.getElementById('payload').textContent)}catch(e){document.body.insertAdjacentHTML('afterbegin','<pre style="background:#450a0a;color:#fecaca;padding:12px">Erreur JSON rapport: '+String(e)+'</pre>')}
const NOTE_STORAGE_KEY='benchsuite-notes:'+((payload.exported_at||'')+'|'+((payload.system&&payload.system.name)||'')+'|'+((payload.results||[]).map(r=>(r.name||'')+':'+(r.timestamp||'')).join('|'))).replace(/\s+/g,' ');
const VERDICT_STORAGE_KEY='benchsuite-verdict-edits:'+((payload.exported_at||'')+'|'+(payload.verdict_edit_ts||'')+'|'+((payload.system&&payload.system.name)||'')+'|'+((payload.results||[]).map(r=>(r.name||'')+':'+(r.timestamp||'')).join('|'))).replace(/\s+/g,' ');
const INITIAL_VERDICT=payload.verdict?JSON.parse(JSON.stringify(payload.verdict)):null;
const INITIAL_DIAG_ALERTS=Array.isArray(payload.diagnostic_alerts)?JSON.parse(JSON.stringify(payload.diagnostic_alerts)):[];
const VERDICT_STYLES={CONFORME:['#a6e3a1','rgba(166,227,161,0.12)'],A_VERIFIER:['#f9e2af','rgba(249,226,175,0.12)'],NON_CONFORME:['#f38ba8','rgba(243,139,168,0.14)'],NON_EVALUE:['#94a3b8','rgba(148,163,184,0.12)']};
const VERDICT_LABELS={CONFORME:'CONFORME',A_VERIFIER:'À VÉRIFIER',NON_CONFORME:'NON CONFORME',NON_EVALUE:'NON ÉVALUÉ'};
const RULE_SYM={fail:'✗',warn:'⚠',pass:'✓',na:'·'};
const RULE_COL={fail:'#f38ba8',warn:'#f9e2af',pass:'#a6e3a1',na:'#94a3b8'};
function updateJsonDump(){const el=document.getElementById('jsonDump');if(el)el.textContent=JSON.stringify(payload,(k,v)=>(Array.isArray(v)&&v.length>40&&typeof v[0]==='number')?'['+v.length+' valeurs numériques]':v,2)}
function hasVerdictEdits(){return JSON.stringify(payload.verdict||null)!==JSON.stringify(INITIAL_VERDICT)}
function hasDiagEdits(){return JSON.stringify(payload.diagnostic_alerts||[])!==JSON.stringify(INITIAL_DIAG_ALERTS)}
function hasAnyVerdictOrAlertEdits(){return hasVerdictEdits()||hasDiagEdits()}
function saveVerdictEdits(){try{localStorage.setItem(VERDICT_STORAGE_KEY,JSON.stringify({verdict:payload.verdict||null,diagnostic_alerts:payload.diagnostic_alerts||[]}))}catch(e){}}
function recomputeVerdict(v){
  if(!v)return;
  const rules=Array.isArray(v.rules)?v.rules:[];
  const counts={pass:0,warn:0,fail:0,na:0};
  rules.forEach(r=>{const lv=r&&r.level;if(Object.prototype.hasOwnProperty.call(counts,lv))counts[lv]++});
  v.counts=counts;
  let autoStatus='NON_EVALUE';
  if(counts.fail>0)autoStatus='NON_CONFORME';
  else if(counts.warn>0)autoStatus='A_VERIFIER';
  else if(counts.pass>0||(Array.isArray(v.dismissed_rules)&&v.dismissed_rules.length>0))autoStatus='CONFORME';
  v.auto_status=autoStatus;
  if(v.manual_override&&v.manual_override.status==='CONFORME'){
    v.status='CONFORME';
    v.label='CONFORME (validé manuellement)';
  }else{
    v.status=autoStatus;
    v.label=VERDICT_LABELS[autoStatus]||autoStatus;
  }
}
function applyBoxStyleAndTheme(el,styleStr){
  if(!el)return;
  el.removeAttribute('data-os');
  el.setAttribute('style',styleStr);
  if(typeof themeIsLight==='function'&&themeIsLight(window.__theme)){fixInline(el,true)}
}
function renderVerdictBox(){
  const box=document.getElementById('verdictBox');
  const v=payload.verdict;
  if(!box||!v)return;
  const pair=VERDICT_STYLES[v.status]||VERDICT_STYLES.NON_EVALUE;
  const col=pair[0],bg=pair[1];
  applyBoxStyleAndTheme(box,'background:'+bg+';border:1px solid '+col+';border-radius:12px;padding:16px 20px;margin-bottom:20px');
  const titleEl=document.getElementById('verdictTitle');
  if(titleEl){
    titleEl.textContent='VERDICT : '+(v.label||v.status||'');
    applyBoxStyleAndTheme(titleEl,'font-size:22px;font-weight:800;color:'+col);
  }
  const mo=v.manual_override&&typeof v.manual_override==='object'?v.manual_override:null;
  const noteEl=document.getElementById('verdictManualNote');
  if(noteEl){
    if(mo){
      const reason=String(mo.reason||'').trim();
      noteEl.textContent='✓ Validé manuellement en CONFORME'+(reason?' — Motif : '+reason:'');
      noteEl.style.display='block';
    }else{
      noteEl.textContent='';
      noteEl.style.display='none';
    }
  }
  const c=v.counts||{};
  const metaEl=document.getElementById('verdictMeta');
  if(metaEl){
    metaEl.innerHTML=(c.pass||0)+' conforme(s) · '+(c.warn||0)+' à vérifier · '+(c.fail||0)+' non conforme(s) · '+(c.na||0)+' non évalué(s)<br>Sources : '+escapeHtml((v.sources||[]).join(', '));
  }
  const btnForce=document.getElementById('btnForceConforme');
  const btnCancel=document.getElementById('btnCancelConforme');
  const btnReset=document.getElementById('btnResetEdits');
  if(btnForce)btnForce.style.display=(v.status!=='CONFORME'&&!mo)?'inline-flex':'none';
  if(btnCancel)btnCancel.style.display=mo?'inline-flex':'none';
  if(btnReset)btnReset.style.display=(hasAnyVerdictOrAlertEdits()||!!mo||(Array.isArray(v.dismissed_rules)&&v.dismissed_rules.length>0))?'inline-flex':'none';
  const rules=Array.isArray(v.rules)?v.rules:[];
  const mainEl=document.getElementById('verdictRulesMain');
  if(mainEl){
    let mainHtml='';
    rules.forEach((r,idx)=>{
      if(r&&(r.level==='fail'||r.level==='warn')){
        const det=r.detail?' <span style="color:#94a3b8">— '+escapeHtml(r.detail)+'</span>':'';
        mainHtml+='<div class="verdict-rule-row" data-rule-idx="'+idx+'">'
          +'<span style="color:'+(RULE_COL[r.level]||'#94a3b8')+'">'+(RULE_SYM[r.level]||'·')+' '
          +'<span style="color:#e2e8f0">'+escapeHtml(r.label||'')+'</span>'+det+'</span>'
          +'<button type="button" class="alert-del-btn no-print" title="Supprimer cette alerte du verdict" onclick="dismissVerdictRule('+idx+')">✕</button></div>';
      }
    });
    mainEl.innerHTML=mainHtml;
    mainEl.style.display=mainHtml?'block':'none';
    if(mainHtml&&typeof themeIsLight==='function'&&themeIsLight(window.__theme)){fixInline(mainEl,true)}
  }
  const restEl=document.getElementById('verdictRulesRest');
  if(restEl){
    let restHtml='';
    let restCount=0;
    rules.forEach(r=>{
      if(r&&(r.level==='pass'||r.level==='na')){
        restCount++;
        const det=r.detail?' <span style="color:#94a3b8">— '+escapeHtml(r.detail)+'</span>':'';
        restHtml+='<div style="margin:3px 0;color:'+(RULE_COL[r.level]||'#94a3b8')+'">'+(RULE_SYM[r.level]||'·')+' '
          +'<span style="color:#e2e8f0">'+escapeHtml(r.label||'')+'</span>'+det+'</div>';
      }
    });
    restEl.innerHTML=restHtml?'<details style="margin-top:8px"><summary style="color:#94a3b8;cursor:pointer">Détail des contrôles réussis / non évalués ('+restCount+')</summary><div style="margin-top:6px;font-size:12px">'+restHtml+'</div></details>':'';
    if(restHtml&&typeof themeIsLight==='function'&&themeIsLight(window.__theme)){fixInline(restEl,true)}
  }
  updateJsonDump();
}
function renderDiagBox(){
  const box=document.getElementById('diagnosticBox');
  if(!box)return;
  const alerts=Array.isArray(payload.diagnostic_alerts)?payload.diagnostic_alerts:[];
  const hasError=alerts.some(a=>a&&a.type==='error');
  const hasWarning=alerts.some(a=>a&&a.type==='warning');
  let boxStyle,titleText,titleColor;
  if(alerts.length>0){
    if(hasError){
      boxStyle='background-color: #2d191e; border: 1px solid #ef4444; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;';
      titleText='ALERTE : Anomalies critiques détectées dans la configuration matérielle !';
      titleColor='#fca5a5';
    }else if(hasWarning){
      boxStyle='background-color: #2b2210; border: 1px solid #f59e0b; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;';
      titleText='ATTENTION : Quelques points matériels à vérifier sur le système';
      titleColor='#fef08a';
    }else{
      boxStyle='background-color: #112233; border: 1px solid #3b82f6; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;';
      titleText="INFORMATION : Recommandations d'optimisation système";
      titleColor='#93c5fd';
    }
  }else{
    boxStyle='background-color: #112615; border: 1px solid #10b981; border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;';
    titleText='Diagnostic Matériel : Système entièrement opérationnel';
    titleColor='#a7f3d0';
  }
  applyBoxStyleAndTheme(box,boxStyle);
  const titleEl=document.getElementById('diagTitle');
  if(titleEl){
    titleEl.textContent=titleText;
    applyBoxStyleAndTheme(titleEl,'color: '+titleColor+'; font-size: 16px; font-weight: 700; margin: 0;');
  }
  const listEl=document.getElementById('diagAlertsList');
  if(listEl){
    if(alerts.length>0){
      let html='';
      alerts.forEach((a,idx)=>{
        const t=(a&&a.type)||'warning';
        const lblColor=t==='error'?'#fca5a5':(t==='warning'?'#fef08a':'#93c5fd');
        const devs=Array.isArray(a&&a.devices)&&a.devices.length?'<p style="color: #94a3b8; font-size: 12px; margin-top: 2px;">Concernés : '+escapeHtml(a.devices.join(' ; '))+'</p>':'';
        html+='<div class="diag-alert-item" data-alert-idx="'+idx+'"><div class="diag-alert-body">'
          +'<strong style="color: '+lblColor+';">• '+escapeHtml(String((a&&a.category)||''))+'</strong> — <strong>'+escapeHtml(String((a&&a.title)||''))+'</strong>'
          +'<p style="color: #cbd5e1; font-size: 13px; margin-top: 4px; line-height: 1.4;">'+escapeHtml(String((a&&a.desc)||''))+'</p>'
          +devs+'</div>'
          +'<button type="button" class="alert-del-btn no-print" title="Supprimer cette alerte" onclick="dismissDiagAlert('+idx+')">✕</button></div>';
      });
      listEl.innerHTML=html;
    }else{
      listEl.innerHTML='<p style="color: #cbd5e1; font-size: 13px; line-height: 1.4;">Toutes les barrettes de RAM détectées sont homogènes et tous les périphériques PCIe clés (cartes graphiques, stockage, interfaces réseau) fonctionnent dans leur configuration et bande passante maximales théoriques.</p>';
    }
    if(typeof themeIsLight==='function'&&themeIsLight(window.__theme)){fixInline(listEl,true)}
  }
  const btnAll=document.getElementById('btnDismissAllDiag');
  const btnResetDiag=document.getElementById('btnResetDiagOnly');
  if(btnAll)btnAll.style.display=alerts.length>0?'inline-flex':'none';
  if(btnResetDiag)btnResetDiag.style.display=hasDiagEdits()?'inline-flex':'none';
  updateJsonDump();
}
function dismissVerdictRule(idx){
  const v=payload.verdict;
  if(!v||!Array.isArray(v.rules)||idx<0||idx>=v.rules.length)return;
  const removed=v.rules.splice(idx,1)[0];
  if(!Array.isArray(v.dismissed_rules))v.dismissed_rules=[];
  if(removed)v.dismissed_rules.push(removed);
  recomputeVerdict(v);
  saveVerdictEdits();
  renderVerdictBox();
}
function dismissDiagAlert(idx){
  if(!Array.isArray(payload.diagnostic_alerts)||idx<0||idx>=payload.diagnostic_alerts.length)return;
  payload.diagnostic_alerts.splice(idx,1);
  saveVerdictEdits();
  renderDiagBox();
  renderVerdictBox();
}
function dismissAllDiagAlerts(){
  if(!Array.isArray(payload.diagnostic_alerts)||payload.diagnostic_alerts.length===0)return;
  payload.diagnostic_alerts=[];
  saveVerdictEdits();
  renderDiagBox();
  renderVerdictBox();
}
function openManualConforme(){
  const form=document.getElementById('manualConformeForm');
  const inp=document.getElementById('manualConformeReason');
  if(!form)return;
  form.style.display='flex';
  if(inp){
    const prev=payload.verdict&&payload.verdict.manual_override&&payload.verdict.manual_override.reason;
    if(prev)inp.value=prev;
    inp.focus();
  }
}
function closeManualConforme(){
  const form=document.getElementById('manualConformeForm');
  if(form)form.style.display='none';
}
function confirmManualConforme(){
  const v=payload.verdict;
  if(!v)return;
  const inp=document.getElementById('manualConformeReason');
  const reason=inp?inp.value.trim():'';
  v.manual_override={status:'CONFORME',reason:reason,at:new Date().toISOString()};
  closeManualConforme();
  recomputeVerdict(v);
  saveVerdictEdits();
  renderVerdictBox();
}
function cancelManualConforme(){
  const v=payload.verdict;
  if(!v)return;
  delete v.manual_override;
  recomputeVerdict(v);
  saveVerdictEdits();
  renderVerdictBox();
}
function resetVerdictAndAlerts(){
  payload.verdict=INITIAL_VERDICT?JSON.parse(JSON.stringify(INITIAL_VERDICT)):null;
  payload.diagnostic_alerts=JSON.parse(JSON.stringify(INITIAL_DIAG_ALERTS));
  try{localStorage.removeItem(VERDICT_STORAGE_KEY)}catch(e){}
  closeManualConforme();
  renderDiagBox();
  renderVerdictBox();
}
function initVerdictAndAlerts(){
  const inp=document.getElementById('manualConformeReason');
  if(inp){
    inp.addEventListener('keydown',e=>{
      if(e.key==='Enter'){e.preventDefault();confirmManualConforme()}
      else if(e.key==='Escape'){e.preventDefault();closeManualConforme()}
    });
  }
  try{
    const raw=localStorage.getItem(VERDICT_STORAGE_KEY);
    if(raw){
      const saved=JSON.parse(raw);
      if(saved&&typeof saved==='object'){
        if(saved.verdict&&payload.verdict)payload.verdict=saved.verdict;
        if(Array.isArray(saved.diagnostic_alerts))payload.diagnostic_alerts=saved.diagnostic_alerts;
        renderDiagBox();
        renderVerdictBox();
      }
    }
  }catch(e){}
}
function currentNoteMap(){const out={};document.querySelectorAll('[data-note-index]').forEach(el=>{out[el.dataset.noteIndex]=el.innerText.trim()});return out}
function applyNoteMap(notes){document.querySelectorAll('[data-note-index]').forEach(el=>{const i=+el.dataset.noteIndex;if(notes&&Object.prototype.hasOwnProperty.call(notes,i))el.innerText=notes[i]||'';if(payload.results&&payload.results[i])payload.results[i].notes=el.innerText.trim()})}
function bindNoteEditors(){let stored={};try{stored=JSON.parse(localStorage.getItem(NOTE_STORAGE_KEY)||'{}')}catch(e){stored={}};applyNoteMap(stored);document.querySelectorAll('[data-note-index]').forEach(el=>{el.addEventListener('input',()=>{const i=+el.dataset.noteIndex;if(payload.results&&payload.results[i])payload.results[i].notes=el.innerText.trim();localStorage.setItem(NOTE_STORAGE_KEY,JSON.stringify(currentNoteMap()));updateJsonDump()})});updateJsonDump()}
function downloadHtmlWithNotes(){applyNoteMap(currentNoteMap());const exportPayload=JSON.parse(JSON.stringify(payload));if(hasAnyVerdictOrAlertEdits())exportPayload.verdict_edit_ts=Date.now();const doc=document.documentElement.cloneNode(true),payloadEl=doc.querySelector('#payload');if(payloadEl)payloadEl.textContent=JSON.stringify(exportPayload).replace(/<\//g,'<\\/');const mf=doc.querySelector('#manualConformeForm');if(mf)mf.style.display='none';doc.querySelectorAll('[data-os]').forEach(el=>{el.setAttribute('style',el.dataset.os);el.removeAttribute('data-os')});doc.querySelectorAll('[data-note-index]').forEach(el=>{const i=+el.dataset.noteIndex;el.innerText=(exportPayload.results&&exportPayload.results[i]&&exportPayload.results[i].notes)||''});const blob=new Blob(['<!DOCTYPE html>\n'+doc.outerHTML],{type:'text/html;charset=utf-8'}),a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=hasAnyVerdictOrAlertEdits()?'benchsuite_report_modifie.html':'benchsuite_report_notes.html';document.body.appendChild(a);a.click();setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove()},0);const btn=document.getElementById('downloadEditedHtml');if(btn){const orig=btn.textContent;btn.textContent='✅ HTML enregistré';setTimeout(()=>{btn.textContent=orig},2000)}}
document.getElementById('downloadEditedHtml')?.addEventListener('click',downloadHtmlWithNotes);
const HAS_CHART=typeof Chart!=='undefined';
const COLORS=['#ff6600','#ff8c00','#ffb366','#4caf50','#2196f3','#a855f7','#f38ba8','#22d3ee','#f59e0b','#e879f9','#60a5fa','#34d399'];
const GPU_COLORS=['#ff6600','#3b82f6','#22c55e','#e879f9','#facc15','#22d3ee','#f43f5e','#a3a3a3','#8b5cf6','#14b8a6','#84cc16','#ec4899','#06b6d4','#fb7185','#fdba74','#d4d4d4'];
var THEME_LABELS={dark:'🌙 Sombre',light:'☀ Clair',auto:'🖥 Auto'},GRIDC='rgba(255,255,255,.06)',TXTC='#cbd5e1';
var INV_RX=/#([0-9a-f]{6}|[0-9a-f]{3})\b|rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)/gi,INV_OBS=null;
function invRGB(r,g,b){r/=255;g/=255;b/=255;var mx=Math.max(r,g,b),mn=Math.min(r,g,b),l=(mx+mn)/2,h=0,s=0,d=mx-mn;if(d){s=l>.5?d/(2-mx-mn):d/(mx+mn);h=mx===r?(g-b)/d+(g<b?6:0):mx===g?(b-r)/d+2:(r-g)/d+4;h/=6}l=1-l;if(!s){var v=Math.round(l*255);return [v,v,v]}var q=l<.5?l*(1+s):l+s-l*s,p=2*l-q;function f(t){if(t<0)t+=1;if(t>1)t-=1;return t<1/6?p+(q-p)*6*t:t<.5?q:t<2/3?p+(q-p)*(2/3-t)*6:p}return [f(h+1/3),f(h),f(h-1/3)].map(function(x){return Math.round(x*255)})}
function invStr(str){return str.replace(INV_RX,function(m,hex,r,g,b,a){if(hex){if(hex.length===3)hex=hex[0]+hex[0]+hex[1]+hex[1]+hex[2]+hex[2];var c=invRGB(parseInt(hex.slice(0,2),16),parseInt(hex.slice(2,4),16),parseInt(hex.slice(4,6),16));return '#'+c.map(function(x){return ('0'+x.toString(16)).slice(-2)}).join('')}var c2=invRGB(+r,+g,+b);return a!==undefined?'rgba('+c2.join(',')+','+a+')':'rgb('+c2.join(',')+')'})}
/* Styles en ligne (verdict, encarts de diagnostic…) : la feuille claire ne les atteint pas, on inverse leurs couleurs à la volée (original conservé dans data-os) */
function fixInline(root,on){var els=(root.nodeType===1&&root.hasAttribute('style')?[root]:[]).concat(Array.prototype.slice.call(root.querySelectorAll?root.querySelectorAll('[style]'):[]));els.forEach(function(el){if(on){if(el.dataset.os===undefined){var s0=el.getAttribute('style'),n=invStr(s0);if(n!==s0){el.dataset.os=s0;el.setAttribute('style',n)}}}else if(el.dataset.os!==undefined){el.setAttribute('style',el.dataset.os);delete el.dataset.os}})}
function watchInline(on){if(INV_OBS){INV_OBS.disconnect();INV_OBS=null}if(!on||!window.MutationObserver||!document.body)return;INV_OBS=new MutationObserver(function(ms){ms.forEach(function(m){m.addedNodes.forEach(function(n){if(n.nodeType===1)fixInline(n,true)})})});INV_OBS.observe(document.body,{childList:true,subtree:true})}
/* Capture d'un graphique en image (titre + légende + courbes sur fond uni du thème courant) : copie dans le presse-papiers, sinon téléchargement PNG */
function chartImage(canvas,title,sub,legendEl){var sc=canvas.width/Math.max(1,canvas.clientWidth),pad=Math.round(20*sc),cs=getComputedStyle(document.documentElement),bg=(cs.getPropertyValue('--bg-surface')||'#141414').trim(),fg=(cs.getPropertyValue('--text-main')||'#f0f0f0').trim(),mu=(cs.getPropertyValue('--text-muted')||'#888').trim(),items=[];
if(legendEl)legendEl.querySelectorAll('.legend-item').forEach(function(el){if(el.classList.contains('muted'))return;var ln=el.querySelector('.legend-line'),col='#888';if(ln){var st=getComputedStyle(ln);col=(st.backgroundColor&&st.backgroundColor!=='rgba(0, 0, 0, 0)')?st.backgroundColor:st.borderTopColor}items.push({t:el.textContent.trim(),c:col})});
var W=canvas.width+2*pad,c2=document.createElement('canvas'),x=c2.getContext('2d'),fT=Math.round(18*sc),fS=Math.round(12*sc),lineW=Math.round(24*sc),gap=Math.round(16*sc),rowH=Math.round(20*sc);
function layout(draw){var yy=pad;if(draw){x.fillStyle=bg;x.fillRect(0,0,c2.width,c2.height)}
 x.textBaseline='top';x.font='700 '+fT+'px Inter,Arial,sans-serif';if(draw){x.fillStyle=fg;x.fillText(title||'',pad,yy)}yy+=Math.round(fT*1.4);
 if(sub){x.font='400 '+fS+'px Inter,Arial,sans-serif';if(draw){x.fillStyle=mu;x.fillText(sub,pad,yy)}yy+=Math.round(fS*1.8)}
 if(items.length){x.font='600 '+fS+'px Inter,Arial,sans-serif';var xx=pad;items.forEach(function(it){var w=lineW+Math.round(6*sc)+x.measureText(it.t).width;if(xx>pad&&xx+w>W-pad){xx=pad;yy+=rowH}if(draw){x.fillStyle=it.c;x.fillRect(xx,yy+Math.round(fS/2)-Math.round(1.5*sc),lineW,Math.max(2,Math.round(3*sc)));x.fillStyle=fg;x.fillText(it.t,xx+lineW+Math.round(6*sc),yy)}xx+=w+gap});yy+=rowH+Math.round(6*sc)}
 return yy}
c2.width=W;c2.height=100;var h0=layout(false);c2.height=h0+canvas.height+pad;layout(true);x.drawImage(canvas,pad,h0);return c2}
function chartShot(kind,btn,download,idx){var cfg=kind==='compare'?['compareChart','compareTitle','compareSubtitle','compareLegend']:(kind==='mini'?['benchChart'+idx,null,null,null]:['mainChart','chartTitle','chartSubtitle','legendRow']),canvas=document.getElementById(cfg[0]),orig=btn.innerHTML;
function done(msg){btn.innerHTML=msg;setTimeout(function(){btn.innerHTML=orig},2000)}
if(!canvas||!canvas.width){done('⚠ Pas de graphique');return}
var title=cfg[1]?(document.getElementById(cfg[1])||{}).textContent:((((payload.results||[])[idx])||{}).name||'Graphique'),sub=cfg[2]?(document.getElementById(cfg[2])||{}).textContent:'',leg=cfg[3]?document.getElementById(cfg[3]):null;
if(sub)sub=sub.replace(/ · onglets limités.*$/,'').replace(/ · courbes réduites.*$/,'');if(sub&&sub.length>140)sub=sub.slice(0,137)+'…';
var c2;try{c2=chartImage(canvas,title,sub,leg)}catch(e){done('❌ Échec');return}
c2.toBlob(function(blob){if(!blob){done('❌ Échec');return}
 function save(){var a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=((title||'graphique').replace(/[^\w\-]+/g,'_').slice(0,60)||'graphique')+'.png';document.body.appendChild(a);a.click();setTimeout(function(){URL.revokeObjectURL(a.href);a.remove()},0);done('💾 PNG téléchargé')}
 if(download||!(navigator.clipboard&&window.ClipboardItem)){save();return}
 navigator.clipboard.write([new ClipboardItem({'image/png':blob})]).then(function(){done('✅ Copié — collez avec Ctrl+V')},save)},'image/png')}
function themeIsLight(m){return m==='light'||(m==='auto'&&!!window.matchMedia&&window.matchMedia('(prefers-color-scheme: light)').matches)}
function applyTheme(m){var light=themeIsLight(m),st=document.getElementById('lightCss');if(st)st.media=light?'all':'not all';GRIDC=light?'rgba(0,0,0,.10)':'rgba(255,255,255,.06)';TXTC=light?'#334155':'#cbd5e1';var b=document.getElementById('themeBtn');if(b)b.textContent=THEME_LABELS[m]||THEME_LABELS.dark;if(document.body){fixInline(document.body,light);watchInline(light)}try{if(window.Chart&&Chart.instances)Object.values(Chart.instances).forEach(function(c){var sc=(c.options&&c.options.scales)||{};Object.keys(sc).forEach(function(k){if(sc[k]&&sc[k].grid)sc[k].grid.color=GRIDC});var lg=c.options&&c.options.plugins&&c.options.plugins.legend;if(lg&&lg.labels)lg.labels.color=TXTC;c.update('none')})}catch(e){}}
function cycleTheme(){var l=['dark','light','auto'],n=l[(l.indexOf(window.__theme||'dark')+1)%3];window.__theme=n;try{localStorage.setItem('benchsuite_theme',n)}catch(e){}applyTheme(n)}
document.addEventListener('DOMContentLoaded',function(){applyTheme(window.__theme||'dark')});
(function(){var m='%%DEFAULT_THEME%%';try{var v=localStorage.getItem('benchsuite_theme');if(v&&THEME_LABELS[v])m=v}catch(e){}window.__theme=THEME_LABELS[m]?m:'dark';applyTheme(window.__theme)})();
const METRIC_GROUPS={temp:{label:'Température',unit:'°C',match:[/(^|_)temp($|_|erature)/i,/temperature/i,/delta_hotspot/i]},power:{label:'Puissance',unit:'W',match:[/power/i,/watt/i,/watts/i,/puissance/i]},cpu_freq:{label:'Fréquence CPU',unit:'MHz',match:[/^series_freq$/i,/^freq$/i,/cpu.*freq/i,/freq.*cpu/i,/mhz/i]},cpu_load:{label:'Charge CPU',unit:'%',match:[/^series_cpu_pct$/i,/cpu_pct/i,/cpu.*load/i,/load.*cpu/i,/charge/i]},gpu_util:{label:'Charge GPU',unit:'%',match:[/^gpu\d+_(util($|_)|mem_util|mem_bw|eu_active|eu_stall)/i]},fan:{label:'Ventilateurs',unit:'RPM',match:[/fan\d*_rpm/i]},voltage:{label:'Tension',unit:'V',match:[/voltage/i]},throughput:{label:'Débit',unit:'',match:[/mib[_\/]s/i,/mo_s/i,/mo\/s/i,/go_s/i,/go\/s/i,/mb_s/i,/mbit/i,/kbps/i,/throughput/i,/download/i,/upload/i,/read/i,/write/i,/lecture/i,/ecriture/i,/iops/i,/iperf/i,/bandwidth/i,/gb_s/i,/gb_sec/i,/_gbs$/i]},latency:{label:'Latence',unit:'ms',match:[/lat/i,/clat/i,/slat/i,/await/i,/ping/i,/jitter/i,/min_ms/i,/avg_ms/i,/max_ms/i,/95pct/i,/99pct/i]},network:{label:'Réseau',unit:'',match:[/iperf/i,/host/i,/port/i,/parallel/i,/bitrate/i,/mbit/i,/jitter/i,/perte/i,/loss/i,/retransmits/i]},ram:{label:'RAM',unit:'',match:[/^ram_/i,/memory_used/i,/mem_used/i,/write_MiB_s/i,/read_MiB_s/i,/coverage/i]},errors:{label:'Erreurs',unit:'',match:[/error/i,/erreur/i,/ecc/i,/ras_/i,/perte/i,/loss/i,/throttle/i]},iops:{label:'IOPS',unit:'IOPS',match:[/iops/i]},other:{label:'Valeurs',unit:'',match:[/^other_/i]},gpu_clock:{label:'Fréquence GPU',unit:'MHz',match:[/freq.*graphics/i,/graphics.*freq/i,/freq_request/i,/freq_media/i,/gpu.*clock/i,/clock.*gpu/i,/sm.*clock/i,/gr.*clock/i,/core.*clock/i,/series_freq_graphics/i]},mem_clock:{label:'Fréquence mémoire',unit:'MHz',match:[/freq.*mem/i,/mem.*freq/i,/memory.*clock/i,/clock.*mem/i,/vram.*clock/i,/series_freq_memory/i]}};
let activeMetric='temp',hiddenSeries={},hiddenCompareSeries={},mainChart=null,showConstructorLimit=false;let compareReports=[{label:(payload.system&&payload.system.name)||'Rapport courant',customName:(payload.system&&payload.system.name)||'Rapport courant',payload,selectedBenchIdx:0}];let compareMetric='temp',compareChart=null;
function escapeHtml(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function filterTable(inp,id){const q=inp.value.toLowerCase();document.querySelectorAll('#'+id+' tbody tr').forEach(r=>{r.style.display=r.textContent.toLowerCase().includes(q)?'':'none'})}
function filterBenches(inp){const q=inp.value.toLowerCase();document.querySelectorAll('#benches .bench-card').forEach(card=>{const title=(card.querySelector('h3')?.textContent||'').toLowerCase();const cat=(card.querySelector('.muted')?.textContent||'').toLowerCase();card.style.display=(title.includes(q)||cat.includes(q))?'':'none'})}
function numericArray(v){return Array.isArray(v)&&v.length>1&&v.some(x=>Number.isFinite(+x))}
function timeArray(m){const nested=(m.series&&typeof m.series==='object')?m.series:{};const t=nested.t||m.series_t||m.t||m.time||m.secs||[];if(numericArray(t))return t.map(Number);let n=0;for(const src of [nested,m||{}])for(const v of Object.values(src))if(numericArray(v))n=Math.max(n,v.length);return Array.from({length:n},(_,i)=>i)}
function gpuInventory(){const sys=(payload.system&&payload.system.snapshot_at_start)||{};if(Array.isArray(sys.gpu))return sys.gpu;if(Array.isArray(sys.gpus))return sys.gpus;return []}
function gpuDisplayName(i){const list=gpuInventory(),g=list.find(x=>String(x.index??x.id??'')===String(i))||list[i]||{};let pci=g.pci_bus||'';if(pci.startsWith('00000000:')){pci=pci.substring(9)}else if(pci.startsWith('0000:')){pci=pci.substring(5)}const mcio=g.mcio||'';const parts=[];if(pci)parts.push(pci);if(mcio)parts.push(mcio);const extra=parts.length>0?' ['+parts.join(' · ')+']':'';return 'GPU '+i+extra}
var LBL_KIND='';
var SERIES_LABELS={ram_used_gb:'RAM utilisée (Go)',series_ram_used_gb:'RAM utilisée (Go)',ram_used_max_gb:'RAM utilisée max (Go)',ram_pct_max:'RAM utilisée max (% du total)',ram_vitesse_mts:'RAM — vitesse configurée (MT/s, firmware)',ram_temp:'RAM — température (DIMM le plus chaud)',series_ram_temp:'RAM — température (DIMM le plus chaud)',ram_power:'RAM — puissance (barrettes)',series_ram_power:'RAM — puissance (barrettes)'};
var CPU_LABELS={temp:'CPU — température',series_temp:'CPU — température',power:'CPU — puissance',series_power:'CPU — puissance',freq:'CPU — fréquence',series_freq:'CPU — fréquence',cpu_pct:'CPU — charge',series_cpu_pct:'CPU — charge'};
var LBL_MAP={};
function niceSeriesLabel(k,groupKey){if(LBL_MAP[k])return LBL_MAP[k];if(SERIES_LABELS[k])return SERIES_LABELS[k];if((LBL_KIND==='cpu'||LBL_KIND==='ram')&&CPU_LABELS[k])return CPU_LABELS[k];let m2=k.match(/^gpu(\d+)_to_gpu(\d+)(_gb_s|_ns)?$/i);if(m2){return gpuDisplayName(+m2[1])+" ➔ "+gpuDisplayName(+m2[2])}let m=k.match(/^gpu(\d+)_(.+)$/i);if(m){const gpu=gpuDisplayName(+m[1]);const raw=m[2].toLowerCase();let coreName='Cœur';try{const gv=(gpuInventory()[+m[1]])||{};if(String(gv.fabricant||'')==='Intel')coreName='Cœur (PKG)'}catch(e){}const sMap={'temp_mctrl':'Contrôleur mémoire (secondaire)','temp_pcie':'Puce PCIe (secondaire)','temp_vram_ch_max':'Canaux VRAM max (secondaire)','temp_vr':'Régulateur VR (secondaire)','temp':coreName,'temp_c':coreName,'temp_max_c':'Cœur max','temp_moy_c':'Cœur moy','temp_hotspot':'Hotspot','temp_hotspot_max_c':'Hotspot max','temp_hotspot_moy_c':'Hotspot moy','hotspot':'Hotspot','temp_vram':'VRAM','temp_vram_max_c':'VRAM max','temp_vram_moy_c':'VRAM moy','vram':'VRAM','power':'Puissance','power_max_w':'Puissance max','power_moy_w':'Puissance moy','voltage_v':'Tension (V)','voltage_moy_v':'Tension moy (V)','voltage_max_v':'Tension max (V)','freq_graphics':'Fréq. GPU','freq_graphics_min_mhz':'Fréq. GPU min','freq_graphics_max_mhz':'Fréq. GPU max','freq_request':'Fréq. GPU demandée','freq_request_min_mhz':'Fréq. GPU demandée min','freq_request_max_mhz':'Fréq. GPU demandée max','freq_memory':'Fréq. VRAM','freq_memory_min_mhz':'Fréq. VRAM min','freq_memory_max_mhz':'Fréq. VRAM max','freq_media':'Fréq. Media','freq_media_min_mhz':'Fréq. Media min','freq_media_max_mhz':'Fréq. Media max','freq_media_request':'Fréq. Media demandée','freq_media_request_max_mhz':'Fréq. Media demandée max','util':'Charge GPU (%)','util_moy_pct':'Charge GPU moy (%)','util_max_pct':'Charge GPU max (%)','util_compute':'Moteur Compute (%)','util_compute_moy_pct':'Moteur Compute moy (%)','util_compute_max_pct':'Moteur Compute max (%)','util_render':'Moteur Render (%)','util_copy':'Moteur Copy (%)','util_media':'Moteur Media (%)','util_3d':'Moteur 3D (%)','eu_active':'EU actives (%)','eu_stall':'EU en attente (%)','mem_util':'VRAM utilisée (%)','mem_util_moy_pct':'VRAM utilisée moy (%)','mem_util_max_pct':'VRAM utilisée max (%)','mem_bw':'Bande passante VRAM (%)','mem_used_mib':'VRAM utilisée (MiB)','mem_used_moy_mib':'VRAM utilisée moy (MiB)','mem_used_max_mib':'VRAM utilisée max (MiB)','mem_read_kbps':'Lecture VRAM (kB/s)','mem_write_kbps':'Écriture VRAM (kB/s)','pcie_read_kbps':'Lecture PCIe (kB/s)','pcie_write_kbps':'Écriture PCIe (kB/s)','fan_rpm':'Ventilateur 1 (RPM)','fan_rpm_max':'Ventilateur 1 max (RPM)','fan2_rpm':'Ventilateur 2 (RPM)','fan2_rpm_max':'Ventilateur 2 max (RPM)','fan3_rpm':'Ventilateur 3 (RPM)','fan3_rpm_max':'Ventilateur 3 max (RPM)','ras_reset':'RAS — Resets','ras_programming_errors':'RAS — Erreurs prog.','ras_driver_errors':'RAS — Erreurs pilote','ras_cache_corr':'RAS — Cache corr.','ras_cache_uncorr':'RAS — Cache non corr.','ras_mem_corr':'RAS — Mémoire corr.','ras_mem_uncorr':'RAS — Mémoire non corr.','ras_errors_total':'RAS — Total erreurs','delta_hotspot_max_c':'ΔT Hotspot-Cœur max','delta_hotspot_moy_c':'ΔT Hotspot-Cœur moy','delta_hotspot_status':'Statut ΔT','throttle_thermal_detected':'Throttling thermique','throttle_thermal_samples':'Éch. throttling'};if(sMap[raw])return gpu+' — '+sMap[raw];return gpu+' — '+raw.replace(/^temp_/,'').replaceAll('_',' ')}let m_soc=k.match(/^(temp|freq|power|cpu_pct)_s(\d+)$/i);if(m_soc){const kind=m_soc[1]==='temp'?'Température':(m_soc[1]==='freq'?'Fréquence':(m_soc[1]==='power'?'Puissance':'Charge'));return 'CPU Socket '+m_soc[2]+' ('+kind+')'}let m_dimm=k.match(/^(temp|power)_dimm_(.+)$/i);if(m_dimm){const kind=m_dimm[1]==='temp'?'Température':'Puissance';return 'RAM '+m_dimm[2].toUpperCase().replaceAll('_',' ')+' ('+kind+')'}let m_vrm=k.match(/^(temp|power)_vrm_(.+)$/i);if(m_vrm){return 'VRM '+m_vrm[2].toUpperCase().replaceAll('_',' ')+' ('+(m_vrm[1]==='temp'?'Température':'Puissance')+')'}return k.replace(/^series_/,'').replace(/^ssd(0?)_/i, 'disque$1_').replaceAll('_',' ')}
function benchKind(result){const cat=(((result&&result.category)||'')+'').toLowerCase();if(/gpu/.test(cat))return 'gpu';if(/stock/.test(cat))return 'storage';if(/réseau|reseau|network/.test(cat))return 'network';if(/cpu|ram/.test(cat))return /ram/.test(cat)&&!/cpu/.test(cat)?'ram':'cpu';const txt=((result&&result.category)||'')+' '+((result&&result.name)||'');const s=txt.toLowerCase();if(/gpu|glmark|fryer|burn/.test(s))return 'gpu';if(/stock|fio|\bdd\b|disk|nvme|ssd/.test(s))return 'storage';if(/réseau|reseau|network|iperf/.test(s))return 'network';if(/ram|memtest|memory|\bvm\b/.test(s))return 'ram';if(/cpu|stress|sysbench/.test(s))return 'cpu';return 'other'}
function allowedMetricSet(result){const kind=benchKind(result);let pref=METRIC_ORDER;if(kind==='gpu'){const isNv=/nvbandwidth/i.test((result&&result.name)||"");pref=isNv?['throughput','latency','temp','power','gpu_clock','mem_clock','gpu_util','fan','voltage','ram','errors']:['temp','power','gpu_clock','mem_clock','gpu_util','fan','voltage','ram','throughput','errors']}else if(kind==='cpu'){pref=['temp','power','cpu_load','cpu_freq','throughput','errors']}else if(kind==='ram'){pref=['ram','throughput','temp','power','errors']}else if(kind==='storage'){pref=['throughput','iops','latency','temp','power','errors']}else if(kind==='network'){pref=['network','throughput','latency','temp','power','errors']}return Array.from(new Set([...pref,...METRIC_ORDER]))}
function isMetricKey(k,groupKey,conf){if(['series_t','t','time','secs'].includes(k))return false;const key=String(k);if(groupKey==='ram'){if(/(^|\.)(series_)?ram_(temp|power)/i.test(key))return false;if(/^(series_)?ram_(used|total|write|read|errors)/i.test(key))return true}if(groupKey==='iops')return /iops/i.test(key)&&!/\(ms\)\s*$|_ms$/i.test(key);if(groupKey==='throughput'){if(/iops/i.test(key))return false;if(/mem_used|memory_used|_mib$/i.test(key))return false}if(groupKey==='latency'||groupKey==='throughput'){const isTput=/\((MiB\/s|MB\/s|Mo\/s|Go\/s|GiB\/s|IOPS|kB\/s)\)\s*$/i.test(key)||/(MiB_s|MB_s|IOPS|mo_s|go_s|gb_s|_gbs|kbps)$/i.test(key),isLat=/\(ms\)\s*$/i.test(key)||/_ms$/i.test(key);if(groupKey==='latency'&&isTput)return false;if(groupKey==='throughput'&&isLat)return false;if(groupKey==='latency'&&/\(ms\)\s*$/i.test(key))return true}if(groupKey==='temp')return /(^|_)temp($|_|\d|erature)|temperature|delta_hotspot/i.test(key);if(groupKey==='power')return /(^|_)power($|_|\d)|watt|watts|puissance|pin|pout/i.test(key);if(groupKey==='cpu_freq')return (/^series_freq$/i.test(key)||/cpu.*freq|freq.*cpu/i.test(key)||/^freq_s\d+$|^freq$/i.test(key))&&!/gpu|graphics|mem|memory|vram/i.test(key);if(groupKey==='cpu_load')return /^series_cpu_pct$|^cpu_pct_s\d+$|cpu_pct|cpu.*load|load.*cpu|charge/i.test(key);if(groupKey==='gpu_clock')return (/freq.*graphics|graphics.*freq|freq_request|freq_media|gpu.*clock|clock.*gpu|sm.*clock|gr.*clock|core.*clock|freq_graphics/i.test(key))&&!/mem|memory|vram/i.test(key);if(groupKey==='mem_clock')return /freq.*mem|mem.*freq|memory.*clock|clock.*mem|vram.*clock|freq_memory/i.test(key);return conf.match.some(rx=>rx.test(key))}
function groupSeries(result,groupKey){LBL_KIND=benchKind(result);LBL_MAP=((result&&result.metrics&&result.metrics.series_labels)||{});const m=(result&&result.metrics)||{},conf=METRIC_GROUPS[groupKey],t=timeArray(m),out=[],seen={};if(!conf)return {t,series:[]};const nested=(m.series&&typeof m.series==='object')?m.series:{};const nestedHasGpu=Object.keys(nested).some(k=>/^gpu\d+_/i.test(k));function add(k,v,prefix){if(nestedHasGpu&&/^(temp|power|freq_graphics|freq_memory)$/i.test(k))return;if(!numericArray(v)||!isMetricKey(k,groupKey,conf))return;const key=prefix+k;if(seen[key])return;seen[key]=true;let gpuIdx=-1;let sensorType='other';const mg=k.match(/^gpu(\d+)_(.+)$/i);if(mg){gpuIdx=+mg[1];const st=mg[2].toLowerCase();if(st.includes('hotspot'))sensorType='hotspot';else if(/temp_(mctrl|pcie|vram_ch|vr($|_))/.test(st))sensorType='aux';else if(st.includes('vram'))sensorType='vram';else if(st.includes('temp'))sensorType='core';else if(st.includes('power'))sensorType='power';else if(st.includes('graphics'))sensorType='freq_gpu';else if(st.includes('memory'))sensorType='freq_mem'}out.push({key,label:niceSeriesLabel(k,groupKey),data:v.map(Number).slice(0,t.length),gpuIdx,sensorType})}for(const [k,v] of Object.entries(nested))add(k,v,'series.');for(const [k,v] of Object.entries(m)){if(k==='series')continue;if(nestedHasGpu&&/^series_(temp|power|freq_graphics|freq_memory)$/i.test(k))continue;add(k,v,'')}var limitAvailable=false;if(groupKey==='temp'&&t.length>0){const tLimit=+(m.thermal_limit_c||m.gpu0_thermal_limit_c||0);if(tLimit>0){limitAvailable=true;if(showConstructorLimit)out.push({key:'constructor_limit',label:'Seuil constructeur ('+tLimit+' °C)',data:Array(t.length).fill(tLimit),isLimit:true,gpuIdx:-1,sensorType:'limit'});}}return {t,series:out.slice(0,64),limitAvailable}}
function metricMatches(k,groupKey){const conf=METRIC_GROUPS[groupKey];return conf?isMetricKey(k,groupKey,conf):false}
function groupScalars(result,groupKey){LBL_KIND=benchKind(result);LBL_MAP=((result&&result.metrics&&result.metrics.series_labels)||{});const m=(result&&result.metrics)||{},out=[];for(const [k,v] of Object.entries(m)){if(Array.isArray(v)||v&&typeof v==='object')continue;const n=+v;if(!Number.isFinite(n)||!metricMatches(k,groupKey))continue;if(out.some(x=>x.key===k))continue;out.push({key:k,label:niceSeriesLabel(k,groupKey),value:n})}return out.slice(0,24)}
const METRIC_ORDER=['temp','power','cpu_load','cpu_freq','gpu_clock','mem_clock','gpu_util','fan','voltage','ram','throughput','iops','latency','network','errors','other'];
function applicableMetrics(r){return allowedMetricSet(r).filter(g=>groupSeries(r,g).series.length||groupScalars(r,g).length)}
function preferredMetricFor(r){const list=applicableMetrics(r);return list[0]||'temp'}
function groupLabel(result,g){const base=(METRIC_GROUPS[g]||{}).label||g;if(result&&g==='ram'&&benchKind(result)==='gpu'){try{const ser=groupSeries(result,'ram').series,sc=groupScalars(result,'ram');const allKeys=ser.map(x=>x.key).concat(sc.map(x=>x.key));if(allKeys.length&&allKeys.every(k=>/gpu\d+_mem_used/i.test(k)))return 'VRAM utilisée'}catch(e){}}if(!result||benchKind(result)!=='ram'||(g!=='temp'&&g!=='power'))return base;let hasRam=false,hasCpu=false;const rx=/ram|dimm/i;try{groupSeries(result,g).series.forEach(function(x){if(!x.data.some(function(v){return +v>0}))return;if(rx.test(x.key))hasRam=true;else hasCpu=true});groupScalars(result,g).forEach(function(x){if(!(x.value>0))return;if(rx.test(x.key))hasRam=true;else hasCpu=true})}catch(e){}const w=g==='temp'?'Température':'Puissance';return hasRam&&hasCpu?w+' RAM + CPU':(hasRam?w+' RAM':w+' CPU (pas de capteur RAM)')}
function renderMetricTabs(rootId,attr,metrics,active,onPick,result){const root=document.getElementById(rootId);if(!root)return;root.innerHTML=metrics.map(g=>'<button class="tab '+(g===active?'active':'')+'" data-'+attr+'="'+g+'">'+escapeHtml(groupLabel(result,g))+'</button>').join('')||'<span class="muted" style="padding:8px 10px">Aucune métrique graphique pour ce test</span>';root.querySelectorAll('[data-'+attr+']').forEach(b=>b.onclick=()=>onPick(b.getAttribute('data-'+attr)))}
function setActiveMetric(g){activeMetric=g}
function benchOptions(){const sel=document.getElementById('benchSelect');if(!sel)return;sel.innerHTML=(payload.results||[]).map((r,i)=>'<option value="'+i+'">'+(i+1)+' — '+escapeHtml(r.name||'Benchmark')+'</option>').join('');sel.onchange=()=>{hiddenSeries={};const r=(payload.results||[])[+sel.value];setActiveMetric(preferredMetricFor(r));buildMainChart()};if((payload.results||[]).length)setActiveMetric(preferredMetricFor(payload.results[0]))}
function calcStats(arr){const vals=(arr||[]).map(Number).filter(Number.isFinite);if(!vals.length)return null;const sum=vals.reduce((a,b)=>a+b,0);return {min:Math.min(...vals),max:Math.max(...vals),avg:sum/vals.length}}
function fmt(v){if(!Number.isFinite(v))return '-';if(Math.abs(v)>=1000)return String(Math.round(v));return v.toFixed(1)}
function renderStats(data,xMin,xMax){const grid=document.getElementById('statGrid'),unit=METRIC_GROUPS[activeMetric].unit;if(!data.series.length){grid.innerHTML='<div class="stat-card"><div class="stat-label">Aucune série</div><div class="stat-val" style="font-size:18px">Non disponible</div><div class="stat-range"><span>metric</span><span>'+activeMetric+'</span></div></div>';return}grid.innerHTML=data.series.filter(s=>!hiddenSeries[s.key]&&!s.isLimit).map((s,i)=>{let filtered=s.data;if(xMin!==undefined&&xMax!==undefined){filtered=s.data.filter((_,idx)=>{const t=data.t[idx];return t!==undefined&&t>=xMin&&t<=xMax})}const st=calcStats(filtered);if(!st)return '';const c=s.gpuIdx>=0?GPU_COLORS[s.gpuIdx%GPU_COLORS.length]:COLORS[i%COLORS.length];let title=s.label,sub='';const bracketM=s.label.match(/^(GPU \d+)\s*(\[[^\]]+\])?\s*—\s*(.+)$/);if(bracketM){title=bracketM[1]+' — '+bracketM[3];sub=(bracketM[2]||'').replace(/[\[\]]/g,'')}const dotBorder=s.sensorType==='hotspot'?'border:2px dashed '+c+';background:transparent;':((s.sensorType==='vram'||s.sensorType==='aux')?'border:2px dotted '+c+';background:transparent;':'background:'+c+';');return '<div class="stat-card"><div class="stat-label" title="'+escapeHtml(s.label)+'"><span class="stat-dot" style="color:'+c+';'+dotBorder+'"></span>'+escapeHtml(title)+'</div>'+(sub?'<div class="stat-sub">'+escapeHtml(sub)+'</div>':'')+'<div class="stat-val" style="color:'+c+'">'+fmt(st.avg)+'<span class="stat-unit"> '+unit+' moy. série</span></div><div class="stat-range"><span>Min: '+fmt(st.min)+'</span><span>Max: '+fmt(st.max)+'</span></div></div>'}).join('')}
function renderLegend(data){const row=document.getElementById('legendRow');row.innerHTML=data.series.map((s,i)=>{const isLimit=s.isLimit,c=isLimit?'#ef4444':(s.gpuIdx>=0?GPU_COLORS[s.gpuIdx%GPU_COLORS.length]:COLORS[i%COLORS.length]);let lineClass='legend-line',lineStyle='background:'+c+';';if(isLimit){lineClass+=' dashed';lineStyle='border-color:#ef4444;'}else if(s.sensorType==='hotspot'){lineClass+=' dashed';lineStyle='border-color:'+c+';'}else if(s.sensorType==='vram'||s.sensorType==='aux'){lineClass+=' dotted';lineStyle='border-color:'+c+';'}return '<span class="legend-item '+(hiddenSeries[s.key]?'muted':'')+'" data-series-key="'+escapeHtml(s.key)+'"><span class="'+lineClass+'" style="'+lineStyle+'"></span>'+escapeHtml(s.label)+'</span>'}).join('');row.querySelectorAll('[data-series-key]').forEach(el=>el.onclick=()=>{hiddenSeries[el.dataset.seriesKey]=!hiddenSeries[el.dataset.seriesKey];buildMainChart()});const filterBox=document.getElementById('chartQuickFilters');if(filterBox){if(activeMetric==='temp'&&data.series.length){filterBox.style.display='inline-flex';const countTotal=data.series.filter(s=>!s.isLimit).length,countCore=data.series.filter(s=>s.sensorType==='core').length,countHotspot=data.series.filter(s=>s.sensorType==='hotspot').length,countVram=data.series.filter(s=>s.sensorType==='vram').length;let btns='';if(countCore||countHotspot||countVram){btns+='<button class="filter-btn" id="qfAll">🔘 Tous ('+countTotal+')</button>';if(countCore)btns+='<button class="filter-btn" id="qfCore">🔥 Cœurs ('+countCore+')</button>';if(countHotspot)btns+='<button class="filter-btn" id="qfHotspot">⚡ Hotspots ('+countHotspot+')</button>';if(countVram)btns+='<button class="filter-btn" id="qfVram">💾 VRAM ('+countVram+')</button>';btns+='<button class="filter-btn" id="qfNone">👁️ Masquer tout</button>'}if(data.limitAvailable){const limitCls=showConstructorLimit?' active':'';btns+='<button class="filter-btn'+limitCls+'" id="qfToggleLimit" title="Afficher ou masquer la ligne de consigne constructeur">⚡ Seuil constructeur ('+(showConstructorLimit?'Actif':'Off')+')</button>'}filterBox.innerHTML=btns;if(document.getElementById('qfAll'))document.getElementById('qfAll').onclick=()=>{hiddenSeries={};buildMainChart()};if(document.getElementById('qfCore'))document.getElementById('qfCore').onclick=()=>{hiddenSeries={};data.series.forEach(s=>{if(!s.isLimit&&s.sensorType!=='core')hiddenSeries[s.key]=true});buildMainChart()};if(document.getElementById('qfHotspot'))document.getElementById('qfHotspot').onclick=()=>{hiddenSeries={};data.series.forEach(s=>{if(!s.isLimit&&s.sensorType!=='hotspot')hiddenSeries[s.key]=true});buildMainChart()};if(document.getElementById('qfVram'))document.getElementById('qfVram').onclick=()=>{hiddenSeries={};data.series.forEach(s=>{if(!s.isLimit&&s.sensorType!=='vram')hiddenSeries[s.key]=true});buildMainChart()};if(document.getElementById('qfNone'))document.getElementById('qfNone').onclick=()=>{data.series.forEach(s=>{if(!s.isLimit)hiddenSeries[s.key]=true});buildMainChart()};if(document.getElementById('qfToggleLimit'))document.getElementById('qfToggleLimit').onclick=()=>{showConstructorLimit=!showConstructorLimit;buildMainChart()}}else{filterBox.style.display='none';filterBox.innerHTML=''}}}
function renderScalarStats(items,unit){const grid=document.getElementById('statGrid');if(!items.length){grid.innerHTML='<div class="stat-card"><div class="stat-label">Aucune métrique</div><div class="stat-val" style="font-size:18px">Non disponible</div><div class="stat-range"><span>metric</span><span>'+activeMetric+'</span></div></div>';return}grid.innerHTML=items.slice(0,8).map((s,i)=>{const c=COLORS[i%COLORS.length];return '<div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:'+c+';background:'+c+'"></span>'+escapeHtml(s.label)+'</div><div class="stat-val" style="color:'+c+'">'+fmt(s.value)+'<span class="stat-unit"> '+unit+'</span></div><div class="stat-range"><span>valeur</span><span>'+escapeHtml(s.key)+'</span></div></div>'}).join('')}
function buildMainChart(){const idx=+(document.getElementById('benchSelect').value||0),result=(payload.results||[])[idx]||{metrics:{}},available=applicableMetrics(result);if(!available.includes(activeMetric))activeMetric=available[0]||'temp';renderMetricTabs('metricTabs','metric',available,activeMetric,g=>{activeMetric=g;hiddenSeries={};buildMainChart()},result);const data=groupSeries(result,activeMetric),allScalars=groupScalars(result,activeMetric),conf0=METRIC_GROUPS[activeMetric]||{label:'Métriques',unit:''},conf=Object.assign({},conf0,{label:groupLabel(result,activeMetric)});document.getElementById('chartTitle').textContent=(result.name||'Benchmark')+' — '+conf.label;document.getElementById('chartSubtitle').textContent=(result.category||'')+' · '+(result.status||'')+' · onglets limités aux métriques disponibles pour ce test'+(payload.series_reduction?' · courbes réduites (1 point sur '+payload.series_reduction.facteur+', pics et creux conservés ; séries complètes dans le fichier JSON)':'');const canvas=document.getElementById('mainChart');if(mainChart&&mainChart.destroy)mainChart.destroy();if(!HAS_CHART){canvas.parentElement.innerHTML='<p class="muted">Chart.js non disponible : tableaux utilisables, graphiques indisponibles.</p>';return}if(!available.length){renderScalarStats([],conf.unit);document.getElementById('legendRow').innerHTML='';mainChart=new Chart(canvas,{type:'line',data:{datasets:[]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{enabled:false}},scales:{x:{display:false},y:{display:false}}}});return}if(data.series.length&&data.t.length>1){renderStats(data);renderLegend(data);mainChart=new Chart(canvas,{type:'line',data:{datasets:data.series.map((s,i)=>{const isLimit=s.isLimit,c=isLimit?'#ef4444':(s.gpuIdx>=0?GPU_COLORS[s.gpuIdx%GPU_COLORS.length]:COLORS[i%COLORS.length]);let borderDash=[],borderWidth=2;if(isLimit){borderDash=[6,4]}else if(s.sensorType==='hotspot'){borderDash=[6,4]}else if(s.sensorType==='vram'){borderDash=[2,3]}else if(s.sensorType==='aux'){borderDash=[1,3];borderWidth=1}else if(s.sensorType==='core'){borderWidth=2.5}return {label:s.label,data:data.t.slice(0,s.data.length).map((x,j)=>({x:+x,y:+s.data[j]})),borderColor:c,borderWidth:borderWidth,borderDash:borderDash,pointRadius:0,pointHoverRadius:isLimit?0:4,tension:0,fill:false,hidden:!!hiddenSeries[s.key]}})},options:{animation:false,parsing:false,normalized:true,responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{decimation:{enabled:true,algorithm:'lttb',samples:700},legend:{display:false},tooltip:{backgroundColor:'rgba(30,41,59,.96)',titleColor:'#f8fafc',bodyColor:'#cbd5e1',borderColor:'#334155',borderWidth:1,padding:12,callbacks:{title:items=>'Temps : '+items[0].parsed.x+' s',label:item=>item.dataset.label+' : '+fmt(item.parsed.y)+' '+conf.unit}},zoom:{limits:{x:{min:'original',max:'original',minRange:0.1}},pan:{enabled:true,mode:'x',modifierKey:'shift',onPanComplete({chart}){const xMin=chart.scales.x.min,xMax=chart.scales.x.max;renderStats(data,xMin,xMax);const btn=document.getElementById('resetZoomBtn');if(btn)btn.style.display='inline-block'}},zoom:{drag:{enabled:true,threshold:5,backgroundColor:'rgba(59,130,246,0.15)',borderColor:'rgba(59,130,246,0.4)',borderWidth:1},wheel:{enabled:true,modifierKey:'ctrl'},pinch:{enabled:true},mode:'x',onZoomStart({chart,point}){const a=chart.chartArea;return !a||!point||(point.x>=a.left&&point.x<=a.right&&point.y>=a.top&&point.y<=a.bottom)},onZoomComplete({chart}){const xMin=chart.scales.x.min,xMax=chart.scales.x.max;renderStats(data,xMin,xMax);const btn=document.getElementById('resetZoomBtn');if(btn)btn.style.display='inline-block'}}}},scales:{x:{type:'linear',min:(data.t&&data.t.length?data.t[0]:undefined),max:(data.t&&data.t.length?data.t[data.t.length-1]:undefined),title:{display:true,text:'Temps (secondes)',color:'#94a3b8'},ticks:{color:'#94a3b8',maxTicksLimit:12},grid:{color:GRIDC}},y:{title:{display:true,text:conf.label+(conf.unit?' ('+conf.unit+')':''),color:'#94a3b8'},ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}}});const resetBtn=document.getElementById('resetZoomBtn');if(resetBtn){resetBtn.style.display='none';resetBtn.onclick=()=>{mainChart.resetZoom();renderStats(data);resetBtn.style.display='none'}};canvas.ondblclick=()=>{mainChart.resetZoom();renderStats(data);if(resetBtn)resetBtn.style.display='none'};return}
const scalars=allScalars.filter(s=>!hiddenSeries[s.key]);renderScalarStats(scalars,conf.unit);
const legRow=document.getElementById('legendRow');legRow.innerHTML=allScalars.map((s,i)=>'<span class="legend-item '+(hiddenSeries[s.key]?'muted':'')+'" data-series-key="'+escapeHtml(s.key)+'"><span class="legend-line" style="background:'+COLORS[i%COLORS.length]+'"></span>'+escapeHtml(s.label)+'</span>').join('');
legRow.querySelectorAll('[data-series-key]').forEach(el=>el.onclick=()=>{hiddenSeries[el.dataset.seriesKey]=!hiddenSeries[el.dataset.seriesKey];buildMainChart()});
mainChart=new Chart(canvas,{type:'bar',data:{labels:scalars.map(s=>s.label),datasets:[{label:conf.label,data:scalars.map(s=>s.value),backgroundColor:scalars.map(s=>COLORS[allScalars.indexOf(s)%COLORS.length]),borderColor:scalars.map(s=>COLORS[allScalars.indexOf(s)%COLORS.length]),borderWidth:1}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false},tooltip:{backgroundColor:'rgba(30,41,59,.96)',titleColor:'#f8fafc',bodyColor:'#cbd5e1',callbacks:{label:item=>fmt(item.parsed.y)+' '+conf.unit}}},scales:{x:{ticks:{color:'#94a3b8'},grid:{display:false}},y:{title:{display:true,text:conf.label+(conf.unit?' ('+conf.unit+')':''),color:'#94a3b8'},ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}}})}
function buildSeqCharts(){if(!HAS_CHART)return;const sq=payload.sequence_series;if(!sq||!sq.channels)return;const ents={};sq.channels.forEach(c=>{const e=c.label.replace(' cœur','').replace(' hotspot','');if(!(e in ents))ents[e]=GPU_COLORS[Object.keys(ents).length%GPU_COLORS.length]});const marks={id:'seqMarks',afterDatasetsDraw(ch){const {ctx,chartArea,scales}=ch;ctx.save();ctx.setLineDash([4,3]);ctx.strokeStyle='rgba(249,226,175,.6)';ctx.fillStyle='#f9e2af';ctx.font='11px Inter,sans-serif';(sq.marks||[]).forEach((m,i)=>{const x=scales.x.getPixelForValue(m.t/60);if(x<chartArea.left||x>chartArea.right)return;ctx.beginPath();ctx.moveTo(x,chartArea.top);ctx.lineTo(x,chartArea.bottom);ctx.stroke();ctx.fillText(String(i+1),x+3,chartArea.top+11)});ctx.restore()}};[['seqChartTemp','temp','Température (°C)'],['seqChartPower','power','Puissance (W)']].forEach(([id,panel,title])=>{const cv=document.getElementById(id);if(!cv||cv.dataset.done)return;cv.dataset.done='1';const chans=sq.channels.filter(c=>c.panel===panel);if(!chans.length){cv.parentElement.innerHTML='<p class="muted">Aucune donnée '+title+'</p>';return}new Chart(cv,{type:'line',data:{datasets:chans.map(c=>({label:c.label,data:c.t.map((t,j)=>({x:t/60,y:c.v[j]})),borderColor:ents[c.label.replace(' cœur','').replace(' hotspot','')],borderWidth:1.8,borderDash:c.dash?[6,4]:[],pointRadius:0,tension:.2}))},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{title:{display:true,text:title,color:'#ff8c33'},legend:{labels:{color:'#cbd5e1',boxWidth:12}},tooltip:{callbacks:{title:it=>'t = '+it[0].parsed.x.toFixed(1)+' min',label:it=>it.dataset.label+' : '+it.parsed.y.toFixed(1)}}},scales:{x:{type:'linear',title:{display:true,text:'Temps (min)',color:'#94a3b8'},ticks:{color:'#94a3b8'},grid:{color:GRIDC}},y:{ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}},plugins:[marks]})})}
function buildElecCharts(){if(!HAS_CHART)return;let data=[];try{data=JSON.parse(document.getElementById('elecData').textContent||'[]')}catch(e){data=[]}const TITLES={rail:'Rails 12 V / 5 V / 3,3 V',voltage:'Tensions variables (Vcore, DIMM…)',current:'Courants',power:'Puissance',fan:'Ventilateurs',temperature:'Températures carte'};document.querySelectorAll('.elec-charts[data-elec-index]').forEach(box=>{if(box.dataset.done)return;box.dataset.done='1';const d=data[+box.dataset.elecIndex];if(!d||!d.series||!d.series.t)return;const t=d.series.t,groups={};Object.keys(d.series).forEach(k=>{if(k==='t')return;const lab=d.labels[k]||{name:k,unit:'',category:'other'};const g=(lab.category||'other')+'|'+(lab.unit||'');(groups[g]=groups[g]||[]).push(k)});Object.keys(groups).forEach(g=>{const [cat,unit]=g.split('|');const wrap=document.createElement('div');wrap.className='mini-chart';wrap.style.height='220px';wrap.style.margin='0';const cv=document.createElement('canvas');wrap.appendChild(cv);box.appendChild(wrap);new Chart(cv,{type:'line',data:{datasets:groups[g].map((k,i)=>({label:(d.labels[k]||{name:k}).name,data:t.map((x,j)=>({x:+x,y:d.series[k][j]==null?null:+d.series[k][j]})),borderColor:GPU_COLORS[i%GPU_COLORS.length],borderWidth:1.8,pointRadius:0,tension:.2,spanGaps:true}))},options:{responsive:true,maintainAspectRatio:false,interaction:{mode:'index',intersect:false},plugins:{title:{display:true,text:(TITLES[cat]||cat)+(unit?' ('+unit+')':''),color:'#ff8c33'},legend:{labels:{color:'#cbd5e1',boxWidth:12}},tooltip:{callbacks:{label:it=>it.dataset.label+' : '+it.parsed.y+' '+unit}}},scales:{x:{type:'linear',title:{display:true,text:'Temps (s)',color:'#94a3b8'},ticks:{color:'#94a3b8'},grid:{color:GRIDC}},y:{ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}}})})})}
function buildMiniChart(r,i){const canvas=document.getElementById('benchChart'+i);if(!canvas||canvas.dataset.done)return;const metric=preferredMetricFor(r),d=groupSeries(r,metric),scalars=groupScalars(r,metric),conf=METRIC_GROUPS[metric];canvas.dataset.done='1';if(d.series.length&&d.t.length>1){new Chart(canvas,{type:'line',data:{datasets:d.series.slice(0,32).map((s,idx)=>{const isLimit=s.isLimit,c=isLimit?'#ef4444':(s.gpuIdx>=0?GPU_COLORS[s.gpuIdx%GPU_COLORS.length]:COLORS[idx%COLORS.length]);let borderDash=[],borderWidth=2;if(isLimit){borderDash=[4,4]}else if(s.sensorType==='hotspot'){borderDash=[5,4]}else if(s.sensorType==='vram'){borderDash=[2,3]}else if(s.sensorType==='aux'){borderDash=[1,3];borderWidth=1}else if(s.sensorType==='core'){borderWidth=2.5}return {label:s.label,data:d.t.slice(0,s.data.length).map((x,j)=>({x:+x,y:+s.data[j]})),borderColor:c,borderWidth:borderWidth,borderDash:borderDash,pointRadius:0,tension:0}})},options:{animation:false,parsing:false,normalized:true,responsive:true,maintainAspectRatio:false,plugins:{decimation:{enabled:true,algorithm:'lttb',samples:500},legend:{labels:{color:TXTC}}},scales:{x:{type:'linear',ticks:{color:'#94a3b8'},grid:{color:GRIDC}},y:{ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}}});return}if(!scalars.length){canvas.parentElement.innerHTML='<p class="muted">Pas de métrique numérique disponible pour ce benchmark.</p>';return}new Chart(canvas,{type:'bar',data:{labels:scalars.slice(0,32).map(s=>s.label),datasets:[{label:conf.label,data:scalars.slice(0,32).map(s=>s.value),backgroundColor:scalars.slice(0,32).map((_,idx)=>COLORS[idx%COLORS.length])}]},options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{display:false}},scales:{x:{ticks:{color:'#94a3b8'},grid:{display:false}},y:{ticks:{color:'#94a3b8'},grid:{color:GRIDC}}}}})}
function buildMiniCharts(){if(!HAS_CHART)return;const todo=[];(payload.results||[]).forEach((r,i)=>{const c=document.getElementById('benchChart'+i);if(c&&!c.dataset.done)todo.push([r,i,c])});if(!('IntersectionObserver' in window)){todo.forEach(([r,i])=>buildMiniChart(r,i));return}const io=new IntersectionObserver(es=>es.forEach(en=>{if(!en.isIntersecting)return;io.unobserve(en.target);const k=+en.target.id.replace('benchChart','');buildMiniChart(payload.results[k],k)}),{rootMargin:'200px'});todo.forEach(([r,i,c])=>io.observe(c))}
function parseBenchSuiteHtml(text){const doc=new DOMParser().parseFromString(text,'text/html');const el=doc.getElementById('payload');if(!el)throw new Error('payload BenchSuite introuvable');return JSON.parse(el.textContent)}

function normalizeBenchName(name) {
  if (!name) return "";
  let n = name.toLowerCase();
  if (n.includes("cpu") || n.includes("stress")) return "cpu";
  if (n.includes("gpu") || n.includes("burn") || n.includes("fryer") || n.includes("glmark")) return "gpu";
  if (n.includes("fio") || n.includes("dd") || n.includes("stockage") || n.includes("disk") || n.includes("storage")) return "storage";
  if (n.includes("ram") || n.includes("memtest") || n.includes("memory")) return "ram";
  if (n.includes("iperf") || n.includes("reseau") || n.includes("network")) return "network";
  return n.replace(/[^a-z0-9]/g, "");
}

function findBestMatchingBenchIdx(p, targetName) {
  const list = p.results || [];
  if (!targetName) return 0;
  const target = targetName.toLowerCase();
  
  // Exact match
  let idx = list.findIndex(r => (r.name || "").toLowerCase() === target);
  if (idx !== -1) return idx;
  
  // Smart category match
  const targetNorm = normalizeBenchName(targetName);
  idx = list.findIndex(r => normalizeBenchName(r.name) === targetNorm);
  if (idx !== -1) return idx;
  
  // Partial match
  idx = list.findIndex(r => {
    const n = (r.name || "").toLowerCase();
    return n && (n.includes(target) || target.includes(n));
  });
  if (idx !== -1) return idx;
  
  return 0; // Default to first
}

function autoMatchCompareReports() {
  const mainIdx = +(document.getElementById('compareBenchSelect')?.value || 0);
  const mainBench = (payload.results || [])[mainIdx] || {};
  compareReports.forEach((rep, ri) => {
    if (ri === 0) {
      rep.selectedBenchIdx = mainIdx;
    } else {
      rep.selectedBenchIdx = findBestMatchingBenchIdx(rep.payload, mainBench.name);
    }
  });
}

function compareBenchOptions(){
  const sel=document.getElementById('compareBenchSelect');
  if(!sel)return;
  sel.innerHTML=(payload.results||[]).map((r,i)=>'<option value="'+i+'">'+(i+1)+' — '+escapeHtml(r.name||'Benchmark')+'</option>').join('');
  sel.onchange=()=>{
    autoMatchCompareReports();
    compareMetric=preferredMetricFor((payload.results||[])[+sel.value]);
    buildCompareChart();
  };
  if((payload.results||[]).length) {
    compareMetric=preferredMetricFor(payload.results[0]);
    autoMatchCompareReports();
  }
}

const REPORT_COLORS=['#3b82f6','#10b981','#a855f7','#f59e0b','#ef4444','#06b6d4','#ec4899','#84cc16','#f97316','#14b8a6'];
const GPU_DASHES=[[],[8,4],[2,3],[10,4,2,4],[12,4],[4,2,1,2],[1,3],[6,2,2,2]];
function shortReportLabel(rep,ri){let name=(rep.customName||(rep.payload&&rep.payload.system&&rep.payload.system.name)||rep.label||('Rapport '+(ri+1))).replace(/\.html?$/i,'');if(name.length>34)name=name.slice(0,31)+'…';return 'R'+(ri+1)+' — '+name}
function seriesGpuIndex(s){const txt=((s&&s.label)||'')+' '+((s&&s.key)||'');const m=txt.match(/(?:GPU|gpu)\s*([0-9]+)/i)||txt.match(/gpu([0-9]+)_/i);return m?parseInt(m[1],10):0}

function renderCompareReportControls(){
  const box=document.getElementById('compareReportNames');
  if(!box)return;
  box.innerHTML=compareReports.map((rep,ri)=>{
    const c=REPORT_COLORS[ri%REPORT_COLORS.length];
    const v=escapeHtml(rep.customName||rep.label||('Rapport '+(ri+1)));
    const benchOptions = (rep.payload.results || []).map((r, i) => {
      const selected = (i === rep.selectedBenchIdx) ? 'selected' : '';
      return '<option value="'+i+'" '+selected+'>'+(i+1)+' — '+escapeHtml(r.name || 'Benchmark')+'</option>';
    }).join('');
    const removeBtn = ri > 0 ? '<button class="action-btn" type="button" onclick="removeCompareReport('+ri+')" style="background:#f38ba8; color:#11111b; border:none; margin-left:auto; padding:2px 6px; border-radius:4px; font-weight:bold; cursor:pointer;">❌</button>' : '';
    return '<div class="report-name-card" style="display:flex; flex-direction:column; align-items:stretch; gap:8px;"><div style="display:flex; align-items:center; gap:10px;"><span class="color-swatch" style="color:'+c+';background:'+c+'"></span><div style="width:100%"><div class="muted" style="display:flex; align-items:center;">R'+(ri+1)+' · nom affiché'+removeBtn+'</div><input data-report-name="'+ri+'" value="'+v+'"></div></div><div style="width:100%"><div class="muted" style="margin-bottom:4px;">Test comparé :</div><select class="select" data-report-select="'+ri+'" style="width:100%; padding:6px 8px; font-size:12px;">'+benchOptions+'</select></div></div>';
  }).join('');
  box.querySelectorAll('[data-report-name]').forEach(inp=>{
    inp.oninput=()=>{
      const i=+inp.dataset.reportName;
      if(compareReports[i])compareReports[i].customName=inp.value.trim()||compareReports[i].label||('Rapport '+(i+1));
      buildCompareChartOnly();
    };
  });
  box.querySelectorAll('[data-report-select]').forEach(sel=>{
    sel.onchange=()=>{
      const i=+sel.dataset.reportSelect;
      if(compareReports[i]) {
        compareReports[i].selectedBenchIdx = +sel.value;
        buildCompareChartOnly();
      }
    };
  });
}

function compareDatasets(){
  const conf=METRIC_GROUPS[compareMetric],datasets=[];
  compareReports.forEach((rep,ri)=>{
    const r=(rep.payload.results || [])[rep.selectedBenchIdx || 0] || {metrics:{}};
    const g=groupSeries(r,compareMetric);
    const reportColor=REPORT_COLORS[ri%REPORT_COLORS.length];
    const reportName=shortReportLabel(rep,ri);
    g.series.forEach((s,si)=>{
      const gpuIdx=seriesGpuIndex(s)||si;
      const dash=s.isLimit?[6,4]:GPU_DASHES[gpuIdx%GPU_DASHES.length];
      const color=s.isLimit?'#ef4444':reportColor;
      const label=reportName+' · '+s.label;
      datasets.push({label,reportName,seriesName:s.label,gpuIdx,data:g.t.slice(0,s.data.length).map((x,j)=>({x:+x,y:+s.data[j]})),borderColor:color,backgroundColor:color,borderWidth:ri===0?3:2,pointRadius:0,pointHoverRadius:s.isLimit?0:4,tension:0,fill:false,borderDash:dash,normalized:true,hidden:!!hiddenCompareSeries[label]})
    });
    if(!g.series.length){
      const scalars=groupScalars(r,compareMetric);
      scalars.forEach((s,si)=>{
        const label=reportName+' · '+s.label;
        datasets.push({label,reportName,seriesName:s.label,gpuIdx:si,data:[{x:ri+si*.08,y:s.value}],borderColor:reportColor,backgroundColor:reportColor,borderWidth:1,pointRadius:6,pointHoverRadius:8,tension:0,fill:false,borderDash:GPU_DASHES[si%GPU_DASHES.length],hidden:!!hiddenCompareSeries[label]})
      })
    }
  });
  return {datasets,conf}
}

function buildCompareChartOnly(){
  const idx=+(document.getElementById('compareBenchSelect').value||0);
  const cur=(payload.results||[])[idx]||{metrics:{}},available=applicableMetrics(cur);
  if(!available.includes(compareMetric))compareMetric=available[0]||'temp';
  renderMetricTabs('compareMetricTabs','compare-metric',available,compareMetric,g=>{compareMetric=g;buildCompareChartOnly()});
  const data=compareDatasets(),canvas=document.getElementById('compareChart');
  document.getElementById('compareTitle').textContent='Comparaison — '+data.conf.label;
  document.getElementById('compareSubtitle').textContent=compareReports.length+' rapport(s) · métriques limitées au benchmark sélectionné';
  const legendWrap=document.getElementById('compareLegend');
  const actionBtns='<div style="width:100%; display:flex; gap:10px; margin-bottom:12px;"><button type="button" class="tab" id="compShowAllBtn" style="padding:4px 10px; font-size:11px; border:1px solid var(--border-color);">Tout afficher</button><button type="button" class="tab" id="compHideAllBtn" style="padding:4px 10px; font-size:11px; border:1px solid var(--border-color);">Tout masquer</button><span class="muted" style="font-size:11px; align-self:center;">(Alt+Clic pour isoler une courbe)</span></div>';
  legendWrap.innerHTML=actionBtns+data.datasets.map(d=>{
    const dashed=(d.borderDash&&d.borderDash.length)?'dashed':'solid';
    const isMuted=hiddenCompareSeries[d.label];
    return '<span class="legend-item '+(isMuted?'muted':'')+'" data-comp-label="'+escapeHtml(d.label)+'" title="Fichier: '+escapeHtml(d.reportName||'')+'"><span class="legend-line" style="background:transparent;border-top:3px '+dashed+' '+d.borderColor+'"></span>'+escapeHtml(d.label)+'</span>'
  }).join('');
  const showBtn=document.getElementById('compShowAllBtn'), hideBtn=document.getElementById('compHideAllBtn');
  if(showBtn) showBtn.onclick=()=>{ hiddenCompareSeries={}; buildCompareChartOnly(); };
  if(hideBtn) hideBtn.onclick=()=>{ data.datasets.forEach(d=>{ hiddenCompareSeries[d.label]=true; }); buildCompareChartOnly(); };
  legendWrap.querySelectorAll('[data-comp-label]').forEach(el=>{
    el.onclick=(e)=>{
      const lbl=el.dataset.compLabel;
      if(e.altKey||e.shiftKey||e.ctrlKey){
        data.datasets.forEach(d=>{ hiddenCompareSeries[d.label]=(d.label!==lbl); });
      }else{
        hiddenCompareSeries[lbl]=!hiddenCompareSeries[lbl];
      }
      buildCompareChartOnly();
    };
  });
  if(compareChart&&compareChart.destroy)compareChart.destroy();
  if(!HAS_CHART){
    canvas.parentElement.innerHTML='<p class="muted">Chart.js non disponible.</p>';
    return;
  }
  compareChart=new Chart(canvas,{
    type:'line',
    data:{datasets:data.datasets},
    options:{
      animation:false,
      responsive:true,
      maintainAspectRatio:false,
      interaction:{mode:'nearest',intersect:false},
      plugins:{
        legend:{display:false},
        tooltip:{
          backgroundColor:'rgba(30,41,59,.96)',
          titleColor:'#f8fafc',
          bodyColor:'#cbd5e1',
          borderColor:'#334155',
          borderWidth:1,
          padding:12,
          callbacks:{
            title:items=>data.datasets.some(d=>d.data&&d.data.length===1)?'Rapport / métrique':'Temps : '+items[0].parsed.x+' s',
            label:item=>item.dataset.label+' : '+fmt(item.parsed.y)+' '+data.conf.unit
          }
        },
        zoom:{limits:{x:{min:'original',max:'original',minRange:0.1}},pan:{enabled:true,mode:'x',modifierKey:'shift',onPanComplete(){const compResetBtn=document.getElementById('compareResetZoomBtn');if(compResetBtn)compResetBtn.style.display='inline-block'}},zoom:{drag:{enabled:true,threshold:5,backgroundColor:'rgba(59,130,246,0.15)',borderColor:'rgba(59,130,246,0.4)',borderWidth:1},wheel:{enabled:true,modifierKey:'ctrl'},pinch:{enabled:true},mode:'x',onZoomStart({chart,point}){const a=chart.chartArea;return !a||!point||(point.x>=a.left&&point.x<=a.right&&point.y>=a.top&&point.y<=a.bottom)},onZoomComplete({chart}){const compResetBtn=document.getElementById('compareResetZoomBtn');if(compResetBtn)compResetBtn.style.display='inline-block'}}}
      },
      scales:{
        x:{
          type:'linear',
          title:{
            display:true,
            text:(data.datasets.some(d=>d.data&&d.data.length===1)?'Rapport / métrique':'Temps (secondes)'),
            color:'#94a3b8'
          },
          ticks:{color:'#94a3b8',maxTicksLimit:12},
          grid:{color:GRIDC}
        },
        y:{
          title:{
            display:true,
            text:data.conf.label+(data.conf.unit?' ('+data.conf.unit+')':''),
            color:'#94a3b8'
          },
          ticks:{color:'#94a3b8'},
          grid:{color:GRIDC}
        }
      }
    }
  });
  const compResetBtn=document.getElementById('compareResetZoomBtn');if(compResetBtn){compResetBtn.style.display='none';compResetBtn.onclick=()=>{compareChart.resetZoom();compResetBtn.style.display='none'}};canvas.ondblclick=()=>{compareChart.resetZoom();if(compResetBtn)compResetBtn.style.display='none'};
  renderCompareTable();
}

function buildCompareChart(){
  renderCompareReportControls();
  buildCompareChartOnly();
}

function hasNestedGpuSeries(m){const nested=(m&&m.series&&typeof m.series==='object')?m.series:{};return Object.entries(nested).some(([k,v])=>/^gpu\d+_/i.test(k)&&Array.isArray(v)&&v.length)}
function hideMetricInHtmlSummary(m,k){return hasNestedGpuSeries(m)&&['series','series_t','series_temp','series_power','series_freq_graphics','series_freq_memory'].includes(k)}
function metricSummaryForCompare(r){const m=(r&&r.metrics)||{},out={};for(const [k,v] of Object.entries(m)){if(hideMetricInHtmlSummary(m,k))continue;if(Array.isArray(v)&&v.length){const vals=v.map(Number).filter(Number.isFinite);if(vals.length){const avg=vals.reduce((a,b)=>a+b,0)/vals.length;out[k+'_avg']=avg;out[k+'_max']=Math.max(...vals)}}else if(typeof v==='number'||(typeof v==='string'&&v.trim()!==''&&!isNaN(+v)))out[k]=+v}return out}

function compactRowsForReport(rep,ri){
  const p=rep.payload||{},label=shortReportLabel(rep,ri),color=REPORT_COLORS[ri%REPORT_COLORS.length];
  const idx = rep.selectedBenchIdx || 0;
  const r = (p.results || [])[idx];
  if (!r) return [];
  const ms=metricSummaryForCompare(r);
  const keys=Object.keys(ms).filter(k=>/(temp|power|watt|freq|clock|error|gflop|tflop|ram|read|write|throughput)/i.test(k)).slice(0,8);
  return [{
    report:label,
    color,
    system:(p.system&&p.system.name)||'',
    bench:r.name||('Benchmark '+(idx+1)),
    status:r.status||'',
    duration:(r.duration_s??'')+' s',
    metrics:keys.map(k=>k+'='+fmt(+ms[k])).join(' · ')
  }];
}

function renderCompareTable(){
  const rows=compareReports.flatMap((x,i)=>compactRowsForReport(x,i));
  document.getElementById('compareOutput').innerHTML='<div class="table-header-flex" style="margin-top:20px;"><h3>Résumé comparatif</h3><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest(\'#compareOutput\').querySelector(\'table\'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest(\'#compareOutput\').querySelector(\'table\'))">📋 Excel</button></div></div><p class="muted">Les pastilles utilisent les mêmes couleurs que les courbes du graphique.</p><table><thead><tr><th>Rapport</th><th>Système</th><th>Benchmark</th><th>Statut</th><th>Durée</th><th>Métriques clés</th></tr></thead><tbody>'+rows.map(r=>'<tr><td><span class="report-badge"><span class="color-swatch" style="color:'+r.color+';background:'+r.color+'"></span>'+escapeHtml(r.report)+'</span></td><td>'+escapeHtml(r.system)+'</td><td>'+escapeHtml(r.bench)+'</td><td>'+escapeHtml(r.status)+'</td><td>'+escapeHtml(r.duration)+'</td><td>'+escapeHtml(r.metrics)+'</td></tr>').join('')+'</tbody></table>'
}

function renderNvbandwidthTab() {
  const results = payload.results || [];
  let nvRes = null;
  for (const r of results) {
    if (r.metrics && r.metrics.nvbandwidth_data) {
      nvRes = r;
      break;
    }
  }
  const tabBtn = document.getElementById('nvbandwidthTab');
  const viewSec = document.getElementById('nvbandwidth');
  if (!nvRes) {
    if (tabBtn) tabBtn.style.display = 'none';
    if (viewSec) viewSec.style.display = 'none';
    return;
  }
  
  if (tabBtn) tabBtn.style.display = '';
  if (viewSec) viewSec.style.display = '';
  
  const content = document.getElementById('nvbandwidthContent');
  if (!content) return;
  
  const testcases = nvRes.metrics.nvbandwidth_data;
  if (!Array.isArray(testcases) || !testcases.length) {
    content.innerHTML = '<p class="muted">Aucune donnée détaillée nvbandwidth disponible dans les métriques.</p>';
    return;
  }
  
  let html = '';
  html += '<style>' +
    '.nv-testcase { margin-bottom: 32px; border-bottom: 1px solid var(--border-color); padding-bottom: 24px; }' +
    '.nv-testcase:last-child { border-bottom: none; }' +
    '.nv-title { font-size: 16px; font-weight: 700; color: #a78bfa; margin-bottom: 10px; display: flex; align-items: center; gap: 8px; }' +
    '.nv-grid-container { display: flex; gap: 24px; flex-wrap: wrap; margin-top: 14px; }' +
    '.nv-table-wrap { flex: 1 1 350px; }' +
    '.nv-heatmap-wrap { flex: 1 1 400px; min-width: 320px; }' +
    '.nv-heatmap { border-collapse: separate; border-spacing: 4px; width: auto; margin-top: 8px; }' +
    '.nv-heatmap th, .nv-heatmap td { padding: 12px 16px; text-align: center; border-radius: 6px; font-weight: 600; border: none; font-family: "Fira Code", monospace; }' +
    '.nv-heatmap th { background: rgba(255,255,255,.05); color: #fff; font-size: 12px; }' +
    '.nv-heatmap td { transition: transform 0.2s ease, filter 0.2s ease, box-shadow 0.2s ease; }' +
    '.nv-heatmap td:hover:not(.diag) { transform: scale(1.06); filter: brightness(1.2); cursor: pointer; z-index: 10; }' +
    '.nv-heatmap td.diag { background: rgba(255,255,255,.02) !important; color: rgba(255,255,255,.15); font-style: italic; }' +
    '.nv-badge { display: inline-block; font-size: 11px; background: rgba(167, 139, 250, 0.15); color: #c084fc; padding: 2px 6px; border-radius: 4px; font-family: "Fira Code", monospace; }' +
    '</style>';
  
  testcases.forEach(tc => {
    const name = tc.name || "Testcase";
    const res = tc.results || {};
    const avg = res.average || res.mean || res.bandwidth;
    const sum = res.sum;
    const min = res.min;
    const max = res.max;
    const matrix = res.bandwidth_matrix || res.matrix;
    const isLatency = name.toLowerCase().includes("latency") || name.toLowerCase().includes("lat");
    const unit = isLatency ? "us" : "GB/s";
    
    html += '<div class="nv-testcase">';
    html += '<div class="nv-title"><span>⚡</span> ' + escapeHtml(name) + '</div>';
    html += '<div class="nv-grid-container">';
    
    html += '<div class="nv-table-wrap">';
    html += '<div class="table-header-flex"><h4>Résumé des métriques</h4><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest(\'.nv-table-wrap\').querySelector(\'table\'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest(\'.nv-table-wrap\').querySelector(\'table\'))">📋 Excel</button></div></div>';
    html += '<table><thead><tr><th>Métrique</th><th>Valeur</th></tr></thead><tbody>';
    const bufferSize = nvRes.metrics.buffer_size_mb;
    const iterations = nvRes.metrics.iterations;
    const extraArgs = nvRes.metrics.extra_args;
    if (bufferSize !== undefined) html += '<tr><td>Taille buffer</td><td><span class="nv-badge">' + bufferSize + ' Mo</span></td></tr>';
    if (iterations !== undefined) html += '<tr><td>Itérations</td><td><span class="nv-badge">' + iterations + '</span></td></tr>';
    if (extraArgs) html += '<tr><td>Options CLI supp.</td><td><code>' + escapeHtml(extraArgs) + '</code></td></tr>';
    if (avg !== undefined) html += '<tr><td>Moyenne</td><td><b>' + fmt(avg) + ' ' + unit + '</b></td></tr>';
    if (sum !== undefined) html += '<tr><td>Somme globale</td><td>' + fmt(sum) + ' ' + unit + '</td></tr>';
    if (min !== undefined) html += '<tr><td>Valeur minimale</td><td>' + fmt(min) + ' ' + unit + '</td></tr>';
    if (max !== undefined) html += '<tr><td>Valeur maximale</td><td>' + fmt(max) + ' ' + unit + '</td></tr>';
    if (res.coefficient_of_variation !== undefined) html += '<tr><td>Coef. Variation (CoV)</td><td>' + fmt(res.coefficient_of_variation) + '</td></tr>';
    html += '</tbody></table>';
    html += '</div>';
    
    if (Array.isArray(matrix) && matrix.length > 0) {
      const numGpus = matrix.length;
      html += '<div class="nv-heatmap-wrap">';
      html += '<div class="table-header-flex"><h4>Matrice inter-GPU (' + unit + ')</h4><div class="table-actions" data-html2canvas-ignore="true"><button class="action-btn" onclick="copyTableToImage(this, this.closest(\'.nv-heatmap-wrap\').querySelector(\'table\'))">📸 Image</button><button class="action-btn" onclick="copyTableToExcel(this, this.closest(\'.nv-heatmap-wrap\').querySelector(\'table\'))">📋 Excel</button></div></div>';
      html += '<table class="nv-heatmap">';
      html += '<thead><tr><th>De \\ À</th>';
      for (let j = 0; j < numGpus; j++) {
        html += '<th>GPU ' + j + '</th>';
      }
      html += '</tr></thead><tbody>';
      
      let maxValInMatrix = 1.0;
      matrix.forEach(r => { if(Array.isArray(r)) r.forEach(v => { if (v && v > maxValInMatrix) maxValInMatrix = v; })});
      
      matrix.forEach((row, rIdx) => {
        html += '<tr><th>GPU ' + rIdx + '</th>';
        for (let cIdx = 0; cIdx < numGpus; cIdx++) {
          if (rIdx === cIdx) {
            html += '<td class="diag">—</td>';
          } else {
            const val = Array.isArray(row) ? row[cIdx] : undefined;
            if (val === undefined || val === null) {
              html += '<td class="diag">N/A</td>';
            } else {
              const pct = Math.min(1.0, Math.max(0.15, val / maxValInMatrix));
              const colorBase = isLatency ? "34, 211, 238" : "167, 139, 250";
              const fontColor = pct > 0.6 ? "#ffffff" : "#cbd5e1";
              html += '<td style="background: rgba(' + colorBase + ', ' + pct + '); color: ' + fontColor + '; box-shadow: 0 0 10px rgba(' + colorBase + ', ' + (pct*0.2) + ');" title="GPU ' + rIdx + ' → GPU ' + cIdx + ' : ' + val + ' ' + unit + '">' + fmt(val) + '</td>';
            }
          }
        }
        html += '</tr>';
      });
      html += '</tbody></table>';
      html += '</div>';
    }
    
    html += '</div>';
    html += '</div>';
  });
  
  content.innerHTML = html;
}

document.getElementById('mainTabs')?.addEventListener('click',e=>{const b=e.target.closest('[data-view]');if(!b)return;document.querySelectorAll('.view').forEach(v=>{v.classList.remove('active');if(v.style.display==='block'||v.style.display==='none')v.style.display='';});const target=document.getElementById(b.dataset.view);if(target){target.classList.add('active');target.style.display='block';}document.querySelectorAll('#mainTabs .tab').forEach(x=>x.classList.remove('active'));b.classList.add('active');if(b.dataset.view==='json')setTimeout(updateJsonDump,30);if(b.dataset.view==='charts')setTimeout(buildMainChart,50);if(b.dataset.view==='benches')setTimeout(buildMiniCharts,50);if(b.dataset.view==='electronics')setTimeout(()=>{buildSeqCharts();buildElecCharts()},50);if(b.dataset.view==='compare')setTimeout(buildCompareChart,50)});

const compareInput=document.getElementById('compareFiles');
if(compareInput){
  compareInput.addEventListener('change',async e=>{
    for(const f of e.target.files){
      try{
        const text=await f.text();
        const p=parseBenchSuiteHtml(text);
        const exists=compareReports.some(r=>r.label===((p.system&&p.system.name)||f.name)&&r.payload.exported_at===p.exported_at);
        if(!exists){
          compareReports.push({label:(p.system&&p.system.name)||f.name,customName:(p.system&&p.system.name)||f.name,payload:p,selectedBenchIdx:0});
        }
      }catch(err){
        compareReports.push({label:f.name,customName:f.name,payload:{system:{name:'Erreur lecture'},results:[{name:'Erreur import',status:'error',unit:'',duration_s:0,metrics:{erreur:String(err)}}]},selectedBenchIdx:0});
      }
    }
    compareInput.value='';
    autoMatchCompareReports();
    buildCompareChart();
  });
}

function removeCompareReport(idx){
  if(idx>0&&compareReports[idx]){
    compareReports.splice(idx,1);
    autoMatchCompareReports();
    buildCompareChart();
  }
}

benchOptions();compareBenchOptions();renderCompareReportControls();bindNoteEditors();initVerdictAndAlerts();buildMainChart();buildCompareChart();renderNvbandwidthTab();
</script></body></html>"""

    meta_html = ""
    meta_info = payload.get("metadata", {})
    if meta_info.get("model"):
        meta_html += f"· Modèle : <b>{_html.escape(meta_info['model'])}</b> "
    if meta_info.get("serial"):
        meta_html += f"· N° de série : <b>{_html.escape(meta_info['serial'])}</b> "
    if meta_info.get("order_no"):
        meta_html += f"· Commande : <b>{_html.escape(meta_info['order_no'])}</b> "
    if meta_info.get("operator"):
        meta_html += f"· Opérateur : <b>{_html.escape(meta_info['operator'])}</b> "
    if meta_info.get("client"):
        meta_html += f"· Client : <b>{_html.escape(meta_info['client'])}</b> "
    if meta_info.get("ambient_temp"):
        meta_html += f"· T. ambiante : <b>{_html.escape(meta_info['ambient_temp'])}</b> "
    if meta_info.get("fan_speed"):
        meta_html += f"· Ventilation : <b>{_html.escape(meta_info['fan_speed'])}</b> "

    html_template = _add_light_theme(html_template)
    html_doc = (html_template
        .replace("%%TITLE%%", _html.escape(payload["system"]["name"]))
        .replace("%%CHART_SCRIPTS%%", _chart_scripts_html())
        .replace("%%DIAGNOSTIC_BOX%%", _verdict_html(payload.get("verdict")) + diagnostic_html)
        .replace("%%DEFAULT_THEME%%", _default_theme())
        .replace("%%PAYLOAD%%", data_json)
        .replace("%%SYSTEM%%", _html.escape(payload["system"]["name"]))
        .replace("%%META_INFO%%", meta_html)
        .replace("%%EXPORTED%%", _html.escape(payload["exported_at"]))
        .replace("%%VERSION%%", _html.escape(payload["benchsuite_version"]))
        .replace("%%COUNT%%", str(len(results)))
        .replace("%%OK%%", str(payload["summary"]["ok"]))
        .replace("%%FAILED%%", str(payload["summary"]["failed"]))
        .replace("%%STOP_CARD%%", _stop_card_html(payload["summary"]))
        .replace("%%COVERED%%", covered_sentence)
        .replace("%%CATEGORY_CARDS%%", category_cards)
        .replace("%%CPU_ROWS%%", cpu_rows)
        .replace("%%STORAGE_ROWS%%", storage_rows)
        .replace("%%GPU_ROWS%%", gpu_rows)
        .replace("%%NETWORK_ROWS%%", network_rows)
        .replace("%%SYS_ROWS%%", sys_rows)
        .replace("%%ELECTRONICS%%", electronics_html)
        .replace("%%ELEC_DATA%%", elec_json)
        .replace("%%BENCH_SECTIONS%%", ''.join(bench_sections)))
    if not electronics_html:
        html_doc = html_doc.replace('<button class="tab" data-view="electronics">Séquence &amp; électronique</button>', "")
        html_doc = re.sub(r'<section id="electronics" class="view">.*?</section>', "", html_doc, count=1, flags=re.S)
    if not results:
        # Rapport « informations système » seul : on retire tout ce qui concerne des tests (onglets, cartes, graphiques)
        for view, label in (("cpu", "CPU + RAM"), ("storage", "Stockage"), ("gpu", "GPU"), ("network", "Réseau"),
                            ("charts", "Graphique"), ("benches", "Résultats détaillés"), ("compare", "Comparaison HTML")):
            # les sections restent dans le DOM (cachées, non atteignables) : le script du rapport y cherche ses éléments
            html_doc = html_doc.replace(f'<button class="tab" data-view="{view}">{label}</button>', "")
        html_doc = html_doc.replace("BenchSuite — Rapport de validation", "BenchSuite — Informations système", 1)
        html_doc = re.sub(r'<div class="stat-card"><div class="stat-label"><span class="stat-dot"[^>]*></span>(?:Benchmarks|OK|Échecs|Interrompus)</div>.*?</div></div>', "", html_doc, flags=re.S)
        html_doc = re.sub(r'<div class="card"><h2>Tests couverts</h2>.*?(?=</section>)', "", html_doc, count=1, flags=re.S)
    if results:
        for bucket, label in (("cpu", "CPU + RAM"), ("storage", "Stockage"), ("gpu", "GPU"), ("network", "Réseau")):
            if not category_rows[bucket]:
                html_doc = html_doc.replace(f'<button class="tab" data-view="{bucket}">{label}</button>', "")
                html_doc = re.sub(rf'<section id="{bucket}" class="view">.*?</section>', "", html_doc, count=1, flags=re.S)
    with open(path, "w", encoding="utf-8") as f:
        f.write(html_doc)
    return path
