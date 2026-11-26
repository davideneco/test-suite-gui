"""
fio_page.py — Page FIO avancée v3.2
- Sélecteur de disque NVMe
- Profils prédéfinis + utilisateur avec création ET modification
- Mode GUI (dropdowns) et mode CLI (commande brute)
- Bouton "Nouveau profil" corrigé
"""

import os, time, uuid, threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, GObject, Gio, Pango

from core.fio_profiles import (
    get_all_profiles, get_profile, add_user_profile, delete_user_profile,
    profile_to_cmd, cmd_to_params, best_engine, get_mounted_disks,
    FIO_RW_MODES_ALL, FIO_BS_ALL, FIO_ENGINES_ALL, FIO_DEPTHS_ALL, FIO_JOBS_ALL,
    FIO_ADVANCED_KEYS, FIO_TABS, profile_tab, profile_standard, load_user_profiles,
)
from core.fio_runner import run_fio_from_params, run_fio_from_cmd, tool_exists
from core.benchmarks import get_system_info, BenchmarkResult
from exporters.export import export_all, save_to_history
from ui.fio_charts import FioResultPanel

OUTPUT_DIR   = os.path.expanduser("~/BenchmarkResults")

# Alias locaux pour les panneaux GUI (même listes)
FIO_RW_MODES = FIO_RW_MODES_ALL
FIO_BS       = FIO_BS_ALL
FIO_ENGINES  = FIO_ENGINES_ALL
FIO_DEPTHS   = FIO_DEPTHS_ALL
FIO_JOBS     = FIO_JOBS_ALL


_OBSERVATION_KEYS = ("pcie_links", "inventory_diff", "electrical", "hardware_events", "hardware_monitor")


def _disk_score(jm: dict, mode: str) -> float:
    """Score d'un disque dans un test multi-disques. Un seul sens mesuré (lecture OU écriture) : sa valeur ; profil mixte (rw) : la moyenne ;
    suite (lecture et écriture mesurées dans des tests DIFFÉRENTS) : le meilleur des deux — jamais leur somme, qui ne correspond à aucune mesure."""
    r, w = float(jm.get("lecture_MiB_s", 0) or 0), float(jm.get("ecriture_MiB_s", 0) or 0)
    if r and w:
        return (r + w) / 2.0 if mode in ("rw", "randrw", "readwrite") else max(r, w)
    return r or w


def _shared_obs(metrics: dict, rd: dict) -> dict:
    """Relevés de la machine entière d'un test : métriques du résultat + événements matériels (liste) du résultat brut de fio."""
    out = dict(metrics or {})
    ev = ((rd or {}).get("hardware_events") or {}).get("events") if isinstance((rd or {}).get("hardware_events"), dict) else None
    if ev and "hardware_events" not in out:
        out["hardware_events"] = ev
    return out


def _disk_metrics(jm: dict, shared: dict, first: bool) -> dict:
    """Métriques propres à un disque (sans le nom interne « job0 ») ; les relevés de la machine entière (liens PCIe, inventaire, électronique,
    événements matériels) ne sont attachés qu'au PREMIER disque pour ne pas être comptés plusieurs fois."""
    out = {k: v for k, v in dict(jm).items() if k != "name"}
    if first:
        out.update({k: shared[k] for k in _OBSERVATION_KEYS if k in shared})
    elif any(k in shared for k in _OBSERVATION_KEYS):
        # Les autres disques n'ont ni section « Liens PCIe » ni relevé électronique : sans cette mention, on croirait qu'ils manquent
        out["observations_groupe"] = "relevés de la machine (liens PCIe, inventaire, électronique) partagés avec le 1er disque du test"
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Worker
# ─────────────────────────────────────────────────────────────────────────────

class FioWorker(threading.Thread):
    def __init__(self, mode, params, raw_cmd, on_progress, on_done, on_series=None):
        super().__init__(daemon=True)
        self.mode = mode; self.params = params; self.raw_cmd = raw_cmd
        self.on_progress = on_progress; self.on_done = on_done
        self.on_series = on_series
        self._stop_event = threading.Event()

    def stop(self):
        self._stop_event.set()

    def run(self):
        def cb(pct, msg):
            if not self._stop_event.is_set():
                GLib.idle_add(self.on_progress, pct, msg)

        def cb_series(point):
            if not self._stop_event.is_set() and self.on_series:
                GLib.idle_add(self.on_series, point)

        def stop_flag():
            return self._stop_event.is_set()

        t0 = time.time()
        # try/except : une exception ici laissait la page bloquée en « test en cours » (on_done jamais appelé)
        try:
            if self.mode == "cli":
                r = run_fio_from_cmd(self.raw_cmd, cb, stop_flag_fn=stop_flag)
            elif self.params.get("is_suite"):
                from core.fio_runner import run_fio_suite
                r = run_fio_suite(
                    suite_test_ids=self.params.get("suite_tests", []),
                    path=self.params.get("path", "/tmp"),
                    cb=cb,
                    stop_flag_fn=stop_flag,
                    raw_device=self.params.get("raw_device", False),
                    on_series=cb_series,
                    suite_name=self.params.get("suite_name", ""),
                )
            else:
                p = {k: v for k, v in self.params.items() if k not in ("is_suite", "suite_tests", "poll_mb_sensors")}
                r = run_fio_from_params(cb=cb, stop_flag_fn=stop_flag, on_series=cb_series, **p)
            if not isinstance(r, dict):
                r = {"status": "error", "error": "fio n'a renvoyé aucun résultat"}
        except Exception as e:
            r = {"status": "error", "error": f"{type(e).__name__} : {e}", "raw": ""}

        # Si arrêté par l'utilisateur → ne pas appeler on_done avec une erreur
        if self._stop_event.is_set() and r.get("status") == "stopped":
            GLib.idle_add(self.on_done, {"status": "stopped", "stopped_by_user": True})
        else:
            r["duration_s"] = time.time() - t0
            GLib.idle_add(self.on_done, r)


# ─────────────────────────────────────────────────────────────────────────────
# Dialog Création / Modification de profil
# ─────────────────────────────────────────────────────────────────────────────

class ProfileDialog(Gtk.Window):
    """
    Dialog unifié :
      mode='create'  → nouveau profil depuis les params courants du GUI
      mode='edit'    → modification d'un profil utilisateur existant
    """

    def __init__(self, root_win, params: dict, on_saved,
                 mode: str = "create", existing: dict = None):
        title = "Modifier le profil" if mode == "edit" else "Nouveau profil FIO"
        super().__init__(title=title)
        self.set_transient_for(root_win)
        self.set_modal(True)
        self.set_default_size(*__import__('ui.screen', fromlist=['fit_size']).fit_size(560, 560))
        self._params   = dict(params or {})
        self._on_saved = on_saved
        self._mode     = mode
        self._existing = existing or {}
        self._pw       = {}     # param widgets
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        hb = Adw.HeaderBar()
        hb.set_show_end_title_buttons(True)
        root.append(hb)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        box.set_margin_top(20); box.set_margin_start(24)
        box.set_margin_end(24); box.set_margin_bottom(20)

        # ── Métadonnées ───────────────────────────────────────────────────────
        meta_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        meta_section.add_css_class("params-panel")

        meta_title = Gtk.Label(label="IDENTITE DU PROFIL")
        meta_title.add_css_class("params-title")
        meta_title.set_halign(Gtk.Align.START)
        meta_section.append(meta_title)

        meta_grid = Gtk.Grid()
        meta_grid.set_row_spacing(10)
        meta_grid.set_column_spacing(12)

        def meta_row(r, label, widget):
            l = Gtk.Label(label=label)
            l.add_css_class("param-label")
            l.set_halign(Gtk.Align.END)
            l.set_size_request(120, -1)
            meta_grid.attach(l, 0, r, 1, 1)
            meta_grid.attach(widget, 1, r, 1, 1)

        self._name_entry = Gtk.Entry()
        self._name_entry.set_hexpand(True)
        self._name_entry.set_placeholder_text("ex: Samsung 990 Pro — randread 4K")
        self._name_entry.set_text(self._existing.get("name", ""))
        meta_row(0, "Nom :", self._name_entry)

        self._cat_entry = Gtk.Entry()
        self._cat_entry.set_hexpand(True)
        self._cat_entry.set_text(self._existing.get("category", "Mes profils"))
        meta_row(1, "Catégorie :", self._cat_entry)

        # Icône + description sur la même ligne
        icon_box = Gtk.Box(spacing=8)
        self._icon_entry = Gtk.Entry()
        self._icon_entry.set_text(self._existing.get("icon", "★"))
        self._icon_entry.set_max_length(4)
        self._icon_entry.set_size_request(60, -1)
        icon_box.append(self._icon_entry)

        icon_hint = Gtk.Label()
        icon_hint.set_markup('<span foreground="#6c7086" size="small">emoji ou symbole court</span>')
        icon_box.append(icon_hint)
        meta_row(2, "Icône :", icon_box)

        meta_section.append(meta_grid)

        desc_lbl = Gtk.Label(label="Description :")
        desc_lbl.add_css_class("param-label")
        desc_lbl.set_halign(Gtk.Align.START)
        desc_lbl.set_margin_top(4)
        meta_section.append(desc_lbl)

        self._desc_buf = Gtk.TextBuffer()
        self._desc_buf.set_text(self._existing.get("description", "Profil personnalisé"))
        dv = Gtk.TextView(buffer=self._desc_buf)
        dv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        dv.add_css_class("log-view")
        ds = Gtk.ScrolledWindow()
        ds.set_child(dv)
        ds.set_size_request(-1, 65)
        meta_section.append(ds)
        box.append(meta_section)

        # ── Paramètres FIO ────────────────────────────────────────────────────
        params_section = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        params_section.add_css_class("params-panel")

        params_title = Gtk.Label(label="PARAMETRES FIO")
        params_title.add_css_class("params-title")
        params_title.set_halign(Gtk.Align.START)
        params_section.append(params_title)

        # Source : params du GUI si 'create', params existants si 'edit'
        src = self._params if self._params else self._existing.get("params", {})

        pgrid = Gtk.Grid()
        pgrid.set_row_spacing(10)
        pgrid.set_column_spacing(12)
        pr = 0

        def plbl(text):
            l = Gtk.Label(label=text)
            l.add_css_class("param-label")
            l.set_halign(Gtk.Align.END)
            l.set_size_request(140, -1)
            return l

        def pdd(options, cur, key):
            sl = Gtk.StringList.new(options)
            w  = Gtk.DropDown(model=sl)
            w.set_hexpand(True)
            s  = str(cur) if cur is not None else ""
            if s in options:
                w.set_selected(options.index(s))
            self._pw[key] = w
            return w

        def pspin(lo, hi, step, val, key):
            w = Gtk.SpinButton.new_with_range(lo, hi, step)
            try:   w.set_value(float(val))
            except: w.set_value(lo)
            w.set_hexpand(True)
            self._pw[key] = w
            return w

        def pchk(label, val, key):
            w = Gtk.CheckButton(label=label)
            w.set_active(bool(val))
            self._pw[key] = w
            return w

        def prow(label, widget):
            nonlocal pr
            pgrid.attach(plbl(label), 0, pr, 1, 1)
            pgrid.attach(widget,       1, pr, 1, 1)
            pr += 1

        prow("Mode R/W :",         pdd(FIO_RW_MODES_ALL, src.get("rw_mode","read"),     "rw_mode"))
        prow("Taille bloc :",       pdd(FIO_BS_ALL,       src.get("bs","1m"),             "bs"))
        prow("IO Engine :",         pdd(FIO_ENGINES_ALL,  src.get("io_engine","auto"),    "io_engine"))
        prow("Queue depth :",       pdd(FIO_DEPTHS_ALL,   str(src.get("iodepth",8)),      "iodepth"))
        prow("Taille fichier (Mo):",pspin(0,  524288, 64,   src.get("size_mb", 2048),      "size_mb"))
        prow("Durée test (s) :",    pspin(0,  604800, 5,    src.get("runtime_s", 30),       "runtime_s"))
        prow("Mix lectures % :",    pspin(0, 100, 5,     src.get("rwmixread",70),        "rwmixread"))

        direct_chk = pchk("Direct I/O (bypass cache OS — recommandé)",
                          src.get("direct", True), "direct")
        pgrid.attach(direct_chk, 1, pr, 1, 1); pr += 1

        params_section.append(pgrid)

        # Aperçu commande générée
        self._cmd_preview_buf = Gtk.TextBuffer()
        self._cmd_preview_buf.set_text("")
        cmd_view = Gtk.TextView(buffer=self._cmd_preview_buf)
        cmd_view.set_editable(False)
        cmd_view.set_cursor_visible(False)
        cmd_view.add_css_class("log-view")
        cmd_scroll = Gtk.ScrolledWindow()
        cmd_scroll.set_child(cmd_view)
        cmd_scroll.set_size_request(-1, 90)

        cmd_lbl = Gtk.Label(label="Aperçu commande fio :")
        cmd_lbl.add_css_class("param-label")
        cmd_lbl.set_halign(Gtk.Align.START)
        cmd_lbl.set_margin_top(4)
        params_section.append(cmd_lbl)
        params_section.append(cmd_scroll)

        btn_preview = Gtk.Button(label="Actualiser l'aperçu")
        btn_preview.add_css_class("btn-json")
        btn_preview.connect("clicked", self._update_preview)
        params_section.append(btn_preview)
        box.append(params_section)

        # Générer l'aperçu initial
        self._update_preview(None)

        # ── Boutons ───────────────────────────────────────────────────────────
        sep = Gtk.Separator(); sep.set_margin_top(6)
        box.append(sep)

        self._err_lbl = Gtk.Label(label="")
        self._err_lbl.set_halign(Gtk.Align.START)
        self._err_lbl.set_visible(False)
        box.append(self._err_lbl)

        btn_row = Gtk.Box(spacing=10)
        btn_cancel = Gtk.Button(label="Annuler")
        btn_cancel.connect("clicked", lambda _: self.destroy())

        save_label = "Enregistrer les modifications" if self._mode == "edit" else "Créer le profil"
        btn_save = Gtk.Button(label=save_label)
        btn_save.add_css_class("btn-run")
        btn_save.set_hexpand(True)
        btn_save.connect("clicked", self._on_save)

        btn_row.append(btn_cancel)
        btn_row.append(btn_save)
        box.append(btn_row)

        scroll.set_child(box)
        root.append(scroll)
        self.set_child(root)

    # ── Logique ───────────────────────────────────────────────────────────────

    def _collect_params(self) -> dict:
        p = {}
        for k, w in self._pw.items():
            if isinstance(w, Gtk.SpinButton):
                p[k] = int(w.get_value())
            elif isinstance(w, Gtk.DropDown):
                item = w.get_selected_item()
                val  = item.get_string() if item else ""
                p[k] = int(val) if val.isdigit() else val
            elif isinstance(w, Gtk.CheckButton):
                p[k] = w.get_active()
        return p

    def _update_preview(self, _):
        try:
            params = self._collect_params()
            cmd = profile_to_cmd({"params": params}, "/chemin/disque")
            self._cmd_preview_buf.set_text(cmd)
        except Exception as e:
            self._cmd_preview_buf.set_text(f"Erreur : {e}")

    def _on_save(self, _):
        name = self._name_entry.get_text().strip()
        if not name:
            self._err_lbl.set_markup(
                '<span foreground="#f38ba8" size="small">⚠  Le nom est obligatoire.</span>')
            self._err_lbl.set_visible(True)
            return

        buf  = self._desc_buf
        desc = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False).strip()

        pid = self._existing.get("id") if self._mode == "edit" else f"user_{uuid.uuid4().hex[:8]}"

        profile = {
            "id":          pid,
            "name":        name,
            "category":    self._cat_entry.get_text().strip() or "Mes profils",
            "description": desc or name,
            "icon":        self._icon_entry.get_text().strip() or "★",
            "builtin":     False,
            "params":      self._collect_params(),
        }
        add_user_profile(profile)
        self._on_saved(profile)
        self.destroy()


def _queue_status_markup(status: str) -> str:
    return {
        "wait": '<span foreground="#6c7086">⏳ attente</span>',
        "run":  '<span foreground="#f97316">▶ en cours</span>',
        "ok":   '<span foreground="#a6e3a1">✓ OK</span>',
        "err":  '<span foreground="#f38ba8">✗ erreur</span>',
        "stop": '<span foreground="#f9e2af">⏹ arrêté</span>',
    }.get(status, status)


class _QueueResult(GObject.Object):
    """GObject léger pour stocker un résultat de file dans ListStore."""
    __gtype_name__ = "FioQueueResult"

    def __init__(self, profile_id: str, profile_name: str, status: str = "wait"):
        super().__init__()
        self.profile_id   = profile_id
        self.profile_name = profile_name
        self.status       = status
        self.score        = 0.0
        self.unit         = "MiB/s"
        self.r_bw         = "—"
        self.w_bw         = "—"
        self.r_iops       = "—"
        self.w_iops       = "—"
        self.lat_moy      = "—"
        self.duration     = "—"


# ─────────────────────────────────────────────────────────────────────────────
# Page FIO principale
# ─────────────────────────────────────────────────────────────────────────────

class FioPage(Gtk.Box):

    def __init__(self, win, disk_filter=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
        self.win    = win
        self.result = None
        self.worker = None
        all_disks = get_mounted_disks(max_age_s=15)      # relevé partagé entre les pages construites au démarrage
        
        if disk_filter == "nvme":
            self._disks = [d for d in all_disks if str(d.get("tran", "")).lower() == "nvme" or str(d.get("type", "")).lower() == "nvme"]
        elif disk_filter == "hdd":
            self._disks = [d for d in all_disks if str(d.get("tran", "")).lower() != "nvme" and str(d.get("type", "")).lower() != "nvme" and d.get("path") != "/tmp"]
        else:
            self._disks = all_disks

        self._gui_w = {}
        self._disk_filter = disk_filter
        self._profile_tab = "perf"          # onglet de profils ouvert (classement par fonction)
        self._tab_buttons = {}
        self._active_profile_name = "🏆 Suite Complète — Tous les tests Peak"
        self._active_profile = get_profile("peak_suite_all")
        # File de tests
        self._queue_jobs    = []
        self._queue_results = []
        self._queue_idx     = 0
        self._queue_stop    = False
        self._queue_checks  = {}
        self._build()

    # ── Construction ─────────────────────────────────────────────────────────

    def _build(self):
        self.append(self._build_profiles_panel())
        self.append(Gtk.Separator(orientation=Gtk.Orientation.VERTICAL))

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True); scroll.set_hexpand(True)

        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        main.set_margin_top(14); main.set_margin_start(16)
        main.set_margin_end(16); main.set_margin_bottom(14)

        # ── En-tête & Barre d'action permanente (évite de devoir scroller) ──
        t_row = Gtk.Box(spacing=12)
        t_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        t_col.set_hexpand(True)
        t1 = Gtk.Label(label="⚡ Stockage FIO — Peak Performance")
        t1.add_css_class("bench-title"); t1.set_halign(Gtk.Align.START)
        t2 = Gtk.Label(label="Sélectionnez un profil à gauche ou ajustez les paramètres ci-dessous")
        t2.add_css_class("bench-subtitle"); t2.set_halign(Gtk.Align.START)
        t_col.append(t1); t_col.append(t2)
        t_row.append(t_col)

        # Boutons d'action directement en haut
        self._btn_run = Gtk.Button(label="▶ Lancer FIO")
        self._btn_run.add_css_class("btn-run")
        self._btn_run.set_sensitive(tool_exists("fio"))
        self._btn_run.connect("clicked", self._on_run)
        t_row.append(self._btn_run)

        self._btn_stop = Gtk.Button(label="⏹ Stopper")
        self._btn_stop.add_css_class("btn-stop")
        self._btn_stop.set_sensitive(False)
        self._btn_stop.connect("clicked", self._on_stop)
        t_row.append(self._btn_stop)

        self._exp_all = Gtk.Button(label="💾 Exporter")
        self._exp_all.add_css_class("btn-json")
        self._exp_all.set_sensitive(False)
        self._exp_all.set_tooltip_text("Exporter en CSV + JSON + PDF + HTML")
        self._exp_all.connect("clicked", self._on_export_all)
        t_row.append(self._exp_all)

        main.append(t_row)

        if not tool_exists("fio"):
            warn = Gtk.Label()
            warn.set_markup('<span foreground="#f9e2af" weight="bold">⚠  fio non installé — exécutez : sudo apt install fio</span>')
            warn.set_halign(Gtk.Align.START)
            main.append(warn)

        main.append(self._build_disk_selector())

        # Mode switcher compact
        mode_row = Gtk.Box(spacing=10)
        mode_lbl = Gtk.Label(label="<b>Configuration :</b>", use_markup=True)
        self._mode_gui = Gtk.ToggleButton(label="🎛️ Profil GUI")
        self._mode_gui.set_active(True)
        self._mode_cli = Gtk.ToggleButton(label="⌨️ Mode CLI libre")
        self._mode_cli.set_group(self._mode_gui)
        self._mode_gui.connect("toggled", self._on_mode_changed)
        mode_row.append(mode_lbl)
        mode_row.append(self._mode_gui)
        mode_row.append(self._mode_cli)
        


        main.append(mode_row)

        self._mode_stack = Gtk.Stack()
        self._mode_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self._mode_stack.set_transition_duration(150)
        self._mode_stack.add_named(self._build_gui_panel(), "gui")
        self._mode_stack.add_named(self._build_cli_panel(), "cli")
        self._mode_stack.set_visible_child_name("gui")
        main.append(self._mode_stack)

        # ── Score display compact ──
        sb = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        sb.add_css_class("params-panel")
        
        sb_left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        sb_left.set_hexpand(True)
        st = Gtk.Label(label="RÉSULTAT DU BENCHMARK"); st.add_css_class("params-title"); st.set_halign(Gtk.Align.START)
        self._progress = Gtk.ProgressBar()
        self._progress.set_fraction(0); self._progress.set_show_text(True); self._progress.set_text("Prêt")
        sb_left.append(st); sb_left.append(self._progress)
        sb.append(sb_left)

        sb_right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self._score_val  = Gtk.Label(label="—"); self._score_val.set_markup('<span size="x-large" weight="heavy" foreground="#ff6600">—</span>'); self._score_val.set_halign(Gtk.Align.END)
        self._score_unit = Gtk.Label(label=""); self._score_unit.add_css_class("score-unit"); self._score_unit.set_halign(Gtk.Align.END)
        sb_right.append(self._score_val); sb_right.append(self._score_unit)
        sb.append(sb_right)

        main.append(sb)

        # ── Métriques détaillées ──
        self._metrics_box = Gtk.FlowBox()
        self._metrics_box.set_max_children_per_line(4); self._metrics_box.set_min_children_per_line(2)
        self._metrics_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self._metrics_box.set_column_spacing(8); self._metrics_box.set_row_spacing(8)
        main.append(self._metrics_box)

        # ── Graphiques et historique comparatif (Cairo native GTK4) ──
        self.charts_panel = FioResultPanel()
        self.charts_panel._score_box.set_visible(False)  # évite doublon avec la barre de score principale ci-dessus
        main.append(self.charts_panel)

        # Log brut collapsible compact
        exp = Gtk.Expander(label="Sortie brute fio (JSON)"); exp.set_margin_top(2)
        ls = Gtk.ScrolledWindow(); ls.set_min_content_height(80); ls.set_max_content_height(160)
        self._log_buf = Gtk.TextBuffer()
        lv = Gtk.TextView(buffer=self._log_buf)
        lv.set_editable(False); lv.set_cursor_visible(False)
        lv.add_css_class("log-view"); lv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        ls.set_child(lv); exp.set_child(ls)
        main.append(exp)

        scroll.set_child(main)
        self.append(scroll)

    # ── File de tests ─────────────────────────────────────────────────────────

    def _build_queue_panel(self) -> Gtk.Widget:
        """Panneau 'Lancer plusieurs profils d'affilée'."""
        exp = Gtk.Expander(label="Lancer plusieurs tests d'affilée")
        exp.set_margin_top(4)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.add_css_class("params-panel")
        box.set_margin_top(6)

        # En-tête
        hdr = Gtk.Box(spacing=10)
        info = Gtk.Label()
        info.set_markup(
            '<span foreground="#6c7086" size="small">'
            'Cochez les profils à exécuter en séquence. '
            'Les paramètres courants (disque, durée…) s\'appliquent à chaque test.'
            '</span>')
        info.set_halign(Gtk.Align.START); info.set_wrap(True); info.set_hexpand(True)
        hdr.append(info)

        btn_sel_all  = Gtk.Button(label="Tout cocher")
        btn_sel_all.connect("clicked", lambda _: self._queue_select_all(True))
        btn_sel_none = Gtk.Button(label="Tout décocher")
        btn_sel_none.connect("clicked", lambda _: self._queue_select_all(False))
        hdr.append(btn_sel_all); hdr.append(btn_sel_none)
        box.append(hdr)

        # Liste des profils avec cases à cocher
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_min_content_height(140)
        list_scroll.set_max_content_height(260)

        self._queue_list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        self._queue_checks = {}   # profile_id → (Gtk.CheckButton, profile_dict)
        self._rebuild_queue_list()
        list_scroll.set_child(self._queue_list)
        box.append(list_scroll)

        # Bouton rafraichir la liste (en cas de nouveau profil ajouté)
        btn_refresh_queue = Gtk.Button(label="↻ Actualiser la liste")
        btn_refresh_queue.connect("clicked", lambda _: self._rebuild_queue_list())
        box.append(btn_refresh_queue)

        # Progression globale
        prog_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._queue_global_prog = Gtk.ProgressBar()
        self._queue_global_prog.set_fraction(0)
        self._queue_global_prog.set_show_text(True)
        self._queue_global_prog.set_text("0 / 0 tests")
        prog_box.append(self._queue_global_prog)
        box.append(prog_box)

        # Boutons lancer / stopper la file
        q_btn_row = Gtk.Box(spacing=10)
        self._btn_run_queue = Gtk.Button(label="▶  Lancer la file de tests")
        self._btn_run_queue.add_css_class("btn-run")
        self._btn_run_queue.set_hexpand(True)
        self._btn_run_queue.set_sensitive(tool_exists("fio"))
        self._btn_run_queue.connect("clicked", self._on_run_queue)
        self._btn_stop_queue = Gtk.Button(label="⏹ Stopper")
        self._btn_stop_queue.add_css_class("btn-stop")
        self._btn_stop_queue.set_sensitive(False)
        self._btn_stop_queue.connect("clicked", self._on_stop_queue)
        q_btn_row.append(self._btn_run_queue)
        q_btn_row.append(self._btn_stop_queue)
        box.append(q_btn_row)

        # Tableau des résultats multi-tests
        results_lbl = Gtk.Label(label="RÉSULTATS DE LA FILE")
        results_lbl.add_css_class("params-title"); results_lbl.set_halign(Gtk.Align.START)
        box.append(results_lbl)

        # ColumnView pour les résultats
        self._queue_store = Gio.ListStore(item_type=_QueueResult)

        sel = Gtk.SingleSelection(model=self._queue_store)
        cv  = Gtk.ColumnView(model=sel)
        cv.set_show_row_separators(True)
        cv.set_size_request(-1, 140)

        def col(title, fn, w, markup=False):
            f = Gtk.SignalListItemFactory()
            def setup(_, item):
                lbl = Gtk.Label(); lbl.set_halign(Gtk.Align.START)
                lbl.set_margin_start(4); item.set_child(lbl)
            def bind(_, item, _fn=fn, _m=markup):
                qr = item.get_item(); lbl = item.get_child()
                v = _fn(qr)
                if _m: lbl.set_markup(v)
                else:  lbl.set_text(str(v))
            f.connect("setup", setup); f.connect("bind", bind)
            c = Gtk.ColumnViewColumn(title=title, factory=f)
            c.set_fixed_width(w); cv.append_column(c)

        col("Statut",   lambda q: _queue_status_markup(q.status), 60,  True)
        col("Profil",   lambda q: q.profile_name,                  180, False)
        col("Score",    lambda q: f"{q.score:.1f}" if q.score else "—", 80, False)
        col("Unité",    lambda q: q.unit,                           60,  False)
        col("R MiB/s", lambda q: q.r_bw,                          80,  False)
        col("W MiB/s", lambda q: q.w_bw,                          80,  False)
        col("R IOPS",   lambda q: q.r_iops,                        80,  False)
        col("W IOPS",   lambda q: q.w_iops,                        80,  False)
        col("Lat moy",  lambda q: q.lat_moy,                       70,  False)
        col("Durée",    lambda q: q.duration,                       60,  False)

        cv_scroll = Gtk.ScrolledWindow()
        cv_scroll.set_child(cv)
        cv_scroll.set_min_content_height(140)
        box.append(cv_scroll)

        # Export des résultats de la file
        qexp_row = Gtk.Box(spacing=8)
        self._q_exp_all = Gtk.Button(label="Exporter la file CSV + JSON + PDF + HTML")
        self._q_exp_all.add_css_class("btn-json")
        self._q_exp_all.set_hexpand(True); self._q_exp_all.set_sensitive(False)
        self._q_exp_all.connect("clicked", self._on_export_queue_all)
        qexp_row.append(self._q_exp_all)
        box.append(qexp_row)

        exp.set_child(box)
        return exp

    def _rebuild_queue_list(self):
        """Reconstruit la liste des cases à cocher depuis les profils disponibles."""
        # Mémoriser les profils déjà cochés
        checked = {pid for pid, (chk, _) in self._queue_checks.items()
                   if chk.get_active()}

        # Vider
        self._queue_checks = {}
        c = self._queue_list.get_first_child()
        while c:
            n = c.get_next_sibling(); self._queue_list.remove(c); c = n

        from core.benchmarks import CAT_COLORS

        profiles = get_all_profiles()
        prev_cat = None
        for p in profiles:
            cat = p.get("category", "Autres")
            if cat != prev_cat:
                color = CAT_COLORS.get(cat, "#f97316")
                cat_lbl = Gtk.Label()
                cat_lbl.set_markup(
                    f'<span foreground="{color}" weight="bold" size="small">'
                    f'  {cat.upper()}</span>')
                cat_lbl.set_halign(Gtk.Align.START)
                cat_lbl.set_margin_top(6)
                self._queue_list.append(cat_lbl)
                prev_cat = cat

            row = Gtk.Box(spacing=8)
            row.set_margin_start(8)

            chk = Gtk.CheckButton()
            chk.set_active(p["id"] in checked)

            icon_name = Gtk.Label(label=f'{p.get("icon","●")}  {p["name"]}')
            icon_name.set_halign(Gtk.Align.START); icon_name.set_hexpand(True)

            desc = Gtk.Label()
            params = p.get("params", {})
            desc.set_markup(
                f'<span foreground="#6c7086" size="small">'
                f'{params.get("rw_mode","?")} · bs={params.get("bs","?")} · '
                f'depth={params.get("iodepth","?")} · '
                f'{params.get("runtime_s","?")}s</span>')

            row.append(chk); row.append(icon_name); row.append(desc)
            self._queue_list.append(row)
            self._queue_checks[p["id"]] = (chk, p)

    def _queue_select_all(self, state: bool):
        for chk, _ in self._queue_checks.values():
            chk.set_active(state)

    def _get_queue_jobs(self) -> list:
        """Retourne la liste des (profile_id, profile_dict) cochés."""
        return [(pid, p) for pid, (chk, p) in self._queue_checks.items()
                if chk.get_active()]

    # ── Logique file d'exécution ──────────────────────────────────────────────

    def _on_run_queue(self, _):
        jobs = self._get_queue_jobs()
        if not jobs:
            self.win._show_dialog("File vide",
                                   "Cochez au moins un profil dans la liste.")
            return

        # Réinitialiser
        self._queue_store.remove_all()
        self._queue_results = []
        self._queue_jobs    = list(jobs)
        self._queue_idx     = 0
        self._queue_stop    = False

        self._btn_run_queue.set_sensitive(False)
        self._btn_stop_queue.set_sensitive(True)
        self._btn_run.set_sensitive(False)

        # Pré-remplir le tableau avec les tests "en attente"
        for pid, p in jobs:
            qr = _QueueResult(
                profile_id=pid, profile_name=p["name"],
                status="wait")
            self._queue_store.append(qr)

        self._queue_global_prog.set_fraction(0)
        self._queue_global_prog.set_text(f"0 / {len(jobs)} tests")
        self.win._set_status(f"File de tests : {len(jobs)} profil(s) en attente…")

        self._run_next_queue_job()

    def _run_next_queue_job(self):
        """Lance le prochain test de la file."""
        if self._queue_stop or self._queue_idx >= len(self._queue_jobs):
            self._on_queue_done()
            return

        pid, profile = self._queue_jobs[self._queue_idx]

        # Marquer le test courant comme "en cours"
        if self._queue_idx < self._queue_store.get_n_items():
            self._queue_store.get_item(self._queue_idx).status = "run"
            self._queue_store.items_changed(self._queue_idx, 1, 1)

        # Construire les params depuis le profil + disque sélectionné
        path, is_dev = self._get_fio_target()
        p = dict(profile.get("params", {}))
        p["path"]       = path
        p["raw_device"] = is_dev
        if is_dev: p["direct"] = True

        # Résoudre io_engine auto
        if p.get("io_engine", "auto") == "auto":
            p["io_engine"] = best_engine()

        n_total = len(self._queue_jobs)
        self._queue_global_prog.set_text(
            f"{self._queue_idx + 1} / {n_total} — {profile['name']}")

        def on_progress(pct, msg):
            # Avancement global = (idx + pct/100) / total
            frac = (self._queue_idx + pct / 100) / n_total
            self._queue_global_prog.set_fraction(frac)
            self._progress.set_fraction(pct / 100)
            self._progress.set_text(f"[{self._queue_idx+1}/{n_total}] {msg}")
            return False

        def on_done(rd):
            self._on_queue_job_done(rd, pid, profile)
            return False

        if hasattr(self, "charts_panel"):
            self.charts_panel.reset_live()

        self.worker = FioWorker("gui", p, "", on_progress, on_done, self._on_series)
        self.worker.start()

    def _on_queue_job_done(self, rd: dict, pid: str, profile: dict):
        """Appelé quand un job de la file se termine."""
        idx = self._queue_idx
        stopped = rd.get("stopped_by_user") or rd.get("status") == "stopped"

        if stopped:
            if idx < self._queue_store.get_n_items():
                self._queue_store.get_item(idx).status = "stop"
                self._queue_store.items_changed(idx, 1, 1)
            self._on_queue_done()
            return

        # Extraire métriques
        m = dict(rd.get("metrics", {}))
        if (rd.get("hardware_events") or {}).get("events"):
            m["hardware_events"] = rd["hardware_events"]["events"]   # surveillance matérielle en direct
        if "series" in rd and "series" not in m:
            m["series"] = rd["series"]
        def _mv(k1, k2=""):
            v = m.get(k1, m.get(k2, 0))
            try: return f"{float(v):.1f}" if float(v) > 0 else "—"
            except: return "—"

        score  = rd.get("score", 0)
        status = "ok" if rd.get("status") == "ok" else "err"

        if idx < self._queue_store.get_n_items():
            qr = self._queue_store.get_item(idx)
            qr.status   = status
            qr.score    = score
            qr.unit     = rd.get("unit", "MiB/s")
            qr.r_bw     = _mv("lecture_MiB_s","read_MiB_s")
            qr.w_bw     = _mv("ecriture_MiB_s","write_MiB_s")
            qr.r_iops   = _mv("lecture_IOPS","read_IOPS")
            qr.w_iops   = _mv("ecriture_IOPS","write_IOPS")
            qr.lat_moy  = _mv("lat_lect_moy_ms","read_lat_ms")
            qr.duration = f"{rd.get('duration_s',0):.1f}s"
            self._queue_store.items_changed(idx, 1, 1)

        # Sauvegarder le BenchmarkResult complet
        if rd.get("status") == "ok":
            disk_info = {}
            idxs = self._get_selected_disk_indices()
            if idxs:
                d_first = self._disks[idxs[0]]
                disk_info = dict(d_first)
                if len(idxs) > 1:
                    disk_info["model"] = f"Grappe {len(idxs)} disques ({d_first.get('model','')})"
            br = BenchmarkResult(
                name=f"fio — {profile['name']}", category="Stockage",
                score=score, unit="MiB/s",
                duration_s=rd.get("duration_s", 0),
                raw_output=rd.get("raw", "")[:4000],
                metrics=m, status="ok",
                system_info={
                    **get_system_info(),
                    "disk_model":  disk_info.get("model",""),
                    "disk_dev":    disk_info.get("dev",""),
                    "disk_size":   disk_info.get("size",""),
                    "disk_type":   disk_info.get("type","NVMe"),
                    "disk_serial": disk_info.get("serial",""),
                    "profile_name": profile["name"],
                },
            )
            br._disk_info   = disk_info
            br._test_params = profile.get("params", {})
            
            results_to_save = []
            jobs_details = m.get("jobs_details", [])
            if len(jobs_details) > 1 and len(idxs) > 1:
                # Créer un BenchmarkResult individuel pour chaque disque (pas de résultat global aggrégé)
                for i_disk, jm in enumerate(jobs_details):
                    try:
                        job_index = int(jm.get("name", "").replace("job", ""))
                        d_indiv = self._disks[idxs[job_index]]
                    except Exception:
                        d_indiv = self._disks[idxs[i_disk]] if i_disk < len(idxs) else {}
                        
                    score_indiv = _disk_score(jm, m.get("mode"))
                    indiv_metrics = _disk_metrics(jm, _shared_obs(m, rd), i_disk == 0)
                    indiv_br = BenchmarkResult(
                        name=f"fio — {profile['name']} ({d_indiv.get('dev', jm.get('name'))})",
                        category="Stockage",
                        score=round(score_indiv, 1),
                        unit="MiB/s",
                        duration_s=rd.get("duration_s", 0),
                        raw_output="",
                        metrics=indiv_metrics, status="ok",
                        system_info={
                            **get_system_info(),
                            "disk_model":  d_indiv.get("model",""),
                            "disk_dev":    d_indiv.get("dev",""),
                            "disk_size":   d_indiv.get("size",""),
                            "disk_type":   d_indiv.get("type","NVMe"),
                            "disk_serial": d_indiv.get("serial",""),
                            "profile_name": profile["name"],
                        },
                    )
                    results_to_save.append(indiv_br)
            else:
                results_to_save = [br]
                    
            self._queue_results.extend(results_to_save)
            save_to_history(results_to_save)
            for res in results_to_save:
                self.win._on_bench_result(res)

            # Mettre à jour aussi l'affichage du test unique
            self._score_val.set_markup(
                f'<span foreground="#e879f9" weight="ultrabold">{score:.1f}</span>')
            self._score_unit.set_markup(
                f'<span foreground="#6c7086">MiB/s — {profile["name"]}</span>')
            self._populate_metrics(m)

        # Passer au suivant
        self._queue_idx += 1
        GLib.idle_add(self._run_next_queue_job)

    def _on_stop_queue(self, _):
        self._queue_stop = True
        if self.worker:
            self.worker.stop()
        self._btn_stop_queue.set_sensitive(False)
        self._progress.set_text("Arrêt en cours…")
        self.win._set_status("Arrêt de la file de tests…")

    def _on_queue_done(self):
        """Fin de la file (normale ou arrêtée)."""
        self._btn_run_queue.set_sensitive(True)
        self._btn_stop_queue.set_sensitive(False)
        self._btn_run.set_sensitive(True)

        n_done = self._queue_idx
        n_ok   = sum(1 for br in self._queue_results)
        self._queue_global_prog.set_fraction(1.0 if not self._queue_stop else
                                              n_done / max(len(self._queue_jobs), 1))
        msg = f"File terminée : {n_ok} / {len(self._queue_jobs)} test(s) réussis"
        if self._queue_stop:
            msg = f"File arrêtée : {n_ok} test(s) complétés"
        self._queue_global_prog.set_text(msg)
        self.win._set_status(msg)

        # Activer l'export si on a des résultats
        has_results = bool(self._queue_results)
        self._q_exp_all.set_sensitive(has_results)
        self._exp_all.set_sensitive(has_results)

        if has_results:
            self.result = self._queue_results[-1]  # dernier résultat pour export simple

    def _on_export_queue_all(self, _):
        """Export de tous les résultats de la file."""
        if not self._queue_results:
            return
        results, sysinfo = list(self._queue_results), self.win.system_info_snapshot()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, system_info_data=sysinfo)
            return f"{len(results)} résultat(s) exporté(s) :\n" + "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())
        self.win.export_async(work, "Export réussi")

    # ── Sélecteur de disque ───────────────────────────────────────────────────

    def _get_selected_disk_indices(self) -> list[int]:
        if not hasattr(self, "_disk_checks"): return []
        return [i for i, cb in enumerate(self._disk_checks) if cb.get_active()]

    def _build_disk_selector(self) -> Gtk.Widget:
        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        frame.add_css_class("params-panel")

        hdr = Gtk.Box(spacing=12)
        title = Gtk.Label(label="DISQUE CIBLE")
        title.add_css_class("params-title"); title.set_halign(Gtk.Align.START); title.set_hexpand(True)
        btn_refresh = Gtk.Button(label="↻ Actualiser")
        btn_refresh.add_css_class("btn-json")
        btn_refresh.connect("clicked", self._refresh_disks)
        hdr.append(title); hdr.append(btn_refresh)
        frame.append(hdr)

        grid = Gtk.Grid(); grid.set_column_spacing(16); grid.set_row_spacing(8)

        lbl = Gtk.Label(label="Disques (NVMe/SATA) :"); lbl.add_css_class("param-label"); lbl.set_halign(Gtk.Align.END)
        
        self._disk_listbox = Gtk.ListBox()
        self._disk_listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        self._disk_listbox.set_hexpand(True)
        self._disk_listbox.add_css_class("boxed-list")
        
        self._disk_checks = []
        for d in self._disks:
            row = Gtk.ListBoxRow()
            cb = Gtk.CheckButton(label=d["label"])
            cb.set_margin_start(8); cb.set_margin_end(8)
            cb.set_margin_top(4); cb.set_margin_bottom(4)
            cb.connect("toggled", self._on_disk_changed)
            row.set_child(cb)
            self._disk_listbox.append(row)
            self._disk_checks.append(cb)
            
        self._disk_scroll = Gtk.ScrolledWindow()
        self._disk_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._disk_scroll.set_min_content_height(100)
        self._disk_scroll.set_max_content_height(200)
        self._disk_scroll.set_child(self._disk_listbox)

        grid.attach(lbl, 0, 0, 1, 1); grid.attach(self._disk_scroll, 1, 0, 1, 1)

        ml = Gtk.Label(label="Ou manuel :"); ml.add_css_class("param-label"); ml.set_halign(Gtk.Align.END)
        self._manual_path = Gtk.Entry(); self._manual_path.set_placeholder_text("ex: /media/user/MyDisk")
        self._manual_path.set_hexpand(True)
        browse = Gtk.Button(label="...")
        browse.set_size_request(34, -1); browse.connect("clicked", self._browse_path)
        
        man_box = Gtk.Box(spacing=4)
        man_box.append(self._manual_path); man_box.append(browse)
        grid.attach(ml, 2, 0, 1, 1); grid.attach(man_box, 3, 0, 1, 1)

        frame.append(grid)

        self._warn_unmounted = Gtk.Label()
        self._warn_unmounted.set_halign(Gtk.Align.START); self._warn_unmounted.set_wrap(True)
        self._warn_unmounted.set_visible(False)
        frame.append(self._warn_unmounted)

        selected_idx = 0
        for i, d in enumerate(self._disks):
            if d["path"] == "/": selected_idx = i; break
            elif d.get("mounted", False) and selected_idx == 0: selected_idx = i
        if self._disks and selected_idx < len(self._disk_checks):
            self._disk_checks[selected_idx].set_active(True)

        engine = best_engine()
        self._disk_info_lbl = Gtk.Label(); self._disk_info_lbl.add_css_class("bench-subtitle")
        self._disk_info_lbl.set_halign(Gtk.Align.START); self._disk_info_lbl.set_wrap(True)
        self._update_disk_info(engine)
        frame.append(self._disk_info_lbl)
        return frame

    def _update_disk_info(self, engine: str = ""):
        if not engine: engine = best_engine()
        idxs = self._get_selected_disk_indices()
        disk_info = ""
        if idxs:
            infos = []
            for idx in idxs:
                d = self._disks[idx]
                mps = ", ".join(d.get("all_mps", [d.get("path", "")])) if d.get("all_mps") else d.get("path", "")
                parts = [p for p in [d.get("type"), d.get("size"), d.get("model"), f"Dev: {d.get('dev')}", f"Monté: {mps}" if d.get("mounted") else "Brut"] if p and p != "?"]
                if parts: infos.append("  ·  ".join(parts))
            
            if infos:
                disk_info = "\n<span foreground='#a6adc8'>ℹ️  " + "\nℹ️  ".join(infos) + "</span>"
                
        self._disk_info_lbl.set_markup(
            f'<span foreground="#6c7086" size="small">'
            f'Moteur I/O : <b>{engine}</b>{disk_info}</span>')

    def _on_disk_changed(self, _=None):
        if hasattr(self, "_warn_unmounted"):
            idxs = self._get_selected_disk_indices()
            has_unmounted = False
            for idx in idxs:
                d = self._disks[idx]
                if not d.get("mounted", True):
                    has_unmounted = True
                    break
            if has_unmounted:
                if d.get("virgin"):
                    msg = '<span foreground="#a6e3a1">✓  Disque vierge — fio peut écrire directement. --direct=1 forcé.</span>'
                else:
                    msg = '<span foreground="#f9e2af">⚠  Disque non monté — fio écrira directement. Données existantes écrasées.</span>'
                self._warn_unmounted.set_markup(msg)
            self._warn_unmounted.set_visible(has_unmounted)
        if hasattr(self, "_disk_info_lbl"):
            self._update_disk_info()

    def _get_target_path(self) -> str:
        """
        Retourne le chemin à passer à fio.
        - Disque monté  → chemin du répertoire (fio crée un fichier dedans)
        - Disque brut   → chemin du périphérique /dev/nvmeXnY (fio écrit directement)
        - Chemin manuel → tel quel
        """
        manual = self._manual_path.get_text().strip()
        if manual:
            return manual
        idxs = self._get_selected_disk_indices()
        if idxs:
            return ",".join(self._disks[i]["path"] for i in idxs)
        return "/tmp"

    def _get_fio_target(self) -> tuple:
        """
        Retourne (path_for_runner, is_raw_device).
        path_for_runner peut être une chaîne de chemins séparés par des virgules (multi-disques).
        """
        manual = self._manual_path.get_text().strip()
        if manual:
            is_dev = manual.startswith("/dev/")
            return manual, is_dev

        idxs = self._get_selected_disk_indices()
        if idxs:
            paths = []
            is_dev_any = False
            for idx in idxs:
                d = self._disks[idx]
                path = d["path"]
                paths.append(path)
                if path.startswith("/dev/"):
                    is_dev_any = True
            return ",".join(paths), is_dev_any
        return "/tmp", False

    def _refresh_disks(self, _):
        self._disks = get_mounted_disks()
        
        # Remove all children
        while child := self._disk_listbox.get_first_child():
            self._disk_listbox.remove(child)
            
        self._disk_checks = []
        for d in self._disks:
            row = Gtk.ListBoxRow()
            cb = Gtk.CheckButton(label=d["label"])
            cb.set_margin_start(8); cb.set_margin_end(8)
            cb.set_margin_top(4); cb.set_margin_bottom(4)
            cb.connect("toggled", self._on_disk_changed)
            row.set_child(cb)
            self._disk_listbox.append(row)
            self._disk_checks.append(cb)
            
        self._update_disk_info()
        n = len(self._disks); nm = sum(1 for d in self._disks if not d.get("mounted", True))
        self.win._set_status(f"{n} NVMe détecté(s)" + (f" dont {nm} non monté(s)" if nm else ""))

    # ── Panneau GUI ───────────────────────────────────────────────────────────

    def _build_gui_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("params-panel")
        pt = Gtk.Label(label="PARAMÈTRES DU PROFIL"); pt.add_css_class("params-title"); pt.set_halign(Gtk.Align.START)
        box.append(pt)

        grid = Gtk.Grid(); grid.set_row_spacing(8); grid.set_column_spacing(16)

        def dd(options, default, key):
            sl = Gtk.StringList.new(options)
            w  = Gtk.DropDown(model=sl); w.set_size_request(140, -1)
            if default in options: w.set_selected(options.index(default))
            self._gui_w[key] = w; return w

        def spin(lo, hi, step, val, key):
            w = Gtk.SpinButton.new_with_range(lo, hi, step)
            w.set_value(val); w.set_size_request(110, -1)
            self._gui_w[key] = w; return w

        def attach_param(row, col_base, label, widget):
            l = Gtk.Label(label=label); l.add_css_class("param-label"); l.set_halign(Gtk.Align.END)
            grid.attach(l, col_base, row, 1, 1); grid.attach(widget, col_base + 1, row, 1, 1)

        # Colonne gauche (0, 1) & Colonne droite (2, 3)
        attach_param(0, 0, "Mode R/W :",       dd(FIO_RW_MODES, "read",  "rw_mode"))
        attach_param(0, 2, "Jobs parallèles :", dd(FIO_JOBS,     "1",     "numjobs"))

        attach_param(1, 0, "Taille bloc :",     dd(FIO_BS,       "1m",    "bs"))
        attach_param(1, 2, "Queue depth :",     dd(FIO_DEPTHS,   "8",     "iodepth"))

        attach_param(2, 0, "IO Engine :",       dd(FIO_ENGINES,  "auto",  "io_engine"))
        attach_param(2, 2, "Taille (Mo) :",     spin(0, 524288, 64, 1024, "size_mb"))

        attach_param(3, 0, "Durée (s) :",       spin(0, 604800, 5,  30,   "runtime_s"))

        chk = Gtk.CheckButton(label="Direct I/O (recommandé)")
        chk.set_active(True); self._gui_w["direct"] = chk
        grid.attach(chk, 3, 3, 1, 1)

        self._rwmix_lbl  = Gtk.Label(label="Mix lectures % :")
        self._rwmix_lbl.add_css_class("param-label"); self._rwmix_lbl.set_halign(Gtk.Align.END)
        self._rwmix_spin = Gtk.SpinButton.new_with_range(0, 100, 5)
        self._rwmix_spin.set_value(70); self._rwmix_spin.set_size_request(110, -1)
        self._gui_w["rwmixread"] = self._rwmix_spin
        grid.attach(self._rwmix_lbl, 0, 4, 1, 1)
        grid.attach(self._rwmix_spin, 1, 4, 1, 1)
        self._rwmix_lbl.set_visible(False); self._rwmix_spin.set_visible(False)

        box.append(grid)
        self._gui_w["rw_mode"].connect("notify::selected", self._on_rw_changed)
        return box

    # ── Panneau CLI ───────────────────────────────────────────────────────────

    def _build_cli_panel(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        box.add_css_class("params-panel")
        pt = Gtk.Label(label="CLI FIO LIBRE"); pt.add_css_class("params-title"); pt.set_halign(Gtk.Align.START)
        box.append(pt)

        info = Gtk.Label()
        info.set_markup('<span foreground="#6c7086" size="small">Mets la commande fio que tu veux. Si --filename manque, BenchSuite ajoute la cible sélectionnée. --output-format=json et --group_reporting sont ajoutés automatiquement.</span>')
        info.set_halign(Gtk.Align.START); info.set_wrap(True)
        box.append(info)

        cli_sc = Gtk.ScrolledWindow(); cli_sc.set_min_content_height(130); cli_sc.set_max_content_height(200)
        self._cli_buf = Gtk.TextBuffer()
        self._cli_buf.set_text(
            "fio --name=bench \\\n"
            "  --rw=read --bs=1m --size=1024M \\\n"
            "  --numjobs=1 --runtime=30 --iodepth=8 \\\n"
            "  --ioengine=auto --time_based --direct=1")
        cli_v = Gtk.TextView(buffer=self._cli_buf)
        cli_v.add_css_class("log-view"); cli_v.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        cli_sc.set_child(cli_v); box.append(cli_sc)

        btn_import = Gtk.Button(label="← Importer depuis le mode GUI")
        btn_import.add_css_class("btn-csv")
        btn_import.connect("clicked", self._import_gui_to_cli)
        box.append(btn_import)
        return box

    # ── Sidebar profils ───────────────────────────────────────────────────────

    def _build_profiles_panel(self) -> Gtk.Widget:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        panel.add_css_class("sidebar"); panel.set_size_request(250, -1); panel.set_hexpand(False)
        panel.set_hexpand(False); panel.set_halign(Gtk.Align.START)

        hdr = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        hdr.set_margin_top(10); hdr.set_margin_start(10)
        hdr.set_margin_end(10); hdr.set_margin_bottom(6)

        t = Gtk.Label()
        t.set_markup('<span weight="bold" size="small">PROFILS FIO — PAR FONCTION</span>')
        t.set_halign(Gtk.Align.START); hdr.append(t)

        # Onglets cliquables : un clic ouvre la liste des profils qui font cela (perf, latence, stabilité, chauffe…)
        self._tab_grid = Gtk.Grid(column_spacing=4, row_spacing=4)
        self._tab_grid.set_column_homogeneous(True)
        hdr.append(self._tab_grid)

        btn_cli = Gtk.Button(label="CLI libre")
        btn_cli.add_css_class("btn-csv")
        btn_cli.connect("clicked", self._on_cli_profile_click)
        hdr.append(btn_cli)

        panel.append(hdr)
        panel.append(Gtk.Separator())

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)     # pas de défilement horizontal : la liste s'adapte à la largeur
        scroll.set_propagate_natural_width(False)
        scroll.set_min_content_width(250)
        scroll.set_vexpand(True)
        self._profiles_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll.set_child(self._profiles_box)
        panel.append(scroll)

        self._refresh_profiles()
        return panel

    def _profiles_by_tab(self) -> dict:
        """Profils intégrés + profils de l'utilisateur, rangés par onglet de fonction."""
        tabs: dict = {}
        for p in get_all_profiles():
            tabs.setdefault(profile_tab(p), []).append(p)
        for p in load_user_profiles():
            tabs.setdefault("user", []).append(dict(p, tab="user"))
        return tabs

    def _on_tab_clicked(self, _btn, tab_id: str):
        self._profile_tab = tab_id
        self._refresh_profiles()

    def _mark_selected_profile(self):
        """Surligne le profil chargé (classe CSS « profile-selected ») et retire la marque des autres : retour visuel du clic."""
        sel = getattr(self, "_sel_profile_id", None)
        for pid, b in getattr(self, "_profile_btn_by_id", {}).items():
            if pid == sel:
                b.add_css_class("profile-selected")
            else:
                b.remove_css_class("profile-selected")

    def _refresh_profiles(self):
        self._profile_btn_by_id = {}
        # Vider
        c = self._profiles_box.get_first_child()
        while c:
            n = c.get_next_sibling()
            self._profiles_box.remove(c)
            c = n

        by_tab = self._profiles_by_tab()
        # Disque NVMe : pas d'onglet HDD ; page HDD : uniquement l'onglet HDD et les profils de l'utilisateur
        f = getattr(self, "_disk_filter", None)
        visible = [(tid, ico, lab) for tid, ico, lab in FIO_TABS
                   if by_tab.get(tid) and not (f == "nvme" and tid == "hdd") and not (f == "hdd" and tid not in ("hdd", "user", "perf"))]
        if not any(t[0] == self._profile_tab for t in visible) and visible:
            self._profile_tab = visible[0][0]

        # Boutons d'onglets (2 par ligne)
        cg = self._tab_grid.get_first_child()
        while cg:
            n = cg.get_next_sibling()
            self._tab_grid.remove(cg)
            cg = n
        self._tab_buttons = {}
        for i, (tid, ico, lab) in enumerate(visible):
            btn = Gtk.Button(); btn.add_css_class("sidebar-item")
            l = Gtk.Label()
            l.set_markup(f'<span size="small">{ico} {GLib.markup_escape_text(lab)} '
                         f'<span foreground="#6c7086">{len(by_tab[tid])}</span></span>')
            l.set_ellipsize(3); l.set_halign(Gtk.Align.START)
            btn.set_child(l)
            if tid == self._profile_tab:
                btn.add_css_class("active")
            btn.connect("clicked", self._on_tab_clicked, tid)
            self._tab_grid.attach(btn, i % 2, i // 2, 1, 1)
            self._tab_buttons[tid] = btn

        # Profils de l'onglet, groupés par norme d'origine (OCP / SNIA / JEDEC / …)
        items = by_tab.get(self._profile_tab, [])
        groups: dict = {}
        for p in items:
            groups.setdefault(profile_standard(p) or "Autres profils", []).append(p)
        for std in sorted(groups, key=lambda k: (k == "Autres profils", k)):
            sec = Gtk.Label()
            sec.set_markup(f'<span foreground="#f97316" weight="bold" size="small">{GLib.markup_escape_text(std.upper())}</span>')
            sec.add_css_class("sidebar-section-label"); sec.set_halign(Gtk.Align.START)
            self._profiles_box.append(sec)

            for p in groups[std]:
                row = Gtk.Box(spacing=0)

                # Bouton principal (charger le profil)
                btn = Gtk.Button(); btn.add_css_class("sidebar-item")
                btn.set_hexpand(True); btn.set_halign(Gtk.Align.FILL)
                name_lbl = Gtk.Label()
                name_lbl.set_markup(f'{p.get("icon","●")}  {GLib.markup_escape_text(p["name"])}')
                name_lbl.set_halign(Gtk.Align.START); name_lbl.set_wrap(True); name_lbl.set_xalign(0)
                name_lbl.set_max_width_chars(30); name_lbl.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
                btn.set_child(name_lbl)
                btn.set_tooltip_text(p.get("description", ""))
                btn.connect("clicked", self._on_profile_click, p["id"])
                self._profile_btn_by_id[p["id"]] = btn
                if p["id"] == getattr(self, "_sel_profile_id", None):
                    btn.add_css_class("profile-selected")
                row.append(btn)

                if self._profile_tab == "user":      # profils de l'utilisateur : modifier / supprimer
                    be = Gtk.Button(label="✏"); be.add_css_class("sidebar-item")
                    be.connect("clicked", self._on_edit_profile, p["id"]); row.append(be)
                    bd = Gtk.Button(label="🗑"); bd.add_css_class("sidebar-item")
                    bd.connect("clicked", self._on_delete_profile, p["id"]); row.append(bd)

                self._profiles_box.append(row)

    # ── Logique profils ───────────────────────────────────────────────────────

    def _get_root_window(self):
        """Remonte la hiérarchie pour trouver la Gtk.Window racine."""
        w = self.win
        while w and not isinstance(w, Gtk.Window):
            if hasattr(w, "get_root"):
                root = w.get_root()
                if root and isinstance(root, Gtk.Window):
                    return root
            w = getattr(w, "win", None) or getattr(w, "_parent", None)
        return w if isinstance(w, Gtk.Window) else self.win

    def _open_profile_dialog(self, mode: str, params: dict, existing: dict = None):
        """Ouvre le dialog de création/modification."""
        root = self._get_root_window()

        def on_saved(profile):
            self._refresh_profiles()
            self.win._set_status(f"Profil {'modifié' if mode == 'edit' else 'créé'} : {profile['name']}")

        dlg = ProfileDialog(root, params, on_saved, mode=mode, existing=existing)
        dlg.present()

    def _on_new_profile_clicked(self, _):
        """Bouton '+ Nouveau profil' — ouvre le dialog avec les params GUI actuels."""
        params = self._get_gui_params() if self._mode_gui.get_active() else {}
        self._open_profile_dialog("create", params)

    def _on_save_current_as_profile(self, _):
        """Bouton 'Sauvegarder comme profil' — depuis le panneau principal."""
        params = self._get_gui_params() if self._mode_gui.get_active() \
                 else cmd_to_params(self._get_cli_cmd())
        self._open_profile_dialog("create", params)

    def _on_profile_click(self, _, pid: str):
        p = get_profile(pid)
        if not p: return
        self._active_profile = p
        self._sel_profile_id = pid
        self._mark_selected_profile()
        self._apply_to_gui(p.get("params", {}))
        path = self._get_target_path()
        self._cli_buf.set_text(profile_to_cmd(p, path))
        self._active_profile_name = p["name"]
        self._mode_gui.set_active(True)
        self.win._set_status(f"Profil chargé : {p['name']}")

    def _on_cli_profile_click(self, _):
        self._sel_profile_id = None
        self._mark_selected_profile()
        self._mode_cli.set_active(True)
        self.win._set_status("Mode CLI libre : commande fio personnalisable")

    def _on_edit_profile(self, _, pid: str):
        p = get_profile(pid)
        if not p: return
        self._open_profile_dialog("edit", {}, existing=p)

    def _on_delete_profile(self, _, pid: str):
        delete_user_profile(pid)
        self._refresh_profiles()

    # ── Logique GUI ───────────────────────────────────────────────────────────

    def _on_mode_changed(self, _):
        self._mode_stack.set_visible_child_name(
            "gui" if self._mode_gui.get_active() else "cli")

    def _on_rw_changed(self, dd, _):
        item = dd.get_selected_item()
        if item:
            mixed = "rw" in item.get_string()
            self._rwmix_lbl.set_visible(mixed)
            self._rwmix_spin.set_visible(mixed)

    def _cli_preset(self, _, cmd: str):
        import re
        path = self._get_target_path()
        cmd  = re.sub(r"--filename=\S+", f"--filename={path}/_bench_fio.tmp", cmd)
        self._cli_buf.set_text(cmd)

    def _import_gui_to_cli(self, _):
        params = self._get_gui_params()
        cmd = profile_to_cmd({"params": params}, params.get("path", "/tmp"))
        self._cli_buf.set_text(cmd)
        self._mode_cli.set_active(True)

    def _browse_path(self, _):
        Gtk.FileDialog().select_folder(self.win, None, self._on_folder)

    def _on_folder(self, dlg, res):
        try:
            f = dlg.select_folder_finish(res)
            if f: self._manual_path.set_text(f.get_path())
        except Exception:
            pass

    def _get_gui_params(self) -> dict:
        path, is_dev = self._get_fio_target()
        p = {"path": path, "raw_device": is_dev}
        for k, w in self._gui_w.items():
            if isinstance(w, Gtk.SpinButton):
                p[k] = int(w.get_value())
            elif isinstance(w, Gtk.Entry):
                p[k] = w.get_text()
            elif isinstance(w, Gtk.DropDown):
                item = w.get_selected_item()
                val  = item.get_string() if item else ""
                p[k] = int(val) if val.isdigit() else val
            elif isinstance(w, Gtk.CheckButton):
                p[k] = w.get_active()
        # Réglages avancés du profil actif (mélange de blocs, zones, loops…) que les widgets ne couvrent pas
        prof = getattr(self, "_active_profile", None) or {}
        if not prof.get("is_suite"):
            pp = prof.get("params", {})
            for k in FIO_ADVANCED_KEYS:
                if k in pp and pp[k] not in ("", None):
                    p[k] = pp[k]
            if pp.get("bs") and str(pp["bs"]) not in FIO_BS_ALL:
                p["bs"] = pp["bs"]      # taille exotique (ex. 2300) absente de la liste déroulante
        # Disque brut → forcer direct=True
        if is_dev:
            p["direct"] = True
        return p

    def _get_cli_cmd(self) -> str:
        buf = self._cli_buf
        return buf.get_text(buf.get_start_iter(), buf.get_end_iter(), False)\
                  .replace("\\\n", " ").replace("\n", " ").strip()

    def _apply_to_gui(self, params: dict):
        maps = {
            "rw_mode":   ("rw_mode",    FIO_RW_MODES),
            "bs":        ("bs",          FIO_BS),
            "io_engine": ("io_engine",   FIO_ENGINES),
            "iodepth":   ("iodepth",     FIO_DEPTHS),
            "numjobs":   ("numjobs",     FIO_JOBS),
        }
        for pk, (wk, opts) in maps.items():
            if pk in params and wk in self._gui_w:
                val = str(params[pk])
                if val in opts:
                    self._gui_w[wk].set_selected(opts.index(val))
        for pk, wk in [("size_mb", "size_mb"), ("runtime_s", "runtime_s")]:
            if pk in params and wk in self._gui_w:
                self._gui_w[wk].set_value(int(params[pk]))
        if "direct" in params and "direct" in self._gui_w:
            self._gui_w["direct"].set_active(bool(params["direct"]))
        if "rwmixread" in params and "rwmixread" in self._gui_w:
            self._gui_w["rwmixread"].set_value(int(params["rwmixread"]))

    # ── Exécution ─────────────────────────────────────────────────────────────

    def _on_run(self, _):
        self.result = None
        self._clear_metrics()
        self._score_val.set_markup('<span foreground="#6c7086">...</span>')
        self._score_unit.set_text("")
        self._progress.set_fraction(0); self._progress.set_text("Démarrage…")
        self._log_buf.set_text("")
        self._btn_run.set_sensitive(False); self._btn_stop.set_sensitive(True)
        self._exp_all.set_sensitive(False)

        if self._mode_gui.get_active():
            mode = "gui"; params = self._get_gui_params(); cmd = ""
            if hasattr(self, "_active_profile") and self._active_profile and self._active_profile.get("is_suite"):
                params["is_suite"] = True
                params["suite_tests"] = self._active_profile.get("suite_tests", [])
                params["suite_name"] = self._active_profile.get("name", "")
            result_name = f"fio — {self._active_profile_name}"
        else:
            mode = "cli"; params = {}
            result_name = "fio — CLI libre"
            raw = self._get_cli_cmd()
            if "--filename" not in raw:
                path, is_dev = self._get_fio_target()
                if is_dev:
                    # Périphérique brut → fio écrit directement sur /dev/nvmeXnY
                    raw += f" --filename={path}"
                else:
                    raw += f" --filename={path}/_bench_fio.tmp"
            cmd = raw

        self.win._set_status("Exécution fio en cours…")
        self._current_result_name = result_name
        if hasattr(self, "charts_panel"):
            self.charts_panel.reset_live()
        self.worker = FioWorker(mode, params, cmd, self._on_progress, self._on_done, self._on_series)
        self.worker.start()

    def _on_stop(self, _):
        if self.worker:
            self.worker.stop()
        self._btn_stop.set_sensitive(False)
        self._progress.set_text("Arrêt en cours…")
        self.win._set_status("Arrêt de fio en cours…")

    def _on_progress(self, pct, msg):
        self._progress.set_fraction(pct / 100)
        self._progress.set_text(msg)
        if hasattr(self, "charts_panel"):
            self.charts_panel.set_progress(pct, msg)
        return False

    def _on_series(self, point: dict):
        if hasattr(self, "charts_panel"):
            self.charts_panel.push_live(point)
        return False

    def _on_done(self, rd: dict):
        self._btn_run.set_sensitive(True)
        self._btn_stop.set_sensitive(False)

        # Arrêt volontaire
        if rd.get("stopped_by_user") or rd.get("status") == "stopped":
            self._score_val.set_markup('<span foreground="#f9e2af">—</span>')
            self._score_unit.set_markup('<span foreground="#6c7086">Arrêté</span>')
            self._progress.set_fraction(0)
            self._progress.set_text("Arrêté par l'utilisateur")
            self.win._set_status("Test fio arrêté")
            return False

        if rd.get("status") in ("error", "skipped"):
            self._score_val.set_markup('<span foreground="#f38ba8" size="xx-large">ERR</span>')
            err = rd.get("error", "Erreur inconnue")
            self._score_unit.set_markup(f'<span foreground="#f38ba8" size="small">{GLib.markup_escape_text(str(err)[:80])}</span>')
            self._progress.set_text("Erreur")
            self._log_buf.set_text(rd.get("raw", err))
            self.win._set_status(f"Erreur fio : {err[:60]}")
            if hasattr(self, "charts_panel"):
                self.charts_panel.set_error(err)
            return False

        score       = rd.get("score", 0)
        engine_used = rd.get("engine_used", "?")
        self._score_val.set_markup(
            f'<span foreground="#e879f9" weight="ultrabold">{score:.1f}</span>')
        self._score_unit.set_markup(
            f'<span foreground="#6c7086">MiB/s  ·  moteur : {engine_used}</span>')
        self._progress.set_fraction(1.0)
        self._progress.set_text("Terminé avec succès")

        self._populate_metrics(rd.get("metrics", {}))
        self._log_buf.set_text(rd.get("raw", "")[:12000])
        self._exp_all.set_sensitive(True)

        # Collecter les infos du disque sélectionné (pour le PDF)
        disk_info = {}
        idxs = self._get_selected_disk_indices()
        if idxs:
            d_first = self._disks[idxs[0]]
            disk_info = dict(d_first)
            if len(idxs) > 1:
                disk_info["model"] = f"Grappe {len(idxs)} disques ({d_first.get('model','')})"
        dev = disk_info.get("dev", "")
        if dev and dev.startswith("/dev/"):
            try:
                from core.privileged import run_if_elevated
                r = run_if_elevated(["smartctl", "-H", dev], timeout=5)
                if r and "PASSED" in r.stdout:   disk_info["smart"] = "OK (SMART PASSED)"
                elif r and "FAILED" in r.stdout: disk_info["smart"] = "ECHEC (SMART FAILED)"
            except Exception:
                pass

        test_params = self._get_gui_params() if self._mode_gui.get_active() else {}
        test_params["io_engine"] = engine_used

        if hasattr(self, "charts_panel"):
            if rd.get("is_suite") and rd.get("suite_results"):
                for sub in rd.get("suite_results", []):
                    sub_res = sub["result"]
                    sub_params = dict(test_params)
                    sub_params["name"] = sub["name"]
                    self.charts_panel.set_result(sub_res, disk_info, sub_params)
            else:
                self.charts_panel.set_result(rd, disk_info, test_params)

        met = dict(rd.get("metrics", {}))
        if (rd.get("hardware_events") or {}).get("events"):
            met["hardware_events"] = rd["hardware_events"]["events"]   # surveillance matérielle en direct
        if "series" in rd and "series" not in met:
            met["series"] = rd["series"]
            
        results_to_save = []
        jobs_details = met.get("jobs_details", [])
        
        if len(jobs_details) > 1 and len(idxs) > 1:
            for i_disk, jm in enumerate(jobs_details):
                try:
                    job_index = int(jm.get("name", "").replace("job", ""))
                    d_indiv = self._disks[idxs[job_index]]
                except Exception:
                    d_indiv = self._disks[idxs[i_disk]] if i_disk < len(idxs) else {}
                    
                score_indiv = _disk_score(jm, met.get("mode"))
                indiv_metrics = _disk_metrics(jm, _shared_obs(met, rd), i_disk == 0)
                indiv_br = BenchmarkResult(
                    name=f"{getattr(self, '_current_result_name', 'Stockage fio')} ({d_indiv.get('dev', jm.get('name'))})",
                    category="Stockage",
                    score=round(score_indiv, 1),
                    unit="MiB/s",
                    duration_s=rd.get("duration_s", 0),
                    raw_output=rd.get("raw", "")[:8000],
                    metrics=indiv_metrics, status="ok",
                    system_info={
                        **get_system_info(),
                        "disk_model":  d_indiv.get("model",""),
                        "disk_dev":    d_indiv.get("dev",""),
                        "disk_size":   d_indiv.get("size",""),
                        "disk_type":   d_indiv.get("type","NVMe"),
                        "disk_serial": d_indiv.get("serial",""),
                        "disk_smart":  d_indiv.get("smart",""),
                        "test_params": str(test_params),
                    },
                )
                indiv_br._disk_info = d_indiv
                indiv_br._test_params = test_params
                results_to_save.append(indiv_br)
                
            self.result = results_to_save[0]
        else:
            self.result = BenchmarkResult(
                name=getattr(self, "_current_result_name", "Stockage fio"), category="Stockage",
                score=score, unit="MiB/s",
                duration_s=rd.get("duration_s", 0),
                raw_output=rd.get("raw", "")[:8000],
                metrics=met,
                status="ok",
                system_info={
                    **get_system_info(),
                    "disk_model":  disk_info.get("model", ""),
                    "disk_dev":    disk_info.get("dev", ""),
                    "disk_size":   disk_info.get("size", ""),
                    "disk_type":   disk_info.get("type", "NVMe"),
                    "disk_serial": disk_info.get("serial", ""),
                    "disk_smart":  disk_info.get("smart", ""),
                    "test_params": str(test_params),
                },
            )
            self.result._disk_info   = disk_info
            self.result._test_params = test_params
            results_to_save = [self.result]

        self._last_results = results_to_save
        save_to_history(results_to_save)
        for res in results_to_save:
            self.win._on_bench_result(res)
            
        self.win._set_status(f"fio terminé — {score:.1f} MiB/s  (moteur : {engine_used})")
        return False

    def _on_export_all(self, _):
        if not hasattr(self, "_last_results") or not self._last_results:
            if not getattr(self, "result", None): return
            self._last_results = [self.result]
            
        results, sysinfo = list(self._last_results), self.win.system_info_snapshot()

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            paths = export_all(results, OUTPUT_DIR, system_info_data=sysinfo)
            return "\n".join(f"{fmt.upper()} : {path}" for fmt, path in paths.items())
        self.win.export_async(work, "Export complet réussi")

    def _clear_metrics(self):
        c = self._metrics_box.get_first_child()
        while c:
            n = c.get_next_sibling(); self._metrics_box.remove(c); c = n

    def _populate_metrics(self, metrics: dict):
        self._clear_metrics()
        priority = ["lecture_MiB_s", "ecriture_MiB_s", "lecture_IOPS", "ecriture_IOPS",
                    "disk_temp_max_c", "disk_temp_moy_c", "disk_power_max_w", "disk_power_moy_w",
                    "lat_lect_moy_ms", "lat_ecr_moy_ms",
                    "lat_lect_p99_ms", "lat_ecr_p99_ms",
                    "lat_lect_p999_ms","lat_ecr_p999_ms"]
        clean_metrics = {k: v for k, v in metrics.items() if not isinstance(v, (list, dict)) and not k.startswith("series")}
        ordered = [(k, clean_metrics[k]) for k in priority if k in clean_metrics]
        ordered += [(k, v) for k, v in clean_metrics.items() if k not in priority]
        from core.electrical import summary_cards
        ordered = [(t, v) for t, v in summary_cards(metrics)] + ordered
        for k, v in ordered:
            card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            card.add_css_class("metric-card"); card.set_size_request(155, -1)
            lbl = Gtk.Label(label=k.replace("_", " ").upper())
            lbl.add_css_class("metric-label"); lbl.set_halign(Gtk.Align.START); lbl.set_wrap(True)
            val_str = str(v)
            if "temp" in k.lower() and not val_str.endswith("°C"): val_str += " °C"
            elif "power" in k.lower() and not val_str.endswith("W"): val_str += " W"
            val = Gtk.Label(label=val_str)
            val.add_css_class("metric-value"); val.set_halign(Gtk.Align.START)
            card.append(lbl); card.append(val)
            self._metrics_box.append(card)
