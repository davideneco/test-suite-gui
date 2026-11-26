"""
nvme_health.py — Santé d'un SSD NVMe avant / après un test fio : stabilité PCIe, erreurs média, chauffe.

Lecture seule, best-effort (jamais d'exception) : sysfs sans privilège, `nvme smart-log -o json` (essai direct puis
`sudo -n`). Sert aux profils « Stabilité » et « Chauffe » et au verdict :
  • erreurs PCIe AER (corrigées / non fatales / fatales) apparues pendant le test, largeur de lien perdue ;
  • erreurs média NVMe, avertissement critique, entrées du journal d'erreurs ;
  • chauffe : transitions de gestion thermique (throttling), temps passé au-dessus des seuils de température.
Un compteur absent (firmware, outil, droits) est simplement ignoré : « non évalué », jamais un échec.
"""

import json
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional

# Compteurs cumulatifs du journal SMART NVMe dont l'augmentation pendant un test est significative
_COUNTERS = ("media_errors", "num_err_log_entries", "warning_temp_time", "critical_comp_time",
             "thm_temp1_trans_count", "thm_temp2_trans_count", "thm_temp1_total_time", "thm_temp2_total_time",
             "unsafe_shutdowns", "percent_used")


def _run(cmd: List[str], timeout: int = 10) -> Optional[str]:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _ctrl_of(path: str) -> str:
    """Contrôleur NVMe (ex. « nvme0 ») derrière un périphérique /dev/nvmeXnY ou un dossier monté ; « » sinon."""
    dev = path.strip()
    try:
        if not dev.startswith("/dev/"):
            probe = dev if os.path.exists(dev) else os.path.dirname(dev)
            out = _run(["df", "--output=source", probe], 4) or ""
            lines = out.strip().splitlines()
            dev = lines[1].strip() if len(lines) > 1 else ""
        m = re.match(r"/dev/(nvme\d+)", dev)
        return m.group(1) if m else ""
    except Exception:
        return ""


def _pci_dir(ctrl: str) -> str:
    try:
        return os.path.realpath(f"/sys/class/nvme/{ctrl}/device")
    except Exception:
        return ""


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def _smart(ctrl: str) -> Dict:
    """Journal SMART NVMe (JSON) du contrôleur ; {} si illisible."""
    if not shutil.which("nvme"):
        return {}
    dev = f"/dev/{ctrl}"
    outputs = []
    out0 = _run(["nvme", "smart-log", dev, "-o", "json"])
    if out0:
        outputs.append(out0)
    elif os.geteuid() != 0:
        try:
            from core.privileged import run_if_elevated
            r = run_if_elevated(["nvme", "smart-log", dev, "-o", "json"], timeout=10)
            if r and r.returncode == 0 and r.stdout:
                outputs.append(r.stdout)
        except Exception:
            pass
    for out in outputs:
        try:
            data = json.loads(out)
            if isinstance(data, dict):
                return data
        except Exception:
            continue
    return {}


def snapshot(path: str) -> Dict:
    """État d'un disque NVMe (lien PCIe, compteurs AER, journal SMART) ; {} si le chemin n'est pas un NVMe."""
    ctrl = _ctrl_of(path)
    if not ctrl:
        return {}
    d = _pci_dir(ctrl)
    snap: Dict = {"ctrl": ctrl, "pci": os.path.basename(d) if d else ""}
    if d and os.path.isdir(d):
        snap["link_speed"] = _read(os.path.join(d, "current_link_speed"))
        snap["link_width"] = _read(os.path.join(d, "current_link_width"))
        snap["max_width"] = _read(os.path.join(d, "max_link_width"))
        try:
            from core.inventory import _aer_counts
            snap.update(_aer_counts(d))
        except Exception:
            pass
    sm = _smart(ctrl)
    if sm:
        snap["smart"] = {k: sm[k] for k in list(_COUNTERS) + ["critical_warning", "temperature"] if k in sm}
    return snap


def snapshot_all(path: str) -> List[Dict]:
    """Un état par disque NVMe d'un chemin ou d'une liste de chemins séparés par des virgules."""
    res, seen = [], set()
    for p in [x.strip() for x in str(path or "").split(",") if x.strip()]:
        s = snapshot(p)
        if s and s["ctrl"] not in seen:
            seen.add(s["ctrl"])
            res.append(s)
    return res


def _inc(b: Dict, a: Dict, key: str) -> int:
    try:
        return max(0, int(a.get(key, 0)) - int(b.get(key, 0)))
    except Exception:
        return 0


def diff(before: Dict, after: Dict) -> Dict:
    """Compare deux états d'un même disque → {'disque', 'findings': [{level,text}], 'compteurs': {...}}."""
    F: List[Dict] = []
    who = f"{after.get('ctrl', '?')} ({after.get('pci', '')})"
    fat, nonf, cor = _inc(before, after, "aer_fatal"), _inc(before, after, "aer_nonfatal"), _inc(before, after, "aer_cor")
    if fat or nonf:
        F.append({"level": "fail", "text": f"{who} : erreurs PCIe non corrigeables apparues pendant le test (+{fat + nonf})"})
    if cor:
        F.append({"level": "warn", "text": f"{who} : erreurs PCIe corrigées (AER) +{cor} — lien marginal (riser, câble, connecteur) ?"})
    try:
        if before.get("link_width") and after.get("link_width") and int(after["link_width"]) < int(before["link_width"]):
            F.append({"level": "fail", "text": f"{who} : largeur du lien PCIe perdue (x{before['link_width']} → x{after['link_width']})"})
    except Exception:
        pass
    sb, sa = before.get("smart") or {}, after.get("smart") or {}
    comp: Dict[str, int] = {}
    if sb and sa:
        for k in _COUNTERS:
            if k in sb and k in sa:
                comp[k] = _inc(sb, sa, k)
        if comp.get("media_errors"):
            F.append({"level": "fail", "text": f"{who} : erreurs média NVMe +{comp['media_errors']}"})
        try:
            if int(sa.get("critical_warning", 0)) != 0:
                F.append({"level": "fail", "text": f"{who} : avertissement critique NVMe actif (0x{int(sa['critical_warning']):02x})"})
        except Exception:
            pass
        if comp.get("num_err_log_entries"):
            F.append({"level": "warn", "text": f"{who} : {comp['num_err_log_entries']} nouvelle(s) entrée(s) dans le journal d'erreurs NVMe"})
        if comp.get("critical_comp_time"):
            F.append({"level": "fail", "text": f"{who} : {comp['critical_comp_time']} min au-dessus de la température critique"})
        thr = comp.get("thm_temp1_trans_count", 0) + comp.get("thm_temp2_trans_count", 0)
        if thr or comp.get("warning_temp_time"):
            F.append({"level": "warn", "text": f"{who} : throttling thermique NVMe détecté ({thr} transition(s) de gestion thermique, "
                                               f"{comp.get('warning_temp_time', 0)} min au-dessus du seuil d'avertissement)"})
    return {"disque": who, "findings": F, "compteurs": comp,
            "pcie": {"aer_cor": cor, "aer_nonfatal": nonf, "aer_fatal": fat,
                     "lien": f"x{after.get('link_width', '?')} {after.get('link_speed', '')}".strip()},
            "evalue": bool(after.get("aer_cor") is not None or sa)}


def diff_all(before: List[Dict], after: List[Dict]) -> Dict:
    """Bilan pour tous les disques NVMe d'un test (clé `metrics.nvme_sante`) ; {} si aucun NVMe."""
    bmap = {b.get("ctrl"): b for b in before or []}
    items = [diff(bmap[a["ctrl"]], a) for a in after or [] if a.get("ctrl") in bmap]
    if not items:
        return {}
    findings = [f for it in items for f in it["findings"]]
    level = "fail" if any(f["level"] == "fail" for f in findings) else ("warn" if findings else "ok")
    return {"niveau": level, "disques": items, "findings": findings}
