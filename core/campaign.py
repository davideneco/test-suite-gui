"""campaign.py — Modèle « séquence » de l'interface : blocs → étapes du moteur de campagne, rapports de fin.

Une séquence se construit en **blocs** :
  {"mode": "seq", "items": [...]}   les tests du bloc s'enchaînent
  {"mode": "par", "items": [...]}   les tests du bloc tournent EN MÊME TEMPS (ex. burn CPU + gpu-fryer + SSD)
Chaque test (item) : {"kind": "stress"|"bench"|"fio"|"pause", "id": "…", "time_s": N}  (time_s 0 = durée du profil).

`blocks_to_steps` produit les étapes du moteur (core.runner) ; `steps_to_blocks` fait l'inverse pour charger un
fichier de séquence (suites/*.json ou ~/.benchsuite/suites). Aucun import GTK : testable sans interface.
"""

import inspect
import json
import os
import re
import threading
import time
from datetime import datetime
from typing import Dict, List, Optional

USER_SUITE_DIR = os.path.expanduser("~/.benchsuite/suites")


def _bench_fn(bench_id: str):
    """Fonction d'un test du catalogue (None si inconnu)."""
    from core.benchmarks import CATALOG
    return next((b["fn"] for b in CATALOG if b["id"] == bench_id), None)


def supports_duration(kind: str, ident: str) -> bool:
    """Vrai si la durée du test peut être forcée (profil stress-ng, profil fio à durée, test avec `time_s`)."""
    if kind in ("stress", "pause"):
        return True
    if kind == "fio":
        from core.fio_profiles import get_profile
        p = get_profile(ident) or {}
        return not p.get("is_suite") and int((p.get("params") or {}).get("runtime_s", 1) or 0) > 0
    fn = _bench_fn(ident)
    return bool(fn and "time_s" in inspect.signature(getattr(fn, "__wrapped__", fn)).parameters)


def supports_gpu_target(bench_id: str) -> bool:
    """Vrai si le test accepte une sélection de GPU (`target_gpus`)."""
    fn = _bench_fn(bench_id)
    return bool(fn and "target_gpus" in inspect.signature(getattr(fn, "__wrapped__", fn)).parameters)


def estimate_item_s(item: Dict) -> Optional[int]:
    """Durée estimée d'un test en secondes (None si elle dépend du matériel : DCGM, bande passante VRAM, fio intégral…)."""
    kind, ident, t = item.get("kind"), item.get("id", ""), int(item.get("time_s") or 0)
    if kind == "pause":
        return t or 30
    if t and supports_duration(kind, ident):
        return t
    if kind == "stress":
        from core.stress_profiles import get_profile
        p = get_profile(ident) or {}
        return int((p.get("params") or {}).get("time_s", 0) or 0) or None
    if kind == "fio":
        from core.fio_profiles import get_profile
        p = get_profile(ident) or {}
        if p.get("is_suite"):
            subs = [get_profile(x) or {} for x in p.get("suite_tests", [])]
            return sum(int((x.get("params") or {}).get("runtime_s", 0) or 0) + 3 for x in subs) or None
        return int((p.get("params") or {}).get("runtime_s", 0) or 0) or None
    from core.benchmarks import CATALOG
    b = next((x for x in CATALOG if x["id"] == ident), None)
    return int((b or {}).get("params", {}).get("time_s", 0) or 0) or None


def estimate_blocks_s(blocks: List[Dict]) -> Dict:
    """Durée estimée de la séquence : {'seconds': N, 'unknown': nombre de tests de durée variable}."""
    total, unknown = 0, 0
    for b in blocks:
        ests = [estimate_item_s(i) for i in b.get("items", []) if i.get("kind")]
        unknown += sum(1 for e in ests if e is None)
        vals = [e for e in ests if e]
        if not vals:
            continue
        total += max(vals) if b.get("mode") == "par" else sum(vals)
    return {"seconds": total, "unknown": unknown}


def format_duration(sec: int) -> str:
    """12 s, 5 min, 1 h 05."""
    sec = int(sec)
    if sec < 60:
        return f"{sec} s"
    if sec < 3600:
        return f"{sec // 60} min"
    return f"{sec // 3600} h {sec % 3600 // 60:02d}"


def item_to_step(item: Dict, fio_path: str = "", target_gpus: str = "") -> Dict:
    """Convertit un test de l'interface en étape du moteur."""
    kind, ident, t = item["kind"], item.get("id", ""), int(item.get("time_s") or 0)
    if kind == "pause":
        return {"pause": t or 30}
    keep = dict(item.get("params") or {})
    if kind == "stress":
        step = {"stress": ident}
    elif kind == "fio":
        step = {"fio": ident}
        if item.get("path") or fio_path:
            step["path"] = item.get("path") or fio_path
    else:
        step = {"bench": ident}
        if target_gpus and supports_gpu_target(ident):
            keep["target_gpus"] = target_gpus
    if keep:
        step["params"] = keep
    if t and supports_duration(kind, ident):
        step["time_s"] = t
        if "time_s" in keep:
            keep["time_s"] = t          # durée modifiée dans l'interface : prioritaire sur celle du fichier
    return step


def blocks_to_steps(blocks: List[Dict], fio_path: str = "", target_gpus: str = "") -> List[Dict]:
    """Étapes du moteur : un bloc « par » de 2 tests ou plus devient {"parallel": [...]}."""
    steps: List[Dict] = []
    for b in blocks:
        items = [i for i in b.get("items", []) if i.get("kind") and (i.get("id") or i["kind"] == "pause")]
        subs = [item_to_step(i, fio_path, target_gpus) for i in items]
        if b.get("mode") == "par" and len([s for s in subs if "pause" not in s]) >= 2:
            steps.append({"parallel": [s for s in subs if "pause" not in s]})
        else:
            steps.extend(subs)
    return steps


def _step_to_item(st: Dict) -> Optional[Dict]:
    """Étape simple du moteur → test de l'interface."""
    for kind in ("stress", "bench", "fio"):
        if kind in st:
            params = dict(st.get("params") or {})
            item = {"kind": kind, "id": st[kind], "time_s": int(st.get("time_s") or params.get("time_s") or 0)}
            if params:
                item["params"] = params         # réglages de la séquence d'origine (ex. time_s 600 de gpu_fryer)
            if st.get("path"):
                item["path"] = st["path"]
            return item
    if "pause" in st:
        return {"kind": "pause", "id": "", "time_s": int(float(st["pause"]))}
    return None


def steps_to_blocks(steps: List[Dict]) -> List[Dict]:
    """Étapes → blocs : les étapes simples consécutives forment un bloc « seq », chaque groupe un bloc « par »."""
    blocks: List[Dict] = []
    for st in steps:
        if "parallel" in st:
            items = [x for x in (_step_to_item(s) for s in st["parallel"]) if x]
            blocks.append({"mode": "par", "items": items})
        else:
            it = _step_to_item(st)
            if not it:
                continue
            if blocks and blocks[-1]["mode"] == "seq":
                blocks[-1]["items"].append(it)
            else:
                blocks.append({"mode": "seq", "items": [it]})
    return blocks


def describe_blocks(blocks: List[Dict]) -> str:
    """Résumé court : « (A + B) puis C »."""
    parts = []
    for b in blocks:
        names = [i.get("id") or "pause" for i in b.get("items", [])]
        if not names:
            continue
        parts.append("(" + " + ".join(names) + ")" if b.get("mode") == "par" and len(names) > 1 else " → ".join(names))
    return " puis ".join(parts)


def save_user_suite(name: str, steps: List[Dict], cooldown_s: float = 0.0, description: str = "") -> str:
    """Enregistre la séquence dans ~/.benchsuite/suites/<nom>.json (lancable aussi par `cli.py --suite`)."""
    os.makedirs(USER_SUITE_DIR, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "sequence"
    path = os.path.join(USER_SUITE_DIR, f"{slug}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"name": name, "description": description, "cooldown_s": cooldown_s, "steps": steps},
                  f, ensure_ascii=False, indent=2)
    return path


def delete_user_suite(suite_id: str) -> bool:
    """Supprime une séquence utilisateur (jamais celles livrées avec le logiciel)."""
    path = os.path.join(USER_SUITE_DIR, f"{suite_id}.json")
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def is_user_suite(path: str) -> bool:
    """Vrai si le fichier est une séquence utilisateur (supprimable)."""
    return os.path.dirname(os.path.abspath(path)) == os.path.abspath(USER_SUITE_DIR)


# ─────────────────────────────────────────────────────────────────────────────
# Profils prêts à l'emploi (CPU + RAM + GPU + SSD, CPU + RAM + GPU, CPU + RAM + SSD)
# ─────────────────────────────────────────────────────────────────────────────

SEQUENCE_PROFILES = (
    {
        "id": "cpu_ram_gpu_ssd",
        "name": "CPU + RAM + GPU + SSD",
        "icon": "🔥",
        "desc": "Validation complète de la machine : chauffe, stabilité et détection d'erreurs sur tous les composants.",
        "components": ("cpu_ram", "gpu", "ssd"),
    },
    {
        "id": "cpu_ram_gpu",
        "name": "CPU + RAM + GPU",
        "icon": "🎮",
        "desc": "Validation calcul et GPU (chauffe, stabilité, intégrité VRAM) sans écriture sur disque de données.",
        "components": ("cpu_ram", "gpu"),
    },
    {
        "id": "cpu_ram_ssd",
        "name": "CPU + RAM + SSD",
        "icon": "💾",
        "desc": "Validation serveur calcul et stockage (chauffe CPU/RAM + chauffe et intégrité CRC SSD, sans GPU).",
        "components": ("cpu_ram", "ssd"),
    },
)

DURATION_TIERS = (
    {
        "id": "quick",
        "label": "Rapide (10 min)",
        "burn_s": 600,
        "vram_s": 180,
        "crc_s": 300,
        "stress_id": "mix_5m",
        "cooldown_s": 30,
    },
    {
        "id": "standard",
        "label": "Standard (30 min)",
        "burn_s": 1800,
        "vram_s": 300,
        "crc_s": 600,
        "stress_id": "mix_30m",
        "cooldown_s": 60,
    },
    {
        "id": "burnin",
        "label": "Burn-in (2 h)",
        "burn_s": 7200,
        "vram_s": 600,
        "crc_s": 1800,
        "stress_id": "mix_4h",
        "cooldown_s": 60,
    },
)


def get_sequence_profile(profile_id: str) -> Optional[Dict]:
    """Retourne la définition d'un profil de séquence prêt à l'emploi (None si inconnu)."""
    return next((p for p in SEQUENCE_PROFILES if p["id"] == profile_id), None)


def get_duration_tier(tier_id: str) -> Dict:
    """Retourne le palier de durée demandé (repli sur « standard » si inconnu)."""
    return next((t for t in DURATION_TIERS if t["id"] == tier_id), DURATION_TIERS[1])


def select_gpu_tools(tool_check=None, has_nvidia: Optional[bool] = None) -> Dict:
    """Choisit automatiquement les outils GPU adaptés à la machine (repli universel NVIDIA → OpenCL → Vulkan/clpeak).

    Retourne {'stress': dict, 'vram': dict|None, 'perf': dict|None, 'label': str}.
    Priorité : chauffe et vérification d'erreurs avant toute mesure de performance.
    """
    if tool_check is None:
        from core.bench_utils import tool_exists
        tool_check = tool_exists
    if has_nvidia is None:
        try:
            from core.safety import nvidia_smi_usable
            has_nvidia = nvidia_smi_usable()
        except Exception:
            has_nvidia = False

    has_fryer = bool(tool_check("gpu-fryer") or tool_check("gpu_fryer"))
    has_burn = bool(tool_check("gpu-burn") or tool_check("gpu_burn"))
    has_nvbw = bool(tool_check("nvbandwidth"))
    has_clpeak = bool(tool_check("clpeak"))
    has_vkpeak = bool(tool_check("vkpeak"))
    # oclburn est compilé automatiquement via gcc + en-têtes OpenCL (ou binaire existant)
    has_ocl = bool(tool_check("oclburn") or tool_check("gcc") or tool_check("clinfo"))

    if has_nvidia and (has_fryer or has_burn):
        stress_id = "gpu_fryer" if has_fryer else "gpu_burn"
        vram_item = {"kind": "bench", "id": "gpu_vram_test"} if has_burn else (
            {"kind": "bench", "id": "gpu_oclburn", "params": {"mode": "memory", "vram_pct": 90}} if has_ocl else None
        )
        perf_item = {"kind": "bench", "id": "gpu_nvbandwidth"} if has_nvbw else (
            {"kind": "bench", "id": "gpu_vram_bw"} if has_clpeak else None
        )
        stress_name = "gpu-fryer" if has_fryer else "gpu-burn"
        vram_name = " + VRAM" if vram_item else ""
        return {
            "stress": {"kind": "bench", "id": stress_id},
            "vram": vram_item,
            "perf": perf_item,
            "label": f"NVIDIA ({stress_name}{vram_name})",
        }

    if has_ocl:
        perf_item = (
            {"kind": "bench", "id": "gpu_vram_bw", "params": {"method": "clpeak"}} if has_clpeak
            else ({"kind": "bench", "id": "gpu_vkpeak"} if has_vkpeak else None)
        )
        return {
            "stress": {"kind": "bench", "id": "gpu_oclburn", "params": {"mode": "power"}},
            "vram": {"kind": "bench", "id": "gpu_oclburn", "params": {"mode": "memory", "vram_pct": 90}},
            "perf": perf_item,
            "label": "OpenCL toutes marques (oclburn puissance + VRAM)",
        }

    if has_vkpeak or has_clpeak:
        stress_id = "gpu_vkpeak" if has_vkpeak else "gpu_clpeak"
        perf_item = {"kind": "bench", "id": "gpu_clpeak"} if has_clpeak else {"kind": "bench", "id": "gpu_vkpeak"}
        return {
            "stress": {"kind": "bench", "id": stress_id},
            "vram": None,
            "perf": perf_item,
            "label": f"Repli universel ({'vkpeak' if has_vkpeak else 'clpeak'})",
        }

    # Dernier repli déclaratif (oclburn reste universel toutes marques si installé ensuite)
    return {
        "stress": {"kind": "bench", "id": "gpu_oclburn", "params": {"mode": "power"}},
        "vram": {"kind": "bench", "id": "gpu_oclburn", "params": {"mode": "memory", "vram_pct": 90}},
        "perf": {"kind": "bench", "id": "gpu_vram_bw"},
        "label": "OpenCL par défaut (oclburn)",
    }


def build_profile_blocks(
    profile_id: str,
    tier: str = "standard",
    mode: str = "par",
    include_perf: bool = False,
    gpu_tools: Optional[Dict] = None,
) -> Dict:
    """Construit les blocs d'une séquence pour l'un des profils (CPU+RAM+GPU+SSD, CPU+RAM+GPU, CPU+RAM+SSD).

    - Priorité chauffe / stabilité / erreurs en tête.
    - `mode='par'` (défaut) : chauffe simultanée de tous les composants du profil puis contrôles d'intégrité (VRAM / CRC).
    - `mode='seq'` : enchaînement composant par composant (CPU+RAM → GPU → SSD) avec pauses de refroidissement.
    - `include_perf=True` : ajoute en fin de séquence un bloc optionnel de contrôle de performance (STREAM / nvbandwidth / FIO Peak).
    """
    prof = get_sequence_profile(profile_id)
    if not prof:
        raise ValueError(f"Profil de séquence inconnu : {profile_id}")
    tcfg = get_duration_tier(tier)
    comps = prof["components"]
    has_gpu = "gpu" in comps
    has_ssd = "ssd" in comps
    burn_s = int(tcfg["burn_s"])
    vram_s = int(tcfg["vram_s"])
    crc_s = int(tcfg["crc_s"])
    cool_s = int(tcfg["cooldown_s"])
    gtools = gpu_tools if gpu_tools is not None else (select_gpu_tools() if has_gpu else {})

    def _with_time(item_tpl: Dict, dur_s: int) -> Dict:
        out = {"kind": item_tpl["kind"], "id": item_tpl["id"], "time_s": dur_s}
        if item_tpl.get("params"):
            out["params"] = dict(item_tpl["params"])
        return out

    blocks: List[Dict] = []
    if mode == "par":
        par_items = [{"kind": "stress", "id": tcfg["stress_id"], "time_s": burn_s}]
        if has_gpu and gtools.get("stress"):
            par_items.append(_with_time(gtools["stress"], burn_s))
        if has_ssd:
            par_items.append({"kind": "fio", "id": "heat_rand_mixed_1h", "time_s": burn_s})
        blocks.append({"mode": "par", "items": par_items})

        check_items: List[Dict] = []
        if has_gpu and gtools.get("vram"):
            check_items.append(_with_time(gtools["vram"], vram_s))
        if has_ssd:
            check_items.append({"kind": "fio", "id": "stab_verify_crc_30m", "time_s": crc_s})
        if check_items:
            blocks.append({"mode": "seq", "items": [{"kind": "pause", "id": "", "time_s": cool_s}] + check_items})
    else:
        seq_items: List[Dict] = [{"kind": "stress", "id": tcfg["stress_id"], "time_s": burn_s}]
        if has_gpu:
            seq_items.append({"kind": "pause", "id": "", "time_s": cool_s})
            if gtools.get("stress"):
                seq_items.append(_with_time(gtools["stress"], burn_s))
            if gtools.get("vram"):
                seq_items.append(_with_time(gtools["vram"], vram_s))
        if has_ssd:
            seq_items.append({"kind": "pause", "id": "", "time_s": cool_s})
            seq_items.append({"kind": "fio", "id": "heat_rand_mixed_1h", "time_s": burn_s})
            seq_items.append({"kind": "fio", "id": "stab_verify_crc_30m", "time_s": crc_s})
        blocks.append({"mode": "seq", "items": seq_items})

    if include_perf:
        perf_items: List[Dict] = [{"kind": "bench", "id": "ram_stream", "time_s": 0}]
        if has_gpu and gtools.get("perf"):
            perf_items.append(_with_time(gtools["perf"], 0))
        if has_ssd:
            perf_items.append({"kind": "fio", "id": "peak_suite_all", "time_s": 0})
        blocks.append({"mode": "seq", "items": perf_items})

    mode_lbl = "parallèle" if mode == "par" else "séquentiel"
    return {
        "id": prof["id"],
        "slug": prof["id"],
        "name": f"{prof['name']} — {tcfg['label']} ({mode_lbl})",
        "description": prof["desc"],
        "cooldown_s": cool_s,
        "blocks": blocks,
        "gpu_label": gtools.get("label", "") if has_gpu else "",
        "needs_gpu": has_gpu,
        "needs_ssd": has_ssd,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Dossier de campagne et rapports
# ─────────────────────────────────────────────────────────────────────────────

def make_run_dir(out_dir: str, meta: Dict, suite_id: str) -> str:
    """Dossier de campagne rangé par client / numéro de série / BenchSuite_v<ver>_<date>_<serveur>_<suite>."""
    from exporters.common import _safe_folder_part, format_report_dir_name
    parts = [out_dir]
    if meta.get("client"):
        parts.append(_safe_folder_part(meta["client"]))
    parts.append(_safe_folder_part(meta.get("serial") or meta.get("system_name") or "machine"))
    parts.append(format_report_dir_name(meta, suffix=suite_id))
    return os.path.join(*parts)


def finalize_campaign(results: List, run_dir: str, meta: Dict, sysinfo: Optional[Dict],
                      inv_before: Optional[Dict] = None) -> Dict:
    """Après les tests : inventaire de fin + comparaison, verdict, rapports HTML/PDF/JSON/CSV.

    Retourne {'verdict': …, 'inventory_diff': …, 'paths': {format: chemin}, 'errors': [...]} (jamais d'exception).
    """
    out: Dict = {"verdict": None, "inventory_diff": None, "paths": {}, "errors": []}
    if inv_before:
        try:
            from core import inventory as inv
            after = inv.collect_inventory()
            diff = inv.diff_inventory(inv_before, after)
            out["inventory_diff"] = diff
            meta["inventory_before"], meta["inventory_after"], meta["inventory_diff"] = inv_before, after, diff
            for name, data in (("inventaire_avant", inv_before), ("inventaire_apres", after), ("inventaire_diff", diff)):
                with open(os.path.join(run_dir, f"{name}.json"), "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=1)
        except Exception as exc:
            out["errors"].append(f"inventaire : {exc}")
    if not results:
        return out
    try:
        from core import verdict as vd
        out["verdict"] = vd.compute_verdict(results, sysinfo or {}, inventory_diff=out["inventory_diff"])
        meta["verdict"] = out["verdict"]
    except Exception as exc:
        out["errors"].append(f"verdict : {exc}")
    try:
        from exporters.export import save_to_history
        save_to_history(results)
    except Exception:
        pass
    from exporters import export as ex
    for fmt, fn in (("csv", lambda: ex.export_csv(results, run_dir, meta, sysinfo)),
                    ("json", lambda: ex.export_json(results, run_dir, meta, sysinfo)),
                    ("pdf", lambda: ex.export_pdf(results, run_dir, system_info_data=sysinfo, report_meta=meta)),
                    ("html", lambda: ex.export_html(results, run_dir, meta, sysinfo))):
        try:
            out["paths"][fmt] = fn()
        except Exception as exc:
            out["errors"].append(f"export {fmt} : {exc}")
    try:
        out["paths"]["dmesg"] = ex.export_dmesg_file(run_dir, meta)
    except Exception:
        pass
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Courbes de la séquence (température / puissance)
# ─────────────────────────────────────────────────────────────────────────────

def _channel_for(key: str, src: str):
    """Canal de courbe (id, libellé, panneau, unité, pointillé) pour une clé de télémétrie, ou None si hors sujet.

    Les clés génériques (temp, power…) dépendent du test émetteur : disque pour fio, CPU/RAM pour stress-ng et
    STREAM ; pour les tests GPU elles ne sont que des alias de gpu0_* (ignorées, les clés gpuN_* sont reprises).
    """
    m = re.match(r"^gpu(\d+)_(temp|temp_hotspot|power)$", key)
    if m:
        i, kind = int(m.group(1)), m.group(2)
        if kind == "temp":
            return key, f"GPU {i} cœur", "temp", "°C", False
        if kind == "temp_hotspot":
            return key, f"GPU {i} hotspot", "temp", "°C", True
        return key, f"GPU {i}", "power", "W", False
    if src == "fio":
        return {"temp": ("disk_temp", "Disque", "temp", "°C", False),
                "power": ("disk_power", "Disque", "power", "W", False)}.get(key)
    if src.startswith("gpu_"):
        return None
    if key == "temp":
        return "cpu_temp", "CPU", "temp", "°C", False
    if key == "power":
        return "cpu_power", "CPU (package)", "power", "W", False
    m = re.match(r"^(temp|power)_s(\d+)$", key)
    if m:
        n = int(m.group(2))
        return (f"cpu_{m.group(1)}_s{n}", f"CPU socket {n}", "temp" if m.group(1) == "temp" else "power",
                "°C" if m.group(1) == "temp" else "W", False)
    if key == "ram_temp":
        return "ram_temp", "RAM", "temp", "°C", False
    if key == "ram_power":
        return "ram_power", "RAM", "power", "W", False
    return None


class SequenceRecorder:
    """Enregistre températures et puissances de toute la séquence sur une même ligne de temps (horloge murale).

    Alimenté par les callbacks de télémétrie des tests (thread-safe) ; sert au graphique en direct de l'interface
    et à l'export dans les rapports (`export()`).
    """

    def __init__(self):
        self.t0 = time.time()
        self.lock = threading.Lock()
        self.channels: Dict[str, Dict] = {}
        self.marks: List[Dict] = []

    def push(self, point: Dict) -> None:
        """Ajoute un point de télémétrie (clé spéciale `_src` : type de test émetteur)."""
        src = str(point.get("_src", ""))
        now = round(time.time() - self.t0, 1)
        with self.lock:
            for k, v in point.items():
                if k.startswith("_") or not isinstance(v, (int, float)) or v <= 0:
                    continue
                ch = _channel_for(k, src)
                if not ch:
                    continue
                cid, label, panel, unit, dash = ch
                c = self.channels.setdefault(cid, {"label": label, "panel": panel, "unit": unit, "dash": dash, "t": [], "v": []})
                c["t"].append(now)
                c["v"].append(float(v))
                if len(c["t"]) > 4000:        # sous-échantillonnage : garde un point sur deux
                    c["t"], c["v"] = c["t"][::2], c["v"][::2]

    def mark(self, label: str) -> None:
        """Repère le début d'une étape sur les courbes."""
        with self.lock:
            self.marks.append({"t": round(time.time() - self.t0, 1), "label": label})

    def snapshot(self) -> Dict:
        """Copie cohérente des données (pour le dessin)."""
        with self.lock:
            return {"channels": {k: dict(c, t=list(c["t"]), v=list(c["v"])) for k, c in self.channels.items()},
                    "marks": list(self.marks), "duration": round(time.time() - self.t0, 1)}

    def export(self, max_points: int = 500) -> Dict:
        """Données pour les rapports : courbes réduites à `max_points` points + min / moyenne / max / dernier."""
        snap = self.snapshot()
        out = []
        for cid, c in snap["channels"].items():
            n = len(c["v"])
            if n < 2:
                continue
            step = max(1, n // max_points)
            vals = c["v"]
            out.append({"id": cid, "label": c["label"], "panel": c["panel"], "unit": c["unit"], "dash": c["dash"],
                        "t": c["t"][::step], "v": [round(x, 2) for x in vals[::step]],
                        "min": round(min(vals), 1), "mean": round(sum(vals) / n, 1), "max": round(max(vals), 1),
                        "last": round(vals[-1], 1)})
        out.sort(key=lambda c: (c["panel"] != "temp", c["id"]))
        return {"duration": snap["duration"], "channels": out, "marks": snap["marks"]} if out else {}
