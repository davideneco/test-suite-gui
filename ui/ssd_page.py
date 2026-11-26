"""
ssd_page.py — Page de diagnostic approfondi SSD & Stockage (NVMe, SATA, SAS, USB)
- Sélection dynamique parmi tous les SSD/disques détectés du système.
- Audit du lien PCIe (Génération et largeur de ligne) et détection de bridage (downgrade).
- Évaluation de la fraîcheur du SSD (Neuf / Occasion / Usure modérée / Fin de vie).
- Télémétrie en direct (Température, Heures de vol, Cycles, Terabytes Written/Read).
- Contrôle strict d'intégrité (Erreurs média, avertissements critiques).
- Possibilité d'élévation Admin (Polkit/Sudo) pour lire 100% des compteurs SMART protégés.
"""

import os
import sys
import json
import threading
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from core.ssd_diagnostics import get_all_ssd_diagnostics
from ui.nvidia_page import run_elevated

class SsdPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.win = win
        self.set_margin_top(24)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.set_margin_bottom(24)

        self.drives = []
        self.current_idx = 0
        self.is_loading = False

        self._build_ui()
        self.refresh_drives(elevated=False)

    def _build_ui(self):
        # ── 1. En-tête ──
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        title = Gtk.Label()
        title.set_markup('<span weight="bold" size="xx-large">💾 Diagnostic &amp; Intégrité SSD / Stockage</span>')
        title.set_halign(Gtk.Align.START)
        hdr_box.append(title)

        desc = Gtk.Label(label="Audit matériel universel pour SSD NVMe, SATA et disques externes : analyse du lien PCIe, usure SMART, télémétrie thermique et détection de défauts.")
        desc.add_css_class("bench-subtitle")
        desc.set_halign(Gtk.Align.START)
        desc.set_wrap(True)
        hdr_box.append(desc)
        self.append(hdr_box)

        # ── 2. Barre de sélection & Actions ──
        bar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        bar_box.add_css_class("toolbar")
        
        lbl_sel = Gtk.Label(label="Disque à inspecter :")
        lbl_sel.add_css_class("params-title")
        bar_box.append(lbl_sel)

        self.combo_drives = Gtk.ComboBoxText()
        self.combo_drives.set_hexpand(True)
        self.combo_drives.connect("changed", self._on_drive_changed)
        bar_box.append(self.combo_drives)

        self.btn_refresh = Gtk.Button(label="🔄 Actualiser")
        self.btn_refresh.add_css_class("btn-secondary")
        self.btn_refresh.set_tooltip_text("Actualisation rapide sans élévation de privilèges")
        self.btn_refresh.connect("clicked", lambda _: self.refresh_drives(elevated=False))
        bar_box.append(self.btn_refresh)

        self.btn_admin = Gtk.Button(label="🛡️ Check SMART Complet (Admin)")
        self.btn_admin.add_css_class("btn-primary")
        self.btn_admin.set_tooltip_text("Récupère 100% des compteurs d'usure, d'heures et d'écriture via privilèges root (sudo/pkexec)")
        self.btn_admin.connect("clicked", self._on_admin_click)
        bar_box.append(self.btn_admin)

        self.spinner = Gtk.Spinner()
        bar_box.append(self.spinner)

        self.append(bar_box)

        self.lbl_notif = Gtk.Label()
        self.lbl_notif.set_wrap(True)
        self.lbl_notif.set_halign(Gtk.Align.START)
        self.append(self.lbl_notif)

        # Conteneur défilant pour le tableau de bord
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        scroll.set_child(self.content_box)
        self.append(scroll)

        # ── 3. Cartes du tableau de bord ──
        
        # Carte A: Santé & Fraîcheur
        self.card_health = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card_health.add_css_class("score-box")
        
        lbl_h_title = Gtk.Label(label="ÉVALUATION DE SANTÉ & FRAÎCHEUR DU SSD")
        lbl_h_title.add_css_class("params-title")
        lbl_h_title.set_halign(Gtk.Align.START)
        self.card_health.append(lbl_h_title)

        self.lbl_freshness = Gtk.Label()
        self.lbl_freshness.set_halign(Gtk.Align.START)
        self.card_health.append(self.lbl_freshness)

        grid_health = Gtk.Grid()
        grid_health.set_row_spacing(8); grid_health.set_column_spacing(24)
        
        grid_health.attach(Gtk.Label(label="Rôle du disque :", halign=Gtk.Align.START), 0, 0, 1, 1)
        self.lbl_os_role = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_health.attach(self.lbl_os_role, 1, 0, 1, 1)

        grid_health.attach(Gtk.Label(label="Points de montage :", halign=Gtk.Align.START), 0, 1, 1, 1)
        self.lbl_mounts = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_mounts.set_wrap(True)
        grid_health.attach(self.lbl_mounts, 1, 1, 1, 1)

        grid_health.attach(Gtk.Label(label="Statut Intégrité :", halign=Gtk.Align.START), 0, 2, 1, 1)
        self.lbl_status = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_health.attach(self.lbl_status, 1, 2, 1, 1)

        grid_health.attach(Gtk.Label(label="Usure SMART (Wear Level) :", halign=Gtk.Align.START), 0, 3, 1, 1)
        self.lbl_wear = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_health.attach(self.lbl_wear, 1, 3, 1, 1)
        self.card_health.append(grid_health)

        self.bar_wear = Gtk.ProgressBar()
        self.bar_wear.set_show_text(True)
        self.card_health.append(self.bar_wear)

        self.content_box.append(self.card_health)

        # Carte B: Interface & Lien PCIe
        self.card_link = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card_link.add_css_class("score-box")

        lbl_l_title = Gtk.Label(label="AUDIT DU LIEN DE TRANSMISSION & PROTOCOLE")
        lbl_l_title.add_css_class("params-title")
        lbl_l_title.set_halign(Gtk.Align.START)
        self.card_link.append(lbl_l_title)

        grid_link = Gtk.Grid()
        grid_link.set_row_spacing(8); grid_link.set_column_spacing(24)
        
        grid_link.attach(Gtk.Label(label="Protocole / Type :", halign=Gtk.Align.START), 0, 0, 1, 1)
        self.lbl_kind = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_kind.add_css_class("metric-value")
        grid_link.attach(self.lbl_kind, 1, 0, 1, 1)

        grid_link.attach(Gtk.Label(label="Bus PCI :", halign=Gtk.Align.START), 2, 0, 1, 1)
        self.lbl_pci = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_pci.add_css_class("metric-value")
        grid_link.attach(self.lbl_pci, 3, 0, 1, 1)

        grid_link.attach(Gtk.Label(label="Lien Actuel Négocié :", halign=Gtk.Align.START), 0, 1, 1, 1)
        self.lbl_link_cur = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_link.attach(self.lbl_link_cur, 1, 1, 1, 1)

        grid_link.attach(Gtk.Label(label="Capacité Maximale :", halign=Gtk.Align.START), 2, 1, 1, 1)
        self.lbl_link_max = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_link.attach(self.lbl_link_max, 3, 1, 1, 1)

        grid_link.attach(Gtk.Label(label="Diagnostic du Lien :", halign=Gtk.Align.START), 0, 2, 1, 1)
        self.lbl_link_status = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_link.attach(self.lbl_link_status, 1, 2, 3, 1)
        
        self.card_link.append(grid_link)
        self.content_box.append(self.card_link)

        # Carte C: Télémétrie Live & Endurance
        self.card_telemetry = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card_telemetry.add_css_class("score-box")

        lbl_t_title = Gtk.Label(label="TÉLÉMÉTRIE EN DIRECT & VOLUME D'UTILISATION")
        lbl_t_title.add_css_class("params-title")
        lbl_t_title.set_halign(Gtk.Align.START)
        self.card_telemetry.append(lbl_t_title)

        grid_tel = Gtk.Grid()
        grid_tel.set_row_spacing(8); grid_tel.set_column_spacing(24)

        grid_tel.attach(Gtk.Label(label="Température interne :", halign=Gtk.Align.START), 0, 0, 1, 1)
        self.lbl_temp = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_tel.attach(self.lbl_temp, 1, 0, 1, 1)

        grid_tel.attach(Gtk.Label(label="Heures d'allumage :", halign=Gtk.Align.START), 2, 0, 1, 1)
        self.lbl_poh = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_tel.attach(self.lbl_poh, 3, 0, 1, 1)

        grid_tel.attach(Gtk.Label(label="Cycles d'allumage :", halign=Gtk.Align.START), 0, 1, 1, 1)
        self.lbl_cycles = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_tel.attach(self.lbl_cycles, 1, 1, 1, 1)

        grid_tel.attach(Gtk.Label(label="Données Écrites (TBW) :", halign=Gtk.Align.START), 2, 1, 1, 1)
        self.lbl_written = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_tel.attach(self.lbl_written, 3, 1, 1, 1)

        grid_tel.attach(Gtk.Label(label="Données Lues :", halign=Gtk.Align.START), 0, 2, 1, 1)
        self.lbl_read = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_tel.attach(self.lbl_read, 1, 2, 1, 1)

        self.card_telemetry.append(grid_tel)
        self.content_box.append(self.card_telemetry)

        # Carte D: Intégrité & Défauts
        self.card_errors = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.card_errors.add_css_class("score-box")

        lbl_e_title = Gtk.Label(label="JOURNAL D'ERREURS & DÉFAUTS DE SURFACE")
        lbl_e_title.add_css_class("params-title")
        lbl_e_title.set_halign(Gtk.Align.START)
        self.card_errors.append(lbl_e_title)

        grid_err = Gtk.Grid()
        grid_err.set_row_spacing(8); grid_err.set_column_spacing(24)

        grid_err.attach(Gtk.Label(label="Erreurs média / secteurs :", halign=Gtk.Align.START), 0, 0, 1, 1)
        self.lbl_err_cnt = Gtk.Label(label="0", halign=Gtk.Align.START)
        grid_err.attach(self.lbl_err_cnt, 1, 0, 1, 1)

        grid_err.attach(Gtk.Label(label="Avertissements critiques :", halign=Gtk.Align.START), 2, 0, 1, 1)
        self.lbl_crit_warn = Gtk.Label(label="Aucun", halign=Gtk.Align.START)
        grid_err.attach(self.lbl_crit_warn, 3, 0, 1, 1)

        grid_err.attach(Gtk.Label(label="Bilan de surface :", halign=Gtk.Align.START), 0, 1, 1, 1)
        self.lbl_err_summary = Gtk.Label(label="—", halign=Gtk.Align.START)
        grid_err.attach(self.lbl_err_summary, 1, 1, 3, 1)

        self.card_errors.append(grid_err)
        self.content_box.append(self.card_errors)

    def refresh_drives(self, elevated=False):
        if self.is_loading:
            return
        self.is_loading = True
        self.spinner.start()
        self.btn_refresh.set_sensitive(False)
        self.btn_admin.set_sensitive(False)

        def work():
            if elevated:
                # pkexec / sudo changent de répertoire courant : chemin du dépôt explicite, sinon « No module named core »
                _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                py_script = f"""
import json, sys
sys.path.insert(0, {_root!r})
from core.ssd_diagnostics import get_all_ssd_diagnostics
print(json.dumps(get_all_ssd_diagnostics(elevated=True)))
"""
                try:
                    res = run_elevated([sys.executable, "-c", py_script], timeout=35)
                    if res and res.returncode == 0 and res.stdout.strip():
                        return json.loads(res.stdout.strip()), True, "🟢 Audit SMART Administrateur terminé avec succès ! 100% des compteurs sont déverrouillés."
                    else:
                        err = res.stderr.strip() if res and res.stderr else "Authentification Polkit / Sudo annulée ou refusée par l'utilisateur."
                        return get_all_ssd_diagnostics(elevated=False), False, f"⚠️ Échec d'élévation : {err}"
                except Exception as e:
                    return get_all_ssd_diagnostics(elevated=False), False, f"⚠️ Erreur lors de l'élévation : {str(e)}"
            return get_all_ssd_diagnostics(elevated=False), False, ""

        def on_done(result):
            self.spinner.stop()
            self.btn_refresh.set_sensitive(True)
            self.btn_admin.set_sensitive(True)
            self.is_loading = False

            drives, is_admin, msg = result
            self.drives = drives or []

            if msg:
                col = "#a6e3a1" if is_admin else "#fab387"
                self.lbl_notif.set_markup(f'<span foreground="{col}" weight="bold">{GLib.markup_escape_text(str(msg))}</span>')
            else:
                self.lbl_notif.set_text("")

            self.combo_drives.handler_block_by_func(self._on_drive_changed)
            self.combo_drives.remove_all()
            for idx, d in enumerate(self.drives):
                star = "⭐ [OS / BOOT] " if d.get("is_os_drive") else ""
                label = f"{star}{d['name']} — {d['model']} ({d['size']} - {d['kind']})"
                self.combo_drives.append(str(idx), label)
            
            if self.drives:
                sel = min(self.current_idx, len(self.drives) - 1)
                self.combo_drives.set_active(sel)
            self.combo_drives.handler_unblock_by_func(self._on_drive_changed)
            self._update_display()

        def safe_work():
            """Une erreur de diagnostic ne doit pas laisser la page « en chargement » pour toujours."""
            try:
                return work()
            except Exception as e:
                return [], False, f"⚠️ Diagnostic impossible : {e}"
        threading.Thread(target=lambda: GLib.idle_add(on_done, safe_work()), daemon=True).start()

    def _on_admin_click(self, _):
        self.refresh_drives(elevated=True)

    def _on_drive_changed(self, _):
        idx = self.combo_drives.get_active()
        if idx >= 0:
            self.current_idx = idx
            self._update_display()

    def _update_display(self):
        if not self.drives or self.current_idx >= len(self.drives):
            self.lbl_freshness.set_markup('<span foreground="#f38ba8" weight="bold">Aucun disque détecté</span>')
            return

        d = self.drives[self.current_idx]

        # Rôle OS & Points de montage
        is_os = d.get("is_os_drive")
        if is_os:
            self.lbl_os_role.set_markup('<span foreground="#f9e2af" weight="bold">👑 Disque Hôte du Système d\'Exploitation (OS / Boot)</span>')
        else:
            self.lbl_os_role.set_markup('<span foreground="#a6adc8">Disque de Stockage Secondaire / Données</span>')

        mpts = d.get("mountpoints", [])
        if mpts:
            self.lbl_mounts.set_markup(f'<span foreground="#89dceb">{GLib.markup_escape_text(" | ".join(mpts[:6]))}</span>')
        else:
            self.lbl_mounts.set_markup('<span foreground="#6c7086">Non monté</span>')

        # Santé & Fraîcheur
        self.lbl_freshness.set_markup(f'<span weight="bold" size="large">{GLib.markup_escape_text(str(d.get("freshness_label", "—")))}</span>')
        self.lbl_status.set_markup(f'<b>{GLib.markup_escape_text(str(d.get("health_status", "—")))}</b>')

        wear = d.get("wear_pct")
        if wear is not None:
            self.lbl_wear.set_markup(f'<span foreground="#fab387" weight="bold">{wear:.1f} %</span>')
            frac = min(1.0, max(0.0, wear / 100.0))
            self.bar_wear.set_fraction(frac)
            self.bar_wear.set_text(f"Usure : {wear:.1f} %")
        else:
            if not d.get("is_ssd"):
                self.lbl_wear.set_markup('<span foreground="#6c7086">N/A (Disque mécanique HDD)</span>')
                self.bar_wear.set_fraction(0.0)
                self.bar_wear.set_text("Non applicable")
            else:
                self.lbl_wear.set_markup('<span foreground="#6c7086">N/A (Nécessite privilèges Admin)</span>')
                self.bar_wear.set_fraction(0.0)
                self.bar_wear.set_text("Usure inconnue")

        # Lien & Protocole
        self.lbl_kind.set_text(d.get("kind", "—"))
        self.lbl_pci.set_text(d.get("pci_bus") or "N/A")
        self.lbl_link_cur.set_markup(f'<b>{GLib.markup_escape_text(str(d.get("pcie_link_cur") or "N/A"))}</b>')
        self.lbl_link_max.set_markup(f'<b>{GLib.markup_escape_text(str(d.get("pcie_link_max") or "N/A"))}</b>')
        self.lbl_link_status.set_markup(f'<b>{GLib.markup_escape_text(str(d.get("pcie_status", "—")))}</b>')

        # Télémétrie
        temp = d.get("temperature_c")
        if temp is not None:
            col = "#a6e3a1" if temp < 50 else ("#fab387" if temp < 70 else "#f38ba8")
            self.lbl_temp.set_markup(f'<span foreground="{col}" weight="bold">{temp:.1f} °C</span>')
        else:
            self.lbl_temp.set_markup('<span foreground="#6c7086">N/A</span>')

        poh = d.get("power_on_hours")
        self.lbl_poh.set_markup(f'<b>{poh:,} h</b>'.replace(',', ' ') if poh is not None else '<span foreground="#6c7086">N/A</span>')

        pc = d.get("power_cycles")
        self.lbl_cycles.set_markup(f'<b>{pc:,} cycles</b>'.replace(',', ' ') if pc is not None else '<span foreground="#6c7086">N/A</span>')

        written = d.get("written_tb")
        self.lbl_written.set_markup(f'<span foreground="#f9e2af" weight="bold">{written:.2f} To</span>' if written is not None else '<span foreground="#6c7086">N/A</span>')

        read_tb = d.get("read_tb")
        self.lbl_read.set_markup(f'<span foreground="#89dceb" weight="bold">{read_tb:.2f} To</span>' if read_tb is not None else '<span foreground="#6c7086">N/A</span>')

        # Erreurs
        err = d.get("media_errors", 0) or 0
        err_col = "#f38ba8" if err > 0 else "#a6e3a1"
        self.lbl_err_cnt.set_markup(f'<span foreground="{err_col}" weight="bold">{GLib.markup_escape_text(str(err))}</span>')

        cw = d.get("critical_warning", "Aucun")
        cw_col = "#f38ba8" if cw not in ("0", "0x0", "None", "Aucun") else "#a6e3a1"
        self.lbl_crit_warn.set_markup(f'<span foreground="{cw_col}" weight="bold">{GLib.markup_escape_text(str(cw))}</span>')

        err_sum = d.get("errors_summary", "Aucune erreur détectée")
        self.lbl_err_summary.set_markup(f'<span foreground="{err_col}" weight="bold">{GLib.markup_escape_text(str(err_sum))}</span>')
