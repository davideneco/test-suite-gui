"""
dmesg_page.py — Page de surveillance en temps réel des erreurs et journaux noyau (dmesg / journalctl -k)
- Surveillance live auto-refresh (3s) asynchrone sans gel UI GTK4
- Filtrage instantané par niveau de gravité (Erreurs, Avertissements, Matériel/GPU) et mot-clé
- Coloration syntaxique et compteurs statistiques en direct
"""

import os
import threading
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from core.dmesg_monitor import get_kernel_logs

class DmesgPage(Gtk.Box):
    def __init__(self, main_win=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.main_win = main_win
        self.set_margin_top(16)
        self.set_margin_bottom(16)
        self.set_margin_start(16)
        self.set_margin_end(16)

        self.is_loading = False
        self.live_active = False
        self.live_timer_id = None
        self.is_elevated = False
        self.max_lines = 300
        self.current_filter = "all"
        self.keyword = ""
        self.auto_scroll = True

        self._build_ui()
        self.refresh_logs()

    def _build_ui(self):
        # ── 1. En-tête & Contrôles ──
        header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        lbl_title = Gtk.Label(label="📡 Télémétrie Noyau & Erreurs (dmesg)")
        lbl_title.add_css_class("title-1")
        lbl_title.set_halign(Gtk.Align.START)
        
        lbl_sub = Gtk.Label(label="Surveillance en temps réel des événements noyau, GPU NVIDIA, PCIe et stockage")
        lbl_sub.add_css_class("dim-label")
        lbl_sub.set_halign(Gtk.Align.START)
        
        title_box.append(lbl_title)
        title_box.append(lbl_sub)
        header_box.append(title_box)
        
        header_box.append(Gtk.Box(hexpand=True))

        # Interrupteur Live Stream
        self.chk_live = Gtk.CheckButton(label="🟢 Live Stream (3s)")
        self.chk_live.set_tooltip_text("Interroge le noyau toutes les 3 secondes en arrière-plan sans bloquer l'UI")
        self.chk_live.connect("toggled", self._on_live_toggled)
        header_box.append(self.chk_live)

        # Interrupteur Auto-Scroll
        self.chk_scroll = Gtk.CheckButton(label="📜 Auto-Scroll")
        self.chk_scroll.set_active(True)
        self.chk_scroll.connect("toggled", lambda b: setattr(self, "auto_scroll", b.get_active()))
        header_box.append(self.chk_scroll)

        # Bouton Rafraîchir
        self.btn_refresh = Gtk.Button(label="🔄 Actualiser")
        self.btn_refresh.add_css_class("btn-secondary")
        self.btn_refresh.connect("clicked", lambda _: self.refresh_logs())
        header_box.append(self.btn_refresh)

        # Bouton Admin
        self.btn_admin = Gtk.Button(label="🛡️ Privilèges Admin")
        self.btn_admin.add_css_class("btn-primary")
        self.btn_admin.set_tooltip_text("Élévation root pour déverrouiller dmesg complet si restreint par le noyau")
        self.btn_admin.connect("clicked", self._on_admin_click)
        header_box.append(self.btn_admin)

        self.spinner = Gtk.Spinner()
        header_box.append(self.spinner)

        self.append(header_box)

        # ── 2. Barre de Filtres & Recherche ──
        filter_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)

        lbl_filter = Gtk.Label(label="<b>Filtre :</b>", use_markup=True)
        filter_bar.append(lbl_filter)

        self.combo_filter = Gtk.ComboBoxText()
        self.combo_filter.append("all", "🌐 Tous les événements")
        self.combo_filter.append("error", "🔴 Erreurs / Critiques uniquement")
        self.combo_filter.append("warning", "🟡 Avertissements & Throttling")
        self.combo_filter.append("hardware", "🔵 Événements Matériels (GPU/NVMe/PCIe)")
        self.combo_filter.set_active_id("all")
        self.combo_filter.connect("changed", self._on_filter_changed)
        filter_bar.append(self.combo_filter)

        # Nombre de lignes
        self.combo_lines = Gtk.ComboBoxText()
        self.combo_lines.append("150", "150 dernières lignes")
        self.combo_lines.append("300", "300 dernières lignes")
        self.combo_lines.append("600", "600 dernières lignes")
        self.combo_lines.append("1000", "1000 dernières lignes")
        self.combo_lines.set_active_id("300")
        self.combo_lines.connect("changed", self._on_lines_changed)
        filter_bar.append(self.combo_lines)

        # Recherche par mot-clé
        self.entry_search = Gtk.Entry()
        self.entry_search.set_placeholder_text("🔍 Filtrer mot-clé (ex: NVRM, pcie, nvme)...")
        self.entry_search.set_hexpand(True)
        self.entry_search.connect("changed", self._on_search_changed)
        filter_bar.append(self.entry_search)

        # Bouton d'exportation
        self.btn_export = Gtk.Button(label="💾 Exporter Log")
        self.btn_export.connect("clicked", self._on_export_click)
        filter_bar.append(self.btn_export)

        self.append(filter_bar)

        # ── 3. Cartes de Statistiques ──
        stats_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        stats_box.set_homogeneous(True)

        self.card_source, self.lbl_source = self._create_stat_card("Source & Statut", "⏳ Chargement...", "info")
        self.card_total, self.lbl_total = self._create_stat_card("Total Messages", "0", "info")
        self.card_err, self.lbl_err = self._create_stat_card("Erreurs Critiques", "0", "error")
        self.card_hw, self.lbl_hw = self._create_stat_card("Événements Matériels", "0", "warning")

        stats_box.append(self.card_source)
        stats_box.append(self.card_total)
        stats_box.append(self.card_err)
        stats_box.append(self.card_hw)
        self.append(stats_box)

        # Notification en cas d'erreur ou d'info
        self.lbl_notif = Gtk.Label()
        self.lbl_notif.set_wrap(True)
        self.lbl_notif.set_halign(Gtk.Align.START)
        self.append(self.lbl_notif)

        # ── 4. Zone de Journal Console (TextView) ──
        frame_log = Gtk.Frame()
        frame_log.set_vexpand(True)
        frame_log.add_css_class("card")

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_scroll = scroll

        self.text_view = Gtk.TextView()
        self.text_view.set_editable(False)
        self.text_view.set_cursor_visible(False)
        self.text_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.text_view.set_left_margin(12)
        self.text_view.set_right_margin(12)
        self.text_view.set_top_margin(12)
        self.text_view.set_bottom_margin(12)
        self.text_view.add_css_class("monospace")

        # Configuration du buffer et des tags de couleur
        self.buffer = self.text_view.get_buffer()
        self.tag_ts = self.buffer.create_tag("timestamp", foreground="#6c7086")
        self.tag_err = self.buffer.create_tag("error", foreground="#f38ba8", weight=700)
        self.tag_warn = self.buffer.create_tag("warning", foreground="#fab387", weight=600)
        self.tag_hw = self.buffer.create_tag("hw", foreground="#89b4fa")
        self.tag_info = self.buffer.create_tag("info")

        scroll.set_child(self.text_view)
        frame_log.set_child(scroll)
        self.append(frame_log)

    def _create_stat_card(self, title: str, val: str, kind: str):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        card.add_css_class("card")
        lbl_t = Gtk.Label(label=title)
        lbl_t.add_css_class("dim-label")
        lbl_t.set_halign(Gtk.Align.START)
        
        lbl_v = Gtk.Label(label=val)
        lbl_v.add_css_class("title-2")
        lbl_v.set_halign(Gtk.Align.START)
        if kind == "error":
            lbl_v.add_css_class("error-value")
        
        card.append(lbl_t)
        card.append(lbl_v)
        return card, lbl_v

    def _on_filter_changed(self, combo):
        self.current_filter = combo.get_active_id() or "all"
        self.refresh_logs()

    def _on_lines_changed(self, combo):
        try:
            self.max_lines = int(combo.get_active_id() or 300)
        except ValueError:
            self.max_lines = 300
        self.refresh_logs()

    def _on_search_changed(self, entry):
        self.keyword = entry.get_text().strip()
        self.refresh_logs()

    def _on_live_toggled(self, chk):
        self.live_active = chk.get_active()
        if self.live_active:
            if self.live_timer_id is None:
                self.live_timer_id = GLib.timeout_add(3000, self._on_live_timer)
        else:
            if self.live_timer_id is not None:
                GLib.source_remove(self.live_timer_id)
                self.live_timer_id = None

    def _on_live_timer(self):
        if not self.live_active:
            return False
        self.refresh_logs(silent=True)
        return True

    def _on_admin_click(self, _):
        self.is_elevated = True
        self.refresh_logs()

    def refresh_logs(self, silent=False):
        if self.is_loading and not silent:
            return
        if not silent:
            self.is_loading = True
            self.spinner.start()
            self.btn_refresh.set_sensitive(False)

        max_l = self.max_lines
        l_filt = self.current_filter
        kw = self.keyword
        elev = self.is_elevated

        def work():
            return get_kernel_logs(max_lines=max_l, level_filter=l_filt, keyword_filter=kw, elevated=elev)

        def on_done(res):
            if not silent:
                self.spinner.stop()
                self.btn_refresh.set_sensitive(True)
                self.is_loading = False

            self.lbl_source.set_text(res.get("source", "Inconnue"))
            self.lbl_total.set_text(str(res.get("total_count", 0)))
            
            err_c = res.get("error_count", 0)
            self.lbl_err.set_text(str(err_c))
            if err_c > 0:
                self.lbl_err.set_markup(f'<span foreground="#f38ba8" weight="bold">{err_c} ⚠️</span>')
            else:
                self.lbl_err.set_markup('<span foreground="#a6e3a1">0 🟢</span>')

            self.lbl_hw.set_text(str(res.get("hw_count", 0)))

            if res.get("error_message"):
                self.lbl_notif.set_markup(f'<span foreground="#f38ba8" weight="bold">{GLib.markup_escape_text(str(res["error_message"]))}</span>')
            else:
                self.lbl_notif.set_text("")

            # Construction du buffer texte
            self.buffer.set_text("")
            iter_end = self.buffer.get_end_iter()

            entries = res.get("entries", [])
            for e in entries:
                ts = e.get("timestamp", "")
                content = e.get("content", "")
                sev = e.get("severity", "info")
                is_hw = e.get("is_hw", False)

                if ts:
                    self.buffer.insert_with_tags(iter_end, f"[{ts}] ", self.tag_ts)
                
                tag_to_use = self.tag_info
                if sev == "error":
                    tag_to_use = self.tag_err
                elif sev == "warning":
                    tag_to_use = self.tag_warn
                elif is_hw:
                    tag_to_use = self.tag_hw

                self.buffer.insert_with_tags(iter_end, content + "\n", tag_to_use)

            # Auto-scroll vers le bas
            if self.auto_scroll and entries:
                mark = self.buffer.create_mark("end", self.buffer.get_end_iter(), False)
                self.text_view.scroll_to_mark(mark, 0.05, True, 0.0, 1.0)

        def safe_work():
            """Une erreur de lecture ne doit pas laisser la page « en chargement » pour toujours."""
            try:
                return work()
            except Exception as e:
                return {"lines": [], "error_message": f"Lecture du journal impossible : {e}", "error_count": 0}
        threading.Thread(target=lambda: GLib.idle_add(on_done, safe_work()), daemon=True).start()

    def _on_export_click(self, _):
        # Sauvegarde rapide dans le dossier BenchmarkResults ou dossier personnel
        out_dir = os.path.expanduser("~/BenchmarkResults")
        os.makedirs(out_dir, exist_ok=True)
        out_file = os.path.join(out_dir, "kernel_dmesg_export.log")
        
        start_iter = self.buffer.get_start_iter()
        end_iter = self.buffer.get_end_iter()
        txt = self.buffer.get_text(start_iter, end_iter, True)
        try:
            with open(out_file, "w", encoding="utf-8") as f:
                f.write(f"=== Export Journal Noyau BenchSuite ===\nSource : {self.lbl_source.get_text()}\n\n")
                f.write(txt)
            self.lbl_notif.set_markup(f'<span foreground="#a6e3a1" weight="bold">💾 Journal exporté avec succès vers : {GLib.markup_escape_text(str(out_file))}</span>')
        except Exception as e:
            self.lbl_notif.set_markup(f'<span foreground="#f38ba8" weight="bold">⚠️ Erreur lors de l\'export : {GLib.markup_escape_text(str(e))}</span>')
