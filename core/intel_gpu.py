"""
intel_gpu.py — GPU Intel (Arc, Data Center GPU Flex / Max) via xpu-smi (Intel XPU Manager).

xpu-smi donne ce que sysfs/hwmon n'expose pas : nom exact, mémoire, UUID, température du cœur ET de la mémoire,
puissance, fréquence, utilisation du GPU et de la mémoire, débit mémoire, limite de puissance. Les droits root sont
généralement nécessaires : on essaie `sudo -n` (sans mot de passe) puis l'appel direct ; sans droits ni xpu-smi,
BenchSuite retombe sur sysfs et le dit (« non évalué », jamais un échec).

Lecture seule. Le format de sortie d'xpu-smi varie selon les versions : l'analyse accepte la sortie JSON (`-j`) comme
le tableau texte, et reconnaît les grandeurs par leur libellé.
"""

import json
import os
import re
import shutil
import subprocess
import threading
import time
from typing import Dict, List, Optional

_LOCK = threading.Lock()
_MODE: Optional[List[str]] = None          # préfixe qui fonctionne : [] (direct) ou ["sudo", "-n"]
_DISC: Dict = {"t": 0.0, "devices": []}
_LAST_ERROR = ""


def available() -> bool:
    return bool(shutil.which("xpu-smi"))


def last_error() -> str:
    """Dernière raison d'échec (vide si tout va bien) : à afficher comme remarque, pas comme erreur."""
    return _LAST_ERROR


def _run(args: List[str], timeout: int = 8) -> Optional[str]:
    """Exécute xpu-smi (sudo -n puis direct selon ce qui marche) ; None en cas d'échec ; jamais d'exception."""
    global _MODE, _LAST_ERROR
    if not available():
        _LAST_ERROR = "xpu-smi non installé (paquet xpu-smi du dépôt Intel GPU)"
        return None
    if os.geteuid() == 0:
        prefixes = [[]]
    elif _MODE is not None:
        prefixes = [_MODE]
    else:
        prefixes = [["sudo", "-n"], []] if shutil.which("sudo") else [[]]
    for pre in prefixes:
        try:
            r = subprocess.run(pre + ["xpu-smi"] + args, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            _LAST_ERROR = f"xpu-smi : {e}"
            continue
        out = (r.stdout or "").strip()
        if r.returncode == 0 and out:
            _MODE = pre
            _LAST_ERROR = ""
            return out
        _LAST_ERROR = ("xpu-smi : droits insuffisants (lancer avec sudo, ou sudo sans mot de passe)"
                       if "permission" in (r.stderr or "").lower() or "password" in (r.stderr or "").lower()
                       else f"xpu-smi : {(r.stderr or out or 'aucune sortie')[:120]}")
    return None


def _num(v) -> Optional[float]:
    """Nombre dans une valeur (« 43 », « 12.5 W », 'N/A') ; None si absent."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:[.,]\d+)?", str(v))
    return float(m.group(0).replace(",", ".")) if m and "n/a" not in str(v).lower() else None


def _norm(label: str) -> str:
    s = re.sub(r"^xpum_stats_", "", str(label).strip(), flags=re.I)
    return re.sub(r"[\s_]+", " ", s).strip().lower()


def classify(label: str) -> str:
    """Grandeur normalisée à partir d'un libellé xpu-smi (tableau, JSON ou chemin « temperature gpu core celsius ») ; '' si sans intérêt."""
    s = _norm(label)
    if not s:
        return ""
    if "temperature" in s or s.endswith(" temp") or " temp " in s:
        if "memory" in s or "vram" in s:
            return "temp_vram"
        if re.search(r"\bvr\b", s):
            return "temp_vr"           # régulateurs de tension de la carte
        return "temp"
    if "energy" in s:
        return ""
    if "voltage" in s:
        return "voltage_v"
    # Erreurs matérielles RAS / ECC (priorité stabilité / erreurs)
    if "reset" in s and ("counter" in s or s == "reset" or "ras" in s):
        return "ras_reset"
    if "programming" in s and "error" in s:
        return "ras_programming_errors"
    if "driver" in s and "error" in s:
        return "ras_driver_errors"
    if "cache" in s and "error" in s:
        if "uncorrectable" in s:
            return "ras_cache_uncorr"
        if "correctable" in s:
            return "ras_cache_corr"
    if ("memory" in s or "mem " in s or s.startswith("mem")) and "error" in s:
        if "uncorrectable" in s:
            return "ras_mem_uncorr"
        if "correctable" in s:
            return "ras_mem_corr"
    if "pcie" in s and ("read" in s or "write" in s):
        return "pcie_read_kbps" if "read" in s else "pcie_write_kbps"
    if "power" in s and "limit" not in s and "throttle" not in s:
        return "power"
    if "frequency" in s or "freq" in s:
        if any(w in s for w in ("min ", "max ", "efficient", "limit", "range", "rp0", "rp1", "rpe", "rpn")):
            return ""
        if "memory" in s or "vram" in s:
            return "freq_memory"
        if "media" in s:
            return "freq_media_request" if "request" in s else "freq_media"
        if "request" in s or "cur " in s or s.endswith(" cur"):
            return "freq_request"
        return "freq_graphics"
    if "memory" in s and "used" in s:
        return "mem_used_mib"
    if "memory" in s and "bandwidth" in s:
        return "mem_bw"
    if "memory" in s and "util" in s:
        return "mem_util"
    if "memory" in s and "read" in s:
        return "mem_read_kbps"
    if "memory" in s and "write" in s:
        return "mem_write_kbps"
    # Activité EU (Execution Units) et moteurs GPU
    if "eu" in s and "active" in s:
        return "eu_active"
    if "eu" in s and "stall" in s:
        return "eu_stall"
    if "eu" in s and "idle" in s:
        return "eu_idle"
    if "compute" in s and "util" in s:
        return "util_compute"
    if "render" in s and "util" in s:
        return "util_render"
    if "copy" in s and "util" in s:
        return "util_copy"
    if "media" in s and "util" in s and "em" not in s:
        return "util_media"
    if "3d" in s and "util" in s:
        return "util_3d"
    if "utilization" in s and ("gpu percent" in s or s.startswith("gpu utilization") or s.startswith("utilization gpu") or "all gpu engines" in s):
        return "util"
    if "gpu utilization" in s or s in ("gpu util", "utilization"):
        return "util"
    if "throttle" in s and ("thermal" in s or "reason" in s):
        return "throttle_thermal"
    return ""


_AGG_MAX = (
    "temp", "temp_vram", "temp_vr", "power", "voltage_v",
    "freq_graphics", "freq_request", "freq_memory", "freq_media", "freq_media_request",
    "mem_used_mib", "mem_bw", "mem_read_kbps", "mem_write_kbps",
    "pcie_read_kbps", "pcie_write_kbps",
    "throttle_thermal",
    "ras_reset", "ras_programming_errors", "ras_driver_errors",
    "ras_cache_corr", "ras_cache_uncorr", "ras_mem_corr", "ras_mem_uncorr",
)


def _collect_json(obj, out: Dict[str, float], tiles: Dict[str, List[float]], path: tuple = ()) -> None:
    """Parcourt un JSON xpu-smi. Formats gérés :
    • récent : {"temperature": {"gpu_core_celsius": {"tile_0": {"avg":…, "current": 31}}}} (nom = chemin) ;
    • ancien : [{"metrics_type": "XPUM_STATS_…", "value": 43}]."""
    if isinstance(obj, dict):
        label = obj.get("metrics_type") or obj.get("metricsType") or obj.get("metrics_name")
        if label is not None:                                   # ancien format
            val = None
            for k in ("value", "avg", "current", "max"):
                if k in obj:
                    val = _num(obj[k])
                    break
            key = classify(label)
            if key and val is not None:
                tiles.setdefault(key, []).append(val)
            return
        leaf = None
        for k in ("current", "avg"):
            if k in obj and not isinstance(obj[k], (dict, list)):
                leaf = _num(obj[k])
                break
        if leaf is not None:                                    # feuille de mesure : le nom vient du chemin
            key = classify(" ".join(x for x in path if not re.match(r"(tile|engine)_\d+$", x)))
            if key:
                tiles.setdefault(key, []).append(leaf)
            return
        for k, v in obj.items():
            _collect_json(v, out, tiles, path + (str(k),))
    elif isinstance(obj, list):
        for it in obj:
            _collect_json(it, out, tiles, path)
    else:                                                       # nombre nu (ex. utilization.gpu_percent.tile_0 = 0.0)
        n = _num(obj)
        if n is not None and path:
            key = classify(" ".join(x for x in path if not re.match(r"(tile|engine)_\d+$", x)))
            if key:
                tiles.setdefault(key, []).append(n)


def _tile_values(text: str) -> List[float]:
    """Valeurs d'une cellule de tableau texte : « Tile 0: avg: 51, …, current: 51 » → [51] (une valeur par tuile)."""
    vals = []
    parts = re.split(r"Tile\s*\d+\s*:", text)
    for seg in (parts[1:] if len(parts) > 1 else [text]):
        m = re.search(r"current:\s*(-?\d+(?:[.,]\d+)?)", seg) or re.search(r"(?<![\w.])(-?\d+(?:[.,]\d+)?)", seg)
        if m and "n/a" not in seg.lower():
            vals.append(float(m.group(1).replace(",", ".")))
    return vals


def _parse_stats(text: str) -> Dict[str, float]:
    """Mesures d'un `xpu-smi stats` (JSON ou tableau texte) → {temp, temp_vram, power, freq_graphics, util, …}."""
    tiles: Dict[str, List[float]] = {}
    try:
        _collect_json(json.loads(text), {}, tiles)
    except Exception:
        for line in text.splitlines():
            if "|" in line:                          # « | GPU Power (W) | Tile 0: … | »
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
            else:                                    # « GPU Power (W)          Tile 0: avg: 51, … »
                cells = [c.strip() for c in re.split(r"\s{2,}", line.strip(), maxsplit=1)]
            if len(cells) >= 2 and cells[0]:
                key = classify(cells[0])
                if key:
                    tiles.setdefault(key, []).extend(_tile_values(cells[1]))
    res: Dict[str, float] = {}
    if tiles.get("temp_vr") and max(tiles["temp_vr"]) <= 0:
        tiles.pop("temp_vr")           # xpu-smi renvoie 0 quand le capteur VR n'est pas lu : mesure absente, pas 0 °C
    for k, vals in tiles.items():
        if vals:
            # plusieurs tuiles : maximum pour températures/puissance/fréquence/débits, moyenne pour les pourcentages
            res[k] = max(vals) if k in _AGG_MAX else sum(vals) / len(vals)
    return res


def discovery(max_age_s: float = 60.0) -> List[Dict]:
    """GPU vus par xpu-smi : [{id, name, uuid, bdf, mem_mib}] (mis en cache 60 s) ; [] si indisponible."""
    with _LOCK:
        if time.time() - _DISC["t"] < max_age_s and _DISC["devices"]:
            return list(_DISC["devices"])
    devs: List[Dict] = []
    out = _run(["discovery", "-j"]) or _run(["discovery"])
    if out:
        try:
            data = json.loads(out)
            items = data.get("device_list") if isinstance(data, dict) else data
            for d in items or []:
                bdf = str(d.get("pci_bdf_address") or d.get("pci_bdf") or d.get("bdf") or "").lower()
                mem = _num(d.get("memory_physical_size_byte"))
                devs.append({"id": int(_num(d.get("device_id")) or 0), "name": str(d.get("device_name") or d.get("name") or ""),
                             "uuid": str(d.get("uuid") or ""), "bdf": bdf,
                             "mem_mib": int(mem / 1048576) if mem else None, "function": str(d.get("function_type") or "")})
        except Exception:
            cur: Dict = {}
            for line in out.splitlines():           # tableau texte : « | Device ID | 0 | », « | Device Name | … | »
                cells = [c.strip() for c in line.strip().strip("|").split("|")]
                if len(cells) < 2:
                    continue
                k, v = _norm(cells[0]), cells[1]
                if k == "device id":
                    if cur:
                        devs.append(cur)
                    cur = {"id": int(_num(v) or 0), "name": "", "uuid": "", "bdf": "", "mem_mib": None, "function": ""}
                elif cur:
                    if "device name" in k:
                        cur["name"] = v
                    elif k == "uuid":
                        cur["uuid"] = v
                    elif "bdf" in k:
                        cur["bdf"] = v.lower()
                    elif "memory physical size" in k:
                        n = _num(v)
                        cur["mem_mib"] = int(n) if n and "mib" in v.lower() else (int(n / 1048576) if n and n > 1e6 else (int(n * 1024) if n else None))
            if cur:
                devs.append(cur)
    with _LOCK:
        _DISC["t"], _DISC["devices"] = time.time(), devs
    return list(devs)


def device_for_bdf(bdf: str) -> Optional[Dict]:
    """GPU xpu-smi correspondant à une adresse PCI (comparaison sur bus:slot.fonction)."""
    key = str(bdf or "").lower().split(":", 1)[-1] if str(bdf or "").count(":") >= 2 else str(bdf or "").lower()
    for d in discovery():
        b = d["bdf"].split(":", 1)[-1] if d["bdf"].count(":") >= 2 else d["bdf"]
        if b and key and b == key:
            return d
    return None


def stats(dev_id: int, include_ras: bool = False) -> Dict[str, float]:
    """Mesures instantanées d'un GPU (voir _parse_stats) ; {} si indisponible."""
    extra = ["-r"] if include_ras else []
    out = _run(["stats", "-d", str(dev_id)] + extra + ["-j"], timeout=6) or _run(["stats", "-d", str(dev_id)] + extra, timeout=6)
    return _parse_stats(out) if out else {}


def health(dev_id: int) -> Dict[str, float]:
    """Seuils constructeur lus dans `xpu-smi health -d <id>` (température cœur/VRAM, puissance) ; {} si indisponible."""
    out = _run(["health", "-d", str(dev_id)], timeout=6)
    res: Dict[str, float] = {}
    if not out:
        return res
    section = ""
    for line in out.splitlines():
        low = line.lower()
        if "core temperature" in low:
            section = "core_temp"
        elif "memory temperature" in low:
            section = "mem_temp"
        elif "power" in low and ("3." in low or "health" in low or "domain" in low or "|" in low):
            if "throttle threshold" not in low:
                section = "power"
        m = re.search(r"throttle\s+threshold:\s*([\d.]+)", low)
        if m:
            val = float(m.group(1))
            if section == "core_temp" and val > 0:
                res["thermal_limit_c"] = val
            elif section == "mem_temp" and val > 0:
                res["vram_thermal_limit_c"] = val
            elif section == "power" and val > 0:
                res["power_throttle_w"] = val
    return res


def power_limit_w(dev_id: int) -> Optional[float]:
    """Limite de puissance de la carte (W) lue dans `xpu-smi config` : soutenue si connue, sinon « burst » ; None si absente."""
    out = _run(["config", "-d", str(dev_id)], timeout=6)
    in_card, vals = False, {}
    for line in (out or "").splitlines():
        low = line.lower()
        if "power domain" in low:
            in_card = "card" in low
            continue
        if in_card:
            m = re.search(r"(sustain|burst|peak)\(w\):\s*([\d.]+)", low)
            if m:
                vals[m.group(1)] = float(m.group(2))
    for k in ("sustain", "burst", "peak"):
        if vals.get(k):
            return vals[k]
    return None


def _vram_estimate_mib(st: Dict[str, float]) -> Optional[int]:
    """Taille mémoire estimée = utilisée / utilisation, arrondie au Gio le plus proche (si l'écart reste < 3 %) ; sinon None."""
    used, util = st.get("mem_used_mib"), st.get("mem_util")
    if not used or not util or util < 0.5:
        return None
    est = used / (util / 100.0)
    gib = round(est / 1024.0)
    return int(gib * 1024) if gib >= 1 and abs(est - gib * 1024) / (gib * 1024) < 0.03 else None


def enrich(gpu: Dict) -> Dict:
    """Complète la fiche d'un GPU Intel détecté par PCI (nom, mémoire, UUID, mesures, limites) ; ajoute une
    remarque `intel_xpu` quand xpu-smi manque ou refuse. Ne lève jamais d'exception."""
    try:
        if not available():
            gpu["intel_xpu"] = "xpu-smi non installé : informations limitées (sysfs). Voir ./cli.py --install-guide"
            return gpu
        d = device_for_bdf(gpu.get("pci_bus", ""))
        if not d:
            gpu["intel_xpu"] = last_error() or "GPU absent de xpu-smi"
            return gpu
        if d["name"]:
            gpu["nom"] = gpu["modele"] = d["name"]
        if d["mem_mib"]:
            gpu["vram_total_mo"] = str(d["mem_mib"])
        if d["uuid"]:
            gpu["serial"] = d["uuid"]
        gpu["xpu_device_id"] = str(d["id"])
        st = stats(d["id"])
        if "temp" in st:
            gpu["temperature_c"] = f"{st['temp']:.0f}"
        if "temp_vram" in st:
            gpu["vram_temp_c"] = f"{st['temp_vram']:.0f}"
        if not d["mem_mib"]:
            est = _vram_estimate_mib(st)
            if est:
                gpu["vram_total_mo"] = str(est)
                gpu["vram_note"] = "taille estimée (mémoire utilisée / utilisation) : xpu-smi ne la donne pas directement"
        pl = power_limit_w(d["id"])
        if pl:
            gpu["puissance_max_w"] = f"{pl:.0f}"
        hl = health(d["id"])
        if hl.get("thermal_limit_c") and not gpu.get("limite_thermique_c"):
            gpu["limite_thermique_c"] = gpu["thermal_limit_c"] = f"{hl['thermal_limit_c']:.0f}"
        if hl.get("vram_thermal_limit_c") and not gpu.get("limite_thermique_vram_c"):
            gpu["limite_thermique_vram_c"] = f"{hl['vram_thermal_limit_c']:.0f}"
        if hl.get("power_throttle_w") and not gpu.get("puissance_max_w"):
            gpu["puissance_max_w"] = f"{hl['power_throttle_w']:.0f}"
        if not st:
            gpu["intel_xpu"] = last_error() or "xpu-smi ne renvoie aucune mesure"
    except Exception:
        pass
    return gpu


def _read_file(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def hwmon_extras(bdf: str) -> Dict[str, float]:
    """Capteurs du pilote xe/i915 (hwmon, sans droits ni xpu-smi) : températures contrôleur mémoire / PCIe / canaux VRAM,
    ventilateurs (fan1..3), tension, limites de température et de puissance. {} si indisponible."""
    res: Dict[str, float] = {}
    try:
        from core.system_info import _find_hwmon_dir
        d = _find_hwmon_dir(bdf)
        if not d:
            return res
        ch: List[float] = []
        for name in sorted(os.listdir(d)):
            m = re.match(r"temp(\d+)_input$", name)
            if not m:
                continue
            label = _read_file(os.path.join(d, f"temp{m.group(1)}_label")).lower()
            v = _num(_read_file(os.path.join(d, name)))
            if v is None:
                continue
            v /= 1000.0
            if label == "mctrl":
                res["temp_mctrl"] = v
            elif label == "pcie":
                res["temp_pcie"] = v
            elif label.startswith("vram_ch"):
                ch.append(v)
            elif label == "vram":
                res["temp_vram_hwmon"] = v
                crit = _num(_read_file(os.path.join(d, f"temp{m.group(1)}_crit")))
                if crit:
                    res["temp_vram_crit_c"] = crit / 1000.0
            elif label == "pkg":
                res["temp_pkg"] = v
                crit = _num(_read_file(os.path.join(d, f"temp{m.group(1)}_crit")))
                if crit:
                    res["temp_crit_c"] = crit / 1000.0
        if ch:
            res["temp_vram_ch_max"] = max(ch)
        for idx, key in ((1, "fan_rpm"), (2, "fan2_rpm"), (3, "fan3_rpm")):
            fan = _num(_read_file(os.path.join(d, f"fan{idx}_input")))
            if fan is not None and (idx == 1 or fan > 0):
                res[key] = fan
        for vfile in ("in1_input", "in0_input"):
            mv = _num(_read_file(os.path.join(d, vfile)))
            if mv and mv > 0:
                res["voltage_v"] = round(mv / 1000.0, 3)
                break
        for src, dst in (("power1_cap", "power_cap_w"), ("power1_max", "power_sustain_w"),
                         ("power1_rated_max", "power_rated_w"), ("power1_crit", "power_crit_w")):
            w = _num(_read_file(os.path.join(d, src)))
            if w and w > 0:
                res[dst] = w / 1e6
    except Exception:
        pass
    return res


def xe_freqs(bdf: str, sysfs_pci_root: str = "/sys/bus/pci/devices") -> Dict[str, float]:
    """Fréquences et throttling du pilote Linux `xe` (sysfs, sans root ni xpu-smi) :
    - `tile0/gt0/freq0/act_freq` → `freq_graphics` (MHz réelle GT0 / Render-Compute)
    - `tile0/gt0/freq0/cur_freq` → `freq_request` (MHz demandée GT0)
    - `tile0/gt1/freq0/act_freq` → `freq_media` (MHz réelle GT1 / Media)
    - `tile0/gt1/freq0/cur_freq` → `freq_media_request` (MHz demandée GT1)
    - `tile0/gt0/freq0/throttle/reason_thermal` (ou `reason_vr_thermalert`) → `throttle_thermal` (0/1).
    {} si le périphérique n'expose pas l'arborescence `xe`.
    """
    res: Dict[str, float] = {}
    if not bdf:
        return res
    try:
        pci_dir = os.path.join(sysfs_pci_root, bdf)
        if not os.path.isdir(pci_dir):
            return res
        tile0 = os.path.join(pci_dir, "tile0")
        if not os.path.isdir(tile0):
            return res
        gt0_f = os.path.join(tile0, "gt0", "freq0")
        if os.path.isdir(gt0_f):
            act = _num(_read_file(os.path.join(gt0_f, "act_freq")))
            cur = _num(_read_file(os.path.join(gt0_f, "cur_freq")))
            if act is not None and act >= 0:
                res["freq_graphics"] = act
            if cur is not None and cur >= 0:
                res["freq_request"] = cur
            th_dir = os.path.join(gt0_f, "throttle")
            if os.path.isdir(th_dir):
                th_th = _num(_read_file(os.path.join(th_dir, "reason_thermal"))) or 0.0
                th_vr = _num(_read_file(os.path.join(th_dir, "reason_vr_thermalert"))) or 0.0
                th_pr = _num(_read_file(os.path.join(th_dir, "reason_prochot"))) or 0.0
                res["throttle_thermal"] = 1.0 if (th_th > 0 or th_vr > 0 or th_pr > 0) else 0.0
        gt1_f = os.path.join(tile0, "gt1", "freq0")
        if os.path.isdir(gt1_f):
            m_act = _num(_read_file(os.path.join(gt1_f, "act_freq")))
            m_cur = _num(_read_file(os.path.join(gt1_f, "cur_freq")))
            if m_act is not None and m_act >= 0:
                res["freq_media"] = m_act
            if m_cur is not None and m_cur >= 0:
                res["freq_media_request"] = m_cur
    except Exception:
        pass
    return res


def enrich_hwmon(gpu: Dict) -> Dict:
    """Complète la fiche d'un GPU Intel avec hwmon (limite thermique, température VRAM, limite de puissance) sans xpu-smi."""
    try:
        ex = hwmon_extras(gpu.get("pci_bus", ""))
        if ex.get("temp_pkg") is not None:           # « pkg » = température de la puce GPU (cœur)
            gpu["temperature_c"] = f"{ex['temp_pkg']:.0f}"
        if ex.get("temp_crit_c") and not gpu.get("limite_thermique_c"):
            gpu["limite_thermique_c"] = gpu["thermal_limit_c"] = f"{ex['temp_crit_c']:.0f}"
        if ex.get("temp_vram_crit_c") and not gpu.get("limite_thermique_vram_c"):
            gpu["limite_thermique_vram_c"] = f"{ex['temp_vram_crit_c']:.0f}"
        if ex.get("temp_vram_hwmon") and not gpu.get("vram_temp_c"):
            gpu["vram_temp_c"] = f"{ex['temp_vram_hwmon']:.0f}"
        pl = ex.get("power_sustain_w") or ex.get("power_rated_w") or ex.get("power_cap_w")
        if pl and not gpu.get("puissance_max_w"):
            gpu["puissance_max_w"] = f"{pl:.0f}"
        if ex.get("fan_rpm"):   # 0 tr/min = capteur non lu : on n'affiche rien
            gpu["ventilateur_rpm"] = f"{ex['fan_rpm']:.0f}"
    except Exception:
        pass
    return gpu


class XpuSampler:
    """Interroge xpu-smi en arrière-plan (≈ 1 fois par seconde et par GPU) pour ne jamais bloquer la boucle de télémétrie."""

    def __init__(self):
        self._vals: Dict[int, Dict[str, float]] = {}
        self._stop = threading.Event()
        self._th: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self._tick = 0

    def start(self, dev_ids: List[int]) -> None:
        if self._th or not dev_ids or not available():
            return
        self._ids = list(dev_ids)
        self._th = threading.Thread(target=self._loop, daemon=True)
        self._th.start()

    def _loop(self) -> None:
        while not self._stop.is_set():
            for i in self._ids:
                v = stats(i)
                # Compléter avec les compteurs RAS/ECC si absents de la sortie standard (toutes les 5 itérations)
                if v and (self._tick % 5 == 0) and not any(k.startswith("ras_") for k in v):
                    rv = stats(i, include_ras=True)
                    for rk, rval in rv.items():
                        if rk.startswith("ras_"):
                            v[rk] = rval
                with self._lock:
                    if v:
                        # Préserver les derniers compteurs RAS connus entre deux interrogations -r
                        prev = self._vals.get(i, {})
                        for rk, rval in prev.items():
                            if rk.startswith("ras_") and rk not in v:
                                v[rk] = rval
                        self._vals[i] = v
            self._tick += 1
            self._stop.wait(0.5)

    def latest(self, dev_id: int) -> Dict[str, float]:
        with self._lock:
            return dict(self._vals.get(dev_id, {}))

    def stop(self) -> None:
        self._stop.set()
