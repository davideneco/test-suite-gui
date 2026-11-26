# -*- coding: utf-8 -*-
"""Collecte télémétrique temps réel pendant les benchmarks (CPU, GPU, SSD, puissance, fréquences)."""

import atexit
import json
import os
try:
    import psutil
except ImportError:
    psutil = None
import queue
import re
import shutil
import signal
import statistics
import subprocess
import threading
import time

from core.bench_utils import (
    _read_ram_temperature_power_best_effort,
    _run,
    tool_exists,
)

def _poll_ssd_sensors(series: dict, stop: threading.Event, t0: float, target_path: str, on_series_cb=None) -> None:
    try:
        from core.telemetry_aggregator import start_background_telemetry
        start_background_telemetry(series, stop.is_set)
    except:
        pass
    from core.ssd_diagnostics import read_ssd_sensors
    while not stop.is_set():
        t = round(time.time() - t0, 1)
        try:
            temp, power, _ = read_ssd_sensors(target_path)
            series.setdefault("t", []).append(t)
            series.setdefault("temp", []).append(temp)
            series.setdefault("power", []).append(power)
            if on_series_cb:
                on_series_cb({"t": t, "temp": temp, "power": power})
        except Exception:
            pass
        stop.wait(1.0)


_HWMON_ROOT = "/sys/class/hwmon"
_THERMAL_ROOT = "/sys/class/thermal"
_CPU_TEMP_SOURCE = {"v": ""}                    # source de la dernière température CPU lue (traçabilité dans les rapports)
_CPU_TEMP_DRIVERS = ("k10temp", "coretemp", "zenpower", "cpu_thermal")
_CPU_TEMP_LABEL = re.compile(r"^(cpu|soc|package|pkg|tctl|tdie|core|proc|die)", re.I)
_CPU_THERMAL_TYPE = re.compile(r"^(cpu|soc|x86_pkg_temp|package|cluster|big|little)", re.I)
_BMC_CPU_TEMP = re.compile(r"CPU|SOC|PROC|SOCKET|PKG|PACKAGE", re.I)
_BMC_NOT_CPU = re.compile(r"DIMM|DDR|VRD|VRM|MEM|FAN|PCH|BMC|INLET|OUTLET|AMBIENT|PSU|DISK|NVME|GPU", re.I)


def _read_milli(path: str):
    """Valeur sysfs en milli-degrés → °C ; None si illisible ou aberrante."""
    try:
        with open(path) as f:
            v = float(f.read().strip()) / 1000.0
        return v if -40.0 < v < 150.0 else None
    except Exception:
        return None


def _cpu_temps_hwmon_known() -> "list[float]":
    """Pilotes hwmon connus pour le CPU (AMD / Intel / ARM générique)."""
    temps = []
    for hw in sorted(os.listdir(_HWMON_ROOT)):
        try:
            with open(f"{_HWMON_ROOT}/{hw}/name") as f:
                name = f.read().strip()
        except Exception:
            continue
        if name not in _CPU_TEMP_DRIVERS:
            continue
        for tf in ("temp1_input", "temp2_input"):
            v = _read_milli(f"{_HWMON_ROOT}/{hw}/{tf}")
            if v is not None:
                temps.append(v)
                _CPU_TEMP_SOURCE["v"] = f"hwmon:{name}"
                break
    return temps


def _cpu_temps_hwmon_labels() -> "list[float]":
    """N'importe quel pilote hwmon dont une entrée de température est ÉTIQUETÉE cpu / soc / package / tctl / core… (une valeur par pilote)."""
    temps = []
    for hw in sorted(os.listdir(_HWMON_ROOT)):
        base = f"{_HWMON_ROOT}/{hw}"
        best, src = None, ""
        try:
            with open(f"{base}/name") as f:
                name = f.read().strip()
            files = sorted(os.listdir(base))
        except Exception:
            continue
        for fn in files:
            m = re.match(r"^(temp\d+)_label$", fn)
            if not m:
                continue
            try:
                with open(f"{base}/{fn}") as f:
                    label = f.read().strip()
            except Exception:
                continue
            if not _CPU_TEMP_LABEL.match(label):
                continue
            v = _read_milli(f"{base}/{m.group(1)}_input")
            if v is not None and (best is None or v > best):
                best, src = v, f"hwmon:{name}/{label}"
        if best is not None:
            temps.append(best)
            _CPU_TEMP_SOURCE["v"] = src
    return temps


def _cpu_temps_thermal_zones() -> "list[float]":
    """Zones thermiques noyau (/sys/class/thermal) dont le type évoque le CPU / SoC."""
    temps = []
    for z in sorted(os.listdir(_THERMAL_ROOT)):
        if not z.startswith("thermal_zone"):
            continue
        try:
            with open(f"{_THERMAL_ROOT}/{z}/type") as f:
                ztype = f.read().strip()
        except Exception:
            continue
        if not _CPU_THERMAL_TYPE.match(ztype):
            continue
        v = _read_milli(f"{_THERMAL_ROOT}/{z}/temp")
        if v is not None:
            temps.append(v)
            _CPU_TEMP_SOURCE["v"] = f"thermal:{ztype}"
    return temps


def _bmc_cpu_temps(items) -> "list[float]":
    """Températures CPU / SoC remontées par le BMC (IPMI / Redfish), d'après les capteurs déjà collectés (aucun appel supplémentaire).
    Un capteur compte s'il est de catégorie « temperature », que son nom évoque le CPU / SoC / socket et pas la RAM, un VRM, etc."""
    by_socket: dict = {}
    for m in items or []:
        try:
            name = str(m.get("name", ""))
            if m.get("category") != "temperature" or not _BMC_CPU_TEMP.search(name) or _BMC_NOT_CPU.search(name):
                continue
            val = float(m.get("value", 0.0))
            if not (0.0 < val < 150.0):
                continue
            mi = re.search(r"(?:CPU|SOC|SOCKET|PROC|P)[_\-\s]*(\d+)", name, re.I)
            idx = int(mi.group(1)) if mi else 0
            if val > by_socket.get(idx, (0.0, ""))[0]:
                by_socket[idx] = (val, name)
        except Exception:
            continue
    if not by_socket:
        return []
    _CPU_TEMP_SOURCE["v"] = "bmc:" + ",".join(n for _, n in (by_socket[k] for k in sorted(by_socket)))
    return [by_socket[k][0] for k in sorted(by_socket)]


def _read_cpu_temps_per_socket(bmc_items=None) -> "list[float]":
    """Retourne une température par socket / instance, avec plusieurs sources de secours (rien d'exclusif à une marque) :
    1. pilotes hwmon CPU connus · 2. toute entrée hwmon étiquetée cpu / soc / package · 3. zones thermiques noyau cpu / soc
    4. `sensors` (Tctl, Core 0) · 5. capteurs CPU / SoC du BMC (déjà collectés par l'appelant, `bmc_items`).
    Liste vide si aucune source : l'appelant n'affiche alors AUCUNE température (pas de « 0 °C »)."""
    _CPU_TEMP_SOURCE["v"] = ""
    for fn in (_cpu_temps_hwmon_known, _cpu_temps_hwmon_labels, _cpu_temps_thermal_zones):
        try:
            temps = fn()
        except Exception:
            temps = []
        if temps:
            return temps
    rc, out, _ = _run(["sensors"], timeout=3)
    if rc == 0:
        vals = re.findall(r"Tctl:\s+\+?([\d.]+)", out) or re.findall(r"Core 0:\s+\+?([\d.]+)", out)
        if vals:
            _CPU_TEMP_SOURCE["v"] = "sensors"
            return [float(v) for v in vals]
    return _bmc_cpu_temps(bmc_items)


def _read_sysfs_file(path: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""


def _find_hwmon_dir(bus_id: str) -> str:
    if not bus_id:
        return ""
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    if not os.path.exists(pci_path):
        return ""
    
    hwmon_base = os.path.join(pci_path, "hwmon")
    if os.path.exists(hwmon_base):
        try:
            for subdir in os.listdir(hwmon_base):
                if subdir.startswith("hwmon"):
                    return os.path.join(hwmon_base, subdir)
        except Exception:
            pass
            
    drm_base = os.path.join(pci_path, "drm")
    if os.path.exists(drm_base):
        try:
            for card in os.listdir(drm_base):
                if card.startswith("card"):
                    hwmon_drm = os.path.join(drm_base, card, "device", "hwmon")
                    if os.path.exists(hwmon_drm):
                        for subdir in os.listdir(hwmon_drm):
                            if subdir.startswith("hwmon"):
                                return os.path.join(hwmon_drm, subdir)
        except Exception:
            pass
            
    return ""


def _find_card_dir(bus_id: str) -> str:
    if not bus_id:
        return ""
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    if not os.path.exists(pci_path):
        return ""
    drm_base = os.path.join(pci_path, "drm")
    if os.path.exists(drm_base):
        try:
            for card in os.listdir(drm_base):
                if card.startswith("card"):
                    return os.path.join(drm_base, card)
        except Exception:
            pass
    return ""


def _get_gpu_temp_sysfs(bus_id: str) -> float:
    hwmon = _find_hwmon_dir(bus_id)
    if hwmon:
        val = _read_sysfs_file(os.path.join(hwmon, "temp1_input"))
        if val:
            try:
                return float(val) / 1000.0
            except ValueError:
                pass
    return None


def _get_gpu_power_sysfs(bus_id: str) -> float:
    hwmon = _find_hwmon_dir(bus_id)
    if hwmon:
        for fname in ("power1_average", "power1_input"):
            val = _read_sysfs_file(os.path.join(hwmon, fname))
            if val:
                try:
                    return float(val) / 1000000.0
                except ValueError:
                    pass
    return None


def _get_gpu_freq_sysfs(bus_id: str, fabricant: str) -> float:
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    if fabricant == "AMD" or os.path.exists(os.path.join(pci_path, "pp_dpm_sclk")):
        sclk_path = os.path.join(pci_path, "pp_dpm_sclk")
        content = _read_sysfs_file(sclk_path)
        if content:
            for line in content.splitlines():
                if "*" in line:
                    match = re.search(r"(\d+)\s*(Mhz|MHz)", line)
                    if match:
                        return float(match.group(1))
                    parts = line.split()
                    for part in parts:
                        if part.lower().endswith("mhz"):
                            try:
                                return float(part[:-3])
                            except ValueError:
                                pass
        hwmon = _find_hwmon_dir(bus_id)
        if hwmon:
            for fname in ("freq1_input", "freq1_actual"):
                val = _read_sysfs_file(os.path.join(hwmon, fname))
                if val:
                    try:
                        return float(val) / 1000000.0
                    except ValueError:
                        pass
    card = _find_card_dir(bus_id)
    if card:
        val = _read_sysfs_file(os.path.join(card, "gt_act_freq_mhz"))
        if val:
            try:
                return float(val)
            except ValueError:
                pass
        val = _read_sysfs_file(os.path.join(card, "gt", "gt0", "rps_act_freq_mhz"))
        if val:
            try:
                return float(val)
            except ValueError:
                pass
    val = _read_sysfs_file(os.path.join(pci_path, "tile0", "gt0", "freq0", "act_freq"))
    if val:
        try:
            return float(val)
        except ValueError:
            pass
    return None


def _get_gpu_mem_freq_sysfs(bus_id: str) -> float:
    pci_path = f"/sys/bus/pci/devices/{bus_id}"
    mclk_path = os.path.join(pci_path, "pp_dpm_mclk")
    content = _read_sysfs_file(mclk_path)
    if content:
        for line in content.splitlines():
            if "*" in line:
                match = re.search(r"(\d+)\s*(Mhz|MHz)", line)
                if match:
                    return float(match.group(1))
                parts = line.split()
                for part in parts:
                    if part.lower().endswith("mhz"):
                        try:
                            return float(part[:-3])
                        except ValueError:
                            pass
    return None


def _poll_gpu_sensors(series: dict, stop: threading.Event, t0: float,
                      series_cb=None, target_gpus: str = "") -> None:
    """Background thread: interroge les capteurs GPU (NVIDIA, AMD, Intel) toutes les secondes."""
    own_keys: set = set()          # séries écrites par ce relevé (les autres appartiennent à d'autres fils)
    from core.system_info import get_gpu_info
    
    detected_gpus = []
    try:
        detected_gpus = get_gpu_info()
    except Exception:
        pass

    if not detected_gpus:
        has_nvidia = tool_exists("nvidia-smi")
    else:
        has_nvidia = any(g.get("fabricant") == "NVIDIA" for g in detected_gpus)

    # GPU Intel : xpu-smi (températures cœur/mémoire, puissance, fréquence, utilisation) interrogé en arrière-plan
    xpu = None
    xpu_map: dict = {}
    try:
        from core import intel_gpu
        if intel_gpu.available() and any(g.get("fabricant") == "Intel" for g in detected_gpus):
            for i_g, g_ in enumerate(detected_gpus):
                if g_.get("fabricant") == "Intel":
                    d_ = intel_gpu.device_for_bdf(g_.get("pci_bus", ""))
                    if d_:
                        xpu_map[i_g] = d_["id"]
            if xpu_map:
                xpu = intel_gpu.XpuSampler()
                xpu.start(sorted(set(xpu_map.values())))
    except Exception:
        xpu = None

    while not stop.is_set():
        t = round(time.time() - t0, 1)
        point: dict = {"t": t}
        
        # ── Hotspot / VRAM via LACT (sans root, timeouts courts) ──
        try:
            from core.system_info import get_lact_temps
            lact_temps = get_lact_temps()
        except Exception:
            lact_temps = {}

        nvidia_points = {}
        if has_nvidia and tool_exists("nvidia-smi"):
            rc, out, _ = _run([
                "nvidia-smi",
                "--query-gpu=pci.bus_id,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory,clocks_throttle_reasons.sw_thermal_slowdown,clocks_throttle_reasons.hw_thermal_slowdown",
                "--format=csv,noheader,nounits",
            ], timeout=4)
            if rc != 0 or not out.strip():
                rc, out, _ = _run([
                    "nvidia-smi",
                    "--query-gpu=pci.bus_id,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory",
                    "--format=csv,noheader,nounits",
                ], timeout=4)
            if rc == 0 and out.strip():
                gpu_lines = [l.strip() for l in out.strip().splitlines() if l.strip()]
                for i, line in enumerate(gpu_lines):
                    try:
                        parts = line.split(",")
                        if len(parts) >= 5:
                            bus = parts[0].strip().lower()
                            short_bus = ':'.join(bus.split(':')[-2:])
                            # Champ par champ : un seul « [N/A] » (souvent power.draw) ne doit pas faire disparaître
                            # tout le GPU du relevé (on perdait aussi sa température).
                            def _fnum(x):
                                try:
                                    return float(x.strip())
                                except (TypeError, ValueError):
                                    return None
                            throttled = 0
                            if len(parts) >= 7:
                                sw_th = parts[5].strip().lower() == "active"
                                hw_th = parts[6].strip().lower() == "active"
                                throttled = 1 if (sw_th or hw_th) else 0
                            pt = {"bus": short_bus, "throttle_thermal": throttled}
                            for key, raw in (("temp", parts[1]), ("power", parts[2]),
                                             ("freq_graphics", parts[3]), ("freq_memory", parts[4])):
                                v = _fnum(raw)
                                if v is not None:
                                    pt[key] = v
                            if len(pt) > 2:
                                nvidia_points[i] = pt
                    except ValueError:
                        pass
        
        try:
            nvidia_idx = 0
            for i, gpu in enumerate(detected_gpus):
                fabricant = gpu.get("fabricant")
                bus_id = gpu.get("pci_bus", "")
                pfx = f"gpu{i}_"
                
                if fabricant == "NVIDIA":
                    if nvidia_idx in nvidia_points:
                        pt = nvidia_points[nvidia_idx]
                        for _k in ("temp", "power", "freq_graphics", "freq_memory"):
                            if _k in pt:
                                point[pfx + _k] = pt[_k]
                        point[pfx + "throttle_thermal"] = pt.get("throttle_thermal", 0)
                        
                        bdf = pt.get("bus", "")
                        if not bdf and bus_id:
                            bdf = ':'.join(bus_id.lower().split(':')[-2:])
                        if bdf in lact_temps:
                            hs, vr = lact_temps[bdf]
                            if hs is not None: point[pfx + "temp_hotspot"] = hs
                            if vr is not None: point[pfx + "temp_vram"] = vr
                    else:
                        temp = _get_gpu_temp_sysfs(bus_id)
                        power = _get_gpu_power_sysfs(bus_id)
                        freq = _get_gpu_freq_sysfs(bus_id, "NVIDIA")
                        if temp is not None: point[pfx + "temp"] = temp
                        if power is not None: point[pfx + "power"] = power
                        if freq is not None: point[pfx + "freq_graphics"] = freq
                    nvidia_idx += 1
                else:
                    temp = _get_gpu_temp_sysfs(bus_id)
                    power = _get_gpu_power_sysfs(bus_id)
                    freq_gpu = _get_gpu_freq_sysfs(bus_id, fabricant)
                    
                    if temp is not None:
                        point[pfx + "temp"] = temp
                    if power is not None:
                        point[pfx + "power"] = power
                    if freq_gpu is not None:
                        point[pfx + "freq_graphics"] = freq_gpu

                    # Intel : capteurs hwmon du pilote xe (contrôleur mémoire, PCIe, canaux VRAM, ventilateur), sans droits.
                    # Le capteur « pkg » (package = puce GPU) est LA température du cœur ; les autres sont secondaires.
                    pkg_ok = False
                    if fabricant == "Intel":
                        try:
                            from core import intel_gpu as _ig
                            ex_ = _ig.hwmon_extras(bus_id)
                            if "temp_pkg" in ex_:
                                point[pfx + "temp"] = ex_["temp_pkg"]
                                pkg_ok = True
                            for src_, dst_ in (("temp_mctrl", "temp_mctrl"), ("temp_pcie", "temp_pcie"),
                                               ("temp_vram_ch_max", "temp_vram_ch_max"),
                                               ("fan_rpm", "fan_rpm"), ("fan2_rpm", "fan2_rpm"), ("fan3_rpm", "fan3_rpm"),
                                               ("voltage_v", "voltage_v")):
                                if src_ in ex_:
                                    point[pfx + dst_] = ex_[src_]
                            if "temp_vram_hwmon" in ex_ and (pfx + "temp_vram") not in point:
                                point[pfx + "temp_vram"] = ex_["temp_vram_hwmon"]
                            xf_ = _ig.xe_freqs(bus_id)
                            for src_, dst_ in (("freq_graphics", "freq_graphics"), ("freq_request", "freq_request"),
                                               ("freq_media", "freq_media"), ("freq_media_request", "freq_media_request"),
                                               ("throttle_thermal", "throttle_thermal")):
                                if src_ in xf_:
                                    point[pfx + dst_] = xf_[src_]
                        except Exception:
                            pass

                    # Intel : xpu-smi complète / remplace sysfs (la température du processeur n'est plus prise pour celle du GPU)
                    if fabricant == "Intel" and xpu is not None and i in xpu_map:
                        xv = xpu.latest(xpu_map[i])
                        for src, dst in (
                            ("temp", "temp"), ("temp_vram", "temp_vram"), ("temp_vr", "temp_vr"),
                            ("power", "power"), ("voltage_v", "voltage_v"),
                            ("freq_graphics", "freq_graphics"), ("freq_request", "freq_request"),
                            ("freq_memory", "freq_memory"),
                            ("freq_media", "freq_media"), ("freq_media_request", "freq_media_request"),
                            ("util", "util"), ("util_compute", "util_compute"), ("util_render", "util_render"),
                            ("util_copy", "util_copy"), ("util_media", "util_media"), ("util_3d", "util_3d"),
                            ("eu_active", "eu_active"), ("eu_stall", "eu_stall"),
                            ("mem_util", "mem_util"), ("mem_used_mib", "mem_used_mib"),
                            ("mem_bw", "mem_bw"), ("mem_read_kbps", "mem_read_kbps"), ("mem_write_kbps", "mem_write_kbps"),
                            ("pcie_read_kbps", "pcie_read_kbps"), ("pcie_write_kbps", "pcie_write_kbps"),
                            ("throttle_thermal", "throttle_thermal"),
                            ("ras_reset", "ras_reset"), ("ras_programming_errors", "ras_programming_errors"),
                            ("ras_driver_errors", "ras_driver_errors"),
                            ("ras_cache_corr", "ras_cache_corr"), ("ras_cache_uncorr", "ras_cache_uncorr"),
                            ("ras_mem_corr", "ras_mem_corr"), ("ras_mem_uncorr", "ras_mem_uncorr"),
                        ):
                            if src == "temp" and pkg_ok:
                                continue                       # la température du cœur vient déjà du capteur « pkg » (hwmon)
                            if src in xv:
                                point[pfx + dst] = xv[src]
                        
                    if fabricant == "AMD":
                        freq_mem = _get_gpu_mem_freq_sysfs(bus_id)
                        if freq_mem is not None:
                            point[pfx + "freq_memory"] = freq_mem

            if not detected_gpus and nvidia_points:
                for idx, pt in nvidia_points.items():
                    pfx = f"gpu{idx}_"
                    for _k in ("temp", "power", "freq_graphics", "freq_memory"):
                        if _k in pt:
                            point[pfx + _k] = pt[_k]
                    point[pfx + "throttle_thermal"] = pt.get("throttle_thermal", 0)

            if "gpu0_temp" in point:
                point["temp"]          = point["gpu0_temp"]
            if "gpu0_power" in point:
                point["power"]         = point["gpu0_power"]
            if "gpu0_freq_graphics" in point:
                point["freq_graphics"] = point["gpu0_freq_graphics"]
            if "gpu0_freq_memory" in point:
                point["freq_memory"]   = point["gpu0_freq_memory"]

            if len(point) > 1:
                series["t"].append(t)
                n_t = len(series["t"])
                for k, v in point.items():
                    if k == "t":
                        continue
                    if k not in series:
                        series[k] = [v] * (n_t - 1)       # capteur apparu en cours de test : aligné sur « t »
                    series[k].append(v)
                    own_keys.add(k)
                # Capteur absent de ce relevé (champ [N/A] ponctuel, LACT intermittent) : on répète sa dernière valeur. Sans cela
                # sa série devenait plus courte que « t » et toutes ses valeurs suivantes étaient décalées dans le temps
                # (courbes et statistiques « en charge » faussées).
                for k in own_keys:
                    lst = series.get(k)
                    if isinstance(lst, list) and lst and len(lst) < n_t:
                        lst.extend([lst[-1]] * (n_t - len(lst)))
                if series_cb:
                    series_cb(point)
        except Exception:
            pass
        stop.wait(1.0)
    if xpu is not None:
        xpu.stop()


# ── Détection de la source de puissance CPU (cache, multi-socket) ────────────
# Format : ("rapl", [path, ...]) | ("amd_energy", [path, ...]) |
#          ("hwmon_w", [path, ...]) | ("sensors_w",) | ("powerstat", [cmd, ...]) |
#          ("ipmi",) | ("turbostat",) | ("none",)
_POWER_SRC: "list[tuple]" = []
_POWERSTAT_PROC = None
_POWERSTAT_Q = queue.Queue(maxsize=32)
_POWERSTAT_LAST = None
_POWERSTAT_LOCK = threading.Lock()


def _terminate_proc_maybe_sudo(proc) -> None:
    """Arrête proprement un sous-processus et son groupe, y compris s'il a été lancé via `sudo -n` (root)."""
    if proc is None:
        return
    try:
        if proc.poll() is not None:
            return
    except Exception:
        return

    def _send_sig(sig_name: str) -> None:
        sig = getattr(signal, "SIG" + sig_name)
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
        except Exception:
            pass
        try:
            if pgid == proc.pid:
                os.killpg(pgid, sig)
            else:
                os.kill(proc.pid, sig)
            return
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        except Exception:
            pass
        # Repli non-interactif si le processus tourne en root via `sudo -n`
        if shutil.which("sudo"):
            target = [f"-{pgid}"] if pgid == proc.pid else [str(proc.pid)]
            try:
                subprocess.run(["sudo", "-n", "kill", f"-{sig_name}", "--"] + target,
                               capture_output=True, timeout=3)
            except Exception:
                pass

    _send_sig("TERM")
    try:
        proc.wait(timeout=1.5)
        return
    except Exception:
        pass
    _send_sig("KILL")
    try:
        proc.wait(timeout=1.5)
    except Exception:
        pass


def _stop_powerstat_proc() -> None:
    """Arrête proprement powerstat et ses descendants à la fermeture."""
    global _POWERSTAT_PROC
    proc = _POWERSTAT_PROC
    if proc is None:
        return
    _terminate_proc_maybe_sudo(proc)
    _POWERSTAT_PROC = None


atexit.register(_stop_powerstat_proc)


def _start_powerstat_reader(cmd: list) -> bool:
    """Lance powerstat en streaming et parse la dernière colonne Watts.

    Commande attendue côté utilisateur :
        sudo sh -c 'stdbuf -oL powerstat -R 1 999999'

    En GUI on utilise sudo -n pour éviter tout blocage sur un prompt mot de passe.
    Il faut donc soit un timestamp sudo récent, soit une règle sudoers/NOPASSWD.
    """
    global _POWERSTAT_PROC, _POWERSTAT_LAST
    if _POWERSTAT_PROC is not None and _POWERSTAT_PROC.poll() is None:
        return True
    _POWERSTAT_LAST = None

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        _POWERSTAT_PROC = proc
    except Exception:
        _POWERSTAT_PROC = None
        return False

    def _reader():
        global _POWERSTAT_LAST
        try:
            assert proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                # Exemple powerstat Ryzen :
                # 14:31:04 ... 17.66
                if not re.match(r"^\d\d:\d\d:\d\d\s+", line):
                    continue
                parts = line.split()
                if not parts:
                    continue
                try:
                    watts = float(parts[-1].replace(",", "."))
                except Exception:
                    continue
                with _POWERSTAT_LOCK:
                    _POWERSTAT_LAST = watts
                try:
                    if _POWERSTAT_Q.full():
                        _POWERSTAT_Q.get_nowait()
                    _POWERSTAT_Q.put_nowait(watts)
                except Exception:
                    pass
        except Exception:
            pass

    threading.Thread(target=_reader, daemon=True).start()
    return True


def _get_powerstat_latest(timeout_s: float = 0.0) -> float:
    """Retourne la dernière valeur Watts lue par powerstat, ou 0.0."""
    global _POWERSTAT_LAST
    if timeout_s and _POWERSTAT_LAST is None:
        end = time.time() + timeout_s
        while time.time() < end and _POWERSTAT_LAST is None:
            time.sleep(0.05)
    with _POWERSTAT_LOCK:
        return float(_POWERSTAT_LAST or 0.0)


def _reset_power_cache() -> None:
    """Réinitialise le cache de détection (utile si un module est chargé entre 2 runs)."""
    _POWER_SRC.clear()


def _detect_power_source() -> tuple:
    if _POWER_SRC:
        return _POWER_SRC[0]

    # 1. turbostat — priorité absolue, gère multi-socket nativement.
    #    turbostat peut rester vivant sans fournir de mesures exploitables selon
    #    les permissions/plateformes → validation sur une vraie ligne watts.
    #    On essaie d'abord sans sudo (app lancée en root), puis avec sudo -n (NOPASSWD).
    #    Le tuple stocké contient la commande complète : ("turbostat", [cmd, ...])
    _TSTAT_CMD = ["turbostat", "--quiet", "--interval", "1", "--show", "Package,PkgWatt"]
    _tstat_env = os.environ.copy()
    _tstat_env["LC_ALL"] = "C"
    def _turbostat_has_sample(cmd: list) -> bool:
        import select as _select
        proc = None
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                env=_tstat_env,
                start_new_session=True,
            )
            end = time.time() + 2.5
            while time.time() < end:
                if proc.poll() is not None:
                    return False
                if proc.stdout is None:
                    return False
                rem = max(0.05, end - time.time())
                ready, _, _ = _select.select([proc.stdout], [], [], min(0.25, rem))
                if not ready:
                    continue
                line = proc.stdout.readline()
                if not line:
                    continue
                parts = line.strip().split()
                if len(parts) != 2 or not parts[0].isdigit():
                    continue
                try:
                    float(parts[1].replace(",", "."))
                    return True
                except Exception:
                    continue
            return False
        except Exception:
            return False
        finally:
            if proc is not None:
                _terminate_proc_maybe_sudo(proc)
                if proc.stdout is not None:
                    try:
                        proc.stdout.close()
                    except Exception:
                        pass
    if tool_exists("turbostat"):
        for prefix in ([], ["sudo", "-n"]):
            if _turbostat_has_sample(prefix + _TSTAT_CMD):
                _POWER_SRC.append(("turbostat", prefix + _TSTAT_CMD))
                return _POWER_SRC[0]

    # 2. RAPL powercap — collecte TOUS les packages (intel-rapl:0, intel-rapl:1, …)
    #    Essai direct d'abord, puis sudo -n si permission refusée.
    rapl_paths = []
    rapl_sudo  = False
    try:
        pc_base = "/sys/class/powercap"
        if os.path.exists(pc_base):
            for entry in sorted(os.listdir(pc_base)):
                if re.match(r"intel-rapl:\d+$", entry):
                    p = f"{pc_base}/{entry}/energy_uj"
                    if not os.path.exists(p):
                        continue
                    # Tentative de lecture directe
                    try:
                        with open(p) as f:
                            int(f.read().strip())
                        rapl_paths.append(p)
                    except PermissionError:
                        # Tentative via sudo -n cat
                        rc2, out2, _ = _run(["sudo", "-n", "cat", p], timeout=2)
                        if rc2 == 0:
                            rapl_paths.append(p)
                            rapl_sudo = True
    except Exception:
        pass
    if rapl_paths:
        _POWER_SRC.append(("rapl", rapl_paths, rapl_sudo))
        return _POWER_SRC[0]

    # 2. amd_energy — collecte toutes les entrées energy*_input (1 par socket)
    amd_paths = []
    try:
        for hwmon in sorted(os.listdir("/sys/class/hwmon/")):
            name_f = f"/sys/class/hwmon/{hwmon}/name"
            if not os.path.exists(name_f):
                continue
            with open(name_f) as f:
                if f.read().strip() != "amd_energy":
                    continue
            base = f"/sys/class/hwmon/{hwmon}"
            for fname in sorted(os.listdir(base)):
                if re.match(r"energy\d+_input", fname):
                    amd_paths.append(f"{base}/{fname}")
    except Exception:
        pass
    if amd_paths:
        _POWER_SRC.append(("amd_energy", amd_paths))
        return _POWER_SRC[0]

    # 3. hwmon générique power*_input en µW.
    #    Important Ryzen: certaines machines exposent la puissance CPU via
    #    k10temp/zenpower avec des labels PPT, Package, Core, SOC. Les anciennes
    #    versions ignoraient k10temp, ce qui masquait la puissance sur Ryzen.
    hw_paths = []
    cpu_power_drivers = {"zenpower", "k10temp", "amd_energy", "fam15h_power", "acpi_power_meter"}
    bad_power_drivers = {"amdgpu", "nouveau", "i915", "nvme", "drivetemp", "iwlwifi"}
    cpu_power_label_re = re.compile(r"(cpu|package|pkg|ppt|socket|core|soc|svi2|tdp)", re.I)
    try:
        for hwmon in sorted(os.listdir("/sys/class/hwmon/")):
            base = f"/sys/class/hwmon/{hwmon}"
            name_f = f"{base}/name"
            drv = ""
            try:
                with open(name_f) as f:
                    drv = f.read().strip()
            except Exception:
                pass
            if drv in bad_power_drivers:
                continue
            for fname in sorted(os.listdir(base)):
                m = re.match(r"power(\d+)_(input|average)", fname)
                if not m:
                    continue
                idx = m.group(1)
                p = f"{base}/{fname}"
                label = ""
                for lf in (f"{base}/power{idx}_label", f"{base}/power{idx}_name"):
                    try:
                        with open(lf) as f:
                            label = f.read().strip()
                        if label:
                            break
                    except Exception:
                        pass

                # Garder si driver clairement CPU/AMD, ou label clairement CPU — ou domaine IO / SoC d'une puce qui a aussi un
                # canal CPU (ARM Ampere xgene-hwmon : « CPU power » = cœurs, « IO power » = reste du die, additionnés à la lecture).
                if drv not in cpu_power_drivers and not cpu_power_label_re.search(label):
                    if not (_CPU_AUX_POWER_RE.search(label) and _hwmon_has_cpu_power(base)):
                        continue
                try:
                    with open(p) as f:
                        int(f.read().strip())   # vérifie que c'est lisible
                    hw_paths.append((p, label or drv or os.path.basename(base)))
                except Exception:
                    pass
    except Exception:
        pass
    if hw_paths:
        _POWER_SRC.append(("hwmon_w", hw_paths))
        return _POWER_SRC[0]

    # 4. Fallback lm-sensors: utile sur Ryzen avec zenpower/k10temp quand
    #    la puissance est visible dans sensors mais pas normalisée dans hwmon.
    if tool_exists("sensors"):
        rc, out, _ = _run(["sensors"], timeout=3)
        if rc == 0 and re.search(r"(?im)^(PPT|CPU|Package|Tdie|SVI2|Core|SoC).*?([0-9]+(?:\.[0-9]+)?)\s*W", out):
            _POWER_SRC.append(("sensors_w",))
            return _POWER_SRC[0]

    # 5. powerstat -R — fallback utile sur certains Ryzen desktop/workstation.
    #    powerstat impose au moins 60 mesures avec -R ; on le lance donc en
    #    streaming long et on parse la dernière colonne "Watts".
    #    sudo -n évite de bloquer l'interface si le mot de passe n'est pas disponible.
    if tool_exists("powerstat"):
        ps_cmd = ["sudo", "-n", "sh", "-c", "exec stdbuf -oL powerstat -R 1 999999"]
        if _start_powerstat_reader(ps_cmd):
            # On attend brièvement une première mesure. Si sudo refuse ou si la
            # sortie n'est pas exploitable, on continue vers IPMI au prochain appel.
            if _get_powerstat_latest(timeout_s=2.5) > 0:
                _POWER_SRC.append(("powerstat", ps_cmd))
                return _POWER_SRC[0]
            _stop_powerstat_proc()

    # 6. ipmitool DCMI — retourne la puissance totale du nœud (tous sockets)
    rc, out, _ = _run(["ipmitool", "dcmi", "power", "reading"], timeout=4)
    if rc == 0 and "Instantaneous power reading" in out:
        _POWER_SRC.append(("ipmi",))
        return _POWER_SRC[0]
    # "none" est mis en cache pour éviter de bloquer l'UI toutes les 2s avec ipmitool
    _POWER_SRC.append(("none",))
    return _POWER_SRC[0]


# Composition de la dernière puissance CPU lue (traçabilité dans les rapports), ex. « xgene_hwmon-acpi-0 : CPU power + IO power ».
_CPU_POWER_SOURCE = {"v": ""}

# Puissance d'un domaine du processeur hors cœurs (ARM Ampere xgene-hwmon : « IO power » ; AMD zenpower : « SoC »…).
_CPU_AUX_POWER_RE = re.compile(r"\b(io|soc|uncore|mesh|i/o)\b", re.I)
_CPU_CORE_POWER_RE = re.compile(r"\b(cpu|cores?)\b", re.I)
# Une puce n'est retenue que si un de ses canaux dit explicitement « CPU / socket / package » (comme avant) : on ne fait pas
# passer avant RAPL / amd_energy des capteurs ambigus (ex. zenpower SVI2_P_Core).
_CPU_CHIP_QUALIFY_RE = re.compile(r"(cpu|socket|pkg|package|proc)", re.I)
_CPU_TOTAL_POWER_RE = re.compile(r"\b(package|pkg|socket|ppt|total)\b", re.I)
_NOT_CPU_POWER_RE = re.compile(r"(gpu|dimm|mem|dram|psu|nvme|ssd|fan|pch|board|12v|5v|3\.?3v)", re.I)
_NOT_CPU_CHIPS = ("amdgpu", "nouveau", "i915", "xe", "nvme", "drivetemp", "iwlwifi", "acpi_power_meter")


def _hwmon_has_cpu_power(base: str) -> bool:
    """Vrai si la puce hwmon `base` expose un canal de puissance libellé CPU / cœurs (sert à rattacher son canal IO / SoC)."""
    try:
        for f in os.listdir(base):
            if re.match(r"power\d+_label$", f):
                with open(os.path.join(base, f)) as fh:
                    if _CPU_CORE_POWER_RE.search(fh.read()):
                        return True
    except Exception:
        pass
    return False


def _host_cpu_powers_per_socket(items) -> "list[float]":
    """Puissance par socket depuis les capteurs de l'hôte (lm-sensors / hwmon), en additionnant les domaines d'un même processeur.

    Sur ARM (Ampere Altra, xgene-hwmon) le pilote expose « CPU power » (cœurs seuls) et « IO power » (reste du die :
    mémoire, PCIe, maillage) ; la puissance du processeur est leur somme. Un canal « package / PPT / socket » est un
    total : il est pris seul. Chaque puce (un par socket) donne un socket, dans l'ordre de leur nom.
    Retourne [] si aucune puce ne ressemble à un processeur (l'appelant passe alors aux autres sources)."""
    groups: dict = {}
    for src in ("lm-sensors", "hwmon"):          # une seule source : les deux décrivent les mêmes capteurs
        for m in items:
            if m.get("source") != src or m.get("category") != "power":
                continue
            chip = m.get("chip_or_device") or ""
            if any(chip.lower().startswith(c) for c in _NOT_CPU_CHIPS):
                continue
            name = m.get("name", "")
            label = name[len(chip.split("@")[0]):].strip() if name.startswith(chip.split("@")[0]) else name
            try:
                val = float(m.get("value", 0.0))
            except (TypeError, ValueError):
                continue
            if val <= 0 or _NOT_CPU_POWER_RE.search(label):
                continue
            groups.setdefault(chip, []).append((label, val))
        if groups:
            break
    powers, desc = [], []
    for chip in sorted(groups):
        chans = groups[chip]
        if not any(_CPU_CHIP_QUALIFY_RE.search(l) for l, _ in chans):
            continue
        totals = [(l, v) for l, v in chans if _CPU_TOTAL_POWER_RE.search(l)]
        cores = [(l, v) for l, v in chans if _CPU_CORE_POWER_RE.search(l) and not _CPU_TOTAL_POWER_RE.search(l)]
        if totals:
            l, v = max(totals, key=lambda x: x[1])
            powers.append(round(v, 1))
            desc.append(f"{chip.split('@')[0]} : {l}")
        elif cores:
            aux = [(l, v) for l, v in chans if _CPU_AUX_POWER_RE.search(l) and (l, v) not in cores]
            parts = cores[:1] + aux
            powers.append(round(sum(v for _, v in parts), 1))
            desc.append(f"{chip.split('@')[0]} : " + " + ".join(l for l, _ in parts))
    if powers:
        _CPU_POWER_SOURCE["v"] = " | ".join(desc)
    return powers


def _read_bmc_cpu_powers_per_socket(items=None) -> "list[float]":
    """Récupère la puissance CPU par socket depuis les capteurs de la carte : BMC (Redfish/IPMI) d'abord, sinon capteurs de
    l'hôte (lm-sensors / hwmon) en additionnant les domaines d'un même processeur (cœurs + IO sur ARM).
    `items` : capteurs déjà collectés par l'appelant (évite un second appel lent à ipmitool / Redfish dans la même boucle)."""
    try:
        if items is None:
            from core.telemetry_aggregator import collect_all_motherboard_telemetry
            items = collect_all_motherboard_telemetry()
        host = _host_cpu_powers_per_socket(items)
        soc_powers: dict = {}
        for m in items:
            if m.get("source") in ("lm-sensors", "hwmon") and host:
                continue        # déjà regroupés par processeur ci-dessus
            name = m.get("name", "").upper()
            cat = m.get("category", "")
            val = float(m.get("value", 0.0))
            if val <= 0:
                continue
            
            # Priorité aux capteurs catégorisés 'power' avec CPU dans le nom, ou correspondances regex.
            if cat == "power" and ("CPU" in name or "SOCKET" in name or "PKG" in name or "PROC" in name):
                m_soc = re.search(r"(?:CPU|P|SOCKET|PROC)[_\-\s]*([0-9]+)", name)
                soc_idx = int(m_soc.group(1)) if m_soc else 0
                # Ne pas écraser par une lecture redondante plus faible
                soc_powers[soc_idx] = max(soc_powers.get(soc_idx, 0.0), val)
                continue

            m_soc = re.search(r"(?:CPU|P|SOCKET|PROC)[_\-\s]*([0-9]+)[_\-\s]*(?:POWER|PWR|WATT)", name) or \
                    re.search(r"(?:POWER|PWR)[_\-\s]*(?:CPU|P|SOCKET|PROC)[_\-\s]*([0-9]+)", name)
            if m_soc:
                soc_idx = int(m_soc.group(1))
                soc_powers[soc_idx] = max(soc_powers.get(soc_idx, 0.0), val)

        if soc_powers:
            # Assurer qu'on retourne une liste indexée sur [0, 1, ... max_socket] (fallback = 0.0)
            max_idx = max(soc_powers.keys()) if soc_powers else 0
            _CPU_POWER_SOURCE["v"] = "bmc"
            return [soc_powers.get(i, 0.0) for i in range(max_idx + 1)]
        if host:
            return host
    except Exception:
        pass
    return []


def _read_cpu_powers_per_socket(prev_state: dict, items=None) -> "list[float]":
    """Retourne la puissance en watts par socket (liste, un élément par socket/path)."""
    bmc_powers = _read_bmc_cpu_powers_per_socket(items)
    if bmc_powers:
        prev_state["power"] = bmc_powers
        return bmc_powers

    src = _detect_power_source()
    kind = src[0]

    if kind in ("rapl", "amd_energy"):
        paths    = src[1]
        use_sudo = (kind == "rapl") and (len(src) > 2) and src[2]
        now = time.time()
        prev_ujs = prev_state.get("ujs")
        curr_ujs = []
        for p in paths:
            try:
                if use_sudo:
                    rc2, out2, _ = _run(["sudo", "-n", "cat", p], timeout=2)
                    curr_ujs.append(int(out2.strip()) if rc2 == 0 else None)
                else:
                    with open(p) as f:
                        curr_ujs.append(int(f.read().strip()))
            except Exception:
                curr_ujs.append(None)
        result = []
        if prev_ujs is not None and prev_state.get("t") is not None:
            dt = now - prev_state["t"]
            prev_pwr = prev_state.get("power", [])
            for i, (cu, pu) in enumerate(zip(curr_ujs, prev_ujs)):
                if cu is not None and pu is not None and dt > 0:
                    delta = cu - pu
                    if delta < 0:
                        # Débordement (overflow) du compteur microjoules
                        result.append(prev_pwr[i] if i < len(prev_pwr) else 0.0)
                    else:
                        result.append(round(delta / dt / 1e6, 1))
                else:
                    result.append(prev_pwr[i] if i < len(prev_pwr) else 0.0)
        else:
            result = [0.0] * len(paths)
        prev_state["ujs"] = curr_ujs
        prev_state["t"]   = now
        prev_state["power"] = result
        return result

    if kind == "hwmon_w":
        # Un socket = une puce hwmon : ses canaux sont additionnés (cœurs + IO / SoC), sauf s'il y a un canal « total »
        # (package / PPT / socket), pris seul.
        result = []
        prev_pwr = prev_state.get("power", [])
        chips: dict = {}
        for item in src[1]:
            p, label = (item[0], item[1]) if isinstance(item, tuple) else (item, "")
            chips.setdefault(os.path.dirname(p), []).append((p, label))
        desc = []
        for i, (chip, chans) in enumerate(chips.items()):
            totals = [c for c in chans if _CPU_TOTAL_POWER_RE.search(c[1] or "")]
            use = totals[:1] or chans
            try:
                tot = 0.0
                for p, _ in use:
                    with open(p) as f:
                        tot += int(f.read().strip()) / 1e6
                result.append(round(tot, 1))
                desc.append(" + ".join(l or os.path.basename(p) for p, l in use))
            except Exception:
                result.append(prev_pwr[i] if i < len(prev_pwr) else 0.0)
        if desc:
            _CPU_POWER_SOURCE["v"] = "hwmon : " + " | ".join(desc)
        prev_state["power"] = result
        return result

    if kind == "sensors_w":
        rc, out, _ = _run(["sensors"], timeout=3)
        if rc == 0:
            vals = []
            # Priorité aux lignes CPU/package/PPT. Évite les rails PSU/GPU.
            for line in out.splitlines():
                if not re.search(r"(?i)(PPT|CPU|Package|Pkg|Socket|SVI2|Core|SoC|TDP)", line):
                    continue
                if re.search(r"(?i)(GPU|fan|in[0-9]|volt|12V|5V|3V)", line):
                    continue
                m = re.search(r"([0-9]+(?:\.[0-9]+)?)\s*W", line)
                if m:
                    vals.append(float(m.group(1)))
            if vals:
                # Sur Ryzen, PPT est souvent la meilleure valeur totale.
                return [max(vals)]
        return [0.0]

    if kind == "powerstat":
        watts = _get_powerstat_latest(timeout_s=0.2)
        return [round(watts, 2)] if watts > 0 else [0.0]

    if kind == "ipmi":
        rc, out, _ = _run(["ipmitool", "dcmi", "power", "reading"], timeout=4)
        if rc == 0:
            m = re.search(r"Instantaneous power reading:\s+([\d.]+)\s+Watts", out)
            if m:
                return [float(m.group(1))]
        return [0.0]

    return []   # turbostat et none → géré dans _poll_cpu_sensors


_CPU_SYSFS = "/sys/devices/system/cpu"
# État de la mesure de fréquence : source utilisée et divergence « demandée » (scaling_cur_freq) vs « matérielle » (cpuinfo_cur_freq).
_FREQ_STATE: dict = {"source": "", "samples": 0, "diverged": 0, "hw_mhz": 0.0, "sc_mhz": 0.0,
                     "sudo_ok": None, "sudo_retry_at": 0.0}
FREQ_DIVERGENCE_PCT = 8.0     # écart (%) à partir duquel « demandée » et « matérielle » sont jugées différentes


def _freq_state_reset() -> None:
    """Remet à zéro les compteurs de divergence au début d'un test (la source sudo mémorisée est conservée)."""
    _FREQ_STATE.update({"source": "", "samples": 0, "diverged": 0, "hw_mhz": 0.0, "sc_mhz": 0.0})


def _read_hw_freqs_khz(base: str, cpus: list) -> dict:
    """Fréquences lues côté matériel (`cpuinfo_cur_freq`, kHz) : lecture directe, sinon UN seul `sudo -n` (lecture seule).

    `scaling_cur_freq` n'est que la fréquence que le noyau croit avoir demandée : si le firmware / la carte bride le CPU
    (GPIO, limite de puissance ou thermique), elle peut afficher le maximum alors que le matériel tourne beaucoup plus bas.
    `cpuinfo_cur_freq` est en général réservé à root : sans droit, on retourne {} et l'appelant se replie sur scaling_cur_freq."""
    out: dict = {}
    for cpu in cpus:
        try:
            with open(f"{base}/{cpu}/cpufreq/cpuinfo_cur_freq") as f:
                out[cpu] = int(f.read().strip())
        except Exception:
            pass
    if out or _FREQ_STATE["sudo_ok"] is False and time.time() < _FREQ_STATE["sudo_retry_at"]:
        return out
    if not shutil.which("sudo"):
        _FREQ_STATE["sudo_ok"] = False; _FREQ_STATE["sudo_retry_at"] = time.time() + 300
        return out
    try:
        script = 'for f in "$1"/cpu[0-9]*/cpufreq/cpuinfo_cur_freq; do echo "$f $(cat "$f" 2>/dev/null)"; done'
        r = subprocess.run(["sudo", "-n", "sh", "-c", script, "sh", base], capture_output=True, text=True, timeout=4)
        for line in r.stdout.splitlines():
            m = re.match(r".*/(cpu\d+)/cpufreq/cpuinfo_cur_freq\s+(\d+)\s*$", line)
            if m:
                out[m.group(1)] = int(m.group(2))
        _FREQ_STATE["sudo_ok"] = bool(out)
    except Exception:
        _FREQ_STATE["sudo_ok"] = False
    if not out:
        _FREQ_STATE["sudo_retry_at"] = time.time() + 300      # pas de nouvel essai avant 5 min
    return out


def _read_cpu_freqs_per_socket(base: str = None) -> "list[float]":
    """Retourne la fréquence moyenne en MHz par socket physique.

    Par cœur : fréquence matérielle (`cpuinfo_cur_freq`) si lisible, sinon fréquence demandée (`scaling_cur_freq`).
    Les deux moyennes sont comparées pour détecter un CPU bridé (voir `_freq_diagnostic`)."""
    base = base or _CPU_SYSFS
    socket_freqs: dict = {}
    hw_all: list = []
    sc_all: list = []
    try:
        cpus = [c for c in sorted(os.listdir(base)) if c.startswith("cpu") and c[3:].isdigit()]
        hw = _read_hw_freqs_khz(base, cpus)
        for cpu in cpus:
            pkg_f  = f"{base}/{cpu}/topology/physical_package_id"
            freq_f = f"{base}/{cpu}/cpufreq/scaling_cur_freq"
            if not os.path.exists(pkg_f) or not (os.path.exists(freq_f) or cpu in hw):
                continue
            try:
                with open(pkg_f) as f:
                    pkg = int(f.read().strip())
                sc = None
                try:
                    with open(freq_f) as f:
                        sc = int(f.read().strip())
                except Exception:
                    pass
                if sc is not None:
                    sc_all.append(sc)
                if cpu in hw:
                    hw_all.append(hw[cpu])
                val_khz = hw[cpu] if cpu in hw else sc
                if val_khz is not None:
                    socket_freqs.setdefault(pkg, []).append(val_khz)
            except Exception:
                pass
    except Exception:
        pass
    if hw_all:
        _FREQ_STATE["source"] = "cpuinfo_cur_freq"
        h, c = sum(hw_all) / len(hw_all) / 1000, (sum(sc_all) / len(sc_all) / 1000 if sc_all else 0.0)
        _FREQ_STATE["samples"] += 1
        _FREQ_STATE["hw_mhz"], _FREQ_STATE["sc_mhz"] = h, c
        if c and h and abs(c - h) / h * 100 > FREQ_DIVERGENCE_PCT:
            _FREQ_STATE["diverged"] += 1
    elif sc_all:
        _FREQ_STATE["source"] = "scaling_cur_freq"
    result = []
    for pkg_id in sorted(socket_freqs):
        freqs = socket_freqs[pkg_id]
        if freqs:
            result.append(round(sum(freqs) / len(freqs) / 1000))   # kHz → MHz
    return result


def _freq_diagnostic() -> dict:
    """Métriques de fiabilité de la mesure de fréquence pour un test : source utilisée et, si elle a divergé, la note à afficher.

    Retourne {} si aucune fréquence n'a été lue. `freq_source` vaut `cpuinfo_cur_freq` (matériel) ou `scaling_cur_freq`
    (demandée, non vérifiée matériellement : c'est dit explicitement dans la note)."""
    src = _FREQ_STATE["source"]
    if not src:
        return {}
    d: dict = {"freq_source": src}
    n, bad = _FREQ_STATE["samples"], _FREQ_STATE["diverged"]
    if src == "cpuinfo_cur_freq" and n and bad / n >= 0.25:
        d["freq_demandee_mhz"] = round(_FREQ_STATE["sc_mhz"])
        d["freq_materielle_mhz"] = round(_FREQ_STATE["hw_mhz"])
        d["freq_note"] = (f"fréquence matérielle ({d['freq_materielle_mhz']} MHz) très différente de la fréquence demandée au noyau "
                          f"({d['freq_demandee_mhz']} MHz) : le CPU est probablement bridé par le firmware / la carte")
    elif src == "scaling_cur_freq":
        d["freq_note"] = ("fréquence = valeur demandée au noyau (scaling_cur_freq), non vérifiée côté matériel "
                          "(cpuinfo_cur_freq illisible : lancer avec sudo pour la mesure matérielle)")
    return d


def _read_cpu_freq_mhz() -> float:
    """Fréquence CPU moyenne (tous sockets) en MHz."""
    per_socket = _read_cpu_freqs_per_socket()
    if per_socket:
        return round(sum(per_socket) / len(per_socket))
    # Fallback psutil
    fi = psutil.cpu_freq() if psutil else None
    if fi:
        v = fi.current
        return round(v * 1000 if v < 100 else v)
    return 0


def _get_cpu_socket_mapping() -> "dict[int, int]":
    """Mappe chaque id logique CPU (0, 1, 2...) vers son id socket physique (0, 1...)."""
    mapping = {}
    base = "/sys/devices/system/cpu"
    try:
        if os.path.exists(base):
            for entry in os.listdir(base):
                if entry.startswith("cpu") and entry[3:].isdigit():
                    cpu_id = int(entry[3:])
                    pkg_f = f"{base}/{entry}/topology/physical_package_id"
                    if os.path.exists(pkg_f):
                        try:
                            with open(pkg_f) as f:
                                mapping[cpu_id] = int(f.read().strip())
                        except Exception:
                            pass
    except Exception:
        pass
    if mapping:
        return mapping

    try:
        if os.path.exists("/proc/cpuinfo"):
            curr_proc = None
            with open("/proc/cpuinfo") as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("processor"):
                        parts = line.split(":")
                        if len(parts) == 2:
                            curr_proc = int(parts[1].strip())
                    elif line.startswith("physical id") and curr_proc is not None:
                        parts = line.split(":")
                        if len(parts) == 2:
                            mapping[curr_proc] = int(parts[1].strip())
    except Exception:
        pass
    return mapping


def _read_cpu_pcts_per_socket() -> "list[float]":
    """Retourne la charge CPU moyenne (0-100%) par socket physique."""
    try:
        per_cpu_pcts = psutil.cpu_percent(percpu=True)
        if not per_cpu_pcts:
            return [psutil.cpu_percent(interval=None)]

        mapping = _get_cpu_socket_mapping()
        socket_pcts: dict = {}
        for idx, pct in enumerate(per_cpu_pcts):
            pkg = mapping.get(idx, 0)
            socket_pcts.setdefault(pkg, []).append(pct)

        res = []
        for pkg_id in sorted(socket_pcts):
            vals = socket_pcts[pkg_id]
            res.append(round(sum(vals) / len(vals), 1))
        return res if res else [round(sum(per_cpu_pcts) / len(per_cpu_pcts), 1)]
    except Exception:
        return [psutil.cpu_percent(interval=None)] if psutil else [0.0]


def _poll_cpu_sensors(series: dict, stop: threading.Event, t0: float,
                      series_cb=None) -> None:
    """Background thread: temp CPU, fréquence, charge, puissance par socket et capteurs VRM/Carte Mère."""
    try:
        from core.telemetry_aggregator import start_background_telemetry
        start_background_telemetry(series, stop.is_set)
    except Exception:
        pass
    import queue as _queue
    if psutil:
        try:
            psutil.cpu_percent(interval=None)
        except Exception:
            pass
    _freq_state_reset()
    src = _detect_power_source()
    _pwr_state: dict = {}
    last_powers: "list[float]" = []   # [socket0, socket1, …]

    _tstat_proc = None
    _tstat_q: "_queue.Queue[list]" = _queue.Queue()
    if src[0] == "turbostat":
        tstat_cmd = src[1] if len(src) > 1 else \
            ["turbostat", "--quiet", "--interval", "1", "--show", "PkgWatt"]
        tstat_env = os.environ.copy()
        tstat_env["LC_ALL"] = "C"
        try:
            _tstat_proc = subprocess.Popen(
                tstat_cmd,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                env=tstat_env, start_new_session=True,
            )
            def _tstat_reader():
                socket_watts: dict = {}
                for line in _tstat_proc.stdout:
                    line = line.strip()
                    if not line or not line[0].isdigit():
                        continue
                    parts = line.split()
                    if len(parts) != 2:
                        continue
                    try:
                        pkg   = int(parts[0])
                        watts = float(parts[1].replace(",", "."))
                    except ValueError:
                        continue
                    if pkg in socket_watts:
                        _tstat_q.put([socket_watts[k] for k in sorted(socket_watts)])
                        socket_watts = {}
                    socket_watts[pkg] = watts
                if socket_watts:
                    _tstat_q.put([socket_watts[k] for k in sorted(socket_watts)])
            threading.Thread(target=_tstat_reader, daemon=True).start()
        except Exception:
            _tstat_proc = None

    try:
        while not stop.is_set():
            t = round(time.time() - t0, 1)

            # Charges par socket
            pcts = _read_cpu_pcts_per_socket()
            pct  = pcts[0] if pcts else (psutil.cpu_percent(interval=None) if psutil else 0.0)

            # Capteurs de la carte (BMC, hwmon…) : UNE collecte par tour, partagée par la température, la puissance et les capteurs VRM / DIMM
            try:
                from core.telemetry_aggregator import collect_all_motherboard_telemetry
                tel_items = collect_all_motherboard_telemetry()
            except Exception:
                tel_items = []

            # Températures par socket
            temps = _read_cpu_temps_per_socket(tel_items)
            temp  = temps[0] if temps else 0.0

            # Fréquences par socket
            freqs = _read_cpu_freqs_per_socket()
            freq  = freqs[0] if freqs else _read_cpu_freq_mhz()

            # Puissances par socket (Priorité absolue à la BMC)
            bmc_powers = _read_bmc_cpu_powers_per_socket(tel_items)
            if bmc_powers and sum(bmc_powers) > 0:
                powers = bmc_powers
            elif _tstat_proc is not None:
                # Drainer jusqu'au dernier échantillon frais (évite la dérive en retard si IPMI/Redfish ralentit la boucle)
                while True:
                    try:
                        last_powers = _tstat_q.get_nowait()
                    except _queue.Empty:
                        break
                powers = last_powers
            else:
                powers = _read_cpu_powers_per_socket(_pwr_state, tel_items)

            power = powers[0] if powers else 0.0

            # RAM Temp & Power best-effort
            ram_temp, ram_power = _read_ram_temperature_power_best_effort()

            # Métriques de base
            series.setdefault("t", []).append(t)
            series.setdefault("temp", []).append(temp)
            series.setdefault("freq", []).append(freq)
            series.setdefault("cpu_pct", []).append(pct)
            series.setdefault("power", []).append(power)

            # Sockets supplémentaires (Socket 1, 2, 3…)
            for i in range(1, len(temps)):
                series.setdefault(f"temp_s{i}", []).append(temps[i])
            for i in range(1, len(freqs)):
                series.setdefault(f"freq_s{i}", []).append(freqs[i])
            for i in range(1, len(pcts)):
                series.setdefault(f"cpu_pct_s{i}", []).append(pcts[i])
            for i in range(1, len(powers)):
                series.setdefault(f"power_s{i}", []).append(powers[i])

            # Capteurs VRM / Carte Mère / Boîtier / RAM pendant les tests CPU
            try:
                for m in tel_items:
                    mname = m.get("name", "").lower()
                    mval = float(m.get("value", 0.0))
                    cat = m.get("category", "")
                    
                    if "vrm" in mname or "motherboard" in mname or "system" in mname or "inlet" in mname or "exhaust" in mname:
                        if cat == "temperature":
                            key_name = re.sub(r"[^a-z0-9_]", "", mname.replace(" ", "_").replace("-", "_"))
                            series.setdefault(f"temp_{key_name}", []).append(mval)
                    
                    if "dimm" in mname or "ddr" in mname:
                        clean_n = re.sub(r"^(temp|power|voltage)_*", "", mname)
                        clean_n = re.sub(r"[^a-z0-9_]", "", clean_n.replace(" ", "_").replace("-", "_"))
                        if cat == "temperature":
                            series.setdefault(f"temp_dimm_{clean_n}", []).append(mval)
                        elif cat == "power":
                            series.setdefault(f"power_dimm_{clean_n}", []).append(mval)
            except Exception:
                pass

            if "ram_used_gb" in series:
                try:
                    if psutil:
                        vm = psutil.virtual_memory()
                        series["ram_used_gb"].append(round((vm.total - vm.available) / 1073741824, 2))
                    else:
                        series["ram_used_gb"].append(0.0)
                except Exception:
                    series["ram_used_gb"].append(0.0)
            if "ram_temp" in series:
                series["ram_temp"].append(ram_temp)
            if "ram_power" in series:
                series["ram_power"].append(ram_power)

            if series_cb:
                pt = {"t": t, "temp": temp, "freq": freq, "cpu_pct": pct, "power": power}
                for i in range(1, len(temps)): pt[f"temp_s{i}"] = temps[i]
                for i in range(1, len(freqs)): pt[f"freq_s{i}"] = freqs[i]
                for i in range(1, len(pcts)): pt[f"cpu_pct_s{i}"] = pcts[i]
                for i in range(1, len(powers)): pt[f"power_s{i}"] = powers[i]
                pt["ram_temp"] = ram_temp
                pt["ram_power"] = ram_power
                series_cb(pt)
            stop.wait(1.0)
    finally:
        if _tstat_proc is not None:
            _terminate_proc_maybe_sudo(_tstat_proc)
            if _tstat_proc.stdout is not None:
                try:
                    _tstat_proc.stdout.close()
                except Exception:
                    pass
