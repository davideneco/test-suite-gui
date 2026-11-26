"""redfish_client.py — Client REST API Redfish (DMTF) pour la télémétrie BMC / Carte Mère.

Permet de récupérer les températures, la consommation électrique et les métriques matérielles
depuis l'API Redfish des cartes mères serveurs (Supermicro, Dell iDRAC, HPE iLO, Asus, Gigabyte, Lenovo).
Gère le bypass SSL auto-signé, le filtrage sanitaire des fausses valeurs et le basculement multi-source.
"""

import os
import re
import json
import ssl
import urllib.request
import urllib.error
from dataclasses import dataclass
from typing import List, Optional, Tuple


@dataclass
class RedfishSensor:
    name: str
    kind: str             # temperature | power | voltage | current | other
    value: float
    unit: str
    member_id: str = ""
    health: str = "OK"
    state: str = "Enabled"
    raw_reading: str = ""


# Identifiants par défaut courants sur BMC serveurs
DEFAULT_CREDENTIALS = [
    ("ADMIN", "ADMIN"),
    ("root", "calvin"),
    ("admin", "admin"),
    ("root", "root"),
    ("Administrator", "superuser"),
]


def _create_ssl_context():
    """Crée un contexte SSL autorisant les certificats auto-signés BMC."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_json(url: str, user: str = "", password: str = "", timeout: int = 4) -> Optional[dict]:
    """Exécute une requête GET HTTP/HTTPS vers l'API Redfish et retourne le JSON."""
    try:
        req = urllib.request.Request(url)
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", "BenchSuite-Redfish/1.2")

        if user and password:
            import base64
            auth_str = f"{user}:{password}"
            b64_auth = base64.b64encode(auth_str.encode("utf-8")).decode("utf-8")
            req.add_header("Authorization", f"Basic {b64_auth}")

        ctx = _create_ssl_context()
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
            if resp.status in (200, 201):
                payload = resp.read().decode("utf-8", errors="ignore")
                return json.loads(payload)
    except Exception:
        pass
    return None


def get_bmc_target_host(default_ip: str = "") -> str:
    """Détermine l'adresse IP / hôte de la BMC à interroger."""
    env_host = os.environ.get("REDFISH_HOST") or os.environ.get("BMC_IP")
    if env_host:
        return env_host.strip()

    if default_ip and default_ip not in ("0.0.0.0", "127.0.0.1", "::1", ""):
        return default_ip.strip()

    # Tente d'abord localhost / 127.0.0.1 si un agent Redfish local tourne
    return "127.0.0.1"


def get_redfish_credentials() -> Tuple[str, str]:
    """Retourne (user, password) définis dans l'environnement ou par défaut."""
    u = os.environ.get("REDFISH_USER") or os.environ.get("BMC_USER") or ""
    p = os.environ.get("REDFISH_PASS") or os.environ.get("BMC_PASS") or ""
    return u.strip(), p.strip()


# Résultat de la sonde par (hôte, utilisateur) : (expiration, résultat). Sans ce cache, chaque relevé (toutes les 5 s pendant
# un test) refaisait la sonde https + http et rejouait les identifiants par défaut — lent, et de quoi verrouiller un compte BMC.
_AVAIL_CACHE: dict = {}
_AVAIL_TTL_OK_S, _AVAIL_TTL_KO_S = 300.0, 120.0
_DEFAULTS_TRIED: set = set()         # hôtes sur lesquels les identifiants d'usine ont déjà été essayés (une seule fois par exécution)


def _members(res) -> bool:
    return isinstance(res, dict) and isinstance(res.get("Members"), list)


def is_redfish_available(host: str = None, user: str = None, password: str = None, timeout: int = 3) -> Tuple[bool, str, Optional[Tuple[str, str]]]:
    """Vérifie si l'API Redfish répond sur l'hôte spécifié.

    Retourne (disponible, URL_de_base, (user, password)_fonctionnel).

    Les identifiants sont validés sur une ressource PROTÉGÉE (`/redfish/v1/Chassis`) : la racine `/redfish/v1` répond sans
    authentification sur une BMC conforme, donc l'ancien test retenait des identifiants vides et toutes les lectures de capteurs
    recevaient ensuite un refus (401). Aucun identifiant n'est envoyé en http (en clair). Résultat mémorisé quelques minutes.
    """
    import time as _time
    if not host:
        host = get_bmc_target_host()

    env_u, env_p = get_redfish_credentials()
    user = user or env_u
    password = password or env_p

    key = (host, user or "")
    hit = _AVAIL_CACHE.get(key)
    if hit and hit[0] > _time.time():
        return hit[1]

    targets = []
    if host.startswith("http://") or host.startswith("https://"):
        targets.append(host.rstrip("/"))
    else:
        targets.append(f"https://{host}")
        targets.append(f"http://{host}")

    result: Tuple[bool, str, Optional[Tuple[str, str]]] = (False, "", None)
    for base_url in targets:
        secure = base_url.startswith("https://")
        root = _fetch_json(f"{base_url}/redfish/v1", timeout=timeout)
        present = isinstance(root, dict) and "RedfishVersion" in root
        cands = [("", "")]                                    # d'abord sans authentification
        if secure and user:
            cands.append((user, password))
        if secure and host not in _DEFAULTS_TRIED and (present or user):
            _DEFAULTS_TRIED.add(host)
            cands.extend(DEFAULT_CREDENTIALS)
        found = None
        if present or (secure and user):
            for u, p in cands:
                if _members(_fetch_json(f"{base_url}/redfish/v1/Chassis", user=u, password=p, timeout=timeout)):
                    found = (u, p)
                    break
        if found is not None:
            result = (True, base_url, found)
            break
        if present:
            result = (True, base_url, ("", ""))              # service présent mais capteurs protégés : REDFISH_USER / REDFISH_PASS requis
            break

    _AVAIL_CACHE[key] = (_time.time() + (_AVAIL_TTL_OK_S if result[0] else _AVAIL_TTL_KO_S), result)
    return result


def _sanitize_redfish_reading(val: float, kind: str, name: str) -> Optional[float]:
    """Contrôle sanitaire des valeurs Redfish : écarte les sentinelles d'erreur BMC (0 °C, 255, 65535, -1…) et les valeurs hors
    plage physique. Aucune mise à l'échelle : Redfish publie des °C, W, V (ReadingCelsius, PowerConsumedWatts, ReadingVolts) ;
    deviner une unité d'après la taille de la valeur transformait 130 °C en 13 °C ou 1500 W en 15 W."""
    if val is None:
        return None
    try:
        v = float(val)
    except Exception:
        return None

    if kind == "temperature":
        if v in (255.0, 65535.0, 32767.0, 4294967295.0) or not (2.0 <= v <= 150.0):
            return None
        return round(v, 1)
    if kind == "power":
        if v in (65535.0, 4294967295.0) or not (0.1 <= v <= 20000.0):
            return None
        return round(v, 2)
    if kind == "voltage":
        if not (0.1 <= v <= 60.0):
            return None
        return round(v, 3)
    return round(v, 2)


def collect_redfish_sensors(host: str = None, user: str = None, password: str = None) -> List[RedfishSensor]:
    """Collecte les capteurs thermiques et électriques via l'API Redfish."""
    avail, base_url, creds = is_redfish_available(host, user, password)
    if not avail or not base_url:
        return []

    u, p = creds if creds else ("", "")
    sensors: List[RedfishSensor] = []

    # Chemins des ressources Redfish Thermal / Power
    chassis_res = _fetch_json(f"{base_url}/redfish/v1/Chassis", u, p)
    chassis_members = []
    if chassis_res and "Members" in chassis_res:
        for m in chassis_res["Members"]:
            if isinstance(m, dict) and "@odata.id" in m:
                chassis_members.append(m["@odata.id"])

    if not chassis_members:
        chassis_members = [
            "/redfish/v1/Chassis/1",
            "/redfish/v1/Chassis/Self",
            "/redfish/v1/Chassis/System.Embedded.1",
        ]

    for c_path in chassis_members:
        # 1. Thermal
        thermal_url = f"{base_url}{c_path}/Thermal"
        t_data = _fetch_json(thermal_url, u, p)
        if t_data and "Temperatures" in t_data:
            for item in t_data["Temperatures"]:
                name = item.get("Name") or item.get("MemberId") or "Temp"
                reading = item.get("ReadingCelsius")
                status_obj = item.get("Status") or {}
                state = status_obj.get("State", "Enabled")
                health = status_obj.get("Health", "OK")

                if state not in ("Enabled", "OK", "Absent", ""):
                    continue
                clean_val = _sanitize_redfish_reading(reading, "temperature", name)
                if clean_val is not None:
                    sensors.append(RedfishSensor(
                        name=name, kind="temperature", value=clean_val, unit="°C",
                        member_id=str(item.get("MemberId", "")), health=health, state=state, raw_reading=str(reading)
                    ))

        # 2. Power
        power_url = f"{base_url}{c_path}/Power"
        p_data = _fetch_json(power_url, u, p)
        if p_data:
            # Power Control (Consommation globale ou par rail)
            for item in p_data.get("PowerControl", []):
                name = item.get("Name") or item.get("MemberId") or "Power"
                reading = item.get("PowerConsumedWatts")
                status_obj = item.get("Status") or {}
                state = status_obj.get("State", "Enabled")
                health = status_obj.get("Health", "OK")
                clean_val = _sanitize_redfish_reading(reading, "power", name)
                if clean_val is not None:
                    sensors.append(RedfishSensor(
                        name=name, kind="power", value=clean_val, unit="W",
                        member_id=str(item.get("MemberId", "")), health=health, state=state, raw_reading=str(reading)
                    ))
            # Voltages
            for item in p_data.get("Voltages", []):
                name = item.get("Name") or item.get("MemberId") or "Volt"
                reading = item.get("ReadingVolts")
                status_obj = item.get("Status") or {}
                state = status_obj.get("State", "Enabled")
                health = status_obj.get("Health", "OK")
                clean_val = _sanitize_redfish_reading(reading, "voltage", name)
                if clean_val is not None:
                    sensors.append(RedfishSensor(
                        name=name, kind="voltage", value=clean_val, unit="V",
                        member_id=str(item.get("MemberId", "")), health=health, state=state, raw_reading=str(reading)
                    ))
        # 3. Sensors (Nouvelle norme Redfish)
        sensors_url = f"{base_url}{c_path}/Sensors"
        s_data = _fetch_json(sensors_url, u, p)
        if s_data and "Members" in s_data:
            for s_member in s_data["Members"]:
                s_id = s_member.get("@odata.id")
                if not s_id: continue
                s_detail = _fetch_json(f"{base_url}{s_id}", u, p)
                if not s_detail: continue
                
                name = s_detail.get("Name") or s_detail.get("Id") or "Sensor"
                reading = s_detail.get("Reading")
                unit_str = s_detail.get("ReadingUnits", "")
                
                status_obj = s_detail.get("Status") or {}
                state = status_obj.get("State", "Enabled")
                health = status_obj.get("Health", "OK")
                
                if state not in ("Enabled", "OK", "Absent", ""):
                    continue
                    
                kind = "unknown"
                unit = ""
                if "Celsius" in unit_str:
                    kind = "temperature"
                    unit = "°C"
                elif "Watts" in unit_str:
                    kind = "power"
                    unit = "W"
                elif "Volts" in unit_str:
                    kind = "voltage"
                    unit = "V"
                elif "RPM" in unit_str or "Percent" in unit_str and "Fan" in name:
                    kind = "fan"
                    unit = "RPM" if "RPM" in unit_str else "%"
                elif "Amperes" in unit_str:
                    kind = "current"
                    unit = "A"
                
                if kind != "unknown":
                    clean_val = _sanitize_redfish_reading(reading, kind, name)
                    if clean_val is not None:
                        sensors.append(RedfishSensor(
                            name=name, kind=kind, value=clean_val, unit=unit,
                            member_id=str(s_detail.get("Id", "")), health=health, state=state, raw_reading=str(reading)
                        ))

    return sensors


def collect_redfish_memory_sensors(host: str = None, user: str = None, password: str = None) -> list:
    """Filtre et retourne uniquement les capteurs de RAM (DIMM) au format dict compatible MemorySensor."""
    all_sensors = collect_redfish_sensors(host, user, password)
    mem_sensors = []

    _RAM_PATTERN = re.compile(r"(?i)\b(dimm|ddr|dram|memory|ram|spd)\b")

    for s in all_sensors:
        if _RAM_PATTERN.search(s.name) or "dimm" in s.member_id.lower():
            mem_sensors.append({
                "name": s.name,
                "kind": s.kind,
                "value": s.value,
                "unit": s.unit,
                "source": "redfish",
                "raw": f"{s.raw_reading} ({s.health})",
                "status": s.state,
            })
    return mem_sensors
