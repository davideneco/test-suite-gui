"""
runner.py — Moteur de campagne de tests (utilisé par la CLI, réutilisable par l'interface).

Une campagne = une liste d'étapes exécutées à la suite :
  {"bench": "gpu_fryer", "params": {"time_s": 600}}        test du catalogue
  {"stress": "cpu_only_quick", "params": {...}}            profil stress-ng (nom du profil = nom du résultat)
  {"fio": "peak_seq1m_q8t1_read", "path": "/mnt/test"}     profil fio
  {"pause": 60}                                            temps de refroidissement
  {"parallel": [étape, étape, …]}                          plusieurs tests EN MÊME TEMPS (ex. burn CPU + gpu-fryer + fio)

Une étape peut porter {"time_s": N} pour forcer sa durée (profil stress-ng, test GPU time_s, runtime fio).

Chaque campagne vit dans un dossier (run_dir) avec un fichier d'état `state.json` mis à jour
après chaque étape et un point de contrôle `current_partial.json` toutes les 30 s pendant un
test : après un crash / une coupure, `--resume` reprend là où on s'est arrêté et conserve les
données partielles du test interrompu.
"""

import inspect
import json
import os
import signal
import threading
import time
from dataclasses import asdict
from datetime import datetime
from typing import Callable, Dict, List, Optional, Tuple

from core.result import BenchmarkResult

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SUITE_DIRS = [os.path.join(ROOT, "suites"), os.path.expanduser("~/.benchsuite/suites")]
STATE_VERSION = 1
CHECKPOINT_EVERY_S = float(os.environ.get("BENCHSUITE_CHECKPOINT_S", "30"))
MAX_PARTIAL_POINTS = 4000


# ─────────────────────────────────────────────────────────────────────────────
# Séquences (suites) de tests
# ─────────────────────────────────────────────────────────────────────────────

def list_suites() -> List[Dict]:
    """Liste les séquences disponibles (dossier suites/ du logiciel + ~/.benchsuite/suites)."""
    out, seen = [], set()
    for d in SUITE_DIRS:
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            sid = fn[:-5]
            if sid in seen:
                continue
            try:
                with open(os.path.join(d, fn), encoding="utf-8") as f:
                    data = json.load(f)
                out.append({"id": sid, "name": data.get("name", sid), "description": data.get("description", ""),
                            "path": os.path.join(d, fn), "steps": len(data.get("steps", []))})
                seen.add(sid)
            except Exception:
                continue
    return out


def load_suite(ref: str) -> Dict:
    """Charge une séquence par identifiant (ex. « recette_gpu ») ou par chemin de fichier JSON."""
    path = ref if os.path.isfile(ref) else ""
    if not path:
        for s in list_suites():
            if s["id"] == ref:
                path = s["path"]
                break
    if not path:
        raise ValueError(f"Séquence introuvable : {ref} (voir --suites)")
    with open(path, encoding="utf-8") as f:
        suite = json.load(f)
    suite.setdefault("id", os.path.splitext(os.path.basename(path))[0])
    suite["_path"] = path
    errs = validate_steps(suite.get("steps", []))
    if errs:
        raise ValueError("Séquence invalide :\n  - " + "\n  - ".join(errs))
    return suite


def validate_steps(steps: List[Dict]) -> List[str]:
    """Vérifie la structure des étapes (types connus, identifiants existants)."""
    from core.benchmarks import CATALOG
    from core.stress_profiles import get_profile as get_stress
    from core.fio_profiles import get_profile as get_fio
    errs = []
    ids = {b["id"] for b in CATALOG}
    if not steps:
        errs.append("aucune étape")
    def check(i, st, in_group=False):
        kinds = [k for k in ("bench", "stress", "fio", "pause", "parallel") if k in st]
        if len(kinds) != 1:
            errs.append(f"étape {i} : une seule clé parmi bench/stress/fio/pause/parallel est attendue")
            return
        k = kinds[0]
        if k == "parallel":
            subs = st["parallel"]
            if in_group:
                errs.append(f"étape {i} : un groupe parallèle ne peut pas contenir un autre groupe")
            elif not isinstance(subs, list) or len(subs) < 2:
                errs.append(f"étape {i} : un groupe parallèle contient au moins 2 tests")
            else:
                for j, sub in enumerate(subs, 1):
                    if not isinstance(sub, dict) or "pause" in sub:
                        errs.append(f"étape {i}.{j} : une pause n'a pas de sens dans un groupe parallèle")
                    else:
                        check(f"{i}.{j}", sub, True)
            return
        if k == "bench" and st["bench"] not in ids:
            errs.append(f"étape {i} : test inconnu « {st['bench']} » (voir --list)")
        elif k == "stress" and not get_stress(st["stress"]):
            errs.append(f"étape {i} : profil stress inconnu « {st['stress']} » (voir --profiles)")
        elif k == "fio" and not get_fio(st["fio"]):
            errs.append(f"étape {i} : profil fio inconnu « {st['fio']} » (voir --profiles)")
        elif k == "pause":
            try:
                float(st["pause"])
            except (TypeError, ValueError):
                errs.append(f"étape {i} : durée de pause invalide")

    for i, st in enumerate(steps, 1):
        check(i, st)
    return errs


def parallel_conflicts(step: Dict) -> List[str]:
    """Avertissements pour un groupe parallèle : ressources partagées qui faussent les mesures."""
    subs = step.get("parallel", [])
    warns = []
    gpu = [s for s in subs if "bench" in s and str(s["bench"]).startswith("gpu")]
    if len(gpu) > 1:
        warns.append("Plusieurs tests GPU en même temps : ils se partagent les GPU (mesures faussées). Séparez-les ou ciblez des GPU différents.")
    paths = [s.get("path") for s in subs if "fio" in s]
    if len(paths) > 1 and len(set(paths)) < len(paths):
        warns.append("Deux tests fio visent le même disque : les débits se gêneront.")
    cpu = [s for s in subs if "stress" in s or s.get("bench") in ("cpu_stress", "ram_stream")]
    if len(cpu) > 1:
        warns.append("Plusieurs charges CPU/RAM en même temps : elles se partagent les cœurs et la mémoire.")
    return warns


def step_label(step: Dict) -> str:
    """Libellé lisible d'une étape."""
    from core.benchmarks import CATALOG
    from core.stress_profiles import get_profile as get_stress
    from core.fio_profiles import get_profile as get_fio
    if "bench" in step:
        b = next((x for x in CATALOG if x["id"] == step["bench"]), None)
        return b["name"] if b else step["bench"]
    if "stress" in step:
        p = get_stress(step["stress"])
        return p["name"] if p else step["stress"]
    if "fio" in step:
        p = get_fio(step["fio"])
        return "fio — " + (p["name"] if p else step["fio"])
    if "pause" in step:
        return f"Pause {step['pause']} s"
    if "parallel" in step:
        return "En parallèle : " + " + ".join(step_label(x) for x in step["parallel"])
    return "?"


# ─────────────────────────────────────────────────────────────────────────────
# Exécution d'une étape
# ─────────────────────────────────────────────────────────────────────────────

def _fio_result(name: str, rd: dict, t0: float, path: str) -> BenchmarkResult:
    """Convertit le dict retourné par core.fio_runner en BenchmarkResult."""
    status = rd.get("status", "ok")
    if status not in ("ok", "skipped", "stopped"):
        status = "error"
    m = dict(rd.get("metrics", {}))
    if "series" in rd and "series" not in m:
        m["series"] = rd["series"]
    if rd.get("error"):
        m["erreur"] = rd["error"]
    if (rd.get("hardware_events") or {}).get("events"):
        m["hardware_events"] = rd["hardware_events"]["events"]
        # Sans cette clé, une MCE / AER fatale / OOM pendant fio n'arrêtait pas la campagne (stop_on_critical) et la CLI
        # sortait en 1 au lieu de 2.
        m["hardware_fault_critical"] = rd["hardware_events"].get("critical_count", 0)
    m["path"] = path
    br = BenchmarkResult(name=name, category="Stockage", score=rd.get("score"), unit=rd.get("unit", "MiB/s"),
                         duration_s=rd.get("duration_s", time.time() - t0), raw_output=str(rd.get("raw", ""))[:8000],
                         metrics=m, status=status, command=str(m.get("command", "") or ""))
    if rd.get("error") and status == "error":
        br.notes = str(rd["error"])[:500]
    return br


def run_step(step: Dict, cb: Optional[Callable] = None, series_cb: Optional[Callable] = None,
             kill_event: Optional[threading.Event] = None, defaults: Optional[Dict] = None) -> BenchmarkResult:
    """Exécute une étape et retourne son BenchmarkResult (n'est jamais levée : erreurs → status error).
    Un test « stopped » reçoit une note : durée écoulée et cause possible (le rapport disait seulement « arrêté »)."""
    t_start = time.time()
    res = _run_step_inner(step, cb, series_cb, kill_event, defaults)
    if res.status == "stopped" and not (res.notes or "").strip():
        want = (step.get("time_s") or (step.get("params") or {}).get("time_s")) if isinstance(step, dict) else None
        res.notes = (f"Arrêt demandé après {time.time() - t_start:.0f} s" + (f" (durée prévue : {int(want)} s)" if want else "")
                     + " : bouton Stopper, ou arrêt automatique d'un test du même groupe (panne matérielle critique détectée). "
                     "Les valeurs ci-dessus portent sur la partie exécutée.")
    return res


def _run_step_inner(step: Dict, cb: Optional[Callable] = None, series_cb: Optional[Callable] = None,
                    kill_event: Optional[threading.Event] = None, defaults: Optional[Dict] = None) -> BenchmarkResult:
    defaults = defaults or {}
    kill_event = kill_event or threading.Event()
    t0 = time.time()
    label = step_label(step)
    if series_cb:       # chaque point de télémétrie est étiqueté avec le type de test émetteur (clé `_src`)
        src = "fio" if "fio" in step else ("stress" if "stress" in step else str(step.get("bench", "")))
        _raw_series_cb = series_cb
        series_cb = lambda pt, _s=src: _raw_series_cb({**pt, "_src": _s})
    try:
        if "bench" in step:
            from core.benchmarks import CATALOG
            item = next(b for b in CATALOG if b["id"] == step["bench"])
            params = dict(item["params"])
            params.update(step.get("params", {}))
            sig = inspect.signature(item["fn"]).parameters
            if step.get("time_s") and "time_s" in sig:
                params["time_s"] = int(step["time_s"])
            if "cb" in sig:
                params["cb"] = cb
            if "series_cb" in sig:
                params["series_cb"] = series_cb
            if "on_series_cb" in sig:
                params["on_series_cb"] = series_cb
            if "kill_event" in sig:
                params["kill_event"] = kill_event
            if not any(p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.values()):
                params = {k: v for k, v in params.items() if k in sig}   # ex. « filter » du catalogue fio, géré par l'interface
            res = item["fn"](**params)
            if isinstance(res, dict):       # anciens tests retournant un dict
                res = BenchmarkResult(item["name"], item["category"], res.get("score"), res.get("unit", ""),
                                      res.get("duration_s", time.time() - t0), "",
                                      metrics={k: v for k, v in res.items() if k not in ("score", "unit", "duration_s", "status")},
                                      status=res.get("status", "ok"))
            if isinstance(res, BenchmarkResult):
                for pk, pv in params.items():
                    if pk not in ("cb", "series_cb", "on_series_cb", "kill_event") and pv not in ("", None) and pk not in res.metrics:
                        res.metrics[pk] = pv
                if not getattr(res, "command", "") and res.metrics.get("command"):
                    res.command = str(res.metrics["command"])
            return res

        if "stress" in step:
            from core.benchmarks import bench_stress_profile
            from core.stress_profiles import get_profile, resolve_auto
            prof = get_profile(step["stress"])
            params = resolve_auto(prof.get("params", {}))
            params.update(step.get("params", {}))
            if "time_s" in step:
                params["time_s"] = int(step["time_s"])
            params["profile_name"] = prof["name"]
            params["profile_id"] = prof.get("id", step["stress"])
            return bench_stress_profile(cb=cb, series_cb=series_cb, kill_event=kill_event, **params)

        if "fio" in step:
            from core.fio_profiles import get_profile
            from core.fio_runner import run_fio_from_params, run_fio_suite
            prof = get_profile(step["fio"])
            path = step.get("path") or defaults.get("fio_path") or "/tmp"
            raw = path.startswith("/dev/")
            stop = kill_event.is_set
            name = f"fio — {prof['name']}"
            if prof.get("is_suite"):
                rd = run_fio_suite(prof.get("suite_tests", []), path=path, cb=cb, stop_flag_fn=stop,
                                   raw_device=raw, on_series=series_cb, suite_name=prof.get("name", ""))
            else:
                params = dict(prof.get("params", {}))
                params.update(step.get("params", {}))
                if step.get("time_s"):
                    params["runtime_s"] = int(step["time_s"])
                params["path"] = path
                params["raw_device"] = raw
                if raw:
                    params["direct"] = True
                rd = run_fio_from_params(cb=cb, stop_flag_fn=stop, on_series=series_cb, **params)
            br = _fio_result(name, rd, t0, path)
            br.metrics["profile_id"] = step["fio"]      # permet aux règles du verdict de reconnaître le profil (ex. OCP)
            return br

        return BenchmarkResult(label, "Autre", None, "", 0, "", status="error",
                               metrics={"erreur": "type d'étape inconnu"})
    except Exception as exc:     # best-effort : une étape qui plante ne doit jamais arrêter la campagne
        return BenchmarkResult(label, "Autre", None, "", time.time() - t0, str(exc), status="error",
                               metrics={"erreur": f"{type(exc).__name__}: {exc}"}, notes=str(exc)[:300])


def run_parallel(step: Dict, cb: Optional[Callable] = None, series_cb: Optional[Callable] = None,
                 kill_event: Optional[threading.Event] = None, defaults: Optional[Dict] = None) -> List[BenchmarkResult]:
    """Exécute les tests d'un groupe EN MÊME TEMPS (un thread par test) et retourne leurs résultats, dans l'ordre.

    Un seul relevé électronique + inventaire couvre tout le groupe. Un arrêt (bouton, panne matérielle critique
    détectée par l'un des tests) stoppe tous les tests du groupe via le même `kill_event`.
    """
    from core import fault_monitor
    subs = step["parallel"]
    kill_event = kill_event or threading.Event()
    n = len(subs)
    results: List[Optional[BenchmarkResult]] = [None] * n
    pct = [0] * n
    msgs = [""] * n
    lock = threading.Lock()

    def make_cb(i: int):
        def sub_cb(p, m=""):
            with lock:
                pct[i], msgs[i] = int(p or 0), str(m or "")
                avg = int(sum(pct) / n)
                last = f"[{i + 1}/{n}] {step_label(subs[i])} — {msgs[i]}"
            if cb:
                try:
                    cb(avg, last)
                except Exception:
                    pass
        return sub_cb

    def work(i: int):
        fault_monitor.suppress_observers()      # le groupe gère déjà les relevés passifs
        try:
            results[i] = run_step(subs[i], cb=make_cb(i), series_cb=series_cb, kill_event=kill_event, defaults=defaults)
        except Exception as exc:                # run_step n'en lève pas, mais un thread ne doit jamais mourir en silence
            results[i] = BenchmarkResult(step_label(subs[i]), "Autre", None, "", 0, str(exc), status="error",
                                         metrics={"erreur": f"{type(exc).__name__}: {exc}"}, notes=str(exc)[:300])

    obs = fault_monitor.start_group_observers()
    threads = [threading.Thread(target=work, args=(i,), daemon=True) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    observations = obs.stop()
    done = [r for r in results if r is not None]
    # Relevé électronique + inventaire du groupe : attachés UNE fois (au premier test réellement exécuté), pas N fois
    holder = next((r for r in done if r.status != "skipped"), done[0] if done else None)
    if holder is not None:
        fault_monitor.attach_observations(holder, observations)
        for r in done:
            if r is not holder:
                r.metrics["observations_groupe"] = holder.name
    return done


# ─────────────────────────────────────────────────────────────────────────────
# État de campagne (reprise après crash)
# ─────────────────────────────────────────────────────────────────────────────

def _atomic_write(path: str, data) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, path)


def points_to_series(points: List[dict]) -> Dict[str, list]:
    """Convertit une liste de points {t, gpu0_temp…} en séries colonne (format des métriques)."""
    keys = []
    for p in points:
        for k in p:
            if k not in keys:
                keys.append(k)
    return {k: [p.get(k) for p in points] for k in keys}


class CampaignRunner:
    """Exécute une campagne avec état persistant, reprise, arrêt propre et point de contrôle."""

    def __init__(self, steps: List[Dict], run_dir: str, meta: Optional[Dict] = None,
                 defaults: Optional[Dict] = None, cooldown_s: float = 0.0, stop_on_critical: bool = True,
                 on_step_start: Optional[Callable] = None, on_step_done: Optional[Callable] = None,
                 on_progress: Optional[Callable] = None, on_series: Optional[Callable] = None,
                 suite_name: str = ""):
        self.steps = steps
        self.run_dir = run_dir
        self.meta = meta or {}
        self.defaults = defaults or {}
        self.cooldown_s = cooldown_s
        self.stop_on_critical = stop_on_critical
        self.on_step_start, self.on_step_done = on_step_start, on_step_done
        self.on_progress, self.on_series = on_progress, on_series
        self.suite_name = suite_name
        self.results: List[BenchmarkResult] = []
        self.done_idx: List[int] = []           # un index PAR résultat (zip(done_idx, results) doit rester aligné)
        self.done_pauses: List[int] = []        # pauses terminées : pas de résultat, donc listées à part
        self._cp_lock = threading.Lock()
        self.status = "new"
        self.kill_event = threading.Event()
        self._user_stop = False
        self._points: List[dict] = []
        self._last_cp = 0.0
        self._cur_index = -1
        self._cur_label = ""
        self._cur_start = 0.0
        os.makedirs(run_dir, exist_ok=True)

    # ── fichiers d'état ─────────────────────────────────────────────────────
    @property
    def state_path(self) -> str:
        return os.path.join(self.run_dir, "state.json")

    @property
    def partial_path(self) -> str:
        return os.path.join(self.run_dir, "current_partial.json")

    def _save_state(self, current: Optional[dict] = None) -> None:
        _atomic_write(self.state_path, {
            "version": STATE_VERSION, "suite": self.suite_name, "meta": self.meta, "defaults": self.defaults,
            "cooldown_s": self.cooldown_s, "stop_on_critical": self.stop_on_critical,
            "steps": self.steps, "status": self.status, "updated": datetime.now().isoformat(),
            "done": [{"index": i, "result": asdict(r)} for i, r in zip(self.done_idx, self.results)],
            "done_pauses": list(self.done_pauses),
            "current": current,
        })

    @classmethod
    def resume(cls, run_dir: str, **kw) -> "CampaignRunner":
        """Recharge une campagne interrompue ; le test en cours au moment du crash est conservé
        comme résultat partiel (« interrompu ») puis rejoué."""
        with open(os.path.join(run_dir, "state.json"), encoding="utf-8") as f:
            st = json.load(f)
        run = cls(st["steps"], run_dir, st.get("meta"), st.get("defaults"), st.get("cooldown_s", 0.0),
                  st.get("stop_on_critical", True), suite_name=st.get("suite", ""), **kw)
        for d in st.get("done", []):
            fields = {k: v for k, v in d["result"].items() if k in BenchmarkResult.__dataclass_fields__}
            run.results.append(BenchmarkResult(**fields))
            run.done_idx.append(d["index"])
        cur = st.get("current")
        if cur and os.path.exists(os.path.join(run_dir, "current_partial.json")):
            try:
                with open(os.path.join(run_dir, "current_partial.json"), encoding="utf-8") as f:
                    part = json.load(f)
                pts = part.get("points", [])
                ser = points_to_series(pts) if pts else {}
                metrics = {"series": ser, "interrompu": True, "duree_partielle_s": part.get("elapsed_s", 0)}
                from core.benchmarks import _add_gpu_series_metrics
                try:
                    _add_gpu_series_metrics(metrics, ser)
                except Exception:
                    pass
                run.results.append(BenchmarkResult(
                    name=f"{cur.get('label', 'Test')} (interrompu)", category=cur.get("category", "Autre"),
                    score=None, unit="", duration_s=part.get("elapsed_s", 0), raw_output="", metrics=metrics,
                    status="interrupted",
                    notes=f"Test interrompu (crash, coupure ou arrêt) après {part.get('elapsed_s', 0):.0f} s ; "
                          f"données partielles conservées ({len(pts)} points). Le test est rejoué ensuite."))
                run.done_idx.append(-1 - len(run.done_idx))     # index fictif : n'empêche pas de rejouer l'étape
            except Exception:
                pass
        run.done_pauses = [int(i) for i in st.get("done_pauses", [])]
        run.status = "resumed"
        return run

    # ── télémétrie / point de contrôle ──────────────────────────────────────
    def _series_hook(self, point: dict) -> None:
        now = time.time()
        with self._cp_lock:             # les threads d'un groupe parallèle appellent ce hook en même temps
            self._points.append({k: v for k, v in point.items() if k != "_src"})
            if len(self._points) > MAX_PARTIAL_POINTS:
                del self._points[: len(self._points) - MAX_PARTIAL_POINTS]
            if now - self._last_cp >= CHECKPOINT_EVERY_S:
                self._last_cp = now
                try:
                    _atomic_write(self.partial_path, {"index": self._cur_index, "label": self._cur_label,
                                                      "elapsed_s": round(now - self._cur_start, 1), "points": list(self._points)})
                except Exception:
                    pass
        if self.on_series:
            try:
                self.on_series(point)
            except Exception:
                pass

    # ── arrêt propre ────────────────────────────────────────────────────────
    def request_stop(self) -> None:
        self._user_stop = True
        self.kill_event.set()

    def install_signal_handlers(self) -> None:
        def _handler(signum, _frame):
            self.request_stop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _handler)
            except Exception:
                pass

    # ── boucle principale ───────────────────────────────────────────────────
    def run(self) -> Tuple[List[BenchmarkResult], str]:
        """Exécute les étapes restantes. Retourne (résultats, statut) : finished | aborted | fault."""
        from core.benchmarks import CATALOG
        real_done = {i for i in self.done_idx if i >= 0} | set(self.done_pauses)
        n = len(self.steps)
        self.status = "running"
        self._save_state()
        for i, step in enumerate(self.steps):
            if i in real_done:
                continue
            if self._user_stop:
                self.status = "aborted"
                break
            label = step_label(step)
            if "pause" in step:
                if self.on_step_start:
                    self.on_step_start(i, n, label, step)
                end = time.time() + float(step["pause"])
                while time.time() < end and not self._user_stop:
                    if self.on_progress:
                        self.on_progress(int(100 * (1 - (end - time.time()) / max(float(step["pause"]), 1))),
                                         f"refroidissement {int(end - time.time())} s")
                    time.sleep(1.0)
                self.done_pauses.append(i)
                self._save_state()
                continue
            if "parallel" in step:
                self._points, self._last_cp = [], time.time()
                self._cur_index, self._cur_label, self._cur_start = i, label, time.time()
                self._save_state(current={"index": i, "label": label, "category": "Groupe parallèle",
                                          "started": datetime.now().isoformat()})
                if self.on_step_start:
                    self.on_step_start(i, n, label, step)
                group = run_parallel(step, cb=self.on_progress, series_cb=self._series_hook,
                                     kill_event=self.kill_event, defaults=self.defaults)
                for res in group:
                    self.results.append(res)
                    self.done_idx.append(i)
                try:
                    if os.path.exists(self.partial_path):
                        os.remove(self.partial_path)
                except Exception:
                    pass
                self._save_state()
                for res in group:
                    if self.on_step_done:
                        self.on_step_done(i, n, res)
                fault = any((r.metrics or {}).get("hardware_fault_critical") for r in group)
                if self._user_stop:
                    self.status = "aborted"
                    break
                if fault:
                    if self.stop_on_critical:
                        self.status = "fault"
                        break
                    self.kill_event.clear()
                if self.cooldown_s and i < n - 1 and not self._user_stop:
                    end = time.time() + self.cooldown_s
                    while time.time() < end and not self._user_stop:
                        if self.on_progress:
                            self.on_progress(int(100 * (1 - (end - time.time()) / self.cooldown_s)),
                                             f"refroidissement {int(end - time.time())} s")
                        time.sleep(1.0)
                continue
            category = "Autre"
            if "bench" in step:
                category = next((b["category"] for b in CATALOG if b["id"] == step["bench"]), "Autre")
            elif "stress" in step:
                category = "CPU + RAM"
            elif "fio" in step:
                category = "Stockage"
            self._points, self._last_cp = [], time.time()
            self._cur_index, self._cur_label, self._cur_start = i, label, time.time()
            self._save_state(current={"index": i, "label": label, "category": category,
                                      "started": datetime.now().isoformat()})
            if self.on_step_start:
                self.on_step_start(i, n, label, step)
            res = run_step(step, cb=self.on_progress, series_cb=self._series_hook,
                           kill_event=self.kill_event, defaults=self.defaults)
            self.results.append(res)
            self.done_idx.append(i)
            try:
                if os.path.exists(self.partial_path):
                    os.remove(self.partial_path)
            except Exception:
                pass
            self._save_state()
            if self.on_step_done:
                self.on_step_done(i, n, res)
            fault = bool(res.metrics.get("hardware_fault_critical"))
            if self._user_stop:
                self.status = "aborted"
                break
            if fault:
                if self.stop_on_critical:
                    self.status = "fault"
                    break
                self.kill_event.clear()     # le moniteur avait interrompu ce test ; on continue la campagne
            if self.cooldown_s and i < n - 1 and not self._user_stop:
                end = time.time() + self.cooldown_s
                while time.time() < end and not self._user_stop:
                    if self.on_progress:
                        self.on_progress(int(100 * (1 - (end - time.time()) / self.cooldown_s)),
                                         f"refroidissement {int(end - time.time())} s")
                    time.sleep(1.0)
        else:
            self.status = "finished"
        if self.status == "running":
            self.status = "finished"
        self._save_state()
        return self.results, self.status
