"""
cli_utils.py — parsing CLI robuste pour BenchSuite.

Objectif : permettre un mode « ligne de commande » pratique sans shell=True.
On accepte les usages courants d'un terminal, mais on bloque les opérateurs
shell qui nécessitent un vrai shell (pipes, redirections, &&, ;, etc.).
"""

from __future__ import annotations

import os
import re
import shlex
from pathlib import Path
from typing import Dict, List, Tuple


_SHELL_OPERATORS = {";", "&&", "||", "|", ">", ">>", "<", "<<", "2>", "2>>", "&"}


def _machine_vars() -> Dict[str, str]:
    cpu = os.cpu_count() or 1
    try:
        import psutil  # type: ignore
        vm = psutil.virtual_memory()
        ram_mb = int(vm.total // (1024 * 1024))
        ram_gb = int(round(vm.total / (1024 ** 3)))
    except Exception:
        ram_mb = 0
        ram_gb = 0
    return {
        "NPROC": str(cpu),
        "CPU_COUNT": str(cpu),
        "THREADS": str(cpu),
        "RAM_MB": str(ram_mb),
        "RAM_GB": str(ram_gb),
    }


def expand_safe_cli(text: str) -> Tuple[str, Dict[str, str]]:
    """Expansion contrôlée des raccourcis CLI utiles.

    Supporté :
    - $(nproc), $(nproc --all)
    - $NPROC, ${NPROC}, $CPU_COUNT, $THREADS, $RAM_MB, $RAM_GB
    - variables d'environnement classiques via $HOME, ${USER}, etc.
    - ~ dans les chemins après découpage shlex

    Non supporté volontairement : pipes, redirections, &&, ;, backticks,
    substitutions de commandes arbitraires. Cela évite shell=True.
    """
    original = text or ""
    s = original.strip()
    vars_map = _machine_vars()

    if "`" in s:
        raise ValueError("Les substitutions avec backticks ne sont pas supportées. Utilise $NPROC ou $(nproc).")

    def repl_cmd(match: re.Match) -> str:
        inner = " ".join(match.group(1).strip().split())
        if inner in {"nproc", "nproc --all"}:
            return vars_map["NPROC"]
        raise ValueError(
            f"Substitution shell non supportée: $({inner}). "
            "BenchSuite accepte seulement $(nproc) et $(nproc --all) en mode sécurisé."
        )

    s = re.sub(r"\$\(([^)]+)\)", repl_cmd, s)

    # Variables BenchSuite dédiées, puis environnement utilisateur standard.
    for k, v in vars_map.items():
        s = s.replace(f"${{{k}}}", v).replace(f"${k}", v)
    s = os.path.expandvars(s)
    return s, vars_map


def split_safe_cli_args(text: str) -> Tuple[List[str], Dict[str, str], str]:
    expanded, vars_map = expand_safe_cli(text)
    try:
        args = shlex.split(expanded, posix=True)
    except ValueError as e:
        raise ValueError(f"Syntaxe CLI invalide: {e}") from e

    if args and Path(args[0]).name == "stress-ng":
        args = args[1:]
    elif args and args[0] == "stress-ng":
        args = args[1:]

    bad = [tok for tok in args if tok in _SHELL_OPERATORS]
    if bad:
        raise ValueError(
            "Opérateur shell non supporté en mode sécurisé: " + ", ".join(bad) +
            ". Lance uniquement les options stress-ng, sans pipe/redirection/&&."
        )

    # Expande ~ dans les tokens de chemin sans toucher aux options normales.
    args = [os.path.expanduser(a) if a.startswith("~") else a for a in args]
    return args, vars_map, expanded


def build_stress_ng_cli_command(text: str) -> Tuple[List[str], Dict[str, str], str]:
    args, vars_map, expanded = split_safe_cli_args(text)
    if not args:
        raise ValueError("Aucune option CLI fournie pour stress-ng")
    return ["stress-ng"] + args, vars_map, expanded


def extract_timeout_seconds(args: List[str]) -> int | None:
    """Retourne --timeout en secondes quand c'est lisible simplement."""
    for i, tok in enumerate(args):
        val = None
        if tok == "--timeout" and i + 1 < len(args):
            val = args[i + 1]
        elif tok.startswith("--timeout="):
            val = tok.split("=", 1)[1]
        if not val:
            continue
        m = re.fullmatch(r"(\d+)([smhdSMHD]?)", val.strip())
        if not m:
            return None
        n = int(m.group(1)); unit = m.group(2).lower()
        return n * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}.get(unit, 1)
    return None
