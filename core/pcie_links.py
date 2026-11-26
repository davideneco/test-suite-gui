"""Enregistrement passif des liens PCIe PENDANT chaque test : les composants montent-ils bien à leur lien maximum ?

Un instantané au repos ne suffit pas : beaucoup de périphériques (GPU, NVMe…) baissent leur vitesse de lien quand ils ne travaillent pas
et ne remontent qu'en charge. On relève donc, toutes les ~2 s, vitesse (Gen) et largeur actuelles de chaque périphérique PCIe (sysfs, sans
droits), et on compare à son MAXIMUM matériel (`max_link_speed` / `max_link_width`) et à celui de son port parent (le plafond du slot).

Interprétation (aucun seuil inventé) :
 - largeur sous le maximum du périphérique pendant l'essentiel du test → anomalie « warn » (slot / riser / câblage) ;
 - vitesse maximale jamais atteinte → « info » (peut être normal si le périphérique était au repos) ; le verdict la relève en « warn »
   seulement si le périphérique est celui sollicité par le test (GPU pour un test GPU, NVMe pour un test stockage, carte réseau pour iperf3) ;
 - maximum inconnu → rien n'est évalué.
Best-effort : jamais d'exception vers l'appelant ; BENCHSUITE_NO_PCIE=1 désactive le relevé.
"""
import os
import re
import threading
import time
from typing import Dict, List, Optional

SYSFS = "/sys/bus/pci/devices"
WIDTH_SHARE = 0.8        # part des relevés sous la largeur maximale à partir de laquelle l'anomalie est « durable »
MAX_TRANSITIONS = 60     # changements de lien conservés par périphérique

_CLASS_LABELS = {"01": "Stockage", "02": "Réseau", "03": "GPU / affichage", "04": "Multimédia", "05": "Mémoire",
                 "0c": "Bus série / USB", "12": "Accélérateur", "0b": "Co-processeur"}


def _rd(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _gen(speed: str) -> int:
    """« 16.0 GT/s PCIe » → 4 ; 0 si inconnu."""
    m = re.match(r"^\s*([\d.]+)\s*GT/s", speed or "")
    if not m:
        return 0
    return {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5, 64.0: 6}.get(round(float(m.group(1)), 1), 0)


def _int(v: str) -> int:
    return int(v) if v.isdigit() else 0


def list_devices(base: str = None) -> List[str]:
    """Adresses des périphériques PCIe qui ont un lien (hors ponts et switchs de classe 06xx)."""
    out = []
    base = base or SYSFS
    try:
        for bdf in sorted(os.listdir(base)):
            d = os.path.join(base, bdf)
            if _rd(os.path.join(d, "class"))[2:4] == "06":
                continue
            if _rd(os.path.join(d, "current_link_speed")):
                out.append(bdf)
    except Exception:
        pass
    return out


def _parent(bdf: str, base: str) -> str:
    """Adresse du port parent (pont amont) d'un périphérique, '' si inconnu."""
    try:
        p = os.path.basename(os.path.dirname(os.path.realpath(os.path.join(base, bdf))))
        return p if p != bdf and os.path.isdir(os.path.join(base, p)) else ""
    except Exception:
        return ""


class PcieLinkRecorder:
    """Relève les liens PCIe en arrière-plan entre ``start()`` et ``stop()`` (jamais d'exception)."""

    def __init__(self, interval: float = 2.0, base: str = None):
        self.interval, self.base = interval, (base or SYSFS)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._t0 = 0.0
        self._lock = threading.Lock()
        self._devs: Dict[str, dict] = {}

    def _sample(self, now: float) -> None:
        for bdf, e in self._devs.items():
            d = os.path.join(self.base, bdf)
            gen, w = _gen(_rd(os.path.join(d, "current_link_speed"))), _int(_rd(os.path.join(d, "current_link_width")))
            if not gen or not w:
                continue
            with self._lock:
                e["n"] += 1
                e["obs"][(gen, w)] = e["obs"].get((gen, w), 0) + 1
                if e["last"] != (gen, w):
                    if len(e["transitions"]) < MAX_TRANSITIONS:
                        e["transitions"].append([round(now - self._t0, 1), gen, w])
                    e["last"] = (gen, w)

    def _loop(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                self._sample(started)
            except Exception:
                pass
            self._stop.wait(max(0.2, self.interval - (time.time() - started)))

    def start(self) -> "PcieLinkRecorder":
        """Recense les périphériques (maximum du périphérique et de son port parent) puis démarre le relevé."""
        try:
            self._t0 = time.time()
            for bdf in list_devices(self.base):
                d = os.path.join(self.base, bdf)
                par = _parent(bdf, self.base)
                pd = os.path.join(self.base, par) if par else ""
                self._devs[bdf] = {
                    "classe": _rd(os.path.join(d, "class"))[2:6].lower(),
                    "gen_max": _gen(_rd(os.path.join(d, "max_link_speed"))), "w_max": _int(_rd(os.path.join(d, "max_link_width"))),
                    "port": par,
                    "port_gen_max": _gen(_rd(os.path.join(pd, "max_link_speed"))) if pd else 0,
                    "port_w_max": _int(_rd(os.path.join(pd, "max_link_width"))) if pd else 0,
                    "n": 0, "obs": {}, "transitions": [], "last": None}
            if self._devs:
                self._sample(self._t0)
                self._thread = threading.Thread(target=self._loop, daemon=True)
                self._thread.start()
        except Exception:
            self._devs = {}
        return self

    def stop(self) -> dict:
        """Arrête le relevé et retourne le résumé ({} si aucun périphérique PCIe lisible)."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
        try:
            with self._lock:
                devs = {k: dict(v, obs=dict(v["obs"]), transitions=list(v["transitions"])) for k, v in self._devs.items()}
            return summarize(devs, time.time() - self._t0 if self._t0 else 0.0)
        except Exception:
            return {}


def summarize(devs: Dict[str, dict], duration: float) -> dict:
    """Résumé par périphérique : lien maximum, plafond du port, liens observés (part du temps), anomalies."""
    rows: List[dict] = []
    for bdf, e in devs.items():
        n = e.get("n", 0)
        if not n:
            continue
        gmax, wmax = e["gen_max"], e["w_max"]
        obs = sorted(e["obs"].items(), key=lambda kv: (-kv[0][0], -kv[0][1]))
        best_g = max(g for (g, _), _c in obs)
        best_w = max(w for (_, w), _c in obs)
        at_max = sum(c for (g, w), c in obs if (not gmax or g >= gmax) and (not wmax or w >= wmax))
        low_w = sum(c for (_, w), c in obs if wmax and w < wmax)
        flags: List[dict] = []
        port_limit = bool((e["port_gen_max"] and gmax and e["port_gen_max"] < gmax) or (e["port_w_max"] and wmax and e["port_w_max"] < wmax))
        cause = ""
        if port_limit:
            cause = f" — le port parent est limité à Gen{e['port_gen_max'] or '?'} x{e['port_w_max'] or '?'}"
        if wmax and low_w / n >= WIDTH_SHARE:
            flags.append({"level": "warn", "code": "largeur",
                          "label": f"largeur x{min(w for (_, w), _c in obs)} au lieu de x{wmax} (maximum du périphérique)" + cause})
        elif wmax and low_w:
            flags.append({"level": "info", "code": "largeur_transitoire",
                          "label": f"largeur passée sous x{wmax} pendant {100 * low_w / n:.0f} % du test"})
        if gmax and best_g < gmax:
            flags.append({"level": "info", "code": "vitesse",
                          "label": f"vitesse maximale observée Gen{best_g} au lieu de Gen{gmax}" + cause +
                                   " (peut être normal si le périphérique était au repos)"})
        rows.append({"bus": bdf, "classe": e["classe"], "type": _CLASS_LABELS.get(e["classe"][:2], "Autre"),
                     "gen_max": gmax, "largeur_max": wmax, "port_parent": e["port"],
                     "port_gen_max": e["port_gen_max"], "port_largeur_max": e["port_w_max"],
                     "gen_obs_max": best_g, "largeur_obs_max": best_w, "releves": n,
                     "pct_au_maximum": round(100 * at_max / n, 1) if (gmax or wmax) else None,
                     "liens": [{"gen": g, "largeur": w, "pct": round(100 * c / n, 1)} for (g, w), c in obs],
                     "transitions": e["transitions"], "anomalies": flags})
    if not rows:
        return {}
    _add_names(rows)
    rows.sort(key=lambda r: (not any(f["level"] == "warn" for f in r["anomalies"]), not r["anomalies"], r["bus"]))
    return {"duree_s": round(duration, 1), "periphériques": rows,
            "anomalies_warn": sum(1 for r in rows if any(f["level"] == "warn" for f in r["anomalies"])),
            "au_maximum": sum(1 for r in rows if not r["anomalies"] and (r["gen_max"] or r["largeur_max"])),
            "non_evalues": sum(1 for r in rows if not (r["gen_max"] or r["largeur_max"]))}


def _add_names(rows: List[dict]) -> None:
    """Nom et pilote lisibles (lspci / pci.ids) quand disponibles ; sinon identifiants (best-effort)."""
    try:
        from core.system_info import get_pcie_devices, name_pcie_devices
        by_bus = {d["bus"]: d for d in name_pcie_devices(get_pcie_devices())}
        for r in rows:
            d = by_bus.get(r["bus"]) or {}
            if d.get("nom"):
                r["nom"] = d["nom"]
            if d.get("pilote"):
                r["pilote"] = d["pilote"]
            if d.get("type"):
                r["type"] = d["type"]
    except Exception:
        pass
