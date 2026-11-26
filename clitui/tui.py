"""Écran interactif plein écran (curses) : la version graphique de BenchSuite, dans un terminal (SSH compris).

Disposition : menu à gauche (Accueil · CPU + RAM · Stockage · GPU · Réseau · Infos système · Rapports), à droite la liste des profils / tests
puis leurs réglages. Flèches / Entrée / r (lancer) / ? (aide) / q (quitter). Un test se lance dans le terminal normal (suivi en direct, verdict),
puis on revient à l'écran. Repli automatique sur le menu numéroté (`plain.py`) si le terminal est trop petit ou sans curses.
"""
import curses
import curses.ascii
import textwrap
import threading
from typing import Callable, List, Optional

from clitui import model, run as bridge
from clitui.model import Item

MIN_W, MIN_H = 72, 20
MENU_W = 21
_SPIN = "|/-\\"

_ICONS = {"home": "⌂", "seq": "☰", "cpu": "◉", "fio": "▤", "gpu": "◈", "net": "⇄", "outils": "⚙", "infos": "ⓘ", "rapports": "≡", "quit": "✕"}

MENU = [("home", "Accueil")] + list(model.PAGES) + [("infos", "Infos système"), ("rapports", "Rapports"), ("quit", "Quitter")]


class _Theme:
    """Couleurs (rôles) ; 256 couleurs → orange de la charte BenchSuite, sinon jaune."""
    def __init__(self):
        self.a = {}
        try:
            curses.start_color()
            curses.use_default_colors()
            orange = 208 if curses.COLORS >= 256 else curses.COLOR_YELLOW
            grey = 245 if curses.COLORS >= 256 else -1
            for i, (fg, bg) in enumerate([(orange, -1), (curses.COLOR_GREEN, -1), (curses.COLOR_RED, -1), (curses.COLOR_YELLOW, -1),
                                          (grey, -1), (curses.COLOR_BLACK, orange), (curses.COLOR_CYAN, -1)], 1):
                curses.init_pair(i, fg, bg)
            self.a = {"accent": curses.color_pair(1) | curses.A_BOLD, "ok": curses.color_pair(2), "err": curses.color_pair(3),
                      "warn": curses.color_pair(4), "dim": curses.color_pair(5), "sel": curses.color_pair(6) | curses.A_BOLD,
                      "info": curses.color_pair(7), "bold": curses.A_BOLD, "norm": 0}
        except Exception:
            self.a = {k: 0 for k in ("accent", "ok", "err", "warn", "dim", "sel", "info", "bold", "norm")}
            self.a["sel"] = curses.A_REVERSE
            self.a["bold"] = curses.A_BOLD

    def __getitem__(self, k):
        return self.a.get(k, 0)


class App:
    """État et dessin de l'écran interactif."""

    def __init__(self, scr, cli, parsed):
        self.scr, self.cli, self.parsed = scr, cli, parsed
        self.t = _Theme()
        self.menu_i = 0
        self.focus = "menu"                       # menu · list · params
        self.list_i = {p: 0 for p, _ in model.PAGES}
        self.list_off = {p: 0 for p, _ in model.PAGES}
        self.param_i = 0
        self.msg = ""
        self.msg_kind = "dim"
        self.info_lines: Optional[List[str]] = None
        self.info_off = 0
        self.info_loading = False
        self.home: Optional[dict] = None
        self.home_loading = False
        self.id_i = 0
        self.ident = bridge.load_identity()
        self.tick = 0
        self.show_help = False
        self.suspended_once = False
        self.args_over = {}
        self._start_loaders()

    # ── Chargements en arrière-plan (jamais de blocage de l'écran) ────────────

    def _start_loaders(self):
        def home():
            try:
                self.home = self.cli.get_hardware_report()
            except Exception:
                self.home = {}
            self.home_loading = False
        self.home_loading = True
        threading.Thread(target=home, daemon=True).start()

    def load_infos(self):
        if self.info_loading:
            return
        self.info_loading = True

        def work():
            try:
                from core.system_info import collect_full_system_info, format_summary
                self.info_lines = format_summary(collect_full_system_info()).splitlines()
            except Exception as e:
                self.info_lines = [f"Collecte impossible : {e}"]
            self.info_loading = False
        threading.Thread(target=work, daemon=True).start()

    # ── Dessin ────────────────────────────────────────────────────────────────

    def put(self, y: int, x: int, text: str, role: str = "norm", width: int = None):
        h, w = self.scr.getmaxyx()
        if y < 0 or y >= h or x >= w:
            return
        n = (w - x) if width is None else min(width, w - x)
        if n <= 0:
            return
        try:
            self.scr.addnstr(y, x, text, n, self.t[role])
        except curses.error:
            pass

    def fill(self, y: int, x: int, width: int, role: str):
        self.put(y, x, " " * max(0, width), role, width)

    def wrap(self, text: str, width: int) -> List[str]:
        out: List[str] = []
        for para in (text or "").split("\n"):
            out += textwrap.wrap(para, max(10, width)) or [""]
        return out

    def draw(self):
        self.tick += 1
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        # en-tête
        self.fill(0, 0, w, "sel")
        ver = model.read_version()
        host = (self.home or {}).get("systeme", {}).get("hostname", "")
        self.put(0, 1, "⚡ BenchSuite", "sel", w - 2)
        right = (f"{host}  ·  " if host else "") + "interface terminal" + (f"  ·  v{ver}" if ver else "")
        self.put(0, max(16, w - len(right) - 2), right, "sel", len(right) + 1)
        # menu
        for i, (key, label) in enumerate(MENU):
            y = 2 + i
            sel = i == self.menu_i
            role = "sel" if (sel and self.focus == "menu") else ("accent" if sel else "norm")
            self.fill(y, 0, MENU_W - 1, role if sel else "norm")
            self.put(y, 1, ("▌" if sel else " ") + _ICONS.get(key, " ") + " " + label, role, MENU_W - 2)
        for y in range(1, h - 2):
            self.put(y, MENU_W - 1, "│", "dim")
        # contenu
        key = MENU[self.menu_i][0]
        x0, wpane = MENU_W + 1, w - MENU_W - 2
        if key == "home":
            self.draw_home(x0, wpane, h)
        elif key in dict(model.PAGES):
            self.draw_page(key, x0, wpane, h)
        elif key == "infos":
            self.draw_infos(x0, wpane, h)
        elif key == "rapports":
            self.draw_reports(x0, wpane, h)
        elif key == "quit":
            self.put(3, x0, "Quitter BenchSuite ? Appuyez sur Entrée (ou q).", "warn")
        # pied
        self.put(h - 2, 1, self.msg[: w - 2], self.msg_kind)
        self.fill(h - 1, 0, w, "dim")
        x = 1
        for part in [p for p in self.footer().split("   ") if p.strip()]:     # « ↑↓ choisir » → touche en relief + action
            k, _, act = part.strip().partition(" ")
            if x + len(part) + 2 >= w:
                break
            self.put(h - 1, x, f" {k} ", "sel", len(k) + 2)
            self.put(h - 1, x + len(k) + 2, " " + act, "dim", w - x - len(k) - 3)
            x += len(k) + len(act) + 5
        if self.show_help:
            self.draw_help(h, w)
        self.scr.refresh()

    def footer(self) -> str:
        key = MENU[self.menu_i][0]
        if self.focus == "menu":
            return "↑↓ choisir   →/Entrée ouvrir   ? aide   q quitter"
        if key in dict(model.PAGES):
            if self.focus == "list":
                return "↑↓ choisir   →/Entrée réglages   r LANCER   ←/Échap menu   ? aide"
            return "↑↓ choisir   Entrée modifier   ←/Échap retour   r LANCER   ? aide"
        if key == "infos":
            return "↑↓ PgUp/PgDn défiler   e exporter le rapport   u actualiser   ←/Échap menu"
        if key == "rapports":
            return "↑↓ champ   Entrée modifier l'identité   g régénérer le rapport   ←/Échap menu"
        return "←/Échap menu"

    # ── Pages ─────────────────────────────────────────────────────────────────

    def draw_home(self, x0, wp, h):
        self.put(2, x0, "Bienvenue", "accent")
        intro = self.wrap("Lancez un test comme dans la version graphique : choisissez une catégorie à gauche, un profil, ses réglages, puis r.", wp)
        for i, l in enumerate(intro[:3]):
            self.put(3 + i, x0, l, "dim", wp)
        y = 3 + len(intro[:3]) + 1
        if self.home_loading:
            self.put(y, x0, f"{_SPIN[self.tick % 4]} lecture de la machine…", "dim")
            return
        r = self.home or {}
        cpu, mem, sysi = r.get("cpu", {}), r.get("memoire", {}), r.get("systeme", {})
        rows = [("Machine", f"{sysi.get('hostname', '?')}  ·  {sysi.get('os', '')}"),
                ("Processeur", f"{cpu.get('modele', '?')}  ({cpu.get('threads_total', '?')} threads)"),
                ("Mémoire", f"{mem.get('total_go', '?')} Go"),
                ("GPU", ", ".join(g.get("nom", "?") for g in r.get("gpu", [])[:3]) or "aucun détecté"),
                ("Disques", f"{len(r.get('stockage', []))}   ·   Réseau : {len(r.get('reseau', []))} interface(s)")]
        for k, v in rows:
            self.put(y, x0, f"{k:<11}", "dim")
            self.put(y, x0 + 12, v, "norm", wp - 12)
            y += 1
        y += 1
        total = model.all_items()
        blocked = [i for i in total if i.missing_tools()]
        self.put(y, x0, f"{len(total)} profils / tests disponibles", "ok")
        if blocked:
            tools = sorted({t for i in blocked for t in i.missing_tools()})
            lines = self.wrap(f"⚠ {len(blocked)} bloqués par des outils manquants : {', '.join(tools)}", wp)[:3]
            for k, l in enumerate(lines):
                self.put(y + 1 + k, x0, l, "warn", wp)
            y += len(lines)
            self.put(y + 1, x0, "(sélectionnez un profil concerné : les commandes apt s'affichent)", "dim", wp)
            y += 1
        self.put(y + 3, x0, "Commandes courtes (hors de cet écran) :", "accent")
        cmds = ["./cli.py cpu 5m", "./cli.py fio perf <dossier>", "./cli.py gpu oclburn 10m", "./cli.py infos", "./cli.py liste"]
        line, k = "", 0
        for c in cmds:
            if line and len(line) + len(c) + 3 > wp:
                self.put(y + 4 + k, x0, line, "dim", wp); k += 1; line = ""
            line += ("   " if line else "") + c
        self.put(y + 4 + k, x0, line, "dim", wp)

    def _rows(self, page: str):
        """Lignes d'affichage de la liste : en-têtes de groupe (non sélectionnables) + éléments."""
        rows, last = [], None
        for it in model.items_for(page):
            if it.group != last:
                rows.append(("hdr", it.group or "Divers"))
                last = it.group
            rows.append(("item", it))
        return rows

    def draw_page(self, page: str, x0: int, wp: int, h: int):
        items = model.items_for(page)
        title = dict(model.PAGES)[page]
        self.put(1, x0, title, "accent")
        if not items:
            self.put(3, x0, "Aucun élément disponible sur cette machine.", "warn")
            return
        rows = self._rows(page)
        list_h = max(5, (h - 6) * 45 // 100)
        sel = items[self.list_i[page]]
        sel_row = next(i for i, (k, o) in enumerate(rows) if k == "item" and o is sel)
        off = self.list_off[page]
        if sel_row < off:
            off = sel_row
        if sel_row >= off + list_h:
            off = sel_row - list_h + 1
        self.list_off[page] = off
        for i in range(list_h):
            ri = off + i
            if ri >= len(rows):
                break
            kind, obj = rows[ri]
            y = 2 + i
            if kind == "hdr":
                self.put(y, x0, "— " + obj, "dim", wp)
            else:
                miss = obj.missing_tools()
                is_sel = obj is sel
                role = ("sel" if self.focus == "list" else "accent") if is_sel else ("dim" if miss else "norm")
                if is_sel:
                    self.fill(y, x0, wp, role)
                badge = ("⚠ " + miss[0]) if miss else ("▶" if obj.kind in ("suite", "action") else "✓")
                self.put(y, x0, ("▌ " if is_sel else "  ") + obj.label, role, max(1, wp - len(badge) - 2))
                self.put(y, x0 + wp - len(badge) - 1, badge, role if is_sel else ("warn" if miss else "ok"), len(badge) + 1)
        self.put(1, x0 + wp - 12, f"{self.list_i[page] + 1}/{len(items)}".rjust(11), "dim")
        if len(rows) > list_h:                      # ascenseur
            thumb = 2 + int((list_h - 1) * off / max(1, len(rows) - list_h))
            self.put(thumb, x0 + wp, "█", "accent", 1)
        # détails + réglages
        y = 2 + list_h + 1
        self.put(y - 1, x0, "─" * wp, "dim")
        y = self._draw_item(sel, x0, wp, y, h)

    def _draw_item(self, it: Item, x0: int, wp: int, y: int, h: int) -> int:
        for line in self.wrap(it.desc, wp)[:3]:
            self.put(y, x0, line, "dim", wp)
            y += 1
        if it.standard:
            self.put(y, x0, f"Norme : {it.standard}", "info", wp)
            y += 1
        miss = it.missing_tools()
        if miss:
            self.put(y, x0, "⚠ outil manquant : " + ", ".join(miss), "warn", wp)
            y += 1
            for tool in miss[:1]:
                for lab, cmd in model.install_commands(tool)[:3]:
                    guess = "   (nom supposé, à vérifier)" if "supposé" in lab else ""
                    self.put(y, x0, f"  {lab.split(' ')[0]:<11} {cmd}{guess}", "dim", wp)
                    y += 1
        y += 0
        for i, p in enumerate(it.params):
            if y >= h - 4:
                break
            is_sel = self.focus == "params" and i == self.param_i
            role = "sel" if is_sel else ("accent" if p.dirty() else "norm")
            if is_sel:
                self.fill(y, x0, wp, "sel")
            lab = p.label[:30]
            self.put(y, x0, "  " + lab + " ", role, wp)
            self.put(y, x0 + 3 + len(lab), "·" * max(1, 33 - 3 - len(lab)), "dim" if not is_sel else role, wp)
            self.put(y, x0 + 33, p.display(), role if (is_sel or p.dirty()) else "info", max(1, wp - 33))
            y += 1
        if self.focus == "params" and it.params:
            p = it.params[min(self.param_i, len(it.params) - 1)]
            hint = p.hint + (f"   [Tab : {', '.join(p.suggest[:3])}]" if p.suggest else "")
            if hint:
                self.put(h - 4, x0, hint, "dim", wp)
        err = it.validate()
        self.put(h - 3, x0, ("✗ " + err) if err else "✓ prêt à lancer : r", "err" if err else "ok", wp)
        return y

    def draw_infos(self, x0, wp, h):
        self.put(1, x0, "Informations système", "accent")
        if self.info_lines is None:
            if not self.info_loading:
                self.load_infos()
            self.put(3, x0, f"{_SPIN[self.tick % 4]} collecte en cours (PCIe, disques, réseau, carte mère…)", "dim")
            return
        vis = h - 5
        self.info_off = max(0, min(self.info_off, max(0, len(self.info_lines) - vis)))
        for i in range(vis):
            if self.info_off + i < len(self.info_lines):
                self.put(2 + i, x0, self.info_lines[self.info_off + i], "norm", wp)

    def _session_results(self):
        try:
            from core import cli_session as cs
            sd = bridge.latest_session(self.parsed.output_dir if self.parsed else "~/BenchmarkResults")
            return sd, (cs.results_of(cs.load(sd)) if sd else [])
        except Exception:
            return None, []

    def draw_reports(self, x0, wp, h):
        self.put(1, x0, "Rapports de ce démarrage de la machine", "accent")
        sd, results = self._session_results()
        y = 2
        if not results:
            self.put(y, x0, "Aucun test lancé depuis le démarrage.", "dim")
            y += 1
        else:
            rows = bridge.result_rows(results)[-(h // 3):]
            for r in rows:
                role = "ok" if r[2] == "OK" else ("err" if r[2] == "ÉCHEC" else "warn")
                self.put(y, x0, f"{r[0]:>2} {r[1][:34]:<34} {r[2]:<9}{r[3]:>7}  {r[5]}", role, wp)
                y += 1
            self.put(y, x0, f"Rapport : {sd}/benchsuite_report.*", "dim", wp)
            y += 1
        y += 1
        self.put(y, x0, "Identité du rapport (mémorisée)", "accent")
        y += 1
        for i, (k, label) in enumerate(bridge.IDENTITY_FIELDS):
            is_sel = self.focus == "params" and i == self.id_i
            role = "sel" if is_sel else "norm"
            if is_sel:
                self.fill(y, x0, wp, "sel")
            self.put(y, x0, f"  {label:<28}{self.ident.get(k, '') or '—'}", role, wp)
            y += 1

    def draw_help(self, h, w):
        raw = ["Aide", "", "↑ ↓        choisir            → / Entrée   ouvrir, modifier", "← / Échap  revenir            r            LANCER le test",
               "g          régénérer le rapport (page Rapports)", "e          exporter le rapport (page Infos système)",
               "Tab        propose les disques de données (champ « disque »)", "q          quitter",
               "", "Le test se lance dans le terminal normal : suivi en direct, verdict, puis retour à cet écran. Ctrl-C l'arrête proprement.",
               "L'équivalent en commande courte est affiché dans le pied de chaque profil."]
        bw = min(w - 4, 76)
        lines: List[str] = []
        for l in raw:
            lines += self.wrap(l, bw - 4) if len(l) > bw - 4 else [l]
        bh = len(lines) + 2
        y0, x0 = max(1, (h - bh) // 2), max(1, (w - bw) // 2)
        for i in range(bh):
            self.put(y0 + i, x0, " " * bw, "norm", bw)
        for i, l in enumerate(lines):
            self.put(y0 + 1 + i, x0 + 2, l, "accent" if i == 0 else "norm", bw - 4)
            self.put(y0 + 1 + i, x0, "│", "dim", 1)
            self.put(y0 + 1 + i, x0 + bw - 1, "│", "dim", 1)
        self.put(y0, x0, "┌" + "─" * (bw - 2) + "┐", "dim", bw)
        self.put(y0 + bh - 1, x0, "└" + "─" * (bw - 2) + "┘", "dim", bw)

    # ── Saisie ────────────────────────────────────────────────────────────────

    def prompt(self, label: str, initial: str = "", suggest: List[str] = None) -> Optional[str]:
        """Ligne de saisie en bas de l'écran. Entrée valide, Échap annule, Tab fait défiler les propositions. None si annulé."""
        h, w = self.scr.getmaxyx()
        buf = initial
        fresh = True            # la 1re frappe remplace la valeur proposée (Retour arrière la modifie à la place)
        si = -1
        self.scr.timeout(-1)
        try:
            while True:
                self.fill(h - 2, 0, w, "norm")
                self.put(h - 2, 1, f"{label} : ", "accent")
                self.put(h - 2, 4 + len(label), buf[-(w - len(label) - 8):] + "▌", "norm")
                self.scr.refresh()
                c = self.scr.getch()
                if c in (10, 13, curses.KEY_ENTER):
                    return buf
                if c == 27:
                    return None
                if c in (curses.KEY_BACKSPACE, 127, 8):
                    buf, fresh = buf[:-1], False
                elif c == 21:               # Ctrl-U
                    buf = ""
                elif c == 9 and suggest:
                    si = (si + 1) % len(suggest)
                    buf, fresh = suggest[si], False
                elif 32 <= c < 127 or c > 160:
                    buf = chr(c) if fresh else buf + chr(c)
                    fresh = False
        finally:
            self.scr.timeout(250)

    def say(self, text: str, kind: str = "dim"):
        self.msg, self.msg_kind = text, kind

    # ── Actions ───────────────────────────────────────────────────────────────

    def suspend(self, fn: Callable, *a):
        """Quitte l'écran curses, exécute `fn` dans le terminal normal (suivi en direct), puis revient à l'écran."""
        curses.def_prog_mode()
        curses.endwin()
        rc = 0
        try:
            rc = fn(*a)
        except KeyboardInterrupt:
            print("\nInterrompu.")
        except Exception as e:                  # jamais de traceback utilisateur
            print(f"\n✗ {e}")
        try:
            input("\nEntrée pour revenir à l'écran BenchSuite… ")
        except (EOFError, KeyboardInterrupt):
            pass
        curses.reset_prog_mode()
        self.scr.clear()
        self.scr.refresh()
        self.suspended_once = True
        model.forget_tool_cache()               # un outil a pu être installé pendant ce temps
        return rc

    def run_selected(self, it: Item):
        err = it.validate()
        if err:
            self.say("✗ " + err, "err")
            return
        rc = self.suspend(bridge.launch, self.cli, [it], self.parsed)
        self.say(f"Terminé (code {rc}). Résultats : page Rapports." if rc == 0 else f"Terminé avec code {rc} — voir la page Rapports.",
                 "ok" if rc == 0 else "warn")

    def edit_param(self, it: Item):
        p = it.params[self.param_i]
        if p.kind in ("bool", "choice"):
            p.cycle()
            return
        v = self.prompt(p.label, "" if p.value is None else str(p.value), p.suggest)
        if v is None:
            return
        ok, val = p.parse(v)
        if ok:
            p.value = val
            self.say("", "dim")
        else:
            self.say(f"✗ {p.label} : {val}", "err")

    def edit_identity(self):
        k, label = bridge.IDENTITY_FIELDS[self.id_i]
        v = self.prompt(label, self.ident.get(k, ""))
        if v is not None:
            self.ident[k] = v.strip()
            bridge.save_identity(self.ident)

    def export_info_report(self):
        def go():
            ns = bridge.make_args(self.cli, self.parsed)
            return self.cli.run_info_report(ns)
        self.suspend(go)

    def regenerate_report(self):
        sd, results = self._session_results()
        if not results:
            self.say("Rien à régénérer : aucun test depuis le démarrage.", "warn")
            return

        def go():
            ns = bridge.make_args(self.cli, self.parsed)
            meta = self.cli.build_meta(ns, __import__("core.identity", fromlist=["x"]).collect_identity())
            from core.system_info import collect_full_system_info
            paths = self.cli.export_campaign(results, sd, meta, collect_full_system_info(), ns.export, fixed_names=True)
            for f, pth in paths.items():
                print(f"  ✓ {f.upper():<7} {pth}")
            return 0
        self.suspend(go)

    # ── Boucle ────────────────────────────────────────────────────────────────

    def page_items(self) -> List[Item]:
        return model.items_for(MENU[self.menu_i][0])

    def handle(self, c: int) -> bool:
        """Traite une touche ; False pour quitter."""
        key = MENU[self.menu_i][0]
        h, w = self.scr.getmaxyx()
        if self.show_help:
            self.show_help = False
            return True
        if c in (ord("?"), curses.KEY_F1):
            self.show_help = True
            return True
        if c in (ord("q"), ord("Q")) and self.focus == "menu":
            return False
        up, down = c in (curses.KEY_UP, ord("k")), c in (curses.KEY_DOWN, ord("j"))
        left = c in (curses.KEY_LEFT, 27, ord("h"))
        right = c in (curses.KEY_RIGHT, ord("l"), 10, 13, curses.KEY_ENTER)
        if self.focus == "menu":
            if up:
                self.menu_i = (self.menu_i - 1) % len(MENU)
                self.say("")
            elif down:
                self.menu_i = (self.menu_i + 1) % len(MENU)
                self.say("")
            elif right:
                if key == "quit":
                    return False
                if key in dict(model.PAGES):
                    self.focus, self.param_i = "list", 0
                elif key in ("infos", "rapports"):
                    self.focus = "params" if key == "rapports" else "list"
                    if key == "infos" and self.info_lines is None:
                        self.load_infos()
            return True
        if key in dict(model.PAGES):
            items = self.page_items()
            if not items:
                self.focus = "menu"
                return True
            it = items[self.list_i[key]]
            if self.focus == "list":
                if up:
                    self.list_i[key] = (self.list_i[key] - 1) % len(items); self.param_i = 0
                elif down:
                    self.list_i[key] = (self.list_i[key] + 1) % len(items); self.param_i = 0
                elif c in (curses.KEY_PPAGE,):
                    self.list_i[key] = max(0, self.list_i[key] - 8)
                elif c in (curses.KEY_NPAGE,):
                    self.list_i[key] = min(len(items) - 1, self.list_i[key] + 8)
                elif c == ord("r"):
                    self.run_selected(it)
                elif right and it.params:
                    self.focus, self.param_i = "params", 0
                elif right:
                    self.run_selected(it)
                elif left:
                    self.focus = "menu"
            else:
                if up:
                    self.param_i = (self.param_i - 1) % max(1, len(it.params))
                elif down:
                    self.param_i = (self.param_i + 1) % max(1, len(it.params))
                elif c == ord("r"):
                    self.run_selected(it)
                elif c == 9 and it.params and it.params[self.param_i].suggest:
                    it.params[self.param_i].cycle()
                elif right:
                    self.edit_param(it)
                elif left:
                    self.focus = "list"
            return True
        if key == "infos":
            if c in (curses.KEY_UP, ord("k")):
                self.info_off -= 1
            elif c in (curses.KEY_DOWN, ord("j")):
                self.info_off += 1
            elif c == curses.KEY_PPAGE:
                self.info_off -= h - 6
            elif c == curses.KEY_NPAGE:
                self.info_off += h - 6
            elif c == ord("e"):
                self.export_info_report()
            elif c == ord("u"):
                self.info_lines = None
            elif left:
                self.focus = "menu"
            return True
        if key == "rapports":
            if up:
                self.id_i = (self.id_i - 1) % len(bridge.IDENTITY_FIELDS)
            elif down:
                self.id_i = (self.id_i + 1) % len(bridge.IDENTITY_FIELDS)
            elif right:
                self.edit_identity()
            elif c == ord("g"):
                self.regenerate_report()
            elif left:
                self.focus = "menu"
            return True
        return True


def _loop(scr, cli, parsed) -> int:
    try:
        curses.curs_set(0)
    except curses.error:
        pass
    curses.set_escdelay(25) if hasattr(curses, "set_escdelay") else None
    scr.keypad(True)
    app = App(scr, cli, parsed)
    last_draw = 0.0
    dirty = True
    while True:
        busy = app.home_loading or app.info_loading                # chargements en arrière-plan : tourner la roue
        scr.timeout(120 if busy else 1000)
        h, w = scr.getmaxyx()
        if h < MIN_H or w < MIN_W:
            scr.erase()
            try:
                scr.addnstr(0, 0, f"Terminal trop petit ({w}x{h}) : minimum {MIN_W}x{MIN_H}. Agrandissez la fenêtre, ou q pour quitter.", max(1, w - 1))
            except curses.error:
                pass
            scr.refresh()
            c = scr.getch()
            if c in (ord("q"), ord("Q")):
                return 0
            continue
        if dirty or busy:
            app.draw()
            dirty = False
        c = scr.getch()
        if c == -1:
            continue                                               # rien n'a changé : pas de redessin (écran fixe = pas de scintillement)
        dirty = True
        if c == curses.KEY_RESIZE:
            continue
        # Toutes les touches déjà en attente (touche maintenue, répétition rapide) sont traitées AVANT de redessiner : un seul dessin au lieu
        # d'un par touche — c'est ce qui supprime la latence quand on reste appuyé sur ↑ / ↓.
        keys = [c]
        scr.nodelay(True)
        try:
            while len(keys) < 40:
                k = scr.getch()
                if k == -1:
                    break
                keys.append(k)
        finally:
            scr.nodelay(False)
        for k in keys:
            if k == curses.KEY_RESIZE:
                continue
            if not app.handle(k):
                return 0
            if app.suspended_once:                                 # un test vient de tourner dans le terminal : ne pas rejouer les touches tapées pendant
                app.suspended_once = False
                curses.flushinp()
                break


def run_tui(cli, parsed=None) -> int:
    return curses.wrapper(_loop, cli, parsed)


def run(cli, parsed=None) -> int:
    """Lance l'écran interactif ; repli sur le menu numéroté si curses est indisponible ou le terminal trop petit."""
    import os
    if parsed is None:
        parsed = cli.build_parser().parse_args([])
    try:
        sz = os.get_terminal_size()
        if sz.columns < MIN_W or sz.lines < MIN_H:
            from clitui import plain
            print(f"Terminal {sz.columns}x{sz.lines} trop petit pour l'écran plein (minimum {MIN_W}x{MIN_H}) : menu simple.\n")
            return plain.run(cli, parsed)
        return run_tui(cli, parsed)
    except (curses.error, OSError):
        from clitui import plain
        print("Écran plein indisponible dans ce terminal : menu simple.\n")
        return plain.run(cli, parsed)
