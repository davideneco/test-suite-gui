"""
sequence_page.py — Séquence automatique : enchaîner des tests, ou les lancer EN MÊME TEMPS.

On construit la séquence en blocs :
  • bloc « Les uns après les autres » : les tests du bloc s'enchaînent ;
  • bloc « En même temps » : les tests du bloc tournent en parallèle
    (ex. burn CPU + gpu-fryer + profil SSD pour chauffer toute la machine d'un coup).
Les blocs s'exécutent dans l'ordre. La séquence peut être enregistrée (~/.benchsuite/suites, lancable aussi par
`cli.py --suite`) ; à la fin : inventaire avant/après, verdict et rapports HTML/PDF/JSON/CSV.
"""

import os
import subprocess
import threading

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Pango

try:
    import cairo
    gi.require_foreign("cairo")   # exige python3-gi-cairo : sinon « foreign struct converter for cairo.Context » à chaque dessin
    _HAS_CAIRO = True
except Exception:       # pycairo absent : pas de graphique, le reste fonctionne
    _HAS_CAIRO = False

from core import campaign as cp
from core.safety import nvidia_smi_usable
from core.runner import (CampaignRunner, validate_steps, parallel_conflicts, list_suites, load_suite)

OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")
MODE_SEQ, MODE_PAR = "seq", "par"
MODE_LABELS = ["Les uns après les autres", "En même temps (parallèle)"]
KIND_ICON = {"stress": "🔥", "bench": "🎮", "fio": "💾", "pause": "⏸"}


def _name_of(kind: str, ident: str) -> str:
    """Nom lisible d'un test (profil ou test du catalogue)."""
    try:
        if kind == "stress":
            from core.stress_profiles import get_profile
            return (get_profile(ident) or {}).get("name", ident)
        if kind == "fio":
            from core.fio_profiles import get_profile
            return (get_profile(ident) or {}).get("name", ident)
        if kind == "bench":
            from core.benchmarks import CATALOG
            return next((b["name"] for b in CATALOG if b["id"] == ident), ident)
    except Exception:
        pass
    return "Pause" if kind == "pause" else ident


_PALETTE = ["ff6600", "3b82f6", "22c55e", "e879f9", "facc15", "22d3ee", "f43f5e", "a3a3a3", "8b5cf6", "14b8a6", "84cc16", "ec4899"]


def _rgb(h: str):
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))


def _nice_range(vmin: float, vmax: float, floor_zero: bool):
    """Bornes d'axe : plus petit pas « rond » dont 4 intervalles couvrent les données (graduations rondes)."""
    lo0 = 0.0 if floor_zero else max(0.0, vmin - 3)
    for step in (1, 2, 5, 10, 15, 20, 25, 30, 40, 50, 60, 75, 100, 150, 200, 250, 500, 1000):
        lo = 0.0 if floor_zero else float(int(lo0 // step) * step)
        if lo + 4 * step >= vmax:
            return lo, lo + 4 * step
    return 0.0, float(int(vmax) + 1)


class SequenceChart(Gtk.DrawingArea):
    """Courbes température et puissance de toute la séquence (une ligne par GPU, CPU, RAM, disque), repères d'étapes."""

    PANELS = (("temp", "Température (°C)", False), ("power", "Puissance (W)", True))

    def __init__(self, recorder):
        super().__init__()
        self.rec = recorder
        self.set_size_request(-1, 330)
        self.set_hexpand(True); self.set_vexpand(True)
        if _HAS_CAIRO:
            self.set_draw_func(lambda _a, cr, w, h: self.draw_into(cr, w, h))

    def set_recorder(self, recorder):
        self.rec = recorder
        self.queue_draw()

    def save_png(self, path: str, w: int = 1300, h: int = 660) -> bool:
        """Enregistre le graphique en image (joint au dossier de campagne)."""
        if not _HAS_CAIRO:
            return False
        try:
            surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, w, h)
            self.draw_into(cairo.Context(surf), w, h)
            surf.write_to_png(path)
            return True
        except Exception:
            return False

    def draw_into(self, cr, w, h):
        """Dessine les deux panneaux (température en haut, puissance en bas) sur un contexte Cairo."""
        cr.set_source_rgb(*_rgb("1e1e2e")); cr.paint()
        snap = self.rec.snapshot()
        panels = [(k, t, z) for k, t, z in self.PANELS if any(c["panel"] == k and len(c["v"]) >= 2 for c in snap["channels"].values())]
        cr.select_font_face("Sans", 0, 0)
        if not panels:
            cr.set_source_rgb(*_rgb("6c7086")); cr.set_font_size(12)
            cr.move_to(max(10, w / 2 - 170), h / 2)
            cr.show_text("Les courbes de température et de puissance apparaîtront au lancement.")
            return
        # Couleur stable par entité (GPU 0, CPU, Disque…), identique entre les deux panneaux
        ents, order = {}, []
        for cid, c in sorted(snap["channels"].items()):
            ent = c["label"].replace(" cœur", "").replace(" hotspot", "")
            if ent not in ents:
                ents[ent] = _PALETTE[len(ents) % len(_PALETTE)]
        t_max = max(snap["duration"], max((c["t"][-1] for c in snap["channels"].values() if c["t"]), default=1.0), 1.0)
        ml, mr, mt, mb, gap = 52, 14, 8, 22, 14
        ph = (h - mt - mb - gap * (len(panels) - 1)) / len(panels)
        for pi, (key, title, floor_zero) in enumerate(panels):
            chans = [c for c in snap["channels"].values() if c["panel"] == key and len(c["v"]) >= 2]
            vals = [v for c in chans for v in c["v"]]
            lo, hi = _nice_range(min(vals), max(vals), floor_zero)
            y0 = mt + pi * (ph + gap)
            pw = w - ml - mr
            inner_top, inner_h = y0 + 22, ph - 22
            cr.set_source_rgb(*_rgb("cdd6f4")); cr.set_font_size(11)
            cr.move_to(ml, y0 + 12); cr.show_text(title)
            # Légende avec dernière valeur
            lx = ml + 150
            cr.set_font_size(9)
            for c in chans:
                col = _rgb(ents[c["label"].replace(" cœur", "").replace(" hotspot", "")])
                txt = f"{c['label']} {c['v'][-1]:.0f}{c['unit']}"
                if lx + 18 + len(txt) * 5.3 > w - mr:
                    break
                cr.set_source_rgb(*col)
                if c["dash"]:
                    cr.set_dash([3, 2]); cr.set_line_width(2); cr.move_to(lx, y0 + 9); cr.line_to(lx + 12, y0 + 9); cr.stroke(); cr.set_dash([])
                else:
                    cr.rectangle(lx, y0 + 6, 12, 5); cr.fill()
                cr.set_source_rgb(*_rgb("cdd6f4")); cr.move_to(lx + 16, y0 + 12); cr.show_text(txt)
                lx += 24 + len(txt) * 5.3
            # Cadre, grille, graduations
            cr.set_source_rgba(*_rgb("45475a"), 0.5); cr.set_line_width(0.6)
            for i in range(5):
                y = inner_top + inner_h * i / 4
                cr.move_to(ml, y); cr.line_to(ml + pw, y); cr.stroke()
                cr.set_source_rgb(*_rgb("6c7086")); cr.set_font_size(9)
                cr.move_to(4, y + 3); cr.show_text(f"{hi - (hi - lo) * i / 4:.0f}")
                cr.set_source_rgba(*_rgb("45475a"), 0.5)
            if pi == len(panels) - 1:
                cr.set_source_rgb(*_rgb("6c7086")); cr.set_font_size(9)
                for i in range(6):
                    x = ml + pw * i / 5
                    cr.move_to(x - 10, h - 6); cr.show_text(f"{t_max * i / 5 / 60:.0f} min" if t_max >= 120 else f"{t_max * i / 5:.0f} s")
            # Repères d'étapes
            for mi, m in enumerate(snap["marks"]):
                x = ml + pw * min(1.0, m["t"] / t_max)
                cr.set_source_rgba(*_rgb("f9e2af"), 0.55); cr.set_line_width(0.8); cr.set_dash([4, 3])
                cr.move_to(x, inner_top); cr.line_to(x, inner_top + inner_h); cr.stroke(); cr.set_dash([])
                if pi == 0:
                    cr.set_source_rgb(*_rgb("f9e2af")); cr.set_font_size(9); cr.move_to(x + 3, inner_top + 10); cr.show_text(str(mi + 1))
            # Courbes
            for c in chans:
                cr.set_source_rgb(*_rgb(ents[c["label"].replace(" cœur", "").replace(" hotspot", "")]))
                cr.set_line_width(1.8)
                cr.set_dash([5, 3] if c["dash"] else [])
                first = True
                for t, v in zip(c["t"], c["v"]):
                    x = ml + pw * t / t_max
                    y = inner_top + inner_h * (1 - (v - lo) / (hi - lo))
                    if first:
                        cr.move_to(x, y); first = False
                    else:
                        cr.line_to(x, y)
                cr.stroke(); cr.set_dash([])


class SequencePage(Gtk.Box):
    """Onglet « Séquence automatique »."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.win = win
        self.blocks = [{"mode": MODE_SEQ, "items": []}]
        self._runner = None
        self._running = False
        self._run_dir = ""
        self._html_path = ""
        self._cur_i, self._cur_n = 0, 1
        self._gpu_checks = []
        self._disk_checks = []
        self._disks = []
        self._gpu_tools = None
        self._active_profile_id = None
        self._suite_slug = "sequence"
        self._profile_btns = {}
        self._rec = cp.SequenceRecorder()
        self._timer = 0
        self._stop_requested = False
        for m in ("top", "bottom", "start", "end"):
            getattr(self, f"set_margin_{m}")(16)
        self._build()
        self._render()
        threading.Thread(target=self._detect_hardware, daemon=True).start()

    # ── Construction de l'interface ─────────────────────────────────────────
    def _build(self):
        title = Gtk.Label()
        title.set_markup('<span size="large" weight="bold" foreground="#ff6600">🧬 Séquence automatique</span>')
        title.set_halign(Gtk.Align.START)
        self.append(title)
        sub = Gtk.Label()
        sub.set_markup('<span foreground="#6c7086" size="small">Choisissez un profil prêt à l’emploi ou composez votre séquence par blocs. '
                       'Priorité chauffe, stabilité et détection d’erreurs (parallèle par défaut, bascule séquentielle en 1 clic).</span>')
        sub.set_halign(Gtk.Align.START); sub.set_wrap(True)
        self.append(sub)

        # Barre d'actions épinglée en haut
        bar = Gtk.Box(spacing=8)
        self._btn_run = Gtk.Button(label="▶ Lancer la séquence")
        self._btn_run.add_css_class("btn-run"); self._btn_run.connect("clicked", self._on_run)
        self._btn_stop = Gtk.Button(label="⏹ Stopper")
        self._btn_stop.add_css_class("btn-stop"); self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)
        self._models = Gtk.MenuButton(label="📂 Modèles")
        self._models_pop = Gtk.Popover()
        self._models.set_popover(self._models_pop)
        self._models.connect("notify::active", lambda *_: self._fill_models() if self._models.get_active() else None)
        btn_save = Gtk.Button(label="💾 Enregistrer"); btn_save.connect("clicked", self._on_save)
        btn_clear = Gtk.Button(label="🧹 Vider"); btn_clear.connect("clicked", self._on_clear)
        for w in (self._btn_run, self._btn_stop, self._models, btn_save, btn_clear):
            bar.append(w)
        self._btn_open = Gtk.Button(label="📁 Ouvrir le dossier"); self._btn_open.set_sensitive(False)
        self._btn_open.connect("clicked", lambda *_: self._open(self._run_dir))
        self._btn_html = Gtk.Button(label="🌐 Ouvrir le rapport"); self._btn_html.set_sensitive(False)
        self._btn_html.connect("clicked", lambda *_: self._open(self._html_path))
        bar.append(self._btn_open); bar.append(self._btn_html)
        self.append(bar)

        # Encart Profils rapides (CPU+RAM+GPU+SSD, CPU+RAM+GPU, CPU+RAM+SSD) + palier + mode + option perf
        prof_frame = Gtk.Frame()
        prof_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        for m in ("top", "bottom", "start", "end"):
            getattr(prof_box, f"set_margin_{m}")(10)

        p_row = Gtk.Box(spacing=8)
        p_lbl = Gtk.Label(); p_lbl.set_markup('<b>Profils rapides :</b>')
        p_lbl.set_halign(Gtk.Align.START)
        p_row.append(p_lbl)
        for prof in cp.SEQUENCE_PROFILES:
            btn = Gtk.ToggleButton(label=f"{prof['icon']} {prof['name']}")
            btn.set_tooltip_text(prof["desc"])
            btn.connect("toggled", self._on_profile_toggled, prof["id"])
            self._profile_btns[prof["id"]] = btn
            p_row.append(btn)
        prof_box.append(p_row)

        opts_row = Gtk.Box(spacing=12)
        opts_row.append(self._label("Palier :"))
        self._tier_ids = [t["id"] for t in cp.DURATION_TIERS]
        self._tier_dd = Gtk.DropDown(model=Gtk.StringList.new([t["label"] for t in cp.DURATION_TIERS]))
        self._tier_dd.set_selected(1)   # Standard (30 min) par défaut
        self._tier_dd.connect("notify::selected", lambda *_: self._refresh_active_profile())
        opts_row.append(self._tier_dd)

        opts_row.append(self._label("Exécution :"))
        self._exec_mode_dd = Gtk.DropDown(model=Gtk.StringList.new([
            "Parallèle (chauffe max simultanée)",
            "Séquentiel (étape par étape)",
        ]))
        self._exec_mode_dd.set_selected(0)   # Parallèle par défaut
        self._exec_mode_dd.connect("notify::selected", lambda *_: self._refresh_active_profile())
        opts_row.append(self._exec_mode_dd)

        self._perf_chk = Gtk.CheckButton(label="Contrôle perf. en fin (STREAM / nvbandwidth / FIO Peak)")
        self._perf_chk.set_active(False)
        self._perf_chk.set_tooltip_text("Optionnel (décoché par défaut) : ajoute en fin de séquence un contrôle rapide de bande passante et débit.")
        self._perf_chk.connect("toggled", lambda *_: self._refresh_active_profile())
        opts_row.append(self._perf_chk)
        prof_box.append(opts_row)

        self._gpu_backend_lbl = Gtk.Label()
        self._gpu_backend_lbl.set_halign(Gtk.Align.START); self._gpu_backend_lbl.set_wrap(True)
        self._gpu_backend_lbl.set_markup('<span foreground="#6c7086" size="small">Détection des disques et des outils GPU…</span>')
        prof_box.append(self._gpu_backend_lbl)
        prof_frame.set_child(prof_box)
        self.append(prof_frame)

        # Options matérielles (grille à 2 colonnes : multi-disques + multi-GPU + pause + arrêt panne)
        grid = Gtk.Grid(column_spacing=14, row_spacing=6)
        lbl = lambda t: self._label(t)
        disk_head = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        disk_head.append(lbl("Disques de test (fio) :"))
        self._disk_all_btn = Gtk.Button(label="Tout cocher")
        self._disk_all_btn.add_css_class("flat")
        self._disk_all_btn.set_halign(Gtk.Align.END)
        self._disk_all_btn.set_visible(False)
        self._disk_all_btn.connect("clicked", self._on_toggle_all_disks)
        disk_head.append(self._disk_all_btn)
        grid.attach(disk_head, 0, 0, 1, 1)

        self._disk_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, column_spacing=8, row_spacing=2)
        self._disk_box.append(Gtk.Label(label="détection…"))
        disk_sc = Gtk.ScrolledWindow()
        disk_sc.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        disk_sc.set_max_content_height(72); disk_sc.set_propagate_natural_height(True)
        disk_sc.set_hexpand(True); disk_sc.set_child(self._disk_box)
        grid.attach(disk_sc, 1, 0, 1, 1)

        grid.attach(lbl("GPU à tester :"), 2, 0, 1, 1)
        self._gpu_box = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=3, column_spacing=8, row_spacing=2)
        self._gpu_box.append(Gtk.Label(label="détection…"))
        grid.attach(self._gpu_box, 3, 0, 1, 1)

        grid.attach(lbl("Pause entre blocs (s) :"), 0, 1, 1, 1)
        self._cool = Gtk.SpinButton.new_with_range(0, 3600, 10); self._cool.set_value(60)
        self._cool.set_halign(Gtk.Align.START)
        grid.attach(self._cool, 1, 1, 1, 1)
        self._stop_fault = Gtk.CheckButton(label="Arrêter si panne matérielle critique")
        self._stop_fault.set_active(True)
        grid.attach(self._stop_fault, 2, 1, 2, 1)
        self.append(grid)

        # Deux onglets : construction de la séquence / suivi en direct (graphiques + journal)
        self._nb = Gtk.Notebook(); self._nb.set_vexpand(True)
        build_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._builder = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        sc = Gtk.ScrolledWindow(); sc.set_vexpand(True); sc.set_min_content_height(240)
        sc.set_child(self._builder)
        build_tab.append(sc)
        self._summary = Gtk.Label(); self._summary.set_halign(Gtk.Align.START); self._summary.set_wrap(True)
        self._warn = Gtk.Label(); self._warn.set_halign(Gtk.Align.START); self._warn.set_wrap(True)
        build_tab.append(self._summary); build_tab.append(self._warn)
        self._nb.append_page(build_tab, Gtk.Label(label="🧩 Construction"))

        live_tab = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._chart = SequenceChart(self._rec)
        if _HAS_CAIRO:
            live_tab.append(self._chart)
        else:
            nc = Gtk.Label(label="Graphiques indisponibles : pycairo manquant (sudo apt install python3-gi-cairo). "
                                 "Les courbes sont tout de même enregistrées dans les rapports.")
            nc.set_wrap(True); nc.set_vexpand(True)
            live_tab.append(nc)
        self._log = Gtk.TextView(editable=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        self._log.set_cursor_visible(False)
        lsc = Gtk.ScrolledWindow(); lsc.set_min_content_height(110); lsc.set_max_content_height(150)
        lsc.set_propagate_natural_height(True); lsc.set_child(self._log)
        live_tab.append(lsc)
        self._nb.append_page(live_tab, Gtk.Label(label="📈 Suivi & graphiques"))
        self.append(self._nb)

        self._progress = Gtk.ProgressBar(); self._progress.set_show_text(True); self._progress.set_text("Prêt")
        self.append(self._progress)

    def _label(self, text: str) -> Gtk.Label:
        l = Gtk.Label(label=text); l.add_css_class("param-label"); l.set_halign(Gtk.Align.END)
        return l

    # ── Profils rapides ─────────────────────────────────────────────────────
    def _current_tier(self) -> str:
        i = self._tier_dd.get_selected()
        return self._tier_ids[i] if 0 <= i < len(self._tier_ids) else "standard"

    def _current_exec_mode(self) -> str:
        return MODE_SEQ if self._exec_mode_dd.get_selected() == 1 else MODE_PAR

    def _on_profile_toggled(self, btn, profile_id: str):
        if not btn.get_active():
            if self._active_profile_id == profile_id:
                self._active_profile_id = None
            return
        for pid, other in self._profile_btns.items():
            if pid != profile_id and other.get_active():
                other.set_active(False)
        self._active_profile_id = profile_id
        self._apply_active_profile()

    def _refresh_active_profile(self):
        if self._active_profile_id:
            self._apply_active_profile()

    def _apply_active_profile(self):
        if not self._active_profile_id or self._running:
            return
        spec = cp.build_profile_blocks(
            self._active_profile_id,
            tier=self._current_tier(),
            mode=self._current_exec_mode(),
            include_perf=self._perf_chk.get_active(),
            gpu_tools=self._gpu_tools,
        )
        self.blocks = spec["blocks"]
        self._suite_slug = spec["slug"]
        self._cool.set_value(float(spec.get("cooldown_s", 60)))
        if spec.get("gpu_label"):
            self._gpu_backend_lbl.set_markup(
                f'<span foreground="#a0a0a0" size="small">Profil actif : <b>{GLib.markup_escape_text(spec["name"])}</b>'
                f' · Outil GPU auto : <tt>{GLib.markup_escape_text(spec["gpu_label"])}</tt></span>'
            )
        else:
            self._gpu_backend_lbl.set_markup(
                f'<span foreground="#a0a0a0" size="small">Profil actif : <b>{GLib.markup_escape_text(spec["name"])}</b></span>'
            )
        self._render()

    # ── Détection matériel (thread) ─────────────────────────────────────────
    def _detect_hardware(self):
        disks, gpus = [], []
        try:
            from core.fio_profiles import get_mounted_disks
            disks = get_mounted_disks(max_age_s=15)
        except Exception:
            pass
        has_nvidia = False
        try:
            if nvidia_smi_usable():
                out = subprocess.run(["nvidia-smi", "--query-gpu=index,name", "--format=csv,noheader"],
                                     capture_output=True, text=True, timeout=6).stdout
                for line in out.splitlines():
                    idx, _, name = line.partition(",")
                    if idx.strip().isdigit():
                        has_nvidia = True
                        gpus.append((idx.strip(), f"GPU {idx.strip()} — {name.strip()}", "nvidia"))
        except Exception:
            pass
        if not gpus:
            # Détection best-effort des GPU Intel / AMD pour l'affichage et le choix de profil
            try:
                from core.intel_gpu import detect_intel_gpus
                for i, ig in enumerate(detect_intel_gpus() or []):
                    nm = ig.get("nom") or ig.get("modele") or f"Intel GPU {i}"
                    gpus.append((str(i), f"GPU {i} — {nm} (Intel)", "intel"))
            except Exception:
                pass
        if not gpus:
            try:
                out = subprocess.run(["lspci", "-nn"], capture_output=True, text=True, timeout=5).stdout
                idx_other = 0
                for line in out.splitlines():
                    low = line.lower()
                    if any(k in low for k in ("vga compatible", "3d controller", "display controller")):
                        if any(bmc in low for bmc in ("aspeed", "matrox", "cirrus", "bochs")):
                            continue
                        desc = line.split(":", 2)[-1].strip()
                        gpus.append((str(idx_other), f"GPU {idx_other} — {desc[:48]}", "other"))
                        idx_other += 1
            except Exception:
                pass
        gpu_tools = cp.select_gpu_tools(has_nvidia=has_nvidia)
        GLib.idle_add(self._apply_hardware, disks, gpus, gpu_tools)

    def _apply_hardware(self, disks, gpus, gpu_tools):
        # Disques de données uniquement : ni zram/loop/0 o, dossiers montés d'abord (jamais un disque OS pré-coché)
        disks = [d for d in disks if not any(x in (d.get("dev") or d.get("path") or "") for x in ("zram", "/dev/ram", "/dev/loop"))
                 and not str(d.get("size", "")).startswith("0B")]
        disks.sort(key=lambda d: not d.get("mounted", False))
        has_real_disk = bool(disks)
        if not disks:
            disks = [{"path": "/tmp", "label": "/tmp (aucun disque de données détecté — attention : disque système)", "mounted": True, "is_fallback": True}]
        self._disks = disks
        self._gpu_tools = gpu_tools

        child = self._disk_box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self._disk_box.remove(child); child = nxt
        self._disk_checks = []
        for i, d in enumerate(disks):
            lbl_txt = d.get("label", d.get("path", "?"))
            chk = Gtk.CheckButton(label=lbl_txt)
            chk._disk_path = d.get("path", "")
            chk.set_tooltip_text(d.get("path", ""))
            # Pré-cocher le 1er vrai disque de données détecté (jamais le repli /tmp)
            if has_real_disk and i == 0:
                chk.set_active(True)
            chk.connect("toggled", lambda *_: self._update_summary())
            self._disk_checks.append(chk)
            self._disk_box.append(chk)
        self._disk_all_btn.set_visible(len(self._disk_checks) >= 2)

        child = self._gpu_box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self._gpu_box.remove(child); child = nxt
        self._gpu_checks = []
        if not gpus:
            self._gpu_box.append(Gtk.Label(label="aucun GPU dédié détecté"))
        for idx, label, vendor in gpus:
            if vendor == "nvidia":
                chk = Gtk.CheckButton(label=label)
                chk._gpu_index = idx
                chk.connect("toggled", lambda *_: self._update_summary())
                self._gpu_checks.append(chk)
                self._gpu_box.append(chk)
            else:
                info_lbl = Gtk.Label(label=f"• {label}")
                info_lbl.set_halign(Gtk.Align.START)
                self._gpu_box.append(info_lbl)

        # Si aucun test n'est encore dans la séquence, pré-sélectionner le profil le plus adapté au matériel détecté
        if not any(b.get("items") for b in self.blocks) and not self._active_profile_id:
            if gpus and has_real_disk:
                default_pid = "cpu_ram_gpu_ssd"
            elif gpus:
                default_pid = "cpu_ram_gpu"
            else:
                default_pid = "cpu_ram_ssd"
            if default_pid in self._profile_btns:
                self._profile_btns[default_pid].set_active(True)
        elif self._active_profile_id:
            self._apply_active_profile()
        else:
            self._gpu_backend_lbl.set_markup(
                f'<span foreground="#6c7086" size="small">Outil GPU auto : <tt>{GLib.markup_escape_text(gpu_tools.get("label", ""))}</tt></span>'
            )
        return False

    def _on_toggle_all_disks(self, *_):
        if not self._disk_checks:
            return
        all_checked = all(c.get_active() for c in self._disk_checks)
        for c in self._disk_checks:
            c.set_active(not all_checked)
        self._disk_all_btn.set_label("Tout cocher" if all_checked else "Tout décocher")

    def _fio_path(self) -> str:
        selected = [c._disk_path for c in self._disk_checks if c.get_active() and getattr(c, "_disk_path", "")]
        return ",".join(selected)

    def _target_gpus(self) -> str:
        return ",".join(c._gpu_index for c in self._gpu_checks if c.get_active())

    # ── Rendu des blocs ─────────────────────────────────────────────────────
    def _render(self):
        child = self._builder.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self._builder.remove(child); child = nxt
        for bi, block in enumerate(self.blocks):
            self._builder.append(self._render_block(bi, block))
        add = Gtk.Button(label="＋ Ajouter un bloc")
        add.set_halign(Gtk.Align.START)
        add.connect("clicked", lambda *_: self._add_block())
        self._builder.append(add)
        self._update_summary()

    def _render_block(self, bi: int, block: dict) -> Gtk.Widget:
        frame = Gtk.Frame()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(8)
        head = Gtk.Box(spacing=8)
        title = Gtk.Label(); title.set_markup(f'<b>Bloc {bi + 1}</b>')
        head.append(title)
        mode = Gtk.DropDown(model=Gtk.StringList.new(MODE_LABELS))
        mode.set_selected(1 if block["mode"] == MODE_PAR else 0)
        mode.connect("notify::selected", self._on_mode, block)
        head.append(mode)
        spacer = Gtk.Box(); spacer.set_hexpand(True); head.append(spacer)
        for txt, tip, fn in (("▲", "Monter le bloc", lambda *_: self._move_block(bi, -1)),
                             ("▼", "Descendre le bloc", lambda *_: self._move_block(bi, 1)),
                             ("✕", "Supprimer le bloc", lambda *_: self._remove_block(bi))):
            b = Gtk.Button(label=txt); b.set_tooltip_text(tip); b.connect("clicked", fn)
            if txt == "✕":
                b.add_css_class("btn-stop")
            head.append(b)
        box.append(head)

        for ii, item in enumerate(block["items"]):
            box.append(self._render_item(bi, ii, item))
        if not block["items"]:
            hint = Gtk.Label(); hint.set_halign(Gtk.Align.START)
            hint.set_markup('<span foreground="#6c7086" size="small">Bloc vide — ajoute un test avec le bouton ci-dessous.</span>')
            box.append(hint)

        menu = Gtk.MenuButton(label="＋ Ajouter un test"); menu.set_halign(Gtk.Align.START)
        menu.set_popover(self._build_add_popover(block))
        box.append(menu)
        frame.set_child(box)
        return frame

    def _render_item(self, bi: int, ii: int, item: dict) -> Gtk.Widget:
        row = Gtk.Box(spacing=8)
        name = Gtk.Label(label=f'{KIND_ICON.get(item["kind"], "•")}  {_name_of(item["kind"], item.get("id", ""))}')
        name.set_halign(Gtk.Align.START); name.set_hexpand(True); name.set_ellipsize(Pango.EllipsizeMode.END)
        row.append(name)
        can = cp.supports_duration(item["kind"], item.get("id", ""))
        row.append(Gtk.Label(label="pause (s) :" if item["kind"] == "pause" else "durée (s) :"))
        spin = Gtk.SpinButton.new_with_range(0, 86400, 60)
        spin.set_value(item.get("time_s", 0) or (30 if item["kind"] == "pause" else 0))
        spin.set_sensitive(can)
        spin.set_tooltip_text("0 = durée du profil" if can else "Durée imposée par le test")
        spin.connect("value-changed", self._on_time, item)
        row.append(spin)
        rm = Gtk.Button(label="✕"); rm.set_tooltip_text("Retirer ce test")
        rm.connect("clicked", lambda *_: self._remove_item(bi, ii))
        row.append(rm)
        return row

    def _build_add_popover(self, block: dict) -> Gtk.Popover:
        pop = Gtk.Popover()
        sc = Gtk.ScrolledWindow(); sc.set_min_content_height(380); sc.set_min_content_width(380)
        inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            getattr(inner, f"set_margin_{m}")(6)

        def section(title):
            l = Gtk.Label(); l.set_markup(f'<span foreground="#f97316" weight="bold" size="small">{GLib.markup_escape_text(title)}</span>')
            l.set_halign(Gtk.Align.START); l.set_margin_top(8); inner.append(l)

        def entry(kind, ident, label):
            b = Gtk.Button(); b.add_css_class("flat")
            lab = Gtk.Label(label=label); lab.set_halign(Gtk.Align.START); lab.set_ellipsize(Pango.EllipsizeMode.END)
            b.set_child(lab)
            b.connect("clicked", lambda *_: (pop.popdown(), self._add_item(block, kind, ident)))
            inner.append(b)

        from core.stress_profiles import get_all_profiles as stress_profiles
        from core.fio_profiles import get_all_profiles as fio_profiles
        from core.benchmarks import CATALOG, tool_exists
        section("CPU / RAM (stress-ng)")
        for p in stress_profiles():
            entry("stress", p["id"], p["name"])
        section("GPU & mémoire")
        for b in CATALOG:
            if b["id"].startswith("gpu_") or b["id"] == "ram_stream":
                miss = [t for t in b.get("requires", []) if not tool_exists(t)]
                entry("bench", b["id"], b["name"] + ("  (non installé)" if miss else ""))
        section("Stockage (fio) — disque choisi ci-dessus")
        for p in fio_profiles():
            entry("fio", p["id"], p["name"])
        if block.get("mode") != MODE_PAR:
            section("Divers")
            entry("pause", "", "⏸ Pause (refroidissement)")
        sc.set_child(inner)
        pop.set_child(sc)
        return pop

    # ── Actions sur le modèle ───────────────────────────────────────────────
    def _add_block(self):
        self.blocks.append({"mode": MODE_SEQ, "items": []})
        self._render()

    def _remove_block(self, bi: int):
        del self.blocks[bi]
        if not self.blocks:
            self.blocks.append({"mode": MODE_SEQ, "items": []})
        self._render()

    def _move_block(self, bi: int, d: int):
        j = bi + d
        if 0 <= j < len(self.blocks):
            self.blocks[bi], self.blocks[j] = self.blocks[j], self.blocks[bi]
            self._render()

    def _add_item(self, block: dict, kind: str, ident: str):
        block["items"].append({"kind": kind, "id": ident, "time_s": 30 if kind == "pause" else 0})
        self._render()

    def _remove_item(self, bi: int, ii: int):
        del self.blocks[bi]["items"][ii]
        self._render()

    def _on_mode(self, dd, _pspec, block):
        block["mode"] = MODE_PAR if dd.get_selected() == 1 else MODE_SEQ
        if block["mode"] == MODE_PAR:      # une pause n'a pas de sens en parallèle
            block["items"] = [i for i in block["items"] if i["kind"] != "pause"]
        self._render()

    def _on_time(self, spin, item):
        item["time_s"] = int(spin.get_value())
        self._update_summary()

    def _on_clear(self, *_):
        if self._running:
            return
        self._active_profile_id = None
        self._suite_slug = "sequence"
        for b in self._profile_btns.values():
            if b.get_active():
                b.set_active(False)
        self.blocks = [{"mode": MODE_SEQ, "items": []}]
        self._render()

    def _update_summary(self):
        n = sum(len(b["items"]) for b in self.blocks)
        est = cp.estimate_blocks_s(self.blocks)
        txt = f"{n} test(s) dans {len(self.blocks)} bloc(s)"
        if est["seconds"]:
            txt += f" — durée estimée ≈ {cp.format_duration(est['seconds'])}"
            if est["unknown"]:
                txt += f" (+ {est['unknown']} test(s) de durée variable)"
        fio_path = self._fio_path()
        if fio_path:
            n_d = len([p for p in fio_path.split(",") if p.strip()])
            txt += f" · {n_d} disque(s) FIO sélectionné(s)"
        d = cp.describe_blocks(self.blocks)
        self._summary.set_markup(f'<span foreground="#a0a0a0" size="small">{GLib.markup_escape_text(txt)}'
                                 + (f'\n{GLib.markup_escape_text(d)}' if d else "") + '</span>')
        warns = []
        steps = cp.blocks_to_steps(self.blocks, fio_path, self._target_gpus())
        flat = [s for st in steps for s in (st.get("parallel") or [st])]
        if any("fio" in s for s in flat) and not fio_path:
            warns.append("Aucun disque de test (fio) coché : sélectionnez au moins un disque de données ci-dessus, ou choisissez le profil CPU + RAM + GPU.")
        for st in steps:
            if "parallel" in st:
                warns += parallel_conflicts(st)
        self._warn.set_markup("".join(f'<span foreground="#f9e2af" size="small">⚠ {GLib.markup_escape_text(w)}</span>\n' for w in warns).rstrip())

    # ── Modèles / enregistrement ────────────────────────────────────────────
    def _fill_models(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(6)
        suites = list_suites()
        if not suites:
            box.append(Gtk.Label(label="Aucun modèle"))
        for s in suites:
            row = Gtk.Box(spacing=4)
            b = Gtk.Button(); b.add_css_class("flat")
            lab = Gtk.Label(label=f'{s["name"]}  ({s["steps"]} étapes)'); lab.set_halign(Gtk.Align.START)
            lab.set_tooltip_text(s.get("description", ""))
            b.set_child(lab); b.set_hexpand(True)
            b.connect("clicked", lambda *_, sid=s["id"]: (self._models_pop.popdown(), self._load_model(sid)))
            row.append(b)
            if cp.is_user_suite(s["path"]):
                d = Gtk.Button(label="🗑"); d.add_css_class("flat"); d.set_tooltip_text("Supprimer ce modèle")
                d.connect("clicked", lambda *_, sid=s["id"]: (cp.delete_user_suite(sid), self._models_pop.popdown()))
                row.append(d)
            box.append(row)
        self._models_pop.set_child(box)

    def _load_model(self, sid: str):
        try:
            suite = load_suite(sid)
        except Exception as exc:
            self._log_line(f"✗ Modèle illisible : {exc}")
            return
        self._active_profile_id = None
        for b in self._profile_btns.values():
            if b.get_active():
                b.set_active(False)
        self._suite_slug = sid or "sequence"
        self.blocks = cp.steps_to_blocks(suite.get("steps", [])) or [{"mode": MODE_SEQ, "items": []}]
        self._cool.set_value(float(suite.get("cooldown_s", 30) or 0))
        self._gpu_backend_lbl.set_markup(
            f'<span foreground="#a0a0a0" size="small">Modèle chargé : <b>{GLib.markup_escape_text(suite.get("name", sid))}</b></span>'
        )
        self._render()
        self._log_line(f"Modèle chargé : {suite.get('name', sid)}")

    def _on_save(self, *_):
        steps = cp.blocks_to_steps(self.blocks)
        if not steps:
            self._log_line("Rien à enregistrer : la séquence est vide.")
            return
        dlg = Gtk.Window(title="Enregistrer la séquence")
        dlg.set_transient_for(self.win); dlg.set_modal(True); dlg.set_default_size(380, 120)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        for m in ("top", "bottom", "start", "end"):
            getattr(box, f"set_margin_{m}")(14)
        box.append(Gtk.Label(label="Nom de la séquence :", halign=Gtk.Align.START))
        entry = Gtk.Entry(); entry.set_text("Ma séquence")
        box.append(entry)
        ok = Gtk.Button(label="💾 Enregistrer"); ok.add_css_class("btn-run")

        def save(*_):
            name = entry.get_text().strip() or "Ma séquence"
            path = cp.save_user_suite(name, steps, float(self._cool.get_value()), cp.describe_blocks(self.blocks))
            self._log_line(f"💾 Séquence enregistrée : {path}\n   (relançable aussi en ligne de commande : ./cli.py --suite \"{path}\")")
            dlg.close()
        ok.connect("clicked", save); entry.connect("activate", save)
        box.append(ok)
        dlg.set_child(box)
        dlg.present()

    # ── Exécution ───────────────────────────────────────────────────────────
    def _on_run(self, *_):
        if self._running:
            return
        fio_path = self._fio_path()
        steps = cp.blocks_to_steps(self.blocks, fio_path, self._target_gpus())
        errs = validate_steps(steps)
        flat = [s for st in steps for s in (st.get("parallel") or [st])]
        if any("fio" in s for s in flat) and not fio_path:
            errs.append("Aucun disque de test (fio) sélectionné : cochez au moins un disque de données (ou utilisez le profil CPU + RAM + GPU).")
        if errs:
            self._nb.set_current_page(1)
            self._log_line("✗ Séquence invalide :\n  - " + "\n  - ".join(errs))
            return
        self._running = True
        self._stop_requested = False
        self._btn_run.set_sensitive(False); self._btn_stop.set_sensitive(True)
        self._btn_open.set_sensitive(False); self._btn_html.set_sensitive(False)
        self._set_text(self._log, "")
        self._rec = cp.SequenceRecorder()
        self._chart.set_recorder(self._rec)
        self._nb.set_current_page(1)
        if not self._timer:
            self._timer = GLib.timeout_add(1500, self._tick)
        self._progress.set_fraction(0); self._progress.set_text("Démarrage…")
        sysinfo = None
        try:
            sysinfo = self.win.system_info_snapshot()
        except Exception:
            pass
        params = {
            "fio_path": fio_path,
            "cooldown": float(self._cool.get_value()),
            "stop_on_fault": self._stop_fault.get_active(),
            "suite_slug": self._suite_slug or "sequence",
        }
        threading.Thread(target=self._worker, args=(steps, sysinfo, params), daemon=True).start()

    def _on_stop(self, *_):
        self._stop_requested = True          # mémorisé : le moteur n'existe pas encore pendant le préflight / l'inventaire
        if self._runner:
            self._runner.request_stop()
        self._btn_stop.set_sensitive(False)
        self._progress.set_text("Arrêt en cours…")
        self._log_line("⏹ Arrêt demandé : les tests en cours s'arrêtent proprement.")

    def _worker(self, steps, sysinfo, params):
        """Thread : exécute la séquence ; toute exception est affichée et l'interface est TOUJOURS libérée."""
        try:
            self._worker_inner(steps, sysinfo, params)
        except Exception as exc:
            self._ui(self._log_line, f"✗ Erreur inattendue pendant la séquence : {type(exc).__name__}: {exc}")
            self._ui(self._finished, [], "aborted", {}, self._run_dir, True)

    def _worker_inner(self, steps, sysinfo, params):
        """Préflight, inventaire, campagne, rapports. L'interface n'est touchée que via GLib.idle_add."""
        from core import preflight, inventory
        from exporters.export import load_report_meta
        from core.identity import collect_identity, best_serial
        meta = dict(load_report_meta())
        try:
            ident = collect_identity()
            meta.setdefault("serial", best_serial(ident))
            meta.setdefault("model", " ".join(x for x in (ident.get("vendor"), ident.get("model")) if x))
        except Exception:
            pass
        meta.setdefault("system_name", meta.get("model") or "machine")
        suite_slug = params.get("suite_slug") or "sequence"
        run_dir = cp.make_run_dir(OUTPUT_DIR, meta, suite_slug)
        os.makedirs(run_dir, exist_ok=True)
        self._run_dir = run_dir
        self._ui(self._log_line, f"Séquence ({suite_slug}) : {cp.describe_blocks(self.blocks)}\nDossier : {run_dir}")
        for st in steps:
            for w in (parallel_conflicts(st) if "parallel" in st else []):
                self._ui(self._log_line, f"⚠ {w}")
        try:
            flat = [s for st in steps for s in (st.get("parallel") or [st])]
            need_fio = any("fio" in s for s in flat)
            fio_targets = [p.strip() for p in str(params.get("fio_path") or "").split(",") if p.strip()]
            items = preflight.run_preflight(out_dir=OUTPUT_DIR, target_paths=fio_targets if (need_fio and fio_targets) else [],
                                            target_need_mb=2048 if need_fio else 0,
                                            gpus=any(str(s.get("bench", "")).startswith("gpu") for s in flat))
            if items:
                self._ui(self._log_line, "Vérifications avant test :\n" + preflight.format_items(items))
            if preflight.has_blocking(items):
                self._ui(self._log_line, "✗ ARRÊT : corrigez les erreurs ci-dessus.")
                self._ui(self._finished, [], "aborted", {}, run_dir, True)
                return
        except Exception as exc:
            self._ui(self._log_line, f"(vérifications ignorées : {exc})")
        inv_before = None
        try:
            inv_before = inventory.collect_inventory()
            meta["inventory_before"] = inv_before
            self._ui(self._log_line, f"Inventaire (départ) : {inventory.summarize(inv_before)}")
        except Exception:
            pass

        if self._stop_requested:
            self._ui(self._log_line, "⏹ Séquence annulée avant le départ.")
            self._ui(self._finished, [], "aborted", {}, run_dir, True)
            return

        rec = self._rec

        def on_start(i, total, label, _step):
            rec.mark(f"{i + 1}. {label}"[:60])
            self._cur_i, self._cur_n = i, total
            self._ui(self._log_line, f"\n[{i + 1}/{total}] {label}")

        def on_progress(pct, msg=""):
            frac = min(1.0, (self._cur_i + max(0, min(100, pct)) / 100.0) / max(1, self._cur_n))
            self._ui(self._set_progress, frac, f"[{self._cur_i + 1}/{self._cur_n}] {msg}")

        def on_done(i, total, res):
            score = f"{res.score:.2f} {res.unit}" if res.score is not None else "—"
            sym = {"ok": "✓", "error": "✗", "skipped": "⚠", "stopped": "⏹"}.get(res.status, "•")
            note = f" — {res.notes[:160]}" if res.notes and res.status != "ok" else ""
            self._ui(self._log_line, f"   {sym} {res.name} : {res.status}, {score}{note}")
            self._ui(self._push_result, res)

        runner = CampaignRunner(steps, run_dir, meta, {"fio_path": params["fio_path"]} if params["fio_path"] else {},
                                cooldown_s=params["cooldown"], stop_on_critical=params["stop_on_fault"], suite_name=suite_slug,
                                on_step_start=on_start, on_step_done=on_done, on_progress=on_progress, on_series=rec.push)
        self._runner = runner
        if self._stop_requested:             # Stopper cliqué pendant la création : le moteur s'arrête à la première étape
            runner.request_stop()
        results, status = runner.run()
        seq = rec.export()
        if seq:
            meta["sequence_series"] = seq     # courbes reprises dans les rapports (HTML, PDF, CSV, JSON)
        self._ui(self._set_progress, 1.0, "Génération des rapports…")
        fin = cp.finalize_campaign(results, run_dir, meta, sysinfo, inv_before)
        self._ui(self._finished, results, status, fin, run_dir, False)

    def _ui(self, fn, *args):
        """Planifie fn(*args) dans le thread GTK."""
        def call():
            fn(*args)
            return False
        GLib.idle_add(call)

    def _push_result(self, res):
        try:
            self.win._on_bench_result(res)
        except Exception:
            pass

    def _finished(self, results, status, fin, run_dir, blocked):
        self._running = False
        self._runner = None
        if self._timer:
            GLib.source_remove(self._timer); self._timer = 0
        self._chart.queue_draw()
        if not blocked and results and self._chart.save_png(os.path.join(run_dir, "courbes_sequence.png")):
            self._log_line(f"  PNG   {os.path.join(run_dir, 'courbes_sequence.png')}")
        self._btn_run.set_sensitive(True); self._btn_stop.set_sensitive(False)
        if blocked:
            self._progress.set_text("Arrêtée avant le départ")
            return
        v = (fin or {}).get("verdict")
        if v:
            self._log_line(f"\nVerdict : {v['label']}")
            for r in v["rules"]:
                if r["level"] in ("fail", "warn"):
                    self._log_line(f"  {'✗' if r['level'] == 'fail' else '⚠'} {r['label']}" + (f" — {r['detail']}" if r.get("detail") else ""))
        diff = (fin or {}).get("inventory_diff")
        if diff is not None:
            from core import inventory
            if diff["identical"]:
                self._log_line("Inventaire avant/après : identique ✓")
            else:
                self._log_line("Inventaire avant/après :\n  " + "\n  ".join(inventory.format_findings(diff)))
        for fmt, path in ((fin or {}).get("paths") or {}).items():
            self._log_line(f"  {fmt.upper():<5} {path}")
        for e in (fin or {}).get("errors", []):
            self._log_line(f"  ⚠ {e}")
        label = {"finished": "Terminée", "aborted": "Interrompue", "fault": "Arrêtée : panne matérielle"}.get(status, status)
        self._progress.set_fraction(1.0 if status == "finished" else self._progress.get_fraction())
        self._progress.set_text(label)
        self._btn_open.set_sensitive(True)
        self._html_path = ((fin or {}).get("paths") or {}).get("html", "")
        self._btn_html.set_sensitive(bool(self._html_path))
        try:
            self.win._set_status(f"Séquence {label.lower()} — {len(results)} test(s)")
        except Exception:
            pass

    # ── Utilitaires ─────────────────────────────────────────────────────────
    def _tick(self):
        """Rafraîchit le graphique pendant l'exécution."""
        if not self._running:
            self._timer = 0
            return False
        self._chart.queue_draw()
        return True

    def _set_progress(self, frac, text):
        self._progress.set_fraction(frac)
        self._progress.set_text(text[:120])

    def _set_text(self, view, text):
        view.get_buffer().set_text(text)

    def _log_line(self, text: str):
        buf = self._log.get_buffer()
        buf.insert(buf.get_end_iter(), text + "\n")
        adj = self._log.get_parent().get_vadjustment() if hasattr(self._log.get_parent(), "get_vadjustment") else None
        if adj:
            GLib.idle_add(lambda: (adj.set_value(adj.get_upper()), False)[1])

    def _open(self, path: str):
        if not path:
            return
        try:
            subprocess.Popen(["xdg-open", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception as exc:
            self._log_line(f"Ouverture impossible : {exc}")
