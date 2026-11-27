#!/usr/bin/env python3
"""
BenchSuite — Architecture GTK4
- Fenêtre native déplaçable avec boutons fermer/réduire/agrandir (Adw.HeaderBar)
- Sidebar par benchmark individuel groupé par catégorie
- Paramètres adaptés à chaque benchmark
"""

import sys
import os
import threading
import time
import inspect
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Gdk, Gio, GObject, Pango

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.benchmarks import (CATALOG, CAT_COLORS, CAT_ORDER,
                               BenchmarkResult, get_system_info, tool_exists,
                               get_ram_total_mb, set_custom_tool_path)
from core import theme
from core.safety import nvidia_smi_usable
from exporters.export import (export_html, export_all, save_to_history, load_history, clear_history,
                               delete_history_indices, history_result, save_report_meta,
                               load_report_meta)

APP_ID     = "io.benchsuite.app"
APP_NAME   = "BenchSuite"
VERSION    = "1.0.29"
OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")

_VERSION_DISPLAY: list = []


def get_full_version_display():
    """Version affichée, avec le commit git court si le logiciel tourne depuis un dépôt (mémorisée : un seul appel à git)."""
    if not _VERSION_DISPLAY:
        _VERSION_DISPLAY.append(_full_version_display())
    return _VERSION_DISPLAY[0]


def _full_version_display():
    v = VERSION
    try:
        import subprocess
        root = os.path.dirname(os.path.abspath(__file__))
        h = subprocess.check_output(["git", "-C", root, "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, timeout=3).decode().strip()
        if h:
            v = f"{VERSION} [{h}]"
    except Exception:
        pass
    return v

# ─────────────────────────────────────────────────────────────────────────────
# CSS
# ─────────────────────────────────────────────────────────────────────────────

CSS = """
window { background-color: #0d0d0d; color: #f0f0f0; }

headerbar {
    background-color: #141414;
    border-bottom: 1px solid #2a2a2a;
    color: #f0f0f0;
}

.sidebar {
    background-color: #141414;
    border-right: 1px solid #2a2a2a;
}
.sidebar-section-label {
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.5px;
    color: #666666;
    padding: 12px 14px 4px 14px;
}
.sidebar-item {
    background: transparent;
    color: #a0a0a0;
    border: none;
    border-radius: 8px;
    padding: 9px 14px;
    font-size: 13px;
    font-weight: 500;
    margin: 1px 6px;
}
.sidebar-item:hover {
    background-color: #222222;
    color: #ffffff;
}
.sidebar-item.active {
    background-color: rgba(255, 102, 0, 0.15);
    color: #ff6600;
    border-left: 3px solid #ff6600;
    font-weight: 700;
}

.bench-title { font-size: 20px; font-weight: 800; color: #ffffff; }
.bench-subtitle { font-size: 12px; color: #888888; }
.status-badge { border-radius: 6px; padding: 3px 10px; font-size: 11px; font-weight: 700; }

.score-value { font-size: 42px; font-weight: 900; color: #ff6600; text-shadow: 0 0 15px rgba(255, 102, 0, 0.3); }
.score-unit  { font-size: 16px; color: #888888; font-weight: 500; }
.score-box {
    background-color: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 16px;
    padding: 14px 24px;
    box-shadow: inset 0 0 20px rgba(255, 102, 0, 0.05);
}

.params-panel {
    background-color: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 12px;
    padding: 16px;
}
.params-title { font-size: 10px; font-weight: 800; letter-spacing: 1px; color: #666666; }
.param-label  { color: #a0a0a0; font-size: 13px; }

.btn-run {
    background: linear-gradient(135deg, #ff6600, #e65c00);
    color: #ffffff;
    border: none;
    border-radius: 10px;
    padding: 11px 28px;
    font-size: 14px;
    font-weight: 800;
    box-shadow: 0 4px 15px rgba(255, 102, 0, 0.2);
    transition: all 0.2s ease;
}
.btn-run:hover    { background: linear-gradient(135deg, #ff7b22, #ff6600); box-shadow: 0 6px 20px rgba(255, 102, 0, 0.4); }
.btn-run:disabled { opacity: 0.55; box-shadow: none; }
.btn-stop {
    background-color: #2a1010;
    color: #ff3333;
    border: 1px solid #ff3333;
    border-radius: 10px;
    padding: 11px 20px;
    font-size: 13px;
    font-weight: 700;
}
.btn-stop:hover    { background-color: #3d1515; }
.btn-stop:disabled { opacity: 0.65; background-color: #1f1212; color: #c77373; border-color: #6b2b2b; }

.btn-csv  { background:#1a2a1a; color:#4caf50; border:1px solid #4caf50; border-radius:8px; padding:8px 14px; font-weight:700; font-size:12px; }
.btn-csv:disabled  { opacity: 0.62; color: #79b87c; border-color: #2e5e31; }
.btn-json { background:#1a2433; color:#2196f3; border:1px solid #2196f3; border-radius:8px; padding:8px 14px; font-weight:700; font-size:12px; }
.btn-json:disabled { opacity: 0.62; color: #7bb6eb; border-color: #27537c; }
.btn-pdf  { background:#2a1a1a; color:#f44336; border:1px solid #f44336; border-radius:8px; padding:8px 14px; font-weight:700; font-size:12px; }
.btn-pdf:disabled  { opacity: 0.62; color: #df827b; border-color: #7c302b; }

progressbar > trough { background-color:#222222; border-radius:4px; min-height:7px; }
progressbar > trough > progress {
    background: linear-gradient(90deg, #ff6600, #ff8c00);
    border-radius:4px; min-height:7px;
    box-shadow: 0 0 10px rgba(255, 102, 0, 0.5);
}

.metric-card {
    background-color: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 10px;
    padding: 12px 16px;
}
.home-hero {
    background: linear-gradient(135deg, #1f140f, #141414);
    border: 1px solid #ff6600;
    border-radius: 18px;
    padding: 28px 32px;
}
.home-card {
    background-color: #141414;
    border: 1px solid #2a2a2a;
    border-radius: 14px;
    padding: 18px 20px;
}
.home-pill {
    background-color: #2a1a10;
    color: #ff9966;
    border: 1px solid #ff6600;
    border-radius: 999px;
    padding: 5px 12px;
    font-size: 11px;
    font-weight: 800;
}
.metric-label { color: #888888; font-size: 11px; font-weight: 700; }
.metric-value { color: #f0f0f0; font-size: 14px; font-weight: 700; }

.log-view { background-color:#0a0a0a; color:#4caf50; border:1px solid #2a2a2a; border-radius:8px; padding:8px; font-family:monospace; font-size:12px; }
.log-view text { background-color:#0a0a0a; color:#4caf50; font-family:monospace; font-size:12px; }

columnview > listview > row { border-bottom:1px solid #1a1a1a; }
columnview > listview > row:selected { background-color:#2a1a10; border-left: 2px solid #ff6600; }
columnview > header > button { background-color:#141414; color:#888888; border:none; border-right:1px solid #2a2a2a; font-weight:700; font-size:11px; }

.install-card { background-color:#141414; border:1px solid #2a2a2a; border-radius:10px; padding:14px; }
.install-cmd  { background-color:#0a0a0a; color:#ff6600; font-family:monospace; font-size:12px; border:1px solid #2a2a2a; border-radius:6px; padding:6px 10px; }

.statusbar { background-color:#141414; border-top:1px solid #2a2a2a; padding:5px 14px; font-size:11px; color:#888888; }
.history-info { color:#888888; font-size:12px; }

scrollbar > trough { background-color:#0d0d0d; }
scrollbar > trough > slider { background-color:#333333; border-radius:4px; min-width:6px; min-height:6px; }
scrollbar > trough > slider:hover { background-color:#555555; }

/* ── Cohérence du thème sombre : onglets, listes, popovers, boutons génériques ── */
notebook > header { background-color:#141414; border-bottom:1px solid #2a2a2a; }
notebook > header > tabs > tab { color:#a0a0a0; padding:8px 16px; font-weight:600; }
notebook > header > tabs > tab:hover { color:#ffffff; }
notebook > header > tabs > tab:checked { color:#ff6600; box-shadow: inset 0 -2px #ff6600; }
notebook > stack { background-color:#0d0d0d; }
popover > contents { background-color:#161616; border:1px solid #2a2a2a; border-radius:10px; color:#f0f0f0; }
modelbutton:hover { background-color:#262626; }
list, listview { background-color:transparent; color:#f0f0f0; }
button { background-color:#1c1c1c; color:#e8e8e8; border:1px solid #2a2a2a; border-radius:8px; }
button:hover { background-color:#262626; }
button:disabled { color:#666666; }
button.toggle:checked, togglebutton:checked { background-color:#2a1a10; color:#ff9966; border-color:#ff6600; }
linkbutton > button, button.link { background:transparent; border:none; color:#7aa2f7; padding:2px 6px; box-shadow:none; }
linkbutton > button:hover { color:#9ec1ff; background:transparent; }
dropdown > button { background-color:#0a0a0a; border:1px solid #2a2a2a; }
expander title { color:#cfcfcf; font-weight:600; }
scrolledwindow, viewport { background-color:transparent; }

/* ── Menu latéral ── */
.sidebar-search { margin:10px 10px 4px 10px; }
.sidebar-group { background:transparent; border:none; border-radius:6px; padding:6px 12px 2px 12px; margin:6px 6px 0 6px;
                 color:#8c8c8c; font-size:10px; font-weight:800; letter-spacing:1.2px; box-shadow:none; }
.sidebar-group:hover { color:#ffffff; background:transparent; }
.sidebar-item .nav-icon { font-size:15px; min-width:22px; }
.nav-count { color:#6c7086; font-size:10px; font-weight:bold; }
.sidebar-subgroup { background:transparent; border:none; border-radius:6px; padding:3px 12px 1px 22px; margin:4px 6px 0 6px;
                    color:#9a9a9a; font-size:11px; font-weight:bold; min-height:0; }
.sidebar-subgroup:hover { color:#ffffff; background:transparent; }
.nav-sep { margin:4px 14px; background:#2a2a2a; min-height:1px; }
.sidebar-item.nav-primary .nav-text { color:#ff6600; font-weight:bold; }
.sidebar-item .nav-text { font-size:13px; }
.sidebar-item .nav-warn { color:#f9e2af; font-size:12px; }
.sidebar-item.missing .nav-text { color:#6a6a6a; }

/* ── Page de test : en-tête fixe ── */
.page-header { background-color:#101010; border-bottom:1px solid #2a2a2a; padding:16px 28px 14px 28px; }
.warn-card { background-color:#2a2210; border:1px solid #6b5a1e; border-radius:10px; padding:10px 14px; }
.hint-tt { background-color:#0a0a0a; color:#ff9966; font-family:monospace; font-size:12px; border:1px solid #2a2a2a; border-radius:6px; padding:5px 8px; }
.home-test { background:transparent; border:none; border-radius:6px; padding:3px 8px; box-shadow:none; }
.home-test:hover { background-color:#222222; }
.btn-primary-big { background:linear-gradient(135deg,#ff6600,#e65c00); color:#fff; border:none; border-radius:10px; padding:11px 22px;
                   font-size:14px; font-weight:800; box-shadow:0 4px 15px rgba(255,102,0,0.25); }
.btn-primary-big:hover { background:linear-gradient(135deg,#ff7b22,#ff6600); }
.btn-soft { background-color:#1c1c1c; color:#f0f0f0; border:1px solid #333333; border-radius:10px; padding:11px 18px; font-size:13px; font-weight:700; }
.btn-soft:hover { border-color:#ff6600; color:#ff9966; }
.score-box { padding:14px 24px; }
.score-value { font-size:34px; }

spinbutton, entry { background-color:#0a0a0a; color:#f0f0f0; border:1px solid #2a2a2a; border-radius:6px; padding:4px 8px; font-size:13px; }
spinbutton:focus, entry:focus { border-color:#ff6600; }
separator { background-color:#2a2a2a; }

/* Retour visuel au clic : appui immédiat + sélection persistante (profils, onglets, cartes cliquables) */
button:active, .sidebar-item:active { background-color: rgba(255, 102, 0, 0.30); color: #ffffff; }
button:focus-visible { outline: 2px solid rgba(255, 102, 0, 0.55); outline-offset: -2px; }
.profile-selected, .profile-selected:hover {
    background-color: rgba(255, 102, 0, 0.16);
    color: #ff8c33;
    border-left: 3px solid #ff6600;
    font-weight: 700;
}
.profile-selected label { color: #ff8c33; }
list row:selected, listbox row:selected { background-color: rgba(255, 102, 0, 0.16); border-left: 3px solid #ff6600; }

/* Petits écrans (< 1366x800) : fenêtre avec classe « compact » */
.compact .bench-title { font-size: 16px; }
.compact .score-value { font-size: 30px; }
.compact .score-box { padding: 8px 14px; }
.compact .params-panel { padding: 10px; }
.compact .home-hero { padding: 16px 18px; }
.compact .home-card { padding: 12px 14px; }
.compact .metric-card { padding: 8px 10px; }
.compact .btn-run { padding: 8px 16px; font-size: 13px; }
.compact .btn-stop { padding: 8px 12px; }
.compact .sidebar-item { padding: 6px 10px; }
.compact .sidebar-item .nav-text { font-size: 12px; }
.compact .statusbar { padding: 3px 10px; }
"""

# ─────────────────────────────────────────────────────────────────────────────
# Widget live — courbes GPU (temp / power / freq) en Cairo
# ─────────────────────────────────────────────────────────────────────────────

try:
    import cairo as _cairo  # noqa: F401  (test de disponibilité)
    gi.require_foreign("cairo")   # exige python3-gi-cairo : sinon « foreign struct converter for cairo.Context » à chaque dessin
    _HAS_CAIRO = True
except Exception:
    _HAS_CAIRO = False


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


# Couleurs assignées automatiquement aux séries GPU (GPU0 à GPU7+)
_GPU_PALETTE = [
    _hex_rgb("ff6600"),  # GPU0 (Orange signature)
    _hex_rgb("3b82f6"),  # GPU1 (Bleu)
    _hex_rgb("10b981"),  # GPU2 (Vert)
    _hex_rgb("a855f7"),  # GPU3 (Violet)
    _hex_rgb("f59e0b"),  # GPU4 (Ambre)
    _hex_rgb("ec4899"),  # GPU5 (Rose)
    _hex_rgb("06b6d4"),  # GPU6 (Cyan)
    _hex_rgb("f43f5e"),  # GPU7 (Corail)
]


def _gpu_dyn_color(gpu_idx: int, metric: str):
    """Retourne la couleur RGB pour un GPU et une métrique donnée."""
    base = _GPU_PALETTE[gpu_idx % len(_GPU_PALETTE)]
    r, g, b = base
    if metric == "temp_hotspot":
        return (min(1.0, r * 1.15), min(1.0, g * 0.85), min(1.0, b * 0.85))
    elif metric == "temp_vram":
        return (min(1.0, r * 0.85), min(1.0, g * 1.1), min(1.0, b * 1.1))
    elif metric == "power":
        return _hex_rgb("f97316")
    elif "freq" in metric:
        return _hex_rgb("22d3ee")
    return (r, g, b)


class LiveThermalChart(Gtk.DrawingArea):
    """Graphique en temps réel générique — temp / freq / load ou power."""

    # Définitions de séries par benchmark (GPU 0 uniquement pour la liste statique) :
    SERIES_GPU = [
        ("gpu0_temp",          "00 Cœur °C",    _hex_rgb("ff6600")),
        ("gpu0_temp_hotspot",  "00 Hotspot °C", _hex_rgb("ff8c00")),
        ("gpu0_temp_vram",     "00 VRAM °C",    _hex_rgb("ffb366")),
        ("gpu0_power",         "00 Power W",    _hex_rgb("f97316")),
        ("gpu0_freq_graphics", "00 MHz",        _hex_rgb("22d3ee")),
    ]
    SERIES_CPU = [
        ("temp",    "Temp °C",  _hex_rgb("f38ba8")),
        ("freq",    "CPU MHz",  _hex_rgb("22d3ee")),
        ("cpu_pct", "Load %",   _hex_rgb("a6e3a1")),
    ]
    SERIES_RAM = [
        ("ram_used_gb",    "RAM utilisée Go", _hex_rgb("a6e3a1")),
        ("ram_pct",        "RAM %",           _hex_rgb("f9e2af")),
        ("ram_write_mibs", "Écriture MiB/s", _hex_rgb("22d3ee")),
        ("ram_read_mibs",  "Lecture MiB/s",  _hex_rgb("89b4fa")),
        ("ram_errors",     "Erreurs",         _hex_rgb("f38ba8")),
        ("ram_temp",       "RAM Temp °C",     _hex_rgb("fab387")),
        ("ram_power",      "RAM Power W",     _hex_rgb("cba6f7")),
    ]

    def __init__(self, series_defs: list, title: str = ""):
        super().__init__()
        self._title         = title
        self._static_defs   = list(series_defs)   # référence immuable
        self._series_def    = list(series_defs)   # copie mutable (séries dyn ajoutées ici)
        self._pts           = {"t": []}
        for key, _, _ in series_defs:
            self._pts[key] = []
        self.set_size_request(-1, 170)
        self.set_hexpand(True)
        if _HAS_CAIRO:   # sans python3-gi-cairo, GTK lèverait « foreign struct converter for cairo.Context » à chaque dessin
            self.set_draw_func(self._draw_func)

    def reset(self):
        self._series_def = list(self._static_defs)
        self._pts = {"t": []}
        for key, _, _ in self._series_def:
            self._pts[key] = []
        self.queue_draw()

    def push(self, point: dict):
        import re as _re
        self._pts["t"].append(point.get("t", 0))
        n_t = len(self._pts["t"])
        # Séries connues : valeur du point, sinon « trou » (None) — chaque série reste alignée sur l'axe du temps. Avant, une
        # série absente d'un point devenait plus courte que « t » et sa courbe était tracée décalée vers le début.
        for key, _, _ in self._series_def:
            lst = self._pts.setdefault(key, [])
            if len(lst) < n_t - 1:
                lst.extend([None] * (n_t - 1 - len(lst)))
            lst.append(point.get(key))
        # Séries GPU dynamiques (gpu0_*, gpu1_*, gpu2_*, …)
        for k, v in point.items():
            if k in self._pts or k == "t":
                continue
            m = _re.match(r"^gpu(\d+)_(temp|temp_hotspot|temp_vram|power|freq_graphics|freq_memory)$", k)
            if not m:
                continue
            gi   = int(m.group(1))
            mkey = m.group(2)
            name_map = {
                "temp": "Cœur °C",
                "temp_hotspot": "Hotspot °C",
                "temp_vram": "VRAM °C",
                "power": "Power W",
                "freq_graphics": "MHz",
                "freq_memory": "VRAM MHz",
            }
            lbl  = f"{gi:02d} {name_map.get(mkey, mkey)}"
            col  = _gpu_dyn_color(gi, mkey)
            self._series_def.append((k, lbl, col))
            self._pts[k] = [None] * (n_t - 1) + [v]      # capteur apparu en cours de test : aligné sur « t »
        self.queue_draw()
        return False   # compatible GLib.idle_add

    def get_series(self):
        return self._pts

    def _range_for_key(self, key, vals):
        vals = [float(v) for v in vals if v is not None]
        if not vals:
            return 0.0, 1.0
        k = key.lower()
        vmax_data = max(vals)
        vmin_data = min(vals)
        if "pct" in k or "load" in k or "cpu_pct" in k:
            return 0.0, 100.0
        if "temp" in k:
            return 0.0, max(100.0, ((vmax_data + 19) // 20) * 20)
        if "power" in k:
            return 0.0, max(50.0, ((vmax_data + 49) // 50) * 50)
        if "freq" in k or "mhz" in k:
            return 0.0, max(1000.0, ((vmax_data + 499) // 500) * 500)
        if "mibs" in k:
            return 0.0, max(100.0, ((vmax_data + 499) // 500) * 500)
        if "errors" in k:
            return 0.0, max(1.0, vmax_data)
        if "used_gb" in k:
            return 0.0, max(1.0, ((vmax_data + 7) // 8) * 8)
        if vmax_data == vmin_data:
            return 0.0, vmax_data + 1.0
        pad = (vmax_data - vmin_data) * 0.12
        return vmin_data - pad, vmax_data + pad

    def _draw_func(self, _area, cr, w, h):
        if not _HAS_CAIRO:
            return
        cr.set_source_rgb(*_hex_rgb("1e1e2e")); cr.paint()
        pts = self._pts
        t_vals = pts.get("t", [])

        cr.select_font_face("Sans", 0, 0)
        cr.set_source_rgb(*_hex_rgb("cdd6f4"))
        cr.set_font_size(11)
        cr.move_to(10, 16); cr.show_text(self._title)

        if len(t_vals) < 2:
            cr.set_source_rgb(*_hex_rgb("45475a")); cr.set_font_size(11)
            cr.move_to(max(10, w / 2 - 75), h / 2 + 4)
            cr.show_text("En attente de données…")
            return

        ml, mr, mt, mb = 54, 18, 26, 42
        pw, ph = max(1, w - ml - mr), max(1, h - mt - mb)
        t_min, t_max = t_vals[0], max(t_vals[-1], t_vals[0] + 1)

        # Cadre et grille
        cr.set_source_rgba(*_hex_rgb("313244"), 0.85); cr.set_line_width(1)
        cr.rectangle(ml, mt, pw, ph); cr.stroke()
        cr.set_source_rgba(*_hex_rgb("45475a"), 0.45); cr.set_line_width(0.5)
        for i in range(1, 4):
            y = mt + ph * i / 4
            cr.move_to(ml, y); cr.line_to(w - mr, y); cr.stroke()
        for i in range(1, 5):
            x = ml + pw * i / 5
            cr.move_to(x, mt); cr.line_to(x, mt + ph); cr.stroke()

        visible = []
        for key, lbl, col in self._series_def:
            vals = pts.get(key, [])
            clean = [v for v in vals if v is not None]
            if not clean:
                continue
            # Ne trace pas les capteurs indisponibles à zéro permanent sauf erreurs.
            if key not in ("ram_errors",) and all(float(v) == 0 for v in clean):
                continue
            visible.append((key, lbl, col, vals))

        for key, lbl, col, vals in visible:
            n = min(len(t_vals), len(vals))
            if n < 2:
                continue
            # Tout le test est tracé (axe du temps depuis le début), échantillonné à ~600 points. Avant : seuls les 300 derniers
            # points étaient tracés sur un axe partant de t=0 — sur un test long, la courbe se tassait à droite.
            step = max(1, -(-n // 600))
            idx = list(range(0, n, step))
            if idx[-1] != n - 1:
                idx.append(n - 1)
            vmin, vmax = self._range_for_key(key, [vals[i] for i in idx])
            if vmax <= vmin:
                vmax = vmin + 1
            cr.set_source_rgb(*col); cr.set_line_width(2.0)
            first = True
            for t, v in ((t_vals[i], vals[i]) for i in idx):
                try: fv = float(v)
                except Exception: continue
                x = ml + pw * (t - t_min) / (t_max - t_min)
                y = mt + ph * (1.0 - (fv - vmin) / (vmax - vmin))
                y = max(mt, min(mt + ph, y))
                if first:
                    cr.move_to(x, y); first = False
                else:
                    cr.line_to(x, y)
            cr.stroke()
            try:
                lx = ml + pw * (t_vals[n - 1] - t_min) / (t_max - t_min)
                ly = mt + ph * (1.0 - (float(vals[n - 1]) - vmin) / (vmax - vmin))
                ly = max(mt, min(mt + ph, ly))
                cr.arc(lx, ly, 3.3, 0, 6.2832); cr.fill()
            except Exception:
                pass

        # Axes temps
        cr.set_source_rgb(*_hex_rgb("6c7086")); cr.set_font_size(9)
        cr.move_to(ml, h - 8); cr.show_text("0s")
        lbl = f"{int(t_vals[-1])}s"
        cr.move_to(w - mr - max(24, len(lbl) * 5), h - 8); cr.show_text(lbl)

        # Légende compacte, retour à la ligne automatique
        lx, ly = ml, h - mb + 12
        cr.set_font_size(9)
        for key, lbl, col, vals in visible:
            last = next((v for v in reversed(vals) if v is not None), None)     # dernière valeur connue (None = trou)
            if last is None:
                continue
            text = f"{lbl}: {float(last):.0f}"
            est_w = 14 + len(text) * 5.4
            if lx + est_w > w - mr:
                lx = ml; ly += 13
            cr.set_source_rgb(*col); cr.rectangle(lx, ly - 8, 8, 7); cr.fill()
            cr.set_source_rgb(*_hex_rgb("cdd6f4")); cr.move_to(lx + 11, ly)
            cr.show_text(text[:34])
            lx += est_w + 12



# ─────────────────────────────────────────────────────────────────────────────
# GObject wrapper
# ─────────────────────────────────────────────────────────────────────────────

class ResultWrapper(GObject.Object):
    __gtype_name__ = "ResultWrapper"
    result = GObject.Property(type=GObject.TYPE_PYOBJECT)
    hist_index = GObject.Property(type=int, default=-1)      # position dans ~/.benchsuite_history.json (page Historique)
    def __init__(self, result):
        super().__init__()
        self.result = result


# ─────────────────────────────────────────────────────────────────────────────
# Worker thread — un benchmark à la fois
# ─────────────────────────────────────────────────────────────────────────────

class BenchWorker(threading.Thread):
    def __init__(self, bench_def, params, on_progress, on_done):
        super().__init__(daemon=True)
        self.bench_def   = bench_def
        self.params      = params
        self.on_progress = on_progress
        self.on_done     = on_done
        self._stop_event = threading.Event()

    def run(self):
        def cb(pct, msg):
            if not self._stop_event.is_set():
                GLib.idle_add(self.on_progress, pct, msg)
        try:
            params = dict(self.params)
            try:
                sig = inspect.signature(self.bench_def["fn"])
                if "kill_event" in sig.parameters:
                    params["kill_event"] = self._stop_event
            except Exception:
                pass
            result = self.bench_def["fn"](cb=cb, **params)
            if isinstance(result, dict):
                score = result.get("score")
                unit = result.get("unit", "")
                dur = result.get("duration_s", 0.0)
                status = result.get("status", "ok")
                metrics = {k: v for k, v in result.items() if k not in ("score", "unit", "duration_s", "status")}
                result = BenchmarkResult(
                    self.bench_def["name"], self.bench_def["category"],
                    score, unit, dur, "", metrics=metrics, status=status
                )
            if isinstance(result, BenchmarkResult):
                clean_params = {k: v for k, v in self.params.items() if k not in ("series_cb", "kill_event", "cb")}
                for pk, pv in clean_params.items():
                    if pk not in result.metrics:
                        result.metrics[pk] = pv
                if not getattr(result, "command", "") and "command" in result.metrics:
                    result.command = str(result.metrics["command"])
        except Exception as e:
            result = BenchmarkResult(
                self.bench_def["name"], self.bench_def["category"],
                None, "", 0, str(e), status="error")
        GLib.idle_add(self.on_done, result)

    def stop(self):
        self._stop_event.set()


# ─────────────────────────────────────────────────────────────────────────────
# Page individuelle par benchmark
# ─────────────────────────────────────────────────────────────────────────────

class MultiCheck(Gtk.Box):
    """Liste de cases à cocher (sélection multiple) : évite de saisir des valeurs à la main.

    `items` = [(valeur, libellé)]. `get_bench_value()` retourne les valeurs cochées séparées par des
    virgules (chaîne vide = aucune case cochée = « tout », selon le test).
    """

    def __init__(self, items, columns: int = 2):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._checks = []
        fb = Gtk.FlowBox()
        fb.set_selection_mode(Gtk.SelectionMode.NONE)
        fb.set_max_children_per_line(columns)
        fb.set_min_children_per_line(1)
        fb.set_column_spacing(14)
        fb.set_row_spacing(2)
        for value, label in items:
            cb = Gtk.CheckButton(label=label)
            fb.append(cb)
            self._checks.append((value, cb))
        self.append(fb)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for text, state in (("Tout cocher", True), ("Tout décocher", False)):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, st=state: [cb.set_active(st) for _v, cb in self._checks])
            bar.append(b)
        self.append(bar)

    def get_bench_value(self) -> str:
        return ",".join(str(v) for v, cb in self._checks if cb.get_active())


# Liste des GPU NVIDIA [(index, libellé)] : détectée UNE fois, dans un fil, puis distribuée aux pages qui l'attendent.
# Avant, chacune des 9 pages GPU lançait son propre nvidia-smi dans le fil de l'interface à la construction de la fenêtre
# (≈ 0,5 s chacune, jusqu'à 4 s si un GPU ne répond plus).
_NVIDIA_GPU_ITEMS = {"items": None, "waiters": [], "started": False}


def nvidia_gpu_items_async(callback) -> None:
    """Appelle `callback(items)` dans le fil GTK dès que la liste des GPU NVIDIA est connue (tout de suite si elle l'est déjà)."""
    st = _NVIDIA_GPU_ITEMS
    if st["items"] is not None:
        callback(st["items"])
        return
    st["waiters"].append(callback)
    if st["started"]:
        return
    st["started"] = True

    def work():
        items = []
        try:
            import subprocess
            if nvidia_smi_usable():
                out = subprocess.check_output(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                                              text=True, stderr=subprocess.DEVNULL, timeout=6)
                for line in out.splitlines():
                    idx, _, name = line.partition(",")
                    if idx.strip().isdigit():
                        items.append((idx.strip(), f"GPU {idx.strip()} — {name.strip()}"))
        except Exception:
            pass

        def deliver():
            st["items"] = items
            waiters, st["waiters"] = st["waiters"], []
            for cb in waiters:
                try:
                    cb(items)
                except Exception:
                    pass
            return False
        GLib.idle_add(deliver)
    threading.Thread(target=work, daemon=True).start()


class GpuTargetChooser(MultiCheck):
    """Choix des GPU à tester : cases à cocher dès que la liste des GPU NVIDIA est connue (détection en arrière-plan) ;
    sans nvidia-smi (GPU AMD / Intel) ou en attendant, un champ d'index « 0,1 ». Aucun choix = tous les GPU."""

    def __init__(self):
        Gtk.Box.__init__(self, orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._checks = []
        self._entry = Gtk.Entry()
        self._entry.set_size_request(160, -1)
        self._entry.set_placeholder_text("Index des GPU, ex : 0,1 (vide = tous)")
        self.append(self._entry)
        nvidia_gpu_items_async(self._set_items)

    def _set_items(self, items):
        if not items:
            return                      # pas de GPU NVIDIA : le champ d'index reste
        typed = {x.strip() for x in self._entry.get_text().split(",") if x.strip()}
        self.remove(self._entry)
        fb = Gtk.FlowBox()
        fb.set_selection_mode(Gtk.SelectionMode.NONE)
        fb.set_max_children_per_line(2 if len(items) > 2 else 1)
        fb.set_min_children_per_line(1)
        fb.set_column_spacing(14)
        fb.set_row_spacing(2)
        for value, label in items:
            cb = Gtk.CheckButton(label=label)
            cb.set_active(str(value) in typed)
            fb.append(cb)
            self._checks.append((value, cb))
        self.append(fb)
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        for text, state in (("Tout cocher", True), ("Tout décocher", False)):
            b = Gtk.Button(label=text)
            b.add_css_class("flat")
            b.connect("clicked", lambda _b, st=state: [c.set_active(st) for _v, c in self._checks])
            bar.append(b)
        self.append(bar)

    def get_bench_value(self) -> str:
        if self._checks:
            return ",".join(str(v) for v, cb in self._checks if cb.get_active())
        return ",".join(x.strip() for x in self._entry.get_text().split(",") if x.strip())


class BenchPage(Gtk.Box):

    def __init__(self, bench_def: dict, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.bench_def = bench_def
        self.win       = win
        self.result    = None
        self.worker    = None
        self._pw         = {}          # param widgets
        self._missing    = [t for t in bench_def.get("requires", []) if not tool_exists(t)]
        self._live_chart = None        # LiveThermalChart, créé dans _build
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        cat   = self.bench_def["category"]
        color = CAT_COLORS.get(cat, "#888")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        # En-tête FIXE (titre, outil manquant, boutons Lancer / Stopper / Exporter) : les actions restent visibles sans défiler
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header.add_css_class("page-header")

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18); box.set_margin_start(28)
        box.set_margin_end(28); box.set_margin_bottom(24)

        # Titre
        row = Gtk.Box(spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.set_hexpand(True)
        n = Gtk.Label(label=self.bench_def["name"])
        n.add_css_class("bench-title"); n.set_halign(Gtk.Align.START)
        d = Gtk.Label(label=self.bench_def["desc"])
        d.add_css_class("bench-subtitle"); d.set_halign(Gtk.Align.START); d.set_wrap(True)
        col.append(n); col.append(d)
        badge = Gtk.Label()
        badge.set_markup(f'<span foreground="{color}" background="{color}22" weight="bold"> {cat} </span>')
        badge.add_css_class("status-badge"); badge.set_valign(Gtk.Align.START)
        row.append(col); row.append(badge)
        header.append(row)

        # Avertissement outil manquant
        self._warning_box = Gtk.Box(spacing=10)
        self._warning_box.set_orientation(Gtk.Orientation.VERTICAL)
        self._warning_box.add_css_class("warn-card")
        
        warning_header = Gtk.Box(spacing=10)
        wi = Gtk.Label()
        wi.set_markup('<span foreground="#f9e2af" size="large">⚠</span>')
        self._warning_label = Gtk.Label()
        self._warning_label.set_wrap(True); self._warning_label.set_halign(Gtk.Align.START)
        warning_header.append(wi); warning_header.append(self._warning_label)
        self._warning_box.append(warning_header)
        
        self._warning_actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._warning_actions.set_margin_start(4)
        self._warning_box.append(self._warning_actions)
        
        header.append(self._warning_box)

        # Score display
        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sb.add_css_class("score-box")
        stitle = Gtk.Label(label="RESULTAT")
        stitle.add_css_class("params-title"); stitle.set_halign(Gtk.Align.START)
        self._score_val  = Gtk.Label(label="—")
        self._score_val.add_css_class("score-value"); self._score_val.set_halign(Gtk.Align.CENTER)
        self._score_unit = Gtk.Label(label="")
        self._score_unit.add_css_class("score-unit"); self._score_unit.set_halign(Gtk.Align.CENTER)
        self._progress = Gtk.ProgressBar()
        self._progress.set_fraction(0); self._progress.set_show_text(True)
        self._progress.set_text("En attente")
        sb.append(stitle); sb.append(self._score_val)
        sb.append(self._score_unit); sb.append(self._progress)
        box.append(sb)

        # Live chart pour benchmarks thermiques
        _bid = self.bench_def["id"]
        if _bid in ("gpu_fryer", "gpu_burn", "gpu_nvbandwidth", "gpu_vram_bw", "gpu_vram_test", "gpu_dcgm",
                    "gpu_vkpeak", "gpu_clpeak", "gpu_oclburn"):
            self._live_chart = LiveThermalChart(
                LiveThermalChart.SERIES_GPU,
                "Chauffe GPU — Temp · Power · Fréquence (temps réel)")
            box.append(self._live_chart)
        elif _bid == "cpu_stress":
            self._live_chart = LiveThermalChart(
                LiveThermalChart.SERIES_CPU,
                "Charge CPU — Temp · Fréquence · Utilisation (temps réel)")
            box.append(self._live_chart)

        # Métriques
        self._metrics_box = Gtk.FlowBox()
        self._metrics_box.set_max_children_per_line(4)
        self._metrics_box.set_min_children_per_line(2)
        self._metrics_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._metrics_box.set_column_spacing(10)
        self._metrics_box.set_row_spacing(10)
        box.append(self._metrics_box)

        # Paramètres
        pf = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        pf.add_css_class("params-panel")
        pt = Gtk.Label(label="PARAMETRES")
        pt.add_css_class("params-title"); pt.set_halign(Gtk.Align.START)
        pf.append(pt)
        grid = Gtk.Grid()
        grid.set_row_spacing(10); grid.set_column_spacing(16)
        self._build_params(grid)
        pf.append(grid)
        box.append(pf)

        # Boutons run / stop
        br = Gtk.Box(spacing=10)
        self._btn_run = Gtk.Button(label="▶  Lancer le test")
        self._btn_run.add_css_class("btn-run")
        self._btn_run.set_hexpand(True)
        self._btn_run.set_sensitive(not bool(self._missing))
        self._btn_run.connect("clicked", self._on_run)
        self._btn_stop = Gtk.Button(label="⏹  Stopper")
        self._btn_stop.add_css_class("btn-stop")
        self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)
        br.append(self._btn_run); br.append(self._btn_stop)
        header.append(br)

        # Export (activé après résultat)
        er = Gtk.Box(spacing=8)
        b = Gtk.Button(label="💾  Exporter")
        b.set_tooltip_text("Exporter CSV + JSON + PDF + HTML")
        b.add_css_class("btn-json")
        b.set_sensitive(False)
        b.connect("clicked", self._on_export_all)
        br.append(b)
        self._pw["exp_all"] = b

        # Log brut repliable
        exp = Gtk.Expander(label="Sortie brute du benchmark")
        exp.set_margin_top(4)
        ls = Gtk.ScrolledWindow()
        ls.set_min_content_height(110); ls.set_max_content_height(200)
        self._log_buf = Gtk.TextBuffer()
        lv = Gtk.TextView(buffer=self._log_buf)
        lv.set_editable(False); lv.set_cursor_visible(False)
        lv.add_css_class("log-view"); lv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ls.set_child(lv); exp.set_child(ls)
        box.append(exp)

        scroll.set_child(box)
        self.append(header)
        self.append(scroll)
        self._refresh_dependencies()

    def _refresh_dependencies(self):
        """Affiche, pour chaque outil manquant, la commande d'installation (copiable) et le choix d'un chemin personnalisé."""
        self._missing = [t for t in self.bench_def.get("requires", []) if not tool_exists(t)]

        if self._missing:
            self._warning_label.set_markup(
                f'<span foreground="#f9e2af"><b>Outil manquant : {GLib.markup_escape_text(", ".join(self._missing))}</b> — '
                f'ce test est désactivé tant qu\'il n\'est pas installé.</span>')
            while True:
                child = self._warning_actions.get_first_child()
                if not child:
                    break
                self._warning_actions.remove(child)

            from core import tools_catalog as _tc
            for tool in self._missing:
                # Une ligne par procédure du catalogue (Ubuntu apt, compilation…) : commande à copier
                for label, cmd in _tc.install_hint(tool)[:3]:
                    row = Gtk.Box(spacing=8)
                    first = cmd.split("\n")[0]
                    cl = Gtk.Label(label=f"{label} :  {first}" + ("  …" if "\n" in cmd else ""))
                    cl.add_css_class("hint-tt"); cl.set_selectable(True)
                    cl.set_halign(Gtk.Align.START); cl.set_hexpand(True); cl.set_xalign(0); cl.set_wrap(True)
                    cp = Gtk.Button(label="📋 Copier")
                    cp.connect("clicked", lambda _b, c=cmd: self.get_clipboard().set(c))
                    cp.set_tooltip_text("Copie toute la procédure d'installation")
                    row.append(cl); row.append(cp)
                    self._warning_actions.append(row)
                acts = Gtk.Box(spacing=8)
                more = Gtk.Button(label="Procédure complète")
                more.connect("clicked", lambda _b: self.win._nav_click(None, "installation"))
                acts.append(more)
                btn = Gtk.Button(label=f"Choisir le chemin de « {tool} »…")
                btn.connect("clicked", lambda _b, t=tool: self._browse_custom_tool(t))
                acts.append(btn)
                self._warning_actions.append(acts)

            self._warning_box.set_visible(True)
        else:
            self._warning_box.set_visible(False)

        if hasattr(self, "_btn_run") and self._btn_run:
            self._btn_run.set_sensitive(not bool(self._missing))

    def _browse_custom_tool(self, tool_name):
        title = f"Sélectionner l'exécutable pour {tool_name}"
        
        def on_selected(path):
            if path:
                if os.path.isfile(path) and os.access(path, os.X_OK):
                    set_custom_tool_path(tool_name, path)
                    self._refresh_dependencies()
                    self.win._show_dialog(
                        "Chemin configuré",
                        f"L'outil '{tool_name}' utilise désormais :\n{path}"
                    )
                else:
                    self.win._show_dialog(
                        "Erreur",
                        f"Le fichier sélectionné n'est pas un exécutable valide :\n{path}"
                    )
        
        if hasattr(Gtk, "FileDialog"):
            dialog = Gtk.FileDialog()
            dialog.set_title(title)
            def on_open_finish(dlg, res):
                try:
                    f = dlg.open_finish(res)
                    if f:
                        GLib.idle_add(on_selected, f.get_path())
                    else:
                        GLib.idle_add(on_selected, None)
                except Exception:
                    GLib.idle_add(on_selected, None)
            dialog.open(self.win, None, on_open_finish)
        else:
            dialog = Gtk.FileChooserNative(
                title=title,
                transient_for=self.win,
                action=Gtk.FileChooserAction.OPEN,
                accept_label="Sélectionner",
                cancel_label="Annuler"
            )
            def on_response(dlg, response_id):
                if response_id == Gtk.ResponseType.ACCEPT:
                    f = dlg.get_file()
                    path = f.get_path() if f else None
                    GLib.idle_add(on_selected, path)
                else:
                    GLib.idle_add(on_selected, None)
                dlg.destroy()
            dialog.connect("response", on_response)
            dialog.show()

    # ── Helpers params ────────────────────────────────────────────────────────

    def _spin(self, lo, hi, step, val):
        s = Gtk.SpinButton.new_with_range(lo, hi, step)
        s.set_value(val); s.set_size_request(120, -1)
        return s

    def _dropdown_map(self, pairs, default_label=None):
        """Liste déroulante de libellés lisibles ; la valeur réelle est retrouvée par _get_params."""
        labels = [lab for lab, _v in pairs]
        dd = self._dropdown(labels, default_label or labels[0])
        dd._value_map = dict(pairs)
        dd.set_size_request(320, -1)
        return dd

    def _iperf_bind_row(self, grid, r):
        """Ligne « Port d'envoi » des tests iperf3 : liste des adresses IPv4 de cette machine (une par port) ; « Automatique » = route du système."""
        pairs = [("Automatique (route du système)", "")]
        try:
            from core import iperf_server as isrv
            for a in isrv.local_addresses():
                pairs.append((f"{a['iface']} — {a['ip']}" + (f" ({a['speed_mbps']} Mb/s)" if a.get("speed_mbps") else ""), a["ip"]))
        except Exception:
            pass
        dd = self._dropdown_map(pairs)
        dd.set_tooltip_text("Force le test sur une carte / un port précis de CETTE machine (iperf3 -B). Un port sans adresse n'apparaît pas : "
                            "donnez-lui une adresse depuis la page « Serveur iperf3 (réseau) ».")
        self._pw["bind"] = dd
        self._row(grid, r, "Port d'envoi :", dd)
        return r + 1

    def _cli_check(self, grid, r, label, flag):
        """Case à cocher qui ajoute `flag` à la ligne de commande de l'outil ; retourne la ligne suivante."""
        cb = self._check(label, default=False)
        if not hasattr(self, "_cli_opts"):
            self._cli_opts = []
        self._cli_opts.append((cb, flag))
        grid.attach(cb, 0, r, 2, 1)
        return r + 1

    def _cli_choice(self, grid, r, label, pairs, default_label=None):
        """Liste déroulante : chaque choix correspond à des options de ligne de commande (éventuellement vides)."""
        dd = self._dropdown_map(pairs, default_label)
        if not hasattr(self, "_cli_opts"):
            self._cli_opts = []
        self._cli_opts.append((dd, dict(pairs)))
        self._row(grid, r, label, dd)
        return r + 1

    def _advanced_cli(self, grid, r, placeholder="Options supplémentaires (optionnel)…"):
        """Zone repliée pour les experts : saisie libre d'options en plus des choix cliquables."""
        exp = Gtk.Expander(label="Options avancées (ligne de commande)")
        entry = self._entry("")
        entry.set_placeholder_text(placeholder)
        exp.set_child(entry)
        self._pw["extra_args"] = entry
        grid.attach(exp, 0, r, 2, 1)
        return r + 1

    def _gpu_target_rows(self, grid, r):
        """Choix des GPU cibles (aucun coché = tous) ; retourne la ligne suivante. La liste arrive en arrière-plan
        (GpuTargetChooser) : la construction de la fenêtre ne lance plus de nvidia-smi."""
        w = GpuTargetChooser()
        self._pw["target_gpus"] = w
        self._row(grid, r, "GPU à tester (aucun coché = tous) :", w)
        return r + 1

    def _entry(self, text):
        e = Gtk.Entry(); e.set_text(text); e.set_size_request(160, -1)
        return e

    def _row(self, grid, row, label, widget):
        l = Gtk.Label(label=label)
        l.add_css_class("param-label"); l.set_halign(Gtk.Align.END)
        grid.attach(l, 0, row, 1, 1)
        grid.attach(widget, 1, row, 1, 1)

    def _dropdown(self, options: list, default: str = "") -> Gtk.DropDown:
        sl = Gtk.StringList.new(options)
        dd = Gtk.DropDown(model=sl)
        dd.set_size_request(160, -1)
        if default in options:
            dd.set_selected(options.index(default))
        return dd

    def _check(self, label: str, default: bool = False) -> Gtk.CheckButton:
        cb = Gtk.CheckButton(label=label)
        cb.set_active(default)
        return cb

    def _build_params(self, grid):
        from core.benchmarks import STRESS_NG_STRESSORS
        bid = self.bench_def["id"]
        r   = 0
        cpu_max = os.cpu_count() or 8
        self._cli_opts = []          # options de ligne de commande cliquables (voir _cli_check / _cli_choice)

        # ── CPU stress-ng ───────────────────────────────────────────────────
        if bid == "cpu_stress":
            w = self._spin(1, cpu_max, 1, min(4, cpu_max))
            self._pw["threads"] = w; self._row(grid, r, "Threads :", w); r += 1

            w2 = self._spin(5, 300, 5, 10)
            self._pw["time_s"] = w2; self._row(grid, r, "Duree (s) :", w2); r += 1

            dd = self._dropdown(STRESS_NG_STRESSORS, "cpu")
            self._pw["stressor"] = dd; self._row(grid, r, "Stressor (methode) :", dd); r += 1

            w3 = self._spin(0, 99, 5, 0)
            self._pw["cpu_load_pct"] = w3
            self._row(grid, r, "Charge cible % (0=max) :", w3); r += 1

            extra = self._entry("")
            self._pw["extra_args"] = extra
            self._row(grid, r, "Options stress-ng CLI :", extra); r += 1

            info = Gtk.Label()
            info.set_markup(
                '<span foreground="#6c7086" size="small">'
                'cpu=entiers   fft=transformee de Fourier   matrix=matrices\n'
                'bitops=operations bit   float/double=virgule flottante   all=tous'
                '</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU gpu-burn ────────────────────────────────────────────────────
        elif bid == "gpu_burn":
            w = self._spin(10, 86400, 10, 30)
            self._pw["time_s"] = w; self._row(grid, r, "Duree stress (s) :", w); r += 1

            dd = self._dropdown_map([("Simple précision FP32 (float)", "float"), ("Double précision FP64 (double)", "double")])
            self._pw["precision"] = dd; self._row(grid, r, "Précision :", dd); r += 1

            r = self._cli_choice(grid, r, "Mémoire GPU utilisée :", [
                ("Par défaut de gpu-burn (~90 %)", ""), ("50 %", "-m 50%"), ("75 %", "-m 75%"),
                ("95 %", "-m 95%"), ("98 %", "-m 98%")])
            r = self._cli_check(grid, r, "Utiliser les Tensor Cores si disponibles (-tc)", "-tc")
            r = self._gpu_target_rows(grid, r)
            r = self._advanced_cli(grid, r)

            info = Gtk.Label()
            info.set_markup(
                '<span foreground="#6c7086" size="small">'
                'Pousse le GPU a 100% et detecte les erreurs memoire CUDA.\n'
                'FP64 = plus lent mais teste la double precision. Attention : necessite un bon refroidissement.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── RAM STREAM ───────────────────────────────────────────────────────
        elif bid == "ram_stream":
            nproc = os.cpu_count() or 1
            w = self._spin(0, nproc, 1, 0)
            self._pw["threads"] = w; self._row(grid, r, "Threads (0 = cœurs physiques) :", w); r += 1
            w2 = self._spin(0, 262144, 64, 0)
            self._pw["array_mb"] = w2; self._row(grid, r, "Taille par tableau (Mo, 0 = auto) :", w2); r += 1
            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">STREAM (McCalpin) est compilé au premier lancement avec gcc '
                            '(sudo apt install gcc). Auto = tableaux ≥ 4× le cache L3 total, bornés à 25 % de la RAM libre.\n'
                            'Résultat = Triad en GB/s ; à citer comme « basé sur STREAM ».</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU bande passante VRAM ──────────────────────────────────────────
        elif bid == "gpu_vram_bw":
            w = self._spin(256, 16384, 256, 2048)
            self._pw["buffer_size_mb"] = w; self._row(grid, r, "Taille buffer (Mo) :", w); r += 1
            w2 = self._spin(1, 20, 1, 5)
            self._pw["iterations"] = w2; self._row(grid, r, "Itérations :", w2); r += 1
            dd = self._dropdown_map([
                ("Automatique (nvbandwidth si présent, sinon clpeak)", "auto"),
                ("nvbandwidth — NVIDIA, une mesure par GPU", "nvbandwidth"),
                ("clpeak — OpenCL, toutes marques", "clpeak"),
                ("Les deux (pour recouper)", "both")])
            self._pw["method"] = dd; self._row(grid, r, "Outil de mesure :", dd); r += 1
            r = self._gpu_target_rows(grid, r)
            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">Un GPU nettement sous les autres indique un souci mémoire ou de bridage.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU diagnostic DCGM ──────────────────────────────────────────────
        elif bid == "gpu_dcgm":
            dd = self._dropdown_map([
                ("1 — Rapide (quelques secondes)", 1), ("2 — Moyen (≈ 2 min)", 2),
                ("3 — Long (≈ 15 min) : mémoire, calcul, PCIe, stress", 3), ("4 — Étendu (plus d'une heure)", 4)],
                "3 — Long (≈ 15 min) : mémoire, calcul, PCIe, stress")
            self._pw["level"] = dd; self._row(grid, r, "Niveau du diagnostic :", dd); r += 1
            mods = MultiCheck([
                ("software", "Logiciel / pilote"), ("pcie", "Liaison PCIe"), ("memory", "Mémoire GPU"),
                ("memtest", "Memtest (motifs mémoire)"), ("diagnostic", "Calcul (diagnostic)"),
                ("sm_stress", "Stress des SM"), ("targeted_stress", "Stress ciblé"),
                ("targeted_power", "Puissance ciblée"), ("memory_bandwidth", "Bande passante mémoire"),
                ("context_create", "Création de contexte"), ("pulse_test", "Pulse (variations de charge)")], columns=2)
            self._pw["tests"] = mods
            self._row(grid, r, "Modules précis (aucun coché = selon le niveau) :", mods); r += 1
            chk = self._check("Arrêt anticipé en cas d'échec (--fail-early)", default=True)
            self._pw["fail_early"] = chk; grid.attach(chk, 0, r, 2, 1); r += 1
            chk2 = self._check("Adapter le seuil PCIe à la liaison réelle (x4, x8… évite un faux échec sur câblage MCIO)", default=True)
            self._pw["pcie_auto"] = chk2; grid.attach(chk2, 0, r, 2, 1); r += 1
            r = self._gpu_target_rows(grid, r)
            r = self._advanced_cli(grid, r)
            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">Diagnostic NVIDIA DCGM (dcgmi diag). Aucun autre test GPU ne doit tourner en même temps.\n'
                            'Installation : sudo apt install datacenter-gpu-manager-4-cuda13 (ou -cuda12) puis sudo systemctl --now enable nvidia-dcgm.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU intégrité VRAM ───────────────────────────────────────────────
        elif bid == "gpu_vram_test":
            w = self._spin(30, 7200, 30, 300)
            self._pw["time_s"] = w; self._row(grid, r, "Durée (s) :", w); r += 1
            w2 = self._spin(10, 98, 1, 95)
            self._pw["vram_pct"] = w2; self._row(grid, r, "VRAM remplie (%) :", w2); r += 1
            dd = self._dropdown_map([("Simple précision FP32 (float)", "float"), ("Double précision FP64 (double)", "double")])
            self._pw["precision"] = dd; self._row(grid, r, "Précision :", dd); r += 1
            r = self._gpu_target_rows(grid, r)
            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">gpu-burn remplit la VRAM et compare chaque résultat : '
                            'toute erreur (mémoire ou calcul) est détectée. Nécessite un bon refroidissement.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU nvbandwidth ──────────────────────────────────────────────────
        elif bid == "gpu_nvbandwidth":
            w = self._spin(64, 8192, 64, 512)
            self._pw["buffer_size_mb"] = w; self._row(grid, r, "Taille buffer (Mo) :", w); r += 1

            w2 = self._spin(1, 20, 1, 3)
            self._pw["iterations"] = w2; self._row(grid, r, "Iterations :", w2); r += 1

            r = self._cli_choice(grid, r, "Tests à lancer :", [
                ("Tous les tests", ""),
                ("Hôte ↔ GPU (PCIe) : host_to_device + device_to_host", "-t host_to_device_memcpy_ce device_to_host_memcpy_ce"),
                ("GPU ↔ GPU (tous les device_to_device)", "-p device_to_device"),
                ("Copie locale de la VRAM (device_local_copy)", "-t device_local_copy"),
                ("Latence GPU ↔ GPU (device_to_device_latency)", "-p device_to_device_latency")])
            r = self._cli_check(grid, r, "Moyenne au lieu de la médiane (-m)", "-m")
            r = self._cli_check(grid, r, "Ne pas vérifier les données copiées (-s, plus rapide)", "-s")
            r = self._cli_check(grid, r, "Pages mémoire « huge » côté hôte (-H)", "-H")
            r = self._gpu_target_rows(grid, r)
            r = self._advanced_cli(grid, r)

            info = Gtk.Label()
            info.set_markup(
                '<span foreground="#6c7086" size="small">'
                'Mesure la bande passante (CE/SM) via NVLink / PCIe entre les GPU et le CPU.\n'
                'Taille buffer : taille des transferts mémoire (Mo). Itérations : nombre de mesures.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU gpu-fryer ───────────────────────────────────────────────────
        elif bid == "gpu_fryer":
            w = self._spin(10, 86400, 10, 30)
            self._pw["time_s"] = w; self._row(grid, r, "Duree stress (s) :", w); r += 1

            dd = self._dropdown_map([("FP32 (float32)", "float32"), ("BF16 (bfloat16, GPU récents)", "bfloat16")])
            self._pw["dtype"] = dd; self._row(grid, r, "Précision :", dd); r += 1

            r = self._cli_choice(grid, r, "Tolérance d'écart de TFLOPS entre GPU :", [
                ("10 % (défaut de gpu-fryer)", ""), ("5 % (strict)", "--tflops-tolerance 5"),
                ("15 %", "--tflops-tolerance 15"), ("20 % (large)", "--tflops-tolerance 20")])
            r = self._cli_check(grid, r, "Tolérer le throttling logiciel (--tolerate-software-throttling)",
                                "--tolerate-software-throttling")
            r = self._gpu_target_rows(grid, r)
            r = self._advanced_cli(grid, r)

            info = Gtk.Label()
            info.set_markup(
                '<span foreground="#6c7086" size="small">'
                'Stresse tous les GPU et détecte le throttling ou un GPU plus lent que les autres.\n'
                'Mesure les TFLOPS réels en conditions de charge IA (GPU NVIDIA, CUDA).</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── Reseau iperf3 TCP ───────────────────────────────────────────────
        elif bid == "net_iperf_tcp":
            w = self._entry("127.0.0.1")
            self._pw["host"] = w; self._row(grid, r, "Serveur iperf3 :", w); r += 1

            w2 = self._spin(1, 65535, 1, 5201)
            self._pw["port"] = w2; self._row(grid, r, "Port :", w2); r += 1

            w3 = self._spin(1, 86400, 1, 10)
            self._pw["time_s"] = w3; self._row(grid, r, "Duree (s) :", w3); r += 1

            w4 = self._spin(1, 128, 1, 1)
            self._pw["parallel"] = w4; self._row(grid, r, "Flux paralleles :", w4); r += 1

            r = self._iperf_bind_row(grid, r)

            chk = self._check("Reverse (-R, serveur vers client)", default=False)
            self._pw["reverse"] = chk; grid.attach(chk, 1, r, 1, 1); r += 1

            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">Sur l’autre machine : menu « Serveur iperf3 (réseau) » de BenchSuite, ou `iperf3 -s`</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── Reseau iperf3 UDP ───────────────────────────────────────────────
        elif bid == "net_iperf_udp":
            w = self._entry("127.0.0.1")
            self._pw["host"] = w; self._row(grid, r, "Serveur iperf3 :", w); r += 1

            w2 = self._spin(1, 65535, 1, 5201)
            self._pw["port"] = w2; self._row(grid, r, "Port :", w2); r += 1

            w3 = self._spin(1, 86400, 1, 10)
            self._pw["time_s"] = w3; self._row(grid, r, "Duree (s) :", w3); r += 1

            bw = self._entry("100M")
            bw.set_placeholder_text("ex: 100M, 1G, 0 = illimite")
            self._pw["bandwidth"] = bw; self._row(grid, r, "Bande passante cible :", bw); r += 1

            w4 = self._spin(1, 128, 1, 1)
            self._pw["parallel"] = w4; self._row(grid, r, "Flux paralleles :", w4); r += 1

            r = self._iperf_bind_row(grid, r)

            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">UDP mesure débit, gigue et pertes. Sur l’autre machine : menu « Serveur iperf3 (réseau) » ou `iperf3 -s`</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        # ── GPU clpeak ──────────────────────────────────────────────────────
        elif bid == "gpu_clpeak":
            r = self._gpu_target_rows(grid, r)
            dd_cl = self._dropdown_map([
                ("Tous les tests", "all-tests"),
                ("Bande passante mémoire (VRAM)", "global-bandwidth"),
                ("Calcul FP32 (simple précision)", "compute-sp"),
                ("Calcul FP64 (double précision)", "compute-dp"),
                ("Calcul entiers", "compute-integer"),
                ("Transferts hôte ↔ GPU (PCIe)", "transfer-bw")])
            self._pw["test_mode"] = dd_cl
            self._row(grid, r, "Test OpenCL :", dd_cl); r += 1
            w_burn = self._spin(0, 86400, 60, 0)
            self._pw["time_s"] = w_burn; self._row(grid, r, "Chauffe (s) :", w_burn); r += 1
            hint = Gtk.Label()
            hint.set_markup('<span foreground="#6c7086" size="small">0 = un seul passage. &gt; 0 : clpeak est relancé en boucle pendant cette durée '
                            '(par GPU) pour chauffer le GPU ; choisir « Calcul FP32 » pour la charge la plus forte. '
                            'La chute de performance en charge soutenue est mesurée.</span>')
            hint.set_halign(Gtk.Align.START); hint.set_wrap(True)
            grid.attach(hint, 0, r, 2, 1); r += 1
            r = self._advanced_cli(grid, r)

        # ── GPU burn OpenCL vérifié (oclburn) ───────────────────────────────
        elif bid == "gpu_oclburn":
            r = self._gpu_target_rows(grid, r)
            w = self._spin(10, 86400, 60, 300)
            self._pw["time_s"] = w; self._row(grid, r, "Durée (s) :", w); r += 1
            w_v = self._spin(5, 95, 5, 90)
            self._pw["vram_pct"] = w_v; self._row(grid, r, "VRAM testée (%) :", w_v); r += 1
            dd_m = self._dropdown_map([
                ("Puissance constante (FP32 FMA pur vérifié — chauffe stable)", "power"),
                ("Calcul + mémoire (100 % du GPU + VRAM)", "all"),
                ("Calcul seul (entiers, FP32, FP16)", "compute"),
                ("Mémoire seule (motifs sur la VRAM)", "memory"),
            ])
            self._pw["mode"] = dd_m
            self._row(grid, r, "Charge :", dd_m); r += 1
            dd_v = self._dropdown_map([("Vérification maximale (recommandé)", "max"), ("Équilibrée", "balanced"), ("Charge maximale (détection réduite)", "power")])
            self._pw["verification"] = dd_v
            self._row(grid, r, "Contrôle FP32 / FP16 :", dd_v); r += 1
            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">Burn OpenCL maison (Intel, AMD, NVIDIA) : chaque résultat est <b>vérifié</b> (erreurs comptées). '
                            'Le mode <b>Puissance constante</b> maintient une charge FP32 FMA pure et plate au TDP (sans chute due à la concurrence VRAM sur Intel/AMD) ; '
                            'le mode <b>Calcul + mémoire</b> teste aussi la VRAM en parallèle. Compilé automatiquement avec gcc.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1); r += 1

        # ── GPU vkpeak ──────────────────────────────────────────────────────
        elif bid == "gpu_vkpeak":
            r = self._gpu_target_rows(grid, r)
            w_burn = self._spin(0, 86400, 60, 0)
            self._pw["time_s"] = w_burn; self._row(grid, r, "Chauffe (s) :", w_burn); r += 1
            r = self._advanced_cli(grid, r)

            info = Gtk.Label()
            info.set_markup('<span foreground="#6c7086" size="small">vkpeak exécute sa suite complète (FP16/FP32/FP64/INT) sur l&#39;API Vulkan, '
                            'un passage par GPU coché (index de device Vulkan). <b>Chauffe</b> : 0 = un seul passage ; &gt; 0 = vkpeak est relancé en boucle '
                            'pendant cette durée (par GPU) pour faire chauffer le GPU (~300 W sur une Arc Pro B70), avec mesure de la chute de performance.</span>')
            info.set_halign(Gtk.Align.START); info.set_wrap(True)
            grid.attach(info, 0, r, 2, 1)

        else:
            lbl = Gtk.Label(label="Aucun parametre supplementaire.")
            lbl.add_css_class("bench-subtitle"); lbl.set_halign(Gtk.Align.START)
            grid.attach(lbl, 0, r, 2, 1)

    def _browse(self, _):
        Gtk.FileDialog().select_folder(self.win, None, self._on_folder)

    def _on_folder(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
            if f and "path" in self._pw:
                self._pw["path"].set_text(f.get_path())
        except Exception:
            pass

    def _get_params(self):
        p = {}
        for k, w in self._pw.items():
            if k.startswith("exp_"): continue
            if isinstance(w, Gtk.SpinButton):
                p[k] = int(w.get_value())
            elif isinstance(w, Gtk.Entry):
                p[k] = w.get_text()
            elif isinstance(w, Gtk.DropDown):
                item = w.get_selected_item()
                val  = item.get_string() if item else ""
                if hasattr(w, "_value_map"):          # libellé lisible → valeur réelle
                    p[k] = w._value_map.get(val, val)
                    continue
                # Convertir les valeurs numériques stringifiées
                if val.isdigit():
                    val = int(val)
                p[k] = val
            elif isinstance(w, Gtk.CheckButton):
                p[k] = w.get_active()
            elif isinstance(w, MultiCheck):
                p[k] = w.get_bench_value()
        # Options de ligne de commande choisies par clic (+ saisie avancée éventuelle)
        flags = []
        for w, spec in getattr(self, "_cli_opts", []):
            if isinstance(w, Gtk.CheckButton):
                if w.get_active():
                    flags.append(spec)
            elif isinstance(w, Gtk.DropDown):
                item = w.get_selected_item()
                flags.append(spec.get(item.get_string() if item else "", ""))
        flags = [f for f in flags if f]
        if flags or "extra_args" in p:
            p["extra_args"] = " ".join(flags + [str(p.get("extra_args", "")).strip()]).strip()
        return p

    # ── Actions ───────────────────────────────────────────────────────────────

    def _on_run(self, _):
        self.result = None
        self._clear_metrics()
        self._score_val.set_markup('<span foreground="#6c7086">...</span>')
        self._score_unit.set_text("")
        self._progress.set_fraction(0); self._progress.set_text("Demarrage...")
        self._log_buf.set_text("")
        self._btn_run.set_sensitive(False)
        self._btn_stop.set_sensitive(True)
        self._pw["exp_all"].set_sensitive(False)
        self.win._set_status(f"Execution : {self.bench_def['name']}...")
        params = self._get_params()
        if self._live_chart is not None:
            self._live_chart.reset()
            def _series_cb(point):
                GLib.idle_add(self._live_chart.push, point)
            params["series_cb"] = _series_cb
        self.worker = BenchWorker(self.bench_def, params,
                                   self._on_progress, self._on_done)
        self.worker.start()

    def _on_stop(self, _):
        if self.worker: self.worker.stop()
        self._btn_stop.set_sensitive(False)
        self._progress.set_text("Arrete")
        self.win._set_status("Arrete")

    def _on_progress(self, pct, msg):
        self._progress.set_fraction(pct / 100)
        self._progress.set_text(msg)
        return False

    def _on_done(self, result: BenchmarkResult):
        self.result = result
        self._btn_run.set_sensitive(not bool(self._missing))
        self._btn_stop.set_sensitive(False)
        color = CAT_COLORS.get(result.category, "#f97316")

        if result.status == "ok":
            if result.score is not None and (result.metrics or {}).get("boucle_locale"):
                # iperf3 vers une adresse de cette machine : débit de la machine, pas du réseau — affiché en jaune avec l'explication
                self._score_val.set_markup(f'<span foreground="#f9e2af" weight="ultrabold">{result.score:.3f}</span>')
                self._score_unit.set_markup(f'<span foreground="#f9e2af">{result.unit} — ⚠ test local : le trafic n’a pas traversé le réseau</span>')
            elif result.score is not None:
                self._score_val.set_markup(
                    f'<span foreground="{color}" weight="ultrabold">{result.score:.3f}</span>')
                self._score_unit.set_markup(
                    f'<span foreground="#6c7086">{result.unit}</span>')
            else:
                self._score_val.set_markup('<span foreground="#a6e3a1" size="xx-large">OK</span>')
                self._score_unit.set_text("Termine")
            self._progress.set_fraction(1.0)
            self._progress.set_text("⚠ Termine, mais sans traverser le reseau (adresse de cette machine)" if (result.metrics or {}).get("boucle_locale") else "Termine avec succes")
        elif result.status == "stopped":
            self._score_val.set_markup('<span foreground="#f9e2af" size="xx-large">STOP</span>')
            self._score_unit.set_text("Arrêté par utilisateur")
            self._progress.set_text("Arrêté")
        elif result.status == "skipped":
            self._score_val.set_markup('<span foreground="#f9e2af" size="xx-large">N/D</span>')
            self._score_unit.set_text("Outil non installe")
            self._progress.set_text("Ignore")
        else:
            self._score_val.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
            self._score_unit.set_text("Erreur")
            self._progress.set_text("Erreur")

        self._populate_metrics(result)
        self._log_buf.set_text(result.raw_output[:8000] if result.raw_output else "")
        self._pw["exp_all"].set_sensitive(True)
        save_to_history([result])
        self.win._on_bench_result(result)
        return False

    def _clear_metrics(self):
        c = self._metrics_box.get_first_child()
        while c:
            n = c.get_next_sibling()
            self._metrics_box.remove(c); c = n

    def _populate_metrics(self, result: BenchmarkResult):
        self._clear_metrics()
        cmd = getattr(result, "command", "") or result.metrics.get("command", "")
        if cmd:
            cmd_card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            cmd_card.add_css_class("metric-card")
            cmd_card.set_hexpand(True)
            cmd_card.set_size_request(320, -1)
            clbl = Gtk.Label(label="COMMANDE EXÉCUTÉE")
            clbl.add_css_class("metric-label"); clbl.set_halign(Gtk.Align.START)
            cval = Gtk.Label(label=str(cmd))
            cval.add_css_class("metric-value"); cval.set_halign(Gtk.Align.START); cval.set_wrap(True)
            cval.set_selectable(True)
            cmd_card.append(clbl); cmd_card.append(cval)
            self._metrics_box.append(cmd_card)
        from core.electrical import summary_cards
        for title, txt in summary_cards(result.metrics):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(220, -1)
            lbl = Gtk.Label(label=title); lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START)
            val = Gtk.Label(label=txt); val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START); val.set_wrap(True)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)
        for k, v in result.metrics.items():
            if k == "command": continue
            if isinstance(v, (list, dict)) or k.startswith("series_") or k == "series":
                continue   # séries temporelles → graphique, pas carte
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(150, -1)
            lbl = Gtk.Label(label=k.replace("_", " ").upper())
            lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START); lbl.set_wrap(True)
            val = Gtk.Label(label=str(v))
            val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START); val.set_wrap(True)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)

    def _on_export_all(self, _):
        if not self.result: return
        results, sysinfo, meta = [self.result], self.win.system_info_snapshot(), load_report_meta()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, report_meta=meta, system_info_data=sysinfo)
            return "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())
        self.win.export_async(work, "Export complet réussi")


# ─────────────────────────────────────────────────────────────────────────────
# Page résultats globaux
# ─────────────────────────────────────────────────────────────────────────────

class ResultsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.win = win
        self.set_margin_top(20); self.set_margin_start(20)
        self.set_margin_end(20); self.set_margin_bottom(20)
        self._store   = Gio.ListStore(item_type=ResultWrapper)
        self._results = []
        self._selection = None
        self._notes_buf = None
        self._loading_notes = False
        self._build()

    def _build(self):
        top = Gtk.Box(spacing=8)
        hdr = Gtk.Label()
        hdr.set_markup('<span weight="bold" size="large">Resultats — session courante</span>')
        hdr.set_hexpand(True); hdr.set_halign(Gtk.Align.START)

        meta = load_report_meta()
        
        name_box = Gtk.Grid(row_spacing=6, column_spacing=12)
        name_box.set_margin_bottom(12)
        
        # Row 0
        name_box.attach(Gtk.Label(label="Nom système :", halign=Gtk.Align.END), 0, 0, 1, 1)
        self._system_name_entry = Gtk.Entry(placeholder_text="Ex: RACK01-NODE03", width_chars=20)
        self._system_name_entry.set_text(meta.get("system_name", ""))
        name_box.attach(self._system_name_entry, 1, 0, 1, 1)
        
        name_box.attach(Gtk.Label(label="Opérateur :", halign=Gtk.Align.END), 2, 0, 1, 1)
        self._operator_entry = Gtk.Entry(placeholder_text="Ex: John Doe", width_chars=15)
        self._operator_entry.set_text(meta.get("operator", ""))
        name_box.attach(self._operator_entry, 3, 0, 1, 1)
        
        # Row 1
        name_box.attach(Gtk.Label(label="Temp. ambiante :", halign=Gtk.Align.END), 0, 1, 1, 1)
        self._ambient_temp_entry = Gtk.Entry(placeholder_text="Ex: 24°C", width_chars=20)
        self._ambient_temp_entry.set_text(meta.get("ambient_temp", ""))
        name_box.attach(self._ambient_temp_entry, 1, 1, 1, 1)
        
        name_box.attach(Gtk.Label(label="Ventilation :", halign=Gtk.Align.END), 2, 1, 1, 1)
        self._fan_speed_entry = Gtk.Entry(placeholder_text="Ex: Auto / 100%", width_chars=15)
        self._fan_speed_entry.set_text(meta.get("fan_speed", ""))
        name_box.attach(self._fan_speed_entry, 3, 1, 1, 1)
        
        # Row 2
        name_box.attach(Gtk.Label(label="Client :", halign=Gtk.Align.END), 0, 2, 1, 1)
        self._client_entry = Gtk.Entry(placeholder_text="Ex: Client X", width_chars=20)
        self._client_entry.set_text(meta.get("client", ""))
        name_box.attach(self._client_entry, 1, 2, 1, 1)

        name_box.attach(Gtk.Label(label="N° de commande :", halign=Gtk.Align.END), 2, 2, 1, 1)
        self._order_entry = Gtk.Entry(placeholder_text="Ex: CMD-1234", width_chars=15)
        self._order_entry.set_text(meta.get("order_no", ""))
        name_box.attach(self._order_entry, 3, 2, 1, 1)

        # Row 3 : identité de la machine (détectable via DMI)
        name_box.attach(Gtk.Label(label="N° de série :", halign=Gtk.Align.END), 0, 3, 1, 1)
        self._serial_entry = Gtk.Entry(placeholder_text="Détecté via DMI (bouton)", width_chars=20)
        self._serial_entry.set_text(meta.get("serial", ""))
        name_box.attach(self._serial_entry, 1, 3, 1, 1)

        name_box.attach(Gtk.Label(label="Modèle :", halign=Gtk.Align.END), 2, 3, 1, 1)
        self._model_entry = Gtk.Entry(placeholder_text="Ex: Serveur 8 GPU", width_chars=15)
        self._model_entry.set_text(meta.get("model", ""))
        name_box.attach(self._model_entry, 3, 3, 1, 1)

        btn_detect = Gtk.Button(label="Détecter série / modèle")
        btn_detect.set_tooltip_text("Relit le n° de série et le modèle de la machine (DMI) et remplace les valeurs saisies")
        btn_detect.add_css_class("btn-json")
        btn_detect.set_halign(Gtk.Align.START)
        btn_detect.connect("clicked", self._detect_identity)
        name_box.attach(btn_detect, 0, 4, 2, 1)

        btn_save_name = Gtk.Button(label="Sauver les métadonnées")
        btn_save_name.add_css_class("btn-json")
        btn_save_name.set_halign(Gtk.Align.START)
        btn_save_name.connect("clicked", self._save_system_name)
        name_box.attach(btn_save_name, 2, 4, 2, 1)

        self._btns = {}
        b = Gtk.Button(label="Exporter CSV + JSON + PDF + HTML")
        b.add_css_class("btn-json"); b.set_sensitive(False)
        b.connect("clicked", self._export_all)
        self._btns["all"] = b

        btn_clear = Gtk.Button(label="Vider")
        btn_clear.connect("clicked", self._clear)

        btn_delete = Gtk.Button(label="Supprimer le test sélectionné")
        btn_delete.set_sensitive(False)
        btn_delete.connect("clicked", self._delete_selected)
        self._btns["delete"] = btn_delete

        top.append(hdr)
        top.append(name_box)
        for b in self._btns.values(): top.append(b)
        top.append(btn_clear)
        self.append(top)

        self._selection = Gtk.SingleSelection(model=self._store)
        self._selection.connect("selection-changed", self._on_sel)
        cv = Gtk.ColumnView(model=self._selection)
        cv.set_show_row_separators(True); cv.set_vexpand(True)
        cc = CAT_COLORS

        def col(title, fn, w, markup=False):
            f = Gtk.SignalListItemFactory()
            def setup(_, item): l = Gtk.Label(); l.set_halign(Gtk.Align.CENTER); item.set_child(l)
            def bind(_, item, _fn=fn, _m=markup):
                r = item.get_item().result; l = item.get_child(); v = _fn(r)
                if _m: l.set_markup(v)
                else:  l.set_text(str(v))
            f.connect("setup", setup); f.connect("bind", bind)
            c = Gtk.ColumnViewColumn(title=title, factory=f); c.set_fixed_width(w)
            cv.append_column(c)

        col("Categorie",  lambda r: f'<span foreground="{cc.get(r.category,"#888")}" weight="bold">{r.category}</span>', 100, True)
        col("Benchmark",  lambda r: r.name, 165)
        col("Score",      lambda r: f"{r.score:.3f}" if r.score is not None else "—", 95)
        col("Unite",      lambda r: r.unit, 80)
        col("Duree (s)",  lambda r: f"{r.duration_s:.2f}", 80)
        col("Statut",     lambda r: f'<span foreground="{"#a6e3a1" if r.status=="ok" else "#f38ba8" if r.status=="error" else "#f9e2af"}">{r.status.upper()}</span>', 70, True)
        col("Horodatage", lambda r: r.timestamp[:19], 150)
        col("Commande",   lambda r: getattr(r, "command", "") or r.metrics.get("command", "") or "—", 250)

        sc = Gtk.ScrolledWindow(); sc.set_child(cv); sc.set_vexpand(True)
        self.append(sc)

        self._det_buf = Gtk.TextBuffer()
        dv = Gtk.TextView(buffer=self._det_buf)
        dv.set_editable(False); dv.set_cursor_visible(False)
        dv.add_css_class("log-view"); dv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ds = Gtk.ScrolledWindow(); ds.set_child(dv); ds.set_size_request(-1, 130)
        self.append(ds)

        note_lbl = Gtk.Label(label="Notes du test sélectionné")
        note_lbl.add_css_class("params-title"); note_lbl.set_halign(Gtk.Align.START)
        self.append(note_lbl)

        self._notes_buf = Gtk.TextBuffer()
        self._notes_buf.connect("changed", self._on_notes_changed)
        nv = Gtk.TextView(buffer=self._notes_buf)
        nv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        nv.set_vexpand(False)
        nv.add_css_class("log-view")
        ns = Gtk.ScrolledWindow(); ns.set_child(nv); ns.set_size_request(-1, 95)
        self.append(ns)

    def _get_meta(self):
        try:
            return {
                "system_name": self._system_name_entry.get_text().strip(),
                "operator": self._operator_entry.get_text().strip(),
                "client": self._client_entry.get_text().strip(),
                "ambient_temp": self._ambient_temp_entry.get_text().strip(),
                "fan_speed": self._fan_speed_entry.get_text().strip(),
                "order_no": self._order_entry.get_text().strip(),
                "serial": self._serial_entry.get_text().strip(),
                "model": self._model_entry.get_text().strip(),
            }
        except Exception:
            return {}

    def _detect_identity(self, _):
        """Détecte modèle et n° de série (DMI) dans un thread, sans bloquer l'interface."""
        def work():
            try:
                from core.identity import collect_identity, best_serial
                ident = collect_identity()
                serial = best_serial(ident)
                model = " ".join(x for x in (ident.get("vendor"), ident.get("model")) if x)
            except Exception:
                serial, model = "", ""
            def apply():
                # Le bouton est une demande explicite : les valeurs détectées REMPLACENT celles déjà saisies (plus besoin d'effacer d'abord)
                if serial:
                    self._serial_entry.set_text(serial)
                if model:
                    self._model_entry.set_text(model)
                if (serial or model) and not self._system_name_entry.get_text().strip():
                    self._system_name_entry.set_text(model or serial)      # nom de la machine : seulement s'il est vide
                if not serial and not model:
                    self.win._show_dialog("Identité introuvable",
                                          "Numéro de série non lisible (essayez « sudo -v » avant de lancer BenchSuite).")
                return False
            GLib.idle_add(apply)
        threading.Thread(target=work, daemon=True).start()

    def _save_system_name(self, _):
        try:
            meta = self._get_meta()
            save_report_meta(meta)
            self.win._show_dialog("Informations enregistrées", f"Système: {meta.get('system_name')}\nOpérateur: {meta.get('operator')}\nClient: {meta.get('client')}")
        except Exception as e:
            self.win._show_dialog("Erreur", str(e))

    def add_result(self, r):
        self._results.append(r)
        self._store.append(ResultWrapper(r))
        self._sync_buttons()

    def _on_sel(self, sel, *_):
        item = sel.get_selected_item()
        self._sync_buttons()
        if not item:
            self._det_buf.set_text("")
            self._set_notes_text("")
            return
        r = item.result
        lines = [f"Benchmark  : {r.name}", f"Categorie  : {r.category}",
                 f"Score      : {r.score} {r.unit}" if r.score else "Score      : N/A",
                 f"Duree      : {r.duration_s:.3f} s", f"Statut     : {r.status}",
                 f"Horodatage : {r.timestamp}", "", "── Metriques ──"]
        for k, v in r.metrics.items(): lines.append(f"  {k:35s}: {v}")
        self._det_buf.set_text("\n".join(lines))
        self._set_notes_text(getattr(r, "notes", "") or "")

    def _clear(self, _):
        self._results = []; self._store.remove_all(); self._det_buf.set_text("")
        self._set_notes_text("")
        self._sync_buttons()

    def _delete_selected(self, _):
        if self._selection is None:
            return
        pos = self._selection.get_selected()
        if pos >= self._store.get_n_items():
            return
        self._store.remove(pos)
        if pos < len(self._results):
            del self._results[pos]
        self._det_buf.set_text("")
        self._set_notes_text("")
        if self._store.get_n_items():
            self._selection.set_selected(min(pos, self._store.get_n_items() - 1))
        self._sync_buttons()

    def _set_notes_text(self, text: str):
        if self._notes_buf is None:
            return
        self._loading_notes = True
        self._notes_buf.set_text(text or "")
        self._loading_notes = False

    def _on_notes_changed(self, _buf):
        if self._loading_notes or self._selection is None:
            return
        item = self._selection.get_selected_item()
        if not item:
            return
        start, end = self._notes_buf.get_bounds()
        item.result.notes = self._notes_buf.get_text(start, end, False)

    def _sync_buttons(self):
        has_results = bool(self._results)
        if "all" in self._btns:
            self._btns["all"].set_sensitive(has_results)
        if "delete" in self._btns:
            selected = self._selection.get_selected_item() if self._selection else None
            self._btns["delete"].set_sensitive(selected is not None)

    def _export_all(self, _):
        if not self._results: return
        results, sysinfo, meta = list(self._results), self.win.system_info_snapshot(), self._get_meta()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, report_meta=meta, system_info_data=sysinfo)
            return "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())
        self.win.export_async(work, "Export complet réussi")


# ─────────────────────────────────────────────────────────────────────────────
# Page historique
# ─────────────────────────────────────────────────────────────────────────────

class HistoryPage(Gtk.Box):
    """Historique des tests (~/.benchsuite_history.json) : sélection multiple (cases à cocher, Ctrl / Maj + clic),
    suppression groupée, export HTML de la sélection ou de tout, en arrière-plan."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self.win = win
        self.set_margin_top(20); self.set_margin_start(20)
        self.set_margin_end(20); self.set_margin_bottom(20)
        self._store = Gio.ListStore(item_type=ResultWrapper)
        self._selection = None
        self._busy = False
        self._build(); self.reload()

    def _build(self):
        hdr = Gtk.Label()
        hdr.set_markup('<span weight="bold" size="large">Historique comparatif</span>')
        hdr.set_halign(Gtk.Align.START)
        self.append(hdr)
        # Barre d'actions sur sa propre ligne (le titre à côté la faisait déborder) : sélection à gauche, export à droite
        top = Gtk.Box(spacing=8)

        btn_reload = Gtk.Button(label="↻ Recharger")
        btn_reload.connect("clicked", lambda _: self.reload())
        top.append(btn_reload)

        self._btn_all = Gtk.Button(label="Tout sélectionner")
        self._btn_all.connect("clicked", self._toggle_all)
        top.append(self._btn_all)

        self._btn_delete = Gtk.Button(label="Supprimer")
        self._btn_delete.set_tooltip_text("Supprimer les tests cochés")
        self._btn_delete.add_css_class("btn-stop")
        self._btn_delete.set_sensitive(False)
        self._btn_delete.connect("clicked", self._delete_selected)
        top.append(self._btn_delete)

        self._btn_clear_all = Gtk.Button(label="🗑️ Tout l'historique")
        self._btn_clear_all.set_tooltip_text("Supprimer tout l'historique")
        self._btn_clear_all.connect("clicked", self._clear_all_confirm)
        top.append(self._btn_clear_all)

        spacer = Gtk.Box(); spacer.set_hexpand(True); top.append(spacer)
        self._spinner = Gtk.Spinner()
        top.append(self._spinner)
        self._btn_export = Gtk.Button(label="Export HTML (tout)")
        self._btn_export.add_css_class("btn-csv")
        self._btn_export.connect("clicked", self._export_html)
        top.append(self._btn_export)
        self.append(top)

        self._count = Gtk.Label(); self._count.add_css_class("history-info")
        self._count.set_halign(Gtk.Align.START); self.append(self._count)

        self._selection = Gtk.MultiSelection(model=self._store)
        self._selection.connect("selection-changed", lambda *_: self._sync_buttons())
        cv = Gtk.ColumnView(model=self._selection); cv.set_show_row_separators(True); cv.set_vexpand(True)

        # Colonne de cases à cocher, liée à l'état « sélectionné » de la ligne
        fchk = Gtk.SignalListItemFactory()

        def chk_setup(_, item):
            c = Gtk.CheckButton(); c.set_halign(Gtk.Align.CENTER); item.set_child(c)
            c.connect("toggled", lambda b, it=item: self._on_check(b, it))

        def chk_bind(_, item):
            c = item.get_child()
            c._binding = item.bind_property("selected", c, "active", GObject.BindingFlags.SYNC_CREATE)

        def chk_unbind(_, item):
            c = item.get_child()
            if getattr(c, "_binding", None):
                c._binding.unbind(); c._binding = None
        fchk.connect("setup", chk_setup); fchk.connect("bind", chk_bind); fchk.connect("unbind", chk_unbind)
        ccol = Gtk.ColumnViewColumn(title="", factory=fchk); ccol.set_fixed_width(40); cv.append_column(ccol)

        def col(title, fn, w):
            f = Gtk.SignalListItemFactory()
            def setup(_, item):
                l = Gtk.Label(); l.set_halign(Gtk.Align.CENTER); l.set_ellipsize(Pango.EllipsizeMode.END); item.set_child(l)
            def bind(_, item, _fn=fn):
                try:
                    txt = str(_fn(item.get_item().result))
                except Exception:
                    txt = "—"
                item.get_child().set_text(txt); item.get_child().set_tooltip_text(txt)
            f.connect("setup", setup); f.connect("bind", bind)
            c = Gtk.ColumnViewColumn(title=title, factory=f); c.set_fixed_width(w); cv.append_column(c)

        col("Catégorie",  lambda r: r.category, 90)
        col("Benchmark",  lambda r: r.name, 180)
        col("Score",      lambda r: f"{r.score:.3f}" if isinstance(r.score, (int, float)) else "—", 90)
        col("Unité",      lambda r: r.unit, 75)
        col("CPU",        lambda r: (r.system_info or {}).get("cpu_model", "")[:30], 210)
        col("Statut",     lambda r: r.status.upper(), 70)
        col("Horodatage", lambda r: r.timestamp[:19].replace("T", " "), 150)
        col("Commande",   lambda r: r.command or (r.metrics or {}).get("command", "") or "—", 250)

        sc = Gtk.ScrolledWindow(); sc.set_child(cv); sc.set_vexpand(True); self.append(sc)

    def _on_check(self, btn, item):
        """Case cochée / décochée → sélection de la ligne (sans désélectionner les autres)."""
        pos = item.get_position()
        if pos == Gtk.INVALID_LIST_POSITION or btn.get_active() == item.get_selected():
            return
        if btn.get_active():
            self._selection.select_item(pos, False)
        else:
            self._selection.unselect_item(pos)

    def _selected_positions(self) -> list:
        bs = self._selection.get_selection()
        return [bs.get_nth(i) for i in range(bs.get_size())]

    def reload(self):
        self._store.remove_all()
        hist = load_history()
        # Plus récent en haut ; chaque ligne garde son index dans le fichier (une entrée illisible n'en décale plus d'autres)
        for idx in range(len(hist) - 1, -1, -1):
            r = history_result(hist[idx])
            if r is None:
                continue
            w = ResultWrapper(r); w.hist_index = idx
            self._store.append(w)
        n = self._store.get_n_items()
        self._count.set_text(f"{n} entrée(s) — ~/.benchsuite_history.json · cochez plusieurs lignes (ou Ctrl / Maj + clic) "
                             "pour les supprimer ou les exporter ensemble ; sans sélection, l'export prend tout")
        self._sync_buttons()

    def _toggle_all(self, _):
        if self._selected_positions() and len(self._selected_positions()) == self._store.get_n_items():
            self._selection.unselect_all()
        else:
            self._selection.select_all()

    def _sync_buttons(self):
        n_sel = len(self._selected_positions()) if self._selection else 0
        n = self._store.get_n_items()
        self._btn_delete.set_sensitive(n_sel > 0 and not self._busy)
        self._btn_delete.set_label(f"Supprimer ({n_sel})" if n_sel else "Supprimer")
        self._btn_export.set_label(f"Export HTML ({n_sel})" if n_sel else "Export HTML (tout)")
        self._btn_export.set_tooltip_text(f"Exporter les {n_sel} test(s) cochés" if n_sel else "Exporter tout l'historique")
        self._btn_export.set_sensitive(n > 0 and not self._busy)
        self._btn_all.set_label("Tout désélectionner" if n and n_sel == n else "Tout sélectionner")
        self._btn_all.set_sensitive(n > 0)
        self._btn_clear_all.set_sensitive(n > 0 and not self._busy)

    def _confirm(self, title: str, markup: str, ok_label: str, on_ok):
        """Petite fenêtre de confirmation (Annuler / action)."""
        dlg = Gtk.Window(title=title, transient_for=self.win, modal=True)
        dlg.set_default_size(400, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(20); box.set_margin_bottom(20); box.set_margin_start(24); box.set_margin_end(24)
        lbl = Gtk.Label(); lbl.set_markup(markup); lbl.set_wrap(True); lbl.set_halign(Gtk.Align.START)
        box.append(lbl)
        bb = Gtk.Box(spacing=12); bb.set_halign(Gtk.Align.END)
        b1 = Gtk.Button(label="Annuler"); b1.connect("clicked", lambda _: dlg.destroy())
        b2 = Gtk.Button(label=ok_label); b2.add_css_class("btn-stop")
        b2.connect("clicked", lambda _: (dlg.destroy(), on_ok()))
        bb.append(b1); bb.append(b2); box.append(bb)
        dlg.set_child(box); dlg.present()

    def _clear_all_confirm(self, _):
        n = self._store.get_n_items()
        if n == 0:
            return

        def do():
            clear_history()
            self.reload()
            self.win._set_status("Historique entièrement supprimé")
        self._confirm("Confirmer la suppression",
                      f"<b>Supprimer la totalité de l'historique ?</b>\n\nLes <b>{n} entrée(s)</b> de "
                      "<tt>~/.benchsuite_history.json</tt> seront effacées définitivement.", "🗑️ Tout supprimer", do)

    def _delete_selected(self, _):
        pos = self._selected_positions()
        if not pos:
            return
        idx = [self._store.get_item(p).hist_index for p in pos]

        def do():
            k = delete_history_indices(idx)
            self.reload()
            self.win._set_status(f"{k} entrée(s) supprimée(s) de l'historique")
        if len(idx) == 1:
            do()
        else:
            self._confirm("Confirmer la suppression",
                          f"<b>Supprimer les {len(idx)} tests sélectionnés ?</b>\n\nCette suppression est définitive.",
                          f"🗑️ Supprimer ({len(idx)})", do)

    def _export_html(self, _):
        """Export HTML de la sélection (ou de tout l'historique) dans un thread : l'interface ne se fige plus sur un gros
        historique, et une erreur est affichée au lieu d'un bouton qui semble ne rien faire."""
        if self._busy:
            return
        pos = self._selected_positions()
        results = ([self._store.get_item(p).result for p in pos] if pos else
                   [self._store.get_item(i).result for i in range(self._store.get_n_items())])
        if not results:
            self.win._show_dialog("Vide", "Aucun historique."); return
        results = sorted(results, key=lambda r: r.timestamp or "")      # ordre chronologique dans le rapport
        self._busy = True; self._sync_buttons(); self._spinner.start()
        self.win._set_status(f"Export HTML de {len(results)} test(s)…")

        def done(path, err):
            self._busy = False; self._spinner.stop(); self._sync_buttons()
            if err:
                self.win._set_status("Export HTML de l'historique : échec")
                self.win._show_dialog("Export HTML — erreur", err)
            else:
                self.win._set_status(f"Export HTML : {path}")
                self.win._show_dialog("Export HTML", f"{len(results)} test(s) exporté(s).\n\nFichier :\n{path}")
            return False

        def work():
            try:
                os.makedirs(OUTPUT_DIR, exist_ok=True)
                name = "Historique" + (f" — {len(results)} test(s) sélectionné(s)" if pos else " global")
                path = export_html(results, OUTPUT_DIR, report_meta={"system_name": name})
                GLib.idle_add(done, path, "")
            except Exception as e:
                import traceback
                log = os.path.join(OUTPUT_DIR, "export_historique_erreur.log")
                try:
                    with open(log, "w", encoding="utf-8") as f:
                        f.write(traceback.format_exc())
                except Exception:
                    log = ""
                GLib.idle_add(done, "", f"{type(e).__name__} : {e}" + (f"\n\nDétail : {log}" if log else ""))
        threading.Thread(target=work, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Page installation
# ─────────────────────────────────────────────────────────────────────────────

from ui.install_page import InstallPage   # page « Outils & dépendances » (catalogue core/tools_catalog.py)


# ─────────────────────────────────────────────────────────────────────────────
# Fenêtre principale
# ─────────────────────────────────────────────────────────────────────────────

# Ordre des tests dans le menu : chauffe / stabilité / erreurs d'abord, performance ensuite (AGENTS.md §1 bis).
_NAV_ORDER = ["cpu_stress", "ram_stream",
              "storage_fio_nvme", "storage_fio_hdd",
              "gpu_burn", "gpu_fryer", "gpu_oclburn", "gpu_dcgm", "gpu_vram_test",
              "gpu_clpeak", "gpu_vkpeak", "gpu_vram_bw", "gpu_nvbandwidth",
              "axelera_stress",
              "net_iperf_tcp", "net_iperf_udp"]

# Sous-groupes du menu pour les catégories chargées (GPU : 9 tests + 2 pages de configuration).
_NAV_SUB = {"GPU": {"gpu_burn": "Chauffe / stabilité", "gpu_fryer": "Chauffe / stabilité", "gpu_oclburn": "Chauffe / stabilité",
                    "gpu_dcgm": "Erreurs / diagnostic", "gpu_vram_test": "Erreurs / diagnostic",
                    "gpu_clpeak": "Performance", "gpu_vkpeak": "Performance", "gpu_vram_bw": "Performance",
                    "gpu_nvbandwidth": "Performance"}}

# Noms courts pour le menu (le groupe dit déjà la catégorie) ; le nom complet reste en info-bulle et sur la page.
_NAV_SHORT = {"CPU + RAM": "Burn CPU + RAM", "RAM — Bande passante (STREAM)": "Bande passante RAM",
              "GPU Compute fryer": "gpu-fryer", "GPU gpu-burn": "gpu-burn", "GPU nvbandwidth": "nvbandwidth",
              "Axelera — Chauffe & Stabilité AIPU": "Test & modèles AIPU"}


def _nav_short_name(name: str, cat: str) -> str:
    """Retire du libellé le préfixe de sa catégorie (« GPU — », « Stockage », « Réseau ») : le groupe l'indique déjà."""
    if name in _NAV_SHORT:
        return _NAV_SHORT[name]
    for pfx in (cat.split()[0] + " — ", cat.split()[0] + " "):
        if name.startswith(pfx) and len(name) > len(pfx) + 2:
            return name[len(pfx):]
    return name


class BenchWindow(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app)
        self.set_title(APP_NAME)
        self.set_icon_name(APP_ID)
        from ui.screen import fit_size, is_small_screen, is_tiny_screen
        self.set_default_size(*fit_size(1280, 820))       # jamais plus grande que l'écran
        self._small_screen = is_small_screen()
        if self._small_screen:
            self.add_css_class("compact")                 # marges / polices réduites (voir CSS « .compact »)
        if is_tiny_screen():
            self.maximize()                               # 800x600, 1024x768… : toute la surface disponible
        self._bench_pages: dict[str, BenchPage] = {}
        self._nav_btns:    dict[str, Gtk.Button] = {}
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        self._build_ui()
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, *args):
        """Fermeture : arrête les tests en cours ET leurs processus.

        Avant, on levait seulement le drapeau d'arrêt puis Python quittait : les threads daemon mouraient avant d'avoir
        tué leurs processus, et gpu-burn / stress-ng / fio continuaient de tourner sans interface. On attend donc
        (8 s au plus) que les tests s'arrêtent proprement, puis on termine les processus fils restants."""
        workers = []
        pages = list(self._bench_pages.values()) + [getattr(self, n, None) for n in (
            "_cpu_page", "_ssd_page", "_nvidia_page", "_intel_page", "_iperf_page", "_dmesg_page", "_sysinfo_page")]
        for pg in pages:
            w = getattr(pg, "worker", None) if pg is not None else None
            try:
                if w is not None and w.is_alive():
                    w.stop()
                    workers.append(w)
            except Exception:
                pass
        seq = getattr(self, "_sequence_page", None)
        try:
            if seq is not None and getattr(seq, "_runner", None):
                seq._runner.request_stop()
        except Exception:
            pass
        deadline = time.time() + 8
        for w in workers:
            try:
                w.join(timeout=max(0.0, deadline - time.time()))
            except Exception:
                pass
        # Dernier recours : processus fils encore vivants (outil bloqué, séquence en cours, serveur iperf3…)
        try:
            import psutil
            kids = psutil.Process().children(recursive=True)
            for k in kids:
                try:
                    k.terminate()
                except Exception:
                    pass
            _gone, alive = psutil.wait_procs(kids, timeout=3)
            for k in alive:
                try:
                    k.kill()
                except Exception:
                    pass
        except Exception:
            pass
        return False        # laisser la fenêtre se fermer

    def _build_ui(self):
        # ── HeaderBar natif — fenetre deplacable + boutons WM ──
        hb = Adw.HeaderBar()
        hb.set_show_end_title_buttons(True)    # boutons fermer/reduire/agrandir
        hb.set_show_start_title_buttons(False)

        title_box = Gtk.Box(spacing=8)
        title_box.append(Gtk.Label(label="⚡"))
        tl = Gtk.Label(); tl.set_markup(f'<b>{APP_NAME}</b>'); title_box.append(tl)
        vl = Gtk.Label(); vl.set_markup(f'<span foreground="#6c7086" size="small">v{get_full_version_display()}</span>'); title_box.append(vl)
        hb.set_title_widget(title_box)

        btn_sys = Gtk.Button(label="Infos système")
        btn_sys.set_tooltip_text("Ouvrir la page complète des informations système")
        btn_sys.connect("clicked", lambda *_: self._nav_click(None, "system_info"))
        hb.pack_end(btn_sys)

        # Thème : sombre / clair / automatique (mémorisé)
        app_ = self.get_application()
        if app_ is not None and hasattr(app_, "cycle_theme"):
            btn_theme = Gtk.Button(label=theme.LABELS.get(app_.theme_mode, theme.LABELS["dark"]))
            btn_theme.set_tooltip_text("Thème de l'interface : sombre → clair → automatique (suit le système)")
            btn_theme.connect("clicked", lambda *_: app_.cycle_theme())
            app_._theme_buttons.append(btn_theme)
            hb.pack_end(btn_theme)

        # Bouton « menu » : masque / affiche le menu latéral (indispensable sur petit écran, où il prend 25 à 30 % de la largeur)
        from ui.screen import is_tiny_screen as _tiny
        self._btn_menu = Gtk.ToggleButton(label="☰")
        self._btn_menu.set_tooltip_text("Afficher / masquer le menu latéral")
        self._btn_menu.set_active(not _tiny())     # très petit écran : menu masqué au démarrage (l'accueil donne accès aux tests)
        hb.pack_start(self._btn_menu)

        # ── Corps : sidebar + stack ──
        hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        hbox.set_vexpand(True)

        sidebar = self._build_sidebar()
        hbox.append(sidebar)
        sidebar.set_visible(self._btn_menu.get_active())
        self._btn_menu.connect("toggled", lambda b: sidebar.set_visible(b.get_active()))
        self._btn_menu.set_tooltip_text("Afficher / masquer le menu latéral (F9)")

        # Raccourcis : Ctrl+F = rechercher dans le menu (l'affiche s'il est masqué), F9 = afficher / masquer le menu
        def _focus_search(*_a):
            self._btn_menu.set_active(True)
            self._nav_search.grab_focus()
            return True
        sc = Gtk.ShortcutController()
        sc.set_scope(Gtk.ShortcutScope.GLOBAL)
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("<Control>f"), Gtk.CallbackAction.new(_focus_search)))
        sc.add_shortcut(Gtk.Shortcut.new(Gtk.ShortcutTrigger.parse_string("F9"), Gtk.CallbackAction.new(
            lambda *_a: (self._btn_menu.set_active(not self._btn_menu.get_active()), True)[1])))
        self.add_controller(sc)

        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(100)
        self.stack.set_hexpand(True); self.stack.set_vexpand(True)
        # Par défaut un Gtk.Stack prend la taille de sa page la PLUS LARGE (1700+ px ici) : la fenêtre ne pouvait plus rétrécir.
        # Non homogène + défilement : seule la page affichée compte, et un écran trop petit obtient des barres de défilement.
        self.stack.set_hhomogeneous(False); self.stack.set_vhomogeneous(False)
        stack_scroll = Gtk.ScrolledWindow()
        stack_scroll.set_hexpand(True); stack_scroll.set_vexpand(True)
        stack_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        stack_scroll.set_overlay_scrolling(False)   # barres de défilement toujours visibles quand la page déborde (sinon elles restent cachées)
        stack_scroll.set_child(self.stack)
        hbox.append(stack_scroll)

        # ── Statusbar ──
        self._status = Gtk.Label(label="Pret — selectionnez un benchmark dans le menu")
        self._status.set_halign(Gtk.Align.START); self._status.set_hexpand(True)
        sbar = Gtk.Box(); sbar.add_css_class("statusbar"); sbar.append(self._status)

        # ── Root ──
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.append(hb); root.append(hbox); root.append(sbar)
        self.set_content(root)

        # ── Pages benchmark ──
        from ui.fio_page import FioPage
        from ui.stress_page import StressPage
        from ui.axelera_page import AxeleraPage
        from ui.system_info_page import SystemInfoPage
        sorted_cat = sorted(CATALOG,
                            key=lambda b: CAT_ORDER.index(b["category"]) if b["category"] in CAT_ORDER else 99)
        for bd in sorted_cat:
            if bd["id"] == "storage_fio_nvme":
                page = FioPage(self, disk_filter="nvme")
            elif bd["id"] == "storage_fio_hdd":
                page = FioPage(self, disk_filter="hdd")
            elif bd["id"] == "cpu_stress":
                page = StressPage(self)
            elif bd["id"] == "axelera_stress":
                page = AxeleraPage(self)
            else:
                page = BenchPage(bd, self)
            self._bench_pages[bd["id"]] = page
            self.stack.add_named(page, f"bench_{bd['id']}")

        from ui.nvidia_page import NvidiaPage
        from ui.intel_page import IntelPage
        from ui.iperf_page import IperfServerPage
        from ui.cpu_page import CpuPage
        from ui.ssd_page import SsdPage
        from ui.dmesg_page import DmesgPage
        self._nvidia_page   = NvidiaPage(self)
        self._intel_page    = IntelPage(self)
        self._iperf_page    = IperfServerPage(self)
        self._cpu_page      = CpuPage(self)
        self._ssd_page      = SsdPage(self)
        self._dmesg_page    = DmesgPage(self)
        self._results_page  = ResultsPage(self)
        self._history_page  = HistoryPage(self)
        self._sysinfo_page  = SystemInfoPage(self)
        from ui.sequence_page import SequencePage
        self._sequence_page = SequencePage(self)
        self.stack.add_named(self._sequence_page, "sequence")
        self.stack.add_named(self._cpu_page,      "cpu_config")
        self.stack.add_named(self._nvidia_page,   "nvidia_config")
        self.stack.add_named(self._intel_page,    "intel_config")
        self.stack.add_named(self._iperf_page,    "iperf_server")
        self.stack.add_named(self._ssd_page,      "ssd_check")
        self.stack.add_named(self._dmesg_page,    "dmesg_live")
        self.stack.add_named(self._sysinfo_page,  "system_info")
        self.stack.add_named(self._results_page,  "resultats")
        self.stack.add_named(self._history_page,  "historique")
        self.stack.add_named(InstallPage(),        "installation")
        self.stack.add_named(self._build_home(),   "home")
        self.stack.set_visible_child_name("home")

    def system_info_snapshot(self) -> dict:
        try:
            snap = self._sysinfo_page.get_data()
        except Exception:
            snap = None
        return snap

    def _build_home(self):
        """Accueil orienté action : actions rapides, état de la machine (détecté en arrière-plan), tests cliquables, nouveautés repliées."""
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        scroll = Gtk.ScrolledWindow(); scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        inner.set_margin_top(28); inner.set_margin_start(36)
        inner.set_margin_end(36); inner.set_margin_bottom(36)
        inner.set_halign(Gtk.Align.FILL)

        total_tests = len(CATALOG)
        ready_tests = sum(1 for b in CATALOG if all(tool_exists(t) for t in b.get("requires", [])))
        missing_tests = total_tests - ready_tests

        # ── En-tête compact + actions rapides ──
        hero = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        hero.add_css_class("home-hero")
        hero.set_halign(Gtk.Align.FILL)
        top = Gtk.Box(spacing=12)
        t = Gtk.Label()
        t.set_markup(f'<span size="xx-large" weight="ultrabold" foreground="#f97316">{APP_NAME}</span>')
        t.set_halign(Gtk.Align.START)
        pill = Gtk.Label(label=f"v{get_full_version_display()}")
        pill.add_css_class("home-pill"); pill.set_valign(Gtk.Align.CENTER)
        top.append(t); top.append(pill)
        hero.append(top)
        s = Gtk.Label()
        s.set_markup('<span size="large">Bench et recette matérielle : CPU, RAM, GPU, stockage et réseau — '
                     'multi-marque, avec verdict automatique et rapports.</span>')
        s.set_wrap(True); s.set_halign(Gtk.Align.START); s.set_xalign(0)
        hero.append(s)

        quick = Gtk.Box(spacing=10)
        for label, page, css in (("▶  Lancer une séquence", "sequence", "btn-primary-big"),
                                 ("🩺  Diagnostic système", "system_info", "btn-soft"),
                                 ("🧰  Outils & dépendances", "installation", "btn-soft"),
                                 ("📈  Résultats", "resultats", "btn-soft")):
            qb = Gtk.Button(label=label); qb.add_css_class(css)
            qb.connect("clicked", self._nav_click, page)
            quick.append(qb)
        hero.append(quick)
        inner.append(hero)

        # ── État de la machine (rempli en arrière-plan : aucune commande dans le fil GTK) ──
        mt = Gtk.Label()
        mt.set_markup('<span weight="bold" size="large">Votre machine</span>')
        mt.set_halign(Gtk.Align.START)
        inner.append(mt)
        cards = Gtk.FlowBox()
        cards.set_max_children_per_line(4); cards.set_min_children_per_line(1)
        cards.set_selection_mode(Gtk.SelectionMode.NONE)
        cards.set_column_spacing(14); cards.set_row_spacing(14)
        self._home_vals = {}
        for key, icon, title, color in (("cpu", "🧠", "Processeur", "#f97316"), ("ram", "🧮", "Mémoire", "#22d3ee"),
                                        ("gpu", "🎮", "GPU", "#a78bfa"), ("tools", "🧰", "Tests prêts", "#a6e3a1")):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("home-card"); card.set_size_request(230, -1)
            l1 = Gtk.Label(); l1.set_markup(f'<span foreground="#6c7086" weight="bold" size="small">{icon}  {title.upper()}</span>')
            l1.set_halign(Gtk.Align.START); card.append(l1)
            l2 = Gtk.Label(label="détection…"); l2.set_halign(Gtk.Align.START); l2.set_xalign(0)
            l2.set_wrap(True); l2.set_selectable(False)
            l2.set_markup(f'<span foreground="{color}" weight="bold" size="large">détection…</span>')
            card.append(l2)
            l3 = Gtk.Label(); l3.set_halign(Gtk.Align.START); l3.set_xalign(0); l3.set_wrap(True)
            card.append(l3)
            self._home_vals[key] = (l2, l3, color)
            cards.append(card)
        inner.append(cards)
        l2, l3, color = self._home_vals["tools"]
        l2.set_markup(f'<span foreground="{color}" weight="bold" size="large">{ready_tests} / {total_tests}</span>')
        l3.set_markup('<span foreground="#a6adc8" size="small">' + (f"{missing_tests} test(s) à compléter : outil manquant" if missing_tests
                                                                    else "tous les outils sont présents") + '</span>')

        def _fill_home():
            """Détection du matériel (thread) puis mise à jour des cartes via GLib.idle_add."""
            try:
                info = get_system_info() or {}
            except Exception:
                info = {}
            try:
                from core.system_info import get_gpu_info
                gpus = get_gpu_info() or []
            except Exception:
                gpus = []

            def apply():
                def put(key, big, small=""):
                    lab, sub, col = self._home_vals[key]
                    lab.set_markup(f'<span foreground="{col}" weight="bold" size="large">{GLib.markup_escape_text(big)}</span>')
                    sub.set_markup(f'<span foreground="#a6adc8" size="small">{GLib.markup_escape_text(small)}</span>')
                cpu = str(info.get("cpu_model") or "inconnu")
                put("cpu", cpu if len(cpu) < 34 else cpu[:32] + "…", f"{info.get('cpu_threads', '?')} threads")
                put("ram", f"{info.get('ram_gb', '?')} Go", "")
                real = [g for g in gpus if g.get("fabricant") in ("NVIDIA", "AMD", "Intel", "Axelera")]
                if real:
                    names = sorted({(g.get("nom") or g.get("modele") or "GPU").replace("NVIDIA ", "").replace("Intel(R) ", "").replace("(TM) ", "")
                                    for g in real})
                    put("gpu", f"{len(real)} détecté(s)", ", ".join(names)[:80])
                else:
                    put("gpu", "aucun GPU", "NVIDIA / AMD / Intel / Axelera non détecté")
                return False
            GLib.idle_add(apply)

        threading.Thread(target=_fill_home, daemon=True).start()

        # ── Tests : cartes cliquables par catégorie ──
        grid = Gtk.FlowBox()
        grid.set_max_children_per_line(3); grid.set_min_children_per_line(1)
        grid.set_selection_mode(Gtk.SelectionMode.NONE)
        grid.set_column_spacing(14); grid.set_row_spacing(14)
        grid.set_halign(Gtk.Align.FILL)
        for cat in CAT_ORDER:
            color = CAT_COLORS[cat]
            items = [b for b in CATALOG if b["category"] == cat]
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(260, -1)
            cl = Gtk.Label()
            cl.set_markup(f'<span foreground="{color}" weight="bold" size="large">{self._NAV_ICONS.get(cat, "")}  {GLib.markup_escape_text(cat)}</span>')
            cl.set_halign(Gtk.Align.START); card.append(cl)
            for b in items:
                ok = all(tool_exists(t) for t in b.get("requires", []))
                tb = Gtk.Button(); tb.add_css_class("home-test")
                row = Gtk.Box(spacing=8)
                dot = Gtk.Label(); dot.set_markup(f'<span foreground="{"#a6e3a1" if ok else "#f9e2af"}">{"●" if ok else "⚠"}</span>')
                nm = Gtk.Label(label=b["name"]); nm.set_halign(Gtk.Align.START); nm.set_hexpand(True); nm.set_xalign(0)
                nm.set_ellipsize(Pango.EllipsizeMode.END)
                row.append(dot); row.append(nm)
                tb.set_child(row)
                tb.set_tooltip_text(b.get("desc", "") + ("" if ok else "\nOutil manquant : " + ", ".join(t for t in b.get("requires", []) if not tool_exists(t))))
                tb.connect("clicked", self._nav_click, f"bench_{b['id']}")
                card.append(tb)
            grid.append(card)
        catalog_title = Gtk.Label()
        catalog_title.set_markup('<span weight="bold" size="large">Tests disponibles</span> '
                                 '<span foreground="#6c7086" size="small">  ● prêt   ⚠ outil manquant — cliquez pour ouvrir</span>')
        catalog_title.set_halign(Gtk.Align.START)
        inner.append(catalog_title)
        inner.append(grid)

        # ── Nouveautés : repliées (les 6 dernières), le reste dans « versions précédentes » ──
        release_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        release_box.set_margin_top(6)

        from core.changelog import CHANGELOG_UPDATES as updates

        def _news_label(line):
            lbl = Gtk.Label()
            lbl.set_markup(f'<span foreground="#a6e3a1">✓</span> <span>{line}</span>')
            lbl.set_wrap(True); lbl.set_halign(Gtk.Align.START); lbl.set_xalign(0)
            lbl.set_margin_start(10); lbl.set_margin_bottom(4)
            return lbl

        recent = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        for line in updates[:6]:
            recent.append(_news_label(line))
        exp_news = Gtk.Expander()
        exp_news.set_label_widget(Gtk.Label(label=f"🎉  Nouveautés de la version {VERSION}"))
        exp_news.set_child(recent)
        release_box.append(exp_news)
        if len(updates) > 6:
            older = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            for line in updates[6:]:
                older.append(_news_label(line))
            exp_old = Gtk.Expander()
            exp_old.set_label_widget(Gtk.Label(label=f"Versions précédentes ({len(updates) - 6} entrées)"))
            exp_old.set_child(older)
            recent.append(exp_old)
        inner.append(release_box)

        scroll.set_child(inner); box.append(scroll)
        return box

    _NAV_ICONS = {"CPU + RAM": "🧠", "Stockage": "💾", "GPU": "🎮", "Axelera AI": "🤖", "Réseau": "🌐"}

    def _build_sidebar(self):
        """Menu latéral : recherche, groupes repliables, icônes, libellés alignés à gauche et tronqués proprement."""
        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.add_css_class("sidebar"); sidebar.set_size_request(200 if getattr(self, "_small_screen", False) else 240, -1)
        sidebar.set_hexpand(False)   # sinon l'expansion des libellés enfants fait grossir le menu au détriment de la page

        self._nav_groups: list = []            # [(bouton d'en-tête, revealer, [(bouton, texte)])]
        search = Gtk.SearchEntry()
        search.set_placeholder_text("Rechercher… (Ctrl+F)")
        self._nav_search = search
        search.add_css_class("sidebar-search")
        search.connect("search-changed", self._on_nav_search)
        sidebar.append(search)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        inner.set_margin_bottom(10)

        inner.append(self._make_nav_btn("Accueil", "home", "🏠"))
        # Séquence automatique : entrée directe (c'est le point de départ habituel, pas un groupe d'une seule entrée)
        b = self._make_nav_btn("Séquence automatique", "sequence", "▶")
        b.add_css_class("nav-primary")
        inner.append(b)
        self._nav_loose = [(b, "séquence automatique enchaîner parallèle")]

        # Par défaut, la performance GPU est repliée (chauffe / stabilité / erreurs d'abord, AGENTS.md §1 bis)
        collapsed = set(theme.get_setting("nav_collapsed", ["GPU › Performance"]) or [])

        def add_group(title, color="#8c8c8c", icon="", key=""):
            """Crée un groupe repliable (état mémorisé) et retourne (conteneur, entrées, libellé de compteur)."""
            key = key or title
            hdr = Gtk.Button()
            hdr.add_css_class("sidebar-group")
            hl = Gtk.Label(); hl.set_halign(Gtk.Align.START); hl.set_hexpand(True)
            hl.set_markup(f'<span foreground="{color}" weight="bold" size="small">{icon + " " if icon else ""}{GLib.markup_escape_text(title.upper())}</span>')
            count = Gtk.Label(); count.add_css_class("nav-count")
            arrow = Gtk.Label()
            hb = Gtk.Box(spacing=6); hb.append(hl); hb.append(count); hb.append(arrow)
            hdr.set_child(hb)
            body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            rev = Gtk.Revealer(); rev.set_child(body)
            rev.set_transition_duration(120)
            rev.set_reveal_child(key not in collapsed)
            arrow.set_text("▾" if rev.get_reveal_child() else "▸")

            def toggle(_b, rev=rev, arrow=arrow, key=key):
                rev.set_reveal_child(not rev.get_reveal_child())
                arrow.set_text("▾" if rev.get_reveal_child() else "▸")
                (collapsed.discard if rev.get_reveal_child() else collapsed.add)(key)
                theme.set_setting("nav_collapsed", sorted(collapsed))
            hdr.connect("clicked", toggle)
            inner.append(hdr); inner.append(rev)
            group = (hdr, rev, [])
            self._nav_groups.append(group)
            return body, group[2], count

        def add_subgroup(parent, title, key):
            """Sous-groupe repliable à l'intérieur d'un groupe (ex. GPU › Performance), état mémorisé comme les groupes."""
            hdr = Gtk.Button()
            hdr.add_css_class("sidebar-subgroup")
            hl = Gtk.Label(label=title); hl.set_halign(Gtk.Align.START); hl.set_hexpand(True)
            count = Gtk.Label(); count.add_css_class("nav-count")
            arrow = Gtk.Label()
            hb = Gtk.Box(spacing=6); hb.append(hl); hb.append(count); hb.append(arrow)
            hdr.set_child(hb)
            sub_body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            rev = Gtk.Revealer(); rev.set_child(sub_body); rev.set_transition_duration(120)
            rev.set_reveal_child(key not in collapsed)
            arrow.set_text("▾" if rev.get_reveal_child() else "▸")

            def toggle(_b, rev=rev, arrow=arrow, key=key):
                rev.set_reveal_child(not rev.get_reveal_child())
                arrow.set_text("▾" if rev.get_reveal_child() else "▸")
                (collapsed.discard if rev.get_reveal_child() else collapsed.add)(key)
                theme.set_setting("nav_collapsed", sorted(collapsed))
            hdr.connect("clicked", toggle)
            parent.append(hdr); parent.append(rev)
            group = (hdr, rev, [])
            self._nav_groups.append(group)
            return sub_body, group[2], count

        def add_sep(body):
            sep = Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL)
            sep.add_css_class("nav-sep")
            body.append(sep)

        # Par composant : les tests (chauffe / stabilité / erreurs d'abord, performance ensuite), puis, sous un trait,
        # les pages de configuration / diagnostic du même composant. Le nom court évite les libellés tronqués ;
        # l'info-bulle garde le nom complet et la description.
        by_cat: dict = {}
        for bd in CATALOG:
            by_cat.setdefault(bd["category"], []).append(bd)
        tools_by_cat = {
            "CPU + RAM": [("Configuration CPU", "cpu_config", "⚙", "")],
            "Stockage":  [("Diagnostic SSD / stockage", "ssd_check", "🩺", "")],
            "GPU":       [("Configuration NVIDIA", "nvidia_config", "⚙", "nvidia-smi"),
                          ("Configuration GPU Intel", "intel_config", "⚙", "")],
            "Réseau":    [("Serveur iperf3 (2 PC)", "iperf_server", "🌐", "")],
        }
        cats = [c for c in CAT_ORDER if c in by_cat] + [c for c in by_cat if c not in CAT_ORDER]
        for cat in cats:
            body, items, count = add_group(cat, CAT_COLORS.get(cat, "#8c8c8c"), self._NAV_ICONS.get(cat, ""))
            benches = sorted(by_cat[cat], key=lambda bd: (_NAV_ORDER.index(bd["id"]) if bd["id"] in _NAV_ORDER else 99))
            ready = 0
            sub_of = _NAV_SUB.get(cat, {})
            cur_sub, target, sub_items, sub_cnt = None, body, None, None
            sub_counts: dict = {}
            for bd in benches:
                missing = [t for t in bd.get("requires", []) if not tool_exists(t)]
                ready += 0 if missing else 1
                sub = sub_of.get(bd["id"])
                if sub and sub != cur_sub:            # nouveau sous-groupe (GPU : chauffe / erreurs / performance)
                    target, sub_items, sub_cnt = add_subgroup(body, sub, f"{cat} › {sub}")
                    cur_sub = sub
                short = _nav_short_name(bd["name"], cat)
                btn = self._make_nav_btn(short, f"bench_{bd['id']}", self._NAV_ICONS.get(cat, "▫"), missing=bool(missing))
                tip = bd["name"] + (f" — {bd.get('desc', '')}" if bd.get("desc") else "")
                if missing:
                    tip += f"\nOutil manquant : {', '.join(missing)} — voir « Outils & dépendances »"
                btn.set_tooltip_text(tip)
                key_txt = f"{bd['name']} {short} {bd.get('desc', '')} {sub or ''}".lower()
                target.append(btn); items.append((btn, key_txt))
                if sub_items is not None:
                    sub_items.append((btn, key_txt))
                    r, n = sub_counts.get(sub, (0, 0))
                    sub_counts[sub] = (r + (0 if missing else 1), n + 1)
                    sub_cnt.set_text(f"{sub_counts[sub][0]}/{sub_counts[sub][1]}")
            count.set_text(f"{ready}/{len(benches)}")
            count.set_tooltip_text(f"{ready} test(s) prêt(s) sur {len(benches)} (les autres attendent un outil)")
            extra = tools_by_cat.get(cat, [])
            if extra and sub_of:
                body, sub_items, _c = add_subgroup(body, "Configuration", f"{cat} › Configuration")
                items = items      # les entrées restent aussi cherchables via le groupe parent
            elif extra:
                add_sep(body)
                sub_items = None
            for label, pid, ic, need in extra:
                absent = bool(need) and not tool_exists(need)
                b = self._make_nav_btn(label, pid, ic, missing=absent)
                if absent:
                    b.set_tooltip_text(f"{label} — {need} introuvable (pilote NVIDIA absent ?)")
                body.append(b); items.append((b, f"{label} configuration réglage".lower()))
                if sub_of and sub_items is not None:
                    sub_items.append((b, f"{label} configuration réglage".lower()))

        for title, color, icon, entries in (
            ("Diagnostic", "#cba6f7", "🔧", [("Journaux noyau (dmesg)", "dmesg_live", "📜"), ("Infos système", "system_info", "🖥")]),
            ("Rapports", "#89dceb", "📊", [("Résultats de la session", "resultats", "📈"), ("Historique des exports", "historique", "🕘")]),
        ):
            body, items, count = add_group(title, color, icon)
            for label, pid, ic in entries:
                b = self._make_nav_btn(label, pid, ic)
                body.append(b); items.append((b, label.lower()))
        b = self._make_nav_btn("Outils & dépendances", "installation", "🧰")
        inner.append(b)
        self._nav_loose.append((b, "outils dépendances installation prérequis"))

        scroll.set_child(inner); sidebar.append(scroll)
        return sidebar

    def _on_nav_search(self, entry):
        """Filtre le menu : n'affiche que les entrées dont le nom ou la description contient le texte saisi."""
        q = entry.get_text().strip().lower()
        for btn, text in getattr(self, "_nav_loose", []):
            btn.set_visible((not q) or q in text)
        for hdr, rev, items in self._nav_groups:
            shown = 0
            for btn, text in items:
                vis = (not q) or q in text
                btn.set_visible(vis)
                shown += 1 if vis else 0
            hdr.set_visible(shown > 0)
            rev.set_visible(shown > 0)
            if q and shown:
                rev.set_reveal_child(True)

    def _make_nav_btn(self, label: str, page_id: str, icon: str = "▫", missing: bool = False) -> Gtk.Button:
        """Entrée de menu : icône + texte tronqué à droite si trop long (info-bulle = nom complet) + ⚠ si outil manquant."""
        btn = Gtk.Button()
        btn.add_css_class("sidebar-item")
        if missing:
            btn.add_css_class("missing")
        box = Gtk.Box(spacing=8)
        ic = Gtk.Label(label=icon); ic.add_css_class("nav-icon")
        tx = Gtk.Label(label=label); tx.add_css_class("nav-text")
        tx.set_halign(Gtk.Align.START); tx.set_hexpand(True); tx.set_ellipsize(Pango.EllipsizeMode.END)
        tx.set_xalign(0)
        box.append(ic); box.append(tx)
        if missing:
            w = Gtk.Label(label="⚠"); w.add_css_class("nav-warn"); box.append(w)
        btn.set_child(box)
        btn.set_halign(Gtk.Align.FILL)
        btn.set_tooltip_text(label)
        btn.connect("clicked", self._nav_click, page_id)
        self._nav_btns[page_id] = btn
        return btn

    def _nav_click(self, _, page_id: str):
        self.stack.set_visible_child_name(page_id)
        for pid, b in self._nav_btns.items():
            b.remove_css_class("active")
        self._nav_btns[page_id].add_css_class("active")
        if page_id == "historique":
            self._history_page.reload()

    def _on_bench_result(self, result: BenchmarkResult):
        self._results_page.add_result(result)

    def _show_dialog(self, title: str, body: str):
        dlg = Gtk.Window(title=title, transient_for=self, modal=True)
        dlg.set_default_size(360, -1)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(20); box.set_margin_bottom(20)
        box.set_margin_start(24); box.set_margin_end(24)
        lbl = Gtk.Label(label=body)
        lbl.set_wrap(True); lbl.set_halign(Gtk.Align.START)
        btn = Gtk.Button(label="OK")
        btn.add_css_class("btn-run")
        btn.connect("clicked", lambda _: dlg.destroy())
        box.append(lbl); box.append(btn)
        dlg.set_child(box)
        dlg.present()

    def _set_status(self, msg: str):
        self._status.set_text(msg)

    def export_async(self, work, ok_title: str = "Export réussi", status: str = "Export en cours…"):
        """Lance un export dans un fil : `work()` produit les fichiers et retourne le texte à afficher (ou lève une exception).

        Les exports (PDF, HTML, relevé matériel si la page Infos système n'est pas chargée) prenaient d'une à plusieurs secondes
        dans le fil de l'interface : la fenêtre se figeait. Un seul export à la fois ; le résultat ou l'erreur s'affiche à la fin."""
        if getattr(self, "_export_busy", False):
            self._set_status("Un export est déjà en cours…")
            return
        self._export_busy = True
        self._set_status(status)

        def run():
            try:
                msg, err = work(), ""
            except Exception as e:
                msg, err = "", f"{type(e).__name__} : {e}"

            def done():
                self._export_busy = False
                if err:
                    self._set_status("Export : échec")
                    self._show_dialog("Erreur export", err)
                else:
                    self._set_status("Export terminé")
                    self._show_dialog(ok_title, msg)
                return False
            GLib.idle_add(done)
        threading.Thread(target=run, daemon=True).start()


# ─────────────────────────────────────────────────────────────────────────────
# Application
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_desktop_icon() -> None:
    """Enregistre l'icône SVG de BenchSuite auprès de GTK4 et du bureau Linux (dock Ubuntu/GNOME).

    Sur GNOME (Wayland/X11), le dock associe la fenêtre au fichier `<APP_ID>.desktop` (ou `StartupWMClass`).
    Sans ce fichier et l'icône dans `~/.local/share/icons/hicolor/scalable/apps/`, Ubuntu affiche un engrenage gris.
    Best-effort : ne lève jamais d'exception.
    """
    try:
        root = os.path.dirname(os.path.abspath(__file__))
        src_svg = os.path.join(root, "assets", f"{APP_ID}.svg")
        if not os.path.isfile(src_svg):
            return
        display = Gdk.Display.get_default()
        if display is not None:
            icon_theme = Gtk.IconTheme.get_for_display(display)
            assets_dir = os.path.join(root, "assets")
            if assets_dir not in (icon_theme.get_search_path() or []):
                icon_theme.add_search_path(assets_dir)
        # Installer l'icône utilisateur et l'entrée .desktop assortie à APP_ID (idempotent)
        data_home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
        icon_dir = os.path.join(data_home, "icons", "hicolor", "scalable", "apps")
        dst_svg = os.path.join(icon_dir, f"{APP_ID}.svg")
        with open(src_svg, "rb") as f:
            svg_bytes = f.read()
        need_icon = True
        if os.path.isfile(dst_svg):
            try:
                with open(dst_svg, "rb") as f:
                    need_icon = (f.read() != svg_bytes)
            except Exception:
                need_icon = True
        if need_icon:
            os.makedirs(icon_dir, exist_ok=True)
            with open(dst_svg, "wb") as f:
                f.write(svg_bytes)
        apps_dir = os.path.join(data_home, "applications")
        desk_path = os.path.join(apps_dir, f"{APP_ID}.desktop")
        launcher = os.path.join(root, "benchsuite.sh")
        exec_cmd = f'"{launcher}"' if os.path.isfile(launcher) else f'python3 "{os.path.join(root, "main.py")}"'
        desk_content = (
            "[Desktop Entry]\n"
            "Version=1.0\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "GenericName=Benchmark Suite\n"
            "Comment=Bench et recette matérielle — CPU, RAM, GPU, Stockage, Réseau\n"
            f"Exec={exec_cmd}\n"
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "Categories=System;Monitor;Utility;\n"
            "Keywords=benchmark;performance;cpu;gpu;ram;storage;\n"
            f"StartupWMClass={APP_ID}\n"
            "StartupNotify=true\n"
        )
        need_desk = True
        if os.path.isfile(desk_path):
            try:
                with open(desk_path, "r", encoding="utf-8") as f:
                    need_desk = (f.read() != desk_content)
            except Exception:
                need_desk = True
        if need_desk:
            os.makedirs(apps_dir, exist_ok=True)
            with open(desk_path, "w", encoding="utf-8") as f:
                f.write(desk_content)
            try:
                os.chmod(desk_path, 0o755)
            except Exception:
                pass
    except Exception:
        pass


class BenchApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID, flags=Gio.ApplicationFlags.FLAGS_NONE)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        _ensure_desktop_icon()
        Gtk.Window.set_default_icon_name(APP_ID)
        css = Gtk.CssProvider()
        css.load_from_data(CSS.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        self._light_provider = None
        self._theme_buttons = []
        self.apply_theme(theme.get_mode())
        try:      # mode « auto » : suit le thème du système quand il change
            Adw.StyleManager.get_default().connect("notify::dark", lambda *_: self.apply_theme(self.theme_mode, save=False))
        except Exception:
            pass
        BenchWindow(app).present()

    # ── Thème clair / sombre ──────────────────────────────────────────────────

    theme_mode = "dark"

    def apply_theme(self, mode: str, save: bool = False):
        """Applique « dark » / « light » / « auto » : schéma Adwaita + feuille de style claire déduite de la sombre (core/theme.py)."""
        if mode not in theme.MODES:
            mode = "dark"
        self.theme_mode = mode
        try:
            sm = Adw.StyleManager.get_default()
            sm.set_color_scheme({"dark": Adw.ColorScheme.FORCE_DARK, "light": Adw.ColorScheme.FORCE_LIGHT}.get(mode, Adw.ColorScheme.DEFAULT))
            light = (not sm.get_dark()) if mode == "auto" else (mode == "light")
        except Exception:
            light = (mode == "light")
        try:
            display = Gdk.Display.get_default()
            if self._light_provider is not None:
                Gtk.StyleContext.remove_provider_for_display(display, self._light_provider)
                self._light_provider = None
            if light:
                prov = Gtk.CssProvider()
                extra = ""
                try:      # feuilles de style propres à certaines pages : même inversion
                    from ui.system_info_page import CSS_EXTRA
                    extra = CSS_EXTRA
                except Exception:
                    pass
                prov.load_from_data(theme.light_css(CSS + "\n" + extra).encode("utf-8"))
                Gtk.StyleContext.add_provider_for_display(display, prov, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION + 1)
                self._light_provider = prov
        except Exception:
            pass
        if save:
            theme.set_mode(mode)
        for b in self._theme_buttons:
            b.set_label(theme.LABELS[mode])

    def cycle_theme(self):
        """Bouton de l'en-tête : sombre → clair → auto, choix mémorisé."""
        self.apply_theme(theme.next_mode(self.theme_mode), save=True)


lock_file_fd = None

def acquire_lock() -> bool:
    global lock_file_fd
    import fcntl
    lock_file = "/tmp/benchsuite.lock"
    try:
        lock_file_fd = open(lock_file, "w")
        fcntl.flock(lock_file_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False

def show_single_instance_error():
    print("Erreur : Une autre instance de BenchSuite est déjà en cours d'exécution.", file=sys.stderr)
    try:
        gi.require_version('Gtk', '4.0')
        gi.require_version('Adw', '1')
        # Essayer de démarrer une application d'erreur temporaire
        app = Adw.Application.new("io.benchsuite.errorapp", Gio.ApplicationFlags.FLAGS_NONE)
        
        def on_activate(app):
            win = Adw.ApplicationWindow(application=app)
            win.set_title("BenchSuite")
            win.set_default_size(380, 160)
            
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
            box.set_margin_top(20)
            box.set_margin_bottom(20)
            box.set_margin_start(20)
            box.set_margin_end(20)
            
            lbl = Gtk.Label()
            lbl.set_markup("<span size='large'><b>Instance déjà active</b></span>\n\nUne autre instance de BenchSuite s'exécute actuellement.")
            lbl.set_wrap(True)
            lbl.set_halign(Gtk.Align.CENTER)
            
            btn = Gtk.Button(label="Quitter")
            btn.add_css_class("btn-run")
            btn.connect("clicked", lambda _: win.close())
            
            box.append(lbl)
            box.append(btn)
            win.set_child(box)
            win.present()
            
        app.connect("activate", on_activate)
        app.run([])
    except Exception:
        pass


def _filtrer_avertissements_theme():
    """Masque les « Gtk-WARNING : Theme parser error: gtk.css:… » d'un thème / d'un ~/.config/gtk-4.0/gtk.css étranger (souvent écrit pour GTK3 :
    -gtk-icon-effect, -GtkTreeView-… ne sont plus des propriétés GTK4). Ils sont sans effet (GTK ignore la règle) mais noient le terminal.
    Les erreurs du CSS de BenchSuite (« <data>:… ») et tous les autres messages restent affichés ; BENCHSUITE_GTK_WARNINGS=1 désactive le filtre.
    GTK4 journalise en « structuré » : un simple log_set_handler n'est pas appelé, il faut un writer GLib (une seule fois par processus)."""
    if os.environ.get("BENCHSUITE_GTK_WARNINGS") == "1":
        return
    try:
        import ctypes
        from gi.repository import GLib

        def _writer(level, fields, *args):
            try:
                for f in fields:
                    if f.key == "MESSAGE":
                        raw = ctypes.string_at(f.value, f.length) if f.length >= 0 else ctypes.string_at(f.value)
                        msg = raw.decode("utf-8", "replace")
                        if msg.startswith("Theme parser error: ") and "<data>" not in msg[:48]:
                            return GLib.LogWriterOutput.HANDLED
                        break
            except Exception:
                pass
            return GLib.log_writer_default(level, fields, None)

        GLib.log_set_writer_func(_writer, None)
    except Exception:
        pass            # sans filtre : simples avertissements dans le terminal


def main():
    _filtrer_avertissements_theme()
    if not acquire_lock():
        show_single_instance_error()
        return 1
    return BenchApp().run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
