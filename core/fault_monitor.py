"""
fault_monitor.py — Détection de pannes matérielles PENDANT un test.

Suit le journal noyau en direct (journalctl -k -f, repli dmesg --follow) à la recherche de
signatures matérielles (NVIDIA Xid, GPU « tombé du bus », MCE, AER, EDAC, NVMe, OOM,
thermal shutdown, reset amdgpu) et interroge nvidia-smi pour les erreurs ECC. Sur événement
critique, le test en cours est interrompu (kill_event / stop_flag_fn) et le rapport le signale.

Lecture seule, sans privilège obligatoire, jamais d'exception vers l'appelant.
"""

import functools
import inspect
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Dict, List, Optional, Set
from core.safety import nvidia_smi_usable

# Xid NVIDIA : https://docs.nvidia.com/deploy/xid-errors/
CRITICAL_XIDS = {48, 62, 64, 74, 79, 94, 95, 119, 120, 140}

MAX_EVENTS = 50

# (regex, sévérité, catégorie) — testés dans l'ordre, première correspondance retenue
_PATTERNS = [
    (re.compile(r"GPU has fallen off the bus|fallen off the bus", re.I), "critical", "GPU tombé du bus PCIe"),
    (re.compile(r"\[Hardware Error\].*(uncorrected|fatal)|Machine Check.*(uncorrected|fatal)|mce:.*uncorrected", re.I),
     "critical", "Erreur machine check (MCE) non corrigée"),
    (re.compile(r"\[Hardware Error\]|Machine Check|mce:", re.I), "warning", "Erreur machine check (MCE)"),
    (re.compile(r"AER:.*(uncorrected|fatal)|PCIe Bus Error:.*(Uncorrected|Fatal)", re.I),
     "critical", "Erreur PCIe AER non corrigée"),
    (re.compile(r"AER:.*corrected|PCIe Bus Error:.*Corrected", re.I), "warning", "Erreur PCIe AER corrigée"),
    (re.compile(r"EDAC.*\bUE\b|EDAC.*uncorrectable|Uncorrected error", re.I), "critical", "Erreur mémoire ECC non corrigée (UE)"),
    (re.compile(r"EDAC.*\bCE\b|EDAC.*correctable", re.I), "warning", "Erreur mémoire ECC corrigée (CE)"),
    (re.compile(r"amdgpu.*(ring .* timeout|GPU reset|fault)", re.I), "critical", "Blocage / reset GPU AMD"),
    (re.compile(r"thermal.*(shutdown|critical)|Critical temperature reached|Temperature above threshold", re.I),
     "critical", "Surchauffe critique"),
    (re.compile(r"(?:nvme\s+)?nvme\d+: .*(controller is down|I/O .*timeout|resetting controller|aborting)", re.I),
     "warning", "Problème NVMe (timeout / reset)"),
    (re.compile(r"blk_update_request: I/O error|Buffer I/O error|I/O error, dev ", re.I), "warning", "Erreur d'E/S disque"),
    (re.compile(r"Out of memory:|oom-kill|Killed process", re.I), "critical", "Manque de mémoire (OOM killer)"),
    (re.compile(r"NVLink.*(error|fail)|nvidia-nvswitch.*(error|fail|fatal)", re.I), "warning", "Erreur NVLink / NVSwitch"),
]
_XID_RE = re.compile(r"NVRM: Xid \((?P<bus>[^)]*)\):\s*(?P<code>\d+)")


def classify_line(line: str) -> Optional[Dict]:
    """Classe une ligne de journal noyau ; retourne un événement ou None si sans intérêt."""
    m = _XID_RE.search(line)
    if m:
        code = int(m.group("code"))
        sev = "critical" if code in CRITICAL_XIDS else "warning"
        return {"severity": sev, "category": f"NVIDIA Xid {code}", "xid": code, "device": m.group("bus"),
                "line": line.strip()[:300]}
    for rx, sev, cat in _PATTERNS:
        if rx.search(line):
            return {"severity": sev, "category": cat, "line": line.strip()[:300]}
    return None


def _terminate_proc(proc: subprocess.Popen) -> None:
    """Termine proprement un sous-processus (et son groupe si session isolée), puis tue si nécessaire."""
    try:
        if proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                if pgid == proc.pid:
                    os.killpg(pgid, signal.SIGTERM)
                else:
                    proc.terminate()
            except Exception:
                proc.terminate()
            try:
                proc.wait(timeout=1.0)
            except subprocess.TimeoutExpired:
                try:
                    pgid = os.getpgid(proc.pid)
                    if pgid == proc.pid:
                        os.killpg(pgid, signal.SIGKILL)
                    else:
                        proc.kill()
                except Exception:
                    proc.kill()
                try:
                    proc.wait(timeout=1.0)
                except Exception:
                    pass
    except Exception:
        pass


class FaultMonitor:
    """Surveille le matériel en arrière-plan pendant un test (journal noyau + ECC nvidia-smi)."""

    def __init__(self, abort_event: Optional[threading.Event] = None, abort_on_critical: bool = True,
                 poll_nvidia: bool = True, ecc_interval_s: float = 10.0):
        self.abort_event = abort_event
        self.abort_on_critical = abort_on_critical
        self.poll_nvidia = poll_nvidia and bool(nvidia_smi_usable())
        self.ecc_interval_s = ecc_interval_s
        self.events: List[Dict] = []
        self.critical = False
        self.source = ""
        self._stop = threading.Event()
        self._threads: List[threading.Thread] = []
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._t0 = 0.0
        self._ecc_base: Dict[str, int] = {}
        self._retired_seen: Set[str] = set()
        self._critical_total = 0

    # ── événements ──────────────────────────────────────────────────────────
    def _add(self, ev: Dict) -> None:
        ev["t"] = round(time.time() - self._t0, 1)
        is_crit = ev.get("severity") == "critical"
        with self._lock:
            if is_crit:
                self.critical = True
                self._critical_total += 1
            if len(self.events) < MAX_EVENTS:
                self.events.append(ev)
            elif is_crit:
                # Ne jamais perdre un événement critique à cause d'une saturation d'avertissements
                for idx, old in enumerate(self.events):
                    if old.get("severity") != "critical":
                        self.events.pop(idx)
                        self.events.append(ev)
                        break
        if is_crit and self.abort_on_critical and self.abort_event is not None:
            self.abort_event.set()

    # ── journal noyau ───────────────────────────────────────────────────────
    def _candidates(self) -> List[List[str]]:
        cands = []
        if shutil.which("journalctl"):
            cands.append(["journalctl", "-k", "-f", "-n", "0", "-o", "short-iso", "--no-pager"])
        if shutil.which("dmesg"):
            if shutil.which("sudo"):
                cands.append(["sudo", "-n", "dmesg", "--follow", "--ctime", "--color=never"])
            cands.append(["dmesg", "--follow", "--ctime", "--color=never"])
        return cands

    def _open_source(self) -> bool:
        for cmd in self._candidates():
            if self._stop.is_set():
                return False
            try:
                p = subprocess.Popen(
                    cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    text=True, errors="replace", bufsize=1, start_new_session=True,
                )
            except Exception:
                continue
            if self._stop.wait(0.5):
                _terminate_proc(p)
                try:
                    if p.stdout:
                        p.stdout.close()
                except Exception:
                    pass
                return False
            if p.poll() is None:      # toujours vivant : source utilisable
                self._proc, self.source = p, " ".join(cmd[:3] if cmd[0] != "sudo" else cmd[:4])
                return True
            _terminate_proc(p)
            try:
                p.wait(timeout=0.5)
            except Exception:
                pass
            try:
                if p.stdout:
                    p.stdout.close()
            except Exception:
                pass
        return False

    def _read_loop(self) -> None:
        proc = self._proc
        if not proc or not proc.stdout:
            return
        try:
            for line in proc.stdout:
                ev = classify_line(line)
                if ev:
                    self._add(ev)
                if self._stop.is_set():
                    break
        except (ValueError, OSError):
            pass

    # ── ECC nvidia-smi ──────────────────────────────────────────────────────
    def _ecc_once(self) -> None:
        try:
            r = subprocess.run(["nvidia-smi", "--query-gpu=index,ecc.errors.uncorrected.volatile.total,retired_pages.pending",
                                "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=6)
        except Exception:
            return
        if r.returncode != 0:
            return
        for line in r.stdout.strip().splitlines():
            parts = [x.strip() for x in line.split(",")]
            if len(parts) < 3:
                continue
            idx, unc, pend = parts[0], parts[1], parts[2].lower()
            if unc.isdigit():
                base = self._ecc_base.setdefault(idx, int(unc))
                if int(unc) > base:
                    self._ecc_base[idx] = int(unc)
                    self._add({"severity": "critical", "category": "Erreurs ECC non corrigées (nvidia-smi)",
                               "device": f"GPU {idx}", "line": f"GPU {idx} : erreurs ECC non corrigées {base} → {unc}"})
            if pend == "yes" and idx not in self._retired_seen:
                self._retired_seen.add(idx)
                self._add({"severity": "warning", "category": "Pages mémoire GPU en attente de retrait",
                           "device": f"GPU {idx}", "line": f"GPU {idx} : retired_pages.pending = Yes"})

    def _ecc_loop(self) -> None:
        self._ecc_once()
        while not self._stop.wait(self.ecc_interval_s):
            self._ecc_once()

    # ── cycle de vie ────────────────────────────────────────────────────────
    def start(self) -> "FaultMonitor":
        self._t0 = time.time()
        try:
            if self._open_source():
                t = threading.Thread(target=self._read_loop, daemon=True)
                t.start(); self._threads.append(t)
            if self.poll_nvidia:
                t = threading.Thread(target=self._ecc_loop, daemon=True)
                t.start(); self._threads.append(t)
        except Exception:
            pass
        return self

    def stop(self) -> Dict:
        """Arrête la surveillance et retourne un résumé sérialisable."""
        self._stop.set()
        proc = self._proc
        if proc is not None:
            _terminate_proc(proc)
        for t in self._threads:
            t.join(timeout=2.0)
        if proc is not None:
            try:
                if proc.stdout:
                    proc.stdout.close()
            except Exception:
                pass
        with self._lock:
            evs = list(self.events)
            crit_total = self._critical_total
        return {
            "source": self.source or "indisponible",
            "critical": self.critical,
            "count": len(evs),
            "critical_count": crit_total,
            "events": evs,
        }


def _attach_summary(result, summary: Dict) -> None:
    """Ajoute le résumé de surveillance au résultat (BenchmarkResult ou dict)."""
    note = ""
    if summary["critical"]:
        first = next((e for e in summary["events"] if e.get("severity") == "critical"), {})
        dev = f" ({first['device']})" if first.get("device") else ""
        note = (f"PANNE MATÉRIELLE détectée pendant le test : {first.get('category', '')}"
                f"{dev} — {first.get('line', '')}").strip()
    if isinstance(result, dict):
        result["hardware_events"] = summary
        if note and result.get("status") in (None, "ok", "stopped"):
            result["status"], result["error"] = "error", note
        return
    try:
        result.metrics["hardware_monitor"] = summary["source"]
        if summary["events"]:
            result.metrics["hardware_events"] = summary["events"]
            result.metrics["hardware_fault_critical"] = summary["critical_count"]
        elif summary["critical"]:
            result.metrics["hardware_fault_critical"] = summary.get("critical_count", 1)
        if note:
            if getattr(result, "status", "") in ("ok", "stopped"):
                result.status = "error"
            result.notes = (result.notes + "\n" if result.notes else "") + note
    except Exception:
        pass


_DEPTH = threading.local()


class _Observers:
    """Relevés passifs du test le plus externe : capteurs électroniques, liens PCIe et inventaire avant/après.

    Désactivables avec BENCHSUITE_NO_ELECTRICAL=1, BENCHSUITE_NO_PCIE=1 et BENCHSUITE_NO_INVENTORY=1. L'inventaire « avant » est pris
    dans un thread pour ne pas retarder le démarrage du test.
    """

    def __init__(self):
        self.rec = None
        self.pcie = None
        self.inv_before: Dict = {}
        self._inv_thread: Optional[threading.Thread] = None

    def start(self) -> "_Observers":
        if os.environ.get("BENCHSUITE_NO_ELECTRICAL") != "1":
            try:
                from core.electrical import ElectricalRecorder
                self.rec = ElectricalRecorder().start()
            except Exception:
                self.rec = None
        if os.environ.get("BENCHSUITE_NO_PCIE") != "1":
            try:
                from core.pcie_links import PcieLinkRecorder
                self.pcie = PcieLinkRecorder().start()      # liens PCIe (Gen / largeur) relevés PENDANT le test
            except Exception:
                self.pcie = None
        if os.environ.get("BENCHSUITE_NO_INVENTORY") != "1":
            def snap():
                try:
                    from core.inventory import collect_inventory
                    self.inv_before = collect_inventory()
                except Exception:
                    self.inv_before = {}
            self._inv_thread = threading.Thread(target=snap, daemon=True)
            self._inv_thread.start()
        return self

    def stop(self) -> Dict:
        """Retourne {'electrical': …, 'inventory_diff': …} (clés absentes si rien de lisible)."""
        out: Dict = {}
        if self.rec is not None:
            try:
                summ = self.rec.stop()
                if summ:
                    out["electrical"] = summ
            except Exception:
                pass
        if self.pcie is not None:
            try:
                pl = self.pcie.stop()
                if pl:
                    out["pcie_links"] = pl
            except Exception:
                pass
        if self._inv_thread is not None:
            self._inv_thread.join(timeout=60)
            if self.inv_before:
                try:
                    from core.inventory import collect_inventory, diff_inventory
                    out["inventory_diff"] = diff_inventory(self.inv_before, collect_inventory())
                except Exception:
                    pass
        return out


def start_group_observers() -> "_Observers":
    """Démarre UN relevé électronique + inventaire pour tout un groupe de tests lancés en parallèle."""
    return _Observers().start()


def suppress_observers() -> None:
    """À appeler au début d'un thread de test parallèle : le groupe gère déjà les relevés passifs."""
    _DEPTH.n = getattr(_DEPTH, "n", 0) + 1


def attach_observations(result, obs: Dict) -> None:
    """Version publique de _attach_observations (relevés d'un groupe parallèle)."""
    _attach_observations(result, obs)


def _attach_observations(result, obs: Dict) -> None:
    """Ajoute relevés électroniques et inventaire au résultat (BenchmarkResult ou dict fio)."""
    if not obs:
        return
    try:
        if isinstance(result, dict):
            result.setdefault("metrics", {}).update(obs)
        else:
            result.metrics.update(obs)
    except Exception:
        pass


def with_fault_watch(fn: Callable) -> Callable:
    """Décorateur : surveille le matériel pendant `fn` et interrompt le test sur panne critique.

    Pour le test le plus externe (pas les sous-tests d'une suite fio), relève aussi en tâche de fond les capteurs
    électroniques (tensions, puissances, ventilateurs) et compare l'inventaire matériel avant/après.
    Utilise `kill_event` (threading.Event) ou `stop_flag_fn` (callable) si la fonction les
    accepte. Désactivable avec BENCHSUITE_NO_FAULT_WATCH=1.
    """
    params = inspect.signature(fn).parameters

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if os.environ.get("BENCHSUITE_NO_FAULT_WATCH") == "1":
            return fn(*args, **kwargs)
        abort: Optional[threading.Event] = None
        if "kill_event" in params:
            abort = kwargs.get("kill_event") or threading.Event()
            kwargs["kill_event"] = abort
        elif "stop_flag_fn" in params:
            abort = threading.Event()
            orig = kwargs.get("stop_flag_fn")
            kwargs["stop_flag_fn"] = (lambda: bool((orig and orig()) or abort.is_set()))
        outer = getattr(_DEPTH, "n", 0) == 0
        _DEPTH.n = getattr(_DEPTH, "n", 0) + 1
        observers = None
        mon = None
        summary: Optional[Dict] = None
        obs: Dict = {}
        try:
            if outer:
                try:
                    observers = _Observers().start()
                except Exception:
                    observers = None
            try:
                mon = FaultMonitor(abort_event=abort).start()
            except Exception:
                mon = None
            result = fn(*args, **kwargs)
        finally:
            if mon is not None:
                try:
                    summary = mon.stop()
                except Exception:
                    summary = None
            _DEPTH.n = max(0, getattr(_DEPTH, "n", 1) - 1)
            if observers is not None:
                try:
                    obs = observers.stop()
                except Exception:
                    obs = {}
        if summary is not None:
            _attach_summary(result, summary)
        _attach_observations(result, obs)
        return result

    return wrapper
