"""
cpu_page.py — Interface de configuration CPU (AMD/Intel) pour BenchSuite
- Affichage du pilote actif (intel_pstate, amd-pstate-epp, acpi-cpufreq).
- Modification du gouverneur CPU (performance, powersave, schedutil, etc.).
- Modification de la fréquence maximale et minimale.
- Activation/Désactivation du Turbo Boost.
- Utilisation de Polkit/Sudo pour l'élévation de privilèges.
"""

import os
import json
import threading
import sys
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, GLib

from core.system_info import get_cpu_info
from ui.nvidia_page import run_elevated

def get_cpu_topology_details() -> dict:
    """Récupère l'état actuel de la configuration CPU depuis sysfs."""
    details = {
        "driver": "Inconnu",
        "governors": ["performance", "powersave"],
        "cur_governor": "powersave",
        "epp_available": [],
        "cur_epp": "",
        "min_freq_mhz": 0,
        "max_freq_mhz": 0,
        "hardware_min_mhz": 0,
        "hardware_max_mhz": 0,
        "turbo_enabled": True,
        "turbo_supported": False,
        "turbo_path": ""
    }
    
    cpu0_path = "/sys/devices/system/cpu/cpu0/cpufreq"
    
    if os.path.exists(cpu0_path):
        try:
            with open(os.path.join(cpu0_path, "scaling_driver")) as f:
                details["driver"] = f.read().strip()
                
            with open(os.path.join(cpu0_path, "scaling_available_governors")) as f:
                details["governors"] = f.read().strip().split()
                
            with open(os.path.join(cpu0_path, "scaling_governor")) as f:
                details["cur_governor"] = f.read().strip()
                
            with open(os.path.join(cpu0_path, "cpuinfo_min_freq")) as f:
                details["hardware_min_mhz"] = int(f.read().strip()) // 1000
                
            with open(os.path.join(cpu0_path, "cpuinfo_max_freq")) as f:
                details["hardware_max_mhz"] = int(f.read().strip()) // 1000
                
            with open(os.path.join(cpu0_path, "scaling_min_freq")) as f:
                details["min_freq_mhz"] = int(f.read().strip()) // 1000
                
            with open(os.path.join(cpu0_path, "scaling_max_freq")) as f:
                details["max_freq_mhz"] = int(f.read().strip()) // 1000
                
            epp_avail_path = os.path.join(cpu0_path, "energy_performance_available_preferences")
            if os.path.exists(epp_avail_path):
                with open(epp_avail_path) as f:
                    details["epp_available"] = f.read().strip().split()
                with open(os.path.join(cpu0_path, "energy_performance_preference")) as f:
                    details["cur_epp"] = f.read().strip()
                    
        except Exception:
            pass

    # Détection Turbo Boost
    intel_turbo = "/sys/devices/system/cpu/intel_pstate/no_turbo"
    amd_boost = "/sys/devices/system/cpu/cpufreq/boost"
    
    if os.path.exists(intel_turbo):
        details["turbo_supported"] = True
        details["turbo_path"] = intel_turbo
        try:
            with open(intel_turbo) as f:
                details["turbo_enabled"] = f.read().strip() == "0"
        except Exception:
            pass
    elif os.path.exists(amd_boost):
        details["turbo_supported"] = True
        details["turbo_path"] = amd_boost
        try:
            with open(amd_boost) as f:
                details["turbo_enabled"] = f.read().strip() == "1"
        except Exception:
            pass

    return details


def apply_cpu_settings(governor: str, epp: str, min_freq_mhz: int, max_freq_mhz: int, 
                       turbo_enabled: bool, turbo_path: str) -> tuple[bool, str]:
    """Applique globalement les paramètres CPU via un script Python exécuté avec privilèges."""
    
    turbo_val = ""
    if turbo_path:
        if "intel_pstate" in turbo_path:
            turbo_val = "0" if turbo_enabled else "1"
        else:
            turbo_val = "1" if turbo_enabled else "0"

    py_script = f"""import os, glob, json

results = {{}}
success_count = 0
error_msg = ""

def write_to_all(pattern, value):
    global success_count, error_msg
    paths = glob.glob(pattern)
    if not paths:
        return
    for p in paths:
        try:
            with open(p, "w") as f:
                f.write(value)
            success_count += 1
        except Exception as e:
            error_msg = str(e)

# 1. Gouverneur
write_to_all("/sys/devices/system/cpu/cpu*/cpufreq/scaling_governor", "{governor}")

# 2. EPP (si applicable)
if "{epp}":
    write_to_all("/sys/devices/system/cpu/cpu*/cpufreq/energy_performance_preference", "{epp}")

# 3. Frequences
write_to_all("/sys/devices/system/cpu/cpu*/cpufreq/scaling_max_freq", str({max_freq_mhz} * 1000))
write_to_all("/sys/devices/system/cpu/cpu*/cpufreq/scaling_min_freq", str({min_freq_mhz} * 1000))

# 4. Turbo
if "{turbo_path}" and "{turbo_val}":
    try:
        with open("{turbo_path}", "w") as f:
            f.write("{turbo_val}")
    except Exception as e:
        error_msg = str(e)

print(json.dumps({{"success": True, "writes": success_count, "error": error_msg}}))
"""

    try:
        res = run_elevated([sys.executable, "-c", py_script], timeout=30)
        if res.returncode == 0:
            try:
                data = json.loads(res.stdout.strip())
                if data.get("error"):
                    return False, f"Erreur partielle : {data.get('error')}"
                return True, f"Configuration CPU appliquée avec succès !"
            except Exception as ex:
                return False, f"Erreur de décodage des résultats : {str(ex)}"
        else:
            err_msg = res.stderr.strip() or res.stdout.strip() or f"Code retour {res.returncode}"
            if "polkit" in err_msg.lower() or "auth" in err_msg.lower() or "not authorized" in err_msg.lower():
                return False, "Authentification refusée ou annulée."
            return False, f"Erreur d'élévation de privilèges : {err_msg}"
    except Exception as e:
        return False, f"Échec de l'exécution : {str(e)}"


class CpuPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        self.win = win
        self.set_margin_top(24)
        self.set_margin_start(28)
        self.set_margin_end(28)
        self.set_margin_bottom(24)

        self.cpu_info = get_cpu_info()
        self.topology = get_cpu_topology_details()
        self.is_applying = False
        
        self._build_ui()
        self._refresh_stats()

    def _build_ui(self):
        # 1. En-tête
        hdr_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.hdr = Gtk.Label()
        self.hdr.set_markup(f'<span weight="bold" size="xx-large">🧠 Configuration {GLib.markup_escape_text(str(self.cpu_info["modele"]))}</span>')
        self.hdr.set_halign(Gtk.Align.START)
        hdr_box.append(self.hdr)
        
        desc = Gtk.Label(label="Pilotez le comportement de gestion d'énergie (gouverneurs) et les fréquences d'horloge de votre processeur (Intel/AMD).")
        desc.add_css_class("bench-subtitle")
        desc.set_halign(Gtk.Align.START)
        desc.set_wrap(True)
        hdr_box.append(desc)
        self.append(hdr_box)

        self.main_content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)

        # 2. Informations globales (Pilote, CPU)
        info_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        info_panel.add_css_class("score-box")
        
        t_lbl = Gtk.Label(label="INFORMATIONS MATÉRIELLES")
        t_lbl.add_css_class("params-title")
        t_lbl.set_halign(Gtk.Align.START)
        info_panel.append(t_lbl)

        grid_info = Gtk.Grid()
        grid_info.set_row_spacing(8); grid_info.set_column_spacing(24)
        
        grid_info.attach(Gtk.Label(label="Architecture :", halign=Gtk.Align.START), 0, 0, 1, 1)
        lbl_arch = Gtk.Label(label=f"{self.cpu_info['coeurs_phys']} Cœurs Physiques / {self.cpu_info['threads']} Threads", halign=Gtk.Align.START)
        lbl_arch.add_css_class("metric-value")
        grid_info.attach(lbl_arch, 1, 0, 1, 1)
        
        grid_info.attach(Gtk.Label(label="Pilote actif :", halign=Gtk.Align.START), 2, 0, 1, 1)
        lbl_driver = Gtk.Label(label=self.topology["driver"], halign=Gtk.Align.START)
        lbl_driver.set_markup(f'<span foreground="#89b4fa" weight="bold">{GLib.markup_escape_text(str(self.topology["driver"]))}</span>')
        grid_info.attach(lbl_driver, 3, 0, 1, 1)
        
        grid_info.attach(Gtk.Label(label="Fréquence Max Stock :", halign=Gtk.Align.START), 0, 1, 1, 1)
        lbl_fstock = Gtk.Label(label=f"{self.topology['hardware_max_mhz']} MHz", halign=Gtk.Align.START)
        lbl_fstock.add_css_class("metric-value")
        grid_info.attach(lbl_fstock, 1, 1, 1, 1)
        
        info_panel.append(grid_info)
        self.main_content.append(info_panel)

        # 3. Paramètres de réglage
        settings_panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        settings_panel.add_css_class("params-panel")
        
        st_lbl = Gtk.Label(label="PARAMÈTRES GLOBAUX (Tous les cœurs)")
        st_lbl.add_css_class("params-title")
        st_lbl.set_halign(Gtk.Align.START)
        settings_panel.append(st_lbl)
        
        grid_set = Gtk.Grid()
        grid_set.set_row_spacing(10); grid_set.set_column_spacing(16)

        # Gouverneur
        gov_lbl = Gtk.Label(label="Gouverneur CPU :")
        gov_lbl.add_css_class("param-label"); gov_lbl.set_halign(Gtk.Align.END); gov_lbl.set_size_request(180, -1)
        grid_set.attach(gov_lbl, 0, 0, 1, 1)
        
        gov_list = Gtk.StringList.new(self.topology["governors"])
        self.gov_selector = Gtk.DropDown(model=gov_list)
        if self.topology["cur_governor"] in self.topology["governors"]:
            self.gov_selector.set_selected(self.topology["governors"].index(self.topology["cur_governor"]))
        grid_set.attach(self.gov_selector, 1, 0, 1, 1)

        row_idx = 1
        
        # EPP (Energy Performance Preference) - si dispo
        self.epp_selector = None
        if self.topology["epp_available"]:
            epp_lbl = Gtk.Label(label="Profil d'énergie (EPP) :")
            epp_lbl.add_css_class("param-label"); epp_lbl.set_halign(Gtk.Align.END)
            grid_set.attach(epp_lbl, 0, row_idx, 1, 1)
            
            epp_list = Gtk.StringList.new(self.topology["epp_available"])
            self.epp_selector = Gtk.DropDown(model=epp_list)
            if self.topology["cur_epp"] in self.topology["epp_available"]:
                self.epp_selector.set_selected(self.topology["epp_available"].index(self.topology["cur_epp"]))
            grid_set.attach(self.epp_selector, 1, row_idx, 1, 1)
            row_idx += 1

        # Fréquences limit
        min_f_lbl = Gtk.Label(label="Fréquence minimale :")
        min_f_lbl.add_css_class("param-label"); min_f_lbl.set_halign(Gtk.Align.END)
        grid_set.attach(min_f_lbl, 0, row_idx, 1, 1)
        
        h_min = self.topology["hardware_min_mhz"]
        h_max = self.topology["hardware_max_mhz"]
        if h_max <= h_min or h_max == 0:
            h_min = 800
            h_max = 6000
            
        adj_min = Gtk.Adjustment(value=self.topology["min_freq_mhz"] or h_min, lower=h_min, upper=h_max, step_increment=100)
        self.freq_min_spin = Gtk.SpinButton()
        self.freq_min_spin.set_adjustment(adj_min)
        grid_set.attach(self.freq_min_spin, 1, row_idx, 1, 1)
        row_idx += 1

        max_f_lbl = Gtk.Label(label="Fréquence maximale :")
        max_f_lbl.add_css_class("param-label"); max_f_lbl.set_halign(Gtk.Align.END)
        grid_set.attach(max_f_lbl, 0, row_idx, 1, 1)
        
        adj_max = Gtk.Adjustment(value=self.topology["max_freq_mhz"] or h_max, lower=h_min, upper=h_max, step_increment=100)
        self.freq_max_spin = Gtk.SpinButton()
        self.freq_max_spin.set_adjustment(adj_max)
        grid_set.attach(self.freq_max_spin, 1, row_idx, 1, 1)
        row_idx += 1

        # Turbo Boost
        self.turbo_switch = None
        if self.topology["turbo_supported"]:
            t_lbl = Gtk.Label(label="Activer le Turbo Boost :")
            t_lbl.add_css_class("param-label"); t_lbl.set_halign(Gtk.Align.END)
            grid_set.attach(t_lbl, 0, row_idx, 1, 1)
            
            self.turbo_switch = Gtk.Switch()
            self.turbo_switch.set_active(self.topology["turbo_enabled"])
            self.turbo_switch.set_halign(Gtk.Align.START)
            self.turbo_switch.set_valign(Gtk.Align.CENTER)
            grid_set.attach(self.turbo_switch, 1, row_idx, 1, 1)

        settings_panel.append(grid_set)
        self.main_content.append(settings_panel)

        # 4. Actions
        self.status_label = Gtk.Label(label="Prêt à appliquer.")
        self.status_label.set_halign(Gtk.Align.START)
        self.status_label.add_css_class("bench-subtitle")
        self.main_content.append(self.status_label)

        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        
        self.apply_spinner = Gtk.Spinner()
        self.apply_spinner.set_visible(False)
        btn_box.append(self.apply_spinner)

        self.btn_apply = Gtk.Button(label="💾 Appliquer la configuration CPU")
        self.btn_apply.add_css_class("btn-run")
        self.btn_apply.set_hexpand(True)
        self.btn_apply.connect("clicked", self._on_apply_clicked)
        btn_box.append(self.btn_apply)
        
        self.main_content.append(btn_box)

        # ScrolledWindow pour adaptation
        scroll = Gtk.ScrolledWindow()
        scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroll.set_child(self.main_content)
        scroll.set_vexpand(True)
        
        self.append(scroll)

    def _refresh_stats(self):
        # Peut-être mis à jour par un timer si on ajoute la fréquence live, 
        # mais la topologie change peu souvent.
        pass

    def _on_apply_clicked(self, _):
        if self.is_applying:
            return
        from core.safety import system_changes_allowed, MSG_PRESERVATION
        if not system_changes_allowed():
            self.status_label.set_markup('<span foreground="#f9e2af">' + GLib.markup_escape_text(MSG_PRESERVATION) + '</span>')
            return
            
        # Validation des fréquences
        f_min = int(self.freq_min_spin.get_value())
        f_max = int(self.freq_max_spin.get_value())
        if f_min > f_max:
            self.status_label.set_markup('<span foreground="#f38ba8"><b>Erreur :</b> La fréquence minimale ne peut excéder la maximale.</span>')
            return

        self.is_applying = True
        self.btn_apply.set_sensitive(False)
        self.apply_spinner.set_visible(True)
        self.apply_spinner.start()
        self.status_label.set_markup('<span foreground="#fab387">Application des paramètres en cours... (Validez l\'invite Polkit)</span>')

        # Récupération valeurs
        gov_item = self.gov_selector.get_selected_item()
        gov_val = gov_item.get_string() if gov_item else self.topology["cur_governor"]
        
        epp_val = ""
        if self.epp_selector:
            epp_item = self.epp_selector.get_selected_item()
            epp_val = epp_item.get_string() if epp_item else ""
            
        turbo_val = self.turbo_switch.get_active() if self.turbo_switch else False

        def done_cb(success, msg):
            self.is_applying = False
            self.btn_apply.set_sensitive(True)
            self.apply_spinner.stop()
            self.apply_spinner.set_visible(False)
            
            if success:
                self.status_label.set_markup(f'<span foreground="#a6e3a1"><b>{GLib.markup_escape_text(str(msg))}</b></span>')
                # Rafraîchir l'interface
                self.topology = get_cpu_topology_details()
            else:
                self.status_label.set_markup(f'<span foreground="#f38ba8"><b>{GLib.markup_escape_text(str(msg))}</b></span>')

        def run_thread():
            success, msg = apply_cpu_settings(
                gov_val, epp_val, f_min, f_max, turbo_val, self.topology.get("turbo_path", "")
            )
            GLib.idle_add(done_cb, success, msg)

        threading.Thread(target=run_thread, daemon=True).start()
