"""
privileged.py — Exécution de commandes avec privilèges (root, sudo -n, ou assistant persistant pkexec).

Problème résolu : `pkexec` utilise par défaut l'action Polkit `auth_admin` (sans mémorisation),
ce qui obligeait à ressaisir le mot de passe à chaque étape d'une suite fio (ex. 8 étapes) et à
chaque nouveau test ou arrêt. Désormais, lorsqu'une élévation interactive est nécessaire, un unique
assistant privilégié (`core/priv_helper.py`) est démarré via `pkexec` et conservé pour toute la
session BenchSuite (1 seule authentification au maximum).

Si `os.geteuid() == 0` ou si `sudo -n true` réussit (NOPASSWD / ticket sudo actif), aucun assistant
ni invite graphique n'est lancé.
"""

import atexit
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import uuid
from typing import Optional


_HELPER_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "priv_helper.py")
_HELPER_LOCK = threading.Lock()
_HELPER: Optional["_PrivHelperClient"] = None
_SUDO_CACHE = {"ok": False, "ts": 0.0}


def is_root() -> bool:
    """Retourne True si BenchSuite tourne déjà avec l'UID 0."""
    try:
        return os.geteuid() == 0
    except Exception:
        return False


def has_sudo_nopasswd(cache_ttl_s: float = 2.0) -> bool:
    """Teste si `sudo -n true` réussit sans mot de passe (cache court pour limiter les appels répétés)."""
    if is_root():
        return True
    if not shutil.which("sudo"):
        return False
    now = time.monotonic()
    if _SUDO_CACHE["ok"] and (now - _SUDO_CACHE["ts"]) < cache_ttl_s:
        return True
    try:
        ok = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=5).returncode == 0
    except Exception:
        ok = False
    _SUDO_CACHE["ok"] = ok
    _SUDO_CACHE["ts"] = now
    return ok


class ElevatedProcess:
    """Poignée de processus asynchrone exécuté dans l'assistant privilégié (interface proche de Popen)."""

    def __init__(self, client: "_PrivHelperClient", job_id: str, pid: int, entry: dict):
        self._client = client
        self.job_id = job_id
        self.pid = pid
        self._entry = entry
        self.returncode: Optional[int] = None

    def poll(self) -> Optional[int]:
        if self.returncode is not None:
            return self.returncode
        if self._entry["event"].is_set():
            res = self._entry.get("result") or {}
            self.returncode = int(res.get("rc", -1))
            return self.returncode
        return None

    def wait(self, timeout: Optional[float] = None) -> int:
        if not self._entry["event"].wait(timeout=timeout):
            raise subprocess.TimeoutExpired(cmd="elevated-job", timeout=timeout or 0)
        rc = self.poll()
        return rc if rc is not None else -1

    def stop_elevated(self, sig_name: str = "TERM") -> bool:
        """Envoie SIGTERM ou SIGKILL au groupe de processus du job via l'assistant root."""
        if self.poll() is not None:
            return True
        return self._client.kill_job(self.job_id, sig=sig_name)

    def terminate(self) -> None:
        self.stop_elevated("TERM")

    def kill(self) -> None:
        self.stop_elevated("KILL")

    def get_output(self) -> tuple:
        """Retourne (stdout, stderr) une fois le job terminé."""
        self._entry["event"].wait(timeout=5)
        res = self._entry.get("result") or {}
        return str(res.get("stdout", "")), str(res.get("stderr", ""))


class _PrivHelperClient:
    """Client JSON-ligne dialoguant avec `core/priv_helper.py` via pipes anonymes stdin/stdout."""

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._pending: dict = {}
        self._jobs: dict = {}
        self._alive = True
        self._stderr_lines: list = []
        self._reader_th = threading.Thread(target=self._reader_loop, daemon=True)
        self._err_th = threading.Thread(target=self._stderr_loop, daemon=True)

    @classmethod
    def launch(cls, launcher_cmd: list, auth_timeout_s: float = 120.0) -> tuple:
        """Lance `priv_helper.py` via `pkexec` ou `sudo` et attend l'événement `ready`.

        Retourne `(client, "")` en cas de succès, ou `(None, message_erreur)` en cas de refus/échec.
        """
        proc = subprocess.Popen(
            launcher_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )

        q: queue.Queue = queue.Queue(maxsize=1)

        def _wait_ready():
            try:
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        q.put(("eof", None))
                        return
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        msg = json.loads(s)
                    except Exception:
                        continue
                    if isinstance(msg, dict) and msg.get("event") == "ready":
                        q.put(("ready", msg))
                        return
            except Exception as e:
                q.put(("err", str(e)))

        th = threading.Thread(target=_wait_ready, daemon=True)
        th.start()

        try:
            kind, payload = q.get(timeout=auth_timeout_s)
        except queue.Empty:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            for fh in (proc.stdin, proc.stdout, proc.stderr):
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            return None, "Délai d'authentification dépassé."

        if kind != "ready":
            err_txt = ""
            try:
                proc.wait(timeout=3)
            except Exception:
                pass
            try:
                err_txt = proc.stderr.read().strip()
            except Exception:
                pass
            for fh in (proc.stdin, proc.stdout, proc.stderr):
                if fh is not None:
                    try:
                        fh.close()
                    except Exception:
                        pass
            if not err_txt:
                err_txt = "Authentification Polkit / sudo annulée ou refusée."
            elif "polkit" not in err_txt.lower() and "auth" not in err_txt.lower():
                err_txt = f"Authentification Polkit / sudo refusée ({err_txt})."
            return None, err_txt

        client = cls(proc)
        client._reader_th.start()
        client._err_th.start()
        return client, ""

    def is_alive(self) -> bool:
        return self._alive and (self._proc.poll() is None)

    def _stderr_loop(self) -> None:
        try:
            for line in self._proc.stderr:
                if len(self._stderr_lines) > 50:
                    self._stderr_lines.pop(0)
                self._stderr_lines.append(line.rstrip())
        except Exception:
            pass

    def _reader_loop(self) -> None:
        try:
            for line in self._proc.stdout:
                s = line.strip()
                if not s:
                    continue
                try:
                    msg = json.loads(s)
                except Exception:
                    continue
                if not isinstance(msg, dict):
                    continue
                req_id = msg.get("id")
                if req_id is not None:
                    with self._state_lock:
                        slot = self._pending.get(req_id)
                    if slot is not None:
                        slot["result"] = msg
                        slot["event"].set()
                    continue
                if msg.get("event") == "exit":
                    job_id = msg.get("job_id")
                    with self._state_lock:
                        job = self._jobs.pop(job_id, None)
                    if job is not None:
                        job["result"] = msg
                        job["event"].set()
        except Exception:
            pass
        finally:
            self._alive = False
            with self._state_lock:
                for slot in self._pending.values():
                    if slot["result"] is None:
                        slot["result"] = {"ok": False, "rc": -1, "stdout": "", "stderr": "Assistant privilégié arrêté."}
                    slot["event"].set()
                for job in self._jobs.values():
                    if job["result"] is None:
                        job["result"] = {"rc": -1, "stdout": "", "stderr": "Assistant privilégié arrêté."}
                    job["event"].set()
                self._pending.clear()
                self._jobs.clear()

    def _send(self, payload: dict) -> bool:
        if not self.is_alive():
            return False
        data = json.dumps(payload, ensure_ascii=False) + "\n"
        with self._write_lock:
            try:
                self._proc.stdin.write(data)
                self._proc.stdin.flush()
                return True
            except Exception:
                self._alive = False
                return False

    def _request(self, op: str, timeout_s: Optional[float] = 35.0, **fields) -> dict:
        req_id = uuid.uuid4().hex
        slot = {"event": threading.Event(), "result": None}
        with self._state_lock:
            self._pending[req_id] = slot
        req = {"id": req_id, "op": op, **fields}
        if not self._send(req):
            with self._state_lock:
                self._pending.pop(req_id, None)
            raise RuntimeError("Assistant privilégié indisponible.")
        try:
            ok = slot["event"].wait(timeout=timeout_s)
        finally:
            with self._state_lock:
                self._pending.pop(req_id, None)
        if not ok or slot["result"] is None:
            raise subprocess.TimeoutExpired(cmd=fields.get("cmd", op), timeout=timeout_s or 0)
        return slot["result"]

    def run(self, cmd_args: list, timeout: int = 35, env: Optional[dict] = None) -> subprocess.CompletedProcess:
        wait_s = (float(timeout) + 15.0) if timeout else None
        res = self._request("run", timeout_s=wait_s, cmd=[str(x) for x in cmd_args], timeout=timeout, env=env)
        if res.get("timed_out"):
            raise subprocess.TimeoutExpired(
                cmd=cmd_args,
                timeout=timeout,
                output=res.get("stdout", ""),
                stderr=res.get("stderr", ""),
            )
        if res.get("not_found"):
            raise FileNotFoundError(res.get("stderr") or str(cmd_args[0]))
        return subprocess.CompletedProcess(
            args=cmd_args,
            returncode=int(res.get("rc", -1)),
            stdout=str(res.get("stdout", "")),
            stderr=str(res.get("stderr", "")),
        )

    def start_job(self, cmd_args: list, env: Optional[dict] = None) -> ElevatedProcess:
        job_id = uuid.uuid4().hex
        entry = {"event": threading.Event(), "result": None}
        with self._state_lock:
            self._jobs[job_id] = entry
        try:
            res = self._request("start", timeout_s=15.0, job_id=job_id, cmd=[str(x) for x in cmd_args], env=env)
        except Exception:
            with self._state_lock:
                self._jobs.pop(job_id, None)
            raise
        if not res.get("ok"):
            with self._state_lock:
                self._jobs.pop(job_id, None)
            err = str(res.get("error") or "Échec du démarrage privilégié")
            if res.get("not_found"):
                raise FileNotFoundError(err)
            raise RuntimeError(err)
        return ElevatedProcess(self, job_id, int(res.get("pid") or 0), entry)

    def kill_job(self, job_id: str, sig: str = "TERM") -> bool:
        try:
            res = self._request("kill", timeout_s=5.0, job_id=job_id, sig=sig)
            return bool(res.get("ok"))
        except Exception:
            return False

    def close(self) -> None:
        was_alive = self._alive
        self._alive = False
        if was_alive:
            try:
                with self._write_lock:
                    self._proc.stdin.write(json.dumps({"id": "bye", "op": "shutdown"}) + "\n")
                    self._proc.stdin.flush()
                    self._proc.stdin.close()
            except Exception:
                pass
            try:
                self._proc.wait(timeout=2)
            except Exception:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=1)
                except Exception:
                    pass
        for th in (self._reader_th, self._err_th):
            if th.is_alive():
                th.join(timeout=0.5)
        for fh in (self._proc.stdin, self._proc.stdout, self._proc.stderr):
            if fh is not None:
                try:
                    fh.close()
                except Exception:
                    pass


def _active_helper() -> Optional[_PrivHelperClient]:
    with _HELPER_LOCK:
        if _HELPER is not None and _HELPER.is_alive():
            return _HELPER
        return None


def is_session_elevated() -> bool:
    """Retourne True si l'exécution root est déjà acquise pour la session (uid=0, sudo -n ou helper actif)."""
    return is_root() or (_active_helper() is not None) or has_sudo_nopasswd()


def _select_launcher() -> tuple:
    """Choisit le lanceur de l'assistant (`pkexec` en session graphique, `sudo` en terminal)."""
    py_exe = sys.executable or shutil.which("python3") or "python3"
    has_display = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
    has_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())

    # En session graphique (ou sans TTY), pkexec affiche la fenêtre Polkit standard
    if shutil.which("pkexec") and (has_display or not has_tty):
        return "pkexec", ["pkexec", py_exe, "-u", _HELPER_PATH]

    # En CLI sans interface graphique avec terminal : `sudo -v` puis assistant `sudo -n`
    if shutil.which("sudo"):
        return "sudo", [py_exe, "-u", _HELPER_PATH]

    if shutil.which("pkexec"):
        return "pkexec", ["pkexec", py_exe, "-u", _HELPER_PATH]

    raise FileNotFoundError("Ni pkexec ni sudo ne sont disponibles sur le système.")


def ensure_elevated_session() -> tuple:
    """Garantit une session privilégiée (uid=0, `sudo -n` ou assistant persistant démarré une fois).

    Retourne `(True, "")` si prêt, ou `(False, message_erreur)` si l'utilisateur annule ou refuse.
    Lève `FileNotFoundError` si ni `pkexec` ni `sudo` ne sont disponibles.
    """
    if is_root() or has_sudo_nopasswd():
        return True, ""

    with _HELPER_LOCK:
        global _HELPER
        if _HELPER is not None and _HELPER.is_alive():
            return True, ""
        if has_sudo_nopasswd():
            return True, ""

        kind, launcher = _select_launcher()
        if kind == "sudo":
            has_tty = bool(getattr(sys.stdin, "isatty", lambda: False)())
            if has_tty:
                try:
                    r = subprocess.run(["sudo", "-v"], timeout=120)
                    if r.returncode != 0:
                        return False, "Authentification sudo annulée ou refusée."
                    _SUDO_CACHE["ok"] = True
                    _SUDO_CACHE["ts"] = time.monotonic()
                except subprocess.TimeoutExpired:
                    return False, "Délai d'authentification sudo dépassé."
                except Exception as e:
                    return False, f"Échec de sudo : {e}"
                launcher = ["sudo", "-n"] + launcher
            else:
                return False, "pkexec absent et sudo nécessite un terminal pour le mot de passe."

        client, err = _PrivHelperClient.launch(launcher)
        if client is None:
            return False, err
        _HELPER = client
        return True, ""


def start_elevated_job(cmd_args: list, env: Optional[dict] = None) -> ElevatedProcess:
    """Démarre une commande longue (ex. `fio`) dans l'assistant privilégié persistant.

    Lève `PermissionError` si l'authentification est annulée/refusée, `FileNotFoundError` si
    l'outil ou les outils d'élévation sont absents.
    """
    ok, err = ensure_elevated_session()
    if not ok:
        raise PermissionError(err or "Authentification Polkit / sudo annulée ou refusée.")
    client = _active_helper()
    if client is None:
        # Cas sudo -n ou uid=0 : démarrer via assistant si lancé sous sudo -n n'était pas souhaité,
        # sinon relancer explicitement via helper sudo -n pour disposer d'une poignée ElevatedProcess.
        py_exe = sys.executable or shutil.which("python3") or "python3"
        prefix = [] if is_root() else ["sudo", "-n"]
        with _HELPER_LOCK:
            global _HELPER
            if _HELPER is None or not _HELPER.is_alive():
                client, l_err = _PrivHelperClient.launch(prefix + [py_exe, "-u", _HELPER_PATH], auth_timeout_s=15.0)
                if client is None:
                    raise RuntimeError(l_err)
                _HELPER = client
            client = _HELPER
    return client.start_job(cmd_args, env=env)


def run_if_elevated(cmd_args: list, timeout: int = 10, env: Optional[dict] = None) -> Optional[subprocess.CompletedProcess]:
    """Exécute `cmd_args` en root UNIQUEMENT si les droits sont déjà acquis sans aucune invite.

    Retourne None si aucune session privilégiée n'est active (ne déclenche jamais de fenêtre Polkit).
    Utile pour les relevés passifs (nvme smart-log, smartctl) pendant/après un test.
    """
    merged_env = None
    if env:
        merged_env = os.environ.copy()
        merged_env.update({str(k): str(v) for k, v in env.items()})

    if is_root():
        try:
            return subprocess.run(cmd_args, capture_output=True, text=True, timeout=timeout, env=merged_env)
        except Exception:
            return None

    client = _active_helper()
    if client is not None:
        try:
            return client.run(cmd_args, timeout=timeout, env=env)
        except Exception:
            pass

    if has_sudo_nopasswd():
        try:
            return subprocess.run(["sudo", "-n"] + cmd_args, capture_output=True, text=True, timeout=timeout, env=merged_env)
        except Exception:
            return None

    return None


def run_elevated(cmd_args: list, timeout: int = 35, retry_on_failure: bool = True, env: Optional[dict] = None):
    """Exécute une commande avec privilèges : root direct, sinon `sudo -n`, sinon assistant persistant (`pkexec`).

    Une seule authentification Polkit est demandée pour toute la session BenchSuite.
    `retry_on_failure` est conservé pour compatibilité et n'a plus d'effet.
    Lève `FileNotFoundError` si ni `pkexec` ni `sudo` ne sont disponibles.
    """
    merged_env = None
    if env:
        merged_env = os.environ.copy()
        merged_env.update({str(k): str(v) for k, v in env.items()})

    if is_root():
        return subprocess.run(cmd_args, capture_output=True, text=True, timeout=timeout, env=merged_env)

    if has_sudo_nopasswd():
        return subprocess.run(["sudo", "-n"] + cmd_args, capture_output=True, text=True, timeout=timeout, env=merged_env)

    # Si l'assistant privilégié est déjà actif, le réutiliser immédiatement (0 invite)
    client = _active_helper()
    if client is not None:
        return client.run(cmd_args, timeout=timeout, env=env)

    # Sinon démarrer la session persistante (1 seule invite Polkit / sudo pour toute la session)
    ok, err = ensure_elevated_session()
    if not ok:
        return subprocess.CompletedProcess(args=cmd_args, returncode=126, stdout="", stderr=err)

    # Cas où ensure_elevated_session a activé un ticket `sudo -v` en terminal
    if has_sudo_nopasswd():
        return subprocess.run(["sudo", "-n"] + cmd_args, capture_output=True, text=True, timeout=timeout, env=merged_env)

    client = _active_helper()
    if client is not None:
        return client.run(cmd_args, timeout=timeout, env=env)

    raise FileNotFoundError("Ni pkexec ni sudo ne sont disponibles sur le système.")


def close_elevated_session() -> None:
    """Ferme proprement l'assistant privilégié s'il est actif."""
    global _HELPER
    with _HELPER_LOCK:
        client = _HELPER
        _HELPER = None
    if client is not None:
        client.close()


atexit.register(close_elevated_session)
