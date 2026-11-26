"""electrical.py — Enregistrement passif des capteurs électroniques pendant un test (pas un test en soi).

Pendant qu'un test tourne, ``ElectricalRecorder`` relève en tâche de fond tout ce que la machine expose :
tensions, courants, puissances, ventilateurs et températures de carte (VRM, PSU, entrée/sortie d'air…).
Sources lues en parallèle, car la carte mère n'expose pas toujours ses mesures au BMC :
Redfish, IPMI (SDR), DCMI, hwmon / lm-sensors (Super-I/O, PMBus), RAPL (puissance CPU/DRAM).
Les capteurs GPU ne sont pas repris ici (déjà suivis par la télémétrie GPU de chaque test).

Le résumé (min / moyenne / max par capteur, courbes sous-échantillonnées) est attaché au résultat du test et
repris dans les rapports HTML, PDF, CSV et JSON. Aucune note ni verdict : seules les tensions des rails fixes
lues par le BMC (sources calibrées) sont signalées si elles sortent de la tolérance ATX ±5 %, à titre d'information.
"""

import os
import re
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

TRUSTED_SOURCES = {"ipmi", "redfish", "i2c_pmbus"}      # sources dont l'échelle est calibrée
_BOARD_TEMP_RE = re.compile(r"vrm|psu|ps\d|inlet|outlet|ambient|board|system|pch|mos|chipset|exhaust|front|rear", re.I)
MAX_SERIES_KEYS = 16
MAX_SERIES_POINTS = 400


def rail_nominal(name: str, value: float) -> Optional[Tuple[float, str]]:
    """(tension nominale, étiquette) si le libellé désigne un rail fixe ATX plausible (valeur à ±25 %), sinon None."""
    n = (name or "").lower().replace("_", " ")
    if re.search(r"-\s*12\s*v|neg(ative)?\s*12", n):
        return None
    cand = None
    if re.search(r"5\s*v\s*sb|5vsb|5\s*v\s*stby|5\s*v\s*standby|5\s*v\s*aux", n):
        cand = (5.0, "5VSB")
    elif re.search(r"(?<![0-9.])3\.3\s*v|(?<![0-9.])3v3", n):
        cand = (3.3, "3,3V")
    elif re.search(r"(?<![0-9.])12\s*v", n):
        cand = (12.0, "12V")
    elif re.search(r"(?<![0-9.])5\s*v(?!\w*sb)", n):
        cand = (5.0, "5V")
    if cand and value and abs(value - cand[0]) / cand[0] <= 0.25:
        return cand
    return None


def _keep(m) -> bool:
    """Garde tensions, courants, puissances, ventilateurs et températures de carte (pas CPU/GPU/disque)."""
    if m.value is None or m.source == "nvidia":
        return False
    if m.category in ("voltage", "current", "power", "fan"):
        return True
    return m.category == "temperature" and bool(_BOARD_TEMP_RE.search(m.name or ""))


def _rapl_collector() -> Optional[Callable]:
    """Collecteur RAPL (puissance CPU/DRAM en W, dérivée de l'énergie) ; None si illisible (souvent root requis)."""
    from core.telemetry_aggregator import TelemetryMetric
    base = "/sys/class/powercap"
    try:
        zones = [z for z in os.listdir(base) if re.match(r"intel-rapl:\d+(:\d+)?$", z)]
        with open(os.path.join(base, zones[0], "energy_uj")) as f:
            f.read()
    except Exception:
        return None
    last: Dict[str, Tuple[float, float]] = {}

    def collect():
        out = []
        now = time.time()
        for z in zones:
            try:
                with open(os.path.join(base, z, "energy_uj")) as f:
                    e = float(f.read().strip())
                with open(os.path.join(base, z, "name")) as f:
                    nm = f.read().strip()
            except Exception:
                continue
            if z in last and now > last[z][0] and e >= last[z][1]:
                out.append(TelemetryMetric(name=f"RAPL {nm} ({z})", category="power",
                                           value=(e - last[z][1]) / 1e6 / (now - last[z][0]), unit="W", source="rapl"))
            last[z] = (now, e)
        return out
    return collect


class ElectricalRecorder:
    """Relève les capteurs électroniques en arrière-plan entre ``start()`` et ``stop()`` (jamais d'exception)."""

    def __init__(self, fast_interval: float = 2.0, slow_interval: float = 5.0):
        self.fast_interval, self.slow_interval = fast_interval, slow_interval
        self._lock = threading.Lock()
        self._samples: Dict[str, List[Tuple[float, float]]] = {}
        self._meta: Dict[str, dict] = {}
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._t0 = 0.0

    def _loop(self, fns: List[Callable], interval: float) -> None:
        """Boucle d'un groupe de collecteurs (rapides : hwmon/RAPL ; lents : BMC)."""
        while not self._stop.is_set():
            started = time.time()
            for fn in fns:
                try:
                    for m in fn() or []:
                        if _keep(m):
                            key = f"{m.source}|{m.name}"
                            with self._lock:
                                self._samples.setdefault(key, []).append((time.time() - self._t0, float(m.value)))
                                self._meta.setdefault(key, {"name": m.name, "category": m.category, "unit": m.unit,
                                                            "source": m.source})
                except Exception:
                    continue
            self._stop.wait(max(0.2, interval - (time.time() - started)))

    def start(self) -> "ElectricalRecorder":
        """Démarre les deux threads de relevé."""
        try:
            from core import telemetry_aggregator as ta
        except Exception:
            return self
        self._t0 = time.time()
        fast = [ta.collect_hwmon_telemetry]
        rapl = _rapl_collector()
        if rapl:
            fast.append(rapl)
        slow = [ta.collect_ipmi_telemetry, ta.collect_dcmi_power_telemetry, ta.collect_redfish_telemetry]
        for fns, iv in ((fast, self.fast_interval), (slow, self.slow_interval)):
            th = threading.Thread(target=self._loop, args=(fns, iv), daemon=True)
            th.start()
            self._threads.append(th)
        return self

    def stop(self) -> dict:
        """Arrête le relevé et retourne le résumé ({} si aucun capteur lisible)."""
        self._stop.set()
        for th in self._threads:
            th.join(timeout=8)
        with self._lock:
            samples = {k: list(v) for k, v in self._samples.items()}
            meta = dict(self._meta)
        return summarize(samples, meta, time.time() - self._t0 if self._t0 else 0.0)


def summarize(samples: Dict[str, List[Tuple[float, float]]], meta: Dict[str, dict], duration: float) -> dict:
    """Résumé par capteur (min / moyenne / max / début / fin) et courbes bornées en taille."""
    if not samples:
        return {}
    order = {"voltage": 0, "current": 1, "power": 2, "fan": 3, "temperature": 4}
    sensors, alerts = [], []
    for key, pts in samples.items():
        if not pts:
            continue
        m = meta[key]
        vals = [v for _, v in pts]
        mean = sum(vals) / len(vals)
        s = {"name": m["name"], "source": m["source"], "category": m["category"], "unit": m["unit"],
             "min": round(min(vals), 3), "mean": round(mean, 3), "max": round(max(vals), 3),
             "first": round(vals[0], 3), "last": round(vals[-1], 3), "samples": len(vals)}
        if m["category"] == "voltage":
            nom = rail_nominal(m["name"], mean)
            if nom:
                s["rail"], s["nominal"] = nom[1], nom[0]
                s["variation_pct"] = round((max(vals) - min(vals)) / mean * 100.0, 2) if mean else 0.0
                if m["source"] in TRUSTED_SOURCES:
                    dev = max(abs(min(vals) - nom[0]), abs(max(vals) - nom[0])) / nom[0] * 100.0
                    s["dev_nominal_pct"] = round(dev, 2)
                    if dev > 5.0:
                        s["flag"] = "hors tolérance ATX ±5 %"
                        alerts.append(f"{m['name']} ({m['source']}) : {min(vals):.2f}–{max(vals):.2f} V pour {nom[0]:g} V "
                                      f"(écart {dev:.1f} %, tolérance ATX ±5 %)")
        sensors.append(s)
    sensors.sort(key=lambda s: (order.get(s["category"], 9), s["name"]))
    # Courbes : rails fixes et puissances d'abord, puis ventilateurs, bornées en nombre et en points
    pri = sorted(samples, key=lambda k: (0 if rail_nominal(meta[k]["name"], samples[k][0][1]) else
                                         1 if meta[k]["category"] == "power" else
                                         2 if meta[k]["category"] == "voltage" else 3, meta[k]["name"]))
    step = max(1.0, duration / MAX_SERIES_POINTS)
    n = int(duration / step) + 1
    series: Dict[str, list] = {"t": [round(i * step, 1) for i in range(n)]}
    for key in pri[:MAX_SERIES_KEYS]:
        pts = sorted(samples[key])
        col, j, last = [], 0, None
        for i in range(n):
            t = i * step
            while j < len(pts) and pts[j][0] <= t:
                last = pts[j][1]
                j += 1
            col.append(round(last, 3) if last is not None else None)
        slug = re.sub(r"[^a-z0-9]+", "_", f"{meta[key]['source']}_{meta[key]['name']}".lower()).strip("_")
        series[slug] = col
    def chart_cat(k: str) -> str:
        """Groupe de graphique : rails fixes séparés des tensions variables (échelles très différentes)."""
        if meta[k]["category"] == "voltage":
            return "rail" if rail_nominal(meta[k]["name"], samples[k][0][1]) else "voltage"
        return meta[k]["category"]
    labels = {re.sub(r"[^a-z0-9]+", "_", f"{meta[k]['source']}_{meta[k]['name']}".lower()).strip("_"):
              {"name": meta[k]["name"], "unit": meta[k]["unit"], "category": chart_cat(k)} for k in pri[:MAX_SERIES_KEYS]}
    return {"sources": sorted({m["source"] for m in meta.values()}), "sensor_count": len(sensors),
            "duration_s": round(duration, 1), "sensors": sensors, "alerts": alerts, "series": series, "series_labels": labels}


def summary_cards(metrics: dict) -> List[Tuple[str, str]]:
    """Cartes courtes pour l'interface : relevé électronique et inventaire avant/après d'un résultat."""
    cards = []
    el = (metrics or {}).get("electrical") or {}
    if el.get("sensor_count"):
        txt = f"{el['sensor_count']} capteurs ({', '.join(el.get('sources', []))})"
        if el.get("alerts"):
            txt += f" — {len(el['alerts'])} hors tolérance"
        cards.append(("ÉLECTRONIQUE (RELEVÉ)", txt))
    inv = (metrics or {}).get("inventory_diff") or {}
    if inv:
        c = inv.get("counts", {})
        txt = "identique" if inv.get("identical") else f"{c.get('fail', 0)} anomalie(s), {c.get('warn', 0)} à vérifier"
        if inv.get("sel_new"):
            txt += f" — {len(inv['sel_new'])} événement(s) BMC"
        cards.append(("INVENTAIRE AVANT/APRÈS", txt))
    return cards
