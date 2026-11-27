# -*- coding: utf-8 -*-
"""Benchmarks GPU NVIDIA CUDA (gpu-burn, gpu-fryer, nvbandwidth, intégrité VRAM)."""

import json
import os
import queue
import re
import shlex
import shutil
import signal
import statistics
import subprocess
import threading
import time

from core.bench_sensors import _poll_gpu_sensors
from core.bench_utils import (
    BenchmarkResult,
    LOG_POST_SECONDS,
    LOG_PRE_SECONDS,
    ProgressCb,
    SeriesCb,
    _add_gpu_series_metrics,
    _find_gpu_burn_command,
    _find_tool_path,
    _gpu_burn_workdir,
    _log_window_wait,
    _normalize_gpu_list,
    _run,
    _terminate_process_tree,
    get_system_info,
    nvidia_smi_usable,
    tool_exists,
    with_fault_watch,
)

def _cuda_lib_dirs(libname: str) -> list:
    """Dossiers contenant la bibliothèque CUDA manquante (ex. libcublas.so.12), hors chemin du chargeur ; [] si introuvable."""
    import glob
    base = libname
    pats = ["/usr/local/cuda*/lib64", "/usr/local/cuda*/targets/*/lib", "/opt/cuda*/lib64", "/usr/lib/x86_64-linux-gnu",
            "/usr/lib64", "/usr/lib/aarch64-linux-gnu", "/opt/gpu-burn", "/opt/nvidia/*/lib*",
            "/usr/lib/python3*/site-packages/nvidia/*/lib", "/usr/local/lib/python3*/dist-packages/nvidia/*/lib",
            os.path.expanduser("~/.local/lib/python3*/site-packages/nvidia/*/lib")]
    found = []
    for pat in pats:
        for d in glob.glob(pat):
            if glob.glob(os.path.join(d, base + "*")) and d not in found:
                found.append(d)
    return found


def _cuda_lib_autres_versions(libname: str) -> list:
    """Autres versions installées de la même bibliothèque (ex. libcublas.so.12 quand libcublas.so.13 manque)."""
    import glob
    racine = libname.split(".so")[0] + ".so"
    trouves = []
    for d in _cuda_lib_dirs(racine):
        for f in sorted(glob.glob(os.path.join(d, racine + ".*"))):
            nom = os.path.basename(f)
            if nom != libname and nom not in trouves:
                trouves.append(nom)
    return trouves


def _gpu_burn_lib_hint(libname: str) -> str:
    """Message d'aide quand gpu-burn ne trouve pas une bibliothèque CUDA (version CUDA différente de celle de compilation)."""
    if libname.startswith("libcuda.so"):
        return ("gpu-burn ne trouve pas « libcuda.so.1 » (bibliothèque du pilote NVIDIA, pas du toolkit CUDA). "
                "Causes fréquentes : gpu-burn installé en snap (confiné, ne voit pas le pilote) ou pilote NVIDIA cassé "
                "(vérifiez que nvidia-smi répond ; « API mismatch » = module noyau et bibliothèques de versions différentes : "
                "sudo dkms autoinstall && sudo reboot). Remède : sudo snap remove gpu-burn, puis compiler gpu-burn "
                "(./install.sh ou git clone https://github.com/wilicc/gpu-burn && make COMPUTE=<arch>) et l'installer dans /opt/gpu-burn.")
    m = re.search(r"\.so\.(\d+)", libname)
    maj = m.group(1) if m else ""
    autres = _cuda_lib_autres_versions(libname)
    txt = (f"gpu-burn ne trouve pas « {libname} » : le binaire a été compilé avec CUDA {maj or '?'} "
           "mais cette bibliothèque n'est pas installée (ou pas dans le chemin du chargeur).")
    if autres:
        txt += f" Versions présentes sur cette machine : {', '.join(autres)}."
        # cuBLAS suit la version du toolkit : libcublas.so.12.0.x = CUDA 12.0, 12.8.x = CUDA 12.8…
        vmax = (0, 0)
        for n in autres:
            mv = re.search(r"\.so\.(\d+)\.(\d+)", n)
            if mv:
                vmax = max(vmax, (int(mv.group(1)), int(mv.group(2))))
        blackwell = False
        try:
            rc, out = (-1, "")
            if nvidia_smi_usable():
                rc, out, _ = _run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], timeout=6)
            blackwell = rc == 0 and any(float(x) >= 12.0 for x in re.findall(r"\d+\.\d+", out))
        except Exception:
            pass
        if blackwell and vmax != (0, 0) and vmax < (12, 8):
            txt += (f" ATTENTION : ce CUDA est ancien (≈ {vmax[0]}.{vmax[1]}) et ne sait pas compiler pour les GPU Blackwell "
                    "(il faut CUDA ≥ 12.8) : recompiler avec lui ne marchera pas. Installez un toolkit récent "
                    "(CUDA 13 pour un pilote ≥ 580, voir developer.nvidia.com/cuda-downloads, dépôt NVIDIA) puis "
                    "make clean && make COMPUTE=120 CUDAPATH=/usr/local/cuda-13.0 ; ou installez seulement le runtime CUDA 13 "
                    "(paquets libcublas-13-x) pour que le binaire existant démarre.")
        else:
            txt += (" Recompilez gpu-burn avec le CUDA installé : cd gpu-burn && make clean && "
                    "make CUDAPATH=/usr/local/cuda (Blackwell : COMPUTE=120, CUDA ≥ 12.8).")
    else:
        txt += (f" Deux solutions : installer le runtime CUDA {maj or ''} (sudo apt install cuda-toolkit-{maj}, "
                "pilote compatible requis ; CUDA 13 demande un pilote ≥ 580), "
                "ou compiler gpu-burn contre un CUDA déjà présent (make COMPUTE=<arch> CUDAPATH=/usr/local/cuda).")
    txt += (" Vérifiez : ldd $(which gpu_burn) | grep 'not found'. "
            "Blackwell (sm_120) : CUDA ≥ 12.8 ; RTX 30xx : COMPUTE=86. Le binaire snap est souvent trop ancien. "
            "gpu-fryer n'est pas concerné par cette erreur.")
    return txt


def _gpu_burn_compare_hint(output: str, cwd: str) -> str:
    """Aide quand gpu-burn n'arrive pas à charger son noyau de comparaison (compare.fatbin / compare.ptx)."""
    cc = ""
    try:
        rc, out = (-1, "")
        if nvidia_smi_usable():
            rc, out, _ = _run(["nvidia-smi", "--query-gpu=compute_cap", "--format=csv,noheader"], timeout=6)
        caps = sorted(set(x.strip().replace(".", "") for x in out.splitlines() if re.fullmatch(r"\d+\.\d+", x.strip())))
        cc = caps[0] if caps else ""
    except Exception:
        pass
    fichiers = [f for f in ("compare.fatbin", "compare.ptx") if cwd and os.path.isfile(os.path.join(cwd, f))]
    return ("gpu-burn n'a pas pu initialiser le GPU : son noyau de comparaison (compare.fatbin / compare.ptx) est introuvable, "
            "n'est pas celui de ce binaire, ou n'a pas été compilé pour ce GPU (« named symbol not found »). "
            f"Dossier d'exécution : {cwd or '?'} (fichiers trouvés : {', '.join(fichiers) or 'aucun'}). "
            "Le binaire gpu_burn et son fichier compare.* doivent venir du MÊME « make » : ne copiez pas un compare.* d'une autre version. "
            "Remède : cd gpu-burn && make clean && make" + (f" COMPUTE={cc}" if cc else " COMPUTE=<arch du GPU>") +
            " CUDAPATH=/usr/local/cuda-<X.Y> puis lancer ./gpu_burn 20 depuis ce dossier ; supprimez les anciennes copies "
            "(find / -name 'compare.fatbin' -o -name 'compare.ptx' 2>/dev/null) pour éviter un mélange de versions.")


def _gpu_burn_progress(text: str) -> "tuple[dict, int]":
    """Analyse les lignes de progression de gpu-burn. Retourne ({index GPU: Gflop/s moyen}, erreurs max vues).

    La moyenne est calculée sur les lignes (une par seconde) : elle pondère donc par le temps ; les valeurs nulles du
    démarrage sont ignorées. « errors: 0 - 0 » donne un compteur par GPU."""
    per: dict = {}
    err_max = 0
    for line in text.splitlines():
        if "proc'd" not in line:
            continue
        head, _sep, tail = line.partition("errors:")
        for i, v in enumerate(re.findall(r"\(\s*([\d.]+)\s*Gflop/s\s*\)", head)):
            try:
                per.setdefault(i, []).append(float(v))
            except ValueError:
                pass
        if tail:
            for n in re.findall(r"\d+", tail.split("temps:")[0]):
                err_max = max(err_max, int(n))
    avg = {}
    for i, vals in per.items():
        pos = [x for x in vals if x > 0]
        if pos:
            avg[i] = sum(pos) / len(pos)
    return avg, err_max


@with_fault_watch
def bench_gpu_burn(
    time_s: int = 30,
    precision: str = "float",     # float | double
    target_gpus: str = "",
    extra_args: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    target_gpus = _normalize_gpu_list(target_gpus)
    t0 = time.time()
    cmd_base = _find_gpu_burn_command()
    if not cmd_base:
        return BenchmarkResult("GPU gpu-burn", "GPU", None, "GFLOPS", 0, "", status="skipped",
                               metrics={"erreur": "gpu-burn non installe ou hors PATH"})
    if cb: cb(5, f"Demarrage gpu-burn {time_s}s {precision}…")

    # Syntaxe de gpu-burn : gpu_burn [OPTIONS] [DURÉE] — la durée vient EN DERNIER. Placée avant « -d », certaines versions
    # lisent le dernier argument comme durée et brûlent « 0 seconds ».
    opts = []
    if precision == "double":
        opts.append("-d")
    if extra_args:
        try:
            opts += shlex.split(str(extra_args))
        except Exception:
            opts += str(extra_args).split()
    cmd = cmd_base + opts + [str(time_s)]

    env = os.environ.copy()
    # Les variables graphiques GTK ne doivent pas influencer les outils CUDA.
    for k in ("GSK_RENDERER", "GDK_BACKEND"):
        env.pop(k, None)
    if target_gpus:
        env["CUDA_VISIBLE_DEVICES"] = str(target_gpus)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"   # même numérotation que nvidia-smi (sinon CUDA range du plus rapide au plus lent)
        
    cmd_str = (f"CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES={target_gpus} " if target_gpus else "") + shlex.join(cmd)
    cwd = _gpu_burn_workdir(cmd_base)
    metrics_cwd = cwd or os.getcwd()

    # Monitoring temps réel identique à gpu-fryer.
    series: dict = {"t": []}
    stop_event = threading.Event()
    
    from core.telemetry_aggregator import start_background_telemetry
    start_background_telemetry(series, stop_event.is_set)
    
    poll_thread = threading.Thread(target=_poll_gpu_sensors,
                                   args=(series, stop_event, t0, series_cb, target_gpus), daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    lines = []
    q = queue.Queue()
    proc = None
    stopped_by_user = False

    def _reader(stream, prefix=""):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                q.put(prefix + line)
        except Exception:
            pass

    segments, early_exits, lib_note = 0, [], ""
    try:
        t_end = time.time() + time_s      # fin voulue de TOUT le burn : gpu-burn est relancé s'il s'arrête avant
        while True:
            remaining = max(1, int(t_end - time.time()))
            seg_cmd = cmd_base + opts + [str(remaining)]
            seg_start = time.time()
            seg_from = len(lines)
            proc = subprocess.Popen(
                seg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, bufsize=1, env=env, cwd=cwd, start_new_session=True,
            )
            threading.Thread(target=_reader, args=(proc.stdout, ""), daemon=True).start()
            threading.Thread(target=_reader, args=(proc.stderr, "ERR: "), daemon=True).start()

            hard_deadline = seg_start + max(remaining + 90, 120)
            while proc.poll() is None:
                if kill_event and kill_event.is_set():
                    stopped_by_user = True
                    if cb:
                        cb(95, "Arrêt gpu-burn…")
                    _terminate_process_tree(proc, grace=0.8)
                    break
                if time.time() > hard_deadline:
                    if cb:
                        cb(95, "Timeout gpu-burn, arrêt forcé…")
                    _terminate_process_tree(proc, grace=0.8)
                    break
                try:
                    while True:
                        line = q.get_nowait()
                        lines.append(line)
                        # Exemples gpu-burn : "GPU 0: 12345 Gflop/s" ou "Gflops" selon fork.
                        mm = re.search(r"GPU\s+(\d+)\s*:\s*([\d.]+)\s*Gflop", line, re.IGNORECASE)
                        if mm and cb:
                            elapsed = time.time() - t0
                            pct = min(90, int(10 + 80 * elapsed / max(time_s, 1)))
                            gf = float(mm.group(2))
                            temp_str  = f"{series.get('gpu0_temp', ['—'])[-1]:.0f}°C" if series.get("gpu0_temp") else "—"
                            power_str = f"{series.get('gpu0_power', ['—'])[-1]:.0f}W" if series.get("gpu0_power") else "—"
                            cb(pct, f"{gf:.0f} GFLOPS | {temp_str} | {power_str}")
                except queue.Empty:
                    pass
                time.sleep(0.1)

            # Vide les dernières lignes sans bloquer.
            deadline = time.time() + 1.0
            while time.time() < deadline:
                try:
                    lines.append(q.get_nowait())
                except queue.Empty:
                    break
            rc = proc.returncode if proc else -1
            segments += 1
            seg_len = time.time() - seg_start
            if stopped_by_user or time.time() >= t_end - 2:
                break
            seg_text = "".join(lines[seg_from:])
            # Bibliothèque CUDA introuvable (gpu-burn compilé ailleurs / CUDA non dans le chemin) : une seule nouvelle tentative
            miss = re.search(r"error while loading shared libraries:\s*(\S+?):", seg_text)
            if miss:
                libdirs = _cuda_lib_dirs(miss.group(1)) if not lib_note else []
                if libdirs:
                    env["LD_LIBRARY_PATH"] = ":".join(libdirs + [env.get("LD_LIBRARY_PATH", "")]).strip(":")
                    lib_note = f"{miss.group(1)} trouvée dans {', '.join(libdirs)} : LD_LIBRARY_PATH ajouté pour gpu-burn"
                    lines.append(f"[BenchSuite] {lib_note}\n")
                    continue
                break
            if rc != 0 or re.search(r"ERRORS FOUND|\bDIED?\b", seg_text, re.IGNORECASE):
                break              # vraie erreur : on ne relance pas
            # Sortie « propre » mais trop tôt (ex. arrêt à ~240 s sur certaines machines) : relance pour la durée restante
            if seg_len < float(os.environ.get("BENCHSUITE_BURN_MIN_SEG", "20")) or segments >= 500:
                break              # trop court pour être une charge utile : ne pas boucler indéfiniment
            early_exits.append(round(seg_len))
            lines.append(f"[BenchSuite] gpu-burn s'est arrêté après {seg_len:.0f} s (demandé : {remaining} s) : "
                         f"relance pour la durée restante ({int(t_end - time.time())} s)\n")
    except FileNotFoundError:
        rc = -1
        lines = [f"Introuvable : {cmd[0]}"]
    except Exception as e:
        rc = -3
        lines = [str(e)]
    finally:
        _log_window_wait(stop_event, LOG_POST_SECONDS)
        if proc and proc.poll() is None:
            _terminate_process_tree(proc, grace=0.2)
        stop_event.set()
        poll_thread.join(timeout=3)

    dur = time.time() - t0
    combined = "".join(lines)
    cmd_str_burn = (f"CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES={target_gpus} " if target_gpus else "") + shlex.join(cmd)
    metrics = {
        "precision": precision,
        "time_s": time_s,
        "duration_s": time_s,
        "command": cmd_str_burn,
        "cwd": metrics_cwd,
    }
    if target_gpus:
        metrics["target_gpus"] = target_gpus
    if extra_args:
        metrics["extra_args"] = extra_args
    if stopped_by_user:
        metrics["stopped_by_user"] = True
    if segments > 1:
        metrics["burn_segments"] = segments
    if early_exits:
        metrics["burn_arrets_anticipes_s"] = early_exits
    if lib_note:
        metrics["burn_bibliotheque"] = lib_note
    notes = ""
    if early_exits:
        notes = (f"gpu-burn s'est arrêté seul {len(early_exits)} fois (après {', '.join(str(x) for x in early_exits[:5])} s) "
                 f"avant la durée demandée ({time_s} s) : BenchSuite l'a relancé pour tenir la durée. "
                 "Cause à chercher côté gpu-burn / pilote (voir la sortie brute).")
    miss_final = re.search(r"error while loading shared libraries:\s*(\S+?):", combined)
    if miss_final and (not lib_note or rc != 0):      # pas d'alerte si le chemin ajouté a réglé le problème
        hint = _gpu_burn_lib_hint(miss_final.group(1))
        metrics["erreur"] = hint
        notes = hint
        combined = hint + "\n\n" + combined

    init_failed = False
    if re.search(r"compare kernel|compare\.(fatbin|ptx)", combined, re.IGNORECASE) and \
            re.search(r"not found|couldn't init|could not|no such file|no kernel image", combined, re.IGNORECASE):
        hint = _gpu_burn_compare_hint(combined, metrics_cwd)
        init_failed = True
        metrics["erreur"] = hint
        notes = hint
        combined = hint + "\n\n" + combined

    total_gflops = 0.0
    for mm in re.finditer(r"GPU\s+(\d+)\s*:\s*([\d.]+)\s*Gflop", combined, re.IGNORECASE):
        gid = int(mm.group(1))
        gf  = float(mm.group(2))
        metrics[f"gpu{gid}_Gflops"] = gf
        total_gflops += gf
    burn_err_max = 0
    if total_gflops <= 0:
        # gpu-burn standard / snap : « 33.9% proc'd: 499 (34886 Gflop/s) - 498 (34790 Gflop/s)  errors: 0 - 0  temps: 44 C - 45 C »
        # → une valeur par GPU, dans l'ordre. Le format « GPU 0: 12345 Gflop » ci-dessus n'existe que sur certains forks.
        avg, burn_err_max = _gpu_burn_progress(combined)
        for gid, gf in sorted(avg.items()):
            metrics[f"gpu{gid}_Gflops"] = round(gf, 1)
            total_gflops += gf
    else:
        burn_err_max = _gpu_burn_progress(combined)[1]
    score = round(total_gflops, 1) if total_gflops > 0 else None

    upper = combined.upper()
    # Fin de gpu-burn : « GPU 0: OK » / « GPU 0: FAULTY » ; en cours : « errors: N » par GPU. Avant, seuls « ERRORS FOUND » / « DIED »
    # étaient reconnus : un GPU déclaré FAULTY était noté « ok ».
    faulty = re.search(r"\bFAULTY\b", upper) is not None
    errors_found = "ERRORS FOUND" in upper or "DIED" in upper or " DIE" in upper or faulty or burn_err_max > 0
    if burn_err_max > 0:
        metrics["gpu_burn_erreurs_max"] = burn_err_max
    if faulty:
        metrics["gpu_burn_faulty"] = True
        notes = (notes + "\n" if notes else "") + "gpu-burn déclare au moins un GPU FAULTY (erreurs de calcul détectées)."
    if errors_found:
        metrics["ERREURS_MEMOIRE"] = True

    _add_gpu_series_metrics(metrics, series)

    status = "stopped" if stopped_by_user else ("error" if (errors_found or init_failed or rc not in (0,)) else "ok")
    if cb:
        cb(100, "Arrêté" if stopped_by_user else "Termine")
    return BenchmarkResult("GPU gpu-burn", "GPU", score, "GFLOPS", dur, combined,
                           metrics=metrics, status=status,
                           system_info=get_system_info(), notes=notes, command=cmd_str_burn)


@with_fault_watch
def bench_gpu_fryer(
    time_s: int = 30,
    dtype: str = "float32",
    target_gpus: str = "",
    extra_args: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    target_gpus = _normalize_gpu_list(target_gpus)
    t0 = time.time()
    fryer_cmd = _find_tool_path("gpu-fryer") or _find_tool_path("gpu_fryer")
    if not fryer_cmd:
        return BenchmarkResult("GPU gpu-fryer", "GPU", None, "TFLOPS", 0, "", status="skipped",
                               metrics={"erreur": "gpu-fryer non installe"})

    if cb: cb(5, f"Demarrage gpu-fryer {time_s}s dtype={dtype}…")

    # Séries temporelles remplies dynamiquement par le thread nvidia-smi (multi-GPU)
    series: dict = {"t": []}
    stop_event = threading.Event()
    
    from core.telemetry_aggregator import start_background_telemetry
    start_background_telemetry(series, stop_event.is_set)
    
    poll_thread = threading.Thread(target=_poll_gpu_sensors,
                                   args=(series, stop_event, t0, series_cb, target_gpus), daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    cmd = [fryer_cmd, str(time_s), "--use-bf16", "true" if dtype == "bfloat16" else "false"]
    if extra_args:
        try:
            cmd += shlex.split(str(extra_args))
        except ValueError:
            cmd += str(extra_args).split()
    cmd_str = (f"CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES={target_gpus} " if target_gpus else "") + shlex.join(cmd)
    
    env = os.environ.copy()
    if target_gpus:
        env["CUDA_VISIBLE_DEVICES"] = str(target_gpus)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"   # même numérotation que nvidia-smi (sinon CUDA range du plus rapide au plus lent)
        
    lines = []
    timed_out = False
    stopped_by_user = False
    stderr_out = ""
    proc = None
    q = queue.Queue()

    def _reader(stream, prefix=""):
        try:
            for line in iter(stream.readline, ""):
                if not line:
                    break
                q.put(prefix + line)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1, env=env, start_new_session=True)
        threading.Thread(target=_reader, args=(proc.stdout, ""), daemon=True).start()
        threading.Thread(target=_reader, args=(proc.stderr, "ERR: "), daemon=True).start()

        # Garde-fou : gpu-fryer ajoute à la durée demandée son initialisation et son analyse finale ; s'il ne rend pas la main
        # (blocage CUDA / NVML, GPU en erreur) le test ne doit pas tourner indéfiniment.
        hard_limit = int(time_s * 1.5) + 180
        t_run = time.time()
        timed_out = False
        crashed, crash_t = False, 0.0
        while proc.poll() is None:
            # 5 s de grâce après le message de plantage (le temps de lire la fin de la sortie), puis arrêt.
            if crashed and time.time() - crash_t > 5:
                if cb:
                    cb(95, "gpu-fryer a planté : arrêt…")
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(1.5)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                break
            if time.time() - t_run > hard_limit:
                timed_out = True
                if cb:
                    cb(95, f"gpu-fryer ne s'arrête pas ({hard_limit} s) : arrêt forcé…")
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(1.5)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                break
            if kill_event and kill_event.is_set():
                stopped_by_user = True
                if cb:
                    cb(95, "Arrêt gpu-fryer…")
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(0.8)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                break
            try:
                while True:
                    line = q.get_nowait()
                    lines.append(line)
                    # Plantage interne de gpu-fryer (ex. NVML introuvable) : son thread principal, qui applique la durée,
                    # est mort alors que le calcul GPU continue → il ne s'arrêterait jamais. On le coupe tout de suite.
                    if not crashed and ("panicked at" in line or "Unable to initialize NVML" in line):
                        crashed = True
                        crash_t = time.time()
                    mm = re.search(r"\((\d+)\s+Gflops/s\)", line)
                    if mm and cb:
                        gf = float(mm.group(1))
                        elapsed = time.time() - t0
                        pct = min(90, int(10 + 80 * elapsed / max(time_s, 1)))
                        temp_str  = f"{series['gpu0_temp'][-1]:.0f}°C"   if series.get("gpu0_temp")  else "—"
                        power_str = f"{series['gpu0_power'][-1]:.0f}W"   if series.get("gpu0_power") else "—"
                        cb(pct, f"{gf/1000:.1f} TFLOPS | {temp_str} | {power_str}")
            except queue.Empty:
                pass
            time.sleep(0.1)

        # vider les dernières lignes sans bloquer
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                lines.append(q.get_nowait())
            except queue.Empty:
                break
        rc = proc.returncode if proc else -1
    except FileNotFoundError:
        rc = -1
        lines = [f"Introuvable : {fryer_cmd}"]
    except Exception as e:
        rc = -3
        lines = [str(e)]
    finally:
        _log_window_wait(stop_event, LOG_POST_SECONDS)
        if proc:
            if proc.poll() is None:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
            for fh in (proc.stdout, proc.stderr):
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
        stop_event.set()
        poll_thread.join(timeout=3)

    dur = time.time() - t0
    combined = "".join(lines)
    if stderr_out:
        combined += "\n--- stderr ---\n" + stderr_out

    metrics: dict = {"dtype": dtype, "time_s": time_s, "duration_s": time_s, "command": cmd_str}
    if target_gpus:
        metrics["target_gpus"] = target_gpus
    if extra_args:
        metrics["extra_args"] = extra_args

    # Score : somme des TFLOPS de tous les GPUs (ou premier GPU trouvé)
    score = None
    total_gflops = 0.0
    for mm in re.finditer(r"GPU\s+#(\d+):\s+([\d.]+)\s+Gflops/s", combined, re.IGNORECASE):
        gid = int(mm.group(1))
        gf  = float(mm.group(2))
        metrics[f"gpu{gid}_Gflops"] = gf
        total_gflops += gf
    if total_gflops > 0:
        score = round(total_gflops / 1000, 3)

    # Agrégats par GPU
    n_gpu = sum(1 for k in series if re.match(r"^gpu\d+_temp$", k))
    for i in range(n_gpu):
        temps  = series.get(f"gpu{i}_temp",          [])
        powers = series.get(f"gpu{i}_power",         [])
        freqs  = series.get(f"gpu{i}_freq_graphics", [])
        pfx = f"gpu{i}_"
        if temps:
            metrics[pfx + "temp_max_c"]  = max(temps)
            metrics[pfx + "temp_moy_c"]  = round(sum(temps) / len(temps), 1)
        if powers:
            metrics[pfx + "power_max_w"] = max(powers)
            metrics[pfx + "power_moy_w"] = round(sum(powers) / len(powers), 1)
        if freqs:
            metrics[pfx + "freq_min_mhz"] = min(freqs)
            metrics[pfx + "freq_max_mhz"] = max(freqs)

    # Séries complètes (pour graphique PDF, JSON et live chart)
    _add_gpu_series_metrics(metrics, series)

    status, notes = "ok", ""
    ran = max(0.0, dur - LOG_PRE_SECONDS - LOG_POST_SECONDS)
    metrics["duree_reelle_s"] = round(ran, 1)
    if "Unable to initialize NVML" in combined or "libnvidia-ml" in combined:
        # GPU vu via CUDA mais NVML non chargé par gpu-fryer (bibliothèque absente, lien cassé ou chemin codé en dur) : il plante.
        status = "error"
        metrics["erreur"] = "NVML introuvable (libnvidia-ml.so.1)"
        metrics["arret_force"] = True
        import platform
        arch_dir = f"/usr/lib/{platform.machine()}-linux-gnu"
        if "/usr/lib/x86_64-linux-gnu/libnvidia-ml" in combined and platform.machine() != "x86_64":
            # Cas constaté (Ampere Altra + H200) : gpu-fryer 1.0.0 cherche NVML à un chemin ABSOLU x86_64, que LD_LIBRARY_PATH ne
            # change pas. Un lien vers la vraie bibliothèque suffit (hypothèse à confirmer : non testé sur la machine).
            notes = ("gpu-fryer a planté : il cherche NVML à un chemin x86_64 écrit en dur "
                     "(/usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1) alors que cette machine est en "
                     f"{platform.machine()}. LD_LIBRARY_PATH n'y change rien (chemin absolu). Solution à essayer : "
                     f"`sudo mkdir -p /usr/lib/x86_64-linux-gnu && sudo ln -s {arch_dir}/libnvidia-ml.so.1 /usr/lib/x86_64-linux-gnu/libnvidia-ml.so.1`, "
                     "puis relancer. Un gpu-fryer resté lancé continue de charger le GPU : `pkill -9 gpu-fryer`.")
        else:
            notes = ("gpu-fryer a planté : il n'a pas pu charger NVML (libnvidia-ml.so.1) et ne s'arrête alors plus tout seul. "
                     "Le GPU est vu (CUDA), donc le pilote est là. À vérifier : `ldconfig -p | grep nvidia-ml`, `ls -l /usr/lib/*/libnvidia-ml.so*` "
                     "(lien cassé ?) et `nvidia-smi`. Si elle est absente : Ubuntu "
                     "`sudo apt install libnvidia-compute-<version>-server nvidia-utils-<version>-server`, "
                     "puis `sudo ldconfig`. Un gpu-fryer resté lancé continue de charger le GPU : `pkill -9 gpu-fryer`.")
    elif stopped_by_user:
        status = "stopped"
        notes = f"gpu-fryer interrompu après {int(ran)} s."
    elif timed_out:
        status = "error" if score is None else "ok"
        notes = (f"gpu-fryer n'a pas rendu la main après {int(time_s * 1.5) + 180} s pour une durée demandée de {time_s} s : arrêt forcé. "
                 "Voir la sortie brute (dernière ligne affichée par gpu-fryer) ; `gpu-fryer --help` donne les options de la version installée.")
        metrics["arret_force"] = True
    elif rc not in (0, None):
        status = "error"
        metrics["erreur"] = f"gpu-fryer a échoué (code retour {rc})"
        notes = f"gpu-fryer s'est terminé avec le code retour {rc} — voir la sortie brute."
    elif ran > time_s * 1.15 + 30:
        notes = (f"gpu-fryer a tourné {int(ran)} s pour {time_s} s demandées : il ajoute son initialisation et son analyse finale "
                 "à la durée du burn.")
    if cb: cb(100, "Termine")
    return BenchmarkResult("GPU gpu-fryer", "GPU", score, "TFLOPS", dur, combined, status=status, notes=notes,
                           metrics=metrics, system_info=get_system_info(), command=cmd_str)


def parse_nvbandwidth_text_output(text: str) -> list:
    """Analyse la sortie texte brute de nvbandwidth et la convertit en structure testcases compatible JSON."""
    testcases = []
    
    # Nettoyer les codes ANSI de couleur
    ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
    text = ansi_escape.sub('', text)
    
    lines = text.splitlines()
    i = 0
    cur_test = None
    cur_tc_results = {}
    
    num_re = re.compile(r'^(?:NA|nan|[-+]?\d+(?:\.\d+)?)$', re.I)
    
    # Stocker les dernières lignes pour retrouver le titre exact
    last_non_empty_lines = []
    
    while i < len(lines):
        line = lines[i].rstrip('\n')
        s_line = line.strip()
        if not s_line:
            i += 1
            continue
            
        m = re.search(r'Running\s+([^\.\s]+)\.?', line)
        if m:
            if cur_test and cur_tc_results:
                testcases.append({
                    "name": cur_test,
                    "results": cur_tc_results
                })
            cur_test = m.group(1)
            cur_tc_results = {}
            last_non_empty_lines = []
            i += 1
            continue
            
        # Si on a un test courant, on cherche la ligne d'en-tête numérique de la matrice (ex: "  0   1")
        if cur_test:
            tokens = s_line.split()
            if tokens and all(re.match(r'^\d+$', t) for t in tokens):
                cols = tokens
                
                # Chercher le titre de la métrique dans les lignes précédentes
                title = "Bandwidth / Latency Matrix"
                for prev in reversed(last_non_empty_lines):
                    if "bandwidth" in prev.lower() or "latency" in prev.lower() or "matrix" in prev.lower():
                        title = prev.strip()
                        break
                        
                unit = 'GB/s' if 'gb/s' in title.lower() else ('us' if 'us' in title.lower() else ('ns' if 'ns' in title.lower() else ''))
                
                k = i + 1
                matrix = []
                while k < len(lines):
                    toks = lines[k].split()
                    if not toks:
                        break
                    if re.search(r'Running\s+[^\.\s]+\.?', lines[k]):
                        break
                    if not re.match(r'^\d+$', toks[0]):
                        break
                        
                    r_idx = int(toks[0])
                    vals = toks[1:]
                    
                    while len(matrix) <= r_idx:
                        matrix.append([])
                        
                    row_vals = []
                    for col, val in zip(cols, vals):
                        c_idx = int(col)
                        while len(row_vals) <= c_idx:
                            row_vals.append(None)
                            
                        if num_re.match(val):
                            try:
                                f_val = float(val) if val.upper() != 'NA' else None
                                row_vals[c_idx] = f_val
                            except Exception:
                                row_vals[c_idx] = None
                                
                    matrix[r_idx] = row_vals
                    k += 1
                    
                flat_vals = [v for r_arr in matrix for v in r_arr if isinstance(v, (int, float))]
                if flat_vals:
                    cur_tc_results["average"] = round(sum(flat_vals) / len(flat_vals), 2)
                    cur_tc_results["min"] = round(min(flat_vals), 2)
                    cur_tc_results["max"] = round(max(flat_vals), 2)
                    
                cur_tc_results["bandwidth_matrix"] = matrix
                i = k
                continue
                
        last_non_empty_lines.append(line)
        if len(last_non_empty_lines) > 5:
            last_non_empty_lines.pop(0)
        i += 1
        
    if cur_test and cur_tc_results:
        testcases.append({
            "name": cur_test,
            "results": cur_tc_results
        })
        
    return testcases


@with_fault_watch
def bench_gpu_nvbandwidth(
    buffer_size_mb: int = 512,
    iterations: int = 3,
    target_gpus: str = "",
    extra_args: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    target_gpus = _normalize_gpu_list(target_gpus)
    t0 = time.time()
    cmd_path = _find_tool_path("nvbandwidth")
    if not cmd_path:
        return BenchmarkResult("GPU nvbandwidth", "GPU", None, "GB/s", 0, "", status="skipped",
                               metrics={"erreur": "nvbandwidth non installe ou hors PATH"})
    if cb: cb(5, "Demarrage nvbandwidth…")

    # Arguments CLI officiels et corrects pour nvbandwidth
    cmd = [cmd_path, "--bufferSize", str(buffer_size_mb), "--testSamples", str(iterations)]
    if extra_args:
        try:
            cmd += shlex.split(str(extra_args))
        except Exception:
            cmd += str(extra_args).split()

    cmd_str = (f"CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES={target_gpus} " if target_gpus else "") + shlex.join(cmd)
    env = os.environ.copy()
    for k in ("GSK_RENDERER", "GDK_BACKEND"):
        env.pop(k, None)
    if target_gpus:
        env["CUDA_VISIBLE_DEVICES"] = str(target_gpus)
        env["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"   # même numérotation que nvidia-smi (sinon CUDA range du plus rapide au plus lent)
        

    # Monitoring temps reel en direct (chauffe GPU)
    series: dict = {"t": []}
    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_poll_gpu_sensors,
                                   args=(series, stop_event, t0, series_cb, target_gpus), daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    stopped_by_user = False
    timed_out = False
    proc = None
    stdout, stderr = "", ""
    import tempfile
    f_out = f_err = None
    try:
        f_out = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        f_err = tempfile.TemporaryFile(mode="w+", encoding="utf-8", errors="replace")
        proc = subprocess.Popen(
            cmd, stdout=f_out, stderr=f_err,
            text=True, env=env, start_new_session=True,
        )

        estimated_duration = max(5, iterations * 4)
        hard_limit = max(300, int(iterations) * 120)
        t_run = time.time()
        while proc.poll() is None:
            if kill_event and kill_event.is_set():
                stopped_by_user = True
                if cb: cb(95, "Arret nvbandwidth…")
                _terminate_process_tree(proc, grace=0.8)
                break
            if time.time() - t_run > hard_limit:
                timed_out = True
                if cb: cb(95, f"nvbandwidth ne répond plus ({hard_limit} s) : arrêt forcé…")
                _terminate_process_tree(proc, grace=0.8)
                break

            elapsed = time.time() - t0
            pct = min(90, int(10 + 80 * elapsed / estimated_duration))

            temp_str  = f"{series.get('gpu0_temp', ['—'])[-1]:.0f}°C" if series.get("gpu0_temp") else "—"
            power_str = f"{series.get('gpu0_power', ['—'])[-1]:.0f}W" if series.get("gpu0_power") else "—"
            if cb:
                cb(pct, f"Bande passante GPU… | {temp_str} | {power_str}")
            time.sleep(0.2)

        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate_process_tree(proc, grace=0.5)
        f_out.seek(0); stdout = f_out.read()
        f_err.seek(0); stderr = f_err.read()
        rc = -2 if timed_out else (proc.returncode if proc.returncode is not None else -3)
    except Exception as e:
        rc = -3
        stdout, stderr = "", str(e)
    finally:
        for fh in (f_out, f_err):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass

    _log_window_wait(stop_event, LOG_POST_SECONDS)
    stop_event.set()
    poll_thread.join(timeout=3)

    dur = time.time() - t0
    combined = stdout
    if stderr:
        combined += "\n--- stderr ---\n" + stderr

    metrics: dict = {
        "command": cmd_str,
        "buffer_size_mb": buffer_size_mb,
        "iterations": iterations,
        "duration_s": round(dur, 1),
    }
    if target_gpus:
        metrics["target_gpus"] = target_gpus
    if extra_args:
        metrics["extra_args"] = str(extra_args).strip()

    score = None
    testcases = []
    
    # 1. Tenter le parsing du format JSON si stdout contient du JSON
    if rc == 0 and stdout.strip():
        start_idx = stdout.find('{')
        if start_idx != -1:
            try:
                # Recherche robuste de l'accolade fermante correspondante pour isoler le JSON
                json_str = ""
                brace_count = 0
                in_quote = False
                escape = False
                for idx in range(start_idx, len(stdout)):
                    char = stdout[idx]
                    if escape:
                        escape = False
                        continue
                    if char == '\\':
                        escape = True
                        continue
                    if char == '"':
                        in_quote = not in_quote
                        continue
                    if not in_quote:
                        if char == '{':
                            brace_count += 1
                        elif char == '}':
                            brace_count -= 1
                            if brace_count == 0:
                                json_str = stdout[start_idx:idx+1]
                                break
                if not json_str:
                    # Fallback au dernier '}'
                    last_brace = stdout.rfind('}')
                    if last_brace != -1 and last_brace > start_idx:
                        json_str = stdout[start_idx:last_brace+1]
                    else:
                        json_str = stdout[start_idx:]
                
                data = json.loads(json_str)
                raw_tcs = data.get("testcases")
                if not raw_tcs and "nvbandwidth" in data:
                    raw_tcs = data.get("nvbandwidth", {}).get("testcases")
                
                if isinstance(raw_tcs, list):
                    normalized_tcs = []
                    for tc in raw_tcs:
                        if not isinstance(tc, dict):
                            continue
                        name = tc.get("name")
                        
                        # Si déjà formaté avec "results", conserver
                        if "results" in tc and isinstance(tc["results"], dict):
                            normalized_tcs.append(tc)
                            continue
                        
                        # Sinon, normaliser et construire le dictionnaire "results"
                        results_dict = {}
                        matrix = tc.get("bandwidth_matrix") or tc.get("latency_matrix") or tc.get("matrix")
                        
                        float_matrix = None
                        if isinstance(matrix, list):
                            float_matrix = []
                            for row in matrix:
                                if isinstance(row, list):
                                    float_row = []
                                    for val in row:
                                        if val is None or str(val).upper() == "N/A":
                                            float_row.append(None)
                                        else:
                                            try:
                                                float_row.append(float(val))
                                            except Exception:
                                                float_row.append(None)
                                    float_matrix.append(float_row)
                                else:
                                    float_matrix.append(None)
                                    
                        if float_matrix:
                            results_dict["bandwidth_matrix"] = float_matrix
                            flat_vals = [v for r_arr in float_matrix if isinstance(r_arr, list) for v in r_arr if isinstance(v, (int, float))]
                            if flat_vals:
                                results_dict["average"] = round(sum(flat_vals) / len(flat_vals), 2)
                                results_dict["min"] = round(min(flat_vals), 2)
                                results_dict["max"] = round(max(flat_vals), 2)
                                
                        sum_val = tc.get("sum")
                        if sum_val is not None:
                            try:
                                results_dict["sum"] = round(float(sum_val), 2)
                            except Exception:
                                pass
                                
                        normalized_tcs.append({
                            "name": name,
                            "results": results_dict
                        })
                    testcases = normalized_tcs
            except Exception as je:
                combined += f"\n[Parser JSON] Echec de lecture ({je})."

    # 2. Si aucun JSON n'a été parsé, fallback sur l'analyseur de texte de référence
    if not testcases and stdout.strip():
        try:
            testcases = parse_nvbandwidth_text_output(stdout)
        except Exception as te:
            combined += f"\n[Parser Texte] Echec de lecture ({te})."

    # 3. Extraction générique des métriques pour les testcases trouvés
    if isinstance(testcases, list) and testcases:
        total_bw = 0.0
        count_bw = 0
        max_bw = 0.0
        min_bw = float('inf')
        
        for tc in testcases:
            tc_name = tc.get("name") or "memcpy"
            tc_results = tc.get("results") or {}
            
            avg_val = tc_results.get("average") or tc_results.get("mean") or tc_results.get("bandwidth")
            sum_val = tc_results.get("sum")
            min_val = tc_results.get("min")
            max_val = tc_results.get("max")
            cov_val = tc_results.get("coefficient_of_variation") or tc_results.get("cov")
            
            if isinstance(avg_val, (int, float)):
                total_bw += float(avg_val)
                count_bw += 1
                max_bw = max(max_bw, float(avg_val))
                min_bw = min(min_bw, float(avg_val))
                
                metrics[f"bw_{tc_name}_avg_gb_s"] = round(float(avg_val), 2)
            
            if isinstance(sum_val, (int, float)):
                metrics[f"bw_{tc_name}_sum_gb_s"] = round(float(sum_val), 2)
            if isinstance(min_val, (int, float)):
                metrics[f"bw_{tc_name}_min_gb_s"] = round(float(min_val), 2)
            if isinstance(max_val, (int, float)):
                metrics[f"bw_{tc_name}_max_gb_s"] = round(float(max_val), 2)
            if isinstance(cov_val, (int, float)):
                metrics[f"bw_{tc_name}_cov"] = round(float(cov_val), 4)

            matrix = tc_results.get("bandwidth_matrix") or tc_results.get("matrix")
            if isinstance(matrix, list):
                for r_idx, row in enumerate(matrix):
                    if isinstance(row, list):
                        for c_idx, val in enumerate(row):
                            if isinstance(val, (int, float)) and r_idx != c_idx:
                                metrics[f"gpu{r_idx}_to_gpu{c_idx}_gb_s"] = round(float(val), 2)
                                total_bw += float(val)
                                count_bw += 1
                                max_bw = max(max_bw, float(val))
                                min_bw = min(min_bw, float(val))

        if count_bw > 0:
            score = round(total_bw / count_bw, 2)
            metrics["bandwidth_moy_gb_s"] = score
            metrics["bandwidth_max_gb_s"] = round(max_bw, 2)
            if min_bw != float('inf'):
                metrics["bandwidth_min_gb_s"] = round(min_bw, 2)
        metrics["nvbandwidth_data"] = testcases
        tc_names = [str(tc.get("name", "")).strip() for tc in testcases if tc.get("name")]
        if tc_names:
            metrics["nvbandwidth_tests"] = tc_names

    # 4. Si toujours rien n'a été trouvé, fallback sur la recherche regex globale simple
    if score is None:
        sums = [float(x) for x in re.findall(r"(?:Sum|Total)\s*:\s*([\d.]+)\s*(?:GB/s)?", combined, re.IGNORECASE)]
        avgs = [float(x) for x in re.findall(r"(?:Average|Mean|Moyenne)\s*:\s*([\d.]+)\s*(?:GB/s)?", combined, re.IGNORECASE)]
        all_vals = [float(x) for x in re.findall(r"\b([\d.]+)\s*GB/s\b", combined, re.IGNORECASE)]
        
        if avgs:
            score = round(sum(avgs) / len(avgs), 2)
            metrics["bandwidth_moy_gb_s"] = score
        elif all_vals:
            score = round(sum(all_vals) / len(all_vals), 2)
            metrics["bandwidth_moy_gb_s"] = score
            
        if sums:
            metrics["bandwidth_sum_gb_s"] = round(sums[0], 2)
        if all_vals:
            metrics["bandwidth_max_gb_s"] = round(max(all_vals), 2)
            metrics["bandwidth_min_gb_s"] = round(min(all_vals), 2)

    # 5. Si le score global a été extrait mais que la structure testcases est restée vide,
    # fabriquer un testcase virtuel pour forcer l'affichage de l'onglet HTML avec ces données.
    if score is not None and not testcases:
        testcases = [{
            "name": "memcpy_fallback_gen",
            "results": {
                "average": score,
                "min": metrics.get("bandwidth_min_gb_s", score),
                "max": metrics.get("bandwidth_max_gb_s", score),
                "sum": metrics.get("bandwidth_sum_gb_s")
            }
        }]
        metrics["nvbandwidth_data"] = testcases

    _add_gpu_series_metrics(metrics, series)

    status = "ok"
    if stopped_by_user:
        status = "stopped"
    elif timed_out:
        status = "error"
        metrics["erreur"] = "nvbandwidth a dépassé le délai maximal (arrêt forcé)"
    elif rc != 0 and score is None:
        status = "error"
        metrics["erreur"] = f"Code retour {rc}"

    if cb:
        msg = f"Termine ({score} GB/s)" if score else "Termine"
        cb(100, msg)

    return BenchmarkResult(
        "GPU nvbandwidth", "GPU", score, "GB/s", dur, combined,
        status=status, metrics=metrics, system_info=get_system_info(), command=cmd_str
    )


def bench_gpu_vram_test(
    time_s: int = 300,
    vram_pct: int = 95,
    precision: str = "float",
    target_gpus: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
    series_cb: SeriesCb = None,
) -> BenchmarkResult:
    """Intégrité de la mémoire GPU : gpu-burn remplit `vram_pct` % de la VRAM et compare les résultats
    (toute erreur de calcul/mémoire est détectée)."""
    pct = max(10, min(int(vram_pct), 98))
    res = bench_gpu_burn(time_s=time_s, precision=precision, target_gpus=target_gpus,
                         extra_args=f"-m {pct}%", cb=cb, kill_event=kill_event, series_cb=series_cb)
    res.name = f"GPU — Intégrité VRAM ({pct} %)"
    res.metrics["vram_pct"] = pct
    res.metrics["test_kind"] = "vram_integrity"
    return res
