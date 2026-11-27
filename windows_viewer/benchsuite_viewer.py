#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
windows_viewer/benchsuite_viewer.py — Visionneuse et base SQLite locale pour Windows/Linux.

Scanne un dossier unique contenant les rapports BenchSuite (.json, .html, .pdf, .csv,
dmesg_*.txt, fleet_summary.*, state.json...), alimente une base SQLite (benchsuite.db)
sans dépendance externe (bibliothèque standard Python 3.10+ uniquement) et ouvre une
interface web locale sur 127.0.0.1 pour consulter, filtrer, comparer et ouvrir tous les fichiers.
"""

import argparse
import contextlib
import csv
import datetime as _dt
import hashlib
import html as _html
import io
import json
import mimetypes
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


VIEWER_VERSION = "1.0.22"
DB_SCHEMA_VERSION = 1
IGNORED_FILES = {"benchsuite.db", "benchsuite.db-wal", "benchsuite.db-shm", "benchsuite.db-journal"}
IGNORED_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}

# Motifs d'erreurs critiques/avertissements pour le surlignage des journaux dmesg
DMESG_ERR_RE = re.compile(
    r"(NVRM:\s*Xid|MCE|Hardware Error|AER:|PCIe Bus Error|ECC|Uncorrected|"
    r"nvme\d*.*(?:error|timeout|reset|I/O)|I/O error,\s*dev|"
    r"Out of memory|oom-killer|Call Trace|BUG:|Oops:|panic|EDAC|"
    r"thermal throttling|critical temperature|segfault)",
    re.IGNORECASE,
)


def _norm_rel(path: Path, root: Path) -> str:
    """Chemin relatif POSIX (avec /) pour stockage portable Windows/Linux en BDD."""
    return path.resolve().relative_to(root.resolve()).as_posix()


def _safe_resolve(root: Path, rel_path: str) -> Optional[Path]:
    """Résout un chemin relatif en garantissant qu'il reste strictement dans `root`."""
    if not rel_path or "\x00" in rel_path:
        return None
    clean = rel_path.replace("\\", "/").lstrip("/")
    try:
        root_res = root.resolve()
        target = (root_res / clean).resolve()
        if target == root_res or not target.is_relative_to(root_res):
            return None
        if not target.is_file():
            return None
        return target
    except Exception:
        return None


def _read_text_auto(path: Path, max_bytes: int = 2_000_000) -> Tuple[str, str]:
    """Lit un fichier texte avec détection d'encodage (UTF-8, BOM, UTF-16, Windows-1252)."""
    with open(path, "rb") as f:
        raw = f.read(max_bytes)
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")) or (b"\x00" in raw[:512]):
        try:
            return raw.decode("utf-16"), "utf-16"
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace"), "utf-8(lossy)"


def _parse_csv_preview(path: Path, max_rows: int = 300) -> Dict[str, Any]:
    """Lit et normalise un CSV BenchSuite/Windows (détection séparateur et encodage)."""
    text, enc = _read_text_auto(path, max_bytes=1_500_000)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"encoding": enc, "delimiter": ",", "headers": [], "rows": [], "truncated": False}
    sample = "\n".join(lines[:20])
    delim = ","
    for cand in (";", ",", "\t", "|"):
        counts = [ln.count(cand) for ln in lines[:10] if ln.strip()]
        if counts and min(counts) >= 1:
            delim = cand
            break
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delim = dialect.delimiter
    except Exception:
        pass
    reader = csv.reader(io.StringIO("\n".join(lines)), delimiter=delim)
    all_rows = []
    truncated = False
    for idx, row in enumerate(reader):
        if idx > max_rows:
            truncated = True
            break
        all_rows.append(row)
    headers = all_rows[0] if all_rows else []
    body = all_rows[1:] if len(all_rows) > 1 else []
    return {"encoding": enc, "delimiter": delim, "headers": headers, "rows": body, "truncated": truncated}


def _classify_file(path: Path, is_report_json: Optional[bool] = None) -> str:
    """Classe un fichier généré par BenchSuite selon son nom et son extension."""
    name = path.name.lower()
    ext = path.suffix.lower()
    if name.startswith("fleet_summary"):
        return "fleet"
    if name.startswith("dmesg") and ext in (".txt", ".log", ""):
        return "dmesg"
    if name in ("state.json", "benchsuite_history.json") or "history" in name and ext == ".json":
        return "session_state"
    if ext == ".json":
        if is_report_json is True:
            return "report_json"
        if is_report_json is False:
            return "json_other"
        return "report_json" if name.startswith("benchsuite_report") else "json_other"
    if ext in (".html", ".htm"):
        return "report_html"
    if ext == ".pdf":
        return "report_pdf"
    if ext == ".csv":
        return "report_csv"
    if ext in (".txt", ".log"):
        return "log_text"
    return "other"


def _is_benchsuite_report_json(data: Any) -> bool:
    """Vérifie qu'un objet JSON correspond à un rapport BenchSuite (actuel ou ancien)."""
    if not isinstance(data, dict):
        return False
    return (
        "results" in data and isinstance(data.get("results"), list)
        and ("benchsuite_version" in data or "system" in data or "summary" in data or "verdict" in data)
    )


def _extract_gpu_metrics(data: dict) -> Dict[str, Any]:
    """Extrait les métriques GPU/thermiques clés en tolérant les anciens schémas."""
    snap = ((data.get("system") or {}).get("snapshot_at_start") or {})
    gpu_list = snap.get("gpus") or snap.get("gpu") or []
    if not isinstance(gpu_list, list):
        gpu_list = []
    compute_gpus = [
        g for g in gpu_list
        if isinstance(g, dict) and not any(
            x in str(g.get("nom") or g.get("modele") or "").lower()
            for x in ("aspeed", "matrox", "vga compatible")
        )
    ]

    tel_gpus = ((data.get("telemetry_summary") or {}).get("gpus") or [])
    core_vals = [float(g["temp_core_max_c"]) for g in tel_gpus if isinstance(g, dict) and g.get("temp_core_max_c") is not None]
    hs_vals = [float(g["temp_hotspot_max_c"]) for g in tel_gpus if isinstance(g, dict) and g.get("temp_hotspot_max_c") is not None]
    throttled = any(bool(g.get("throttle_detected")) for g in tel_gpus if isinstance(g, dict))

    # Repli sur les résultats individuels si telemetry_summary est absent (anciens rapports)
    if not core_vals or not throttled:
        for r in data.get("results", []):
            if not isinstance(r, dict):
                continue
            m = r.get("metrics") or {}
            for k, v in m.items():
                if not isinstance(v, (int, float)):
                    continue
                lk = str(k).lower()
                if lk.endswith("temp_max_c") or lk in ("gpu_temp_max_c", "temp_gpu_max_c"):
                    core_vals.append(float(v))
                elif "hotspot" in lk and "max" in lk:
                    hs_vals.append(float(v))
                elif "throttle" in lk and "sample" in lk and v > 0:
                    throttled = True
            if m.get("throttle_detected"):
                throttled = True

    return {
        "gpu_count": max(len(compute_gpus), len(tel_gpus)),
        "gpu_temp_max_c": round(max(core_vals), 1) if core_vals else None,
        "gpu_hotspot_max_c": round(max(hs_vals), 1) if hs_vals else None,
        "throttle_detected": 1 if throttled else 0,
    }


def _extract_report_summary(path: Path, root: Path, raw_bytes: bytes) -> Optional[Dict[str, Any]]:
    """Parse un rapport JSON BenchSuite et extrait les champs indexables en SQLite."""
    try:
        data = json.loads(raw_bytes.decode("utf-8", errors="replace"))
    except Exception:
        return None
    if not _is_benchsuite_report_json(data):
        return None

    meta = data.get("metadata") or {}
    sys_blk = data.get("system") or {}
    snap = sys_blk.get("snapshot_at_start") or {}
    cpu_blk = snap.get("cpu") or {}
    ram_blk = snap.get("ram") or {}
    os_blk = snap.get("os") or {}
    verdict = data.get("verdict") or {}
    summary = data.get("summary") or {}
    results = [r for r in (data.get("results") or []) if isinstance(r, dict)]

    crit_faults = 0
    warn_faults = 0
    pcie_anomalies = 0
    inv_diffs = 0
    alerts = []

    for r in verdict.get("rules") or []:
        if isinstance(r, dict) and r.get("level") in ("fail", "warn"):
            alerts.append({"source": "verdict", "level": r.get("level"), "label": r.get("label", ""), "detail": r.get("detail", "")})

    camp = data.get("inventory_campaign")
    if isinstance(camp, dict):
        findings = camp.get("findings") or []
        sel_new = camp.get("sel_new") or []
        inv_diffs += len(findings) + len(sel_new)
        for fnd in findings:
            if isinstance(fnd, dict):
                alerts.append({"source": "inventaire", "level": fnd.get("level", "warn"), "label": fnd.get("label", ""), "detail": fnd.get("detail", "")})

    benches = []
    for r in results:
        m = r.get("metrics") or {}
        evs = m.get("hardware_events") if isinstance(m.get("hardware_events"), list) else []
        r_crit = sum(1 for e in evs if isinstance(e, dict) and e.get("severity") == "critical")
        r_warn = sum(1 for e in evs if isinstance(e, dict) and e.get("severity") != "critical")
        crit_faults += r_crit
        warn_faults += r_warn

        pl = m.get("pcie_links") if isinstance(m.get("pcie_links"), dict) else {}
        for dev in pl.get("periphériques") or pl.get("peripheriques") or []:
            if isinstance(dev, dict) and dev.get("anomalies"):
                pcie_anomalies += len(dev["anomalies"])

        idiff = m.get("inventory_diff") if isinstance(m.get("inventory_diff"), dict) else {}
        if idiff:
            inv_diffs += len(idiff.get("findings") or []) + len(idiff.get("sel_new") or [])

        benches.append({
            "bench_name": str(r.get("name") or ""),
            "category": str(r.get("category") or ""),
            "status": str(r.get("status") or ""),
            "score": float(r["score"]) if isinstance(r.get("score"), (int, float)) else None,
            "unit": str(r.get("unit") or ""),
            "duration_s": float(r["duration_s"]) if isinstance(r.get("duration_s"), (int, float)) else None,
            "timestamp": str(r.get("timestamp") or ""),
            "notes": str(r.get("notes") or ""),
            "command": str(m.get("command") or ""),
            "fault_count": r_crit + r_warn,
        })

    gpu_info = _extract_gpu_metrics(data)
    incomplete_list = snap.get("sections_incompletes") or []
    st = path.stat()

    ram_val = ram_blk.get("total_go") or ram_blk.get("total_gb")
    try:
        ram_gb = round(float(ram_val), 1) if ram_val is not None else None
    except (TypeError, ValueError):
        ram_gb = None

    return {
        "rel_path": _norm_rel(path, root),
        "dir_rel_path": _norm_rel(path.parent, root) if path.parent.resolve() != root.resolve() else ".",
        "file_name": path.name,
        "file_size": st.st_size,
        "mtime": st.st_mtime,
        "sha1": hashlib.sha1(raw_bytes).hexdigest(),
        "exported_at": str(data.get("exported_at") or ""),
        "benchsuite_version": str(data.get("benchsuite_version") or ""),
        "system_name": str(meta.get("system_name") or sys_blk.get("name") or path.parent.name),
        "model": str(meta.get("model") or ""),
        "serial": str(meta.get("serial") or ""),
        "order_no": str(meta.get("order_no") or ""),
        "operator": str(meta.get("operator") or ""),
        "client": str(meta.get("client") or ""),
        "ambient_temp": str(meta.get("ambient_temp") or ""),
        "fan_speed": str(meta.get("fan_speed") or ""),
        "verdict_status": str(verdict.get("status") or ("NON_EVALUE" if results else "INFO_SEULE")),
        "verdict_label": str(verdict.get("label") or ("Non évalué" if results else "Infos système")),
        "critical_faults_count": crit_faults,
        "warn_faults_count": warn_faults,
        "bench_count": int(summary.get("bench_count") if summary.get("bench_count") is not None else len(results)),
        "ok_count": int(summary.get("ok") or sum(1 for r in results if r.get("status") == "ok")),
        "failed_count": int(summary.get("failed") or sum(1 for r in results if r.get("status") in ("error", "failed"))),
        "stopped_count": int(summary.get("stopped") or sum(1 for r in results if r.get("status") == "stopped")),
        "skipped_count": int(summary.get("skipped") or sum(1 for r in results if r.get("status") == "skipped")),
        "cpu_model": str(cpu_blk.get("modele") or cpu_blk.get("model") or ""),
        "ram_total_gb": ram_gb,
        "os_info": str(os_blk.get("distro") or os_blk.get("os") or os_blk.get("nom") or ""),
        "gpu_count": gpu_info["gpu_count"],
        "gpu_temp_max_c": gpu_info["gpu_temp_max_c"],
        "gpu_hotspot_max_c": gpu_info["gpu_hotspot_max_c"],
        "throttle_detected": gpu_info["throttle_detected"],
        "pcie_anomalies_count": pcie_anomalies,
        "inventory_diffs_count": inv_diffs,
        "incomplete_sections_count": len(incomplete_list) if isinstance(incomplete_list, list) else 0,
        "alerts_summary": json.dumps(alerts[:30], ensure_ascii=False),
        "benchmarks": benches,
    }


def _dmesg_error_count(path: Path) -> int:
    """Compte rapidement les erreurs dans un fichier dmesg_*.txt."""
    try:
        text, _ = _read_text_auto(path, max_bytes=1_000_000)
    except Exception:
        return 0
    m = re.search(r"Erreurs dmesg detectees:\s*(\d+)", text)
    if m:
        try:
            return int(m.group(1))
        except ValueError:
            pass
    return sum(1 for ln in text.splitlines() if DMESG_ERR_RE.search(ln))


class BenchSuiteDB:
    """Gestion de la base SQLite locale `benchsuite.db` (indexation incrémentale)."""

    def __init__(self, root_dir: Path, db_path: Optional[Path] = None):
        self.root_dir = root_dir.resolve()
        self.db_path = (db_path or (self.root_dir / "benchsuite.db")).resolve()
        self._lock = threading.Lock()
        self._init_schema()

    @contextlib.contextmanager
    def _connect(self):
        conn = sqlite3.connect(str(self.db_path), timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta_db (
                    key TEXT PRIMARY KEY,
                    value TEXT
                );
                CREATE TABLE IF NOT EXISTS reports (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rel_path TEXT UNIQUE NOT NULL,
                    dir_rel_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    sha1 TEXT NOT NULL,
                    exported_at TEXT,
                    benchsuite_version TEXT,
                    system_name TEXT,
                    model TEXT,
                    serial TEXT,
                    order_no TEXT,
                    operator TEXT,
                    client TEXT,
                    ambient_temp TEXT,
                    fan_speed TEXT,
                    verdict_status TEXT,
                    verdict_label TEXT,
                    critical_faults_count INTEGER DEFAULT 0,
                    warn_faults_count INTEGER DEFAULT 0,
                    bench_count INTEGER DEFAULT 0,
                    ok_count INTEGER DEFAULT 0,
                    failed_count INTEGER DEFAULT 0,
                    stopped_count INTEGER DEFAULT 0,
                    skipped_count INTEGER DEFAULT 0,
                    cpu_model TEXT,
                    ram_total_gb REAL,
                    os_info TEXT,
                    gpu_count INTEGER DEFAULT 0,
                    gpu_temp_max_c REAL,
                    gpu_hotspot_max_c REAL,
                    throttle_detected INTEGER DEFAULT 0,
                    pcie_anomalies_count INTEGER DEFAULT 0,
                    inventory_diffs_count INTEGER DEFAULT 0,
                    incomplete_sections_count INTEGER DEFAULT 0,
                    dmesg_rel_path TEXT,
                    html_rel_path TEXT,
                    pdf_rel_path TEXT,
                    csv_rel_path TEXT,
                    alerts_summary TEXT
                );
                CREATE TABLE IF NOT EXISTS benchmarks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                    bench_name TEXT,
                    category TEXT,
                    status TEXT,
                    score REAL,
                    unit TEXT,
                    duration_s REAL,
                    timestamp TEXT,
                    notes TEXT,
                    command TEXT,
                    fault_count INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS files (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    rel_path TEXT UNIQUE NOT NULL,
                    dir_rel_path TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    ext TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    mtime REAL NOT NULL,
                    report_id INTEGER REFERENCES reports(id) ON DELETE SET NULL,
                    dmesg_error_count INTEGER DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_reports_verdict ON reports(verdict_status);
                CREATE INDEX IF NOT EXISTS idx_reports_serial ON reports(serial);
                CREATE INDEX IF NOT EXISTS idx_reports_client ON reports(client);
                CREATE INDEX IF NOT EXISTS idx_reports_exported ON reports(exported_at);
                CREATE INDEX IF NOT EXISTS idx_benchmarks_report ON benchmarks(report_id);
                CREATE INDEX IF NOT EXISTS idx_files_kind ON files(kind);
                CREATE INDEX IF NOT EXISTS idx_files_dir ON files(dir_rel_path);
                """
            )
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('schema_version', ?)", (str(DB_SCHEMA_VERSION),))
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('viewer_version', ?)", (VIEWER_VERSION,))
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('root_dir', ?)", (str(self.root_dir),))

    def scan(self) -> Dict[str, Any]:
        """Scanne récursivement `root_dir` et met à jour la BDD SQLite de façon incrémentale."""
        t0 = time.monotonic()
        discovered_files: List[Path] = []
        for dirpath, dirnames, filenames in os.walk(self.root_dir):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS and not d.startswith(".")]
            for fname in filenames:
                if fname in IGNORED_FILES or fname.startswith("."):
                    continue
                p = Path(dirpath) / fname
                if p.resolve() == self.db_path:
                    continue
                discovered_files.append(p)

        seen_rels = set()
        updated_reports = 0
        updated_files = 0
        skipped_corrupt = []

        with self._lock, self._connect() as conn:
            existing_reports = {
                row["rel_path"]: (row["id"], row["file_size"], row["mtime"])
                for row in conn.execute("SELECT id, rel_path, file_size, mtime FROM reports")
            }
            existing_files = {
                row["rel_path"]: (row["id"], row["size"], row["mtime"], row["kind"])
                for row in conn.execute("SELECT id, rel_path, size, mtime, kind FROM files")
            }

            for p in discovered_files:
                try:
                    st = p.stat()
                    rel = _norm_rel(p, self.root_dir)
                except OSError:
                    continue
                seen_rels.add(rel)
                dir_rel = _norm_rel(p.parent, self.root_dir) if p.parent.resolve() != self.root_dir else "."
                ext = p.suffix.lower()

                rep_cached = existing_reports.get(rel)
                file_cached = existing_files.get(rel)
                same_file = bool(file_cached and file_cached[1] == st.st_size and abs(file_cached[2] - st.st_mtime) < 1e-6)
                same_rep = bool(rep_cached and rep_cached[1] == st.st_size and abs(rep_cached[2] - st.st_mtime) < 1e-6)

                if same_file and (ext != ".json" or same_rep or file_cached[3] != "report_json"):
                    continue

                is_rep = None
                dmesg_errs = 0
                if ext == ".json" and st.st_size <= 150_000_000:
                    try:
                        raw = p.read_bytes()
                    except OSError:
                        raw = b""
                    rec = _extract_report_summary(p, self.root_dir, raw) if raw else None
                    if rec is not None:
                        is_rep = True
                        updated_reports += 1
                        conn.execute(
                            """
                            INSERT INTO reports (
                                rel_path, dir_rel_path, file_name, file_size, mtime, sha1,
                                exported_at, benchsuite_version, system_name, model, serial, order_no,
                                operator, client, ambient_temp, fan_speed, verdict_status, verdict_label,
                                critical_faults_count, warn_faults_count, bench_count, ok_count,
                                failed_count, stopped_count, skipped_count, cpu_model, ram_total_gb,
                                os_info, gpu_count, gpu_temp_max_c, gpu_hotspot_max_c, throttle_detected,
                                pcie_anomalies_count, inventory_diffs_count, incomplete_sections_count,
                                alerts_summary
                            ) VALUES (
                                :rel_path, :dir_rel_path, :file_name, :file_size, :mtime, :sha1,
                                :exported_at, :benchsuite_version, :system_name, :model, :serial, :order_no,
                                :operator, :client, :ambient_temp, :fan_speed, :verdict_status, :verdict_label,
                                :critical_faults_count, :warn_faults_count, :bench_count, :ok_count,
                                :failed_count, :stopped_count, :skipped_count, :cpu_model, :ram_total_gb,
                                :os_info, :gpu_count, :gpu_temp_max_c, :gpu_hotspot_max_c, :throttle_detected,
                                :pcie_anomalies_count, :inventory_diffs_count, :incomplete_sections_count,
                                :alerts_summary
                            )
                            ON CONFLICT(rel_path) DO UPDATE SET
                                dir_rel_path=excluded.dir_rel_path, file_name=excluded.file_name,
                                file_size=excluded.file_size, mtime=excluded.mtime, sha1=excluded.sha1,
                                exported_at=excluded.exported_at, benchsuite_version=excluded.benchsuite_version,
                                system_name=excluded.system_name, model=excluded.model, serial=excluded.serial,
                                order_no=excluded.order_no, operator=excluded.operator, client=excluded.client,
                                ambient_temp=excluded.ambient_temp, fan_speed=excluded.fan_speed,
                                verdict_status=excluded.verdict_status, verdict_label=excluded.verdict_label,
                                critical_faults_count=excluded.critical_faults_count,
                                warn_faults_count=excluded.warn_faults_count, bench_count=excluded.bench_count,
                                ok_count=excluded.ok_count, failed_count=excluded.failed_count,
                                stopped_count=excluded.stopped_count, skipped_count=excluded.skipped_count,
                                cpu_model=excluded.cpu_model, ram_total_gb=excluded.ram_total_gb,
                                os_info=excluded.os_info, gpu_count=excluded.gpu_count,
                                gpu_temp_max_c=excluded.gpu_temp_max_c,
                                gpu_hotspot_max_c=excluded.gpu_hotspot_max_c,
                                throttle_detected=excluded.throttle_detected,
                                pcie_anomalies_count=excluded.pcie_anomalies_count,
                                inventory_diffs_count=excluded.inventory_diffs_count,
                                incomplete_sections_count=excluded.incomplete_sections_count,
                                alerts_summary=excluded.alerts_summary
                            """,
                            rec,
                        )
                        rid = conn.execute("SELECT id FROM reports WHERE rel_path = ?", (rel,)).fetchone()["id"]
                        conn.execute("DELETE FROM benchmarks WHERE report_id = ?", (rid,))
                        for b in rec["benchmarks"]:
                            conn.execute(
                                """
                                INSERT INTO benchmarks (
                                    report_id, bench_name, category, status, score, unit,
                                    duration_s, timestamp, notes, command, fault_count
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """,
                                (
                                    rid, b["bench_name"], b["category"], b["status"], b["score"],
                                    b["unit"], b["duration_s"], b["timestamp"], b["notes"],
                                    b["command"], b["fault_count"],
                                ),
                            )
                    else:
                        is_rep = False
                        if p.name.lower().startswith("benchsuite_report"):
                            skipped_corrupt.append(rel)

                kind = _classify_file(p, is_report_json=is_rep)
                if kind == "dmesg":
                    dmesg_errs = _dmesg_error_count(p)

                conn.execute(
                    """
                    INSERT INTO files (rel_path, dir_rel_path, file_name, ext, kind, size, mtime, dmesg_error_count)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(rel_path) DO UPDATE SET
                        dir_rel_path=excluded.dir_rel_path, file_name=excluded.file_name,
                        ext=excluded.ext, kind=excluded.kind, size=excluded.size,
                        mtime=excluded.mtime, dmesg_error_count=excluded.dmesg_error_count
                    """,
                    (rel, dir_rel, p.name, ext, kind, st.st_size, st.st_mtime, dmesg_errs),
                )
                updated_files += 1

            # Supprimer les entrées disparues du disque
            removed_reports = 0
            for rel in list(existing_reports):
                if rel not in seen_rels:
                    conn.execute("DELETE FROM reports WHERE rel_path = ?", (rel,))
                    removed_reports += 1
            for rel in list(existing_files):
                if rel not in seen_rels:
                    conn.execute("DELETE FROM files WHERE rel_path = ?", (rel,))

            # Associer à chaque rapport ses fichiers compagnons (.html, .pdf, .csv, dmesg) du même dossier
            conn.execute("UPDATE files SET report_id = NULL")
            reports_rows = conn.execute(
                "SELECT id, rel_path, dir_rel_path, file_name, system_name, serial FROM reports ORDER BY exported_at DESC, mtime DESC"
            ).fetchall()
            files_by_dir: Dict[str, List[sqlite3.Row]] = {}
            for frow in conn.execute("SELECT id, rel_path, dir_rel_path, file_name, kind FROM files ORDER BY mtime DESC"):
                files_by_dir.setdefault(frow["dir_rel_path"], []).append(frow)

            # Compter le nombre de RAPPORTS distincts par dossier (et non de n° de série,
            # car un même châssis SN-CHASSIS-01 peut avoir plusieurs rapports différents dans le dossier)
            dir_reports_count: Dict[str, int] = {}
            dir_single_rid: Dict[str, int] = {}
            for rrow in reports_rows:
                drel = rrow["dir_rel_path"]
                dir_reports_count[drel] = dir_reports_count.get(drel, 0) + 1
                dir_single_rid[drel] = rrow["id"]

            for rrow in reports_rows:
                rid = rrow["id"]
                drel = rrow["dir_rel_path"]
                dfiles = files_by_dir.get(drel, [])
                stem = Path(rrow["file_name"]).stem  # ex: benchsuite_report_20261116_120000
                ts_suffix = stem.replace("benchsuite_report", "", 1)
                is_single_report_dir = (dir_reports_count.get(drel, 0) == 1)

                def _pick(target_kind: str) -> Optional[str]:
                    cands = [f["rel_path"] for f in dfiles if f["kind"] == target_kind]
                    if not cands:
                        return None
                    # 1. Même préfixe exact (ex. benchsuite_report.json <-> benchsuite_report.html)
                    for c in cands:
                        if Path(c).stem == stem:
                            return c
                    # 2. Même suffixe horaire (ex. _20261116_120000)
                    if ts_suffix:
                        for c in cands:
                            if ts_suffix in Path(c).name:
                                return c
                    # 3. Repli uniquement si le dossier ne contient qu'un seul rapport
                    if is_single_report_dir:
                        return cands[0]
                    return None

                html_rel = _pick("report_html")
                pdf_rel = _pick("report_pdf")
                csv_rel = _pick("report_csv")
                dmesg_rel = _pick("dmesg")
                conn.execute(
                    """
                    UPDATE reports SET html_rel_path=?, pdf_rel_path=?, csv_rel_path=?, dmesg_rel_path=?
                    WHERE id=?
                    """,
                    (html_rel, pdf_rel, csv_rel, dmesg_rel, rid),
                )
                exact_rels = {rrow["rel_path"]}
                for rel_comp in (html_rel, pdf_rel, csv_rel, dmesg_rel):
                    if rel_comp:
                        exact_rels.add(rel_comp)
                for f in dfiles:
                    fname = f["file_name"]
                    match_ts = bool(ts_suffix and len(ts_suffix) >= 4 and ts_suffix in fname)
                    if f["rel_path"] in exact_rels or match_ts:
                        conn.execute("UPDATE files SET report_id=? WHERE id=? AND report_id IS NULL", (rid, f["id"]))

            # Les fichiers annexes (state.json, logs...) d'un dossier ne contenant qu'un seul rapport lui sont rattachés
            for drel, cnt in dir_reports_count.items():
                if cnt == 1 and drel in dir_single_rid:
                    conn.execute(
                        "UPDATE files SET report_id=? WHERE dir_rel_path=? AND report_id IS NULL",
                        (dir_single_rid[drel], drel),
                    )

            now_iso = _dt.datetime.now().isoformat(timespec="seconds")
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('last_scan', ?)", (now_iso,))
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('viewer_version', ?)", (VIEWER_VERSION,))
            conn.execute("INSERT OR REPLACE INTO meta_db(key, value) VALUES('schema_version', ?)", (str(DB_SCHEMA_VERSION),))

        return {
            "last_scan": now_iso,
            "duration_ms": int((time.monotonic() - t0) * 1000),
            "updated_reports": updated_reports,
            "updated_files": updated_files,
            "removed_reports": removed_reports,
            "skipped_corrupt": skipped_corrupt,
        }

    @staticmethod
    def _machine_key(r: Dict[str, Any]) -> str:
        """Identifiant d'une machine (n° de série si connu, sinon nom système, sinon dossier)."""
        return (
            str(r.get("serial") or "").strip()
            or str(r.get("system_name") or "").strip()
            or str(r.get("dir_rel_path") or "").strip()
            or str(r.get("rel_path") or "")
        )

    def _machine_files_map(self, reports: List[Dict[str, Any]], files: List[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
        """Associe chaque fichier à sa machine sans jamais mélanger deux machines distinctes."""
        rid_to_mkey = {r["id"]: self._machine_key(r) for r in reports}
        dir_to_mkeys: Dict[str, set] = {}
        for r in reports:
            dir_to_mkeys.setdefault(r["dir_rel_path"], set()).add(self._machine_key(r))

        out: Dict[str, List[Dict[str, Any]]] = {}
        for f in files:
            mkey = rid_to_mkey.get(f.get("report_id"))
            if not mkey:
                d_keys = dir_to_mkeys.get(f.get("dir_rel_path"), set())
                if len(d_keys) == 1:
                    mkey = next(iter(d_keys))
            f["machine_key"] = mkey or ""
            if mkey:
                out.setdefault(mkey, []).append(f)
        return out

    def get_overview(self, q: str = "", verdict: str = "", anomaly: str = "") -> Dict[str, Any]:
        """Retourne les KPI, chaque rapport individuel (1 ligne = 1 rapport), et tous les fichiers du dossier."""
        with self._lock, self._connect() as conn:
            reports = [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY exported_at DESC, mtime DESC")]
            files = [dict(f) for f in conn.execute("SELECT * FROM files ORDER BY mtime DESC, file_name ASC")]
            meta = {r["key"]: r["value"] for r in conn.execute("SELECT key, value FROM meta_db")}

        mfiles_map = self._machine_files_map(reports, files)
        reports_by_id: Dict[int, Dict[str, Any]] = {}
        files_by_rid: Dict[int, List[Dict[str, Any]]] = {}
        mkey_to_name: Dict[str, str] = {}
        for r in reports:
            rid = int(r["id"])
            reports_by_id[rid] = r
            mk = self._machine_key(r)
            r["machine_key"] = mk
            mkey_to_name.setdefault(mk, r.get("system_name") or mk)

        for f in files:
            rid = f.get("report_id")
            if rid is not None:
                files_by_rid.setdefault(int(rid), []).append(f)
                rep = reports_by_id.get(int(rid))
                f["report_name"] = (rep.get("system_name") or rep.get("file_name") or "") if rep else ""
                f["report_exported_at"] = (rep.get("exported_at") or "") if rep else ""
            else:
                f["report_name"] = ""
                f["report_exported_at"] = ""
            f["machine_name"] = mkey_to_name.get(f.get("machine_key") or "", "")

        for r in reports:
            r["file_count"] = len(files_by_rid.get(int(r["id"]), []))

        total_reports = len(reports)
        non_conf = sum(1 for r in reports if r["verdict_status"] == "NON_CONFORME" or r["critical_faults_count"] > 0)
        to_check = sum(1 for r in reports if r["verdict_status"] == "A_VERIFIER" or r["throttle_detected"] or r["pcie_anomalies_count"] > 0)
        distinct_machines = len({r["machine_key"] for r in reports})

        ql = q.strip().lower()
        filtered = []
        for r in reports:
            if verdict and r["verdict_status"] != verdict:
                continue
            if anomaly == "throttle" and not r["throttle_detected"]:
                continue
            if anomaly == "faults" and not (r["critical_faults_count"] or r["warn_faults_count"] or r["failed_count"]):
                continue
            if anomaly == "hw" and not (r["pcie_anomalies_count"] or r["inventory_diffs_count"] or r["incomplete_sections_count"]):
                continue
            if ql:
                hay = " ".join([
                    str(r.get("system_name") or ""),
                    str(r.get("serial") or ""),
                    str(r.get("model") or ""),
                    str(r.get("client") or ""),
                    str(r.get("order_no") or ""),
                    str(r.get("operator") or ""),
                    str(r.get("cpu_model") or ""),
                    str(r.get("verdict_label") or ""),
                    str(r.get("alerts_summary") or ""),
                    str(r.get("rel_path") or ""),
                ]).lower()
                if ql not in hay:
                    continue
            filtered.append(r)

        # Synthèse optionnelle par clé machine (conservée pour compatibilité /api/machine)
        verdict_rank = {"NON_CONFORME": 4, "A_VERIFIER": 3, "CONFORME": 2, "NON_EVALUE": 1, "INFO_SEULE": 0}
        machines_map: Dict[str, Dict[str, Any]] = {}
        for r in filtered:
            mk = r["machine_key"]
            m = machines_map.get(mk)
            if not m:
                m = {
                    "machine_key": mk,
                    "system_name": r.get("system_name") or mk,
                    "model": r.get("model") or "",
                    "serial": r.get("serial") or "",
                    "client": r.get("client") or "",
                    "order_no": r.get("order_no") or "",
                    "cpu_model": r.get("cpu_model") or "",
                    "ram_total_gb": r.get("ram_total_gb"),
                    "os_info": r.get("os_info") or "",
                    "latest_exported_at": r.get("exported_at") or "",
                    "latest_mtime": r.get("mtime") or 0,
                    "latest_report_id": r["id"],
                    "verdict_status": r.get("verdict_status") or "NON_EVALUE",
                    "verdict_label": r.get("verdict_label") or "Non évalué",
                    "report_count": 0,
                    "file_count": len(mfiles_map.get(mk, [])),
                    "ok_count": 0,
                    "bench_count": 0,
                    "failed_count": 0,
                    "critical_faults_count": 0,
                    "throttle_detected": 0,
                    "pcie_anomalies_count": 0,
                    "inventory_diffs_count": 0,
                    "incomplete_sections_count": 0,
                    "gpu_temp_max_c": None,
                    "gpu_hotspot_max_c": None,
                }
                machines_map[mk] = m
            m["report_count"] += 1
            m["ok_count"] += int(r.get("ok_count") or 0)
            m["bench_count"] += int(r.get("bench_count") or 0)
            m["failed_count"] += int(r.get("failed_count") or 0)
            m["critical_faults_count"] += int(r.get("critical_faults_count") or 0)
            m["throttle_detected"] = 1 if (m["throttle_detected"] or r.get("throttle_detected")) else 0
            m["pcie_anomalies_count"] += int(r.get("pcie_anomalies_count") or 0)
            m["inventory_diffs_count"] += int(r.get("inventory_diffs_count") or 0)
            m["incomplete_sections_count"] += int(r.get("incomplete_sections_count") or 0)
            for tkey in ("gpu_temp_max_c", "gpu_hotspot_max_c"):
                if r.get(tkey) is not None:
                    m[tkey] = r[tkey] if m[tkey] is None else max(float(m[tkey]), float(r[tkey]))
            if verdict_rank.get(r.get("verdict_status"), 0) > verdict_rank.get(m["verdict_status"], 0):
                m["verdict_status"] = r.get("verdict_status") or m["verdict_status"]
                m["verdict_label"] = r.get("verdict_label") or m["verdict_label"]

        filtered_files = files
        if ql:
            filtered_files = [
                f for f in files
                if ql in f["file_name"].lower() or ql in f["rel_path"].lower()
                or ql in f["kind"].lower() or ql in (f.get("report_name") or "").lower()
                or ql in (f.get("machine_name") or "").lower()
            ]

        return {
            "meta": meta,
            "viewer_version": VIEWER_VERSION,
            "schema_version": DB_SCHEMA_VERSION,
            "db_path": str(self.db_path),
            "root_dir": str(self.root_dir),
            "stats": {
                "total_reports": total_reports,
                "distinct_machines": distinct_machines,
                "non_conforme": non_conf,
                "a_verifier": to_check,
                "total_files": len(files),
            },
            "machines": list(machines_map.values()),
            "reports": filtered,
            "files": filtered_files,
        }

    def get_machine_detail(self, machine_key: str, report_id: Optional[int] = None) -> Optional[Dict[str, Any]]:
        """Retourne uniquement les rapports et les fichiers appartenant à `machine_key`."""
        with self._lock, self._connect() as conn:
            reports = [dict(r) for r in conn.execute("SELECT * FROM reports ORDER BY exported_at DESC, mtime DESC")]
            files = [dict(f) for f in conn.execute("SELECT * FROM files ORDER BY dir_rel_path ASC, kind ASC, file_name ASC")]

        mfiles_map = self._machine_files_map(reports, files)
        m_reports = [r for r in reports if self._machine_key(r) == machine_key]
        if not m_reports:
            return None
        for r in m_reports:
            r["machine_key"] = machine_key
        m_files = mfiles_map.get(machine_key, [])

        chosen_id = report_id if (report_id and any(r["id"] == report_id for r in m_reports)) else m_reports[0]["id"]
        active_detail = self.get_report_detail(chosen_id)

        latest = m_reports[0]
        return {
            "machine": {
                "machine_key": machine_key,
                "system_name": latest.get("system_name") or machine_key,
                "model": latest.get("model") or "",
                "serial": latest.get("serial") or "",
                "client": latest.get("client") or "",
                "order_no": latest.get("order_no") or "",
                "cpu_model": latest.get("cpu_model") or "",
                "ram_total_gb": latest.get("ram_total_gb"),
                "os_info": latest.get("os_info") or "",
                "report_count": len(m_reports),
                "file_count": len(m_files),
            },
            "reports": m_reports,
            "files": m_files,
            "active_report_id": chosen_id,
            "active_detail": active_detail,
        }

    def get_report_detail(self, report_id: int) -> Optional[Dict[str, Any]]:
        """Retourne le détail complet d'un rapport, ses benchmarks, le JSON brut et le dmesg."""
        with self._lock, self._connect() as conn:
            row = conn.execute("SELECT * FROM reports WHERE id = ?", (report_id,)).fetchone()
            if not row:
                return None
            rep = dict(row)
            benches = [dict(b) for b in conn.execute("SELECT * FROM benchmarks WHERE report_id = ? ORDER BY id ASC", (report_id,))]
            comp_files = [
                dict(f) for f in conn.execute(
                    "SELECT * FROM files WHERE report_id = ? ORDER BY kind, file_name", (report_id,)
                )
            ]

        json_data = None
        p_json = _safe_resolve(self.root_dir, rep["rel_path"])
        if p_json:
            try:
                json_data = json.loads(p_json.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                json_data = None

        dmesg_lines = []
        if rep.get("dmesg_rel_path"):
            p_dm = _safe_resolve(self.root_dir, rep["dmesg_rel_path"])
            if p_dm:
                txt, _ = _read_text_auto(p_dm, max_bytes=1_000_000)
                for ln in txt.splitlines()[:1500]:
                    dmesg_lines.append({"text": ln, "is_err": bool(DMESG_ERR_RE.search(ln))})

        return {
            "report": rep,
            "benchmarks": benches,
            "files": comp_files,
            "payload": json_data,
            "dmesg": dmesg_lines,
        }


def _load_chartjs_scripts_inline() -> str:
    """Charge Chart.js + zoom depuis third_party/chartjs/ si disponible (100 % hors ligne)."""
    base = Path(__file__).resolve().parent.parent / "third_party" / "chartjs"
    tags = []
    for fname in ("chart.umd.min.js", "hammer.min.js", "chartjs-plugin-zoom.min.js"):
        fpath = base / fname
        if fpath.is_file():
            try:
                js = fpath.read_text(encoding="utf-8")
                js = re.sub(r"</script", r"<\/script", js, flags=re.IGNORECASE)
                tags.append(f"<script>\n{js}\n</script>")
            except Exception:
                pass
    return "\n".join(tags)


def build_viewer_html() -> str:
    """Construit la page HTML de l'interface locale BenchSuite Windows Viewer."""
    chart_scripts = _load_chartjs_scripts_inline()
    return f"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>BenchSuite Viewer — Base locale &amp; Rapports</title>
<style>
:root {{
  --bg: #0d0d0d; --card: #141414; --card2: #1b1b1b; --border: #2a2a2a;
  --text: #ececec; --muted: #9ca3af; --accent: #f97316; --accent2: #ff6600;
  --ok: #a6e3a1; --warn: #f9e2af; --err: #f38ba8; --info: #89b4fa;
}}
body.light {{
  --bg: #f4f5f7; --card: #ffffff; --card2: #eef0f4; --border: #d4d7dd;
  --text: #18181b; --muted: #52525b; --accent: #ea580c; --accent2: #f97316;
  --ok: #15803d; --warn: #b45309; --err: #b91c1c; --info: #1d4ed8;
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; font-family: "Segoe UI", system-ui, -apple-system, sans-serif; background: var(--bg); color: var(--text); font-size: 13px; }}
header {{
  position: sticky; top: 0; z-index: 20; background: var(--card); border-bottom: 1px solid var(--border);
  padding: 12px 20px; display: flex; flex-wrap: wrap; align-items: center; gap: 12px; justify-content: space-between;
}}
.brand {{ display: flex; flex-direction: column; gap: 2px; }}
.brand h1 {{ margin: 0; font-size: 17px; color: var(--accent); }}
.brand small {{ color: var(--muted); font-size: 11px; }}
.actions {{ display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }}
button, .btn {{
  background: var(--card2); color: var(--text); border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 12px; cursor: pointer; font-size: 12px; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; gap: 5px;
}}
button:hover, .btn:hover {{ border-color: var(--accent); }}
.btn-accent {{ background: var(--accent); color: #fff; border-color: var(--accent); }}
.btn-accent:hover {{ opacity: 0.92; }}
main {{ padding: 18px 20px 36px; max-width: 1520px; margin: 0 auto; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 12px; margin-bottom: 16px; }}
.kpi {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 12px 15px; }}
.kpi .lbl {{ color: var(--muted); font-size: 11px; text-transform: uppercase; letter-spacing: 0.7px; }}
.kpi .val {{ font-size: 24px; font-weight: 800; margin-top: 4px; }}
.tabs {{ display: flex; gap: 8px; border-bottom: 1px solid var(--border); margin-bottom: 14px; padding-bottom: 8px; flex-wrap: wrap; align-items: center; }}
.tab {{ background: transparent; border: 1px solid transparent; padding: 7px 12px; border-radius: 7px; color: var(--muted); display: inline-flex; align-items: center; gap: 7px; }}
.tab.active {{ background: var(--card2); border-color: var(--accent); color: var(--text); }}
.tab-close {{ font-size: 11px; opacity: 0.75; padding: 0 3px; border-radius: 4px; }}
.tab-close:hover {{ opacity: 1; background: rgba(243, 139, 168, 0.25); color: var(--err); }}
.subtabs {{ display: flex; gap: 8px; margin-bottom: 12px; flex-wrap: wrap; }}
.subtab {{ background: var(--card2); border: 1px solid var(--border); padding: 6px 12px; border-radius: 7px; color: var(--muted); }}
.subtab.active {{ border-color: var(--accent); color: var(--text); background: rgba(249, 115, 22, 0.14); }}
.toolbar {{ display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 14px; align-items: center; }}
input[type="text"], select {{
  background: var(--card); color: var(--text); border: 1px solid var(--border); border-radius: 7px;
  padding: 7px 10px; font-size: 12px;
}}
input[type="text"] {{ min-width: 280px; flex: 1; max-width: 480px; }}
.card {{ background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-bottom: 14px; overflow-x: auto; }}
table {{ width: 100%; border-collapse: collapse; font-size: 12px; }}
th, td {{ text-align: left; padding: 8px 9px; border-bottom: 1px solid var(--border); vertical-align: middle; }}
th {{ color: var(--accent); font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; cursor: pointer; user-select: none; white-space: nowrap; }}
th:hover {{ color: var(--text); }}
.sort-ind {{ font-size: 10px; margin-left: 4px; opacity: 0.9; }}
tr:hover {{ background: rgba(249, 115, 22, 0.06); }}
tr.row-click {{ cursor: pointer; }}
.mach-link {{ background: none; border: none; padding: 0; color: var(--text); font-weight: 700; font-size: 13px; text-align: left; cursor: pointer; }}
.mach-link:hover {{ color: var(--accent); text-decoration: underline; }}
.pill {{ display: inline-block; padding: 2px 8px; border-radius: 999px; font-weight: 700; font-size: 11px; border: 1px solid currentColor; }}
.p-ok {{ color: var(--ok); }} .p-warn {{ color: var(--warn); }} .p-err {{ color: var(--err); }} .p-info {{ color: var(--info); }} .p-muted {{ color: var(--muted); }}
.links {{ display: flex; flex-wrap: wrap; gap: 5px; }}
.links a, .links button {{ padding: 4px 8px; font-size: 11px; }}
.hidden {{ display: none !important; }}
pre.logbox {{
  background: #0a0a0a; color: #d4d4d8; border: 1px solid var(--border); border-radius: 8px;
  padding: 12px; max-height: 460px; overflow: auto; font-family: Consolas, monospace; font-size: 11.5px; line-height: 1.45;
}}
body.light pre.logbox {{ background: #1e1e2e; color: #e2e8f0; }}
.log-err {{ color: #fda4af; background: rgba(243, 139, 168, 0.16); display: block; padding: 1px 4px; border-radius: 3px; }}
.grid-2 {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(360px, 1fr)); gap: 14px; }}
.chart-box {{ height: 320px; position: relative; }}
iframe.doc-frame {{ width: 100%; height: 680px; border: 1px solid var(--border); border-radius: 8px; background: #fff; }}
</style>
{chart_scripts}
</head>
<body>
<header>
  <div class="brand">
    <h1>⚡ BenchSuite Viewer — Explorateur Windows &amp; BDD SQLite <span class="pill p-info" id="versionBadge" style="font-size:11px;vertical-align:middle;margin-left:6px">v{_html.escape(VIEWER_VERSION)} · BDD v{DB_SCHEMA_VERSION}</span></h1>
    <small id="metaInfo">Chargement…</small>
  </div>
  <div class="actions">
    <button id="btnRescan" class="btn-accent" onclick="rescanDir()">↻ Réindexer</button>
    <a class="btn" id="btnExportCsv" href="/api/export_fleet_csv">⬇ Export CSV (Excel)</a>
    <a class="btn" href="/api/export_catalog_html">📄 Catalogue HTML autonome</a>
    <button onclick="toggleTheme()" id="btnTheme">☀ Clair</button>
  </div>
</header>

<main>
  <div class="kpi-grid">
    <div class="kpi"><div class="lbl">Rapports indexés</div><div class="val" id="kpiReports">0</div></div>
    <div class="kpi"><div class="lbl">Non conformes / Pannes</div><div class="val p-err" id="kpiFail">0</div></div>
    <div class="kpi"><div class="lbl">À vérifier / Throttling / PCIe</div><div class="val p-warn" id="kpiWarn">0</div></div>
    <div class="kpi"><div class="lbl">Fichiers suivis dans le dossier</div><div class="val p-info" id="kpiFiles">0</div></div>
  </div>

  <div class="tabs" id="tabBar">
    <button class="tab active" data-tab="reports" onclick="switchTab('reports')">📋 Rapports</button>
    <button class="tab" data-tab="files" onclick="switchTab('files')">📁 Tous les fichiers du dossier</button>
    <span id="reportTabsContainer" style="display:inline-flex;gap:8px;flex-wrap:wrap"></span>
    <button class="tab hidden" id="tabPreviewBtn" data-tab="preview" onclick="switchTab('preview')">👁️ Aperçu fichier</button>
  </div>

  <div class="toolbar" id="filterBar">
    <input type="text" id="searchBox" placeholder="Rechercher : machine, n° série, modèle, client, dossier, alerte, fichier…" oninput="debouncedLoad()">
    <select id="verdictFilter" onchange="loadOverview()">
      <option value="">Tous les verdicts</option>
      <option value="NON_CONFORME">NON CONFORME</option>
      <option value="A_VERIFIER">À VÉRIFIER</option>
      <option value="CONFORME">CONFORME</option>
      <option value="NON_EVALUE">Non évalué</option>
      <option value="INFO_SEULE">Infos système seules</option>
    </select>
    <select id="anomalyFilter" onchange="loadOverview()">
      <option value="">Toutes anomalies</option>
      <option value="faults">Pannes / Tests en erreur</option>
      <option value="throttle">Throttling détecté</option>
      <option value="hw">Alertes PCIe / Inventaire / Incomplet</option>
    </select>
    <select id="sortSelect" onchange="applySortPreset(this.value)">
      <option value="date:desc">Tri : Date (récent d'abord)</option>
      <option value="date:asc">Tri : Date (ancien d'abord)</option>
      <option value="verdict:desc">Tri : Verdict (critique d'abord)</option>
      <option value="heat:desc">Tri : Chauffe &amp; Pannes (max)</option>
      <option value="machine:asc">Tri : Machine (A → Z)</option>
      <option value="serial:asc">Tri : N° Série (A → Z)</option>
      <option value="files:desc">Tri : Nb fichiers (max)</option>
    </select>
  </div>

  <section id="view-reports" class="card">
    <p style="margin:0 0 10px;color:var(--muted)"><b>1 ligne = 1 rapport.</b> Clique sur une ligne pour ouvrir son onglet dédié contenant <b>uniquement les fichiers de ce rapport</b>. Clique sur un en-tête de colonne pour trier.</p>
    <table>
      <thead>
        <tr>
          <th onclick="sortReports('date')">Date <span class="sort-ind" id="s-date">▼</span></th>
          <th onclick="sortReports('machine')">Machine / Dossier <span class="sort-ind" id="s-machine"></span></th>
          <th onclick="sortReports('serial')">N° Série / Client <span class="sort-ind" id="s-serial"></span></th>
          <th onclick="sortReports('verdict')">Verdict <span class="sort-ind" id="s-verdict"></span></th>
          <th onclick="sortReports('heat')">Chauffe &amp; Erreurs <span class="sort-ind" id="s-heat"></span></th>
          <th onclick="sortReports('tests')">Tests / Fichiers <span class="sort-ind" id="s-tests"></span></th>
          <th onclick="sortReports('files')">Ouvrir <span class="sort-ind" id="s-files"></span></th>
        </tr>
      </thead>
      <tbody id="reportsTbody"></tbody>
    </table>
  </section>

  <section id="view-files" class="card hidden">
    <div class="toolbar">
      <select id="reportFilter" onchange="renderFiles()">
        <option value="">Tous les rapports</option>
      </select>
      <select id="kindFilter" onchange="renderFiles()">
        <option value="">Tous les types</option>
        <option value="report_json">Rapports JSON</option>
        <option value="report_html">Rapports HTML</option>
        <option value="report_pdf">Rapports PDF</option>
        <option value="report_csv">Tableaux CSV</option>
        <option value="dmesg">Journaux dmesg</option>
        <option value="fleet">Synthèses de lot (fleet)</option>
        <option value="session_state">États / Historique JSON</option>
      </select>
      <span id="filesCount" style="color:var(--muted)"></span>
    </div>
    <table>
      <thead>
        <tr>
          <th onclick="sortFiles('report')">Rapport / Machine <span class="sort-ind" id="fs-report"></span></th>
          <th onclick="sortFiles('name')">Fichier <span class="sort-ind" id="fs-name"></span></th>
          <th onclick="sortFiles('dir')">Dossier <span class="sort-ind" id="fs-dir"></span></th>
          <th onclick="sortFiles('kind')">Type <span class="sort-ind" id="fs-kind"></span></th>
          <th onclick="sortFiles('size')">Taille <span class="sort-ind" id="fs-size"></span></th>
          <th onclick="sortFiles('mtime')">Modifié <span class="sort-ind" id="fs-mtime">▼</span></th>
          <th>Actions</th>
        </tr>
      </thead>
      <tbody id="filesTbody"></tbody>
    </table>
  </section>

  <section id="view-report" class="hidden"></section>
  <section id="view-preview" class="hidden"></section>
</main>

<script>
let STATE = {{ machines: [], reports: [], files: [], stats: {{}}, meta: {{}} }};
let OPEN_REPORTS = []; // [{{ id, label, title, data, subtab, inlinePreview }}]
let ACTIVE_TAB = "reports";
let ACTIVE_REPORT_ID = null;
let REPORT_SORT = {{ col: "date", dir: "desc" }};
let FILE_SORT = {{ col: "mtime", dir: "desc" }};
let CURRENT_CHART = null;
let SEARCH_TIMER = null;

const VERDICT_RANK = {{ NON_CONFORME: 4, A_VERIFIER: 3, CONFORME: 2, NON_EVALUE: 1, INFO_SEULE: 0 }};

function esc(s) {{
  return String(s ?? "").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;").replace(/"/g,"&quot;").replace(/'/g,"&#39;");
}}
function escJs(s) {{
  return esc(JSON.stringify(String(s ?? "")));
}}
function fmtSize(n) {{
  if (n < 1024) return n + " o";
  if (n < 1048576) return (n/1024).toFixed(1) + " Ko";
  return (n/1048576).toFixed(2) + " Mo";
}}
function fmtDate(ts) {{
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString("fr-FR");
}}
function reportDisplayDate(r) {{
  return (r.exported_at || "").slice(0, 16).replace("T", " ") || fmtDate(r.mtime);
}}
function reportDateTs(r) {{
  if (r.exported_at) {{
    const t = Date.parse(r.exported_at);
    if (!Number.isNaN(t)) return t;
  }}
  return Number(r.mtime || 0) * 1000;
}}
function verdictBadge(status, label) {{
  const cls = status === "CONFORME" ? "p-ok" : status === "A_VERIFIER" ? "p-warn" : status === "NON_CONFORME" ? "p-err" : "p-muted";
  return `<span class="pill ${{cls}}">${{esc(label || status)}}</span>`;
}}
function toggleTheme() {{
  const isLight = document.body.classList.toggle("light");
  localStorage.setItem("bs_viewer_theme", isLight ? "light" : "dark");
  document.getElementById("btnTheme").textContent = isLight ? "🌙 Sombre" : "☀ Clair";
}}
if (localStorage.getItem("bs_viewer_theme") === "light") toggleTheme();

function renderReportTabsBar() {{
  const box = document.getElementById("reportTabsContainer");
  box.innerHTML = OPEN_REPORTS.map((slot, idx) => {{
    const isAct = (ACTIVE_TAB === "report" && ACTIVE_REPORT_ID === slot.id);
    return `<button class="tab ${{isAct ? "active" : ""}}" title="${{esc(slot.title)}}" onclick="activateReportTab(${{idx}})">
      <span>🖥️ ${{esc(slot.label)}}</span>
      <span class="tab-close" title="Fermer cet onglet" onclick="event.stopPropagation(); closeReportTab(${{idx}})">✕</span>
    </button>`;
  }}).join("");
}}

function switchTab(name) {{
  ACTIVE_TAB = name;
  if (name !== "report") ACTIVE_REPORT_ID = null;
  for (const id of ["reports", "files", "report", "preview"]) {{
    document.getElementById("view-" + id).classList.toggle("hidden", id !== name);
  }}
  document.querySelectorAll("#tabBar > .tab").forEach(b => b.classList.toggle("active", b.dataset.tab === name));
  renderReportTabsBar();
  document.getElementById("filterBar").classList.toggle("hidden", name === "report" || name === "preview");
}}

function debouncedLoad() {{
  clearTimeout(SEARCH_TIMER);
  SEARCH_TIMER = setTimeout(loadOverview, 180);
}}

function sortReports(col) {{
  if (REPORT_SORT.col === col) {{
    REPORT_SORT.dir = (REPORT_SORT.dir === "asc") ? "desc" : "asc";
  }} else {{
    REPORT_SORT.col = col;
    REPORT_SORT.dir = (col === "machine" || col === "serial") ? "asc" : "desc";
  }}
  const preset = `${{REPORT_SORT.col}}:${{REPORT_SORT.dir}}`;
  const sel = document.getElementById("sortSelect");
  if ([...sel.options].some(o => o.value === preset)) sel.value = preset;
  renderReports();
}}

function applySortPreset(val) {{
  const [col, dir] = String(val || "date:desc").split(":");
  REPORT_SORT = {{ col: col || "date", dir: dir || "desc" }};
  renderReports();
}}

function sortFiles(col) {{
  if (FILE_SORT.col === col) {{
    FILE_SORT.dir = (FILE_SORT.dir === "asc") ? "desc" : "asc";
  }} else {{
    FILE_SORT.col = col;
    FILE_SORT.dir = (col === "mtime" || col === "size") ? "desc" : "asc";
  }}
  renderFiles();
}}

async function loadOverview() {{
  const q = encodeURIComponent(document.getElementById("searchBox").value || "");
  const v = encodeURIComponent(document.getElementById("verdictFilter").value || "");
  const a = encodeURIComponent(document.getElementById("anomalyFilter").value || "");
  document.getElementById("btnExportCsv").href = `/api/export_fleet_csv?q=${{q}}&verdict=${{v}}&anomaly=${{a}}`;
  const resp = await fetch(`/api/overview?q=${{q}}&verdict=${{v}}&anomaly=${{a}}`);
  STATE = await resp.json();

  const vViewer = STATE.viewer_version || (STATE.meta && STATE.meta.viewer_version) || "—";
  const vDbSchema = STATE.schema_version || (STATE.meta && STATE.meta.schema_version) || "—";
  const vDbIndexed = (STATE.meta && STATE.meta.viewer_version) || vViewer;
  document.getElementById("versionBadge").textContent = `v${{vViewer}} · BDD v${{vDbSchema}}`;
  document.getElementById("metaInfo").textContent =
    `Viewer v${{vViewer}} · BDD schéma v${{vDbSchema}} (indexée par v${{vDbIndexed}})  |  Dossier : ${{STATE.root_dir}}  |  BDD SQLite : ${{STATE.db_path}}  |  Dernier scan : ${{(STATE.meta && STATE.meta.last_scan) || "—"}}`;
  document.getElementById("kpiReports").textContent = STATE.stats.total_reports || 0;
  document.getElementById("kpiFail").textContent = STATE.stats.non_conforme || 0;
  document.getElementById("kpiWarn").textContent = STATE.stats.a_verifier || 0;
  document.getElementById("kpiFiles").textContent = STATE.stats.total_files || 0;

  const rSel = document.getElementById("reportFilter");
  const prevVal = rSel.value;
  rSel.innerHTML = '<option value="">Tous les rapports</option>'
    + (STATE.reports || []).map(r => {{
        const dt = reportDisplayDate(r);
        return `<option value="${{r.id}}">${{esc(r.system_name || r.file_name)}} — ${{esc(dt)}} (${{r.file_count || 0}} fich.)</option>`;
      }}).join("");
  rSel.value = prevVal;

  renderReports();
  renderFiles();
}}

function getSortedReports() {{
  const rows = [...(STATE.reports || [])];
  const col = REPORT_SORT.col;
  const mul = REPORT_SORT.dir === "asc" ? 1 : -1;
  const cmpStr = (a, b) => String(a || "").localeCompare(String(b || ""), "fr", {{ numeric: true, sensitivity: "base" }});

  rows.sort((a, b) => {{
    let d = 0;
    if (col === "date") {{
      d = reportDateTs(a) - reportDateTs(b);
    }} else if (col === "machine") {{
      d = cmpStr(a.system_name, b.system_name) || cmpStr(a.dir_rel_path, b.dir_rel_path);
    }} else if (col === "serial") {{
      d = cmpStr(a.serial, b.serial) || cmpStr(a.client, b.client);
    }} else if (col === "verdict") {{
      d = (VERDICT_RANK[a.verdict_status] ?? 0) - (VERDICT_RANK[b.verdict_status] ?? 0);
    }} else if (col === "heat") {{
      const score = r => (Number(r.critical_faults_count || 0) * 10000)
        + (r.throttle_detected ? 1000 : 0)
        + Number(r.pcie_anomalies_count || 0) * 50
        + Number(r.gpu_hotspot_max_c ?? r.gpu_temp_max_c ?? -1);
      d = score(a) - score(b);
    }} else if (col === "tests") {{
      const sa = Number(a.failed_count || 0) * 1000 + Number(a.bench_count || 0);
      const sb = Number(b.failed_count || 0) * 1000 + Number(b.bench_count || 0);
      d = sa - sb;
    }} else if (col === "files") {{
      d = Number(a.file_count || 0) - Number(b.file_count || 0);
    }}
    if (d === 0) d = reportDateTs(a) - reportDateTs(b);
    return d * mul;
  }});

  for (const k of ["date", "machine", "serial", "verdict", "heat", "tests", "files"]) {{
    const el = document.getElementById("s-" + k);
    if (el) el.textContent = (k === col) ? (REPORT_SORT.dir === "asc" ? "▲" : "▼") : "";
  }}
  return rows;
}}

function renderReports() {{
  const tb = document.getElementById("reportsTbody");
  const rows = getSortedReports();
  if (!rows.length) {{
    tb.innerHTML = '<tr><td colspan="7" style="color:var(--muted);padding:18px">Aucun rapport correspondant dans le dossier.</td></tr>';
    return;
  }}
  tb.innerHTML = rows.map(r => {{
    const dt = reportDisplayDate(r);
    const hwTags = [];
    if (r.gpu_hotspot_max_c != null) hwTags.push(`Hotspot ${{r.gpu_hotspot_max_c}} °C`);
    else if (r.gpu_temp_max_c != null) hwTags.push(`GPU ${{r.gpu_temp_max_c}} °C`);
    if (r.throttle_detected) hwTags.push('<span class="p-warn">⚠ Throttling</span>');
    if (r.critical_faults_count) hwTags.push(`<span class="p-err">✘ ${{r.critical_faults_count}} panne(s)</span>`);
    if (r.pcie_anomalies_count) hwTags.push(`<span class="p-warn">PCIe (${{r.pcie_anomalies_count}})</span>`);
    if (r.inventory_diffs_count) hwTags.push(`<span class="p-warn">Inventaire (${{r.inventory_diffs_count}})</span>`);
    if (r.incomplete_sections_count) hwTags.push(`<span class="p-warn">Incomplet (${{r.incomplete_sections_count}})</span>`);

    const subModel = [r.model || r.cpu_model || "", (r.dir_rel_path && r.dir_rel_path !== ".") ? r.dir_rel_path : ""].filter(Boolean).join(" · ");
    const fc = Number(r.file_count || 0);
    const countsTxt = `${{r.ok_count}}/${{r.bench_count}} tests OK`
      + (r.failed_count ? ` · <span class="p-err">${{r.failed_count}} échec</span>` : "")
      + `<br><small style="color:var(--muted)"><b>${{fc}} fichier(s)</b></small>`;

    return `<tr class="row-click" onclick="openReportTab(${{r.id}})">
      <td style="white-space:nowrap">${{esc(dt)}}</td>
      <td><button class="mach-link" onclick="event.stopPropagation(); openReportTab(${{r.id}})">${{esc(r.system_name || r.file_name || "—")}}</button><br>
          <small style="color:var(--muted)">${{esc(subModel)}}</small></td>
      <td>${{esc(r.serial || "—")}}<br><small style="color:var(--muted)">${{esc(r.client || "")}} ${{r.order_no ? "· " + esc(r.order_no) : ""}}</small></td>
      <td>${{verdictBadge(r.verdict_status, r.verdict_label)}}</td>
      <td>${{hwTags.join(" · ") || '<span style="color:var(--muted)">RAS</span>'}}</td>
      <td>${{countsTxt}}</td>
      <td><button class="btn-accent" onclick="event.stopPropagation(); openReportTab(${{r.id}})">Ouvrir (${{fc}})</button></td>
    </tr>`;
  }}).join("");
}}

function makeTabLabel(r) {{
  const name = String(r.system_name || r.file_name || ("Rapport #" + r.id));
  const shortName = name.length > 22 ? (name.slice(0, 21) + "…") : name;
  const dt = reportDisplayDate(r);
  const shortDt = dt.length >= 16 ? dt.slice(5, 16) : dt;
  return `${{shortName}} · ${{shortDt}}`;
}}

async function openReportTab(reportId) {{
  const rid = Number(reportId);
  if (!rid) return;
  const resp = await fetch(`/api/report/${{rid}}`);
  if (!resp.ok) return;
  const det = await resp.json();
  const r = det.report || {{}};
  const label = makeTabLabel(r);
  const title = `${{r.system_name || ""}} | S/N: ${{r.serial || "—"}} | ${{r.rel_path || ""}}`;

  let slotIdx = OPEN_REPORTS.findIndex(x => x.id === rid);
  if (slotIdx === -1) {{
    OPEN_REPORTS.push({{
      id: rid,
      label,
      title,
      data: det,
      subtab: "files",
      inlinePreview: null,
    }});
    slotIdx = OPEN_REPORTS.length - 1;
  }} else {{
    OPEN_REPORTS[slotIdx].data = det;
    OPEN_REPORTS[slotIdx].label = label;
    OPEN_REPORTS[slotIdx].title = title;
  }}
  activateReportTab(slotIdx);
}}

function activateReportTab(slotIdx) {{
  const slot = OPEN_REPORTS[slotIdx];
  if (!slot) return;
  ACTIVE_REPORT_ID = slot.id;
  switchTab("report");
  renderActiveReportView(slotIdx);
}}

function closeReportTab(slotIdx) {{
  const wasActive = OPEN_REPORTS[slotIdx] && ACTIVE_REPORT_ID === OPEN_REPORTS[slotIdx].id && ACTIVE_TAB === "report";
  OPEN_REPORTS.splice(slotIdx, 1);
  if (wasActive) {{
    if (OPEN_REPORTS.length) {{
      activateReportTab(Math.max(0, slotIdx - 1));
    }} else {{
      switchTab("reports");
    }}
  }} else {{
    renderReportTabsBar();
  }}
}}

function setReportSubtab(slotIdx, subtab) {{
  const slot = OPEN_REPORTS[slotIdx];
  if (!slot) return;
  slot.subtab = subtab;
  renderActiveReportView(slotIdx);
}}

async function previewInReport(slotIdx, relPath, modeHint) {{
  const slot = OPEN_REPORTS[slotIdx];
  if (!slot) return;
  if (modeHint === "embed_html" || modeHint === "embed_pdf") {{
    slot.inlinePreview = {{ path: relPath, kind: modeHint }};
    renderActiveReportView(slotIdx);
    return;
  }}
  const resp = await fetch(`/api/preview?path=${{encodeURIComponent(relPath)}}`);
  if (!resp.ok) return;
  const prev = await resp.json();
  slot.inlinePreview = {{ path: relPath, kind: "api", data: prev }};
  renderActiveReportView(slotIdx);
}}

function closeReportInlinePreview(slotIdx) {{
  const slot = OPEN_REPORTS[slotIdx];
  if (!slot) return;
  slot.inlinePreview = null;
  renderActiveReportView(slotIdx);
}}

function renderPreviewHtmlBlock(prevData) {{
  if (prevData.mode === "csv") {{
    const th = (prevData.headers || []).map(h => `<th>${{esc(h)}}</th>`).join("");
    const tr = (prevData.rows || []).map(r => "<tr>" + r.map(c => `<td>${{esc(c)}}</td>`).join("") + "</tr>").join("");
    return `<p style="color:var(--muted)">Encodage : ${{esc(prevData.encoding)}} · Séparateur : <code>${{esc(prevData.delimiter)}}</code>${{prevData.truncated ? " · Aperçu limité aux 300 premières lignes" : ""}}</p>
            <div style="overflow-x:auto"><table><thead><tr>${{th}}</tr></thead><tbody>${{tr}}</tbody></table></div>`;
  }}
  if (prevData.mode === "dmesg") {{
    return `<p style="color:var(--muted)">${{prevData.error_count}} erreur(s) détectée(s) · <label><input type="checkbox" onchange="filterDmesgBlock(this)"> Afficher seulement les erreurs</label></p>
            <pre class="logbox">${{prevData.lines.map(l => l.is_err ? `<span class="log-err">${{esc(l.text)}}</span>` : `<span class="log-ok">${{esc(l.text)}}</span>`).join("\\n")}}</pre>`;
  }}
  return `<pre class="logbox">${{esc(prevData.text || "")}}</pre>`;
}}

function filterDmesgBlock(chk) {{
  const box = chk.closest(".card").querySelector("pre.logbox");
  if (!box) return;
  box.querySelectorAll(".log-ok").forEach(el => el.style.display = chk.checked ? "none" : "inline");
}}

function renderActiveReportView(slotIdx) {{
  const slot = OPEN_REPORTS[slotIdx];
  if (!slot) return;
  const det = slot.data || {{}};
  const r = det.report || {{}};
  const p = det.payload || {{}};
  const rfiles = det.files || [];
  const sub = slot.subtab || "files";
  const dt = reportDisplayDate(r);

  // 1) Sous-vue : Fichiers strictement liés à CE rapport unique
  const filesRows = rfiles.map(f => {{
    const isHtml = (f.kind === "report_html" || f.ext === ".html" || f.ext === ".htm");
    const isPdf = (f.kind === "report_pdf" || f.ext === ".pdf");
    const errNote = (f.kind === "dmesg" && f.dmesg_error_count > 0)
      ? ` <span class="pill p-err">${{f.dmesg_error_count}} erreur(s)</span>` : "";
    const actions = [];
    if (isHtml || isPdf) {{
      actions.push(`<button class="btn-accent" onclick="previewInReport(${{slotIdx}}, ${{escJs(f.rel_path)}}, '${{isHtml ? "embed_html" : "embed_pdf"}}')">👁️ Voir ici</button>`);
      actions.push(`<a class="btn" target="_blank" href="/api/file?path=${{encodeURIComponent(f.rel_path)}}">↗ Nouvel onglet</a>`);
    }} else {{
      actions.push(`<button class="btn-accent" onclick="previewInReport(${{slotIdx}}, ${{escJs(f.rel_path)}}, 'api')">👁️ Aperçu ici</button>`);
    }}
    actions.push(`<a class="btn" download href="/api/file?path=${{encodeURIComponent(f.rel_path)}}">⬇ Télécharger</a>`);
    return `<tr>
      <td><b>${{esc(f.file_name)}}</b>${{errNote}}</td>
      <td style="color:var(--muted)">${{esc(f.dir_rel_path)}}</td>
      <td><span class="pill p-info">${{esc(f.kind)}}</span></td>
      <td>${{fmtSize(f.size)}}</td>
      <td>${{fmtDate(f.mtime)}}</td>
      <td><div class="links">${{actions.join("")}}</div></td>
    </tr>`;
  }}).join("");

  let inlinePrevHtml = "";
  if (slot.inlinePreview) {{
    const ip = slot.inlinePreview;
    let content = "";
    if (ip.kind === "embed_html" || ip.kind === "embed_pdf") {{
      content = `<iframe class="doc-frame" src="/api/file?path=${{encodeURIComponent(ip.path)}}"></iframe>`;
    }} else {{
      content = renderPreviewHtmlBlock(ip.data);
    }}
    inlinePrevHtml = `
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
          <h3 style="margin:0">Aperçu : ${{esc(ip.path)}}</h3>
          <div class="links">
            <a class="btn" target="_blank" href="/api/file?path=${{encodeURIComponent(ip.path)}}">↗ Ouvrir seul</a>
            <button onclick="closeReportInlinePreview(${{slotIdx}})">✕ Fermer l'aperçu</button>
          </div>
        </div>
        ${{content}}
      </div>`;
  }}

  const filesSubHtml = `
    <div class="card">
      <p style="margin:0 0 10px;color:var(--muted)">Fichiers isolés de ce rapport uniquement : <b>${{esc(r.file_name)}}</b> (${{rfiles.length}} fichier(s)).</p>
      <table>
        <thead><tr><th>Fichier</th><th>Dossier</th><th>Type</th><th>Taille</th><th>Modifié</th><th>Actions</th></tr></thead>
        <tbody>${{filesRows || '<tr><td colspan="6">Aucun fichier associé à ce rapport.</td></tr>'}}</tbody>
      </table>
    </div>
    ${{inlinePrevHtml}}`;

  // 2) Sous-vue : Verdict, Thermique & Graphiques
  const alerts = JSON.parse(r.alerts_summary || "[]");
  const telGpus = (p.telemetry_summary || {{}}).gpus || [];
  const alertHtml = alerts.length
    ? alerts.map(a => `<div class="${{a.level === "fail" || a.level === "critical" || a.level === "error" ? "p-err" : "p-warn"}}" style="margin:4px 0">
        • [${{esc(a.source)}}] <b>${{esc(a.label)}}</b> ${{a.detail ? "— " + esc(a.detail) : ""}}</div>`).join("")
    : '<div class="p-ok">Aucune alerte relevée.</div>';
  const gpuHtml = telGpus.length
    ? `<table><thead><tr><th>GPU</th><th>Cœur max</th><th>Hotspot max</th><th>VRAM max</th><th>ΔT moy</th><th>Puissance max</th><th>Fréq min</th><th>Throttling</th></tr></thead><tbody>`
      + telGpus.map(g => `<tr>
          <td>${{esc(g.label || "GPU " + g.gpu_index)}}</td>
          <td>${{g.temp_core_max_c ?? "—"}} °C</td>
          <td>${{g.temp_hotspot_max_c ?? "—"}} °C</td>
          <td>${{g.temp_vram_max_c ?? "—"}} °C</td>
          <td>${{g.delta_hotspot_moy_c ?? "—"}} °C</td>
          <td>${{g.power_max_w ?? "—"}} W</td>
          <td>${{g.freq_min_mhz ?? "—"}} MHz</td>
          <td>${{g.throttle_detected ? '<span class="p-warn">Oui (' + (g.throttle_samples || 0) + ')</span>' : '<span class="p-ok">Non</span>'}}</td>
        </tr>`).join("") + "</tbody></table>"
    : '<p style="color:var(--muted)">Aucune synthèse télémétrique GPU.</p>';
  const benchHtml = (det.benchmarks || []).map(b => {{
    const stCls = b.status === "ok" ? "p-ok" : (b.status === "stopped" || b.status === "skipped") ? "p-warn" : "p-err";
    return `<tr>
      <td><b>${{esc(b.bench_name)}}</b><br><small style="color:var(--muted)">${{esc(b.category)}}</small></td>
      <td><span class="pill ${{stCls}}">${{esc(b.status)}}</span></td>
      <td>${{b.score != null ? esc(b.score + " " + (b.unit || "")) : "—"}}</td>
      <td>${{b.duration_s != null ? b.duration_s.toFixed(1) + " s" : "—"}}</td>
      <td>${{b.fault_count ? '<span class="p-err">' + b.fault_count + '</span>' : '0'}}</td>
      <td><small style="font-family:Consolas,monospace">${{esc(b.command || b.notes || "—")}}</small></td>
    </tr>`;
  }}).join("");
  const chartCard = (typeof Chart !== "undefined")
    ? `<div class="card"><h3>Courbes complètes (JSON source)</h3>
        <div class="toolbar"><select id="chartBenchSelect" onchange="drawDetailChart()"></select></div>
        <div class="chart-box"><canvas id="detailCanvas"></canvas></div></div>`
    : "";
  const reportSubHtml = `
    <div class="grid-2">
      <div class="card"><h3>Alertes &amp; Verdict</h3>${{alertHtml}}</div>
      <div class="card"><h3>Synthèse thermique &amp; GPU</h3>${{gpuHtml}}</div>
    </div>
    ${{chartCard}}
    <div class="card">
      <h3>Tests exécutés (${{(det.benchmarks || []).length}})</h3>
      <table><thead><tr><th>Test</th><th>Statut</th><th>Score</th><th>Durée</th><th>Erreurs</th><th>Commande / Notes</th></tr></thead>
      <tbody>${{benchHtml || '<tr><td colspan="6">Aucun test dans ce rapport.</td></tr>'}}</tbody></table>
    </div>`;

  // 3) Sous-vue : Journal dmesg lié
  const dmesgSubHtml = (det.dmesg && det.dmesg.length)
    ? `<div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <h3 style="margin:0">Journal noyau (${{esc(r.dmesg_rel_path || "dmesg")}})</h3>
          <label><input type="checkbox" onchange="filterDmesgBlock(this)"> Afficher seulement les erreurs</label>
        </div>
        <pre class="logbox">${{det.dmesg.map(l => l.is_err ? `<span class="log-err">${{esc(l.text)}}</span>` : `<span class="log-ok">${{esc(l.text)}}</span>`).join("\\n")}}</pre>
      </div>`
    : `<div class="card"><p style="color:var(--muted)">Aucun fichier dmesg lié à ce rapport.</p></div>`;

  const activeSubHtml = sub === "report" ? reportSubHtml : sub === "dmesg" ? dmesgSubHtml : filesSubHtml;

  document.getElementById("view-report").innerHTML = `
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
        <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
          <button onclick="switchTab('reports')">← Retour aux rapports</button>
          <div>
            <h2 style="margin:0">🖥️ ${{esc(r.system_name || r.file_name)}} ${{verdictBadge(r.verdict_status, r.verdict_label)}}</h2>
            <p style="margin:4px 0 0;color:var(--muted)">
              Date : <b>${{esc(dt)}}</b> · Dossier : <b>${{esc(r.dir_rel_path)}}</b> ·
              Modèle : <b>${{esc(r.model || "—")}}</b> · N° série : <b>${{esc(r.serial || "—")}}</b> ·
              Client : <b>${{esc(r.client || "—")}}</b> · CPU : <b>${{esc(r.cpu_model || "—")}}</b> · RAM : <b>${{r.ram_total_gb ?? "—"}} Go</b>
            </p>
          </div>
        </div>
        <div class="links">
          ${{r.html_rel_path ? `<a class="btn btn-accent" target="_blank" href="/api/file?path=${{encodeURIComponent(r.html_rel_path)}}">↗ HTML</a>` : ""}}
          ${{r.pdf_rel_path ? `<a class="btn" target="_blank" href="/api/file?path=${{encodeURIComponent(r.pdf_rel_path)}}">↗ PDF</a>` : ""}}
          <button onclick="closeReportTab(${{slotIdx}})">✕ Fermer l'onglet</button>
        </div>
      </div>
    </div>

    <div class="subtabs">
      <button class="subtab ${{sub === "files" ? "active" : ""}}" onclick="setReportSubtab(${{slotIdx}}, 'files')">📂 Fichiers de ce rapport (${{rfiles.length}})</button>
      <button class="subtab ${{sub === "report" ? "active" : ""}}" onclick="setReportSubtab(${{slotIdx}}, 'report')">📊 Verdict, Thermique &amp; Graphiques</button>
      <button class="subtab ${{sub === "dmesg" ? "active" : ""}}" onclick="setReportSubtab(${{slotIdx}}, 'dmesg')">📜 Journal dmesg (${{(det.dmesg || []).length}} l.)</button>
    </div>

    ${{activeSubHtml}}
  `;

  window._DETAIL_PAYLOAD = p;
  if (sub === "report" && typeof Chart !== "undefined") populateChartChoices(p);
}}

function renderFiles() {{
  const kind = document.getElementById("kindFilter").value;
  const rFilter = document.getElementById("reportFilter").value;
  let rows = (STATE.files || []).filter(f => (!kind || f.kind === kind) && (!rFilter || String(f.report_id || "") === rFilter));

  const fcol = FILE_SORT.col;
  const mul = FILE_SORT.dir === "asc" ? 1 : -1;
  const cmpStr = (a, b) => String(a || "").localeCompare(String(b || ""), "fr", {{ numeric: true, sensitivity: "base" }});
  rows.sort((a, b) => {{
    let d = 0;
    if (fcol === "report") d = cmpStr(a.report_name, b.report_name);
    else if (fcol === "name") d = cmpStr(a.file_name, b.file_name);
    else if (fcol === "dir") d = cmpStr(a.dir_rel_path, b.dir_rel_path);
    else if (fcol === "kind") d = cmpStr(a.kind, b.kind);
    else if (fcol === "size") d = Number(a.size || 0) - Number(b.size || 0);
    else d = Number(a.mtime || 0) - Number(b.mtime || 0);
    if (d === 0) d = cmpStr(a.file_name, b.file_name);
    return d * mul;
  }});
  for (const k of ["report", "name", "dir", "kind", "size", "mtime"]) {{
    const el = document.getElementById("fs-" + k);
    if (el) el.textContent = (k === fcol) ? (FILE_SORT.dir === "asc" ? "▲" : "▼") : "";
  }}

  document.getElementById("filesCount").textContent = `${{rows.length}} fichier(s)`;
  const tb = document.getElementById("filesTbody");
  tb.innerHTML = rows.map(f => {{
    const directOpen = (f.kind === "report_html" || f.kind === "report_pdf" || f.ext === ".html" || f.ext === ".pdf");
    const errNote = (f.kind === "dmesg" && f.dmesg_error_count > 0)
      ? ` <span class="pill p-err">${{f.dmesg_error_count}} erreur(s)</span>` : "";
    const repBtn = f.report_id
      ? `<button class="mach-link" onclick="openReportTab(${{f.report_id}})">${{esc(f.report_name || ("Rapport #" + f.report_id))}}</button><br><small style="color:var(--muted)">${{esc((f.report_exported_at || "").slice(0, 16).replace("T", " "))}}</small>`
      : '<span style="color:var(--muted)">Global / Lot</span>';
    const actions = [];
    if (directOpen) {{
      actions.push(`<a class="btn btn-accent" target="_blank" href="/api/file?path=${{encodeURIComponent(f.rel_path)}}">Ouvrir dans le navigateur</a>`);
    }} else {{
      actions.push(`<button onclick="previewFile(${{escJs(f.rel_path)}})">Aperçu</button>`);
    }}
    actions.push(`<a class="btn" download href="/api/file?path=${{encodeURIComponent(f.rel_path)}}">Télécharger</a>`);
    return `<tr>
      <td>${{repBtn}}</td>
      <td><b>${{esc(f.file_name)}}</b>${{errNote}}</td>
      <td style="color:var(--muted)">${{esc(f.dir_rel_path)}}</td>
      <td><span class="pill p-info">${{esc(f.kind)}}</span></td>
      <td>${{fmtSize(f.size)}}</td>
      <td>${{fmtDate(f.mtime)}}</td>
      <td><div class="links">${{actions.join("")}}</div></td>
    </tr>`;
  }}).join("");
}}

function populateChartChoices(p) {{
  const sel = document.getElementById("chartBenchSelect");
  if (!sel) return;
  const opts = [];
  (p.results || []).forEach((res, idx) => {{
    const ser = ((res.metrics || {{}}).series) || {{}};
    if (ser && Array.isArray(ser.t) && ser.t.length > 1) {{
      opts.push({{ id: String(idx), label: res.name || ("Test #" + (idx + 1)), ser }});
    }}
  }});
  if (p.sequence_series && Array.isArray(p.sequence_series.channels) && p.sequence_series.channels.length) {{
    opts.unshift({{ id: "seq", label: "Séquence complète (capteurs)", seq: p.sequence_series }});
  }}
  if (!opts.length) {{
    sel.innerHTML = "<option value=''>Aucune série temporelle dans ce JSON</option>";
    return;
  }}
  window._CHART_OPTS = opts;
  sel.innerHTML = opts.map((o, i) => `<option value="${{i}}">${{esc(o.label)}}</option>`).join("");
  drawDetailChart();
}}

function drawDetailChart() {{
  const sel = document.getElementById("chartBenchSelect");
  if (!sel || !window._CHART_OPTS || sel.value === "") return;
  const item = window._CHART_OPTS[Number(sel.value)];
  const ctx = document.getElementById("detailCanvas");
  if (!ctx) return;
  if (CURRENT_CHART) CURRENT_CHART.destroy();
  const palette = ["#f97316", "#38bdf8", "#a6e3a1", "#f38ba8", "#f9e2af", "#c084fc", "#2dd4bf"];
  let labels = [];
  let datasets = [];

  if (item.seq) {{
    const chs = item.seq.channels || [];
    const tArr = Array.isArray(item.seq.t) ? item.seq.t : [];
    const maxLen = Math.max(tArr.length, ...chs.map(c => (c.v || c.values || []).length), 0);
    labels = tArr.length === maxLen
      ? tArr.map(v => Number(v).toFixed(0) + " s")
      : Array.from({{ length: maxLen }}, (_, i) => i + " s");
    datasets = chs.slice(0, 12).map((c, i) => ({{
      label: `${{c.label}} (${{c.unit || ""}})`,
      data: c.v || c.values || [],
      borderColor: palette[i % palette.length],
      borderWidth: 1.8, pointRadius: 0, tension: 0.15,
    }}));
  }} else {{
    const ser = item.ser;
    labels = (ser.t || []).map(v => Number(v).toFixed(0) + " s");
    const keys = Object.keys(ser).filter(k => k !== "t" && Array.isArray(ser[k]) && ser[k].some(x => x != null));
    datasets = keys.slice(0, 12).map((k, i) => ({{
      label: k, data: ser[k],
      borderColor: palette[i % palette.length],
      borderWidth: 1.8, pointRadius: 0, tension: 0.15,
    }}));
  }}

  CURRENT_CHART = new Chart(ctx, {{
    type: "line",
    data: {{ labels, datasets }},
    options: {{
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: {{ legend: {{ labels: {{ color: "#9ca3af" }} }} }},
      scales: {{ x: {{ ticks: {{ color: "#9ca3af", maxTicksLimit: 12 }} }}, y: {{ ticks: {{ color: "#9ca3af" }} }} }}
    }}
  }});
}}

async function previewFile(relPath) {{
  const resp = await fetch(`/api/preview?path=${{encodeURIComponent(relPath)}}`);
  if (!resp.ok) return;
  const d = await resp.json();
  const body = renderPreviewHtmlBlock(d);
  document.getElementById("view-preview").innerHTML = `
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:8px">
        <div style="display:flex;align-items:center;gap:10px">
          <button onclick="switchTab('files')">← Retour aux fichiers</button>
          <h3 style="margin:0">${{esc(relPath)}}</h3>
        </div>
        <a class="btn" download href="/api/file?path=${{encodeURIComponent(relPath)}}">⬇ Télécharger le fichier</a>
      </div>
      ${{body}}
    </div>`;
  document.getElementById("tabPreviewBtn").classList.remove("hidden");
  switchTab("preview");
}}

async function rescanDir() {{
  const btn = document.getElementById("btnRescan");
  btn.disabled = true; btn.textContent = "⏳ Scan…";
  try {{
    await fetch("/api/rescan", {{ method: "POST" }});
    await loadOverview();
  }} finally {{
    btn.disabled = false; btn.textContent = "↻ Réindexer";
  }}
}}

loadOverview();
</script>
</body>
</html>"""


def export_static_catalog_html(db: BenchSuiteDB, q: str = "", verdict: str = "", anomaly: str = "") -> str:
    """Génère un catalogue HTML autonome exportable (ouvrable sans serveur ni Python)."""
    ov = db.get_overview(q=q, verdict=verdict, anomaly=anomaly)
    rows_html = []
    for r in ov["reports"]:
        st = r["verdict_status"]
        col = "#a6e3a1" if st == "CONFORME" else "#f9e2af" if st == "A_VERIFIER" else "#f38ba8" if st == "NON_CONFORME" else "#9ca3af"
        rows_html.append(
            f"<tr><td>{_html.escape((r.get('exported_at') or '')[:16].replace('T', ' '))}</td>"
            f"<td><b>{_html.escape(r.get('system_name') or '')}</b><br><small>{_html.escape(r.get('model') or '')}</small></td>"
            f"<td>{_html.escape(r.get('serial') or '—')}</td>"
            f"<td>{_html.escape(r.get('client') or '—')}</td>"
            f"<td style='color:{col};font-weight:700'>{_html.escape(r.get('verdict_label') or st)}</td>"
            f"<td>{r.get('gpu_hotspot_max_c') or r.get('gpu_temp_max_c') or '—'}</td>"
            f"<td>{r.get('critical_faults_count', 0)}</td>"
            f"<td>{r.get('ok_count', 0)}/{r.get('bench_count', 0)}</td>"
            f"<td><code>{_html.escape(r.get('rel_path') or '')}</code></td></tr>"
        )
    meta = ov.get("meta") or {}
    db_schema = _html.escape(str(meta.get("schema_version") or DB_SCHEMA_VERSION))
    db_viewer = _html.escape(str(meta.get("viewer_version") or VIEWER_VERSION))
    return (
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'><title>Catalogue BenchSuite</title>"
        "<style>body{background:#0d0d0d;color:#ececec;font:13px 'Segoe UI',sans-serif;padding:24px}"
        "h1{color:#f97316}table{width:100%;border-collapse:collapse}th,td{border-bottom:1px solid #2a2a2a;padding:7px 9px;text-align:left}"
        "th{color:#f97316;background:#141414}</style></head><body>"
        f"<h1>Catalogue BenchSuite ({len(ov['reports'])} rapports)</h1>"
        f"<p>Viewer v{_html.escape(VIEWER_VERSION)} · BDD schéma v{db_schema} (indexée par v{db_viewer}) · "
        f"Source : {_html.escape(str(db.root_dir))} · Généré le {_dt.datetime.now():%Y-%m-%d %H:%M}</p>"
        "<table><thead><tr><th>Date</th><th>Machine</th><th>N° Série</th><th>Client</th><th>Verdict</th>"
        "<th>T° max GPU</th><th>Pannes</th><th>Tests OK</th><th>Fichier JSON</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody></table></body></html>"
    )


def make_handler(db: BenchSuiteDB):
    """Fabrique le gestionnaire HTTP lié strictement à `db`."""

    class ViewerHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send_bytes(self, data: bytes, ctype: str, status: int = 200, extra_headers: Optional[Dict[str, str]] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            if extra_headers:
                for k, v in extra_headers.items():
                    self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _send_json(self, obj: Any, status: int = 200) -> None:
            self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8", status)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            qs = urllib.parse.parse_qs(parsed.query)
            q1 = lambda k: (qs.get(k) or [""])[0]

            if parsed.path == "/":
                self._send_bytes(build_viewer_html().encode("utf-8"), "text/html; charset=utf-8")
                return

            if parsed.path == "/api/overview":
                self._send_json(db.get_overview(q=q1("q"), verdict=q1("verdict"), anomaly=q1("anomaly")))
                return

            if parsed.path == "/api/machine":
                mkey = q1("key")
                rid_raw = q1("report_id")
                rid = int(rid_raw) if rid_raw.isdigit() else None
                det = db.get_machine_detail(mkey, report_id=rid)
                if not det:
                    self._send_json({"error": "Machine introuvable"}, 404)
                    return
                self._send_json(det)
                return

            if parsed.path.startswith("/api/report/"):
                try:
                    rid = int(parsed.path.rsplit("/", 1)[-1])
                except ValueError:
                    self._send_json({"error": "ID invalide"}, 400)
                    return
                det = db.get_report_detail(rid)
                if not det:
                    self._send_json({"error": "Rapport introuvable"}, 404)
                    return
                self._send_json(det)
                return

            if parsed.path == "/api/preview":
                rel = q1("path")
                target = _safe_resolve(db.root_dir, rel)
                if not target:
                    self._send_json({"error": "Fichier inaccessible"}, 404)
                    return
                ext = target.suffix.lower()
                kind = _classify_file(target)
                if ext == ".csv":
                    prev = _parse_csv_preview(target)
                    prev["mode"] = "csv"
                    self._send_json(prev)
                elif kind == "dmesg" or ext == ".log":
                    txt, _ = _read_text_auto(target, max_bytes=1_000_000)
                    lines = [{"text": ln, "is_err": bool(DMESG_ERR_RE.search(ln))} for ln in txt.splitlines()[:2500]]
                    self._send_json({"mode": "dmesg", "error_count": sum(1 for l in lines if l["is_err"]), "lines": lines})
                else:
                    txt, _ = _read_text_auto(target, max_bytes=1_000_000)
                    if ext == ".json":
                        try:
                            txt = json.dumps(json.loads(txt), indent=2, ensure_ascii=False)[:400_000]
                        except Exception:
                            pass
                    self._send_json({"mode": "text", "text": txt[:400_000]})
                return

            if parsed.path == "/api/file":
                rel = q1("path")
                target = _safe_resolve(db.root_dir, rel)
                if not target:
                    self._send_bytes(b"Fichier introuvable ou interdit", "text/plain; charset=utf-8", 404)
                    return
                ctype, _ = mimetypes.guess_type(target.name)
                if not ctype:
                    ctype = "application/octet-stream"
                elif ctype.startswith("text/") or ctype == "application/json":
                    ctype += "; charset=utf-8"
                self._send_bytes(target.read_bytes(), ctype)
                return

            if parsed.path == "/api/export_fleet_csv":
                ov = db.get_overview(q=q1("q"), verdict=q1("verdict"), anomaly=q1("anomaly"))
                buf = io.StringIO()
                cols = ["exported_at", "system_name", "model", "serial", "client", "order_no", "operator",
                        "verdict_label", "bench_count", "ok_count", "failed_count", "critical_faults_count",
                        "gpu_temp_max_c", "gpu_hotspot_max_c", "throttle_detected", "pcie_anomalies_count",
                        "inventory_diffs_count", "rel_path"]
                w = csv.writer(buf, delimiter=";")
                w.writerow(cols)
                for r in ov["reports"]:
                    w.writerow([r.get(c, "") for c in cols])
                data = ("\ufeff" + buf.getvalue()).encode("utf-8")
                self._send_bytes(
                    data, "text/csv; charset=utf-8",
                    extra_headers={"Content-Disposition": 'attachment; filename="benchsuite_parc.csv"'},
                )
                return

            if parsed.path == "/api/export_catalog_html":
                doc = export_static_catalog_html(db, q=q1("q"), verdict=q1("verdict"), anomaly=q1("anomaly"))
                self._send_bytes(
                    doc.encode("utf-8"), "text/html; charset=utf-8",
                    extra_headers={"Content-Disposition": 'attachment; filename="benchsuite_catalogue.html"'},
                )
                return

            self._send_bytes(b"Not found", "text/plain; charset=utf-8", 404)

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/api/rescan":
                res = db.scan()
                self._send_json(res)
                return
            self._send_json({"error": "Not found"}, 404)

    return ViewerHandler


def _config_file() -> Path:
    base = Path(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"))
    return base / "BenchSuiteViewer" / "config.json"


def _remember_dir(path: Path) -> None:
    try:
        cfg = _config_file()
        cfg.parent.mkdir(parents=True, exist_ok=True)
        cfg.write_text(json.dumps({"last_dir": str(path)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _choose_directory(arg_dir: Optional[str]) -> Optional[Path]:
    """Résout le dossier cible via CLI, sélecteur graphique Tkinter (Windows) ou saisie console."""
    if arg_dir:
        p = Path(arg_dir).expanduser()
        return p if p.is_dir() else None

    last = ""
    try:
        cfg = _config_file()
        if cfg.is_file():
            last = json.loads(cfg.read_text(encoding="utf-8")).get("last_dir", "")
    except Exception:
        last = ""

    # Boîte de dialogue native Windows/Linux si un affichage est disponible
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            title="Choisir le dossier contenant les rapports BenchSuite",
            initialdir=last if (last and os.path.isdir(last)) else os.path.expanduser("~"),
        )
        root.destroy()
        if chosen:
            return Path(chosen)
        return None
    except Exception:
        pass

    default_dir = last if (last and os.path.isdir(last)) else os.getcwd()
    try:
        entered = input(f"Dossier des rapports BenchSuite [{default_dir}] : ").strip()
    except EOFError:
        entered = ""
    target = Path(entered or default_dir).expanduser()
    return target if target.is_dir() else None


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="BenchSuite Viewer — Indexeur SQLite et visionneuse web locale pour Windows/Linux"
    )
    parser.add_argument("dossier", nargs="?", help="Dossier contenant les rapports BenchSuite")
    parser.add_argument("--db", help="Chemin du fichier SQLite (défaut : <dossier>/benchsuite.db)")
    parser.add_argument("--port", type=int, default=0, help="Port HTTP local sur 127.0.0.1 (0 = automatique)")
    parser.add_argument("--scan-only", action="store_true", help="Met à jour benchsuite.db sans lancer le serveur")
    parser.add_argument("--export-html", metavar="FICHIER", help="Exporte un catalogue HTML autonome et quitte")
    parser.add_argument("--no-browser", action="store_true", help="Ne pas ouvrir automatiquement le navigateur")
    args = parser.parse_args(argv)

    root_dir = _choose_directory(args.dossier)
    if not root_dir or not root_dir.is_dir():
        print("Aucun dossier valide sélectionné. Arrêt.", file=sys.stderr)
        return 1

    _remember_dir(root_dir)
    db_path = Path(args.db).expanduser() if args.db else None
    try:
        db = BenchSuiteDB(root_dir, db_path=db_path)
    except (OSError, sqlite3.Error):
        fallback_db = _config_file().parent / "benchsuite.db"
        print(f"Dossier en lecture seule pour la BDD ; utilisation de : {fallback_db}")
        db = BenchSuiteDB(root_dir, db_path=fallback_db)
    stats = db.scan()
    print(
        f"BenchSuite Viewer v{VIEWER_VERSION} — BDD : {db.db_path} | "
        f"{stats['updated_reports']} rapport(s) mis à jour, {stats['updated_files']} fichier(s) indexé(s) en {stats['duration_ms']} ms."
    )

    if args.export_html:
        out = Path(args.export_html).expanduser()
        out.write_text(export_static_catalog_html(db), encoding="utf-8")
        print(f"Catalogue HTML autonome écrit : {out}")
        return 0

    if args.scan_only:
        return 0

    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(db))
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}/"
    print(f"Interface locale disponible sur : {url}")
    print("Appuyez sur Ctrl+C pour fermer.")
    if not args.no_browser:
        threading.Timer(0.3, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nFermeture de BenchSuite Viewer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
