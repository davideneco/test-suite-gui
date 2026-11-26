"""Adaptation de l'interface à la taille de l'écran (petits écrans 800x600, 1024x768…).

Principe : jamais de taille de fenêtre supérieure à l'écran, mode « compact » (marges et polices réduites) sur petit écran,
et repli sûr si la taille de l'écran est inconnue (comportement habituel). Variables d'environnement utiles :
  BENCHSUITE_COMPACT=1|0   force / interdit le mode compact
  BENCHSUITE_SCREEN=LxH    force la taille d'écran supposée (tests, écrans mal détectés)
"""
import os
import re
from typing import Tuple

SMALL_W = 1366      # en dessous : mode compact
SMALL_H = 800
TINY_W = 1024       # en dessous (ou hauteur ≤ TINY_H) : fenêtre ouverte agrandie
TINY_H = 700


def screen_size() -> Tuple[int, int]:
    """(largeur, hauteur) en pixels du plus petit moniteur connu ; (0, 0) si inconnue. Ne lève jamais."""
    forced = os.environ.get("BENCHSUITE_SCREEN", "")
    m = re.match(r"^\s*(\d{3,5})\s*[xX]\s*(\d{3,5})\s*$", forced)
    if m:
        return int(m.group(1)), int(m.group(2))
    try:
        import gi
        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk
        display = Gdk.Display.get_default()
        if display is None:
            return 0, 0
        sizes = []
        monitors = display.get_monitors()
        for i in range(monitors.get_n_items()):
            g = monitors.get_item(i).get_geometry()
            if g.width > 0 and g.height > 0:
                sizes.append((g.width, g.height))
        if not sizes:
            return 0, 0
        return min(w for w, _ in sizes), min(h for _, h in sizes)
    except Exception:
        return 0, 0


def fit_size(width: int, height: int, margin_w: int = 40, margin_h: int = 90) -> Tuple[int, int]:
    """Taille souhaitée ramenée à l'écran (marges pour barres de titre / panneaux) ; inchangée si l'écran est inconnu."""
    sw, sh = screen_size()
    if sw <= 0 or sh <= 0:
        return width, height
    w = min(width, max(480, sw - margin_w))
    h = min(height if height > 0 else sh, max(360, sh - margin_h))
    return w, (h if height > 0 else height)


def is_small_screen() -> bool:
    """Vrai si le mode compact doit être utilisé (écran inférieur à 1366x800, ou BENCHSUITE_COMPACT=1)."""
    forced = os.environ.get("BENCHSUITE_COMPACT", "")
    if forced in ("0", "1"):
        return forced == "1"
    sw, sh = screen_size()
    return bool(sw and sh and (sw < SMALL_W or sh < SMALL_H))


def is_tiny_screen() -> bool:
    """Vrai pour les très petits écrans (≤ 1024 de large ou ≤ 700 de haut) : la fenêtre s'ouvre agrandie."""
    sw, sh = screen_size()
    return bool(sw and sh and (sw <= TINY_W or sh <= TINY_H))
