"""Modèle commun de la nouvelle CLI : ce qu'on peut lancer (profils, tests), leurs paramètres modifiables et l'étape de moteur correspondante.

Utilisé par l'écran interactif (`tui.py`), le menu numéroté (`plain.py`) et les commandes courtes (`commands.py`) : une seule source de
vérité, calquée sur les pages de la GUI (CPU + RAM, Stockage, GPU, Réseau). Aucun accès matériel à l'import ; tout est best-effort.
"""
import functools
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

PAGES = [("seq", "Séquences"), ("cpu", "CPU + RAM"), ("fio", "Stockage"), ("gpu", "GPU"), ("net", "Réseau"), ("outils", "Outils")]

_LABELS = {
    "time_s": "Durée (s)", "runtime_s": "Durée (s)", "cpu_workers": "Threads CPU", "cpu_stressor": "Méthode CPU",
    "vm_workers": "Workers mémoire", "vm_bytes": "Mémoire / worker", "cpu_load_pct": "Charge cible %",
    "target_gpus": "GPU ciblés (vide = tous)", "vram_pct": "VRAM utilisée %", "mode": "Mode", "verification": "Vérification",
    "precision": "Précision", "method": "Méthode", "buffer_size_mb": "Tampon (Mo)", "iterations": "Itérations", "level": "Niveau",
    "tests": "Tests", "extra_args": "Options supplémentaires", "test_mode": "Mode de test", "host": "Serveur iperf3 (IP)",
    "port": "Port", "bind": "Port d'envoi (IP locale -B)", "parallel": "Flux parallèles", "reverse": "Sens inversé (-R)", "bandwidth": "Débit cible (UDP)",
    "threads": "Threads", "array_mb": "Tableau (Mo, 0 = auto)", "ntimes": "Répétitions", "fail_early": "Arrêt au premier échec",
    "pcie_auto": "Seuil PCIe automatique", "path": "Disque / dossier de DONNÉES",
}
_CHOICES = {
    "cpu_stressor": None,           # rempli depuis stress_profiles.STRESS_STRESSORS
    "mode": ["all", "compute", "memory"], "verification": ["max", "balanced", "fast"],
    "precision": ["float", "double", "tensor"], "method": ["auto", "nvbandwidth", "clpeak", "both"],
}
_HINTS = {
    "cpu_workers": "« auto » = tous les threads ; sinon un nombre", "vm_bytes": "ex. 512M, 2G, 70% (0 = aucun)",
    "target_gpus": "numéros séparés par des virgules (0,1) ; vide = tous", "host": "adresse IP de la machine qui fait serveur", "bind": "vide = route du système ; Tab propose les adresses de cette machine",
    "path": "JAMAIS le disque système : un disque de données (écrit dessus)",
}


_TOOL_CACHE: Dict[str, tuple] = {}
_VALID_CACHE: Dict[tuple, tuple] = {}


def forget_tool_cache() -> None:
    """Oublie les outils déjà cherchés (après un lancement ou une actualisation : un outil a pu être installé entre-temps)."""
    _TOOL_CACHE.clear()
    _VALID_CACHE.clear()
    try:
        from core.benchmarks import forget_tool_cache as _core_forget
        _core_forget()
    except Exception:
        pass


@dataclass
class Param:
    """Un réglage modifiable d'un élément. kind : int · str · bool · choice."""
    key: str
    label: str
    kind: str
    value: Any
    default: Any
    choices: List[str] = field(default_factory=list)
    hint: str = ""
    suggest: List[str] = field(default_factory=list)      # valeurs proposées (ex. disques de données)

    def dirty(self) -> bool:
        return self.value != self.default

    def display(self) -> str:
        if self.kind == "bool":
            return "oui" if self.value else "non"
        v = "" if self.value is None else str(self.value)
        return v if v != "" else "(vide)"

    def parse(self, text: str) -> Tuple[bool, Any]:
        """Convertit la saisie ; retourne (ok, valeur ou message d'erreur)."""
        t = (text or "").strip()
        if self.kind == "int":
            try:
                if not re.fullmatch(r"-?\d+", t):
                    return False, "entier attendu"
                return True, int(t)
            except Exception:
                return False, "entier attendu"
        if self.kind == "bool":
            if t.lower() in ("1", "oui", "o", "yes", "y", "true", "vrai"):
                return True, True
            if t.lower() in ("0", "non", "n", "no", "false", "faux"):
                return True, False
            return False, "oui ou non"
        if self.kind == "choice" and self.choices and t not in self.choices:
            return False, "choix : " + ", ".join(self.choices[:8])
        return True, t

    def cycle(self, step: int = 1) -> None:
        """Choix suivant (kind choice) ou bascule (bool) ; sans effet pour les autres types."""
        if self.kind == "bool":
            self.value = not self.value
        elif self.kind == "choice" and self.choices:
            i = self.choices.index(self.value) if self.value in self.choices else -1
            self.value = self.choices[(i + step) % len(self.choices)]
        elif self.kind == "str" and self.suggest:
            i = self.suggest.index(self.value) if self.value in self.suggest else -1
            self.value = self.suggest[(i + step) % len(self.suggest)]


@dataclass
class Item:
    """Un élément lançable : profil stress-ng, profil fio, test du catalogue ou action (serveur iperf3)."""
    key: str
    label: str
    page: str
    kind: str               # stress · fio · bench · action
    ref: str                # identifiant du profil / du test
    group: str = ""
    desc: str = ""
    needs: List[str] = field(default_factory=list)
    params: List[Param] = field(default_factory=list)
    standard: str = ""

    def get(self, key: str):
        return next((p.value for p in self.params if p.key == key), None)

    def missing_tools(self) -> List[str]:
        """Outils absents (résultat mémorisé 15 s : la recherche d'un outil parcourt des dossiers et lance des commandes, ce qui faisait ramer
        l'écran à chaque touche — l'installation d'un outil est prise en compte au plus tard 15 s plus tard, ou immédiatement après un lancement)."""
        try:
            from core.benchmarks import tool_exists
            now = time.monotonic()
            out = []
            for t in self.needs:
                hit = _TOOL_CACHE.get(t)
                if hit is None or now - hit[0] > 15.0:
                    hit = (now, bool(tool_exists(t)))
                    _TOOL_CACHE[t] = hit
                if not hit[1]:
                    out.append(t)
            return out
        except Exception:
            return []

    def validate(self) -> str:
        """Message d'erreur si l'élément ne peut pas être lancé tel quel ('' si tout va bien)."""
        miss = self.missing_tools()
        if miss:
            return "outil manquant : " + ", ".join(miss)
        key = (self.key, tuple((p.key, str(p.value)) for p in self.params))
        hit = _VALID_CACHE.get(key)
        now = time.monotonic()
        if hit and now - hit[0] < 3.0:          # contrôle des disques / outils : au plus toutes les 3 s pour un même réglage (écran fluide)
            return hit[1]
        msg = self._validate()
        _VALID_CACHE[key] = (now, msg)
        return msg

    def _validate(self) -> str:
        miss = self.missing_tools()
        if miss:
            return "outil manquant : " + ", ".join(miss)
        if self.kind == "fio":
            path = str(self.get("path") or "").strip()
            if not path:
                return "choisissez le disque / dossier de DONNÉES (jamais le disque système)"
            try:
                from core.safety import check_targets
                level, msg = check_targets([path], raw_device=path.startswith("/dev/"), writes=True)
                if level == "block":
                    return msg
            except Exception:
                pass
        if self.kind == "suite" and any(p.key == "fio_path" for p in self.params):
            path = str(self.get("fio_path") or "").strip()
            if not path:
                return "test de stockage : choisissez le disque / dossier de DONNÉES (jamais le disque système)"
            try:
                from core.safety import check_targets
                level, msg = check_targets([path], raw_device=path.startswith("/dev/"), writes=True)
                if level == "block":
                    return msg
            except Exception:
                pass
        if self.ref in ("net_iperf_tcp", "net_iperf_udp"):
            if not str(self.get("host") or "").strip():
                return "indiquez l'adresse IP du serveur iperf3 (l'autre machine : page « Serveur iperf3 » ou `iperf3 -s`)"
        return ""

    def step(self) -> Dict:
        """Étape du moteur (core.runner) correspondant aux réglages courants : seuls les paramètres modifiés sont envoyés."""
        changed = {p.key: p.value for p in self.params if p.dirty()}
        if self.kind == "stress":
            st: Dict = {"stress": self.ref}
            if "time_s" in changed:
                st["time_s"] = int(changed.pop("time_s"))
            for k in ("cpu_workers", "vm_workers", "cpu_load_pct"):      # « auto » reste une chaîne, les nombres deviennent des entiers
                if k in changed and isinstance(changed[k], str) and re.fullmatch(r"\d+", changed[k]):
                    changed[k] = int(changed[k])
            if changed:
                st["params"] = changed
            return st
        if self.kind == "fio":
            st = {"fio": self.ref, "path": str(self.get("path") or "").strip()}
            t = self.get("time_s")
            if isinstance(t, int) and t > 0:
                st["time_s"] = t
            return st
        if self.kind == "bench":
            st = {"bench": self.ref}
            if changed:
                st["params"] = changed
            return st
        return {}

    def command_hint(self) -> str:
        """Commande courte équivalente (affichée dans l'aide de l'écran interactif)."""
        if self.kind == "action":
            return {"iperf_server": "./cli.py net serveur", "ports": "./cli.py net ports", "deps": "./cli.py outils"}.get(self.ref, "./cli.py aide")
        if self.kind == "suite":
            fp = str(self.get("fio_path") or "").strip()
            return f"./cli.py seq {self.ref}" + (f" {fp}" if fp else "")
        return f"./cli.py lancer {self.key}" + "".join(f" {p.key}={p.value}" for p in self.params if p.dirty() and p.kind != "bool"
                                                       and " " not in str(p.value))


# ── Durées ────────────────────────────────────────────────────────────────────

def parse_duration(text: str) -> Optional[int]:
    """« 5m », « 30min », « 4h », « 90s », « 90 » → secondes ; None si illisible."""
    t = (text or "").strip().lower().replace(" ", "")
    m = re.fullmatch(r"(\d+(?:[.,]\d+)?)(s|sec|m|min|h|heure|heures)?", t)
    if not m:
        return None
    n = float(m.group(1).replace(",", "."))
    unit = m.group(2) or "s"
    mult = 3600 if unit.startswith("h") else 60 if unit.startswith("m") else 1
    return int(n * mult)


# ── Disques de données (stockage) ─────────────────────────────────────────────

def data_paths() -> List[Tuple[str, str]]:
    """[(point de montage, périphérique)] des disques de DONNÉES montés (jamais ceux qui portent le système), best-effort."""
    out: List[Tuple[str, str]] = []
    try:
        from core.safety import os_disks, whole_disks_of
        sysd = os_disks()
        skip = ("/boot", "/snap", "/sys", "/proc", "/dev", "/run", "/var/lib/docker", "/var/snap")
        with open("/proc/mounts") as f:
            for line in f:
                dev, mnt = line.split()[:2]
                if not dev.startswith("/dev/") or any(mnt == s or mnt.startswith(s + "/") for s in skip) or mnt == "/":
                    continue
                disks = whole_disks_of(os.path.basename(dev))
                if disks and not (disks & sysd) and os.access(mnt, os.W_OK):
                    out.append((mnt, dev))
    except Exception:
        pass
    return out


# ── Construction des éléments ─────────────────────────────────────────────────

def _kind_of(v: Any) -> str:
    return "bool" if isinstance(v, bool) else "int" if isinstance(v, int) else "str"


def _param(key: str, value: Any, hint: str = "") -> Param:
    kind = _kind_of(value)
    choices: List[str] = []
    if key in _CHOICES:
        ch = _CHOICES[key]
        if ch is None and key == "cpu_stressor":
            try:
                from core.stress_profiles import STRESS_STRESSORS
                ch = list(STRESS_STRESSORS)
            except Exception:
                ch = []
        if ch:
            choices, kind = list(ch), "choice"
            if value not in choices:
                choices.insert(0, value)
    return Param(key, _LABELS.get(key, key), kind, value, value, choices, _HINTS.get(key, hint))


def _cpu_items() -> List[Item]:
    items: List[Item] = []
    try:
        from core.stress_profiles import get_all_profiles, describe_params
        for p in get_all_profiles():
            if p.get("id") in ("stress_custom", "stress_ng_cli_only"):
                continue        # le mode « tout modifiable » est l'écran de paramètres de chaque profil
            pr = p.get("params", {})
            keys = [k for k in ("time_s", "cpu_workers", "cpu_stressor", "vm_workers", "vm_bytes", "cpu_load_pct") if k in pr]
            items.append(Item(p["id"], p["name"], "cpu", "stress", p["id"], p.get("category", ""),
                              p.get("description", "") or describe_params(pr), ["stress-ng"], [_param(k, pr[k]) for k in keys]))
    except Exception:
        pass
    try:
        from core.benchmarks import CATALOG
        for b in CATALOG:
            if b["id"] == "ram_stream":
                items.append(Item("ram_stream", b["name"], "cpu", "bench", b["id"], "Bande passante mémoire", b.get("desc", ""),
                                  list(b.get("requires", [])), [_param(k, v) for k, v in b["params"].items()]))
    except Exception:
        pass
    return items


def _fio_items() -> List[Item]:
    items: List[Item] = []
    try:
        from core.fio_profiles import get_all_profiles, FIO_TABS, profile_tab, profile_standard
        tabs = {t[0]: t[2] for t in FIO_TABS}
        order = {t[0]: i for i, t in enumerate(FIO_TABS)}
        sugg = [m for m, _d in data_paths()]
        for p in sorted(get_all_profiles(), key=lambda x: (order.get(profile_tab(x), 99), x.get("name", ""))):
            path = Param("path", _LABELS["path"], "str", sugg[0] if sugg else "", sugg[0] if sugg else "", [], _HINTS["path"], sugg)
            items.append(Item(p["id"], p["name"], "fio", "fio", p["id"], tabs.get(profile_tab(p), "Autres"), p.get("description", ""),
                              ["fio"], [path, Param("time_s", "Durée (s, 0 = celle du profil)", "int", 0, 0)], profile_standard(p) or ""))
    except Exception:
        pass
    return items


def _gpu_items() -> List[Item]:
    items: List[Item] = []
    try:
        from core.benchmarks import CATALOG
        for b in CATALOG:
            if b["category"] == "GPU":
                items.append(Item(b["id"], b["name"], "gpu", "bench", b["id"], "", b.get("desc", ""), list(b.get("requires", [])),
                                  [_param(k, v) for k, v in b["params"].items()]))
    except Exception:
        pass
    return items


def _net_items() -> List[Item]:
    items: List[Item] = []
    try:
        from core.benchmarks import CATALOG
        for b in CATALOG:
            if b["category"] == "Réseau":
                it = Item(b["id"], b["name"], "net", "bench", b["id"], "Client (cette machine → serveur)", b.get("desc", ""),
                          list(b.get("requires", [])), [_param(k, v) for k, v in b["params"].items()])
                for prm in it.params:
                    if prm.key == "bind":                       # Tab : adresses IPv4 de cette machine (un port = une adresse)
                        try:
                            from core.iperf_server import local_addresses
                            prm.suggest = [""] + [a["ip"] for a in local_addresses()]
                        except Exception:
                            pass
                items.append(it)
    except Exception:
        pass
    items.append(Item("net_serveur", "Serveur iperf3 (cette machine fait serveur)", "net", "action", "iperf_server",
                      "Serveur (l'autre machine lance le test client)",
                      "Démarre `iperf3 -s` sur cette machine et affiche l'adresse IP à donner à l'autre machine. Ctrl-C pour arrêter.",
                      ["iperf3"], [Param("port", _LABELS["port"], "int", 5201, 5201)]))
    return items


def _has_fio(steps) -> bool:
    return any(("fio" in s) or _has_fio(s.get("parallel", [])) for s in steps if isinstance(s, dict))


def _seq_items() -> List[Item]:
    """Séquences automatiques (suites/*.json livrées + ~/.benchsuite/suites) : mêmes séquences que `--suite` et que l'onglet de la version graphique."""
    items: List[Item] = []
    try:
        from core.runner import list_suites, load_suite, step_label
        for s in list_suites():
            try:
                suite = load_suite(s["id"])
            except Exception as exc:
                items.append(Item("seq_" + s["id"], s["name"] + "  (invalide)", "seq", "suite", s["id"], "Séquences", f"Séquence illisible : {exc}"))
                continue
            steps = suite.get("steps", [])
            labels = []
            for st in steps:
                try:
                    labels.append(step_label(st))
                except Exception:
                    labels.append("?")
            desc = (suite.get("description") or "").strip()
            desc = f"{len(steps)} étape(s) : " + " → ".join(labels) + ("\n" + desc if desc else "")
            params = [Param("cooldown", "Pause entre les étapes (s)", "int", int(suite.get("cooldown_s", 0) or 0), int(suite.get("cooldown_s", 0) or 0))]
            if _has_fio(steps):
                params.insert(0, Param("fio_path", _LABELS["path"], "str", "", "", [], _HINTS["path"], data_paths()))
            own = "/.benchsuite/" in s.get("path", "")
            items.append(Item("seq_" + s["id"], suite.get("name", s["id"]), "seq", "suite", s["id"], "Vos séquences" if own else "Séquences livrées", desc, [], params))
    except Exception:
        pass
    return items


def _tool_items() -> List[Item]:
    return [Item("outils_deps", "Outils manquants et commandes d'installation", "outils", "action", "deps", "Diagnostic",
                 "Liste ce qui est installé et ce qui manque sur CETTE machine, avec les commandes à copier (apt). Lecture seule : rien n'est installé."),
            Item("outils_ports", "Ports Ethernet et adresses", "outils", "action", "ports", "Diagnostic",
                 "Tous les ports réseau physiques, même sans adresse, et les commandes pour relier deux machines en direct.")]


_BUILDERS = {"seq": _seq_items, "cpu": _cpu_items, "fio": _fio_items, "gpu": _gpu_items, "net": _net_items, "outils": _tool_items}
_CACHE: Dict[str, List[Item]] = {}


def items_for(page: str, fresh: bool = False) -> List[Item]:
    """Éléments d'une page (CPU + RAM, Stockage, GPU, Réseau). Mis en cache : les réglages modifiés sont conservés pendant la session."""
    if fresh or page not in _CACHE:
        _CACHE[page] = _BUILDERS.get(page, lambda: [])()
    return _CACHE[page]


def all_items() -> List[Item]:
    return [i for p, _ in PAGES for i in items_for(p)]


def find_item(key: str) -> Optional[Item]:
    """Retrouve un élément par identifiant exact, sinon par préfixe unique, sinon par fin (ex. « oclburn » → gpu_oclburn)."""
    key = (key or "").strip().lower()
    items = all_items()
    for i in items:
        if i.key.lower() == key:
            return i
    cand = [i for i in items if i.key.lower().startswith(key) or i.key.lower().endswith("_" + key)]
    return cand[0] if len(cand) == 1 else None


@functools.lru_cache(maxsize=64)
def install_commands(tool: str) -> List[Tuple[str, str]]:
    """[(famille, commande)] d'installation d'un outil."""
    try:
        from core.tools_catalog import TOOLS
        for t in TOOLS:
            if tool in (t.get("id"), t.get("name")) or tool in (t.get("check") or {}).get("bins", []):
                return [(x["label"], x["cmd"]) for x in t.get("install", [])]
    except Exception:
        pass
    return []


@functools.lru_cache(maxsize=1)
def read_version() -> str:
    """Version affichée (lue dans main.py sans l'importer : pas de GTK nécessaire). Chaîne vide si illisible."""
    try:
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py"), encoding="utf-8") as f:
            for line in f:
                m = re.match(r'^VERSION\s*=\s*"([^"]+)"', line)
                if m:
                    return m.group(1).split()[0]
    except Exception:
        pass
    return ""
