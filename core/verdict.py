"""
verdict.py — Verdict automatique CONFORME / À VÉRIFIER / NON CONFORME / NON ÉVALUÉ.

Principe : les critères changent à chaque modèle et certaines limites ne sont écrites nulle part.
Le verdict repose donc sur trois sources, toutes optionnelles et cumulables :

 1. Règles universelles (toujours actives, AUCUNE spec nécessaire) : test en erreur, panne matérielle
    détectée, throttling thermique, écart anormal entre GPU identiques (médiane), écart ΔT hotspot.
 2. Critères utilisateur (fichier JSON ou --crit) : seuils min/max par grandeur, matériel attendu.
 3. Référence : une machine validée enregistrée (--save-reference), les suivantes sont comparées
    à ±N % (ce qui évite de connaître les limites constructeur).

Règle d'or : une donnée ou une limite absente donne « non évalué » (NA), jamais un échec.
"""

import json
import os
import re
import statistics
from datetime import datetime
from typing import Dict, List, Optional

PASS, WARN, FAIL, NA = "pass", "warn", "fail", "na"
REF_DIR = os.path.expanduser("~/.benchsuite/references")

# Seuils par défaut des règles universelles (relatifs : pas besoin de spec constructeur)
DEFAULTS = {
    "gpu_score_spread_warn_pct": 5.0,     # écart d'un GPU à la médiane (TFLOPS)
    "gpu_score_spread_fail_pct": 10.0,
    "gpu_hotspot_spread_warn_c": 10.0,    # hotspot d'un GPU au-dessus de la médiane des autres
    "gpu_hotspot_spread_fail_c": 20.0,
    "delta_hotspot_warn_c": 20.0,         # ΔT hotspot−cœur moyen en charge (heuristique courante)
    "delta_hotspot_fail_c": None,         # None : jamais bloquant par défaut (à fixer si votre modèle le justifie)
    "throttle_level": WARN,               # throttling thermique détecté : avertissement (mettre "fail" pour bloquer)
    "burn_drop_warn_pct": 10.0,           # chute de performance en charge soutenue (test de chauffe vkpeak / clpeak)
    "burn_drop_fail_pct": None,           # None : jamais bloquant par défaut
    "burn_power_warn_pct": 85.0,          # puissance atteinte en % de la limite de la carte : en dessous, le burn ne sature pas le GPU
    "ref_score_tol_pct": 5.0,
    "ref_temp_tol_c": 5.0,
}

# Grandeurs GPU utilisables dans les critères : clé → suffixe de métrique (ou fonction)
GPU_METRICS = {
    "tflops": "Gflops", "temp_core_max_c": "temp_max_c", "temp_core_moy_c": "temp_moy_c",
    "temp_hotspot_max_c": "temp_hotspot_max_c", "temp_hotspot_moy_c": "temp_hotspot_moy_c",
    "temp_vram_max_c": "temp_vram_max_c", "delta_hotspot_moy_c": "delta_hotspot_moy_c",
    "delta_hotspot_p95_c": "delta_hotspot_p95_c", "power_moy_w": "power_moy_w", "power_max_w": "power_max_w",
    "freq_min_mhz": "freq_graphics_min_mhz", "freq_max_mhz": "freq_graphics_max_mhz",
    "throttle_samples": "throttle_thermal_samples", "vram_bw_gbs": "vram_bw_gbs",
}


def _rule(rid: str, label: str, level: str, detail: str = "", scope: str = "") -> Dict:
    return {"id": rid, "label": label, "level": level, "detail": detail, "scope": scope}


def _num(v) -> Optional[float]:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Extraction des grandeurs mesurées
# ─────────────────────────────────────────────────────────────────────────────

def gpu_values(results) -> Dict[int, Dict[str, float]]:
    """{index_gpu: {grandeur: valeur}} fusionné sur tous les résultats (dernière valeur gagne)."""
    out: Dict[int, Dict[str, float]] = {}
    for r in results:
        for k, v in (r.metrics or {}).items():
            m = re.match(r"^gpu(\d+)_(.+)$", str(k))
            if not m or isinstance(v, (list, dict, bool)):
                continue
            idx, suffix = int(m.group(1)), m.group(2)
            for name, src in GPU_METRICS.items():
                if suffix == src:
                    val = _num(v)
                    if val is not None:
                        out.setdefault(idx, {})[name] = val / 1000.0 if name == "tflops" else val
    return out


def snapshot_hardware(snapshot: dict) -> Dict:
    """Matériel observé (comptes et modèles) à partir du snapshot système."""
    snap = snapshot or {}
    gpus = [g for g in (snap.get("gpus") or snap.get("gpu") or []) if isinstance(g, dict)
            and (g.get("vram_total_mo") or g.get("serial"))]
    ram = snap.get("ram") or {}
    return {
        "gpus": len(gpus),
        "gpu_model": (gpus[0].get("nom") or gpus[0].get("modele") or "") if gpus else "",
        "gpu_pcie": sorted({(str(g.get("pcie_gen", "")), str(g.get("pcie_largeur", ""))) for g in gpus}),
        "dimms": len(ram.get("barrettes") or []),
        "ram_gb": _num(ram.get("total_go")),
        "nvme": len([d for d in (snap.get("stockage") or []) if "nvme" in str(d.get("type", "")).lower()
                     or "nvme" in str(d.get("nom", "")).lower()]),
        "cpu_model": (snap.get("cpu") or {}).get("modele", ""),
        "sockets": _num((snap.get("cpu") or {}).get("socket")),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Règles
# ─────────────────────────────────────────────────────────────────────────────

def _universal_rules(results, gv: Dict[int, Dict[str, float]], cfg: Dict) -> List[Dict]:
    rules: List[Dict] = []
    # 1. Tests en erreur / interrompus
    for r in results:
        if r.status == "error":
            rules.append(_rule("test_error", f"Test « {r.name} » en échec", FAIL, (r.notes or "")[:160]))
        elif r.status == "interrupted":
            rules.append(_rule("test_interrupted", f"Test « {r.name} » interrompu puis rejoué", WARN))
        elif r.status == "stopped":
            rules.append(_rule("test_stopped", f"Test « {r.name} » arrêté avant la fin", WARN, (r.notes or "")[:160]))
        elif r.status == "skipped":
            rules.append(_rule("test_skipped", f"Test « {r.name} » non exécuté (outil manquant)", WARN))
    if results and all(r.status == "ok" for r in results):
        rules.append(_rule("tests_ok", "Tous les tests se sont terminés sans erreur", PASS))
    # 2. Pannes matérielles
    crit = warn = 0
    first = ""
    for r in results:
        for e in (r.metrics or {}).get("hardware_events") or []:
            if e.get("severity") == "critical":
                crit += 1
                first = first or f"{e.get('category', '')} {e.get('device', '')}".strip()
            else:
                warn += 1
    if crit:
        rules.append(_rule("hw_fault", f"Panne matérielle détectée pendant les tests ({crit})", FAIL, first))
    elif warn:
        rules.append(_rule("hw_events", f"Événements matériels non critiques ({warn})", WARN))
    elif results:
        rules.append(_rule("hw_clean", "Aucun événement matériel (Xid, MCE, AER, ECC) pendant les tests", PASS))
    # 2b. Diagnostic DCGM (NVIDIA) : avertissements et réussite
    for r in results:
        m = r.metrics or {}
        if "dcgm_pass" in m:
            if m.get("dcgm_fail"):
                continue                      # déjà signalé par « test en échec » (détail dans les notes)
            if m.get("dcgm_warn"):
                rules.append(_rule("dcgm_warn", f"DCGM : {m['dcgm_warn']} test(s) avec avertissement", WARN,
                                   "; ".join(m.get("dcgm_failures", [])[:3])))
            else:
                rules.append(_rule("dcgm_ok", f"DCGM niveau {m.get('dcgm_level', '?')} : tous les tests réussis", PASS))
    # 3. Throttling
    th = sorted(i for i, v in gv.items() if v.get("throttle_samples", 0) > 0)
    if th:
        rules.append(_rule("throttle", "Throttling thermique détecté", cfg["throttle_level"],
                           "GPU " + ", ".join(map(str, th))))
    elif gv:
        rules.append(_rule("no_throttle", "Aucun throttling thermique signalé par le pilote", PASS))
    # 3b. Chauffe prolongée (vkpeak / clpeak en boucle) : le GPU garde-t-il ses performances à température stabilisée ?
    for r in results:
        for k, v in (r.metrics or {}).items():
            if str(k).endswith("burn_drop_pct") and _num(v) is not None:
                drop = _num(v)
                first = _num((r.metrics or {}).get(str(k).replace("burn_drop_pct", "burn_score_first")))
                end = _num((r.metrics or {}).get(str(k).replace("burn_drop_pct", "burn_score_end")))
                detail = (f"{first:.0f} → {end:.0f} {r.unit} sur {int((r.metrics or {}).get(str(k).replace('burn_drop_pct', 'burn_passes')) or 0) or '?'} passages"
                          if first and end else "")
                fail_c = cfg.get("burn_drop_fail_pct")
                lvl = (FAIL if fail_c is not None and drop > fail_c else WARN if drop > cfg["burn_drop_warn_pct"] else PASS)
                label = (f"Performances tenues en charge soutenue (« {r.name} », −{drop:.1f} %)" if lvl == PASS
                         else f"Chute de performance en charge soutenue (« {r.name} », −{drop:.1f} %) — refroidissement / limite de puissance ?")
                rules.append(_rule("burn_drop", label, lvl, detail))
                break
    # 3c. Burn OpenCL vérifié : erreurs de calcul / de mémoire détectées = GPU défectueux ou instable ; puissance atteinte
    for r in results:
        m = r.metrics or {}
        if "compute_errors" in m or "memory_errors" in m:
            ce, me = int(_num(m.get("compute_errors")) or 0), int(_num(m.get("memory_errors")) or 0)
            if ce or me:
                first = (m.get("erreurs_detail") or [""])[0]
                rules.append(_rule("gpu_compute_errors", f"Erreurs détectées par « {r.name} » : {ce} de calcul, {me} de mémoire", FAIL, str(first)[:200]))
            elif r.status == "ok" and m.get("verifie"):
                rules.append(_rule("gpu_compute_ok", f"« {r.name} » : aucune erreur de calcul ni de mémoire", PASS))
            else:
                rules.append(_rule("gpu_compute_ok", f"« {r.name} » : vérification non terminée (test interrompu ou en échec)", NA))
        if "burn_time_s" in m:
            for k, v in sorted(m.items()):
                mt = re.match(r"^gpu(\d+)_power_cap_pct$", str(k))
                if not mt or _num(v) is None:
                    continue
                pct = _num(v)
                cap, med = _num(m.get(f"gpu{mt.group(1)}_power_cap_w")), _num(m.get(f"gpu{mt.group(1)}_power_med_w"))
                p95 = _num(m.get(f"gpu{mt.group(1)}_power_p95_w"))
                det = (f"GPU {mt.group(1)} : {med:.0f} W soutenus (médiane) sur {cap:.0f} W de limite" + (f", pics {p95:.0f} W (P95)" if p95 else "")) if cap and med else f"GPU {mt.group(1)}"
                if pct < cfg["burn_power_warn_pct"]:
                    rules.append(_rule("burn_power", f"Burn sous la limite de puissance de la carte ({pct:.0f} % en puissance soutenue) — charge insuffisante pour saturer le GPU ?", WARN, det, f"GPU {mt.group(1)}"))
                else:
                    rules.append(_rule("burn_power", f"Burn à {pct:.0f} % de la limite de puissance de la carte (puissance soutenue)", PASS, det, f"GPU {mt.group(1)}"))
    # 4. Dispersion entre GPU (comparaison à la médiane : aucune spec nécessaire)
    if len(gv) >= 3:
        tf = {i: v["tflops"] for i, v in gv.items() if "tflops" in v}
        if len(tf) >= 3:
            med = statistics.median(tf.values())
            worst = min(tf, key=tf.get)
            gap = 100.0 * (med - tf[worst]) / med if med else 0.0
            lvl = FAIL if gap > cfg["gpu_score_spread_fail_pct"] else WARN if gap > cfg["gpu_score_spread_warn_pct"] else PASS
            rules.append(_rule("gpu_spread", "Homogénéité des performances entre GPU", lvl,
                               f"pire : GPU {worst} à −{gap:.1f} % de la médiane ({med:.1f} TFLOPS)", f"GPU {worst}"))
        vb = {i: v["vram_bw_gbs"] for i, v in gv.items() if "vram_bw_gbs" in v}
        if len(vb) >= 3:
            med = statistics.median(vb.values())
            worst = min(vb, key=vb.get)
            gap = 100.0 * (med - vb[worst]) / med if med else 0.0
            lvl = FAIL if gap > cfg["gpu_score_spread_fail_pct"] else WARN if gap > cfg["gpu_score_spread_warn_pct"] else PASS
            rules.append(_rule("vram_spread", "Homogénéité de la bande passante VRAM entre GPU", lvl,
                               f"pire : GPU {worst} à −{gap:.1f} % de la médiane ({med:.0f} GB/s)", f"GPU {worst}"))
        hs = {i: v["temp_hotspot_max_c"] for i, v in gv.items() if "temp_hotspot_max_c" in v}
        if len(hs) >= 3:
            med = statistics.median(hs.values())
            hot = max(hs, key=hs.get)
            diff = hs[hot] - med
            lvl = (FAIL if diff > cfg["gpu_hotspot_spread_fail_c"] else WARN if diff > cfg["gpu_hotspot_spread_warn_c"] else PASS)
            rules.append(_rule("hotspot_spread", "Homogénéité du hotspot entre GPU", lvl,
                               f"GPU {hot} : {hs[hot]:.0f} °C, soit +{diff:.0f} °C vs médiane ({med:.0f} °C)", f"GPU {hot}"))
    # 4b. Mémoire : une seule barrette = un seul canal mémoire (bande passante divisée)
    if cfg.get("_dimms") == 1:
        rules.append(_rule("ram_single_dimm", "Mémoire en un seul canal (1 barrette)", WARN,
                           "Une seule barrette installée : bande passante mémoire et performances CPU réduites ; "
                           "peupler les canaux du processeur (voir le manuel de la carte mère)."))
    # 5. ΔT hotspot−cœur
    deltas = {i: v["delta_hotspot_moy_c"] for i, v in gv.items() if "delta_hotspot_moy_c" in v}
    if deltas:
        bad_w = {i: d for i, d in deltas.items() if d > cfg["delta_hotspot_warn_c"]}
        fail_c = cfg["delta_hotspot_fail_c"]
        bad_f = {i: d for i, d in deltas.items() if fail_c is not None and d > fail_c}
        worst = max(deltas, key=deltas.get)
        if bad_f:
            rules.append(_rule("delta_hotspot", "ΔT hotspot−cœur au-delà de la limite fixée", FAIL,
                               f"GPU {worst} : +{deltas[worst]:.1f} °C", f"GPU {worst}"))
        elif bad_w:
            rules.append(_rule("delta_hotspot", "ΔT hotspot−cœur élevé (contact dissipateur / pâte à vérifier)", WARN,
                               f"{len(bad_w)} GPU — pire : GPU {worst} à +{deltas[worst]:.1f} °C", f"GPU {worst}"))
        else:
            rules.append(_rule("delta_hotspot", "ΔT hotspot−cœur normal", PASS, f"max +{deltas[worst]:.1f} °C"))
    return rules


def _parse_limit(spec) -> Dict:
    """Normalise un seuil : nombre → max ; dict {min,max,level}."""
    if isinstance(spec, dict):
        return {"min": _num(spec.get("min")), "max": _num(spec.get("max")), "level": spec.get("level", FAIL)}
    return {"min": None, "max": _num(spec), "level": FAIL}


def _limit_rules(results, gv, limits: Dict) -> List[Dict]:
    rules: List[Dict] = []
    for key, spec in (limits or {}).items():
        lim = _parse_limit(spec)
        label = f"Critère {key}" + (f" ≥ {lim['min']:g}" if lim["min"] is not None else "") \
            + (f" ≤ {lim['max']:g}" if lim["max"] is not None else "")
        if key.startswith("gpu."):
            name = key[4:]
            vals = {i: v[name] for i, v in gv.items() if name in v}
            if name not in GPU_METRICS:
                rules.append(_rule("limit", label, NA, "grandeur GPU inconnue"))
                continue
            if not vals:
                rules.append(_rule("limit", label, NA, "aucune mesure GPU"))
                continue
            bad = []
            for i, val in sorted(vals.items()):
                if (lim["min"] is not None and val < lim["min"]) or (lim["max"] is not None and val > lim["max"]):
                    bad.append(f"GPU {i} = {val:.1f}")
            rules.append(_rule("limit", label, (lim["level"] if bad else PASS),
                               ", ".join(bad) if bad else f"{len(vals)} GPU conformes"))
        elif key.startswith("test:"):
            tname, _, metric = key[5:].rpartition(".")
            r = next((x for x in results if tname.lower() in (x.name or "").lower()), None)
            val = None
            if r is not None:
                val = _num(r.score) if metric == "score" else _num(r.duration_s if metric == "duration_s" else (r.metrics or {}).get(metric))
            if val is None:
                rules.append(_rule("limit", label, NA, "test ou mesure absente"))
            else:
                ko = (lim["min"] is not None and val < lim["min"]) or (lim["max"] is not None and val > lim["max"])
                rules.append(_rule("limit", label, lim["level"] if ko else PASS, f"mesuré : {val:g}"))
        else:
            rules.append(_rule("limit", label, NA, "clé non reconnue (gpu.<grandeur> ou test:<nom>.<métrique>)"))
    return rules


def _expected_rules(expected: Dict, hw: Dict) -> List[Dict]:
    rules: List[Dict] = []
    for key, exp in (expected or {}).items():
        obs = hw.get(key)
        label = f"Matériel attendu : {key} = {exp}"
        if obs in (None, "", []) and key not in ("gpus", "dimms", "nvme"):
            rules.append(_rule("expected", label, NA, "non détecté"))
            continue
        if key in ("gpus", "dimms", "nvme", "sockets"):
            ok = obs is not None and int(obs) == int(exp)
            rules.append(_rule("expected", label, PASS if ok else FAIL, f"détecté : {obs}"))
        elif key == "ram_gb":
            ok = obs is not None and abs(obs - float(exp)) <= 0.06 * float(exp)
            rules.append(_rule("expected", label, PASS if ok else FAIL, f"détecté : {obs} Go"))
        elif key in ("gpu_model", "cpu_model"):
            ok = str(exp).lower() in str(obs).lower()
            rules.append(_rule("expected", label, PASS if ok else FAIL, f"détecté : {obs}"))
        elif key == "gpu_pcie":
            want = tuple(map(str, exp)) if isinstance(exp, (list, tuple)) and len(exp) == 2 else None
            links = obs or []
            ok = bool(want) and all(l == want for l in map(tuple, links))
            rules.append(_rule("expected", f"Lien PCIe GPU attendu Gen{want[0]} x{want[1]}" if want else label,
                               PASS if ok else FAIL, "détecté : " + ", ".join(f"Gen{g} x{w}" for g, w in links)))
    return rules


def _reference_rules(results, gv, ref: Dict, cfg: Dict) -> List[Dict]:
    rules: List[Dict] = []
    tol = float(ref.get("tolerance_pct", cfg["ref_score_tol_pct"]))
    ttol = float(ref.get("temp_tol_c", cfg["ref_temp_tol_c"]))
    for name, rt in (ref.get("tests") or {}).items():
        cur = next((r for r in results if r.name == name), None)
        if cur is None or cur.score is None or rt.get("score") in (None, 0):
            rules.append(_rule("reference", f"Référence « {name} »", NA, "test absent ou sans score"))
            continue
        gap = 100.0 * (cur.score - rt["score"]) / rt["score"]
        lvl = PASS if gap >= -tol else FAIL
        rules.append(_rule("reference", f"Score « {name} » vs référence", lvl,
                           f"{cur.score:g} vs {rt['score']:g} {rt.get('unit', '')} ({gap:+.1f} %, tolérance ±{tol:g} %)"))
    rg = ref.get("gpu") or {}
    if gv and rg:
        for key, label in (("tflops", "TFLOPS par GPU"), ("power_moy_w", "Puissance moyenne par GPU")):
            if key in rg and rg[key]:
                med = statistics.median(v[key] for v in gv.values() if key in v) if any(key in v for v in gv.values()) else None
                if med is not None:
                    gap = 100.0 * (med - rg[key]) / rg[key]
                    rules.append(_rule("reference", f"{label} (médiane) vs référence", PASS if gap >= -tol else FAIL,
                                       f"{med:.1f} vs {rg[key]:.1f} ({gap:+.1f} %)"))
        for key, label in (("temp_hotspot_max_c", "Hotspot max"), ("delta_hotspot_moy_c", "ΔT hotspot moyen")):
            if key in rg and rg[key] is not None:
                vals = [v[key] for v in gv.values() if key in v]
                if vals:
                    med = statistics.median(vals)
                    diff = med - rg[key]
                    lvl = FAIL if diff > 2 * ttol else WARN if diff > ttol else PASS
                    rules.append(_rule("reference", f"{label} (médiane) vs référence", lvl,
                                       f"{med:.1f} vs {rg[key]:.1f} °C ({diff:+.1f} °C, tolérance {ttol:g} °C)"))
    return rules


# ─────────────────────────────────────────────────────────────────────────────
# Référence enregistrée
# ─────────────────────────────────────────────────────────────────────────────

def _expected_from_snapshot(snapshot: dict) -> Dict:
    """Matériel observé converti en « attendu » (PCIe uniquement si tous les GPU sont identiques)."""
    hw = snapshot_hardware(snapshot)
    exp = {k: v for k, v in hw.items() if v not in (None, "", [], 0) and k != "gpu_pcie"}
    links = hw.get("gpu_pcie") or []
    if len(links) == 1 and all(links[0]):
        exp["gpu_pcie"] = list(links[0])
    return exp


def build_reference(name: str, results, snapshot: dict, meta: Optional[dict] = None,
                    tolerance_pct: float = DEFAULTS["ref_score_tol_pct"]) -> Dict:
    """Résume une machine validée (scores, médianes GPU, matériel) pour servir de référence."""
    gv = gpu_values(results)
    gpu = {}
    for key in ("tflops", "power_moy_w", "temp_hotspot_max_c", "delta_hotspot_moy_c", "freq_min_mhz"):
        vals = [v[key] for v in gv.values() if key in v]
        if vals:
            gpu[key] = round(statistics.median(vals), 2)
    return {
        "name": name, "created": datetime.now().isoformat(),
        "machine": {k: (meta or {}).get(k, "") for k in ("model", "serial", "system_name")},
        "tolerance_pct": tolerance_pct, "temp_tol_c": DEFAULTS["ref_temp_tol_c"],
        "expected": _expected_from_snapshot(snapshot),
        "tests": {r.name: {"score": r.score, "unit": r.unit} for r in results if r.status == "ok" and r.score},
        "gpu": gpu,
    }


def save_reference(ref: Dict) -> str:
    os.makedirs(REF_DIR, exist_ok=True)
    path = os.path.join(REF_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "_", ref["name"]) + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ref, f, ensure_ascii=False, indent=2)
    return path


def load_reference(name_or_path: str) -> Dict:
    path = name_or_path if os.path.isfile(name_or_path) else os.path.join(
        REF_DIR, re.sub(r"[^A-Za-z0-9_.-]+", "_", name_or_path) + ".json")
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def list_references() -> List[str]:
    try:
        return sorted(f[:-5] for f in os.listdir(REF_DIR) if f.endswith(".json"))
    except OSError:
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Critères en ligne de commande
# ─────────────────────────────────────────────────────────────────────────────

_INLINE = re.compile(r"^(?P<key>[\w:.\-]+?)\s*(?P<op><=|>=|<|>)\s*(?P<val>-?\d+(?:\.\d+)?)(?::(?P<lvl>warn|fail))?$")


def parse_inline_criteria(items: List[str]) -> Dict:
    """Convertit « gpu.temp_hotspot_max_c<=95 » / « gpu.tflops>=70:warn » en dict de limites."""
    limits: Dict = {}
    for it in items or []:
        m = _INLINE.match(it.strip())
        if not m:
            raise ValueError(f"critère invalide « {it} » (attendu : gpu.tflops>=70 ou gpu.temp_hotspot_max_c<=95:warn)")
        bound = "min" if m.group("op") in (">=", ">") else "max"
        spec = limits.setdefault(m.group("key"), {})
        spec[bound] = float(m.group("val"))
        spec["level"] = m.group("lvl") or FAIL
    return limits


def load_criteria(path: str) -> Dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


CRITERIA_TEMPLATE = {
    "_aide": "Tout est optionnel : une clé absente = non évalué. Niveaux : fail (non conforme) ou warn (à vérifier).",
    "name": "Modèle de machine",
    "expected": {"gpus": 8, "gpu_model": "RTX PRO 6000", "gpu_pcie": ["5", "4"], "dimms": 24, "ram_gb": 768,
                 "nvme": 1, "cpu_model": "EPYC", "sockets": 2},
    "limits": {
        "gpu.tflops": {"min": 70, "level": "fail"},
        "gpu.temp_hotspot_max_c": {"max": 95, "level": "warn"},
        "gpu.temp_core_max_c": {"max": 85},
        "gpu.delta_hotspot_moy_c": {"max": 25, "level": "warn"},
        "gpu.power_moy_w": {"min": 550, "level": "warn"},
        "gpu.freq_min_mhz": {"min": 1800, "level": "warn"},
        "test:gpu-fryer.score": {"min": 600}
    },
    "thresholds": {"gpu_score_spread_warn_pct": 5, "gpu_score_spread_fail_pct": 10,
                   "delta_hotspot_warn_c": 20, "delta_hotspot_fail_c": None, "throttle_level": "warn"},
}


# ─────────────────────────────────────────────────────────────────────────────
# Verdict global
# ─────────────────────────────────────────────────────────────────────────────

def _inventory_rules(diff: Dict) -> List[Dict]:
    """Règles issues de la comparaison d'inventaire avant/après (périphérique perdu, lien dégradé, erreurs apparues)."""
    out: List[Dict] = []
    for f in diff.get("findings", []):
        if f["level"] == "fail":
            out.append(_rule("inv_fail", f["label"], FAIL, f.get("detail", "")))
        elif f["level"] == "warn":
            out.append(_rule("inv_warn", f["label"], WARN, f.get("detail", "")))
    if diff.get("identical"):
        out.append(_rule("inv_ok", "Inventaire matériel identique avant et après les tests", PASS))
    return out


# Classe PCI (2 premiers chiffres) sollicitée par catégorie de test : un lien en dessous de son maximum n'est une anomalie « warn »
# que pour le périphérique réellement chargé par le test (un GPU au repos pendant un test CPU baisse sa vitesse : normal).
_PCIE_CLASS_BY_CATEGORY = {"GPU": ("03", "12", "0b"), "Stockage": ("01",), "Réseau": ("02",), "Reseau": ("02",)}


PCIE_IDLE_HINT = " (peut être normal si le périphérique était au repos)"


def pcie_speed_is_warn(category: str, classe: str) -> bool:
    """Vrai si une vitesse PCIe sous le maximum est une anomalie pour ce test : le périphérique est celui qu'il sollicite
    (GPU pendant un test GPU, SSD pendant fio…), donc il n'était pas au repos."""
    return str(classe or "")[:2] in _PCIE_CLASS_BY_CATEGORY.get(category or "", ())


def _pcie_rules(results) -> List[Dict]:
    """Règles sur les liens PCIe relevés pendant les tests (`metrics.pcie_links`) : largeur sous le maximum du périphérique (« warn »),
    vitesse maximale jamais atteinte (« warn » si le périphérique est celui sollicité par le test, sinon seulement informatif).
    Aucun maximum connu → aucune règle (« non évalué »)."""
    out: List[Dict] = []
    seen_any = False
    idle = 0                                       # périphériques qui n'ont pas atteint leur vitesse max (info) : sans doute au repos
    for r in results:
        pl = (r.metrics or {}).get("pcie_links")
        if not isinstance(pl, dict) or not pl.get("periphériques"):
            continue
        targeted = _PCIE_CLASS_BY_CATEGORY.get(getattr(r, "category", ""), ())
        for d in pl["periphériques"]:
            if not (d.get("gen_max") or d.get("largeur_max")):
                continue
            seen_any = True
            name = d.get("nom") or d.get("bus", "?")
            labels = []
            for f in d.get("anomalies", []):
                lvl = f.get("level")
                lab = f.get("label", "")
                if f.get("code") == "vitesse" and str(d.get("classe", ""))[:2] in targeted:
                    lvl = "warn"
                    lab = lab.replace(PCIE_IDLE_HINT, "")      # périphérique sollicité par le test : « au repos » ne s'applique pas
                if lvl == "info" and f.get("code") == "vitesse":
                    idle += 1
                if lvl == "warn":
                    labels.append(lab)
            if labels:             # une seule règle par périphérique et par test (largeur + vitesse regroupées)
                out.append(_rule("pcie_link", f"Lien PCIe {name}{(' (' + d.get('bus', '') + ')') if d.get('nom') else ''} : " + " ; ".join(labels), WARN, f"test « {r.name} »"))
    if seen_any and not out:
        out.append(_rule("pcie_ok", "Aucun lien PCIe dégradé pendant les tests", PASS,
                         f"{idle} périphérique(s) sans atteindre leur vitesse maximale (probablement au repos pendant ce test)" if idle else ""))
    return out


def _storage_rules(results) -> List[Dict]:
    """Règles stockage : stabilité NVMe (PCIe, média), chauffe (throttling), latence maximale OCP et remplissage OCP.

    Chaque règle n'existe que si la mesure existe (« non évalué » sinon). Les limites 8 s / 2 s viennent de la
    spécification publique OCP Datacenter NVMe SSD (CTO-3) ; elles ne s'appliquent qu'aux profils OCP « latence ».
    """
    out: List[Dict] = []
    saw_health, health_bad = False, False
    for r in results:
        m = getattr(r, "metrics", None) or {}
        sante = m.get("nvme_sante")
        if isinstance(sante, dict) and sante.get("disques"):
            saw_health = True
            for f in sante.get("findings", []):
                health_bad = True
                out.append(_rule("nvme_sante", f["text"], FAIL if f["level"] == "fail" else WARN,
                                 f"test « {r.name} »"))
        pid = str(m.get("profile_id", ""))
        if pid.startswith("ocp_cto3"):
            vals = [_num(m.get(k)) for k in ("lat_lect_max_ms", "lat_ecr_max_ms")]
            vals = [v for v in vals if v is not None]
            mx = max(vals) if vals else None
            if mx is not None:
                if mx > 8000:
                    out.append(_rule("ocp_cto3", f"Latence maximale {mx / 1000:.1f} s > 8 s (OCP CTO-3)", FAIL, f"test « {r.name} »"))
                elif mx > 2000:
                    out.append(_rule("ocp_cto3", f"Latence maximale {mx / 1000:.1f} s > 2 s : OCP CTO-3 tolère au plus 7 E/S > 2 s par heure",
                                     WARN, "le nombre d'E/S > 2 s n'est pas mesuré : à vérifier"))
                else:
                    out.append(_rule("ocp_cto3", f"Latence maximale {mx:.0f} ms ≤ 2 s (OCP CTO-3)", PASS, f"test « {r.name} »"))
        if pid == "ocp_fill_seq" and getattr(r, "status", "") == "ok" and getattr(r, "duration_s", 0):
            mins = r.duration_s / 60.0
            out.append(_rule("ocp_fill", f"Remplissage séquentiel complet en {mins:.0f} min (cible Meta ≤ 180 min)",
                             PASS if mins <= 180 else WARN, "cible OCP / Meta META-PERF-5, indicative"))
    if saw_health and not health_bad:
        out.append(_rule("nvme_sante_ok", "NVMe : aucune erreur PCIe ni erreur média, pas de throttling thermique pendant les tests", PASS))
    return out


_TEST_SUFFIX_RE = re.compile(r"\s*\(?tests? « (.+?) »\)?\s*$")


def _dedupe_rules(rules: List[Dict]) -> List[Dict]:
    """Fusionne les règles identiques relevées pendant plusieurs tests (même identifiant, même libellé, même détail).

    Un lien PCIe dégradé ou un événement BMC vu pendant trois tests donnait trois lignes « ⚠ » identiques et gonflait le décompte
    (« 8 à vérifier » pour 4 problèmes). Une seule règle subsiste, qui liste les tests concernés."""
    out: List[Dict] = []
    index: Dict = {}
    for r in rules:
        detail = r.get("detail", "") or ""
        m = _TEST_SUFFIX_RE.search(detail)
        base = detail[:m.start()].strip() if m else detail
        name = m.group(1) if m else ""
        key = (r.get("id"), r.get("label"), r.get("level"), base)
        if key in index:
            tests = index[key][1]
            if name and name not in tests:
                tests.append(name)
            continue
        entry = dict(r)
        index[key] = (entry, [name] if name else [], base)
        out.append(entry)
    for entry, tests, base in index.values():
        if tests:
            tag = f"test « {tests[0]} »" if len(tests) == 1 else "tests : " + ", ".join(f"« {t} »" for t in tests)
            entry["detail"] = (base + " — " if base else "") + tag
    return out


def compute_verdict(results, snapshot: Optional[dict] = None, criteria: Optional[dict] = None,
                    reference: Optional[dict] = None, inline_limits: Optional[dict] = None,
                    inventory_diff: Optional[dict] = None) -> Dict:
    """Calcule le verdict global (règles universelles + critères + référence éventuels)."""
    cfg = dict(DEFAULTS)
    crit = criteria or {}
    cfg.update({k: v for k, v in (crit.get("thresholds") or {}).items() if k in DEFAULTS})
    gv = gpu_values(results)
    hw = snapshot_hardware(snapshot or {})
    cfg["_dimms"] = hw.get("dimms") if (snapshot or {}).get("ram") else None
    rules = _universal_rules(results, gv, cfg)
    sources = ["règles universelles"]
    pc_rules = _pcie_rules(results)
    if pc_rules:
        rules += pc_rules
        sources.append("liens PCIe pendant les tests")
    net_rules = [_rule("iperf_local", f"Test réseau « {r.name} » : le trafic n'a pas traversé le réseau (adresse de cette machine) — débit non représentatif du lien",
                       WARN, str((r.metrics or {}).get("avertissement", ""))[:300], f"test « {r.name} »")
                 for r in results if (r.metrics or {}).get("boucle_locale")]
    if net_rules:
        rules += net_rules
    st_rules = _storage_rules(results)
    if st_rules:
        rules += st_rules
        sources.append("stabilité / chauffe SSD")
    if inventory_diff is not None:            # campagne : un seul inventaire avant/après pour toute la séquence
        rules += _inventory_rules(inventory_diff)
        sources.append("inventaire avant/après")
    else:                                     # tests isolés : inventaire avant/après de chaque test
        diffs = [(r.name, (r.metrics or {}).get("inventory_diff")) for r in results]
        diffs = [(n, d) for n, d in diffs if isinstance(d, dict)]
        if diffs:
            merged = {"findings": [dict(f, detail=(f.get("detail", "") + f" (test « {n} »)").strip())
                                   for n, d in diffs for f in d.get("findings", [])]}
            merged["identical"] = not merged["findings"]
            rules += _inventory_rules(merged)
            sources.append("inventaire avant/après")
    expected = dict((reference or {}).get("expected") or {})
    expected.update(crit.get("expected") or {})
    if expected and snapshot:
        rules += _expected_rules(expected, hw)
        sources.append("matériel attendu")
    limits = dict(crit.get("limits") or {})
    limits.update(inline_limits or {})
    if limits:
        rules += _limit_rules(results, gv, limits)
        sources.append("critères" + (f" « {crit['name']} »" if crit.get("name") else ""))
    if reference:
        rules += _reference_rules(results, gv, reference, cfg)
        sources.append(f"référence « {reference.get('name', '?')} »")
    rules = _dedupe_rules(rules)
    counts = {PASS: 0, WARN: 0, FAIL: 0, NA: 0}
    for r in rules:
        counts[r["level"]] += 1
    if counts[FAIL]:
        status = "NON_CONFORME"
    elif counts[WARN]:
        status = "A_VERIFIER"
    elif counts[PASS]:
        status = "CONFORME"
    else:
        status = "NON_EVALUE"
    labels = {"CONFORME": "CONFORME", "A_VERIFIER": "À VÉRIFIER", "NON_CONFORME": "NON CONFORME", "NON_EVALUE": "NON ÉVALUÉ"}
    return {"status": status, "label": labels[status], "counts": counts, "sources": sources, "rules": rules,
            "generated": datetime.now().isoformat()}
