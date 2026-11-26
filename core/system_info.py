"""
system_info.py — Détection matériel complète
CPU, GPU (NVIDIA/AMD/Intel), RAM, Carte mère, Stockage, Réseau, OS
Utilisé pour la page "Informations système" de BenchSuite
"""

import subprocess
import os
import re
import platform
from datetime import datetime
try:
    from core.bmc_sensors import collect_memory_sensors, summarize_memory_sensors
except Exception:
    collect_memory_sensors = None
    summarize_memory_sensors = None


def _run(cmd, timeout=10):
    try:
        # LC_ALL=C : les outils (lscpu, ip, lsblk…) traduisent leurs libellés selon la langue du système (« Nom de modèle », « Cœur(s) par socket »…)
        # et tous les analyseurs de BenchSuite cherchent les libellés anglais : sans cela, sur un système en français, le modèle du CPU et le nombre de cœurs restaient vides
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception:
        return -1, "", ""


def _tool(name):
    if name == "nvidia-smi":
        from core.safety import nvidia_smi_usable
        return nvidia_smi_usable()
    import shutil            # sans processus `which` (absent de certaines installations minimales / conteneurs)
    return shutil.which(name) is not None


def _has_local_ipmi_device() -> bool:
    """True seulement si le device IPMI local existe.

    Sur beaucoup de PC/workstations, ipmitool est installé mais /dev/ipmi0
    n'existe pas. Dans ce cas, lancer `ipmitool mc info` affiche une erreur
    bruyante et inutile. On l'évite pour garder l'UI propre.
    """
    return any(os.path.exists(p) for p in (
        "/dev/ipmi0",
        "/dev/ipmi/0",
        "/dev/ipmidev/0",
    ))


def _run_root_readonly(cmd, timeout=10):
    """Tente une commande lecture seule avec sudo non-interactif puis sans sudo.

    Ne demande jamais le mot de passe. Retourne simplement rc/out/err.
    Ça évite qu'une collecte SMART/DMI/NVMe bloque ou casse toute l'analyse.
    """
    if _tool("sudo"):
        rc, out, err = _run(["sudo", "-n"] + list(cmd), timeout=timeout)
        if rc == 0:
            return rc, out, err
    return _run(list(cmd), timeout=timeout)


_CPU_NAME_CACHE: dict = {}


def cpu_model_name() -> str:
    """Nom du processeur, y compris sur ARM où /proc/cpuinfo n'a pas de « model name ».

    Ordre : /proc/cpuinfo (x86), puis lscpu « Model name » ; sur ARM on ajoute le nom commercial lu dans le DMI
    (`dmidecode -s processor-version`, lecture seule, si disponible) car lscpu ne donne que le cœur (ex. « Neoverse-N1 »).
    Retourne « inconnu » si rien n'est lisible. Le résultat est mémorisé (un seul appel à dmidecode)."""
    if "v" in _CPU_NAME_CACHE:
        return _CPU_NAME_CACHE["v"]
    name = ""
    try:
        with open("/proc/cpuinfo") as f:
            m = re.search(r"model name\s*:\s*(.+)", f.read())
        if m:
            name = m.group(1).strip()
    except Exception:
        pass
    if not name and _tool("lscpu"):
        rc, out, _ = _run(["lscpu"], timeout=5)
        if rc == 0:
            m = re.search(r"^Model name:\s*(.+)$", out, re.M)
            if m:
                name = m.group(1).strip()
        if name and platform.machine().lower() in ("aarch64", "arm64") and _tool("dmidecode"):
            rc, out, _ = _run_root_readonly(["dmidecode", "-s", "processor-version"], timeout=5)
            if rc == 0:
                vers = [v.strip() for v in out.splitlines() if v.strip() and not v.startswith("#")]
                vers = [v for v in vers if v.lower() not in ("unknown", "not specified", "to be filled by o.e.m.", "n/a")]
                if vers and vers[0].lower() not in name.lower():
                    name = f"{vers[0]} ({name})"
    _CPU_NAME_CACHE["v"] = name or "inconnu"
    return _CPU_NAME_CACHE["v"]


def _permission_note(tool: str) -> str:
    return f"{tool}: droits insuffisants ou accès matériel indisponible"


# ─────────────────────────────────────────────────────────────────────────────
# LACT (Hotspot / VRAM) & mapping MCIO
# ─────────────────────────────────────────────────────────────────────────────

# Profil MCIO intégré (serveur multi-GPU de référence), indexé par ROOT COMPLEX PCI
# (premier octet du bus, ex. « 60 » pour 0000:61:00.0 ou 0000:60:01.1). Le root complex reste
# identique quand un BIOS/une carte décale la numérotation des bus en aval (01→07, 81→87…).
_BUILTIN_MCIO_MAP = {
    "00": "MCIO1 + MCIO2",
    "10": "MCIO3 + MCIO4",
    "60": "MCIO5 + MCIO6",
    "70": "MCIO7 + MCIO8",
    "80": "MCIO11 + MCIO12",
    "90": "MCIO13 + MCIO14",
    "e0": "MCIO19 + MCIO20",
    "f0": "MCIO17 + MCIO18",
}
_MCIO_USER_FILE = os.path.expanduser("~/.benchsuite_mcio.json")
_mcio_cache: dict = {}
_LACT_ID_RE = re.compile(r"(\w+:\w+-\w+:\w+-[0-9a-fA-F:\.]+)")
_lact_ids_cache = {"t": 0.0, "ids": []}


def _short_bdf(bus_id: str) -> str:
    """Réduit un identifiant PCI à la forme courte 'bb:dd.f' en minuscules ('' si invalide)."""
    parts = (bus_id or "").strip().lower().split(":")
    return ":".join(parts[-2:]) if len(parts) >= 2 else ""


def _pci_root_key(bus_id: str) -> str:
    """Retourne le root complex PCI ('00', '60', 'e0'…) d'un périphérique.

    Lit le chemin sysfs réel (/sys/devices/pci0000:60/…) ; repli sur le premier
    octet du bus si sysfs est indisponible. '' si l'identifiant est invalide.
    """
    short = _short_bdf(bus_id)
    if not short:
        return ""
    full = f"0000:{short}"
    try:
        real = os.path.realpath(f"/sys/bus/pci/devices/{full}")
        m = re.search(r"/pci[0-9a-f]{4}:([0-9a-f]{2})/", real)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return short.split(":")[0][:1] + "0"


def _pci_port_key(bus_id: str) -> str:
    """Retourne le root port PCI d'un périphérique ('60:01.1'…), '' si inconnu.

    C'est le pont directement raccordé au root complex : il correspond au
    connecteur physique et ne bouge pas quand on ajoute/retire des SSD ou qu'un
    BIOS renumérote les bus en aval.
    """
    short = _short_bdf(bus_id)
    if not short:
        return ""
    try:
        real = os.path.realpath(f"/sys/bus/pci/devices/0000:{short}")
        m = re.search(r"/pci[0-9a-f]{4}:[0-9a-f]{2}/0000:([0-9a-f]{2}:[0-9a-f]{2}\.[0-7])", real)
        if m:
            return m.group(1).lower()
    except Exception:
        pass
    return ""


def list_gpu_pci_topology() -> list:
    """Liste les GPU PCI avec leur root complex, root port et étiquette MCIO actuelle.

    Sert à calibrer le mapping (``cli.py --mcio-scan``). Liste vide sans sysfs.
    """
    rows = []
    base = "/sys/bus/pci/devices"
    try:
        devs = sorted(os.listdir(base))
    except Exception:
        return rows
    for dev in devs:
        try:
            with open(os.path.join(base, dev, "class")) as f:
                if not f.read().strip().startswith("0x03"):
                    continue
        except Exception:
            continue
        rows.append({"bdf": dev, "root": _pci_root_key(dev), "port": _pci_port_key(dev),
                     "label": get_mcio_label(dev)})
    return rows


def set_mcio_label(key: str, label: str) -> str:
    """Enregistre (ou efface si label vide) un lien MCIO dans ~/.benchsuite_mcio.json.

    ``key`` : « port:60:01.1 » (recommandé), « root:60 » ou « bb:dd.f ». Retourne la clé normalisée.
    """
    import json
    key = key.strip().lower()
    if not (key.startswith(("port:", "root:")) or _short_bdf(key)):
        raise ValueError(f"clé invalide : {key}")
    if not key.startswith(("port:", "root:")):
        key = _short_bdf(key)
    try:
        with open(_MCIO_USER_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if not isinstance(raw, dict):
            raw = {}
    except Exception:
        raw = {}
    if label:
        raw[key] = label
    else:
        raw.pop(key, None)
    with open(_MCIO_USER_FILE, "w", encoding="utf-8") as f:
        json.dump(raw, f, ensure_ascii=False, indent=2)
    _mcio_cache.clear()
    return key


def _builtin_mcio_profile_active() -> bool:
    """Vrai si au moins 3 GPU du système sont sur des root complex du profil intégré.

    Évite d'étiqueter un simple PC (un GPU sur le root 00) comme « MCIO1 + MCIO2 ».
    """
    if "active" in _mcio_cache:
        return _mcio_cache["active"]
    roots = set()
    try:
        base = "/sys/bus/pci/devices"
        for dev in os.listdir(base):
            try:
                with open(os.path.join(base, dev, "class")) as f:
                    if not f.read().strip().startswith("0x03"):
                        continue
            except Exception:
                continue
            key = _pci_root_key(dev)
            if key in _BUILTIN_MCIO_MAP:
                roots.add(key)
    except Exception:
        pass
    _mcio_cache["active"] = len(roots) >= 3
    return _mcio_cache["active"]


def _load_mcio_map() -> dict:
    """Charge le mapping MCIO.

    Priorité : fichier ~/.benchsuite_mcio.json — clés « bb:dd.f » (GPU précis) ou
    « port:bb:dd.f » (root port, le plus précis et stable) ou « root:xx » (root complex, ex. « root:60 ») — sinon profil intégré (par root
    complex) s'il correspond à la machine, sinon vide.
    """
    if "map" in _mcio_cache:
        return _mcio_cache["map"]
    mapping, roots, ports = {}, {}, {}
    try:
        import json
        with open(_MCIO_USER_FILE, encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            for k, v in raw.items():
                k = str(k).strip().lower()
                if k.startswith("root:"):
                    roots[k[5:].strip()] = str(v)
                elif k.startswith("port:"):
                    ports[k[5:].strip()] = str(v)
                elif _short_bdf(k):
                    mapping[_short_bdf(k)] = str(v)
    except Exception:
        pass
    if not mapping and not roots and not ports and _builtin_mcio_profile_active():
        # Table intégrée = hypothèse non vérifiée (Linux ne connaît pas les connecteurs) :
        # on l'affiche comme « supposé » tant que l'utilisateur ne l'a pas confirmée.
        roots = {k: f"{v} (supposé)" for k, v in _BUILTIN_MCIO_MAP.items()}
    _mcio_cache["map"] = mapping
    _mcio_cache["roots"] = roots
    _mcio_cache["ports"] = ports
    return mapping


def get_mcio_label(bus_id: str) -> str:
    """Retourne les connecteurs MCIO associés à un GPU ('' si inconnu)."""
    by_bdf = _load_mcio_map()
    short = _short_bdf(bus_id)
    if short in by_bdf:
        return by_bdf[short]
    port = _pci_port_key(bus_id)
    if port and port in _mcio_cache.get("ports", {}):
        return _mcio_cache["ports"][port]
    return _mcio_cache.get("roots", {}).get(_pci_root_key(bus_id), "")


def _lact_gpu_ids() -> list:
    """Liste les identifiants GPU LACT (mis en cache 60 s ; [] si lact absent)."""
    import shutil
    import time as _t
    if not shutil.which("lact"):
        return []
    now = _t.time()
    if now - _lact_ids_cache["t"] < 60:
        return _lact_ids_cache["ids"]
    rc, out, _ = _run(["lact", "cli", "list"], timeout=3)
    ids = _LACT_ID_RE.findall(out) if rc == 0 else []
    _lact_ids_cache.update(t=now, ids=ids)
    return ids


def _lact_read_one(gid: str):
    """Lit Hotspot/VRAM d'un GPU LACT ; retourne (bdf, (hotspot, vram)) ou None."""
    rc, out, _ = _run(["lact", "cli", "-g", gid, "stats"], timeout=3)
    if rc != 0:
        return None
    vals = []
    for patterns in ((r"GPU Hotspot:\s*([0-9.]+)", r"Junction:\s*([0-9.]+)"),
                     (r"VRAM:\s*([0-9.]+)", r"Mem:\s*([0-9.]+)")):
        val = None
        for pat in patterns:
            m = re.search(pat, out)
            if m:
                try:
                    val = float(m.group(1))
                except ValueError:
                    pass
                break
        vals.append(val)
    return _short_bdf(gid), (vals[0], vals[1])


def get_lact_temps(bus_id: str = "") -> dict:
    """Lit Hotspot et VRAM via LACT (sans root, timeouts explicites, GPU en parallèle).

    Retourne {bdf_court: (hotspot|None, vram|None)}. Si `bus_id` est fourni,
    seul ce GPU est interrogé. Retourne {} si LACT est absent ou en erreur.
    """
    want = _short_bdf(bus_id) if bus_id else ""
    gids = [g for g in _lact_gpu_ids() if not want or _short_bdf(g) == want]
    if not gids:
        return {}
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=min(8, len(gids))) as ex:
        return dict(r for r in ex.map(_lact_read_one, gids) if r)


def _get_mcio_and_temps(bus_id: str):
    """Retourne (mcio, hotspot, vram) en chaînes ; chaîne vide si donnée indisponible."""
    mcio = get_mcio_label(bus_id)
    hs, vr = get_lact_temps(bus_id).get(_short_bdf(bus_id), (None, None))
    return mcio, (str(int(hs)) if hs is not None else ""), (str(int(vr)) if vr is not None else "")


# ─────────────────────────────────────────────────────────────────────────────
# Helpers PCIe / commandes système
# ─────────────────────────────────────────────────────────────────────────────

def _clean_value(value: str) -> str:
    value = (value or "").strip()
    return "" if value in ("N/A", "[Not Supported]", "Unknown", "Not Available", "") else value


def _read_first_existing(paths) -> str:
    for path in paths:
        try:
            with open(path) as f:
                value = f.read().strip()
            if _clean_value(value):
                return value
        except Exception:
            pass
    return ""


def _normalize_pci_bus(bus_id: str) -> str:
    """Normalise les formats PCI venant de nvidia-smi/lspci/sysfs.

    Exemples:
      - 00000000:65:00.0 -> 0000:65:00.0
      - 0000000C:01:00.0 -> 000c:01:00.0   (ARM multi-segments : le domaine PCI vaut 0x0C, pas 0x0000)
      - 65:00.0          -> 0000:65:00.0
    """
    bid = (bus_id or "").strip()
    if not bid:
        return ""
    # nvidia-smi donne le domaine sur 8 chiffres : on garde les 4 DERNIERS (les 4 premiers sont nuls). Garder les 4 premiers
    # (ancien comportement) ramenait tout domaine non nul à « 0000 » : deux GPU de domaines 0C et 0D devenaient identiques
    # et le GPU vu par nvidia-smi n'était pas reconnu comme celui vu par lspci (000c:01:00.0) → GPU en double.
    bid = re.sub(r"^0{4}([0-9a-fA-F]{4}):", r"\1:", bid)
    if re.match(r"^[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]$", bid):
        bid = "0000:" + bid
    return bid.lower()


def _pci_seen_key(bus_id: str) -> str:
    """Adresse PCI complète (domaine inclus). Ne jamais retirer le domaine : sur ARM / multi-segments, `lspci -s 02:00.0`
    sans domaine correspond à plusieurs périphériques (ex. 0000:02:00.0 et 0004:02:00.0) et mélange leurs lignes."""
    return _normalize_pci_bus(bus_id)


def _pci_device_path(bus_id: str) -> str:
    if not bus_id:
        return ""
    bid = _normalize_pci_bus(bus_id)
    path = f"/sys/bus/pci/devices/{bid}"
    return path if os.path.exists(path) else ""


def _pci_speed_to_gen(speed: str) -> str:
    speed = (speed or "").strip()
    mapping = {
        "2.5 GT/s": "1", "5.0 GT/s": "2", "5 GT/s": "2",
        "8.0 GT/s": "3", "8 GT/s": "3", "16.0 GT/s": "4",
        "16 GT/s": "4", "32.0 GT/s": "5", "32 GT/s": "5",
        "64.0 GT/s": "6", "64 GT/s": "6",
    }
    for needle, gen in mapping.items():
        if needle in speed:
            return gen
    return ""


def _get_kernel_driver(bus_id: str) -> str:
    path = _pci_device_path(bus_id)
    if not path:
        return ""
    try:
        drv = os.path.realpath(os.path.join(path, "driver"))
        if drv and os.path.exists(drv):
            return os.path.basename(drv)
    except Exception:
        pass
    return ""


# Ponts PCIe internes à la carte (switch embarqué) : le lien de l'endpoint GPU y est un artefact (2.5 GT/s x1 permanent) ;
# le vrai lien carte ↔ carte mère se lit sur le pont amont. Intel Arc Pro B70 (Battlemage) : amont e2ff, aval e2f0.
_CARD_BRIDGES_INTEL = {"e2ff", "e2f0"}


def _card_upstream_link(dev_path: str) -> dict:
    """Si le GPU est derrière le switch interne de sa carte (Intel Arc Pro B-series), retourne la vitesse/largeur du pont AMONT
    (vrai lien avec la carte mère) : {speed, width, bridge} ; {} sinon. Lecture sysfs, sans droits."""
    try:
        real = os.path.realpath(dev_path)
        cur = os.path.dirname(real)
        top = ""
        while cur and os.path.basename(cur) not in ("", "devices") and re.match(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d$", os.path.basename(cur)):
            ven = _read_first_existing([os.path.join(cur, "vendor")]).lower()
            dev = _read_first_existing([os.path.join(cur, "device")]).lower().replace("0x", "")
            if ven.endswith("8086") and dev in _CARD_BRIDGES_INTEL:
                top = cur                 # on continue à monter : le pont le plus haut est l'amont du switch
                cur = os.path.dirname(cur)
                continue
            break
        if not top:
            return {}
        speed = _read_first_existing([os.path.join(top, "current_link_speed")])
        width = _read_first_existing([os.path.join(top, "current_link_width")])
        if speed and width:
            return {"speed": speed, "width": width.lstrip("x"), "bridge": os.path.basename(top)}
    except Exception:
        pass
    return {}


def _get_pcie_info(bus_id: str) -> dict:
    """Retourne Gen/width PCIe depuis sysfs, avec fallback lspci -vv."""
    bus_id = _normalize_pci_bus(bus_id)
    info = {"pci_bus": bus_id or "", "pcie_gen": "", "pcie_largeur": "", "pcie_vitesse": "", "driver": ""}
    path = _pci_device_path(bus_id)
    if path:
        speed = _read_first_existing([os.path.join(path, "current_link_speed")])
        width = _read_first_existing([os.path.join(path, "current_link_width")])
        if speed:
            info["pcie_vitesse"] = speed
            info["pcie_gen"] = _pci_speed_to_gen(speed)
        if width:
            info["pcie_largeur"] = width.lstrip("x")
        info["driver"] = _get_kernel_driver(bus_id)
        up = _card_upstream_link(path)
        if up:                       # GPU derrière le switch de sa carte : le lien de l'endpoint (x1 2.5 GT/s) est un artefact
            info["note"] = (f"lien réel lu sur le pont amont de la carte {up['bridge']} ; "
                            f"l'endpoint annonce {info['pcie_vitesse']} x{info['pcie_largeur']} (artefact du switch interne)")
            info["pcie_vitesse"] = up["speed"]
            info["pcie_gen"] = _pci_speed_to_gen(up["speed"])
            info["pcie_largeur"] = up["width"]

    if (not info["pcie_gen"] or not info["pcie_largeur"] or not info["driver"]) and _tool("lspci") and bus_id:
        rc, out, _ = _run_root_readonly(["lspci", "-s", _pci_seen_key(bus_id), "-vv"], timeout=5)
        if rc == 0:
            m = re.search(r"LnkSta:\s*Speed\s*([^,]+),\s*Width\s*x(\d+)", out)
            if m:
                info["pcie_vitesse"] = info["pcie_vitesse"] or m.group(1).strip()
                info["pcie_gen"] = info["pcie_gen"] or _pci_speed_to_gen(m.group(1))
                info["pcie_largeur"] = info["pcie_largeur"] or m.group(2).strip()
            m = re.search(r"Kernel driver in use:\s*(.+)", out)
            if m and not info["driver"]:
                info["driver"] = m.group(1).strip()
    return info


def _pci_model(bus_id: str) -> str:
    if not bus_id or not _tool("lspci"):
        return ""
    rc, out, _ = _run(["lspci", "-s", _pci_seen_key(bus_id)], timeout=5)
    if rc == 0 and out:
        return out.split(": ", 1)[-1].strip()
    return ""


def detect_ssd_native_gen(model: str, bus_id: str = "") -> int:
    """
    Détecte intelligemment la génération PCIe native d'un SSD (Gen 3, Gen 4 ou Gen 5)
    en analysant son modèle ou sa signature lspci.
    Évite les fausses alertes de downgrade quand un SSD Gen 4 tourne sur un port/contrôleur Gen 5.
    """
    s = (model or "").upper()
    if bus_id and _tool("lspci"):
        try:
            rc, out, _ = _run(["lspci", "-s", _pci_seen_key(bus_id)], timeout=3)
            if rc == 0 and out:
                s += " " + out.upper()
        except Exception:
            pass

    # 1. Modèles / Mots-clés Gen 5 explicitement connus
    gen5_patterns = [
        "T700", "T705", "MP700", "Z540", "LEGEND 970", "SPATIUM M570",
        "AORUS GEN5", "PG5", "NM1090", "ROCKET 5", "GEN5", "GEN 5", "PCIE 5"
    ]
    if any(p in s for p in gen5_patterns):
        return 5

    # 2. Modèles / Mots-clés Gen 4 explicitement connus
    gen4_patterns = [
        "GEN4", "GEN 4", "PCIE 4", "4.0", "T500", "SN850", "SN770", "SN740",
        "980 PRO", "990 PRO", "990 EVO", "KC3000", "NV2", "NM790", "NM710", "NM800",
        "MP600", "AORUS GEN4", "RENEGADE", "FURY", "FX900", "GM7000", "EXCERIA PRO",
        "EXCERIA G2", "VP4300", "ROCKET 4", "M480", "M470", "A440", "P41", "P44 PRO",
        "P5 PLUS", "P3 PLUS", "S70", "LEGEND 960", "LEGEND 850", "US70"
    ]
    if any(p in s for p in gen4_patterns):
        return 4

    # 3. Modèles / Mots-clés Gen 3 explicitement connus
    gen3_patterns = [
        "GEN3", "GEN 3", "PCIE 3", "3.0", "P2", "P3 ", "P3/", "970 EVO", "970 PRO",
        "SN550", "SN570", "SN750", "A2000", "NV1", "SX8200", "NM620", "M390"
    ]
    if any(p in s for p in gen3_patterns) and not ("PLUS" in s and "P3" in s):
        return 3

    return 0


def check_pcie_downgrade(bus_id: str, device_model: str = "") -> tuple:
    """
    Vérifie si le périphérique PCI tourne dans sa configuration optimale (max vs current).
    Retourne (is_downgraded, desc, detail_dict)
    """
    if not bus_id:
        return False, "", {}
    
    # Normalisation du bus ID
    bus_id = _normalize_pci_bus(bus_id)
    if not bus_id:
        return False, "", {}

    native_gen = detect_ssd_native_gen(device_model or _pci_model(bus_id), bus_id)

    # 1. Méthode SYSFS (Préférée car ne requiert pas sudo)
    path = _pci_device_path(bus_id)
    if path:
        try:
            max_speed = _read_first_existing([os.path.join(path, "max_link_speed")])
            max_width = _read_first_existing([os.path.join(path, "max_link_width")])
            cur_speed = _read_first_existing([os.path.join(path, "current_link_speed")])
            cur_width = _read_first_existing([os.path.join(path, "current_link_width")])

            if max_speed and max_width and cur_speed and cur_width:
                mw = int(max_width.lstrip("x"))
                cw = int(cur_width.lstrip("x"))
                
                max_gen = _pci_speed_to_gen(max_speed)
                cur_gen = _pci_speed_to_gen(cur_speed)

                eff_cur = int(cur_gen) if str(cur_gen).isdigit() else 0
                eff_max = int(max_gen) if str(max_gen).isdigit() else 0
                if native_gen > 0 and eff_cur >= native_gen:
                    eff_max = eff_cur

                details = {
                    "max_width": mw, "cur_width": cw,
                    "max_gen": max_gen, "cur_gen": cur_gen,
                    "method": "sysfs"
                }

                if cw < mw:
                    return True, f"x{cw} au lieu de x{mw} (Largeur réduite)", details
                
                if eff_max > 0 and eff_cur > 0 and eff_cur < eff_max:
                    return True, f"Gen {cur_gen} au lieu de Gen {max_gen} (Vitesse réduite)", details

                return False, "", details
        except Exception:
            pass

    # 2. Méthode lspci -vv (Fallback)
    if _tool("lspci"):
        try:
            rc, out, _ = _run_root_readonly(["lspci", "-s", _pci_seen_key(bus_id), "-vv"], timeout=5)
            if rc == 0 and out:
                cap_speed, cap_width = "", ""
                m_cap = re.search(r"LnkCap:.*Speed\s*([^,]+),\s*Width\s*x(\d+)", out)
                if m_cap:
                    cap_speed = m_cap.group(1).strip()
                    cap_width = m_cap.group(2).strip()

                sta_speed, sta_width = "", ""
                m_sta = re.search(r"LnkSta:.*Speed\s*([^,]+),\s*Width\s*x(\d+)", out)
                if m_sta:
                    sta_speed = m_sta.group(1).strip()
                    sta_width = m_sta.group(2).strip()

                if cap_speed and cap_width and sta_speed and sta_width:
                    mw = int(cap_width)
                    cw = int(sta_width)
                    max_gen = _pci_speed_to_gen(cap_speed)
                    cur_gen = _pci_speed_to_gen(sta_speed)

                    eff_cur = int(cur_gen) if str(cur_gen).isdigit() else 0
                    eff_max = int(max_gen) if str(max_gen).isdigit() else 0
                    if native_gen > 0 and eff_cur >= native_gen:
                        eff_max = eff_cur

                    details = {
                        "max_width": mw, "cur_width": cw,
                        "max_gen": max_gen, "cur_gen": cur_gen,
                        "method": "lspci"
                    }

                    if cw < mw:
                        return True, f"x{cw} au lieu de x{mw} (Largeur réduite)", details
                    if eff_max > 0 and eff_cur > 0 and eff_cur < eff_max:
                        return True, f"Gen {cur_gen} au lieu de Gen {max_gen} (Vitesse réduite)", details
                    
                    return False, "", details
        except Exception:
            pass

    return False, "", {}


def group_hardware_alerts(alerts: list) -> list:
    """Regroupe les alertes identiques (même type/catégorie/titre/description).

    Ex. 8 GPU identiques avec la même dégradation PCIe → une seule alerte listant
    les périphériques concernés dans `devices`.
    """
    grouped, order = {}, []
    for a in alerts:
        key = (a.get("type"), a.get("category"), a.get("title"), a.get("desc"))
        if key not in grouped:
            grouped[key] = dict(a)
            grouped[key]["devices"] = []
            order.append(key)
        if a.get("device"):
            grouped[key]["devices"].append(a["device"])
    out = []
    for key in order:
        a = grouped[key]
        a.pop("device", None)
        n = len(a["devices"])
        if n > 1:
            a["title"] = f"{a['title']} ({n} périphériques)"
        out.append(a)
    return out


def run_hardware_audit(data: dict) -> list:
    """
    Exécute un audit complet du matériel pour détecter les anomalies de configuration.
    Retourne une liste de dictionnaires d'alertes : 
    [{"type": "error"|"warning"|"info", "category": str, "title": str, "desc": str}]
    """
    alerts = []

    # 00. Collecte incomplète : une section a dépassé son budget et a été abandonnée (outil ou périphérique bloqué)
    for late in (data.get("sections_incompletes") or []):
        alerts.append({
            "type": "warning", "category": "Collecte",
            "title": f"Section « {late.get('section')} » abandonnée après {late.get('budget_s')} s",
            "desc": ("Cette partie des informations système n'a pas répondu à temps (outil bloqué, BMC muet, périphérique en erreur, bus I²C lent) "
                     "et manque donc dans ce rapport. Relancer l'analyse ; si cela se répète, identifier le coupable avec "
                     "la procédure « Collecte d'informations lente » du guide d'utilisation.")})

    # 0. Horloge : un événement BMC (SEL) daté APRÈS l'instant du relevé prouve qu'une des deux horloges est fausse
    #    (machine fraîchement démarrée sans NTP, pile RTC vide…). Tous les horodatages du rapport en sont alors suspects.
    try:
        from datetime import datetime as _dt
        m_sel = re.search(r"(\d{2})/(\d{2})/(\d{2,4})\s+(\d{2}):(\d{2}):(\d{2})", str((data.get("bmc") or {}).get("sel_status", "")))
        if m_sel:
            mo, dd, yy, hh, mi, ss = (int(x) for x in m_sel.groups())
            sel_t = _dt(yy + 2000 if yy < 100 else yy, mo, dd, hh, mi, ss)
            try:
                ref_t = _dt.fromisoformat(str(data.get("timestamp"))).replace(tzinfo=None)
            except (TypeError, ValueError):
                ref_t = _dt.now()
            gap = (sel_t - ref_t).total_seconds()
            if gap > 86400:      # > 24 h : plus grand que n'importe quel fuseau horaire, donc pas une simple différence UTC / heure locale
                alerts.append({
                    "type": "warning", "category": "Horloge",
                    "title": "Horloge de la machine incohérente avec celle de la BMC",
                    "desc": (f"La BMC a enregistré un événement le {sel_t:%d/%m/%Y %H:%M} alors que la machine indique {ref_t:%d/%m/%Y %H:%M} "
                             f"(écart ≈ {gap / 86400:.1f} jour(s)). L'une des deux horloges est fausse : les horodatages de ce rapport "
                             "(tests, historique, journaux) sont à vérifier. Contrôler `timedatectl` (synchronisation NTP) et l'heure de la BMC.")})
    except Exception:
        pass

    # 1. Vérification RAM (Tailles, vitesses dépareillées, canal unique, permission sudo)
    ram = data.get("ram", {})
    barrettes = ram.get("barrettes", [])
    total_go = ram.get("total_go", 0.0)

    if barrettes:
        sizes = [b.get("taille") for b in barrettes if b.get("taille")]
        speeds = [b.get("vitesse_mhz") or b.get("vitesse") for b in barrettes if b.get("vitesse_mhz") or b.get("vitesse")]
        
        # Barrettes de tailles différentes
        cleaned_sizes = list(set(sizes))
        if len(cleaned_sizes) > 1:
            alerts.append({
                "type": "warning",
                "category": "Mémoire RAM",
                "title": "Barrettes mémoire de capacités dépareillées",
                "desc": f"Vous utilisez des barrettes de capacités différentes ({', '.join(cleaned_sizes)}). Cela peut perturber l'activation ou l'efficacité du mode Dual/Multi-Channel."
            })
        
        # Barrettes de vitesses différentes
        cleaned_speeds = list(set(speeds))
        if len(cleaned_speeds) > 1:
            alerts.append({
                "type": "warning",
                "category": "Mémoire RAM",
                "title": "Barrettes mémoire de fréquences dépareillées",
                "desc": f"Vous utilisez des barrettes fonctionnant à des fréquences différentes ({', '.join(cleaned_speeds)}). Les barrettes s'aligneront automatiquement sur la vitesse de la plus basse, limitant les performances."
            })

        # Alerte Monocanal (Single Channel)
        if len(barrettes) == 1:
            alerts.append({
                "type": "warning",
                "category": "Mémoire RAM",
                "title": "Mémoire en mode monocanal (Single Channel)",
                "desc": "Une seule barrette de RAM est installée : un seul canal mémoire est utilisé, donc une fraction de la bande passante "
                        "possible du processeur (un processeur serveur — Xeon, EPYC, Ampere Altra — en gère généralement 6 à 12 selon le modèle, un poste 2). Les débits mesurés "
                        "(STREAM, transferts hôte ↔ GPU) en sont limités. Installer des barrettes identiques sur les emplacements indiqués "
                        "par le manuel de la carte mère pour peupler les canaux."
            })
    elif total_go > 0.0:
        # Aucun stick trouvé par dmidecode alors que de la RAM est présente (pas lancé sous sudo)
        if not _tool("sudo") or os.geteuid() != 0:
            alerts.append({
                "type": "info",
                "category": "Mémoire RAM",
                "title": "Privilèges insuffisants pour l'audit détaillé",
                "desc": "Lancer l'application avec des privilèges administrateur (sudo) permettrait à 'dmidecode' d'accéder aux détails physiques des slots RAM (canaux, slots occupés/vides, fabricants)."
            })

    # 2. Vérification GPU (Dégradation PCIe + ASPM)
    gpus = data.get("gpus", [])
    for i, gpu in enumerate(gpus):
        nom = gpu.get("nom") or gpu.get("modele") or f"GPU {i}"
        bus_id = gpu.get("pci_bus", "")
        dev_label = f"GPU {gpu.get('index', i)} ({_short_bdf(bus_id)}" + (f" · {gpu['mcio']}" if gpu.get("mcio") else "") + ")"
        if bus_id:
            is_downgraded, desc, details = check_pcie_downgrade(bus_id)
            if is_downgraded:
                # Si c'est juste un downgrade de vitesse (Gen), mentionner ASPM
                is_only_gen = "Vitesse" in desc or (details.get("cur_gen") and details.get("max_gen") and details.get("cur_width") == details.get("max_width"))
                if is_only_gen:
                    alerts.append({
                        "type": "warning",
                        "category": f"Carte graphique ({nom})",
                        "device": dev_label,
                        "title": "Vitesse de liaison PCIe réduite au repos",
                        "desc": f"Le bus PCIe du GPU fonctionne en {desc}. Note : Les cartes graphiques réduisent dynamiquement leur vitesse au repos (ASPM). Ce statut est normal si aucun test 3D n'est actif."
                    })
                else:
                    alerts.append({
                        "type": "warning",
                        "category": f"Carte graphique ({nom})",
                        "device": dev_label,
                        "title": "Liaison PCIe physiquement dégradée",
                        "desc": f"La carte graphique fonctionne avec un bus bridé : {desc}. Vérifiez le slot ou le câble (MCIO/riser), ou confirmez que ce câblage réduit est voulu par la conception du châssis."
                    })

    # 3. Vérification Stockage (SMART & PCIe NVMe)
    stockage = data.get("stockage", [])
    for disk in stockage:
        nom = disk.get("nom") or disk.get("name") or "Disque"
        modele = disk.get("modele") or disk.get("model") or "Inconnu"
        sante = str(disk.get("sante", "")).upper()

        if any(term in sante for term in ("ECHEC", "FAILED", "BAD", "CRITICAL")):
            alerts.append({
                "type": "error",
                "category": f"Stockage ({modele})",
                "title": "DANGER : Défaillance SMART critique détectée !",
                "desc": f"Le disque /dev/{nom} signale un état de santé SMART critique ({sante}). Il y a un risque imminent de perte de données. Remplacement recommandé de toute urgence."
            })
        elif sante and not any(term in sante for term in ("OK", "PASSED", "GOOD", "INCONNU", "UNKNOWN")):
            alerts.append({
                "type": "warning",
                "category": f"Stockage ({modele})",
                "title": "Avertissement de santé SMART",
                "desc": f"Le disque /dev/{nom} a renvoyé un statut de santé inhabituel : {sante}. Lancez un diagnostic approfondi."
            })

        # PCIe pour SSD NVMe
        pci_bus = disk.get("pci_bus", "")
        if pci_bus:
            is_downgraded, desc, _ = check_pcie_downgrade(pci_bus, device_model=modele)
            if is_downgraded:
                alerts.append({
                    "type": "warning",
                    "category": f"SSD NVMe ({modele})",
                    "title": "Liaison PCIe de SSD sous-optimale",
                    "desc": f"Le SSD NVMe ({pci_bus}) fonctionne en configuration de bus réduite : {desc}. Cela limite ses débits de lecture/écriture séquentiels maximaux."
                })

    # 4. Vérification Réseau (PCIe pour NICs)
    reseau = data.get("reseau", [])
    for iface in reseau:
        nom = iface.get("nom", "Interface")
        modele = iface.get("modele") or "Interface réseau"
        pci_bus = iface.get("pci_bus", "")
        vitesse = iface.get("vitesse", "").lower()
        if pci_bus:
            is_downgraded, desc, details = check_pcie_downgrade(pci_bus)
            
            # Ignorer l'alerte de largeur pour les cartes 1G/2.5G (souvent des puces LnkCap x4 câblées en x1 sur carte mère)
            if is_downgraded and "Largeur" in desc:
                if "10000" not in vitesse and "10g" not in str(modele).lower() and "mellanox" not in str(modele).lower():
                    is_downgraded = False

            if is_downgraded:
                alerts.append({
                    "type": "warning",
                    "category": f"Interface Réseau ({nom})",
                    "title": "Interface PCIe de carte réseau bridée",
                    "desc": f"La carte réseau ({modele}) sur le bus {pci_bus} fonctionne sur un lien dégradé : {desc}. Cela peut brider ses capacités de transfert (si ce n'est pas un port intégré)."
                })

    # 4. Erreurs AER PCIe depuis le démarrage (compteurs sysfs) : non corrigeables = anomalie, corrigées = à surveiller
    pcie = [d for d in (data.get("pcie") or []) if isinstance(d, dict)]
    bad = [d for d in pcie if (d.get("aer_nonfatal") or 0) + (d.get("aer_fatal") or 0) > 0]
    cor = [d for d in pcie if (d.get("aer_cor") or 0) > 0 and d not in bad]
    for d in bad:
        alerts.append({
            "type": "error", "category": f"PCIe ({d.get('type', '')})",
            "device": f"{d.get('nom', '')} ({d.get('bus', '')})",
            "title": "Erreurs PCIe non corrigeables (AER)",
            "desc": f"{d.get('aer_nonfatal', 0)} non fatale(s) et {d.get('aer_fatal', 0)} fatale(s) depuis le démarrage : "
                    "vérifier le slot, le riser / câble (MCIO, OCuLink) et le pilote."})
    if cor:
        tot = sum(d.get("aer_cor", 0) for d in cor)
        alerts.append({
            "type": "info", "category": "PCIe",
            "title": f"Erreurs PCIe corrigées (AER) : {tot} sur {len(cor)} périphérique(s)",
            "desc": "Compteurs depuis le démarrage : " + ", ".join(f"{d.get('nom', '')} ({d.get('bus', '')}) : {d['aer_cor']}" for d in cor[:6])
                    + ". Quelques erreurs corrigées sont courantes ; une croissance rapide pendant les tests signale un lien marginal "
                      "(riser, câble, connecteur). Relancer le rapport après un test pour comparer."})
    return group_hardware_alerts(alerts)


# ─────────────────────────────────────────────────────────────────────────────
# OS & Kernel
# ─────────────────────────────────────────────────────────────────────────────

def get_os_info() -> dict:
    info = {
        "os":            platform.system(),
        "distro":        "",
        "kernel":        platform.release(),
        "architecture":  platform.machine(),
        "hostname":      platform.node(),
        "uptime":        "",
    }
    # Distro
    try:
        rc, out, _ = _run(["lsb_release", "-d"])
        if rc == 0 and out:
            info["distro"] = out.replace("Description:", "").strip()
    except Exception:
        pass
    if not info["distro"]:
        try:
            with open("/etc/os-release") as f:
                for line in f:
                    if line.startswith("PRETTY_NAME="):
                        info["distro"] = line.split("=", 1)[1].strip().strip('"')
                        break
        except Exception:
            pass
    # Uptime
    try:
        with open("/proc/uptime") as f:
            secs = float(f.read().split()[0])
        d, r = divmod(int(secs), 86400)
        h, r = divmod(r, 3600)
        m = r // 60
        info["uptime"] = f"{d}j {h}h {m}m" if d else f"{h}h {m}m"
    except Exception:
        pass
    return info


# ─────────────────────────────────────────────────────────────────────────────
# CPU
# ─────────────────────────────────────────────────────────────────────────────

def get_cpu_info() -> dict:
    info = {
        "modele":       "",
        "coeurs_phys":  0,
        "threads":      os.cpu_count() or 0,
        "freq_max_mhz": "",
        "freq_cur_mhz": "",
        "architecture": platform.machine(),
        "cache_l3":     "",
        "socket":       "",
        "microcode":    "",
        "vulnerabilites": [],
    }
    try:   # nœuds NUMA (sysfs, sans droits)
        nodes = [n for n in os.listdir("/sys/devices/system/node") if re.fullmatch(r"node\d+", n)]
        if nodes:
            info["numa_noeuds"] = len(nodes)
    except Exception:
        pass
    try:
        with open("/proc/cpuinfo") as f:
            raw = f.read()
        info["modele"] = cpu_model_name()
        if info["modele"] == "inconnu":
            info["modele"] = ""
        m = re.search(r"cpu cores\s*:\s*(\d+)", raw)
        if m: info["coeurs_phys"] = int(m.group(1))
        m = re.search(r"cpu MHz\s*:\s*([\d.]+)", raw)
        if m: info["freq_cur_mhz"] = f"{float(m.group(1)):.0f} MHz"
        m = re.search(r"cache size\s*:\s*(.+)", raw)
        if m: info["cache_l3"] = m.group(1).strip()
        m = re.search(r"microcode\s*:\s*(.+)", raw)
        if m: info["microcode"] = m.group(1).strip()
    except Exception:
        pass

    # Fréquence max via cpufreq
    try:
        with open("/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq") as f:
            info["freq_max_mhz"] = f"{int(f.read().strip()) // 1000} MHz"
    except Exception:
        pass

    # lscpu pour compléter
    if _tool("lscpu"):
        rc, out, _ = _run(["lscpu"])
        if rc == 0:
            for line in out.splitlines():
                k, _, v = line.partition(":")
                k = k.strip(); v = v.strip()
                if k == "Model name" and not info["modele"]: info["modele"] = v
                elif k == "Core(s) per socket" and not info["coeurs_phys"]:
                    try: info["coeurs_phys"] = int(v)
                    except: pass
                elif k == "Socket(s)": info["socket"] = v
                elif k == "L3 cache" and v:
                    # lscpu donne le total (ex. « 1 GiB (2 instances) ») ; /proc/cpuinfo ne donne
                    # que la taille d'une instance (ex. « 1024 KB »), trompeuse sur EPYC/Xeon.
                    info["cache_l3"] = v

    # Vulnérabilités CPU
    try:
        vuln_path = "/sys/devices/system/cpu/vulnerabilities"
        if os.path.isdir(vuln_path):
            vulns = []
            for name in sorted(os.listdir(vuln_path)):
                try:
                    with open(os.path.join(vuln_path, name)) as f:
                        status = f.read().strip()
                    if "Vulnerable" in status or "vulnerable" in status:
                        vulns.append(f"{name}: {status}")
                except Exception:
                    pass
            info["vulnerabilites"] = vulns
    except Exception:
        pass

    return info


# ─────────────────────────────────────────────────────────────────────────────
# RAM
# ─────────────────────────────────────────────────────────────────────────────

def get_ram_info() -> dict:
    info = {
        "total_go":    0.0,
        "disponible_go": 0.0,
        "utilise_go":  0.0,
        "utilisation_pct": 0,
        "type":        "",
        "vitesse_mhz": "",
        "barrettes":   [],
        "canaux":      "",
        "ecc":         "",
        "capteurs":    [],
        "temperature_max_c": "",
        "temperature_avg_c": "",
        "power_total_w": "",
        "sensor_sources": [],
    }
    try:
        with open("/proc/meminfo") as f:
            raw = f.read()
        def mem_kb(key):
            m = re.search(rf"{key}:\s+(\d+)", raw)
            return int(m.group(1)) if m else 0
        total = mem_kb("MemTotal")
        avail = mem_kb("MemAvailable")
        used  = total - avail
        info["total_go"]       = round(total / 1048576, 2)
        info["disponible_go"]  = round(avail / 1048576, 2)
        info["utilise_go"]     = round(used  / 1048576, 2)
        info["utilisation_pct"] = round(used / total * 100) if total else 0
    except Exception:
        pass

    # dmidecode pour les détails des barrettes (root ou sudo requis)
    if _tool("dmidecode"):
        rc, out, _ = _run_root_readonly(["dmidecode", "-t", "memory"])
        if rc == 0 and out:
            sticks = []
            current = {}

            def _size_is_populated(v: str) -> bool:
                if not v:
                    return False
                v = v.strip()
                if v in ("No Module Installed", "Not Installed", "Unknown"):
                    return False
                # Some platforms report empty slots as "0 MB" (or "0 GB").
                m = re.match(r"^\s*(\d+(?:\.\d+)?)\s*([KMG]B)\b", v, re.IGNORECASE)
                if m:
                    try:
                        return float(m.group(1)) > 0.0
                    except Exception:
                        return False
                if v.startswith("0 "):
                    return False
                return True

            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Memory Device"):
                    if current: sticks.append(current)
                    current = {}
                elif ":" in line:
                    k, _, v = line.partition(":")
                    k = k.strip(); v = v.strip()
                    if k == "Size":
                        current["_size_raw"] = v
                    if k == "Size" and _size_is_populated(v):
                        current["taille"] = v
                    elif k == "Type" and v not in ("Unknown", "None"):
                        current["type"] = v
                    elif k == "Speed" and v not in ("Unknown", "0"):
                        current["vitesse"] = v
                    elif k == "Manufacturer" and v not in ("Unknown", "Not Specified"):
                        current["fabricant"] = v
                    elif k == "Part Number" and v not in ("Unknown", "Not Specified"):
                        current["ref"] = v.strip()
                    elif k == "Locator":
                        current["slot"] = v
                    elif k == "Bank Locator" and v not in ("", "Unknown", "Not Specified"):
                        current["bank"] = v
                    elif k == "Error Correction Type":
                        if "ECC" in v: info["ecc"] = v
            if current: sticks.append(current)

            populated = [s for s in sticks if "taille" in s]
            # Emplacements : un « Memory Device » (type 17) par support, vide ou non. Ne PAS se fier au champ « Number Of
            # Devices » des « Physical Memory Array » : certains firmwares y mettent une valeur générique (16 pour 6 supports réels).
            empty_raw = ("no module installed", "not installed")
            libres = [st.get("slot", "") for st in sticks
                      if "taille" not in st and (st.get("_size_raw") or "").strip().lower() in empty_raw and st.get("slot")]
            total_slots = sum(1 for st in sticks if st.get("slot"))
            if total_slots and total_slots >= len(populated):
                txt = f"{len(populated)} / {total_slots} occupés"
                if libres:
                    txt += " (libres : " + ", ".join(libres) + ")"
                info["emplacements"] = txt
            # Locator identique sur plusieurs barrettes (bi-socket) : on ajoute le Bank Locator
            slots = [st.get("slot", "") for st in populated]
            for st in populated:
                bank = st.pop("bank", "")
                st.pop("_size_raw", None)
                if bank and slots.count(st.get("slot", "")) > 1:
                    st["slot"] = f"{st.get('slot', '')} ({bank})"
            info["barrettes"] = populated
            if populated:
                types  = set(s.get("type", "") for s in populated if s.get("type"))
                speeds = set(s.get("vitesse", "") for s in populated if s.get("vitesse"))
                if types:  info["type"]       = ", ".join(types)
                if speeds: info["vitesse_mhz"] = ", ".join(speeds)

    # Capteurs RAM autonomes: BMC/IPMI SDR, lm-sensors, hwmon.
    # Les noms varient beaucoup selon les cartes meres; le collecteur cherche
    # DIMM/DDR/DRAM/Memory/RAM/SPD et classe temperature/puissance/voltage.
    if collect_memory_sensors:
        try:
            sensors = collect_memory_sensors()
            summary = summarize_memory_sensors(sensors) if summarize_memory_sensors else {}
            info["capteurs"] = sensors
            if summary.get("temperature_max_c"):
                info["temperature_max_c"] = summary["temperature_max_c"]
            if summary.get("temperature_avg_c"):
                info["temperature_avg_c"] = summary["temperature_avg_c"]
            if summary.get("power_total_w"):
                info["power_total_w"] = summary["power_total_w"]
            info["sensor_sources"] = summary.get("sources", [])
        except Exception:
            pass
    return info


# ─────────────────────────────────────────────────────────────────────────────
# Carte mère
# ─────────────────────────────────────────────────────────────────────────────

def get_motherboard_info() -> dict:
    info = {
        "fabricant":  "",
        "modele":     "",
        "version":    "",
        "bios":       "",
        "bios_date":  "",
        "chipset":    "",
        "form_factor": "",
    }
    info["mode_boot"] = "UEFI" if os.path.isdir("/sys/firmware/efi") else "BIOS (legacy)"
    try:   # IOMMU : actif si des groupes existent (VT-d / AMD-Vi activé dans le BIOS et le noyau)
        groups = os.listdir("/sys/kernel/iommu_groups")
        info["iommu"] = f"actif ({len(groups)} groupes)" if groups else "inactif"
    except Exception:
        pass
    # Via DMI
    for key, path in [
        ("fabricant", "/sys/class/dmi/id/board_vendor"),
        ("modele",    "/sys/class/dmi/id/board_name"),
        ("version",   "/sys/class/dmi/id/board_version"),
        ("bios",      "/sys/class/dmi/id/bios_version"),
        ("bios_date", "/sys/class/dmi/id/bios_date"),
    ]:
        try:
            with open(path) as f:
                val = f.read().strip()
            if val and val not in ("Unknown", "To be filled by O.E.M.", "None"):
                info[key] = val
        except Exception:
            pass
    # Chipset via lspci
    if _tool("lspci"):
        rc, out, _ = _run(["lspci"])
        if rc == 0:
            for line in out.splitlines():
                if "ISA bridge" in line or "Host bridge" in line:
                    info["chipset"] = line.split(":", 2)[-1].strip()[:60]
                    break
    return info


# ─────────────────────────────────────────────────────────────────────────────
# BMC / IPMI
# ─────────────────────────────────────────────────────────────────────────────

def _parse_key_values(raw: str) -> dict:
    data = {}
    for line in (raw or "").splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            data[k.strip()] = v.strip()
    return data


def get_bmc_info() -> dict:
    """Infos BMC/IPMI locales quand disponibles.

    La BMC/IPMI est optionnelle. Sans /dev/ipmi*, on n'appelle pas ipmitool:
    ça évite l'erreur "Could not open device at /dev/ipmi0..." et empêche
    la collecte BMC de polluer/casser les autres informations système.
    """
    info = {
        "present": False,
        "device_id": "",
        "manufacturer": "",
        "product_id": "",
        "firmware_revision": "",
        "ipmi_version": "",
        "mac": "",
        "ip_address": "",
        "channel": "",
        "sel_status": "",
        "notes": [],
    }

    if not _tool("ipmitool"):
        info["notes"].append("ipmitool non installé")
        return info

    if not _has_local_ipmi_device():
        info["notes"].append("BMC/IPMI local non disponible (/dev/ipmi* absent)")
        info["notes"].append("Sur serveur avec BMC, charger ipmi_si/ipmi_devintf ou lancer avec les droits nécessaires")
        return info

    # On essaie sans sudo puis avec sudo -n. Important: l'application GTK ne doit pas
    # être lancée entièrement en root, sinon des outils utilisateur comme gpu-fryer
    # installés dans ~/.cargo/bin peuvent disparaître/griser.
    rc, out, err = _run(["ipmitool", "mc", "info"], timeout=8)
    if rc != 0:
        rc, out, err = _run_root_readonly(["ipmitool", "mc", "info"], timeout=8)
    if rc == 0 and out:
        info["present"] = True
        kv = _parse_key_values(out)
        info["device_id"] = kv.get("Device ID", "")
        info["manufacturer"] = kv.get("Manufacturer Name", "") or kv.get("Manufacturer ID", "")
        info["product_id"] = kv.get("Product ID", "")
        info["firmware_revision"] = kv.get("Firmware Revision", "")
        info["ipmi_version"] = kv.get("IPMI Version", "")
    else:
        info["notes"].append(_permission_note("ipmitool"))
        return info

    for channel in ("1", "8"):
        rc, out, _ = _run(["ipmitool", "lan", "print", channel], timeout=8)
        if rc != 0:
            rc, out, _ = _run_root_readonly(["ipmitool", "lan", "print", channel], timeout=8)
        if rc == 0 and out:
            kv = _parse_key_values(out)
            info["channel"] = channel
            info["mac"] = kv.get("MAC Address", "")
            info["ip_address"] = kv.get("IP Address", "")
            break

    rc, out, _ = _run(["ipmitool", "sel", "info"], timeout=8)
    if rc != 0:
        rc, out, _ = _run_root_readonly(["ipmitool", "sel", "info"], timeout=8)
    if rc == 0 and out:
        kv = _parse_key_values(out)
        entries = kv.get("Entries", "")
        last_add = kv.get("Last Add Time", "")
        info["sel_status"] = f"{entries} entree(s)" + (f", dernier ajout: {last_add}" if last_add else "")

    # Contrôle Redfish API
    try:
        from core.redfish_client import is_redfish_available
        rf_ok, rf_url, _ = is_redfish_available(host=info.get("ip_address") or "", timeout=3)
        if rf_ok:
            info["present"] = True
            info["redfish_present"] = True
            info["redfish_url"] = rf_url
    except Exception:
        pass

    return info

# ─────────────────────────────────────────────────────────────────────────────
# GPU
# ─────────────────────────────────────────────────────────────────────────────

def _read_sysfs_file(path: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _find_hwmon_dir(bus_id: str) -> str:
    if not bus_id:
        return ""
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    if not os.path.exists(pci_path):
        return ""
    
    hwmon_base = os.path.join(pci_path, "hwmon")
    if os.path.exists(hwmon_base):
        try:
            for subdir in os.listdir(hwmon_base):
                if subdir.startswith("hwmon"):
                    return os.path.join(hwmon_base, subdir)
        except Exception:
            pass
            
    drm_base = os.path.join(pci_path, "drm")
    if os.path.exists(drm_base):
        try:
            for card in os.listdir(drm_base):
                if card.startswith("card"):
                    hwmon_drm = os.path.join(drm_base, card, "device", "hwmon")
                    if os.path.exists(hwmon_drm):
                        for subdir in os.listdir(hwmon_drm):
                            if subdir.startswith("hwmon"):
                                return os.path.join(hwmon_drm, subdir)
        except Exception:
            pass
            
    return ""


def _find_card_dir(bus_id: str) -> str:
    if not bus_id:
        return ""
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    if not os.path.exists(pci_path):
        return ""
    drm_base = os.path.join(pci_path, "drm")
    if os.path.exists(drm_base):
        try:
            for card in os.listdir(drm_base):
                if card.startswith("card"):
                    return os.path.join(drm_base, card)
        except Exception:
            pass
    return ""


def _get_gpu_temp_sysfs(bus_id: str) -> float:
    hwmon = _find_hwmon_dir(bus_id)
    if hwmon:
        val = _read_sysfs_file(os.path.join(hwmon, "temp1_input"))
        if val:
            try:
                return float(val) / 1000.0
            except ValueError:
                pass
    return None


def _get_gpu_power_sysfs(bus_id: str) -> float:
    hwmon = _find_hwmon_dir(bus_id)
    if hwmon:
        for fname in ("power1_average", "power1_input"):
            val = _read_sysfs_file(os.path.join(hwmon, fname))
            if val:
                try:
                    return float(val) / 1000000.0
                except ValueError:
                    pass
    return None


def _gpu_power_cap_sysfs(bus_id: str) -> str:
    """Limite de puissance du GPU en watts (hwmon `power1_cap`, sinon `power1_max` / `power1_rated_max`), '' si absente.
    La puissance instantanée (`power1_average`) était affichée à tort comme « Limite » / « Pmax » pour les GPU AMD et Intel."""
    hwmon = _find_hwmon_dir(bus_id)
    if hwmon:
        for fname in ("power1_cap", "power1_max", "power1_rated_max"):
            val = _read_sysfs_file(os.path.join(hwmon, fname))
            if val:
                try:
                    w = float(val) / 1000000.0
                    if w > 0:
                        return f"{w:.0f}"
                except ValueError:
                    pass
    return ""


def _get_amd_vram_info(bus_id: str) -> tuple:
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    tot = _read_sysfs_file(os.path.join(pci_path, "mem_info_vram_total"))
    used = _read_sysfs_file(os.path.join(pci_path, "mem_info_vram_used"))
    vram_tot_mo = ""
    vram_used_mo = ""
    if tot:
        try:
            vram_tot_mo = str(int(tot) // (1024 * 1024))
        except ValueError:
            pass
    if used:
        try:
            vram_used_mo = str(int(used) // (1024 * 1024))
        except ValueError:
            pass
    return vram_tot_mo, vram_used_mo


# Accélérateurs de calcul « externes » reconnus (classe PCI 1200) : Habana/Intel Gaudi, Huawei Ascend.
# Les accélérateurs intégrés au CPU (Intel QAT, DSA, IAA, DLB…) ne sont PAS des GPU.
_ACCEL_VENDORS = {"1da3", "19e5"}


def _is_gpu_pci_line(line: str) -> bool:
    """Vrai si une ligne `lspci -nn` décrit un GPU / accélérateur de calcul (pas un co-processeur du CPU).

    Classe PCI 03xx (VGA, 3D, affichage) : toujours. Classe 1200 (« Processing accelerators ») : seulement
    pour les fabricants d'accélérateurs de calcul connus. Les co-processeurs (0b40) comme Intel QAT/DSA/IAA des
    Xeon n'ont ni VRAM ni capteur GPU : ils étaient comptés à tort comme GPU 1…N.
    """
    m = re.search(r"\[([0-9a-f]{4})\]:.*\[([0-9a-f]{4}):([0-9a-f]{4})\]", line, re.I)
    if m:
        cls, vendor = m.group(1).lower(), m.group(2).lower()
        return cls.startswith("03") or (cls == "1200" and vendor in _ACCEL_VENDORS)
    return any(k in line for k in ("VGA compatible controller", "3D controller", "Display controller"))


def get_gpu_info() -> list:
    """Retourne une liste volontairement courte: nom, bus PCI, modèle, VRAM, puissance, température, PCIe, pilote."""
    gpus = []
    seen = set()

    if _tool("nvidia-smi"):
        fields = "name,pci.bus_id,memory.total,power.limit,temperature.gpu,driver_version,pcie.link.gen.current,pcie.link.width.current,serial"
        rc, out, _ = _run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout=8)
        if rc == 0 and out.strip():
            for line in out.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                while len(parts) < 9:
                    parts.append("")
                name, bus, vram, power_limit, temp, drv_ver, gen, width, serial = parts[:9]
                pcie = _get_pcie_info(bus)
                mcio, hotspot, vram_temp = _get_mcio_and_temps(bus)
                gpu = {
                    "index": str(len(gpus)),
                    "serial": _clean_value(serial),
                    "mcio": mcio,
                    "hotspot_c": hotspot,
                    "vram_temp_c": vram_temp,
                    "nom": _clean_value(name),
                    "pci_bus": _normalize_pci_bus(_clean_value(bus)),
                    "modele": _pci_model(bus) or _clean_value(name),
                    "vram_total_mo": _clean_value(vram),
                    "puissance_max_w": _clean_value(power_limit),
                    "temperature_c": _clean_value(temp),
                    "temperature_max_c": "",
                    "limite_thermique_c": "",
                    "thermal_limit_c": "",
                    "seuil_throttling_c": "",
                    "pcie_gen": _clean_value(gen) or pcie.get("pcie_gen", ""),
                    "pcie_largeur": _clean_value(width) or pcie.get("pcie_largeur", ""),
                    "pcie_vitesse": pcie.get("pcie_vitesse", ""),
                    "pilote": pcie.get("driver", "") or "nvidia",
                    "driver_version": _clean_value(drv_ver),
                    "fabricant": "NVIDIA",
                }
                seen.add(_pci_seen_key(gpu["pci_bus"]))
                gpus.append(gpu)

    # Tous les GPU visibles via PCI, y compris AMD/Intel ou fallback NVIDIA sans nvidia-smi
    if _tool("lspci"):
        rc, out, _ = _run(["lspci", "-D", "-nn"], timeout=8)
        if rc == 0:
            for line in out.splitlines():
                if not _is_gpu_pci_line(line):
                    continue
                bus = line.split()[0]
                if _pci_seen_key(bus) in seen:
                    continue
                desc = line.split(": ", 1)[-1].strip()
                pcie = _get_pcie_info(bus)
                fabricant = "Intel" if "Intel" in desc else ("AMD" if any(x in desc for x in ("AMD", "ATI")) else ("NVIDIA" if "NVIDIA" in desc else "Autre"))
                
                # Lectures sysfs en direct pour AMD / Intel / NVIDIA (si nvidia-smi absent)
                temp = _get_gpu_temp_sysfs(bus)
                power = _get_gpu_power_sysfs(bus)
                
                vram_tot = ""
                if fabricant == "AMD":
                    vram_tot, _ = _get_amd_vram_info(bus)

                mcio_fb, hs_fb, vr_fb = _get_mcio_and_temps(bus)
                gpu = {
                    "index": str(len(gpus)),
                    "serial": "",
                    "mcio": mcio_fb,
                    "hotspot_c": hs_fb,
                    "vram_temp_c": vr_fb,
                    "nom": desc,
                    "pci_bus": bus,
                    "modele": desc,
                    "vram_total_mo": vram_tot,
                    "puissance_max_w": _gpu_power_cap_sysfs(bus),      # LIMITE de puissance (power1_cap), pas la puissance instantanée
                    "puissance_w": f"{power:.0f}" if power is not None else "",
                    "temperature_c": f"{temp:.0f}" if temp is not None else "",
                    "temperature_max_c": "",
                    "limite_thermique_c": "",
                    "thermal_limit_c": "",
                    "seuil_throttling_c": "",
                    "pcie_gen": pcie.get("pcie_gen", ""),
                    "pcie_largeur": pcie.get("pcie_largeur", ""),
                    "pcie_vitesse": pcie.get("pcie_vitesse", ""),
                    "pilote": pcie.get("driver", ""),
                    "driver_version": "",
                    "fabricant": fabricant,
                }
                if pcie.get("note"):
                    gpu["pcie_note"] = pcie["note"]
                if fabricant == "Intel":
                    try:      # xpu-smi : nom exact, mémoire, UUID, températures, limite de puissance
                        from core import intel_gpu
                        intel_gpu.enrich(gpu)
                        intel_gpu.enrich_hwmon(gpu)
                    except Exception:
                        pass
                gpus.append(gpu)
    return gpus


# ─────────────────────────────────────────────────────────────────────────────
# Stockage
# ─────────────────────────────────────────────────────────────────────────────


def _block_device_pci_bus(name: str) -> str:
    try:
        real = os.path.realpath(f"/sys/block/{name}/device")
        matches = re.findall(r"([0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", real)
        if matches:
            return matches[-1]
            
        if "nvme-subsys" in real or name.startswith("nvme"):
            import glob
            for ctrl in glob.glob(os.path.join(real, "nvme*")):
                if os.path.isdir(ctrl):
                    ctrl_real = os.path.realpath(ctrl)
                    m = re.findall(r"([0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", ctrl_real)
                    if m: return m[-1]
                    
            m = re.match(r"(nvme\d+)", name)
            if m:
                ctrl_path = os.path.realpath(f"/sys/class/nvme/{m.group(1)}")
                m2 = re.findall(r"([0-9a-fA-F]{4,}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", ctrl_path)
                if m2: return m2[-1]
                
        return ""
    except Exception:
        return ""


def _nvme_extra_info(dev_path: str) -> dict:
    extra = {"firmware": "", "temperature_c": "", "sante": "", "endurance_pct": ""}
    if not _tool("nvme"):
        return extra
    rc, out, _ = _run_root_readonly(["nvme", "id-ctrl", dev_path], timeout=8)
    if rc == 0:
        m = re.search(r"^fr\s*:\s*(.+)$", out, re.M)
        if m: extra["firmware"] = m.group(1).strip()
    rc, out, _ = _run_root_readonly(["nvme", "smart-log", dev_path], timeout=8)
    if rc == 0:
        m = re.search(r"temperature\s*:\s*([0-9]+)", out)
        if m: extra["temperature_c"] = m.group(1)
        m = re.search(r"percentage_used\s*:\s*([0-9]+)%", out)
        if m: extra["endurance_pct"] = m.group(1)
        crit = re.search(r"critical_warning\s*:\s*(.+)", out)
        if crit:
            extra["sante"] = "OK" if crit.group(1).strip() in ("0", "0x0") else f"Warning {crit.group(1).strip()}"
    return extra


def _format_usb_speed(mbps: str) -> str:
    try:
        val = float(str(mbps).strip())
    except Exception:
        return ""
    mapping = {
        1.5: "USB 1.0 Low Speed 1.5 Mb/s",
        12: "USB 1.1 Full Speed 12 Mb/s",
        480: "USB 2.0 High Speed 480 Mb/s",
        5000: "USB 3.x Gen1 5 Gb/s",
        10000: "USB 3.x Gen2 10 Gb/s",
        20000: "USB 3.2 Gen2x2 20 Gb/s",
        40000: "USB4 40 Gb/s",
    }
    for k, label in mapping.items():
        if abs(val - k) < max(1, k * 0.05):
            return label
    return f"USB {val:g} Mb/s"


def _usb_speed_for_block(name: str) -> str:
    """Retourne la vitesse USB négociée du périphérique de stockage si disponible."""
    try:
        path = os.path.realpath(f"/sys/block/{name}/device")
        cur = path
        while cur and cur != "/":
            speed_file = os.path.join(cur, "speed")
            if os.path.exists(speed_file):
                return _format_usb_speed(_read_first_existing([speed_file]))
            cur = os.path.dirname(cur)
    except Exception:
        pass
    return ""


def _smartctl_text(dev_path: str, args, tran: str = "") -> str:
    """Exécute smartctl sans jamais demander de mot de passe.

    Pour les disques USB/SATA en boîtier, essaye aussi -d sat. Les erreurs
    de droit ou de support SMART sont silencieuses: elles ne doivent pas
    empêcher l'affichage du reste des disques.
    """
    if not _tool("smartctl"):
        return ""

    base_args = list(args) + [dev_path]
    attempts = [base_args]
    if (tran or "").lower() == "usb":
        attempts.append(["-d", "sat"] + base_args)

    for a in attempts:
        rc, out, _ = _run_root_readonly(["smartctl"] + a, timeout=10)
        if rc == 0 and out:
            return out
    return ""


def _smart_extra_info(dev_path: str, tran: str = "") -> dict:
    """Infos SATA/SAS/USB/HDD/SSD via SMART: vitesse interface, RPM, firmware, santé, température."""
    extra = {
        "interface_detail": "",
        "vitesse_interface": "",
        "vitesse_negociee": "",
        "rotation_rpm": "",
        "firmware": "",
        "serial": "",
        "temperature_c": "",
        "sante": "",
        "smart_supported": "",
    }
    info = _smartctl_text(dev_path, ["-i"], tran)
    if info:
        m = re.search(r"^(?:Firmware Version|Revision):\s*(.+)$", info, re.M)
        if m: extra["firmware"] = m.group(1).strip()
        m = re.search(r"^Serial Number:\s*(.+)$", info, re.M)
        if m: extra["serial"] = m.group(1).strip()
        m = re.search(r"^Rotation Rate:\s*(.+)$", info, re.M)
        if m:
            val = m.group(1).strip()
            extra["rotation_rpm"] = "SSD / non rotatif" if "Solid State" in val else val
        m = re.search(r"^SATA Version is:\s*(.+)$", info, re.M)
        if m:
            sata = m.group(1).strip()
            extra["interface_detail"] = sata
            # Exemples: SATA 3.3, 6.0 Gb/s (current: 6.0 Gb/s)
            m2 = re.search(r"(1\.5|3\.0|6\.0)\s*Gb/s", sata)
            if m2: extra["vitesse_interface"] = f"SATA {m2.group(1)} Gb/s"
            m2 = re.search(r"current:\s*([^\)]+)", sata, re.I)
            if m2: extra["vitesse_negociee"] = m2.group(1).strip()
        m = re.search(r"^Transport protocol:\s*(.+)$", info, re.M)
        if m and not extra["interface_detail"]:
            extra["interface_detail"] = m.group(1).strip()
        m = re.search(r"SMART support is:\s*(.+)$", info, re.M)
        if m: extra["smart_supported"] = m.group(1).strip()

    health = _smartctl_text(dev_path, ["-H"], tran)
    if health:
        if "PASSED" in health:
            extra["sante"] = "OK (SMART PASSED)"
        elif "FAILED" in health:
            extra["sante"] = "ECHEC (SMART FAILED)"
        else:
            m = re.search(r"SMART overall-health[^:]*:\s*(.+)$", health, re.M)
            if m: extra["sante"] = m.group(1).strip()

    attrs = _smartctl_text(dev_path, ["-A"], tran)
    if attrs:
        patterns = [
            r"Temperature_Celsius\s+.*?\s(\d+)\s*(?:\(|$)",
            r"Airflow_Temperature_Cel\s+.*?\s(\d+)\s*(?:\(|$)",
            r"Current Drive Temperature:\s*(\d+)",
            r"Temperature:\s*(\d+)\s*Celsius",
        ]
        for pat in patterns:
            m = re.search(pat, attrs, re.I)
            if m:
                extra["temperature_c"] = m.group(1)
                break
    return extra

def _is_real_nvme_disk(name: str, tran: str) -> bool:
    """Détecte un vrai disque NVMe local, pas un SSD SATA/NVMe derrière USB."""
    if not name.startswith("nvme"):
        return False
    if (tran or "").lower() == "usb":
        return False
    try:
        real = os.path.realpath(f"/sys/block/{name}/device")
        return "/nvme/" in real or "/nvme-subsystem/" in real or name.startswith("nvme")
    except Exception:
        return name.startswith("nvme")


def get_storage_info() -> list:
    """Retourne la liste des disques physiques avec séparation claire NVMe/SATA/USB.

    Important: on n'attribue des infos PCIe Gen/Width qu'aux vrais NVMe locaux.
    Un disque SATA/NVMe branché en USB dépend du contrôleur USB: afficher le PCIe
    du contrôleur serait faux et donnait par exemple Gen5 x16.
    """
    drives = []
    if not _tool("lsblk"):
        return drives
    import json as _json
    rc, out, _ = _run(["lsblk", "-J", "-o",
                        "NAME,KNAME,PATH,SIZE,TYPE,TRAN,MODEL,ROTA,MOUNTPOINT,FSTYPE,VENDOR,SERIAL"])
    if rc != 0:
        # Certaines vieilles versions de lsblk n'ont pas SERIAL dans -J
        rc, out, _ = _run(["lsblk", "-J", "-o",
                            "NAME,KNAME,PATH,SIZE,TYPE,TRAN,MODEL,ROTA,MOUNTPOINT,FSTYPE,VENDOR"])
    if rc != 0:
        return drives
    try:
        data = _json.loads(out)
    except Exception:
        return drives

    def disk_kind(name: str, tran: str, rota: str) -> str:
        tran_l = (tran or "").lower()
        if tran_l == "usb":
            return "USB"
        if _is_real_nvme_disk(name, tran_l) or tran_l == "nvme":
            return "NVMe"
        if tran_l in ("sata", "ata"):
            return "SSD SATA" if str(rota) == "0" else "HDD SATA"
        if tran_l in ("sas",):
            return "SSD SAS" if str(rota) == "0" else "HDD SAS"
        return "SSD" if str(rota) == "0" else "HDD"

    def walk(devices, current_drive=None, parent_tran=""):
        for dev in devices:
            dtype = dev.get("type", "")
            tran  = (dev.get("tran") or parent_tran or "").strip().lower()
            name  = dev.get("kname") or dev.get("name", "")
            size  = dev.get("size", "?")
            model = " ".join(x for x in [(dev.get("vendor") or "").strip(), (dev.get("model") or "").strip()] if x).strip()
            rota  = str(dev.get("rota", "1"))
            mp    = dev.get("mountpoint") or ""
            fstype = dev.get("fstype") or ""

            drive_ref = current_drive
            if dtype == "disk":
                kind = disk_kind(name, tran, rota)
                is_nvme = kind == "NVMe"
                pci_bus = _block_device_pci_bus(name) if is_nvme else ""
                pcie = _get_pcie_info(pci_bus) if pci_bus else {}
                smart = _smart_extra_info(f"/dev/{name}", tran)
                usb_speed = _usb_speed_for_block(name) if kind == "USB" else ""
                interface_label = "NVMe" if is_nvme else (tran.upper() if tran else "?")
                if kind == "USB" and usb_speed:
                    interface_label = usb_speed
                elif smart.get("vitesse_interface"):
                    interface_label = smart["vitesse_interface"]

                drive_ref = {
                    "nom":      f"/dev/{name}",
                    "modele":   model,
                    "taille":   size,
                    "type":     kind,
                    "interface": interface_label,
                    "interface_detail": smart.get("interface_detail", ""),
                    "vitesse_interface": smart.get("vitesse_interface", ""),
                    "vitesse_negociee": smart.get("vitesse_negociee", ""),
                    "rotation_rpm": smart.get("rotation_rpm", ""),
                    "usb_speed": usb_speed,
                    "pci_bus":  pci_bus,
                    "pcie_gen": pcie.get("pcie_gen", ""),
                    "pcie_largeur": pcie.get("pcie_largeur", ""),
                    "pcie_vitesse": pcie.get("pcie_vitesse", ""),
                    "pilote": pcie.get("driver", ""),
                    "firmware": smart.get("firmware", ""),
                    "serial": smart.get("serial", ""),
                    "temperature_c": smart.get("temperature_c", ""),
                    "endurance_pct": "",
                    "sante": smart.get("sante", ""),
                    "smart_supported": smart.get("smart_supported", ""),
                    "partitions": [],
                    "note": "PCIe non applicable: disque via USB" if kind == "USB" else "",
                }
                if is_nvme:
                    # NVMe natif: PCIe + nvme-cli prioritaire, SMART en complément.
                    nvme_extra = {k: v for k, v in _nvme_extra_info(f"/dev/{name}").items() if v}
                    drive_ref.update(nvme_extra)
                drives.append(drive_ref)

            elif dtype == "part" and drive_ref:
                drive_ref["partitions"].append({
                    "nom":    f"/dev/{name}",
                    "taille": size,
                    "mp":     mp,
                    "fs":     fstype,
                })

            for child in dev.get("children", []) or []:
                walk([child], drive_ref, tran)

    walk(data.get("blockdevices", []))
    return drives


# ─────────────────────────────────────────────────────────────────────────────
# Réseau
# ─────────────────────────────────────────────────────────────────────────────

def get_network_info() -> list:
    interfaces = []

    if _tool("ip"):
        rc, out, _ = _run(["ip", "-j", "addr"])
        if rc == 0:
            import json as _json
            try:
                ifaces = _json.loads(out)
                for iface in ifaces:
                    name  = iface.get("ifname", "")
                    state = iface.get("operstate", "")
                    mtu   = iface.get("mtu", "")
                    mac   = iface.get("address", "")
                    addrs = []
                    for a in iface.get("addr_info", []):
                        prefix = a.get("prefixlen", "")
                        local  = a.get("local", "")
                        if local:
                            addrs.append(f"{local}/{prefix}")
                    if name and name != "lo":
                        interfaces.append({
                            "nom":   name,
                            "etat":  state,
                            "mtu":   mtu,
                            "mac":   mac,
                            "adresses": addrs,
                            "modele": "",
                            "pci_bus": "",
                            "pcie_gen": "",
                            "pcie_largeur": "",
                            "pcie_vitesse": "",
                            "pilote": "",
                            "firmware": "",
                        })
            except Exception:
                pass

    for iface in interfaces:
        if _tool("ethtool"):
            rc, out, _ = _run(["ethtool", iface["nom"]], timeout=5)
            m = re.search(r"Speed:\s*(\S+)", out)
            if m: iface["vitesse"] = m.group(1)
            m = re.search(r"Duplex:\s*(\S+)", out)
            if m: iface["duplex"] = m.group(1)

            rc, out, _ = _run(["ethtool", "-i", iface["nom"]], timeout=5)
            if rc == 0:
                kv = _parse_key_values(out)
                iface["pilote"] = kv.get("driver", "")
                iface["firmware"] = kv.get("firmware-version", "")
                iface["pci_bus"] = kv.get("bus-info", "") if kv.get("bus-info", "") != "N/A" else ""
                if iface["pci_bus"]:
                    pcie = _get_pcie_info(iface["pci_bus"])
                    iface["pcie_gen"] = pcie.get("pcie_gen", "")
                    iface["pcie_largeur"] = pcie.get("pcie_largeur", "")
                    iface["pcie_vitesse"] = pcie.get("pcie_vitesse", "")
                    iface["pilote"] = iface["pilote"] or pcie.get("driver", "")
                    iface["modele"] = _pci_model(iface["pci_bus"])

    return interfaces


# ─────────────────────────────────────────────────────────────────────────────
# Télémétrie PMBus
# ─────────────────────────────────────────────────────────────────────────────

def get_pmbus_info() -> list:
    """Récupère les informations et la télémétrie des périphériques PMBus en I2C direct."""
    try:
        from core.pmbus_i2c import scan_pmbus_devices, get_pmbus_telemetry
        devices = scan_pmbus_devices()
        results = []
        for bus, addr in devices:
            tel = get_pmbus_telemetry(bus, addr)
            if tel:
                results.append(tel)
        return results
    except Exception as e:
        return [{"erreur": f"Impossible de scanner PMBus : {e}"}]


# ─────────────────────────────────────────────────────────────────────────────
# Collecte complète
# ─────────────────────────────────────────────────────────────────────────────

_PCI_CLASS_LABELS = {"01": "Stockage", "02": "Réseau", "03": "GPU / affichage", "04": "Multimédia / audio", "05": "Mémoire",
                     "06": "Pont", "07": "Communication", "08": "Système", "0c": "Bus série / USB", "0d": "Sans fil",
                     "10": "Chiffrement", "11": "Acquisition", "12": "Accélérateur"}


def _pci_ids_lookup(wanted: set) -> dict:
    """Noms {(vendeur, périphérique): (nom_vendeur, nom_périphérique)} lus dans pci.ids ({} si le fichier est absent)."""
    out: dict = {}
    for path in ("/usr/share/misc/pci.ids", "/usr/share/hwdata/pci.ids", "/usr/share/pci.ids"):
        try:
            vend_names, cur_v = {}, None
            vendors = {v for v, _d in wanted}
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    if not line.strip() or line.startswith("#"):
                        continue
                    if not line.startswith("\t"):
                        parts = line.split(None, 1)
                        cur_v = parts[0] if len(parts) == 2 and len(parts[0]) == 4 else None
                        if cur_v in vendors:
                            vend_names[cur_v] = parts[1].strip()
                    elif cur_v in vendors and not line.startswith("\t\t"):
                        parts = line.strip().split(None, 1)
                        if len(parts) == 2 and (cur_v, parts[0]) in wanted:
                            out[(cur_v, parts[0])] = (vend_names.get(cur_v, ""), parts[1].strip())
            return out
        except Exception:
            continue
    return out


def get_pcie_devices(base: str = "/sys/bus/pci/devices") -> list:
    """Tous les périphériques PCIe (hors ponts) : lien actuel / maximal (Gen + largeur), pilote, NUMA, erreurs AER.

    Lecture sysfs sans privilège ; noms via `lspci -mm -nn` si présent, sinon pci.ids, sinon identifiants. [] si indisponible.
    Le lien du port parent est ajouté : pour un GPU Intel derrière le switch de sa carte, c'est lui qui reflète le vrai câblage.
    """
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return []
    from core.inventory import _aer_counts, _read as _rd
    devs = []
    for bdf in names:
        d = os.path.join(base, bdf)
        cls = _rd(os.path.join(d, "class"))[2:6].lower()
        spd = _rd(os.path.join(d, "current_link_speed"))
        if not spd or cls.startswith("06"):           # sans lien PCIe (ou pont / switch) : ignoré
            continue
        try:
            drv = os.path.basename(os.path.realpath(os.path.join(d, "driver"))) if os.path.exists(os.path.join(d, "driver")) else ""
        except Exception:
            drv = ""
        def _w(name):
            v = _rd(os.path.join(d, name))
            return int(v) if v.isdigit() else 0
        cur_gen, max_gen = _pci_speed_to_gen(spd), _pci_speed_to_gen(_rd(os.path.join(d, "max_link_speed")))
        cur_w, max_w = _w("current_link_width"), _w("max_link_width")
        note = ""
        up = _card_upstream_link(d)          # GPU derrière le switch interne de sa carte : le vrai lien est celui du pont amont
        if up:
            pd_up = os.path.join(base, up["bridge"])
            spd = up["speed"]
            cur_gen = _pci_speed_to_gen(spd)
            cur_w = int(up["width"]) if str(up["width"]).isdigit() else cur_w
            mg = _pci_speed_to_gen(_rd(os.path.join(pd_up, "max_link_speed")))
            mw = _rd(os.path.join(pd_up, "max_link_width"))
            max_gen, max_w = mg or max_gen, int(mw) if mw.isdigit() else max_w
            note = f"lien réel lu sur le pont amont {up['bridge']} (le GPU annonce un lien interne de 2.5 GT/s x1)"
        e = {"bus": bdf, "classe": cls, "type": _PCI_CLASS_LABELS.get(cls[:2], "Autre"),
             "vendeur_id": _rd(os.path.join(d, "vendor"))[2:], "peripherique_id": _rd(os.path.join(d, "device"))[2:],
             "pilote": drv, "gen": cur_gen, "largeur": cur_w, "gen_max": max_gen, "largeur_max": max_w,
             "vitesse": spd, "vitesse_max": _rd(os.path.join(d, "max_link_speed"))}
        numa = _rd(os.path.join(d, "numa_node"))
        if numa.lstrip("-").isdigit() and int(numa) >= 0:
            e["numa"] = int(numa)
        e.update(_aer_counts(d))
        # lien du port parent (pont amont)
        try:
            parent = os.path.basename(os.path.dirname(os.path.realpath(d)))
            pd = os.path.join(base, parent)
            if parent != bdf and os.path.isdir(pd) and _rd(os.path.join(pd, "current_link_speed")):
                e["port_parent"] = parent
                e["port_gen"] = _pci_speed_to_gen(_rd(os.path.join(pd, "current_link_speed")))
                e["port_largeur"] = int(_rd(os.path.join(pd, "current_link_width")) or 0)
        except Exception:
            pass
        # état du lien
        probs = []
        if cur_w and max_w and cur_w < max_w:
            probs.append(f"largeur x{cur_w} au lieu de x{max_w}")
        if cur_gen and max_gen and cur_gen < max_gen:
            probs.append(f"Gen{cur_gen} au lieu de Gen{max_gen}" + (" (économie d'énergie possible au repos)" if cls.startswith("03") else ""))
        e["etat"] = "réduit : " + ", ".join(probs) if probs else "OK"
        if note:
            e["note"] = note
        devs.append(e)
    # noms
    named: dict = {}
    if _tool("lspci"):
        rc, out, _ = _run(["lspci", "-mm", "-nn"], timeout=10)
        if rc == 0:
            for line in out.splitlines():
                m = re.match(r'^(\S+)\s+"[^"]*"\s+"(.*?) \[[0-9a-f]{4}\]"\s+"(.*?) \[[0-9a-f]{4}\]"', line)
                if m:
                    bus = m.group(1) if m.group(1).count(":") == 2 else "0000:" + m.group(1)
                    named[bus] = f"{m.group(2)} {m.group(3)}"
    missing = {(e["vendeur_id"], e["peripherique_id"]) for e in devs if e["bus"] not in named}
    ids = _pci_ids_lookup(missing) if missing else {}
    for e in devs:
        nm = named.get(e["bus"])
        if not nm:
            v, dn = ids.get((e["vendeur_id"], e["peripherique_id"]), ("", ""))
            nm = f"{v} {dn}".strip() or f"{e['vendeur_id']}:{e['peripherique_id']}"
        e["nom"] = nm
    return devs


def name_pcie_devices(devs: list, gpus: list = None, nics: list = None) -> list:
    """Complète les noms génériques (« Intel Corporation Device », pci.ids trop ancien) avec le modèle de la fiche GPU / réseau
    ayant la même adresse PCI, sinon avec l'identifiant [vendeur:périphérique]. Retourne une nouvelle liste."""
    known = {}
    for g in (gpus or []) + (nics or []):
        if isinstance(g, dict) and g.get("pci_bus"):
            known[_normalize_pci_bus(g["pci_bus"]).lower()] = g.get("nom") or g.get("modele") or ""
    out = []
    for d in devs or []:
        d = dict(d) if isinstance(d, dict) else d
        if isinstance(d, dict):
            nm = str(d.get("nom", ""))
            if re.search(r"\bDevice$", nm) or not nm:
                nm = known.get(str(d.get("bus", "")).lower()) or f"{nm} [{d.get('vendeur_id', '')}:{d.get('peripherique_id', '')}]".strip()
                d["nom"] = nm
        out.append(d)
    return out


def group_pcie_devices(devs: list) -> list:
    """Regroupe les fonctions d'une même carte (ex. les 4 ports d'une X710 : 000d:01:00.0 à .3) en une seule ligne.

    Clé : même domaine:bus:périphérique ET même nom/type. Le lien affiché est celui de la première fonction (identique en pratique,
    sinon l'état « réduit » l'emporte), les erreurs AER sont additionnées. Le JSON garde le détail par fonction (`pcie`).
    """
    groups: dict = {}
    order: list = []
    for d in devs or []:
        if not isinstance(d, dict):
            continue
        bus = str(d.get("bus", ""))
        key = (bus.rsplit(".", 1)[0], d.get("nom", ""), d.get("type", ""))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(d)
    out = []
    for key in order:
        members = groups[key]
        first = dict(members[0])
        if len(members) > 1:
            funcs = [str(m.get("bus", "")).rsplit(".", 1)[-1] for m in members]
            first["bus"] = f"{key[0]}.{funcs[0]}-{funcs[-1]}" if len(funcs) > 2 else f"{key[0]}.{'+'.join(funcs)}"
            first["fonctions"] = len(members)
            for k in ("aer_cor", "aer_nonfatal", "aer_fatal"):
                first[k] = sum(int(m.get(k, 0) or 0) for m in members)
            drivers = sorted({m.get("pilote", "") for m in members if m.get("pilote")})
            first["pilote"] = "/".join(drivers)
            bad = next((m for m in members if str(m.get("etat", "")).startswith("réduit")), None)
            if bad:
                first.update({"etat": bad["etat"], "gen": bad.get("gen"), "largeur": bad.get("largeur")})
        out.append(first)
    return out


# Budget de temps (secondes) par section de la collecte : au-delà, la section est abandonnée (résultat vide) et signalée.
# Les sections tournent EN PARALLÈLE : la collecte dure au plus le plus grand budget, jamais la somme des attentes.
SECTION_BUDGET_S = {"pmbus": 25, "bmc": 45, "telemetry": 45, "stockage": 60, "gpus": 45, "pcie": 40}
SECTION_BUDGET_DEFAULT_S = 30


def collect_full_system_info(progress=None) -> dict:
    """Collecte toutes les informations matérielles et télémétriques.

    Chaque section (OS, CPU, BMC, GPU, stockage, PMBus I²C…) tourne dans son propre fil avec un budget de temps. Avant, tout
    s'enchaînait : un BMC muet (10 appels de 8-15 s), un nvidia-smi bloqué, un disque en erreur ou le balayage I²C
    (112 adresses × chaque bus, sans aucun délai possible) pouvaient donner une collecte de plusieurs minutes, voire sans fin.
    Une section abandonnée est listée dans `sections_incompletes` (et dans l'audit matériel) ; `durees_s` donne le temps de chacune.
    `progress(section, état)` (facultatif) est appelé au début (« en cours ») et à la fin (« ok » / « abandonnée »)."""
    import threading
    import time
    from core.telemetry_aggregator import collect_all_motherboard_telemetry

    jobs = {
        "os": (get_os_info, {}), "cpu": (get_cpu_info, {}), "ram": (get_ram_info, {}), "carte_mere": (get_motherboard_info, {}),
        "bmc": (get_bmc_info, {}), "gpus": (get_gpu_info, []), "stockage": (get_storage_info, []), "reseau": (get_network_info, []),
        "pcie": (get_pcie_devices, []), "pmbus": (get_pmbus_info, []), "telemetry": (collect_all_motherboard_telemetry, []),
    }
    out: dict = {k: dflt for k, (_fn, dflt) in jobs.items()}
    durations: dict = {}
    lock = threading.Lock()

    def runner(key, fn):
        t = time.time()
        if progress:
            try: progress(key, "en cours")
            except Exception: pass
        try:
            val = fn()
            with lock:
                if key not in durations:                 # ignoré si la section a déjà été abandonnée
                    out[key] = val
        except Exception as e:
            with lock:
                if key not in durations:
                    out[key] = {"erreur": f"{type(e).__name__}: {e}"} if isinstance(out[key], dict) else out[key]
        with lock:
            durations.setdefault(key, round(time.time() - t, 1))
        if progress:
            try: progress(key, "ok")
            except Exception: pass

    threads = {k: threading.Thread(target=runner, args=(k, fn), daemon=True, name=f"sysinfo-{k}") for k, (fn, _d) in jobs.items()}
    t0 = time.time()
    for th in threads.values():
        th.start()
    late = []
    for key, th in threads.items():
        th.join(timeout=max(0.1, SECTION_BUDGET_S.get(key, SECTION_BUDGET_DEFAULT_S) - (time.time() - t0)))
        if th.is_alive():
            with lock:
                durations[key] = None                    # marque « abandonnée » : le résultat tardif sera ignoré
            late.append({"section": key, "budget_s": SECTION_BUDGET_S.get(key, SECTION_BUDGET_DEFAULT_S)})
            if progress:
                try: progress(key, "abandonnée")
                except Exception: pass
    try:
        from core.telemetry_aggregator import summarize_motherboard_telemetry
        tel_sum = summarize_motherboard_telemetry(out["telemetry"] if isinstance(out["telemetry"], list) else [])
    except Exception:
        tel_sum = {}
    res = {"timestamp": datetime.now().isoformat(), **out, "telemetry_summary": tel_sum,
           "durees_s": {k: v for k, v in durations.items() if v is not None}}
    if late:
        res["sections_incompletes"] = late
    return res


# ─────────────────────────────────────────────────────────────────────────────
# Formatage texte pour affichage rapide
# ─────────────────────────────────────────────────────────────────────────────

def format_summary(info: dict) -> str:
    lines = []
    def sep(title):
        lines.append(f"\n{'─'*50}")
        lines.append(f"  {title}")
        lines.append('─'*50)

    sep("SYSTEME D'EXPLOITATION")
    os_ = info.get("os", {})
    lines.append(f"  Distro    : {os_.get('distro','?')}")
    lines.append(f"  Kernel    : {os_.get('kernel','?')}")
    lines.append(f"  Hostname  : {os_.get('hostname','?')}")
    lines.append(f"  Uptime    : {os_.get('uptime','?')}")

    sep("PROCESSEUR (CPU)")
    cpu = info.get("cpu", {})
    lines.append(f"  Modele    : {cpu.get('modele','?')}")
    lines.append(f"  Coeurs    : {cpu.get('coeurs_phys','?')} physiques / {cpu.get('threads','?')} threads")
    if cpu.get("freq_max_mhz"): lines.append(f"  Freq max  : {cpu['freq_max_mhz']}")
    if cpu.get("freq_cur_mhz"): lines.append(f"  Freq actu : {cpu['freq_cur_mhz']}")
    if cpu.get("cache_l3"):     lines.append(f"  Cache L3  : {cpu['cache_l3']}")
    if cpu.get("socket"):       lines.append(f"  Socket    : {cpu['socket']}")
    if cpu.get("vulnerabilites"):
        lines.append(f"  Vuln.     : {len(cpu['vulnerabilites'])} detectee(s)")

    sep("MEMOIRE VIVE (RAM)")
    ram = info.get("ram", {})
    lines.append(f"  Total     : {ram.get('total_go','?')} Go")
    lines.append(f"  Utilisee  : {ram.get('utilise_go','?')} Go  ({ram.get('utilisation_pct','?')}%)")
    lines.append(f"  Disponible: {ram.get('disponible_go','?')} Go")
    if ram.get("type"):       lines.append(f"  Type      : {ram['type']}")
    if ram.get("vitesse_mhz"): lines.append(f"  Vitesse   : {ram['vitesse_mhz']}")
    if ram.get("barrettes"):
        for i, b in enumerate(ram["barrettes"]):
            parts = [b.get("taille","?"), b.get("type",""), b.get("vitesse",""), b.get("fabricant","")]
            lines.append(f"  Slot {i+1}    : {' | '.join(p for p in parts if p)}")

    sep("CARTE MERE")
    mb = info.get("carte_mere", {})
    lines.append(f"  Fabricant : {mb.get('fabricant','?')}")
    lines.append(f"  Modele    : {mb.get('modele','?')}")
    if mb.get("version"):  lines.append(f"  Version   : {mb['version']}")
    if mb.get("bios"):     lines.append(f"  BIOS      : {mb['bios']}  ({mb.get('bios_date','')})")
    if mb.get("chipset"):  lines.append(f"  Chipset   : {mb['chipset']}")

    sep("BMC / IPMI")
    bmc = info.get("bmc", {})
    if not bmc or not bmc.get("present"):
        lines.append("  Non detectee / ipmitool indisponible")
    else:
        if bmc.get("firmware_revision"): lines.append(f"  Version BMC : {bmc['firmware_revision']}")
        if bmc.get("manufacturer"):      lines.append(f"  Fabricant   : {bmc['manufacturer']}")
        if bmc.get("ipmi_version"):      lines.append(f"  IPMI        : {bmc['ipmi_version']}")
        if bmc.get("ip_address"):        lines.append(f"  IP BMC      : {bmc['ip_address']}")
        if bmc.get("mac"):               lines.append(f"  MAC BMC     : {bmc['mac']}")
        if bmc.get("sel_status"):        lines.append(f"  SEL         : {bmc['sel_status']}")
        if bmc.get("redfish_present"):   lines.append(f"  Redfish API : Disponible ({bmc.get('redfish_url','')})")

    sep("CARTE(S) GRAPHIQUE(S) (GPU)")
    gpus = info.get("gpus", [])
    if not gpus:
        lines.append("  Aucun GPU detecte")
    for i, gpu in enumerate(gpus):
        prefix = f"  GPU {i}    : "
        lines.append(f"{prefix}{gpu.get('nom') or gpu.get('modele','?')}")
        if gpu.get("pci_bus"):         lines.append(f"            Bus PCI   : {gpu['pci_bus']}")
        if gpu.get("modele"):          lines.append(f"            Modele    : {gpu['modele']}")
        if gpu.get("vram_total_mo"):   lines.append(f"            VRAM      : {gpu['vram_total_mo']} Mo")
        if gpu.get("puissance_max_w"): lines.append(f"            Pmax      : {gpu['puissance_max_w']} W")
        if gpu.get("temperature_c"):   lines.append(f"            Temp      : {gpu['temperature_c']} °C")
        if gpu.get("pcie_gen") or gpu.get("pcie_largeur"): lines.append(f"            PCIe      : Gen {gpu.get('pcie_gen','?')} x{gpu.get('pcie_largeur','?')}")
        if gpu.get("pilote") or gpu.get("driver_version"): lines.append(f"            Pilote    : {gpu.get('pilote','')} {gpu.get('driver_version','')}")

    sep("STOCKAGE")
    for i, drv in enumerate(info.get("stockage", []), 1):
        line = f"  Disque {i}: {drv['nom']}  [{drv.get('type','?')} / {drv.get('interface','?')}  {drv.get('taille','?')}]"
        if drv.get("modele"): line += f"  {drv['modele']}"
        if drv.get("sante"):  line += f"  — {drv['sante']}"
        lines.append(line)
        if drv.get("note"): lines.append(f"    Note    : {drv['note']}")
        if drv.get("pci_bus"): lines.append(f"    Bus PCI : {drv['pci_bus']}")
        if drv.get("pcie_gen") or drv.get("pcie_largeur"): lines.append(f"    PCIe    : Gen {drv.get('pcie_gen','?')} x{drv.get('pcie_largeur','?')}")
        if drv.get("firmware"): lines.append(f"    FW      : {drv['firmware']}")
        if drv.get("temperature_c"): lines.append(f"    Temp    : {drv['temperature_c']} °C")
        for p in drv.get("partitions", []):
            mp_str = f" -> {p['mp']}" if p.get("mp") else ""
            lines.append(f"    {p['nom']}  {p['taille']}  {p.get('fs','')}{mp_str}")

    sep("RESEAU")
    for iface in info.get("reseau", []):
        state = "UP" if iface.get("etat") == "UP" else "down"
        speed = f"  {iface.get('vitesse','')}" if iface.get("vitesse") else ""
        lines.append(f"  {iface['nom']:12} [{state}]{speed}")
        if iface.get("modele"): lines.append(f"               {iface['modele']}")
        if iface.get("pci_bus"): lines.append(f"               Bus PCI: {iface['pci_bus']}")
        if iface.get("pcie_gen") or iface.get("pcie_largeur"): lines.append(f"               PCIe: Gen {iface.get('pcie_gen','?')} x{iface.get('pcie_largeur','?')}")
        if iface.get("pilote"): lines.append(f"               Pilote: {iface['pilote']}")
        for addr in iface.get("adresses", []):
            lines.append(f"               {addr}")

    return "\n".join(lines)
