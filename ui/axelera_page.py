"""
axelera_page.py — Interface graphique GTK4 / Libadwaita pour tester et surveiller les cartes Axelera AI (Metis).

Fonctionnalités :
- Détection des cartes Axelera Metis (PCIe 1f9d + pilote noyau `metis` + `axdevice`) et configuration du dossier Voyager SDK.
- Télémétrie en direct sans blocage GTK : température (cœur / 5 sondes), puissance (INA236), horloge MHz,
  utilisation MVM %, mémoire DDR, KPS et lien PCIe (Gen/largeur).
- Chargement et exécution de modèles IA (`axrunmodel` en boucle chauffe/stabilité ou `inference.py` sur média/vidéo),
  téléchargement explicite d'un modèle Zoo (`axdownloadmodel`), sélection multi-cartes et cœurs AIPU.
- Graphique temps réel (gardé par cairo), sortie console en direct, sauvegarde dans l'historique et exports CSV/JSON/PDF/HTML.
- Réglages carte (`axdevice` : horloge, limite MVM, throttling logiciel, limite de puissance 113m, `--refresh`).
"""

from __future__ import annotations

import os
import re
import threading
from typing import Dict, List, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

try:
    gi.require_foreign("cairo")
    _HAS_CAIRO = True
except Exception:
    _HAS_CAIRO = False

from core import axelera_aipu as ax
from core.bench_axelera import bench_axelera_stress
from core.bench_utils import forget_tool_cache
from core.result import BenchmarkResult
from exporters.export import export_all, load_report_meta, save_to_history

OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")
_CAT_COLOR = "#38bdf8"


def _hex_rgb(h: str):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _fmt(val, unit: str = "", nd: int = 1, default: str = "—") -> str:
    if val is None or val == "":
        return default
    try:
        return f"{float(val):.{nd}f}{unit}"
    except (TypeError, ValueError):
        return f"{val}{unit}"


# ─────────────────────────────────────────────────────────────────────────────
# Graphique temps réel (température, puissance, horloge, MVM %, KPS)
# ─────────────────────────────────────────────────────────────────────────────

class AxeleraLiveChart(Gtk.DrawingArea):
    """Courbe temps réel des métriques Axelera AIPU pendant un test."""

    SERIES_DEF = [
        ("temp",          "Temp °C",      _hex_rgb("f38ba8")),
        ("power",         "Puissance W",  _hex_rgb("fab387")),
        ("freq_graphics", "Horloge MHz",  _hex_rgb("89b4fa")),
        ("mvm_util",      "MVM %",        _hex_rgb("a6e3a1")),
        ("kps",           "KPS",          _hex_rgb("cba6f7")),
    ]

    def __init__(self, title: str = "Télémétrie Axelera AIPU — Temp · Puissance · Horloge · MVM (temps réel)"):
        super().__init__()
        self._title = title
        self._pts: Dict[str, List] = {"t": []}
        for key, _, _ in self.SERIES_DEF:
            self._pts[key] = []
        self.set_size_request(-1, 175)
        self.set_hexpand(True)
        if _HAS_CAIRO:
            self.set_draw_func(self._draw_func)

    def reset(self) -> None:
        self._pts = {"t": []}
        for key, _, _ in self.SERIES_DEF:
            self._pts[key] = []
        self.queue_draw()

    def push(self, point: dict) -> bool:
        self._pts["t"].append(point.get("t", 0))
        n_t = len(self._pts["t"])
        for key, _, _ in self.SERIES_DEF:
            lst = self._pts.setdefault(key, [])
            if len(lst) < n_t - 1:
                lst.extend([None] * (n_t - 1 - len(lst)))
            lst.append(point.get(key))
        self.queue_draw()
        return False

    def _range_for_key(self, key: str, vals: List[float]):
        clean = [float(v) for v in vals if v is not None]
        if not clean:
            return 0.0, 1.0
        vmax = max(clean)
        vmin = min(clean)
        if "mvm" in key:
            return 0.0, 100.0
        if "temp" in key:
            return 0.0, max(120.0, ((vmax + 19) // 20) * 20)
        if "power" in key:
            return 0.0, max(25.0, ((vmax + 9) // 10) * 10)
        if "freq" in key:
            return 0.0, max(900.0, ((vmax + 199) // 200) * 200)
        if vmax == vmin:
            return 0.0, vmax + 1.0
        pad = (vmax - vmin) * 0.12
        return max(0.0, vmin - pad), vmax + pad

    def _draw_func(self, _area, cr, w: int, h: int) -> None:
        if not _HAS_CAIRO:
            return
        cr.set_source_rgb(*_hex_rgb("1e1e2e"))
        cr.paint()
        t_vals = self._pts.get("t", [])
        cr.select_font_face("Sans", 0, 0)
        cr.set_source_rgb(*_hex_rgb("cdd6f4"))
        cr.set_font_size(11)
        cr.move_to(10, 16)
        cr.show_text(self._title)

        if len(t_vals) < 2:
            cr.set_source_rgb(*_hex_rgb("45475a"))
            cr.set_font_size(11)
            cr.move_to(max(10, w / 2 - 75), h / 2 + 4)
            cr.show_text("En attente de données…")
            return

        ml, mr, mt, mb = 54, 18, 26, 42
        pw, ph = max(1, w - ml - mr), max(1, h - mt - mb)
        t_min, t_max = t_vals[0], max(t_vals[-1], t_vals[0] + 1)

        cr.set_source_rgba(*_hex_rgb("313244"), 0.85)
        cr.set_line_width(1)
        cr.rectangle(ml, mt, pw, ph)
        cr.stroke()
        cr.set_source_rgba(*_hex_rgb("45475a"), 0.45)
        cr.set_line_width(0.5)
        for i in range(1, 4):
            y = mt + ph * i / 4
            cr.move_to(ml, y)
            cr.line_to(w - mr, y)
            cr.stroke()
        for i in range(1, 5):
            x = ml + pw * i / 5
            cr.move_to(x, mt)
            cr.line_to(x, mt + ph)
            cr.stroke()

        visible = []
        for key, lbl, col in self.SERIES_DEF:
            vals = self._pts.get(key, [])
            clean = [v for v in vals if v is not None]
            if not clean or all(float(v) == 0 for v in clean):
                continue
            visible.append((key, lbl, col, vals))

        for key, _lbl, col, vals in visible:
            n = min(len(t_vals), len(vals))
            if n < 2:
                continue
            step = max(1, -(-n // 600))
            idx = list(range(0, n, step))
            if idx[-1] != n - 1:
                idx.append(n - 1)
            vmin, vmax = self._range_for_key(key, [vals[i] for i in idx])
            if vmax <= vmin:
                vmax = vmin + 1.0
            cr.set_source_rgb(*col)
            cr.set_line_width(2.0)
            first = True
            for i in idx:
                v = vals[i]
                if v is None:
                    continue
                try:
                    fv = float(v)
                except Exception:
                    continue
                x = ml + pw * (t_vals[i] - t_min) / (t_max - t_min)
                y = mt + ph * (1.0 - (fv - vmin) / (vmax - vmin))
                y = max(mt, min(mt + ph, y))
                if first:
                    cr.move_to(x, y)
                    first = False
                else:
                    cr.line_to(x, y)
            cr.stroke()

        cr.set_source_rgb(*_hex_rgb("6c7086"))
        cr.set_font_size(9)
        cr.move_to(ml, h - 8)
        cr.show_text("0s")
        lbl_end = f"{int(t_vals[-1])}s"
        cr.move_to(w - mr - max(24, len(lbl_end) * 5), h - 8)
        cr.show_text(lbl_end)

        lx, ly = ml, h - mb + 12
        for _key, lbl, col, vals in visible:
            last = next((v for v in reversed(vals) if v is not None), None)
            if last is None:
                continue
            text = f"{lbl}: {float(last):.1f}" if float(last) < 20 else f"{lbl}: {float(last):.0f}"
            est_w = 14 + len(text) * 5.4
            if lx + est_w > w - mr:
                lx = ml
                ly += 13
            cr.set_source_rgb(*col)
            cr.rectangle(lx, ly - 8, 8, 7)
            cr.fill()
            cr.set_source_rgb(*_hex_rgb("cdd6f4"))
            cr.move_to(lx + 11, ly)
            cr.show_text(text[:34])
            lx += est_w + 12


# ─────────────────────────────────────────────────────────────────────────────
# Worker d'exécution
# ─────────────────────────────────────────────────────────────────────────────

class AxeleraWorker(threading.Thread):
    """Exécute `bench_axelera_stress` dans un fil dédié avec arrêt propre."""

    def __init__(self, params: dict, on_progress, on_done, on_series, on_log):
        super().__init__(daemon=True)
        self._params = params
        self._on_progress = on_progress
        self._on_done = on_done
        self._on_series = on_series
        self._on_log = on_log
        self._kill_event = threading.Event()

    def stop(self) -> None:
        self._kill_event.set()

    def run(self) -> None:
        def cb(pct, msg):
            if not self._kill_event.is_set():
                GLib.idle_add(self._on_progress, pct, msg)

        def series_cb(pt):
            if not self._kill_event.is_set():
                GLib.idle_add(self._on_series, pt)

        def log_cb(line):
            GLib.idle_add(self._on_log, line)

        try:
            result = bench_axelera_stress(
                cb=cb,
                series_cb=series_cb,
                kill_event=self._kill_event,
                log_cb=log_cb,
                **self._params,
            )
        except Exception as exc:
            result = BenchmarkResult(
                "Axelera — Chauffe & Stabilité AIPU",
                "Axelera AI",
                None,
                "FPS",
                0.0,
                str(exc),
                status="error",
                notes=str(exc),
            )
        GLib.idle_add(self._on_done, result)


# ─────────────────────────────────────────────────────────────────────────────
# Page GTK4 Axelera AI
# ─────────────────────────────────────────────────────────────────────────────

class AxeleraPage(Gtk.Box):
    """Page dédiée Axelera AI : détection, télémétrie live, chargement de modèles, test et réglages."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.win = win
        self.worker: Optional[AxeleraWorker] = None
        self.result: Optional[BenchmarkResult] = None
        self._devices: List[Dict] = []
        self._models: List[Dict] = []
        self._medias: List[str] = []
        self._last_status: Dict = {}
        self._target_checks: List = []
        self._refresh_tag: Optional[int] = None
        self._reading = False
        self._detected_once = False
        self._setting_model_combo = False
        self._build_ui()
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._stop_monitoring())

    # ── Helpers UI ────────────────────────────────────────────────────────────

    def _lbl(self, text: str, css: str = "bench-subtitle", halign=Gtk.Align.START) -> Gtk.Label:
        l = Gtk.Label(label=text, halign=halign)
        if css:
            l.add_css_class(css)
        return l

    def _pick_path(self, title: str, folder: bool, callback, initial_dir: Optional[str] = None) -> None:
        from gi.repository import Gio
        if hasattr(Gtk, "FileDialog"):
            dlg = Gtk.FileDialog()
            dlg.set_title(title)
            if initial_dir and os.path.isdir(initial_dir):
                try:
                    dlg.set_initial_folder(Gio.File.new_for_path(initial_dir))
                except Exception:
                    pass

            def _done(d, res):
                try:
                    f = d.select_folder_finish(res) if folder else d.open_finish(res)
                    GLib.idle_add(callback, f.get_path() if f else None)
                except Exception:
                    GLib.idle_add(callback, None)

            if folder:
                dlg.select_folder(self.win, None, _done)
            else:
                dlg.open(self.win, None, _done)
        else:
            action = Gtk.FileChooserAction.SELECT_FOLDER if folder else Gtk.FileChooserAction.OPEN
            dlg = Gtk.FileChooserNative(
                title=title,
                transient_for=self.win,
                action=action,
                accept_label="Sélectionner",
                cancel_label="Annuler",
            )
            if initial_dir and os.path.isdir(initial_dir):
                try:
                    dlg.set_current_folder(Gio.File.new_for_path(initial_dir))
                except Exception:
                    pass

            def _resp(d, resp):
                if resp == Gtk.ResponseType.ACCEPT:
                    f = d.get_file()
                    GLib.idle_add(callback, f.get_path() if f else None)
                else:
                    GLib.idle_add(callback, None)
                d.destroy()

            dlg.connect("response", _resp)
            dlg.show()

    # ── Construction UI ───────────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # ── En-tête FIXE ──
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        header.add_css_class("page-header")

        row = Gtk.Box(spacing=12)
        col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        col.set_hexpand(True)
        title = Gtk.Label(label="Axelera AI — Test & Télémétrie AIPU")
        title.add_css_class("bench-title")
        title.set_halign(Gtk.Align.START)
        sub = Gtk.Label(
            label="Chargement de modèles IA, test de chauffe / stabilité (axrunmodel / inference.py), "
                  "suivi températures, puissance INA236, horloges, MVM et réglages axdevice."
        )
        sub.add_css_class("bench-subtitle")
        sub.set_halign(Gtk.Align.START)
        sub.set_wrap(True)
        col.append(title)
        col.append(sub)
        badge = Gtk.Label()
        badge.set_markup(f'<span foreground="{_CAT_COLOR}" background="{_CAT_COLOR}22" weight="bold"> Axelera AI </span>')
        badge.add_css_class("status-badge")
        badge.set_valign(Gtk.Align.START)
        row.append(col)
        row.append(badge)
        header.append(row)

        # Bandeau SDK / pilote / avertissement
        self._warn_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._warn_box.add_css_class("warn-card")
        wh = Gtk.Box(spacing=8)
        wi = Gtk.Label()
        wi.set_markup('<span foreground="#f9e2af" size="large">⚠</span>')
        self._warn_lbl = Gtk.Label(halign=Gtk.Align.START, wrap=True, hexpand=True, xalign=0)
        wh.append(wi)
        wh.append(self._warn_lbl)
        self._warn_box.append(wh)

        wb = Gtk.Box(spacing=8)
        btn_sdk = Gtk.Button(label="📁 Dossier Voyager SDK / venv…")
        btn_sdk.connect("clicked", self._on_browse_sdk)
        self._btn_copy_diag = Gtk.Button(label="📋 Copier commande installation SDK")
        self._btn_copy_diag.connect("clicked", self._on_copy_install)
        wb.append(btn_sdk)
        wb.append(self._btn_copy_diag)
        self._warn_box.append(wb)
        self._warn_box.set_visible(False)
        header.append(self._warn_box)

        # Barre d'actions fixe
        br = Gtk.Box(spacing=10)
        self._btn_run = Gtk.Button(label="▶  Lancer le test")
        self._btn_run.add_css_class("btn-run")
        self._btn_run.set_hexpand(True)
        self._btn_run.connect("clicked", self._on_run)

        self._btn_stop = Gtk.Button(label="⏹  Stopper")
        self._btn_stop.add_css_class("btn-stop")
        self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)

        self._btn_refresh = Gtk.Button(label="🔄  Actualiser")
        self._btn_refresh.set_tooltip_text("Re-détecter les cartes Axelera, le SDK Voyager et les modèles")
        self._btn_refresh.connect("clicked", lambda *_: self._trigger_discovery(force=True))

        self._btn_export = Gtk.Button(label="💾  Exporter")
        self._btn_export.set_tooltip_text("Exporter CSV + JSON + PDF + HTML")
        self._btn_export.add_css_class("btn-json")
        self._btn_export.set_sensitive(False)
        self._btn_export.connect("clicked", self._on_export_all)

        br.append(self._btn_run)
        br.append(self._btn_stop)
        br.append(self._btn_refresh)
        br.append(self._btn_export)
        header.append(br)
        self.append(header)

        # ── Corps défilant ──
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        box.set_margin_top(18)
        box.set_margin_start(28)
        box.set_margin_end(28)
        box.set_margin_bottom(24)

        # 1. État SDK & Cartes détectées + Télémétrie live
        telem_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        telem_box.add_css_class("score-box")

        thdr = Gtk.Box(spacing=10)
        thdr.append(self._lbl("CARTES AXELERA & TÉLÉMÉTRIE LIVE", "params-title"))
        self._sdk_badge = Gtk.Label(halign=Gtk.Align.END, hexpand=True, wrap=True)
        self._sdk_badge.add_css_class("bench-subtitle")
        thdr.append(self._sdk_badge)
        telem_box.append(thdr)

        sel_row = Gtk.Box(spacing=10)
        sel_row.append(self._lbl("Carte affichée :", "param-label"))
        self._card_combo = Gtk.ComboBoxText()
        self._card_combo.set_hexpand(True)
        self._card_combo.connect("changed", lambda *_: self._refresh_telemetry_once())
        sel_row.append(self._card_combo)
        btn_pick_sdk = Gtk.Button(label="📁 SDK / venv…")
        btn_pick_sdk.set_tooltip_text("Choisir le dossier `voyager-sdk` ou son environnement virtuel `venv`")
        btn_pick_sdk.connect("clicked", self._on_browse_sdk)
        sel_row.append(btn_pick_sdk)
        telem_box.append(sel_row)

        grid = Gtk.Grid(row_spacing=8, column_spacing=22)
        self._tv: Dict[str, Gtk.Label] = {}

        def trow(r: int, c: int, title_txt: str, key: str) -> None:
            l = Gtk.Label(label=title_txt, halign=Gtk.Align.START)
            l.add_css_class("param-label")
            grid.attach(l, c * 2, r, 1, 1)
            v = Gtk.Label(label="—", halign=Gtk.Align.START, wrap=True, selectable=True)
            v.add_css_class("metric-value")
            grid.attach(v, c * 2 + 1, r, 1, 1)
            self._tv[key] = v

        trow(0, 0, "Modèle / Carte :", "model");        trow(0, 1, "Pilote / Firmware :", "fw")
        trow(1, 0, "Température AIPU :", "temp");       trow(1, 1, "Puissance (INA236) :", "power")
        trow(2, 0, "Horloge AIPU :", "clock");          trow(2, 1, "Utilisation MVM :", "mvm")
        trow(3, 0, "Mémoire DDR :", "mem");             trow(3, 1, "KPS / Bande passante PCIe :", "kps_bw")
        trow(4, 0, "Bus PCI & Lien :", "pcie");         trow(4, 1, "Source télémétrie :", "source")
        telem_box.append(grid)

        bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bars.set_margin_top(6)
        bars.append(self._lbl("Température jonction / seuil matériel (95 °C alerte · 105 °C throttle · 120 °C arrêt) :"))
        self._bar_temp = Gtk.ProgressBar(show_text=True)
        self._bar_temp.set_text("—")
        bars.append(self._bar_temp)
        telem_box.append(bars)
        box.append(telem_box)

        # 2. Chargement de modèles IA & paramètres de test
        pf = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        pf.add_css_class("params-panel")
        pf.append(self._lbl("CHARGEMENT DE MODÈLES IA & PARAMÈTRES DU TEST", "params-title"))

        pgrid = Gtk.Grid(row_spacing=10, column_spacing=14)
        r = 0

        def prow(label_txt: str, widget: Gtk.Widget) -> None:
            nonlocal r
            l = Gtk.Label(label=label_txt, halign=Gtk.Align.END)
            l.add_css_class("param-label")
            pgrid.attach(l, 0, r, 1, 1)
            pgrid.attach(widget, 1, r, 1, 1)
            r += 1

        # Moteur
        self._engine_combo = Gtk.ComboBoxText()
        self._engine_combo.append("auto", "Automatique (axrunmodel pour model.json, sinon inference.py)")
        self._engine_combo.append("axrunmodel", "Chauffe / Stabilité AIPU (axrunmodel — boucle continue)")
        self._engine_combo.append("inference", "Pipeline complet (inference.py — vidéo / média)")
        self._engine_combo.set_active(0)
        self._engine_combo.set_hexpand(True)
        prow("Mode d'exécution :", self._engine_combo)

        # Modèles découverts + saisie + Parcourir
        mod_row = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        mod_top = Gtk.Box(spacing=8)
        self._model_combo = Gtk.ComboBoxText()
        self._model_combo.set_hexpand(True)
        self._model_combo.connect("changed", self._on_model_combo_changed)
        mod_top.append(self._model_combo)
        btn_mod_file = Gtk.Button(label="📂 Parcourir…")
        btn_mod_file.set_tooltip_text("Charger un fichier model.json compilé, .axm, .axe ou .yaml")
        btn_mod_file.connect("clicked", self._on_browse_model)
        mod_top.append(btn_mod_file)
        mod_row.append(mod_top)

        self._model_entry = Gtk.Entry()
        self._model_entry.set_placeholder_text("Chemin vers model.json (ex. build/.../1/model.json) ou nom Zoo (ex. yolov8s-coco-onnx)")
        self._model_entry.set_hexpand(True)
        mod_row.append(self._model_entry)
        prow("Modèle IA :", mod_row)

        # Téléchargement rapide depuis le Model Zoo (axdownloadmodel)
        dl_box = Gtk.Box(spacing=8)
        self._zoo_dl_combo = Gtk.ComboBoxText()
        for mid, desc in ax.SUGGESTED_ZOO_MODELS:
            self._zoo_dl_combo.append(mid, f"{mid} — {desc}")
        self._zoo_dl_combo.set_active(0)
        self._zoo_dl_combo.set_hexpand(True)
        self._btn_dl_model = Gtk.Button(label="⬇ Télécharger (axdownloadmodel)")
        self._btn_dl_model.connect("clicked", self._on_download_model)
        dl_box.append(self._zoo_dl_combo)
        dl_box.append(self._btn_dl_model)
        prow("Préparer un modèle :", dl_box)

        # Média (pour inference.py)
        med_box = Gtk.Box(spacing=8)
        self._media_entry = Gtk.Entry()
        self._media_entry.set_placeholder_text("Optionnel pour axrunmodel ; vidéo/image/flux pour inference.py (ex. media/traffic1_1080p.mp4)")
        self._media_entry.set_hexpand(True)
        btn_med = Gtk.Button(label="📂 Média…")
        btn_med.connect("clicked", self._on_browse_media)
        med_box.append(self._media_entry)
        med_box.append(btn_med)
        prow("Source vidéo / média :", med_box)

        # Cartes cibles
        self._targets_box = Gtk.FlowBox()
        self._targets_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._targets_box.set_max_children_per_line(4)
        self._targets_box.set_min_children_per_line(1)
        self._targets_box.set_column_spacing(10)
        self._targets_box.set_row_spacing(4)
        prow("Cartes cibles (aucune = toutes) :", self._targets_box)

        # Paramètres numériques compacts
        num_row = Gtk.Box(spacing=16)
        num_row.append(self._lbl("Durée (s) :", "param-label"))
        self._spin_time = Gtk.SpinButton.new_with_range(5, 86400, 10)
        self._spin_time.set_value(60)
        num_row.append(self._spin_time)

        num_row.append(self._lbl("Cœurs AIPU :", "param-label"))
        self._spin_cores = Gtk.SpinButton.new_with_range(1, 4, 1)
        self._spin_cores.set_value(4)
        num_row.append(self._spin_cores)

        num_row.append(self._lbl("Cap FPS (0=max) :", "param-label"))
        self._spin_fps = Gtk.SpinButton.new_with_range(0, 5000, 10)
        self._spin_fps.set_value(0)
        num_row.append(self._spin_fps)
        prow("Charge & cœurs :", num_row)

        exp_adv = Gtk.Expander(label="Options CLI avancées (axrunmodel / inference.py)")
        self._extra_entry = Gtk.Entry()
        self._extra_entry.set_placeholder_text("ex. --show-bar-chart --no-double-buffer ou --frames 500")
        exp_adv.set_child(self._extra_entry)
        pgrid.attach(exp_adv, 0, r, 2, 1)
        r += 1

        pf.append(pgrid)
        box.append(pf)

        # 3. Score + Progression + Live Chart + Métriques
        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sb.add_css_class("score-box")
        stitle = Gtk.Label(label="RÉSULTAT DU TEST")
        stitle.add_css_class("params-title")
        stitle.set_halign(Gtk.Align.START)
        self._score_val = Gtk.Label(label="—")
        self._score_val.add_css_class("score-value")
        self._score_val.set_halign(Gtk.Align.CENTER)
        self._score_unit = Gtk.Label(label="")
        self._score_unit.add_css_class("score-unit")
        self._score_unit.set_halign(Gtk.Align.CENTER)
        self._progress = Gtk.ProgressBar()
        self._progress.set_fraction(0.0)
        self._progress.set_show_text(True)
        self._progress.set_text("En attente")
        sb.append(stitle)
        sb.append(self._score_val)
        sb.append(self._score_unit)
        sb.append(self._progress)
        box.append(sb)

        self._live_chart = AxeleraLiveChart()
        box.append(self._live_chart)

        self._metrics_box = Gtk.FlowBox()
        self._metrics_box.set_max_children_per_line(4)
        self._metrics_box.set_min_children_per_line(2)
        self._metrics_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._metrics_box.set_column_spacing(10)
        self._metrics_box.set_row_spacing(10)
        box.append(self._metrics_box)

        # 4. Sortie console en direct
        exp_log = Gtk.Expander(label="Sortie console du test (Voyager SDK)")
        ls = Gtk.ScrolledWindow()
        ls.set_min_content_height(120)
        ls.set_max_content_height(240)
        self._log_buf = Gtk.TextBuffer()
        lv = Gtk.TextView(buffer=self._log_buf)
        lv.set_editable(False)
        lv.set_cursor_visible(False)
        lv.add_css_class("log-view")
        lv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ls.set_child(lv)
        exp_log.set_child(ls)
        box.append(exp_log)

        # 5. Réglages matériels axdevice (repliés par défaut)
        exp_cfg = Gtk.Expander(label="⚙ Réglages carte Axelera (axdevice — horloge, MVM, throttling, limite de puissance)")
        cfg_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        cfg_box.add_css_class("params-panel")
        cgrid = Gtk.Grid(row_spacing=8, column_spacing=14)

        self._clk_combo = Gtk.ComboBoxText()
        for mhz in ax.VALID_CLOCKS_MHZ:
            self._clk_combo.append(str(mhz), f"{mhz} MHz")
        self._clk_combo.set_active_id("800")
        btn_clk = Gtk.Button(label="Appliquer horloge")
        btn_clk.connect("clicked", self._on_apply_clock)
        cgrid.attach(self._lbl("Horloge AIPU :", "param-label"), 0, 0, 1, 1)
        cgrid.attach(self._clk_combo, 1, 0, 1, 1)
        cgrid.attach(btn_clk, 2, 0, 1, 1)

        self._mvm_spin = Gtk.SpinButton.new_with_range(1, 100, 5)
        self._mvm_spin.set_value(100)
        btn_mvm = Gtk.Button(label="Appliquer limite MVM")
        btn_mvm.connect("clicked", self._on_apply_mvm)
        cgrid.attach(self._lbl("Limite MVM (%) :", "param-label"), 0, 1, 1, 1)
        cgrid.attach(self._mvm_spin, 1, 1, 1, 1)
        cgrid.attach(btn_mvm, 2, 1, 1, 1)

        sw_box = Gtk.Box(spacing=6)
        self._sw_t = Gtk.SpinButton.new_with_range(60, 115, 5); self._sw_t.set_value(100)
        self._sw_h = Gtk.SpinButton.new_with_range(1, 30, 1); self._sw_h.set_value(5)
        self._sw_l = Gtk.SpinButton.new_with_range(1, 100, 5); self._sw_l.set_value(10)
        sw_box.append(self._lbl("Seuil °C:", None)); sw_box.append(self._sw_t)
        sw_box.append(self._lbl("Hyst °C:", None)); sw_box.append(self._sw_h)
        sw_box.append(self._lbl("MVM %:", None)); sw_box.append(self._sw_l)
        btn_sw = Gtk.Button(label="Appliquer throttling")
        btn_sw.connect("clicked", self._on_apply_sw_throttle)
        cgrid.attach(self._lbl("Throttling logiciel :", "param-label"), 0, 2, 1, 1)
        cgrid.attach(sw_box, 1, 2, 1, 1)
        cgrid.attach(btn_sw, 2, 2, 1, 1)

        self._pwr_spin = Gtk.SpinButton.new_with_range(0, 30, 0.5)
        self._pwr_spin.set_digits(1)
        self._pwr_spin.set_value(8.5)
        btn_pwr = Gtk.Button(label="Appliquer puissance (113m)")
        btn_pwr.connect("clicked", self._on_apply_power_limit)
        cgrid.attach(self._lbl("Limite puissance W (0=off) :", "param-label"), 0, 3, 1, 1)
        cgrid.attach(self._pwr_spin, 1, 3, 1, 1)
        cgrid.attach(btn_pwr, 2, 3, 1, 1)

        cfg_box.append(cgrid)

        act_row = Gtk.Box(spacing=10)
        btn_rescan = Gtk.Button(label="🔄 axdevice --refresh (rescan PCIe + firmware)")
        btn_rescan.connect("clicked", self._on_axdevice_refresh)
        self._cfg_status = Gtk.Label(halign=Gtk.Align.START, wrap=True, hexpand=True)
        act_row.append(btn_rescan)
        act_row.append(self._cfg_status)
        cfg_box.append(act_row)

        note = self._lbl(
            "Note : les réglages axdevice ne persistent pas après un redémarrage. "
            "BenchSuite exporte AXELERA_CONFIGURE_BOARD=0 lors des lancements pour conserver vos réglages manuels."
        )
        note.set_wrap(True)
        cfg_box.append(note)
        exp_cfg.set_child(cfg_box)
        box.append(exp_cfg)

        scroll.set_child(box)
        self.append(scroll)

    # ── Découverte & télémétrie en arrière-plan ───────────────────────────────

    def _on_map(self) -> None:
        if not self._detected_once:
            self._detected_once = True
            self._trigger_discovery(force=False)
        else:
            self._start_monitoring()

    def _start_monitoring(self) -> None:
        if self._refresh_tag is None:
            self._refresh_tag = GLib.timeout_add_seconds(2, self._refresh_telemetry_once)

    def _stop_monitoring(self) -> None:
        if self._refresh_tag is not None:
            GLib.source_remove(self._refresh_tag)
            self._refresh_tag = None

    def _trigger_discovery(self, force: bool = False) -> None:
        if force:
            forget_tool_cache()
            ax.forget_cache()

        def work():
            devs = ax.list_devices(timeout=4.0)
            st = ax.sdk_status()
            models = ax.discover_models(sdk_dir=st.get("sdk_dir") or None)
            medias = ax.discover_media(sdk_dir=st.get("sdk_dir") or None)
            telem = ax.poll_telemetry(timeout=2.5)
            return st, devs, models, medias, telem

        threading.Thread(
            target=lambda: GLib.idle_add(self._apply_discovery, *work()),
            daemon=True,
        ).start()

    def _apply_discovery(self, st: dict, devs: list, models: list, medias: list, telem: dict) -> bool:
        self._devices = devs
        self._models = models
        self._medias = medias
        self._last_status = st or {}

        any_axdev_ok = any(d.get("axdevice_ok") for d in devs)
        missing_axdev = [d.get("bdf") or d.get("name") for d in devs if not d.get("axdevice_ok")]
        drv_name = st.get("driver_name") or "metis"
        if st.get("driver_loaded"):
            drv_txt = f"{drv_name} {st['driver_version'] or 'chargé'}"
        elif any_axdev_ok:
            drv_txt = "triton / userspace"
        else:
            drv_txt = "pilote metis absent"
        detected_sdks = st.get("detected_sdks") or []
        if st.get("sdk_dir"):
            sdk_base = os.path.basename(st["sdk_dir"])
            sdk_txt = f"SDK {st['sdk_version'] or sdk_base}"
            if not st.get("tools", {}).get("axdevice"):
                sdk_txt += " (outils CLI absents)"
            if len(detected_sdks) > 1:
                sdk_txt += f" · {len(detected_sdks)} SDK détectés"
        else:
            sdk_txt = "Voyager SDK absent"
        srv_txt = "axsystemserver actif" if st.get("axsystemserver_active") else "axsystemserver inactif"
        self._sdk_badge.set_text(f"{drv_txt} · {sdk_txt} · {srv_txt}")

        # Bandeau avertissement détaillé et actionnable
        issues = []
        if not devs:
            issues.append("Aucune carte Axelera Metis (PCI 1f9d) détectée sur cette machine.")
        if not st.get("driver_loaded") and not any_axdev_ok:
            krel = st.get("kernel_release") or ""
            if devs:
                hint = (
                    f"{len(devs)} carte(s) PCIe 1f9d détectée(s), mais aucun module noyau Axelera n'est chargé "
                    f"pour Linux {krel} (/sys/class/metis et lsmod vides)."
                )
            else:
                hint = "Module noyau `metis` non chargé (/sys/class/metis absent)."
            if st.get("dkms_present"):
                mok = st.get("mok_path") or "/var/lib/shim-signed/mok/MOK.der"
                if st.get("secure_boot") is True:
                    hint += (
                        " Secure Boot UEFI est activé : la signature DKMS risque d'être rejetée (« Key was rejected by service »). "
                        f"Désactivez Secure Boot dans le BIOS/UEFI ou importez `{mok}` via `sudo mokutil --import {mok}` puis redémarrez."
                    )
                else:
                    hint += (
                        f" Essayez : `sudo modprobe metis` (`dkms status`) — si « Key was rejected by service », "
                        f"vérifiez la clé `{mok}` ou recompilez `metis-dkms` pour Linux {krel}."
                    )
            elif st.get("tools", {}).get("axdevice"):
                hint += " Installez le pilote : `axdevice driver --install`."
            issues.append(hint)
        elif any_axdev_ok and missing_axdev:
            issues.append(
                f"{len(missing_axdev)} carte(s) PCIe non initialisée(s) dans axdevice : {', '.join(missing_axdev)}."
            )
        if not st.get("tools", {}).get("axdevice"):
            if st.get("sdk_dir"):
                other_paths = [
                    f"{d['path']} ({d.get('version') or '?'})"
                    for d in detected_sdks
                    if d["path"] != st.get("sdk_dir")
                ]
                msg = (
                    f"Outils CLI (`axdevice`, `axrunmodel`) introuvables dans `{st['sdk_dir']}` "
                    f"(ni son venv)."
                )
                if other_paths:
                    msg += " Autres SDK détectés : " + ", ".join(other_paths[:3]) + "."
                msg += " Sélectionnez le bon dossier SDK/venv."
                issues.append(msg)
            else:
                issues.append("Outils Voyager SDK (`axdevice`, `axrunmodel`) introuvables : sélectionnez le dossier SDK/venv.")
        if st.get("axdevice_error"):
            issues.append(f"Message `axdevice` : {st['axdevice_error']}")
        if st.get("axsystemserver_installed") and not st.get("axsystemserver_active"):
            issues.append("Service `axsystemserver` inactif (sondes temp/puissance indisponibles sans `sudo systemctl enable --now axsystemserver`).")

        if issues:
            self._warn_lbl.set_markup(
                f'<span foreground="#f9e2af">{GLib.markup_escape_text(" ".join(issues))}</span>'
            )
            if devs and (st.get("sdk_dir") or st.get("axsystemserver_installed")):
                self._btn_copy_diag.set_label("📋 Copier commandes diag / activation pilote")
            else:
                self._btn_copy_diag.set_label("📋 Copier commande installation SDK")
            self._warn_box.set_visible(True)
        else:
            self._warn_box.set_visible(False)

        # Sélecteur de carte affichée
        prev_sel = self._card_combo.get_active_id()
        self._card_combo.remove_all()
        if devs:
            for d in devs:
                idx = str(d["index"])
                lbl = f"[{idx}] {d.get('model', 'Metis')} — {d.get('name', d.get('bdf', ''))}"
                self._card_combo.append(idx, lbl)
            if prev_sel and any(str(d["index"]) == prev_sel for d in devs):
                self._card_combo.set_active_id(prev_sel)
            else:
                self._card_combo.set_active(0)
        else:
            self._card_combo.append("0", "Aucune carte détectée")
            self._card_combo.set_active(0)

        # Cases à cocher multi-cartes
        while True:
            c = self._targets_box.get_first_child()
            if not c:
                break
            self._targets_box.remove(c)
        self._target_checks = []
        if devs:
            for d in devs:
                idx = str(d["index"])
                cb = Gtk.CheckButton(label=f"[{idx}] {d.get('model', 'Metis')} ({d.get('bdf') or d.get('name', '')})")
                self._targets_box.append(cb)
                self._target_checks.append((idx, cb))
        else:
            self._targets_box.append(self._lbl("Aucune carte détectée (mode libre)", "bench-subtitle"))

        # Modèles découverts
        self._setting_model_combo = True
        self._model_combo.remove_all()
        if models:
            for i, m in enumerate(models):
                self._model_combo.append(str(i), m["label"])
            self._model_combo.set_active(0)
            if not self._model_entry.get_text().strip():
                first = models[0]
                self._model_entry.set_text(first["path"] if first["kind"] == "compiled" else first["name"])
        else:
            self._model_combo.append("", "Aucun modèle compilé / Zoo détecté — parcourez ou téléchargez ci-dessous")
            self._model_combo.set_active(0)
        self._setting_model_combo = False

        if medias and not self._media_entry.get_text().strip():
            self._media_entry.set_text(medias[0])

        self._render_telemetry(telem)
        if self.get_mapped():
            self._start_monitoring()
        return False

    def _refresh_telemetry_once(self) -> bool:
        if self._reading:
            return True
        self._reading = True

        def work():
            try:
                return ax.poll_telemetry(timeout=2.5)
            finally:
                pass

        def done(telem):
            self._reading = False
            self._render_telemetry(telem)
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()
        return True

    def _render_telemetry(self, telem: dict) -> None:
        sel_id = self._card_combo.get_active_id()
        try:
            idx = int(sel_id) if sel_id is not None else 0
        except ValueError:
            idx = 0
        dev_info = next((d for d in self._devices if d["index"] == idx), None)
        td = (telem.get("devices") or {}).get(idx, {}) if isinstance(telem, dict) else {}

        if not dev_info and not td:
            for k in self._tv:
                self._tv[k].set_text("—")
            self._bar_temp.set_fraction(0.0)
            self._bar_temp.set_text("Aucune carte Axelera détectée")
            return

        model = td.get("model") or (dev_info or {}).get("model") or "Metis AIPU"
        name = td.get("name") or (dev_info or {}).get("name") or ""
        self._tv["model"].set_text(f"{model}" + (f" ({name})" if name else ""))

        drv = (dev_info or {}).get("driver") or "—"
        drv_v = (dev_info or {}).get("driver_version") or ""
        flver = td.get("flver") or (dev_info or {}).get("flver") or ""
        bcver = td.get("bcver") or (dev_info or {}).get("bcver") or ""
        fw_parts = [f"{drv}" + (f" {drv_v}" if drv_v else "")]
        if flver:
            fw_parts.append(f"flver={flver}")
        if bcver:
            fw_parts.append(f"bcver={bcver}")
        self._tv["fw"].set_text(" · ".join(fw_parts))

        temp_c = td.get("temp_c")
        temp_cores = td.get("temp_cores")
        if temp_c is not None:
            t_txt = f"{temp_c:.1f} °C"
            if temp_cores and len(temp_cores) > 1:
                t_txt += "  [" + ", ".join(f"{x:.0f}" for x in temp_cores) + "]"
            self._tv["temp"].set_text(t_txt)
            self._bar_temp.set_fraction(max(0.0, min(1.0, float(temp_c) / ax.TEMP_SHUTDOWN_C)))
            self._bar_temp.set_text(f"{temp_c:.1f} °C / {ax.TEMP_SHUTDOWN_C:.0f} °C")
        else:
            if not (self._last_status or {}).get("driver_loaded") and not (dev_info or {}).get("axdevice_ok"):
                self._tv["temp"].set_text("— (pilote metis non chargé)")
                self._bar_temp.set_text("Pilote noyau metis non chargé")
            else:
                self._tv["temp"].set_text("— (activer axsystemserver pour les sondes live)")
                self._bar_temp.set_text("Température non exposée sans axsystemserver")
            self._bar_temp.set_fraction(0.0)

        self._tv["power"].set_text(_fmt(td.get("power_w"), " W", 2))
        clk = td.get("clock_mhz") or (dev_info or {}).get("clock_mhz")
        cclk = (dev_info or {}).get("core_clocks") or ""
        self._tv["clock"].set_text(_fmt(clk, " MHz", 0) + (f" ({cclk})" if cclk else ""))

        mvm = td.get("mvm_pct") if td.get("mvm_pct") is not None else (dev_info or {}).get("mvm_pct")
        self._tv["mvm"].set_text(_fmt(mvm, " %", 1))

        mem_mb = td.get("mem_mb") or (dev_info or {}).get("mem_mb")
        ddr_used = td.get("ddr_used_mb")
        if ddr_used is not None and mem_mb:
            self._tv["mem"].set_text(f"{ddr_used:.0f} / {mem_mb} Mo")
        elif mem_mb:
            self._tv["mem"].set_text(f"{mem_mb / 1024:.1f} Go ({mem_mb} Mo)")
        else:
            self._tv["mem"].set_text("—")

        kps = td.get("kps")
        bw = td.get("pcie_bw_mbs")
        kb_parts = []
        if kps is not None:
            kb_parts.append(f"{kps:.1f} KPS")
        if bw is not None:
            kb_parts.append(f"{bw:.1f} MB/s")
        self._tv["kps_bw"].set_text(" · ".join(kb_parts) if kb_parts else "—")

        bdf = (dev_info or {}).get("bdf") or td.get("bdf") or "—"
        pgen = (dev_info or {}).get("pcie_gen") or ""
        pw = (dev_info or {}).get("pcie_width") or ""
        link_txt = f"Gen{pgen} x{pw}" if pgen and pw else (f"x{pw}" if pw else "")
        self._tv["pcie"].set_text(f"{bdf}" + (f" ({link_txt})" if link_txt else ""))
        self._tv["source"].set_text(telem.get("source") or "—")

    # ── Sélections & actions ──────────────────────────────────────────────────

    def _on_model_combo_changed(self, _combo) -> None:
        if self._setting_model_combo:
            return
        aid = self._model_combo.get_active_id()
        if aid is None or not aid.isdigit():
            return
        idx = int(aid)
        if 0 <= idx < len(self._models):
            m = self._models[idx]
            self._model_entry.set_text(m["path"] if m["kind"] == "compiled" else m["name"])

    def _on_browse_sdk(self, _btn) -> None:
        def chosen(path):
            if not path:
                return
            updates = {}
            if os.path.isfile(os.path.join(path, "bin", "axdevice")) or os.path.isfile(os.path.join(path, "axdevice")):
                updates["venv_dir"] = path
            else:
                updates["sdk_dir"] = path
            ax.save_config(updates)
            forget_tool_cache()
            self._trigger_discovery(force=True)
            self.win._show_dialog("Voyager SDK configuré", f"Dossier enregistré :\n{path}")

        cur_sdk = ax.find_sdk_dir()
        init_dir = os.path.dirname(cur_sdk) if cur_sdk and os.path.isdir(os.path.dirname(cur_sdk)) else os.path.expanduser("~")
        self._pick_path("Sélectionner le dossier voyager-sdk ou son venv", True, chosen, initial_dir=init_dir)

    def _on_browse_model(self, _btn) -> None:
        def chosen(path):
            if path:
                self._model_entry.set_text(path)

        self._pick_path("Charger un modèle Axelera (model.json, .axm, .axe, .yaml)", False, chosen, initial_dir=ax.find_sdk_dir() or None)

    def _on_browse_media(self, _btn) -> None:
        def chosen(path):
            if path:
                self._media_entry.set_text(path)

        self._pick_path("Sélectionner une vidéo ou image de test", False, chosen, initial_dir=ax.find_sdk_dir() or None)

    def _on_copy_install(self, _btn) -> None:
        st = self._last_status or {}
        if self._devices and (st.get("sdk_dir") or st.get("axsystemserver_installed") or st.get("opt_runtimes")):
            sdk_path = st.get("sdk_dir") or "~/voyager-sdk"
            mok = st.get("mok_path") or "/var/lib/shim-signed/mok/MOK.der"
            sb_block = (
                "# Enrôler la clé MOK si `modprobe metis` renvoie « Key was rejected by service »\n"
                "mokutil --sb-state\n"
                f"# sudo mokutil --import {mok}  # puis redémarrer (Enroll MOK) ou recompiler metis sans signature\n"
            )
            cmd = (
                f"# 1. Vérifier / charger le pilote noyau metis (noyau {st.get('kernel_release') or '$(uname -r)'})\n"
                "dkms status\n"
                f"{sb_block}"
                "sudo modprobe metis\n"
                "ls -la /sys/class/metis /dev/metis*\n"
                "# 2. Activer le service de télémétrie\n"
                "sudo systemctl enable --now axsystemserver\n"
                "# 3. Vérifier les outils du runtime / SDK\n"
                f"for v in {sdk_path}/venv*/bin/activate; do [ -f \"$v\" ] && source \"$v\" && break; done\n"
                "which axdevice || ls /opt/axelera/*/bin/axmonitor 2>/dev/null\n"
                "axdevice"
            )
            msg = "Commandes de diagnostic / activation pilote copiées dans le presse-papiers"
        else:
            cmd = (
                "git clone https://github.com/axelera-ai-hub/voyager-sdk.git\n"
                "cd voyager-sdk && ./install-dependencies.sh\n"
                "python3 -m venv venv && source venv/bin/activate\n"
                "pip install --extra-index-url https://software.axelera.ai/artifactory/api/pypi/axelera-pypi/simple axelera-rt axelera-devkit[all]\n"
                "axdevice driver --install"
            )
            msg = "Procédure d'installation Voyager SDK copiée dans le presse-papiers"
        self.get_clipboard().set(cmd)
        self.win._set_status(msg)

    def _on_download_model(self, _btn) -> None:
        model_id = self._zoo_dl_combo.get_active_id() or "yolov8s-coco-onnx"
        self._btn_dl_model.set_sensitive(False)
        self._append_log(f"Téléchargement du modèle « {model_id} » via axdownloadmodel…")
        self.win._set_status(f"Téléchargement modèle Axelera : {model_id}…")

        def work():
            ok, msg = ax.download_zoo_model(model_id, target="metis")

            def finish():
                self._btn_dl_model.set_sensitive(True)
                self._append_log(msg)
                if ok:
                    self._model_entry.set_text(model_id)
                    self._trigger_discovery(force=True)
                    self.win._set_status(f"Modèle {model_id} prêt")
                else:
                    self.win._show_dialog("Téléchargement modèle", msg)
                return False

            GLib.idle_add(finish)

        threading.Thread(target=work, daemon=True).start()

    def _selected_device_arg(self) -> str:
        sel = [idx for idx, cb in self._target_checks if cb.get_active()]
        return ",".join(sel)

    # ── Exécution du test ─────────────────────────────────────────────────────

    def _on_run(self, _btn) -> None:
        if self.worker is not None and self.worker.is_alive():
            return
        self.result = None
        self._clear_metrics()
        self._log_buf.set_text("")
        self._score_val.set_markup('<span foreground="#6c7086">…</span>')
        self._score_unit.set_text("")
        self._progress.set_fraction(0.0)
        self._progress.set_text("Démarrage du test Axelera…")
        self._btn_run.set_sensitive(False)
        self._btn_stop.set_sensitive(True)
        self._btn_export.set_sensitive(False)
        self._live_chart.reset()

        params = {
            "model_path": self._model_entry.get_text().strip(),
            "engine": self._engine_combo.get_active_id() or "auto",
            "media_path": self._media_entry.get_text().strip(),
            "time_s": int(self._spin_time.get_value()),
            "aipu_cores": int(self._spin_cores.get_value()),
            "target_devices": self._selected_device_arg(),
            "throttle_fps": int(self._spin_fps.get_value()),
            "sdk_dir": ax.find_sdk_dir(),
            "extra_args": self._extra_entry.get_text().strip(),
        }
        self.win._set_status("Exécution : Axelera AIPU…")
        self.worker = AxeleraWorker(
            params,
            self._on_progress,
            self._on_done,
            self._live_chart.push,
            self._append_log,
        )
        self.worker.start()

    def _on_stop(self, _btn) -> None:
        if self.worker is not None:
            self.worker.stop()
        self._btn_stop.set_sensitive(False)
        self._progress.set_text("Arrêt en cours…")
        self.win._set_status("Arrêt du test Axelera…")

    def _on_progress(self, pct: int, msg: str) -> bool:
        self._progress.set_fraction(max(0.0, min(1.0, pct / 100.0)))
        self._progress.set_text(msg)
        return False

    def _append_log(self, line: str) -> bool:
        it = self._log_buf.get_end_iter()
        self._log_buf.insert(it, (line or "") + "\n")
        return False

    def _on_done(self, result: BenchmarkResult) -> bool:
        self.result = result
        self._btn_run.set_sensitive(True)
        self._btn_stop.set_sensitive(False)

        if result.status == "ok":
            if result.score is not None:
                self._score_val.set_markup(
                    f'<span foreground="{_CAT_COLOR}" weight="ultrabold">{result.score:.2f}</span>'
                )
                self._score_unit.set_markup(f'<span foreground="#6c7086">{GLib.markup_escape_text(result.unit or "FPS")}</span>')
            else:
                self._score_val.set_markup('<span foreground="#a6e3a1" size="xx-large">OK</span>')
                self._score_unit.set_text("Terminé")
            self._progress.set_fraction(1.0)
            self._progress.set_text("Terminé avec succès")
        elif result.status == "stopped":
            self._score_val.set_markup('<span foreground="#f9e2af" size="xx-large">STOP</span>')
            self._score_unit.set_text("Arrêté par utilisateur")
            self._progress.set_text("Arrêté")
        elif result.status == "skipped":
            self._score_val.set_markup('<span foreground="#f9e2af" size="xx-large">N/D</span>')
            self._score_unit.set_text("Outil ou modèle absent")
            self._progress.set_text("Ignoré")
        else:
            self._score_val.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
            self._score_unit.set_text(result.notes or "Erreur")
            self._progress.set_text("Erreur")

        self._populate_metrics(result)
        if result.raw_output and not self._log_buf.get_char_count():
            self._log_buf.set_text(result.raw_output[:8000])
        self._btn_export.set_sensitive(True)
        save_to_history([result])
        self.win._on_bench_result(result)
        return False

    def _clear_metrics(self) -> None:
        while True:
            c = self._metrics_box.get_first_child()
            if not c:
                break
            self._metrics_box.remove(c)

    def _populate_metrics(self, result: BenchmarkResult) -> None:
        self._clear_metrics()
        cmd = getattr(result, "command", "") or (result.metrics or {}).get("command", "")
        if cmd:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card")
            card.set_size_request(320, -1)
            lbl = Gtk.Label(label="COMMANDE EXÉCUTÉE", halign=Gtk.Align.START)
            lbl.add_css_class("metric-label")
            val = Gtk.Label(label=str(cmd), halign=Gtk.Align.START, wrap=True, selectable=True)
            val.add_css_class("metric-value")
            card.append(lbl)
            card.append(val)
            self._metrics_box.append(card)
        try:
            from core.electrical import summary_cards
            for title, txt in summary_cards(result.metrics or {}):
                card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                card.add_css_class("metric-card")
                card.set_size_request(210, -1)
                lbl = Gtk.Label(label=title, halign=Gtk.Align.START)
                lbl.add_css_class("metric-label")
                val = Gtk.Label(label=txt, halign=Gtk.Align.START, wrap=True)
                val.add_css_class("metric-value")
                card.append(lbl)
                card.append(val)
                self._metrics_box.append(card)
        except Exception:
            pass
        for k, v in (result.metrics or {}).items():
            if k == "command" or isinstance(v, (list, dict)) or k.startswith("series"):
                continue
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card")
            card.set_size_request(155, -1)
            lbl = Gtk.Label(label=k.replace("_", " ").upper(), halign=Gtk.Align.START, wrap=True)
            lbl.add_css_class("metric-label")
            val = Gtk.Label(label=str(v), halign=Gtk.Align.START, wrap=True)
            val.add_css_class("metric-value")
            card.append(lbl)
            card.append(val)
            self._metrics_box.append(card)

    def _on_export_all(self, _btn) -> None:
        if not self.result:
            return
        results = [self.result]
        sysinfo = self.win.system_info_snapshot()
        meta = load_report_meta()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, report_meta=meta, system_info_data=sysinfo)
            return "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())

        self.win.export_async(work, "Export complet réussi")

    # ── Réglages axdevice ─────────────────────────────────────────────────────

    def _active_device_idx(self) -> Optional[str]:
        aid = self._card_combo.get_active_id()
        if aid and self._devices and any(str(d["index"]) == aid for d in self._devices):
            return aid
        return None

    def _run_cfg_action(self, fn, *args) -> None:
        self._cfg_status.set_text("Application en cours…")

        def work():
            return fn(*args)

        def done(res):
            ok, msg = res
            self._cfg_status.set_text(("✅ " if ok else "⚠ ") + (msg or ""))
            if ok:
                self._refresh_telemetry_once()
            return False

        threading.Thread(target=lambda: GLib.idle_add(done, work()), daemon=True).start()

    def _on_apply_clock(self, _btn) -> None:
        mhz = int(self._clk_combo.get_active_id() or 800)
        self._run_cfg_action(ax.set_clock, self._active_device_idx(), mhz)

    def _on_apply_mvm(self, _btn) -> None:
        pct = int(self._mvm_spin.get_value())
        self._run_cfg_action(ax.set_mvm_limitation, self._active_device_idx(), pct)

    def _on_apply_sw_throttle(self, _btn) -> None:
        self._run_cfg_action(
            ax.set_sw_throttling,
            self._active_device_idx(),
            int(self._sw_t.get_value()),
            int(self._sw_h.get_value()),
            int(self._sw_l.get_value()),
        )

    def _on_apply_power_limit(self, _btn) -> None:
        self._run_cfg_action(ax.set_power_limit, self._active_device_idx(), float(self._pwr_spin.get_value()))

    def _on_axdevice_refresh(self, _btn) -> None:
        self._run_cfg_action(ax.refresh_devices)
