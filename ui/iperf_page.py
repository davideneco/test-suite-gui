"""Page « Serveur iperf3 » : transforme cette machine en serveur de test réseau pour qu'une autre machine lance le test client.

L'autre machine (ou un test BenchSuite « Réseau iperf3 ») se connecte à l'adresse IP affichée ici. Aucun protocole entre deux
BenchSuite : c'est un simple `iperf3 -s`. Démarrage / arrêt dans un thread (jamais de blocage de l'interface).
"""
import threading
from typing import List

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib

from core import iperf_server as isrv


class IperfServerPage(Gtk.ScrolledWindow):
    """Contrôle du serveur iperf3 local : IP à donner au client, port, démarrage / arrêt, journal des connexions."""

    def __init__(self, win):
        super().__init__()
        self.win = win
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)
        self.timer = None
        self._busy = False
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        for m in ("top", "bottom"):
            getattr(self.box, f"set_margin_{m}")(24)
        for m in ("start", "end"):
            getattr(self.box, f"set_margin_{m}")(28)
        self.set_child(self.box)
        self._build_ui()
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._on_unmap())

    # ── Construction ──────────────────────────────────────────────────────────

    def _label(self, text: str, css: str = "bench-subtitle", selectable: bool = False) -> Gtk.Label:
        l = Gtk.Label(label=text, halign=Gtk.Align.START, xalign=0, wrap=True, selectable=selectable)
        l.add_css_class(css)
        return l

    def _panel(self, title: str) -> Gtk.Box:
        p = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        p.add_css_class("params-panel")
        p.append(self._label(title.upper(), "params-title"))
        self.box.append(p)
        return p

    def _cmd_row(self, parent: Gtk.Box, caption: str, cmd: str) -> None:
        """Ligne « légende + commande copiable » (sélectionnable à la souris)."""
        parent.append(self._label(caption))
        c = Gtk.Label(label=cmd, halign=Gtk.Align.START, xalign=0, wrap=True, selectable=True)
        c.add_css_class("cmd-text")
        parent.append(c)

    def _build_ui(self):
        t = Gtk.Label(halign=Gtk.Align.START)
        t.set_markup('<span size="xx-large" weight="bold">🌐 Serveur iperf3</span>')
        self.box.append(t)
        self.box.append(self._label("Fait de cette machine le serveur d'un test réseau : l'autre machine (branchée sur le même switch) "
                                    "lance le test « Réseau iperf3 TCP / UDP » de BenchSuite, ou `iperf3 -c`, vers l'adresse IP ci-dessous. "
                                    "Un seul client à la fois."))

        # ── Contrôle ──
        ctl = self._panel("Serveur")
        row = Gtk.Box(spacing=10)
        row.append(Gtk.Label(label="Port :"))
        self.port_spin = Gtk.SpinButton.new_with_range(1, 65535, 1)
        self.port_spin.set_value(isrv.DEFAULT_PORT)
        row.append(self.port_spin)
        self.btn_start = Gtk.Button(label="▶ Démarrer le serveur")
        self.btn_start.add_css_class("btn-run")
        self.btn_start.connect("clicked", self._on_start)
        self.btn_stop = Gtk.Button(label="⏹ Arrêter")
        self.btn_stop.add_css_class("btn-stop")
        self.btn_stop.connect("clicked", self._on_stop)
        self.spinner = Gtk.Spinner()
        row.append(self.btn_start); row.append(self.btn_stop); row.append(self.spinner)
        ctl.append(row)
        self.status = Gtk.Label(halign=Gtk.Align.START, xalign=0, wrap=True, use_markup=True)
        ctl.append(self.status)

        # ── Adresses à donner au client ──
        self.addr_panel = self._panel("Adresse à donner au client")
        self.addr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        self.addr_panel.append(self.addr_box)
        self.cmd_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.addr_panel.append(self.cmd_box)

        # ── Ports Ethernet physiques (même sans adresse ni câble) ──
        self.ports_panel = self._panel("Ports Ethernet de cette machine")
        hd = Gtk.Box(spacing=10)
        hd.append(self._label("Tous les ports réseau physiques, y compris ceux sans adresse IP : pour relier deux machines en direct, "
                              "donnez une adresse à un port de chaque côté (même réseau, ex. .1 et .2)."))
        hd.get_first_child().set_hexpand(True)
        self.btn_refresh = Gtk.Button(label="↻ Actualiser")
        self.btn_refresh.connect("clicked", lambda *_: self._refresh_addresses())
        hd.append(self.btn_refresh)
        self.ports_panel.append(hd)
        # Les deux valeurs qui changent d'un PC à l'autre : le réseau de test (identique des deux côtés) et le numéro de CE PC (différent)
        self._ports: List[dict] = []
        cfg = Gtk.Box(spacing=10)
        cfg.append(Gtk.Label(label="Réseau de test :"))
        self.net_entry = Gtk.Entry(); self.net_entry.set_width_chars(13); self.net_entry.set_text(isrv.TEST_NET)
        self.net_entry.set_tooltip_text("3 premiers nombres de l'adresse (réseau /24), IDENTIQUES sur les deux machines reliées par ce câble")
        cfg.append(self.net_entry)
        cfg.append(Gtk.Label(label="   N° de CE PC :"))
        self.num_spin = Gtk.SpinButton.new_with_range(1, 254, 1); self.num_spin.set_value(1)
        self.num_spin.set_tooltip_text("Dernier nombre de l'adresse de CE PC : 1 sur un PC, 2 sur l'autre (jamais le même des deux côtés)")
        cfg.append(self.num_spin)
        cfg.append(self._label("  ← à changer d'un PC à l'autre : 1 sur l'un, 2 sur l'autre ; le réseau reste le même."))
        self.ports_panel.append(cfg)
        self.net_entry.connect("changed", lambda *_: self._show_ports(self._ports))
        self.num_spin.connect("value-changed", lambda *_: self._show_ports(self._ports))
        self.ports_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.ports_panel.append(self.ports_box)

        # ── Journal ──
        jp = self._panel("Journal du serveur")
        self.conn_label = self._label("Aucune connexion pour l'instant.")
        jp.append(self.conn_label)
        sc = Gtk.ScrolledWindow(); sc.set_min_content_height(140); sc.set_max_content_height(220)
        self.log_view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True)
        self.log_view.add_css_class("log-view")
        sc.set_child(self.log_view)
        jp.append(sc)

        # ── Aide : installation + pare-feu (Ubuntu / Debian) ──
        self.help_panel = self._panel("Si le client n'arrive pas à se connecter")
        self._fill_help()

    def _fill_help(self):
        p = self.help_panel
        while p.get_last_child() is not None and p.get_last_child() is not p.get_first_child():
            p.remove(p.get_last_child())
        if not isrv.iperf3_available():
            p.append(self._label("iperf3 n'est pas installé sur cette machine :"))
            for lab, cmd in isrv.INSTALL_CMDS:
                self._cmd_row(p, lab, cmd)
        port = int(self.port_spin.get_value())
        p.append(self._label(f"Ouvrir le port {port} (TCP et UDP) dans le pare-feu si besoin :"))
        for lab, cmd in isrv.firewall_commands(port):
            self._cmd_row(p, lab, cmd)
        p.append(self._label("Vérifier aussi que les deux machines sont dans le même réseau (même switch / même sous-réseau) : "
                             "`ping <IP du serveur>` depuis le client."))

    # ── Cycle de vie ──────────────────────────────────────────────────────────

    def _on_map(self):
        self._refresh_addresses()
        self._fill_help()
        self._tick()
        if self.timer is None:
            self.timer = GLib.timeout_add_seconds(1, self._tick)

    def _on_unmap(self):
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None

    def _refresh_addresses(self):
        """Relit les adresses IP (thread) puis met à jour la liste : jamais de lecture réseau sur le thread GTK."""
        def work():
            addrs = isrv.local_addresses()
            ports = isrv.ethernet_ports()
            GLib.idle_add(self._show_addresses, addrs)
            GLib.idle_add(self._show_ports, ports)
        threading.Thread(target=work, daemon=True).start()

    def _show_ports(self, ports: List[dict]):
        """Un bloc par port Ethernet physique : état du lien, vitesse, adresses ; pour un port sans adresse, les commandes à copier."""
        if ports is not self._ports:
            first = not self._ports
            self._ports = ports
            if first and ports:                                   # 1er affichage : réseau de test non utilisé ici
                self.net_entry.set_text(isrv.suggest_test_network(ports))
        child = self.ports_box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self.ports_box.remove(child); child = nxt
        if not ports:
            self.ports_box.append(self._label("Aucun port Ethernet physique détecté."))
            return False
        net = self.net_entry.get_text().strip()
        if not isrv.valid_net(net):
            self.ports_box.append(self._label("Réseau de test invalide : trois nombres séparés par des points (ex. 192.168.100)."))
            return False
        me = int(self.num_spin.get_value()); other = isrv.other_number(me)
        for p in ports:
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            if p["link"]:
                state = f'<span foreground="#a6e3a1">● lien actif</span>' + (f' · {p["speed_mbps"]} Mb/s' if p["speed_mbps"] else "")
            elif p["link"] is False:
                state = '<span foreground="#888888">○ pas de câble</span>'
            else:
                state = '<span foreground="#888888">○ pas de câble ou port désactivé</span>'
            t = Gtk.Label(halign=Gtk.Align.START, xalign=0, use_markup=True, selectable=True)
            t.set_markup(f'<span size="large" weight="bold">{GLib.markup_escape_text(p["iface"])}</span>   {state}'
                         f'   <span foreground="#888888" size="small">{GLib.markup_escape_text(" · ".join(x for x in (p["driver"], p["mac"], p["pci"]) if x))}</span>')
            box.append(t)
            if p["ipv4"]:
                ips = ", ".join(f'{a["ip"]}/{a["prefix"]}' if a.get("prefix") else a["ip"] for a in p["ipv4"])
                l = Gtk.Label(halign=Gtk.Align.START, xalign=0, use_markup=True, selectable=True)
                l.set_markup(f'Adresse IPv4 : <span foreground="#ff8c33" weight="bold">{GLib.markup_escape_text(ips)}</span>')
                box.append(l)
            elif p["usb"]:
                box.append(self._label("Port vu comme périphérique USB : souvent l'interface « hôte » du BMC (Redfish), pas un port à câbler."))
            else:
                box.append(self._label("Aucune adresse IPv4." + ("" if p["link"] else " Branchez le câble, puis :")))
                for lab, cmd in isrv.port_commands(p["iface"], net, me):
                    self._cmd_row(box, lab + " :", cmd)
                box.append(self._label(f"Sur l'autre PC : même réseau ({net}), N° {other}, sur le port relié par le câble. Puis, depuis l'un des PC : "
                                       f"`ping {net}.<N° de l'autre PC>`. Cliquez ensuite sur « ↻ Actualiser » : l'adresse apparaît ci-dessus et dans le choix « Port d'envoi » des tests."))
            self.ports_box.append(box)
        return False

    def _show_addresses(self, addrs: List[dict]):
        child = self.addr_box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self.addr_box.remove(child); child = nxt
        child = self.cmd_box.get_first_child()
        while child:
            nxt = child.get_next_sibling(); self.cmd_box.remove(child); child = nxt
        if not addrs:
            self.addr_box.append(self._label("Aucune adresse IPv4 détectée : branchez le câble réseau et configurez l'adresse de cette machine."))
            return False
        port = int(self.port_spin.get_value())
        for a in addrs:
            spd = f" · {a['speed_mbps']} Mb/s" if a.get("speed_mbps") else ""
            state = "" if a.get("up") else " · lien inactif"
            l = Gtk.Label(halign=Gtk.Align.START, xalign=0, selectable=True, use_markup=True)
            l.set_markup(f'<span size="large" weight="bold" foreground="#ff8c33">{GLib.markup_escape_text(a["ip"])}</span>'
                         f'<span foreground="#888888">   {GLib.markup_escape_text(a["iface"])}{spd}{state}</span>')
            self.addr_box.append(l)
        self._cmd_row(self.cmd_box, "Commande à lancer sur l'autre machine (hors BenchSuite), à adapter — remplacez par l'adresse du lien à tester :",
                      isrv.client_command_generic(port))
        for a in addrs:
            self._cmd_row(self.cmd_box, f"Pour {a['iface']} :", isrv.client_command(a["ip"], port))
        return False

    def _tick(self):
        """Mise à jour périodique (1 s) de l'état, du compteur de connexions et du journal."""
        run = isrv.SERVER.is_running()
        self.btn_start.set_sensitive(not run and not self._busy)
        self.btn_stop.set_sensitive(run and not self._busy)
        self.port_spin.set_sensitive(not run and not self._busy)
        if run:
            up = isrv.SERVER.uptime_s()
            self.status.set_markup(f'<span foreground="#a6e3a1"><b>● Serveur actif</b></span> sur le port {isrv.SERVER.port} '
                                   f'— depuis {up // 60} min {up % 60:02d} s')
        elif not self._busy and not self.status.get_text().startswith(("✖", "iperf3", "le port", "port")):
            self.status.set_markup('<span foreground="#888888">○ Serveur arrêté</span>')
        n = isrv.SERVER.connections
        self.conn_label.set_text(f"{n} connexion(s) reçue(s) — dernier client : {isrv.SERVER.last_client}" if n else "Aucune connexion pour l'instant.")
        buf = self.log_view.get_buffer()
        text = "\n".join(isrv.SERVER.log)
        if buf.get_char_count() != len(text):
            buf.set_text(text)
            it = buf.get_end_iter()
            self.log_view.scroll_to_iter(it, 0.0, False, 0.0, 0.0)
        return True

    # ── Actions ───────────────────────────────────────────────────────────────

    def _set_busy(self, on: bool, msg: str = ""):
        self._busy = on
        self.spinner.set_visible(on)
        self.spinner.start() if on else self.spinner.stop()
        if msg:
            self.status.set_markup(msg)
        self._tick()
        return False

    def _on_start(self, _):
        if self._busy:
            return
        port = int(self.port_spin.get_value())
        self._set_busy(True, "Démarrage…")

        def work():
            ok, msg = isrv.SERVER.start(port)
            def done():
                self._set_busy(False)
                if not ok:
                    self.status.set_markup(f'<span foreground="#f38ba8"><b>✖ {GLib.markup_escape_text(msg)}</b></span>')
                self._refresh_addresses(); self._fill_help()
                return False
            GLib.idle_add(done)
        threading.Thread(target=work, daemon=True).start()

    def _on_stop(self, _):
        if self._busy:
            return
        self._set_busy(True, "Arrêt…")

        def work():
            isrv.SERVER.stop()
            GLib.idle_add(lambda: (self._set_busy(False, '<span foreground="#888888">○ Serveur arrêté</span>'), False)[1])
        threading.Thread(target=work, daemon=True).start()

    def destroy(self):
        self._on_unmap()
        super().destroy()
