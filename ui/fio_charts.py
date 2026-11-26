"""
ui/fio_charts.py — Graphiques de performance FIO dessinés en Cairo (GTK4 natif)
- FioBarChart   : graphique en barres pour débit / IOPS / latence
- FioDiskCard   : carte d'identité du disque avec ses spécifications
- FioResultPanel: panneau complet (graphiques + specs + historique comparatif)
"""

import math
from datetime import datetime
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
try:
    import cairo
    gi.require_foreign("cairo")   # exige python3-gi-cairo : sinon « foreign struct converter for cairo.Context » à chaque dessin
    HAS_CAIRO = True
except Exception:
    HAS_CAIRO = False


# ─────────────────────────────────────────────────────────────────────────────
# Helpers couleurs (r,g,b en 0-1)
# ─────────────────────────────────────────────────────────────────────────────

def _hex(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))

C_BG      = _hex("1e1e2e")
C_PANEL   = _hex("181825")
C_BORDER  = _hex("313244")
C_TEXT    = _hex("cdd6f4")
C_MUTED   = _hex("6c7086")
C_READ    = _hex("f97316")   # orange  = lecture
C_WRITE   = _hex("22d3ee")   # cyan    = écriture
C_IOPS_R  = _hex("a78bfa")   # violet  = IOPS lecture
C_IOPS_W  = _hex("e879f9")   # rose    = IOPS écriture
C_LAT     = _hex("f9e2af")   # jaune   = latence
C_LAT2    = _hex("f38ba8")   # rouge   = P99/P999
C_OK      = _hex("a6e3a1")
C_ACCENT  = _hex("f97316")


def _set(ctx, col, alpha=1.0):
    ctx.set_source_rgba(*col, alpha)


# ─────────────────────────────────────────────────────────────────────────────
# Widget Cairo de base
# ─────────────────────────────────────────────────────────────────────────────

class CairoWidget(Gtk.DrawingArea):
    """Widget GTK4 avec rendu Cairo. Sous-classer et implémenter _draw(ctx, w, h)."""

    def __init__(self):
        super().__init__()
        if HAS_CAIRO:   # sans python3-gi-cairo, GTK lèverait « foreign struct converter for cairo.Context » à chaque dessin
            self.set_draw_func(self._draw_func)

    def _draw_func(self, area, ctx, w, h):
        if not HAS_CAIRO:
            return
        # Fond
        _set(ctx, C_BG)
        ctx.paint()
        self._draw(ctx, w, h)

    def _draw(self, ctx, w, h):
        pass   # à implémenter

    def redraw(self):
        self.queue_draw()


# ─────────────────────────────────────────────────────────────────────────────
# Graphique en barres groupées
# ─────────────────────────────────────────────────────────────────────────────

class FioBarChart(CairoWidget):
    """
    Graphique en barres groupées pour un type de métrique FIO.
    Supporte plusieurs sessions (comparaison).
    groups = [{"label": str, "values": [float, ...], "colors": [...]}]
    bar_labels = ["Lecture", "Ecriture"]   (une par valeur dans chaque groupe)
    unit = "MiB/s"
    title = "Débit"
    """

    def __init__(self, title: str, unit: str,
                 bar_labels: list, groups: list,
                 min_height: int = 220):
        super().__init__()
        self.title      = title
        self.unit       = unit
        self.bar_labels = bar_labels
        self.groups     = groups       # list of {"label","values","colors"}
        self.set_size_request(-1, min_height)
        self.set_vexpand(False)
        self.set_hexpand(True)

    def update(self, groups: list):
        self.groups = groups
        self.redraw()

    def _draw(self, ctx, W, H):
        if not self.groups:
            return

        PAD_L = 62   # espace axe Y
        PAD_R = 16
        PAD_T = 38   # titre
        PAD_B = 44   # labels

        chart_x = PAD_L
        chart_y = PAD_T
        chart_w = W - PAD_L - PAD_R
        chart_h = H - PAD_T - PAD_B

        # ── Fond panneau ──
        ctx.set_line_width(1)
        _set(ctx, C_PANEL)
        _roundrect(ctx, 0, 0, W, H, 10)
        ctx.fill()
        _set(ctx, C_BORDER)
        _roundrect(ctx, 0, 0, W, H, 10)
        ctx.stroke()

        # ── Titre ──
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(12)
        _set(ctx, C_TEXT)
        te = ctx.text_extents(self.title)
        ctx.move_to(W / 2 - te.width / 2, 22)
        ctx.show_text(self.title)

        # ── Valeurs max pour l'échelle ──
        all_vals = [v for g in self.groups for v in g.get("values", [])]
        max_val  = max(all_vals) * 1.18 if all_vals else 1
        if max_val == 0: max_val = 1

        # ── Grille + labels axe Y ──
        ctx.set_font_size(9)
        n_lines = 5
        for i in range(n_lines + 1):
            frac = i / n_lines
            gy   = chart_y + chart_h - frac * chart_h
            # Ligne de grille
            ctx.set_line_width(0.4)
            _set(ctx, C_BORDER)
            ctx.move_to(chart_x, gy); ctx.line_to(chart_x + chart_w, gy)
            ctx.stroke()
            # Valeur
            v_label = _fmt_val(max_val * frac, self.unit)
            te = ctx.text_extents(v_label)
            _set(ctx, C_MUTED)
            ctx.move_to(chart_x - te.width - 6, gy + te.height / 2)
            ctx.show_text(v_label)

        # Unité axe Y
        ctx.set_font_size(8)
        _set(ctx, C_MUTED)
        ctx.save()
        ctx.translate(11, chart_y + chart_h / 2)
        ctx.rotate(-math.pi / 2)
        te = ctx.text_extents(self.unit)
        ctx.move_to(-te.width / 2, 0)
        ctx.show_text(self.unit)
        ctx.restore()

        # ── Barres ──
        n_groups  = len(self.groups)
        n_bars    = len(self.bar_labels)
        grp_w     = chart_w / n_groups
        bar_w     = min(grp_w * 0.8 / max(n_bars, 1), 48)
        bar_gap   = 4

        for gi, group in enumerate(self.groups):
            grp_cx = chart_x + gi * grp_w + grp_w / 2
            values = group.get("values", [])
            colors = group.get("colors", [C_READ, C_WRITE, C_IOPS_R, C_IOPS_W, C_LAT])

            total_bw = (n_bars * bar_w + (n_bars - 1) * bar_gap)
            start_x  = grp_cx - total_bw / 2

            for bi, (val, col) in enumerate(zip(values, colors)):
                bx = start_x + bi * (bar_w + bar_gap)
                bh = (val / max_val) * chart_h if max_val > 0 else 0
                by = chart_y + chart_h - bh

                # Ombre légère
                _set(ctx, (0, 0, 0), 0.3)
                ctx.rectangle(bx + 2, by + 2, bar_w, bh)
                ctx.fill()

                # Barre avec dégradé vertical
                pat = cairo.LinearGradient(bx, by, bx, by + bh)
                pat.add_color_stop_rgba(0, *col, 1.0)
                pat.add_color_stop_rgba(1, *col, 0.6)
                ctx.set_source(pat)
                _roundrect(ctx, bx, by, bar_w, bh, 3)
                ctx.fill()

                # Contour barre
                _set(ctx, col, 0.4)
                ctx.set_line_width(0.5)
                _roundrect(ctx, bx, by, bar_w, bh, 3)
                ctx.stroke()

                # Valeur au-dessus
                if val > 0:
                    ctx.set_font_size(8.5)
                    ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL,
                                         cairo.FONT_WEIGHT_BOLD)
                    vstr = _fmt_val(val, self.unit)
                    te   = ctx.text_extents(vstr)
                    # Fond pastille
                    _set(ctx, C_PANEL, 0.85)
                    ctx.rectangle(bx + bar_w / 2 - te.width / 2 - 2,
                                  by - 14, te.width + 4, 12)
                    ctx.fill()
                    _set(ctx, col)
                    ctx.move_to(bx + bar_w / 2 - te.width / 2, by - 4)
                    ctx.show_text(vstr)

            # Label groupe (session / disque)
            ctx.set_font_size(9)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            grp_label = group.get("label", "")
            if grp_label:
                te = ctx.text_extents(grp_label)
                _set(ctx, C_TEXT)
                ctx.move_to(grp_cx - te.width / 2,
                             chart_y + chart_h + 14)
                ctx.show_text(grp_label)

        # ── Légende ──
        leg_x = chart_x
        leg_y = chart_y + chart_h + 28
        ctx.set_font_size(9)
        for i, (lbl, col) in enumerate(zip(self.bar_labels, [C_READ, C_WRITE, C_IOPS_R, C_IOPS_W, C_LAT, C_LAT2])):
            lx = leg_x + i * 90
            if lx + 80 > W: break
            # Carré couleur
            _set(ctx, col)
            ctx.rectangle(lx, leg_y - 8, 10, 10)
            ctx.fill()
            _set(ctx, C_TEXT)
            ctx.move_to(lx + 14, leg_y)
            ctx.show_text(lbl)

        # ── Axe X ──
        ctx.set_line_width(1)
        _set(ctx, C_BORDER)
        ctx.move_to(chart_x, chart_y + chart_h)
        ctx.line_to(chart_x + chart_w, chart_y + chart_h)
        ctx.stroke()


# ─────────────────────────────────────────────────────────────────────────────
# Carte identité disque (spécifications)
# ─────────────────────────────────────────────────────────────────────────────

class FioDiskCard(CairoWidget):
    """
    Carte Cairo affichant les spécifications du disque testé.
    disk_info = {
      "name": "Samsung 990 Pro 2To",
      "dev":  "/dev/nvme0n1",
      "size": "2T",
      "type": "NVMe",
      "model": "...",
      "serial": "...",
      "smart": "OK",
      "interface": "PCIe 4.0 x4",
      ...
    }
    test_params = {rw_mode, bs, iodepth, numjobs, io_engine, ...}
    """

    def __init__(self, disk_info: dict, test_params: dict):
        super().__init__()
        self.disk_info   = disk_info
        self.test_params = test_params
        self.set_size_request(-1, 180)
        self.set_vexpand(False); self.set_hexpand(True)

    def update(self, disk_info: dict, test_params: dict):
        self.disk_info   = disk_info
        self.test_params = test_params
        self.redraw()

    def _draw(self, ctx, W, H):
        PAD = 16

        # Fond
        _set(ctx, C_PANEL)
        _roundrect(ctx, 0, 0, W, H, 10); ctx.fill()
        _set(ctx, C_BORDER)
        _roundrect(ctx, 0, 0, W, H, 10); ctx.stroke()

        # Bande couleur gauche
        _set(ctx, C_ACCENT)
        _roundrect(ctx, 0, 0, 4, H, 2); ctx.fill()

        col_w = (W - PAD * 3) / 2
        x1 = PAD + 8
        x2 = x1 + col_w + PAD

        # ── Titre ──
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
        ctx.set_font_size(13)
        _set(ctx, C_ACCENT)
        name = self.disk_info.get("model") or self.disk_info.get("name", "Disque NVMe")
        ctx.move_to(x1, PAD + 12)
        ctx.show_text(name[:45])

        # Chemin device
        ctx.set_font_size(9)
        ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
        _set(ctx, C_MUTED)
        dev_str = self.disk_info.get("dev", "") or self.disk_info.get("path", "")
        ctx.move_to(x1, PAD + 26)
        ctx.show_text(dev_str)

        sep_y = PAD + 34
        _set(ctx, C_BORDER)
        ctx.set_line_width(0.5)
        ctx.move_to(x1, sep_y); ctx.line_to(W - PAD, sep_y); ctx.stroke()

        # ── Colonnes d'infos ──
        ctx.set_font_size(10)
        y = sep_y + 16

        def kv(x, cy, key, val, col=C_TEXT):
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_NORMAL)
            _set(ctx, C_MUTED); ctx.move_to(x, cy); ctx.show_text(key)
            ctx.select_font_face("Sans", cairo.FONT_SLANT_NORMAL, cairo.FONT_WEIGHT_BOLD)
            _set(ctx, col)
            kw = ctx.text_extents(key).width
            ctx.move_to(x + kw + 6, cy); ctx.show_text(str(val)[:28])

        di   = self.disk_info
        tp   = self.test_params
        dy   = 16

        # Colonne gauche — disque
        kv(x1, y,      "Taille : ", di.get("size", "?"))
        kv(x1, y+dy,   "Type   : ", di.get("type", "NVMe"))
        kv(x1, y+dy*2, "S/N    : ", (di.get("serial") or "—")[:16])
        smart = di.get("smart", "")
        smart_col = C_OK if "OK" in str(smart).upper() else (C_LAT2 if smart else C_MUTED)
        kv(x1, y+dy*3, "SMART  : ", smart or "non vérifié", smart_col)

        # Colonne droite — paramètres du test
        kv(x2, y,      "Mode   : ", tp.get("rw_mode", "?"))
        kv(x2, y+dy,   "Bloc   : ", tp.get("bs", "?"))
        kv(x2, y+dy*2, "Depth  : ", f"iodepth={tp.get('iodepth','?')}  jobs={tp.get('numjobs','?')}")
        kv(x2, y+dy*3, "Engine : ", tp.get("io_engine", "?"))


# ─────────────────────────────────────────────────────────────────────────────
# Graphique en ligne (Temps Réel / Courbes thermiques et puissance SSD)
# ─────────────────────────────────────────────────────────────────────────────

class FioLiveChart(CairoWidget):
    """
    Graphique en ligne temps réel / post-test pour la température (°C) et puissance (W) du SSD.
    """
    def __init__(self, min_height: int = 220):
        super().__init__()
        self._pts = {"t": [], "temp": [], "power": []}
        self.set_size_request(-1, min_height)
        self.set_vexpand(False)
        self.set_hexpand(True)

    def reset(self):
        self._pts = {"t": [], "temp": [], "power": []}
        self.queue_draw()

    def push(self, point: dict):
        self._pts["t"].append(point.get("t", 0.0))
        self._pts["temp"].append(point.get("temp", point.get("disk_temp", point.get("ssd_temp", 0.0))))
        self._pts["power"].append(point.get("power", point.get("disk_power", 0.0)))
        self.queue_draw()
        return False

    def set_series(self, series: dict):
        if not series or not series.get("t"):
            return
        t = series.get("t", [])
        temp = series.get("disk_temp", series.get("temp", [0]*len(t)))
        power = series.get("disk_power", series.get("power", [0]*len(t)))
        self._pts = {"t": list(t), "temp": list(temp), "power": list(power)}
        self.queue_draw()

    def _draw(self, ctx, w, h):
        ctx.select_font_face("Sans", 0, 0)
        pts = self._pts
        t_vals = pts.get("t", [])
        if len(t_vals) < 2:
            _set(ctx, C_MUTED)
            ctx.set_font_size(11)
            ctx.move_to(w / 2 - 120, h / 2 + 4)
            ctx.show_text("En attente de données thermiques et puissance Disque…")
            return

        t_min = t_vals[0]
        t_max = max(t_vals[-1], t_min + 1.0)
        ml = 48; mr = 16
        pw = w - ml - mr
        time_bar = 16

        panels = [
            ("Temp Disque (°C)", "temp", "f38ba8"),
            ("Puiss. Disque (W)", "power", "f97316"),
        ]
        active = [p for p in panels if any(v > 0 for v in pts.get(p[1], []))]
        if not active:
            _set(ctx, C_MUTED)
            ctx.set_font_size(11)
            ctx.move_to(w / 2 - 120, h / 2 + 4)
            ctx.show_text("Aucune donnée capteur Disque (> 0) disponible")
            return

        panel_h = (h - time_bar) / len(active)

        for pi, (p_lbl, p_key, col_hex) in enumerate(active):
            py = pi * panel_h
            bg = "181825" if pi % 2 == 0 else "1e1e2e"
            _set(ctx, _hex(bg))
            ctx.rectangle(0, py, w, panel_h); ctx.fill()

            if pi > 0:
                _set(ctx, _hex("45475a"), 1.0)
                ctx.set_line_width(0.5)
                ctx.move_to(0, py); ctx.line_to(w, py); ctx.stroke()

            pt = py + 6; pb = py + panel_h - 6
            ph = pb - pt
            if ph < 8: continue

            vals = pts.get(p_key, [])
            vmin, vmax = min(vals), max(vals)
            if vmax == vmin: vmax = vmin + 1.0

            # Grille
            _set(ctx, _hex("313244"), 0.8)
            ctx.set_line_width(0.4)
            for frac in (0.0, 0.5, 1.0):
                gy = pt + ph * frac
                ctx.move_to(ml, gy); ctx.line_to(w - mr, gy); ctx.stroke()

            # Labels Y
            _set(ctx, _hex("585b70"))
            ctx.set_font_size(8)
            ctx.move_to(2, pt + 8); ctx.show_text(f"{vmax:.1f}")
            ctx.move_to(2, pb); ctx.show_text(f"{vmin:.1f}")
            ctx.set_font_size(7.5)
            ctx.move_to(2, pt + ph / 2 + 3); ctx.show_text(p_lbl)

            # Courbe
            _set(ctx, _hex(col_hex))
            ctx.set_line_width(1.8)
            first = True
            for tv, v in zip(t_vals, vals):
                x = ml + pw * (tv - t_min) / (t_max - t_min)
                y = pt + ph * (1.0 - (v - vmin) / (vmax - vmin))
                if first: ctx.move_to(x, y); first = False
                else: ctx.line_to(x, y)
            ctx.stroke()

            # Point final
            tx = ml + pw
            ty = pt + ph * (1.0 - (vals[-1] - vmin) / (vmax - vmin))
            ctx.arc(tx, ty, 3, 0, 6.2832); ctx.fill()
            unit = "°C" if "temp" in p_key else "W"
            tag = f"{vals[-1]:.1f}{unit}"
            _set(ctx, _hex("cdd6f4"))
            ctx.set_font_size(8.5)
            ctx.move_to(min(tx - len(tag)*5 - 5, w - mr - 40), ty - 5 if ty > pt + 12 else ty + 12)
            ctx.show_text(tag)

        # Axe du temps
        _set(ctx, _hex("585b70"))
        ctx.set_font_size(7.5)
        ctx.move_to(ml, h - 2); ctx.show_text(f"{t_min:.0f}s")
        end_lbl = f"{t_max:.0f}s"
        ctx.move_to(w - mr - len(end_lbl)*5, h - 2); ctx.show_text(end_lbl)


# ─────────────────────────────────────────────────────────────────────────────
# Panneau de résultats complet
# ─────────────────────────────────────────────────────────────────────────────

class FioResultPanel(Gtk.Box):
    """
    Panneau GTK de résultats FIO avec :
    - Carte identité du disque (spécifications)
    - Score principal + progression
    - 3 graphiques : Débit, IOPS, Latence
    - Historique des sessions précédentes (comparatif)
    """

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._sessions = []    # historique des résultats de cette session
        self._max_sessions = 5  # garder les 5 dernières pour comparaison
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        self.set_margin_top(0)

        # ── Bandeau score + progression ──
        self._score_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._score_box.add_css_class("score-box")

        score_row = Gtk.Box(spacing=16)
        score_row.set_halign(Gtk.Align.CENTER)

        self._score_lbl = Gtk.Label(label="—")
        self._score_lbl.add_css_class("score-value")

        self._score_unit_lbl = Gtk.Label(label="")
        self._score_unit_lbl.add_css_class("score-unit")
        self._score_unit_lbl.set_valign(Gtk.Align.END)
        self._score_unit_lbl.set_margin_bottom(8)

        score_row.append(self._score_lbl)
        score_row.append(self._score_unit_lbl)

        self._progress = Gtk.ProgressBar()
        self._progress.set_fraction(0)
        self._progress.set_show_text(True)
        self._progress.set_text("En attente")

        self._score_box.append(score_row)
        self._score_box.append(self._progress)
        self.append(self._score_box)

        # ── Carte disque ──
        self._disk_card = FioDiskCard({}, {})
        self._disk_card.set_size_request(-1, 180)
        self.append(self._disk_card)

        if not HAS_CAIRO:
            warn = Gtk.Label()
            warn.set_markup('<span foreground="#f9e2af">⚠  cairo non disponible — graphiques désactivés\n   sudo apt install python3-gi-cairo</span>')
            warn.set_halign(Gtk.Align.START)
            self.append(warn)
            # Fallback métriques texte
            self._metrics_box = Gtk.FlowBox()
            self._metrics_box.set_max_children_per_line(4)
            self._metrics_box.set_selection_mode(Gtk.SelectionMode.NONE)
            self._metrics_box.set_column_spacing(10); self._metrics_box.set_row_spacing(10)
            self.append(self._metrics_box)
            return

        # ── Graphique Temps Réel / Température & Puissance SSD ──
        live_title = Gtk.Label()
        live_title.set_markup('<span weight="bold" size="small">TELEMETRIE EN DIRECT / THERMIQUE DISQUE</span>')
        live_title.set_halign(Gtk.Align.START)
        live_title.add_css_class("params-title")
        self.append(live_title)

        self._live_chart = FioLiveChart(min_height=200)
        self.append(self._live_chart)

        # ── Titre graphiques ──
        charts_title = Gtk.Label()
        charts_title.set_markup('<span weight="bold" size="small">GRAPHIQUES DE PERFORMANCE</span>')
        charts_title.set_halign(Gtk.Align.START)
        charts_title.add_css_class("params-title")
        self.append(charts_title)

        # ── Graphique 1 : Débit MiB/s ──
        self._chart_bw = FioBarChart(
            "Débit séquentiel (MiB/s)", "MiB/s",
            ["Lecture", "Écriture"], [],
            min_height=230)
        self.append(self._chart_bw)

        # ── Graphique 2 : IOPS ──
        self._chart_iops = FioBarChart(
            "IOPS", "IOPS",
            ["Lect. IOPS", "Écr. IOPS"], [],
            min_height=200)
        self.append(self._chart_iops)

        # ── Graphique 3 : Latence ──
        self._chart_lat = FioBarChart(
            "Latence (ms) — Moy · P99 · P999", "ms",
            ["Lect. moy", "Écr. moy", "Lect. P99", "Écr. P99", "Lect. P999", "Écr. P999"],
            [], min_height=200)
        self._chart_lat._bar_colors = [C_READ, C_WRITE, C_IOPS_R, C_IOPS_W, C_LAT, C_LAT2]
        self.append(self._chart_lat)

        # ── Historique comparatif ──
        hist_title = Gtk.Label()
        hist_title.set_markup('<span weight="bold" size="small">SESSIONS PRECEDENTES (comparatif)</span>')
        hist_title.set_halign(Gtk.Align.START)
        hist_title.add_css_class("params-title")
        hist_title.set_margin_top(8)
        self.append(hist_title)

        self._hist_lbl = Gtk.Label()
        self._hist_lbl.set_markup('<span foreground="#6c7086" size="small">Aucune session précédente</span>')
        self._hist_lbl.set_halign(Gtk.Align.START)
        self.append(self._hist_lbl)

        btn_clear = Gtk.Button(label="Effacer l'historique des graphiques")
        btn_clear.connect("clicked", lambda _: self._clear_sessions())
        self.append(btn_clear)

    # ── Télémétrie en direct ──────────────────────────────────────────────────

    def reset_live(self):
        if hasattr(self, "_live_chart"):
            self._live_chart.reset()

    def push_live(self, point: dict):
        if hasattr(self, "_live_chart"):
            self._live_chart.push(point)

    def set_live_series(self, series: dict):
        if hasattr(self, "_live_chart"):
            self._live_chart.set_series(series)

    # ── Mise à jour ───────────────────────────────────────────────────────────

    def set_progress(self, pct: float, msg: str):
        self._progress.set_fraction(pct / 100)
        self._progress.set_text(msg)

    def set_error(self, msg: str):
        self._score_lbl.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
        self._score_unit_lbl.set_markup(f'<span foreground="#f38ba8" size="small">{GLib.markup_escape_text(str(msg)[:80])}</span>')
        self._progress.set_fraction(0)
        self._progress.set_text("Erreur")

    def set_result(self, result_dict: dict, disk_info: dict, test_params: dict):
        """Appeler quand un test FIO se termine."""
        score  = result_dict.get("score", 0)
        engine = result_dict.get("engine_used", test_params.get("io_engine", "?"))
        m      = result_dict.get("metrics", {})

        s = result_dict.get("series") or m.get("series") or ({"t": m.get("series_t", []), "disk_temp": m.get("series_temp", []), "disk_power": m.get("series_power", [])} if m.get("series_t") else None)
        if s:
            self.set_live_series(s)

        # Score
        self._score_lbl.set_markup(
            f'<span foreground="#e879f9" weight="ultrabold">{score:.1f}</span>')
        self._score_unit_lbl.set_markup(
            f'<span foreground="#6c7086">MiB/s  ·  {engine}</span>')
        self._progress.set_fraction(1.0)
        self._progress.set_text("Terminé")

        # Carte disque
        self._disk_card.update(disk_info, test_params)

        if not HAS_CAIRO:
            self._populate_fallback(m)
            return

        # Ajouter à l'historique des sessions
        session = {
            "label":       _session_label(disk_info, test_params),
            "score":       score,
            "metrics":     m,
            "disk_info":   disk_info,
            "test_params": test_params,
            "timestamp":   datetime.now().strftime("%H:%M:%S"),
        }
        self._sessions.append(session)
        if len(self._sessions) > self._max_sessions:
            self._sessions = self._sessions[-self._max_sessions:]

        self._rebuild_charts()
        self._rebuild_history_label()

    def _rebuild_charts(self):
        """Reconstruit les 3 graphiques avec toutes les sessions."""
        sessions = self._sessions
        if not sessions:
            return

        bw_groups   = []
        iops_groups = []
        lat_groups  = []

        for s in sessions:
            m = s["metrics"]
            lbl = s["label"]

            def _f(k1, k2="", d=0.0):
                try: return float(m.get(k1, m.get(k2, d)) or d)
                except: return d

            bw_groups.append({
                "label":  lbl,
                "values": [_f("lecture_MiB_s","read_MiB_s"),
                           _f("ecriture_MiB_s","write_MiB_s")],
                "colors": [C_READ, C_WRITE],
            })
            iops_groups.append({
                "label":  lbl,
                "values": [_f("lecture_IOPS","read_IOPS"),
                           _f("ecriture_IOPS","write_IOPS")],
                "colors": [C_IOPS_R, C_IOPS_W],
            })

            r_moy  = _f("lat_lect_moy_ms","read_lat_ms")
            w_moy  = _f("lat_ecr_moy_ms","write_lat_ms")
            r_p99  = _f("lat_lect_p99_ms") or r_moy * 1.5
            w_p99  = _f("lat_ecr_p99_ms")  or w_moy * 1.5
            r_p999 = _f("lat_lect_p999_ms") or r_p99 * 1.3
            w_p999 = _f("lat_ecr_p999_ms")  or w_p99 * 1.3
            lat_groups.append({
                "label":  lbl,
                "values": [r_moy, w_moy, r_p99, w_p99, r_p999, w_p999],
                "colors": [C_READ, C_WRITE, C_IOPS_R, C_IOPS_W, C_LAT, C_LAT2],
            })

        self._chart_bw.update(bw_groups)
        self._chart_iops.update(iops_groups)
        self._chart_lat.update(lat_groups)

    def _rebuild_history_label(self):
        n = len(self._sessions)
        if n <= 1:
            self._hist_lbl.set_markup(
                '<span foreground="#6c7086" size="small">Lance un autre test pour voir la comparaison</span>')
            return
        lines = []
        for i, s in enumerate(self._sessions):
            m = s["metrics"]
            def _f(k1, k2=""):
                try: return float(m.get(k1, m.get(k2, 0)) or 0)
                except: return 0.0
            r_bw = _f("lecture_MiB_s","read_MiB_s")
            w_bw = _f("ecriture_MiB_s","write_MiB_s")
            riops = _f("lecture_IOPS","read_IOPS")
            wiops = _f("ecriture_IOPS","write_IOPS")
            lines.append(
                f"<b>{GLib.markup_escape_text(str(s['timestamp']))}</b>  {GLib.markup_escape_text(str(s['label']))}  —  "
                f"R:{r_bw:.0f} W:{w_bw:.0f} MiB/s  |  "
                f"R:{riops:.0f} W:{wiops:.0f} IOPS")
        self._hist_lbl.set_markup(
            '<span foreground="#a6adc8" size="small">' +
            '\n'.join(lines) + '</span>')

    def _clear_sessions(self):
        self._sessions = []
        if HAS_CAIRO:
            self._chart_bw.update([])
            self._chart_iops.update([])
            self._chart_lat.update([])
            self._rebuild_history_label()
        self._progress.set_fraction(0)
        self._progress.set_text("En attente")
        self._score_lbl.set_text("—")
        self._score_unit_lbl.set_text("")

    def _populate_fallback(self, metrics: dict):
        """Fallback sans Cairo : cartes texte."""
        if not hasattr(self, "_metrics_box"):
            return
        c = self._metrics_box.get_first_child()
        while c:
            n = c.get_next_sibling(); self._metrics_box.remove(c); c = n
        for k, v in metrics.items():
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(150, -1)
            lbl = Gtk.Label(label=k.replace("_", " ").upper())
            lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START)
            val = Gtk.Label(label=str(v))
            val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _roundrect(ctx, x, y, w, h, r):
    """Trace un rectangle arrondi dans Cairo."""
    if h <= 0: h = 0.1
    if w <= 0: w = 0.1
    r = min(r, w / 2, h / 2)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r,     r,  -math.pi/2,  0)
    ctx.arc(x + w - r, y + h - r, r,  0,            math.pi/2)
    ctx.arc(x + r,     y + h - r, r,  math.pi/2,    math.pi)
    ctx.arc(x + r,     y + r,     r,  math.pi,      3*math.pi/2)
    ctx.close_path()


def _fmt_val(val: float, unit: str) -> str:
    """Formate une valeur selon son unité."""
    if unit in ("Mo/s", "MiB/s", "MB/s"):
        # Les NVMe peuvent dépasser 7000 Mo/s — pas d'abréviation K
        if val >= 1:
            return f"{val:.1f}"
        return f"{val:.3f}"
    if unit == "ms":
        if val >= 1:
            return f"{val:.1f}"
        return f"{val:.3f}"
    if unit == "IOPS":
        if val >= 1_000_000:
            return f"{val/1_000_000:.1f}M"
        if val >= 1000:
            return f"{val/1000:.0f}K"
        return f"{val:.0f}"
    # Générique
    if val >= 1000:
        return f"{val/1000:.1f}K"
    return f"{val:.1f}"


def _session_label(disk_info: dict, test_params: dict) -> str:
    """Génère un label court pour identifier une session."""
    model = disk_info.get("model","") or disk_info.get("name","") or disk_info.get("dev","?")
    model = model[:12]
    mode  = test_params.get("rw_mode", "?")
    bs    = test_params.get("bs", "?")
    return f"{model}  {mode}/{bs}"
