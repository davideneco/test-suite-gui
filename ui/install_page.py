"""
install_page.py — Page « Outils & dépendances » : ce qu'il faut installer, comment, et où trouver la documentation.

Lecture seule : BenchSuite n'installe jamais rien. Chaque outil du catalogue (core/tools_catalog.py) est détecté en
arrière-plan ; ceux qui manquent affichent leurs commandes d'installation (bouton Copier), leurs mises en garde et les liens
officiels. « Guide adapté à cette machine » appelle core/install_advisor (lecture seule) pour des commandes tenant compte
de l'existant (CUDA déjà présent, dépôt NVIDIA déjà configuré, snap à éviter…).
"""

import threading

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Pango

from core import tools_catalog as tc

_NEED_COLOR = {tc.REQUIS: "#f38ba8", tc.RECOMMANDE: "#f9e2af", tc.OPTIONNEL: "#8c8c8c"}


def _wrap_label(text: str, css: str = "", selectable: bool = False) -> Gtk.Label:
    lab = Gtk.Label(label=text)
    lab.set_wrap(True); lab.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    lab.set_xalign(0); lab.set_halign(Gtk.Align.START); lab.set_hexpand(True)
    lab.set_selectable(selectable)
    if css:
        lab.add_css_class(css)
    return lab


class InstallPage(Gtk.Box):
    """Catalogue des outils : état détecté, commandes à copier, liens officiels, filtre Tous / Manquants / Installés."""

    def __init__(self):
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        self._results: dict = {}
        self._cards: list = []          # [(carte, présent, groupe)]
        self._filter = "all"
        self._busy = False

        # ── En-tête fixe : titre, résumé, filtres, actions ──
        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        header.add_css_class("page-header")
        t = Gtk.Label()
        t.set_markup('<span weight="bold" size="x-large" foreground="#f97316">Outils &amp; dépendances</span>')
        t.set_halign(Gtk.Align.START)
        header.append(t)
        sub = _wrap_label("BenchSuite n'installe rien tout seul : copiez les commandes ci-dessous. Les outils déjà présents ne sont pas touchés, "
                          "et un outil absent désactive seulement le test qui en a besoin.", "bench-subtitle")
        header.append(sub)

        bar = Gtk.Box(spacing=8)
        self._summary = Gtk.Label(label="détection en cours…")
        self._summary.set_halign(Gtk.Align.START)
        self._summary.set_hexpand(True)
        bar.append(self._summary)
        self._spinner = Gtk.Spinner()
        bar.append(self._spinner)
        header.append(bar)

        tools = Gtk.Box(spacing=8)
        first = None
        self._filter_btns = {}
        for key, label in (("all", "Tous"), ("missing", "Manquants"), ("ok", "Installés")):
            b = Gtk.ToggleButton(label=label)
            if first is None:
                first = b
                b.set_active(True)
            else:
                b.set_group(first)
            b.connect("toggled", self._on_filter, key)
            self._filter_btns[key] = b
            tools.append(b)
        sp = Gtk.Box(); sp.set_hexpand(True); tools.append(sp)
        self._btn_apt = Gtk.Button(label="📋  Copier la commande apt des paquets manquants")
        self._btn_apt.connect("clicked", self._copy_apt_missing)
        tools.append(self._btn_apt)
        self._btn_guide = Gtk.Button(label="🧭  Guide adapté à cette machine")
        self._btn_guide.set_tooltip_text("Commandes tenant compte de ce qui est déjà installé (CUDA, dépôts, snap…) — lecture seule")
        self._btn_guide.connect("clicked", self._show_guide)
        tools.append(self._btn_guide)
        btn_ref = Gtk.Button(label="↻  Actualiser")
        btn_ref.connect("clicked", lambda _b: self.refresh())
        tools.append(btn_ref)
        header.append(tools)
        self.append(header)

        # ── Liste défilante ──
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._list = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._list.set_margin_top(16); self._list.set_margin_start(28)
        self._list.set_margin_end(28); self._list.set_margin_bottom(28)
        scroll.set_child(self._list)
        self.append(scroll)

        self.refresh()

    # ── Détection (thread) ─────────────────────────────────────────────────
    def refresh(self):
        """Détecte tous les outils en arrière-plan puis reconstruit la liste."""
        if self._busy:
            return
        self._busy = True
        self._spinner.start()
        self._summary.set_text("détection en cours…")

        def work():
            # try/except : sans lui, une erreur de détection laissait la page « occupée » pour toujours (bouton Actualiser inerte)
            try:
                from core.benchmarks import forget_tool_cache
                forget_tool_cache()                 # « Actualiser » = re-chercher vraiment (un outil vient d'être installé)
                res = {t["id"]: tc.detect(t) for t in tc.TOOLS}
                vendors = tc.detect_gpu_vendors()
            except Exception:
                res, vendors = {}, set()
            GLib.idle_add(self._render, res, vendors)
        threading.Thread(target=work, daemon=True).start()

    # ── Rendu ──────────────────────────────────────────────────────────────
    def _concerned(self, t: dict, vendors: set) -> bool:
        """Un outil d'une marque de GPU n'est « concerné » que si un GPU de cette marque est présent (ou si lspci est indisponible)."""
        gv = tc.GROUP_VENDOR.get(t["group"])
        return (not gv) or (not vendors) or (gv[0] in vendors)

    def _render(self, results: dict, vendors: set = None):
        vendors = vendors or set()
        self._vendors = vendors
        self._results = results
        self._busy = False
        self._spinner.stop()
        child = self._list.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._list.remove(child)
            child = nxt
        self._cards = []

        n_ok = sum(1 for v in results.values() if v[0])
        miss = [t for t in tc.TOOLS if not results[t["id"]][0] and self._concerned(t, vendors)]
        miss_req = [t for t in miss if t["need"] == tc.REQUIS]
        self._summary.set_markup(
            f'<span foreground="#a6e3a1" weight="bold">✓ {n_ok} installé(s)</span>   '
            f'<span foreground="#f9e2af" weight="bold">✗ {len(miss)} manquant(s)</span>'
            + (f'   <span foreground="#f38ba8" weight="bold">dont {len(miss_req)} requis</span>' if miss_req else ""))
        self._btn_apt.set_sensitive(bool(tc.apt_command_for_missing([t["id"] for t in miss])))

        for gid, gtitle, gdesc in tc.GROUPS:
            items = [t for t in tc.TOOLS if t["group"] == gid]
            if not items:
                continue
            gh = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            gh.set_margin_top(10)
            gl = Gtk.Label()
            gl.set_markup(f'<span weight="bold" size="large">{GLib.markup_escape_text(gtitle)}</span>')
            gl.set_halign(Gtk.Align.START)
            gh.append(gl)
            gv = tc.GROUP_VENDOR.get(gid)
            if gv and vendors and gv[0] not in vendors:
                gdesc = f"Non concerné : aucun GPU {gv[1]} détecté sur cette machine. " + gdesc
            gh.append(_wrap_label(gdesc, "bench-subtitle"))
            self._list.append(gh)
            group_cards = []
            for t in items:
                concerned = self._concerned(t, vendors)
                card = self._tool_card(t, results[t["id"]], concerned)
                self._list.append(card)
                group_cards.append(card)
                # état pour le filtre : "ok", "missing" ou "na" (marque absente : visible dans « Tous » seulement)
                state = "ok" if results[t["id"]][0] else ("missing" if concerned else "na")
                self._cards.append((card, state, gh, group_cards))
        self._apply_filter()
        return False

    def _tool_card(self, t: dict, res: tuple, concerned: bool = True) -> Gtk.Box:
        ok, path, ver = res
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        card.add_css_class("install-card")
        if not ok and not concerned:
            card.set_opacity(0.6)

        top = Gtk.Box(spacing=8)
        st = Gtk.Label()
        sym, col = ("✓", "#a6e3a1") if ok else (("✗", "#f38ba8") if concerned else ("–", "#8c8c8c"))
        st.set_markup(f'<span foreground="{col}" weight="bold" size="large">{sym}</span>')
        name = Gtk.Label()
        name.set_markup(f'<span weight="bold" size="large" foreground="#f4f4f5">{GLib.markup_escape_text(t["name"])}</span>')
        name.set_halign(Gtk.Align.START)
        need = Gtk.Label()
        need.set_markup(f'<span foreground="{_NEED_COLOR[t["need"]]}" size="small" weight="bold">{t["need"].upper()}</span>')
        sp = Gtk.Box(); sp.set_hexpand(True)
        top.append(st); top.append(name); top.append(need); top.append(sp)
        if ok:
            where = Gtk.Label()
            txt = (ver + " · " if ver else "") + path
            where.set_markup(f'<span foreground="#6c7086" size="small">{GLib.markup_escape_text(txt[:90])}</span>')
            where.set_ellipsize(Pango.EllipsizeMode.START)
            top.append(where)
        card.append(top)

        card.append(_wrap_label(t["role"], "bench-subtitle"))
        if t["used_by"]:
            ub = Gtk.Label()
            ub.set_markup('<span foreground="#6c7086" size="small">Utilisé par : ' + GLib.markup_escape_text(" · ".join(t["used_by"])) + '</span>')
            ub.set_halign(Gtk.Align.START); ub.set_xalign(0); ub.set_wrap(True)
            card.append(ub)

        if not ok:
            for m in t["install"]:
                ml = Gtk.Label()
                ml.set_markup(f'<span foreground="#a6adc8" size="small" weight="bold">{GLib.markup_escape_text(m["label"])}</span>')
                ml.set_halign(Gtk.Align.START); ml.set_xalign(0); ml.set_wrap(True)
                ml.set_margin_top(4)
                card.append(ml)
                row = Gtk.Box(spacing=8)
                code = _wrap_label(m["cmd"], "install-cmd", selectable=True)
                cp = Gtk.Button(label="📋 Copier")
                cp.set_valign(Gtk.Align.START)
                cp.connect("clicked", lambda _b, c=m["cmd"]: self.get_clipboard().set(c))
                row.append(code); row.append(cp)
                card.append(row)
        if t["notes"]:
            nl = _wrap_label(t["notes"])
            if t["notes"].startswith("⚠"):
                nl.add_css_class("warn-card")
            else:
                nl.add_css_class("bench-subtitle")
            card.append(nl)

        if t["links"]:
            lk = Gtk.Box(spacing=4)
            lk.set_margin_top(2)
            li = Gtk.Label()
            li.set_markup('<span foreground="#6c7086" size="small">Liens :</span>')
            lk.append(li)
            for label, url in t["links"]:
                b = Gtk.LinkButton.new_with_label(url, label)
                b.set_tooltip_text(url)
                lk.append(b)
            card.append(lk)
        return card

    # ── Filtre ─────────────────────────────────────────────────────────────
    def _on_filter(self, btn, key):
        if btn.get_active():
            self._filter = key
            self._apply_filter()

    def _apply_filter(self):
        f = self._filter
        for card, state, _gh, group_cards in self._cards:
            card.set_visible(f == "all" or f == state)
        # un titre de groupe sans carte visible est masqué
        seen = {}
        for card, state, gh, group_cards in self._cards:
            seen.setdefault(id(gh), [gh, []])[1].append(card)
        for gh, cards in seen.values():
            gh.set_visible(any(c.get_visible() for c in cards))

    # ── Actions ────────────────────────────────────────────────────────────
    def _copy_apt_missing(self, _b):
        miss = [t["id"] for t in tc.TOOLS if not self._results.get(t["id"], (True,))[0] and self._concerned(t, getattr(self, "_vendors", set()))]
        cmd = tc.apt_command_for_missing(miss)
        if cmd:
            self.get_clipboard().set(cmd)
            self._summary.set_markup('<span foreground="#a6e3a1">Commande copiée — collez-la dans un terminal.</span>')

    def _show_guide(self, _b):
        """Génère (thread) le guide adapté à la machine et l'affiche dans une fenêtre ; lecture seule."""
        self._btn_guide.set_sensitive(False)
        self._btn_guide.set_label("🧭  Analyse de la machine…")

        def work():
            try:
                from core.install_advisor import collect, render
                text = render(collect())
            except Exception as e:
                text = f"Analyse impossible : {e}"
            GLib.idle_add(self._guide_window, text)
        threading.Thread(target=work, daemon=True).start()

    def _guide_window(self, text: str):
        self._btn_guide.set_sensitive(True)
        self._btn_guide.set_label("🧭  Guide adapté à cette machine")
        root = self.get_root()
        win = Gtk.Window(title="Guide d'installation adapté à cette machine")
        if isinstance(root, Gtk.Window):
            win.set_transient_for(root)
        win.set_default_size(*__import__('ui.screen', fromlist=['fit_size']).fit_size(920, 700))
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12); box.set_margin_bottom(12); box.set_margin_start(12); box.set_margin_end(12)
        buf = Gtk.TextBuffer()
        buf.set_text(text)
        tv = Gtk.TextView(buffer=buf)
        tv.set_editable(False); tv.set_monospace(True); tv.add_css_class("log-view")
        tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        sc = Gtk.ScrolledWindow(); sc.set_vexpand(True); sc.set_child(tv)
        box.append(sc)
        bar = Gtk.Box(spacing=8)
        cp = Gtk.Button(label="📋 Copier tout le guide")
        cp.connect("clicked", lambda _b: self.get_clipboard().set(text))
        cl = Gtk.Button(label="Fermer")
        cl.connect("clicked", lambda _b: win.close())
        bar.append(cp); bar.append(cl)
        box.append(bar)
        win.set_child(box)
        win.present()
        return False
