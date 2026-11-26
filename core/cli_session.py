"""Session CLI : tous les résultats d'un même démarrage de la machine s'ajoutent au MÊME rapport (mêmes fichiers).

Avant : chaque lancement de `./cli.py` créait un nouveau dossier et de nouveaux fichiers horodatés. Maintenant, tant que la machine n'a pas
redémarré (même `boot_id` noyau), les résultats de chaque lancement sont ajoutés à ceux des lancements précédents et le rapport
(`benchsuite_report.html / .pdf / .json / .csv`) est régénéré au même endroit :

    <sortie>/<client>/<machine>/session_<AAAAMMJJ_HHMMSS du démarrage>/
        benchsuite_report.{html,pdf,json,csv}   rapport unique, mis à jour à chaque lancement
        session.json                            résultats cumulés + inventaire de départ de la session
        runs/<horodatage>_<séquence>/           détail de chaque lancement (état, reprise, inventaires)

Un redémarrage de la machine, un autre client / numéro de série ou `--new-report` ouvre une nouvelle session.
Best-effort : un `session.json` illisible repart simplement d'une session vide (rien n'est perdu côté `runs/`).
"""
import json
import os
import time
from datetime import datetime
from typing import Dict, Optional

SESSION_FILE = "session.json"
SESSION_VERSION = 1
REPORT_BASENAME = "benchsuite_report"


def boot_id() -> str:
    """Identifiant du démarrage courant (noyau) ; repli : heure de démarrage arrondie à la minute."""
    try:
        with open("/proc/sys/kernel/random/boot_id") as f:
            v = f.read().strip()
        if v:
            return v
    except Exception:
        pass
    return f"boot-{int(boot_start().timestamp()) // 60}"


def boot_start() -> datetime:
    """Date / heure de démarrage de la machine (psutil, sinon /proc/uptime)."""
    try:
        import psutil
        return datetime.fromtimestamp(psutil.boot_time())
    except Exception:
        pass
    try:
        with open("/proc/uptime") as f:
            return datetime.fromtimestamp(time.time() - float(f.read().split()[0]))
    except Exception:
        return datetime.now()


def session_dir(out_dir: str, meta: dict) -> str:
    """Dossier de la session : <sortie>/<client>/<machine>/BenchSuite_v<ver>_<démarrage>_<serveur>_session."""
    from exporters.common import _safe_folder_part, format_report_dir_name
    parts = [out_dir]
    if meta.get("client"):
        parts.append(_safe_folder_part(meta["client"]))
    parts.append(_safe_folder_part(meta.get("serial") or meta.get("system_name") or "machine"))
    parts.append(format_report_dir_name(meta, suffix="session", dt=boot_start()))
    return os.path.join(*parts)


def load(sdir: str) -> Dict:
    """Session enregistrée dans `sdir` ({} si absente, illisible ou d'un autre démarrage de la machine)."""
    try:
        with open(os.path.join(sdir, SESSION_FILE), encoding="utf-8") as f:
            data = json.load(f)
        if data.get("version") == SESSION_VERSION and data.get("boot_id") == boot_id():
            return data
    except Exception:
        pass
    return {}


def results_of(session: Dict) -> list:
    """Résultats cumulés de la session, reconstruits en BenchmarkResult."""
    from core.result import BenchmarkResult
    out = []
    for d in (session or {}).get("results", []):
        try:
            fields = {k: v for k, v in d.items() if k in BenchmarkResult.__dataclass_fields__}
            out.append(BenchmarkResult(**fields))
        except Exception:
            continue
    return out


def save(sdir: str, results: list, inventory_before: Optional[dict], run_info: dict, previous: Dict = None) -> None:
    """Enregistre (écriture atomique) les résultats cumulés, l'inventaire de départ de la session et l'historique des lancements."""
    from dataclasses import asdict
    os.makedirs(sdir, exist_ok=True)
    prev = previous or {}
    data = {"version": SESSION_VERSION, "boot_id": boot_id(), "boot_start": boot_start().isoformat(),
            "created": prev.get("created") or datetime.now().isoformat(), "updated": datetime.now().isoformat(),
            "inventory_before": (prev.get("inventory_before") or inventory_before),
            "runs": list(prev.get("runs", [])) + ([run_info] if run_info else []),
            "results": [asdict(r) for r in results]}
    path = os.path.join(sdir, SESSION_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, default=str)
    os.replace(tmp, path)


def fixed_report_name(path: str) -> str:
    """`…/benchsuite_report_20261104_153000.html` → `…/benchsuite_report.html` (même nom à chaque régénération)."""
    ext = os.path.splitext(path)[1]
    return os.path.join(os.path.dirname(path), REPORT_BASENAME + ext)
