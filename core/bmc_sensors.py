"""bmc_sensors.py — Capteurs RAM autonomes via BMC/IPMI, hwmon et lm-sensors.

Objectif: recuperer temperature/puissance DIMM quand la carte mere/BMC les expose,
sans casser l'application si les droits, modules ou outils sont absents.
"""

import os
import re
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Tuple


@dataclass
class MemorySensor:
    name: str
    kind: str              # temperature | power | voltage | current | other
    value: float
    unit: str
    source: str            # ipmi | sensors | hwmon
    raw: str = ""
    status: str = ""

    def __post_init__(self):
        st = (self.status or "").lower()
        if st in ("na", "nr", "ns", "disabled", "no sensor", "error", "cr"):
            self.value = None
            return

        if self.kind == "temperature" and self.value is not None:
            try:
                val = float(self.value)
                # Sentinelles courantes d'erreur BMC / capteur absent : 0, 255, 65535, -128
                if val <= 0.0 or val in (255.0, 65535.0, 32767.0, 4294967295.0) or val < -40.0:
                    self.value = None
                    return
                # Auto-échelle si le BMC renvoie la valeur en dixièmes de °C (ex: 550 -> 55.0°C)
                if 125.0 < val <= 1250.0:
                    val /= 10.0
                    self.status = (self.status + " auto_scaled_decic").strip()
                elif val > 125.0:
                    self.value = None
                    return
                if 2.0 <= val <= 120.0:
                    self.value = round(val, 1)
                    self.unit = "°C"
                else:
                    self.value = None
            except Exception:
                self.value = None

        elif self.kind == "power" and self.value is not None:
            try:
                self.value = float(self.value)
                u = (self.unit or "").lower()
                if "mw" in u or "milli" in u:
                    self.value /= 1000.0
                    self.unit = "W"
                elif "uw" in u or "micro" in u:
                    self.value /= 1000000.0
                    self.unit = "W"
                elif "kw" in u or "kilo" in u:
                    self.value *= 1000.0
                    self.unit = "W"
                else:
                    self.unit = "W"

                if self.value <= 0.0 or self.value in (65535.0, 4294967295.0):
                    self.value = None
                    return

                # Sanity check Best-Effort pour la RAM (Plafond physique ~250W par barrette/canal)
                # Ex: 1500 remonté via IPMI -> 15.0W (exposant -2 linéaire) ou 1.5W
                if self.value > 250.0:
                    if self.value >= 100000.0:
                        self.value /= 1000000.0
                        self.status = (self.status + " auto_scaled_uW").strip()
                    elif self.value >= 1000.0:
                        self.value /= 100.0
                        self.status = (self.status + " auto_scaled_cW").strip()
                    elif self.value > 250.0:
                        self.value /= 10.0
                        self.status = (self.status + " auto_scaled_dW").strip()

                if self.value > 250.0:
                    self.value = None
            except Exception:
                self.value = None


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _tool(name: str) -> bool:
    import shutil            # sans processus `which` (absent de certaines installations minimales)
    return shutil.which(name) is not None


# Patterns RAM haute confiance.
# Inclus explicitement : TEMP_DDR5_X1, POWER_DDR5_X1, PWR_DDR5_*, DIMM_*, CPU1_DIMM_*, DRAM_*, etc.
_HIGH_CONFIDENCE_MEMORY_RE = re.compile(
    r"(?ix)"
    r"("
    r"(?:temp|power|pwr|volt|vmon|current|amp)?[_\- ]*(?:ddr[345]?|dram|dimm|memory|ram)[_\- ]*[a-z0-9_\-]+"
    r"|(?:ddr[345]?|dram|dimm|memory|ram)[_\- ]*(?:temp|power|pwr|volt|vmon|current|amp)?[_\- ]*[a-z0-9_\-]*"
    r"|[cp]\d+[_\- ]*dimm[_\- ]*[a-z0-9_\-]*"
    r"|cpu\d+\s+dimm[_\- ]*[a-z0-9_\-]*"
    r"|p\d+[_\- ]*dimm[_\- ]*[a-z0-9_\-]*"
    r"|nvdimm|rdimm|lrdimm|udimm"
    r")"
)

_POSSIBLE_MEMORY_RE = re.compile(r"(?i)\b(ddr|dram|dimm|spd|memory|ram)\b")

# Evite de prendre "CPU Temp" / "Package" / "System Temp" pour de la RAM.
_NEGATIVE_RE = re.compile(
    r"(?i)\b(cpu\s*temp|package|core|pch|system\s*temp|mb\s*temp|"
    r"inlet|exhaust|vrm|gpu|nvme|ssd|hdd|fan|soc|tctl|tdie|"
    r"system\s*power|psu|power\s*supply|total\s*power|platform)\b"
)


def _is_high_confidence_memory_label(label: str) -> bool:
    l = label.strip()
    if not l:
        return False
    if _NEGATIVE_RE.search(l) and not _HIGH_CONFIDENCE_MEMORY_RE.search(l):
        return False
    return bool(_HIGH_CONFIDENCE_MEMORY_RE.search(l))


def _looks_like_memory_label(label: str) -> bool:
    return _is_high_confidence_memory_label(label)


def _classify_unit(unit: str, label: str = "") -> str:
    u = (unit or "").lower()
    l = (label or "").lower()
    if "degree" in u or "°c" in u or u == "c" or "temp" in l:
        return "temperature"
    if "watt" in u or u == "w" or "power" in l or "pwr" in l:
        return "power"
    if "volt" in u or u == "v" or "voltage" in l:
        return "voltage"
    if "amp" in u or u == "a" or "current" in l:
        return "current"
    return "other"


def _safe_float(s: str):
    try:
        return float(str(s).replace(",", "."))
    except Exception:
        return None


def collect_ipmi_memory_sensors() -> List[MemorySensor]:
    """Lit les capteurs RAM exposes par IPMI SDR/Sensor.

    Strategie:
    - ipmitool sdr elist full : format (Name | HexID | Status | EntityID | Reading+Unit)
    - ipmitool sensor         : format (Name | Reading | Unit | Status)
    - sudo -n uniquement, jamais de prompt mot de passe
    """
    sensors: List[MemorySensor] = []
    if not _tool("ipmitool"):
        return sensors

    commands = [
        ["ipmitool", "sdr", "elist", "full"],
        ["ipmitool", "sensor"],
    ]
    for base_cmd in commands:
        tried = []
        if _tool("sudo"):
            tried.append(["sudo", "-n"] + base_cmd)
        tried.append(base_cmd)
        out = ""
        rc = -1
        for cmd in tried:
            rc, out, _ = _run(cmd, timeout=10)
            if rc == 0 and out:
                break
        if rc != 0 or not out:
            continue

        for line in out.splitlines():
            raw = line.strip()
            if not raw:
                continue
            parts = [p.strip() for p in raw.split("|")]
            if len(parts) < 2:
                continue
            name = parts[0].strip()
            if not _is_high_confidence_memory_label(name):
                continue

            value = None
            unit = ""
            status = ""

            # 1. Format SDR (5+ colonnes, colonne 2 = ID Hex 0x.../..h, lecture réelle en col 5)
            # Ex: TEMP_DDR5_X1 | 34h | ok | 32.1 | 38 degrees C
            # Ex: POWER_DDR5_X1 | 35h | ok | 32.1 | 14.5 Watts
            if len(parts) >= 5 and (re.search(r"^[0-9a-fA-F]+h$", parts[1]) or "." in parts[3]):
                status = parts[2].strip()
                reading_part = parts[4].strip()
                m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*(.*)", reading_part)
                if m:
                    value = _safe_float(m.group(1))
                    unit = m.group(2).strip()

            # 2. Format Sensor (colonne 2 = Valeur numérique, col 3 = Unité)
            # Ex: TEMP_DDR5_X1 | 38.000 | degrees C | ok
            elif len(parts) >= 3:
                reading_str = parts[1].strip()
                m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)", reading_str)
                if m:
                    value = _safe_float(m.group(1))
                    unit = parts[2].strip()
                if len(parts) >= 4:
                    status = parts[3].strip()

            # 3. Fallback: recherche sur la ligne entière si non trouvé
            if value is None:
                m = re.search(r"\|\s*([+-]?\d+(?:[\.,]\d+)?)\s*(degrees\s*c|°c|watts?|volts?|amps?|w|v|a)\b", raw, re.I)
                if m:
                    value = _safe_float(m.group(1))
                    unit = m.group(2).strip()

            if value is None:
                continue
            kind = _classify_unit(unit, name)
            if kind not in ("temperature", "power", "voltage", "current"):
                continue
            sensors.append(MemorySensor(name=name, kind=kind, value=value, unit=unit or "", source="ipmi", raw=raw, status=status))
    return _dedupe_sensors(sensors)


def collect_lm_sensors_memory_sensors() -> List[MemorySensor]:
    sensors: List[MemorySensor] = []
    if not _tool("sensors"):
        return sensors
    rc, out, _ = _run(["sensors"], timeout=5)
    if rc != 0 or not out:
        return sensors
    current_chip = ""
    for line in out.splitlines():
        if line and not line.startswith((" ", "\t")) and ":" not in line:
            current_chip = line.strip()
            continue
        raw = line.strip()
        if ":" not in raw or not _looks_like_memory_label(raw + " " + current_chip):
            continue
        label, rest = raw.split(":", 1)
        name = f"{current_chip} {label}".strip()
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*°?C", rest, re.I)
        if m:
            sensors.append(MemorySensor(name=name, kind="temperature", value=float(m.group(1).replace(',', '.')), unit="°C", source="sensors", raw=raw))
            continue
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*W\b", rest, re.I)
        if m:
            sensors.append(MemorySensor(name=name, kind="power", value=float(m.group(1).replace(',', '.')), unit="W", source="sensors", raw=raw))
    return _dedupe_sensors(sensors)


def collect_hwmon_memory_sensors() -> List[MemorySensor]:
    sensors: List[MemorySensor] = []
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return sensors
    try:
        roots = [os.path.join(base, x) for x in os.listdir(base)]
    except Exception:
        return sensors
    for root in roots:
        try:
            chip = open(os.path.join(root, "name")).read().strip()
        except Exception:
            chip = os.path.basename(root)
        try:
            files = os.listdir(root)
        except Exception:
            continue
        for f in files:
            if not f.endswith("_label"):
                continue
            try:
                label = open(os.path.join(root, f)).read().strip()
            except Exception:
                continue
            name = f"{chip} {label}".strip()
            if not _looks_like_memory_label(name):
                continue
            prefix = f[:-6]
            inp = os.path.join(root, prefix + "_input")
            if not os.path.exists(inp):
                continue
            try:
                rawv = float(open(inp).read().strip())
            except Exception:
                continue
            if prefix.startswith("temp"):
                sensors.append(MemorySensor(name=name, kind="temperature", value=rawv / 1000.0 if rawv > 200 else rawv, unit="°C", source="hwmon"))
            elif prefix.startswith("power"):
                sensors.append(MemorySensor(name=name, kind="power", value=rawv / 1000000.0 if rawv > 1000 else rawv, unit="W", source="hwmon"))
            elif prefix.startswith("in"):
                sensors.append(MemorySensor(name=name, kind="voltage", value=rawv / 1000.0 if rawv > 10 else rawv, unit="V", source="hwmon"))
    return _dedupe_sensors(sensors)


def collect_redfish_memory_sensors_wrapper() -> List[MemorySensor]:
    try:
        from core.redfish_client import collect_redfish_memory_sensors
        raw_items = collect_redfish_memory_sensors()
        out = []
        for r in raw_items:
            m = MemorySensor(
                name=r.get("name", "DIMM Redfish"),
                kind=r.get("kind", "temperature"),
                value=r.get("value"),
                unit=r.get("unit", ""),
                source="redfish",
                raw=r.get("raw", ""),
                status=r.get("status", "")
            )
            if m.value is not None:
                out.append(m)
        return out
    except Exception:
        return []


def _dedupe_sensors(items: List[MemorySensor]) -> List[MemorySensor]:
    seen = set()
    out = []
    for s in items:
        if s.value is None:
            continue
        key = (s.source, re.sub(r"\s+", " ", s.name.lower()), s.kind, round(float(s.value), 3))
        if key in seen:
            continue
        seen.add(key)
        out.append(s)
    return out


def collect_memory_sensors() -> List[dict]:
    # Priorité Redfish (REST API certifiée CM) > BMC/IPMI > lm-sensors > hwmon
    items = []
    found_temps = False
    found_powers = False
    for fn in (collect_redfish_memory_sensors_wrapper, collect_ipmi_memory_sensors, collect_lm_sensors_memory_sensors, collect_hwmon_memory_sensors):
        try:
            res = fn()
            if not res: continue
            
            # Filtre des capteurs de cette source selon nos besoins
            res_temps = [x for x in res if x.kind == "temperature"]
            res_powers = [x for x in res if x.kind == "power"]
            
            if res_temps and not found_temps:
                items.extend(res_temps)
                found_temps = True
                
            if res_powers and not found_powers:
                items.extend(res_powers)
                found_powers = True
                
            if found_temps and found_powers:
                break
        except Exception:
            pass
            
    valid_items = _dedupe_sensors(items)
    return [asdict(x) for x in valid_items]


def summarize_memory_sensors(items=None) -> dict:
    if items is None:
        items = collect_memory_sensors()
    temps = [float(x["value"]) for x in items if x.get("kind") == "temperature"]
    
    # Pour la puissance, on évite le double-comptage (total vs individuels)
    power_items = [x for x in items if x.get("kind") == "power"]
    indiv_powers = [float(x["value"]) for x in power_items if "dimm" in x.get("name", "").lower() and not any(k in x.get("name", "").lower() for k in ("vrm", "cpu", "ab", "cd", "ef", "gh"))]
    agg_powers = [float(x["value"]) for x in power_items if not ("dimm" in x.get("name", "").lower() and not any(k in x.get("name", "").lower() for k in ("vrm", "cpu", "ab", "cd", "ef", "gh")))]
    
    if len(indiv_powers) > 0:
        tot_pwr = sum(indiv_powers)
    else:
        tot_pwr = sum(agg_powers) if agg_powers else 0.0
    # Sanity check global au cas où le total de barrettes non converties dépasse 300W
    if tot_pwr > 300.0:
        if tot_pwr >= 100000.0:
            tot_pwr /= 1000000.0
        elif tot_pwr > 300.0:
            tot_pwr /= 1000.0

    return {
        "temperature_max_c": round(max(temps), 1) if temps else 0.0,
        "temperature_avg_c": round(sum(temps) / len(temps), 1) if temps else 0.0,
        "power_total_w": round(tot_pwr, 1),
        "sensor_count": len(items),
        "sources": sorted(set(x.get("source", "") for x in items if x.get("source"))),
    }


def read_ram_temperature_power() -> Tuple[float, float]:
    items = collect_memory_sensors()
    summary = summarize_memory_sensors(items)
    return float(summary.get("temperature_max_c") or 0.0), float(summary.get("power_total_w") or 0.0)
