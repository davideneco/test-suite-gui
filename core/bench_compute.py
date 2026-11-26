# -*- coding: utf-8 -*-
"""Benchmarks de calcul et mémoire multi-marques (STREAM, VRAM BW, DCGM, clpeak, vkpeak, oclburn)."""

import json
import os
import platform
try:
    import psutil
except ImportError:
    psutil = None
import queue
import re
import shlex
import shutil
import statistics
import subprocess
import threading
import time

from core.bench_nvidia import bench_gpu_nvbandwidth, parse_nvbandwidth_text_output
from core.bench_sensors import _find_hwmon_dir, _poll_gpu_sensors
from core.bench_utils import (
    BenchmarkResult,
    ProgressCb,
    SeriesCb,
    _OCLBURN_SRC,
    _add_gpu_series_metrics,
    _candidate_user_homes,
    _find_tool_path,
    _normalize_gpu_list,
    _run,
    _terminate_process_tree,
    get_system_info,
    nvidia_smi_usable,
    tool_exists,
    with_fault_watch,
)

_STREAM_SRC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "third_party", "stream", "stream.c")


def _parse_size_bytes(txt: str) -> int:
    """Convertit « 1 GiB », « 32 MiB », « 512 KiB » en octets (0 si illisible)."""
    m = re.match(r"\s*([\d.]+)\s*([KMGT]i?B)", txt or "", re.I)
    if not m:
        return 0
    mult = {"K": 1 << 10, "M": 1 << 20, "G": 1 << 30, "T": 1 << 40}[m.group(2)[0].upper()]
    return int(float(m.group(1)) * mult)


def _l3_total_bytes() -> int:
    """Taille totale du cache L3 (toutes instances) via lscpu ; 0 si inconnue."""
    try:
        r = subprocess.run(["lscpu"], capture_output=True, text=True, timeout=5, env={**os.environ, "LC_ALL": "C", "LANG": "C"})
        for line in r.stdout.splitlines():
            if line.strip().startswith("L3 cache"):
                return _parse_size_bytes(line.split(":", 1)[1])
    except Exception:
        pass
    return 0


def _stream_array_elements(array_mb: int = 0) -> int:
    """Taille de tableau STREAM : ≥ 4× le cache total (règle officielle), bornée à 25 % de la RAM libre."""
    if array_mb and array_mb > 0:
        return int(array_mb) * 1024 * 1024 // 8
    want = max(100_000_000, (4 * _l3_total_bytes()) // 8)
    try:
        avail = psutil.virtual_memory().available
        cap = int(avail * 0.25) // (3 * 8)        # 3 tableaux de 8 octets/élément
        want = min(want, max(10_000_000, cap))
    except Exception:
        pass
    return want


def _stream_binary(elements: int, ntimes: int = 20) -> tuple:
    """Retourne (chemin binaire, message d'erreur) : binaire « stream » du PATH, sinon compilation avec gcc."""
    sys_bin = shutil.which("stream") or shutil.which("stream_c.exe")
    if sys_bin:
        return sys_bin, ""
    gcc = shutil.which("gcc") or shutil.which("cc")
    if not gcc:
        return "", "gcc absent : sudo apt install gcc (ou fournir un binaire « stream » dans le PATH)"
    if not os.path.isfile(_STREAM_SRC):
        return "", "third_party/stream/stream.c introuvable"
    cache = os.path.expanduser("~/.cache/benchsuite/stream")
    os.makedirs(cache, exist_ok=True)
    out = os.path.join(cache, f"stream_{platform.machine()}_{elements}_{ntimes}")
    if os.path.isfile(out) and os.path.getmtime(out) >= os.path.getmtime(_STREAM_SRC):
        return out, ""
    flags = ["-O3", "-fopenmp", f"-DSTREAM_ARRAY_SIZE={elements}", f"-DNTIMES={ntimes}"]
    if platform.machine() in ("x86_64", "AMD64"):
        flags += ["-march=native", "-mcmodel=medium"]       # tableaux statiques > 2 Go
    rc, o, e = _run([gcc] + flags + [_STREAM_SRC, "-o", out], timeout=120)
    if rc != 0 and "-march=native" in flags:                # repli : sans options spécifiques CPU
        flags = [f for f in flags if f != "-march=native"]
        rc, o, e = _run([gcc] + flags + [_STREAM_SRC, "-o", out], timeout=120)
    if rc != 0 or not os.path.isfile(out):
        return "", "compilation de STREAM impossible : " + (e or o)[-300:]
    return out, ""


@with_fault_watch
def bench_ram_stream(
    threads: int = 0,
    array_mb: int = 0,
    ntimes: int = 20,
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    """Bande passante mémoire (STREAM, McCalpin) : Copy / Scale / Add / Triad en GB/s.

    threads=0 → cœurs physiques ; array_mb=0 → taille automatique (≥ 4× le cache L3 total).
    Compile `third_party/stream/stream.c` avec gcc au premier lancement (cache ~/.cache/benchsuite).
    """
    t0 = time.time()
    elements = _stream_array_elements(array_mb)
    if cb: cb(5, f"Préparation de STREAM ({elements * 8 // (1 << 20)} Mo par tableau)…")
    binary, err = _stream_binary(elements, ntimes)
    if not binary:
        return BenchmarkResult("RAM — Bande passante (STREAM)", "CPU + RAM", None, "GB/s", 0, err, status="skipped",
                               metrics={"erreur": err})
    try:
        nthreads = int(threads) if threads and int(threads) > 0 else (psutil.cpu_count(logical=False) or os.cpu_count() or 1)
    except Exception:
        nthreads = os.cpu_count() or 1
    env = os.environ.copy()
    env.update({"OMP_NUM_THREADS": str(nthreads), "OMP_PROC_BIND": "spread", "OMP_PLACES": "cores"})
    cmd_str = f"OMP_NUM_THREADS={nthreads} OMP_PROC_BIND=spread OMP_PLACES=cores {binary}"
    if cb: cb(20, f"STREAM sur {nthreads} thread(s)…")
    try:
        proc = subprocess.Popen([binary], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
        deadline = time.time() + 900
        out = ""
        while True:
            try:
                out, _ = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                if (kill_event and kill_event.is_set()) or time.time() > deadline:
                    proc.kill()
                    out, _ = proc.communicate()
                    break
        rc = proc.returncode
    except Exception as e:
        return BenchmarkResult("RAM — Bande passante (STREAM)", "CPU + RAM", None, "GB/s", time.time() - t0, str(e),
                               status="error", metrics={"erreur": str(e)}, command=cmd_str)
    rates = {}
    for m in re.finditer(r"^(Copy|Scale|Add|Triad):\s+([0-9.]+)", out, re.M):
        rates[m.group(1).lower()] = round(float(m.group(2)) / 1000.0, 2)     # MB/s → GB/s
    valid = "Solution Validates" in out
    mt = re.search(r"Number of Threads counted\s*=\s*(\d+)", out)
    metrics = {"array_elements": elements, "array_mb": elements * 8 // (1 << 20), "threads": nthreads,
               "ntimes": ntimes, "command": cmd_str,
               "validation": "OK" if valid else "ÉCHEC / non confirmée"}
    if mt:
        metrics["threads_counted"] = int(mt.group(1))
    for k, v in rates.items():
        metrics[f"{k}_gbs"] = v
    score = rates.get("triad")
    status = "ok" if (score and valid and rc == 0) else ("stopped" if (kill_event and kill_event.is_set()) else "error")
    notes = ("Exécution basée sur le code STREAM (John D. McCalpin), non certifiée selon les Run Rules : "
             "à citer comme « résultats basés sur STREAM ».")
    if cb: cb(100, "Terminé")
    return BenchmarkResult("RAM — Bande passante (STREAM)", "CPU + RAM", score, "GB/s", time.time() - t0, out,
                           metrics=metrics, status=status, system_info=get_system_info(), notes=notes, command=cmd_str)


# ── GPU — mémoire : bande passante VRAM (nvbandwidth) et intégrité (gpu-burn) ─────
def _nvbandwidth_local_values(text: str) -> list:
    """Valeurs GB/s par GPU du test device_local_copy (1 ligne : colonne = GPU ; sinon diagonale)."""
    for tc in parse_nvbandwidth_text_output(text):
        mat = (tc.get("results") or {}).get("bandwidth_matrix") or []
        if not mat:
            continue
        if len(mat) == 1:
            return [v for v in mat[0] if isinstance(v, (int, float))]
        return [row[i] for i, row in enumerate(mat) if i < len(row) and isinstance(row[i], (int, float))]
    return []


_UNIT_SCALE = {"TFLOPS": ("GFLOPS", 1000.0), "GFLOPS": ("GFLOPS", 1.0), "MFLOPS": ("GFLOPS", 0.001),
               "TOPS": ("GIOPS", 1000.0), "GOPS": ("GIOPS", 1.0), "TIOPS": ("GIOPS", 1000.0), "GIOPS": ("GIOPS", 1.0),
               "TB/S": ("GB/s", 1000.0), "GB/S": ("GB/s", 1.0), "MB/S": ("GB/s", 0.001), "GBPS": ("GB/s", 1.0),
               "MS": ("us", 1000.0), "US": ("us", 1.0), "NS": ("us", 0.001)}


def parse_clpeak_generic(text: str) -> list:
    """Analyse la sortie de clpeak, ancienne (« Device: », « (GBPS) ») ou récente (« Backend: », « GB/s »).

    Retourne [{'name', 'backend', 'index', 'sections': {titre: {mesure: (valeur, unité)}}}] ; valeurs
    converties en GFLOPS / GIOPS / GB/s / us.
    """
    devices, cur, backend, section, sec_unit = [], None, "OpenCL", "", ""
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = re.match(r"^Backend:\s*(.+)$", line)
        if m:
            backend, cur, section = m.group(1).strip(), None, ""
            continue
        m = re.match(r"^\s*Device(?:\s+(\d+))?:\s*(.+)$", line)
        if m:
            cur = {"name": m.group(2).strip(), "backend": backend, "index": int(m.group(1)) if m.group(1) else len(devices),
                   "sections": {}}
            devices.append(cur)
            section = ""
            continue
        if cur is None or re.match(r"^\s*Platform", line):
            continue
        m = re.match(r"^\s+([A-Za-z0-9_./ +\-]+?)\s+:\s*([0-9]+(?:\.[0-9]+)?)\s*([A-Za-z/]+)?\s*$", line)
        if m and section:
            unit = (m.group(3) or sec_unit or "").upper()
            norm, factor = _UNIT_SCALE.get(unit, (m.group(3) or sec_unit or "", 1.0))
            cur["sections"][section][m.group(1).strip()] = (round(float(m.group(2)) * factor, 3), norm)
            continue
        if ":" not in line and line.startswith(" "):        # titre de test
            mh = re.match(r"^\s+(.+?)\s*(?:\(([A-Za-z/]+)\))?\s*$", line)
            if mh:
                section = mh.group(1).strip()
                sec_unit = mh.group(2) or ""
                cur["sections"].setdefault(section, {})
    return devices


def parse_clpeak_devices(text: str) -> list:
    """Bande passante mémoire globale par périphérique : [{'name', 'backend', 'global_bw_gbs'}]."""
    out = []
    for d in parse_clpeak_generic(text):
        vals = [v for sec, meas in d["sections"].items() if re.search(r"global memory bandwidth", sec, re.I)
                for k, (v, u) in meas.items() if u == "GB/s" and k.lower().startswith("float")]
        if vals:
            out.append({"name": d["name"], "backend": d["backend"], "global_bw_gbs": max(vals)})
    return out


def bench_gpu_vram_bandwidth(
    buffer_size_mb: int = 2048,
    iterations: int = 5,
    target_gpus: str = "",
    method: str = "auto",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    """Bande passante de la mémoire GPU (VRAM).

    method : "nvbandwidth" (NVIDIA, copie locale par GPU) · "clpeak" (OpenCL, toutes marques) ·
    "both" (les deux, avec comparaison) · "auto" (nvbandwidth si présent, sinon clpeak).
    Les valeurs par GPU sont exportées en `gpuN_vram_bw_gbs` (nvbandwidth prioritaire, sinon clpeak
    dans l'ordre OpenCL) ; toutes les mesures clpeak sont aussi en `clpeak_devN_*`.
    """
    has_nv, has_cl = tool_exists("nvbandwidth"), tool_exists("clpeak")
    method = (method or "auto").lower()
    if method == "auto":
        method = "nvbandwidth" if has_nv else "clpeak"
    use_nv = method in ("nvbandwidth", "both")
    use_cl = method in ("clpeak", "both")
    missing = [n for n, use, ok in (("nvbandwidth", use_nv, has_nv), ("clpeak", use_cl, has_cl)) if use and not ok]
    if missing and (method != "both" or len(missing) == 2):
        msg = "Outil manquant : " + ", ".join(missing) + " (nvbandwidth : github.com/NVIDIA/nvbandwidth ; clpeak : sudo apt install clpeak)"
        return BenchmarkResult("GPU — Bande passante VRAM", "GPU", None, "GB/s", 0, "", status="skipped",
                               metrics={"erreur": msg})
    t0 = time.time()
    metrics: dict = {"vram_bw_method": method}
    raws, cmds, notes = [], [], []
    nv_vals, cl_devs = [], []
    status = "ok"
    series, sysinfo = {}, {}
    if use_nv and has_nv:
        inner = bench_gpu_nvbandwidth(buffer_size_mb=buffer_size_mb, iterations=iterations, target_gpus=target_gpus,
                                      extra_args="-t device_local_copy", cb=cb, kill_event=kill_event, series_cb=series_cb)
        raws.append(inner.raw_output or ""); cmds.append(inner.command); sysinfo = inner.system_info
        nv_vals = _nvbandwidth_local_values(inner.raw_output or "")
        for k, v in (inner.metrics or {}).items():
            if re.match(r"^gpu\d+_", str(k)) or k == "series" or str(k).startswith("series_"):
                metrics[k] = v
        if inner.status in ("stopped", "error", "skipped") and not nv_vals:
            status = inner.status
        if not nv_vals:
            notes.append("nvbandwidth : device_local_copy absent de cette version (nvbandwidth -l) ou sortie illisible")
    if use_cl and has_cl and status != "stopped" and not (kill_event and kill_event.is_set()):
        inner = bench_clpeak(target_gpus=target_gpus, test_mode="global-bandwidth", cb=cb, kill_event=kill_event,
                             series_cb=series_cb if not use_nv else None)
        raws.append(inner.raw_output or ""); cmds.append(inner.command)
        sysinfo = sysinfo or inner.system_info
        cl_devs = parse_clpeak_devices(inner.raw_output or "")
        if not cl_devs:
            notes.append("clpeak : aucune mesure de bande passante globale lue")
        if not use_nv:
            for k, v in (inner.metrics or {}).items():
                if re.match(r"^gpu\d+_", str(k)) or k == "series" or str(k).startswith("series_"):
                    metrics[k] = v
    for i, v in enumerate(nv_vals):
        metrics[f"gpu{i}_vram_bw_gbs"] = round(v, 1)
        metrics[f"gpu{i}_vram_bw_nvbandwidth_gbs"] = round(v, 1)
    for i, d in enumerate(cl_devs):
        metrics[f"clpeak_dev{i}_name"] = d["name"]
        metrics[f"clpeak_dev{i}_gbs"] = round(d["global_bw_gbs"], 1)
        if not nv_vals:                      # pas de nvbandwidth : clpeak alimente la valeur par GPU (ordre OpenCL)
            metrics[f"gpu{i}_vram_bw_gbs"] = round(d["global_bw_gbs"], 1)
    if nv_vals and cl_devs and len(nv_vals) == len(cl_devs):
        ratios = [d["global_bw_gbs"] / v for d, v in zip(cl_devs, nv_vals) if v]
        if ratios:
            metrics["clpeak_vs_nvbandwidth_ratio"] = round(statistics.median(ratios), 2)
    vals = nv_vals or [d["global_bw_gbs"] for d in cl_devs]
    score = round(statistics.median(vals), 1) if vals else None
    metrics["vram_bw_source"] = "nvbandwidth" if nv_vals else ("clpeak" if cl_devs else "aucune")
    if status == "ok" and not score:
        status = "error"
    if notes:
        metrics["erreur"] = " ; ".join(notes)
    return BenchmarkResult("GPU — Bande passante VRAM", "GPU", score, "GB/s", time.time() - t0, "\n".join(raws),
                           metrics=metrics, status=status, system_info=sysinfo, notes=" ; ".join(notes),
                           command=" && ".join(c for c in cmds if c))


DCGM_LEVEL_INFO = {1: ("rapide", 60, 120), 2: ("moyen", 240, 900), 3: ("long", 1200, 3600), 4: ("étendu", 5400, 14400)}


def parse_dcgm_json(text: str) -> dict:
    """Analyse la sortie `dcgmi diag -j` : {'tests': [...], 'counts': {...}, 'failures': [...]}.

    Structure (sources NVIDIA/DCGM, dcgmi/Diag.cpp) : {"DCGM Diagnostic": {"test_categories": [{"category", "tests":
    [{"name", "results": [{"entity_group", "entity_id", "status", "warnings": [{"warning"}], "info": []}],
    "test_summary": {"status"}}]}]}}. Parcours tolérant : versions/clés légèrement différentes acceptées.
    """
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        data = json.loads(text[start:end + 1])
    except ValueError:
        return {}
    tests, counts, failures = [], {"pass": 0, "fail": 0, "warn": 0, "skip": 0}, []

    def norm(st: str) -> str:
        st = (st or "").strip().lower()
        return "pass" if st.startswith("pass") else "fail" if st.startswith("fail") else \
            "warn" if st.startswith("warn") else "skip" if st.startswith(("skip", "not run")) else ""

    def walk(node, category=""):
        if isinstance(node, dict):
            if isinstance(node.get("tests"), list):
                cat = node.get("category", category)
                for t in node["tests"]:
                    if isinstance(t, dict) and "name" in t:
                        add_test(t, cat)
                return
            for v in node.values():
                walk(v, category)
        elif isinstance(node, list):
            for v in node:
                walk(v, category)

    def add_test(t: dict, cat: str):
        results, per_gpu, per_info = t.get("results") or [], {}, {}
        rank = {"": 0, "skip": 1, "pass": 2, "warn": 3, "fail": 4}
        summary = norm((t.get("test_summary") or {}).get("status", ""))
        worst = ""
        for r in results:
            st = norm(r.get("status", ""))
            gid = r.get("entity_id", r.get("gpu_id", ""))
            grp = str(r.get("entity_group", "GPU"))
            label = f"GPU {gid}" if grp.upper() == "GPU" else f"{grp} {gid}"
            per_gpu[label] = st
            if r.get("info"):
                per_info[label] = [str(x) for x in r["info"]]
            for w in r.get("warnings") or []:
                msg = w.get("warning", w) if isinstance(w, dict) else str(w)
                eid = f" [erreur {w['error_id']}]" if isinstance(w, dict) and w.get("error_id") is not None else ""
                if st in ("fail", "warn"):
                    failures.append(f"{t['name']} — {label} : {str(msg)[:200]}{eid}")
            if rank.get(st, 0) > rank[worst]:
                worst = st
        final = summary or worst
        if final in counts:
            counts[final] += 1
        tests.append({"category": cat, "name": t["name"], "status": final or "?", "gpus": per_gpu, "info": per_info})

    walk(data)
    return {"tests": tests, "counts": counts, "failures": failures}


def _dcgm_ready(cb: ProgressCb = None) -> tuple:
    """Vérifie que nv-hostengine répond ; tente de le démarrer (service nvidia-dcgm ou nv-hostengine)."""
    rc, out, err = _run(["dcgmi", "discovery", "-l"], timeout=15)
    if rc == 0:
        return True, ""
    from core.safety import system_changes_allowed
    if not system_changes_allowed():
        return False, ("le service DCGM (nv-hostengine) ne répond pas et BenchSuite ne le démarre pas en mode lecture seule. "
                       "Démarrez-le vous-même : sudo systemctl start nvidia-dcgm (détail : " + (err or out)[:160] + ")")
    if cb: cb(3, "Démarrage du service DCGM…")
    attempts = []
    if shutil.which("systemctl"):
        pre = [] if os.geteuid() == 0 else (["sudo", "-n"] if shutil.which("sudo") else [])
        attempts.append(pre + ["systemctl", "start", "nvidia-dcgm"])
    if _find_tool_path("nv-hostengine"):
        attempts.append([_find_tool_path("nv-hostengine")])
    for cmd in attempts:
        _run(cmd, timeout=20)
        time.sleep(2.0)
        rc, _, _ = _run(["dcgmi", "discovery", "-l"], timeout=15)
        if rc == 0:
            return True, ""
    return False, ("dcgmi ne peut pas joindre le service DCGM (nv-hostengine). Démarrez-le : "
                   "sudo systemctl --now enable nvidia-dcgm — détail : " + (err or out)[:160])


def _ensure_gpu_persistence() -> tuple:
    """Active le mode persistance NVIDIA si besoin (root ou sudo -n). Retourne (ok, note).

    DCGM (test « software ») échoue sinon, et les autres tests ne sont alors pas exécutés.
    """
    if not nvidia_smi_usable():
        return True, ""
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=persistence_mode", "--format=csv,noheader"], timeout=10)
    if rc != 0 or "disabled" not in out.lower():
        return True, ""
    from core.safety import system_changes_allowed
    if not system_changes_allowed():
        return False, ("mode persistance désactivé : non modifié (mode lecture seule) ; "
                       "avertissement DCGM n°29 ignoré pour ce test")
    pre = [] if os.geteuid() == 0 else (["sudo", "-n"] if shutil.which("sudo") else None)
    if pre is not None:
        _run(pre + ["nvidia-smi", "-pm", "1"], timeout=20)
        rc, out, _ = _run(["nvidia-smi", "--query-gpu=persistence_mode", "--format=csv,noheader"], timeout=10)
        if rc == 0 and "disabled" not in out.lower():
            return True, "mode persistance activé automatiquement (nvidia-smi -pm 1)"
    return False, ("mode persistance désactivé et impossible à activer sans droits : sudo nvidia-smi -pm 1 "
                   "(avertissement DCGM n°29 ignoré pour ce test)")


def _pcie_min_width() -> int:
    """Plus petite largeur de lien PCIe possible (nvidia-smi pcie.link.width.max) parmi les GPU ; 16 si inconnue.

    Sert à régler le seuil « min_pci_width » de DCGM (16 par défaut) : des GPU câblés en x4/x8 (MCIO, riser)
    feraient sinon échouer le test PCIe alors que la liaison est conforme à la conception du châssis.
    """
    if not nvidia_smi_usable():
        return 16
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=pcie.link.width.max,pcie.link.width.current",
                       "--format=csv,noheader,nounits"], timeout=10)
    widths = []
    if rc == 0:
        for line in out.splitlines():
            parts = [x.strip() for x in line.split(",")]
            for x in parts[:1]:
                if x.isdigit():
                    widths.append(int(x))
    return min(widths) if widths else 16


def _dcgm_runtime_error(text: str) -> str:
    """Retourne le message « runtime_error » d'une sortie `dcgmi diag -j` ('' si absent)."""
    m = re.search(r'"runtime_error"\s*:\s*"((?:[^"\\]|\\.)*)"', text or "")
    return m.group(1).replace("\\n", "\n") if m else ""


def _nvidia_gpu_indices() -> list:
    """Indices des GPU vus par nvidia-smi ([] si indisponible)."""
    if not nvidia_smi_usable():
        return []
    rc, out, _ = _run(["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"], timeout=10)
    return [int(x) for x in out.split() if x.strip().isdigit()] if rc == 0 else []


def _dcgm_no_entity_hint() -> str:
    """Explique et aide à corriger « No available testing entities » (GPU invisibles pour DCGM)."""
    smi = len(_nvidia_gpu_indices())
    rc, out, _ = _run(["dcgmi", "discovery", "-l"], timeout=15)
    seen = len(re.findall(r"^\|\s*GPU\s+\d+", out or "", re.M))
    base = f"nvidia-smi voit {smi} GPU, DCGM en voit {seen}. "
    # DCGM installé en snap : le confinement l'empêche d'accéder aux GPU → paquet apt officiel requis
    try:
        if os.path.realpath(shutil.which("dcgmi") or "").startswith("/snap/") or (shutil.which("dcgmi") or "").startswith("/snap/"):
            return base + ("DCGM est installé en snap (confiné, ne voit pas les GPU). Remplacez-le : sudo snap remove dcgm ; "
                           "sudo pkill nv-hostengine ; sudo apt install datacenter-gpu-manager-4-cuda13 (dépôt NVIDIA cuda-keyring) ; "
                           "sudo systemctl --now enable nvidia-dcgm.")
    except Exception:
        pass
    # Paquet DCGM « cudaNN » incompatible avec le CUDA du pilote (ex. -cuda12 avec un pilote CUDA 13)
    try:
        _, smi_out, _ = _run(["nvidia-smi"], timeout=10)
        mc = re.search(r"CUDA Version:\s*(\d+)", smi_out or "")
        _, pk, _ = _run(["dpkg-query", "-W", "-f=${Package}\\n", "datacenter-gpu-manager-4-cuda*"], timeout=10)
        mp = re.search(r"cuda(\d+)", pk or "")
        if mc and mp and mc.group(1) != mp.group(1):
            return base + (f"Le paquet DCGM installé (cuda{mp.group(1)}) ne correspond pas au pilote (CUDA {mc.group(1)}) : "
                           f"sudo apt install datacenter-gpu-manager-4-cuda{mc.group(1)} puis sudo systemctl restart nvidia-dcgm.")
    except Exception:
        pass
    if smi and seen < smi:
        return base + ("Redémarrez le service après tout changement PCIe/GPU : sudo systemctl restart nvidia-dcgm "
                       "(ou arrêtez nv-hostengine puis relancez). Si l'écart persiste : sudo reboot.")
    if not smi:
        return base + "Aucun GPU NVIDIA actif : vérifiez le pilote (nvidia-smi) et le câblage PCIe/MCIO."
    return base + ("GPU visibles mais non testables : libérez-les (autres processus, MIG, mode de calcul) "
                   "ou essayez sudo systemctl restart nvidia-dcgm.")


@with_fault_watch
def bench_gpu_dcgm(
    level: int = 3,
    tests: str = "",
    target_gpus: str = "",
    fail_early: bool = True,
    pcie_auto: bool = True,
    extra_args: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    """Diagnostic matériel NVIDIA DCGM (`dcgmi diag`) : niveaux 1 (secondes) à 4 (plus d'une heure).

    Le niveau 3 (~15 min) teste notamment la mémoire GPU, la charge de calcul, le PCIe, la bande passante
    et le stress ciblé. `tests` permet de choisir des modules précis (ex. « memory,pcie »).
    Échec d'un test = statut « error » ; avertissements dans les notes et le verdict.
    """
    target_gpus = _normalize_gpu_list(target_gpus)
    name = "GPU — Diagnostic DCGM"
    if not tool_exists("dcgmi"):
        msg = "DCGM non installé (sudo apt install datacenter-gpu-manager-4-cuda12 ; voir documentation/GUIDE_UTILISATION.md)"
        return BenchmarkResult(name, "GPU", None, "% OK", 0, "", status="skipped", metrics={"erreur": msg})
    try:
        level = max(1, min(int(level), 4))
    except (TypeError, ValueError):
        level = 3
    t0 = time.time()
    ready, why = _dcgm_ready(cb)
    if not ready:
        return BenchmarkResult(name, "GPU", None, "% OK", time.time() - t0, why, status="skipped", metrics={"erreur": why})
    pm_ok, pm_note = _ensure_gpu_persistence()
    cmd = ["dcgmi", "diag", "-r", (tests.strip() or str(level)), "-j"]
    if not pm_ok and "--ignoreErrorCodes" not in str(extra_args):
        cmd += ["--ignoreErrorCodes", "29"]
    pcie_w = 16
    if pcie_auto and level >= 2 or (tests.strip() and "pcie" in tests):
        pcie_w = _pcie_min_width() if pcie_auto else 16
        if pcie_w < 16 and "min_pci_width" not in str(extra_args):
            cmd += ["-p", f"pcie.min_pci_width={pcie_w}"]
    if target_gpus:
        cmd += ["-i", target_gpus]
    if fail_early:
        cmd.append("--fail-early")
    if extra_args:
        try:
            cmd += shlex.split(str(extra_args))
        except ValueError:
            cmd += str(extra_args).split()
    cmd_str = shlex.join(cmd)
    label, est_s, max_s = DCGM_LEVEL_INFO[level]
    if tests.strip():
        label, est_s, max_s = "ciblé", 600, 7200
    series: dict = {"t": []}
    stop_event = threading.Event()
    poll = threading.Thread(target=_poll_gpu_sensors, args=(series, stop_event, t0, series_cb, target_gpus), daemon=True)
    poll.start()
    if cb: cb(5, f"DCGM niveau {level} ({label}, ≈ {est_s // 60 or 1} min)…")
    def run_once(command: list) -> tuple:
        """Lance dcgmi une fois ; retourne (sortie, stderr, arrêté, délai_dépassé)."""
        o = e = ""
        kl = to = False
        proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
        t_run = time.time()
        while True:
            try:
                o, e = proc.communicate(timeout=1.0)
                break
            except subprocess.TimeoutExpired:
                el = time.time() - t_run
                if kill_event and kill_event.is_set():
                    kl = True
                    _terminate_process_tree(proc, grace=2.0)
                    o, e = proc.communicate()
                    break
                if el > max_s:
                    to = True
                    _terminate_process_tree(proc, grace=2.0)
                    o, e = proc.communicate()
                    break
                if cb: cb(min(92, int(8 + 84 * el / est_s)), f"DCGM niveau {level} en cours… {int(el)} s")
        return o, e, kl, to

    out = err = ""
    killed = timed_out = False
    try:
        out, err, killed, timed_out = run_once(cmd)
        # DCGM 4.x : « No available testing entities » quand la sélection par défaut (*,cpu:*) ne
        # trouve rien ; on réessaie avec la liste explicite des GPU vus par nvidia-smi.
        if not target_gpus and not killed and _dcgm_runtime_error(out) and "-i" not in cmd:
            idx = _nvidia_gpu_indices()
            if idx:
                cmd = cmd[:cmd.index("-j") + 1] + ["-i", ",".join(map(str, idx))] + cmd[cmd.index("-j") + 1:]
                cmd_str = shlex.join(cmd)
                if cb: cb(6, "DCGM : nouvel essai avec la liste explicite des GPU…")
                out, err, killed, timed_out = run_once(cmd)
    except Exception as exc:
        stop_event.set(); poll.join(timeout=2.0)
        return BenchmarkResult(name, "GPU", None, "% OK", time.time() - t0, str(exc), status="error",
                               metrics={"erreur": str(exc)}, command=cmd_str)
    stop_event.set(); poll.join(timeout=3.0)
    dur = time.time() - t0
    parsed = parse_dcgm_json(out)
    metrics: dict = {"dcgm_level": level, "command": cmd_str, "duration_s": round(dur, 1)}
    notes = ""
    if pm_note:
        metrics["persistence_mode"] = pm_note
    if pcie_w < 16:
        metrics["dcgm_pcie_min_width"] = pcie_w
        metrics["pcie_note"] = f"seuil PCIe DCGM adapté à la liaison réelle x{pcie_w} (défaut DCGM : x16)"
    try:
        md = json.loads(out[out.find("{"):out.rfind("}") + 1]).get("metadata", {})
        if md.get("version"): metrics["dcgm_version"] = md["version"]
        if md.get("Driver Version Detected"): metrics["dcgm_driver"] = md["Driver Version Detected"]
    except ValueError:
        pass
    if parsed.get("tests"):
        c = parsed["counts"]
        metrics.update({"dcgm_pass": c["pass"], "dcgm_fail": c["fail"], "dcgm_warn": c["warn"], "dcgm_skip": c["skip"],
                        "dcgm_tests": len(parsed["tests"])})
        gpu_fail: dict = {}
        for t in parsed["tests"]:
            slug = re.sub(r"[^a-z0-9]+", "_", t["name"].lower()).strip("_")
            metrics[f"dcgm_{slug}"] = t["status"]
            for label_g, st in t["gpus"].items():
                m = re.match(r"GPU (\d+)", label_g)
                if m and st in ("fail", "warn"):
                    gpu_fail.setdefault(int(m.group(1)), []).append(f"{t['name']}:{st}")
        for gi, lst in gpu_fail.items():
            metrics[f"gpu{gi}_dcgm_issues"] = ", ".join(lst)
        for t in parsed["tests"]:
            for label_g, infos in (t.get("info") or {}).items():
                mg = re.match(r"GPU (\d+)", label_g)
                if not mg:
                    continue
                gi = int(mg.group(1))
                for line_i in infos:
                    ml = re.search(r"(GPU to Host|Host to GPU|bidirectional) latency:\s*([0-9.]+)\s*us", line_i)
                    if ml:
                        key = {"GPU to Host": "d2h", "Host to GPU": "h2d", "bidirectional": "bidir"}[ml.group(1)]
                        metrics[f"gpu{gi}_dcgm_pcie_{key}_latency_us"] = float(ml.group(2))
                    ma = re.search(r"Allocated\s+(\d+)\s+bytes\s+\(([0-9.]+)%\)", line_i)
                    if ma:
                        metrics[f"gpu{gi}_dcgm_mem_tested_gb"] = round(int(ma.group(1)) / 1e9, 1)
        metrics["dcgm_results"] = parsed["tests"]
        if parsed["failures"]:
            metrics["dcgm_failures"] = parsed["failures"][:40]
            notes = " | ".join(parsed["failures"][:5])
        done = c["pass"] + c["fail"] + c["warn"]
        score = round(100.0 * c["pass"] / done, 1) if done else None
        status = "error" if c["fail"] else "ok"
    else:
        score, status = None, "error"
        rte = _dcgm_runtime_error(out)
        notes = (rte or err.strip() or out.strip() or "sortie DCGM illisible")[:300]
        if "No available testing entities" in rte or "(-30)" in rte:
            notes = "DCGM ne trouve aucun GPU à tester. " + _dcgm_no_entity_hint()
        metrics["erreur"] = notes
    if killed:
        status, notes = "stopped", notes or "arrêté"
    if timed_out:
        status, notes = "error", f"délai dépassé ({max_s} s) : {notes}"
    _add_gpu_series_metrics(metrics, series)
    metrics["series"] = series
    if cb: cb(100, "Terminé")
    return BenchmarkResult(name, "GPU", score, "% OK", dur, (out + ("\n--- stderr ---\n" + err if err.strip() else ""))[:60000],
                           metrics=metrics, status=status, system_info=get_system_info(), notes=notes, command=cmd_str)


def _stream_peak_tool(cmd: list, env: dict, parse_line, label: str,
                      cb: ProgressCb, kill_event, series_cb, target_gpus: str,
                      time_s: int = 0, passes: "list | None" = None, relaunch: bool = True, grace_s: int = 60):
    """Exécute un outil de crête (clpeak/vkpeak) en flux avec télémétrie GPU.

    `parse_line(line)` est appelé pour chaque ligne de sortie. Retourne
    (returncode, sortie_brute, series, durée_s, interrompu).

    `relaunch=False` : un seul lancement (outil qui gère lui-même sa durée, ex. oclburn) ; `time_s` sert alors à la progression
    et au garde-fou d'arrêt (à `time_s + grace_s`). Après un arrêt demandé, les dernières lignes de l'outil (bilan) sont récupérées.

    `time_s` > 0 : mode « chauffe » — l'outil est relancé passage après passage jusqu'à `time_s` secondes, la télémétrie
    GPU restant continue sur toute la durée. Chaque passage terminé est ajouté (texte brut) à `passes` ; un dernier
    passage en cours est interrompu au plus tard 60 s après l'échéance et n'est pas compté.
    """
    series = {"t": []}
    stop_event = threading.Event()
    t0 = time.time()
    poll_thread = threading.Thread(target=_poll_gpu_sensors,
                                   args=(series, stop_event, t0, series_cb, target_gpus), daemon=True)
    poll_thread.start()
    lines, killed, rc = [], False, -3
    max_s = max(900, int(time_s) + 600)         # garde-fou global
    n_all, n_pass = 0, 0
    try:
        while True:
            pass_lines: list = []
            n_pass += 1
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, env=env)
            # Lecture dans un thread pour pouvoir tester kill_event sans bloquer sur readline()
            q: "queue.Queue" = queue.Queue()
            threading.Thread(target=lambda p=proc, qq=q: ([qq.put(l) for l in p.stdout], qq.put(None)),
                             daemon=True).start()
            done, n, truncated = False, 0, False
            while not done:
                if kill_event and kill_event.is_set():
                    proc.terminate()
                    killed = True
                    break
                if time_s and time.time() - t0 > time_s + grace_s:
                    proc.terminate()                 # échéance dépassée : passage en cours abandonné
                    truncated = True
                    break
                try:
                    line = q.get(timeout=0.5)
                except queue.Empty:
                    if time.time() - t0 > max_s:
                        proc.kill()
                        truncated = True
                        break
                    continue
                if line is None:
                    done = True
                    break
                lines.append(line)
                pass_lines.append(line)
                if parse_line(line):
                    n += 1
                    n_all += 1
                    if cb:
                        pct = (time.time() - t0) / time_s * 90 if time_s else 10 + n_all * 4
                        cb(min(90, max(5, pct)), (line.strip()[:60] or label) + (f"  [passage {n_pass}]" if (time_s and relaunch) else ""))
            if killed or truncated:       # l'outil écrit son bilan en s'arrêtant : on le récupère (au plus 8 s)
                end_drain = time.time() + 8
                while time.time() < end_drain:
                    try:
                        last = q.get(timeout=0.3)
                    except queue.Empty:
                        if proc.poll() is not None:
                            break
                        continue
                    if last is None:
                        break
                    lines.append(last)
                    pass_lines.append(last)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
            rc = proc.returncode if proc.returncode is not None else -3
            if done and passes is not None and n:
                passes.append("".join(pass_lines))
            # Nouveau passage seulement si : mode chauffe, pas d'arrêt demandé, passage réussi et productif, échéance non atteinte
            if not (relaunch and time_s and done and not killed and not truncated and rc == 0 and n and time.time() - t0 < time_s):
                break
            lines.append(f"\n# --- passage {n_pass + 1} ---\n")
    except FileNotFoundError:
        lines.append(f"Introuvable : {cmd[0]}\n")
        rc = -1
    except Exception as e:
        lines.append(f"Erreur : {e}\n")
    finally:
        stop_event.set()
        poll_thread.join(timeout=2.0)
    return rc, "".join(lines), series, time.time() - t0, killed


def _opencl_icd_present() -> bool:
    """Vrai si au moins un pilote OpenCL (fichier .icd) est déclaré au chargeur ocl-icd."""
    import glob as _g
    dirs = [os.environ.get("OCL_ICD_VENDORS", ""), "/etc/OpenCL/vendors"]
    for d in dirs:
        if d and (os.path.isfile(d) or _g.glob(os.path.join(d, "*.icd"))):
            return True
    return bool(os.environ.get("OPENCL_VENDOR_PATH"))


def _vulkan_icd_present() -> bool:
    """Vrai si un pilote Vulkan (ICD .json) est déclaré (fichiers standards ou variables d'environnement)."""
    import glob as _g
    if os.environ.get("VK_ICD_FILENAMES") or os.environ.get("VK_DRIVER_FILES"):
        return True
    for d in ("/usr/share/vulkan/icd.d", "/etc/vulkan/icd.d", "/usr/local/share/vulkan/icd.d",
              os.path.expanduser("~/.local/share/vulkan/icd.d")):
        if _g.glob(os.path.join(d, "*.json")):
            return True
    return False


_OPENCL_HINT = ("Aucun pilote OpenCL détecté. Installer le chargeur et vérifier : sudo apt install ocl-icd-libopencl1 clinfo ; "
                "clinfo -l doit lister le GPU. Pilote NVIDIA : le paquet libnvidia-compute-<version> fournit /etc/OpenCL/vendors/nvidia.icd.")
_VULKAN_HINT = ("Aucun pilote Vulkan détecté. Installer le chargeur : sudo apt install libvulkan1 vulkan-tools ; "
                "vulkaninfo --summary doit lister le GPU. Pilote NVIDIA serveur (sans écran) : installer aussi le paquet "
                "libnvidia-gl-<version> (même version que le pilote) qui fournit nvidia_icd.json.")


def _peak_failure_hint(kind: str, out: str, rc: int) -> str:
    """Explication lisible d'un échec clpeak/vkpeak d'après sa sortie ; '' si rien de reconnu."""
    low = (out or "").lower()
    if "error while loading shared libraries" in low or rc == 127:
        lib = re.search(r"loading shared libraries:\s*(\S+)", out or "")
        return f"Bibliothèque manquante ({lib.group(1) if lib else 'voir sortie'}) : " + (
            "sudo apt install ocl-icd-libopencl1" if kind == "opencl" else "sudo apt install libvulkan1")
    if kind == "opencl" and any(x in low for x in ("clgetplatformids", "no platforms", "platform not found", "cl_platform_not_found")):
        return _OPENCL_HINT
    if kind == "vulkan" and any(x in low for x in ("vkcreateinstance", "vkenumeratephysicaldevices", "no vulkan", "incompatible driver",
                                                   "failed to create", "initialization failed", "no devices")):
        return _VULKAN_HINT
    return ""


_CLPEAK_OLD_FLAGS = {"all-tests": [], "global-bandwidth": ["--global-bandwidth"], "compute-sp": ["--compute-sp"],
                     "compute-dp": ["--compute-dp"], "compute-integer": ["--compute-integer"],
                     "transfer-bw": ["--transfer-bandwidth"]}
_CLPEAK_NEW_FLAGS = {"all-tests": ["--compute", "--bandwidth"], "global-bandwidth": ["--global-memory-bandwidth"],
                     "compute-sp": ["--single-precision-compute"], "compute-dp": ["--double-precision-compute"],
                     "compute-integer": ["--integer-compute"], "transfer-bw": ["--transfer-bandwidth"]}
_CPU_NAME_RE = re.compile(r"processor|\bcpu\b|pthread|epyc|xeon|ryzen|core\(tm\)|neoverse|graviton|pocl", re.I)


def _clpeak_flavor(binary: str) -> str:
    """'new' (multi-backend : --list-devices, --devices) ou 'old' (clpeak 1.1.x : -p / -d)."""
    rc, out, err = _run([binary, "--help"], timeout=10)
    return "new" if "--list-devices" in (out + err) else "old"


def _clinfo_devices() -> list:
    """[(platform_index, device_index, nom_plateforme, nom_device)] d'après `clinfo -l` ([] si clinfo absent)."""
    if not shutil.which("clinfo"):
        return []
    rc, out, _ = _run(["clinfo", "-l"], timeout=20)
    devs, plat_i, plat_name = [], -1, ""
    for line in out.splitlines():
        m = re.match(r"^Platform #(\d+):\s*(.*)$", line.strip())
        if m:
            plat_i, plat_name = int(m.group(1)), m.group(2).strip()
            continue
        m = re.search(r"Device #(\d+):\s*(.*)$", line)
        if m and plat_i >= 0:
            devs.append((plat_i, int(m.group(1)), plat_name, m.group(2).strip()))
    return devs


def _select_indices(n: int, target_gpus: str) -> list:
    """Indices (0..n-1) retenus d'après la liste « GPU cibles » ('' = tous)."""
    if not target_gpus:
        return list(range(n))
    want = {int(x) for x in target_gpus.split(",") if x.isdigit()}
    return [i for i in range(n) if i in want]


def _burn_summary(scores: list) -> dict:
    """Synthèse d'un test de chauffe (passages successifs) : score du 1er passage, du dernier, chute en %.

    La chute compare le 1er passage (GPU froid) à la moyenne du dernier tiers des passages (GPU à température stabilisée) :
    pas besoin de spécification constructeur, c'est la perte de performance réellement observée en charge soutenue.
    """
    vals = [float(x) for x in scores if x]
    out: dict = {"burn_passes": len(vals)}
    if not vals:
        return out
    out["burn_scores"] = [round(v, 1) for v in vals]
    out["burn_score_first"] = round(vals[0], 1)
    out["burn_score_min"] = round(min(vals), 1)
    if len(vals) >= 2:
        tail = vals[-max(1, len(vals) // 3):]
        end = sum(tail) / len(tail)
        out["burn_score_end"] = round(end, 1)
        out["burn_drop_pct"] = round(max(0.0, 100.0 * (vals[0] - end) / vals[0]), 1) if vals[0] else 0.0
    return out


@with_fault_watch
def bench_clpeak(
    target_gpus: str = "",
    test_mode: str = "all-tests",
    extra_args: str = "",
    time_s: int = 0,
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None
) -> BenchmarkResult:
    """Benchmark clpeak : calcul FP32/FP64/INT, bande passante VRAM et PCIe, une mesure par GPU.

    `time_s` > 0 : mode chauffe — l'outil est relancé en boucle pendant `time_s` secondes par GPU (courbes de
    température continues, chute de performance en charge soutenue dans `burn_*`). 0 = un seul passage.

    Gère les deux générations de l'outil. Ancienne (apt, 1.1.x) : parcourt les plateformes OpenCL
    (`clinfo -l`) et lance `clpeak -p N -d M` pour chaque GPU, ce qui évite l'échec quand une plateforme
    sans GPU (ex. « AMD Accelerated Parallel Processing ») est listée en premier. Récente : choisit le
    meilleur backend (CUDA > ROCm > OpenCL > Vulkan) via `--devices`.
    """
    target_gpus = _normalize_gpu_list(target_gpus)
    name = "Compute OpenCL"
    binary = _find_tool_path("clpeak")
    if not binary:
        return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, "", status="skipped",
                               metrics={"erreur": "clpeak non installe (sudo apt install clpeak ocl-icd-libopencl1 clinfo)"})
    mode = test_mode if test_mode in _CLPEAK_OLD_FLAGS else "all-tests"
    flavor = _clpeak_flavor(binary)
    extra = []
    if extra_args:
        try:
            extra = shlex.split(str(extra_args))
        except ValueError:
            extra = str(extra_args).split()
    t0 = time.time()
    plan, notes = [], []          # plan = [(commande, libellé)]
    metrics: dict = {"test_mode": test_mode, "clpeak_flavor": flavor}
    if flavor == "new":
        rc, out, err = _run([binary, "--list-devices"], timeout=30)
        found = re.findall(r"\b(cuda|rocm|opencl|vulkan|oneapi):(\d+)", out + err)
        by_backend: dict = {}
        for be, idx in found:
            by_backend.setdefault(be, []).append(int(idx))
        chosen = next((b for b in ("cuda", "rocm", "opencl", "vulkan", "oneapi") if b in by_backend), "")
        if not chosen:
            msg = "clpeak ne voit aucun GPU (clpeak --list-devices vide). " + _OPENCL_HINT
            return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, msg + "\n" + out + err, status="error", metrics={"erreur": msg})
        ids = [by_backend[chosen][i] for i in _select_indices(len(by_backend[chosen]), target_gpus)]
        metrics["clpeak_backend"] = chosen
        plan.append(([binary, "--devices", ",".join(f"{chosen}:{i}" for i in ids)] + _CLPEAK_NEW_FLAGS[mode] + extra, chosen))
    else:
        if not _opencl_icd_present():
            return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, _OPENCL_HINT, status="skipped", metrics={"erreur": _OPENCL_HINT})
        devs = _clinfo_devices()
        gpus = [d for d in devs if not _CPU_NAME_RE.search(d[3])]
        if devs:
            skipped = [d[3] for d in devs if _CPU_NAME_RE.search(d[3])]
            if skipped:
                metrics["clpeak_devices_ignores"] = ", ".join(skipped)
            if not gpus:
                msg = "Aucun GPU dans clinfo -l (plateformes : " + ", ".join(sorted({d[2] for d in devs})) + "). " + _OPENCL_HINT
                return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, msg, status="error", metrics={"erreur": msg})
            for k in _select_indices(len(gpus), target_gpus):
                pi, di, pn, dn = gpus[k]
                plan.append(([binary, "-p", str(pi), "-d", str(di)] + _CLPEAK_OLD_FLAGS[mode] + extra, f"{pn} / {dn}"))
        else:
            notes.append("clinfo absent : sélection de la plateforme OpenCL impossible (sudo apt install clinfo)")
            plan.append(([binary] + _CLPEAK_OLD_FLAGS[mode] + extra, "plateformes par défaut"))
    if cb: cb(5, f"clpeak ({flavor}) : {len(plan)} exécution(s)…")
    env = os.environ.copy()
    outs, parse_outs, killed, rc = [], [], False, 0
    series_all = {"t": []}
    time_s = max(0, int(time_s or 0))
    burn_passes: dict = {}
    for n_run, (cmd, label) in enumerate(plan):
        passes: list = []
        rc, o, series, _el, killed = _stream_peak_tool(
            cmd, env, lambda l: bool(re.search(r"\s:\s*[0-9.]+", l)), "clpeak", cb, kill_event,
            series_cb if n_run == 0 else None, target_gpus, time_s=time_s, passes=passes)
        outs.append(f"# {' '.join(cmd)}\n{o}")
        # Mesures de référence = 1er passage (GPU froid) ; les passages suivants servent à la chute en charge soutenue
        parse_outs.append(passes[0] if (time_s and passes) else o)
        burn_passes[n_run] = passes
        if n_run == 0:
            series_all = series
        if killed:
            break
    out = "\n".join(outs)
    parse_text = "\n".join(parse_outs)
    elapsed = time.time() - t0
    devices = [d for d in parse_clpeak_generic(parse_text) if any(d["sections"].values())]
    devices = [d for d in devices if not (flavor == "new" and _CPU_NAME_RE.search(d["name"]) and len(devices) > 1)]
    primary = {"compute-sp": "Single-precision", "compute-dp": "Double-precision", "global-bandwidth": "Global memory",
               "compute-integer": "Integer compute", "transfer-bw": "Transfer"}.get(mode, "Single-precision")
    vals, unit = [], "GFLOPS"
    for i, d in enumerate(devices):
        metrics[f"clpeak_dev{i}_name"] = d["name"]
        metrics[f"clpeak_dev{i}_backend"] = d["backend"]
        best = None
        for sec, meas in d["sections"].items():
            slug = re.sub(r"[^a-z0-9]+", "_", sec.lower()).strip("_")
            for mk, (v, u) in meas.items():
                metrics[f"clpeak_dev{i}_{slug}_{re.sub(r'[^A-Za-z0-9]+', '_', mk).strip('_')}"] = v
                if sec.lower().startswith(primary.lower()) and u in ("GFLOPS", "GIOPS", "GB/s"):
                    best = (max(best[0], v), u) if best else (v, u)
        if best is None:                    # repli : première mesure exploitable
            for sec, meas in d["sections"].items():
                for mk, (v, u) in meas.items():
                    if u in ("GFLOPS", "GIOPS", "GB/s"):
                        best = (v, u); break
                if best: break
        if best:
            vals.append(best[0]); unit = best[1]
            metrics[f"clpeak_dev{i}_score"] = round(best[0], 2)
    score = round(statistics.median(vals), 2) if vals else None
    metrics["clpeak_devices"] = len(devices)
    if time_s:
        metrics["burn_time_s"] = time_s
        for n_run, plist in burn_passes.items():
            per_pass = []
            for ptxt in plist:
                best_p = None
                for d in parse_clpeak_generic(ptxt):
                    for sec, meas in d["sections"].items():
                        if sec.lower().startswith(primary.lower()):
                            for _mk, (v, u) in meas.items():
                                if u in ("GFLOPS", "GIOPS", "GB/s"):
                                    best_p = v if best_p is None else max(best_p, v)
                if best_p:
                    per_pass.append(best_p)
            sm = _burn_summary(per_pass)
            for k, v in sm.items():
                metrics[f"clpeak_dev{n_run}_{k}"] = v
    if target_gpus: metrics["target_gpus"] = target_gpus
    metrics["command"] = " && ".join(" ".join(c) for c, _ in plan)
    metrics["series"] = series_all
    _add_gpu_series_metrics(metrics, series_all)
    status = "stopped" if killed else ("ok" if score else "error")
    if status == "error":
        hint = (_peak_failure_hint("opencl", out, rc) or "clpeak n'a retourné aucune mesure (voir la sortie brute). "
                "Vérifiez clinfo -l ; une plateforme OpenCL sans GPU peut faire échouer clpeak.")
        if notes: hint += " " + " ; ".join(notes)
        metrics["erreur"] = hint
        out = hint + "\n\n" + out
    elif notes:
        metrics["avertissement"] = " ; ".join(notes)
    if cb: cb(100, "Termine")
    return BenchmarkResult(name, "GPU", score, unit, elapsed, out, metrics=metrics, status=status,
                           system_info=get_system_info(), notes=" ; ".join(notes), command=metrics["command"])


def _vulkan_gpu_devices() -> list:
    """GPU Vulkan réels [(index, nom)] d'après `vulkaninfo --summary` (processeurs logiciels type llvmpipe exclus).

    Liste vide si vulkaninfo est absent ou illisible (l'appelant retombe alors sur le device 0).
    """
    if not shutil.which("vulkaninfo"):
        return []
    rc, out, err = _run(["vulkaninfo", "--summary"], timeout=30)
    devs, cur = [], None
    for line in (out + "\n" + err).splitlines():
        m = re.match(r"^\s*GPU(\d+):", line)
        if m:
            cur = {"index": int(m.group(1)), "type": "", "name": ""}
            devs.append(cur)
            continue
        if cur is not None:
            m = re.match(r"^\s*deviceType\s*=\s*(\S+)", line)
            if m:
                cur["type"] = m.group(1)
            m = re.match(r"^\s*deviceName\s*=\s*(.+?)\s*$", line)
            if m:
                cur["name"] = m.group(1)
    return [(d["index"], d["name"]) for d in devs
            if d["type"] and not any(x in d["type"] for x in ("CPU", "VIRTUAL", "OTHER")) and "llvmpipe" not in d["name"].lower()]


def _nonexec_hint(name: str) -> str:
    """Si l'outil existe dans un dossier habituel mais sans droit d'exécution (cas d'un zip extrait), indique la commande."""
    for home in _candidate_user_homes():
        for sub in ("", "Downloads", "Téléchargements", "Desktop"):
            base = os.path.join(home, sub) if sub else home
            try:
                for root, dirs, files in os.walk(base):
                    if root[len(base):].count(os.sep) >= 3:
                        dirs[:] = []
                    if name in files:
                        path = os.path.join(root, name)
                        if not os.access(path, os.X_OK):
                            return f"{path} n'est pas exécutable : chmod +x \"{path}\" (puis le copier dans /usr/local/bin)"
            except Exception:
                continue
    return ""


def parse_vkpeak_output(text: str) -> dict:
    """Parse la sortie de vkpeak : {'fp32-scalar': 1234.5, …} et nom du device."""
    vals, device = {}, ""
    for line in text.splitlines():
        m = re.match(r"^\s*device\s*=\s*(.+?)\s*$", line)
        if m:
            device = m.group(1)
            continue
        m = re.match(r"^\s*([a-z0-9]+-[a-z0-9]+)\s*=\s*([0-9.]+)\s*(GFLOPS|GIOPS)?", line)
        if m and float(m.group(2)) > 0:   # 0.0 = format non pris en charge par le GPU (fp8, fp4…) : non évalué
            vals[m.group(1)] = float(m.group(2))
    return {"values": vals, "device": device}


@with_fault_watch
def bench_vkpeak(
    target_gpus: str = "",
    extra_args: str = "",
    time_s: int = 0,
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None
) -> BenchmarkResult:
    """Benchmark Vulkan (vkpeak) : un passage par index de device Vulkan demandé.

    `time_s` > 0 : mode chauffe — vkpeak est relancé en boucle pendant `time_s` secondes par GPU (vkpeak sature
    le GPU en FP32/FP16/INT : ~300 W sur une Arc Pro B70). La chute de performance en charge soutenue est
    mesurée (`burn_*`). 0 = un seul passage, comme avant.
    """
    target_gpus = _normalize_gpu_list(target_gpus)
    binary = _find_tool_path("vkpeak")
    if not binary:
        msg = _nonexec_hint("vkpeak") or ("vkpeak non installé : télécharger l'exécutable Linux sur "
                                          "https://github.com/nihui/vkpeak/releases, chmod +x vkpeak, sudo mv vkpeak /usr/local/bin/")
        return BenchmarkResult("Compute Vulkan", "GPU", None, "GFLOPS", 0, msg, status="skipped", metrics={"erreur": msg})
    if not _vulkan_icd_present():
        return BenchmarkResult("Compute Vulkan", "GPU", None, "GFLOPS", 0, _VULKAN_HINT, status="skipped",
                               metrics={"erreur": _VULKAN_HINT})
    extra = []
    if extra_args:
        try:
            extra = shlex.split(str(extra_args))
        except ValueError:
            extra = str(extra_args).split()
    # vkpeak prend l'index du device Vulkan en argument positionnel (pas CUDA_VISIBLE_DEVICES).
    # Sans sélection, on teste TOUS les GPU Vulkan réels (le device 0 peut être un rendu logiciel ou un autre GPU).
    vk_gpus = _vulkan_gpu_devices()
    if vk_gpus:
        chosen = _select_indices(len(vk_gpus), target_gpus)
        devices = [str(vk_gpus[i][0]) for i in chosen]
        metrics_names = {str(vk_gpus[i][0]): vk_gpus[i][1] for i in chosen}
        if not devices:
            msg = f"Aucun GPU Vulkan ne correspond à la sélection ({target_gpus}) ; GPU Vulkan vus : " + ", ".join(f"{i}={n}" for i, n in vk_gpus)
            return BenchmarkResult("Compute Vulkan", "GPU", None, "GFLOPS", 0, msg, status="error", metrics={"erreur": msg})
    else:
        devices = target_gpus.split(",") if target_gpus else [""]
        metrics_names = {}
    env = os.environ.copy()
    cmds = [[binary] + ([d] if d else []) + extra for d in devices]
    cmd_str = " && ".join(shlex.join(c) for c in cmds)
    if cb: cb(5, "Démarrage vkpeak…")

    outs, all_series, elapsed_tot, killed = [], {"t": []}, 0.0, False
    time_s = max(0, int(time_s or 0))
    metrics = {"command": cmd_str}
    if time_s:
        metrics["burn_time_s"] = time_s
    best_fp32, rc = None, 0
    for idx, cmd in enumerate(cmds):
        passes: list = []
        rc, out, series, elapsed, killed = _stream_peak_tool(
            cmd, env, lambda l: bool(re.search(r"=\s*[0-9.]+", l)), "vkpeak",
            cb, kill_event, series_cb, target_gpus, time_s=time_s, passes=passes)
        outs.append(out)
        elapsed_tot += elapsed
        if idx == 0:
            all_series = series
        parsed = parse_vkpeak_output(passes[0] if (time_s and passes) else out)   # référence = 1er passage (GPU froid)
        prefix = f"vk{devices[idx]}_" if devices[idx] else ""
        if time_s and passes:
            fp32_passes = []
            for ptxt in passes:
                pv = [v for k, v in parse_vkpeak_output(ptxt)["values"].items() if k.startswith("fp32")]
                if pv:
                    fp32_passes.append(max(pv))
            for k, v in _burn_summary(fp32_passes).items():
                metrics[prefix + k] = v
        if parsed["device"]:
            metrics[prefix + "device"] = parsed["device"]
        for k, v in parsed["values"].items():
            metrics[prefix + k.replace("-", "_")] = v
            if k.startswith("fp32") and (best_fp32 is None or v > best_fp32):
                best_fp32 = v
        if killed:
            break
    metrics["series"] = all_series
    if target_gpus: metrics["target_gpus"] = target_gpus
    for d, nm in metrics_names.items():
        metrics.setdefault(f"vk{d}_device", nm)
    _add_gpu_series_metrics(metrics, all_series)
    status = "stopped" if killed else ("ok" if best_fp32 else "error")
    all_out = "\n".join(outs)
    if status == "error":
        hint = _peak_failure_hint("vulkan", all_out, rc) or ("vkpeak n'a retourné aucune mesure (voir la sortie brute). "
                                                              "Vérifiez vulkaninfo --summary et l'index de device choisi.")
        metrics["erreur"] = hint
        all_out = hint + "\n\n" + all_out
    if cb: cb(100, "Termine")
    return BenchmarkResult("Compute Vulkan", "GPU", best_fp32, "GFLOPS", elapsed_tot, all_out,
                           metrics=metrics, status=status, system_info=get_system_info(), command=cmd_str)


def _oclburn_binary() -> tuple:
    """Retourne (chemin, message d'erreur) : « oclburn » du PATH, sinon compilé avec gcc depuis third_party/ dans ~/.benchsuite/tools/oclburn.

    Seul gcc est nécessaire (l'API OpenCL est chargée dynamiquement) ; BenchSuite ne compile que sa propre source, il n'installe rien.
    """
    found = _find_tool_path("oclburn")
    out = os.path.expanduser("~/.benchsuite/tools/oclburn/oclburn")
    if found and (not os.path.isfile(_OCLBURN_SRC) or found != out or os.path.getmtime(found) >= os.path.getmtime(_OCLBURN_SRC)):
        return found, ""
    gcc = shutil.which("gcc") or shutil.which("cc")
    if not gcc:
        return "", "gcc absent : sudo apt install gcc (oclburn est compilé depuis third_party/benchsuite_oclburn/oclburn.c)"
    if not os.path.isfile(_OCLBURN_SRC):
        return "", "third_party/benchsuite_oclburn/oclburn.c introuvable"
    os.makedirs(os.path.dirname(out), exist_ok=True)
    tmp = f"{out}.{os.getpid()}.tmp"          # compilation vers un fichier temporaire puis remplacement atomique (étapes parallèles)
    rc, o, e = _run([gcc, "-O2", "-pthread", _OCLBURN_SRC, "-o", tmp, "-ldl"], timeout=120)
    if rc != 0 or not os.path.isfile(tmp):
        return "", "compilation d'oclburn impossible : " + (e or o)[-300:]
    os.replace(tmp, out)
    return out, ""


def _oclburn_kv(line: str) -> dict:
    """Paires clé=valeur d'une ligne oclburn (valeurs entre guillemets acceptées)."""
    return {k: v.strip('"') for k, v in re.findall(r'(\w+)=("[^"]*"|\S+)', line)}


def _oclburn_extra_args() -> list:
    """BENCHSUITE_OCLBURN_CPU=1 : inclut les périphériques OpenCL CPU (tests du logiciel sans GPU)."""
    return ["--cpu"] if os.environ.get("BENCHSUITE_OCLBURN_CPU") == "1" else []


def _oclburn_devices(binary: str) -> list:
    """GPU OpenCL listés par `oclburn -l` : [{'idx', 'name', 'platform', 'mem_mb', ...}] ([] si aucun)."""
    rc, out, err = _run([binary, "-l"] + _oclburn_extra_args(), timeout=60)
    devs = []
    for line in (out or "").splitlines():
        if line.startswith("DEVICE "):
            devs.append(_oclburn_kv(line))
    return devs


def _gpu_power_limit_w(g: dict, idx: int) -> float:
    """Vraie limite de puissance de la carte en W (0 si inconnue → « non évalué ») : `power1_max` (soutenue, sinon `power1_cap`) hwmon (AMD, Intel xe),
    `power.limit` de nvidia-smi (NVIDIA). Jamais la puissance instantanée (champ `puissance_max_w` de la fiche GPU, parfois une mesure)."""
    try:
        if g.get("fabricant") == "NVIDIA" and nvidia_smi_usable():
            rc, out, _ = _run(["nvidia-smi", "-i", str(g.get("index", idx)), "--query-gpu=power.limit", "--format=csv,noheader,nounits"], timeout=8)
            if rc == 0 and out.strip():
                return float(out.strip().splitlines()[0])
        from core.system_info import _find_hwmon_dir, _normalize_pci_bus
        d = _find_hwmon_dir(_normalize_pci_bus(g.get("pci_bus", "")))
        if d:
            # pilote xe / amdgpu : power1_max = limite SOUTENUE (PL1) ; power1_cap = limite de pointe (PL2). La soutenue prime.
            for name in ("power1_max", "power1_cap"):
                try:
                    with open(os.path.join(d, name)) as f:
                        v = float(f.read().strip()) / 1e6
                    if v > 0:
                        return v
                except (OSError, ValueError):
                    continue
    except Exception:
        pass
    return 0.0


def _add_power_cap_metrics(metrics: dict, series: dict, gpu_indices=None) -> None:
    """Puissance atteinte en % de la limite de la carte (preuve que le burn sature le GPU) : gpuN_power_cap_w / _p95_w / _pct.

    Seuls les GPU de `gpu_indices` (indices de la fiche GPU, ceux réellement chargés) sont évalués ; médiane de la puissance après les
    10 premières secondes (P95 en plus). Limite inconnue ou GPU non identifié → rien n'est calculé (« non évalué »)."""
    if not gpu_indices:
        return
    try:
        from core.system_info import get_gpu_info
        gpus = get_gpu_info()
    except Exception:
        return
    t = series.get("t") or []
    for i in gpu_indices:
        if i >= len(gpus):
            continue
        pw = series.get(f"gpu{i}_power")
        cap = _gpu_power_limit_w(gpus[i], i)
        if not pw or cap <= 0:
            continue
        vals = [float(v) for tt, v in zip(t, pw) if v is not None and tt >= 10] or [float(v) for v in pw if v is not None]
        if not vals:
            continue
        vals.sort()
        p95 = vals[min(len(vals) - 1, int(len(vals) * 0.95))]
        med = statistics.median(vals)                        # puissance SOUTENUE : les pics de 1 s (PL2) gonflent le P95
        std_w = statistics.pstdev(vals) if len(vals) >= 2 else 0.0
        metrics[f"gpu{i}_power_cap_w"] = round(cap, 0)
        metrics[f"gpu{i}_power_p95_w"] = round(p95, 1)
        metrics[f"gpu{i}_power_med_w"] = round(med, 1)
        metrics[f"gpu{i}_power_min_load_w"] = round(vals[0], 1)
        metrics[f"gpu{i}_power_std_w"] = round(std_w, 1)
        if med > 0:
            metrics[f"gpu{i}_power_variation_pct"] = round(100.0 * std_w / med, 1)
        metrics[f"gpu{i}_power_cap_pct"] = round(100.0 * med / cap, 1)


@with_fault_watch
def bench_gpu_oclburn(
    time_s: int = 300,
    vram_pct: int = 90,
    mode: str = "all",
    verification: str = "max",
    target_gpus: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None
) -> BenchmarkResult:
    """Burn GPU OpenCL à 100 % avec vérification des résultats — Intel, AMD, NVIDIA (tout GPU avec un pilote OpenCL).

    Lance `oclburn` (source `third_party/benchsuite_oclburn/oclburn.c`, compilée avec gcc) : noyaux entiers exacts, FP32 FMA et FP16
    en continu + test de motifs sur `vram_pct` % de la VRAM, en même temps, sur chaque GPU ciblé. Chaque résultat est comparé à une
    référence : toute erreur de calcul ou de mémoire est comptée (`compute_errors` / `memory_errors`) et fait échouer le verdict.
    `mode` : power (FP32 FMA pur en continu sans concurrence mémoire — puissance maximale plate au TDP + vérification d'erreurs,
    recommandé pour chauffe stable Intel/AMD/NVIDIA) | all | compute | memory.
    `verification` : max (contrôle des noyaux flottants à chaque série de FMA, recommandé) | balanced | power.
    « GPU cibles » = indices de la fiche GPU ; ils sont associés aux périphériques OpenCL par adresse PCI quand le pilote la donne.
    Compilation et pilote OpenCL absents → test ignoré avec la marche à suivre.
    """
    target_gpus = _normalize_gpu_list(target_gpus)
    time_s = max(10, int(time_s or 300))
    vram_pct = max(5, min(95, int(vram_pct or 90)))
    mode = mode if mode in ("all", "power", "compute", "memory") else "all"
    name = "GPU — Burn OpenCL puissance constante" if mode == "power" else "GPU — Burn OpenCL vérifié"
    fold = {"max": "1", "balanced": "2", "power": "4"}.get(str(verification), "1")
    binary, err = _oclburn_binary()
    if not binary:
        return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, err, status="skipped", metrics={"erreur": err})
    if not _opencl_icd_present():
        return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, _OPENCL_HINT, status="skipped", metrics={"erreur": _OPENCL_HINT})
    devs = _oclburn_devices(binary)
    if not devs:
        msg = "oclburn ne voit aucun GPU OpenCL. " + _OPENCL_HINT
        return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, msg, status="error", metrics={"erreur": msg})

    # Association périphérique OpenCL ↔ GPU de la fiche (télémétrie) par adresse PCI ; sinon repli sur l'ordre de la liste OpenCL
    pci_to_gpu: dict = {}
    try:
        from core.system_info import get_gpu_info, _normalize_pci_bus
        for gi_, g in enumerate(get_gpu_info()):
            if g.get("pci_bus"):
                pci_to_gpu[_normalize_pci_bus(g["pci_bus"]).lower()] = gi_
    except Exception:
        pass
    cl_to_gpu = {pos: pci_to_gpu.get(str(d.get("pci", "")).lower()) for pos, d in enumerate(devs)}
    mapped = all(v is not None for v in cl_to_gpu.values())
    want = {int(x) for x in target_gpus.split(",") if x.isdigit()}
    if want and mapped:
        chosen = [pos for pos in range(len(devs)) if cl_to_gpu[pos] in want]
    elif want:
        chosen = _select_indices(len(devs), target_gpus)      # pas d'adresse PCI : indices pris dans l'ordre OpenCL (clinfo -l)
    else:
        chosen = list(range(len(devs)))
    if not chosen:
        msg = f"Aucun GPU OpenCL ne correspond à la sélection ({target_gpus}) ; GPU OpenCL vus : " + ", ".join(
            f"{i}={d.get('name', '')} ({d.get('pci', '-')})" for i, d in enumerate(devs))
        return BenchmarkResult(name, "GPU", None, "GFLOPS", 0, msg, status="error", metrics={"erreur": msg})
    cmd = [binary, "-d", ",".join(str(devs[i].get("idx", i)) for i in chosen), "-t", str(time_s), "-m", str(vram_pct), "-M", mode,
           "-F", fold, "-i", "5"] + _oclburn_extra_args()
    if cb: cb(3, f"oclburn : {len(chosen)} GPU, {time_s} s, mode {mode}…")
    rc, out, series, elapsed, killed = _stream_peak_tool(
        cmd, os.environ.copy(), lambda l: l.startswith("STAT "), "oclburn", cb, kill_event, series_cb, target_gpus,
        time_s=time_s, relaunch=False, grace_s=180)
    metrics: dict = {"command": " ".join(cmd), "burn_time_s": time_s, "oclburn_mode": mode, "oclburn_verification": verification}
    if want and not mapped:
        metrics["avertissement"] = "adresse PCI des périphériques OpenCL indisponible : GPU cibles interprétés dans l'ordre d'OpenCL (clinfo -l)"
    results, stats, fails, errlines = {}, {}, [], []
    for line in out.splitlines():
        kv = _oclburn_kv(line)
        if line.startswith("RESULT "):
            results[kv.get("dev", "?")] = kv
        elif line.startswith("STAT "):
            stats.setdefault(kv.get("dev", "?"), []).append(kv)
        elif line.startswith("FAIL "):
            fails.append(kv.get("what", "échec OpenCL"))
        elif line.startswith("ERROR "):
            errlines.append(kv.get("detail", ""))
        elif line.startswith("MEMORY "):
            metrics[f"oclburn_dev{kv.get('dev', '?')}_mem_buffers"] = kv.get("buffers")
        elif line.startswith("START "):
            metrics[f"oclburn_dev{kv.get('dev', '?')}_vram_pct"] = kv.get("vram_pct")      # pourcentage réellement appliqué (iGPU : plafonné)
    def fnum(kv, k, dflt=0.0):
        try:
            return float(kv.get(k, dflt))
        except (TypeError, ValueError):
            return dflt
    tot_c = tot_m = 0
    fp32 = []
    devices_seen = set(results) | set(stats)
    for dv in sorted(devices_seen):
        pre = f"oclburn_dev{dv}_"
        kv = results.get(dv)
        last = (stats.get(dv) or [{}])[-1]
        src = kv if kv else last                      # test interrompu : dernier relevé cumulatif (STAT) faute de bilan (RESULT)
        if kv:
            metrics[pre + "name"] = kv.get("name", "")
            for k in ("fp32_gflops", "fp16_gflops", "giops", "mem_gbs"):
                if fnum(kv, k) > 0:
                    metrics[pre + k] = round(fnum(kv, k), 1)
            metrics[pre + "mem_tested_gb"] = round(fnum(kv, "mem_tested_mb") / 1024.0, 1)
            if fnum(kv, "fp32_gflops") > 0:
                fp32.append(fnum(kv, "fp32_gflops"))
        ce, me = int(fnum(src, "compute_errors")), int(fnum(src, "memory_errors"))
        metrics[pre + "compute_errors"] = ce
        metrics[pre + "memory_errors"] = me
        tot_c += ce; tot_m += me
        # stabilité en charge soutenue : moyennes sur ~10 s (2 relevés STAT de 5 s) après la montée en charge, 1re fenêtre vs fin
        lst = [fnum(x, "fp32_gflops") for x in stats.get(dv, [])]
        lst = lst[max(1, len(lst) // 6):]
        win = [sum(lst[k:k + 2]) / len(lst[k:k + 2]) for k in range(0, len(lst), 2) if lst[k:k + 2]]
        for k, v in _burn_summary([x for x in win if x > 0]).items():
            metrics[pre + k] = v
    if errlines and not (tot_c or tot_m):
        tot_c = len(errlines)                          # erreurs signalées en cours de test mais compteurs absents : on ne les perd pas
    metrics["compute_errors"] = tot_c
    metrics["memory_errors"] = tot_m
    metrics["verifie"] = bool(results) and not fails and not killed
    metrics["series"] = series
    _add_gpu_series_metrics(metrics, series)
    if mode != "memory":                               # le mode mémoire seul ne vise pas la limite de puissance
        _add_power_cap_metrics(metrics, series, sorted({cl_to_gpu[p] for p in chosen if cl_to_gpu.get(p) is not None}))
    if errlines:
        metrics["erreurs_detail"] = errlines[:5]
    score = round(statistics.median(fp32), 1) if fp32 else None
    status = "stopped" if killed else ("ok" if results and not fails else "error")
    notes = ""
    if status == "error":
        notes = ("; ".join(fails) if fails else "oclburn n'a retourné aucun résultat") + " — voir la sortie brute"
        metrics["erreur"] = notes
    elif tot_c or tot_m:
        notes = f"{tot_c} erreur(s) de calcul et {tot_m} erreur(s) mémoire détectée(s) : GPU défectueux ou instable"
    if cb: cb(100, "Terminé")
    return BenchmarkResult(name, "GPU", score, "GFLOPS", elapsed, out, metrics=metrics, status=status,
                           system_info=get_system_info(), notes=notes, command=" ".join(cmd))
