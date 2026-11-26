# core/ssd_diagnostics.py — Module de diagnostic avancé et intégrité pour SSD / Stockage
import os
import re
import json
import subprocess
from core.system_info import _run, _run_root_readonly, _tool, _pci_speed_to_gen, detect_ssd_native_gen

def _get_sysfs_val(path: str) -> str:
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read().strip()
    except Exception:
        pass
    return ""

def _parse_nvme_smart(out: str) -> dict:
    res = {
        "temperature_c": None,
        "wear_pct": None,
        "power_on_hours": None,
        "power_cycles": None,
        "read_tb": None,
        "written_tb": None,
        "media_errors": None,
        "critical_warning": None,
    }
    if not out:
        return res

    # Essai JSON
    try:
        data = json.loads(out)
        if isinstance(data, dict):
            # Température (Kelvin ou Celsius selon nvme-cli)
            t = data.get("temperature")
            if t is not None:
                res["temperature_c"] = float(t) - 273.15 if float(t) > 200 else float(t)
            pu = data.get("percent_used")
            if pu is not None:
                res["wear_pct"] = float(pu)
            poh = data.get("power_on_hours")
            if poh is not None:
                res["power_on_hours"] = int(poh)
            pc = data.get("power_cycles")
            if pc is not None:
                res["power_cycles"] = int(pc)
            me = data.get("media_errors")
            if me is not None:
                res["media_errors"] = int(me)
            cw = data.get("critical_warning")
            if cw is not None:
                res["critical_warning"] = str(cw)
            dur = data.get("data_units_read")
            if dur is not None:
                res["read_tb"] = (int(dur) * 512000) / 1e12
            duw = data.get("data_units_written")
            if duw is not None:
                res["written_tb"] = (int(duw) * 512000) / 1e12
            return res
    except Exception:
        pass

    # Fallback texte brut
    for line in out.splitlines():
        line = line.strip()
        m = re.search(r"^temperature\s*:\s*([\d.]+)\s*(C|K)?", line, re.I)
        if m:
            val = float(m.group(1))
            unit = (m.group(2) or "").upper()
            if unit == "K" or val > 200:
                res["temperature_c"] = round(val - 273.15, 1)
            else:
                res["temperature_c"] = round(val, 1)
        m = re.search(r"^percentage_used\s*:\s*([\d.]+)%", line, re.I)
        if m:
            res["wear_pct"] = float(m.group(1))
        m = re.search(r"^power_on_hours\s*:\s*([\d,]+)", line, re.I)
        if m:
            res["power_on_hours"] = int(m.group(1).replace(",", ""))
        m = re.search(r"^power_cycles\s*:\s*([\d,]+)", line, re.I)
        if m:
            res["power_cycles"] = int(m.group(1).replace(",", ""))
        m = re.search(r"^media_errors\s*:\s*([\d,]+)", line, re.I)
        if m:
            res["media_errors"] = int(m.group(1).replace(",", ""))
        m = re.search(r"^critical_warning\s*:\s*(.+)", line, re.I)
        if m:
            res["critical_warning"] = m.group(1).strip()
        m = re.search(r"^data_units_read\s*:\s*([\d,]+)", line, re.I)
        if m:
            val = int(m.group(1).replace(",", ""))
            res["read_tb"] = round((val * 512000) / 1e12, 2)
        m = re.search(r"^data_units_written\s*:\s*([\d,]+)", line, re.I)
        if m:
            val = int(m.group(1).replace(",", ""))
            res["written_tb"] = round((val * 512000) / 1e12, 2)

    return res

def _parse_smartctl_smart(out: str) -> dict:
    res = {
        "temperature_c": None,
        "wear_pct": None,
        "power_on_hours": None,
        "power_cycles": None,
        "read_tb": None,
        "written_tb": None,
        "media_errors": None,
        "critical_warning": None,
        "health_passed": None,
        "sata_speed": "",
    }
    if not out:
        return res

    if "PASSED" in out:
        res["health_passed"] = True
    elif "FAILED" in out:
        res["health_passed"] = False

    m = re.search(r"^SATA Version is:\s*(.+)$", out, re.M)
    if m:
        res["sata_speed"] = m.group(1).strip()

    for line in out.splitlines():
        line_str = line.strip()

        # Optimisation pour les lignes de table ATA SMART (HDD/SATA SSD)
        parts = line_str.split()
        if len(parts) >= 10 and parts[0].isdigit():
            attr = parts[1].lower()
            raw_str = parts[9].split("(")[0].strip() # Prendre le nombre brut, ex: "40" au lieu de "40 (Min/Max...)"
            if raw_str.isdigit():
                raw_val = float(raw_str)
                if attr in ("temperature_celsius", "airflow_temperature_cel", "temperature_internal"):
                    res["temperature_c"] = raw_val
                    continue
                elif attr == "power_on_hours":
                    res["power_on_hours"] = int(raw_val)
                    continue
                elif attr == "power_cycle_count":
                    res["power_cycles"] = int(raw_val)
                    continue
                elif attr in ("media_wearout_indicator", "wear_leveling_count", "ssd_life_left"):
                    if "life_left" in attr or "wearout" in attr:
                        res["wear_pct"] = max(0.0, 100.0 - raw_val)
                    else:
                        res["wear_pct"] = raw_val
                    continue
                elif attr == "total_lbas_written":
                    res["written_tb"] = round((raw_val * 512) / 1e12, 2)
                    continue
                elif attr == "host_writes_32mib":
                    res["written_tb"] = round((raw_val * 32 * 1024 * 1024) / 1e12, 2)
                    continue
                elif attr == "total_lbas_read":
                    res["read_tb"] = round((raw_val * 512) / 1e12, 2)
                    continue
                elif attr in ("reallocated_sector_ct", "current_pending_sector", "offline_uncorrectable"):
                    if raw_val > 0:
                        if res["media_errors"] is None:
                            res["media_errors"] = 0
                        res["media_errors"] += int(raw_val)
                    continue
        # Température
        m = re.search(r"(?:Temperature_Celsius|Airflow_Temperature_Cel|Current Drive Temperature:|Temperature:)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d.]+)", line_str, re.I)
        if not m:
            m = re.search(r"^Temperature:\s*([\d.]+)", line_str, re.I)
        if m:
            try: res["temperature_c"] = float(m.group(1))
            except ValueError: pass

        # Power on hours
        m = re.search(r"(?:Power_On_Hours|Power-On_Hours|Power On Hours:)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d,]+)", line_str, re.I)
        if not m:
            m = re.search(r"^Power On Hours:\s*([\d,]+)", line_str, re.I)
        if m:
            try: res["power_on_hours"] = int(m.group(1).replace(",", "").split()[0])
            except ValueError: pass

        # Power cycles
        m = re.search(r"(?:Power_Cycle_Count|Power Cycles:)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d,]+)", line_str, re.I)
        if not m:
            m = re.search(r"^Power Cycles:\s*([\d,]+)", line_str, re.I)
        if m:
            try: res["power_cycles"] = int(m.group(1).replace(",", "").split()[0])
            except ValueError: pass

        # Wear leveling / pourcentage restant
        m = re.search(r"(?:Media_Wearout_Indicator|Percentage Used|Wear_Leveling_Count|SSD_Life_Left)\s*:\s*([\d.]+)", line_str, re.I)
        if not m:
            m = re.search(r"(?:Media_Wearout_Indicator|Wear_Leveling_Count|SSD_Life_Left)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d.]+)", line_str, re.I)
        if m:
            val = float(m.group(1))
            if "Life_Left" in line_str or "Wearout_Indicator" in line_str:
                res["wear_pct"] = max(0.0, 100.0 - val)
            else:
                res["wear_pct"] = val

        # Data units read / written (NVMe format)
        m = re.search(r"^Data Units Read:\s+[\d,]+\s+\[([\d.]+)\s*(TB|GB|MB)\]", line_str, re.I)
        if m:
            val = float(m.group(1))
            unit = m.group(2).upper()
            res["read_tb"] = round(val if unit == "TB" else (val / 1000.0 if unit == "GB" else val / 1e6), 2)
        m = re.search(r"^Data Units Written:\s+[\d,]+\s+\[([\d.]+)\s*(TB|GB|MB)\]", line_str, re.I)
        if m:
            val = float(m.group(1))
            unit = m.group(2).upper()
            res["written_tb"] = round(val if unit == "TB" else (val / 1000.0 if unit == "GB" else val / 1e6), 2)

        # Host writes / read (SATA format)
        m = re.search(r"(?:Total_LBAs_Written|Host_Writes_32MiB)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d,]+)", line_str, re.I)
        if m and res["written_tb"] is None:
            try:
                raw = int(m.group(1).replace(",", ""))
                if "32MiB" in line_str:
                    res["written_tb"] = round((raw * 32 * 1024 * 1024) / 1e12, 2)
                else:
                    res["written_tb"] = round((raw * 512) / 1e12, 2)
            except ValueError:
                pass

        # Media errors
        m = re.search(r"(?:Reallocated_Sector_Ct|UDMA_CRC_Error_Count|Media and Data Integrity Errors)\s*:\s*([\d,]+)", line_str, re.I)
        if not m:
            m = re.search(r"(?:Reallocated_Sector_Ct|UDMA_CRC_Error_Count)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d,]+)", line_str, re.I)
        if m:
            try:
                err = int(m.group(1).replace(",", "").split()[0])
                if err > 0:
                    res["media_errors"] = (res["media_errors"] or 0) + err
            except ValueError:
                pass

        # Critical warning
        m = re.search(r"^Critical Warning:\s*(.+)", line_str, re.I)
        if m:
            res["critical_warning"] = m.group(1).strip()

    return res

def _check_if_os_drive(dev_node: dict) -> tuple:
    """Vérifie récursivement si un disque ou ses partitions hébergent le système (/, /boot)."""
    is_os = False
    mounts = []
    
    def _walk(node):
        nonlocal is_os
        mp = node.get("mountpoint")
        mps = node.get("mountpoints") or ([mp] if mp else [])
        for m in mps:
            if m:
                m_str = str(m).strip()
                if m_str in ("/", "/boot", "/boot/efi"):
                    is_os = True
                if m_str and not m_str.startswith("[") and m_str not in [x.split(" ")[0] for x in mounts]:
                    n = node.get("name", "")
                    mounts.append(f"{m_str} ({n})" if n else m_str)
        for child in node.get("children", []):
            _walk(child)
            
    _walk(dev_node)
    # Trier pour mettre /, /boot en tête
    mounts.sort(key=lambda x: 0 if x.startswith("/ ") else (1 if x.startswith("/boot") else 2))
    return is_os, mounts


def get_all_ssd_diagnostics(elevated: bool = False) -> list:
    """Récupère le diagnostic complet de tous les SSD / disques du système.
    
    Si elevated est True, tente d'exécuter nvme smart-log et smartctl en root (sudo/pkexec)
    pour obtenir 100% des compteurs d'usure et d'erreurs sur tous les protocoles.
    """
    drives = []
    if not _tool("lsblk"):
        return drives

    rc, out, _ = _run(["lsblk", "-J", "-o", "NAME,KNAME,PATH,SIZE,TYPE,TRAN,MODEL,ROTA,VENDOR,SERIAL,MOUNTPOINT,MOUNTPOINTS"])
    if rc != 0 or not out:
        rc, out, _ = _run(["lsblk", "-J", "-o", "NAME,KNAME,PATH,SIZE,TYPE,TRAN,MODEL,ROTA,VENDOR,SERIAL,MOUNTPOINT"])
    if rc != 0 or not out:
        return drives

    try:
        data = json.loads(out).get("blockdevices", [])
    except Exception:
        return drives

    for dev in data:
        dtype = dev.get("type", "")
        name = dev.get("kname") or dev.get("name", "")
        if dtype != "disk" or name.startswith(("zram", "loop", "ram")):
            continue

        dev_path = f"/dev/{name}"
        size = dev.get("size", "?")
        vendor = (dev.get("vendor") or "").strip()
        model = " ".join(x for x in [vendor, (dev.get("model") or "").strip()] if x).strip() or name
        serial = (dev.get("serial") or "").strip()
        rota = str(dev.get("rota", "1"))
        tran = (dev.get("tran") or "").strip().lower()

        is_ssd = (rota == "0" or "nvme" in tran or "solid state" in model.lower() or "ssd" in model.lower())
        kind = "NVMe PCIe" if ("nvme" in tran or name.startswith("nvme")) else ("SSD SATA" if is_ssd and tran in ("sata", "ata") else ("SSD USB" if is_ssd and tran == "usb" else ("SSD" if is_ssd else "Disque Dur HDD")))

        is_os, mount_list = _check_if_os_drive(dev)
        os_label = "👑 Disque Hôte du Système d'Exploitation (OS / Boot)" if is_os else "Disque de Stockage Secondaire / Données"

        diag = {
            "dev_path": dev_path,
            "name": name,
            "model": model,
            "vendor": vendor,
            "size": size,
            "kind": kind,
            "serial": serial,
            "firmware": "",
            "is_ssd": is_ssd,
            "is_os_drive": is_os,
            "os_label": os_label,
            "mountpoints": mount_list,
            # PCIe Link info
            "pci_bus": "",
            "pcie_link_cur": "",
            "pcie_link_max": "",
            "pcie_status": "Non applicable (Disque non-NVMe/PCIe)",
            # Télémétrie & Santé
            "health_status": "Inconnu (Nécessite privilèges)",
            "wear_pct": None,
            "freshness_label": "ℹ️ Analyse SMART requiert élévation",
            "temperature_c": None,
            "power_on_hours": None,
            "power_cycles": None,
            "read_tb": None,
            "written_tb": None,
            "media_errors": 0,
            "critical_warning": "Aucun",
            "errors_summary": "Aucune erreur détectée",
        }

        # 1. Extraction Sysfs et PCIe Link pour NVMe
        if "nvme" in kind.lower():
            real = os.path.realpath(f"/sys/block/{name}/device")
            pci_dir = os.path.join(real, "device")
            
            # Bus PCI (prendre le dernier match pour avoir l'endpoint exact du SSD)
            pci_matches = re.findall(r"(\d{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7])", real)
            if pci_matches:
                diag["pci_bus"] = pci_matches[-1]

            diag["firmware"] = _get_sysfs_val(f"{real}/firmware_rev") or _get_sysfs_val(f"/sys/block/{name}/device/firmware_rev")
            if not diag["serial"]:
                diag["serial"] = _get_sysfs_val(f"{real}/serial")
            if not model or model == name:
                diag["model"] = _get_sysfs_val(f"{real}/model") or model

            # Link speed / width
            cur_s = _get_sysfs_val(f"{pci_dir}/current_link_speed")
            cur_w = _get_sysfs_val(f"{pci_dir}/current_link_width")
            max_s = _get_sysfs_val(f"{pci_dir}/max_link_speed")
            max_w = _get_sysfs_val(f"{pci_dir}/max_link_width")

            cur_gen = _pci_speed_to_gen(cur_s) if cur_s else ""
            max_gen = _pci_speed_to_gen(max_s) if max_s else ""

            if cur_gen and cur_w:
                diag["pcie_link_cur"] = f"Gen{cur_gen} x{cur_w.lstrip('x')} ({cur_s})"
            if max_gen and max_w:
                diag["pcie_link_max"] = f"Gen{max_gen} x{max_w.lstrip('x')} ({max_s})"

            if cur_gen and max_gen and cur_w and max_w:
                cw = int(cur_w.lstrip("x")) if cur_w.lstrip("x").isdigit() else 0
                mw = int(max_w.lstrip("x")) if max_w.lstrip("x").isdigit() else 0
                cg = int(cur_gen) if str(cur_gen).isdigit() else 0
                mg = int(max_gen) if str(max_gen).isdigit() else 0

                native_gen = detect_ssd_native_gen(diag["model"], diag.get("pci_bus"))
                effective_mg = native_gen if (native_gen > 0 and cg >= native_gen) else mg

                if cg < effective_mg or cw < mw:
                    diag["pcie_status"] = f"⚠️ BRIDÉ / DOWNGRADE (Actuel : Gen{cg} x{cw} vs Supporté : Gen{effective_mg} x{mw})"
                else:
                    if native_gen > 0 and cg == native_gen and mg > native_gen:
                        diag["pcie_status"] = f"🟢 Optimal — Exploitation 100% nominale (Gen{cg} x{cw} — Natif Gen{native_gen})"
                    else:
                        diag["pcie_status"] = f"🟢 Optimal — Exploitation 100% nominale (Gen{cg} x{cw})"
            elif diag["pcie_link_cur"]:
                diag["pcie_status"] = f"🟢 Actif ({diag['pcie_link_cur']})"

            # Température Sysfs Hwmon
            for root_dir in [real, f"/sys/block/{name}"]:
                if os.path.exists(root_dir):
                    for h in os.listdir(root_dir):
                        if h.startswith("hwmon"):
                            t_str = _get_sysfs_val(os.path.join(root_dir, h, "temp1_input"))
                            if t_str and t_str.isdigit():
                                diag["temperature_c"] = round(int(t_str) / 1000.0, 1)

        # 2. Interrogation SMART (nvme-cli ou smartctl)
        smart_data = {}
        if "nvme" in kind.lower() and _tool("nvme"):
            cmd = ["nvme", "smart-log", dev_path]
            rc_s, out_s, _ = _run(cmd, timeout=6) if os.geteuid() == 0 else _run_root_readonly(cmd, timeout=6)
            if out_s:
                smart_data = _parse_nvme_smart(out_s)

        if not smart_data and _tool("smartctl"):
            cmd = ["smartctl", "-a", dev_path]
            rc_s, out_s, _ = _run(cmd, timeout=6) if os.geteuid() == 0 else _run_root_readonly(cmd, timeout=6)
            if out_s:
                smart_data = _parse_smartctl_smart(out_s)

        # Merge SMART data
        if smart_data.get("temperature_c") is not None and diag["temperature_c"] is None:
            diag["temperature_c"] = smart_data["temperature_c"]
        if smart_data.get("wear_pct") is not None:
            diag["wear_pct"] = smart_data["wear_pct"]
        if smart_data.get("power_on_hours") is not None:
            diag["power_on_hours"] = smart_data["power_on_hours"]
        if smart_data.get("power_cycles") is not None:
            diag["power_cycles"] = smart_data["power_cycles"]
        if smart_data.get("read_tb") is not None:
            diag["read_tb"] = smart_data["read_tb"]
        if smart_data.get("written_tb") is not None:
            diag["written_tb"] = smart_data["written_tb"]
        if smart_data.get("media_errors") is not None:
            diag["media_errors"] = smart_data["media_errors"]
        if smart_data.get("critical_warning"):
            diag["critical_warning"] = smart_data["critical_warning"]

        # 3. Évaluation de la Fraîcheur & Santé
        if diag["media_errors"] and diag["media_errors"] > 0:
            diag["health_status"] = "🔴 ERREUR CRITIQUE (Secteurs/Cellules défaillantes)"
            diag["errors_summary"] = f"{diag['media_errors']} erreur(s) média enregistrée(s)"
        elif diag["critical_warning"] and str(diag["critical_warning"]) not in ("0", "0x0", "None", "Aucun"):
            diag["health_status"] = f"🟡 AVERTISSEMENT CRITIQUE ({diag['critical_warning']})"
        elif smart_data or diag["temperature_c"] is not None:
            diag["health_status"] = "🟢 OK (Intégrité nominale)"

        wp = diag["wear_pct"]
        poh = diag["power_on_hours"]
        tbw = diag["written_tb"]

        if wp is not None:
            if wp == 0 and (poh is None or poh < 80):
                diag["freshness_label"] = "🟢 NEUF / SORTI D'USINE (Usure : 0%)"
            elif wp <= 5:
                diag["freshness_label"] = f"🟢 EXCELLENT ÉTAT / QUASI NEUF (Usure : {wp:.0f}%)"
            elif wp <= 25:
                diag["freshness_label"] = f"🟢 TRÈS BON ÉTAT (Usure : {wp:.0f}%)"
            elif wp <= 60:
                diag["freshness_label"] = f"🟡 USURE MODÉRÉE (Usure : {wp:.0f}%)"
            elif wp <= 85:
                diag["freshness_label"] = f"🟠 USURE AVANCÉE (Usure : {wp:.0f}%)"
            else:
                diag["freshness_label"] = f"🔴 FIN DE VIE / CRITIQUE (Usure : {wp:.0f}%)"
        elif poh is not None:
            if poh < 50:
                diag["freshness_label"] = f"🟢 NEUF / TRÈS RÉCENT ({poh} h de vol)"
            elif poh < 3000:
                diag["freshness_label"] = f"🟢 BON ÉTAT ({poh} h de vol)"
            else:
                diag["freshness_label"] = f"🟡 UTILISÉ ({poh} h de vol)"

        drives.append(diag)

    return drives


def read_ssd_sensors(target_path: str = "/tmp") -> tuple[float, float, list]:
    """
    Lit en temps réel la température max (°C), la puissance totale (W) des SSD,
    ainsi qu'une liste détaillée par SSD: [(temp_1, pwr_1), (temp_2, pwr_2), ...].
    Supporte les chemins multiples (séparés par des virgules).
    Résilience Best-Effort : ne plante jamais.
    """
    paths = target_path.split(",") if target_path and "," in target_path else [target_path]
    max_temp_c = 0.0
    total_power_w = 0.0
    per_ssd = []
    
    for p in paths:
        t, pw = _read_single_ssd_sensors(p.strip())
        max_temp_c = max(max_temp_c, t)
        total_power_w += pw
        per_ssd.append((t, pw))
        
    return max_temp_c, total_power_w, per_ssd

def _read_single_ssd_sensors(target_path: str) -> tuple[float, float]:
    temp_c = 0.0
    power_w = 0.0
    try:
        dev_name = ""
        if target_path and target_path.startswith("/dev/"):
            dev_name = os.path.basename(target_path)
        elif target_path:
            check_path = target_path if os.path.exists(target_path) else os.path.dirname(target_path)
            try:
                r = subprocess.run(
                    ["df", "--output=source", check_path],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    lines = r.stdout.strip().split("\n")
                    if len(lines) > 1 and lines[1].startswith("/dev/"):
                        dev_name = os.path.basename(lines[1])
            except Exception:
                pass

        # Simplifier nvme0n1p1 -> nvme0
        ctrl_name = dev_name
        if "nvme" in ctrl_name:
            m = re.match(r"(nvme\d+)", ctrl_name)
            if m:
                ctrl_name = m.group(1)

        # Chercher dans /sys/class/hwmon/
        hwmon_dirs = []
        if ctrl_name and os.path.exists(f"/sys/class/block/{ctrl_name}/device/hwmon"):
            for h in os.listdir(f"/sys/class/block/{ctrl_name}/device/hwmon"):
                hwmon_dirs.append(os.path.join(f"/sys/class/block/{ctrl_name}/device/hwmon", h))

        if os.path.exists("/sys/class/hwmon"):
            for h in sorted(os.listdir("/sys/class/hwmon")):
                h_path = os.path.join("/sys/class/hwmon", h)
                name_val = _get_sysfs_val(os.path.join(h_path, "name"))
                # Si dev_name spécifique trouvé
                dev_link = os.path.join(h_path, "device")
                realpath = os.path.realpath(dev_link) if os.path.exists(dev_link) else ""
                if (ctrl_name and ctrl_name in realpath) or (ctrl_name and ctrl_name == name_val):
                    hwmon_dirs.insert(0, h_path)
                elif name_val == "nvme":
                    hwmon_dirs.append(h_path)

        for h_path in hwmon_dirs:
            if temp_c <= 0.0:
                for t_file in ("temp1_input", "temp2_input"):
                    t_val = _get_sysfs_val(os.path.join(h_path, t_file))
                    if t_val and t_val.isdigit() and int(t_val) > 0:
                        temp_c = round(int(t_val) / 1000.0, 1)
                        break

            if power_w <= 0.0:
                for p_file in ("power1_input", "power1_average"):
                    p_val = _get_sysfs_val(os.path.join(h_path, p_file))
                    if p_val and p_val.isdigit() and int(p_val) > 0:
                        power_w = round(int(p_val) / 1000000.0, 2)
                        break
                if power_w <= 0.0:
                    curr_val = _get_sysfs_val(os.path.join(h_path, "curr1_input"))
                    in_val = _get_sysfs_val(os.path.join(h_path, "in0_input"))
                    if not in_val:
                        in_val = _get_sysfs_val(os.path.join(h_path, "in1_input"))
                    if curr_val and curr_val.isdigit() and in_val and in_val.isdigit():
                        power_w = round((int(curr_val) / 1000.0) * (int(in_val) / 1000.0) / 1000.0, 2)

            if temp_c > 0.0 or power_w > 0.0:
                break
                
        # Fallback pour HDD ou disques sans hwmon via smartctl
        if temp_c <= 0.0 and dev_name:
            try:
                from core.privileged import run_if_elevated
                r = run_if_elevated(["smartctl", "-A", "-n", "standby", f"/dev/{dev_name}"], timeout=2)
                if r and r.stdout:
                    for line in r.stdout.splitlines():
                        m = re.search(r"(?:Temperature_Celsius|Airflow_Temperature_Cel|Current Drive Temperature:|Temperature:)\s*(?:0x[0-9a-fA-F]+)?\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*[\d-]*\s*([\d.]+)", line, re.I)
                        if not m:
                            m = re.search(r"^Temperature:\s*([\d.]+)", line, re.I)
                        if m:
                            temp_c = float(m.group(1))
                            break
            except Exception:
                pass

    except Exception:
        pass

    return temp_c, power_w
