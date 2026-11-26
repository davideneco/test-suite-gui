"""
safety.py — Protection du disque système et contrôle des cibles d'écriture.

Règle (AGENTS.md §5.3) : ne jamais lancer de test d'écriture destructif sur un disque
qui contient le système d'exploitation. Les fonctions ci-dessous résolvent le(s) disque(s)
physique(s) derrière un chemin ou un périphérique (partitions, LVM/dm, RAID md inclus) et
décident si l'écriture est autorisée. Tout est best-effort, sans privilège, sans crash.
"""

import os
import subprocess
from typing import Dict, List, Optional, Set, Tuple

def system_changes_allowed() -> bool:
    """True sauf si l'utilisateur a demandé le mode lecture seule (BENCHSUITE_READ_ONLY=1 ou --read-only).

    Règle : BenchSuite ne fait JAMAIS d'installation ni de désinstallation (paquets, pilotes, toolkits). En revanche il
    peut lancer ce dont les tests ont besoin : charger un module (IPMI), démarrer le service DCGM, activer le mode
    persistance GPU, appliquer un réglage demandé par l'utilisateur. Le mode lecture seule coupe aussi ces actions.
    """
    if os.environ.get("BENCHSUITE_READ_ONLY", "").strip().lower() in ("1", "true", "oui", "yes"):
        return False
    return os.environ.get("BENCHSUITE_ALLOW_SYSTEM_CHANGES", "").strip().lower() not in ("0", "false", "non", "no")


_NVIDIA_PRESENT: Optional[bool] = None


def nvidia_gpu_present() -> bool:
    """True si un GPU NVIDIA (vendeur PCI 0x10de, classe affichage/3D) est présent sur le bus PCI.

    Lecture sysfs seule, mise en cache. Sert à ne JAMAIS lancer `nvidia-smi` sur une machine sans GPU NVIDIA : l'outil
    charge le module noyau `nvidia` (journal noyau : « NVRM: No NVIDIA GPU found », nvlink initialisé puis retiré) à
    chaque appel. Si le bus PCI est illisible (conteneur…), on répond True pour ne rien masquer à tort.
    """
    global _NVIDIA_PRESENT
    if _NVIDIA_PRESENT is not None:
        return _NVIDIA_PRESENT
    root = "/sys/bus/pci/devices"
    try:
        devs = os.listdir(root)
    except Exception:
        devs = []
    if not devs:
        _NVIDIA_PRESENT = True
        return True
    found = False
    for d in devs:
        try:
            with open(os.path.join(root, d, "vendor")) as f:
                if f.read().strip().lower() != "0x10de":
                    continue
            with open(os.path.join(root, d, "class")) as f:
                if f.read().strip().startswith(("0x03", "0x12")):
                    found = True
                    break
        except Exception:
            continue
    _NVIDIA_PRESENT = found
    return found


def nvidia_smi_usable() -> bool:
    """True si `nvidia-smi` existe ET qu'un GPU NVIDIA est présent (sinon on évite de charger le pilote pour rien)."""
    import shutil
    return bool(shutil.which("nvidia-smi")) and nvidia_gpu_present()


MSG_PRESERVATION = ("Mode lecture seule : BenchSuite ne modifie pas la machine (réglages GPU/CPU, services, modules). "
                    "Retirer BENCHSUITE_READ_ONLY / --read-only pour l'autoriser.")

# Points de montage qui définissent le « disque OS »
_OS_MOUNTPOINTS = ("/", "/boot", "/boot/efi", "/usr", "/var", "/home", "/opt", "/srv")
# Modes fio qui ne modifient pas les données
_READ_ONLY_MODES = {"read", "randread"}


def _sysfs_block(name: str) -> str:
    return os.path.join("/sys/class/block", name)


def whole_disks_of(blockname: str, _seen: Optional[Set[str]] = None) -> Set[str]:
    """Retourne les disques physiques (noms de base) derrière un périphérique bloc.

    Partition → disque parent ; dm/md/LVM → leurs `slaves` (récursif) ; sinon le disque lui-même.
    """
    seen = _seen if _seen is not None else set()
    name = os.path.basename(blockname)
    if not name or name in seen:
        return set()
    seen.add(name)
    base = _sysfs_block(name)
    try:
        real = os.path.realpath(base)
        if os.path.exists(os.path.join(real, "partition")):
            parent = os.path.basename(os.path.dirname(real))
            return whole_disks_of(parent, seen) or {parent}
        slaves_dir = os.path.join(real, "slaves")
        if os.path.isdir(slaves_dir):
            slaves = os.listdir(slaves_dir)
            if slaves:
                out: Set[str] = set()
                for s in slaves:
                    out |= whole_disks_of(s, seen)
                return out or {name}
    except Exception:
        pass
    return {name}


def _source_device(path: str) -> str:
    """Périphérique qui porte un chemin (findmnt, repli sur st_dev)."""
    try:
        r = subprocess.run(["findmnt", "-no", "SOURCE", "-T", path],
                           capture_output=True, text=True, timeout=5)
        src = (r.stdout or "").strip().splitlines()
        if r.returncode == 0 and src and src[0].startswith("/dev/"):
            return os.path.realpath(src[0].split("[")[0])
    except Exception:
        pass
    try:
        st = os.stat(path)
        real = os.path.realpath(f"/sys/dev/block/{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        return "/dev/" + os.path.basename(real)
    except Exception:
        return ""


def os_disks() -> Set[str]:
    """Disques physiques qui contiennent /, /boot, /usr, /var, /home… et le swap."""
    disks: Set[str] = set()
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/") and parts[1] in _OS_MOUNTPOINTS:
                    disks |= whole_disks_of(os.path.realpath(parts[0]))
    except Exception:
        pass
    try:
        with open("/proc/swaps") as f:
            for line in list(f)[1:]:
                dev = line.split()[0]
                if dev.startswith("/dev/"):
                    disks |= whole_disks_of(os.path.realpath(dev))
    except Exception:
        pass
    if not disks:
        src = _source_device("/")
        if src:
            disks |= whole_disks_of(src)
    return disks


def mounted_partitions_of(disk: str) -> List[str]:
    """Points de montage des partitions d'un disque physique (hors swap)."""
    out: List[str] = []
    try:
        with open("/proc/mounts") as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 2 and parts[0].startswith("/dev/"):
                    if disk in whole_disks_of(os.path.realpath(parts[0])):
                        out.append(parts[1])
    except Exception:
        pass
    return out


def disks_for_target(path: str) -> Set[str]:
    """Disques physiques visés par un chemin (fichier, répertoire ou /dev/…)."""
    if path.startswith("/dev/"):
        return whole_disks_of(os.path.realpath(path))
    probe = path
    while probe and not os.path.exists(probe):
        probe = os.path.dirname(probe)
    src = _source_device(probe or "/")
    return whole_disks_of(src) if src else set()


def is_write_mode(rw_mode: str) -> bool:
    """Vrai si le mode fio modifie les données (tout sauf read/randread)."""
    return str(rw_mode or "").strip().lower() not in _READ_ONLY_MODES


def check_write_target(path: str, raw_device: bool = False, writes: bool = True,
                       allow_mounted: Optional[bool] = None) -> Tuple[str, str]:
    """Évalue une cible de test disque. Retourne (niveau, message).

    niveau : "ok" | "warn" | "block".
      - périphérique brut + écriture sur le disque système      → block (jamais contournable)
      - périphérique brut + écriture sur un disque avec montages → block (sauf BENCHSUITE_ALLOW_MOUNTED=1)
      - répertoire non accessible en écriture                    → block
      - fichier de test sur le disque système                    → warn (pas destructif)
    """
    if allow_mounted is None:
        allow_mounted = os.environ.get("BENCHSUITE_ALLOW_MOUNTED") == "1"
    is_raw = raw_device or path.startswith("/dev/")
    try:
        if is_raw:
            if not os.path.exists(path):
                return "block", f"Périphérique introuvable : {path}"
            if not writes:
                return "ok", ""
            target = disks_for_target(path)
            os_set = os_disks()
            clash = sorted(target & os_set)
            if clash:
                return "block", (f"REFUSÉ : {path} est le disque système ({', '.join(clash)}). "
                                 "Aucun test d'écriture brute n'est autorisé sur le disque de l'OS.")
            for d in sorted(target):
                mps = mounted_partitions_of(d)
                if mps and not allow_mounted:
                    return "block", (f"REFUSÉ : {path} contient des partitions montées ({', '.join(mps)}). "
                                     "Démontez-les avant un test d'écriture brute (sinon perte de données).")
            return "ok", ""
        # Cible fichier : répertoire
        if writes:
            d = path if os.path.isdir(path) else os.path.dirname(path) or "."
            if not os.path.isdir(d):
                return "block", f"Répertoire introuvable : {d}"
            if not os.access(d, os.W_OK):
                return "block", f"Répertoire non accessible en écriture : {d}"
            if disks_for_target(d) & os_disks():
                return "warn", (f"Le fichier de test sera créé sur le disque système ({d}) : "
                                "résultats non représentatifs d'un disque de données.")
    except Exception as e:  # best-effort : ne jamais planter
        return "warn", f"Vérification de la cible impossible ({e})"
    return "ok", ""


def check_targets(paths: List[str], raw_device: bool = False, writes: bool = True) -> Tuple[str, str]:
    """Évalue plusieurs cibles ; retourne le pire niveau et les messages concaténés."""
    worst, msgs = "ok", []
    rank: Dict[str, int] = {"ok": 0, "warn": 1, "block": 2}
    for p in paths:
        lvl, msg = check_write_target(p.strip(), raw_device=raw_device, writes=writes)
        if msg:
            msgs.append(msg)
        if rank[lvl] > rank[worst]:
            worst = lvl
    return worst, "\n".join(msgs)
