"""
nvidia_page.py — Interface de configuration NVIDIA-SMI pour BenchSuite
- Affichage en temps réel des données thermiques, de consommation et de limites de puissance.
- Modification asynchrone de la limite de puissance (Power Limit).
- Activation/Désactivation du mode de persistance.
- Verrouillage des fréquences GPU (Core Clocks) et Mémoire (Memory Clocks).
- Sélection du mode de calcul (Compute Mode).
- Activation/Désactivation du drainage de GPU (Drain Mode).
- Réinitialisation matérielle (GPU Reset).
- Élévation de privilèges Polkit (pkexec) sécurisée et à invite unique.
"""

import re
import threading
import subprocess
import json
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from core.safety import nvidia_smi_usable
from core.privileged import run_elevated   # conservé ici : les pages CPU / SSD l'importent depuis ui.nvidia_page
from core.system_info import get_gpu_info

def get_nvidia_gpu_details(gpu_idx: int) -> dict:
    """Récupère les détails avancés du GPU via nvidia-smi."""
    from core.system_info import _run
    details = {
        "name": f"NVIDIA GPU {gpu_idx}",
        "temp": 0.0,
        "power_draw": 0.0,
        "power_limit": 80.0,
        "default_power_limit": 80.0,
        "min_power_limit": 1.0,
        "max_power_limit": 130.0,
        "persistence_mode": "Disabled",
        "pci_bus_id": "",
        "util_gpu": 0.0,
        "util_mem": 0.0,
        "vram_total": 0.0,
        "vram_used": 0.0,
        "clock_graphics": 0.0,
        "clock_memory": 0.0,
        "compute_mode": "Default",
        "max_graphics_clock": 3000.0,
        "max_memory_clock": 10000.0,
        "drain_mode": False,
        "temp_mem": None,
        "temp_shutdown": None,
        "temp_slowdown": None,
        "temp_max_op": None,
        "throttling_reasons": [],
        "errors_detected": False,
        "errors_summary": "Aucune erreur"
    }
    
    if not nvidia_smi_usable():
        return details

    # 1. Query principale des paramètres
    fields = [
        "name",
        "temperature.gpu",
        "power.draw",
        "persistence_mode",
        "pci.bus_id",
        "utilization.gpu",
        "utilization.memory",
        "memory.total",
        "memory.used",
        "clocks.gr",
        "clocks.mem",
        "compute_mode",
        "clocks.max.gr",
        "clocks.max.mem"
    ]
    
    rc, out, _ = _run([
        "nvidia-smi", "-i", str(gpu_idx),
        f"--query-gpu={','.join(fields)}",
        "--format=csv,noheader,nounits"
    ], timeout=4)
    
    if rc == 0 and out.strip():
        parts = [p.strip() for p in out.strip().split(",")]
        if len(parts) >= len(fields):
            details["name"] = parts[0]
            try: details["temp"] = float(parts[1])
            except ValueError: pass
            try: details["power_draw"] = float(parts[2])
            except ValueError: pass
            details["persistence_mode"] = parts[3]
            details["pci_bus_id"] = parts[4]
            try: details["util_gpu"] = float(parts[5])
            except ValueError: pass
            try: details["util_mem"] = float(parts[6])
            except ValueError: pass
            try: details["vram_total"] = float(parts[7])
            except ValueError: pass
            try: details["vram_used"] = float(parts[8])
            except ValueError: pass
            try: details["clock_graphics"] = float(parts[9])
            except ValueError: pass
            try: details["clock_memory"] = float(parts[10])
            except ValueError: pass
            details["compute_mode"] = parts[11]
            try: details["max_graphics_clock"] = float(parts[12])
            except ValueError: pass
            try: details["max_memory_clock"] = float(parts[13])
            except ValueError: pass
            
    # 2. Query exhaustive (-q) pour limites de puissance, températures critiques, throttling et erreurs
    rc, out, _ = _run(["nvidia-smi", "-i", str(gpu_idx), "-q"], timeout=6)
    if rc == 0 and out:
        in_throttle = False
        for line in out.splitlines():
            s = line.strip()
            if "Clocks Throttle Reasons" in s or "Clocks Event Reasons" in s:
                if "Counters" not in s:
                    in_throttle = True
            elif s.startswith("Clocks Event Reasons Counters") or s.startswith("Sparse Operation Mode") or (in_throttle and not line.startswith(" ") and not line.startswith("\t")):
                if not line.startswith(" ") and not line.startswith("\t") and "Clocks" not in s:
                    in_throttle = False
            
            if in_throttle and ":" in s:
                k, v = [x.strip() for x in s.split(":", 1)]
                if v == "Active" and k != "Idle":
                    details["throttling_reasons"].append(k)

            # Power limits
            if "Current Power Limit" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*W", s)
                if m: details["power_limit"] = float(m.group(1))
            elif "Default Power Limit" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*W", s)
                if m: details["default_power_limit"] = float(m.group(1))
            elif "Min Power Limit" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*W", s)
                if m: details["min_power_limit"] = float(m.group(1))
            elif "Max Power Limit" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*W", s)
                if m: details["max_power_limit"] = float(m.group(1))

            # Temperatures
            elif "GPU Shutdown Temp" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*C", s)
                if m: details["temp_shutdown"] = float(m.group(1))
            elif "GPU Slowdown Temp" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*C", s)
                if m: details["temp_slowdown"] = float(m.group(1))
            elif "GPU Max Operating Temp" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*C", s)
                if m: details["temp_max_op"] = float(m.group(1))
            elif "Memory Current Temp" in s and "N/A" not in s:
                m = re.search(r"([\d.]+)\s*C", s)
                if m: details["temp_mem"] = float(m.group(1))

            # Erreurs
            elif "Unrepairable Memory" in s and "N/A" not in s and "No" not in s and "0" not in s.split(":",1)[-1]:
                details["errors_detected"] = True
                details["errors_summary"] = f"Irréparable: {s.split(':',1)[-1].strip()}"
            elif ("Single Bit ECC" in s or "Double Bit ECC" in s) and "N/A" not in s:
                val = s.split(":", 1)[-1].strip()
                if val.isdigit() and int(val) > 0:
                    details["errors_detected"] = True
                    details["errors_summary"] = f"Erreur ECC ({val})"
                
    # 3. Query du drain state
    pci_id = details.get("pci_bus_id")
    if pci_id:
        if len(pci_id) > 12 and pci_id.count(":") == 2:
            parts = pci_id.split(":")
            if len(parts[0]) == 8:
                pci_id = parts[0][4:] + ":" + parts[1] + ":" + parts[2]
        rc_d, out_d, _ = _run(["nvidia-smi", "drain", "-p", pci_id, "-q"], timeout=4)
        if rc_d == 0 and "draining" in out_d.lower() and "not draining" not in out_d.lower():
            details["drain_mode"] = True
            
    return details


import sys

# Script exécuté en root (une seule invite Polkit) : reçoit la liste des commandes en JSON (argv[1]), sans gabarit de texte.
_APPLY_SCRIPT = """import subprocess, json, sys
results = {}
for name, cmd in json.loads(sys.argv[1]):
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        out = (res.stdout or "").strip()
        err = (res.stderr or "").strip()
        results[name] = {"success": res.returncode == 0, "out": out, "err": err or out or ("code " + str(res.returncode))}
    except Exception as e:
        results[name] = {"success": False, "out": "", "err": str(e)}
print(json.dumps(results))
"""

_COMPUTE_NAMES = {"0": "Default", "1": "Exclusive_Process", "2": "Prohibited"}


def _short_bus_id(pci_bus_id: str) -> str:
    """00000000:0C:01.0 → 0000:0C:01.0 (forme attendue par `nvidia-smi drain -p`)."""
    if pci_bus_id and len(pci_bus_id) > 12 and pci_bus_id.count(":") == 2:
        parts = pci_bus_id.split(":")
        if len(parts[0]) == 8:
            return parts[0][4:] + ":" + parts[1] + ":" + parts[2]
    return pci_bus_id or ""


def build_nvidia_commands(gpu_idx: int, changes: dict, pci_bus_id: str = "") -> list:
    """Traduit les SEULS réglages modifiés en commandes nvidia-smi, dans un ordre sûr.

    `changes` peut contenir : persistence_mode (bool), power_limit (W), lock_clocks ((min, max) ou None = déverrouiller),
    lock_mem_clocks ((min, max) ou None), compute_mode ("0" / "1" / "2"), drain_mode (bool).
    Ne renvoyer que ce qui change évite les faux échecs : avant, tout était renvoyé à chaque clic (ex. déverrouillage de la
    fréquence mémoire, non pris en charge par certains GPU) et un seul refus affichait « échec » alors que la puissance était appliquée."""
    i = str(gpu_idx)
    cmds = []
    if changes.get("drain_mode") is True and "persistence_mode" not in changes:
        changes = dict(changes, persistence_mode=False)      # le drainage exige la persistance désactivée
    if "persistence_mode" in changes:
        cmds.append(("persistence_mode", ["nvidia-smi", "-i", i, "-pm", "1" if changes["persistence_mode"] else "0"]))
    if "power_limit" in changes:
        cmds.append(("power_limit", ["nvidia-smi", "-i", i, "-pl", f"{float(changes['power_limit']):.0f}"]))
    if "lock_clocks" in changes:
        v = changes["lock_clocks"]
        cmds.append(("lock_clocks", ["nvidia-smi", "-i", i, "-lgc", f"{int(v[0])},{int(v[1])}"] if v
                     else ["nvidia-smi", "-i", i, "-rgc"]))
    if "lock_mem_clocks" in changes:
        v = changes["lock_mem_clocks"]
        cmds.append(("lock_mem_clocks", ["nvidia-smi", "-i", i, "-lmc", f"{int(v[0])},{int(v[1])}"] if v
                     else ["nvidia-smi", "-i", i, "-rmc"]))
    if "compute_mode" in changes:
        cmds.append(("compute_mode", ["nvidia-smi", "-i", i, "-c", str(changes["compute_mode"])]))
    if "drain_mode" in changes and _short_bus_id(pci_bus_id):
        cmds.append(("drain_mode", ["nvidia-smi", "drain", "-p", _short_bus_id(pci_bus_id), "-m",
                                    "1" if changes["drain_mode"] else "0"]))
    return cmds


def apply_nvidia_changes(gpu_idx: int, changes: dict, pci_bus_id: str = "") -> "tuple[dict, str]":
    """Exécute les commandes des réglages modifiés en root (une seule invite). Retourne (résultats par réglage, erreur globale)."""
    cmds = build_nvidia_commands(gpu_idx, changes, pci_bus_id)
    if not cmds:
        return {}, ""
    try:
        res = run_elevated([sys.executable, "-c", _APPLY_SCRIPT, json.dumps(cmds)], timeout=60)
    except subprocess.TimeoutExpired:
        return {}, "Délai d'authentification ou d'exécution dépassé."
    except Exception as e:
        return {}, f"Échec de l'exécution : {e}"
    if res.returncode != 0:
        err = (res.stderr or "").strip() or (res.stdout or "").strip() or f"Code retour {res.returncode}"
        if any(k in err.lower() for k in ("polkit", "auth", "not authorized")):
            return {}, "Authentification refusée ou annulée."
        return {}, f"Erreur : {err}"
    try:
        return json.loads((res.stdout or "").strip().splitlines()[-1]), ""
    except Exception:
        return {}, f"Réponse illisible : {(res.stdout or '').strip()[:300]}"


_SETTING_NAMES = {
    "persistence_mode": "Persistance", "power_limit": "Limite de puissance", "lock_clocks": "Fréquence GPU",
    "lock_mem_clocks": "Fréquence mémoire", "compute_mode": "Mode de calcul", "drain_mode": "Drainage",
}


def verify_nvidia_changes(changes: dict, results: dict, after: dict) -> list:
    """Compare chaque réglage demandé à la valeur RELUE sur le GPU après application.

    Retourne [(réglage, état, texte)] avec état « ok » (valeur relue = demandée, même si nvidia-smi a renvoyé un code
    d'erreur), « warn » (commande acceptée mais valeur relue différente, ou non vérifiable), « fail » (refus et valeur
    inchangée). C'est la valeur relue qui fait foi, pas le code retour de nvidia-smi."""
    out = []
    for key in ("persistence_mode", "power_limit", "lock_clocks", "lock_mem_clocks", "compute_mode", "drain_mode"):
        if key not in changes:
            continue
        r = results.get(key)
        if r is None:
            continue
        ok_cmd = bool(r.get("success"))
        err = (r.get("err") or "").splitlines()[-1][:160] if not ok_cmd else ""
        want = changes[key]
        read, match = None, None
        if key == "power_limit":
            read = after.get("power_limit")
            match = read is not None and abs(float(read) - float(want)) <= 1.0
            want_s, read_s = f"{float(want):.0f} W", (f"{float(read):.0f} W" if read is not None else "—")
        elif key == "persistence_mode":
            read = after.get("persistence_mode")
            match = (read == "Enabled") == bool(want)
            want_s, read_s = ("activée" if want else "désactivée"), (read or "—")
        elif key == "compute_mode":
            read = after.get("compute_mode")
            match = read in (_COMPUTE_NAMES.get(str(want)), str(want))
            want_s, read_s = _COMPUTE_NAMES.get(str(want), str(want)), (read or "—")
        elif key == "drain_mode":
            read = after.get("drain_mode")
            match = bool(read) == bool(want)
            want_s, read_s = ("actif" if want else "inactif"), ("actif" if read else "inactif")
        else:   # verrouillage de fréquence : nvidia-smi ne relit pas la consigne, seule la fréquence instantanée est visible
            fk = "clock_graphics" if key == "lock_clocks" else "clock_memory"
            want_s = f"{int(want[0])}–{int(want[1])} MHz" if want else "déverrouillée"
            read_s = f"{after.get(fk, 0):.0f} MHz (instantanée)"
        name = _SETTING_NAMES[key]
        if match is None:
            state = "ok" if ok_cmd else "fail"
            txt = f"{name} : {want_s} — " + ("commande acceptée" if ok_cmd else f"refusé ({err})") + f" ; fréquence lue {read_s}"
        elif match:
            state = "ok"
            txt = f"{name} : {read_s}" + ("" if ok_cmd else f" (appliqué malgré le message de nvidia-smi : {err})")
        elif ok_cmd:
            state = "warn"
            txt = f"{name} : commande acceptée mais le GPU indique {read_s} (demandé {want_s})"
        else:
            state = "fail"
            txt = f"{name} : non appliqué — {err} (le GPU indique {read_s})"
        out.append((key, state, txt))
    return out


def reset_nvidia_gpu(gpu_idx: int) -> tuple[bool, str]:
    """Réinitialise le GPU spécifié via nvidia-smi --gpu-reset."""
    try:
        res = run_elevated(["nvidia-smi", "-i", str(gpu_idx), "--gpu-reset"], timeout=30)
        if res.returncode == 0:
            return True, "Le GPU a été réinitialisé avec succès !"
        else:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"Code retour {res.returncode}"
            if "polkit" in err_msg.lower() or "auth" in err_msg.lower() or "not authorized" in err_msg.lower():
                return False, "Authentification refusée ou annulée par l'utilisateur."
            return False, f"Erreur de réinitialisation : {err_msg}"
    except subprocess.TimeoutExpired:
        return False, "Délai d'authentification ou d'exécution dépassé."
    except Exception as e:
        return False, f"Échec de l'exécution : {str(e)}"


class NvidiaPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.win = win
        self.set_margin_top(24)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.set_margin_bottom(24)

        self.gpu_list = []
        self.selected_gpu_idx = 0
        self.refresh_tag = None
        self.is_applying = False
        self._refresh_busy = False      # un seul relevé nvidia-smi à la fois (nvidia-smi -q peut dépasser 1 s sur une grosse machine)
        self._last_details = {}         # dernier relevé du GPU sélectionné (sert aux « Actuel » et au calcul des changements)
        self._clk_dirty = False
        self._mem_dirty = False
        self._loading = False

        self._build_ui()
        # Rien au démarrage de l'application : détection au premier affichage de la page (dans un thread), relevé
        # arrêté quand on quitte la page (sinon ~3 nvidia-smi / s en permanence, y compris pendant gpu-burn ou DCGM).
        self._detected = False
        self.connect("map", lambda *_: self._on_map())
        self.connect("unmap", lambda *_: self._stop_monitoring())

    def _on_map(self):
        if not self._detected:
            self._detected = True
            self.gpu_selector.remove_all()
            self.gpu_selector.append("-1", "Détection des GPU NVIDIA…")
            self.gpu_selector.set_active(0)

            def work():
                gl = []
                try:
                    for idx, g in enumerate(get_gpu_info()):
                        if g.get("fabricant") == "NVIDIA":
                            pci_id = g.get("pci_bus") or f"GPU {idx}"      # déjà normalisée (core.system_info)
                            gl.append((idx, pci_id))
                except Exception:
                    pass
                GLib.idle_add(self._fill_gpu_list, gl)
            threading.Thread(target=work, daemon=True).start()
        elif self.gpu_list:
            self._refresh_stats(first_run=True)
            self._start_monitoring()

    def _fill_gpu_list(self, gpu_list):
        """Remplit la liste des GPU (thread GTK) après la détection."""
        self.gpu_list = gpu_list
        self.gpu_selector.set_sensitive(True)
        self.gpu_selector.remove_all()
        if self.gpu_list:
            for idx, pci_id in self.gpu_list:
                self.gpu_selector.append(str(idx), f"[{idx}] {pci_id}")
            self.gpu_selector.set_active(0)        # déclenche _on_gpu_changed → premier relevé
            if self.get_mapped():                  # page quittée pendant la détection : pas de relevé en fond
                self._start_monitoring()
        else:
            self.gpu_selector.append("-1", "Aucun GPU NVIDIA détecté")
            self.gpu_selector.set_active(0)
            self.gpu_selector.set_sensitive(False)
            self.info_panel.set_visible(False)
            self.settings_panel.set_visible(False)
            self.advanced_panel.set_visible(False)
            self.btn_apply.set_visible(False)
            self.status_label.set_visible(False)
            self.no_gpu_label.set_visible(True)
        return False

    def _build_ui(self):
        # 1. En-tête
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.hdr = Gtk.Label()
        self.hdr.set_markup('<span weight="bold" size="xx-large">🟢 Configuration NVIDIA</span>')
        self.hdr.set_halign(Gtk.Align.START)
        hdr_box.append(self.hdr)
        
        desc = Gtk.Label(label="Pilotez et ajustez les paramètres d'alimentation, de fréquence et de calcul de vos cartes graphiques NVIDIA via nvidia-smi.")
        desc.add_css_class("bench-subtitle")
        desc.set_halign(Gtk.Align.START)
        desc.set_wrap(True)
        hdr_box.append(desc)
        self.append(hdr_box)

        # Message d'avertissement si pas de GPU
        self.no_gpu_label = Gtk.Label()
        self.no_gpu_label.set_markup('<span foreground="#f38ba8" size="large"><b>Aucune carte graphique NVIDIA compatible détectée.</b></span>\n\nAssurez-vous que le pilote propriétaire NVIDIA est installé et que la commande <b>nvidia-smi</b> est accessible.')
        self.no_gpu_label.set_halign(Gtk.Align.CENTER)
        self.no_gpu_label.set_valign(Gtk.Align.CENTER)
        self.no_gpu_label.set_vexpand(True)
        self.no_gpu_label.set_wrap(True)
        self.no_gpu_label.set_visible(False)
        self.append(self.no_gpu_label)

        # 2. Sélecteur de GPU
        self.selector_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        self.selector_box.add_css_class("params-panel")
        
        sel_lbl = Gtk.Label(label="Sélectionner le GPU :")
        sel_lbl.add_css_class("param-label")
        self.selector_box.append(sel_lbl)
        
        self.gpu_selector = Gtk.ComboBoxText()
        self.gpu_selector.connect("changed", self._on_gpu_changed)
        self.selector_box.append(self.gpu_selector)
        
        self.append(self.selector_box)

        # Conteneur principal (Visible si GPU trouvé)
        self.main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        
        # 3. Panneau d'informations en temps réel (Télémétrie)
        self.info_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.info_panel.add_css_class("score-box")
        
        t_lbl = Gtk.Label(label="TELEMÉTRIE LIVE")
        t_lbl.add_css_class("params-title")
        t_lbl.set_halign(Gtk.Align.START)
        self.info_panel.append(t_lbl)

        grid = Gtk.Grid()
        grid.set_row_spacing(8)
        grid.set_column_spacing(24)

        # Row 0
        self.lbl_temp_title = Gtk.Label(label="Température GPU :", halign=Gtk.Align.START)
        grid.attach(self.lbl_temp_title, 0, 0, 1, 1)
        self.lbl_temp = Gtk.Label(label="— °C", halign=Gtk.Align.START)
        self.lbl_temp.add_css_class("metric-value")
        grid.attach(self.lbl_temp, 1, 0, 1, 1)

        self.lbl_power_title = Gtk.Label(label="Consommation GPU :", halign=Gtk.Align.START)
        grid.attach(self.lbl_power_title, 2, 0, 1, 1)
        self.lbl_power = Gtk.Label(label="— W", halign=Gtk.Align.START)
        self.lbl_power.add_css_class("metric-value")
        grid.attach(self.lbl_power, 3, 0, 1, 1)

        # Row 1
        grid.attach(Gtk.Label(label="Limite de puissance :", halign=Gtk.Align.START), 0, 1, 1, 1)
        self.lbl_plimit = Gtk.Label(label="— W", halign=Gtk.Align.START)
        self.lbl_plimit.add_css_class("metric-value")
        grid.attach(self.lbl_plimit, 1, 1, 1, 1)

        grid.attach(Gtk.Label(label="Mode Persistance :", halign=Gtk.Align.START), 2, 1, 1, 1)
        self.lbl_pmode = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_pmode.add_css_class("metric-value")
        grid.attach(self.lbl_pmode, 3, 1, 1, 1)

        # Row 2
        self.lbl_clk_gpu_title = Gtk.Label(label="Fréquence GPU :", halign=Gtk.Align.START)
        grid.attach(self.lbl_clk_gpu_title, 0, 2, 1, 1)
        self.lbl_clk_gpu = Gtk.Label(label="— MHz", halign=Gtk.Align.START)
        self.lbl_clk_gpu.add_css_class("metric-value")
        grid.attach(self.lbl_clk_gpu, 1, 2, 1, 1)

        self.lbl_clk_mem_title = Gtk.Label(label="Fréquence Mémoire :", halign=Gtk.Align.START)
        grid.attach(self.lbl_clk_mem_title, 2, 2, 1, 1)
        self.lbl_clk_mem = Gtk.Label(label="— MHz", halign=Gtk.Align.START)
        self.lbl_clk_mem.add_css_class("metric-value")
        grid.attach(self.lbl_clk_mem, 3, 2, 1, 1)

        # Row 3
        grid.attach(Gtk.Label(label="Mode de calcul :", halign=Gtk.Align.START), 0, 3, 1, 1)
        self.lbl_cmode = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_cmode.add_css_class("metric-value")
        grid.attach(self.lbl_cmode, 1, 3, 1, 1)

        grid.attach(Gtk.Label(label="État Drain :", halign=Gtk.Align.START), 2, 3, 1, 1)
        self.lbl_drain = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_drain.add_css_class("metric-value")
        grid.attach(self.lbl_drain, 3, 3, 1, 1)

        # Row 4
        grid.attach(Gtk.Label(label="PCI Bus ID :", halign=Gtk.Align.START), 0, 4, 1, 1)
        self.lbl_pci = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_pci.add_css_class("metric-value")
        grid.attach(self.lbl_pci, 1, 4, 1, 1)

        grid.attach(Gtk.Label(label="Température VRAM :", halign=Gtk.Align.START), 2, 4, 1, 1)
        self.lbl_temp_mem = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_temp_mem.add_css_class("metric-value")
        grid.attach(self.lbl_temp_mem, 3, 4, 1, 1)

        # Row 5
        grid.attach(Gtk.Label(label="Seuils Temp. (Slow/Shut) :", halign=Gtk.Align.START), 0, 5, 1, 1)
        self.lbl_temp_limits = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_temp_limits.add_css_class("metric-value")
        grid.attach(self.lbl_temp_limits, 1, 5, 3, 1)

        # Row 6
        grid.attach(Gtk.Label(label="Bridage (Throttling) :", halign=Gtk.Align.START), 0, 6, 1, 1)
        self.lbl_throttling = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_throttling.add_css_class("metric-value")
        grid.attach(self.lbl_throttling, 1, 6, 1, 1)

        grid.attach(Gtk.Label(label="Intégrité & Erreurs :", halign=Gtk.Align.START), 2, 6, 1, 1)
        self.lbl_errors = Gtk.Label(label="—", halign=Gtk.Align.START)
        self.lbl_errors.add_css_class("metric-value")
        grid.attach(self.lbl_errors, 3, 6, 1, 1)

        self.info_panel.append(grid)

        # Utilisation bars
        util_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        util_box.set_margin_top(12)

        # GPU Util
        self.gpu_util_lbl = Gtk.Label(label="Activité GPU :", halign=Gtk.Align.START)
        self.gpu_util_lbl.add_css_class("bench-subtitle")
        util_box.append(self.gpu_util_lbl)
        self.bar_gpu_util = Gtk.ProgressBar()
        self.bar_gpu_util.set_show_text(True)
        util_box.append(self.bar_gpu_util)

        # Mem Util
        self.mem_util_lbl = Gtk.Label(label="Activité Contrôleur Mémoire :", halign=Gtk.Align.START)
        self.mem_util_lbl.add_css_class("bench-subtitle")
        util_box.append(self.mem_util_lbl)
        self.bar_mem_util = Gtk.ProgressBar()
        self.bar_mem_util.set_show_text(True)
        util_box.append(self.bar_mem_util)

        # VRAM
        self.vram_lbl = Gtk.Label(label="Utilisation Mémoire VRAM :", halign=Gtk.Align.START)
        self.vram_lbl.add_css_class("bench-subtitle")
        util_box.append(self.vram_lbl)
        self.bar_vram = Gtk.ProgressBar()
        self.bar_vram.set_show_text(True)
        util_box.append(self.bar_vram)

        self.info_panel.append(util_box)
        self.main_content.append(self.info_panel)

        # 4. Formulaire de paramétrage : une ligne par réglage — libellé | valeur à appliquer | valeur ACTUELLE lue sur le GPU
        #    (rafraîchie chaque seconde) pour voir tout de suite si un réglage a été pris en compte.
        self.settings_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.settings_panel.add_css_class("params-panel")

        st_lbl = Gtk.Label(label="RÉGLAGES (REQUIERT ÉLÉVATION)")
        st_lbl.add_css_class("params-title")
        st_lbl.set_halign(Gtk.Align.START)
        self.settings_panel.append(st_lbl)
        st_help = Gtk.Label(label="Seuls les réglages modifiés sont envoyés au GPU. La colonne « Actuel » est relue en direct "
                                  "sur le GPU : orange = valeur demandée différente de l'actuelle (pas encore appliquée ou refusée).")
        st_help.add_css_class("bench-subtitle")
        st_help.set_wrap(True)
        st_help.set_xalign(0)
        self.settings_panel.append(st_help)

        sg = Gtk.Grid()
        sg.set_row_spacing(10)
        sg.set_column_spacing(16)
        for col, txt in enumerate(("Réglage", "Valeur à appliquer", "Actuel (GPU)")):
            h = Gtk.Label(label=txt, halign=Gtk.Align.START)
            h.add_css_class("param-label")
            sg.attach(h, col, 0, 1, 1)
        self._cur_lbls = {}

        def _row(r, title, control, key):
            lbl = Gtk.Label(label=title, halign=Gtk.Align.START)
            sg.attach(lbl, 0, r, 1, 1)
            control.set_halign(Gtk.Align.START)
            sg.attach(control, 1, r, 1, 1)
            cur = Gtk.Label(label="—", halign=Gtk.Align.START)
            cur.add_css_class("metric-value")
            cur.set_hexpand(True)
            cur.set_xalign(0)
            sg.attach(cur, 2, r, 1, 1)
            self._cur_lbls[key] = cur

        # Power limit
        pl_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.pl_spin = Gtk.SpinButton()
        self.pl_spin.set_numeric(True)
        self.pl_spin.connect("value-changed", lambda *_: self._update_current_labels())
        pl_box.append(self.pl_spin)
        self.pl_range_lbl = Gtk.Label(label="(plage : —)")
        self.pl_range_lbl.add_css_class("bench-subtitle")
        pl_box.append(self.pl_range_lbl)
        _row(1, "Limite de puissance (W)", pl_box, "power_limit")

        # Persistence Mode
        self.pm_switch = Gtk.Switch()
        self.pm_switch.set_valign(Gtk.Align.CENTER)
        self.pm_switch.connect("notify::active", lambda *_: self._update_current_labels())
        _row(2, "Mode persistance", self.pm_switch, "persistence_mode")

        # Clocks lock (Fréquences GPU Core)
        clk_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.clk_switch = Gtk.Switch()
        self.clk_switch.set_valign(Gtk.Align.CENTER)
        self.clk_switch.connect("state-set", self._on_clk_switch_toggled)
        clk_box.append(self.clk_switch)
        self.clk_min_lbl = Gtk.Label(label="Min")
        clk_box.append(self.clk_min_lbl)
        self.clk_min_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=800, lower=200, upper=3000, step_increment=50))
        self.clk_min_spin.set_sensitive(False)
        self.clk_min_spin.connect("value-changed", self._mark_clk_dirty)
        clk_box.append(self.clk_min_spin)
        self.clk_max_lbl = Gtk.Label(label="Max (MHz)")
        clk_box.append(self.clk_max_lbl)
        self.clk_max_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=1800, lower=200, upper=3000, step_increment=50))
        self.clk_max_spin.set_sensitive(False)
        self.clk_max_spin.connect("value-changed", self._mark_clk_dirty)
        clk_box.append(self.clk_max_spin)
        _row(3, "Verrouiller fréquence GPU", clk_box, "lock_clocks")

        # Memory Clocks Lock
        clk_mem_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.clk_mem_switch = Gtk.Switch()
        self.clk_mem_switch.set_valign(Gtk.Align.CENTER)
        self.clk_mem_switch.connect("state-set", self._on_clk_mem_switch_toggled)
        clk_mem_box.append(self.clk_mem_switch)
        self.clk_mem_min_lbl = Gtk.Label(label="Min")
        clk_mem_box.append(self.clk_mem_min_lbl)
        self.clk_mem_min_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=800, lower=200, upper=10000, step_increment=100))
        self.clk_mem_min_spin.set_sensitive(False)
        self.clk_mem_min_spin.connect("value-changed", self._mark_mem_dirty)
        clk_mem_box.append(self.clk_mem_min_spin)
        self.clk_mem_max_lbl = Gtk.Label(label="Max (MHz)")
        clk_mem_box.append(self.clk_mem_max_lbl)
        self.clk_mem_max_spin = Gtk.SpinButton(adjustment=Gtk.Adjustment(value=7000, lower=200, upper=10000, step_increment=100))
        self.clk_mem_max_spin.set_sensitive(False)
        self.clk_mem_max_spin.connect("value-changed", self._mark_mem_dirty)
        clk_mem_box.append(self.clk_mem_max_spin)
        _row(4, "Verrouiller fréq. mémoire", clk_mem_box, "lock_mem_clocks")

        # Compute Mode Selector
        self.compute_mode_selector = Gtk.ComboBoxText()
        self.compute_mode_selector.append("Default", "Standard (plusieurs processus)")
        self.compute_mode_selector.append("Exclusive_Process", "Exclusif (un seul processus)")
        self.compute_mode_selector.append("Prohibited", "Interdit (désactive CUDA / calcul)")
        self.compute_mode_selector.set_active(0)
        self.compute_mode_selector.connect("changed", lambda *_: self._update_current_labels())
        _row(5, "Mode de calcul", self.compute_mode_selector, "compute_mode")

        # Drain Mode
        drain_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.drain_switch = Gtk.Switch()
        self.drain_switch.set_valign(Gtk.Align.CENTER)
        self.drain_switch.connect("notify::active", lambda *_: self._update_current_labels())
        drain_box.append(self.drain_switch)
        drain_help = Gtk.Label(label="(retire logiquement le GPU ; désactive la persistance)")
        drain_help.add_css_class("bench-subtitle")
        drain_box.append(drain_help)
        _row(6, "Mode drainage", drain_box, "drain_mode")

        self.settings_panel.append(sg)
        # Réglages en premier (avec leur valeur actuelle), télémétrie détaillée en dessous
        self.main_content.prepend(self.settings_panel)

        # 4b. Panneau d'Actions Avancées (GPU Reset)
        self.advanced_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.advanced_panel.add_css_class("params-panel")
        
        adv_lbl = Gtk.Label(label="ACTIONS AVANCÉES (REQUIERT ÉLÉVATION)")
        adv_lbl.add_css_class("params-title")
        adv_lbl.set_halign(Gtk.Align.START)
        self.advanced_panel.append(adv_lbl)

        adv_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        
        self.btn_reset_gpu = Gtk.Button(label="🔄 Réinitialiser le GPU (GPU Reset)")
        self.btn_reset_gpu.add_css_class("btn-run")
        self.btn_reset_gpu.connect("clicked", self._on_reset_gpu_clicked)
        adv_box.append(self.btn_reset_gpu)
        
        adv_help = Gtk.Label(label="Réinitialise le processeur graphique en cas d'erreur ou de plantage.")
        adv_help.add_css_class("bench-subtitle")
        adv_box.append(adv_help)
        self.advanced_panel.append(adv_box)
        
        self.main_content.append(self.advanced_panel)

        # 5. Statut et Actions de sauvegarde
        self.status_label = Gtk.Label(label="Modifiez un réglage puis « Appliquer » : seuls les réglages modifiés sont envoyés.")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.set_wrap(True)
        self.status_label.add_css_class("bench-subtitle")
        # Bouton « Appliquer » et compte rendu épinglés en haut (hors défilement), à côté du choix du GPU : visibles quelle
        # que soit la position dans la page.
        self.apply_spinner = Gtk.Spinner()
        self.apply_spinner.set_visible(False)
        self.selector_box.append(self.apply_spinner)

        self.btn_apply = Gtk.Button(label="💾 Appliquer les réglages modifiés")
        self.btn_apply.add_css_class("btn-run")
        self.btn_apply.set_hexpand(True)
        self.btn_apply.set_halign(Gtk.Align.END)
        self.btn_apply.connect("clicked", self._on_apply_clicked)
        self.selector_box.append(self.btn_apply)
        self.insert_child_after(self.status_label, self.selector_box)

        # Ajout d'une zone de défilement pour ne pas bloquer le redimensionnement de l'application
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.main_content)
        scroll.set_vexpand(True)
        
        self.append(scroll)

    def _on_clk_switch_toggled(self, _, state):
        self.clk_min_spin.set_sensitive(state)
        self.clk_max_spin.set_sensitive(state)
        self._mark_clk_dirty()
        return False

    def _on_clk_mem_switch_toggled(self, _, state):
        self.clk_mem_min_spin.set_sensitive(state)
        self.clk_mem_max_spin.set_sensitive(state)
        self._mark_mem_dirty()
        return False

    def _mark_clk_dirty(self, *_):
        """Le verrouillage de fréquence ne se relit pas sur le GPU : on n'envoie la consigne que si l'utilisateur l'a touchée."""
        if not self._loading:
            self._clk_dirty = True
            self._update_current_labels()

    def _mark_mem_dirty(self, *_):
        if not self._loading:
            self._mem_dirty = True
            self._update_current_labels()

    def _requested_changes(self) -> dict:
        """Réglages dont la valeur à appliquer diffère de la valeur actuelle lue sur le GPU (ou touchés, pour les fréquences)."""
        d = self._last_details or {}
        ch = {}
        if not d:
            return ch
        pl = round(self.pl_spin.get_value())
        if abs(pl - float(d.get("power_limit", pl))) > 0.5:
            ch["power_limit"] = pl
        pm = self.pm_switch.get_active()
        if pm != (d.get("persistence_mode") == "Enabled"):
            ch["persistence_mode"] = pm
        cm_id = self.compute_mode_selector.get_active_id() or "Default"
        cur_cm = d.get("compute_mode", "Default")
        if cm_id != cur_cm and _COMPUTE_NAMES.get(cur_cm) != cm_id:
            ch["compute_mode"] = {"Default": "0", "Exclusive_Process": "1", "Prohibited": "2"}.get(cm_id, "0")
        dr = self.drain_switch.get_active()
        if dr != bool(d.get("drain_mode")):
            ch["drain_mode"] = dr
        if self._clk_dirty:
            ch["lock_clocks"] = ((int(self.clk_min_spin.get_value()), int(self.clk_max_spin.get_value()))
                                 if self.clk_switch.get_active() else None)
        if self._mem_dirty:
            ch["lock_mem_clocks"] = ((int(self.clk_mem_min_spin.get_value()), int(self.clk_mem_max_spin.get_value()))
                                     if self.clk_mem_switch.get_active() else None)
        return ch

    def _update_current_labels(self):
        """Colonne « Actuel (GPU) » : valeurs relues ; orange si la valeur à appliquer diffère (non appliquée ou refusée)."""
        d = self._last_details
        if not d or not getattr(self, "_cur_lbls", None):
            return
        pending = self._requested_changes()
        esc = GLib.markup_escape_text

        def put(key, text):
            col = "#fab387" if key in pending else "#a6e3a1"
            extra = "  ← à appliquer" if key in pending else ""
            self._cur_lbls[key].set_markup(f'<span foreground="{col}"><b>{esc(text)}</b>{esc(extra)}</span>')

        put("power_limit", f"{d.get('power_limit', 0):.0f} W  (conso {d.get('power_draw', 0):.0f} W)")
        put("persistence_mode", "activée" if d.get("persistence_mode") == "Enabled" else "désactivée")
        put("lock_clocks", f"{d.get('clock_graphics', 0):.0f} MHz instantanée")
        put("lock_mem_clocks", f"{d.get('clock_memory', 0):.0f} MHz instantanée")
        put("compute_mode", d.get("compute_mode", "—"))
        put("drain_mode", "actif" if d.get("drain_mode") else "inactif")

    def _on_gpu_changed(self, _=None):
        active_id = self.gpu_selector.get_active_id()
        if not active_id or active_id == "-1":
            return
        self.selected_gpu_idx = int(active_id)
        self._refresh_stats(first_run=True)

    def _start_monitoring(self):
        if self.refresh_tag is None:
            self.refresh_tag = GLib.timeout_add_seconds(1, self._refresh_stats)

    def _stop_monitoring(self):
        if self.refresh_tag is not None:
            GLib.source_remove(self.refresh_tag)
            self.refresh_tag = None

    def _refresh_stats(self, first_run=False):
        if self.selected_gpu_idx is None or self.is_applying:
            return True
        if self._refresh_busy and not first_run:
            return True          # relevé précédent pas fini : on n'empile pas les nvidia-smi
        self._refresh_busy = True
        gpu_idx = self.selected_gpu_idx

        def update_ui(details):
            self._refresh_busy = False
            if self.is_applying or gpu_idx != self.selected_gpu_idx:
                return False
            self._last_details = details
                
            # Normalisation du Bus ID PCI pour affichage
            from core.system_info import _normalize_pci_bus
            pci_id = _normalize_pci_bus(details.get("pci_bus_id", "")) or "—"

            # Mises à jour textuelles
            self.hdr.set_markup(f'<span weight="bold" size="xx-large">🟢 Configuration {pci_id}</span>')
            self.lbl_temp_title.set_text(f"Température {pci_id} :")
            self.lbl_power_title.set_text(f"Consommation {pci_id} :")
            self.lbl_clk_gpu_title.set_text(f"Fréquence {pci_id} :")
            self.lbl_clk_mem_title.set_text(f"Fréquence Mémoire {pci_id} :")
            self.gpu_util_lbl.set_text(f"Activité {pci_id} :")
            self.mem_util_lbl.set_text(f"Activité Contrôleur {pci_id} :")
            self.vram_lbl.set_text(f"Mémoire VRAM {pci_id} :")

            self.lbl_temp.set_markup(f'<span foreground="#f38ba8" weight="bold">{details["temp"]:.0f} °C</span>')
            self.lbl_power.set_markup(f'<span foreground="#fab387" weight="bold">{details["power_draw"]:.1f} W</span>')
            
            min_pl = details.get("min_power_limit", 1.0)
            max_pl = details.get("max_power_limit", 130.0)
            cur_pl = details.get("power_limit", 80.0)
            self.lbl_plimit.set_markup(f'<span foreground="#a6e3a1" weight="bold">{cur_pl:.0f} W</span> <span size="small" foreground="#9399b2">(Min: {min_pl:.0f}W | Max: {max_pl:.0f}W)</span>')
            
            self.lbl_pmode.set_text(details["persistence_mode"])
            self.lbl_clk_gpu.set_text(f"{details.get('clock_graphics', 0):.0f} MHz")
            self.lbl_clk_mem.set_text(f"{details.get('clock_memory', 0):.0f} MHz")
            self.lbl_cmode.set_text(details.get("compute_mode", "—"))
            self.lbl_pci.set_text(f"{details.get('pci_bus_id', '—')}")
            
            # Temp Mémoire VRAM
            t_mem = details.get("temp_mem")
            if t_mem is not None:
                self.lbl_temp_mem.set_markup(f'<span foreground="#fab387" weight="bold">{t_mem:.0f} °C</span>')
            else:
                self.lbl_temp_mem.set_markup('<span foreground="#6c7086">N/A</span>')
                
            # Seuils Température
            t_slow = details.get("temp_slowdown")
            t_shut = details.get("temp_shutdown")
            slow_str = f"{t_slow:.0f} °C" if t_slow is not None else "N/A"
            shut_str = f"{t_shut:.0f} °C" if t_shut is not None else "N/A"
            self.lbl_temp_limits.set_markup(f'<span foreground="#f9e2af">Slowdown: <b>{slow_str}</b></span> | <span foreground="#f38ba8">Shutdown: <b>{shut_str}</b></span>')
            
            # Throttling
            reasons = details.get("throttling_reasons", [])
            if not reasons:
                self.lbl_throttling.set_markup('<span foreground="#a6e3a1" weight="bold">Aucun (Normal)</span>')
            else:
                r_str = ", ".join(reasons)
                self.lbl_throttling.set_markup(f'<span foreground="#f38ba8" weight="bold">⚠️ {GLib.markup_escape_text(r_str)}</span>')
                
            # Erreurs / Intégrité
            if details.get("errors_detected", False):
                err_summary = details.get("errors_summary", "Erreurs détectées")
                self.lbl_errors.set_markup(f'<span foreground="#f38ba8" weight="bold">⚠️ {GLib.markup_escape_text(err_summary)}</span>')
            else:
                self.lbl_errors.set_markup('<span foreground="#a6e3a1" weight="bold">Aucune (OK)</span>')
            
            # Drain status
            self.lbl_drain.set_markup(
                '<span foreground="#f38ba8" weight="bold">Draining</span>' if details.get("drain_mode")
                else '<span foreground="#a6e3a1">Not draining</span>'
            )
            
            # Progress Bars de Télémétrie
            gpu_util = details.get("util_gpu", 0.0) / 100.0
            self.bar_gpu_util.set_fraction(gpu_util)
            self.bar_gpu_util.set_text(f"{details.get('util_gpu', 0.0):.0f} %")

            mem_util = details.get("util_mem", 0.0) / 100.0
            self.bar_mem_util.set_fraction(mem_util)
            self.bar_mem_util.set_text(f"{details.get('util_mem', 0.0):.0f} %")

            vram_total = details.get("vram_total", 0.0)
            vram_used = details.get("vram_used", 0.0)
            if vram_total > 0:
                self.bar_vram.set_fraction(vram_used / vram_total)
                self.bar_vram.set_text(f"{vram_used:.0f} Mo / {vram_total:.0f} Mo")
            else:
                self.bar_vram.set_fraction(0.0)
                self.bar_vram.set_text("—")
            
            if first_run:
                self._loading = True
                self._clk_dirty = self._mem_dirty = False
                # Configuration initiale des SpinButtons basée sur le GPU sélectionné
                min_pl = details["min_power_limit"]
                max_pl = details["max_power_limit"]
                cur_pl = details["power_limit"]
                
                self.pl_range_lbl.set_text(f"(plage {min_pl:.0f}–{max_pl:.0f} W)")
                
                # Ajustement de la plage du spin button
                adj = Gtk.Adjustment(value=cur_pl, lower=min_pl, upper=max_pl, step_increment=1)
                self.pl_spin.set_adjustment(adj)
                
                # Mode Persistance
                self.pm_switch.set_active(details["persistence_mode"] == "Enabled")
                
                # Mode de calcul
                cm = details.get("compute_mode", "Default")
                if cm == "Default" or cm == "0":
                    self.compute_mode_selector.set_active(0)
                elif cm == "Exclusive_Process" or cm == "1":
                    self.compute_mode_selector.set_active(1)
                elif cm == "Prohibited" or cm == "2":
                    self.compute_mode_selector.set_active(2)

                # Drain mode
                self.drain_switch.set_active(details.get("drain_mode", False))

                # Ajustement des fréquences maximales détectées
                max_gc = details.get("max_graphics_clock", 3000.0)
                max_mc = details.get("max_memory_clock", 10000.0)
                
                cur_gc_val = details.get("clock_graphics", 1500.0)
                self.clk_min_spin.set_adjustment(Gtk.Adjustment(value=min(cur_gc_val, max_gc), lower=200, upper=max_gc, step_increment=50))
                self.clk_max_spin.set_adjustment(Gtk.Adjustment(value=max_gc, lower=200, upper=max_gc, step_increment=50))

                cur_mc_val = details.get("clock_memory", 5000.0)
                self.clk_mem_min_spin.set_adjustment(Gtk.Adjustment(value=min(cur_mc_val, max_mc), lower=200, upper=max_mc, step_increment=100))
                self.clk_mem_max_spin.set_adjustment(Gtk.Adjustment(value=max_mc, lower=200, upper=max_mc, step_increment=100))
                self._loading = False

            self._update_current_labels()
            return False

        def worker():
            try:
                details = get_nvidia_gpu_details(gpu_idx)
                GLib.idle_add(update_ui, details)
            except Exception:
                self._refresh_busy = False

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _on_apply_clicked(self, _):
        if self.is_applying:
            return
        from core.safety import system_changes_allowed, MSG_PRESERVATION
        if not system_changes_allowed():
            self.status_label.set_markup('<span foreground="#f9e2af">' + GLib.markup_escape_text(MSG_PRESERVATION) + '</span>')
            return
        changes = self._requested_changes()
        if not changes:
            self.status_label.set_markup('<span foreground="#a6e3a1">Rien à appliquer : les valeurs demandées sont déjà celles '
                                         'du GPU (colonne « Actuel »).</span>')
            return

        self.is_applying = True
        self._set_busy(True)
        names = ", ".join(_SETTING_NAMES[k] for k in changes if k in _SETTING_NAMES)
        self.status_label.set_markup('<span foreground="#fab387">Application : ' + GLib.markup_escape_text(names)
                                     + '… Validez l\'invite d\'authentification si elle apparaît.</span>')
        gpu_idx = self.selected_gpu_idx
        pci_bus_id = (self._last_details or {}).get("pci_bus_id", "")

        def done_cb(checks, err):
            self.is_applying = False
            self._set_busy(False)
            esc = GLib.markup_escape_text
            if err:
                self.status_label.set_markup(f'<span foreground="#f38ba8"><b>{esc(err)}</b></span>')
            else:
                col = {"ok": "#a6e3a1", "warn": "#f9e2af", "fail": "#f38ba8"}
                icon = {"ok": "✔", "warn": "⚠", "fail": "✘"}
                n_bad = sum(1 for _, st, _ in checks if st != "ok")
                head = ("<b>Configuration appliquée — vérifiée sur le GPU.</b>" if not n_bad else
                        f"<b>{n_bad} réglage(s) à vérifier</b> (valeurs relues sur le GPU) :")
                lines = [f'<span foreground="{col[st]}">{icon[st]} {esc(txt)}</span>' for _, st, txt in checks]
                self.status_label.set_markup(head + "\n" + "\n".join(lines))
            self._refresh_stats(first_run=True)
            return False

        def run_thread():
            results, err = apply_nvidia_changes(gpu_idx, changes, pci_bus_id)
            checks = []
            if not err:
                import time as _t
                _t.sleep(1.0)                       # laisser le pilote prendre la consigne avant de relire
                after = get_nvidia_gpu_details(gpu_idx)
                checks = verify_nvidia_changes(changes, results, after)
                if any(st == "warn" for _, st, _ in checks):
                    _t.sleep(2.0)                   # seconde lecture : certaines consignes mettent un moment à apparaître
                    after = get_nvidia_gpu_details(gpu_idx)
                    checks = verify_nvidia_changes(changes, results, after)
            GLib.idle_add(done_cb, checks, err)

        threading.Thread(target=run_thread, daemon=True).start()

    def _set_busy(self, busy: bool):
        """Grise les actions pendant une application / réinitialisation."""
        self.btn_apply.set_sensitive(not busy)
        self.gpu_selector.set_sensitive(not busy)
        self.btn_reset_gpu.set_sensitive(not busy)
        self.apply_spinner.set_visible(busy)
        if busy:
            self.apply_spinner.start()
        else:
            self.apply_spinner.stop()

    def _on_reset_gpu_clicked(self, _):
        if self.is_applying:
            return
        from core.safety import system_changes_allowed, MSG_PRESERVATION
        if not system_changes_allowed():
            self.status_label.set_markup('<span foreground="#f9e2af">' + GLib.markup_escape_text(MSG_PRESERVATION) + '</span>')
            return
            
        self.is_applying = True
        self.btn_apply.set_sensitive(False)
        self.gpu_selector.set_sensitive(False)
        self.btn_reset_gpu.set_sensitive(False)
        self.apply_spinner.set_visible(True)
        self.apply_spinner.start()
        self.status_label.set_markup('<span foreground="#fab387">Réinitialisation du GPU en cours... Veuillez valider l\'invite Polkit si elle apparaît.</span>')

        def done_cb(success, msg):
            self.is_applying = False
            self.btn_apply.set_sensitive(True)
            self.gpu_selector.set_sensitive(True)
            self.btn_reset_gpu.set_sensitive(True)
            self.apply_spinner.stop()
            self.apply_spinner.set_visible(False)
            
            col = "#a6e3a1" if success else "#f38ba8"
            self.status_label.set_markup(f'<span foreground="{col}"><b>{GLib.markup_escape_text(msg)}</b></span>')
            self._refresh_stats(first_run=True)

        def run_thread():
            success, msg = reset_nvidia_gpu(self.selected_gpu_idx)
            GLib.idle_add(done_cb, success, msg)

        threading.Thread(target=run_thread, daemon=True).start()

    def destroy(self):
        self._stop_monitoring()
        super().destroy()
