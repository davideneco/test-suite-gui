"""Thème clair / sombre / automatique de l'interface graphique (aucun import GTK : testable sans interface).

Le thème d'origine est sombre (noir / orange, voir `CSS` dans main.py). Le thème clair n'est PAS une seconde feuille de style écrite à la main :
il est déduit de la première en inversant la LUMINOSITÉ de chaque couleur (teinte et saturation conservées) — un fond #0d0d0d devient #f2f2f2,
un texte #f0f0f0 devient #0f0f0f, l'orange d'accent (luminosité moyenne) reste orange, les vert / rouge / jaune pastel deviennent assez foncés
pour être lisibles sur fond clair. Une nouvelle règle ajoutée au thème sombre est donc automatiquement prise en compte en clair.
Choix mémorisé dans ~/.benchsuite/settings.json ; BENCHSUITE_THEME=dark|light|auto l'impose sans le modifier. Best-effort : jamais d'exception.
"""
import colorsys
import json
import os
import re

SETTINGS_FILE = os.path.expanduser("~/.benchsuite/settings.json")
MODES = ("dark", "light", "auto")
LABELS = {"dark": "🌙 Sombre", "light": "☀ Clair", "auto": "🖥 Auto"}

# Texte blanc sur fond orange (boutons d'action) : doit rester blanc, l'inversion le rendrait noir.
# Même chose pour le rapport HTML : texte blanc sur fond d'accent (onglet actif, boutons) et sur les cartes de score.
HTML_LIGHT_EXTRA = ".tab.active,.tab.active:hover,.filter-btn.active{color:#fff}"

LIGHT_EXTRA = """
.btn-run, .btn-run label, .btn-primary-big, .btn-primary-big label { color: #ffffff; }
button:active, .sidebar-item:active { color: #000000; }
.log-view, .log-view text { color: #1b6e20; }
"""

_HEX = re.compile(r"#([0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")
_RGBA = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*(?:,\s*([\d.]+)\s*)?\)")


def _invert_rgb(r: int, g: int, b: int):
    """Inverse la luminosité (HLS) d'une couleur 0-255 en gardant teinte et saturation."""
    h, l, s = colorsys.rgb_to_hls(r / 255.0, g / 255.0, b / 255.0)
    r2, g2, b2 = colorsys.hls_to_rgb(h, 1.0 - l, s)
    return round(r2 * 255), round(g2 * 255), round(b2 * 255)


def _hex_sub(m) -> str:
    v = m.group(1)
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    r, g, b = _invert_rgb(int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rgba_sub(m) -> str:
    r, g, b = _invert_rgb(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return f"rgba({r}, {g}, {b}, {m.group(4)})" if m.group(4) is not None else f"rgb({r}, {g}, {b})"


def invert_css(css: str) -> str:
    """Inverse la luminosité de toutes les couleurs d'une feuille de style (sans correctif), pour d'autres sorties que la fenêtre GTK (rapport HTML)."""
    return _RGBA.sub(_rgba_sub, _HEX.sub(_hex_sub, css))


def light_css(css: str) -> str:
    """Version claire d'une feuille de style sombre (luminosité inversée) + corrections des textes sur fond d'accent."""
    return invert_css(css) + LIGHT_EXTRA


def get_mode() -> str:
    """Mode choisi : variable d'environnement, sinon réglage mémorisé, sinon « dark » (identité historique)."""
    env = os.environ.get("BENCHSUITE_THEME", "").strip().lower()
    if env in MODES:
        return env
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            v = str(json.load(f).get("theme", "")).lower()
        if v in MODES:
            return v
    except Exception:
        pass
    return "dark"


def set_mode(mode: str) -> bool:
    """Mémorise le mode (écriture atomique, autres réglages conservés). False si invalide ou non écrivable."""
    if mode not in MODES:
        return False
    try:
        data = {}
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            pass
        data["theme"] = mode
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except Exception:
        return False


def get_setting(key: str, default=None):
    """Lit un réglage d'interface mémorisé (ex. groupes du menu repliés). Best-effort : `default` en cas de problème."""
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            return (json.load(f) or {}).get(key, default)
    except Exception:
        return default


def set_setting(key: str, value) -> bool:
    """Mémorise un réglage d'interface (écriture atomique, autres réglages conservés)."""
    try:
        data = {}
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                data = json.load(f) or {}
        except Exception:
            pass
        data[key] = value
        os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
        tmp = SETTINGS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
        os.replace(tmp, SETTINGS_FILE)
        return True
    except Exception:
        return False


def next_mode(mode: str) -> str:
    """Mode suivant dans le cycle sombre → clair → auto."""
    return MODES[(MODES.index(mode) + 1) % len(MODES)] if mode in MODES else "dark"
