"""
dmesg_monitor.py — Moteur d'extraction et d'analyse des journaux du noyau Linux (dmesg / journalctl -k)
- Extraction best-effort sans privilège root via `journalctl -k` avec fallback sur `dmesg -T` ou élévation admin.
- Analyse par expressions régulières pour catégoriser la sévérité (Erreur, Avertissement, Info) et le sous-système (GPU NVIDIA, NVMe/Stockage, PCIe, CPU, Réseau).
"""

import os
import re
import subprocess
from core.benchmarks import tool_exists as _tool

def get_kernel_logs(max_lines: int = 300, level_filter: str = "all", keyword_filter: str = "", elevated: bool = False) -> dict:
    """Récupère les derniers logs du noyau et renvoie des statistiques et une liste d'entrées parsées.
    
    level_filter: "all", "error", "warning", "hardware"
    keyword_filter: sous-chaine de filtrage
    """
    out = ""
    rc = -1
    source_used = "Inconnue"

    # 1. Tentative avec journalctl -k (fonctionne sans root sur systemd moderne)
    if not elevated and _tool("journalctl"):
        cmd = ["journalctl", "-k", f"-n", str(max_lines), "--no-pager", "--output=short-iso"]
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
            if res.returncode == 0 and res.stdout.strip():
                out = res.stdout
                rc = 0
                source_used = "journalctl -k (Sans privilège)"
        except Exception:
            pass

    # 2. Si échec ou elevated=True, tentative avec dmesg -T
    if (rc != 0 or not out) and _tool("dmesg"):
        if elevated:
            try:
                from core.privileged import run_elevated
                r = run_elevated(["dmesg", "-T"], timeout=15)
                rc, out = r.returncode, r.stdout or ""
                if rc == 0:
                    source_used = "dmesg -T (Admin)"
            except Exception:
                pass
        else:
            rc, out, _ = _run_cmd(["dmesg", "-T"])
            if rc == 0:
                source_used = "dmesg -T"

    # 3. Fallback sur journalctl avec élévation si toujours vide ou permission denied
    if (rc != 0 or not out) and _tool("journalctl"):
        if elevated or os.geteuid() == 0:
            cmd = ["journalctl", "-k", "-n", str(max_lines), "--no-pager"]
            try:
                from core.privileged import run_elevated
                r = run_elevated(cmd, timeout=15)
                rc, out = r.returncode, r.stdout or ""
            except Exception:
                rc, out, _ = _run_cmd(cmd)
            if rc == 0:
                source_used = "journalctl -k (Admin)"

    if not out:
        return {
            "source": source_used,
            "total_count": 0,
            "error_count": 0,
            "warning_count": 0,
            "hw_count": 0,
            "entries": [],
            "error_message": "Impossible de lire le tampon noyau (Droits insuffisants ou aucun journal disponible)."
        }

    lines = out.strip().splitlines()
    if len(lines) > max_lines:
        lines = lines[-max_lines:]

    entries = []
    err_cnt = 0
    warn_cnt = 0
    hw_cnt = 0

    err_regex = re.compile(r"(error|failed|failure|crit|alert|emerg|fatal|call trace|segfault|nvrm.*status=0x)", re.I)
    warn_regex = re.compile(r"(warn|warning|throttled|downgrade|deprecated|retry)", re.I)
    hw_regex = re.compile(r"(nvme|nvidia|nvrm|pcie|pci|sata|ahci|usb|ext4|btrfs|thermal|cpu|acpi)", re.I)

    for line in lines:
        line_str = line.strip()
        if not line_str:
            continue

        # Extraire le timestamp si présent
        timestamp = ""
        content = line_str
        m_ts = re.match(r"^(\d{4}-\d{2}-\d{2}T[\d:+-]+|\S+\s+\d+\s+\d{2}:\d{2}:\d{2}|\[\s*\d+\.\d+\])\s*(.*)$", line_str)
        if m_ts:
            timestamp = m_ts.group(1)
            content = m_ts.group(2)

        content = re.sub(r"^\S+\s+kernel:\s*", "", content)

        # Déterminer la sévérité
        severity = "info"
        if err_regex.search(content):
            severity = "error"
            err_cnt += 1
        elif warn_regex.search(content):
            severity = "warning"
            warn_cnt += 1

        is_hw = bool(hw_regex.search(content))
        if is_hw:
            hw_cnt += 1

        # Application des filtres
        if level_filter == "error" and severity != "error":
            continue
        if level_filter == "warning" and severity not in ("error", "warning"):
            continue
        if level_filter == "hardware" and not is_hw:
            continue
        if keyword_filter and keyword_filter.lower() not in line_str.lower():
            continue

        entries.append({
            "timestamp": timestamp,
            "content": content,
            "raw": line_str,
            "severity": severity,
            "is_hw": is_hw
        })

    return {
        "source": source_used,
        "total_count": len(lines),
        "error_count": err_cnt,
        "warning_count": warn_cnt,
        "hw_count": hw_cnt,
        "entries": entries,
        "error_message": ""
    }

def _run_cmd(cmd: list, timeout: int = 8) -> tuple[int, str, str]:
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return res.returncode, res.stdout, res.stderr
    except Exception as e:
        return -1, "", str(e)
