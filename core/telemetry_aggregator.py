"""telemetry_aggregator.py — Agrégateur Universel de Télémétrie & Capteurs Carte Mère.

Collecte et unifie TOUTES les métriques matérielles avec unité renvoyées par la carte mère et le système :
- Redfish REST API (DMTF)
- BMC / IPMI (SDR & Sensor)
- lm-sensors (Sensors Linux)
- Linux sysfs / hwmon / PMBus / I2C
- IPMI DCMI (Power Reading)
- GPU NVIDIA / ROCm
- NVMe SMART

Gère le filtrage sanitaire des valeurs aberrantes, la conversion d'unités et la catégorisation.
"""

import os
import re
import subprocess
from dataclasses import dataclass, asdict
from typing import List, Optional, Tuple


@dataclass
class TelemetryMetric:
    name: str
    category: str           # temperature | power | voltage | current | fan | frequency | other
    value: float
    unit: str
    source: str             # redfish | ipmi | lm-sensors | hwmon | i2c_pmbus | nvidia | nvme
    chip_or_device: str = ""
    status: str = "OK"
    raw_data: str = ""


def _run(cmd, timeout=6):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _tool(name: str) -> bool:
    if name == "nvidia-smi":
        from core.safety import nvidia_smi_usable
        return nvidia_smi_usable()
    import shutil            # sans processus `which` (absent de certaines installations minimales)
    return shutil.which(name) is not None


def _safe_float(val) -> Optional[float]:
    try:
        return float(str(val).replace(",", "."))
    except Exception:
        return None


def _classify_metric(name: str, unit: str) -> Tuple[str, str]:
    """Détermine la catégorie et l'unité standard : par l'UNITÉ d'abord, le nom ne sert que si l'unité est absente
    (« P12V_PWR » en volts reste une tension, « CPU Power Current » en ampères reste un courant)."""
    u = (unit or "").lower().strip()
    l = (name or "").lower().strip()

    if u:
        if "°c" in u or "degree" in u or u in ("c", "celsius"):
            return "temperature", "°C"
        if "watt" in u or u == "w":
            return "power", "W"
        if "volt" in u or u == "v":
            return "voltage", "V"
        if "amp" in u or u == "a":
            return "current", "A"
        if "rpm" in u:
            return "fan", "RPM"
        if "hz" in u:
            return "frequency", "MHz" if "mhz" in u else ("GHz" if "ghz" in u else "Hz")
        if "%" in u or "pct" in u or "percent" in u:
            return "load", "%"
    if "temp" in l:
        return "temperature", "°C"
    if "power" in l or "pwr" in l or "watt" in l:
        return "power", "W"
    if "voltage" in l or "volt" in l:
        return "voltage", "V"
    if "current" in l or "curr" in l:
        return "current", "A"
    if "fan" in l or "tach" in l:
        return "fan", "RPM"
    if "clock" in l or "freq" in l:
        return "frequency", "MHz" if "mhz" in l else "Hz"
    if "usage" in l or "load" in l:
        return "load", "%"
    return "other", unit


def _sanitize_metric(m: TelemetryMetric) -> Optional[TelemetryMetric]:
    """Filtre les sentinelles d'erreur BMC / hwmon et les valeurs hors plage physique.

    Aucune mise à l'échelle ici : chaque collecteur livre déjà des W / °C / V / A (hwmon convertit lui-même ses
    milli- et micro-unités). L'ancienne conversion « selon l'ordre de grandeur » faussait des valeurs réelles :
    une alimentation à 1250 W devenait 1,25 W, une température de 130 °C devenait 13 °C (surchauffe masquée),
    un rail 54 V devenait 0,054 V puis était écarté."""
    if m.value is None:
        return None

    cat = m.category
    val = m.value

    # Statuts invalides
    st = (m.status or "").lower()
    if st in ("na", "nr", "ns", "disabled", "no sensor", "error", "cr"):
        return None

    if cat == "temperature":
        # Sentinelles 0, 255, 65535, -128, 32767 ; au-delà de 150 °C : valeur non physique (capteur absent / erreur)
        if val <= 0.0 or val in (255.0, 65535.0, 32767.0, 4294967295.0) or not (2.0 <= val <= 150.0):
            return None
        m.value = round(val, 1)
        m.unit = "°C"
        return m

    elif cat == "power":
        # Jusqu'à 20 kW : puissance d'un nœud GPU complet (DCMI) ou d'une alimentation
        if val in (65535.0, 4294967295.0) or not (0.1 <= val <= 20000.0):
            return None
        m.value = round(val, 2)
        m.unit = "W"
        return m

    elif cat == "voltage":
        if not (0.1 <= val <= 60.0):
            return None
        m.value = round(val, 3)
        m.unit = "V"
        return m

    elif cat == "current":
        if not (0.01 <= val <= 1000.0):
            return None
        m.value = round(val, 2)
        m.unit = "A"
        return m

    elif cat == "fan":
        if val < 0.0 or val > 30000.0:
            return None
        m.value = round(val, 0)
        m.unit = "RPM"
        return m

    return m


# ─────────────────────────────────────────────────────────────────────────────
# Collecteurs spécialisés par sous-système
# ─────────────────────────────────────────────────────────────────────────────

def collect_redfish_telemetry() -> List[TelemetryMetric]:
    """Collecte TOUTES les métriques de l'API Redfish REST."""
    metrics = []
    try:
        from core.redfish_client import collect_redfish_sensors
        raw_sensors = collect_redfish_sensors()
        for r in raw_sensors:
            cat, unit = _classify_metric(r.name, r.unit)
            m = TelemetryMetric(
                name=r.name,
                category=cat,
                value=r.value,
                unit=unit,
                source="redfish",
                chip_or_device="Redfish BMC",
                status=r.state,
                raw_data=f"{r.raw_reading} ({r.health})"
            )
            clean = _sanitize_metric(m)
            if clean:
                metrics.append(clean)
    except Exception:
        pass
    return metrics


def collect_ipmi_telemetry() -> List[TelemetryMetric]:
    """Collecte TOUTES les métriques renvoyées par ipmitool (sdr elist full & sensor)."""
    metrics = []
    if not _tool("ipmitool"):
        return metrics

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
            rc, out, _ = _run(cmd, timeout=8)
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

            value = None
            unit = ""
            status = ""

            # 1. Format SDR: Name | HexID | Status | EntityID | Reading+Unit
            if len(parts) >= 5 and (re.search(r"^[0-9a-fA-F]+h$", parts[1]) or "." in parts[3]):
                status = parts[2].strip()
                reading_part = parts[4].strip()
                m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*(.*)", reading_part)
                if m:
                    value = _safe_float(m.group(1))
                    unit = m.group(2).strip()

            # 2. Format Sensor: Name | Reading | Unit | Status
            elif len(parts) >= 3:
                reading_str = parts[1].strip()
                m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)", reading_str)
                if m:
                    value = _safe_float(m.group(1))
                    unit = parts[2].strip()
                if len(parts) >= 4:
                    status = parts[3].strip()

            if value is None:
                continue

            cat, norm_unit = _classify_metric(name, unit)
            m = TelemetryMetric(
                name=name,
                category=cat,
                value=value,
                unit=norm_unit,
                source="ipmi",
                chip_or_device="IPMI BMC",
                status=status,
                raw_data=raw
            )
            clean = _sanitize_metric(m)
            if clean:
                metrics.append(clean)
    return metrics


def collect_dcmi_power_telemetry() -> List[TelemetryMetric]:
    """Collecte la consommation globale du nœud via IPMI DCMI (Power Reading)."""
    metrics = []
    if not _tool("ipmitool"):
        return metrics

    cmd = ["ipmitool", "dcmi", "power", "reading"]
    if _tool("sudo"):
        cmd = ["sudo", "-n"] + cmd

    rc, out, _ = _run(cmd, timeout=5)
    if rc == 0 and out:
        for line in out.splitlines():
            if ":" not in line:
                continue
            k, v = [x.strip() for x in line.split(":", 1)]
            m = re.search(r"(\d+(?:[\.,]\d+)?)\s*Watts?", v, re.I)
            if m:
                val = float(m.group(1))
                metrics.append(TelemetryMetric(
                    name=f"DCMI System {k}",
                    category="power",
                    value=val,
                    unit="W",
                    source="ipmi_dcmi",
                    chip_or_device="DCMI Interface",
                    raw_data=line
                ))
    return metrics


def collect_lmsensors_telemetry() -> List[TelemetryMetric]:
    """Collecte toutes les métriques matérielles via lm-sensors."""
    metrics = []
    if not _tool("sensors"):
        return metrics

    rc, out, _ = _run(["sensors"], timeout=5)
    if rc != 0 or not out:
        return metrics

    current_chip = ""
    for line in out.splitlines():
        if line and not line.startswith((" ", "\t")) and ":" not in line:
            current_chip = line.strip()
            continue
        raw = line.strip()
        if ":" not in raw:
            continue
        label, rest = raw.split(":", 1)
        name = f"{current_chip} {label}".strip()

        # Température
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*°?C", rest, re.I)
        if m:
            tm = TelemetryMetric(name=name, category="temperature", value=float(m.group(1).replace(',', '.')), unit="°C", source="lm-sensors", chip_or_device=current_chip, raw_data=raw)
            clean = _sanitize_metric(tm)
            if clean: metrics.append(clean)
            continue

        # Puissance
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*W\b", rest, re.I)
        if m:
            tm = TelemetryMetric(name=name, category="power", value=float(m.group(1).replace(',', '.')), unit="W", source="lm-sensors", chip_or_device=current_chip, raw_data=raw)
            clean = _sanitize_metric(tm)
            if clean: metrics.append(clean)
            continue

        # Tension
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*V\b", rest, re.I)
        if m:
            tm = TelemetryMetric(name=name, category="voltage", value=float(m.group(1).replace(',', '.')), unit="V", source="lm-sensors", chip_or_device=current_chip, raw_data=raw)
            clean = _sanitize_metric(tm)
            if clean: metrics.append(clean)
            continue

        # Ventilateur
        m = re.search(r"([+-]?\d+(?:[\.,]\d+)?)\s*RPM\b", rest, re.I)
        if m:
            tm = TelemetryMetric(name=name, category="fan", value=float(m.group(1).replace(',', '.')), unit="RPM", source="lm-sensors", chip_or_device=current_chip, raw_data=raw)
            clean = _sanitize_metric(tm)
            if clean: metrics.append(clean)
            continue

    return metrics


def collect_hwmon_telemetry() -> List[TelemetryMetric]:
    """Parcourt l'intégralité de /sys/class/hwmon/ pour extraire les capteurs Linux & I2C/PMBus."""
    metrics = []
    base = "/sys/class/hwmon"
    if not os.path.isdir(base):
        return metrics

    try:
        roots = [os.path.join(base, x) for x in os.listdir(base)]
    except Exception:
        return metrics

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
            if not f.endswith("_input"):
                continue
            prefix = f[:-6]
            inp_path = os.path.join(root, f)
            try:
                rawv = float(open(inp_path).read().strip())
            except Exception:
                continue

            lbl_path = os.path.join(root, prefix + "_label")
            try:
                label = open(lbl_path).read().strip()
            except Exception:
                label = prefix

            name = f"{chip} {label}".strip()

            cat = "other"
            unit = ""
            val = rawv

            if prefix.startswith("temp"):
                cat, unit = "temperature", "°C"
                val = val / 1000.0            # ABI hwmon : milli-degrés
            elif prefix.startswith("power"):
                cat, unit = "power", "W"
                val = val / 1000000.0         # ABI hwmon : micro-watts
            elif prefix.startswith("in"):
                cat, unit = "voltage", "V"
                val = val / 1000.0            # ABI hwmon : milli-volts
            elif prefix.startswith("curr"):
                cat, unit = "current", "A"
                val = val / 1000.0            # ABI hwmon : milli-ampères
            elif prefix.startswith("fan"):
                cat, unit = "fan", "RPM"

            m = TelemetryMetric(name=name, category=cat, value=val, unit=unit, source="hwmon", chip_or_device=f"{chip}@{os.path.basename(root)}", raw_data=f"{prefix}={rawv}")
            clean = _sanitize_metric(m)
            if clean:
                metrics.append(clean)

    return metrics



def collect_lact_telemetry() -> List[TelemetryMetric]:
    """Collecte les métriques de la carte graphique (Hotspot, VRM, etc.) via le démon LACT s'il est présent."""
    metrics = []
    sock_path = "/run/lactd.sock"
    if not os.path.exists(sock_path):
        return metrics

    import socket
    import json

    def _lact_req(cmd: str, args: dict = None):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(2.0)
                s.connect(sock_path)
                req = {"command": cmd}
                if args:
                    req["args"] = args
                s.sendall((json.dumps(req) + "\n").encode("utf-8"))
                
                buf = b""
                while b"\n" not in buf:
                    data = s.recv(4096)
                    if not data:
                        break
                    buf += data
                
                line = buf.decode("utf-8").strip()
                res = json.loads(line)
                if res.get("status") == "ok":
                    return res.get("data")
        except Exception:
            pass
        return None

    devices = _lact_req("list_devices")
    if not devices:
        return metrics

    for dev in devices:
        dev_id = dev.get("id")
        dev_name = dev.get("name", "GPU Inconnu")
        if not dev_id:
            continue
            
        stats = _lact_req("device_stats", {"id": dev_id})
        if not stats:
            continue
            
        # Températures (ex: GPU, GPU Hotspot, VRAM)
        temps = stats.get("temps", {})
        for tname, tdata in temps.items():
            current = tdata.get("current")
            if current is not None:
                # Éviter de dupliquer "GPU GPU Hotspot" si tname contient déjà GPU
                lbl = tname if "GPU" in tname else f"GPU {tname}"
                metrics.append(TelemetryMetric(
                    name=f"LACT {lbl}",
                    category="temperature",
                    value=float(current),
                    unit="°C",
                    source="lact",
                    chip_or_device=dev_name
                ))
                
        # Ventilateur
        fan = stats.get("fan", {})
        pwm = fan.get("pwm_current")
        speed = fan.get("speed_current")
        pwm_max = fan.get("pwm_max", 255)
        if pwm is not None and pwm > 0 and pwm_max > 0:
            pct = (float(pwm) / float(pwm_max)) * 100.0
            metrics.append(TelemetryMetric(
                name="LACT GPU Ventilateur",
                category="fan",
                value=float(pct),
                unit="%",
                source="lact",
                chip_or_device=dev_name
            ))
        elif speed is not None:
            metrics.append(TelemetryMetric(
                name="LACT GPU Ventilateur RPM",
                category="fan",
                value=float(speed),
                unit="RPM",
                source="lact",
                chip_or_device=dev_name
            ))
            
        # Puissance
        power = stats.get("power", {})
        pwr_curr = power.get("current")
        if pwr_curr is not None:
             metrics.append(TelemetryMetric(
                name="LACT GPU Puissance",
                category="power",
                value=float(pwr_curr),
                unit="W",
                source="lact",
                chip_or_device=dev_name
            ))
             
    return metrics

def collect_gpu_telemetry() -> List[TelemetryMetric]:

    """Collecte les températures, puissances et ventilateurs des GPU NVIDIA via nvidia-smi."""
    metrics = []
    if not _tool("nvidia-smi"):
        return metrics

    cmd = ["nvidia-smi", "--query-gpu=index,name,temperature.gpu,power.draw,fan.speed,clocks.gr", "--format=csv,noheader,nounits"]
    rc, out, _ = _run(cmd, timeout=4)
    if rc == 0 and out:
        for line in out.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 6:
                idx, gname, temp, pwr, fan, clk = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5]
                label_prefix = f"GPU {idx} ({gname})"
                if temp and temp != "[N/A]":
                    v = _safe_float(temp)
                    if v: metrics.append(TelemetryMetric(name=f"{label_prefix} Température", category="temperature", value=v, unit="°C", source="nvidia", chip_or_device=gname))
                if pwr and pwr != "[N/A]":
                    v = _safe_float(pwr)
                    if v: metrics.append(TelemetryMetric(name=f"{label_prefix} Puissance", category="power", value=v, unit="W", source="nvidia", chip_or_device=gname))
                if fan and fan != "[N/A]":
                    v = _safe_float(fan)
                    if v: metrics.append(TelemetryMetric(name=f"{label_prefix} Ventilateur", category="fan", value=v, unit="%", source="nvidia", chip_or_device=gname))
                if clk and clk != "[N/A]":
                    v = _safe_float(clk)
                    if v: metrics.append(TelemetryMetric(name=f"{label_prefix} Fréquence", category="frequency", value=v, unit="MHz", source="nvidia", chip_or_device=gname))
    return metrics


# ─────────────────────────────────────────────────────────────────────────────
# Moteur principal et déduplication universelle
# ─────────────────────────────────────────────────────────────────────────────

def _dedupe_telemetry(metrics: List[TelemetryMetric]) -> List[TelemetryMetric]:
    """Déduplique les métriques en privilégiant Redfish > IPMI > lm-sensors > hwmon."""
    seen = set()
    out = []
    # Ordre de priorité des sources
    priority = {"redfish": 1, "ipmi": 2, "ipmi_dcmi": 3, "lact": 4, "nvidia": 5, "lm-sensors": 6, "hwmon": 7}

    # Tri par priorité de source
    sorted_metrics = sorted(metrics, key=lambda x: priority.get(x.source, 10))

    for m in sorted_metrics:
        # Clé de déduplication normalisée
        norm_name = re.sub(r"\s+", " ", m.name.lower().strip())
        key = (norm_name, m.category, round(float(m.value), 2))
        if key in seen:
            continue
        seen.add(key)
        out.append(m)

    return out


def collect_all_motherboard_telemetry(filter_enabled: bool = True) -> List[dict]:
    """Collecte TOUTES les métriques de la carte mère et du système toutes sources confondues."""
    all_metrics: List[TelemetryMetric] = []

    collectors = [
        collect_redfish_telemetry,
        collect_ipmi_telemetry,
        collect_dcmi_power_telemetry,
        collect_lmsensors_telemetry,
        collect_hwmon_telemetry,
        collect_lact_telemetry,
        collect_gpu_telemetry,
    ]

    for fn in collectors:
        try:
            res = fn()
            if res:
                all_metrics.extend(res)
        except Exception:
            pass

    cleaned = _dedupe_telemetry(all_metrics)
    
    if filter_enabled:
        # Filtrage selon les préférences de l'utilisateur
        try:
            from core.sensors_config import is_sensor_enabled
            final_list = [asdict(m) for m in cleaned if is_sensor_enabled(m.name)]
        except Exception:
            final_list = [asdict(m) for m in cleaned]
    else:
        final_list = [asdict(m) for m in cleaned]
        
    return final_list

_PSU_IN_RE = re.compile(r"(psu|ps\d|power\s*supply|\bpin\b|input)", re.I)
_NOT_PSU_IN_RE = re.compile(r"(out|pout|cpu|gpu|dimm|fan|vrm|12v|5v|3\.?3v)", re.I)


def _system_power(metrics_list: List[dict]) -> Tuple[float, str]:
    """Puissance de la machine entière et sa source : mesure DCMI du nœud, sinon somme des ENTRÉES d'alimentation d'une seule
    source. (0.0, "") si aucune n'existe — on n'affiche alors rien.

    L'ancien total additionnait tous les capteurs de puissance (entrées PSU + DCMI + CPU + GPU + rails hwmon) : la même énergie
    comptée deux à trois fois sous le libellé « Consommation système »."""
    pw = [x for x in metrics_list if x.get("category") == "power"]
    for x in pw:
        if x.get("source") == "ipmi_dcmi":
            try:
                return round(float(x["value"]), 1), "DCMI (BMC)"
            except (TypeError, ValueError, KeyError):
                pass
    for src in ("redfish", "ipmi", "hwmon", "lm-sensors"):
        vals = []
        for x in pw:
            name = str(x.get("name", ""))
            if x.get("source") == src and _PSU_IN_RE.search(name) and not _NOT_PSU_IN_RE.search(name):
                try:
                    vals.append(float(x["value"]))
                except (TypeError, ValueError, KeyError):
                    pass
        if vals:
            return round(sum(vals), 1), f"entrées des alimentations ({src})"
    return 0.0, ""


def summarize_motherboard_telemetry(metrics_list: List[dict] = None) -> dict:
    """Génère un résumé complet de la télémétrie matérielle."""
    if metrics_list is None:
        metrics_list = collect_all_motherboard_telemetry()

    temps = [float(x["value"]) for x in metrics_list if x.get("category") == "temperature"]
    powers = [float(x["value"]) for x in metrics_list if x.get("category") == "power"]
    volts = [float(x["value"]) for x in metrics_list if x.get("category") == "voltage"]
    fans = [float(x["value"]) for x in metrics_list if x.get("category") == "fan"]
    currs = [float(x["value"]) for x in metrics_list if x.get("category") == "current"]

    sources = sorted(set(x.get("source", "") for x in metrics_list if x.get("source")))
    sys_pwr, sys_src = _system_power(metrics_list)

    return {
        "metric_count": len(metrics_list),
        "sources": sources,
        "temperature_max_c": round(max(temps), 1) if temps else 0.0,
        "temperature_avg_c": round(sum(temps) / len(temps), 1) if temps else 0.0,
        "power_total_w": sys_pwr,
        "power_total_source": sys_src,
        "power_count": len(powers),
        "fan_count": len(fans),
        "voltage_count": len(volts),
        "current_count": len(currs),
    }

def start_background_telemetry(series_dict: dict, stop_fn) -> None:
    """
    Démarre un thread de télémétrie complète (carte mère, VRM) en arrière-plan
    si l'option globale est activée. Met à jour le dictionnaire series_dict.
    """
    try:
        from core.sensors_config import load_sensor_config
        config = load_sensor_config()
        if not config.get("global_background_telemetry", False):
            return
    except Exception:
        return

    import threading
    import time

    def mb_loop():
        last_mb_telemetry = {}
        while not stop_fn():
            try:
                metrics = collect_all_motherboard_telemetry()
                for m in metrics:
                    val = float(m["value"])
                    cat = m["category"]
                    n = m["name"].lower()
                    if "vrm" in n:
                        k = f"{cat}_vrm_{n.replace(' ','_')}"
                    elif "dimm" in n or "ram" in n or "memory" in n:
                        k = f"{cat}_dimm_{n.replace(' ','_')}"
                    elif "cpu" in n or "core" in n or "socket" in n:
                        k = f"{cat}_s_{n.replace(' ','_')}"
                    else:
                        k = f"{cat}_{n.replace(' ','_')}"
                    k = k.replace("temperature_", "temp_")
                    last_mb_telemetry[k] = val
                    
                # Flush to series_dict directly safely
                if "t" in series_dict and len(series_dict["t"]) > 0:
                    expected_len = len(series_dict["t"])
                    for k, v in last_mb_telemetry.items():
                        lst = series_dict.setdefault(k, [0.0]*max(0, expected_len-1))
                        if isinstance(lst, list):
                            lst.append(v)
                    # Compléter les séries secondaires à la longueur de « t » (seulement les listes : le dictionnaire peut aussi
                    # contenir des valeurs simples posées par un autre relevé, qui faisaient échouer tout le remplissage)
                    for k in list(series_dict.keys()):
                        lst = series_dict.get(k)
                        if not isinstance(lst, list) or str(k).startswith("_"):
                            continue
                        if k not in ("t", "disk_temp", "disk_power", "temp", "power", "ram", "cpu_freq", "cpu_load") and not k.startswith("ssd") and not k.startswith("gpu"):
                            while len(lst) < expected_len:
                                lst.append(lst[-1] if lst else 0.0)
            except Exception:
                pass

            for _ in range(30):
                if stop_fn(): break
                time.sleep(0.1)

    threading.Thread(target=mb_loop, daemon=True).start()
