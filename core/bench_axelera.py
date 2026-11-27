"""
bench_axelera.py — Exécution des tests de chauffe, stabilité et inférence sur cartes Axelera AI (Metis).

Deux moteurs du Voyager SDK sont gérés :
- `axrunmodel` (par défaut pour chauffe / stabilité) : exécute un modèle compilé (`model.json`) directement
  sur l'AIPU en boucle pendant `time_s` secondes, sans goulot d'entrée vidéo.
- `inference.py` : pipeline complet (décodage + pré/post-traitement + AIPU) sur une vidéo ou un flux,
  en mode `--no-display`, répété jusqu'à atteindre `time_s` secondes si demandé.

Intégré à `@with_fault_watch` (journal noyau `axl` / `metis` / PCIe, inventaire avant/après, liens PCIe,
relevé électrique) et à la télémétrie en direct (`AxeleraSampler`).
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional

from core import axelera_aipu as ax
from core.bench_utils import (
    LOG_POST_SECONDS,
    LOG_PRE_SECONDS,
    ProgressCb,
    SeriesCb,
    _drop_empty_series,
    _log_window_wait,
    _terminate_process_tree,
)
from core.fault_monitor import with_fault_watch
from core.result import BenchmarkResult


def parse_axrunmodel_output(text: str) -> Dict:
    """Extrait Device FPS, Host FPS, System FPS et erreurs éventuelles de la sortie `axrunmodel`."""
    out: Dict = {}
    if not text:
        return out
    for key, pat in (
        ("device_fps", r"Device\s+FPS\s*[:=]\s*([\d.]+)"),
        ("host_fps", r"Host\s+FPS\s*[:=]\s*([\d.]+)"),
        ("system_fps", r"System\s+FPS\s*[:=]\s*([\d.]+)"),
    ):
        matches = re.findall(pat, text, re.I)
        if matches:
            try:
                out[key] = round(float(matches[-1]), 2)
            except ValueError:
                pass
    return out


def parse_inference_output(text: str) -> Dict:
    """Extrait System FPS, Device/Host FPS, CPU usage et Core Temp de `inference.py`."""
    out: Dict = {}
    if not text:
        return out
    for key, pat in (
        ("system_fps", r"(?:System\s+FPS|End-to-end\s+average\s+measurement)\s*[:=]?\s*([\d.,]+)"),
        ("device_fps", r"Device\s+FPS\s*[:=]\s*([\d.,]+)"),
        ("host_fps", r"Host\s+FPS\s*[:=]\s*([\d.,]+)"),
        ("cpu_usage_pct", r"CPU\s*(?:usage|%)\s*[:=]\s*([\d.,]+)\s*%?"),
    ):
        matches = re.findall(pat, text, re.I)
        if matches:
            try:
                out[key] = round(float(matches[-1].replace(",", "")), 2)
            except ValueError:
                pass
    temps = re.findall(r"Core\s*Temp\s*[:=]\s*(-?[\d.]+)\s*°?\s*C", text, re.I)
    if temps:
        try:
            vals = [float(t) for t in temps]
            out["core_temp_c"] = max(vals)
            out["core_temps_seen"] = vals
        except ValueError:
            pass
    return out


def _detect_runtime_error(text: str) -> str:
    """Repère les erreurs critiques documentées dans la sortie Voyager SDK."""
    if not text:
        return ""
    for pat, msg in (
        (r"Uio wait kernel failed with return code -1400",
         "Erreur runtime Metis (-1400) : essayer `axdevice --refresh` ou vérifier le lien PCIe"),
        (r"No\s+(?:Metis\s+)?devices?\s+found|PCI device count mismatch",
         "Aucune carte Axelera détectée par le runtime (`axdevice --refresh` / vérifier `lsmod | grep metis`)"),
        (r"Fail to dma alloc",
         "Échec allocation DMA 4 Mo dans le pilote `metis` (compacter la mémoire ou redémarrer)"),
    ):
        if re.search(pat, text, re.I):
            return msg
    return ""


def _resolve_model_and_engine(model_path: str, engine: str, sdk_dir: str) -> tuple:
    """Résout (moteur_effectif, modèle_résolu, raison_erreur)."""
    models = ax.discover_models(sdk_dir=sdk_dir or None)
    chosen = (model_path or "").strip()

    if not chosen:
        # Priorité : modèle compilé déjà présent dans build/ / cache pour axrunmodel
        comp = next((m for m in models if m["kind"] == "compiled"), None)
        if comp:
            chosen = comp["path"]
        else:
            zoo = next((m for m in models if m["kind"] in ("zoo_yaml", "axzoo")), None)
            if zoo:
                chosen = zoo["name"]

    if not chosen:
        return "", "", ("Aucun modèle compilé (`model.json`) ni modèle Zoo trouvé. "
                        "Sélectionnez un `model.json` ou téléchargez un modèle via `axdownloadmodel`.")

    ok, resolved_or_err = ax.validate_model_path(chosen)
    if not ok:
        return "", "", resolved_or_err

    eff_engine = (engine or "auto").strip().lower()
    if eff_engine == "auto":
        if resolved_or_err.endswith((".json", ".axmodel", ".axm", ".axe")) and os.path.isfile(resolved_or_err):
            eff_engine = "axrunmodel"
        else:
            eff_engine = "inference"
    return eff_engine, resolved_or_err, ""


def build_axelera_command(
    engine: str,
    model: str,
    time_s: int = 60,
    aipu_cores: int = 4,
    target_devices: str = "",
    media_path: str = "",
    throttle_fps: int = 0,
    sdk_dir: str = "",
    extra_args: str = "",
) -> tuple:
    """Construit `(cmd_list, cwd, err_msg)` pour `axrunmodel` ou `inference.py`."""
    sdk = sdk_dir or ax.find_sdk_dir()
    extra = shlex.split(extra_args) if (extra_args and extra_args.strip()) else []

    if engine == "axrunmodel":
        axrun = ax.find_tool("axrunmodel")
        if not axrun:
            return [], None, "Outil `axrunmodel` introuvable (Voyager SDK requis)."
        cmd = [axrun, model]
        if target_devices and target_devices.strip():
            cmd.extend(["-d", target_devices.strip()])
        if int(time_s or 0) > 0:
            cmd.extend(["--seconds", str(int(time_s))])
        if int(aipu_cores or 0) > 0:
            cmd.extend(["--aipu-cores", str(max(1, min(4, int(aipu_cores))))])
        if int(throttle_fps or 0) > 0:
            cmd.extend(["--throttle-fps", str(int(throttle_fps))])
        cmd.extend(extra)
        return cmd, (sdk or None), ""

    # inference.py
    inf_py = os.path.join(sdk, "inference.py") if sdk else ""
    if not inf_py or not os.path.isfile(inf_py):
        return [], None, "Script `inference.py` introuvable : indiquez le dossier du Voyager SDK."
    media = (media_path or "").strip()
    if not media:
        medias = ax.discover_media(sdk)
        media = medias[0] if medias else ""
    if not media:
        return [], None, (
            "Aucune source vidéo/média trouvée pour `inference.py`. "
            "Sélectionnez un modèle compilé (`model.json`) ou cliquez sur ⬇ Télécharger (`axdownloadmodel`), "
            "ou indiquez une vidéo/image dans « Source vidéo / média »."
        )
    py = ax.find_python_for_sdk()
    cmd = [py, inf_py, model, media, "--no-display", "--show-host-fps"]
    if int(aipu_cores or 0) > 0:
        cmd.extend(["--aipu-cores", str(max(1, min(4, int(aipu_cores))))])
    cmd.extend(extra)
    return cmd, sdk, ""


@with_fault_watch
def bench_axelera_stress(
    model_path: str = "",
    engine: str = "auto",
    media_path: str = "",
    time_s: int = 60,
    aipu_cores: int = 4,
    target_devices: str = "",
    target_gpus: str = "",
    throttle_fps: int = 0,
    sdk_dir: str = "",
    extra_args: str = "",
    cb: ProgressCb = None,
    series_cb: SeriesCb = None,
    kill_event: "threading.Event | None" = None,
    log_cb: "Callable[[str], None] | None" = None,
) -> BenchmarkResult:
    """Lance un test de chauffe / stabilité / inférence sur une ou plusieurs cartes Axelera Metis."""
    t0 = time.time()
    devs_arg = (target_devices or target_gpus or "").strip()
    eff_engine, resolved_model, mod_err = _resolve_model_and_engine(model_path, engine, sdk_dir)
    if mod_err:
        if log_cb:
            log_cb(f"Ignoré : {mod_err}")
        return BenchmarkResult(
            "Axelera — Chauffe & Stabilité AIPU",
            "Axelera AI",
            None,
            "FPS",
            0.0,
            mod_err,
            status="skipped",
            metrics={"erreur": mod_err},
            notes=mod_err,
        )

    cmd, cwd, cmd_err = build_axelera_command(
        eff_engine,
        resolved_model,
        time_s=time_s,
        aipu_cores=aipu_cores,
        target_devices=devs_arg,
        media_path=media_path,
        throttle_fps=throttle_fps,
        sdk_dir=sdk_dir,
        extra_args=extra_args,
    )
    if cmd_err:
        if log_cb:
            log_cb(f"Ignoré : {cmd_err}")
        return BenchmarkResult(
            "Axelera — Chauffe & Stabilité AIPU",
            "Axelera AI",
            None,
            "FPS",
            0.0,
            cmd_err,
            status="skipped",
            metrics={"erreur": cmd_err},
            notes=cmd_err,
        )

    cmd_str = " ".join(shlex.quote(c) for c in cmd)
    if cb:
        cb(3, f"Démarrage {eff_engine} ({os.path.basename(resolved_model)})…")

    # Télémétrie temps réel en arrière-plan
    series: Dict[str, List] = {
        "t": [],
        "temp": [],
        "power": [],
        "freq_graphics": [],
        "mvm_util": [],
        "kps": [],
        "pcie_bw": [],
    }
    stop_event = threading.Event()
    sampler = ax.AxeleraSampler(interval_s=1.0)
    sampler.start()
    runtime_temp_lock = threading.Lock()
    runtime_temp_latest: Dict[str, Optional[float]] = {"temp": None}

    def _poll_loop() -> None:
        own_keys = set()
        sampler.ready.wait(timeout=2.0)
        while not stop_event.is_set():
            snap = sampler.latest()
            devs = snap.get("devices") or {}
            pt: Dict = {"t": round(time.time() - t0, 1)}

            for idx, d in sorted(devs.items()):
                pfx = f"aipu{idx}_"
                if d.get("temp_c") is not None:
                    pt[pfx + "temp"] = d["temp_c"]
                if d.get("power_w") is not None:
                    pt[pfx + "power"] = d["power_w"]
                if d.get("clock_mhz") is not None:
                    pt[pfx + "clock_mhz"] = d["clock_mhz"]
                if d.get("mvm_pct") is not None:
                    pt[pfx + "mvm_pct"] = d["mvm_pct"]
                if d.get("kps") is not None:
                    pt[pfx + "kps"] = d["kps"]
                if d.get("pcie_bw_mbs") is not None:
                    pt[pfx + "pcie_bw"] = d["pcie_bw_mbs"]
                if "throttle_thermal" in d:
                    pt[pfx + "throttle_thermal"] = d["throttle_thermal"]

            # Agrégats principaux (carte 0 ou moyenne / max multi-cartes)
            temps = [d["temp_c"] for d in devs.values() if d.get("temp_c") is not None]
            if not temps:
                with runtime_temp_lock:
                    if runtime_temp_latest["temp"] is not None:
                        temps = [runtime_temp_latest["temp"]]
            if temps:
                pt["temp"] = max(temps)
            powers = [d["power_w"] for d in devs.values() if d.get("power_w") is not None]
            if powers:
                pt["power"] = round(sum(powers), 2)
            clocks = [d["clock_mhz"] for d in devs.values() if d.get("clock_mhz") is not None]
            if clocks:
                pt["freq_graphics"] = round(sum(clocks) / len(clocks), 1)
            mvms = [d["mvm_pct"] for d in devs.values() if d.get("mvm_pct") is not None]
            if mvms:
                pt["mvm_util"] = round(sum(mvms) / len(mvms), 1)
            kpss = [d["kps"] for d in devs.values() if d.get("kps") is not None]
            if kpss:
                pt["kps"] = round(sum(kpss), 1)
            bws = [d["pcie_bw_mbs"] for d in devs.values() if d.get("pcie_bw_mbs") is not None]
            if bws:
                pt["pcie_bw"] = round(sum(bws), 1)

            series["t"].append(pt["t"])
            n_t = len(series["t"])
            for k, v in pt.items():
                if k == "t":
                    continue
                if k not in series:
                    series[k] = [None] * (n_t - 1)
                series[k].append(v)
                own_keys.add(k)
            for k in own_keys:
                lst = series.get(k)
                if isinstance(lst, list) and len(lst) < n_t:
                    lst.append(lst[-1] if lst else None)

            if series_cb is not None:
                try:
                    series_cb(pt)
                except Exception:
                    pass
            stop_event.wait(1.0)

    poll_thread = threading.Thread(target=_poll_loop, daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    # Exécution du moteur (en boucle si inference.py finit avant `time_s`)
    duration_target = max(0, int(time_s or 0))
    run_start = time.time()
    output_chunks: List[str] = []
    last_rc = 0
    stopped_by_user = False
    timed_out = False
    passes = 0
    env = ax.sdk_env(configure_board=False)

    while True:
        if kill_event and kill_event.is_set():
            stopped_by_user = True
            break
        passes += 1
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                cwd=cwd,
                env=env,
                start_new_session=True,
            )
        except Exception as exc:
            last_rc = -1
            output_chunks.append(f"Erreur lancement : {exc}")
            break

        def _reader() -> None:
            assert proc.stdout is not None
            try:
                for raw in proc.stdout:
                    output_chunks.append(raw)
                    m_temp = re.search(r"Core\s*Temp\s*[:=]\s*(-?[\d.]+)\s*°?\s*C", raw, re.I)
                    if m_temp:
                        try:
                            with runtime_temp_lock:
                                runtime_temp_latest["temp"] = float(m_temp.group(1))
                        except ValueError:
                            pass
                    if log_cb is not None:
                        try:
                            log_cb(raw.rstrip("\n"))
                        except Exception:
                            pass
            except Exception:
                pass

        rt = threading.Thread(target=_reader, daemon=True)
        rt.start()

        hard_deadline = run_start + (duration_target + 180 if duration_target > 0 else 1800)
        while proc.poll() is None:
            if kill_event and kill_event.is_set():
                stopped_by_user = True
                _terminate_process_tree(proc)
                break
            now = time.time()
            if eff_engine == "inference" and duration_target > 0 and (now - run_start) >= duration_target:
                _terminate_process_tree(proc)
                break
            if now >= hard_deadline:
                timed_out = True
                _terminate_process_tree(proc)
                break
            if cb:
                elapsed = now - run_start
                if duration_target > 0:
                    pct = min(95, 5 + int(90 * elapsed / max(1, duration_target)))
                    cb(pct, f"{eff_engine} : {elapsed:.0f}s / {duration_target}s (passe {passes})…")
                else:
                    cb(50, f"{eff_engine} en cours ({elapsed:.0f}s)…")
            time.sleep(0.4)

        rt.join(timeout=2.0)
        last_rc = proc.returncode if proc.returncode is not None else 0

        if stopped_by_user or timed_out:
            break
        if last_rc != 0:
            break
        if eff_engine == "axrunmodel":
            break
        if duration_target <= 0 or (time.time() - run_start) >= duration_target:
            break

    _log_window_wait(stop_event, LOG_POST_SECONDS)
    stop_event.set()
    sampler.stop()
    poll_thread.join(timeout=2.5)

    total_dur = round(time.time() - t0, 2)
    raw_output = "".join(output_chunks)
    parsed = parse_axrunmodel_output(raw_output) if eff_engine == "axrunmodel" else parse_inference_output(raw_output)
    rt_err = _detect_runtime_error(raw_output)

    # Agrégation des statistiques par carte et globales
    _drop_empty_series(series)
    clean_series = series
    metrics: Dict = {
        "command": cmd_str,
        "engine": eff_engine,
        "model": os.path.basename(resolved_model) if os.path.sep in resolved_model else resolved_model,
        "model_path": resolved_model,
        "aipu_cores": int(aipu_cores),
        "target_devices": devs_arg or "toutes",
        "passes": passes,
        "time_s": duration_target,
        "series": clean_series,
        "series_t": clean_series.get("t", []),
    }
    for s_key, m_key in (
        ("temp", "series_temp"),
        ("power", "series_power"),
        ("freq_graphics", "series_freq_graphics"),
        ("mvm_util", "series_mvm_util"),
        ("kps", "series_kps"),
    ):
        if s_key in clean_series:
            metrics[m_key] = clean_series[s_key]

    metrics.update({k: v for k, v in parsed.items() if k != "core_temps_seen"})

    # Statistiques par AIPU (température, puissance, horloge, MVM, throttling)
    aipu_indices = sorted({
        int(m.group(1))
        for k in clean_series
        for m in [re.match(r"^aipu(\d+)_", k)]
        if m
    })
    throttle_total = 0
    for idx in aipu_indices:
        pfx = f"aipu{idx}_"
        tv = [float(x) for x in clean_series.get(pfx + "temp", []) if x is not None]
        if tv:
            metrics[pfx + "temp_max_c"] = round(max(tv), 1)
            metrics[pfx + "temp_moy_c"] = round(sum(tv) / len(tv), 1)
        pv = [float(x) for x in clean_series.get(pfx + "power", []) if x is not None]
        if pv:
            metrics[pfx + "power_max_w"] = round(max(pv), 2)
            metrics[pfx + "power_moy_w"] = round(sum(pv) / len(pv), 2)
        cv = [float(x) for x in clean_series.get(pfx + "clock_mhz", []) if x is not None]
        if cv:
            metrics[pfx + "clock_min_mhz"] = round(min(cv), 0)
            metrics[pfx + "clock_moy_mhz"] = round(sum(cv) / len(cv), 0)
        mv = [float(x) for x in clean_series.get(pfx + "mvm_pct", []) if x is not None]
        if mv:
            metrics[pfx + "mvm_moy_pct"] = round(sum(mv) / len(mv), 1)
        thv = [int(x) for x in clean_series.get(pfx + "throttle_thermal", []) if x]
        if thv:
            metrics[pfx + "throttle_samples"] = len(thv)
            throttle_total += len(thv)

    if throttle_total:
        metrics["axelera_throttle_samples"] = throttle_total

    all_temps = [float(x) for x in clean_series.get("temp", []) if x is not None]
    if all_temps:
        metrics["temp_max_c"] = round(max(all_temps), 1)
        metrics["temp_moy_c"] = round(sum(all_temps) / len(all_temps), 1)
    elif parsed.get("core_temp_c") is not None:
        metrics["temp_max_c"] = round(float(parsed["core_temp_c"]), 1)

    all_powers = [float(x) for x in clean_series.get("power", []) if x is not None]
    if all_powers:
        metrics["power_max_w"] = round(max(all_powers), 2)
        metrics["power_moy_w"] = round(sum(all_powers) / len(all_powers), 2)

    score = parsed.get("system_fps") or parsed.get("host_fps") or parsed.get("device_fps")
    if stopped_by_user:
        status = "stopped"
    elif timed_out:
        status = "error"
        metrics["erreur"] = "Timeout dépassé pendant l'exécution Axelera"
    elif rt_err or (last_rc != 0 and not (eff_engine == "inference" and duration_target > 0 and (time.time() - run_start) >= duration_target)):
        status = "error"
        metrics["erreur"] = rt_err or f"{eff_engine} terminé avec code {last_rc}"
    else:
        status = "ok"

    if cb:
        cb(100, "Terminé" if status == "ok" else ("Arrêté" if status == "stopped" else "Erreur"))

    display_name = f"Axelera AIPU ({metrics['model']})"
    return BenchmarkResult(
        display_name,
        "Axelera AI",
        score,
        "FPS",
        total_dur,
        raw_output,
        metrics=metrics,
        status=status,
        command=cmd_str,
        notes=metrics.get("erreur", ""),
    )
