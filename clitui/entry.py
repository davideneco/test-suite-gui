"""Point d'entrée de la nouvelle interface, appelé par cli.main() avant l'analyse des options historiques."""
import sys
from typing import List, Tuple


def interactive_terminal() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except Exception:
        return False


def maybe_handle(argv: List[str], cli) -> Tuple[bool, int]:
    """(pris en charge, code de sortie). Les options historiques (-x, --xxx) ne sont jamais prises ici."""
    from clitui import commands
    if not argv:
        if interactive_terminal():
            from clitui import tui
            return True, tui.run(cli)
        print(commands.HELP)           # pas de terminal interactif (pipe, cron…) : aide courte, jamais de plein écran
        return True, cli.EXIT_OK
    first = argv[0]
    if first.startswith("-"):
        return False, 0
    if first in commands.SUBCOMMANDS:
        return True, commands.dispatch(argv, cli)
    print(f"{cli.TermColor.RED}Commande inconnue : « {first} »{cli.TermColor.RESET}\n")
    print(commands.HELP)
    return True, cli.EXIT_USAGE
