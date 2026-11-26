"""
stress_profiles.py — Profils stress-ng avec paramètres adaptatifs à la machine.
Logique : cpu_workers="auto" → os.cpu_count(), cpu_workers="auto_physical" → cœurs physiques, vm_bytes="auto" → fraction de la RAM.
"""

import json, os, re, shlex
from copy import deepcopy
from typing import Optional
try:
    from core.cli_utils import split_safe_cli_args
except Exception:
    split_safe_cli_args = None

PROFILES_FILE = os.path.expanduser("~/.benchsuite_stress_profiles.json")


# ─────────────────────────────────────────────────────────────────────────────
# Résolution des valeurs "auto" selon la machine courante
# ─────────────────────────────────────────────────────────────────────────────

def get_physical_cores_count() -> int:
    """Retourne le nombre exact de cœurs physiques réels (sans HyperThreading/SMT)."""
    import psutil
    try:
        phys = psutil.cpu_count(logical=False)
        if phys and phys > 0:
            return phys
    except Exception:
        pass

    # Fallback sysfs core_id
    try:
        import glob
        core_ids = set()
        for p in glob.glob("/sys/devices/system/cpu/cpu[0-9]*/topology/core_id"):
            try:
                with open(p, "r") as f:
                    core_ids.add(f.read().strip())
            except Exception:
                pass
        if core_ids:
            return len(core_ids)
    except Exception:
        pass

    # Fallback division par 2 si SMT probable
    n_logical = os.cpu_count() or 4
    return max(1, n_logical // 2)


def _resolve_vm_bytes(raw: str, vm_workers: int) -> str:
    """Résout 'auto', 'auto_25pct', 'auto_85pct', etc. en taille mémoire réelle."""
    import psutil
    raw = str(raw).strip().lower()
    ram_mb = psutil.virtual_memory().total // (1024 * 1024)
    headroom_mb = 1536
    avail_mb = max(256, ram_mb - headroom_mb)

    # Détection de pourcentages (ex: auto_85pct, auto_25pct, 85%, auto_70%)
    m = re.match(r"^(?:auto_)?(\d+)(?:%|pct)$", raw)
    if m:
        pct = max(5, min(95, int(m.group(1)))) / 100.0
        target_mb = min(int(ram_mb * pct), avail_mb)
        per_mb = max(32, target_mb // max(1, vm_workers))
        return f"{per_mb}M"

    if raw == "auto":
        target_mb = min(int(ram_mb * 0.70), avail_mb)
        per_mb = min(32768, max(32, target_mb // max(1, vm_workers)))
        return f"{per_mb}M"

    return raw   # valeur littérale ("512M", "2G", …)


def resolve_auto(params: dict) -> dict:
    """Remplace toutes les valeurs 'auto' par les vraies valeurs matérielles de la machine."""
    import psutil
    p = deepcopy(params)
    n_cpu  = os.cpu_count() or 4
    ram_mb = psutil.virtual_memory().total // (1024 * 1024)

    cw = p.get("cpu_workers")
    if cw in ("auto_physical", "auto_phys", "physical"):
        p["cpu_workers"] = get_physical_cores_count()
    elif cw == "auto":
        p["cpu_workers"] = n_cpu
    elif cw == "single":
        p["cpu_workers"] = 1
    elif cw == "half":
        p["cpu_workers"] = max(1, n_cpu // 2)
    elif cw is not None:
        try:
            p["cpu_workers"] = int(cw)
        except (ValueError, TypeError):
            p["cpu_workers"] = n_cpu

    if p.get("vm_workers") == "auto_cpu":
        p["vm_workers"] = n_cpu
    elif p.get("vm_workers") == "auto":
        p["vm_workers"] = max(1, n_cpu // 8)

    vm_w = p.get("vm_workers", 0)
    p["vm_bytes"] = _resolve_vm_bytes(str(p.get("vm_bytes", "0")), max(1, vm_w))

    return p


def describe_params(params: dict) -> str:
    """Résumé court et lisible des paramètres pour l'UI."""
    if params.get("cli_only") or str(params.get("stress_ng_cli_args", "")).strip():
        args = str(params.get("stress_ng_cli_args", "") or params.get("stress_ng_extra_args", "")).strip()
        return "CLI pur" + (f" · {args[:42]}…" if len(args) > 42 else (f" · {args}" if args else ""))
    
    p = resolve_auto(params)
    parts = []
    if p.get("cpu_workers", 0):
        cw = p["cpu_workers"]
        raw_cw = params.get("cpu_workers")
        if raw_cw in ("auto_physical", "auto_phys", "physical"):
            parts.append(f'{cw}×{p.get("cpu_stressor","cpu")} (Cœurs physiques)')
        elif cw == 1:
            parts.append(f'1×{p.get("cpu_stressor","cpu")} (Single-thread)')
        else:
            parts.append(f'{cw}×{p.get("cpu_stressor","cpu")}')
            
    if p.get("vm_workers", 0):
        parts.append(f'{p["vm_workers"]}×vm({p.get("vm_bytes","?")})')
    
    # Formatage propre de la durée
    t_sec = p.get("time_s", 30)
    if t_sec >= 3600:
        h = t_sec // 3600
        m = (t_sec % 3600) // 60
        parts.append(f"{h}h{f'{m:02d}m' if m else ''}")
    elif t_sec >= 60:
        m = t_sec // 60
        s = t_sec % 60
        parts.append(f"{m}m{f'{s:02d}s' if s else ''}")
    else:
        parts.append(f"{t_sec}s")

    if p.get("cpu_load_pct", 0):
        parts.append(f'load={p["cpu_load_pct"]}%')
    return "  ·  ".join(parts)


# ─────────────────────────────────────────────────────────────────────────────
# Profils intégrés (Built-in Profiles)
# ─────────────────────────────────────────────────────────────────────────────

_LEGACY_PROFILES = [
    {
        "id":          "cpu_only_quick",
        "name":        "CPU — Test Rapide (2 min)",
        "category":    "CPU",
        "icon":        "⏱️",
        "description": (
            "Test express de 120 secondes sur tous les threads (SMT/HT inclus) avec stressor 'all'.\n"
            "Idéal pour une vérification rapide du système de refroidissement et des fréquences de boost."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "all",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       120,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_physical_only",
        "name":        "CPU — Cœurs Physiques Uniquement (Sans SMT/HT)",
        "category":    "CPU",
        "icon":        "🎯",
        "description": (
            "Alloue exactement 1 worker par cœur physique réel (exclut les threads HyperThreading / SMT virtuels).\n"
            "Mesure la performance brute réelle sans contention de pipeline, idéal pour l'évaluation multi-cœur native."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto_physical",
            "cpu_stressor": "all",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       600,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_single_core",
        "name":        "CPU — Single-Core Boost Max (1 Thread)",
        "category":    "CPU",
        "icon":        "🚀",
        "description": (
            "Charge 100% sur un unique thread isolé (1 worker) pendant 2 minutes.\n"
            "Permet d'enregistrer la fréquence maximale d'horloge (Max Turbo/Boost) sans saturation thermique globale du package."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  1,
            "cpu_stressor": "all",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       120,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_avx_matrix",
        "name":        "CPU — Calcul Flottant et Stress Vectoriel AVX (5 min)",
        "category":    "CPU",
        "icon":        "🧮",
        "description": (
            "Charge intensive sur les unités matricielles et vectorielles (stressor 'matrixprod').\n"
            "Met à rude épreuve les unités FPU/AVX, les étages d'alimentation (VRM) et teste la résistance au throttling thermique."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "matrixprod",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       300,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_stability_15m",
        "name":        "CPU — Stabilité Thermique et Tctl (15 min)",
        "category":    "CPU",
        "icon":        "🌡️",
        "description": (
            "Test moyen de 15 minutes (900s) à 100% de charge sur l'ensemble des cœurs logiques.\n"
            "Permet d'atteindre l'équilibre thermique stable des radiateurs et des boucles de watercooling."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "all",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       900,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_only_main",
        "name":        "CPU — Validation Complète (1 Heure)",
        "category":    "CPU",
        "icon":        "⚡",
        "description": (
            "Charge CPU maximale continue sur tous les threads pendant 1 heure (3600s).\n"
            "Profil de référence pour certifier la stabilité sans crash d'une station de travail ou d'un serveur."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "all",
            "vm_workers":   0,
            "vm_bytes":     "0",
            "time_s":       3600,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "ram_burn_quick",
        "name":        "RAM — Burn-in Rapide (2 min)",
        "category":    "RAM",
        "icon":        "⚡",
        "description": (
            "Test express de 2 minutes de saturation RAM (--vm-populate, --vm-keep, --vm-method all).\n"
            "Alloue ~85% de la mémoire vive physique sans risque d'OOM pour valider rapidement les timings XMP/EXPO."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  0,
            "cpu_stressor": "all",
            "vm_workers":   "auto_cpu",
            "vm_bytes":     "auto_85pct",
            "time_s":       120,
            "cpu_load_pct": 0,
            "stress_ng_extra_args": "--vm-populate --vm-keep --vm-method all",
        },
    },
    {
        "id":          "ram_burn_max",
        "name":        "RAM — Burn-in Mémoire Intensif (15 min)",
        "category":    "RAM",
        "icon":        "🔥",
        "description": (
            "Saturation et chauffe mémoire vive intensive (stress-ng --vm).\n"
            "Alloue ~85% de la RAM physique sans risque d'OOM, pré-remplit les pages (--vm-populate),\n"
            "maintient l'allocation active (--vm-keep) et alterne tous les motifs de bits (--vm-method all).\n"
            "Met à rude épreuve les puces DRAM, le bus mémoire et les étages d'alimentation (VRM RAM)."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  0,
            "cpu_stressor": "all",
            "vm_workers":   "auto_cpu",
            "vm_bytes":     "auto_85pct",
            "time_s":       900,
            "cpu_load_pct": 0,
            "stress_ng_extra_args": "--vm-populate --vm-keep --vm-method all",
        },
    },
    {
        "id":          "ram_burn_endurance_1h",
        "name":        "RAM — Endurance & Intégrité Mémoire (1 Heure)",
        "category":    "RAM",
        "icon":        "🛡️",
        "description": (
            "Test d'endurance d'une heure (3600s) avec saturation à 85% de la RAM physique.\n"
            "Balayage continu de tous les algorithmes de test mémoire pour certifier l'absence d'erreurs mémoire silencieuses (soft errors/bit flips)."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  0,
            "cpu_stressor": "all",
            "vm_workers":   "auto_cpu",
            "vm_bytes":     "auto_85pct",
            "time_s":       3600,
            "cpu_load_pct": 0,
            "stress_ng_extra_args": "--vm-populate --vm-keep --vm-method all",
        },
    },
    {
        "id":          "cpu_ram_main",
        "name":        "CPU + RAM — Charge Mixte (1 Heure)",
        "category":    "Combiné",
        "icon":        "🔥",
        "description": (
            "Charge CPU maximale + allocation dynamique de RAM (~70 % de la mémoire système) en simultané.\n"
            "Valide l'interaction contrôleur mémoire (IMC), bus d'interconnexion (Infinity Fabric / UPI) et stabilité globale."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "all",
            "vm_workers":   "auto_cpu",
            "vm_bytes":     "auto",
            "time_s":       3600,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "cpu_burnin_4h",
        "name":        "🔥 CPU + RAM — Burn-in Serveur (4 Heures)",
        "category":    "Combiné",
        "icon":        "🏢",
        "description": (
            "Test d'endurance lourde 4 heures (14400s) CPU + RAM pour validation.\n"
            "Idéal pour les bancs de recette, l'assurance qualité et la détection précoce des composants défectueux."
        ),
        "builtin": True,
        "params": {
            "cpu_workers":  "auto",
            "cpu_stressor": "all",
            "vm_workers":   "auto_cpu",
            "vm_bytes":     "auto",
            "time_s":       14400,
            "cpu_load_pct": 0,
        },
    },
    {
        "id":          "stress_ng_cli_only",
        "name":        "stress-ng — Ligne de commande CLI",
        "category":    "CLI",
        "icon":        "⌨",
        "description": (
            "Lance stress-ng directement avec les arguments personnalisés que vous saisissez.\n"
            "BenchSuite n'ajoute ni --cpu, ni --vm, ni --timeout automatiquement."
        ),
        "builtin": True,
        "params": {
            "cli_only": True,
            "stress_ng_cli_args": "--cpu 0 --cpu-method all --timeout 1800s --metrics-brief",
            "time_s": 1800,
            "cpu_workers": 0,
            "cpu_stressor": "all",
            "vm_workers": 0,
            "vm_bytes": "0",
            "cpu_load_pct": 0,
        },
    },
]

# Anciens profils : conservés (séquences et scripts existants) mais masqués dans les listes,
# sauf ceux qui n'ont pas d'équivalent dans la nouvelle grille.
_LEGACY_VISIBLE = {"cpu_single_core": "Spécial", "cpu_avx_matrix": "Spécial", "stress_ng_cli_only": "Personnalisé"}
for _p in _LEGACY_PROFILES:
    if _p["id"] in _LEGACY_VISIBLE:
        _p["category"] = _LEGACY_VISIBLE[_p["id"]]
    else:
        _p["hidden"] = True

# Durées standard de la grille : extra rapide / rapide / burn-in
_DURATIONS = [("5m", 300, "Extra rapide (5 min)", "⏱️"), ("30m", 1800, "Rapide (30 min)", "⌛"), ("4h", 14400, "Burn-in (4 h)", "🔥")]
_RAM_ARGS = "--vm-populate --vm-keep --vm-method all"


def _grid_profiles() -> list:
    """Construit la grille de profils CPU / RAM / mixte × 3 durées."""
    out = []
    for suffix, secs, dlabel, icon in _DURATIONS:
        out.append({
            "id": f"cpu_all_{suffix}", "name": f"CPU · Tous les threads — {dlabel}", "category": "CPU · Tous les threads",
            "icon": icon, "builtin": True,
            "description": f"Charge CPU 100 % sur tous les threads logiques (SMT/HT inclus), stressor « all », {secs // 60} min.",
            "params": {"cpu_workers": "auto", "cpu_stressor": "all", "vm_workers": 0, "vm_bytes": "0", "time_s": secs, "cpu_load_pct": 0},
        })
    for suffix, secs, dlabel, icon in _DURATIONS:
        out.append({
            "id": f"cpu_phys_{suffix}", "name": f"CPU · Cœurs physiques — {dlabel}", "category": "CPU · Cœurs physiques",
            "icon": icon, "builtin": True,
            "description": f"Un worker par cœur physique (sans SMT/HT), stressor « all », {secs // 60} min. Mesure la performance brute sans contention de pipeline.",
            "params": {"cpu_workers": "auto_physical", "cpu_stressor": "all", "vm_workers": 0, "vm_bytes": "0", "time_s": secs, "cpu_load_pct": 0},
        })
    for suffix, secs, dlabel, icon in _DURATIONS:
        out.append({
            "id": f"ram_burn_{suffix}", "name": f"RAM · Burn — {dlabel}", "category": "RAM",
            "icon": icon, "builtin": True,
            "description": (f"Burn mémoire stress-ng {secs // 60} min : ~85 % de la RAM, pages pré-remplies (--vm-populate), allocation maintenue "
                            "(--vm-keep), tous les motifs de bits (--vm-method all). Sans risque d'OOM (marge réservée)."),
            "params": {"cpu_workers": 0, "cpu_stressor": "all", "vm_workers": "auto_cpu", "vm_bytes": "auto_85pct",
                       "time_s": secs, "cpu_load_pct": 0, "stress_ng_extra_args": _RAM_ARGS},
        })
    for suffix, secs, dlabel, icon in _DURATIONS:
        out.append({
            "id": f"mix_{suffix}", "name": f"CPU + RAM · Mixte — {dlabel}", "category": "Mixte CPU + RAM",
            "icon": icon, "builtin": True,
            "description": (f"CPU 100 % (tous les threads) et RAM (~70 %) simultanément pendant {secs // 60} min : "
                            "valide contrôleur mémoire, interconnexion (Infinity Fabric / UPI) et alimentation sous charge combinée."),
            "params": {"cpu_workers": "auto", "cpu_stressor": "all", "vm_workers": "auto_cpu", "vm_bytes": "auto",
                       "time_s": secs, "cpu_load_pct": 0},
        })
    out.append({
        "id": "stress_custom", "name": "Personnalisé — tout modifiable (onglets)", "category": "Personnalisé",
        "icon": "🎛️", "builtin": True, "editable": True,
        "description": ("Point de départ entièrement réglable : durée, nombre de workers CPU / vm, méthode, taille mémoire, charge %, "
                        "options stress-ng. Les champs du panneau « Ajustements » sont déjà actifs."),
        "params": {"cpu_workers": "auto", "cpu_stressor": "all", "vm_workers": 0, "vm_bytes": "0", "time_s": 600, "cpu_load_pct": 0},
    })
    return out


_CAT_ORDER = ["CPU · Tous les threads", "CPU · Cœurs physiques", "RAM", "Mixte CPU + RAM", "Spécial", "Personnalisé"]
BUILTIN_PROFILES = sorted(_grid_profiles() + _LEGACY_PROFILES,
                          key=lambda p: _CAT_ORDER.index(p["category"]) if p["category"] in _CAT_ORDER else 99)

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
    existing = [p for p in load_user_profiles() if p.get("id") != profile.get("id")]
    existing.append(profile)
    save_user_profiles(existing)


def delete_user_profile(profile_id: str) -> None:
    save_user_profiles([p for p in load_user_profiles()
                        if p.get("id") != profile_id])


def get_all_profiles() -> list:
    """Profils affichables (les anciens identifiants masqués restent accessibles via get_profile)."""
    return [p for p in BUILTIN_PROFILES if not p.get("hidden")] + load_user_profiles()


def get_profile(profile_id: str) -> Optional[dict]:
    if profile_id in _BUILTIN_INDEX:
        return deepcopy(_BUILTIN_INDEX[profile_id])
    for p in load_user_profiles():
        if p.get("id") == profile_id:
            return deepcopy(p)
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Construction de la commande stress-ng
# ─────────────────────────────────────────────────────────────────────────────

STRESS_STRESSORS = [
    "all", "ackermann", "apery", "bitops", "callfunc",
    "cdouble", "cfloat", "clongdouble", "collatz", "correlate",
    "cpuid", "crc16", "decimal32", "decimal64", "decimal128",
    "dither", "div16", "div32", "div64", "double",
    "euler", "explog", "factorial", "fibonacci", "fft",
    "fletcher16", "float", "float32", "float64", "float80", "float128",
    "floatconversion", "gamma", "gcd", "gray", "hamming",
    "hanoi", "hyperbolic", "idct",
    "int128", "int64", "int32", "int16", "int8",
    "int128float", "int128double", "int128longdouble",
    "int128decimal32", "int128decimal64", "int128decimal128",
    "int64float", "int64double", "int64longdouble",
    "int32float", "int32double", "int32longdouble",
    "intconversion", "ipv4checksum", "jmp", "lfsr32", "ln2",
    "logmap", "longdouble", "loop", "matrixprod", "nsqrt",
    "omega", "parity", "phi", "pi", "prime", "psi",
    "queens", "rand", "rand48", "rgb", "sieve",
    "stats", "sqrt", "trig", "union", "zeta",
]

def profile_to_cmd(profile: dict) -> str:
    """Retourne la commande stress-ng correspondant au profil (après résolution auto)."""
    raw = profile.get("params", {})
    marker = " ".join(str(profile.get(k, "")) for k in ("id", "name", "category")).lower()
    if raw.get("cli_only") or str(raw.get("stress_ng_cli_args", "")).strip() or profile.get("category") == "CLI" or "cli" in marker or "ligne de commande" in marker:
        args = str(raw.get("stress_ng_cli_args", "") or raw.get("stress_ng_extra_args", "")).strip()
        if split_safe_cli_args:
            try:
                parsed, _vars, expanded = split_safe_cli_args(args)
                return "stress-ng " + " ".join(shlex.quote(x) for x in parsed)
            except Exception:
                pass
        return "stress-ng" + (" " + args if args else "")
    
    p = resolve_auto(raw)
    parts = ["stress-ng"]
    if p.get("cpu_workers", 0):
        parts += [f"--cpu", str(p["cpu_workers"]),
                  f"--cpu-method", p.get("cpu_stressor", "cpu")]
        if p.get("cpu_load_pct", 0):
            parts += ["--cpu-load", str(p["cpu_load_pct"])]
    if p.get("vm_workers", 0):
        parts += ["--vm", str(p["vm_workers"]),
                  "--vm-bytes", p.get("vm_bytes", "512M")]
    parts += ["--timeout", str(p.get("time_s", 30)),
              "--metrics-brief", "--log-brief"]
    if str(raw.get("stress_ng_extra_args", "")).strip():
        parts.append(str(raw.get("stress_ng_extra_args")).strip())
    return " \\\n  ".join(parts)
