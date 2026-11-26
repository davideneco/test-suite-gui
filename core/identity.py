"""
identity.py — Identité de la machine testée (modèle, numéros de série) pour la traçabilité.

Lecture DMI sans privilège quand c'est possible (/sys/class/dmi/id), repli sur
`sudo -n dmidecode` (sans mot de passe). Les valeurs génériques des constructeurs
(« To Be Filled By O.E.M. », « Default string »…) sont ignorées.
"""

import os
import re
import shutil
import subprocess
from typing import Dict

_JUNK = {"", "default string", "to be filled by o.e.m.", "to be filled by oem", "not specified", "none",
         "n/a", "na", "0", "system serial number", "system product name", "unknown", "not applicable",
         "serial number", "base board serial number", "chassis serial number", "123456789", "0123456789"}
_DMI = "/sys/class/dmi/id"


def _clean(v: str) -> str:
    v = (v or "").strip()
    return "" if v.lower() in _JUNK or re.fullmatch(r"[0 ]+", v) else v


def _read_dmi(name: str) -> str:
    try:
        with open(os.path.join(_DMI, name)) as f:
            return _clean(f.read())
    except Exception:
        return ""


def _dmidecode(keyword: str) -> str:
    if not shutil.which("dmidecode"):
        return ""
    cmds = [["dmidecode", "-s", keyword]]
    if shutil.which("sudo"):
        cmds.insert(0, ["sudo", "-n", "dmidecode", "-s", keyword])
    for cmd in cmds:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=6)
            if r.returncode == 0:
                lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith("#")]
                if lines and _clean(lines[0]):
                    return _clean(lines[0])
        except Exception:
            continue
    return ""


def collect_identity() -> Dict[str, str]:
    """Retourne {vendor, model, serial, board_serial, chassis_serial, bios} ('' si inconnu)."""
    ident = {
        "vendor": _read_dmi("sys_vendor") or _dmidecode("system-manufacturer"),
        "model": _read_dmi("product_name") or _dmidecode("system-product-name"),
        "serial": _read_dmi("product_serial") or _dmidecode("system-serial-number"),
        "board_serial": _read_dmi("board_serial") or _dmidecode("baseboard-serial-number"),
        "chassis_serial": _read_dmi("chassis_serial") or _dmidecode("chassis-serial-number"),
        "bios": _read_dmi("bios_version"),
    }
    return ident


def best_serial(ident: Dict[str, str]) -> str:
    """Numéro de série le plus pertinent : système, sinon châssis, sinon carte mère."""
    return ident.get("serial") or ident.get("chassis_serial") or ident.get("board_serial") or ""
