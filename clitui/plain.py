"""Menu numéroté de repli : même parcours que l'écran plein (catégorie → profil → réglages → lancer), en simples questions / réponses.

Utilisé quand curses est indisponible (terminal exotique) ou le terminal trop petit. Fonctionne aussi par SSH sans terminal complet.
"""
from typing import List

from clitui import model, run as bridge
from clitui.model import Item


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return "q"


def _pick(title: str, rows: List[str], back: str = "retour") -> int:
    """Affiche une liste numérotée ; retourne l'indice choisi, -1 pour « retour », -2 pour quitter."""
    print(f"\n{title}")
    for i, r in enumerate(rows, 1):
        print(f"  {i:>2}) {r}")
    print(f"   q) quitter" if back == "quitter" else f"   r) {back}     q) quitter")
    while True:
        a = _ask("> ").lower()
        if a == "q":
            return -2
        if a in ("r", ""):
            return -1
        if a.isdigit() and 1 <= int(a) <= len(rows):
            return int(a) - 1
        print("  Choix invalide.")


def _edit(cli, it: Item, parsed) -> int:
    """Réglages d'un élément puis lancement. Retourne -2 pour quitter, 0 sinon."""
    while True:
        print(f"\n== {it.label}")
        if it.desc:
            print("   " + it.desc[:200])
        miss = it.missing_tools()
        if miss:
            print("   ⚠ outil manquant : " + ", ".join(miss))
            for t in miss:
                for lab, cmd in model.install_commands(t)[:2]:
                    print(f"      {lab} : {cmd}")
        for i, p in enumerate(it.params, 1):
            print(f"  {i:>2}) {p.label:<34} {p.display()}" + ("   *" if p.dirty() else ""))
        err = it.validate()
        print(("  ✗ " + err) if err else "  ✓ prêt")
        a = _ask("numéro = modifier · l = LANCER · r = retour > ").lower()
        if a == "q":
            return -2
        if a in ("r", ""):
            return 0
        if a == "l":
            if err:
                continue
            bridge.launch(cli, [it], parsed)
            _ask("\nEntrée pour continuer… ")
            continue
        if a.isdigit() and 1 <= int(a) <= len(it.params):
            p = it.params[int(a) - 1]
            if p.kind in ("bool", "choice"):
                p.cycle()
                continue
            sug = f" (propositions : {', '.join(p.suggest[:4])})" if p.suggest else ""
            v = _ask(f"  {p.label}{sug} : ")
            ok, val = p.parse(v)
            if ok:
                p.value = val
            else:
                print(f"  ✗ {val}")


def run_menu(cli, parsed) -> int:
    T = cli.TermColor
    print(f"\n{T.BOLD}{T.rgb(249, 115, 22)}⚡ BenchSuite{T.RESET} — menu simple (numéro puis Entrée)")
    while True:
        rows = [t for _, t in model.PAGES] + ["Infos système (à l'écran)", "Rapport « infos système » (fichiers)", "Résultats de ce démarrage"]
        i = _pick("Que voulez-vous faire ?", rows, back="quitter")
        if i < 0:
            return 0
        if i < len(model.PAGES):
            page = model.PAGES[i][0]
            items = model.items_for(page)
            while True:
                labels = [("⚠ " if it.missing_tools() else "") + (f"[{it.group}] " if it.group else "") + it.label for it in items]
                j = _pick(model.PAGES[i][1], labels)
                if j == -2:
                    return 0
                if j == -1:
                    break
                if _edit(cli, items[j], parsed) == -2:
                    return 0
        elif i == len(model.PAGES):
            cli.print_hardware_info()
            _ask("\nEntrée pour continuer… ")
        elif i == len(model.PAGES) + 1:
            cli.run_info_report(bridge.make_args(cli, parsed))
            _ask("\nEntrée pour continuer… ")
        else:
            bridge.session_summary(cli, parsed.output_dir)
            _ask("\nEntrée pour continuer… ")


def run(cli, parsed=None) -> int:
    if parsed is None:
        parsed = cli.build_parser().parse_args([])
    return run_menu(cli, parsed)
