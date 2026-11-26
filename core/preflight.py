"""
preflight.py — Vérifications AVANT de lancer une campagne de tests.

Évite de perdre un test de 10 minutes sur une machine mal préparée : espace disque, GPU qui
répondent, tests déjà en cours, pilote incohérent, GPU chauds au départ, droits, dossier de
sortie. Chaque contrôle retourne des lignes {"level": "error|warn|info", "code", "msg"} :
  - error : le test ne doit pas démarrer ;
  - warn  : démarrage possible mais résultats à interpréter avec prudence ;
  - info  : simple information.
Lecture seule, avec timeouts, sans exception vers l'appelant.
"""

import os
import shutil
import subprocess
from typing import Dict, List, Optional
from core.safety import nvidia_smi_usable

BENCH_PROCESS_NAMES = ("stress-ng", "fio", "gpu_burn", "gpu-burn", "gpu-fryer", "nvbandwidth", "clpeak", "vkpeak")
GPU_COLD_MAX_C = 55.0     # au-delà, les GPU ne sont pas « froids » au départ du test
MIN_OUT_DIR_MB = 200


def _run(cmd: List[str], timeout: float = 8.0):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout.strip(), r.stderr.strip()
    except Exception as e:
        return -1, "", str(e)


def _item(level: str, code: str, msg: str) -> Dict[str, str]:
    return {"level": level, "code": code, "msg": msg}


def check_out_dir(path: str, need_mb: int = MIN_OUT_DIR_MB) -> List[Dict]:
    """Dossier de sortie : créable, inscriptible, assez d'espace libre."""
    out = []
    try:
        os.makedirs(path, exist_ok=True)
        if not os.access(path, os.W_OK):
            return [_item("error", "outdir_ro", f"Dossier de sortie non inscriptible : {path}")]
        free_mb = shutil.disk_usage(path).free // (1024 * 1024)
        if free_mb < need_mb:
            out.append(_item("error", "outdir_space", f"Espace libre insuffisant dans {path} : {free_mb} Mo (< {need_mb} Mo)"))
    except Exception as e:
        out.append(_item("error", "outdir", f"Dossier de sortie inutilisable ({path}) : {e}"))
    return out


def check_target_space(paths: List[str], need_mb: int) -> List[Dict]:
    """Espace libre sur les répertoires de test fichier (fio/dd)."""
    out = []
    for p in paths:
        if p.startswith("/dev/"):
            continue
        try:
            d = p if os.path.isdir(p) else os.path.dirname(p) or "."
            free_mb = shutil.disk_usage(d).free // (1024 * 1024)
            if free_mb < need_mb:
                out.append(_item("error", "target_space",
                                 f"Espace libre insuffisant sur {d} : {free_mb} Mo (< {need_mb} Mo nécessaires)"))
        except Exception as e:
            out.append(_item("warn", "target_space", f"Espace libre non vérifiable sur {p} : {e}"))
    return out


def check_running_benchmarks() -> List[Dict]:
    """Détecte des outils de bench déjà actifs (résultats faussés, charge cumulée)."""
    found = []
    if not shutil.which("pgrep"):
        return []
    for name in BENCH_PROCESS_NAMES:
        rc, out, _ = _run(["pgrep", "-x", name], 3)
        if rc == 0 and out:
            found.append(name)
    if found:
        return [_item("warn", "already_running", "Déjà en cours sur cette machine : " + ", ".join(found)
                      + " — les résultats seraient faussés.")]
    return []


def check_gpus(expected: Optional[int] = None) -> List[Dict]:
    """GPU NVIDIA : nvidia-smi répond, pilote cohérent, nombre attendu, GPU froids au départ."""
    out: List[Dict] = []
    if not nvidia_smi_usable():
        return out
    rc, txt, err = _run(["nvidia-smi", "--query-gpu=index,name,driver_version,temperature.gpu,pci.bus_id",
                         "--format=csv,noheader,nounits"], 12)
    if rc != 0:
        brut = ((err or "") + " " + (txt or "")).lower()
        if "version mismatch" in brut or "api mismatch" in brut:
            return [_item("error", "gpu_smi", "Pilote NVIDIA incohérent : le module noyau et les bibliothèques n'ont pas la même "
                          "version (mise à jour du pilote sans reconstruction du module). Corriger : "
                          "sudo dkms autoinstall && sudo reboot (ou réinstaller le pilote), puis vérifier avec nvidia-smi ; "
                          "cat /proc/driver/nvidia/version donne la version du module chargé.")]
        return [_item("error", "gpu_smi", "nvidia-smi ne répond pas (pilote absent, GPU tombé du bus ?) : "
                      + (err or "erreur inconnue")[:160])]
    rows = [[x.strip() for x in l.split(",")] for l in txt.splitlines() if l.strip()]
    if expected is not None and len(rows) != expected:
        out.append(_item("error", "gpu_count", f"{len(rows)} GPU détecté(s) au lieu de {expected} attendu(s)."))
    drivers = {r[2] for r in rows if len(r) > 2}
    if len(drivers) > 1:
        out.append(_item("warn", "gpu_driver", "Versions de pilote différentes : " + ", ".join(sorted(drivers))))
    # GPU visibles sur le bus PCI mais absents de nvidia-smi
    if shutil.which("lspci"):
        rc2, l, _ = _run(["lspci", "-d", "10de:"], 6)
        if rc2 == 0:
            pci_gpus = [x for x in l.splitlines() if any(k in x for k in ("VGA", "3D controller", "Display"))]
            if len(pci_gpus) > len(rows):
                out.append(_item("warn", "gpu_missing",
                                 f"{len(pci_gpus)} GPU NVIDIA sur le bus PCI mais {len(rows)} vus par nvidia-smi."))
    hot = []
    for r in rows:
        try:
            if len(r) > 3 and float(r[3]) > GPU_COLD_MAX_C:
                hot.append(f"GPU {r[0]} ({r[3]} °C)")
        except ValueError:
            pass
    if hot:
        out.append(_item("warn", "gpu_hot", "GPU pas froids au départ (> %d °C) : %s — comparabilité réduite."
                         % (GPU_COLD_MAX_C, ", ".join(hot))))
    return out


def check_privileges() -> List[Dict]:
    """Informe des fonctions limitées sans privilèges (dmesg, SMART, BMC)."""
    if os.geteuid() == 0:
        return []
    if shutil.which("sudo"):
        rc, _, _ = _run(["sudo", "-n", "true"], 4)
        if rc == 0:
            return []
    return [_item("info", "no_root", "Droits limités (ni root ni sudo sans mot de passe) : dmesg, SMART et BMC "
                  "peuvent être incomplets. Lancez « sudo -v » avant BenchSuite pour les activer.")]


def run_preflight(out_dir: Optional[str] = None, target_paths: Optional[List[str]] = None,
                  target_need_mb: int = 0, expected_gpus: Optional[int] = None,
                  gpus: bool = True) -> List[Dict]:
    """Exécute tous les contrôles pertinents et retourne la liste des constats."""
    res: List[Dict] = []
    if out_dir:
        res += check_out_dir(out_dir)
    if target_paths and target_need_mb:
        res += check_target_space(target_paths, target_need_mb)
    res += check_running_benchmarks()
    if gpus:
        res += check_gpus(expected_gpus)
    res += check_privileges()
    return res


def has_blocking(items: List[Dict]) -> bool:
    return any(i["level"] == "error" for i in items)


def format_items(items: List[Dict]) -> str:
    sym = {"error": "✗", "warn": "⚠", "info": "ℹ"}
    return "\n".join(f"  {sym.get(i['level'], '-')} {i['msg']}" for i in items)
