"""
fio_runner.py — Runner FIO avec Popen pour support du stop réel
"""

import shlex
import subprocess
import json
import signal
import os
import re
import time
from typing import Optional, Callable
from core.fio_profiles import best_engine, cmd_to_params
from core.fault_monitor import with_fault_watch

ProgressCb = Optional[Callable[[int, str], None]]


def _run(cmd, timeout=600):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           shell=isinstance(cmd, str))
        return r.returncode, r.stdout, r.stderr
    except FileNotFoundError:
        return -1, "", f"Introuvable : {cmd[0] if isinstance(cmd, list) else cmd}"
    except subprocess.TimeoutExpired:
        return -2, "", "Timeout"
    except Exception as e:
        return -3, "", str(e)


def tool_exists(name):
    import shutil            # sans processus `which` (absent de certaines installations minimales)
    return shutil.which(name) is not None


def _fmt_duration(sec: float) -> str:
    s = int(sec)
    if s >= 3600:
        h = s // 3600
        m = (s % 3600) // 60
        sec_rem = s % 60
        return f"{h}h {m:02d}m {sec_rem:02d}s"
    elif s >= 60:
        m = s // 60
        sec_rem = s % 60
        return f"{m}m {sec_rem:02d}s"
    return f"{s}s"


def _stop_fio(proc, wait_s: float = 10.0) -> None:
    """Arrête fio et ses processus fils, y compris quand il tourne en root (lancé par `sudo -n` ou assistant privilégié).

    Un `terminate()` venant de l'utilisateur échoue en silence (EPERM) sur un fio root : le bouton Stopper et le délai maximal
    restaient sans effet pendant toute la durée du test (des heures pour un pré-conditionnement), puis `proc.wait()` bloquait.
    On tente donc : assistant privilégié → signal direct → `sudo -n kill` → `pkexec kill`, TERM puis KILL, et l'attente est bornée."""
    if proc is None or proc.poll() is not None:
        return
    if hasattr(proc, "stop_elevated"):
        proc.stop_elevated("TERM")
        try:
            proc.wait(timeout=3)
            return
        except Exception:
            pass
        proc.stop_elevated("KILL")
        try:
            proc.wait(timeout=wait_s)
        except Exception:
            pass            # processus bloqué dans le noyau (disque en erreur) : on rend la main quand même
        return

    pids = [proc.pid]
    try:
        import psutil
        pids += [c.pid for c in psutil.Process(proc.pid).children(recursive=True)]
    except Exception:
        pass

    def _signal(sig_name: str) -> None:
        import shutil
        direct_ok = True
        for pid in pids:
            try:
                os.kill(pid, getattr(signal, "SIG" + sig_name))
            except ProcessLookupError:
                pass
            except PermissionError:
                direct_ok = False
            except Exception:
                direct_ok = False
        if direct_ok:
            return
        args = ["kill", f"-{sig_name}"] + [str(x) for x in pids]
        for pre in (["sudo", "-n"], ["pkexec"]):
            if not shutil.which(pre[0]):
                continue
            try:
                if subprocess.run(pre + args, capture_output=True, timeout=30).returncode == 0:
                    return
            except Exception:
                pass

    _signal("TERM")
    try:
        proc.wait(timeout=3)
        return
    except Exception:
        pass
    _signal("KILL")
    try:
        proc.wait(timeout=wait_s)
    except Exception:
        pass            # processus bloqué dans le noyau (disque en erreur) : on rend la main quand même


def _spawn_fio_process(cmd: list, needs_root: bool):
    """Démarre `fio` en direct, via `sudo -n` ou via l'assistant privilégié persistant (1 seule auth par session).

    Retourne `(proc, f_out, f_err, immediate_err_tuple)`.
    """
    import shutil
    import tempfile

    if needs_root and os.geteuid() != 0:
        from core.privileged import has_sudo_nopasswd, start_elevated_job
        fio_bin = shutil.which(cmd[0]) or cmd[0]
        full_cmd = [fio_bin] + list(cmd[1:])
        if has_sudo_nopasswd():
            cmd = ["sudo", "-n"] + full_cmd
        else:
            try:
                proc = start_elevated_job(full_cmd)
                return proc, None, None, None
            except PermissionError as e:
                return None, None, None, (126, "", str(e))
            except FileNotFoundError as e:
                return None, None, None, (-1, "", str(e))
            except Exception as e:
                return None, None, None, (-3, "", str(e))

    f_out = f_err = None
    try:
        f_out = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        f_err = tempfile.TemporaryFile(mode="w+", encoding="utf-8")
        proc = subprocess.Popen(
            cmd,
            stdout=f_out,
            stderr=f_err,
            text=True,
        )
        return proc, f_out, f_err, None
    except FileNotFoundError:
        _close_temp_files(f_out, f_err)
        return None, None, None, (-1, "", f"Introuvable : {cmd[0]}")
    except Exception as e:
        _close_temp_files(f_out, f_err)
        return None, None, None, (-3, "", str(e))


def _close_temp_files(f_out, f_err) -> None:
    for fh in (f_out, f_err):
        if fh is not None:
            try:
                fh.close()
            except Exception:
                pass


def _run_fio_popen(
    cmd: list, timeout: int, stop_flag_fn,
    cb: ProgressCb = None, expected_runtime_s: int = 0,
    target_path: str = "/tmp", series: dict = None,
    on_series = None
) -> tuple[int, str, str]:
    """
    Lance fio via Popen (ou assistant privilégié persistant) pour pouvoir l'interrompre avec stop().
    Retourne (returncode, stdout, stderr).
    stop_flag_fn() doit renvoyer True quand on veut arrêter.
    """
    # Élévation automatique si on attaque un périphérique brut (/dev/...) sans être root
    is_raw_dev = any(arg.startswith("--filename=/dev/") or arg == "--filename=/dev/" for arg in cmd)
    proc, f_out, f_err, immediate_err = _spawn_fio_process(cmd, needs_root=is_raw_dev)
    if immediate_err is not None:
        return immediate_err

    from core.ssd_diagnostics import read_ssd_sensors
    t0 = time.time()
    deadline = t0 + timeout
    last_cb = 0.0
    last_sensor = 0.0

    while proc.poll() is None:
        now = time.time()
        if stop_flag_fn():
            _stop_fio(proc)
            _close_temp_files(f_out, f_err)
            return -99, "", "Arrete par l'utilisateur"
        if now > deadline:
            _stop_fio(proc)
            _close_temp_files(f_out, f_err)
            return -2, "", "Timeout"

        if series is not None and (now - last_sensor >= 1.0):
            last_sensor = now
            t_el = round(now - t0, 1)
            try:
                temp_c, pwr_w, per_ssd = read_ssd_sensors(target_path)
                series.setdefault("t", []).append(t_el)
                series.setdefault("disk_temp", []).append(temp_c)
                series.setdefault("disk_power", []).append(pwr_w)
                for i_ssd, (t, pw) in enumerate(per_ssd):
                    series.setdefault(f"ssd{i_ssd}_temp", []).append(t)
                    series.setdefault(f"ssd{i_ssd}_power", []).append(pw)

                if on_series:
                    on_series({"t": t_el, "temp": temp_c, "power": pwr_w})
            except Exception:
                pass

        if cb and (now - last_cb >= 0.5):
            last_cb = now
            elapsed = now - t0
            tel = f" | 🌡️ {series['disk_temp'][-1]}°C" if series and series.get("disk_temp") and series["disk_temp"][-1] > 0 else ""
            pwr = f" ⚡ {series['disk_power'][-1]}W" if series and series.get("disk_power") and series["disk_power"][-1] > 0 else ""
            if expected_runtime_s > 0:
                pct = int(15 + min(75, (elapsed / expected_runtime_s) * 75))
                rem = max(0, int(expected_runtime_s - elapsed))
                el_str = _fmt_duration(elapsed)
                tot_str = _fmt_duration(expected_runtime_s)
                rem_str = _fmt_duration(rem)
                cb(pct, f"Exécution fio en cours… ({el_str} / {tot_str} — reste ~{rem_str}){tel}{pwr}")
            else:
                pct = int(15 + min(75, (elapsed / 1800) * 75))
                el_str = _fmt_duration(elapsed)
                cb(pct, f"Exécution fio en cours — {el_str} écoulées…{tel}{pwr}")

        time.sleep(0.2)

    proc.wait()
    if hasattr(proc, "get_output"):
        stdout, stderr = proc.get_output()
    else:
        f_out.seek(0); stdout = f_out.read()
        f_err.seek(0); stderr = f_err.read()
        _close_temp_files(f_out, f_err)

    return proc.returncode, stdout, stderr


@with_fault_watch
def run_fio_from_params(
    path: str = "/tmp",
    rw_mode: str = "read",
    bs: str = "1m",
    size_mb: int = 2048,
    runtime_s: int = 30,
    iodepth: int = 8,
    numjobs: int = 1,
    direct: bool = True,
    io_engine: str = "auto",
    rwmixread: int = 50,
    raw_device: bool = False,
    loops: int = 1,
    bssplit: str = "",          # mélange de tailles de bloc, ex. « 4k/67:8k/33 » (profils JEDEC / IOMeter)
    random_distribution: str = "",   # ex. « zoned:50/5:30/15:20/80 » (zones chaudes JESD219)
    blockalign: str = "",       # alignement des blocs aléatoires, ex. « 4k »
    size_expr: str = "",        # taille en % ou valeur fio (ex. « 5% »), prioritaire sur size_mb
    ramp_time_s: int = 3,       # montée en charge ignorée (s)
    fio_extra: str = "",        # options fio supplémentaires (ex. « --fdatasync=1 --percentage_random=80 »)
    cb: ProgressCb = None,
    stop_flag_fn=None,          # callable → bool, pour stopper fio
    on_series=None,             # callable → dict, pour streamer capteurs
    keep_job_details: bool = False,   # True : une suite (run_fio_suite) a besoin de `jobs_details` même pour un seul disque
) -> dict:
    if not tool_exists("fio"):
        return {"error": "fio non installe", "status": "skipped"}

    engine = best_engine() if io_engine == "auto" else io_engine
    if cb: cb(3, f"Moteur I/O : {engine}")

    paths = [p.strip() for p in path.split(",")] if "," in path else [path]
    test_files = []

    # Protection du disque système / des disques montés (écriture brute interdite)
    from core.safety import check_targets, is_write_mode
    lvl, msg = check_targets(paths, raw_device=raw_device, writes=is_write_mode(rw_mode))
    if lvl == "block":
        return {"error": msg, "status": "error", "raw": msg}
    if lvl == "warn" and cb:
        cb(2, "⚠ " + msg.splitlines()[0])
    
    # Chemin du fichier test pour chaque disque
    for p in paths:
        if raw_device or p.startswith("/dev/"):
            test_files.append(p)
            direct    = True
        else:
            if not os.path.isdir(p):
                return {
                    "error": f"Repertoire introuvable : {p}\n"
                              "Verifiez que le disque est monte.",
                    "status": "error", "raw": "",
                }
            test_files.append(os.path.join(p, "_benchsuite_fio.tmp"))

    if cb: cb(8, f"fio {rw_mode} bs={bs} depth={iodepth} jobs={numjobs} engine={engine} sur {len(test_files)} disque(s)…")

    cmd = [
        "fio",
        f"--rw={rw_mode}",
        f"--bs={bs}",
        f"--numjobs={numjobs}",
        f"--iodepth={iodepth}",
        f"--ioengine={engine}",
        "--randrepeat=0",
        "--invalidate=1",
        "--cpus_allowed_policy=split",
        "--output-format=json",
    ]
    if size_expr:
        cmd.append(f"--size={size_expr}")
    elif size_mb > 0:
        cmd.append(f"--size={size_mb}M")
    elif raw_device or any(t.startswith("/dev/") for t in test_files):
        cmd.append("--size=100%")
    else:
        # Fichier sur un système de fichiers : « 100 % » n'a pas de sens → moitié de l'espace libre (max 64 Go)
        try:
            import shutil as _sh
            free = _sh.disk_usage(paths[0]).free
            cmd.append(f"--size={max(256, min(65536, int(free * 0.5 / 1048576)))}M")
        except Exception:
            cmd.append("--size=4096M")
    if bssplit:
        cmd.append(f"--bssplit={bssplit}")
    if random_distribution:
        cmd.append(f"--random_distribution={random_distribution}")
    if blockalign:
        cmd.append(f"--blockalign={blockalign}")
    if fio_extra:
        try:
            cmd += shlex.split(str(fio_extra))
        except ValueError:
            cmd += str(fio_extra).split()

    if runtime_s > 0:
        cmd.append(f"--runtime={runtime_s}")
        if ramp_time_s and int(ramp_time_s) > 0:
            cmd.append(f"--ramp_time={int(ramp_time_s)}")
        cmd.append("--time_based")
        timeout_s = runtime_s + int(ramp_time_s or 0) + 120
        if cb: cb(15, f"Execution fio ({runtime_s}s)…")
    else:
        timeout_s = 86400 * 3  # 3 jours max pour un test intégral sur très gros volume
        if cb: cb(15, f"Execution fio pré-conditionnement sur capacité complète (loops={loops})…")

    if loops > 1:
        cmd.append(f"--loops={loops}")

    if direct:
        cmd.append("--direct=1")
    if "rw" in rw_mode and rwmixread != 50:
        cmd.append(f"--rwmixread={rwmixread}")
        
    for i, tf in enumerate(test_files):
        cmd.append(f"--name=job{i}")
        cmd.append(f"--filename={tf}")

    # Second contrôle sur la commande FINALE : `fio_extra` (profil ou séquence JSON) peut contenir --rw=write, --verify…
    # qui transforment un profil de lecture en écriture ; le premier contrôle ne voyait que rw_mode.
    _final = _parse_fio_cli(shlex.join(cmd))
    if _final["writes"] and not is_write_mode(rw_mode):
        lvl2, msg2 = check_targets(paths, raw_device=raw_device, writes=True)
        if lvl2 == "block":
            return {"error": msg2, "status": "error", "raw": msg2}

    if (raw_device or any(t.startswith("/dev/") for t in test_files)) and os.geteuid() != 0:
        from core.privileged import ensure_elevated_session
        try:
            auth_ok, auth_err = ensure_elevated_session()
        except FileNotFoundError as e:
            return {"error": str(e), "status": "error", "raw": str(e)}
        if not auth_ok:
            return {"error": auth_err, "status": "error", "raw": auth_err}

    _stop = stop_flag_fn or (lambda: False)
    series = {"t": [], "disk_temp": [], "disk_power": []}
    _h0 = _nvme_snapshot(path)          # état NVMe avant (liens PCIe, erreurs, chauffe) pour le bilan stabilité / chauffe
    rc, out, err = _run_fio_popen(cmd, timeout_s, _stop, cb, expected_runtime_s=runtime_s, target_path=path, series=series, on_series=on_series)

    # Cleanup fichier temporaire
    for p, tf in zip(paths, test_files):
        if not (raw_device or p.startswith("/dev/")):
            try: os.remove(tf)
            except Exception: pass

    # Arret volontaire
    if rc == -99:
        return {"error": "Arrete", "status": "stopped", "raw": ""}

    if rc != 0:
        # Fallback sync si engine indisponible
        if engine != "sync" and ("invalid" in err.lower() or "not available" in err.lower()):
            if cb: cb(15, f"Fallback sync…")
            cmd_fb = [c.replace(f"--ioengine={engine}", "--ioengine=sync") for c in cmd]
            cmd = cmd_fb                       # c'est cette commande-là qui a réellement tourné (enregistrée dans le résultat)
            rc, out, err = _run_fio_popen(cmd_fb, timeout_s, _stop, cb, expected_runtime_s=runtime_s, target_path=path, series=series, on_series=on_series)
            engine = "sync"
            for p, tf in zip(paths, test_files):
                if not (raw_device or p.startswith("/dev/")):
                    try: os.remove(tf)
                    except: pass
        if rc == -99:
            return {"error": "Arrete", "status": "stopped", "raw": ""}
        if rc != 0:
            err_msg = err or out or "fio a echoue"
            if "failed opening blockdev" in err_msg or "Permission denied" in err_msg or "Device or resource busy" in err_msg or "filesetup" in err_msg:
                err_msg = (
                    f"Impossible d'exécuter fio sur le périphérique : {err_msg.strip()}\n\n"
                    "💡 Causes possibles et solutions :\n"
                    "1. Périphérique en cours d'utilisation : Si des partitions de ce disque NVMe sont montées ou utilisées par le système (OS, swap, RAID, LVM), Linux interdit l'accès brut en écriture pour éviter toute corruption de données. Sélectionnez un répertoire de montage au lieu du périphérique brut, ou démontez les partitions.\n"
                    "2. Privilèges insuffisants : Les tests sur périphérique brut (/dev/...) nécessitent les privilèges root (sudo/pkexec)."
                )
            return {"error": err_msg, "status": "error", "raw": out + "\n" + err}

    result = _parse_fio_output(out, rw_mode, bs, iodepth, numjobs, engine)
    result["engine_used"] = engine
    if isinstance(result.get("metrics"), dict):
        result["metrics"]["command"] = shlex.join(str(c) for c in cmd)      # la VRAIE commande (le rapport ne la reconstruit plus)
    _nvme_bilan(result, _h0, path)
    if series and series.get("t"):
        result["series"] = series
        temps = [v for v in series.get("disk_temp", []) if v > 0]
        pwrs = [v for v in series.get("disk_power", []) if v > 0]
        if temps:
            result.setdefault("metrics", {})["disk_temp_max_c"] = max(temps)
            result["metrics"]["disk_temp_moy_c"] = round(sum(temps) / len(temps), 1)
        if pwrs:
            result.setdefault("metrics", {})["disk_power_max_w"] = max(pwrs)
            result["metrics"]["disk_power_moy_w"] = round(sum(pwrs) / len(pwrs), 2)
        result.setdefault("metrics", {})["series"] = series
        result["metrics"]["series_t"] = series.get("t", [])
        result["metrics"]["series_temp"] = series.get("disk_temp", [])
        result["metrics"]["series_power"] = series.get("disk_power", [])
        
        # Inject individual series into jobs_details for multi-disk charts
        if "jobs_details" in result["metrics"]:
            for i_disk, jm in enumerate(result["metrics"]["jobs_details"]):
                if f"ssd{i_disk}_temp" in series:
                    jm["series_t"] = series.get("t", [])
                    jm["series_temp"] = series[f"ssd{i_disk}_temp"]
                    jm["series_power"] = series[f"ssd{i_disk}_power"]
                    temps_indiv = [v for v in jm["series_temp"] if v > 0]
                    if temps_indiv:
                        jm["disk_temp_max_c"] = max(temps_indiv)
                        jm["disk_temp_moy_c"] = round(sum(temps_indiv) / len(temps_indiv), 1)
                    pwrs_indiv = [v for v in jm["series_power"] if v > 0]
                    if pwrs_indiv:
                        jm["disk_power_max_w"] = max(pwrs_indiv)
                        jm["disk_power_moy_w"] = round(sum(pwrs_indiv) / len(pwrs_indiv), 2)

    if not keep_job_details:
        _drop_single_job_details(result)
    return result


def _drop_single_job_details(result: dict) -> None:
    """Un seul disque / job : `jobs_details` répétait les mêmes chiffres que les métriques de tête (avec des arrondis légèrement différents) et
    recopiait les séries de température / puissance. Il n'a de sens que pour comparer PLUSIEURS disques : il est alors conservé."""
    m = result.get("metrics") if isinstance(result, dict) else None
    if isinstance(m, dict) and isinstance(m.get("jobs_details"), list) and len(m["jobs_details"]) == 1:
        m.pop("jobs_details", None)


def _side_label(name: str, side: str) -> str:
    """« Seq1M Q8T1 Lecture » + « Lecture » → « Seq1M Q8T1 Lecture » (le mot n'est pas répété) ; sinon « <nom> <côté> »."""
    return name if name.rstrip().lower().endswith(side.lower()) else f"{name} {side}"


_FIO_VALUE_OPTS = {"filename", "rw", "readwrite", "verify", "directory", "output-format", "output"}
_FIO_EXTRA_VALUE_OPTS = {
    "name", "bs", "blocksize", "bssplit", "blockalign", "size", "filesize", "offset",
    "runtime", "ramp_time", "numjobs", "iodepth", "ioengine", "direct", "buffered",
    "rwmixread", "rwmixwrite", "loops", "invalidate", "randrepeat", "random_distribution",
    "cpus_allowed_policy", "cpus_allowed", "fdatasync", "fsync", "percentage_random",
    "rate", "rate_iops", "nrfiles", "openfiles", "thinktime", "verify_fatal", "do_verify",
    "eta", "eta-newline", "status-interval", "io_size", "percentile_list",
}


def _parse_fio_cli(raw_cmd: str) -> dict:
    """Analyse une commande fio libre pour le contrôle de sécurité, sans shell.

    Reconnaît `--opt=val` ET `--opt val` (fio accepte les deux), et lit les fichiers de job passés en argument
    (`filename=`, `directory=`, `rw=` / `readwrite=`, `verify` / `trim`). Retourne {argv, targets, writes, error}.
    En cas de doute (fichier de job illisible, commande qui n'est pas fio), `error` est rempli : la commande est refusée."""
    import shlex
    try:
        argv = shlex.split(raw_cmd)
    except ValueError as e:
        return {"argv": [], "targets": [], "writes": True, "error": f"Commande illisible : {e}"}
    while argv and argv[0] in ("sudo", "pkexec", "-n"):     # « sudo fio … » : l'élévation est ajoutée automatiquement
        argv = argv[1:]
    if not argv or os.path.basename(argv[0]) != "fio":
        return {"argv": argv, "targets": [], "writes": True,
                "error": "La commande personnalisée doit commencer par « fio » (aucun shell : pas de ; | && ni redirection)."}
    targets, rw_vals, writes_flag, jobfiles = [], [], False, []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok.startswith("--"):
            key, eq, val = tok[2:].partition("=")
            if not eq and key in (_FIO_VALUE_OPTS | _FIO_EXTRA_VALUE_OPTS) and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                val = argv[i + 1]
                i += 1
            if key in ("filename", "directory"):
                targets += [t for t in val.split(":") if t]
            elif key in ("rw", "readwrite"):
                rw_vals.append(val)
            elif key.startswith("verify") or key.startswith("trim"):
                writes_flag = True
        elif not tok.startswith("-"):
            jobfiles.append(tok)
        i += 1
    for jf in jobfiles:
        try:
            with open(jf, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    k, eq, v = line.strip().partition("=")
                    k, v = k.strip().lower(), v.strip()
                    if k in ("filename", "directory") and eq:
                        targets += [t for t in v.split(":") if t]
                    elif k in ("rw", "readwrite") and eq:
                        rw_vals.append(v)
                    elif k.startswith("verify") or k.startswith("trim"):
                        writes_flag = True
        except OSError as e:
            return {"argv": argv, "targets": targets, "writes": True,
                    "error": f"Fichier de job illisible ({jf}) : {e} — impossible de vérifier la cible, commande refusée."}
    from core.safety import is_write_mode
    writes = writes_flag or any(is_write_mode(v) for v in rw_vals)
    return {"argv": argv, "targets": targets, "writes": writes, "error": ""}


@with_fault_watch
def run_fio_from_cmd(raw_cmd: str, cb: ProgressCb = None,
                     stop_flag_fn=None, on_series=None) -> dict:
    if not tool_exists("fio"):
        return {"error": "fio non installe", "status": "skipped"}

    if "ioengine=auto" in raw_cmd:
        engine = best_engine()
        raw_cmd = raw_cmd.replace("ioengine=auto", f"ioengine={engine}")
        if cb: cb(3, f"Moteur I/O : {engine}")

    if "--output-format" not in raw_cmd:
        raw_cmd = raw_cmd.rstrip() + " --output-format=json"

    if cb: cb(10, "Execution commande personnalisee…")

    m = re.search(r"--runtime=(\d+)", raw_cmd)
    timeout = int(m.group(1)) + 120 if m else 600

    _stop = stop_flag_fn or (lambda: False)
    from core.ssd_diagnostics import read_ssd_sensors
    series = {"t": [], "disk_temp": [], "disk_power": []}
    # Protection du disque système : toutes les formes de --filename / --directory / --rw / --readwrite, fichiers de job compris
    parsed = _parse_fio_cli(raw_cmd)
    if parsed["error"]:
        return {"error": parsed["error"], "status": "error", "raw": parsed["error"]}
    targets = parsed["targets"]
    from core.safety import check_targets
    lvl, msg = check_targets(targets, raw_device=False, writes=parsed["writes"])
    if lvl == "block":
        return {"error": msg, "status": "error", "raw": msg}
    target_p = targets[0] if targets else "/tmp"
    is_raw_cmd = any(t.startswith("/dev/") for t in targets)
    if is_raw_cmd and os.geteuid() != 0:
        from core.privileged import ensure_elevated_session
        try:
            auth_ok, auth_err = ensure_elevated_session()
        except FileNotFoundError as e:
            return {"error": str(e), "status": "error", "raw": str(e)}
        if not auth_ok:
            return {"error": auth_err, "status": "error", "raw": auth_err}

    _h0_cmd = _nvme_snapshot(",".join(targets))
    # Fichiers qui existaient AVANT le test : jamais supprimés au nettoyage (seuls ceux créés par fio le sont)
    preexisting = {t for t in targets if not t.startswith("/dev/") and os.path.exists(t)}
    argv = list(parsed["argv"])

    def _cleanup_created_files():
        for fp in targets:
            if fp.startswith("/dev/") or fp in preexisting:
                continue
            try:
                if os.path.isfile(fp):
                    os.remove(fp)
            except OSError:
                pass

    proc, f_out, f_err, immediate_err = _spawn_fio_process(argv, needs_root=is_raw_cmd)
    if immediate_err is not None:
        _, _, err_msg = immediate_err
        return {"error": err_msg, "status": "error", "raw": err_msg}

    t0 = time.time()
    deadline = t0 + timeout
    last_sensor = 0.0
    try:
        while proc.poll() is None:
            now = time.time()
            if _stop():
                _stop_fio(proc)
                _close_temp_files(f_out, f_err)
                return {"error": "Arrete", "status": "stopped", "raw": ""}
            if now > deadline:
                _stop_fio(proc)
                _close_temp_files(f_out, f_err)
                return {"error": "Timeout", "status": "error", "raw": ""}
            if now - last_sensor >= 1.0:
                last_sensor = now
                t_el = round(now - t0, 1)
                try:
                    temp_c, pwr_w, _ = read_ssd_sensors(target_p)
                    series["t"].append(t_el)
                    series["disk_temp"].append(temp_c)
                    series["disk_power"].append(pwr_w)
                    if on_series:
                        on_series({"t": t_el, "temp": temp_c, "power": pwr_w})
                except Exception:
                    pass
            time.sleep(0.2)

        proc.wait()
        if hasattr(proc, "get_output"):
            out, err = proc.get_output()
        else:
            f_out.seek(0); out = f_out.read()
            f_err.seek(0); err = f_err.read()
            _close_temp_files(f_out, f_err)
        rc = proc.returncode
    finally:
        # Nettoyage garanti (y compris après arrêt volontaire ou timeout)
        _cleanup_created_files()

    if rc != 0:
        err_msg = err or out or "fio a echoue"
        if "failed opening blockdev" in err_msg or "Permission denied" in err_msg or "Device or resource busy" in err_msg or "filesetup" in err_msg:
            err_msg = (
                f"Impossible d'exécuter fio sur le périphérique : {err_msg.strip()}\n\n"
                "💡 Causes possibles et solutions :\n"
                "1. Périphérique en cours d'utilisation : Si des partitions de ce disque NVMe sont montées ou utilisées par le système (OS, swap, RAID, LVM), Linux interdit l'accès brut en écriture pour éviter toute corruption de données. Sélectionnez un répertoire de montage au lieu du périphérique brut, ou démontez les partitions.\n"
                "2. Privilèges insuffisants : Les tests sur périphérique brut (/dev/...) nécessitent les privilèges root (sudo/pkexec)."
            )
        return {"error": err_msg, "status": "error", "raw": out + err}

    params = cmd_to_params(raw_cmd)
    result = _parse_fio_output(
        out,
        params.get("rw_mode", "?"),
        params.get("bs", "?"),
        params.get("iodepth", 1),
        params.get("numjobs", 1),
        params.get("io_engine", "?"),
    )
    if series and series.get("t"):
        result["series"] = series
        temps = [v for v in series.get("disk_temp", []) if v > 0]
        pwrs = [v for v in series.get("disk_power", []) if v > 0]
        if temps:
            result.setdefault("metrics", {})["disk_temp_max_c"] = max(temps)
            result["metrics"]["disk_temp_moy_c"] = round(sum(temps) / len(temps), 1)
        if pwrs:
            result.setdefault("metrics", {})["disk_power_max_w"] = max(pwrs)
            result["metrics"]["disk_power_moy_w"] = round(sum(pwrs) / len(pwrs), 2)
        result.setdefault("metrics", {})["series"] = series
        result["metrics"]["series_t"] = series.get("t", [])
        result["metrics"]["series_temp"] = series.get("disk_temp", [])
        result["metrics"]["series_power"] = series.get("disk_power", [])
    _nvme_bilan(result, _h0_cmd, ",".join(targets))
    if isinstance(result.get("metrics"), dict):
        result["metrics"]["command"] = raw_cmd.strip()
    _drop_single_job_details(result)
    return result


def _nvme_snapshot(path: str) -> list:
    """État NVMe avant un test (liste vide si le chemin n'est pas un NVMe) ; jamais d'exception."""
    try:
        from core import nvme_health
        return nvme_health.snapshot_all(path)
    except Exception:
        return []


def _nvme_bilan(result: dict, before: list, path: str) -> None:
    """Ajoute `metrics.nvme_sante` (erreurs PCIe / média, throttling) en comparant l'état NVMe avant/après le test."""
    try:
        from core import nvme_health
        if not before:
            return
        bilan = nvme_health.diff_all(before, nvme_health.snapshot_all(path))
        if bilan:
            result.setdefault("metrics", {})["nvme_sante"] = bilan
    except Exception:
        pass


def _parse_fio_output(out: str, rw_mode: str, bs: str,
                      iodepth, numjobs, io_engine: str) -> dict:
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\})", out, re.DOTALL)
        if m:
            try: data = json.loads(m.group(1))
            except: return {"error": "Impossible de parser fio", "status": "error", "raw": out}
        else:
            return {"error": "Sortie fio non reconnue", "status": "error", "raw": out}

    try:
        jobs = data.get("jobs", [])
        if not jobs:
            return {"error": "Aucun job dans la sortie fio", "status": "error", "raw": out}

        total_r_bw = total_w_bw = 0.0
        total_r_iops = total_w_iops = 0.0
        r_lat_mean = w_lat_mean = 0.0
        r_lat_p99 = w_lat_p99 = r_lat_p999 = w_lat_p999 = 0.0

        for job in jobs:
            r_bytes = job["read"].get("bw_bytes", job["read"].get("bw", 0) * 1024)
            w_bytes = job["write"].get("bw_bytes", job["write"].get("bw", 0) * 1024)
            total_r_bw   += r_bytes / 1000000.0
            total_w_bw   += w_bytes / 1000000.0
            total_r_iops += job["read"]["iops"]
            total_w_iops += job["write"]["iops"]
            r_lat_mean   += job["read"]["lat_ns"]["mean"]
            w_lat_mean   += job["write"]["lat_ns"]["mean"]
            r_perc = job["read"].get("clat_ns", {}).get("percentile", {})
            w_perc = job["write"].get("clat_ns", {}).get("percentile", {})
            r_lat_p99  += float(r_perc.get("99.000000", 0))
            w_lat_p99  += float(w_perc.get("99.000000", 0))
            r_lat_p999 += float(r_perc.get("99.900000", 0))
            w_lat_p999 += float(w_perc.get("99.900000", 0))

        n = len(jobs)
        r_lat_mean /= n; w_lat_mean /= n
        r_lat_p99  /= n; w_lat_p99  /= n
        r_lat_p999 /= n; w_lat_p999 /= n

        if rw_mode in ("read", "randread"):     score = total_r_bw
        elif rw_mode in ("write", "randwrite"): score = total_w_bw
        else:                                   score = (total_r_bw + total_w_bw) / 2

        metrics = {
            "moteur_io":    io_engine,
            "mode":         rw_mode,
            "taille_bloc":  bs,
            "iodepth":      iodepth,
            "jobs":         numjobs,
        }
        
        jobs_by_name = {}
        for job in jobs:
            j_r_bytes = job["read"].get("bw_bytes", job["read"].get("bw", 0) * 1024)
            j_w_bytes = job["write"].get("bw_bytes", job["write"].get("bw", 0) * 1024)
            j_r_bw = j_r_bytes / 1000000.0
            j_w_bw = j_w_bytes / 1000000.0
            
            name = job.get("jobname", "job")
            if name not in jobs_by_name:
                jobs_by_name[name] = {
                    "name": name,
                    "file": job.get("job_options", {}).get("filename", ""),
                    "lecture_MiB_s": 0.0,
                    "ecriture_MiB_s": 0.0,
                    "lecture_IOPS": 0.0,
                    "ecriture_IOPS": 0.0,
                    "lat_lect_moy_ms_sum": 0.0,
                    "lat_ecr_moy_ms_sum": 0.0,
                    "count": 0
                }
            
            ref = jobs_by_name[name]
            ref["lecture_MiB_s"] += (round(j_r_bw, 1) if j_r_bw > 0 else 0)
            ref["ecriture_MiB_s"] += (round(j_w_bw, 1) if j_w_bw > 0 else 0)
            ref["lecture_IOPS"] += round(job["read"]["iops"], 0)
            ref["ecriture_IOPS"] += round(job["write"]["iops"], 0)
            ref["lat_lect_moy_ms_sum"] += round(job["read"]["lat_ns"]["mean"] / 1e6, 3)
            ref["lat_ecr_moy_ms_sum"] += round(job["write"]["lat_ns"]["mean"] / 1e6, 3)
            ref["count"] += 1
            
        jobs_metrics = []
        for name, ref in jobs_by_name.items():
            cnt = ref["count"]
            jm = {
                "name": name,
                "file": ref["file"],
                "lecture_MiB_s": round(ref["lecture_MiB_s"], 1),
                "ecriture_MiB_s": round(ref["ecriture_MiB_s"], 1),
                "lecture_IOPS": round(ref["lecture_IOPS"], 0),
                "ecriture_IOPS": round(ref["ecriture_IOPS"], 0),
                "lat_lect_moy_ms": round(ref["lat_lect_moy_ms_sum"] / cnt, 3) if cnt > 0 else 0,
                "lat_ecr_moy_ms": round(ref["lat_ecr_moy_ms_sum"] / cnt, 3) if cnt > 0 else 0
            }
            jobs_metrics.append(jm)
            
        metrics["jobs_details"] = jobs_metrics
        if total_r_bw > 0:
            metrics["lecture_MiB_s"]   = round(total_r_bw, 1)
            metrics["lecture_IOPS"]    = round(total_r_iops, 0)
            metrics["lat_lect_moy_ms"] = round(r_lat_mean / 1e6, 3)
            if r_lat_p99:  metrics["lat_lect_p99_ms"]  = round(r_lat_p99  / 1e6, 3)
            if r_lat_p999: metrics["lat_lect_p999_ms"] = round(r_lat_p999 / 1e6, 3)
        # Latence fdatasync/fsync (profil « journal / WAL », test etcd) : section "sync" du JSON fio
        s_mean, s_p99, s_n = 0.0, 0.0, 0
        for job in jobs:
            sy = job.get("sync") or {}
            if sy.get("total_ios", 0) > 0 and isinstance(sy.get("lat_ns"), dict):
                s_n += 1
                s_mean += float(sy["lat_ns"].get("mean", 0))
                s_p99 += float((sy["lat_ns"].get("percentile") or {}).get("99.000000", 0))
        if s_n:
            metrics["sync_lat_moy_ms"] = round(s_mean / s_n / 1e6, 3)
            if s_p99:
                metrics["sync_lat_p99_ms"] = round(s_p99 / s_n / 1e6, 3)
        if total_w_bw > 0:
            metrics["ecriture_MiB_s"]  = round(total_w_bw, 1)
            metrics["ecriture_IOPS"]   = round(total_w_iops, 0)
            metrics["lat_ecr_moy_ms"]  = round(w_lat_mean / 1e6, 3)
            if w_lat_p99:  metrics["lat_ecr_p99_ms"]   = round(w_lat_p99  / 1e6, 3)
            if w_lat_p999: metrics["lat_ecr_p999_ms"]  = round(w_lat_p999 / 1e6, 3)

        # Latence maximale et percentiles extrêmes (profils de latence OCP / SNIA : jamais une E/S > 8 s, 5 « neuf »)
        for side, lab in (("read", "lect"), ("write", "ecr")):
            mx, p9999, p99999 = 0.0, 0.0, 0.0
            for job in jobs:
                d = job.get(side, {})
                mx = max(mx, float((d.get("lat_ns") or {}).get("max", 0) or 0))
                perc = (d.get("clat_ns") or {}).get("percentile") or {}
                p9999 = max(p9999, float(perc.get("99.990000", 0) or 0))
                p99999 = max(p99999, float(perc.get("99.999000", 0) or 0))
            if mx:
                metrics[f"lat_{lab}_max_ms"] = round(mx / 1e6, 3)
            if p9999:
                metrics[f"lat_{lab}_p9999_ms"] = round(p9999 / 1e6, 3)
            if p99999:
                metrics[f"lat_{lab}_p99999_ms"] = round(p99999 / 1e6, 3)

        return {"score": round(score, 1), "unit": "MiB/s",
                "metrics": metrics, "status": "ok", "raw": out}

    except Exception as e:
        return {"error": str(e), "status": "error", "raw": out}


@with_fault_watch
def run_fio_suite(
    suite_test_ids: list,
    path: str = "/tmp",
    cb: ProgressCb = None,
    stop_flag_fn = None,
    raw_device: bool = False,
    on_series = None,
    suite_name: str = "",
) -> dict:
    from core.fio_profiles import get_profile
    n = len(suite_test_ids)
    if n == 0:
        return {"error": "Aucun test dans la suite", "status": "error", "raw": ""}
    
    suite_results = []
    combined_raw = ""
    agg_metrics = {
        "moteur_io": "io_uring / auto",
        "mode": suite_name or "Suite Peak Complète",
        "taille_bloc": "Multi-Blocs",
        "iodepth": "Multi",
        "jobs": "Multi",
    }
    
    max_read_bw = 0.0
    max_write_bw = 0.0
    suite_cmds: list = []
    
    for i, test_id in enumerate(suite_test_ids):
        if stop_flag_fn and stop_flag_fn():
            return {"error": "Arrêté", "status": "stopped", "raw": combined_raw}
        
        prof = get_profile(test_id)
        if not prof: continue
        
        name = prof.get("name", test_id)
        pct_base = int((i / n) * 90)
        if cb: cb(pct_base, f"Étape {i+1}/{n} : {name}…")
        
        params = prof.get("params", {})
        params["path"] = path
        params["raw_device"] = raw_device
        
        def sub_cb(sub_pct, sub_msg):
            total_pct = pct_base + int((sub_pct / 100) * (90 / n))
            if cb: cb(total_pct, f"[{i+1}/{n}] {name} — {sub_msg}")
            
        r = run_fio_from_params(cb=sub_cb, stop_flag_fn=stop_flag_fn, on_series=on_series, keep_job_details=True, **params)
        if r.get("status") == "stopped":
            return {"error": "Arrêté", "status": "stopped", "raw": combined_raw}
        if r.get("status") == "error":
            return r
            
        suite_results.append({"name": name, "result": r})
        combined_raw += f"=== {name} ===\n" + str(r.get("raw", "")) + "\n\n"
        
        # Agréger dans agg_metrics
        m = r.get("metrics", {})
        if m.get("command"):
            suite_cmds.append(f"# {name}\n{m['command']}")
        if "moteur_io" in m and m["moteur_io"] != "?":
            agg_metrics["moteur_io"] = m["moteur_io"]
        if "lecture_MiB_s" in m:
            agg_metrics[f"{_side_label(name, 'Lecture')} (MiB/s)"] = m["lecture_MiB_s"]
            max_read_bw = max(max_read_bw, m["lecture_MiB_s"])
        if "ecriture_MiB_s" in m:
            agg_metrics[f"{_side_label(name, 'Écriture')} (MiB/s)"] = m["ecriture_MiB_s"]
            max_write_bw = max(max_write_bw, m["ecriture_MiB_s"])
        if "lecture_IOPS" in m:
            agg_metrics[f"{_side_label(name, 'Lecture')} (IOPS)"] = m["lecture_IOPS"]
        if "ecriture_IOPS" in m:
            agg_metrics[f"{_side_label(name, 'Écriture')} (IOPS)"] = m["ecriture_IOPS"]
        # Vraies latences de chaque sous-test (unité « (ms) » dans la clé : l'export les range dans l'onglet Latence, jamais avec les débits)
        for lab, side in (("Lecture", "lect"), ("Écriture", "ecr")):
            for suffix, txt in (("moy", "moyenne"), ("p99", "p99"), ("p999", "p99.9"), ("max", "max")):
                v = m.get(f"lat_{side}_{suffix}_ms")
                if isinstance(v, (int, float)) and v > 0:
                    agg_metrics[f"{_side_label(name, lab)} latence {txt} (ms)"] = v

    if suite_cmds:
        agg_metrics["command"] = "\n".join(suite_cmds)       # commandes réelles, une par sous-test
    # Marqueurs sans information (« Multi ») : retirés, le nom du profil et les lignes par test suffisent
    for k in ("taille_bloc", "iodepth", "jobs"):
        if agg_metrics.get(k) in ("Multi", "Multi-Blocs"):
            agg_metrics.pop(k, None)

    if cb: cb(98, "Traitement des résultats de la suite…")
    
    comb_series = {"t": [], "disk_temp": [], "disk_power": []}
    cur_t_offset = 0.0
    
    num_jobs = 0
    if suite_results:
        m0 = suite_results[0].get("result", {}).get("metrics", {})
        num_jobs = len(m0.get("jobs_details", []))
        
    jobs_agg = [{"name": f"job{i}", "lecture_MiB_s": 0, "ecriture_MiB_s": 0, "series_t": [], "series_temp": [], "series_power": []} for i in range(num_jobs)]
    
    for sub in suite_results:
        sub_res = sub.get("result", {})
        sub_ser = sub_res.get("series") or sub_res.get("metrics", {}).get("series")
        if sub_ser and sub_ser.get("t"):
            st = sub_ser.get("t", [])
            s_temp = sub_ser.get("disk_temp", [])
            s_power = sub_ser.get("disk_power", [])
            for idx_t, val_t in enumerate(st):
                comb_series["t"].append(round(cur_t_offset + val_t, 1))
                comb_series["disk_temp"].append(s_temp[idx_t] if idx_t < len(s_temp) else 0)
                comb_series["disk_power"].append(s_power[idx_t] if idx_t < len(s_power) else 0)
                
            for i_disk in range(num_jobs):
                if f"ssd{i_disk}_temp" in sub_ser:
                    if f"ssd{i_disk}_temp" not in comb_series:
                        comb_series[f"ssd{i_disk}_temp"] = []
                        comb_series[f"ssd{i_disk}_power"] = []
                    s_ind_t = sub_ser[f"ssd{i_disk}_temp"]
                    s_ind_p = sub_ser[f"ssd{i_disk}_power"]
                    for idx_t in range(len(st)):
                        comb_series[f"ssd{i_disk}_temp"].append(s_ind_t[idx_t] if idx_t < len(s_ind_t) else 0)
                        comb_series[f"ssd{i_disk}_power"].append(s_ind_p[idx_t] if idx_t < len(s_ind_p) else 0)
                        
            if st:
                cur_t_offset += max(st)
                
        sub_jm = sub_res.get("metrics", {}).get("jobs_details", [])
        for i_disk, jm in enumerate(sub_jm):
            if i_disk < num_jobs:
                jobs_agg[i_disk]["lecture_MiB_s"] = max(jobs_agg[i_disk]["lecture_MiB_s"], jm.get("lecture_MiB_s", 0))
                jobs_agg[i_disk]["ecriture_MiB_s"] = max(jobs_agg[i_disk]["ecriture_MiB_s"], jm.get("ecriture_MiB_s", 0))
                
                # Conserver la métrique détaillée de ce sous-test pour ce SSD — seulement s'il y a PLUSIEURS disques à comparer
                # (avec un seul disque, ce sont exactement les mêmes chiffres que ceux de tête)
                if num_jobs > 1:
                    test_name = sub.get("name", "Test")
                    if "lecture_MiB_s" in jm and jm["lecture_MiB_s"] > 0:
                        jobs_agg[i_disk][f"{_side_label(test_name, 'Lecture')} (MiB/s)"] = jm["lecture_MiB_s"]
                    if "ecriture_MiB_s" in jm and jm["ecriture_MiB_s"] > 0:
                        jobs_agg[i_disk][f"{_side_label(test_name, 'Écriture')} (MiB/s)"] = jm["ecriture_MiB_s"]
                    if "lecture_IOPS" in jm and jm["lecture_IOPS"] > 0:
                        jobs_agg[i_disk][f"{_side_label(test_name, 'Lecture')} (IOPS)"] = jm["lecture_IOPS"]
                    if "ecriture_IOPS" in jm and jm["ecriture_IOPS"] > 0:
                        jobs_agg[i_disk][f"{_side_label(test_name, 'Écriture')} (IOPS)"] = jm["ecriture_IOPS"]

    if comb_series["t"]:
        agg_metrics["series"] = comb_series
        agg_metrics["series_t"] = comb_series["t"]
        agg_metrics["series_temp"] = comb_series["disk_temp"]
        agg_metrics["series_power"] = comb_series["disk_power"]
        
        for i_disk in range(num_jobs):
            if f"ssd{i_disk}_temp" in comb_series:
                jobs_agg[i_disk]["series_t"] = comb_series["t"]
                jobs_agg[i_disk]["series_temp"] = comb_series[f"ssd{i_disk}_temp"]
                jobs_agg[i_disk]["series_power"] = comb_series[f"ssd{i_disk}_power"]
                temps_indiv = [v for v in jobs_agg[i_disk]["series_temp"] if v > 0]
                if temps_indiv:
                    jobs_agg[i_disk]["disk_temp_max_c"] = max(temps_indiv)
                    jobs_agg[i_disk]["disk_temp_moy_c"] = round(sum(temps_indiv)/len(temps_indiv), 1)
                pwrs_indiv = [v for v in jobs_agg[i_disk]["series_power"] if v > 0]
                if pwrs_indiv:
                    jobs_agg[i_disk]["disk_power_max_w"] = max(pwrs_indiv)
                    jobs_agg[i_disk]["disk_power_moy_w"] = round(sum(pwrs_indiv)/len(pwrs_indiv), 2)
                    
    if num_jobs > 1:                  # un seul disque : pas de « jobs_details » (doublon des chiffres de tête, copie des séries)
        agg_metrics["jobs_details"] = jobs_agg

    overall_score = max(max_read_bw, max_write_bw)
    if overall_score == 0 and suite_results:
        overall_score = suite_results[0]["result"].get("score", 0)
        
    return {
        "score": round(overall_score, 1),
        "unit": "MiB/s",
        "metrics": agg_metrics,
        "status": "ok",
        "raw": combined_raw,
        "is_suite": True,
        "suite_results": suite_results
    }
