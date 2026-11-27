# -*- coding: utf-8 -*-
"""core/benchmarks.py — Catalogue, runners CPU/RAM/FIO, rapport matériel et façade de compatibilité."""

import json
import os
import platform
import re
try:
    import psutil
except ImportError:
    psutil = None
import shlex
import signal
import subprocess
import threading
import time

from core.result import BenchmarkResult, collect_dmesg_log  # noqa: F401  (ré-exporté : exporters.export)
from core.fault_monitor import with_fault_watch
from core.safety import nvidia_smi_usable  # noqa: F401

try:
    from core.cli_utils import build_stress_ng_cli_command, extract_timeout_seconds
except Exception:
    build_stress_ng_cli_command = None
    extract_timeout_seconds = None

from core.bench_utils import (  # noqa: F401
    LOG_POST_SECONDS,
    LOG_PRE_SECONDS,
    ProgressCb,
    SeriesCb,
    _OCLBURN_SRC,
    _RAM_SPEED_CACHE,
    _TOOL_CACHE,
    _TOOL_CACHE_LOCK,
    _TOOL_TTL_FOUND_S,
    _TOOL_TTL_MISSING_S,
    _add_gpu_series_metrics,
    _candidate_user_homes,
    _drop_empty_series,
    _find_gpu_burn_command,
    _find_gpu_burn_command_uncached,
    _find_in_folder_recursive,
    _find_tool_path,
    _find_tool_path_uncached,
    _gpu_burn_cmd_for_path,
    _gpu_burn_workdir,
    _has_valid_exec_format,
    _is_shell_script,
    _log_window_wait,
    _normalize_gpu_list,
    _probe_gpu_burn_command,
    _ram_speed_mts,
    _read_ram_temperature_power_best_effort,
    _run,
    _terminate_process_tree,
    _tool_cached,
    _watch_process,
    forget_tool_cache,
    get_custom_tool_path,
    get_ram_total_mb,
    get_system_info,
    read_ram_temperature_power,
    set_custom_tool_path,
    tool_exists,
)
from core.bench_sensors import (  # noqa: F401
    FREQ_DIVERGENCE_PCT,
    _BMC_CPU_TEMP,
    _BMC_NOT_CPU,
    _CPU_AUX_POWER_RE,
    _CPU_CHIP_QUALIFY_RE,
    _CPU_CORE_POWER_RE,
    _CPU_POWER_SOURCE,
    _CPU_SYSFS,
    _CPU_TEMP_DRIVERS,
    _CPU_TEMP_LABEL,
    _CPU_TEMP_SOURCE,
    _CPU_THERMAL_TYPE,
    _CPU_TOTAL_POWER_RE,
    _FREQ_STATE,
    _HWMON_ROOT,
    _NOT_CPU_CHIPS,
    _NOT_CPU_POWER_RE,
    _POWERSTAT_LAST,
    _POWERSTAT_LOCK,
    _POWERSTAT_PROC,
    _POWERSTAT_Q,
    _POWER_SRC,
    _THERMAL_ROOT,
    _bmc_cpu_temps,
    _cpu_temps_hwmon_known,
    _cpu_temps_hwmon_labels,
    _cpu_temps_thermal_zones,
    _detect_power_source,
    _find_card_dir,
    _find_hwmon_dir,
    _freq_diagnostic,
    _freq_state_reset,
    _get_cpu_socket_mapping,
    _get_gpu_freq_sysfs,
    _get_gpu_mem_freq_sysfs,
    _get_gpu_power_sysfs,
    _get_gpu_temp_sysfs,
    _get_powerstat_latest,
    _host_cpu_powers_per_socket,
    _hwmon_has_cpu_power,
    _poll_cpu_sensors,
    _poll_gpu_sensors,
    _poll_ssd_sensors,
    _read_bmc_cpu_powers_per_socket,
    _read_cpu_freq_mhz,
    _read_cpu_freqs_per_socket,
    _read_cpu_pcts_per_socket,
    _read_cpu_powers_per_socket,
    _read_cpu_temps_per_socket,
    _read_hw_freqs_khz,
    _read_milli,
    _read_sysfs_file,
    _reset_power_cache,
    _start_powerstat_reader,
    _stop_powerstat_proc,
)
from core.bench_net import (  # noqa: F401
    _bench_net_iperf,
    _iperf3_common_metrics,
    _iperf3_error_text,
    _iperf3_hint,
    _iperf3_loopback,
    _iperf3_run,
    bench_net_iperf_tcp,
    bench_net_iperf_udp,
)
from core.bench_nvidia import (  # noqa: F401
    _cuda_lib_autres_versions,
    _cuda_lib_dirs,
    _gpu_burn_compare_hint,
    _gpu_burn_lib_hint,
    _gpu_burn_progress,
    bench_gpu_burn,
    bench_gpu_fryer,
    bench_gpu_nvbandwidth,
    bench_gpu_vram_test,
    parse_nvbandwidth_text_output,
)
from core.bench_compute import (  # noqa: F401
    DCGM_LEVEL_INFO,
    _CLPEAK_NEW_FLAGS,
    _CLPEAK_OLD_FLAGS,
    _CPU_NAME_RE,
    _OPENCL_HINT,
    _STREAM_SRC,
    _UNIT_SCALE,
    _VULKAN_HINT,
    _burn_summary,
    _clinfo_devices,
    _clpeak_flavor,
    _dcgm_no_entity_hint,
    _dcgm_ready,
    _dcgm_runtime_error,
    _ensure_gpu_persistence,
    _gpu_power_limit_w,
    _l3_total_bytes,
    _nonexec_hint,
    _nvbandwidth_local_values,
    _nvidia_gpu_indices,
    _oclburn_binary,
    _oclburn_devices,
    _oclburn_extra_args,
    _oclburn_kv,
    _opencl_icd_present,
    _parse_size_bytes,
    _pcie_min_width,
    _peak_failure_hint,
    _select_indices,
    _stream_array_elements,
    _stream_binary,
    _stream_peak_tool,
    _vulkan_gpu_devices,
    _vulkan_icd_present,
    _add_power_cap_metrics,
    bench_clpeak,
    bench_gpu_dcgm,
    bench_gpu_oclburn,
    bench_gpu_vram_bandwidth,
    bench_ram_stream,
    bench_vkpeak,
    parse_clpeak_devices,
    parse_clpeak_generic,
    parse_dcgm_json,
    parse_vkpeak_output,
)
from core.bench_axelera import bench_axelera_stress  # noqa: F401


# ─────────────────────────────────────────────────────────────────────────────
# CPU — stress-ng  (stressors + options avancées)
# ─────────────────────────────────────────────────────────────────────────────
# stressor : cpu | matrix | fft | bitops | int8 | int32 | int64 | float | double | all
# cpu_load_pct : 0 = max, 1-99 = pourcentage cible

STRESS_NG_STRESSORS = [
    "cpu", "matrix", "fft", "bitops",
    "int8", "int32", "int64", "float", "double", "all",
]

_STRESS_NG_ROW = re.compile(
    r"^\s*(?:stress-ng:\s*\w+:\s*(?:\[\d+\]\s*)?)?([A-Za-z][\w-]*)\s+(\d+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)(?:\s+[\d.]+)*\s*$",
    re.M)


def _stress_ng_scores(text: str) -> dict:
    """Extrait {stressor: bogo-ops/s (temps réel)} du tableau final de `stress-ng --metrics-brief`.

    Accepte les lignes avec préfixe (`stress-ng: info: [123] cpu …`) comme sans préfixe (`--log-brief`, qui le supprime :
    sans cela aucun score n'était trouvé). Colonnes : bogo ops · temps réel (s) · usr (s) · sys (s) · bogo ops/s (réel) · bogo ops/s (usr+sys)."""
    scores: dict = {}
    for m in _STRESS_NG_ROW.finditer(text or ""):
        try:
            scores[m.group(1)] = float(m.group(6))
        except ValueError:
            pass
    return scores



@with_fault_watch
def bench_cpu_stress(
    threads: int = 4,
    time_s: int = 10,
    stressor: str = "cpu",
    cpu_load_pct: int = 0,
    extra_args: str = "",
    cb: ProgressCb = None,
    series_cb: SeriesCb = None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    t0 = time.time()
    if not tool_exists("stress-ng"):
        return BenchmarkResult("CPU stress-ng", "CPU + RAM", None, "bogo-ops/s", 0, "", status="skipped",
                               metrics={"erreur": "stress-ng non installe"})
    if cb: cb(5, f"Demarrage stress-ng ({stressor})…")

    series: dict = {"t": [], "temp": [], "freq": [], "cpu_pct": [], "power": []}
    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_poll_cpu_sensors,
                                   args=(series, stop_event, t0, series_cb), daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    cmd = ["stress-ng", f"--{stressor}", str(threads),
           "--timeout", str(time_s), "--metrics-brief", "--log-brief"]
    if cpu_load_pct > 0:
        cmd += ["--cpu-load", str(cpu_load_pct)]
    if extra_args:
        try:
            cmd += shlex.split(str(extra_args))
        except Exception:
            cmd += str(extra_args).split()
    cmd_str = shlex.join(cmd)

    lines = []
    stop_state = {"killed": False, "timeout": False}
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, start_new_session=True)
        # Surveillant : bouton Stopper / panne critique (kill_event) ou dépassement (durée × 1,5 + 5 min) → arrêt de l'arbre de processus
        threading.Thread(target=_watch_process, args=(proc, kill_event, int(time_s * 1.5) + 300, stop_state), daemon=True).start()
        for line in proc.stdout:
            lines.append(line)
            elapsed = time.time() - t0
            pct = min(90, int(5 + 85 * elapsed / max(time_s, 1)))
            temp_s = f"{series['temp'][-1]:.0f}°C" if series["temp"] else "—"
            freq_s = f"{series['freq'][-1]}MHz"     if series["freq"] else "—"
            if cb: cb(pct, f"Stress {stressor} | {temp_s} | {freq_s}")
        proc.wait()
        rc = proc.returncode
    except FileNotFoundError:
        rc = -1; lines = ["stress-ng introuvable"]
    except Exception as e:
        rc = -3; lines = [str(e)]
    finally:
        _log_window_wait(stop_event, LOG_POST_SECONDS)
        stop_event.set()
        poll_thread.join(timeout=3)

    dur = time.time() - t0
    combined = "".join(lines)
    score = None
    sc = _stress_ng_scores(combined)
    if stressor in sc:
        score = sc[stressor]
    elif sc:
        score = next(iter(sc.values()))

    metrics: dict = {"stressor": stressor, "threads": threads, "command": " ".join(cmd)}
    if cpu_load_pct > 0: metrics["cpu_load_target_pct"] = cpu_load_pct
    if series.get("temp"):
        metrics["temp_max_c"] = max(series["temp"])
        metrics["temp_moy_c"] = round(sum(series["temp"]) / len(series["temp"]), 1)
    if series.get("freq"):
        metrics["freq_max_mhz"] = max(series["freq"])
        metrics["freq_moy_mhz"] = round(sum(series["freq"]) / len(series["freq"]))
    if series.get("cpu_pct"):
        metrics["charge_moy_pct"] = round(sum(series["cpu_pct"]) / len(series["cpu_pct"]), 1)
    pw = [v for v in series.get("power", []) if v > 0]
    if pw:
        metrics["power_max_w"] = max(pw)
        metrics["power_moy_w"] = round(sum(pw) / len(pw), 1)

    for k, v in series.items():
        if k.startswith("_"): continue
        if k == "t":
            metrics["series_t"] = v
        else:
            metrics[f"series_{k}"] = v

    status, notes = "ok", ""
    if stop_state["killed"]:
        status, notes = "stopped", f"arrêté avant la fin ({dur:.0f} s sur {time_s} s prévues)"
    elif stop_state["timeout"]:
        status, notes = "error", f"stress-ng n'a pas rendu la main pour {time_s} s demandées : arrêt forcé"
    if cb: cb(100, "Termine")
    return BenchmarkResult("CPU stress-ng", "CPU + RAM", score, "bogo-ops/s", dur, combined, status=status, notes=notes,
                           metrics=metrics, system_info=get_system_info(), command=cmd_str)


# ─────────────────────────────────────────────────────────────────────────────
# CPU + RAM — bench_stress_profile (multi-stressor avec profils)
# ─────────────────────────────────────────────────────────────────────────────

@with_fault_watch
def bench_stress_profile(
    cpu_workers: int = 0,
    cpu_stressor: str = "all",
    vm_workers: int = 0,
    vm_bytes: str = "512M",
    time_s: int = 30,
    cpu_load_pct: int = 0,
    stress_ng_extra_args: str = "",
    cli_only: bool = False,
    stress_ng_cli_args: str = "",
    profile_name: str = "",
    profile_id: str = "",
    cb: ProgressCb = None,
    series_cb: SeriesCb = None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    """Lance stress-ng avec combinaison CPU + VM arbitraire et polling thermique.

    `profile_name` (ex. « CPU — Test rapide (2 min) ») devient le nom du résultat dans
    l'interface, l'historique et les rapports (HTML/PDF/CSV/JSON).
    """
    t0 = time.time()
    if not tool_exists("stress-ng"):
        return BenchmarkResult("Stress profil", "CPU + RAM", None, "bogo-ops/s", 0, "",
                               status="skipped",
                               metrics={"erreur": "stress-ng non installe"})

    cli_expanded = ""
    cli_vars = {}
    cmd = None
    if cli_only:
        try:
            if build_stress_ng_cli_command is None:
                raise ValueError("Module CLI indisponible")
            cmd, cli_vars, cli_expanded = build_stress_ng_cli_command(str(stress_ng_cli_args or stress_ng_extra_args or ""))
        except ValueError as e:
            return BenchmarkResult("stress-ng CLI", "CPU + RAM", None, "bogo-ops/s", 0, str(e), status="error",
                                   metrics={"erreur": f"Commande CLI stress-ng invalide: {e}"})
        # Progression indicative seulement : en CLI pur, la durée dépend de la commande saisie.
        try:
            parsed_timeout = extract_timeout_seconds(cmd[1:]) if extract_timeout_seconds else None
            if parsed_timeout:
                time_s = int(parsed_timeout)
        except Exception:
            parsed_timeout = None
    # Garde-fou : durée demandée × 1,5 + 5 min. En CLI libre sans --timeout la durée est inconnue : pas de limite imposée.
    hard_limit_s = None if (cli_only and not locals().get("parsed_timeout")) else int(time_s * 1.5) + 300

    if cli_only:
        pass
    else:
        cmd = ["stress-ng"]
        if cpu_workers:
            cmd += ["--cpu", str(cpu_workers), "--cpu-method", cpu_stressor]
            if cpu_load_pct:
                cmd += ["--cpu-load", str(cpu_load_pct)]
        if vm_workers:
            cmd += ["--vm", str(vm_workers), "--vm-bytes", vm_bytes]
        cmd += ["--timeout", str(time_s), "--metrics-brief", "--log-brief"]
        if stress_ng_extra_args:
            try:
                cmd += shlex.split(stress_ng_extra_args)
            except ValueError as e:
                return BenchmarkResult("Stress profil", "CPU + RAM", None, "bogo-ops/s", 0, str(e), status="error",
                                       metrics={"erreur": f"Options CLI stress-ng invalides: {e}"})

    if cb: cb(5, "Demarrage stress-ng…")
    _reset_power_cache()

    series: dict = {"t": [], "temp": [], "temp_s1": [], "freq": [], "freq_s1": [],
                    "cpu_pct": [], "power": [], "power_s1": [], "ram_temp": [], "ram_power": []}
    if vm_workers or re.search(r"--(vm|vm-bytes|mmap|stream)\b", stress_ng_cli_args or ""):
        # Occupation de la mémoire pendant le test (psutil, universel) : preuve que la charge remplit bien la RAM
        series["ram_used_gb"] = []
    stop_event = threading.Event()
    poll_thread = threading.Thread(target=_poll_cpu_sensors,
                                   args=(series, stop_event, t0, series_cb),
                                   daemon=True)
    poll_thread.start()
    _log_window_wait(stop_event, LOG_PRE_SECONDS)

    killed = False
    timed_out = False
    lines = []
    proc = None
    out_buf: list = []

    def _drain(stream):
        """Lit la sortie au fil de l'eau : sans cela, des options bavardes (--vmstat 1, -v) sur un test long remplissent le tube
        (64 Ko), stress-ng se bloque en écriture et le test ne finit jamais."""
        try:
            for line in iter(stream.readline, ""):
                out_buf.append(line)
        except Exception:
            pass

    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1, start_new_session=True)
        drain_th = threading.Thread(target=_drain, args=(proc.stdout,), daemon=True)
        drain_th.start()
        t_run = time.time()
        while proc.poll() is None:
            if (kill_event and kill_event.is_set()) or (hard_limit_s and time.time() - t_run > hard_limit_s):
                killed = bool(kill_event and kill_event.is_set())
                timed_out = not killed
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    time.sleep(0.8)
                    if proc.poll() is None:
                        os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    proc.kill()
                break
            elapsed = time.time() - t0
            pct = min(90, int(5 + 85 * elapsed / max(time_s, 1)))
            temp_s = f"{series['temp'][-1]:.0f}°C"  if series["temp"]    else "—"
            freq_s = f"{series['freq'][-1]}MHz"      if series["freq"]    else "—"
            load_s = f"{series['cpu_pct'][-1]:.0f}%" if series["cpu_pct"] else "—"
            if cb: cb(pct, f"{temp_s} | {freq_s} | {load_s}")
            time.sleep(0.5)
        try:
            proc.wait(timeout=10)
        except Exception:
            pass
        drain_th.join(timeout=3)
        lines = list(out_buf)
        if timed_out:
            lines.append(f"\n[BenchSuite] stress-ng n'a pas rendu la main après {hard_limit_s} s (durée demandée {time_s} s) : arrêt forcé.\n")
    except FileNotFoundError:
        lines = ["stress-ng introuvable"]
    except Exception as e:
        lines = [str(e)]
    finally:
        _log_window_wait(stop_event, LOG_POST_SECONDS)
        stop_event.set()
        poll_thread.join(timeout=3)
        if proc and proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()

    _drop_empty_series(series)
    dur     = time.time() - t0
    combined = "".join(lines)

    # Extraire bogo-ops/s pour chaque stressor
    scores = _stress_ng_scores(combined)

    # Score principal : méthode exacte > somme CPU > vm > premier dispo
    if cpu_stressor and cpu_stressor != "all" and cpu_stressor in scores:
        score = scores[cpu_stressor]
    else:
        cpu_scores = {k: v for k, v in scores.items() if k != "vm"}
        if cpu_scores:
            score = sum(cpu_scores.values())
        elif "vm" in scores:
            score = scores["vm"]
        else:
            score = next(iter(scores.values()), None) if scores else None

    metrics: dict = {
        "cpu_workers":  cpu_workers,
        "cpu_stressor": cpu_stressor,
        "vm_workers":   vm_workers,
        "vm_bytes":     vm_bytes if vm_workers else "—",
        "stress_ng_command": " ".join(shlex.quote(x) for x in cmd),
        "cli_only": bool(cli_only),
        "stress_ng_cli_args": stress_ng_cli_args if cli_only else stress_ng_extra_args,
        "stress_ng_cli_expanded": cli_expanded if cli_only else "",
        "stress_ng_cli_vars": cli_vars if cli_only else {},
    }
    if scores:
        for k, v in scores.items():
            metrics[f"bogo_ops_s_{k}"] = round(v, 1)
    if series.get("temp"):
        metrics["cpu0_temp_max_c"] = max(series["temp"])
        metrics["cpu0_temp_moy_c"] = round(sum(series["temp"]) / len(series["temp"]), 1)
    if series.get("temp_s1"):
        metrics["cpu1_temp_max_c"] = max(series["temp_s1"])
        metrics["cpu1_temp_moy_c"] = round(sum(series["temp_s1"]) / len(series["temp_s1"]), 1)
    if series["freq"]:
        metrics["cpu0_freq_max_mhz"] = max(series["freq"])
        metrics["cpu0_freq_moy_mhz"] = round(sum(series["freq"]) / len(series["freq"]))
    if series.get("freq_s1"):
        metrics["cpu1_freq_max_mhz"] = max(series["freq_s1"])
        metrics["cpu1_freq_moy_mhz"] = round(sum(series["freq_s1"]) / len(series["freq_s1"]))
    if series["cpu_pct"]:
        metrics["charge_moy_pct"] = round(sum(series["cpu_pct"]) / len(series["cpu_pct"]), 1)
    pw0 = [v for v in series.get("power", []) if v > 0]
    if pw0:
        metrics["cpu0_power_max_w"] = max(pw0)
        metrics["cpu0_power_moy_w"] = round(sum(pw0) / len(pw0), 1)
    pw1 = [v for v in series.get("power_s1", []) if v > 0]
    if pw1:
        metrics["cpu1_power_max_w"] = max(pw1)
        metrics["cpu1_power_moy_w"] = round(sum(pw1) / len(pw1), 1)

    # Statistiques RAM collectées pendant le stress CPU
    ram_temps = [v for v in series.get("ram_temp", []) if v > 0]
    if ram_temps:
        metrics["ram_temp_max_c"] = max(ram_temps)
        metrics["ram_temp_avg_c"] = round(sum(ram_temps) / len(ram_temps), 1)
    ram_used = [v for v in series.get("ram_used_gb", []) if v > 0]
    if ram_used:
        metrics["ram_used_max_gb"] = max(ram_used)
        try:
            metrics["ram_pct_max"] = round(100.0 * max(ram_used) * 1073741824 / psutil.virtual_memory().total, 1)
        except Exception:
            pass
    ram_pwrs = [v for v in series.get("ram_power", []) if v > 0]
    if ram_pwrs:
        metrics["ram_power_max_w"] = max(ram_pwrs)
        metrics["ram_power_moy_w"] = round(sum(ram_pwrs) / len(ram_pwrs), 1)
    if re.search(r"ram|mem|stream", f"{profile_id} {profile_name}", re.I) and not re.search(r"cpu_(all|phys)", profile_id or ""):
        mts = _ram_speed_mts()
        if mts:
            metrics["ram_vitesse_mts"] = mts      # configurée (firmware), pas mesurée

    for k, v in series.items():
        if k.startswith("_"): continue
        if k == "t":
            metrics["series_t"] = v
        else:
            metrics[f"series_{k}"] = v

    name = "stress-ng CLI" if cli_only else "Stress profil"
    if (not cli_only) and cpu_workers and vm_workers:
        name = f"CPU+RAM stress ({cpu_stressor})"
    elif (not cli_only) and cpu_workers:
        name = f"CPU stress ({cpu_stressor})"
    elif (not cli_only) and vm_workers:
        name = "RAM stress (vm)"

    if cb: cb(100, "Termine")
    cmd_str = shlex.join(cmd) if cmd else ""
    metrics["command"] = cmd_str
    if profile_name:
        metrics["profile_name"] = profile_name
        if profile_id:
            metrics["profile_id"] = profile_id
        metrics["stress_kind"] = name   # type de charge (ex. « CPU stress (all) »)
        name = profile_name
    notes = ""
    status = "ok"
    if _CPU_TEMP_SOURCE["v"] and series.get("temp"):
        metrics["cpu_temp_source"] = _CPU_TEMP_SOURCE["v"]     # d'où vient la température CPU affichée (traçabilité)
    if _CPU_POWER_SOURCE["v"] and series.get("power"):
        metrics["cpu_power_source"] = _CPU_POWER_SOURCE["v"]   # capteurs additionnés pour la puissance CPU (ex. CPU + IO sur ARM)
    fd = _freq_diagnostic()                       # source de la fréquence + alerte si le CPU est bridé (fréquence matérielle ≠ demandée)
    metrics.update(fd)
    if fd.get("freq_demandee_mhz"):
        notes = fd["freq_note"]
    if killed:
        status = "stopped"
        notes = (notes + " ; " if notes else "") + f"arrêté avant la fin ({dur:.0f} s sur {time_s} s prévues)"
    elif timed_out:
        status = "error"
        metrics["arret_force"] = True
        notes = (notes + " ; " if notes else "") + (f"stress-ng n'a pas rendu la main après {hard_limit_s} s pour {time_s} s demandées : "
                                                   "arrêt forcé (stressor bloqué ?)")
    return BenchmarkResult(name, "CPU + RAM", score, "bogo-ops/s", dur, combined,
                           status=status, notes=notes,
                           metrics=metrics, system_info=get_system_info(), command=cmd_str)


@with_fault_watch
def bench_storage_fio(
    path: str = "/tmp",
    rw_mode: str = "readwrite",
    bs: str = "128k",
    size_mb: int = 512,
    runtime_s: int = 20,
    iodepth: int = 1,
    numjobs: int = 1,
    direct: bool = True,
    io_engine: str = "sync",
    cb: ProgressCb = None,
    on_series_cb=None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    t0 = time.time()
    if not tool_exists("fio"):
        return BenchmarkResult("Stockage fio", "Stockage", None, "MiB/s", 0, "", status="skipped",
                               metrics={"erreur": "fio non installe"})
    from core.safety import check_write_target, is_write_mode
    lvl, msg = check_write_target(path, writes=is_write_mode(rw_mode))
    if lvl == "block":
        return BenchmarkResult("Stockage fio", "Stockage", None, "MiB/s", 0, msg, status="error",
                               metrics={"erreur": msg})
    test_file = os.path.join(path, "_benchsuite_fio.tmp")
    if cb: cb(10, f"fio {rw_mode} bs={bs} iodepth={iodepth}…")
    cmd = [
        "fio", "--name=bench",
        f"--filename={test_file}",
        f"--rw={rw_mode}",
        f"--bs={bs}",
        f"--size={size_mb}M",
        f"--numjobs={numjobs}",
        f"--runtime={runtime_s}",
        f"--iodepth={iodepth}",
        f"--ioengine={io_engine}",
        "--randrepeat=0",
        "--invalidate=1",
        "--cpus_allowed_policy=split",
        "--ramp_time=3",
        "--time_based",
        "--group_reporting",
        "--output-format=json",
    ]
    if direct:
        cmd.append("--direct=1")
    cmd_str = shlex.join(cmd)
    
    series = {"t": [], "temp": [], "power": []}
    stop_ev = threading.Event()
    poll_th = threading.Thread(target=_poll_ssd_sensors, args=(series, stop_ev, t0, path, on_series_cb), daemon=True)
    poll_th.start()

    # Délai maximal : durée demandée + rampe + marge (un fio bloqué sur un disque défaillant ne doit pas figer le test) ;
    # `kill_event` (bouton Stopper, panne critique) interrompt fio.
    rc, out, err = _run(cmd, timeout=int(runtime_s) + 3 + 300, kill_event=kill_event or threading.Event())
    stopped = rc == -4
    if rc == -2:
        err = f"fio n'a pas rendu la main après {int(runtime_s) + 303} s (disque bloqué ?) : arrêté."
    stop_ev.set()
    if poll_th.is_alive():
        poll_th.join(timeout=2)

    try: os.remove(test_file)
    except Exception: pass
    dur = time.time() - t0
    if rc != 0:
        return BenchmarkResult("Stockage fio", "Stockage", None, "MiB/s", dur, err, status="stopped" if stopped else "error", command=cmd_str, metrics={"command": cmd_str, "rw_mode": rw_mode, "bs": bs, "size_mb": size_mb, "runtime_s": runtime_s, "iodepth": iodepth, "io_engine": io_engine})
    try:
        data = json.loads(out)
        job = data["jobs"][0]
        r_bytes = job["read"].get("bw_bytes", job["read"].get("bw", 0) * 1024)
        w_bytes = job["write"].get("bw_bytes", job["write"].get("bw", 0) * 1024)
        r_bw   = r_bytes / 1048576.0
        w_bw   = w_bytes / 1048576.0
        r_iops = job["read"]["iops"]
        w_iops = job["write"]["iops"]
        # Score selon mode
        if "read" in rw_mode and "write" not in rw_mode:
            score = r_bw
        elif "write" in rw_mode and "read" not in rw_mode:
            score = w_bw
        else:
            score = (r_bw + w_bw) / 2
        metrics = {
            "command":      cmd_str,
            "rw_mode":      rw_mode,
            "block_size":   bs,
            "bs":           bs,
            "size_mb":      size_mb,
            "runtime_s":    runtime_s,
            "iodepth":      iodepth,
            "numjobs":      numjobs,
            "io_engine":    io_engine,
            "direct":       direct,
            "read_MiB_s":   round(r_bw, 2),
            "write_MiB_s":  round(w_bw, 2),
            "read_iops":    round(r_iops, 0),
            "write_iops":   round(w_iops, 0),
            "read_lat_ms":  round(job["read"]["lat_ns"]["mean"] / 1e6, 3),
            "write_lat_ms": round(job["write"]["lat_ns"]["mean"] / 1e6, 3),
        }
        if series and series.get("t"):
            metrics["series"] = {
                "t": series.get("t", []),
                "disk_temp": series.get("temp", []),
                "disk_power": series.get("power", []),
            }
            metrics["series_t"] = series.get("t", [])
            metrics["series_temp"] = series.get("temp", [])
            metrics["series_power"] = series.get("power", [])
        if cb: cb(100, "Termine")
        return BenchmarkResult("Stockage fio", "Stockage", round(score, 2), "MiB/s",
                               dur, out, metrics=metrics, system_info=get_system_info(), command=cmd_str)
    except Exception as e:
        return BenchmarkResult("Stockage fio", "Stockage", None, "MiB/s",
                               dur, out + str(e), status="error")


# Rapport matériel détaillé
# ─────────────────────────────────────────────────────────────────────────────

def get_hardware_report() -> dict:
    """Collecte des informations détaillées sur le matériel de la machine."""
    report: dict = {}

    # ── Système ──
    report["systeme"] = {
        "hostname": platform.node(),
        "os":       f"{platform.system()} {platform.release()}",
        "kernel":   platform.version()[:100],
        "arch":     platform.machine(),
    }

    # ── CPU ──
    cpu: dict = {}
    try:
        with open("/proc/cpuinfo") as f:
            raw = f.read()
        from core.system_info import cpu_model_name
        if cpu_model_name() != "inconnu": cpu["modele"] = cpu_model_name()
        m = re.search(r"cache size\s*:\s*(.+)", raw)
        if m: cpu["cache_l3"] = m.group(1).strip()
        cpu["threads_total"] = raw.count("processor\t:")
        sockets = {mm.group(1) for mm in re.finditer(r"physical id\s*:\s*(\d+)", raw)}
        cpu["sockets"] = len(sockets) if sockets else 1
        m = re.search(r"cpu cores\s*:\s*(\d+)", raw)
        if m: cpu["coeurs_par_socket"] = int(m.group(1))
    except Exception:
        pass
    fi = psutil.cpu_freq() if psutil else None
    if fi:
        cpu["freq_max_mhz"] = round(fi.max)
        cpu["freq_min_mhz"] = round(fi.min)
    report["cpu"] = cpu

    # ── Mémoire ──
    mem: dict = {}
    try:
        with open("/proc/meminfo") as f:
            raw = f.read()
        m = re.search(r"MemTotal:\s+(\d+)", raw)
        if m: mem["total_go"] = round(int(m.group(1)) / 1048576, 2)
        m = re.search(r"SwapTotal:\s+(\d+)", raw)
        if m: mem["swap_go"]  = round(int(m.group(1)) / 1048576, 2)
    except Exception:
        pass
    # Type/vitesse via lshw
    rc, out, _ = _run(["lshw", "-C", "memory", "-quiet"], timeout=10)
    if rc == 0:
        m = re.search(r"(DDR\d+|LPDDR\d+)", out, re.IGNORECASE)
        if m: mem["type"] = m.group(1).upper()
        speeds = re.findall(r"clock:\s*(\d+)MHz", out, re.IGNORECASE)
        if speeds: mem["vitesse_mhz"] = int(speeds[0])
        mem["slots_utilises"] = out.count("*-bank")
    # Modules détaillés via dmidecode (sans sudo, best-effort)
    rc, out, _ = _run(["dmidecode", "-t", "17"], timeout=5)
    if rc == 0 and "Permission denied" not in out and "No SMBIOS" not in out:
        modules = []
        for block in re.split(r"Memory Device\n", out)[1:]:
            sm = re.search(r"Size:\s+(.+)", block)
            if not sm or "No Module" in sm.group(1): continue
            tm = re.search(r"\bType:\s+(.+)",  block)
            vm = re.search(r"Speed:\s+(.+)",   block)
            fm = re.search(r"Manufacturer:\s+(.+)", block)
            pm = re.search(r"Part Number:\s+(.+)",  block)
            modules.append({
                "taille":    sm.group(1).strip(),
                "type":      tm.group(1).strip() if tm else "?",
                "vitesse":   vm.group(1).strip() if vm else "?",
                "fabricant": fm.group(1).strip() if fm else "?",
                "part":      pm.group(1).strip() if pm else "?",
            })
        if modules: mem["modules"] = modules
    report["memoire"] = mem

    # ── GPU ──
    gpus = []
    try:
        from core.system_info import get_gpu_info
        for idx, g_info in enumerate(get_gpu_info()):
            from core.system_info import _normalize_pci_bus
            pci_bus = _normalize_pci_bus(g_info.get("pci_bus") or "")
            g: dict = {
                "index":             str(idx),
                "nom":               g_info.get("nom") or g_info.get("modele") or f"GPU {idx}",
                "modele":            g_info.get("modele") or g_info.get("nom") or f"GPU {idx}",
                "pci_bus":           pci_bus,
                "mcio":              g_info.get("mcio", ""),
                "serial":            g_info.get("serial", ""),
                "vram_mb":           g_info.get("vram_total_mo") or "?",
                "driver":            g_info.get("pilote") or "?",
                "pcie_gen":          f"Gen {g_info.get('pcie_gen')}" if g_info.get('pcie_gen') else "?",
                "pcie_width":        f"x{g_info.get('pcie_largeur')}" if g_info.get('pcie_largeur') else "?",
                "power_limit_w":     g_info.get("puissance_max_w") or "?",
                "freq_max_mhz":      "?",
            }
            if g_info.get("fabricant") == "NVIDIA" and tool_exists("nvidia-smi"):
                try:
                    # -i <bus PCI> : sans lui, chaque GPU recevait les fréquences de TOUS les GPU (ligne du GPU 0 + « \n… »)
                    sel = ["-i", str(g_info.get("index", idx))]      # index nvidia-smi (les GPU NVIDIA viennent en premier)
                    rc_freq, out_freq, _ = _run([
                        "nvidia-smi", *sel,
                        "--query-gpu=clocks.max.graphics,clocks.max.memory",
                        "--format=csv,noheader,nounits"
                    ], timeout=4)
                    if rc_freq == 0 and out_freq.strip():
                        parts_freq = [x.strip() for x in out_freq.strip().splitlines()[0].split(",")]
                        if len(parts_freq) > 0: g["freq_max_mhz"] = parts_freq[0]
                        if len(parts_freq) > 1: g["freq_mem_max_mhz"] = parts_freq[1]
                except Exception:
                    pass
            gpus.append(g)
    except Exception:
        pass
    report["gpu"] = gpus

    # ── Stockage ──
    disks = []
    try:
        for dev in sorted(os.listdir("/sys/block")):
            if not any(dev.startswith(p) for p in ("sd", "nvme", "hd", "vd")):
                continue
            d: dict = {"peripherique": f"/dev/{dev}"}
            for fpath, key, fn in [
                (f"/sys/block/{dev}/size",            "taille_go",
                 lambda x: round(int(x) * 512 / 1e9, 1)),
                (f"/sys/block/{dev}/device/model",    "modele",   str.strip),
                (f"/sys/block/{dev}/device/vendor",   "fabricant",str.strip),
                (f"/sys/block/{dev}/queue/rotational","type",
                 lambda x: "HDD" if x.strip() == "1" else "SSD/NVMe"),
            ]:
                if os.path.exists(fpath):
                    try:
                        with open(fpath) as f: d[key] = fn(f.read())
                    except Exception:
                        pass
            disks.append(d)
    except Exception:
        pass
    report["stockage"] = disks

    # ── Réseau ──
    nets = []
    try:
        for iface in sorted(os.listdir("/sys/class/net")):
            if iface == "lo": continue
            n: dict = {"interface": iface}
            for fpath, key in [
                (f"/sys/class/net/{iface}/speed",     "vitesse_mbps"),
                (f"/sys/class/net/{iface}/operstate", "etat"),
                (f"/sys/class/net/{iface}/address",   "mac"),
            ]:
                if os.path.exists(fpath):
                    try:
                        with open(fpath) as f: val = f.read().strip()
                        n[key] = int(val) if key == "vitesse_mbps" else val
                    except Exception:
                        pass
            nets.append(n)
    except Exception:
        pass
    report["reseau"] = nets

    # ── Capteurs thermiques ──
    sensors: dict = {}
    rc, out, _ = _run(["sensors"], timeout=5)
    if rc == 0:
        cur = None
        for line in out.splitlines():
            s = line.strip()
            if not s:
                cur = None
            elif s.startswith("Adapter:") or s.startswith("("):
                pass  # ligne d'info / continuation, ignorée
            elif ":" not in s and s and not s.startswith("+") and not s.startswith("-"):
                cur = s; sensors[cur] = {}
            elif cur and ":" in s and not s.startswith("("):
                key  = s.split(":")[0].strip()
                rest = s.split(":", 1)[1]
                mt = re.search(r"[+-]?([\d.]+)\s*°C", rest)
                if mt:
                    sensors[cur][key] = mt.group(0)
                    continue
                mv = re.search(r"[+-]?([\d.]+)\s*(V|W|RPM|MHz)", rest)
                if mv: sensors[cur][key] = mv.group(0)
    report["capteurs"] = sensors

    # ── Carte mère ──
    board: dict = {}
    rc, out, _ = _run(["lshw", "-C", "system", "-quiet"], timeout=10)
    if rc == 0:
        for key, pat in [("produit",  r"product:\s*(.+)"),
                         ("fabricant",r"vendor:\s*(.+)"),
                         ("version",  r"version:\s*(.+)")]:
            m = re.search(pat, out)
            if m: board[key] = m.group(1).strip()
    # BIOS via dmidecode
    rc, out, _ = _run(["dmidecode", "-t", "bios"], timeout=5)
    if rc == 0 and "Permission denied" not in out:
        m = re.search(r"Version:\s*(.+)", out)
        if m: board["bios_version"] = m.group(1).strip()
        m = re.search(r"Release Date:\s*(.+)", out)
        if m: board["bios_date"] = m.group(1).strip()
    report["carte_mere"] = board

    return report


CATALOG = [
    dict(id="cpu_stress",   name="CPU + RAM", category="CPU + RAM",
         desc="Charge CPU intensive, stress RAM (Burn RAM) et profils mixtes (stress-ng)",
         requires=["stress-ng"], fn=bench_cpu_stress,
         params={"threads": 4, "time_s": 10}),
    dict(id="storage_fio_nvme",  name="Stockage FIO NVMe",  category="Stockage",
         desc="IOPS + débit NVMe — multiples modes R/W (fio)",
         requires=["fio"], fn=bench_storage_fio,
         params={"path": "/tmp", "filter": "nvme"}),
    dict(id="storage_fio_hdd",   name="Stockage FIO HDD/SATA",   category="Stockage",
         desc="IOPS + débit HDD/SATA — multiples modes R/W (fio)",
         requires=["fio"], fn=bench_storage_fio,
         params={"path": "/tmp", "filter": "hdd"}),
    dict(id="gpu_clpeak", name="Compute OpenCL", category="GPU",
         desc="Bande passante et calcul générique (AMD/Intel/NVIDIA) via OpenCL",
         requires=["clpeak"], fn=bench_clpeak,
         params={"target_gpus": "", "test_mode": "all-tests", "extra_args": "", "time_s": 0}),
    dict(id="gpu_vkpeak", name="Compute Vulkan", category="GPU",
         desc="Bande passante et calcul générique via Vulkan",
         requires=["vkpeak"], fn=bench_vkpeak,
         params={"target_gpus": "", "extra_args": "", "time_s": 0}),
    dict(id="net_iperf_tcp", name="Réseau iperf3 TCP", category="Réseau",
         desc="Débit TCP vers un serveur iperf3 (autre machine : page « Serveur iperf3 » ou `iperf3 -s`), retransmissions",
         requires=["iperf3"], fn=bench_net_iperf_tcp,
         params={"host": "127.0.0.1", "port": 5201, "time_s": 10, "parallel": 1, "reverse": False, "bind": ""}),
    dict(id="net_iperf_udp", name="Réseau iperf3 UDP", category="Réseau",
         desc="Débit, gigue et pertes UDP vers un serveur iperf3",
         requires=["iperf3"], fn=bench_net_iperf_udp,
         params={"host": "127.0.0.1", "port": 5201, "time_s": 10, "bandwidth": "100M", "parallel": 1, "bind": ""}),
    dict(id="gpu_oclburn", name="GPU — Burn OpenCL vérifié", category="GPU",
         desc="Burn à 100 % de tout GPU OpenCL (Intel, AMD, NVIDIA) : calcul entier/FP32/FP16 + VRAM en continu, résultats vérifiés (erreurs de calcul et mémoire)",
         requires=["oclburn"], fn=bench_gpu_oclburn,
         params={"time_s": 300, "vram_pct": 90, "mode": "all", "verification": "max", "target_gpus": ""}),
    dict(id="ram_stream", name="RAM — Bande passante (STREAM)", category="CPU + RAM",
         desc="Bande passante mémoire Copy/Scale/Add/Triad en GB/s (STREAM de McCalpin, compilé automatiquement)",
         requires=[], fn=bench_ram_stream,
         params={"threads": 0, "array_mb": 0, "ntimes": 20}),
    dict(id="gpu_vram_bw", name="GPU — Bande passante VRAM", category="GPU",
         desc="Bande passante de la mémoire GPU : nvbandwidth (copie locale par GPU) et/ou clpeak (OpenCL, toutes marques)",
         requires=[], fn=bench_gpu_vram_bandwidth,
         params={"buffer_size_mb": 2048, "iterations": 5, "target_gpus": "", "method": "auto"}),
    dict(id="gpu_dcgm", name="GPU — Diagnostic DCGM", category="GPU",
         desc="Diagnostic matériel NVIDIA (dcgmi diag) : mémoire GPU, calcul, PCIe, bande passante, stress ciblé",
         requires=["dcgmi"], fn=bench_gpu_dcgm,
         params={"level": 3, "tests": "", "target_gpus": "", "fail_early": True, "pcie_auto": True}),
    dict(id="gpu_vram_test", name="GPU — Intégrité VRAM", category="GPU",
         desc="Remplit 95 % de la VRAM et vérifie les calculs (gpu-burn -m 95%) : détecte les erreurs mémoire",
         requires=["gpu-burn"], fn=bench_gpu_vram_test,
         params={"time_s": 300, "vram_pct": 95, "precision": "float", "target_gpus": ""}),
    dict(id="gpu_burn",    name="GPU gpu-burn",   category="GPU",
         desc="Stress CUDA (NVIDIA uniquement) — GFLOPS + erreurs memoire",
         requires=["gpu-burn"], fn=bench_gpu_burn,
         params={"time_s": 30}),
    dict(id="gpu_fryer",   name="GPU Compute fryer",  category="GPU",
         desc="Stress de calcul GPU NVIDIA uniquement (CUDA) — TFLOPS ; pour Intel / AMD : chauffe vkpeak / clpeak",
         requires=["gpu-fryer"], fn=bench_gpu_fryer,
         params={"time_s": 30}),
    dict(id="gpu_nvbandwidth", name="GPU nvbandwidth", category="GPU",
         desc="Mesure de bande passante inter-GPU et CPU-GPU (nvbandwidth)",
         requires=["nvbandwidth"], fn=bench_gpu_nvbandwidth,
         params={"buffer_size_mb": 512, "iterations": 3}),
    dict(id="axelera_stress", name="Axelera — Chauffe & Stabilité AIPU", category="Axelera AI",
         desc="Charge IA continue et test de modèles sur cartes Axelera Metis (axrunmodel / inference.py) : température, puissance, horloge, MVM et stabilité FPS",
         requires=["axdevice"], fn=bench_axelera_stress,
         params={"model_path": "", "engine": "auto", "media_path": "", "time_s": 60, "aipu_cores": 4, "target_devices": "", "throttle_fps": 0, "sdk_dir": "", "extra_args": ""}),
]

CAT_ORDER  = ["CPU + RAM", "Stockage", "GPU", "Axelera AI", "Réseau"]
CAT_COLORS = {
    "CPU + RAM": "#f97316",
    "CPU":       "#f97316",
    "RAM":       "#22d3ee",
    "Stockage":  "#e879f9",
    "GPU":       "#a78bfa",
    "Axelera AI": "#38bdf8",
    "Réseau":    "#34d399",
}
