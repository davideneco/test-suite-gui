"""inventory.py — Inventaire matériel avant / après tests et détection des dérives.

Un inventaire (``collect_inventory``) photographie ce que le système voit : CPU, mémoire, périphériques PCI
(GPU, NVMe, cartes réseau…) avec largeur/vitesse de lien et compteurs d'erreurs AER, GPU NVIDIA, NVMe, disques,
interfaces réseau, compteurs ECC (EDAC), événements BMC (SEL), versions de firmware.

``diff_inventory(avant, apres)`` compare deux photographies et signale ce qui a disparu, un lien PCIe qui a perdu
de la largeur, les erreurs apparues pendant les tests, etc. Un périphérique qui disparaît ou un lien qui se
dégrade trahit un câble, un connecteur ou un riser douteux.

Tout est best-effort : sans root ni outil (dmidecode, ipmitool, nvidia-smi), la partie correspondante est omise.
"""

import os
import re
import shutil
import socket
import subprocess
from datetime import datetime
from typing import Dict, List, Optional
from core.safety import nvidia_smi_usable

FAIL, WARN, INFO = "fail", "warn", "info"

# Classes PCI suivies (octet de poids fort du code de classe) : stockage, réseau, affichage/GPU, accélérateurs
_TRACKED_CLASSES = {"01": "stockage", "02": "réseau", "03": "gpu", "12": "accélérateur"}


def _run(cmd: list, timeout: float = 6.0) -> tuple:
    """Exécute une commande avec timeout ; retourne (code, stdout, stderr), jamais d'exception."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env={**os.environ, "LC_ALL": "C", "LANG": "C"})      # libellés anglais, quelle que soit la langue du système
        return r.returncode, r.stdout, r.stderr
    except Exception as exc:
        return -1, "", str(exc)


def _read(path: str, default: str = "") -> str:
    """Lit un petit fichier sysfs ; chaîne par défaut en cas d'erreur."""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return default


def _int(path: str, default: int = 0) -> int:
    """Lit un entier depuis un fichier sysfs."""
    try:
        return int(_read(path).split()[0])
    except Exception:
        return default


def _priv_prefix() -> Optional[list]:
    """Préfixe d'élévation non bloquant : [] si root, ['sudo','-n'] si disponible, None sinon."""
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        return []
    return ["sudo", "-n"] if shutil.which("sudo") else None


# ─────────────────────────────────────────────────────────────────────────────
# Collecte
# ─────────────────────────────────────────────────────────────────────────────

def _aer_counts(devdir: str) -> Dict[str, int]:
    """Compteurs AER d'un périphérique PCI : correctable / non fatal / fatal (0 si indisponible)."""
    out = {}
    for key, fname in (("aer_cor", "aer_dev_correctable"), ("aer_nonfatal", "aer_dev_nonfatal"), ("aer_fatal", "aer_dev_fatal")):
        total = 0
        for line in _read(os.path.join(devdir, fname)).splitlines():
            if line.startswith("TOTAL_ERR"):
                try:
                    total = int(line.split()[-1])
                except ValueError:
                    pass
        out[key] = total
    return out


def _pci_entry(bdf: str, base: str) -> dict:
    """Décrit un périphérique PCI : identifiants, pilote, liens courant/max, AER, port parent."""
    d = os.path.join(base, bdf)
    drv = ""
    try:
        drv = os.path.basename(os.path.realpath(os.path.join(d, "driver")))
    except Exception:
        pass
    entry = {
        "bdf": bdf, "class": _read(os.path.join(d, "class"))[2:6],
        "vendor": _read(os.path.join(d, "vendor"))[2:], "device": _read(os.path.join(d, "device"))[2:],
        "driver": drv if drv != "driver" else "",
        "link_speed": _read(os.path.join(d, "current_link_speed")), "link_width": _int(os.path.join(d, "current_link_width")),
        "max_speed": _read(os.path.join(d, "max_link_speed")), "max_width": _int(os.path.join(d, "max_link_width")),
        "numa": _int(os.path.join(d, "numa_node"), -1),
    }
    entry.update(_aer_counts(d))
    # Port parent (pont PCIe auquel le périphérique est relié) : sa liaison reflète le câblage réel
    try:
        parent = os.path.basename(os.path.dirname(os.path.realpath(d)))
        if re.match(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$", parent):
            pd = os.path.join(base, parent)
            entry["port"] = parent
            entry["port_link_speed"] = _read(os.path.join(pd, "current_link_speed"))
            entry["port_link_width"] = _int(os.path.join(pd, "current_link_width"))
            pa = _aer_counts(pd)
            entry["port_aer_cor"], entry["port_aer_nonfatal"], entry["port_aer_fatal"] = pa["aer_cor"], pa["aer_nonfatal"], pa["aer_fatal"]
    except Exception:
        pass
    return entry


def _collect_pci() -> List[dict]:
    """Périphériques PCI de classes suivies (stockage, réseau, GPU, accélérateurs)."""
    base = "/sys/bus/pci/devices"
    out = []
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for bdf in names:
        cls = _read(os.path.join(base, bdf, "class"))[2:4]
        if cls in _TRACKED_CLASSES:
            e = _pci_entry(bdf, base)
            e["kind"] = _TRACKED_CLASSES[cls]
            out.append(e)
    return out


def _collect_gpus() -> List[dict]:
    """GPU NVIDIA via nvidia-smi (identité, lien PCIe, ECC) ; [] si absent."""
    if not nvidia_smi_usable():
        return []
    fields = "index,uuid,name,serial,pci.bus_id,pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max,memory.total,driver_version,vbios_version"
    rc, out, _ = _run(["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"], timeout=15)
    if rc != 0:
        return []
    keys = fields.split(",")
    gpus = []
    for line in out.splitlines():
        vals = [v.strip() for v in line.split(",")]
        if len(vals) == len(keys):
            gpus.append(dict(zip(keys, vals)))
    # ECC : champ séparé (non pris en charge par toutes les cartes → ne doit pas faire échouer le reste)
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=index,ecc.errors.corrected.volatile.total,ecc.errors.uncorrected.volatile.total",
                       "--format=csv,noheader,nounits"], timeout=15)
    if rc == 0:
        ecc = {}
        for line in out.splitlines():
            p = [v.strip() for v in line.split(",")]
            if len(p) == 3:
                ecc[p[0]] = p
        for g in gpus:
            p = ecc.get(g.get("index", ""))
            if p:
                for k, v in (("ecc_corrected", p[1]), ("ecc_uncorrected", p[2])):
                    if v.isdigit():
                        g[k] = int(v)
    return gpus


def _collect_nvme() -> List[dict]:
    """Contrôleurs NVMe (modèle, numéro de série, firmware, état)."""
    base = "/sys/class/nvme"
    out = []
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for n in names:
        d = os.path.join(base, n)
        out.append({"name": n, "model": _read(os.path.join(d, "model")), "serial": _read(os.path.join(d, "serial")),
                    "firmware": _read(os.path.join(d, "firmware_rev")), "state": _read(os.path.join(d, "state")),
                    "transport": _read(os.path.join(d, "transport")), "address": _read(os.path.join(d, "address"))})
    return out


def _collect_disks() -> List[dict]:
    """Disques bloc (hors loop/ram/dm) : taille, modèle, série, transport."""
    out = []
    if not shutil.which("lsblk"):
        return out
    rc, text, _ = _run(["lsblk", "-dn", "-P", "-b", "-o", "NAME,SIZE,MODEL,SERIAL,TRAN,TYPE,ROTA"], timeout=8)
    if rc != 0:
        return out
    for line in text.splitlines():
        kv = dict(re.findall(r'(\w+)="([^"]*)"', line))
        if kv.get("TYPE") == "disk" and not re.match(r"(loop|ram|zram|nbd)", kv.get("NAME", "")):
            out.append({"name": kv.get("NAME", ""), "size_gb": round(int(kv.get("SIZE") or 0) / 1e9, 1),
                        "model": kv.get("MODEL", "").strip(), "serial": kv.get("SERIAL", "").strip(),
                        "transport": kv.get("TRAN", ""), "rotational": kv.get("ROTA", "") == "1"})
    return out


def _collect_nics() -> List[dict]:
    """Interfaces réseau physiques : vitesse, lien, compteurs d'erreurs."""
    base = "/sys/class/net"
    out = []
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for n in names:
        d = os.path.join(base, n)
        if n == "lo" or not os.path.exists(os.path.join(d, "device")):
            continue
        st = os.path.join(d, "statistics")
        out.append({"name": n, "mac": _read(os.path.join(d, "address")), "speed": _int(os.path.join(d, "speed"), -1),
                    "duplex": _read(os.path.join(d, "duplex")), "carrier": _read(os.path.join(d, "carrier")) == "1",
                    "state": _read(os.path.join(d, "operstate")),
                    "driver": os.path.basename(os.path.realpath(os.path.join(d, "device", "driver"))) if os.path.exists(os.path.join(d, "device", "driver")) else "",
                    "rx_errors": _int(os.path.join(st, "rx_errors")), "tx_errors": _int(os.path.join(st, "tx_errors")),
                    "rx_crc_errors": _int(os.path.join(st, "rx_crc_errors")), "rx_dropped": _int(os.path.join(st, "rx_dropped")),
                    "tx_dropped": _int(os.path.join(st, "tx_dropped"))})
    return out


def _collect_cpu() -> dict:
    """CPU : modèle, sockets, cœurs, threads (lscpu, repli /proc/cpuinfo)."""
    info = {"threads": os.cpu_count() or 0}
    rc, out, _ = _run(["lscpu"], timeout=5)
    if rc == 0:
        kv = {}
        for line in out.splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                kv[k.strip()] = v.strip()
        info["model"] = kv.get("Model name", "")
        for key, label in (("sockets", "Socket(s)"), ("cores_per_socket", "Core(s) per socket"), ("threads_per_core", "Thread(s) per core")):
            try:
                info[key] = int(kv.get(label, "0"))
            except ValueError:
                pass
    else:
        for line in _read("/proc/cpuinfo").splitlines():
            if line.lower().startswith("model name"):
                info["model"] = line.split(":", 1)[1].strip()
                break
    return info


def _collect_memory() -> dict:
    """Mémoire : total visible, barrettes (dmidecode si possible), compteurs ECC (EDAC)."""
    mem = {}
    for line in _read("/proc/meminfo").splitlines():
        if line.startswith("MemTotal:"):
            mem["total_gb"] = round(int(line.split()[1]) / 1048576, 1)
            break
    ce = ue = 0
    base = "/sys/devices/system/edac/mc"
    try:
        for mc in os.listdir(base):
            if mc.startswith("mc"):
                ce += _int(os.path.join(base, mc, "ce_count"))
                ue += _int(os.path.join(base, mc, "ue_count"))
        mem["edac"] = True
    except Exception:
        mem["edac"] = False
    mem["ecc_corrected"], mem["ecc_uncorrected"] = ce, ue
    pre = _priv_prefix()
    if pre is not None and shutil.which("dmidecode"):
        rc, out, _ = _run(pre + ["dmidecode", "-t", "memory"], timeout=10)
        if rc == 0:
            dimms = []
            for block in out.split("\n\n"):
                if "Memory Device" not in block:
                    continue
                kv = dict(re.findall(r"^\s*([A-Za-z ]+):\s*(.+)$", block, re.M))
                size = kv.get("Size", "")
                if size and "No Module" not in size:
                    dimms.append({"slot": kv.get("Locator", ""), "size": size, "speed": kv.get("Configured Memory Speed", kv.get("Speed", "")),
                                  "part": kv.get("Part Number", "").strip(), "serial": kv.get("Serial Number", "").strip()})
            if dimms:
                mem["dimms"] = dimms
    return mem


def _collect_bmc() -> dict:
    """Événements BMC (SEL) et version du firmware BMC ; {} si ipmitool/droits indisponibles."""
    pre = _priv_prefix()
    if pre is None or not shutil.which("ipmitool") or not os.path.exists("/dev/ipmi0") and not os.path.exists("/dev/ipmi/0"):
        return {}
    info = {}
    # Nombre d'entrées (rapide) puis seulement les 50 dernières : un SEL complet peut prendre des dizaines de secondes
    rc, out, _ = _run(pre + ["ipmitool", "sel", "info"], timeout=10)
    m = re.search(r"Entries\s*:\s*(\d+)", out or "") if rc == 0 else None
    if m:
        info["sel_count"] = int(m.group(1))
        rc, out, _ = _run(pre + ["ipmitool", "sel", "elist", "last", "50"], timeout=15)
        if rc == 0:
            info["sel_last"] = [l.strip() for l in out.splitlines() if l.strip()][-50:]
    rc, out, _ = _run(pre + ["ipmitool", "mc", "info"], timeout=8)
    if rc == 0:
        m = re.search(r"Firmware Revision\s*:\s*(\S+)", out)
        if m:
            info["bmc_firmware"] = m.group(1)
    return info


def _collect_firmware() -> dict:
    """BIOS, carte mère, noyau."""
    fw = {}
    for key, f in (("bios_version", "bios_version"), ("bios_date", "bios_date"), ("board_vendor", "board_vendor"),
                   ("board_name", "board_name"), ("product_name", "product_name")):
        v = _read(f"/sys/class/dmi/id/{f}")
        if v:
            fw[key] = v
    fw["kernel"] = os.uname().release if hasattr(os, "uname") else ""
    return fw


def collect_inventory() -> dict:
    """Photographie complète du matériel vu par le système (jamais d'exception)."""
    inv = {"taken": datetime.now().isoformat(timespec="seconds"), "host": socket.gethostname()}
    for key, fn in (("cpu", _collect_cpu), ("memory", _collect_memory), ("pci", _collect_pci), ("gpus", _collect_gpus),
                    ("nvme", _collect_nvme), ("disks", _collect_disks), ("nics", _collect_nics),
                    ("bmc", _collect_bmc), ("firmware", _collect_firmware)):
        try:
            inv[key] = fn()
        except Exception as exc:
            inv[key] = {} if key in ("cpu", "memory", "bmc", "firmware") else []
            inv.setdefault("errors", []).append(f"{key}: {exc}")
    return inv


# ─────────────────────────────────────────────────────────────────────────────
# Comparaison
# ─────────────────────────────────────────────────────────────────────────────

def _f(level: str, label: str, detail: str = "") -> dict:
    """Constat de comparaison."""
    return {"level": level, "label": label, "detail": detail}


def _speed_val(s: str) -> float:
    """Extrait la vitesse PCIe en GT/s depuis « 16.0 GT/s PCIe » (0 si inconnue)."""
    m = re.match(r"\s*([0-9.]+)", s or "")
    return float(m.group(1)) if m else 0.0


def _by(items: List[dict], key: str) -> Dict[str, dict]:
    """Indexe une liste de dicts par une clé (entrées sans clé ignorées)."""
    return {i[key]: i for i in items if i.get(key)}


def _inc(before: dict, after: dict, key: str) -> int:
    """Augmentation d'un compteur entre deux photographies (0 si baisse/absent)."""
    return max(0, int(after.get(key, 0) or 0) - int(before.get(key, 0) or 0))


def diff_inventory(before: dict, after: dict) -> dict:
    """Compare deux inventaires. Retourne {'findings': [...], 'counts': {fail,warn,info}, 'identical': bool}.

    Règles : périphérique disparu = échec ; largeur de lien PCIe réduite = échec ; vitesse réduite = avertissement
    (hors GPU : leur vitesse chute légitimement au repos) ; erreurs AER/ECC/réseau/SEL apparues = avertissement
    (échec si non corrigeables) ; nouveau périphérique, firmware modifié = avertissement.
    """
    F: List[dict] = []
    # CPU / mémoire
    bc, ac = before.get("cpu", {}), after.get("cpu", {})
    if bc.get("threads") and ac.get("threads") and bc["threads"] != ac["threads"]:
        F.append(_f(FAIL, "Nombre de threads CPU modifié", f"{bc['threads']} → {ac['threads']}"))
    bm, am = before.get("memory", {}), after.get("memory", {})
    if bm.get("total_gb") and am.get("total_gb") and abs(bm["total_gb"] - am["total_gb"]) / bm["total_gb"] > 0.005:
        F.append(_f(FAIL, "Mémoire visible modifiée", f"{bm['total_gb']} Go → {am['total_gb']} Go"))
    bd, ad = {d["slot"]: d for d in bm.get("dimms", [])}, {d["slot"]: d for d in am.get("dimms", [])}
    for slot in sorted(set(bd) - set(ad)):
        F.append(_f(FAIL, f"Barrette mémoire disparue ({slot})", bd[slot].get("size", "")))
    for slot in sorted(set(ad) - set(bd)):
        F.append(_f(WARN, f"Barrette mémoire apparue ({slot})", ad[slot].get("size", "")))
    if _inc(bm, am, "ecc_corrected"):
        F.append(_f(WARN, f"Erreurs mémoire corrigées (ECC) : +{_inc(bm, am, 'ecc_corrected')}", "compteur EDAC"))
    if _inc(bm, am, "ecc_uncorrected"):
        F.append(_f(FAIL, f"Erreurs mémoire NON corrigées : +{_inc(bm, am, 'ecc_uncorrected')}", "compteur EDAC"))
    # GPU NVIDIA (clé : UUID)
    bg, ag = _by(before.get("gpus", []), "uuid"), _by(after.get("gpus", []), "uuid")
    for u in sorted(set(bg) - set(ag)):
        g = bg[u]
        F.append(_f(FAIL, f"GPU {g.get('index', '?')} disparu ({g.get('name', '')})", f"bus {g.get('pci.bus_id', '')}"))
    for u in sorted(set(ag) - set(bg)):
        F.append(_f(WARN, f"GPU nouveau ({ag[u].get('name', '')})", f"bus {ag[u].get('pci.bus_id', '')}"))
    for u in set(bg) & set(ag):
        b, a = bg[u], ag[u]
        try:
            wb, wa = int(b.get("pcie.link.width.current", 0)), int(a.get("pcie.link.width.current", 0))
        except ValueError:
            wb = wa = 0
        if wa and wb and wa < wb:
            F.append(_f(FAIL, f"GPU {a.get('index', '?')} : lien PCIe réduit de x{wb} à x{wa}", f"bus {a.get('pci.bus_id', '')}"))
        if _inc(b, a, "ecc_corrected"):
            F.append(_f(WARN, f"GPU {a.get('index', '?')} : erreurs ECC corrigées +{_inc(b, a, 'ecc_corrected')}"))
        if _inc(b, a, "ecc_uncorrected"):
            F.append(_f(FAIL, f"GPU {a.get('index', '?')} : erreurs ECC non corrigées +{_inc(b, a, 'ecc_uncorrected')}"))
        for key, label in (("driver_version", "pilote"), ("vbios_version", "VBIOS")):
            if b.get(key) and a.get(key) and b[key] != a[key]:
                F.append(_f(WARN, f"GPU {a.get('index', '?')} : {label} modifié", f"{b[key]} → {a[key]}"))
    # NVMe (clé : numéro de série, sinon nom)
    bn = {n.get("serial") or n["name"]: n for n in before.get("nvme", [])}
    an = {n.get("serial") or n["name"]: n for n in after.get("nvme", [])}
    for k in sorted(set(bn) - set(an)):
        F.append(_f(FAIL, f"NVMe disparu ({bn[k].get('model', '')})", f"série {bn[k].get('serial', '')}"))
    for k in sorted(set(an) - set(bn)):
        F.append(_f(WARN, f"NVMe nouveau ({an[k].get('model', '')})", f"série {an[k].get('serial', '')}"))
    for k in set(an):
        if an[k].get("state") and an[k]["state"] != "live":
            F.append(_f(FAIL, f"NVMe {an[k]['name']} dans l'état « {an[k]['state']} »", an[k].get("model", "")))
        b = bn.get(k)
        if b and b.get("firmware") and an[k].get("firmware") and b["firmware"] != an[k]["firmware"]:
            F.append(_f(WARN, f"NVMe {an[k]['name']} : firmware modifié", f"{b['firmware']} → {an[k]['firmware']}"))
    # Disques (clé : série, sinon nom)
    bk = {d.get("serial") or d["name"]: d for d in before.get("disks", [])}
    ak = {d.get("serial") or d["name"]: d for d in after.get("disks", [])}
    for k in sorted(set(bk) - set(ak)):
        F.append(_f(FAIL, f"Disque disparu ({bk[k].get('model', '') or bk[k]['name']})", f"{bk[k].get('size_gb', '')} Go"))
    for k in sorted(set(ak) - set(bk)):
        F.append(_f(WARN, f"Disque nouveau ({ak[k].get('model', '') or ak[k]['name']})", f"{ak[k].get('size_gb', '')} Go"))
    # Réseau (clé : nom)
    bi, ai = _by(before.get("nics", []), "name"), _by(after.get("nics", []), "name")
    for n in sorted(set(bi) - set(ai)):
        F.append(_f(FAIL, f"Interface réseau disparue ({n})"))
    for n in sorted(set(ai) & set(bi)):
        b, a = bi[n], ai[n]
        if b.get("speed", -1) > 0 and a.get("speed", -1) > 0 and a["speed"] < b["speed"]:
            F.append(_f(WARN, f"{n} : vitesse de lien réduite", f"{b['speed']} → {a['speed']} Mb/s (câble/optique ?)"))
        if b.get("carrier") and not a.get("carrier"):
            F.append(_f(WARN, f"{n} : lien réseau perdu"))
        errs = sum(_inc(b, a, k) for k in ("rx_errors", "tx_errors", "rx_crc_errors"))
        if errs:
            F.append(_f(WARN, f"{n} : {errs} erreur(s) réseau apparue(s)", "rx/tx errors, CRC"))
    # PCI (clé : adresse bus) : disparition, lien, AER
    bp, ap = _by(before.get("pci", []), "bdf"), _by(after.get("pci", []), "bdf")
    for k in sorted(set(bp) - set(ap)):
        e = bp[k]
        F.append(_f(FAIL, f"Périphérique PCI disparu : {k} ({e.get('kind', '')} {e['vendor']}:{e['device']})"))
    for k in sorted(set(ap) - set(bp)):
        e = ap[k]
        F.append(_f(WARN, f"Périphérique PCI apparu : {k} ({e.get('kind', '')} {e['vendor']}:{e['device']})"))
    for k in sorted(set(ap) & set(bp)):
        b, a = bp[k], ap[k]
        kind = a.get("kind", "")
        for side, wk, sk in (("périphérique", "link_width", "link_speed"), ("port parent", "port_link_width", "port_link_speed")):
            if b.get(wk) and a.get(wk) and a[wk] < b[wk]:
                F.append(_f(FAIL, f"{k} ({kind}) : largeur de lien {side} réduite de x{b[wk]} à x{a[wk]}"))
            if kind != "gpu" and _speed_val(a.get(sk, "")) and _speed_val(b.get(sk, "")) > _speed_val(a.get(sk, "")):
                F.append(_f(WARN, f"{k} ({kind}) : vitesse de lien {side} réduite", f"{b[sk]} → {a[sk]}"))
        for pre, who in (("", k), ("port_", f"{k} (port {a.get('port', '?')})")):
            cor, nonf, fat = _inc(b, a, pre + "aer_cor"), _inc(b, a, pre + "aer_nonfatal"), _inc(b, a, pre + "aer_fatal")
            if fat or nonf:
                F.append(_f(FAIL, f"{who} : erreurs PCIe AER non corrigeables (+{fat + nonf})"))
            if cor:
                F.append(_f(WARN, f"{who} : erreurs PCIe AER corrigées +{cor}", "signal PCIe dégradé (câble, connecteur, riser ?)"))
    # BMC
    bb, ab = before.get("bmc", {}), after.get("bmc", {})
    sel_new: List[str] = []
    if bb.get("sel_count") is not None and ab.get("sel_count") is not None and ab["sel_count"] > bb["sel_count"]:
        k = ab["sel_count"] - bb["sel_count"]
        sel_new = ab.get("sel_last", [])[-min(k, 50):]
        F.append(_f(WARN, f"BMC : {k} nouvel(s) événement(s) SEL", " | ".join(sel_new[:3])))
    if bb.get("bmc_firmware") and ab.get("bmc_firmware") and bb["bmc_firmware"] != ab["bmc_firmware"]:
        F.append(_f(WARN, "Firmware BMC modifié", f"{bb['bmc_firmware']} → {ab['bmc_firmware']}"))
    bf, af = before.get("firmware", {}), after.get("firmware", {})
    if bf.get("bios_version") and af.get("bios_version") and bf["bios_version"] != af["bios_version"]:
        F.append(_f(WARN, "BIOS modifié", f"{bf['bios_version']} → {af['bios_version']}"))
    counts = {FAIL: 0, WARN: 0, INFO: 0}
    for f in F:
        counts[f["level"]] += 1
    return {"findings": F, "counts": counts, "identical": not F, "sel_new": sel_new,
            "before": summarize(before), "after": summarize(after),
            "taken_before": before.get("taken", ""), "taken_after": after.get("taken", "")}


def summarize(inv: dict) -> str:
    """Résumé d'une ligne d'un inventaire (affichage CLI / rapports)."""
    cpu, mem = inv.get("cpu", {}), inv.get("memory", {})
    nic = [n for n in inv.get("nics", []) if n.get("carrier")]
    n_gpu = len(inv.get("gpus", []))
    if not n_gpu:
        # Sans nvidia-smi (GPU Intel / AMD) : GPU vus sur le bus PCI, hors puces graphiques des BMC (ASPEED 1a03…)
        n_gpu = len([p for p in inv.get("pci", []) if p.get("kind") == "gpu" and p.get("vendor") not in ("1a03", "102b")])
    return (f"{cpu.get('threads', '?')} threads · {mem.get('total_gb', '?')} Go RAM · {n_gpu} GPU · "
            f"{len(inv.get('nvme', []))} NVMe · {len(inv.get('disks', []))} disque(s) · {len(nic)}/{len(inv.get('nics', []))} NIC en lien")


def format_findings(diff: dict) -> List[str]:
    """Lignes lisibles pour la console / les rapports."""
    sym = {FAIL: "✗", WARN: "⚠", INFO: "•"}
    return [f"{sym[f['level']]} {f['label']}" + (f" — {f['detail']}" if f.get("detail") else "") for f in diff.get("findings", [])]
