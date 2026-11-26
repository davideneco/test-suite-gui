# -*- coding: utf-8 -*-
"""Gestion de l'historique persistant des résultats (~/.benchsuite_history.json)."""

import json
import os
from datetime import datetime
from typing import List, Optional

from core.benchmarks import BenchmarkResult
from exporters.common import compact_raw_output


HISTORY_FILE = os.path.expanduser("~/.benchsuite_history.json")


HISTORY_MAX_ENTRIES = 500
HISTORY_RAW_MAX = 20000          # sortie brute gardée dans l'historique (caractères, fin du texte)


def _write_history(entries: List[dict]) -> None:
    """Écriture ATOMIQUE de l'historique (fichier temporaire puis renommage) : une coupure en pleine écriture ne
    laisse jamais un fichier tronqué, que load_history lirait comme vide et que la sauvegarde suivante écraserait."""
    if not entries:
        if os.path.exists(HISTORY_FILE):
            os.remove(HISTORY_FILE)
        return
    tmp = HISTORY_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False)
    os.replace(tmp, HISTORY_FILE)


def save_to_history(results: List[BenchmarkResult]) -> None:
    existing = load_history()
    for r in results:
        d = r.to_dict()
        d["raw_output"] = compact_raw_output(d.get("raw_output") or "", HISTORY_RAW_MAX)   # définie plus bas : appelée à l'exécution
        existing.append(d)
    _write_history(existing[-HISTORY_MAX_ENTRIES:])


def load_history() -> List[dict]:
    """Entrées de l'historique (les plus anciennes d'abord). Un fichier illisible est mis de côté
    (~/.benchsuite_history.json.illisible-<date>) au lieu d'être écrasé par la sauvegarde suivante."""
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return [e for e in data if isinstance(e, dict)] if isinstance(data, list) else []
    except Exception:
        try:
            os.replace(HISTORY_FILE, f"{HISTORY_FILE}.illisible-{datetime.now().strftime('%Y%m%d_%H%M%S')}")
        except Exception:
            pass
        return []


def history_result(item: dict) -> Optional[BenchmarkResult]:
    """Convertit une entrée d'historique en BenchmarkResult, y compris les entrées d'anciennes versions
    (champs absents ou à null : metrics / system_info / command / notes…). None si l'entrée est inexploitable."""
    try:
        return BenchmarkResult(
            name=str(item.get("name") or "?"), category=str(item.get("category") or "?"),
            score=item.get("score"), unit=str(item.get("unit") or ""),
            duration_s=float(item.get("duration_s") or 0), raw_output=str(item.get("raw_output") or ""),
            metrics=item.get("metrics") if isinstance(item.get("metrics"), dict) else {},
            status=str(item.get("status") or "ok"), timestamp=str(item.get("timestamp") or ""),
            system_info=item.get("system_info") if isinstance(item.get("system_info"), dict) else {},
            notes=str(item.get("notes") or ""), command=str(item.get("command") or ""))
    except Exception:
        return None


def clear_history() -> None:
    if os.path.exists(HISTORY_FILE):
        os.remove(HISTORY_FILE)


def delete_history_indices(indices) -> int:
    """Supprime plusieurs entrées (index dans l'ordre du fichier) en UNE lecture / écriture. Retourne le nombre supprimé."""
    existing = load_history()
    drop = {i for i in indices if 0 <= i < len(existing)}
    if not drop:
        return 0
    _write_history([e for i, e in enumerate(existing) if i not in drop])
    return len(drop)
