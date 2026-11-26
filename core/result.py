"""Result models and log helpers shared by benchmarks and exporters."""

from __future__ import annotations

import re
import subprocess
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Optional


@dataclass
class BenchmarkResult:
    name: str
    category: str
    score: Optional[float]
    unit: str
    duration_s: float
    raw_output: str
    metrics: dict = field(default_factory=dict)
    status: str = "ok"
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    system_info: dict = field(default_factory=dict)
    notes: str = ""
    command: str = ""

    def __post_init__(self):
        if not self.command and self.metrics and "command" in self.metrics:
            self.command = str(self.metrics["command"])
        elif self.command and self.metrics is not None and "command" not in self.metrics:
            self.metrics["command"] = self.command

    def to_dict(self) -> dict:
        return asdict(self)


def collect_dmesg_log(lines: int = 300) -> dict:
    """Capture dmesg best-effort sans prompt interactif."""
    lines = max(20, min(int(lines or 300), 2000))
    cmd = ["sudo", "-n", "dmesg", "--ctime", "--color=never"]
    cmd_err = ["sudo", "-n", "dmesg", "--ctime", "--color=never", "--level=err,crit,alert,emerg"]
    # Vrai problème : niveau kernel err/crit/alert/emerg (dmesg --level) OU signature matérielle
    # critique. Les messages d'initialisation (thermal governor, EDAC chip selects, _OSC,
    # « bridge window failed to assign »…) ne sont volontairement PAS comptés.
    error_re = re.compile(
        r"(NVRM: Xid|Xid \d+|fallen off the bus|GPU has fallen|AER: .*(corrected|uncorrected|fatal)|"
        r"PCIe Bus Error|Machine Check|mce: \[Hardware Error\]|Hardware Error|"
        r"EDAC .*(CE|UE)\b.*error|uncorrectable|I/O error|blk_update_request|"
        r"nvme\d+: .*(I/O .*timeout|aborting|controller is down|resetting)|reset controller|Kernel panic|Oops:|BUG:|"
        r"(gpu[-_]?(burn|fryer)|stress-ng|fio|nvbandwidth|clpeak|vkpeak|sysbench)\S*\[\d+\]: segfault|Out of memory|oom-kill|thermal.*(shut ?down|critical|throttl)|"
        r"(cpu|core).*temperature above threshold|clock throttled|amdgpu.*(ring .* timeout|GPU reset)|"
        r"nvidia-nvswitch\S*: .*(error|fail|fatal)|NVLink.*(error|fail))",
        re.I,
    )
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=8)
        out = r.stdout or ""
        err = r.stderr or ""
        all_lines = out.splitlines()
        tail = "\n".join(all_lines[-lines:])
        error_lines = [line for line in all_lines if error_re.search(line)]
        benign_re = re.compile(r"snd_hda_intel .*Unknown capability", re.I)   # audio HDMI du GPU : sans conséquence
        # Niveaux kernel err et plus graves (en plus des signatures ci-dessus), dédoublonnés
        try:
            r2 = subprocess.run(cmd_err, capture_output=True, text=True, timeout=8)
            if r2.returncode == 0:
                seen = set(error_lines)
                error_lines += [l for l in r2.stdout.splitlines() if l not in seen and not benign_re.search(l)]
        except Exception:
            pass
        error_tail = "\n".join(error_lines[-lines:])
        return {
            "captured_at": datetime.now().isoformat(),
            "command": " ".join(cmd),
            "status": "ok" if r.returncode == 0 else "error",
            "returncode": r.returncode,
            "duration_s": round(time.time() - t0, 3),
            "line_count": len(all_lines),
            "tail_lines": lines,
            "error_line_count": len(error_lines),
            "error_lines": error_tail if r.returncode == 0 else "",
            "output": tail if r.returncode == 0 else "",
            "error": err.strip() if r.returncode != 0 else "",
            "note": "" if r.returncode == 0 else "dmesg requiert sudo; lancez sudo -v avant BenchSuite pour autoriser sudo -n.",
        }
    except FileNotFoundError:
        return {
            "captured_at": datetime.now().isoformat(),
            "command": " ".join(cmd),
            "status": "error",
            "returncode": -1,
            "error_line_count": 0,
            "error_lines": "",
            "output": "",
            "error": "sudo ou dmesg introuvable",
            "note": "Installer sudo/dmesg ou autoriser l'utilisateur a lire le journal kernel.",
        }
    except Exception as e:
        return {
            "captured_at": datetime.now().isoformat(),
            "command": " ".join(cmd),
            "status": "error",
            "returncode": -3,
            "error_line_count": 0,
            "error_lines": "",
            "output": "",
            "error": str(e),
            "note": "Capture dmesg best-effort impossible.",
        }
