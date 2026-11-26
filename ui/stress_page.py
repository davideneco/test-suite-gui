"""
stress_page.py — Page stress-ng avec profils adaptatifs
- Sidebar : profils intégrés + utilisateur (CPU / RAM / Combiné)
- Panneau principal : description, paramètres éditables, live chart, résultats
- Création / suppression de profils utilisateur
"""

import os, threading, uuid
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Pango

from core.stress_profiles import (
    get_all_profiles, add_user_profile, delete_user_profile, resolve_auto,
    describe_params, profile_to_cmd, STRESS_STRESSORS,
)
from core.benchmarks import bench_stress_profile, BenchmarkResult, tool_exists
from exporters.export import export_all, save_to_history

OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")

CAT_COLORS = {
    "CPU":      "#f97316",
    "RAM":      "#22d3ee",
    "Combiné":  "#a78bfa",
    "Mes profils": "#a6e3a1",
    "CLI": "#f9e2af",
    "CPU · Tous les threads": "#f97316",
    "CPU · Cœurs physiques": "#fb923c",
    "Mixte CPU + RAM": "#a78bfa",
    "Spécial": "#f472b6",
    "Personnalisé": "#f9e2af",
}


def _is_cli_profile(profile: dict | None) -> bool:
    if not profile:
        return False
    params = (profile or {}).get("params", {})
    marker = " ".join(str(profile.get(k, "")) for k in ("id", "name", "category")).lower()
    return bool(
        params.get("cli_only")
        or str(params.get("stress_ng_cli_args", "")).strip()
        or profile.get("category") == "CLI"
        or "cli" in marker
        or "ligne de commande" in marker
    )


def _profile_cli_args(profile: dict, resolved_params: dict | None = None) -> str:
    params = profile.get("params", {})
    resolved_params = resolved_params or {}
    return str(
        params.get("stress_ng_cli_args", "")
        or params.get("stress_ng_extra_args", "")
        or resolved_params.get("stress_ng_cli_args", "")
        or resolved_params.get("stress_ng_extra_args", "")
        or ""
    ).strip()


def _hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i+2], 16) / 255 for i in (0, 2, 4))


# ─────────────────────────────────────────────────────────────────────────────
# Worker thread
# ─────────────────────────────────────────────────────────────────────────────

class StressWorker(threading.Thread):
    def __init__(self, params, on_progress, on_done, on_series):
        super().__init__(daemon=True)
        self._params      = params
        self._on_progress = on_progress
        self._on_done     = on_done
        self._on_series   = on_series
        self._kill_event  = threading.Event()

    def stop(self):
        self._kill_event.set()

    def run(self):
        def cb(pct, msg):
            if not self._kill_event.is_set():
                GLib.idle_add(self._on_progress, pct, msg)

        def series_cb(pt):
            if not self._kill_event.is_set():
                GLib.idle_add(self._on_series, pt)

        try:
            result = bench_stress_profile(
                cb=cb, series_cb=series_cb,
                kill_event=self._kill_event,
                **self._params,
            )
        except Exception as e:
            result = BenchmarkResult(self._params.get("profile_name") or "Stress profil", "CPU + RAM",
                                     None, "bogo-ops/s", 0, str(e), status="error")
        GLib.idle_add(self._on_done, result)


# ─────────────────────────────────────────────────────────────────────────────
# Dialog de création / édition de profil
# ─────────────────────────────────────────────────────────────────────────────

class StressProfileDialog(Gtk.Window):
    def __init__(self, parent, existing: dict = None, on_saved=None):
        super().__init__()
        self._existing  = existing or {}
        self._on_saved  = on_saved or (lambda p: None)
        self._mode      = "edit" if existing else "create"

        self.set_title("Modifier le profil" if self._mode == "edit" else "Nouveau profil")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(520, 560)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20); box.set_margin_start(24)
        box.set_margin_end(24); box.set_margin_bottom(20)

        def lbl(text):
            l = Gtk.Label(label=text); l.set_halign(Gtk.Align.START)
            l.add_css_class("param-label"); return l

        p = resolve_auto(self._existing.get("params", {
            "cpu_workers": "auto", "cpu_stressor": "all",
            "vm_workers": 0, "vm_bytes": "512M", "time_s": 30, "cpu_load_pct": 0,
        }))

        # Nom
        box.append(lbl("Nom du profil"))
        self._name = Gtk.Entry()
        self._name.set_text(self._existing.get("name", ""))
        box.append(self._name)

        # Catégorie
        box.append(lbl("Catégorie"))
        self._cat = Gtk.Entry()
        self._cat.set_text(self._existing.get("category", "Mes profils"))
        box.append(self._cat)

        # Description
        box.append(lbl("Description (optionnelle)"))
        sw = Gtk.ScrolledWindow(); sw.set_min_content_height(60)
        self._desc_buf = Gtk.TextBuffer()
        self._desc_buf.set_text(self._existing.get("description", ""))
        tv = Gtk.TextView(buffer=self._desc_buf)
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sw.set_child(tv); box.append(sw)

        grid = Gtk.Grid(); grid.set_row_spacing(10); grid.set_column_spacing(16)

        def row(r, label, widget):
            l = Gtk.Label(label=label); l.add_css_class("param-label"); l.set_halign(Gtk.Align.END)
            grid.attach(l, 0, r, 1, 1); grid.attach(widget, 1, r, 1, 1)
            return l
            return l
            return l

        def spin(lo, hi, val):
            s = Gtk.SpinButton.new_with_range(lo, hi, 1)
            s.set_value(val); s.set_size_request(120, -1); return s

        n_cpu = os.cpu_count() or 4

        self._cpu_w  = spin(0, n_cpu, p.get("cpu_workers", n_cpu))
        self._vm_w   = spin(0, 64, p.get("vm_workers", 0))
        self._time   = spin(5, 600, p.get("time_s", 30))
        self._load   = spin(0, 99, p.get("cpu_load_pct", 0))

        sl = Gtk.StringList.new(STRESS_STRESSORS)
        self._stressor_dd = Gtk.DropDown(model=sl)
        self._stressor_dd.set_size_request(140, -1)
        cur = p.get("cpu_stressor", "all")
        if cur in STRESS_STRESSORS:
            self._stressor_dd.set_selected(STRESS_STRESSORS.index(cur))

        self._vm_bytes = Gtk.Entry()
        self._vm_bytes.set_text(str(p.get("vm_bytes", "512M")))
        self._vm_bytes.set_size_request(120, -1)

        self._extra = Gtk.Entry()
        self._extra.set_text(str(p.get("stress_ng_extra_args", "")))
        self._extra.set_placeholder_text("--vm-keep --vm-populate ...")

        row(0, "CPU workers (0=désactivé) :", self._cpu_w)
        row(1, "Stressor CPU :",              self._stressor_dd)
        row(2, "Load cible % (0=max) :",      self._load)
        row(3, "VM workers (0=désactivé) :",  self._vm_w)
        row(4, "Mémoire par worker :",        self._vm_bytes)
        row(5, "Durée (s) :",                 self._time)
        row(6, "Arguments additionnels CLI :", self._extra)

        hint = Gtk.Label()
        hint.set_markup('<span foreground="#6c7086" size="small">'
                        'vm_bytes : ex. 512M, 2G, auto (= 70% RAM), auto_85pct (= 85% RAM)</span>')
        hint.set_halign(Gtk.Align.START)
        grid.attach(hint, 0, 7, 2, 1)

        box.append(grid)

        # Erreur
        self._err = Gtk.Label(); self._err.set_visible(False)
        self._err.set_halign(Gtk.Align.START); box.append(self._err)

        # Boutons
        br = Gtk.Box(spacing=10)
        btn_cancel = Gtk.Button(label="Annuler")
        btn_cancel.connect("clicked", lambda _: self.destroy())
        btn_save = Gtk.Button(label="Enregistrer")
        btn_save.add_css_class("btn-run"); btn_save.set_hexpand(True)
        btn_save.connect("clicked", self._save)
        br.append(btn_cancel); br.append(btn_save)
        box.append(br)

        self.set_child(box)

    def _save(self, _):
        name = self._name.get_text().strip()
        if not name:
            self._err.set_markup('<span foreground="#f38ba8">⚠  Le nom est obligatoire.</span>')
            self._err.set_visible(True)
            return

        item = self._stressor_dd.get_selected_item()
        stressor = item.get_string() if item else "all"
        buf = self._desc_buf
        desc = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()
        pid = self._existing.get("id") if self._mode == "edit" else f"user_{uuid.uuid4().hex[:8]}"

        profile = {
            "id":          pid,
            "name":        name,
            "category":    self._cat.get_text().strip() or "Mes profils",
            "description": desc or name,
            "icon":        self._existing.get("icon", "★"),
            "builtin":     False,
            "params": {
                "cpu_workers":  int(self._cpu_w.get_value()),
                "cpu_stressor": stressor,
                "vm_workers":   int(self._vm_w.get_value()),
                "vm_bytes":     self._vm_bytes.get_text().strip() or "512M",
                "time_s":       int(self._time.get_value()),
                "cpu_load_pct": int(self._load.get_value()),
                "stress_ng_extra_args": self._extra.get_text().strip(),
            },
        }
        add_user_profile(profile)
        self._on_saved(profile)
        self.destroy()


# ─────────────────────────────────────────────────────────────────────────────
# Widget live chart (réutilise le même rendu Cairo que main.py)
# ─────────────────────────────────────────────────────────────────────────────

try:
    import cairo as _cairo  # noqa: F401  (test de disponibilité)
    gi.require_foreign("cairo")   # exige python3-gi-cairo : sinon « foreign struct converter for cairo.Context » à chaque dessin
    _HAS_CAIRO = True
except Exception:
    _HAS_CAIRO = False

# Panels du graphique : chaque panel a sa propre échelle Y
# Format : (label_axe_Y, [(clé_série, sous_label, couleur_hex), ...])
_PANELS_DEF = [
    ("Temp °C",  [
        ("temp",     "CPU0", "f38ba8"),
        ("temp_s1",  "CPU1", "cba6f7"),
        ("ram_temp", "RAM",  "fab387"),
    ]),
    ("Power W",  [
        ("power",     "CPU0", "f97316"),
        ("power_s1",  "CPU1", "fab387"),
        ("ram_power", "RAM",  "cba6f7"),
    ]),
    ("Freq MHz", [
        ("freq",    "CPU0", "22d3ee"),
        ("freq_s1", "CPU1", "74c7ec"),
    ]),
    ("Load %",   [
        ("cpu_pct", "",     "a6e3a1"),
    ]),
]

# Séries toujours présentes dans _pts (remplies à chaque point)
_SERIES_FIXED = ("temp", "power", "freq", "cpu_pct")
# Séries optionnelles (socket 1, RAM, présentes seulement si les données sont émises)
_SERIES_OPT   = ("temp_s1", "power_s1", "freq_s1", "ram_temp", "ram_power")


class StressLiveChart(Gtk.DrawingArea):
    def __init__(self):
        super().__init__()
        self._pts = {"t": [], "temp": [], "temp_s1": [], "power": [], "power_s1": [],
                     "freq": [], "freq_s1": [], "cpu_pct": [], "ram_temp": [], "ram_power": []}
        self.set_size_request(-1, 280)
        self.set_hexpand(True)
        if _HAS_CAIRO:   # sans python3-gi-cairo, GTK lèverait « foreign struct converter for cairo.Context » à chaque dessin
            self.set_draw_func(self._draw_func)

    def reset(self):
        for v in self._pts.values():
            v.clear()
        self.queue_draw()

    def push(self, point: dict):
        self._pts["t"].append(point.get("t", 0))
        for k in _SERIES_FIXED:
            self._pts[k].append(point.get(k, 0))
        for k in _SERIES_OPT:
            if k in point:
                self._pts[k].append(point[k])
        self.queue_draw()
        return False

    def get_series(self):
        return self._pts

    def _draw_func(self, _area, cr, w, h):
        if not _HAS_CAIRO:
            return
        cr.set_source_rgb(*_hex_rgb("1e1e2e")); cr.paint()
        cr.select_font_face("Sans", 0, 0)
        pts    = self._pts
        t_vals = pts["t"]

        if len(t_vals) < 2:
            cr.set_source_rgb(*_hex_rgb("6c7086"))
            cr.set_font_size(11)
            cr.move_to(w / 2 - 80, h / 2 + 4)
            cr.show_text("En attente de données…")
            return

        t_min = t_vals[0]; t_max = max(t_vals[-1], t_min + 1)
        ml = 46; mr = 10          # marges gauche/droite
        pw = w - ml - mr
        time_bar = 13             # hauteur axe du temps en bas

        # Ne garder que les panels qui ont au moins une série avec des données
        active_panels = [
            (lbl, [(k, sub, c) for k, sub, c in series if pts.get(k)])
            for lbl, series in _PANELS_DEF
        ]
        active_panels = [(lbl, s) for lbl, s in active_panels if s]
        if not active_panels:
            return

        panel_h = (h - time_bar) / len(active_panels)

        for pi, (p_lbl, p_series) in enumerate(active_panels):
            py = pi * panel_h     # haut du panneau

            # Fond alterné
            bg = "181825" if pi % 2 == 0 else "1e1e2e"
            cr.set_source_rgb(*_hex_rgb(bg))
            cr.rectangle(0, py, w, panel_h); cr.fill()

            # Séparateur horizontal
            if pi > 0:
                cr.set_source_rgba(*_hex_rgb("45475a"), 1.0)
                cr.set_line_width(0.5)
                cr.move_to(0, py); cr.line_to(w, py); cr.stroke()

            # Zone interne du panneau
            pt = py + 5; pb = py + panel_h - 5
            ph = pb - pt
            if ph < 8:
                continue

            # Min / max globaux du panneau
            all_v = [v for k, _, _ in p_series for v in pts.get(k, [])]
            vmin, vmax = min(all_v), max(all_v)
            if vmax == vmin: vmax = vmin + 1

            # Grille horizontale (0 %, 50 %, 100 %)
            cr.set_source_rgba(*_hex_rgb("313244"), 0.8)
            cr.set_line_width(0.4)
            for frac in (0.0, 0.5, 1.0):
                gy = pt + ph * frac
                cr.move_to(ml, gy); cr.line_to(w - mr, gy); cr.stroke()

            # Étiquettes axe Y
            cr.set_source_rgb(*_hex_rgb("585b70"))
            cr.set_font_size(7.5)
            cr.move_to(2, pt + 8);    cr.show_text(f"{vmax:.0f}")
            cr.move_to(2, pb);        cr.show_text(f"{vmin:.0f}")
            cr.set_font_size(7)
            cr.move_to(2, pt + ph / 2 + 3); cr.show_text(p_lbl)

            # Courbes
            for key, sub, col_hex in p_series:
                vals = pts.get(key, [])
                if not vals:
                    continue
                # Aligner les séries socket 1 (plus courtes) sur la fin de t_vals
                t_sub = t_vals[-len(vals):] if len(vals) < len(t_vals) else t_vals

                cr.set_source_rgb(*_hex_rgb(col_hex))
                cr.set_line_width(1.6)
                first = True
                for tv, v in zip(t_sub, vals):
                    x = ml + pw * (tv - t_min) / (t_max - t_min)
                    y = pt + ph * (1.0 - (v - vmin) / (vmax - vmin))
                    if first: cr.move_to(x, y); first = False
                    else:     cr.line_to(x, y)
                cr.stroke()

                # Point final + valeur courante
                tx = ml + pw * (t_sub[-1] - t_min) / (t_max - t_min)
                ty = pt + ph * (1.0 - (vals[-1] - vmin) / (vmax - vmin))
                cr.arc(tx, ty, 3, 0, 6.2832); cr.fill()
                tag = f"{sub}: {vals[-1]:.0f}" if sub else f"{vals[-1]:.0f}"
                cr.set_source_rgb(*_hex_rgb("cdd6f4"))
                cr.set_font_size(8)
                cr.move_to(min(tx + 5, w - mr - len(tag) * 5 - 2), ty + 4)
                cr.show_text(tag)

        # Axe du temps partagé
        cr.set_source_rgb(*_hex_rgb("585b70"))
        cr.set_font_size(7.5)
        cr.move_to(ml, h - 2); cr.show_text("0s")
        end_lbl = f"{int(t_vals[-1])}s"
        cr.move_to(w - mr - len(end_lbl) * 5, h - 2); cr.show_text(end_lbl)


# ─────────────────────────────────────────────────────────────────────────────
# Page principale
# ─────────────────────────────────────────────────────────────────────────────

class StressPage(Gtk.Box):

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win    = win
        self.result = None
        self.worker = None
        self._current_profile = None
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        self.append(self._build_sidebar())
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True); scroll.set_hexpand(True)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=18)
        main.set_margin_top(24); main.set_margin_start(28)
        main.set_margin_end(28); main.set_margin_bottom(24)

        # En-tête
        t_row = Gtk.Box(spacing=12)
        t_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        t_col.set_hexpand(True)
        t1 = Gtk.Label(label="CPU & RAM — stress-ng avec profils")
        t1.add_css_class("bench-title"); t1.set_halign(Gtk.Align.START)
        t2 = Gtk.Label(label="Profils adaptatifs · Charge CPU · Stress RAM · Combiné")
        t2.add_css_class("bench-subtitle"); t2.set_halign(Gtk.Align.START)
        t_col.append(t1); t_col.append(t2)
        badge = Gtk.Label()
        badge.set_markup('<span foreground="#f97316" background="#f9731622" weight="bold"> CPU + RAM </span>')
        badge.add_css_class("status-badge"); badge.set_valign(Gtk.Align.START)
        t_row.append(t_col); t_row.append(badge)
        main.append(t_row)

        if not tool_exists("stress-ng"):
            warn = Gtk.Label()
            warn.set_markup('<span foreground="#f9e2af" size="large">⚠  stress-ng non installé — sudo apt install stress-ng</span>')
            warn.set_halign(Gtk.Align.START)
            main.append(warn)

        # Profil sélectionné
        self._profile_info = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self._profile_info.add_css_class("params-panel")
        self._profile_title = Gtk.Label(label="Sélectionnez un profil à gauche")
        self._profile_title.add_css_class("params-title")
        self._profile_title.set_halign(Gtk.Align.START)
        self._profile_desc = Gtk.Label()
        self._profile_desc.set_halign(Gtk.Align.START)
        self._profile_desc.set_wrap(True)
        self._profile_cmd = Gtk.Label()
        self._profile_cmd.add_css_class("log-view")
        self._profile_cmd.set_halign(Gtk.Align.START)
        self._profile_cmd.set_selectable(True)
        self._profile_info.append(self._profile_title)
        self._profile_info.append(self._profile_desc)
        self._profile_info.append(self._profile_cmd)
        main.append(self._profile_info)

        # Paramètres overrides
        over_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        over_box.add_css_class("params-panel")
        self._ov_title = Gtk.Label(label="AJUSTEMENTS / MODE MANUEL")
        self._ov_title.add_css_class("params-title"); self._ov_title.set_halign(Gtk.Align.START)
        over_box.append(self._ov_title)

        grid = Gtk.Grid(); grid.set_row_spacing(10); grid.set_column_spacing(16)

        def spin(lo, hi, step, val):
            s = Gtk.SpinButton.new_with_range(lo, hi, step)
            s.set_value(val); s.set_size_request(120, -1); return s

        def row(r, label, widget):
            l = Gtk.Label(label=label); l.add_css_class("param-label"); l.set_halign(Gtk.Align.END)
            grid.attach(l, 0, r, 1, 1); grid.attach(widget, 1, r, 1, 1)
            return l

        n_cpu = os.cpu_count() or 4
        self._ov_time     = spin(5, 600, 5, 30)
        self._ov_cpu_w    = spin(0, n_cpu, 1, n_cpu)
        self._ov_vm_w     = spin(0, 64, 1, 0)
        self._ov_load     = spin(0, 99, 5, 0)

        sl = Gtk.StringList.new(STRESS_STRESSORS)
        self._ov_stressor = Gtk.DropDown(model=sl)
        self._ov_stressor.set_size_request(150, -1)
        self._ov_stressor.set_selected(STRESS_STRESSORS.index("all"))

        self._ov_vm_bytes = Gtk.Entry()
        self._ov_vm_bytes.set_text("512M")
        self._ov_vm_bytes.set_placeholder_text("ex: 512M, 2G, auto")
        self._ov_vm_bytes.set_size_request(120, -1)

        self._ov_extra_args = Gtk.Entry()
        self._ov_extra_args.set_placeholder_text("Profil normal: options en plus · CLI pur: ex: --cpu $NPROC --timeout 60s --metrics-brief")
        self._ov_extra_args.set_size_request(360, -1)

        self._ov_override = Gtk.CheckButton(label="Activer les ajustements (remplace le profil)")
        self._ov_override.connect("toggled", self._on_override_toggled)

        self._ov_param_rows = [
            (row(0, "Durée (s) :",                 self._ov_time), self._ov_time),
            (row(1, "CPU workers (0=désactivé) :", self._ov_cpu_w), self._ov_cpu_w),
            (row(2, "Méthode CPU :",               self._ov_stressor), self._ov_stressor),
            (row(3, "Load cible % (0=max) :",      self._ov_load), self._ov_load),
            (row(4, "VM workers (0=désactivé) :",  self._ov_vm_w), self._ov_vm_w),
            (row(5, "Mémoire/worker :",            self._ov_vm_bytes), self._ov_vm_bytes),
        ]
        self._ov_extra_label = row(6, "Options CLI stress-ng :", self._ov_extra_args)
        grid.attach(self._ov_override, 0, 7, 2, 1)
        over_box.append(grid)

        self._ov_hint = Gtk.Label()
        self._ov_hint.set_markup('<span foreground="#6c7086" size="small">'
                        'Mémoire : 512M, 2G, auto (70% RAM), auto_85pct (85% RAM) · CLI pur : support $NPROC, ${RAM_GB}, $(nproc), guillemets; pas de pipe/redirection</span>')
        self._ov_hint.set_halign(Gtk.Align.START)
        over_box.append(self._ov_hint)
        main.append(over_box)

        # Score + progress
        sb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        sb.add_css_class("score-box")
        st = Gtk.Label(label="RESULTAT"); st.add_css_class("params-title"); st.set_halign(Gtk.Align.START)
        self._score_val  = Gtk.Label(label="—")
        self._score_val.add_css_class("score-value"); self._score_val.set_halign(Gtk.Align.CENTER)
        self._score_unit = Gtk.Label(label="")
        self._score_unit.add_css_class("score-unit"); self._score_unit.set_halign(Gtk.Align.CENTER)
        self._progress = Gtk.ProgressBar()
        self._progress.set_fraction(0); self._progress.set_show_text(True)
        self._progress.set_text("En attente")
        sb.append(st); sb.append(self._score_val); sb.append(self._score_unit); sb.append(self._progress)
        main.append(sb)

        # Live chart
        self._chart = StressLiveChart()
        main.append(self._chart)

        # Métriques
        mt_lbl = Gtk.Label(label="MÉTRIQUES"); mt_lbl.add_css_class("params-title")
        mt_lbl.set_halign(Gtk.Align.START)
        main.append(mt_lbl)
        self._metrics_box = Gtk.FlowBox()
        self._metrics_box.set_max_children_per_line(4)
        self._metrics_box.set_min_children_per_line(2)
        self._metrics_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._metrics_box.set_column_spacing(10); self._metrics_box.set_row_spacing(10)
        main.append(self._metrics_box)

        # Boutons Run/Stop
        br = Gtk.Box(spacing=10)
        self._btn_run = Gtk.Button(label="Lancer le profil")
        self._btn_run.add_css_class("btn-run"); self._btn_run.set_hexpand(True)
        self._btn_run.set_sensitive(tool_exists("stress-ng"))
        self._btn_run.connect("clicked", self._on_run)
        self._btn_stop = Gtk.Button(label="Stopper")
        self._btn_stop.add_css_class("btn-stop"); self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)
        br.append(self._btn_run); br.append(self._btn_stop)
        main.append(br)

        # Export
        er = Gtk.Box(spacing=8)
        self._exp_all = Gtk.Button(label="Exporter CSV + JSON + PDF + HTML")
        self._exp_all.add_css_class("btn-json")
        self._exp_all.set_hexpand(True); self._exp_all.set_sensitive(False)
        self._exp_all.connect("clicked", self._on_export_all)
        er.append(self._exp_all)
        main.append(er)

        # Log brut
        exp = Gtk.Expander(label="Sortie brute stress-ng"); exp.set_margin_top(4)
        ls = Gtk.ScrolledWindow(); ls.set_min_content_height(100); ls.set_max_content_height(200)
        self._log_buf = Gtk.TextBuffer()
        lv = Gtk.TextView(buffer=self._log_buf)
        lv.set_editable(False); lv.set_cursor_visible(False)
        lv.add_css_class("log-view"); lv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ls.set_child(lv); exp.set_child(ls)
        main.append(exp)

        scroll.set_child(main)
        self.append(scroll)

    # ── Sidebar profils ───────────────────────────────────────────────────────

    def _build_sidebar(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.set_size_request(250, -1); outer.set_hexpand(False)   # panneau de profils : largeur fixe, la page garde le reste

        # En-tête sidebar
        hdr = Gtk.Box(spacing=8)
        hdr.set_margin_top(12); hdr.set_margin_start(12)
        hdr.set_margin_end(8); hdr.set_margin_bottom(8)
        hl = Gtk.Label(label="Profils"); hl.add_css_class("params-title")
        hl.set_halign(Gtk.Align.START); hl.set_hexpand(True)
        btn_new = Gtk.Button(label="+  Nouveau")
        btn_new.add_css_class("btn-json"); btn_new.set_size_request(-1, 28)
        btn_new.connect("clicked", self._on_new_profile)
        hdr.append(hl); hdr.append(btn_new)
        outer.append(hdr)
        outer.append(Gtk.Separator())

        # Bouton mode manuel
        btn_manual = Gtk.Button()
        btn_manual.add_css_class("btn-json")
        btn_manual.set_margin_start(8); btn_manual.set_margin_end(8)
        btn_manual.set_margin_top(6); btn_manual.set_margin_bottom(2)
        inner_m = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        inner_m.set_margin_top(4); inner_m.set_margin_bottom(4)
        inner_m.set_margin_start(6); inner_m.set_margin_end(6)
        lbl_m = Gtk.Label(label="⚙  Mode Manuel")
        lbl_m.set_halign(Gtk.Align.START)
        lbl_m2 = Gtk.Label()
        lbl_m2.set_markup('<span foreground="#6c7086" size="small">Tous les paramètres libres</span>')
        lbl_m2.set_halign(Gtk.Align.START)
        inner_m.append(lbl_m); inner_m.append(lbl_m2)
        btn_manual.set_child(inner_m)
        btn_manual.connect("clicked", self._on_manual_mode)
        self._btn_manual = btn_manual
        outer.append(btn_manual)
        outer.append(Gtk.Separator())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)    # pas de défilement horizontal : les noms passent à la ligne
        scroll.set_vexpand(True)

        self._sidebar_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._sidebar_box.set_margin_top(4); self._sidebar_box.set_margin_bottom(4)
        self._profile_btns = {}
        self._rebuild_sidebar()

        scroll.set_child(self._sidebar_box)
        outer.append(scroll)
        return outer

    def _rebuild_sidebar(self):
        c = self._sidebar_box.get_first_child()
        while c:
            n = c.get_next_sibling(); self._sidebar_box.remove(c); c = n
        self._profile_btns = {}

        prev_cat = None
        for p in get_all_profiles():
            cat = p.get("category", "Autres")
            if cat != prev_cat:
                color = CAT_COLORS.get(cat, "#f97316")
                cl = Gtk.Label()
                cl.set_markup(f'<span foreground="{color}" weight="bold" size="small">  {cat.upper()}</span>')
                cl.set_halign(Gtk.Align.START)
                cl.set_margin_top(8); cl.set_margin_start(8)
                self._sidebar_box.append(cl)
                prev_cat = cat

            row = Gtk.Box(spacing=0)
            row.set_margin_start(4); row.set_margin_end(4)

            btn = Gtk.Button()
            btn.set_hexpand(True)
            btn_inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            btn_inner.set_margin_top(6); btn_inner.set_margin_bottom(6)
            btn_inner.set_margin_start(8); btn_inner.set_margin_end(8)
            name_lbl = Gtk.Label(label=f'{p.get("icon","●")}  {p["name"]}')
            name_lbl.set_halign(Gtk.Align.START); name_lbl.set_wrap(True); name_lbl.set_xalign(0)
            name_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR); name_lbl.set_max_width_chars(21)   # le nom passe à la ligne au lieu d'être coupé
            params_lbl = Gtk.Label()
            params_esc = GLib.markup_escape_text(describe_params(p.get("params", {})))
            params_lbl.set_markup(
                f'<span foreground="#6c7086" size="small">{params_esc}</span>')
            params_lbl.set_halign(Gtk.Align.START)
            btn_inner.append(name_lbl); btn_inner.append(params_lbl)
            btn.set_child(btn_inner)
            btn.connect("clicked", self._on_profile_select, p)
            row.append(btn)

            # Bouton édition/suppression pour les profils utilisateur
            if not p.get("builtin", False):
                actions = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
                actions.set_margin_start(2)
                b_edit = Gtk.Button(label="✏")
                b_edit.set_size_request(28, -1)
                b_edit.set_tooltip_text("Modifier")
                b_edit.connect("clicked", self._on_edit_profile, p)
                b_del = Gtk.Button(label="✕")
                b_del.set_size_request(28, -1)
                b_del.add_css_class("btn-stop")
                b_del.set_tooltip_text("Supprimer")
                b_del.connect("clicked", self._on_delete_profile, p)
                actions.append(b_edit); actions.append(b_del)
                row.append(actions)

            self._sidebar_box.append(row)
            self._profile_btns[p["id"]] = btn
        self._mark_selected_profile()

    # ── Sélection d'un profil ─────────────────────────────────────────────────

    def _mark_selected_profile(self):
        """Surligne le profil choisi (classe CSS « profile-selected ») ; « Mode Manuel » = aucun profil surligné, lui-même surligné."""
        cur = getattr(self, "_current_profile", None)
        cur_id = cur.get("id") if cur else None
        for pid, b in getattr(self, "_profile_btns", {}).items():
            if pid == cur_id:
                b.add_css_class("profile-selected")
            else:
                b.remove_css_class("profile-selected")
        bm = getattr(self, "_btn_manual", None)
        if bm is not None:
            if cur is None and getattr(self, "_manual_active", False):
                bm.add_css_class("profile-selected")
            else:
                bm.remove_css_class("profile-selected")

    def _on_profile_select(self, _btn, profile: dict):
        self._current_profile = profile
        self._manual_active = False
        self._mark_selected_profile()
        p = resolve_auto(profile.get("params", {}))

        title_text = f'{profile.get("icon","●")}  {profile["name"]}'
        title_esc = GLib.markup_escape_text(title_text)
        desc_esc = GLib.markup_escape_text(profile.get("description", ""))
        cmd_esc = GLib.markup_escape_text(profile_to_cmd(profile))

        self._profile_title.set_markup(f'<b>{title_esc}</b>')
        self._profile_desc.set_markup(f'<span foreground="#6c7086">{desc_esc}</span>')
        self._profile_cmd.set_markup(f'<span font_family="monospace" size="small">{cmd_esc}</span>')

        # Pré-remplir les overrides avec les valeurs résolues
        self._ov_time.set_value(p.get("time_s", 30))
        self._ov_cpu_w.set_value(p.get("cpu_workers", 0))
        self._ov_vm_w.set_value(p.get("vm_workers", 0))
        self._ov_load.set_value(p.get("cpu_load_pct", 0))
        stressor = p.get("cpu_stressor", "all")
        if stressor in STRESS_STRESSORS:
            self._ov_stressor.set_selected(STRESS_STRESSORS.index(stressor))
        self._ov_vm_bytes.set_text(str(p.get("vm_bytes", "512M")))
        cli_only = _is_cli_profile(profile)
        if cli_only:
            self._ov_extra_args.set_text(_profile_cli_args(profile, p))
            self._ov_override.set_active(True)
            self._set_cli_only_ui(True)
            self._progress.set_text("Profil CLI pur : BenchSuite n'ajoute aucun paramètre stress-ng")
        else:
            self._ov_extra_args.set_text(str(p.get("stress_ng_extra_args", "")))
            self._ov_override.set_active(bool(profile.get("editable")))
            self._set_cli_only_ui(False)

    def _on_override_toggled(self, chk):
        sensitive = chk.get_active()
        for w in (self._ov_time, self._ov_cpu_w, self._ov_stressor,
                  self._ov_load, self._ov_vm_w, self._ov_vm_bytes, self._ov_extra_args):
            w.set_sensitive(sensitive)

    def _set_cli_only_ui(self, cli_only: bool):
        for label, widget in self._ov_param_rows:
            if label is not None:
                label.set_visible(not cli_only)
            if widget is not None:
                widget.set_visible(not cli_only)
        self._ov_override.set_visible(not cli_only)
        self._ov_title.set_text("LIGNE DE COMMANDE STRESS-NG" if cli_only else "AJUSTEMENTS / MODE MANUEL")
        self._ov_extra_label.set_text("Ligne de commande stress-ng :" if cli_only else "Options CLI stress-ng :")
        if cli_only:
            self._ov_extra_args.set_placeholder_text("--cpu $NPROC --timeout 60s --metrics-brief")
            self._ov_extra_args.set_sensitive(True)
            self._ov_hint.set_markup('<span foreground="#6c7086" size="small">'
                                     'Profil CLI pur : seule cette ligne est utilisée. BenchSuite n’ajoute pas de durée, CPU workers, VM workers ou load cible.</span>')
        else:
            self._ov_extra_args.set_placeholder_text("Options stress-ng en plus du profil sélectionné")
            self._ov_extra_args.set_sensitive(self._ov_override.get_active())
            self._ov_hint.set_markup('<span foreground="#6c7086" size="small">'
                                     'Mémoire : 512M, 2G, auto (70% RAM), auto_85pct (85% RAM)</span>')

    def _on_manual_mode(self, _):
        self._current_profile = None
        self._manual_active = True
        self._mark_selected_profile()
        n_cpu = os.cpu_count() or 4
        self._profile_title.set_markup("<b>⚙  Mode Manuel</b>")
        self._profile_desc.set_markup(
            '<span foreground="#6c7086">Configurez librement tous les paramètres '
            'dans la section ci-dessous, puis lancez.</span>')
        self._profile_cmd.set_text("")
        self._ov_cpu_w.set_value(n_cpu)
        self._ov_vm_w.set_value(0)
        self._ov_load.set_value(0)
        self._ov_time.set_value(30)
        self._ov_stressor.set_selected(STRESS_STRESSORS.index("all"))
        self._ov_vm_bytes.set_text("512M")
        self._ov_extra_args.set_text("")
        self._ov_override.set_active(True)
        self._set_cli_only_ui(False)

    # ── Actions profils utilisateur ───────────────────────────────────────────

    def _on_new_profile(self, _):
        dlg = StressProfileDialog(self.win, on_saved=self._on_profile_saved)
        dlg.present()

    def _on_edit_profile(self, _, profile):
        dlg = StressProfileDialog(self.win, existing=profile, on_saved=self._on_profile_saved)
        dlg.present()

    def _on_delete_profile(self, _, profile):
        delete_user_profile(profile["id"])
        if self._current_profile and self._current_profile["id"] == profile["id"]:
            self._current_profile = None
        self._rebuild_sidebar()

    def _on_profile_saved(self, _profile):
        self._rebuild_sidebar()

    # ── Run / Stop ────────────────────────────────────────────────────────────

    def _on_run(self, _):
        if not self._current_profile and not self._ov_override.get_active():
            if hasattr(self, 'win'):
                self.win._show_dialog("Aucun profil",
                    "Sélectionnez un profil ou activez le Mode Manuel.")
            return

        # Construire les paramètres
        if self._ov_override.get_active():
            item = self._ov_stressor.get_selected_item()
            stressor = item.get_string() if item else "all"
            base = resolve_auto(self._current_profile.get("params", {})) \
                   if self._current_profile else {}
            p = dict(base)
            p["time_s"]        = int(self._ov_time.get_value())
            p["cpu_workers"]   = int(self._ov_cpu_w.get_value())
            p["cpu_stressor"]  = stressor
            p["vm_workers"]    = int(self._ov_vm_w.get_value())
            p["vm_bytes"]      = self._ov_vm_bytes.get_text().strip() or "512M"
            p["cpu_load_pct"]  = int(self._ov_load.get_value())
            if self._current_profile and _is_cli_profile(self._current_profile):
                p["cli_only"] = True
                p["stress_ng_cli_args"] = self._ov_extra_args.get_text().strip()
                p["stress_ng_extra_args"] = ""
                if not p["stress_ng_cli_args"]:
                    self._score_val.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
                    self._score_unit.set_text("")
                    self._progress.set_fraction(0)
                    self._progress.set_text("Erreur : ligne de commande stress-ng vide")
                    self._log_buf.set_text("Aucune option CLI fournie pour stress-ng.")
                    self._btn_run.set_sensitive(tool_exists("stress-ng"))
                    self._btn_stop.set_sensitive(False)
                    self._chart.reset()
                    return
            else:
                p["stress_ng_extra_args"] = self._ov_extra_args.get_text().strip()
        else:
            p = resolve_auto(self._current_profile.get("params", {}))

        # Nom du profil testé : repris comme nom du résultat (interface, historique, rapports)
        if self._current_profile:
            label = self._current_profile.get("name", "")
            if self._ov_override.get_active():
                label += " (paramètres modifiés)"
            p["profile_name"] = label
            p["profile_id"] = self._current_profile.get("id", "")
        else:
            p["profile_name"] = "Stress manuel"

        # Réinitialiser UI
        self._clear_metrics()
        self._score_val.set_markup('<span foreground="#6c7086">...</span>')
        self._score_unit.set_text("")
        self._progress.set_fraction(0); self._progress.set_text("Démarrage…")
        self._log_buf.set_text("")
        self._chart.reset()
        self._btn_run.set_sensitive(False); self._btn_stop.set_sensitive(True)
        self._exp_all.set_sensitive(False)

        if hasattr(self, 'win'):
            name = self._current_profile['name'] if self._current_profile else "Mode Manuel"
            self.win._set_status(f"Stress : {name}…")

        self.worker = StressWorker(p, self._on_progress, self._on_done, self._on_series)
        self.worker.start()

    def _on_stop(self, _):
        if self.worker: self.worker.stop()
        self._btn_stop.set_sensitive(False)
        self._progress.set_text("Arrêté")

    def _on_progress(self, pct, msg):
        self._progress.set_fraction(pct / 100)
        self._progress.set_text(msg)
        return False

    def _on_series(self, point: dict):
        self._chart.push(point)
        return False

    def _on_done(self, result: BenchmarkResult):
        self.result = result
        self._btn_run.set_sensitive(tool_exists("stress-ng"))
        self._btn_stop.set_sensitive(False)

        if result.status == "ok":
            if result.score is not None:
                self._score_val.set_markup(
                    f'<span foreground="#f97316" weight="ultrabold">{result.score:.1f}</span>')
            else:
                self._score_val.set_markup('<span foreground="#a6e3a1" size="xx-large">OK</span>')
            self._score_unit.set_markup(f'<span foreground="#6c7086">{GLib.markup_escape_text(str(result.unit))}</span>')
            self._progress.set_fraction(1.0); self._progress.set_text("Terminé")
        else:
            self._score_val.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
            self._progress.set_text("Erreur")

        self._populate_metrics(result)
        self._log_buf.set_text(result.raw_output[:8000] if result.raw_output else "")
        self._exp_all.set_sensitive(True)

        save_to_history([result])
        if hasattr(self, 'win'):
            self.win._on_bench_result(result)
        return False

    # ── Métriques ─────────────────────────────────────────────────────────────

    def _clear_metrics(self):
        c = self._metrics_box.get_first_child()
        while c:
            n = c.get_next_sibling(); self._metrics_box.remove(c); c = n

    def _populate_metrics(self, result: BenchmarkResult):
        self._clear_metrics()
        from core.electrical import summary_cards
        for title, txt in summary_cards(result.metrics):
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(220, -1)
            lbl = Gtk.Label(label=title); lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START)
            val = Gtk.Label(label=txt); val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START); val.set_wrap(True)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)
        for k, v in result.metrics.items():
            if isinstance(v, (list, dict)) or k.startswith("series_"):
                continue
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(150, -1)
            lbl = Gtk.Label(label=k.replace("_", " ").upper())
            lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START); lbl.set_wrap(True)
            val = Gtk.Label(label=str(v))
            val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START); val.set_wrap(True)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)

    # ── Export ────────────────────────────────────────────────────────────────

    def _on_export_all(self, _):
        if not self.result: return
        results, sysinfo = [self.result], self.win.system_info_snapshot()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, system_info_data=sysinfo)
            return "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())
        self.win.export_async(work, "Export complet réussi")
