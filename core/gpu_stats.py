"""
gpu_stats.py — Agrégats de télémétrie GPU calculés EN CHARGE.

Les séries enregistrent aussi le repos (avant/après test). Pour éviter les
faux positifs (ex. ΔT hotspot transitoire pendant le refroidissement, fréquence
mini du repos), les moyennes, ΔT et fréquences sont calculés uniquement sur la
fenêtre « en charge stabilisée ».
"""

from typing import Dict, List, Optional

WARMUP_S = 10.0          # montée en charge ignorée au début de la fenêtre
LOAD_POWER_RATIO = 0.5   # seuil de charge : 50 % de la puissance max observée
MIN_LOAD_POWER_W = 20.0  # en dessous, aucune charge détectée (série entière utilisée)
DELTA_WARN_C = 20.0
DELTA_CRIT_C = 25.0


def _num(v) -> Optional[float]:
    """Convertit en float ou None (valeurs manquantes/None)."""
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def steady_indices(t: List, power: List, temp: List) -> List[int]:
    """Retourne les indices des échantillons « en charge stabilisée ».

    Charge = puissance >= 50 % du max (sinon température au-dessus du milieu
    de sa plage). Les `WARMUP_S` premières secondes de la charge sont ignorées.
    """
    n = len(t) if t else max(len(power or []), len(temp or []))
    if n == 0:
        return []
    p = [_num(x) for x in (power or [])]
    p_max = max([x for x in p if x is not None], default=0.0)
    if p and p_max >= MIN_LOAD_POWER_W:
        load = [j for j in range(min(n, len(p))) if p[j] is not None and p[j] >= LOAD_POWER_RATIO * p_max]
    else:
        tv = [_num(x) for x in (temp or [])]
        vals = [x for x in tv if x is not None]
        if vals and max(vals) - min(vals) >= 10:
            mid = min(vals) + 0.5 * (max(vals) - min(vals))
            load = [j for j in range(min(n, len(tv))) if tv[j] is not None and tv[j] >= mid]
        else:
            load = list(range(n))
    if not load:
        return list(range(n))
    if t:
        t0 = _num(t[load[0]]) or 0.0
        steady = [j for j in load if (_num(t[j]) or 0.0) >= t0 + WARMUP_S]
        return steady or load
    return load


def _stats(vals: List[float]) -> Dict[str, float]:
    """max / moyenne / P95 d'une liste non vide."""
    s = sorted(vals)
    p95 = s[min(len(s) - 1, int(round(0.95 * (len(s) - 1))))]
    return {"max": max(s), "moy": round(sum(s) / len(s), 1), "p95": p95}


def gpu_load_metrics(series: dict, gpu_index: int) -> dict:
    """Calcule les métriques agrégées d'un GPU (clés préfixées `gpu{i}_`)."""
    pfx = f"gpu{gpu_index}_"
    t = series.get("t", [])
    temps = series.get(f"gpu{gpu_index}_temp", [])
    powers = series.get(f"gpu{gpu_index}_power", [])
    hs = series.get(f"gpu{gpu_index}_temp_hotspot", [])
    vr = series.get(f"gpu{gpu_index}_temp_vram", [])
    gfx = series.get(f"gpu{gpu_index}_freq_graphics", [])
    mem = series.get(f"gpu{gpu_index}_freq_memory", [])
    out: dict = {}
    idx = steady_indices(t, powers, temps)
    if t and idx:
        out[pfx + "load_duration_s"] = round((_num(t[idx[-1]]) or 0) - (_num(t[idx[0]]) or 0), 1)

    def pick(arr):
        return [x for x in (_num(arr[j]) for j in idx if j < len(arr)) if x is not None]

    def peak(arr):
        return [x for x in (_num(v) for v in arr) if x is not None]

    if temps:
        out[pfx + "temp_max_c"] = max(peak(temps))
        st = pick(temps)
        if st: out[pfx + "temp_moy_c"] = _stats(st)["moy"]
    if hs:
        out[pfx + "temp_hotspot_max_c"] = max(peak(hs))
        st = pick(hs)
        if st: out[pfx + "temp_hotspot_moy_c"] = _stats(st)["moy"]
    if vr:
        out[pfx + "temp_vram_max_c"] = max(peak(vr))
        st = pick(vr)
        if st: out[pfx + "temp_vram_moy_c"] = _stats(st)["moy"]

    # ΔT Hotspot ↔ Cœur : uniquement en charge stabilisée
    if temps and hs:
        deltas = []
        for j in idx:
            if j < len(temps) and j < len(hs):
                h, c = _num(hs[j]), _num(temps[j])
                if h is not None and c is not None:
                    deltas.append(h - c)
        if deltas:
            st = _stats(deltas)
            out[pfx + "delta_hotspot_max_c"] = round(st["max"], 1)
            out[pfx + "delta_hotspot_moy_c"] = st["moy"]
            out[pfx + "delta_hotspot_p95_c"] = round(st["p95"], 1)
            status = ("critique" if st["moy"] > DELTA_CRIT_C
                      else "avertissement" if st["moy"] > DELTA_WARN_C else "optimal")
            out[pfx + "delta_hotspot_status"] = status

    th = series.get(f"gpu{gpu_index}_throttle_thermal", [])
    n_th = sum(1 for x in th if x)
    out[pfx + "throttle_thermal_detected"] = n_th > 0
    out[pfx + "throttle_thermal_samples"] = n_th

    if powers:
        pk = peak(powers)
        if pk:
            out[pfx + "power_max_w"] = max(pk)
        st = pick(powers)
        if st: out[pfx + "power_moy_w"] = _stats(st)["moy"]
    for arr, key in (
        (gfx, "freq_graphics"),
        (series.get(f"gpu{gpu_index}_freq_request", []), "freq_request"),
        (mem, "freq_memory"),
        (series.get(f"gpu{gpu_index}_freq_media", []), "freq_media"),
    ):
        st = pick(arr)
        if st:
            out[f"{pfx}{key}_min_mhz"] = min(st)
            out[f"{pfx}{key}_max_mhz"] = max(peak(arr))
    for key, name in (("temp_vr", "temp_vr_max_c"), ("temp_mctrl", "temp_mctrl_max_c"), ("temp_pcie", "temp_pcie_max_c"),
                      ("temp_vram_ch_max", "temp_vram_ch_max_c"),
                      ("fan_rpm", "fan_rpm_max"), ("fan2_rpm", "fan2_rpm_max"), ("fan3_rpm", "fan3_rpm_max"),
                      ("freq_media_request", "freq_media_request_max_mhz")):
        arr = [x for x in peak(series.get(f"gpu{gpu_index}_{key}", [])) if x > 0]   # 0 = capteur absent ou ventilateur passif
        if arr:
            out[pfx + name] = max(arr)
    # Tension GPU (V)
    volt = [x for x in peak(series.get(f"gpu{gpu_index}_voltage_v", [])) if x > 0]
    if volt:
        st_v = [x for x in pick(series.get(f"gpu{gpu_index}_voltage_v", [])) if x > 0]
        if st_v:
            out[pfx + "voltage_moy_v"] = round(sum(st_v) / len(st_v), 3)
        out[pfx + "voltage_max_v"] = round(max(volt), 3)
    # Utilisation du GPU, moteurs et mémoire (GPU Intel via xpu-smi) : absente = non affichée
    for key, name in (("util", "util"), ("util_compute", "util_compute"), ("util_render", "util_render"),
                      ("util_copy", "util_copy"), ("util_media", "util_media"), ("eu_active", "eu_active"),
                      ("eu_stall", "eu_stall"), ("mem_util", "mem_util"), ("mem_bw", "mem_bw"),
                      ("mem_used_mib", "mem_used")):
        arr = series.get(f"gpu{gpu_index}_{key}", [])
        st = pick(arr)
        if st:
            unit = "mib" if key == "mem_used_mib" else "pct"
            out[f"{pfx}{name}_moy_{unit}"] = _stats(st)["moy"]
            out[f"{pfx}{name}_max_{unit}"] = max(peak(arr))
    # Débits mémoire et PCIe (kB/s) si remontés par xpu-smi
    for key in ("mem_read_kbps", "mem_write_kbps", "pcie_read_kbps", "pcie_write_kbps"):
        arr = [x for x in peak(series.get(f"gpu{gpu_index}_{key}", [])) if x >= 0]
        if arr and max(arr) > 0:
            out[f"{pfx}{key}_max"] = round(max(arr), 1)
    # Compteurs d'erreurs RAS / ECC (priorité stabilité/erreurs)
    ras_total = 0.0
    ras_present = False
    for rkey in ("ras_reset", "ras_programming_errors", "ras_driver_errors",
                 "ras_cache_corr", "ras_cache_uncorr", "ras_mem_corr", "ras_mem_uncorr"):
        arr = peak(series.get(f"gpu{gpu_index}_{rkey}", []))
        if arr:
            ras_present = True
            rmax = int(max(arr))
            out[f"{pfx}{rkey}"] = rmax
            ras_total += rmax
    if ras_present:
        out[pfx + "ras_errors_total"] = int(ras_total)
    # Alias historiques utilisés par les anciens exports
    if pfx + "freq_graphics_min_mhz" in out:
        out[pfx + "freq_min_mhz"] = out[pfx + "freq_graphics_min_mhz"]
        out[pfx + "freq_max_mhz"] = out[pfx + "freq_graphics_max_mhz"]
    return out
