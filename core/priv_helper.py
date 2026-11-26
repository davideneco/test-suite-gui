#!/usr/bin/env python3
"""
priv_helper.py — Assistant privilégié persistant pour une session BenchSuite.

Lancé une seule fois par session (via pkexec ou sudo) quand des commandes root
sont nécessaires (fio sur périphérique brut /dev/..., réglages matériel, SMART).
Communique uniquement via stdin/stdout (JSON par ligne) avec le processus parent
BenchSuite : aucun socket ni fichier temporaire accessible à un autre utilisateur.

S'arrête proprement à la réception de « shutdown » ou dès la fermeture de stdin
(fin normale ou crash de BenchSuite), en terminant les processus fils encore actifs.
"""

import json
import os
import signal
import subprocess
import sys
import threading


_WRITE_LOCK = threading.Lock()
_JOBS_LOCK = threading.Lock()
_JOBS: dict = {}
_SHUTDOWN = threading.Event()


def _send(obj: dict) -> None:
    """Envoie un objet JSON sur stdout de façon atomique et immédiatement vidée."""
    line = json.dumps(obj, ensure_ascii=False) + "\n"
    with _WRITE_LOCK:
        try:
            sys.stdout.write(line)
            sys.stdout.flush()
        except BrokenPipeError:
            _SHUTDOWN.set()
        except Exception:
            pass


def _decode(data) -> str:
    if data is None:
        return ""
    if isinstance(data, bytes):
        return data.decode("utf-8", errors="replace")
    return str(data)


def _kill_proc_group(proc: subprocess.Popen, sig: int) -> None:
    """Envoie un signal au groupe de processus du fils (tue fio et ses workers)."""
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, sig)
    except ProcessLookupError:
        return
    except Exception:
        try:
            proc.send_signal(sig)
        except Exception:
            pass


def _handle_run(req: dict) -> None:
    """Exécute une commande synchrone (dans son thread) et renvoie rc/stdout/stderr."""
    req_id = req.get("id")
    cmd = req.get("cmd")
    timeout = req.get("timeout")
    env_extra = req.get("env")
    if not isinstance(cmd, list) or not cmd:
        _send({"id": req_id, "ok": False, "rc": -1, "stdout": "", "stderr": "Commande vide", "timed_out": False})
        return

    env = None
    if isinstance(env_extra, dict) and env_extra:
        env = os.environ.copy()
        for k, v in env_extra.items():
            env[str(k)] = str(v)

    try:
        proc = subprocess.Popen(
            [str(x) for x in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except FileNotFoundError as e:
        _send({"id": req_id, "ok": False, "rc": -1, "stdout": "", "stderr": str(e), "timed_out": False, "not_found": True})
        return
    except Exception as e:
        _send({"id": req_id, "ok": False, "rc": -3, "stdout": "", "stderr": str(e), "timed_out": False})
        return

    timed_out = False
    try:
        out_b, err_b = proc.communicate(timeout=timeout if timeout else None)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_proc_group(proc, signal.SIGTERM)
        try:
            out_b, err_b = proc.communicate(timeout=2)
        except Exception:
            _kill_proc_group(proc, signal.SIGKILL)
            try:
                out_b, err_b = proc.communicate(timeout=3)
            except Exception:
                out_b, err_b = b"", b"Timeout"

    _send({
        "id": req_id,
        "ok": not timed_out,
        "rc": proc.returncode if proc.returncode is not None else -2,
        "stdout": _decode(out_b),
        "stderr": _decode(err_b),
        "timed_out": timed_out,
    })


def _wait_job(job_id: str, proc: subprocess.Popen) -> None:
    """Attend la fin d'un job asynchrone et émet l'événement « exit »."""
    try:
        out_b, err_b = proc.communicate()
        rc = proc.returncode if proc.returncode is not None else -1
    except Exception as e:
        out_b, err_b = b"", str(e).encode("utf-8", errors="replace")
        rc = -3
    finally:
        with _JOBS_LOCK:
            _JOBS.pop(job_id, None)

    _send({
        "event": "exit",
        "job_id": job_id,
        "rc": rc,
        "stdout": _decode(out_b),
        "stderr": _decode(err_b),
    })


def _handle_start(req: dict) -> None:
    """Démarre une commande longue (ex. fio) suivie par job_id."""
    req_id = req.get("id")
    job_id = str(req.get("job_id") or "")
    cmd = req.get("cmd")
    env_extra = req.get("env")
    if not job_id or not isinstance(cmd, list) or not cmd:
        _send({"id": req_id, "ok": False, "error": "Paramètres start invalides"})
        return

    env = None
    if isinstance(env_extra, dict) and env_extra:
        env = os.environ.copy()
        for k, v in env_extra.items():
            env[str(k)] = str(v)

    try:
        proc = subprocess.Popen(
            [str(x) for x in cmd],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except FileNotFoundError as e:
        _send({"id": req_id, "ok": False, "error": f"Introuvable : {cmd[0]} ({e})", "not_found": True})
        return
    except Exception as e:
        _send({"id": req_id, "ok": False, "error": str(e)})
        return

    with _JOBS_LOCK:
        _JOBS[job_id] = proc

    _send({"id": req_id, "ok": True, "job_id": job_id, "pid": proc.pid})
    t = threading.Thread(target=_wait_job, args=(job_id, proc), daemon=True)
    t.start()


def _handle_kill(req: dict) -> None:
    """Envoie SIGTERM ou SIGKILL au groupe de processus d'un job actif."""
    req_id = req.get("id")
    job_id = str(req.get("job_id") or "")
    sig_name = str(req.get("sig") or "TERM").upper()
    sig = signal.SIGKILL if sig_name == "KILL" else signal.SIGTERM

    with _JOBS_LOCK:
        proc = _JOBS.get(job_id)

    if proc is not None:
        _kill_proc_group(proc, sig)
    _send({"id": req_id, "ok": True})


def _cleanup_all_jobs() -> None:
    """Arrête tous les jobs encore en cours avant de quitter l'assistant."""
    with _JOBS_LOCK:
        active = list(_JOBS.values())
    for proc in active:
        _kill_proc_group(proc, signal.SIGTERM)
    for proc in active:
        try:
            proc.wait(timeout=1.5)
        except Exception:
            _kill_proc_group(proc, signal.SIGKILL)


def main() -> int:
    # Ne pas mourir sur SIGPIPE si le parent se ferme brusquement
    try:
        signal.signal(signal.SIGPIPE, signal.SIG_IGN)
    except Exception:
        pass

    _send({"event": "ready", "euid": os.geteuid()})

    while not _SHUTDOWN.is_set():
        try:
            line = sys.stdin.readline()
        except Exception:
            break
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except Exception:
            continue
        if not isinstance(req, dict):
            continue

        op = req.get("op")
        if op == "ping":
            _send({"id": req.get("id"), "ok": True, "euid": os.geteuid()})
        elif op == "run":
            threading.Thread(target=_handle_run, args=(req,), daemon=True).start()
        elif op == "start":
            _handle_start(req)
        elif op == "kill":
            _handle_kill(req)
        elif op == "shutdown":
            _send({"id": req.get("id"), "ok": True})
            break

    _cleanup_all_jobs()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
