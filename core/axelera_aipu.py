"""
axelera_aipu.py — Détection, télémétrie, modèles IA et contrôle des accélérateurs Axelera AI (Metis).

Fonctionne en best-effort (zéro crash si aucune carte, aucun pilote `metis` ou aucun Voyager SDK) :
- Détection PCIe (Vendor ID 1f9d, classe 1200) et module noyau `metis` (/sys/class/metis/version, /dev/metis-*).
- Découverte automatique du Voyager SDK et de son environnement Python (`axdevice`, `axmonitor`, `axrunmodel`,
  `axlogdevice`, `axdownloadmodel`, `axzoo`, `inference.py`) sans imposer l'activation préalable du venv.
- Parsing robuste de `axdevice`, `axmonitor --ui console -c print`, `axlogdevice`, et capteurs `hwmon` éventuels.
- Découverte des modèles compilés locaux (`model.json`, `.axm`, `.axe`) et des modèles YAML / `axzoo` du SDK.
- Réglages sûrs via `axdevice` (horloge, limite MVM, throttling thermique logiciel, limite de puissance 113m, refresh PCIe).

Matériel réel non présent côté développement : le parsing s'appuie sur la documentation officielle Voyager SDK v1.9.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Tuple

CONFIG_FILE = os.path.expanduser("~/.benchsuite_axelera.json")
AXELERA_VENDOR_ID = "1f9d"

# Modèles populaires du Model Zoo (sélection cliquable dans l'UI pour téléchargement explicite via axdownloadmodel)
SUGGESTED_ZOO_MODELS = [
    ("yolov8s-coco-onnx", "YOLOv8s COCO (détection d'objets)"),
    ("yolov8l-coco-onnx", "YOLOv8l COCO (charge lourde / chauffe)"),
    ("yolov5s-v7-coco", "YOLOv5s v7 COCO (détection rapide)"),
    ("resnet50-imagenet-coco", "ResNet-50 (classification haute cadence)"),
    ("mobilenet-v2", "MobileNet v2 (classification légère)"),
]

VALID_CLOCKS_MHZ = [100, 200, 400, 600, 700, 800]

# Seuils thermiques documentés (jonction T_j, docs.axelera.ai/sdk/user-guides/thermal/)
TEMP_WARNING_C = 95.0
TEMP_HW_THROTTLE_C = 105.0
TEMP_FREQ_SCALE_C = 110.0
TEMP_SHUTDOWN_C = 120.0

_TOOL_CACHE_LOCK = threading.Lock()
_TOOL_PATH_CACHE: Dict[str, Tuple[float, str]] = {}
_LAST_TELEM_ERROR = ""
_LAST_AXDEVICE_ERROR = ""


# ─────────────────────────────────────────────────────────────────────────────
# Configuration utilisateur & recherche du Voyager SDK
# ─────────────────────────────────────────────────────────────────────────────

def get_config() -> Dict:
    """Lit ~/.benchsuite_axelera.json ; {} si absent ou invalide."""
    try:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_config(updates: Dict) -> Dict:
    """Met à jour ~/.benchsuite_axelera.json et vide le cache de chemins."""
    cfg = get_config()
    cfg.update(updates or {})
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
    forget_cache()
    return cfg


def forget_cache() -> None:
    """Réinitialise le cache de chemins d'outils Axelera."""
    with _TOOL_CACHE_LOCK:
        _TOOL_PATH_CACHE.clear()


def _candidate_homes() -> List[str]:
    homes = []
    for h in (os.path.expanduser("~"), os.environ.get("HOME", "")):
        if h and h not in homes and os.path.isdir(h):
            homes.append(h)
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user:
        sh = os.path.expanduser(f"~{sudo_user}")
        if sh and sh not in homes and os.path.isdir(sh):
            homes.append(sh)
    return homes


def _sdk_version_tuple(sdk_dir: str) -> Tuple[int, ...]:
    """Extrait un tuple de version depuis VERSION.TXT ou le nom du dossier pour trier du plus récent au plus ancien."""
    ver_str = ""
    vtxt = os.path.join(sdk_dir, "VERSION.TXT")
    if os.path.isfile(vtxt):
        try:
            with open(vtxt, encoding="utf-8", errors="replace") as f:
                ver_str = f.read().strip()
        except Exception:
            pass
    if not ver_str:
        ver_str = os.path.basename(sdk_dir)
    nums = [int(x) for x in re.findall(r"\d+", ver_str)[:4]]
    return tuple(nums)


def _sdk_has_tools(sdk_dir: str) -> bool:
    """Vérifie si un dossier SDK contient un venv avec axdevice ou axrunmodel."""
    for sub in ("venv/bin", "axelera-env/bin", ".venv/bin", "bin", "env/bin"):
        bdir = os.path.join(sdk_dir, sub)
        if os.path.isfile(os.path.join(bdir, "axdevice")) or os.path.isfile(os.path.join(bdir, "axrunmodel")):
            return True
    return False


def _is_valid_sdk_dir(path: str) -> bool:
    return bool(
        path
        and os.path.isdir(path)
        and any(os.path.exists(os.path.join(path, f)) for f in ("inference.py", "VERSION.TXT", "ax_models", "deploy.py"))
    )


def _candidate_sdk_dirs() -> List[str]:
    """Dossiers plausibles contenant un Voyager SDK (`voyager-sdk`, `voyager-sdk-*`, `/opt/axelera/sdk/*`)."""
    cfg = get_config()
    pinned: List[str] = []
    for raw in (
        cfg.get("sdk_dir"),
        os.environ.get("VOYAGER_SDK_DIR"),
        os.environ.get("AXELERA_SDK_DIR"),
    ):
        if raw and os.path.isdir(raw):
            ap = os.path.abspath(raw)
            if ap not in pinned:
                pinned.append(ap)

    discovered: List[str] = []

    def add_disc(p: str) -> None:
        if p and os.path.isdir(p):
            ap = os.path.abspath(p)
            if ap not in pinned and ap not in discovered:
                discovered.append(ap)

    for home in _candidate_homes():
        for pat in (
            "voyager-sdk",
            "voyager-sdk-*",
            "axelera/voyager-sdk*",
            "Desktop/voyager-sdk*",
            "Documents/voyager-sdk*",
            "Downloads/voyager-sdk*",
            "Téléchargements/voyager-sdk*",
            ".benchsuite/tools/voyager-sdk*",
        ):
            for p in sorted(glob.glob(os.path.join(home, pat))):
                add_disc(p)
    for pat in ("/opt/axelera/sdk/*", "/opt/axelera/voyager-sdk*"):
        for p in sorted(glob.glob(pat)):
            add_disc(p)

    # Priorité aux SDK qui possèdent déjà axdevice/axrunmodel, puis version la plus récente
    discovered.sort(key=lambda d: (_sdk_has_tools(d), _sdk_version_tuple(d), d), reverse=True)
    return pinned + discovered


def list_detected_sdks() -> List[Dict]:
    """Retourne tous les dossiers Voyager SDK valides détectés sur la machine."""
    out: List[Dict] = []
    for d in _candidate_sdk_dirs():
        if not _is_valid_sdk_dir(d):
            continue
        ver = ""
        vtxt = os.path.join(d, "VERSION.TXT")
        if os.path.isfile(vtxt):
            try:
                with open(vtxt, encoding="utf-8", errors="replace") as f:
                    ver = f.read().strip()
            except Exception:
                pass
        out.append({
            "path": d,
            "version": ver,
            "has_tools": _sdk_has_tools(d),
        })
    return out


def find_sdk_dir() -> str:
    """Retourne le meilleur dossier Voyager SDK valide trouvé ('' sinon)."""
    cfg = get_config()
    custom = str(cfg.get("sdk_dir") or "").strip()
    if custom and os.path.isdir(custom):
        return os.path.abspath(custom)
    for d in _candidate_sdk_dirs():
        if _is_valid_sdk_dir(d):
            return d
    return ""


def _opt_runtime_dirs() -> List[str]:
    """Dossiers `/opt/axelera/runtime-*` ou `/opt/axelera/*` triés du plus récent au plus ancien."""
    found = []
    for pat in ("/opt/axelera/runtime-*", "/opt/axelera/sdk/*", "/opt/axelera/*"):
        for p in glob.glob(pat):
            if os.path.isdir(p) and os.path.basename(p) not in ("sdk",):
                ap = os.path.abspath(p)
                if ap not in found:
                    found.append(ap)
    found.sort(key=lambda d: (_sdk_version_tuple(d), d), reverse=True)
    return found


def _candidate_bin_dirs() -> List[str]:
    """Dossiers bin contenant potentiellement `axdevice`, `axrunmodel`, `axmonitor`…"""
    cfg = get_config()
    dirs: List[str] = []

    def add(p: Optional[str]) -> None:
        if p and os.path.isdir(p):
            ap = os.path.abspath(p)
            if ap not in dirs:
                dirs.append(ap)

    for key in ("venv_dir", "bin_dir"):
        v = str(cfg.get(key) or "").strip()
        if v:
            add(os.path.join(v, "bin") if os.path.isdir(os.path.join(v, "bin")) else v)
    sdk = find_sdk_dir()
    if sdk:
        for sub in ("venv/bin", "axelera-env/bin", ".venv/bin", "bin", "env/bin"):
            add(os.path.join(sdk, sub))
    for p in (os.environ.get("VIRTUAL_ENV"), os.environ.get("AXELERA_VENV")):
        if p:
            add(os.path.join(p, "bin"))
    for rt in _opt_runtime_dirs():
        add(os.path.join(rt, "bin"))
        add(os.path.join(rt, "venv", "bin"))
    for other_sdk in _candidate_sdk_dirs():
        for sub in ("venv/bin", "axelera-env/bin", ".venv/bin", "bin", "env/bin"):
            add(os.path.join(other_sdk, sub))
    for home in _candidate_homes():
        for pat in (
            "voyager-sdk*/venv/bin",
            "voyager-sdk*/axelera-env/bin",
            "axelera-env/bin",
            "axelera/venv/bin",
            ".local/bin",
        ):
            for p in sorted(glob.glob(os.path.join(home, pat))):
                add(p)
    for sys_bin in ("/usr/local/bin", "/usr/bin"):
        add(sys_bin)
    return dirs


def find_tool(name: str) -> str:
    """Cherche un outil Axelera (`axdevice`, `axmonitor`, `axrunmodel`, `axlogdevice`, `axzoo`, `axdownloadmodel`, `axversion`)."""
    now = time.time()
    with _TOOL_CACHE_LOCK:
        cached = _TOOL_PATH_CACHE.get(name)
        if cached and (now - cached[0]) < 15.0:
            return cached[1]
    found = shutil.which(name) or ""
    if not found:
        for d in _candidate_bin_dirs():
            cand = os.path.join(d, name)
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                found = cand
                break
    with _TOOL_CACHE_LOCK:
        _TOOL_PATH_CACHE[name] = (now, found)
    return found


def find_python_for_sdk() -> str:
    """Interpréteur Python du venv Voyager SDK (pour lancer `inference.py`), repli sys.executable."""
    for d in _candidate_bin_dirs():
        if d in ("/usr/local/bin", "/usr/bin"):
            continue
        py = os.path.join(d, "python3")
        if os.path.isfile(py) and os.access(py, os.X_OK):
            return py
        py2 = os.path.join(d, "python")
        if os.path.isfile(py2) and os.access(py2, os.X_OK):
            return py2
    return shutil.which("python3") or "python3"


def sdk_env(extra: Optional[Dict[str, str]] = None, configure_board: Optional[bool] = None) -> Dict[str, str]:
    """Environnement enrichi pour exécuter les outils Voyager SDK sans activation préalable."""
    env = os.environ.copy()
    env.setdefault("LC_ALL", "C")
    env.setdefault("LANG", "C")
    env.setdefault("AXELERA_SLOG_LEVEL", "err")
    if configure_board is not None:
        env["AXELERA_CONFIGURE_BOARD"] = "1" if configure_board else "0"
    bin_dirs = _candidate_bin_dirs()
    if bin_dirs:
        cur = env.get("PATH", "")
        env["PATH"] = os.pathsep.join(bin_dirs + ([cur] if cur else []))
    lib_dirs: List[str] = []
    sdk = find_sdk_dir()
    if sdk:
        pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = os.pathsep.join([sdk] + ([pp] if pp else []))
        for p in (os.path.join(sdk, "lib"), os.path.join(sdk, "venv", "lib")):
            if os.path.isdir(p) and p not in lib_dirs:
                lib_dirs.append(p)
    for rt in _opt_runtime_dirs():
        for p in (os.path.join(rt, "lib"), os.path.join(rt, "lib", "x86_64-linux-gnu")):
            if os.path.isdir(p) and p not in lib_dirs:
                lib_dirs.append(p)
    if lib_dirs:
        cur_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = os.pathsep.join(lib_dirs + ([cur_ld] if cur_ld else []))
    if extra:
        env.update(extra)
    return env


def _run(cmd: List[str], timeout: float = 8.0, cwd: Optional[str] = None,
         env: Optional[Dict[str, str]] = None) -> Tuple[int, str, str]:
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd or None,
            env=env or sdk_env(),
        )
        return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()
    except Exception as exc:
        return -1, "", str(exc)


def secure_boot_enabled() -> Optional[bool]:
    """Retourne True si UEFI Secure Boot est actif, False sinon, None si indéterminé."""
    for p in glob.glob("/sys/firmware/efi/efivars/SecureBoot-*"):
        try:
            with open(p, "rb") as f:
                data = f.read()
            if len(data) >= 5:
                return bool(data[4] == 1)
        except Exception:
            pass
    if shutil.which("mokutil"):
        rc, out, _ = _run(["mokutil", "--sb-state"], timeout=2, env=os.environ.copy())
        if rc == 0 and out:
            low = out.lower()
            if "enabled" in low:
                return True
            if "disabled" in low:
                return False
    return None


def sdk_status() -> Dict:
    """État synthétique du pilote noyau `metis`, des SDK détectés et des outils Voyager."""
    drv = driver_info()
    sdk_dir = find_sdk_dir()
    detected_sdks = list_detected_sdks()
    tools = {
        t: find_tool(t)
        for t in ("axdevice", "axrunmodel", "axmonitor", "axlogdevice", "axdownloadmodel", "axzoo", "axversion")
    }
    sdk_ver = ""
    if sdk_dir and os.path.isfile(os.path.join(sdk_dir, "VERSION.TXT")):
        try:
            with open(os.path.join(sdk_dir, "VERSION.TXT"), encoding="utf-8") as f:
                sdk_ver = f.read().strip()
        except Exception:
            pass
    if not sdk_ver and tools.get("axversion"):
        rc, out, _ = _run([tools["axversion"]], timeout=4)
        if rc == 0 and out:
            sdk_ver = out.splitlines()[0].strip()[:60]
    inf_py = os.path.join(sdk_dir, "inference.py") if sdk_dir else ""
    if inf_py and not os.path.isfile(inf_py):
        inf_py = ""
    return {
        "driver_loaded": drv["loaded"],
        "driver_name": drv.get("name", ""),
        "driver_version": drv["version"],
        "dev_nodes": drv["dev_nodes"],
        "dkms_present": drv.get("dkms_present", False),
        "dkms_mok_exists": os.path.isfile("/var/lib/dkms/mok.pub"),
        "secure_boot": secure_boot_enabled(),
        "kernel_release": os.uname().release,
        "sdk_dir": sdk_dir,
        "sdk_version": sdk_ver,
        "detected_sdks": detected_sdks,
        "opt_runtimes": _opt_runtime_dirs(),
        "inference_py": inf_py,
        "tools": tools,
        "axdevice_error": _LAST_AXDEVICE_ERROR,
        "axsystemserver_active": _is_service_active("axsystemserver"),
        "axsystemserver_installed": os.path.exists("/usr/lib/systemd/system/axsystemserver.service")
        or os.path.exists("/etc/systemd/system/axsystemserver.service")
        or any(os.path.exists(os.path.join(rt, "lib/systemd/system/axsystemserver.service")) for rt in _opt_runtime_dirs()),
    }


def _is_service_active(name: str) -> bool:
    if not shutil.which("systemctl"):
        return False
    rc, out, _ = _run(["systemctl", "is-active", name], timeout=2, env=os.environ.copy())
    return rc == 0 and out.strip() == "active"


# ─────────────────────────────────────────────────────────────────────────────
# Détection matériel (PCIe + driver metis + axdevice)
# ─────────────────────────────────────────────────────────────────────────────

def driver_info() -> Dict:
    """Lit l'état du pilote noyau Linux `metis` (/sys/class/metis, /sys/module/metis, /proc/modules, DKMS)."""
    version = ""
    ver_path = "/sys/class/metis/version"
    if os.path.isfile(ver_path):
        try:
            with open(ver_path, encoding="utf-8", errors="replace") as f:
                version = f.read().strip()
        except Exception:
            pass

    drv_name = ""
    if os.path.isdir("/sys/module/metis") or os.path.isdir("/sys/class/metis"):
        drv_name = "metis"
    elif os.path.isfile("/proc/modules"):
        try:
            with open("/proc/modules", encoding="utf-8", errors="replace") as f:
                for line in f:
                    mod = line.split()[0].lower()
                    if mod in ("metis", "axl_pcie", "triton", "triton_multi_ctx"):
                        drv_name = mod
                        break
        except Exception:
            pass

    nodes = sorted(
        glob.glob("/dev/metis-*")
        + glob.glob("/dev/metis[0-9]*")
        + glob.glob("/dev/axelera*")
    )
    dkms_present = bool(
        glob.glob("/usr/src/metis*")
        or glob.glob("/var/lib/dkms/metis*")
        or glob.glob("/lib/modules/*/updates/dkms/metis*.ko*")
        or glob.glob("/lib/modules/*/extra/metis*.ko*")
    )
    return {
        "loaded": bool(drv_name),
        "name": drv_name,
        "version": version,
        "dev_nodes": nodes,
        "dkms_present": dkms_present,
    }


def pci_devices() -> List[Dict]:
    """Liste les accélérateurs Axelera AI détectés sur le bus PCIe (Vendor 1f9d)."""
    devs: List[Dict] = []
    if shutil.which("lspci"):
        rc, out, _ = _run(["lspci", "-D", "-nn"], timeout=5, env=os.environ.copy())
        if rc == 0 and out:
            for line in out.splitlines():
                m = re.match(r"^([0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])\s+(.+?)\s+\[([0-9a-fA-F]{4}):([0-9a-fA-F]{4})\](.*)$", line.strip())
                if not m:
                    continue
                bdf, desc, vendor, device, rest = m.groups()
                if vendor.lower() != AXELERA_VENDOR_ID and "axelera" not in desc.lower():
                    continue
                pcie = _read_pci_sysfs(bdf)
                devs.append({
                    "bdf": bdf.lower(),
                    "vendor_id": vendor.lower(),
                    "device_id": device.lower(),
                    "desc": f"{desc} [{vendor}:{device}]{rest}".strip(),
                    "pcie_gen": pcie.get("pcie_gen", ""),
                    "pcie_width": pcie.get("pcie_width", ""),
                    "pcie_speed": pcie.get("pcie_speed", ""),
                    "driver": pcie.get("driver", ""),
                })
    if not devs and os.path.isdir("/sys/bus/pci/devices"):
        for bdf in sorted(os.listdir("/sys/bus/pci/devices")):
            d = os.path.join("/sys/bus/pci/devices", bdf)
            try:
                with open(os.path.join(d, "vendor")) as f:
                    vend = f.read().strip().lower().replace("0x", "")
                if vend != AXELERA_VENDOR_ID:
                    continue
                with open(os.path.join(d, "device")) as f:
                    dev_id = f.read().strip().lower().replace("0x", "")
            except Exception:
                continue
            pcie = _read_pci_sysfs(bdf)
            devs.append({
                "bdf": bdf.lower(),
                "vendor_id": vend,
                "device_id": dev_id,
                "desc": f"Axelera AI Metis AIPU [{vend}:{dev_id}]",
                "pcie_gen": pcie.get("pcie_gen", ""),
                "pcie_width": pcie.get("pcie_width", ""),
                "pcie_speed": pcie.get("pcie_speed", ""),
                "driver": pcie.get("driver", ""),
            })
    return devs


def _read_pci_sysfs(bdf: str) -> Dict[str, str]:
    base = f"/sys/bus/pci/devices/{bdf}"
    info = {"pcie_gen": "", "pcie_width": "", "pcie_speed": "", "driver": ""}
    if not os.path.isdir(base):
        return info
    try:
        drv_link = os.path.realpath(os.path.join(base, "driver"))
        if os.path.basename(drv_link) != "driver":
            info["driver"] = os.path.basename(drv_link)
    except Exception:
        pass
    try:
        with open(os.path.join(base, "current_link_speed")) as f:
            spd = f.read().strip()
        info["pcie_speed"] = spd
        gen_map = {"2.5 GT/s": "1", "5.0 GT/s": "2", "8.0 GT/s": "3", "16.0 GT/s": "4", "32.0 GT/s": "5", "64.0 GT/s": "6"}
        for prefix, gen in gen_map.items():
            if spd.startswith(prefix):
                info["pcie_gen"] = gen
                break
    except Exception:
        pass
    try:
        with open(os.path.join(base, "current_link_width")) as f:
            w = f.read().strip()
        if w.isdigit():
            info["pcie_width"] = w
    except Exception:
        pass
    return info


def _parse_mem_mb(mem_str: str) -> Optional[int]:
    m = re.match(r"(\d+(?:\.\d+)?)\s*(GiB|GB|MiB|MB)", mem_str or "", re.I)
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    return int(round(val * 1024)) if unit.startswith("g") else int(round(val))


def parse_axdevice_output(text: str) -> List[Dict]:
    """Parse la sortie de `axdevice`.

    Exemples supportés (docs.axelera.ai v1.6 → v1.9) :
    - Device 0: metis-0:1:0 130p 4GiB flver=1.3.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%
    - Device 1: metis-0:6c:0 part of 150p 4GiB flver=1.3.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%
    - Device 0: metis-0:2:0 113m std 8GiB flver=1.6.0 bcver=1.4 clock=800MHz(0-3:800MHz) mvm=0-3:100%
    """
    devices: List[Dict] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        m = re.match(r"^Device\s+(\d+)\s*:\s*(\S+)\s+(.+)$", line, re.I)
        if not m:
            continue
        idx = int(m.group(1))
        name = m.group(2)
        rest = m.group(3)

        # Bus PCI à partir de metis-0:1:0 (domain:bus:dev), metis-0:6c:0 ou metis-0000:01:00.0
        bdf = ""
        bm_full = re.search(r"metis-(?:([0-9a-fA-F]{4}):)?([0-9a-fA-F]{2}):([0-9a-fA-F]{2})\.([0-7])$", name)
        bm_ax = re.search(r"metis-([0-9a-fA-F]+):([0-9a-fA-F]+):([0-9a-fA-F]+)$", name)
        if bm_full:
            dom = bm_full.group(1) or "0000"
            bdf = f"{dom}:{bm_full.group(2)}:{bm_full.group(3)}.{bm_full.group(4)}".lower()
        elif bm_ax:
            dom = bm_ax.group(1).zfill(4)
            bus = bm_ax.group(2).zfill(2)
            slot = bm_ax.group(3).zfill(2)
            bdf = f"{dom}:{bus}:{slot}.0".lower()

        mem_m = re.search(r"\b(\d+(?:\.\d+)?\s*(?:GiB|GB|MiB|MB))\b", rest, re.I)
        mem_str = mem_m.group(1).replace(" ", "") if mem_m else ""
        mem_mb = _parse_mem_mb(mem_str)

        model = ""
        if mem_m:
            model = rest[:mem_m.start()].strip()
        else:
            km = re.search(r"\b(?:flver|bcver|clock|mvm)=", rest)
            model = rest[:km.start()].strip() if km else rest.split()[0]

        flver = (re.search(r"flver=([^\s]+)", rest) or [None, ""])[1]
        bcver = (re.search(r"bcver=([^\s]+)", rest) or [None, ""])[1]
        clk_m = re.search(r"clock=(\d+)\s*MHz(?:\(([^)]+)\))?", rest, re.I)
        clock_mhz = int(clk_m.group(1)) if clk_m else None
        core_clocks = clk_m.group(2) if clk_m and clk_m.group(2) else ""
        mvm_m = re.search(r"mvm=([^\s]+)", rest, re.I)
        mvm_spec = mvm_m.group(1) if mvm_m else ""
        mvm_pct = _parse_mvm_avg(mvm_spec)

        devices.append({
            "index": idx,
            "name": name,
            "bdf": bdf,
            "model": model or "Metis",
            "mem_str": mem_str,
            "mem_mb": mem_mb,
            "flver": flver,
            "bcver": bcver,
            "clock_mhz": clock_mhz,
            "core_clocks": core_clocks,
            "mvm_spec": mvm_spec,
            "mvm_pct": mvm_pct,
            "raw": line,
        })
    return devices


def _parse_mvm_avg(spec: str) -> Optional[float]:
    """Extrait une moyenne MVM % depuis `0-3:100%` ou `0:100%,1-3:50%`."""
    if not spec:
        return None
    vals = []
    for m in re.finditer(r"(?:(\d+)(?:-(\d+))?:)?(\d+(?:\.\d+)?)%", spec):
        v = float(m.group(3))
        if m.group(1) is not None and m.group(2) is not None:
            cnt = max(1, int(m.group(2)) - int(m.group(1)) + 1)
            vals.extend([v] * cnt)
        else:
            vals.append(v)
    return round(sum(vals) / len(vals), 1) if vals else None


def parse_axdevice_verbose(text: str) -> Dict[int, Dict]:
    """Parse des infos complémentaires éventuelles de `axdevice -v` (throttling, power limit, température)."""
    out: Dict[int, Dict] = {}
    cur = 0
    for raw in (text or "").splitlines():
        s = raw.strip()
        m = re.match(r"^Device\s+(\d+)\b", s, re.I)
        if m:
            cur = int(m.group(1))
            out.setdefault(cur, {})
            continue
        if not s:
            continue
        pm = re.search(r"power\s*limit\s*[:=]\s*([\d.]+)\s*W", s, re.I)
        if pm:
            out.setdefault(cur, {})["power_limit_w"] = float(pm.group(1))
        tm = re.search(r"(?:temp|temperature)\s*[:=]\s*([\d.]+)\s*°?C", s, re.I)
        if tm:
            out.setdefault(cur, {})["temp_c"] = float(tm.group(1))
        sm = re.search(r"(?:sw[_\s-]*throttl\w*|throttling)\s*[:=]\s*(.+)", s, re.I)
        if sm:
            out.setdefault(cur, {})["throttling_cfg"] = sm.group(1).strip()
    return out


def list_devices(timeout: float = 5.0) -> List[Dict]:
    """Fusionne la découverte `axdevice`, `lspci` (1f9d) et le module noyau (`metis`)."""
    global _LAST_AXDEVICE_ERROR
    drv = driver_info()
    pcis = pci_devices()
    axdev_bin = find_tool("axdevice")
    ax_devs: List[Dict] = []
    verbose_map: Dict[int, Dict] = {}
    _LAST_AXDEVICE_ERROR = ""
    if axdev_bin:
        rc, out, err = _run([axdev_bin], timeout=timeout)
        if rc == 0 and out:
            ax_devs = parse_axdevice_output(out)
        elif rc != 0:
            raw_err = (err or out or f"code retour {rc}").strip()
            _LAST_AXDEVICE_ERROR = raw_err.splitlines()[-1][:180] if raw_err else f"code {rc}"
        if ax_devs:
            rc_v, out_v, _ = _run([axdev_bin, "-v"], timeout=timeout)
            if rc_v == 0 and out_v:
                verbose_map = parse_axdevice_verbose(out_v)

    drv_label = drv.get("name") or ("metis" if drv["loaded"] else "")
    result: List[Dict] = []
    used_bdfs = set()
    for d in ax_devs:
        bdf = d.get("bdf", "")
        pci_match = next((p for p in pcis if _short_bdf(p["bdf"]) == _short_bdf(bdf)), None)
        if pci_match:
            used_bdfs.add(_short_bdf(pci_match["bdf"]))
        item = {
            **d,
            "bdf": (pci_match["bdf"] if pci_match else bdf) or "",
            "pci_desc": pci_match["desc"] if pci_match else "",
            "pcie_gen": pci_match["pcie_gen"] if pci_match else "",
            "pcie_width": pci_match["pcie_width"] if pci_match else "",
            "pcie_speed": pci_match["pcie_speed"] if pci_match else "",
            "driver": (pci_match["driver"] if pci_match else "") or drv_label,
            "driver_version": drv["version"],
            "axdevice_ok": True,
        }
        if d["index"] in verbose_map:
            item.update(verbose_map[d["index"]])
        result.append(item)

    # Cartes vues sur le bus PCIe mais non listées par axdevice (SDK absent, driver non chargé ou erreur BAR/DMA)
    for p in pcis:
        if _short_bdf(p["bdf"]) in used_bdfs:
            continue
        idx = len(result)
        result.append({
            "index": idx,
            "name": f"metis-{_short_bdf(p['bdf'])}",
            "bdf": p["bdf"],
            "model": "Metis AIPU (axdevice absent)" if not axdev_bin else "Metis AIPU (absent d'axdevice)",
            "mem_str": "",
            "mem_mb": None,
            "flver": "",
            "bcver": "",
            "clock_mhz": None,
            "core_clocks": "",
            "mvm_spec": "",
            "mvm_pct": None,
            "pci_desc": p["desc"],
            "pcie_gen": p["pcie_gen"],
            "pcie_width": p["pcie_width"],
            "pcie_speed": p["pcie_speed"],
            "driver": p["driver"] or drv_label,
            "driver_version": drv["version"],
            "axdevice_ok": False,
        })
    return result


def _short_bdf(bdf: str) -> str:
    parts = (bdf or "").strip().lower().split(":")
    return ":".join(parts[-2:]) if len(parts) >= 2 else ""


def enrich(gpu: Dict) -> None:
    """Complète une fiche de `system_info.get_gpu_info()` pour une carte Axelera (lecture seule)."""
    try:
        drv = driver_info()
        if drv["version"] and not gpu.get("driver_version"):
            gpu["driver_version"] = drv["version"]
        if drv["loaded"] and not gpu.get("pilote"):
            gpu["pilote"] = "metis"

        bdf = gpu.get("pci_bus", "")
        devs = list_devices(timeout=4.0)
        match = next((d for d in devs if _short_bdf(d.get("bdf", "")) == _short_bdf(bdf)), None)
        if match:
            if match.get("model") and "non initialisé" not in match["model"]:
                gpu["modele"] = f"Axelera {match['model']} ({match.get('name', '')})".strip()
            if match.get("mem_mb"):
                gpu["vram_total_mo"] = str(match["mem_mb"])
            if match.get("flver"):
                gpu["firmware_version"] = match["flver"]
            if match.get("bcver"):
                gpu["board_controller_version"] = match["bcver"]
            if match.get("clock_mhz"):
                gpu["clock_mhz"] = str(match["clock_mhz"])
            if match.get("power_limit_w") and not gpu.get("puissance_max_w"):
                gpu["puissance_max_w"] = f"{match['power_limit_w']:.1f}"
        telem = poll_telemetry(timeout=2.5)
        idx = match["index"] if match else 0
        tdev = (telem.get("devices") or {}).get(idx)
        if tdev:
            if tdev.get("temp_c") is not None:
                gpu["temperature_c"] = f"{tdev['temp_c']:.0f}"
            if tdev.get("power_w") is not None:
                gpu["puissance_w"] = f"{tdev['power_w']:.1f}"
        if not find_tool("axdevice"):
            gpu["axelera_note"] = "axdevice (Voyager SDK) introuvable : seules les infos PCIe/pilote sont lues."
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Télémétrie live (axmonitor, axdevice, hwmon, axlogdevice)
# ─────────────────────────────────────────────────────────────────────────────

def parse_axmonitor_print(text: str) -> Dict[int, Dict]:
    """Parse la sortie texte de `axmonitor --ui console -c print`.

    Gère les variantes de mise en forme tabulaire / sections DEV0, DEV1 ou Device 0.
    Retourne {device_idx: {temp_c, temp_cores, power_w, clock_mhz, mvm_pct, kps, ddr_used_mb, pcie_bw_mbs}}.
    """
    devices: Dict[int, Dict] = {}
    if not text or not text.strip():
        return devices
    cur_idx = 0
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        dm = re.search(r"\b(?:DEV|Device)\s*(\d+)\b", line, re.I)
        if dm and (line.upper().startswith(("DEV", "DEVICE")) or "===" in line or "---" in line or ":" in line):
            cur_idx = int(dm.group(1))
        d = devices.setdefault(cur_idx, {})

        # Températures : ex. "Core Temp: 42.5 C", "Temp: 41°C", "core_temps=[35,34,34,35,34]"
        cm = re.search(r"core_temps\s*=\s*\[([^\]]+)\]", line, re.I)
        if cm:
            vals = [float(x.strip()) for x in cm.group(1).split(",") if re.match(r"^-?\d+(?:\.\d+)?$", x.strip())]
            if vals:
                d["temp_cores"] = vals
                d["temp_c"] = max(vals)
        elif re.search(r"(?:temp|temperature)", line, re.I):
            temps = [float(x) for x in re.findall(r"(-?\d+(?:\.\d+)?)\s*°?\s*C\b", line, re.I)]
            if temps:
                d["temp_c"] = max(temps) if len(temps) > 1 else temps[0]
                if len(temps) > 1:
                    d["temp_cores"] = temps

        # Puissance : ex. "Power: 8.4 W"
        if re.search(r"\bpower\b", line, re.I):
            pm = re.search(r"(\d+(?:\.\d+)?)\s*(mW|W)\b", line, re.I)
            if pm:
                pw = float(pm.group(1))
                d["power_w"] = round(pw / 1000.0, 2) if pm.group(2).lower() == "mw" else round(pw, 2)

        # Horloge : ex. "Clock: 800 MHz"
        if re.search(r"\b(?:clock|freq|frequency)\b", line, re.I):
            fm = re.search(r"(\d+(?:\.\d+)?)\s*(GHz|MHz)\b", line, re.I)
            if fm:
                fv = float(fm.group(1))
                d["clock_mhz"] = round(fv * 1000.0 if fm.group(2).lower() == "ghz" else fv, 1)

        # Utilisation MVM : ex. "MVM: 98.5 %" / "MVM utilization: 100%"
        if re.search(r"\bmvm\b", line, re.I):
            mm = re.search(r"(\d+(?:\.\d+)?)\s*%", line)
            if mm:
                d["mvm_pct"] = float(mm.group(1))

        # KPS / FPS : ex. "KPS: 1450" / "Total KPS: 420.5"
        if re.search(r"\b(?:kps|fps)\b", line, re.I):
            km = re.search(r"(\d+(?:\.\d+)?)\s*(?:kps|fps)?\b", line.split(":", 1)[-1] if ":" in line else line, re.I)
            if km:
                d["kps"] = float(km.group(1))

        # Mémoire DDR : ex. "DDR: 512 MB / 4096 MB" ou "Memory: 1.2 GiB"
        if re.search(r"\b(?:ddr|memory|mem)\b", line, re.I):
            dm_mem = re.search(r"(\d+(?:\.\d+)?)\s*(GiB|GB|MiB|MB)\b", line, re.I)
            if dm_mem:
                mv = float(dm_mem.group(1))
                d["ddr_used_mb"] = round(mv * 1024.0 if dm_mem.group(2).lower().startswith("g") else mv, 1)

        # PCIe DMA bandwidth : ex. "PCIe: 1250.4 MB/s"
        if re.search(r"\b(?:pcie|dma)\b", line, re.I):
            bwm = re.search(r"(\d+(?:\.\d+)?)\s*(GB/s|MB/s)\b", line, re.I)
            if bwm:
                bv = float(bwm.group(1))
                d["pcie_bw_mbs"] = round(bv * 1000.0 if bwm.group(2).lower().startswith("g") else bv, 1)

    return {k: v for k, v in devices.items() if v}


def parse_axlogdevice_line(line: str) -> Optional[Dict]:
    """Parse une ligne `axlogdevice` (`core_temps=[35,34,34,35,34]` ou événement thermique)."""
    if not line:
        return None
    m = re.search(r"core_temps\s*=\s*\[([^\]]+)\]", line, re.I)
    if m:
        vals = []
        for x in m.group(1).split(","):
            try:
                vals.append(float(x.strip()))
            except ValueError:
                pass
        if vals:
            return {"temp_cores": vals, "temp_c": max(vals), "board_temp_c": vals[0]}
    return None


def _read_hwmon_for_axelera(bdfs: List[str]) -> Dict[int, Dict]:
    """Lit d'éventuelles sondes hwmon liées aux cartes PCI 1f9d (lecture seule sans root)."""
    out: Dict[int, Dict] = {}
    for idx, bdf in enumerate(bdfs):
        if not bdf:
            continue
        hwdir = f"/sys/bus/pci/devices/{bdf}/hwmon"
        if not os.path.isdir(hwdir):
            continue
        d = {}
        try:
            for hname in sorted(os.listdir(hwdir)):
                hp = os.path.join(hwdir, hname)
                temps = []
                for tf in sorted(glob.glob(os.path.join(hp, "temp*_input"))):
                    try:
                        with open(tf) as f:
                            temps.append(int(f.read().strip()) / 1000.0)
                    except Exception:
                        pass
                if temps:
                    d["temp_c"] = max(temps)
                    d["temp_cores"] = temps
                for pf in sorted(glob.glob(os.path.join(hp, "power*_input")) + glob.glob(os.path.join(hp, "power*_average"))):
                    try:
                        with open(pf) as f:
                            d["power_w"] = round(int(f.read().strip()) / 1e6, 2)
                        break
                    except Exception:
                        pass
        except Exception:
            pass
        if d:
            out[idx] = d
    return out


def poll_telemetry(timeout: float = 2.5, server_address: str = "127.0.0.1:5555") -> Dict:
    """Collecte la télémétrie courante de toutes les cartes Axelera (axmonitor + axdevice + hwmon)."""
    global _LAST_TELEM_ERROR
    devices: Dict[int, Dict] = {}
    source_parts: List[str] = []
    server_ok = False
    err = ""

    # 1. axmonitor one-shot (si installé et axsystemserver actif)
    axmon = find_tool("axmonitor")
    if axmon:
        rc, out, serr = _run(
            [axmon, "--server-address", server_address, "--ui", "console", "-c", "print"],
            timeout=timeout,
        )
        if rc == 0 and out:
            parsed = parse_axmonitor_print(out)
            if parsed:
                server_ok = True
                source_parts.append("axmonitor")
                for idx, vals in parsed.items():
                    devices.setdefault(idx, {}).update(vals)
        elif serr:
            err = serr.splitlines()[0][:140]

    # 2. axdevice (toujours utile pour horloge réelle, MVM %, modèle et détection throttling horloge)
    axdev = find_tool("axdevice")
    if axdev:
        rc, out, derr = _run([axdev], timeout=timeout)
        if rc == 0 and out:
            source_parts.append("axdevice")
            for item in parse_axdevice_output(out):
                idx = item["index"]
                d = devices.setdefault(idx, {})
                if item.get("clock_mhz") is not None and "clock_mhz" not in d:
                    d["clock_mhz"] = float(item["clock_mhz"])
                if item.get("mvm_pct") is not None and "mvm_pct" not in d:
                    d["mvm_pct"] = float(item["mvm_pct"])
                d["model"] = item.get("model", "")
                d["name"] = item.get("name", "")
                d["bdf"] = item.get("bdf", "")
                d["mem_mb"] = item.get("mem_mb")
                d["flver"] = item.get("flver", "")
                d["bcver"] = item.get("bcver", "")
                # Indice de throttling thermique si temp >= 105 °C ou horloge abaissée alors que MVM est limité
                if d.get("temp_c") is not None and d["temp_c"] >= TEMP_HW_THROTTLE_C:
                    d["throttle_thermal"] = 1
                elif d.get("clock_mhz") is not None and d["clock_mhz"] < 700 and d.get("temp_c", 0) >= TEMP_WARNING_C:
                    d["throttle_thermal"] = 1
                else:
                    d.setdefault("throttle_thermal", 0)
        elif derr and not err:
            err = derr.splitlines()[0][:140]

    # 3. hwmon PCI sysfs éventuel
    pcis = pci_devices()
    if pcis:
        hw = _read_hwmon_for_axelera([p["bdf"] for p in pcis])
        for idx, vals in hw.items():
            d = devices.setdefault(idx, {})
            for k, v in vals.items():
                d.setdefault(k, v)
        if hw:
            source_parts.append("hwmon")

    _LAST_TELEM_ERROR = err
    return {
        "devices": devices,
        "source": " + ".join(source_parts) if source_parts else "aucune",
        "server_ok": server_ok,
        "error": err,
    }


class AxeleraSampler:
    """Échantillonne la télémétrie Axelera en arrière-plan (≈ 1 Hz) pour ne pas bloquer le benchmark."""

    def __init__(self, interval_s: float = 1.0, server_address: str = "127.0.0.1:5555"):
        self._interval_s = max(0.5, float(interval_s))
        self._server_address = server_address
        self._stop = threading.Event()
        self.ready = threading.Event()
        self._lock = threading.Lock()
        self._latest: Dict = {}
        self._thread: Optional[threading.Thread] = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.ready.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        while not self._stop.is_set():
            snap = poll_telemetry(timeout=2.5, server_address=self._server_address)
            with self._lock:
                self._latest = snap
            self.ready.set()
            self._stop.wait(self._interval_s)

    def latest(self) -> Dict:
        with self._lock:
            return dict(self._latest)


# ─────────────────────────────────────────────────────────────────────────────
# Découverte de modèles IA & médias de test
# ─────────────────────────────────────────────────────────────────────────────

def discover_models(sdk_dir: Optional[str] = None) -> List[Dict]:
    """Découvre les modèles utilisables :
    1) modèles compilés locaux (`model.json`, `.axm`, `.axe`) → prêts pour `axrunmodel` ;
    2) modèles Zoo / YAML du SDK (`ax_models/**/*.yaml` et `axzoo list`) → utilisables avec `inference.py`.
    """
    models: List[Dict] = []
    seen_keys = set()
    sdk = sdk_dir or find_sdk_dir()

    # 1. Modèles compilés locaux (build/**/model.json, ~/.cache/axelera/**/model.json, etc.)
    search_roots = []
    if sdk:
        search_roots.extend([os.path.join(sdk, "build"), os.path.join(sdk, "models"), os.path.join(sdk, "exported")])
    for home in _candidate_homes():
        search_roots.extend([
            os.path.join(home, ".cache", "axelera"),
            os.path.join(home, "axelera_models"),
            os.path.join(home, "build"),
        ])

    for root in search_roots:
        if not os.path.isdir(root):
            continue
        for cur, dirs, files in os.walk(root):
            depth = cur[len(root):].count(os.sep)
            if depth > 4:
                dirs[:] = []
                continue
            for fn in files:
                if fn == "model.json" or fn.endswith((".axm", ".axe")):
                    full = os.path.join(cur, fn)
                    if full in seen_keys:
                        continue
                    seen_keys.add(full)
                    rel = os.path.relpath(cur, root)
                    label = rel if fn == "model.json" else os.path.join(rel, fn)
                    models.append({
                        "name": label,
                        "path": full,
                        "kind": "compiled",
                        "engine": "axrunmodel",
                        "label": f"[Compilé] {label}",
                    })
            if len(models) >= 80:
                break

    # 2. Modèles YAML du Voyager SDK (ax_models/)
    if sdk and os.path.isdir(os.path.join(sdk, "ax_models")):
        axm_dir = os.path.join(sdk, "ax_models")
        for cur, dirs, files in os.walk(axm_dir):
            depth = cur[len(axm_dir):].count(os.sep)
            if depth > 3:
                dirs[:] = []
                continue
            for fn in sorted(files):
                if not fn.endswith((".yaml", ".yml")):
                    continue
                stem = os.path.splitext(fn)[0]
                full = os.path.join(cur, fn)
                if stem in seen_keys:
                    continue
                seen_keys.add(stem)
                models.append({
                    "name": stem,
                    "path": full,
                    "kind": "zoo_yaml",
                    "engine": "inference",
                    "label": f"[Zoo YAML] {stem}",
                })
                if len(models) >= 160:
                    break

    # 3. Modèles `axzoo list` (v1.8+) si disponible
    axzoo = find_tool("axzoo")
    if axzoo:
        rc, out, _ = _run([axzoo, "list"], timeout=4)
        if rc == 0 and out:
            for line in out.splitlines():
                tok = line.strip().split()[0] if line.strip() else ""
                if not tok or tok.startswith(("-", "=", "#")) or tok.lower() in ("model", "name"):
                    continue
                if tok not in seen_keys:
                    seen_keys.add(tok)
                    models.append({
                        "name": tok,
                        "path": tok,
                        "kind": "axzoo",
                        "engine": "inference",
                        "label": f"[axzoo] {tok}",
                    })
                if len(models) >= 200:
                    break

    return models


def discover_media(sdk_dir: Optional[str] = None) -> List[str]:
    """Trouve les vidéos / images de test présentes dans le dossier `media/` du SDK."""
    sdk = sdk_dir or find_sdk_dir()
    found: List[str] = []
    if not sdk:
        return found
    mdir = os.path.join(sdk, "media")
    if not os.path.isdir(mdir):
        return found
    for fn in sorted(os.listdir(mdir)):
        if fn.lower().endswith((".mp4", ".mkv", ".avi", ".mov", ".jpg", ".jpeg", ".png")):
            found.append(os.path.join(mdir, fn))
    return found


def validate_model_path(path: str) -> Tuple[bool, str]:
    """Vérifie qu'un chemin de modèle compilé ou YAML est exploitable."""
    if not path or not str(path).strip():
        return False, "Aucun modèle sélectionné."
    p = os.path.expanduser(str(path).strip())
    if os.path.isfile(p):
        if p.endswith(".json"):
            try:
                with open(p, encoding="utf-8") as f:
                    json.load(f)
                return True, p
            except Exception as exc:
                return False, f"JSON invalide ({exc})"
        return True, p
    # Nom de modèle Zoo (ex. yolov8s-coco-onnx) sans chemin local
    if re.match(r"^[A-Za-z0-9._-]+$", p):
        return True, p
    return False, f"Fichier ou modèle introuvable : {p}"


def download_zoo_model(model_name: str, target: str = "metis",
                       progress_cb: Optional[Callable[[str], None]] = None,
                       timeout: int = 300) -> Tuple[bool, str]:
    """Télécharge un modèle précompilé via `axdownloadmodel` (action explicite utilisateur)."""
    bin_path = find_tool("axdownloadmodel")
    if not bin_path:
        return False, "axdownloadmodel introuvable (Voyager SDK non détecté)."
    name = (model_name or "").strip()
    if not name:
        return False, "Nom de modèle vide."
    cmd = [bin_path, name]
    if target:
        cmd.extend(["--target", target])
    sdk = find_sdk_dir()
    if progress_cb:
        progress_cb(f"$ {' '.join(cmd)}")
    rc, out, err = _run(cmd, timeout=timeout, cwd=sdk or None)
    msg = (out + "\n" + err).strip()
    if rc == 0:
        return True, msg or f"Modèle « {name} » téléchargé."
    return False, msg or f"Échec axdownloadmodel (code {rc})"


# ─────────────────────────────────────────────────────────────────────────────
# Réglages matériels via axdevice
# ─────────────────────────────────────────────────────────────────────────────

def _device_flag(device: Optional[str | int]) -> List[str]:
    if device is None or str(device).strip() == "":
        return []
    return [f"-d{str(device).strip()}"]


def equivalent_commands(device: Optional[str | int] = None,
                        clock_mhz: Optional[int] = None,
                        mvm_pct: Optional[int] = None,
                        sw_throttle: Optional[Tuple[int, int, int]] = None,
                        power_limit_w: Optional[float] = None) -> List[str]:
    """Retourne les commandes `axdevice` exactes à copier pour reproduire les réglages."""
    df = " ".join(_device_flag(device))
    prefix = f"axdevice {df}".strip()
    cmds: List[str] = []
    if clock_mhz is not None:
        cmds.append(f"{prefix} --set-clock {int(clock_mhz)}")
    if mvm_pct is not None:
        cmds.append(f"{prefix} --set-mvm-limitation {int(mvm_pct)}")
    if sw_throttle is not None:
        t, h, l = sw_throttle
        cmds.append(f"{prefix} --set-sw-throttling={int(t)}:{int(h)}:{int(l)}")
    if power_limit_w is not None:
        cmds.append(f"{prefix} --set-power-limit {power_limit_w:g}")
    return cmds


def _run_axdevice_cmd(args: List[str], timeout: float = 15.0) -> Tuple[bool, str]:
    axdev = find_tool("axdevice")
    if not axdev:
        return False, "axdevice introuvable (Voyager SDK requis)."
    rc, out, err = _run([axdev] + args, timeout=timeout)
    if rc == 0:
        return True, out or "OK"
    # Repli élévation si permission refusée sur /dev/metis-* ou rescan PCIe
    combined = (err or out or "").lower()
    if any(k in combined for k in ("permission", "denied", "root", "privilege", "operation not permitted")):
        try:
            from core.privileged import run_elevated
            res = run_elevated([axdev] + args, timeout=max(30, int(timeout)), retry_on_failure=False)
            if res.returncode == 0:
                return True, (res.stdout or "").strip() or "OK (avec élévation)"
            return False, (res.stderr or res.stdout or f"code {res.returncode}").strip()
        except Exception as exc:
            return False, str(exc)
    return False, (err or out or f"axdevice code {rc}").strip()


def set_clock(device: Optional[str | int], mhz: int) -> Tuple[bool, str]:
    if int(mhz) not in VALID_CLOCKS_MHZ:
        return False, f"Fréquence invalide ({mhz} MHz) — valeurs autorisées : {VALID_CLOCKS_MHZ}"
    return _run_axdevice_cmd(_device_flag(device) + ["--set-clock", str(int(mhz))])


def set_mvm_limitation(device: Optional[str | int], pct: int) -> Tuple[bool, str]:
    pct_i = max(1, min(100, int(pct)))
    return _run_axdevice_cmd(_device_flag(device) + ["--set-mvm-limitation", str(pct_i)])


def set_sw_throttling(device: Optional[str | int], temp_c: int, hyst_c: int, mvm_pct: int) -> Tuple[bool, str]:
    t = max(40, min(200, int(temp_c)))
    h = max(1, min(50, int(hyst_c)))
    l = max(1, min(100, int(mvm_pct)))
    return _run_axdevice_cmd(_device_flag(device) + [f"--set-sw-throttling={t}:{h}:{l}"])


def set_power_limit(device: Optional[str | int], watts: float) -> Tuple[bool, str]:
    w = max(0.0, float(watts))
    return _run_axdevice_cmd(_device_flag(device) + ["--set-power-limit", f"{w:g}"])


def refresh_devices() -> Tuple[bool, str]:
    """Lance `axdevice --refresh` (rescan PCIe + rechargement firmware)."""
    return _run_axdevice_cmd(["--refresh"], timeout=30.0)
