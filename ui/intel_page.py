"""
intel_page.py — Configuration d'un GPU Intel (pilote xe) pour BenchSuite : l'équivalent de la page NVIDIA (nvidia-smi).

- Télémétrie en direct (lecture sysfs / hwmon, sans privilège) : températures cœur (pkg) / VRAM / contrôleur mémoire / PCIe,
  ventilateur, puissance consommée (compteur d'énergie), limites PL1 / PL2 / TDP, fréquences, raisons de bridage, lien PCIe.
- Réglages (élévation à la demande, pkexec / sudo) : limite de puissance soutenue (PL1) et de pointe (PL2), plage de fréquences,
  retour aux valeurs d'origine. Tout passe par core/intel_control.py (garde-fous, relecture, repli xpu-smi).
- Commandes équivalentes à copier (sysfs et xpu-smi) pour appliquer ou vérifier à la main.
Les réglages ne survivent pas à un redémarrage. L'écriture n'a pas été vérifiée sur matériel réel côté développement.
"""

import threading
from typing import Dict, Optional

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from core import intel_control as ic


# Raisons de bridage exposées par le pilote xe (freq0/throttle/reason_*) → libellés lisibles
_THROTTLE_LABELS = {"pl1": "limite de puissance soutenue (PL1)", "pl2": "limite de puissance de pointe (PL2)", "pl4": "limite de courant (PL4)",
                    "thermal": "température", "prochot": "PROCHOT (surchauffe)", "ratl": "limiteur thermique (RATL)",
                    "vr_thermalert": "température des régulateurs (VR)", "vr_tdc": "courant des régulateurs (VR TDC)"}


def _fmt(v, unit="", nd=0, dflt="—"):
    return dflt if v is None else f"{v:.{nd}f}{unit}"


class IntelPage(Gtk.ScrolledWindow):
    """Page défilante : le contenu (télémétrie + réglages) dépasse la hauteur d'une fenêtre ordinaire."""

    def __init__(self, win):
        super().__init__()
        self.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.set_vexpand(True)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.box.set_margin_top(24)
        self.box.set_margin_start(28)
        self.box.set_margin_end(28)
        self.box.set_margin_bottom(24)
        self.set_child(self.box)
        self.win = win
        self.gpus = []
        self.sel: Optional[Dict] = None
        self.refresh_tag = None
        self.is_applying = False
        self._detected = False
        self._state: Dict = {}
        self._prev: Optional[Dict] = None
        self._need_fill = True            # remplir les champs de réglage au prochain relevé (1re fois / après application)
        self._filling = False             # vrai pendant le remplissage automatique des champs (ne compte pas comme une saisie)
        self._usable = False
        self._dirty = set()               # champs réellement modifiés par l'utilisateur : seuls ceux-là sont écrits
        self._gen = 0                     # génération : un relevé lancé avant « Appliquer » est ignoré à son arrivée
        self._reading = False             # un relevé est déjà en cours (pas de chevauchement)
        self._pcie: Dict = {}             # lien PCIe lu une seule fois par GPU (évite un lspci toutes les 2 s)
        self._build_ui()
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._stop_monitoring())

    # ── Construction ──────────────────────────────────────────────────────────

    def _lbl(self, text, css="bench-subtitle", halign=Gtk.Align.START):
        l = Gtk.Label(label=text, halign=halign)
        if css:
            l.add_css_class(css)
        return l

    def _build_ui(self):
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.hdr = Gtk.Label(halign=Gtk.Align.START)
        self.hdr.set_markup('<span weight="bold" size="xx-large">🔵 Configuration GPU Intel</span>')
        hdr_box.append(self.hdr)
        desc = self._lbl("Télémétrie en direct et réglages d'un GPU Intel (pilote xe) : limites de puissance PL1 / PL2, plage de fréquences, "
                         "températures, bridage. Équivalent de nvidia-smi ; les réglages ne survivent pas à un redémarrage.")
        desc.set_wrap(True)
        hdr_box.append(desc)
        self.box.append(hdr_box)

        self.no_gpu_label = Gtk.Label(halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER, vexpand=True, wrap=True, visible=False)
        self.no_gpu_label.set_markup('<span foreground="#f38ba8" size="large"><b>Aucun GPU Intel détecté.</b></span>\n\n'
                                     'Il faut un GPU Intel (Arc, Arc Pro, Data Center GPU) avec le pilote <b>xe</b> ou <b>i915</b> chargé.')
        self.box.append(self.no_gpu_label)

        self.selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.selector_box.add_css_class("params-panel")
        self.selector_box.append(self._lbl("Sélectionner le GPU :", "param-label"))
        self.gpu_selector = Gtk.ComboBoxText()
        self.gpu_selector.connect("changed", self._on_gpu_changed)
        self.selector_box.append(self.gpu_selector)
        self.det_label = self._lbl("Détection des GPU…")
        self.selector_box.append(self.det_label)
        self.box.append(self.selector_box)

        # Télémétrie
        self.info_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.info_panel.add_css_class("score-box")
        self.info_panel.append(self._lbl("TÉLÉMÉTRIE LIVE", "params-title"))
        grid = Gtk.Grid(row_spacing=8, column_spacing=24)
        self.v = {}

        def row(r, c, title, key):
            grid.attach(self._lbl(title, None), c * 2, r, 1, 1)
            lab = Gtk.Label(label="—", halign=Gtk.Align.START, wrap=True, selectable=True)
            lab.add_css_class("metric-value")
            grid.attach(lab, c * 2 + 1, r, 1, 1)
            self.v[key] = lab

        row(0, 0, "Température cœur (pkg) :", "temp");        row(0, 1, "Consommation :", "power")
        row(1, 0, "Température VRAM :", "vram");              row(1, 1, "Limite soutenue PL1 :", "pl1")
        row(2, 0, "Contrôleur mémoire (secondaire) :", "mctrl"); row(2, 1, "Limite de pointe PL2 :", "pl2")
        row(3, 0, "Interface PCIe (secondaire) :", "pcie");   row(3, 1, "TDP par défaut :", "rated")
        row(4, 0, "Canaux VRAM (max) :", "vramch");           row(4, 1, "Fenêtres PL1 / PL2 :", "tau")
        row(5, 0, "Ventilateur :", "fan");                    row(5, 1, "Limite critique I1 :", "crit")
        row(6, 0, "Fréquence (active / demandée) :", "freq"); row(6, 1, "Plage de fréquences min–max :", "frange")
        row(7, 0, "Bridage (throttling) :", "throttle");      row(7, 1, "Lien PCIe :", "pcie_link")
        row(8, 0, "Adresse PCI :", "bdf");                    row(8, 1, "xpu-smi :", "xpu")
        self.info_panel.append(grid)
        bars = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        bars.set_margin_top(10)
        bars.append(self._lbl("Puissance / limite soutenue (PL1) :"))
        self.bar_power = Gtk.ProgressBar(show_text=True)
        bars.append(self.bar_power)
        bars.append(self._lbl("Température cœur / limite critique :"))
        self.bar_temp = Gtk.ProgressBar(show_text=True)
        bars.append(self.bar_temp)
        self.info_panel.append(bars)
        self.box.append(self.info_panel)

        # Réglages
        self.settings_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        self.settings_panel.add_css_class("params-panel")
        self.settings_panel.append(self._lbl("AJUSTEMENT DES PARAMÈTRES (REQUIERT ÉLÉVATION)", "params-title"))

        def spin_row(title, attr, key):
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
            t = self._lbl(title, "param-label")
            t.set_size_request(260, -1)
            box.append(t)
            sp = Gtk.SpinButton()
            sp.set_numeric(True)
            sp.set_digits(0)
            sp.connect("value-changed", lambda *_, k=key: self._on_spin(k))
            setattr(self, attr, sp)
            box.append(sp)
            rng = self._lbl("")
            setattr(self, attr + "_rng", rng)
            box.append(rng)
            self.settings_panel.append(box)
            return box

        self.row_pl1 = spin_row("Limite soutenue PL1 (W) :", "pl1_spin", "pl1")
        self.row_pl2 = spin_row("Limite de pointe PL2 (W) :", "pl2_spin", "pl2")
        self.row_fmin = spin_row("Fréquence minimale (MHz) :", "fmin_spin", "freq")
        self.row_fmax = spin_row("Fréquence maximale (MHz) :", "fmax_spin", "freq")
        self.no_ctl_label = self._lbl("")
        self.no_ctl_label.set_wrap(True)
        self.settings_panel.append(self.no_ctl_label)

        btns = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.btn_apply = Gtk.Button(label="✅ Appliquer")
        self.btn_apply.add_css_class("btn-run")
        self.btn_apply.connect("clicked", self._on_apply)
        btns.append(self.btn_apply)
        self.btn_reset = Gtk.Button(label="↺ Valeurs d'origine")
        self.btn_reset.connect("clicked", self._on_reset)
        btns.append(self.btn_reset)
        self.spinner = Gtk.Spinner(visible=False)
        btns.append(self.spinner)
        self.status_label = Gtk.Label(halign=Gtk.Align.START, wrap=True, hexpand=True)
        btns.append(self.status_label)
        self.settings_panel.append(btns)

        self.settings_panel.append(self._lbl("Commandes équivalentes (à copier pour appliquer ou vérifier à la main) :"))
        self.cmd_label = Gtk.Label(halign=Gtk.Align.START, selectable=True, wrap=True, xalign=0)
        self.cmd_label.add_css_class("cmd-text")
        self.settings_panel.append(self.cmd_label)
        note = self._lbl("Les réglages sont perdus au redémarrage. Le retour aux valeurs d'origine remet PL1 et PL2 aux valeurs observées à la "
                         "première ouverture de cette page (PL1 = TDP par défaut si elles sont inconnues), fréquences = minimum / maximum matériels. Le support dépend du modèle et du noyau (≥ 6.5 pour PL1, "
                         "≥ 6.15 pour PL2) ; non vérifié sur toutes les cartes.")
        note.set_wrap(True)
        self.settings_panel.append(note)
        self.box.append(self.settings_panel)

        self.info_panel.set_visible(False)
        self.settings_panel.set_visible(False)

    # ── Détection / surveillance ──────────────────────────────────────────────

    def _on_map(self):
        if not self._detected:
            self._detected = True
            threading.Thread(target=self._detect_worker, daemon=True).start()
        else:
            self._start_monitoring()

    def _detect_worker(self):
        gpus = ic.find_intel_gpus()
        GLib.idle_add(self._after_detect, gpus)

    def _after_detect(self, gpus):
        self.gpus = gpus
        self.det_label.set_visible(False)
        self.gpu_selector.remove_all()
        if not gpus:
            self.selector_box.set_visible(False)
            self.no_gpu_label.set_visible(True)
            return False
        for i, g in enumerate(gpus):
            self.gpu_selector.append(str(i), f"[{g['idx']}] {g['name']} — {g['bdf']}")
        self.info_panel.set_visible(True)
        self.settings_panel.set_visible(True)
        self.gpu_selector.set_active(0)       # déclenche _on_gpu_changed
        if self.get_mapped():
            self._start_monitoring()
        return False

    def _on_gpu_changed(self, _=None):
        i = self.gpu_selector.get_active_id()
        if i is None or not self.gpus:
            return
        self.sel = self.gpus[int(i)]
        self._need_fill, self._prev, self._pcie = True, None, {}
        self._dirty.clear()                  # aucune saisie de l'ancien GPU ne doit être appliquée au nouveau
        self._state = {}
        self._gen += 1
        self.btn_apply.set_sensitive(False)  # réactivés par _fill_controls au premier relevé du nouveau GPU
        self.btn_reset.set_sensitive(False)
        self.hdr.set_markup(f'<span weight="bold" size="xx-large">🔵 {GLib.markup_escape_text(self.sel["name"])}</span>')
        self._refresh()

    def _start_monitoring(self):
        if self.refresh_tag is None and self.sel is not None:
            self.refresh_tag = GLib.timeout_add_seconds(2, self._refresh)

    def _stop_monitoring(self):
        if self.refresh_tag is not None:
            GLib.source_remove(self.refresh_tag)
            self.refresh_tag = None

    def _refresh(self):
        if self.sel is None:
            return True
        if self.is_applying or self._reading:
            return True
        bdf, gen, want_pcie = self.sel["bdf"], self._gen, not self._pcie
        self._reading = True

        def worker():
            try:
                st = ic.read_state(bdf, with_pcie=want_pcie)
            finally:
                GLib.idle_add(self._reading_done)
            GLib.idle_add(self._update_ui, st, gen)
        threading.Thread(target=worker, daemon=True).start()
        return True

    def _reading_done(self):
        self._reading = False
        return False

    # ── Affichage ─────────────────────────────────────────────────────────────

    def _update_ui(self, st: Dict, gen: int = None):
        if self.sel is None or st.get("bdf") != self.sel["bdf"] or self.is_applying or (gen is not None and gen != self._gen):
            return False
        if st.get("pcie"):
            self._pcie = st["pcie"]
        else:
            st["pcie"] = self._pcie
        pw = ic.power_from_energy(self._prev, st)
        self._prev, self._state = st, st
        if self._need_fill:
            ic.remember_defaults(st)
        T = st.get("temps", {})
        v = self.v

        def temp_txt(ent, col="#f38ba8"):
            if not ent:
                return '<span foreground="#6c7086">non disponible</span>'
            extra = " · ".join(f"{n} {ent[k]:.0f} °C" for k, n in (("max", "high"), ("crit", "crit"), ("emerg", "arrêt")) if k in ent)
            return f'<span foreground="{col}" weight="bold">{ent["val"]:.0f} °C</span>' + (f' <span size="small" foreground="#9399b2">({extra})</span>' if extra else "")

        v["temp"].set_markup(temp_txt(T.get("pkg")))
        v["vram"].set_markup(temp_txt(T.get("vram"), "#fab387"))
        v["mctrl"].set_markup(temp_txt(T.get("mctrl"), "#cdd6f4"))
        v["pcie"].set_markup(temp_txt(T.get("pcie"), "#cdd6f4"))
        v["vramch"].set_text(_fmt(st.get("vram_ch_max_c"), " °C"))
        fans = st.get("fan_rpm")
        v["fan"].set_text(" / ".join(f"{f:.0f} tr/min" for f in fans) if fans else "non disponible")
        v["power"].set_markup(f'<span foreground="#fab387" weight="bold">{pw:.0f} W</span> <span size="small" foreground="#9399b2">(carte, moyenne sur 2 s)</span>' if pw is not None
                              else '<span foreground="#6c7086">mesure en cours…</span>')
        for key, k in (("pl1", "pl1_w"), ("pl2", "pl2_w"), ("rated", "rated_w"), ("crit", "crit_w")):
            val = st.get(k)
            v[key].set_text("non exposé" if val is None else ("désactivée (0)" if val == 0 else _fmt(val, " W")))
        v["tau"].set_text(f"{_fmt(st.get('tau1_ms'), ' ms')} / {_fmt(st.get('tau2_ms'), ' ms')}")
        f = st.get("freq") or []
        if f:
            v["freq"].set_text("  |  ".join(f"{e['tile_gt']} : {_fmt(e.get('act'), ' MHz')} / {_fmt(e.get('cur'), ' MHz')}" for e in f))
            v["frange"].set_text("  |  ".join(f"{_fmt(e.get('min'))}–{_fmt(e.get('max'))} MHz (matériel {_fmt(e.get('rpn'))}–{_fmt(e.get('rp0'))})" for e in f))
            reasons = sorted({r for e in f for r in e.get("throttle_reasons", [])})
            if any("throttle_status" in e for e in f):
                if reasons:
                    txt = ", ".join(_THROTTLE_LABELS.get(r, r) for r in reasons)
                    v["throttle"].set_markup(f'<span foreground="#f38ba8" weight="bold">⚠️ {GLib.markup_escape_text(txt)}</span>')
                else:
                    v["throttle"].set_markup('<span foreground="#a6e3a1" weight="bold">Aucun (normal)</span>')
            else:
                v["throttle"].set_text("non exposé par le pilote")
        else:
            for k in ("freq", "frange"):
                v[k].set_text("non exposé par le pilote")
            v["throttle"].set_text("non exposé par le pilote")
        p = st.get("pcie") or {}
        link = f"Gen {p.get('pcie_gen', '?')} x{p.get('pcie_largeur', '?')}" if p.get("pcie_gen") else "—"
        v["pcie_link"].set_text(link + (" (pont amont de la carte)" if p.get("note") else ""))
        v["bdf"].set_text(self.sel["bdf"])
        v["xpu"].set_text("disponible" if ic.xpu_smi_available() else "absent (facultatif)")
        # barres
        if pw is not None and st.get("pl1_w"):
            self.bar_power.set_fraction(max(0.0, min(1.0, pw / st["pl1_w"])))
            self.bar_power.set_text(f"{pw:.0f} W / {st['pl1_w']:.0f} W")
        pk = T.get("pkg")
        if pk and pk.get("crit"):
            self.bar_temp.set_fraction(max(0.0, min(1.0, pk["val"] / pk["crit"])))
            self.bar_temp.set_text(f"{pk['val']:.0f} °C / {pk['crit']:.0f} °C")
        if self._need_fill:
            self._fill_controls(st)
            self._need_fill = False
        return False

    def _fill_controls(self, st: Dict):
        """Remplit les champs de réglage avec les valeurs actuelles et leurs plages ; masque ceux que le pilote n'expose pas."""
        from core.safety import system_changes_allowed, MSG_PRESERVATION
        self._filling = True
        try:
            self._fill_controls_inner(st)
        finally:
            self._filling = False
        self._dirty.clear()
        allowed = system_changes_allowed()
        self.btn_apply.set_sensitive(allowed and self._usable)
        self.btn_reset.set_sensitive(allowed and self._usable)
        if not allowed:
            self.status_label.set_markup('<span foreground="#f9e2af">' + GLib.markup_escape_text(MSG_PRESERVATION) + '</span>')
        self._update_commands()

    def _fill_controls_inner(self, st: Dict):
        """Corps de _fill_controls (appelé avec le drapeau « remplissage » levé)."""
        rated = st.get("rated_w")
        has_pl1, has_pl2 = st.get("pl1_w") is not None, st.get("pl2_w") is not None
        fr = (st.get("freq") or [None])[0]
        has_fr = bool(fr and fr.get("min") is not None and fr.get("max") is not None)
        if has_pl1:
            lo = (rated or st["pl1_w"]) * ic.MIN_PL_RATIO
            hi = rated or st["pl1_w"]
            hi = max(hi, st["pl1_w"])
            lo = min(lo, hi)
            self.pl1_spin.set_range(lo, hi)
            self.pl1_spin.set_increments(1, 10)
            self.pl1_spin.set_value(st["pl1_w"])
            self.pl1_spin_rng.set_text(f"(plage : {lo:.0f}–{hi:.0f} W ; TDP par défaut {_fmt(rated, ' W')})")
        if has_pl2:
            lo = (rated or st.get("pl1_w") or st["pl2_w"]) * ic.MIN_PL_RATIO
            hi = max((rated or st["pl2_w"]) * ic.MAX_PL2_RATIO, st["pl2_w"])
            self.pl2_spin.set_range(min(lo, hi), hi)
            self.pl2_spin.set_increments(1, 10)
            self.pl2_spin.set_value(st["pl2_w"])
            self.pl2_spin_rng.set_text(f"(plage : {lo:.0f}–{hi:.0f} W ; doit rester ≥ PL1)")
        if has_fr:
            lo, hi = fr.get("rpn") or 100, fr.get("rp0") or fr["max"]
            lo, hi = min(lo, fr["min"]), max(hi, fr["max"])
            for sp, val in ((self.fmin_spin, fr["min"]), (self.fmax_spin, fr["max"])):
                sp.set_range(lo, hi)
                sp.set_increments(50, 100)
                sp.set_value(val)
            self.fmin_spin_rng.set_text(f"(matériel : {lo:.0f}–{hi:.0f} MHz)")
            self.fmax_spin_rng.set_text("")
        self.row_pl1.set_visible(has_pl1)
        self.row_pl2.set_visible(has_pl2)
        self.row_fmin.set_visible(has_fr)
        self.row_fmax.set_visible(has_fr)
        usable = has_pl1 or has_pl2 or has_fr
        self._usable = usable
        self.no_ctl_label.set_markup("" if usable else '<span foreground="#f9e2af">Aucun réglage exposé par ce pilote / noyau pour ce GPU '
                                                       '(noyau ≥ 6.5 et pilote xe requis).</span>')

    def _on_spin(self, key: str):
        """Une saisie de l'utilisateur marque le champ comme modifié (le remplissage automatique est ignoré)."""
        if self._filling:
            return
        self._dirty.add(key)
        self._update_commands()

    def _wanted(self):
        """Valeurs (pl1, pl2, fmin, fmax) demandées : uniquement les champs touchés par l'utilisateur (fréquences : les deux ensemble)."""
        pl1 = float(self.pl1_spin.get_value()) if "pl1" in self._dirty and self.row_pl1.get_visible() else None
        pl2 = float(self.pl2_spin.get_value()) if "pl2" in self._dirty and self.row_pl2.get_visible() else None
        fmin = fmax = None
        if ("freq" in self._dirty) and self.row_fmin.get_visible():
            fmin, fmax = float(self.fmin_spin.get_value()), float(self.fmax_spin.get_value())
        return pl1, pl2, fmin, fmax

    def _update_commands(self):
        if not self._state:
            return
        pl1, pl2, fmin, fmax = self._wanted()
        lines = ic.equivalent_commands(self._state, pl1=pl1, pl2=pl2, fmin=fmin, fmax=fmax,
                                       xpu_id=(self.sel or {}).get("xpu_id", ""))
        self.cmd_label.set_text("\n".join(lines) if lines else "—")

    # ── Actions ───────────────────────────────────────────────────────────────

    def _busy(self, on: bool, msg: str = ""):
        self.is_applying = on
        for w in (self.btn_apply, self.btn_reset, self.gpu_selector):
            w.set_sensitive(not on)
        self.spinner.set_visible(on)
        self.spinner.start() if on else self.spinner.stop()
        if msg:
            self.status_label.set_markup(msg)

    def _finish(self, ok: bool, msg: str):
        self._busy(False)
        self._gen += 1
        col = "#a6e3a1" if ok else "#f38ba8"
        self.status_label.set_markup(f'<span foreground="{col}"><b>{GLib.markup_escape_text(msg)}</b></span>')
        self._need_fill, self._prev = True, None
        self._refresh()
        return False

    def _on_apply(self, _):
        if self.is_applying or self.sel is None:
            return
        pl1, pl2, fmin, fmax = self._wanted()
        if pl1 is None and pl2 is None and fmin is None:
            self.status_label.set_markup('<span foreground="#f9e2af">Aucun réglage modifié : rien à appliquer.</span>')
            return
        if fmin is not None and fmin > fmax:
            self.status_label.set_markup('<span foreground="#f38ba8">Fréquence min supérieure à la fréquence max.</span>')
            return
        st = self._state or {}
        eff1 = pl1 if pl1 is not None else st.get("pl1_w")
        eff2 = pl2 if pl2 is not None else st.get("pl2_w")
        if eff1 is not None and eff2 is not None and eff2 > 0 and eff1 > eff2:
            self.status_label.set_markup('<span foreground="#f38ba8">PL1 (soutenu) ne peut pas dépasser PL2 (rafale).</span>')
            return
        bdf, xid = self.sel["bdf"], self.sel.get("xpu_id", "")
        self._gen += 1                       # un relevé en cours avant l'application sera ignoré
        self._busy(True, '<span foreground="#fab387">Application en cours… validez l\'invite d\'authentification si elle apparaît.</span>')

        def run():
            try:
                ok, m = ic.apply_changes(bdf, pl1=pl1, pl2=pl2, fmin=fmin, fmax=fmax, xpu_id=xid)
            except Exception as e:           # jamais de traceback utilisateur
                ok, m = False, f"erreur inattendue : {e}"
            GLib.idle_add(self._finish, ok, m)
        threading.Thread(target=run, daemon=True).start()

    def _on_reset(self, _):
        if self.is_applying or self.sel is None:
            return
        bdf, xid = self.sel["bdf"], self.sel.get("xpu_id", "")
        self._gen += 1
        self._busy(True, '<span foreground="#fab387">Retour aux valeurs d\'origine… validez l\'invite d\'authentification si elle apparaît.</span>')

        def run():
            try:
                ok, m = ic.reset_defaults(bdf, xpu_id=xid)
            except Exception as e:           # jamais de page bloquée ni de traceback utilisateur
                ok, m = False, f"erreur inattendue : {e}"
            GLib.idle_add(self._finish, ok, m)
        threading.Thread(target=run, daemon=True).start()

    def destroy(self):
        self._stop_monitoring()
        super().destroy()
