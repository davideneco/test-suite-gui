"""
ui/system_info_page.py — Page d'informations matériel complète
CPU, GPU, RAM, Carte mère, Stockage, Réseau
"""

import os
import threading
import time
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib, Gdk, Pango

from core.system_info import collect_full_system_info, run_hardware_audit


CSS_EXTRA = """
.info-section {
    background-color: #181825;
    border: 1px solid #313244;
    border-radius: 10px;
    padding: 14px 16px;
}
.info-section-title {
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 1.5px;
    color: #f97316;
}
.info-key {
    font-size: 12px;
    color: #6c7086;
    min-width: 160px;
}
.info-val {
    font-size: 12px;
    color: #cdd6f4;
    font-weight: 600;
}
.info-val-warn {
    font-size: 11px;
    color: #f9e2af;
}
.info-val-ok {
    font-size: 12px;
    color: #a6e3a1;
    font-weight: 600;
}
.info-val-err {
    font-size: 12px;
    color: #f38ba8;
    font-weight: 600;
}
.alert-box-ok {
    background-color: #112615;
    border: 1px solid #a6e3a1;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
}
.alert-box-warn {
    background-color: #2b2210;
    border: 1px solid #f9e2af;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
}
.alert-box-err {
    background-color: #2d191e;
    border: 1px solid #f38ba8;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
}
.alert-box-info {
    background-color: #112233;
    border: 1px solid #89b4fa;
    border-radius: 10px;
    padding: 14px 16px;
    margin-bottom: 12px;
}
.alert-item {
    font-size: 12px;
    color: #cdd6f4;
    margin-top: 4px;
}
.info-badge-incomplete {
    background-color: rgba(245, 158, 11, 0.16);
    border: 1px solid #d97706;
    color: #f59e0b;
    border-radius: 6px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 700;
}
"""


class SystemInfoPage(Gtk.Box):
    """Page complète d'informations matériel."""

    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self.win  = win
        self._data = None
        self._incomplete_map = {}
        self._is_page_visible = False
        self._telemetry_timer_id = None
        self._telemetry_refreshing = False
        self._telemetry_summary_lbl = None
        self._telemetry_val_labels = {}
        self._last_telemetry_summary = {}

        # Charger CSS_EXTRA pour toute la page
        css = Gtk.CssProvider()
        css.load_from_data(CSS_EXTRA.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(), css,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._build()

        # Connexion aux signaux de visibilité GTK4 (anti-gaspillage CPU quand la page est masquée)
        self.connect("map", self._on_widget_mapped)
        self.connect("unmap", self._on_widget_unmapped)

        # Lancement automatique de l'analyse système au démarrage
        GLib.idle_add(self._on_scan, None)

    def get_data(self) -> dict:
        return self._data

    def _build(self):
        # Header
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        hdr.set_margin_top(20); hdr.set_margin_start(24)
        hdr.set_margin_end(24); hdr.set_margin_bottom(8)

        t_col = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        t_col.set_hexpand(True)
        t1 = Gtk.Label(label="Informations système")
        t1.add_css_class("bench-title"); t1.set_halign(Gtk.Align.START)
        t2 = Gtk.Label(label="CPU, GPU, RAM, carte mère, stockage, réseau")
        t2.set_ellipsize(Pango.EllipsizeMode.END)
        t2.add_css_class("bench-subtitle"); t2.set_halign(Gtk.Align.START)
        t_col.append(t1); t_col.append(t2)

        btn_scan = Gtk.Button(label="↻ Analyser")
        btn_scan.set_tooltip_text("Relancer la détection complète du matériel")
        btn_scan.add_css_class("btn-run")
        btn_scan.connect("clicked", self._on_scan)
        self._btn_scan = btn_scan

        btn_export = Gtk.Button(label="📄 PDF")
        btn_export.set_tooltip_text("Exporter le rapport d'informations système en PDF")
        btn_export.add_css_class("btn-pdf")
        btn_export.set_sensitive(False)
        btn_export.connect("clicked", self._on_export_pdf)
        self._btn_export = btn_export

        btn_export_html = Gtk.Button(label="🌐 HTML")
        btn_export_html.set_tooltip_text("Exporter le rapport d'informations système en HTML")
        btn_export_html.add_css_class("btn-json")
        btn_export_html.set_sensitive(False)
        btn_export_html.connect("clicked", self._on_export_html)
        self._btn_export_html = btn_export_html

        btn_sensors = Gtk.Button(label="⚙ Capteurs")
        btn_sensors.set_tooltip_text("Choisir les capteurs affichés et relevés")
        btn_sensors.add_css_class("btn-run")
        btn_sensors.connect("clicked", self._on_config_sensors)
        self._btn_sensors = btn_sensors

        t_col.set_hexpand(True)          # le titre prend la place libre : les boutons restent groupés à droite, sans déborder
        hdr.append(t_col)
        hdr.append(btn_sensors)
        hdr.append(btn_scan)
        hdr.append(btn_export)
        hdr.append(btn_export_html)
        self.append(hdr)

        # Progress bar (cachée)
        self._progress = Gtk.ProgressBar()
        self._progress.set_pulse_step(0.05)
        self._progress.set_show_text(True)
        self._progress.set_text("Cliquer sur Analyser pour détecter le matériel")
        self._progress.set_margin_start(24); self._progress.set_margin_end(24)
        self._progress.set_margin_bottom(8)
        self.append(self._progress)

        # Zone de contenu scrollable
        self._scroll = Gtk.ScrolledWindow()
        self._scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self._scroll.set_vexpand(True)

        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        self._content.set_margin_top(8); self._content.set_margin_start(24)
        self._content.set_margin_end(24); self._content.set_margin_bottom(24)

        self._scroll.set_child(self._content)
        self.append(self._scroll)

        # Message initial
        self._show_placeholder()

    def _show_placeholder(self):
        lbl = Gtk.Label()
        lbl.set_markup(
            '<span foreground="#45475a" size="large">\n\n'
            'Cliquez sur <b>↻ Analyser</b>\n'
            'pour detecter CPU, GPU, RAM, Carte mere, Stockage et Reseau\n\n'
            '<span size="small">La detection peut prendre 5 a 15 secondes\n'
            'certaines infos necessitent sudo (dmidecode, smartctl)</span>'
            '</span>')
        lbl.set_justify(Gtk.Justification.CENTER)
        lbl.set_valign(Gtk.Align.CENTER)
        lbl.set_vexpand(True)
        self._content.append(lbl)

    def _clear_content(self):
        c = self._content.get_first_child()
        while c:
            n = c.get_next_sibling()
            self._content.remove(c)
            c = n

    # ── Section builder ───────────────────────────────────────────────────────

    def _section(self, title: str, color: str = "#f97316", section_key: str = "") -> Gtk.Box:
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("info-section")
        hdr = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = Gtk.Label()
        safe_title = GLib.markup_escape_text(title)
        t.set_markup(f'<span foreground="{color}" weight="ultrabold" size="small">{safe_title}</span>')
        t.set_halign(Gtk.Align.START)
        t.set_hexpand(True)
        hdr.append(t)
        if section_key and section_key in self._incomplete_map:
            budget = self._incomplete_map[section_key]
            b_txt = f"⚠ Incomplète (délai {budget} s dépassé)" if budget is not None else "⚠ Incomplète (délai dépassé)"
            badge = Gtk.Label(label=b_txt)
            badge.add_css_class("info-badge-incomplete")
            badge.set_halign(Gtk.Align.END)
            badge.set_tooltip_text(
                "Cette section n'a pas répondu dans son budget de temps et a été abandonnée "
                "(outil bloqué, BMC muette ou bus I²C lent)."
            )
            hdr.append(badge)
        box.append(hdr)
        box.append(Gtk.Separator())
        return box

    def _row(self, section: Gtk.Box, key: str, value: str,
             style: str = "info-val"):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        k = Gtk.Label(label=key)
        k.add_css_class("info-key"); k.set_halign(Gtk.Align.END)
        k.set_size_request(160, -1)
        v = Gtk.Label(label=str(value) if value else "—")
        v.add_css_class(style); v.set_halign(Gtk.Align.START)
        v.set_hexpand(True); v.set_wrap(True); v.set_selectable(True)
        row.append(k); row.append(v)
        section.append(row)

    # ── Scan ──────────────────────────────────────────────────────────────────

    def _on_scan(self, _):
        self._btn_scan.set_sensitive(False)
        self._btn_export.set_sensitive(False)
        self._btn_export_html.set_sensitive(False)
        self._clear_content()
        self._progress.set_text("Collecte des informations matérielles…")
        self._pulse_timer = GLib.timeout_add(80, self._pulse)
        threading.Thread(target=self._do_scan, daemon=True).start()

    def _pulse(self):
        self._progress.pulse()
        return True  # continuer

    def _do_scan(self):
        running: set = set()
        t0 = time.time()

        def progress(section, state):
            """Affiche les sections encore en cours : sur une machine lente, on voit laquelle fait attendre au lieu d'une barre muette."""
            (running.add if state == "en cours" else running.discard)(section)
            txt = ("Collecte en cours : " + ", ".join(sorted(running)) + f" ({time.time() - t0:.0f} s)") if running else "Finalisation…"
            GLib.idle_add(lambda t=txt: (self._progress.set_text(t), False)[1])

        data = collect_full_system_info(progress=progress)
        GLib.idle_add(self._on_scan_done, data)

    def _on_scan_done(self, data: dict):
        # Arrêter le pulse
        GLib.source_remove(self._pulse_timer)
        self._progress.set_fraction(1.0)
        late = data.get("sections_incompletes") or []
        if late:
            self._progress.set_text(f"Analyse terminée — {len(late)} section(s) incomplète(s)")
        else:
            self._progress.set_text("Analyse terminée")

        self._data = data
        self._clear_content()

        # Audit matériel et affichage des alertes
        alerts = run_hardware_audit(data)
        if alerts:
            self._display_alerts(alerts)
        else:
            self._display_status_ok()

        self._populate(data)

        self._btn_scan.set_sensitive(True)
        self._btn_export.set_sensitive(True)
        self._btn_export_html.set_sensitive(True)
        if late:
            self.win._set_status(f"Infos système collectées ({len(late)} section(s) incomplète(s))")
        else:
            self.win._set_status("Informations système collectées")
        return False

    def _display_alerts(self, alerts: list):
        has_error = any(a["type"] == "error" for a in alerts)
        has_warning = any(a["type"] == "warning" for a in alerts)
        
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        
        if has_error:
            box.add_css_class("alert-box-err")
            title_markup = '<span foreground="#f38ba8" weight="bold" size="large">🚨 ALERTE : Anomalies critiques détectées !</span>'
        elif has_warning:
            box.add_css_class("alert-box-warn")
            title_markup = '<span foreground="#f9e2af" weight="bold" size="large">⚠️ ATTENTION : Quelques points matériels à vérifier</span>'
        else:
            box.add_css_class("alert-box-info")
            title_markup = '<span foreground="#89b4fa" weight="bold" size="large">ℹ️ INFORMATION : Recommandations système</span>'

        t = Gtk.Label()
        t.set_markup(title_markup)
        t.set_halign(Gtk.Align.START)
        box.append(t)
        box.append(Gtk.Separator())

        for alert in alerts:
            item_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            item_box.set_margin_start(12)

            atype = alert.get("type", "warning")
            if atype == "error":
                prefix = "🔴"
                color = "#f38ba8"
            elif atype == "warning":
                prefix = "🟡"
                color = "#f9e2af"
            else:
                prefix = "🔵"
                color = "#89b4fa"

            lbl_title = Gtk.Label()
            lbl_title.set_markup(f'<b>{prefix} <span foreground="{color}">{GLib.markup_escape_text(str(alert["category"]))}</span></b> — <span weight="bold">{GLib.markup_escape_text(str(alert["title"]))}</span>')
            lbl_title.set_halign(Gtk.Align.START)

            lbl_desc = Gtk.Label(label=alert["desc"])
            lbl_desc.add_css_class("alert-item")
            lbl_desc.set_halign(Gtk.Align.START)
            lbl_desc.set_wrap(True)

            item_box.append(lbl_title)
            item_box.append(lbl_desc)
            box.append(item_box)

        self._content.append(box)

    def _display_status_ok(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.add_css_class("alert-box-ok")

        t = Gtk.Label()
        t.set_markup('<span foreground="#a6e3a1" weight="bold" size="large">✓ Système entièrement opérationnel</span>')
        t.set_halign(Gtk.Align.START)
        box.append(t)
        box.append(Gtk.Separator())

        lbl_desc = Gtk.Label(label="Toutes les barrettes de RAM sont homogènes et tous les périphériques PCIe (cartes graphiques, SSD NVMe, cartes réseau haut débit) fonctionnent dans leur configuration et bande passante maximales théoriques.")
        lbl_desc.add_css_class("alert-item")
        lbl_desc.set_halign(Gtk.Align.START)
        lbl_desc.set_wrap(True)
        box.append(lbl_desc)

        self._content.append(box)

    # ── Affichage ─────────────────────────────────────────────────────────────

    def _populate(self, data: dict):
        self._incomplete_map = {
            item["section"]: item.get("budget_s")
            for item in (data.get("sections_incompletes") or [])
            if isinstance(item, dict) and item.get("section")
        }
        self._populate_os(data.get("os", {}))
        self._populate_cpu(data.get("cpu", {}))
        self._populate_ram(data.get("ram", {}))
        self._populate_mb(data.get("carte_mere", {}))
        self._populate_bmc(data.get("bmc", {}))
        self._populate_telemetry(data.get("telemetry", []), data.get("telemetry_summary", {}))
        self._populate_gpus(data.get("gpus", []))
        self._populate_storage(data.get("stockage", []))
        self._populate_network(data.get("reseau", []))
        self._populate_pcie(data.get("pcie", []), data.get("gpus", []))
        self._populate_pmbus(data.get("pmbus", []))

    def _populate_os(self, os_: dict):
        sec = self._section("SYSTÈME D'EXPLOITATION", "#34d399", "os")
        self._row(sec, "Distribution",  os_.get("distro", "?"))
        self._row(sec, "Noyau",         os_.get("kernel", "?"))
        self._row(sec, "Architecture",   os_.get("architecture", "?"))
        self._row(sec, "Nom d'hôte",        os_.get("hostname", "?"))
        if os_.get("uptime"):
            self._row(sec, "Disponibilité", os_["uptime"])
        self._content.append(sec)

    def _populate_cpu(self, cpu: dict):
        sec = self._section("PROCESSEUR (CPU)", "#f97316", "cpu")
        self._row(sec, "Modèle",     cpu.get("modele", "?"))
        cores   = cpu.get("coeurs_phys", 0)
        threads = cpu.get("threads", 0)
        self._row(sec, "Cœurs / threads", f"{cores} physiques / {threads} logiques")
        if cpu.get("socket"):       self._row(sec, "Socket",       cpu["socket"])
        if cpu.get("freq_max_mhz"): self._row(sec, "Fréq. max",    cpu["freq_max_mhz"])
        if cpu.get("freq_cur_mhz"): self._row(sec, "Fréq. actuelle", cpu["freq_cur_mhz"])
        if cpu.get("cache_l3"):     self._row(sec, "Cache L3",     cpu["cache_l3"])
        if cpu.get("microcode"):    self._row(sec, "Microcode",    cpu["microcode"])
        if cpu.get("numa_noeuds"):  self._row(sec, "Nœuds NUMA",  str(cpu["numa_noeuds"]))
        vulns = cpu.get("vulnerabilites", [])
        if vulns:
            self._row(sec, "Vulnérabilités",
                      f"{len(vulns)} détectée(s)", "info-val-warn")
            for v in vulns[:5]:
                self._row(sec, "", v, "info-val-warn")
        self._content.append(sec)

    def _populate_ram(self, ram: dict):
        sec = self._section("MÉMOIRE VIVE (RAM)", "#22d3ee", "ram")
        total = ram.get("total_go", 0)
        used  = ram.get("utilise_go", 0)
        pct   = ram.get("utilisation_pct", 0)
        self._row(sec, "Capacité totale",  f"{total} Go")
        self._row(sec, "Utilisée",          f"{used} Go  ({pct}%)")
        self._row(sec, "Disponible",        f"{ram.get('disponible_go', '?')} Go")
        if ram.get("type"):        self._row(sec, "Type",         ram["type"])
        if ram.get("vitesse_mhz"): self._row(sec, "Vitesse",      ram["vitesse_mhz"])
        if ram.get("ecc"):         self._row(sec, "ECC",          ram["ecc"], "info-val-ok")
        if ram.get("temperature_max_c"):
            self._row(sec, "Temp RAM max", f"{ram['temperature_max_c']} °C", "info-val-ok")
        if ram.get("temperature_avg_c"):
            self._row(sec, "Temp RAM moyenne", f"{ram['temperature_avg_c']} °C")
        if ram.get("power_total_w"):
            self._row(sec, "Puissance RAM", f"{ram['power_total_w']} W")
        if ram.get("sensor_sources"):
            self._row(sec, "Sources capteurs", ", ".join(ram.get("sensor_sources", [])))
        sensors = ram.get("capteurs", [])
        if sensors:
            # V5.4: ne pas lister toutes les sondes DDR5 dans Infos systeme.
            # Sur les serveurs 16/24 DIMM, cela pollue l'onglet. On garde le
            # resume max/moyenne/puissance ci-dessus et un simple compteur.
            temp_count = sum(1 for s in sensors if s.get("kind") == "temperature")
            power_count = sum(1 for s in sensors if s.get("kind") == "power")
            details = []
            if temp_count:
                details.append(f"{temp_count} temperature(s)")
            if power_count:
                details.append(f"{power_count} puissance(s)")
            self._row(sec, "Capteurs RAM détectés", ", ".join(details) if details else str(len(sensors)))
        barrettes = ram.get("barrettes", [])
        if ram.get("emplacements"):
            self._row(sec, "Emplacements RAM", ram["emplacements"])
        if barrettes:
            for i, b in enumerate(barrettes):
                parts = [b.get("taille","?")]
                if b.get("type"):      parts.append(b["type"])
                if b.get("vitesse"):   parts.append(b["vitesse"])
                if b.get("fabricant"): parts.append(b["fabricant"])
                if b.get("ref"):       parts.append(b["ref"])
                slot = b.get("slot", f"Slot {i+1}")
                self._row(sec, slot, "  |  ".join(parts))
        self._content.append(sec)

    def _populate_mb(self, mb: dict):
        sec = self._section("CARTE MÈRE", "#a78bfa", "carte_mere")
        fabricant = mb.get("fabricant", "")
        modele    = mb.get("modele", "?")
        self._row(sec, "Fabricant / modèle",
                  f"{fabricant} {modele}".strip() if fabricant else modele)
        if mb.get("version"):   self._row(sec, "Révision",   mb["version"])
        if mb.get("bios"):
            bios_str = mb["bios"]
            if mb.get("bios_date"): bios_str += f"  ({mb['bios_date']})"
            self._row(sec, "BIOS / UEFI", bios_str)
        if mb.get("chipset"):   self._row(sec, "Chipset",    mb["chipset"])
        if mb.get("mode_boot"): self._row(sec, "Mode de démarrage", mb["mode_boot"])
        if mb.get("iommu"):     self._row(sec, "IOMMU",      mb["iommu"])
        if not any([mb.get("fabricant"), mb.get("modele"), mb.get("bios")]):
            self._row(sec, "Note",
                      "dmidecode requis pour les details (sudo)",
                      "info-val-warn")
        self._content.append(sec)

    def _populate_bmc(self, bmc: dict):
        sec = self._section("BMC / IPMI & REDFISH", "#f59e0b", "bmc")
        bmc = bmc or {}
        if not bmc.get("present"):
            notes = "; ".join(bmc.get("notes", [])) if bmc else ""
            self._row(sec, "Détection IPMI locale", notes or "Aucune BMC/IPMI locale détectée (/dev/ipmi* absent)", "info-val-warn")
        else:
            if bmc.get("firmware_revision"): self._row(sec, "Version BMC", bmc["firmware_revision"])
            if bmc.get("manufacturer"):      self._row(sec, "Fabricant", bmc["manufacturer"])
            if bmc.get("ipmi_version"):      self._row(sec, "Version IPMI", bmc["ipmi_version"])
            if bmc.get("device_id"):         self._row(sec, "Device ID", bmc["device_id"])
            if bmc.get("product_id"):        self._row(sec, "Product ID", bmc["product_id"])
            if bmc.get("mac"):               self._row(sec, "MAC BMC", bmc["mac"])
            if bmc.get("channel"):           self._row(sec, "Channel LAN", bmc["channel"])
            if bmc.get("sel_status"):        self._row(sec, "SEL", bmc["sel_status"])
            if bmc.get("ip_address"):        self._row(sec, "IP BMC", bmc["ip_address"])

        if bmc.get("redfish_present"):
            rf_url = bmc.get("redfish_url", "")
            self._row(sec, "API Redfish", f"Disponible ({rf_url})")

        # ── Formulaire de configuration Redfish REST API (Toujours accessible) ──
        rf_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        rf_box.set_margin_top(12)
        rf_box.set_margin_bottom(8)
        rf_box.add_css_class("params-panel")

        rf_title = Gtk.Label()
        rf_title.set_markup("<b>Configuration Redfish REST API (Télémétrie Carte Mère)</b>")
        rf_title.set_halign(Gtk.Align.START)
        rf_box.append(rf_title)

        rf_grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        
        # Host IP
        l_host = Gtk.Label(label="Hôte / IP BMC :")
        l_host.add_css_class("param-label")
        l_host.set_halign(Gtk.Align.END)
        e_host = Gtk.Entry()
        e_host.set_text(os.environ.get("REDFISH_HOST") or bmc.get("ip_address") or "127.0.0.1")
        e_host.set_placeholder_text("ex: 192.168.1.100 ou 127.0.0.1")
        rf_grid.attach(l_host, 0, 0, 1, 1)
        rf_grid.attach(e_host, 1, 0, 1, 1)

        # User
        l_user = Gtk.Label(label="Utilisateur :")
        l_user.add_css_class("param-label")
        l_user.set_halign(Gtk.Align.END)
        e_user = Gtk.Entry()
        e_user.set_text(os.environ.get("REDFISH_USER") or "")
        e_user.set_placeholder_text("ex: ADMIN ou root")
        rf_grid.attach(l_user, 0, 1, 1, 1)
        rf_grid.attach(e_user, 1, 1, 1, 1)

        # Pass
        l_pass = Gtk.Label(label="Mot de passe :")
        l_pass.add_css_class("param-label")
        l_pass.set_halign(Gtk.Align.END)
        e_pass = Gtk.PasswordEntry()
        e_pass.set_text(os.environ.get("REDFISH_PASS") or "")
        rf_grid.attach(l_pass, 0, 2, 1, 1)
        rf_grid.attach(e_pass, 1, 2, 1, 1)

        rf_box.append(rf_grid)

        status_lbl = Gtk.Label()
        status_lbl.set_halign(Gtk.Align.START)
        status_lbl.set_wrap(True)

        btn_test = Gtk.Button(label="⚡ Tester la connexion Redfish & Appliquer")
        btn_test.add_css_class("btn-run")
        btn_test.set_halign(Gtk.Align.START)

        def on_test_redfish(button):
            h = e_host.get_text().strip()
            u = e_user.get_text().strip()
            p = e_pass.get_text().strip()
            if h:
                os.environ["REDFISH_HOST"] = h
            if u:
                os.environ["REDFISH_USER"] = u
            if p:
                os.environ["REDFISH_PASS"] = p

            button.set_sensitive(False)
            status_lbl.set_markup("<i>Connexion Redfish en cours…</i>")

            def do_check():
                from core.redfish_client import is_redfish_available, collect_redfish_sensors
                avail, base_url, creds = is_redfish_available(host=h, user=u, password=p, timeout=4)
                if avail:
                    m_sensors = collect_redfish_sensors(host=h, user=u, password=p)
                    msg = f"<span foreground='#a6e3a1'>✓ Redfish disponible sur <b>{base_url}</b> ({len(m_sensors)} capteurs détectés)</span>"
                else:
                    msg = "<span foreground='#f38ba8'>❌ API Redfish non joignable (Vérifier l'IP, le port 443 ou les accès)</span>"
                
                def update_ui():
                    status_lbl.set_markup(msg)
                    button.set_sensitive(True)
                GLib.idle_add(update_ui)

            threading.Thread(target=do_check, daemon=True).start()

        btn_test.connect("clicked", on_test_redfish)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        btn_row.append(btn_test)
        btn_row.append(status_lbl)
        rf_box.append(btn_row)

        sec.append(rf_box)

        # Ligne d'action pour ouvrir le KVM / Console Virtuelle
        target_ip = bmc.get("ip_address") or os.environ.get("REDFISH_HOST")
        if target_ip:
            row_btn = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row_btn.set_margin_top(8)
            lbl_spacer = Gtk.Label()
            lbl_spacer.set_size_request(160, -1)
            
            btn_kvm = Gtk.Button(label="Ouvrir la Console Virtuelle (KVM / BIOS Redfish) dans le navigateur…")
            btn_kvm.add_css_class("suggest-btn")
            btn_kvm.set_halign(Gtk.Align.START)
            
            def on_kvm_clicked(button, bmc_ip=target_ip):
                import webbrowser
                webbrowser.open(f"https://{bmc_ip}")
                
            btn_kvm.connect("clicked", on_kvm_clicked)
            row_btn.append(lbl_spacer)
            row_btn.append(btn_kvm)
            sec.append(row_btn)

        self._content.append(sec)

    def _on_widget_mapped(self, _widget):
        """Déclenché quand l'onglet Informations Système devient visible sur l'écran."""
        self._is_page_visible = True
        if self._telemetry_val_labels and not self._telemetry_timer_id:
            self._telemetry_timer_id = GLib.timeout_add_seconds(2, self._trigger_telemetry_live_update)
            self._trigger_telemetry_live_update()

    def _on_widget_unmapped(self, _widget):
        """Déclenché quand l'utilisateur quitte l'onglet Informations Système (figer les capteurs)."""
        self._is_page_visible = False
        if self._telemetry_timer_id:
            GLib.source_remove(self._telemetry_timer_id)
            self._telemetry_timer_id = None
        lbl_sum = getattr(self, "_telemetry_summary_lbl", None)
        if lbl_sum and hasattr(self, "_last_telemetry_summary"):
            cnt = self._last_telemetry_summary.get("metric_count", 0)
            srcs = ", ".join(self._last_telemetry_summary.get("sources", [])) or "Système"
            tot_pwr = self._last_telemetry_summary.get("power_total_w", 0.0)
            max_t = self._last_telemetry_summary.get("temperature_max_c", 0.0)
            lbl_sum.set_markup(
                f"<b>{cnt} capteurs actifs</b> · Sources: <span foreground='#22d3ee'>{GLib.markup_escape_text(str(srcs))}</span>" +
                (f" · Consommation système: <b>{tot_pwr} W</b>" if tot_pwr > 0 else "") +
                (f" · Temp. max: <b>{max_t} °C</b>" if max_t > 0 else "") +
                "  <span size='small' foreground='#6c7086'>⏸ Figé (Page inactive)</span>"
            )

    def _populate_telemetry(self, telemetry: list, summary: dict):
        sec = self._section("TÉLÉMÉTRIE & CAPTEURS CARTE MÈRE (REDFISH / IPMI / HWMON)", "#06b6d4", "telemetry")
        
        if not telemetry:
            lbl = Gtk.Label(label="Aucun capteur sélectionné. Utilisez le bouton « ⚙ Capteurs » en haut pour les activer.")
            lbl.set_halign(Gtk.Align.START)
            lbl.set_margin_top(10)
            sec.append(lbl)
            self._content.append(sec)
            return

        self._last_telemetry_summary = summary or {}
        
        # En-tête résumé
        cnt = summary.get("metric_count", len(telemetry))
        srcs = ", ".join(summary.get("sources", [])) or "Système"
        tot_pwr = summary.get("power_total_w", 0.0)
        max_t = summary.get("temperature_max_c", 0.0)

        sum_lbl = Gtk.Label()
        sum_lbl.set_markup(f"<b>{cnt} capteurs actifs</b> · Sources: <span foreground='#22d3ee'>{GLib.markup_escape_text(str(srcs))}</span>" +
                           (f" · Consommation système: <b>{tot_pwr} W</b>" if tot_pwr > 0 else "") +
                           (f" · Temp. max: <b>{max_t} °C</b>" if max_t > 0 else "") +
                           "  <span size='small' foreground='#a6e3a1'>● Temps réel (2s)</span>")
        sum_lbl.set_halign(Gtk.Align.START)
        sum_lbl.set_margin_bottom(8)
        sec.append(sum_lbl)
        self._telemetry_summary_lbl = sum_lbl

        self._telemetry_val_labels = {}

        # Grille à 2 colonnes pour un affichage compact anti-overflow (Norme AGENTS.md)
        grid = Gtk.Grid(column_spacing=16, row_spacing=6)
        
        # Affichage des 60 premiers capteurs principaux
        for idx, m in enumerate(telemetry[:60]):
            name = m.get("name", "Capteur")
            val = m.get("value", 0.0)
            unit = m.get("unit", "")
            source = m.get("source", "")
            cat = m.get("category", "")

            icon = "🌡️" if cat == "temperature" else ("⚡" if cat == "power" else ("🔌" if cat == "voltage" else ("🌀" if cat == "fan" else "📊")))
            val_str = f"{val} {unit}".strip()

            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            lbl_name = Gtk.Label()
            lbl_name.set_markup(f"{icon} <span foreground='#93c5fd'>{GLib.markup_escape_text(str(name))}</span> <span size='small' foreground='#6b7280'>({GLib.markup_escape_text(str(source))})</span>")
            lbl_name.set_halign(Gtk.Align.START)
            lbl_name.set_hexpand(True)

            lbl_val = Gtk.Label()
            lbl_val.set_markup(f"<b>{val_str}</b>")
            lbl_val.set_halign(Gtk.Align.END)
            self._telemetry_val_labels[name] = lbl_val

            row_box.append(lbl_name)
            row_box.append(lbl_val)

            col = idx % 2
            row = idx // 2
            grid.attach(row_box, col, row, 1, 1)

        sec.append(grid)
        self._content.append(sec)

        # Lancement du timer de mise à jour temps réel uniquement si la page est affichée
        if getattr(self, "_telemetry_timer_id", None):
            GLib.source_remove(self._telemetry_timer_id)
            self._telemetry_timer_id = None
        if self._is_page_visible:
            self._telemetry_timer_id = GLib.timeout_add_seconds(2, self._trigger_telemetry_live_update)

    def _trigger_telemetry_live_update(self):
        if not getattr(self, "_is_page_visible", False):
            if getattr(self, "_telemetry_timer_id", None):
                GLib.source_remove(self._telemetry_timer_id)
                self._telemetry_timer_id = None
            return False
        if getattr(self, "_telemetry_refreshing", False):
            return True
        self._telemetry_refreshing = True
        threading.Thread(target=self._async_fetch_telemetry, daemon=True).start()
        return True

    def _async_fetch_telemetry(self):
        try:
            from core.telemetry_aggregator import collect_all_motherboard_telemetry, summarize_motherboard_telemetry
            m_tel = collect_all_motherboard_telemetry()
            m_sum = summarize_motherboard_telemetry(m_tel)
            GLib.idle_add(self._apply_telemetry_live_update, m_tel, m_sum)
        except Exception:
            self._telemetry_refreshing = False

    def _apply_telemetry_live_update(self, m_tel: list, m_sum: dict):
        self._telemetry_refreshing = False
        if not getattr(self, "_is_page_visible", False):
            return False
        lbl_sum = getattr(self, "_telemetry_summary_lbl", None)
        lbl_map = getattr(self, "_telemetry_val_labels", {})
        if not lbl_sum or not lbl_map:
            return False

        self._last_telemetry_summary = m_sum or {}
        cnt = m_sum.get("metric_count", len(m_tel))
        srcs = ", ".join(m_sum.get("sources", [])) or "Système"
        tot_pwr = m_sum.get("power_total_w", 0.0)
        max_t = m_sum.get("temperature_max_c", 0.0)

        lbl_sum.set_markup(
            f"<b>{cnt} capteurs actifs</b> · Sources: <span foreground='#22d3ee'>{GLib.markup_escape_text(str(srcs))}</span>" +
            (f" · Consommation système: <b>{tot_pwr} W</b>" if tot_pwr > 0 else "") +
            (f" · Temp. max: <b>{max_t} °C</b>" if max_t > 0 else "") +
            "  <span size='small' foreground='#a6e3a1'>● Temps réel (2s)</span>"
        )

        for m in m_tel:
            name = m.get("name", "")
            val = m.get("value", 0.0)
            unit = m.get("unit", "")
            val_str = f"{val} {unit}".strip()
            if name in lbl_map:
                lbl_map[name].set_markup(f"<b>{val_str}</b>")
        return False

    def _populate_pmbus(self, pmbus: list):
        if not pmbus and "pmbus" not in self._incomplete_map:
            return
        sec = self._section("ALIMENTATION & PMBUS (I2C DIRECT)", "#a78bfa", "pmbus")
        if not pmbus:
            budget = self._incomplete_map.get("pmbus")
            self._row(
                sec, "Collecte",
                f"Abandonnée après {budget} s (balayage bus I²C lent ou muet)" if budget else "Abandonnée (délai dépassé)",
                "info-val-warn",
            )
            self._content.append(sec)
            return
        for idx, dev in enumerate(pmbus):
            addr = dev.get("address", "?")
            bus = dev.get("bus", "?")
            mfr = dev.get("manufacturer", "Inconnu")
            model = dev.get("model", "Inconnu")
            
            # Titre du périphérique
            lbl = Gtk.Label()
            lbl.set_markup(f"<b>Périphérique {idx+1} : {GLib.markup_escape_text(str(mfr))} {GLib.markup_escape_text(str(model))}</b> (I2C Bus {bus}, Adresse {addr})")
            lbl.set_halign(Gtk.Align.START)
            lbl.set_margin_top(6)
            sec.append(lbl)
            
            # Télémétrie
            if dev.get("vin") is not None:
                self._row(sec, "  Tension Entrée (Vin)", f"{dev['vin']} V")
            if dev.get("iin") is not None:
                self._row(sec, "  Courant Entrée (Iin)", f"{dev['iin']} A")
            if dev.get("pin") is not None:
                self._row(sec, "  Puissance Entrée (Pin)", f"{dev['pin']} W")
            if dev.get("vout") is not None:
                self._row(sec, "  Tension Sortie (Vout)", f"{dev['vout']} V")
            if dev.get("iout") is not None:
                self._row(sec, "  Courant Sortie (Iout)", f"{dev['iout']} A")
            if dev.get("pout") is not None:
                self._row(sec, "  Puissance Sortie (Pout)", f"{dev['pout']} W")
            if dev.get("temp1") is not None:
                self._row(sec, "  Température 1", f"{dev['temp1']} °C")
            if dev.get("temp2") is not None:
                self._row(sec, "  Température 2", f"{dev['temp2']} °C")
            if dev.get("fan1") is not None:
                self._row(sec, "  Vitesse Ventilateur", f"{dev['fan1']} RPM")
        self._content.append(sec)

    def _populate_gpus(self, gpus: list):
        sec = self._section("CARTE(S) GRAPHIQUE(S) & ACCÉLÉRATEUR(S) PCIe", "#e879f9", "gpus")
        if not gpus:
            self._row(sec, "Détection", "Aucun GPU ou Accélérateur détecté", "info-val-warn")
        for i, gpu in enumerate(gpus):
            if i > 0:
                sec.append(Gtk.Separator())
            prefix = f"Périphérique {i} — " if len(gpus) > 1 else ""
            self._row(sec, f"{prefix}Nom", gpu.get("nom") or gpu.get("modele", "?"))
            if gpu.get("pci_bus"): self._row(sec, "PCI Bus ID", gpu["pci_bus"])
            if gpu.get("modele"):  self._row(sec, "Modèle", gpu["modele"])
            if gpu.get("vram_total_mo"): self._row(sec, "VRAM totale", f"{gpu['vram_total_mo']} Mo")
            if gpu.get("puissance_max_w"): self._row(sec, "Puissance max", f"{gpu['puissance_max_w']} W")
            temp_value = gpu.get("temperature_c") or gpu.get("temperature_max_c")
            if temp_value:
                try:
                    temp = float(str(temp_value).replace(",", "."))
                except Exception:
                    temp = 0
                
                # Dynamic threshold using limite_thermique_c
                t_limit = 85.0
                try:
                    if gpu.get("limite_thermique_c"):
                        t_limit = float(str(gpu.get("limite_thermique_c")).replace(",", "."))
                except Exception:
                    pass
                    
                style = "info-val-err" if temp > t_limit else ("info-val-warn" if temp > (t_limit - 10) else "info-val-ok")
                self._row(sec, "Température", f"{temp_value} °C", style)
            # Limite thermique retirée temporairement à la demande de l'utilisateur
            if gpu.get("hotspot_c"):  self._row(sec, "Hotspot", f"{gpu['hotspot_c']} °C")
            if gpu.get("vram_temp_c"): self._row(sec, "Temp. VRAM", f"{gpu['vram_temp_c']} °C")
            if gpu.get("serial"):     self._row(sec, "Numéro de série", gpu["serial"])
            if gpu.get("mcio"):       self._row(sec, "Connecteurs MCIO", gpu["mcio"])
            if gpu.get("pcie_gen") or gpu.get("pcie_largeur"):
                gen = gpu.get("pcie_gen", "?")
                width = gpu.get("pcie_largeur", "?")
                speed = f" ({gpu['pcie_vitesse']})" if gpu.get("pcie_vitesse") else ""
                self._row(sec, "Interface PCIe", f"Gen {gen} x{width}{speed}")
            pilote = gpu.get("pilote") or gpu.get("driver_version")
            if gpu.get("driver_version") and gpu.get("pilote"):
                pilote = f"{gpu['pilote']} / {gpu['driver_version']}"
            if pilote: self._row(sec, "Pilote", pilote)
        self._content.append(sec)

    def _storage_group_name(self, drv: dict) -> str:
        dtype = (drv.get("type") or "").upper()
        if "NVME" in dtype:
            return "NVMe / PCIe"
        if "USB" in dtype:
            return "Stockage USB"
        if "SATA" in dtype:
            return "SATA SSD / HDD"
        if "SAS" in dtype:
            return "SAS"
        return "Autres disques"

    def _storage_group_header(self, section: Gtk.Box, title: str):
        lbl = Gtk.Label()
        lbl.set_markup(f'<span foreground="#f9e2af" weight="bold">{GLib.markup_escape_text(str(title))}</span>')
        lbl.set_halign(Gtk.Align.START)
        section.append(lbl)

    def _populate_storage(self, drives: list):
        sec = self._section("STOCKAGE", "#e879f9", "stockage")
        if not drives:
            self._row(sec, "Note", "lsblk requis", "info-val-warn")

        order = ["NVMe / PCIe", "SATA SSD / HDD", "SAS", "Stockage USB", "Autres disques"]
        grouped = {name: [] for name in order}
        for drv in drives:
            grouped.setdefault(self._storage_group_name(drv), []).append(drv)

        disk_num = 1
        first_group = True
        for group in order:
            items = grouped.get(group, [])
            if not items:
                continue
            if not first_group:
                sec.append(Gtk.Separator())
            first_group = False
            self._storage_group_header(sec, group)

            for drv in items:
                if disk_num > 1:
                    sec.append(Gtk.Separator())
                dtype = drv.get("type", "?")
                iface = drv.get("interface", "?")
                label = f"{drv.get('nom', '?')}  —  {dtype} / {iface}  —  {drv.get('taille', '?')}"
                if drv.get("modele"):
                    label += f"  |  {drv['modele']}"
                self._row(sec, f"Disque {disk_num}", label)
                disk_num += 1

                if drv.get("note"):
                    self._row(sec, "Note", drv["note"], "info-val-warn")
                if drv.get("sante"):
                    style = "info-val-ok" if "OK" in drv["sante"] else "info-val-err"
                    self._row(sec, "Santé SMART", drv["sante"], style)
                if drv.get("smart_supported"):
                    self._row(sec, "SMART", drv["smart_supported"])
                if drv.get("pci_bus"):
                    self._row(sec, "PCI Bus ID", drv["pci_bus"])
                if drv.get("pcie_gen") or drv.get("pcie_largeur"):
                    gen = drv.get("pcie_gen", "?")
                    width = drv.get("pcie_largeur", "?")
                    speed = f" ({drv['pcie_vitesse']})" if drv.get("pcie_vitesse") else ""
                    self._row(sec, "PCIe", f"Gen {gen} x{width}{speed}")
                if drv.get("vitesse_interface") or drv.get("vitesse_negociee"):
                    txt = drv.get("vitesse_interface") or "SATA"
                    if drv.get("vitesse_negociee"):
                        txt += f"  (négocié: {drv['vitesse_negociee']})"
                    self._row(sec, "Vitesse interface", txt)
                elif drv.get("usb_speed"):
                    self._row(sec, "Vitesse USB", drv["usb_speed"])
                if drv.get("interface_detail"):
                    self._row(sec, "Détail interface", drv["interface_detail"])
                if drv.get("rotation_rpm"):
                    self._row(sec, "Rotation", drv["rotation_rpm"])
                if drv.get("pilote"):
                    self._row(sec, "Pilote", drv["pilote"])
                if drv.get("firmware"):
                    self._row(sec, "Firmware", drv["firmware"])
                if drv.get("serial"):
                    self._row(sec, "N° de série", drv["serial"])
                if drv.get("temperature_c"):
                    self._row(sec, "Température", f"{drv['temperature_c']} °C")
                if drv.get("endurance_pct"):
                    self._row(sec, "Endurance utilisée", f"{drv['endurance_pct']}%")
                for p in drv.get("partitions", []):
                    mp = f" → {p['mp']}" if p.get("mp") else " (non monte)"
                    self._row(sec, f"  {p['nom']}",
                              f"{p['taille']}  {p.get('fs','')} {mp}",
                              "info-val")
        self._content.append(sec)

    def _populate_network(self, interfaces: list):
        sec = self._section("RÉSEAU", "#34d399", "reseau")
        if not interfaces:
            self._row(sec, "Note", "Aucune interface detectee")
        for iface in interfaces:
            state = iface.get("etat", "?")
            style = "info-val-ok" if state == "UP" else "info-val-warn"
            label = f"[{state}]"
            if iface.get("vitesse"): label += f"  {iface['vitesse']}"
            if iface.get("duplex"):  label += f"  {iface['duplex']}"
            if iface.get("mtu"):     label += f"  MTU {iface['mtu']}"
            self._row(sec, iface["nom"], label, style)
            if iface.get("modele"): self._row(sec, "Modèle", iface["modele"])
            if iface.get("pci_bus"): self._row(sec, "PCI Bus ID", iface["pci_bus"])
            if iface.get("pcie_gen") or iface.get("pcie_largeur"):
                gen = iface.get("pcie_gen", "?")
                width = iface.get("pcie_largeur", "?")
                speed = f" ({iface['pcie_vitesse']})" if iface.get("pcie_vitesse") else ""
                self._row(sec, "PCIe", f"Gen {gen} x{width}{speed}")
            if iface.get("pilote"): self._row(sec, "Pilote", iface["pilote"])
            if iface.get("firmware"): self._row(sec, "Firmware", iface["firmware"])
            if iface.get("mac"): self._row(sec, "MAC", iface["mac"])
            for addr in iface.get("adresses", []):
                self._row(sec, "", addr, "info-val")
        self._content.append(sec)

    def _populate_pcie(self, devices: list, gpus: list = None):
        """Liens PCIe de tous les périphériques : Gen + largeur actuels et maximaux, pilote, NUMA, erreurs AER."""
        if not devices and "pcie" not in self._incomplete_map:
            return
        sec = self._section("PÉRIPHÉRIQUES PCIe (liens)", "#22d3ee", "pcie")
        if not devices:
            budget = self._incomplete_map.get("pcie")
            self._row(
                sec, "Collecte",
                f"Abandonnée après {budget} s (lspci bloqué ou trop lent)" if budget else "Abandonnée (délai dépassé)",
                "info-val-warn",
            )
            self._content.append(sec)
            return
        from core.system_info import group_pcie_devices, name_pcie_devices
        for d in group_pcie_devices(name_pcie_devices(devices, gpus)):
            def lk(gen, width):
                return f"Gen{gen} x{width}" if gen and width else (f"Gen{gen}" if gen else "?")
            cur, mx = lk(d.get("gen"), d.get("largeur")), lk(d.get("gen_max"), d.get("largeur_max"))
            txt = f"{cur}" + (f"  (max {mx})" if mx != cur else "") + (f"  ·  {d['pilote']}" if d.get("pilote") else "")
            if d.get("numa") is not None:
                txt += f"  ·  NUMA {d['numa']}"
            err = d.get("aer_cor", 0) + d.get("aer_nonfatal", 0) + d.get("aer_fatal", 0)
            if err:
                txt += f"  ·  AER {d.get('aer_cor', 0)}/{d.get('aer_nonfatal', 0)}/{d.get('aer_fatal', 0)}"
            reduit = str(d.get("etat", "")).startswith("réduit")
            self._row(sec, d.get("bus", "").replace("0000:", ""),
                      d.get("nom", "") + (f"  ({d['fonctions']} fonctions)" if d.get("fonctions") else ""), "info-val")
            self._row(sec, d.get("type", ""), txt, "info-val-warn" if (reduit or d.get("aer_nonfatal") or d.get("aer_fatal")) else "info-val-ok")
            if reduit:
                self._row(sec, "", d["etat"], "info-val-warn")
            if d.get("note"):
                self._row(sec, "", d["note"], "info-val")
        self._content.append(sec)

    # ── Export PDF ────────────────────────────────────────────────────────────

    def _on_export_pdf(self, _):
        if not self._data:
            return
        from exporters.export import export_pdf

        OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")
        # Aucun résultat de test : rapport « informations système » seul (pas de pages de tests ni de graphiques)
        data = self._data

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            return "Rapport créé :\n" + export_pdf([], OUTPUT_DIR, system_info_data=data)
        self.win.export_async(work, "Export PDF réussi")

    def _on_export_html(self, _):
        if not self._data:
            return
        from exporters.export import export_html

        OUTPUT_DIR = os.path.expanduser("~/BenchmarkResults")
        # Aucun résultat de test : rapport « informations système » seul (pas de pages de tests ni de graphiques)
        data = self._data

        def work():
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            return "Rapport créé :\n" + export_html([], OUTPUT_DIR, system_info_data=data)
        self.win.export_async(work, "Export HTML réussi")

    def _on_config_sensors(self, _):
        dialog = SensorsConfigDialog(self.win, on_save_cb=self._force_telemetry_refresh)
        dialog.present()
        
    def _force_telemetry_refresh(self):
        if not self._data: return
        def fetch():
            from core.telemetry_aggregator import collect_all_motherboard_telemetry, summarize_motherboard_telemetry
            m_tel = collect_all_motherboard_telemetry()
            m_sum = summarize_motherboard_telemetry(m_tel)
            self._data["telemetry"] = m_tel
            self._data["telemetry_summary"] = m_sum
            gi.require_version('GLib', '2.0')
            GLib.idle_add(self._on_scan_done, self._data)
        threading.Thread(target=fetch, daemon=True).start()


class SensorsConfigDialog(Gtk.Window):
    def __init__(self, parent_win, on_save_cb=None):
        self.on_save_cb = on_save_cb
        super().__init__(title="Configuration des Capteurs")
        self.set_transient_for(parent_win)
        self.set_modal(True)
        self.set_default_size(550, 700)
        self.set_hide_on_close(True)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_margin_top(15); box.set_margin_bottom(15)
        box.set_margin_start(15); box.set_margin_end(15)

        lbl = Gtk.Label(label="Sélectionnez les capteurs que vous souhaitez afficher/logger durant les benchmarks. Décochez ceux qui sont erronés ou inutiles.")
        lbl.set_wrap(True)
        lbl.add_css_class("bench-subtitle")
        box.append(lbl)

        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroll.set_vexpand(True)
        
        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.NONE)
        scroll.set_child(self.listbox)
        box.append(scroll)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        
        btn_check_all = Gtk.Button(label="Tout cocher")
        btn_check_all.connect("clicked", self._on_check_all)
        btn_box.append(btn_check_all)
        
        btn_uncheck_all = Gtk.Button(label="Tout décocher")
        btn_uncheck_all.connect("clicked", self._on_uncheck_all)
        btn_box.append(btn_uncheck_all)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_box.append(spacer)

        btn_save = Gtk.Button(label="Enregistrer & Fermer")
        btn_save.add_css_class("suggested-action")
        btn_save.connect("clicked", self._on_save)
        btn_box.append(btn_save)
        box.append(btn_box)

        self.set_child(box)

        self._checkboxes = {}
        self.chk_global = None

        # Remplissage de la liste en arrière-plan
        spinner = Gtk.Spinner()
        spinner.start()
        self.listbox.append(spinner)

        threading.Thread(target=self._load_sensors, daemon=True).start()

    def _load_sensors(self):
        try:
            from core.telemetry_aggregator import collect_all_motherboard_telemetry
            items = collect_all_motherboard_telemetry(filter_enabled=False)
        except Exception:
            items = []
        GLib.idle_add(self._populate_list, items)

    def _populate_list(self, items):
        # Nettoyer listbox
        while child := self.listbox.get_first_child():
            self.listbox.remove(child)

        if not items:
            lbl = Gtk.Label(label="Aucun capteur détecté sur cette carte mère.")
            lbl.set_margin_top(20)
            self.listbox.append(lbl)
            return

        from core.sensors_config import load_sensor_config
        config = load_sensor_config()
        self._checkboxes = {}

        # Grouper par catégorie
        grouped = {}
        for it in items:
            cat = it.get("category", "Autre").capitalize()
            grouped.setdefault(cat, []).append(it)

        for cat in sorted(grouped.keys()):
            cat_lbl = Gtk.Label(label=f"<b>{cat}</b>", use_markup=True)
            cat_lbl.set_halign(Gtk.Align.START)
            cat_lbl.set_margin_top(15)
            cat_lbl.set_margin_bottom(5)
            self.listbox.append(cat_lbl)

            for item in sorted(grouped[cat], key=lambda x: x.get("name", "")):
                name = item.get("name", "Inconnu")
                val = item.get("value", "0")
                unit = item.get("unit", "")
                
                row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
                row.set_margin_top(6); row.set_margin_bottom(6)
                
                chk = Gtk.CheckButton()
                chk.set_active(config.get(name, False))
                
                lbl_name = Gtk.Label(label=name)
                lbl_name.set_halign(Gtk.Align.START)
                lbl_name.set_hexpand(True)
                
                lbl_val = Gtk.Label(label=f"{val} {unit}")
                lbl_val.add_css_class("info-val-ok")
                
                row.append(chk)
                row.append(lbl_name)
                row.append(lbl_val)
                
                self.listbox.append(row)
                self._checkboxes[name] = chk

        self.listbox.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))
        
        opt_lbl = Gtk.Label(label="<b>Options Globales</b>", use_markup=True)
        opt_lbl.set_halign(Gtk.Align.START); opt_lbl.set_margin_top(15); opt_lbl.set_margin_bottom(5)
        self.listbox.append(opt_lbl)
        
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.chk_global = Gtk.CheckButton()
        self.chk_global.set_active(config.get("global_background_telemetry", False))
        lbl_global = Gtk.Label(label="Activer l'enregistrement de ces sondes en arrière-plan pendant TOUS les benchmarks (CPU, RAM, GPU, FIO)")
        lbl_global.set_halign(Gtk.Align.START); lbl_global.set_hexpand(True); lbl_global.set_wrap(True)
        row.append(self.chk_global); row.append(lbl_global)
        self.listbox.append(row)

    def _on_save(self, _):
        from core.sensors_config import save_sensor_config
        config = {}
        if hasattr(self, '_checkboxes') and self._checkboxes:
            for name, chk in self._checkboxes.items():
                config[name] = chk.get_active()
        if hasattr(self, 'chk_global') and self.chk_global is not None:
            config["global_background_telemetry"] = self.chk_global.get_active()
        save_sensor_config(config)
        self.destroy()
        if hasattr(self, 'on_save_cb') and self.on_save_cb:
            self.on_save_cb()

    def _on_check_all(self, _):
        for chk in self._checkboxes.values():
            chk.set_active(True)

    def _on_uncheck_all(self, _):
        for chk in self._checkboxes.values():
            chk.set_active(False)
