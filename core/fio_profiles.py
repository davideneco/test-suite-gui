"""
fio_profiles.py — Profils FIO avec paramètres corrects pour mesurer le vrai débit NVMe
Logique CrystalDiskMark : bs=1M, iodepth=8, numjobs=1 pour séquentiel crête
"""

import json, os, re
from copy import deepcopy
from typing import Optional

PROFILES_FILE = os.path.expanduser("~/.benchsuite_fio_profiles.json")

# ─────────────────────────────────────────────────────────────────────────────
# Détection du meilleur io_engine disponible
# ─────────────────────────────────────────────────────────────────────────────

_BEST_ENGINE: list = []          # mémorisé : le moteur disponible ne change pas pendant l'exécution


def best_engine() -> str:
    """Retourne le moteur I/O le plus performant disponible sur le kernel actuel (résultat mémorisé : un seul `fio --enghelp`)."""
    if _BEST_ENGINE:
        return _BEST_ENGINE[0]
    _BEST_ENGINE.append(_best_engine_probe())
    return _BEST_ENGINE[0]


def _best_engine_probe() -> str:
    import subprocess
    # io_uring dispo depuis kernel 5.1
    try:
        r = subprocess.run(["fio", "--enghelp"], capture_output=True, text=True, timeout=5)
        engines = (r.stdout + r.stderr).lower()
        if "io_uring" in engines:
            return "io_uring"
        if "libaio" in engines:
            return "libaio"
    except Exception:
        pass
    return "sync"


# ─────────────────────────────────────────────────────────────────────────────
# Détection des disques — montés ET non montés, USB, /media/, /dev/sdX brut
# ─────────────────────────────────────────────────────────────────────────────

_DISKS_CACHE: dict = {"t": 0.0, "v": None}


def get_mounted_disks(max_age_s: float = 0.0) -> list:
    """Disques détectés. `max_age_s` > 0 : réutilise un relevé de moins de `max_age_s` secondes (au démarrage, les deux pages fio
    et la page Séquence demandaient chacune leur `lsblk`) ; 0 (défaut) : relevé frais."""
    import time as _t
    if max_age_s > 0 and _DISKS_CACHE["v"] is not None and _t.time() - _DISKS_CACHE["t"] < max_age_s:
        return [dict(d) for d in _DISKS_CACHE["v"]]
    disks = _get_mounted_disks_now()
    _DISKS_CACHE["t"], _DISKS_CACHE["v"] = _t.time(), [dict(d) for d in disks]
    return disks


def _get_mounted_disks_now() -> list:
    """
    Retourne les disques de stockage détectés (NVMe, SATA, SAS, etc. montés ou non).
    Conçu pour les tests de disques.
    Exclut les disques système (/, /boot, swap).
    Toujours /tmp en fallback si aucun disque trouvé.
    Format : [{"path": str, "label": str, "type": str, "model": str, "dev": str, "mounted": bool}]
    """
    import subprocess, json as _json

    # Points de montage système à exclure (disque OS)
    SYSTEM_MOUNTPOINTS = {"/", "/boot", "/boot/efi", "/boot/grub",
                          "/home", "/var", "/usr", "/opt", "/srv", "/root"}
    EXCLUDE_PREFIXES   = ("/proc", "/sys", "/dev", "/run", "/snap", "/tmp",
                          "/var/", "/boot/", "/usr/", "/opt/", "/srv/", "/root/")

    def is_system_mount(mp: str) -> bool:
        if mp in SYSTEM_MOUNTPOINTS:
            return True
        for p in EXCLUDE_PREFIXES:
            if mp.startswith(p):
                return True
        return False

    storage_disks = []
    seen_devs  = set()

    try:
        r = subprocess.run(
            ["lsblk", "-J", "-o",
             "NAME,SIZE,TYPE,MOUNTPOINT,MOUNTPOINTS,MODEL,ROTA,TRAN,FSTYPE,LABEL,PATH,SERIAL"],
            capture_output=True, text=True, timeout=8)

        if r.returncode != 0:
            raise RuntimeError("lsblk failed")

        data = _json.loads(r.stdout)

        def get_all_usable_mps(d) -> list:
            res = []
            raw_mp  = d.get("mountpoint") or ""
            raw_mps = d.get("mountpoints") or []
            if isinstance(raw_mps, str):
                raw_mps = [raw_mps]
            for m in ([raw_mp] + raw_mps):
                if m and m.strip() and m.strip() != "[SWAP]" and not is_system_mount(m.strip()):
                    res.append(m.strip())
            for child in d.get("children", []):
                res.extend(get_all_usable_mps(child))
            return list(dict.fromkeys(res))

        def has_system_mount(d) -> bool:
            raw_mp  = d.get("mountpoint") or ""
            raw_mps = d.get("mountpoints") or []
            if isinstance(raw_mps, str):
                raw_mps = [raw_mps]
            for m in ([raw_mp] + raw_mps):
                if m and m.strip() and is_system_mount(m.strip()):
                    return True
            for child in d.get("children", []):
                if has_system_mount(child):
                    return True
            return False

        def walk(devices, parent_tran="", parent_model="", parent_serial=""):
            for dev in devices:
                name    = dev.get("name", "")
                dtype   = dev.get("type", "")
                size    = (dev.get("size")   or "?").strip()
                model   = (dev.get("model")  or parent_model  or "").strip()
                serial  = (dev.get("serial") or parent_serial or "").strip()
                tran    = (dev.get("tran")   or parent_tran   or "").strip().lower()
                fstype  = (dev.get("fstype") or "").strip()
                label   = (dev.get("label")  or "").strip()
                devpath = (dev.get("path")   or f"/dev/{name}").strip()


                # Ignorer loop, rom, swap
                if dtype in ("loop", "rom") or not name:
                    continue
                if fstype == "swap":
                    continue

                if devpath in seen_devs:
                    continue
                seen_devs.add(devpath)

                # Points de montage sur le disque et toutes ses partitions (children)
                usable_mps = get_all_usable_mps(dev)
                is_mounted = bool(usable_mps)
                sys_mounted = has_system_mount(dev)

                # Construire un label court et propre pour ne pas déformer le menu GTK4
                disk_type = tran.upper() if tran else "HDD/SSD"
                short_model = model.split()[0] if model else disk_type

                if is_mounted:
                    usable_mps.sort(key=len)
                    mp = usable_mps[0]
                    if any(d.get("path") == mp for d in storage_disks if d.get("mounted")):
                        continue
                    more = f" (+{len(usable_mps)-1})" if len(usable_mps) > 1 else ""
                    entry_label = f"📁 {mp}{more} ({size} — {short_model})"
                    storage_disks.append({
                        "path":    mp,
                        "all_mps": usable_mps,
                        "label":   entry_label,
                        "type":    disk_type,
                        "model":   model,
                        "size":    size,
                        "dev":     devpath,
                        "mounted": True,
                        "tran":    tran,
                        "serial":  serial,
                    })
                elif not sys_mounted:
                    status = "brut" if not fstype else "non monté"
                    entry_label = f"🖲️ {devpath} ({size} — {short_model} {status})"
                    storage_disks.append({
                        "path":    devpath,
                        "label":   entry_label,
                        "type":    disk_type,
                        "model":   model,
                        "size":    size,
                        "dev":     devpath,
                        "mounted": False,
                        "tran":    tran,
                        "serial":  serial,
                        "virgin":  not bool(fstype),
                        "warning": (
                            "Disque vierge — fio écrira directement, aucune donnée existante ne sera effacée."
                            if not fstype else
                            "Disque formaté non monté — fio écrira directement sur le périphérique."
                        ),
                    })

                for child in dev.get("children", []):
                    walk([child], tran, model, serial)

        walk(data.get("blockdevices", []))

    except Exception:
        pass

    # Trier : non montés (vierges) en premier, puis par nom de device
    storage_disks.sort(key=lambda d: (d.get("mounted", True), d["dev"]))

    # Fallback /tmp si aucun disque détecté
    if not storage_disks:
        storage_disks.append({
            "path":    "/tmp",
            "label":   "/tmp  [aucun stockage detecte — utilise /tmp]",
            "type":    "?",
            "model":   "",
            "size":    "?",
            "dev":     "",
            "mounted": True,
            "tran":    "",
        })

    return storage_disks


# ─────────────────────────────────────────────────────────────────────────────
# Profils prédéfinis
# ─────────────────────────────────────────────────────────────────────────────
# NOTE sur le moteur :
#   "auto" → best_engine() sera appelé au moment du lancement
#   Les autres valeurs sont forcées (sync, libaio, io_uring…)
# ─────────────────────────────────────────────────────────────────────────────

# Sous-tests internes utilisés par la Suite Complète (non affichés isolément dans le menu)
_INTERNAL_SUITE_SUBTESTS = {
    "peak_seq1m_q8t1_read": {
        "id": "peak_seq1m_q8t1_read",
        "name": "Seq1M Q8T1 Lecture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: lecture séquentielle 1M, queue depth 8, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 8, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_seq1m_q8t1_write": {
        "id": "peak_seq1m_q8t1_write",
        "name": "Seq1M Q8T1 Écriture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: écriture séquentielle 1M, queue depth 8, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "write", "bs": "1m", "iodepth": 8, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_seq128k_q32t1_read": {
        "id": "peak_seq128k_q32t1_read",
        "name": "Seq128K Q32T1 Lecture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: lecture séquentielle 128K, queue depth 32, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "read", "bs": "128k", "iodepth": 32, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_seq128k_q32t1_write": {
        "id": "peak_seq128k_q32t1_write",
        "name": "Seq128K Q32T1 Écriture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: écriture séquentielle 128K, queue depth 32, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "write", "bs": "128k", "iodepth": 32, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_rnd4k_q32t16_read": {
        "id": "peak_rnd4k_q32t16_read",
        "name": "Rnd4K Q32T16 IOPS Lecture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: IOPS lecture aléatoire 4K, queue depth 32, 16 jobs.",
        "icon": "⚡",
        "params": {"rw_mode": "randread", "bs": "4k", "iodepth": 32, "numjobs": 16, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_rnd4k_q32t16_write": {
        "id": "peak_rnd4k_q32t16_write",
        "name": "Rnd4K Q32T16 IOPS Écriture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: IOPS écriture aléatoire 4K, queue depth 32, 16 jobs.",
        "icon": "⚡",
        "params": {"rw_mode": "randwrite", "bs": "4k", "iodepth": 32, "numjobs": 16, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_rnd4k_q32t1_read": {
        "id": "peak_rnd4k_q32t1_read",
        "name": "Rnd4K Q32T1 Lecture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: lecture aléatoire 4K, queue depth 32, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "randread", "bs": "4k", "iodepth": 32, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    "peak_rnd4k_q32t1_write": {
        "id": "peak_rnd4k_q32t1_write",
        "name": "Rnd4K Q32T1 Écriture",
        "category": "Peak Performance",
        "description": "CDM Peak Performance: écriture aléatoire 4K, queue depth 32, 1 job.",
        "icon": "⚡",
        "params": {"rw_mode": "randwrite", "bs": "4k", "iodepth": 32, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
}

BUILTIN_PROFILES = [
    {
        "id": "peak_suite_all",
        "name": "🏆 Suite Complète — Tous les tests Peak",
        "category": "Peak Performance",
        "description": "Exécute consécutivement tous les benchmarks Peak (Seq 1M Lecture/Écriture, Seq 128K Lecture/Écriture, Rnd 4K IOPS Lecture/Écriture) pour un bilan global complet.",
        "icon": "🏆",
        "builtin": True,
        "is_suite": True,
        "suite_tests": [
            "peak_seq1m_q8t1_read",
            "peak_seq1m_q8t1_write",
            "peak_seq128k_q32t1_read",
            "peak_seq128k_q32t1_write",
            "peak_rnd4k_q32t16_read",
            "peak_rnd4k_q32t16_write",
            "peak_rnd4k_q32t1_read",
            "peak_rnd4k_q32t1_write"
        ],
        "params": {"rw_mode": "readwrite", "bs": "1m", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 120},
    },
    {
        "id": "stress_nvme_1h",
        "name": "⏳ Stress Endurance NVMe — 1 Heure",
        "category": "Stress et Endurance",
        "description": "Test de stress et de stabilité thermique sur 1 heure (3600s). Charge mixte 70% lecture / 30% écriture aléatoire 4K Q32T4.",
        "icon": "⏳",
        "builtin": True,
        "params": {"rw_mode": "randrw", "bs": "4k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 4096, "runtime_s": 3600, "rwmixread": 70},
    },
    {
        "id": "stress_nvme_4h",
        "name": "🔥 Stress Endurance NVMe — 4 Heures",
        "category": "Stress et Endurance",
        "description": "Test d'endurance intensive sur 4 heures (14400s). Valide la tenue du contrôleur sous charge continue en serveur.",
        "icon": "🔥",
        "builtin": True,
        "params": {"rw_mode": "randrw", "bs": "4k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 4096, "runtime_s": 14400, "rwmixread": 70},
    },
    {
        "id": "conditioning_nvme_quick",
        "name": "🧹 Pré-conditionnement Rapide — 15 min",
        "category": "Conditionnement SSD",
        "description": "Purge rapide et saturation SLC avant benchmark (900s): écriture séquentielle 128K Q32T4.",
        "icon": "🧹",
        "builtin": True,
        "params": {"rw_mode": "write", "bs": "128k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 16384, "runtime_s": 900},
    },
    {
        "id": "conditioning_nvme_full",
        "name": "🏢 Pré-conditionnement SNIA Intégral (2x Capacité)",
        "category": "Conditionnement SSD",
        "description": "Norme officielle SNIA SSS PTS Enterprise: écriture séquentielle 128K Q32T4 sur 100% de la surface physique du disque en 2 passes complètes (loops=2). Indispensable pour atteindre l'état stable FTL d'entreprise.",
        "icon": "🏢",
        "builtin": True,
        "params": {"rw_mode": "write", "bs": "128k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 0, "loops": 2},
    },
    {
        "id": "hdd_burn_in_2h",
        "name": "⏳ Burn-in HDD Rapide — 2 Heures",
        "category": "Stress et Endurance",
        "description": "Test de burn-in préliminaire pour disques durs (7200s): écriture séquentielle 1M, iodepth 16, moteur libaio. Idéal pour une recette rapide de disque.",
        "icon": "⏳",
        "builtin": True,
        "params": {"rw_mode": "write", "bs": "1m", "iodepth": 16, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 0, "runtime_s": 7200},
    },
    {
        "id": "hdd_burn_in_24h",
        "name": "🔥 Burn-in HDD — 24 Heures (Stress Mécanique)",
        "category": "Stress et Endurance",
        "description": "Test de burn-in destructif pour disques durs mécaniques: écriture séquentielle 1M, iodepth 32, moteur libaio. Conçu pour déceler les défauts initiaux.",
        "icon": "🔥",
        "builtin": True,
        "params": {"rw_mode": "write", "bs": "1m", "iodepth": 32, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 0, "runtime_s": 86400},
    },
    {
        "id": "hdd_perf_standard",
        "name": "📊 Benchmark HDD — Lecture Séquentielle 1M",
        "category": "Peak Performance",
        "description": "Test de performance optimal pour disques durs: lecture séquentielle avec bloc 1 Mo, queue depth 4, 1 job. Représentatif de gros transferts de fichiers.",
        "icon": "📊",
        "builtin": True,
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 4096, "runtime_s": 60},
    },
    {
        "id": "hdd_perf_write_standard",
        "name": "📊 Benchmark HDD — Écriture Séquentielle 1M",
        "category": "Peak Performance",
        "description": "Mesure du débit séquentiel continu en écriture pure: bloc 1 Mo, queue depth 4, 1 job. Évalue la vitesse d'enregistrement plateau.",
        "icon": "📊",
        "builtin": True,
        "params": {"rw_mode": "write", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 4096, "runtime_s": 60},
    },
    {
        "id": "hdd_perf_mixed",
        "name": "🔄 Benchmark HDD — Mixte Séquentiel (70% R / 30% W)",
        "category": "Peak Performance",
        "description": "Charge mixte équilibrée sur disque mécanique (70% lecture, 30% écriture): bloc 1 Mo, queue depth 4, 1 job. Simule un serveur de fichiers ou NAS sous charge.",
        "icon": "🔄",
        "builtin": True,
        "params": {"rw_mode": "rw", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 4096, "runtime_s": 60, "rwmixread": 70},
    },
]

# ─────────────────────────────────────────────────────────────────────────────
# Profils « pro » : standards industriels et charges de référence publiques
# Sources : fio examples/jesd219.fio (JEDEC JESD219), SNIA SSS PTS Enterprise, CrystalDiskMark 8,
# profils Intel IOMeter (File/Web/Workstation/Database), test fdatasync d'etcd.
# ─────────────────────────────────────────────────────────────────────────────

_JESD219_BSSPLIT = "512/4:1024/1:1536/1:2048/1:2560/1:3072/1:3584/1:4k/67:8k/10:16k/7:32k/3:64k/3"
_JESD219_ZONES = "zoned:50/5:30/15:20/80"

_PRO_STD = "Pro — Standards JEDEC / SNIA"
_PRO_APP = "Pro — Charges applicatives"
_PRO_HDD = "Pro — Disques durs (HDD)"
_PRO_CDM = "Pro — CrystalDiskMark"


def _snia_sub(prefix: str, bs: str, mix: int, qd: int, jobs: int, label: str) -> dict:
    """Sous-test SNIA : 1 minute, accès aléatoire, mélange lecture/écriture donné (100 = lecture seule)."""
    rw = "randread" if mix == 100 else "randwrite" if mix == 0 else "randrw"
    pid = f"{prefix}_{bs}_r{mix}"
    nice = {"1m": "1M", "512": "0,5K"}.get(bs, bs.upper())
    prm = {"rw_mode": rw, "bs": bs, "iodepth": qd, "numjobs": jobs, "io_engine": "auto", "direct": True,
           "size_mb": 0, "runtime_s": 60}
    if rw == "randrw":
        prm["rwmixread"] = mix
    return {"id": pid, "name": f"{label} {nice} {mix}/{100 - mix}", "category": _PRO_STD,
            "description": f"{label} : bloc {nice}, {mix} % lecture / {100 - mix} % écriture, QD{qd}×{jobs}, 60 s.",
            "icon": "📐", "params": prm}


_SNIA_BS = ["1m", "128k", "64k", "32k", "16k", "8k", "4k", "512"]
_SNIA_MIX = [100, 95, 65, 50, 35, 5, 0]
for _bs in _SNIA_BS:
    for _m in _SNIA_MIX:
        _t = _snia_sub("snia_iops", _bs, _m, 32, 4, "SNIA IOPS")
        _INTERNAL_SUITE_SUBTESTS[_t["id"]] = _t
for _bs in ("8k", "4k", "512"):
    for _m in (100, 65, 0):
        _t = _snia_sub("snia_lat", _bs, _m, 1, 1, "SNIA Latence")
        _INTERNAL_SUITE_SUBTESTS[_t["id"]] = _t
for _bs, _rw, _lbl in (("1m", "read", "Lecture"), ("1m", "write", "Écriture"), ("128k", "read", "Lecture"), ("128k", "write", "Écriture")):
    _INTERNAL_SUITE_SUBTESTS[f"snia_tp_{_bs}_{_rw}"] = {
        "id": f"snia_tp_{_bs}_{_rw}", "name": f"SNIA Débit séquentiel {_bs.upper()} {_lbl}", "category": _PRO_STD,
        "description": f"SNIA débit : {_lbl.lower()} séquentielle {_bs.upper()}, QD32×2, 60 s.", "icon": "📐",
        "params": {"rw_mode": _rw, "bs": _bs, "iodepth": 32, "numjobs": 2, "io_engine": "auto", "direct": True,
                   "size_mb": 0, "runtime_s": 60}}
for _id, _bs, _qd, _rw, _lbl in (("cdm_seq1m_q1t1_read", "1m", 1, "read", "Lecture"), ("cdm_seq1m_q1t1_write", "1m", 1, "write", "Écriture"),
                                  ("cdm_rnd4k_q1t1_read", "4k", 1, "randread", "Lecture"), ("cdm_rnd4k_q1t1_write", "4k", 1, "randwrite", "Écriture")):
    _INTERNAL_SUITE_SUBTESTS[_id] = {
        "id": _id, "name": f"{'Seq1M' if _bs == '1m' else 'Rnd4K'} Q1T1 {_lbl}", "category": _PRO_CDM,
        "description": f"CrystalDiskMark : {_lbl.lower()} {_bs.upper()} Q1T1.", "icon": "💿",
        "params": {"rw_mode": _rw, "bs": _bs, "iodepth": _qd, "numjobs": 1, "io_engine": "auto", "direct": True,
                   "size_mb": 1024, "runtime_s": 30}}

_PRO_PROFILES = [
    {
        "id": "jesd219_enterprise_1h", "name": "JEDEC JESD219 — Charge entreprise (1 h)", "category": _PRO_STD,
        "description": ("Charge d'endurance JEDEC JESD219 (fio examples/jesd219.fio) : 100 % aléatoire, 40 % lecture / 60 % écriture, "
                        "mélange de blocs 512 o à 64 Ko (4 Ko = 67 %), zones chaudes (50 % des accès sur 5 % du disque), QD256×4. "
                        "ÉCRIT sur le disque : disque de test dédié, jamais le disque système."),
        "icon": "🏭", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 40, "bs": "4k", "bssplit": _JESD219_BSSPLIT, "random_distribution": _JESD219_ZONES,
                   "blockalign": "4k", "iodepth": 256, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 3600},
    },
    {
        "id": "jesd219_enterprise_8h", "name": "JEDEC JESD219 — Charge entreprise (8 h)", "category": _PRO_STD,
        "description": "Même charge JESD219 que le profil 1 h, sur 8 heures : montre la dérive de performance du SSD (état stable, GC).",
        "icon": "🏭", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 40, "bs": "4k", "bssplit": _JESD219_BSSPLIT, "random_distribution": _JESD219_ZONES,
                   "blockalign": "4k", "iodepth": 256, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 28800},
    },
    {
        "id": "snia_pts_4k_steady_30m", "name": "SNIA PTS — 4K aléatoire écriture, état stable (30 min)", "category": _PRO_STD,
        "description": ("Point de mesure de référence SNIA SSS PTS (IOPS 4 Ko, 100 % écriture aléatoire). À lancer APRÈS le "
                        "pré-conditionnement SNIA intégral ; l'état stable se lit sur la courbe (débit constant en fin de test)."),
        "icon": "📐", "builtin": True,
        "params": {"rw_mode": "randwrite", "bs": "4k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 1800},
    },
    {
        "id": "snia_pts_iops_matrix", "name": "SNIA PTS — Matrice IOPS (56 points × 1 min)", "category": _PRO_STD,
        "description": ("Matrice IOPS de la spécification SNIA SSS PTS : 8 tailles de bloc (0,5K à 1M) × 7 mélanges lecture/écriture "
                        "(100/0 … 0/100), 1 minute chacun (~1 h). Lancer après le pré-conditionnement SNIA. "
                        "Simplifié : pas de détection automatique d'état stable."),
        "icon": "📐", "builtin": True, "is_suite": True,
        "suite_tests": [f"snia_iops_{b}_r{m}" for b in _SNIA_BS for m in _SNIA_MIX],
        "params": {"rw_mode": "randrw", "bs": "4k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 60},
    },
    {
        "id": "snia_pts_latency", "name": "SNIA PTS — Latence (9 points × 1 min)", "category": _PRO_STD,
        "description": "Latence SNIA : blocs 8K / 4K / 0,5K × lecture seule, 65/35, écriture seule, QD1 × 1 job. Lire les percentiles de latence dans les résultats.",
        "icon": "📐", "builtin": True, "is_suite": True,
        "suite_tests": [f"snia_lat_{b}_r{m}" for b in ("8k", "4k", "512") for m in (100, 65, 0)],
        "params": {"rw_mode": "randrw", "bs": "4k", "iodepth": 1, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 60},
    },
    {
        "id": "snia_pts_throughput", "name": "SNIA PTS — Débit séquentiel (1M / 128K, R et W)", "category": _PRO_STD,
        "description": "Débit SNIA : séquentiel 1 Mo et 128 Ko, lecture et écriture pure, 1 minute chacun.",
        "icon": "📐", "builtin": True, "is_suite": True,
        "suite_tests": ["snia_tp_1m_read", "snia_tp_1m_write", "snia_tp_128k_read", "snia_tp_128k_write"],
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 32, "numjobs": 2, "io_engine": "auto", "direct": True, "size_mb": 0, "runtime_s": 60},
    },
    {
        "id": "cdm8_default_suite", "name": "CrystalDiskMark 8 — Profil par défaut", "category": _PRO_CDM,
        "description": "Les 4 mesures par défaut de CrystalDiskMark 8 : SEQ1M Q8T1, SEQ1M Q1T1, RND4K Q32T1, RND4K Q1T1 (lecture et écriture).",
        "icon": "💿", "builtin": True, "is_suite": True,
        "suite_tests": ["peak_seq1m_q8t1_read", "peak_seq1m_q8t1_write", "cdm_seq1m_q1t1_read", "cdm_seq1m_q1t1_write",
                        "peak_rnd4k_q32t1_read", "peak_rnd4k_q32t1_write", "cdm_rnd4k_q1t1_read", "cdm_rnd4k_q1t1_write"],
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 8, "numjobs": 1, "io_engine": "auto", "direct": True, "size_mb": 1024, "runtime_s": 30},
    },
    {
        "id": "iometer_fileserver", "name": "IOMeter — Serveur de fichiers (80 % lecture)", "category": _PRO_APP,
        "description": ("Profil classique Intel/IOMeter « File Server » : accès 100 % aléatoire, 80 % lecture / 20 % écriture, "
                        "blocs 512 o→64 Ko (4 Ko = 60 %). 10 min."),
        "icon": "🗄️", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 80, "bs": "4k", "bssplit": "512/10:1k/5:2k/5:4k/60:8k/2:16k/4:32k/4:64k/10",
                   "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 4096, "runtime_s": 600},
    },
    {
        "id": "iometer_webserver", "name": "IOMeter — Serveur web (100 % lecture)", "category": _PRO_APP,
        "description": "Profil classique IOMeter « Web Server » : lecture aléatoire seule, blocs 512 o→512 Ko (4 Ko = 23 %, 512 o = 22 %). 10 min.",
        "icon": "🌐", "builtin": True,
        "params": {"rw_mode": "randread", "bs": "4k", "bssplit": "512/22:1k/15:2k/8:4k/23:8k/15:16k/2:32k/6:64k/7:128k/1:512k/1",
                   "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True, "size_mb": 4096, "runtime_s": 600},
    },
    {
        "id": "iometer_workstation", "name": "IOMeter — Station de travail (8K, 80 % lecture)", "category": _PRO_APP,
        "description": "Profil IOMeter « Workstation » : blocs 8 Ko, 80 % lecture / 20 % écriture, 80 % d'accès aléatoires. 10 min.",
        "icon": "🖥️", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 80, "bs": "8k", "fio_extra": "--percentage_random=80",
                   "iodepth": 16, "numjobs": 2, "io_engine": "auto", "direct": True, "size_mb": 4096, "runtime_s": 600},
    },
    {
        "id": "oltp_8k_70_30", "name": "Base de données OLTP — 8K aléatoire 70/30", "category": _PRO_APP,
        "description": "Charge transactionnelle type SQL Server / Oracle / PostgreSQL : blocs 8 Ko aléatoires, 70 % lecture / 30 % écriture, QD32×8. 10 min.",
        "icon": "🧾", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 70, "bs": "8k", "iodepth": 32, "numjobs": 8, "io_engine": "auto",
                   "direct": True, "size_mb": 8192, "runtime_s": 600},
    },
    {
        "id": "oltp_16k_70_30", "name": "Base de données MySQL/InnoDB — 16K aléatoire 70/30", "category": _PRO_APP,
        "description": "Pages de 16 Ko (InnoDB), 70 % lecture / 30 % écriture, QD32×8. 10 min.",
        "icon": "🧾", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 70, "bs": "16k", "iodepth": 32, "numjobs": 8, "io_engine": "auto",
                   "direct": True, "size_mb": 8192, "runtime_s": 600},
    },
    {
        "id": "etcd_wal_fdatasync", "name": "Journal / WAL — écriture fdatasync (test etcd)", "category": _PRO_APP,
        "description": ("Test officiel de disque pour etcd/Kubernetes : écritures séquentielles de 2300 o avec fdatasync à chaque écriture, "
                        "1 job. Regarder la latence fdatasync (p99 < 10 ms recommandé). Valable aussi pour journaux de bases de données."),
        "icon": "📝", "builtin": True,
        "params": {"rw_mode": "write", "bs": "2300", "fio_extra": "--fdatasync=1", "iodepth": 1, "numjobs": 1, "io_engine": "sync",
                   "direct": False, "size_mb": 512, "runtime_s": 120},
    },
    {
        "id": "hdd_rand4k_qd1", "name": "HDD — 4K aléatoire QD1 (temps d'accès)", "category": _PRO_HDD,
        "description": "Lecture aléatoire 4 Ko, QD1 : temps d'accès réel de la tête (≈ 100–200 IOPS attendus sur un 7200 tr/min). 2 min.",
        "icon": "🧲", "builtin": True,
        "params": {"rw_mode": "randread", "bs": "4k", "iodepth": 1, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 0, "runtime_s": 120},
    },
    {
        "id": "hdd_rand4k_ncq", "name": "HDD — 4K aléatoire 70/30 QD16 (NCQ)", "category": _PRO_HDD,
        "description": "Charge aléatoire mixte 4 Ko, 70 % lecture, QD16 : efficacité du réordonnancement NCQ/TCQ. 5 min.",
        "icon": "🧲", "builtin": True,
        "params": {"rw_mode": "randrw", "rwmixread": 70, "bs": "4k", "iodepth": 16, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 0, "runtime_s": 300},
    },
    {
        "id": "hdd_zone_outer_read", "name": "HDD — Débit zone externe (0–5 %, lecture 1M)", "category": _PRO_HDD,
        "description": "Lecture séquentielle 1 Mo sur les 5 % du début du disque (zone la plus rapide). Compare avec la zone interne pour juger l'homogénéité. 2 min.",
        "icon": "🧲", "builtin": True,
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True,
                   "size_expr": "5%", "runtime_s": 120},
    },
    {
        "id": "hdd_zone_inner_read", "name": "HDD — Débit zone interne (95–100 %, lecture 1M)", "category": _PRO_HDD,
        "description": "Lecture séquentielle 1 Mo sur les 5 % de la fin du disque (zone la plus lente, ≈ 50–60 % de la zone externe). 2 min.",
        "icon": "🧲", "builtin": True,
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True,
                   "size_expr": "5%", "fio_extra": "--offset=95%", "runtime_s": 120},
    },
    {
        "id": "hdd_full_surface_read", "name": "HDD — Lecture complète de la surface (sans écriture)", "category": _PRO_HDD,
        "description": ("Lit tout le disque une fois (1 Mo, QD4) sans rien écrire : détecte secteurs illisibles et ralentissements localisés. "
                        "Durée = capacité / débit (ex. 10 To ≈ 15 h). Surveiller SMART (secteurs réalloués) avant/après."),
        "icon": "🧲", "builtin": True,
        "params": {"rw_mode": "read", "bs": "1m", "iodepth": 4, "numjobs": 1, "io_engine": "libaio", "direct": True, "size_mb": 0, "runtime_s": 0},
    },
]
BUILTIN_PROFILES.extend(_PRO_PROFILES)


# Profils « Peak » UNITAIRES (en plus de la suite complète) : mêmes paramètres que les sous-tests de la suite, listés dans l'onglet Performance.
# Identifiants distincts (peak_u_*) : la suite continue d'utiliser ses sous-tests internes, dont les noms servent de libellés dans ses résultats.
_PEAK_UNIT_ORDER = ["peak_seq1m_q8t1_read", "peak_seq1m_q8t1_write", "peak_seq128k_q32t1_read", "peak_seq128k_q32t1_write",
                    "peak_rnd4k_q32t16_read", "peak_rnd4k_q32t16_write", "peak_rnd4k_q32t1_read", "peak_rnd4k_q32t1_write"]
for _pid in _PEAK_UNIT_ORDER:
    _sub = _INTERNAL_SUITE_SUBTESTS[_pid]
    BUILTIN_PROFILES.append({
        "id": "peak_u_" + _pid[len("peak_"):], "name": "Peak — " + _sub["name"], "category": "Peak Performance", "tab": "perf",
        "description": _sub["description"] + " Test unitaire (la suite complète « Tous les tests Peak » les enchaîne tous).",
        "icon": "⚡", "builtin": True, "params": dict(_sub["params"]),
    })


# ─────────────────────────────────────────────────────────────────────────────
# Onglets du menu (classement par FONCTION) et normes d'origine
# ─────────────────────────────────────────────────────────────────────────────

# (identifiant, icône, libellé) : un onglet cliquable ouvre la liste des profils qui font cela
FIO_TABS = [
    ("perf", "⚡", "Performance"),
    ("lat",  "⏱", "Latence"),
    ("stab", "🛡", "Stabilité"),
    ("heat", "🌡", "Chauffe"),
    ("endu", "⏳", "Endurance"),
    ("app",  "🧾", "Applicatif"),
    ("hdd",  "🧲", "HDD"),
    ("user", "👤", "Mes profils"),
]

# Profils historiques : onglet explicite (les nouveaux profils portent directement la clé « tab »)
_TAB_BY_ID = {
    "stress_nvme_1h": "heat", "stress_nvme_4h": "heat",
    "conditioning_nvme_quick": "endu", "conditioning_nvme_full": "endu",
    "jesd219_enterprise_1h": "endu", "jesd219_enterprise_8h": "endu",
    "snia_pts_4k_steady_30m": "perf", "snia_pts_iops_matrix": "perf", "snia_pts_throughput": "perf",
    "snia_pts_latency": "lat", "etcd_wal_fdatasync": "lat",
}


def profile_tab(p: dict) -> str:
    """Onglet d'un profil : clé « tab » explicite, sinon déduit de l'identifiant / de la catégorie."""
    if p.get("tab"):
        return p["tab"]
    pid = str(p.get("id", ""))
    if not p.get("builtin") and pid not in _TAB_BY_ID and not str(p.get("category", "")).startswith(("Peak", "Pro", "Stress", "Cond")):
        return "user"
    if pid in _TAB_BY_ID:
        return _TAB_BY_ID[pid]
    cat = str(p.get("category", ""))
    if pid.startswith("hdd") or "HDD" in cat or "Disques durs" in cat:
        return "hdd"
    if "applicatives" in cat:
        return "app"
    return "perf"


def profile_standard(p: dict) -> str:
    """Norme d'origine d'un profil (OCP, SNIA, JEDEC, CDM, IOMeter…) ; « » pour un profil libre."""
    if p.get("standard"):
        return p["standard"]
    pid = str(p.get("id", ""))
    for pre, std in (("ocp_", "OCP"), ("snia_", "SNIA"), ("jesd", "JEDEC"), ("cdm", "CrystalDiskMark"),
                     ("iometer", "IOMeter"), ("conditioning_nvme_full", "SNIA")):
        if pid.startswith(pre):
            return std
    return ""


_PERC = "--percentile_list=50:90:99:99.9:99.99:99.999"      # percentiles étendus (5 « neuf » : SNIA / OCP)
_OCP = "OCP Datacenter NVMe SSD v2.7"


def _P(pid, name, tab, standard, desc, icon, params, cat=None, **extra):
    base = {"rw_mode": "randrw", "bs": "4k", "iodepth": 32, "numjobs": 4, "io_engine": "auto", "direct": True,
            "size_mb": 0, "runtime_s": 1800}
    base.update(params)
    d = {"id": pid, "name": name, "category": cat or f"{standard or 'BenchSuite'} — {dict((t, l) for t, _, l in FIO_TABS)[tab]}",
         "tab": tab, "standard": standard, "description": desc, "icon": icon, "builtin": True, "params": base}
    d.update(extra)
    return d


_WRITE = "ÉCRIT sur le disque : disque de test dédié, jamais le disque système."

_NEW_PROFILES = [
    # ── OCP Datacenter NVMe SSD Specification v2.7 ──────────────────────────
    _P("ocp_endud2_eol_80", "OCP ENDUD-2 — Charge de fin de vie (80 % plein, 1 h)", "endu", "OCP",
       (f"{_OCP}, exigence ENDUD-2 : 50 % lecture / 50 % écriture (en nombre d'E/S), lectures 4 Ko aléatoires alignées, "
        "écritures 128 Ko séquentielles alignées, plage active 100 %, disque rempli à 80 %, données incompressibles. " + _WRITE),
       "🏢", {"rwmixread": 50, "size_expr": "80%", "runtime_s": 3600,
              "bs": "4k,128k", "blockalign": "4k,128k",
              "fio_extra": "--percentage_random=100,0 --buffer_compress_percentage=0 --refill_buffers"}),
    _P("ocp_endud2_eol_short", "OCP ENDUD-2 — Fin de vie, capacité réduite 10 % (≥ 2 To, 1 h)", "endu", "OCP",
       (f"{_OCP}, ENDUD-2 / EOL-2 : même charge que ci-dessus limitée à 10 % de la capacité (la spec réduit le firmware à 3–10 % "
        "pour les SSD ≥ 2 To ; ici on borne seulement la zone testée, approximation logicielle). " + _WRITE),
       "🏢", {"rwmixread": 50, "size_expr": "10%", "runtime_s": 3600,
              "bs": "4k,128k", "blockalign": "4k,128k",
              "fio_extra": "--percentage_random=100,0 --buffer_compress_percentage=0 --refill_buffers"}),
    _P("ocp_cto3_qd1", "OCP CTO-3 — Latence à QD1 (20 min, aucune E/S > 8 s)", "lat", "OCP",
       (f"{_OCP}, CTO-3 : une E/S isolée (profondeur de file 1) ne doit jamais dépasser 8 s. 4 Ko aléatoire 70/30, QD1. "
        "Verdict : latence maximale > 8 s = non conforme. " + _WRITE),
       "⏱", {"rwmixread": 70, "iodepth": 1, "numjobs": 1, "runtime_s": 1200, "fio_extra": _PERC}),
    _P("ocp_cto3_fullload_1h", "OCP CTO-3 — Latence sous pleine charge (1 h, ≤ 7 E/S > 2 s)", "lat", "OCP",
       (f"{_OCP}, CTO-3 : sous pleine charge pas plus de 7 E/S ne doivent dépasser 2 s en une heure. 4 Ko aléatoire 70/30, QD128×4. "
        "BenchSuite mesure la latence maximale (> 2 s = à vérifier, > 8 s = non conforme) ; le nombre exact d'E/S > 2 s n'est pas compté. " + _WRITE),
       "⏱", {"rwmixread": 70, "iodepth": 128, "numjobs": 4, "runtime_s": 3600, "fio_extra": _PERC}),
    _P("ocp_fill_seq", "OCP META-PERF-5 — Remplissage séquentiel complet (≤ 180 min)", "endu", "OCP",
       (f"{_OCP}, section Meta : tout le disque doit être écrit en 180 minutes au plus (script fio mono-thread séquentiel). "
        "Durée = temps pour remplir la capacité ; cible indicative (exigence propre à Meta). Sur un disque brut /dev/nvmeXnY. " + _WRITE),
       "📥", {"rw_mode": "write", "bs": "1m", "iodepth": 32, "numjobs": 1, "runtime_s": 0}),

    # ── SNIA SSS PTS 2.0.2 : saturation d'écriture, histogramme de temps de réponse ─
    _P("snia_wsat_4k_w_6h", "SNIA WSAT — 4K aléatoire écriture, 6 h", "endu", "SNIA",
       ("SNIA SSS PTS 2.0.2, Write Saturation (§10) : écriture aléatoire 4 Ko continue depuis un disque purgé pendant 6 h (ou 4× la capacité), "
        "pour voir la dérive des IOPS (FOB → transition → état stable). Purger le SSD avant (blkdiscard / secure erase). " + _WRITE),
       "📐", {"rw_mode": "randwrite", "runtime_s": 21600, "fio_extra": "--buffer_compress_percentage=0 --refill_buffers"}),
    _P("snia_wsat_8k_oltp_6h", "SNIA WSAT — 8K aléatoire 65/35 (OLTP), 6 h", "endu", "SNIA",
       "SNIA PTS 2.0.2, WSAT charge optionnelle « Mixed / OLTP » : RND 8 Ko, 65 % lecture. Même déroulé que le WSAT 4K. " + _WRITE,
       "📐", {"rw_mode": "randrw", "bs": "8k", "rwmixread": 65, "runtime_s": 21600}),
    _P("snia_wsat_128k_vod_6h", "SNIA WSAT — 128K séquentiel 90/10 (VOD), 6 h", "endu", "SNIA",
       "SNIA PTS 2.0.2, WSAT charge optionnelle « VOD » : SEQ 128 Ko, 90 % lecture. " + _WRITE,
       "📐", {"rw_mode": "readwrite", "bs": "128k", "rwmixread": 90, "numjobs": 2, "runtime_s": 21600}),
    _P("snia_rt_hist_4k_w_20m", "SNIA — Histogramme de temps de réponse 4K écriture (20 min)", "lat", "SNIA",
       ("SNIA PTS 2.0.2 §10.2 : « Response Histogram – Time Confidence Level » : RND 4 Ko écriture 20 min ; percentiles jusqu'à 99,999 % (5 « neuf ») "
        "et latence maximale dans les résultats. " + _WRITE),
       "📐", {"rw_mode": "randwrite", "runtime_s": 1200, "fio_extra": _PERC}),

    # ── JEDEC JESD219 (version courte) ─────────────────────────────────────
    _P("jesd219_enterprise_30m", "JEDEC JESD219 — Charge entreprise (30 min)", "endu", "JEDEC",
       ("Même charge JESD219 que les profils 1 h / 8 h (mélange 512 o→64 Ko, 40/60 lecture/écriture, zones chaudes), en 30 min : contrôle rapide. "
        + _WRITE), "🏭",
       {"rw_mode": "randrw", "rwmixread": 40, "bssplit": _JESD219_BSSPLIT, "random_distribution": _JESD219_ZONES,
        "blockalign": "4k", "iodepth": 256, "runtime_s": 1800}),

    # ── Stabilité : intégrité des données, liens PCIe, erreurs média ─────────
    _P("stab_verify_crc_30m", "Stabilité — Intégrité des données (écriture + relecture CRC, 30 min)", "stab", "",
       ("Écrit des blocs 16 Ko avec somme de contrôle (crc32c) et les relit en continu ; la moindre donnée corrompue arrête le test en erreur. "
        "BenchSuite compare aussi les erreurs PCIe (AER), la largeur du lien et les erreurs média NVMe avant/après. " + _WRITE),
       "🛡", {"rw_mode": "randwrite", "bs": "16k", "iodepth": 32, "numjobs": 4, "size_expr": "10%", "runtime_s": 1800,
              "fio_extra": "--verify=crc32c --verify_backlog=1024 --verify_fatal=1 --do_verify=1"}),
    _P("stab_link_saturation_30m", "Stabilité — Saturation du lien PCIe (128K séquentiel 50/50, 30 min)", "stab", "",
       ("Lecture et écriture 128 Ko simultanées au débit maximum : sature le lien PCIe dans les deux sens. Verdict : aucune erreur PCIe AER, "
        "lien stable, aucune erreur média. " + _WRITE),
       "🛡", {"rw_mode": "readwrite", "bs": "128k", "rwmixread": 50, "iodepth": 64, "numjobs": 4, "runtime_s": 1800}),
    _P("stab_small_io_flood_30m", "Stabilité — Déluge de petites E/S (4K 70/30, QD256×8, 30 min)", "stab", "",
       ("Un maximum d'IOPS : des millions de petits paquets PCIe par seconde, qui révèlent riser, connecteur ou câble marginaux. "
        "Verdict : aucune erreur PCIe, aucune erreur média. " + _WRITE),
       "🛡", {"rw_mode": "randrw", "rwmixread": 70, "iodepth": 256, "numjobs": 8, "runtime_s": 1800}),
    _P("stab_soak_4h", "Stabilité — Endurance mixte (mélange de blocs 70/30, 4 h)", "stab", "",
       ("Charge mixte longue (blocs 4 Ko à 128 Ko, 60 % lecture) pour provoquer les défauts intermittents : erreurs PCIe, perte de lien, "
        "ralentissements. " + _WRITE),
       "🛡", {"rw_mode": "randrw", "rwmixread": 60, "bssplit": "4k/50:16k/20:64k/20:128k/10", "iodepth": 64, "numjobs": 4,
              "runtime_s": 14400}),

    # ── Chauffe : montée en température, throttling ─────────────────────────
    _P("heat_seq_write_30m", "Chauffe — Écriture séquentielle continue (128K, 30 min)", "heat", "",
       ("Écriture séquentielle maximale : puissance et température les plus élevées d'un SSD. Courbe de température, maximum et détection de "
        "throttling (compteurs de gestion thermique NVMe avant/après). " + _WRITE),
       "🌡", {"rw_mode": "write", "bs": "128k", "iodepth": 32, "numjobs": 2, "runtime_s": 1800}),
    _P("heat_rand_mixed_1h", "Chauffe — Aléatoire mixte soutenu (4K 70/30, 1 h)", "heat", "",
       ("Charge aléatoire soutenue pendant une heure : le SSD atteint son équilibre thermique ; une chute de débit en fin de test indique un "
        "throttling. " + _WRITE),
       "🌡", {"rw_mode": "randrw", "rwmixread": 70, "iodepth": 64, "numjobs": 8, "runtime_s": 3600}),
    _P("heat_read_flood_30m", "Chauffe — Lecture aléatoire maximale (4K, 30 min)", "heat", "",
       "Lecture 4 Ko au maximum d'IOPS, sans écriture : chauffe du contrôleur sans usure du SSD (convient aussi à un disque contenant des données).",
       "🌡", {"rw_mode": "randread", "iodepth": 256, "numjobs": 8, "runtime_s": 1800}),
]
BUILTIN_PROFILES.extend(_NEW_PROFILES)

_BUILTIN_INDEX = {p["id"]: p for p in BUILTIN_PROFILES}


# ─────────────────────────────────────────────────────────────────────────────
# Profils utilisateur
# ─────────────────────────────────────────────────────────────────────────────

def load_user_profiles() -> list:
    if not os.path.exists(PROFILES_FILE):
        return []
    try:
        with open(PROFILES_FILE, "r", encoding="utf-8") as f:
            return [p for p in json.load(f) if isinstance(p, dict)]
    except Exception:
        return []


def save_user_profiles(profiles: list) -> None:
    with open(PROFILES_FILE, "w", encoding="utf-8") as f:
        json.dump(profiles, f, indent=2, ensure_ascii=False)


def add_user_profile(profile: dict) -> None:
    profiles = [p for p in load_user_profiles() if p.get("id") != profile.get("id")]
    profiles.append(profile)
    save_user_profiles(profiles)


def delete_user_profile(profile_id: str) -> None:
    save_user_profiles([p for p in load_user_profiles() if p.get("id") != profile_id])


def get_all_profiles() -> list:
    return BUILTIN_PROFILES


def get_profile(profile_id: str) -> Optional[dict]:
    if profile_id in _BUILTIN_INDEX:
        return deepcopy(_BUILTIN_INDEX[profile_id])
    if profile_id in _INTERNAL_SUITE_SUBTESTS:
        return deepcopy(_INTERNAL_SUITE_SUBTESTS[profile_id])
    for p in load_user_profiles():
        if p.get("id") == profile_id:
            return deepcopy(p)
    return None
    for p in load_user_profiles():
        if p.get("id") == profile_id:
            return deepcopy(p)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Profil → commande CLI
# ─────────────────────────────────────────────────────────────────────────────

def profile_to_cmd(profile: dict, path: str = "/tmp", resolved_engine: str = "") -> str:
    p = profile.get("params", {})
    engine = resolved_engine or p.get("io_engine", "sync")
    if engine == "auto":
        engine = best_engine()

    # Périphérique brut (/dev/nvmeXnY) → fio écrit directement
    if path.startswith("/dev/"):
        test_file = path
    else:
        test_file = os.path.join(path, "_benchsuite_fio.tmp")
    runtime = int(p.get("runtime_s", 30) or 0)
    if p.get("size_expr"):
        size_arg = f"--size={p['size_expr']}"
    elif int(p.get("size_mb", 2048) or 0) > 0:
        size_arg = f"--size={p.get('size_mb', 2048)}M"
    else:
        size_arg = "--size=100%"
    parts = [
        "fio",
        "--name=bench",
        f"--filename={test_file}",
        f"--rw={p.get('rw_mode', 'read')}",
        f"--bs={p.get('bs', '1m')}",
        size_arg,
        f"--numjobs={p.get('numjobs', 1)}",
        f"--iodepth={p.get('iodepth', 8)}",
        f"--ioengine={engine}",
        "--randrepeat=0",
        "--invalidate=1",
        "--cpus_allowed_policy=split",
        "--group_reporting",
        "--output-format=json",
    ]
    if runtime > 0:
        parts += [f"--runtime={runtime}", "--time_based"]
        if int(p.get("ramp_time_s", 3) or 0) > 0:
            parts.append(f"--ramp_time={int(p.get('ramp_time_s', 3))}")
    if int(p.get("loops", 1) or 1) > 1:
        parts.append(f"--loops={int(p['loops'])}")
    if p.get("bssplit"):
        parts.append(f"--bssplit={p['bssplit']}")
    if p.get("random_distribution"):
        parts.append(f"--random_distribution={p['random_distribution']}")
    if p.get("blockalign"):
        parts.append(f"--blockalign={p['blockalign']}")
    if str(p.get("fio_extra", "")).strip():
        parts.append(str(p["fio_extra"]).strip())
    if p.get("direct", True):
        parts.append("--direct=1")
    if "rwmixread" in p and "rw" in p.get("rw_mode", ""):
        parts.append(f"--rwmixread={p['rwmixread']}")
    return " \\\n  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Commande CLI → paramètres
# ─────────────────────────────────────────────────────────────────────────────

def cmd_to_params(cmd: str) -> dict:
    p = {}
    for pattern, key, cast in [
        (r"--rw=(\S+)",        "rw_mode",   str),
        (r"--bs=(\S+)",        "bs",         str),
        (r"--size=(\d+)M",     "size_mb",    int),
        (r"--numjobs=(\d+)",   "numjobs",    int),
        (r"--runtime=(\d+)",   "runtime_s",  int),
        (r"--iodepth=(\d+)",   "iodepth",    int),
        (r"--ioengine=(\S+)",  "io_engine",  str),
        (r"--rwmixread=(\d+)", "rwmixread",  int),
        (r"--bssplit=(\S+)",   "bssplit",    str),
        (r"--random_distribution=(\S+)", "random_distribution", str),
        (r"--blockalign=(\S+)", "blockalign", str),
        (r"--loops=(\d+)",     "loops",      int),
    ]:
        m = re.search(pattern, cmd)
        if m:
            try:
                p[key] = cast(m.group(1))
            except ValueError:
                pass
    p["direct"] = "--direct=1" in cmd
    return p

# ─────────────────────────────────────────────────────────────────────────────
# Constantes pour les menus déroulants du dialog d'édition
# ─────────────────────────────────────────────────────────────────────────────

# Réglages avancés d'un profil que les widgets simples de l'interface ne couvrent pas :
# ils sont repris tels quels du profil actif (sinon un profil « pro » perdrait son mélange de blocs).
FIO_ADVANCED_KEYS = ("bssplit", "random_distribution", "blockalign", "size_expr", "ramp_time_s", "fio_extra", "loops")

FIO_RW_MODES_ALL = ["read", "write", "readwrite", "randread", "randwrite", "randrw"]
FIO_BS_ALL       = ["512", "1k", "4k", "8k", "16k", "32k", "64k", "128k",
                    "256k", "512k", "1m", "2m", "4m"]
FIO_ENGINES_ALL  = ["auto", "io_uring", "libaio", "sync", "psync", "mmap"]
FIO_DEPTHS_ALL   = ["1", "2", "4", "8", "16", "32", "64", "128", "256"]
FIO_JOBS_ALL     = ["1", "2", "4", "8", "16", "32"]
