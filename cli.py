#!/usr/bin/env python3
"""
BenchSuite CLI — Version Ligne de Commande (non-GUI)
Permet de lancer tous les stress tests, benchmarks et collectes matérielles 
dans un environnement de serveur ou terminal sans interface graphique (sans GTK4/X11).
"""
from __future__ import annotations

import sys
import os
import argparse
import re
import time
import json
import threading
from datetime import datetime
from typing import List, Dict, Any

# Ajouter le chemin courant pour importer core et exporters
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from core.result import BenchmarkResult      # léger (aucune dépendance)

_HEAVY_NAMES = ("CATALOG", "CAT_COLORS", "CAT_ORDER", "get_system_info", "tool_exists", "get_hardware_report",
                "export_all", "export_csv", "export_json", "export_pdf", "export_html", "save_to_history", "load_history")
# Commandes qui n'ont besoin ni du catalogue de tests ni des exports : elles démarrent sans les charger (≈ 0,5 s gagnée, davantage sur une machine lente)
_LIGHT_COMMANDS = ("aide", "dernier")


def _load_heavy() -> None:
    """Charge le catalogue de tests et les exports (psutil, ReportLab…) : fait une seule fois, seulement quand une commande en a besoin."""
    g = globals()
    if "CATALOG" in g:
        return
    try:
        from core import benchmarks as _b
        from exporters import export as _e
    except ImportError as e:
        print(f"Erreur d'importation : {e}")
        print("Veuillez lancer le script depuis la racine du dossier BenchSuite.")
        sys.exit(1)
    for n in _HEAVY_NAMES:
        g[n] = getattr(_b, n) if hasattr(_b, n) else getattr(_e, n)


def _is_light(argv) -> bool:
    """Vrai si la commande peut s'exécuter sans le catalogue (aide, résultats du démarrage, ports réseau, ou aucun terminal interactif)."""
    if not argv:
        try:
            return not (sys.stdin.isatty() and sys.stdout.isatty())      # sans terminal : seule l'aide s'affiche
        except Exception:
            return True
    return argv[0] in _LIGHT_COMMANDS or (argv[0] in ("net", "reseau") and len(argv) > 1 and argv[1] in ("ports", "port", "cartes"))


# Codes couleurs ANSI pour le terminal
class TermColor:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    UNDERLINE = "\033[4m"
    
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    
    BG_RED = "\033[41m"
    BG_GREEN = "\033[42m"
    BG_YELLOW = "\033[43m"
    
    @staticmethod
    def rgb(r: int, g: int, b: int) -> str:
        return f"\033[38;2;{r};{g};{b}m"

# Couleurs associées aux catégories (proches de l'UI GTK4)
CAT_TERM_COLORS = {
    "CPU": TermColor.rgb(249, 115, 22),       # Orange
    "RAM": TermColor.rgb(34, 211, 238),       # Cyan
    "Stockage": TermColor.rgb(232, 121, 249),  # Magenta/Rose
    "GPU": TermColor.rgb(167, 139, 250),      # Violet
    "Reseau": TermColor.rgb(52, 211, 153),     # Vert
}

# Variables globales pour le suivi du progrès live en mode asynchrone
current_telemetry = {}
current_pct = 0
current_msg = ""
display_lock = threading.Lock()

def print_banner():
    banner = f"""
{TermColor.BOLD}{TermColor.rgb(249, 115, 22)}  ____                  _      ____        _ _       
 | __ )  ___ _ __   ___| |__  / ___| _   _(_) |_ ___ 
 |  _ \\ / _ \\ '_ \\ / __| '_ \\ \\___ \\| | | | | __/ _ \\
 | |_) |  __/ | | | (__| | | | ___) | |_| | | ||  __/
 |____/ \\___|_| |_|\\___|_| |_||____/ \\__,_|_|\\__\\___|{TermColor.RESET}
                                {TermColor.BOLD}{TermColor.rgb(34, 211, 238)}Version CLI (Headless) 1.0.19{TermColor.RESET}
"""
    print(banner)

def print_separator(char="─", length=80):
    print(f"{TermColor.DIM}{char * length}{TermColor.RESET}")

_step_t0 = 0.0          # début de l'étape en cours (remis à zéro par run_campaign)
_last_plain = (-1, "")  # dernière ligne écrite hors terminal (journal, redirection)


def _fmt_hms(sec: float) -> str:
    sec = int(max(0, sec))
    return f"{sec // 3600}h{sec % 3600 // 60:02d}m" if sec >= 3600 else f"{sec // 60}m{sec % 60:02d}s"


def _telemetry_items() -> list:
    """Mesures en direct à afficher (seulement celles réellement reçues) : température, puissance, fréquence, charge, RAM."""
    t = current_telemetry
    items = []
    temp = t.get("temp") or t.get("ram_temp")
    if temp:
        items.append(f"{temp:.0f} °C")
    power = t.get("power") or t.get("ram_power")
    if power:
        items.append(f"{power:.0f} W")
    freq = t.get("freq") or t.get("freq_graphics")
    if freq:
        items.append(f"{freq:.0f} MHz")
    load = t.get("cpu_pct") or t.get("load") or t.get("ram_pct")
    if load:
        items.append(f"charge {load:.0f} %")
    if t.get("ram_errors") is not None:
        items.append(f"erreurs RAM {t['ram_errors']}")
    if t.get("ram_write_mibs") or t.get("ram_read_mibs"):
        items.append(f"écr./lect. {t.get('ram_write_mibs') or 0:.0f}/{t.get('ram_read_mibs') or 0:.0f} MiB/s")
    return items


def render_progress_bar(pct: int, msg: str, width: int = 25):
    """Ligne d'état du test en cours : barre, %, temps écoulé (et restant estimé), mesures en direct, message.
    Terminal : une seule ligne réécrite sur place, coupée à la largeur de la fenêtre (elle ne déborde jamais sur la ligne suivante).
    Sortie redirigée (journal, nohup, CI) : une ligne seulement quand le pourcentage ou le message change, sans codes couleur."""
    global _last_plain
    pct = max(0, min(100, int(pct or 0)))
    elapsed = time.time() - _step_t0 if _step_t0 else 0.0
    timing = _fmt_hms(elapsed)
    if 3 <= pct < 100 and elapsed >= 5:                      # estimation seulement quand elle a un sens
        timing += f" (reste ≈ {_fmt_hms(elapsed * (100 - pct) / pct)})"
    tel = " · ".join(_telemetry_items())
    if tel and re.search(r"°C|MHz|\d\s*W\b", msg or ""):
        msg = ""                                             # le test répète ses mesures dans son message : on ne les affiche qu'une fois
    try:
        tty = sys.stderr.isatty()
    except Exception:
        tty = False
    if not tty:
        key = (pct // 5, "" if tel else msg)                 # un palier de 5 % = une ligne (pas une ligne par seconde dans un journal)
        if key != _last_plain:
            _last_plain = key
            sys.stderr.write(f"  [{pct:3d}%] {timing}" + (f"  {msg}" if msg else "") + (f"  |  {tel}" if tel else "") + "\n")
            sys.stderr.flush()
        return
    try:
        cols = os.get_terminal_size(sys.stderr.fileno()).columns
    except Exception:
        cols = 100
    width = 25 if cols >= 100 else 12
    filled = int(round(width * pct / 100))
    plain = f"  [{'█' * filled}{'░' * (width - filled)}] {pct:3d}%  {timing}" + (f"  │ {tel}" if tel else "") + (f"  │ {msg}" if msg else "")
    plain = plain[:max(20, cols - 1)]
    head_len = len(f"  [{'█' * width}] {pct:3d}%")
    line = f"{TermColor.BOLD}{TermColor.CYAN}{plain[:head_len]}{TermColor.RESET}{plain[head_len:]}"
    sys.stderr.write("\r\033[2K" + line)
    sys.stderr.flush()


def clear_progress_bar():
    try:
        if sys.stderr.isatty():
            sys.stderr.write("\r\033[2K")
            sys.stderr.flush()
    except Exception:
        pass


def series_callback(point: dict):
    global current_telemetry
    with display_lock:
        current_telemetry.update(point)
        render_progress_bar(current_pct, current_msg)

def progress_callback(pct: int, msg: str):
    global current_pct, current_msg
    with display_lock:
        current_pct = pct
        current_msg = msg
        render_progress_bar(pct, msg)

def print_hardware_info():
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Collecte des informations matérielles (Best-Effort)...{TermColor.RESET}")
    print_separator("─", 80)
    report = get_hardware_report()
    
    # ── Système ──
    sys_info = report.get("systeme", {})
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Système / OS]{TermColor.RESET}")
    print(f"  {TermColor.BOLD}Nom d'hôte (node):{TermColor.RESET} {sys_info.get('hostname', 'Inconnu')}")
    print(f"  {TermColor.BOLD}Distribution/OS:{TermColor.RESET}  {sys_info.get('os', 'Inconnu')}")
    print(f"  {TermColor.BOLD}Version Kernel:{TermColor.RESET}   {sys_info.get('kernel', 'Inconnu')}")
    print(f"  {TermColor.BOLD}Architecture:{TermColor.RESET}     {sys_info.get('arch', 'Inconnu')}")
    
    # ── Carte Mère / BIOS ──
    board = report.get("carte_mere", {})
    if board:
        print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Carte Mère]{TermColor.RESET}")
        print(f"  {TermColor.BOLD}Fabricant:{TermColor.RESET} {board.get('fabricant', 'Inconnu')}")
        print(f"  {TermColor.BOLD}Produit:{TermColor.RESET}   {board.get('produit', 'Inconnu')}")
        print(f"  {TermColor.BOLD}Version:{TermColor.RESET}   {board.get('version', 'Inconnu')}")
        print(f"  {TermColor.BOLD}BIOS (Ver):{TermColor.RESET} {board.get('bios_version', 'Inconnu')} (Date: {board.get('bios_date', 'Inconnu')})")

    # ── CPU ──
    cpu_info = report.get("cpu", {})
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Processeur CPU]{TermColor.RESET}")
    print(f"  {TermColor.BOLD}Modèle:{TermColor.RESET}      {cpu_info.get('modele', 'Inconnu')}")
    print(f"  {TermColor.BOLD}Sockets:{TermColor.RESET}     {cpu_info.get('sockets', 1)}")
    print(f"  {TermColor.BOLD}Cœurs/Sock:{TermColor.RESET}  {cpu_info.get('coeurs_par_socket', 'Inconnu')}")
    print(f"  {TermColor.BOLD}Threads Tot:{TermColor.RESET} {cpu_info.get('threads_total', 'Inconnu')}")
    if "freq_max_mhz" in cpu_info:
        print(f"  {TermColor.BOLD}Fréquences:{TermColor.RESET}  {cpu_info.get('freq_min_mhz')} MHz - {cpu_info.get('freq_max_mhz')} MHz")
    if cpu_info.get("cache_l3"):
        print(f"  {TermColor.BOLD}Cache L3:{TermColor.RESET}    {cpu_info.get('cache_l3')}")
    
    # ── Mémoire ──
    mem_info = report.get("memoire", {})
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Mémoire RAM]{TermColor.RESET}")
    print(f"  {TermColor.BOLD}Taille totale:{TermColor.RESET} {mem_info.get('total_go', 'Inconnu')} Go")
    if mem_info.get("swap_go"):
        print(f"  {TermColor.BOLD}Swap total:{TermColor.RESET}    {mem_info.get('swap_go')} Go")
    if mem_info.get("type"):
        print(f"  {TermColor.BOLD}Type DDR:{TermColor.RESET}      {mem_info.get('type')}")
    if mem_info.get("vitesse_mhz"):
        print(f"  {TermColor.BOLD}Fréquence:{TermColor.RESET}     {mem_info.get('vitesse_mhz')} MHz")
    if mem_info.get("slots_utilises"):
        print(f"  {TermColor.BOLD}Slots occupés:{TermColor.RESET} {mem_info.get('slots_utilises')}")
    if "modules" in mem_info:
        print(f"  {TermColor.BOLD}Modules physiques detectés (dmidecode):{TermColor.RESET}")
        for i, mod in enumerate(mem_info["modules"]):
            print(f"    - Module #{i+1}: {mod.get('taille')} {mod.get('type')} @ {mod.get('vitesse')} ({mod.get('fabricant')} - P/N: {mod.get('part')})")
    
    # ── GPU ──
    gpus = report.get("gpu", [])
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Cartes Graphiques GPU]{TermColor.RESET}")
    if gpus:
        for idx, gpu in enumerate(gpus):
            gpu_lbl = gpu.get("pci_bus") or gpu.get("nom", "NVIDIA GPU")
            print(f"  {TermColor.BOLD}GPU #{gpu.get('index', idx)}:{TermColor.RESET} {gpu_lbl}")
            print(f"    {TermColor.BOLD}Mémoire VRAM:{TermColor.RESET}  {gpu.get('vram_mb', '?')} Mo")
            print(f"    {TermColor.BOLD}Pilote:{TermColor.RESET}        {gpu.get('driver', '?')}")
            print(f"    {TermColor.BOLD}Liaison PCIe:{TermColor.RESET}  {gpu.get('pcie_gen', '?')} {gpu.get('pcie_width', '?')}")
            print(f"    {TermColor.BOLD}Limite Power:{TermColor.RESET}  {gpu.get('power_limit_w', '?')} W")
            print(f"    {TermColor.BOLD}Fréquence Max:{TermColor.RESET} {gpu.get('freq_max_mhz', '?')} MHz")
    else:
        print("  Aucun GPU NVIDIA détecté (nvidia-smi absent ou inutilisable)")
        
    # ── Stockage ──
    disks = report.get("stockage", [])
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Périphériques de Stockage]{TermColor.RESET}")
    if disks:
        for disk in disks:
            ptype = disk.get("type", "Inconnu")
            pcolor = TermColor.GREEN if "SSD" in ptype or "NVMe" in ptype else TermColor.YELLOW
            print(f"  {TermColor.BOLD}{disk.get('peripherique', '/dev/???')}{TermColor.RESET} : {disk.get('modele', 'Modèle Inconnu')} "
                  f"({disk.get('taille_go', '???')} Go) │ {pcolor}{ptype}{TermColor.RESET}")
    else:
        print("  Aucun disque détecté dans /sys/block")
        
    # ── Réseau ──
    nets = report.get("reseau", [])
    print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Interfaces Réseau]{TermColor.RESET}")
    if nets:
        for net in nets:
            state = net.get("etat", "down")
            scolor = TermColor.GREEN if state == "up" else TermColor.RED
            speed = f" @ {net.get('vitesse_mbps')} Mbps" if net.get("vitesse_mbps") else ""
            print(f"  {TermColor.BOLD}{net.get('interface', 'eth?')}{TermColor.RESET} │ MAC: {net.get('mac', '??:??:??:??:??:??')} │ Etat: {scolor}{state}{TermColor.RESET}{speed}")
    else:
        print("  Aucune interface réseau détectée")
        
    # ── Capteurs ──
    sensors = report.get("capteurs", {})
    if sensors:
        print(f"\n{TermColor.BOLD}{TermColor.WHITE}[Capteurs Matériels]{TermColor.RESET}")
        for adapter, readings in sensors.items():
            if readings:
                readings_str = ", ".join(f"{k}: {v}" for k, v in readings.items())
                print(f"  {TermColor.BOLD}{adapter}:{TermColor.RESET} {readings_str}")
    print()

def list_benchmarks():
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Liste des Benchmarks & Stress Tests disponibles :{TermColor.RESET}\n")
    current_cat = None
    for item in CATALOG:
        cat = item["category"]
        if cat != current_cat:
            current_cat = cat
            color = CAT_TERM_COLORS.get(cat, TermColor.WHITE)
            print(f"{TermColor.BOLD}{color}■ Catégorie : {cat}{TermColor.RESET}")
            print_separator("╌", 60)
        
        req_installed = all(tool_exists(r) for r in item.get("requires", []))
        req_str = ""
        if item.get("requires"):
            status_color = TermColor.GREEN if req_installed else TermColor.RED
            req_str = f" (Dépendance: {' + '.join(item['requires'])} {status_color}[{'OK' if req_installed else 'Manquant'}]{TermColor.RESET})"
            
        print(f"  {TermColor.BOLD}{TermColor.WHITE}{item['id']:<15}{TermColor.RESET} : {item['name']}")
        print(f"    {TermColor.DIM}{item['desc']}{TermColor.RESET}{req_str}")
        print(f"    {TermColor.DIM}Paramètres par défaut : {item['params']}{TermColor.RESET}")
        print()

def parse_param_overrides(param_strings: List[str]) -> Dict[str, Dict[str, Any]]:
    overrides = {}
    if not param_strings:
        return overrides
    for ps in param_strings:
        if "=" not in ps or "." not in ps:
            print(f"{TermColor.YELLOW}Avertissement : syntaxe de paramètre ignorée '{ps}' (attendu: test_id.param=valeur){TermColor.RESET}")
            continue
        target, val_str = ps.split("=", 1)
        test_id, param_name = target.split(".", 1)
        
        # Typer la valeur
        val = val_str
        if val_str.lower() in ("true", "yes", "on"):
            val = True
        elif val_str.lower() in ("false", "no", "off"):
            val = False
        else:
            try:
                val = int(val_str)
            except ValueError:
                try:
                    val = float(val_str)
                except ValueError:
                    pass
        
        if test_id not in overrides:
            overrides[test_id] = {}
        overrides[test_id][param_name] = val
    return overrides


# ─────────────────────────────────────────────────────────────────────────────
# Campagne de tests (séquence, tests du catalogue, profils) — moteur core.runner
# ─────────────────────────────────────────────────────────────────────────────

EXIT_OK, EXIT_FAIL, EXIT_FAULT, EXIT_USAGE, EXIT_INTERRUPTED = 0, 1, 2, 3, 130


def print_result(res: BenchmarkResult):
    """Affiche le résultat d'une étape (statut, score, métriques clés, alertes matérielles)."""
    clear_progress_bar()
    if res.status == "ok":
        status_str = f"{TermColor.BG_GREEN}{TermColor.BOLD} SUCCESS {TermColor.RESET}"
        score_str = (f"{TermColor.BOLD}{TermColor.GREEN}{res.score:.2f} {res.unit}{TermColor.RESET}"
                     if res.score is not None else "—")
        print(f"  Status : {status_str} │ Score : {score_str} │ Durée : {res.duration_s:.1f} s")
        shown = 0
        skip_keys = set()
        try:      # suite fio (SNIA, Peak, CrystalDiskMark…) : un tableau « un test par ligne » plutôt que 14 clés sur des dizaines
            from exporters.export import _fio_matrix
            from clitui.run import print_table
            fm = _fio_matrix(res.metrics or {})
            if fm:
                print_table(fm[1], fm[0])
                skip_keys = fm[2]
        except Exception:
            skip_keys = set()
        for k, v in (res.metrics or {}).items():
            if k in skip_keys:
                continue
            if (k.startswith("series") or k in ("raw_output", "output", "command", "hardware_events")
                    or isinstance(v, (list, dict)) or v in ("", None, "—")):
                continue
            if shown < 14:
                print(f"    - {k}: {v}")
            shown += 1
        if shown > 14:
            print(f"    {TermColor.DIM}… {shown - 14} autres métriques dans les rapports{TermColor.RESET}")
        if (res.metrics or {}).get("avertissement"):      # ex. iperf3 vers une adresse de cette machine : débit non représentatif du réseau
            print(f"  {TermColor.YELLOW}{TermColor.BOLD}⚠ {res.metrics['avertissement']}{TermColor.RESET}")
    elif res.status == "skipped":
        reason = (res.metrics or {}).get("erreur") or "Dépendances manquantes"
        print(f"  Status : {TermColor.BG_YELLOW}{TermColor.BOLD} SKIPPED {TermColor.RESET} (Raison : {reason})")
    else:
        print(f"  Status : {TermColor.BG_RED}{TermColor.BOLD} {res.status.upper()} {TermColor.RESET}")
        msg = res.notes or (res.metrics or {}).get("erreur") or res.raw_output[:500]
        if msg:
            print(f"  {TermColor.DIM}{str(msg)[:600]}{TermColor.RESET}")
    el = (res.metrics or {}).get("electrical") or {}
    if el.get("sensor_count"):
        print(f"  {TermColor.DIM}Électronique : {el['sensor_count']} capteur(s) relevé(s) ({', '.join(el.get('sources', []))}){TermColor.RESET}")
        for a in el.get("alerts", [])[:3]:
            print(f"  {TermColor.YELLOW}⚠ {a}{TermColor.RESET}")
    inv = (res.metrics or {}).get("inventory_diff") or {}
    for f in inv.get("findings", [])[:5]:
        col = TermColor.RED if f["level"] == "fail" else TermColor.YELLOW
        print(f"  {col}{'✗' if f['level'] == 'fail' else '⚠'} Inventaire : {f['label']}{TermColor.RESET}")
    pl = (res.metrics or {}).get("pcie_links") or {}
    if pl.get("periphériques"):
        print(f"  {TermColor.DIM}Liens PCIe : {pl.get('au_maximum', 0)} périphérique(s) au maximum, {pl.get('anomalies_warn', 0)} à vérifier{TermColor.RESET}")
        for d in pl["periphériques"]:
            for f in d.get("anomalies", []):
                if f.get("level") == "warn":
                    print(f"  {TermColor.YELLOW}⚠ PCIe {d.get('nom') or d.get('bus', '?')} : {f.get('label', '')[:120]}{TermColor.RESET}")
    evs = (res.metrics or {}).get("hardware_events") or []
    for e in evs[:5]:
        col = TermColor.RED if e.get("severity") == "critical" else TermColor.YELLOW
        print(f"  {col}⚠ {e.get('category', '')} (t+{e.get('t', '?')} s){TermColor.RESET} {TermColor.DIM}{e.get('line', '')[:110]}{TermColor.RESET}")


def run_info_report(args) -> int:
    """Rapport « informations système » SANS aucun test : fiche matériel complète + inventaire dans HTML / PDF / JSON / CSV (même contenu
    que l'export de la page « Infos système » de l'interface). Best-effort : une source illisible est simplement absente du rapport."""
    from core.identity import collect_identity
    from core.system_info import collect_full_system_info
    from core import inventory as inv_mod
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Collecte des informations de la machine (sans test)…{TermColor.RESET}")
    ident = collect_identity()
    meta = build_meta(args, ident)
    sysinfo = collect_full_system_info()
    try:
        inventory = inv_mod.collect_inventory()
    except Exception:
        inventory = {}
    print(f"  Machine : {meta.get('system_name', '?')}")
    if inventory:
        print(f"  Inventaire : {inv_mod.summarize(inventory)}")
    if args.no_export:
        print(f"{TermColor.YELLOW}--no-export : aucun fichier écrit.{TermColor.RESET}")
        return EXIT_OK
    from core import cli_session as cs
    session_mode = not getattr(args, "new_report", False)
    prev_results, prev_session = [], {}
    if session_mode:                 # même dossier / mêmes fichiers que les tests de cette session : le rapport garde leurs résultats
        run_dir = cs.session_dir(args.output_dir, meta)
        prev_session = cs.load(run_dir)
        prev_results = cs.results_of(prev_session)
    else:
        run_dir = make_run_dir(args.output_dir, meta, "infos_systeme")
    os.makedirs(run_dir, exist_ok=True)
    if inventory:
        _write_json(os.path.join(run_dir, "inventaire_infos.json" if session_mode else "inventaire.json"), inventory)
    if session_mode:
        try:
            cs.save(run_dir, prev_results, inventory or None,
                    {"suite": "infos_systeme", "finished": datetime.now().isoformat(), "n_results": 0}, previous=prev_session)
        except Exception:
            pass
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Génération du rapport…{TermColor.RESET}")
    paths = export_campaign(prev_results, run_dir, meta, sysinfo, args.export, fixed_names=session_mode)
    if inventory:
        paths["inventaire"] = os.path.join(run_dir, "inventaire_infos.json" if session_mode else "inventaire.json")
    for fmt, path in paths.items():
        print(f"  ✓ {fmt.upper():<10} {path}")
    print(f"\nDossier : {run_dir}")
    return EXIT_OK


def build_meta(args, ident: dict) -> dict:
    """Métadonnées de rapport : identité machine + saisie opérateur."""
    from core.identity import best_serial
    import socket
    serial = args.serial or best_serial(ident)
    model = args.model or " ".join(x for x in (ident.get("vendor"), ident.get("model")) if x)
    name = args.sysname or (f"{model} {serial}".strip() if (model or serial) else socket.gethostname())
    return {
        "system_name": name, "operator": args.operator or "", "client": args.client or "",
        "order_no": args.order or "", "serial": serial, "model": model,
        "ambient_temp": args.ambient or "", "fan_speed": args.fan or "",
    }


def make_run_dir(out_dir: str, meta: dict, suite_id: str) -> str:
    """Dossier de campagne rangé par client / numéro de série / BenchSuite_v<ver>_<date>_<serveur>_<suite>."""
    from exporters.common import _safe_folder_part, format_report_dir_name
    parts = [out_dir]
    if meta.get("client"):
        parts.append(_safe_folder_part(meta["client"]))
    parts.append(_safe_folder_part(meta.get("serial") or meta.get("system_name") or "machine"))
    parts.append(format_report_dir_name(meta, suffix=suite_id))
    return os.path.join(*parts)


def export_campaign(results, run_dir: str, meta: dict, sysinfo: dict, formats, extra_payload=None, fixed_names: bool = False) -> dict:
    """Écrit les rapports dans le dossier de campagne (formats : csv json pdf html).

    fixed_names=True (session CLI) : les fichiers s'appellent toujours `benchsuite_report.<ext>` et `dmesg.txt` : chaque régénération
    remplace le rapport précédent au lieu d'en créer un nouveau (voir core/cli_session.py)."""
    from exporters.export import (export_csv, export_json, export_pdf, export_html, export_dmesg_file)
    want = set(formats)
    if "all" in want:
        want = {"csv", "json", "pdf", "html"}
    out = {}
    for fmt, fn in (("csv", lambda: export_csv(results, run_dir, meta, sysinfo)),
                    ("json", lambda: export_json(results, run_dir, meta, sysinfo)),
                    ("pdf", lambda: export_pdf(results, run_dir, system_info_data=sysinfo, report_meta=meta)),
                    ("html", lambda: export_html(results, run_dir, meta, sysinfo))):
        if fmt in want:
            try:
                out[fmt] = fn()
                if fixed_names:
                    from core.cli_session import fixed_report_name
                    final = fixed_report_name(out[fmt])
                    os.replace(out[fmt], final)
                    out[fmt] = final
            except Exception as exc:
                print(f"  {TermColor.RED}✗ Export {fmt.upper()} impossible : {exc}{TermColor.RESET}")
    try:
        out["dmesg"] = export_dmesg_file(run_dir, meta)
        if fixed_names and out["dmesg"]:
            final = os.path.join(run_dir, "dmesg.txt")
            os.replace(out["dmesg"], final)
            out["dmesg"] = final
    except Exception:
        pass
    return out


def _write_json(path: str, data) -> None:
    """Écrit un fichier JSON (best-effort : une erreur d'écriture ne doit pas interrompre la campagne)."""
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


_LOCK_FD = None


def _campaign_lock():
    """Verrou « une seule campagne CLI à la fois » (fichier verrouillé dans le dossier temporaire, libéré automatiquement à la fin du processus,
    même après un plantage). Retourne False si une autre campagne tourne, sinon un objet quelconque. Best-effort : sans verrou possible, on continue."""
    global _LOCK_FD
    if os.environ.get("BENCHSUITE_ALLOW_PARALLEL") == "1" or _LOCK_FD is not None:
        return True
    try:
        import fcntl, tempfile
        path = os.path.join(tempfile.gettempdir(), "benchsuite_cli.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o666)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        _LOCK_FD = fd
    except Exception:
        pass
    return True


def run_campaign(steps, args, suite_id: str = "manuel", suite_name: str = "", cooldown_s: float = 0.0,
                 resume_dir: str = "") -> int:
    """Lance (ou reprend) une campagne et retourne le code de sortie."""
    global current_telemetry, current_pct, current_msg
    from core.runner import CampaignRunner
    from core.identity import collect_identity
    from core import preflight
    from core.system_info import collect_full_system_info

    lock = _campaign_lock()
    if lock is False:
        print(f"{TermColor.RED}✗ Un autre test BenchSuite (CLI) est déjà en cours sur cette machine.{TermColor.RESET}\n"
              f"  {TermColor.DIM}Deux tests en même temps se perturbent et écriraient dans le même rapport. Attendez sa fin, "
              f"ou forcez avec BENCHSUITE_ALLOW_PARALLEL=1.{TermColor.RESET}")
        return EXIT_USAGE

    ident = collect_identity()
    defaults = {"fio_path": args.fio_path} if args.fio_path else {}

    # Critères de verdict : validés AVANT de lancer les tests (une faute de frappe ne doit pas coûter 1 h)
    from core import verdict as vd
    criteria = inline = reference = None
    try:
        if args.criteria:
            criteria = vd.load_criteria(args.criteria)
        if args.crit:
            inline = vd.parse_inline_criteria(args.crit)
        if args.reference:
            reference = vd.load_reference(args.reference)
            if args.tolerance is not None:
                reference["tolerance_pct"] = args.tolerance
    except (OSError, ValueError) as exc:
        print(f"{TermColor.RED}Erreur de critères : {exc}{TermColor.RESET}")
        return EXIT_USAGE

    from core.campaign import SequenceRecorder
    recorder = SequenceRecorder()      # courbes température / puissance de toute la campagne (rapports)

    def series_both(point):
        recorder.push(point)
        series_callback(point)

    def on_start(i, n, label, step):
        global current_pct, current_msg
        recorder.mark(f"{i + 1}. {label}"[:60])
        current_telemetry.clear(); current_pct = 0; current_msg = "Initialisation..."
        globals()['_step_t0'] = time.time(); globals()['_last_plain'] = (-1, '')
        if "pause" in step:
            print(f"\n{TermColor.DIM}[{i+1}/{n}] {label}{TermColor.RESET}")
        else:
            print(f"\n{TermColor.BOLD}[{i+1}/{n}] {label}{TermColor.RESET}")

    def on_done(i, n, res):
        print_result(res)

    from core import cli_session as cs
    session_mode = (not resume_dir) and not getattr(args, "new_report", False)   # une reprise garde son propre dossier
    sdir, prev_session = "", {}

    if resume_dir:
        runner = CampaignRunner.resume(resume_dir, on_step_start=on_start, on_step_done=on_done,
                                       on_progress=progress_callback, on_series=series_both)
        meta = runner.meta
        steps = runner.steps
        run_dir = resume_dir
        print(f"{TermColor.BOLD}{TermColor.YELLOW}▕ Reprise de la campagne : {run_dir}{TermColor.RESET}")
        n_done = len([i for i in runner.done_idx if i >= 0])
        print(f"  {n_done}/{len(steps)} étape(s) déjà terminée(s) ; le test interrompu est rejoué.")
        sysinfo = collect_full_system_info()
    else:
        meta = build_meta(args, ident)
        run_dir = make_run_dir(args.output_dir, meta, suite_id)
        if session_mode:
            # Tous les lancements depuis le démarrage de la machine alimentent le MÊME rapport ; le détail de chaque lancement va dans runs/
            sdir = cs.session_dir(args.output_dir, meta)
            prev_session = cs.load(sdir)
            run_dir = os.path.join(sdir, "runs", os.path.basename(run_dir))
        print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Campagne : {suite_name or suite_id}{TermColor.RESET}  "
              f"({len(steps)} étape(s))")
        print(f"  Machine : {meta['system_name']}" + (f" │ Série : {meta['serial']}" if meta.get('serial') else "")
              + (f" │ Client : {meta['client']}" if meta.get('client') else ""))
        if session_mode:
            n_prev = len(prev_session.get("results", []))
            print(f"  Rapport : {sdir}" + (f"  {TermColor.DIM}(session en cours : {n_prev} résultat(s) déjà enregistré(s), les nouveaux s'y ajoutent){TermColor.RESET}"
                                          if n_prev else f"  {TermColor.DIM}(nouvelle session : démarrée avec la machine){TermColor.RESET}"))
        else:
            print(f"  Dossier : {run_dir}")
        runner = CampaignRunner(steps, run_dir, meta, defaults, cooldown_s=cooldown_s,
                                stop_on_critical=not args.continue_on_fault, suite_name=suite_id,
                                on_step_start=on_start, on_step_done=on_done,
                                on_progress=progress_callback, on_series=series_both)
        # Vérifications avant test
        if not args.no_preflight:
            need_fio = any("fio" in s for s in steps)
            fio_paths = [s.get("path") or args.fio_path for s in steps if "fio" in s]
            fio_paths = [p for p in fio_paths if p] or (["/tmp"] if need_fio else [])
            items = preflight.run_preflight(out_dir=args.output_dir, target_paths=fio_paths,
                                            target_need_mb=2048 if need_fio else 0,
                                            expected_gpus=args.expected_gpus,
                                            gpus=any(("bench" in s and str(s["bench"]).startswith("gpu")) for s in steps))
            if items:
                print(f"\n{TermColor.BOLD}Vérifications avant test :{TermColor.RESET}")
                print(preflight.format_items(items))
            if preflight.has_blocking(items):
                print(f"\n{TermColor.BG_RED}{TermColor.BOLD} ARRÊT {TermColor.RESET} Corrigez les erreurs ci-dessus "
                      f"(ou --no-preflight pour ignorer).")
                return EXIT_USAGE
        print(f"\n{TermColor.DIM}Collecte de l'inventaire matériel (départ)…{TermColor.RESET}")
        sysinfo = collect_full_system_info()
    if getattr(args, "preflight_only", False):
        print("Vérifications terminées (--preflight-only).")
        return EXIT_OK

    # Inventaire matériel AVANT les tests (conservé tel quel à la reprise d'une campagne)
    from core import inventory as inv_mod
    inv_before = None
    if not getattr(args, "no_inventory", False):
        inv_before = meta.get("inventory_before")
        if not inv_before:
            inv_before = inv_mod.collect_inventory()
            meta["inventory_before"] = inv_before
            _write_json(os.path.join(run_dir, "inventaire_avant.json"), inv_before)
            print(f"  Inventaire (avant) : {inv_mod.summarize(inv_before)}")

    runner.install_signal_handlers()
    print_separator("─", 80)
    results, status = runner.run()
    clear_progress_bar()
    print_separator("─", 80)

    try:
        seq = recorder.export()
        if seq:
            meta["sequence_series"] = seq
    except Exception:
        pass

    # Inventaire APRÈS les tests et comparaison (disparition, lien PCIe dégradé, erreurs apparues…)
    inv_diff = None
    if inv_before and results:
        inv_after = inv_mod.collect_inventory()
        inv_diff = inv_mod.diff_inventory(inv_before, inv_after)
        if session_mode and prev_session.get("inventory_before"):
            # session : on compare à l'inventaire d'AVANT LE PREMIER test depuis le démarrage (pas seulement à celui de ce lancement)
            meta["inventory_before"] = prev_session["inventory_before"]
            inv_diff = inv_mod.diff_inventory(prev_session["inventory_before"], inv_after)
        meta["inventory_after"] = inv_after
        meta["inventory_diff"] = inv_diff
        _write_json(os.path.join(run_dir, "inventaire_apres.json"), inv_after)
        _write_json(os.path.join(run_dir, "inventaire_diff.json"), inv_diff)
        print(f"\n{TermColor.BOLD}Inventaire avant / après :{TermColor.RESET} {inv_mod.summarize(inv_after)}")
        if inv_diff["identical"]:
            print(f"  {TermColor.GREEN}✓ identique : aucun périphérique perdu, aucun lien dégradé, aucune erreur apparue{TermColor.RESET}")
        else:
            for line, f in zip(inv_mod.format_findings(inv_diff), inv_diff["findings"]):
                colr = {"fail": TermColor.RED, "warn": TermColor.YELLOW}.get(f["level"], TermColor.DIM)
                print(f"  {colr}{line}{TermColor.RESET}")

    # Session : résultats de ce lancement + ceux des lancements précédents depuis le démarrage de la machine
    all_results = results
    if session_mode and results:
        all_results = cs.results_of(prev_session) + results
        try:
            cs.save(sdir, all_results, inv_before, {"suite": suite_id, "run": os.path.basename(run_dir), "status": status,
                                                    "finished": datetime.now().isoformat(), "n_results": len(results)}, previous=prev_session)
        except Exception as exc:
            print(f"  {TermColor.YELLOW}⚠ session.json non enregistré : {exc}{TermColor.RESET}")

    # Verdict automatique (sur tous les résultats de la session)
    verdict = None
    if all_results:
        verdict = vd.compute_verdict(all_results, sysinfo, criteria, reference, inline, inventory_diff=inv_diff)
        meta["verdict"] = verdict
        print_verdict(verdict)

    # Rapports (aussi en cas d'arrêt ou de panne : données partielles conservées)
    if results and not args.no_export:
        print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Génération des rapports…{TermColor.RESET}")
        try:
            save_to_history(results)
        except Exception:
            pass
        paths = export_campaign(all_results, sdir if session_mode else run_dir, meta, sysinfo, args.export, fixed_names=session_mode)
        for fmt, path in paths.items():
            print(f"  ✓ {fmt.upper():<5} {path}")
        if session_mode:
            print(f"  {TermColor.DIM}Rapport de session : {len(all_results)} résultat(s) cumulé(s) depuis le démarrage de la machine "
                  f"({len(results)} de ce lancement). Même fichiers à chaque lancement ; --new-report pour repartir d'un nouveau rapport.{TermColor.RESET}")

    if args.save_reference and results:
        if verdict and verdict["status"] in ("CONFORME", "A_VERIFIER", "NON_EVALUE") and status == "finished":
            ref = vd.build_reference(args.save_reference, results, sysinfo, meta,
                                     args.tolerance if args.tolerance is not None else vd.DEFAULTS["ref_score_tol_pct"])
            print(f"  ✓ Référence enregistrée : {vd.save_reference(ref)}")
        else:
            print(f"  {TermColor.YELLOW}Référence NON enregistrée : la campagne doit être terminée et non « NON CONFORME ».{TermColor.RESET}")

    n_ok = sum(1 for r in results if r.status == "ok")
    n_fail = sum(1 for r in results if r.status == "error")
    n_int = sum(1 for r in results if r.status == "interrupted")
    fault = any((r.metrics or {}).get("hardware_fault_critical") for r in results)
    print(f"\n{TermColor.BOLD}Bilan : {n_ok}/{len(results)} test(s) OK"
          + (f", {n_fail} en échec" if n_fail else "") + (f", {n_int} interrompu(s) puis rejoué(s)" if n_int else "")
          + f" — campagne {status}{TermColor.RESET}")
    if status == "fault" or fault:
        print(f"{TermColor.BG_RED}{TermColor.BOLD} PANNE MATÉRIELLE DÉTECTÉE {TermColor.RESET} voir le rapport (surveillance matérielle).")
        return EXIT_FAULT
    if status == "aborted":
        print(f"{TermColor.YELLOW}Campagne interrompue. Reprise possible : --resume \"{run_dir}\"{TermColor.RESET}")
        return EXIT_INTERRUPTED
    if verdict and (verdict["status"] == "NON_CONFORME" or (args.strict and verdict["status"] == "A_VERIFIER")):
        return EXIT_FAIL
    return EXIT_FAIL if n_fail else EXIT_OK


def print_verdict(v: dict):
    """Affiche le verdict et les contrôles en écart."""
    col = {"CONFORME": TermColor.BG_GREEN, "A_VERIFIER": TermColor.BG_YELLOW, "NON_CONFORME": TermColor.BG_RED,
           "NON_EVALUE": TermColor.DIM}[v["status"]]
    c = v["counts"]
    print(f"\n{TermColor.BOLD}Verdict :{TermColor.RESET} {col}{TermColor.BOLD} {v['label']} {TermColor.RESET}  "
          f"{TermColor.DIM}({c['pass']} conforme(s), {c['warn']} à vérifier, {c['fail']} non conforme(s), {c['na']} non évalué(s) "
          f"— {', '.join(v['sources'])}){TermColor.RESET}")
    for r in v["rules"]:
        if r["level"] in ("fail", "warn", "na"):
            sym, colr = {"fail": ("✗", TermColor.RED), "warn": ("⚠", TermColor.YELLOW), "na": ("·", TermColor.DIM)}[r["level"]]
            print(f"  {colr}{sym} {r['label']}{TermColor.RESET}" + (f" {TermColor.DIM}— {r['detail']}{TermColor.RESET}" if r["detail"] else ""))


def list_suites_cli():
    from core.runner import list_suites
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Séquences de tests disponibles (--suite ID) :{TermColor.RESET}\n")
    suites = list_suites()
    if not suites:
        print("  Aucune séquence trouvée (dossier suites/ ou ~/.benchsuite/suites/).")
    for s in suites:
        print(f"  {TermColor.BOLD}{s['id']:<20}{TermColor.RESET} {s['name']}  ({s['steps']} étapes)")
        if s["description"]:
            print(f"    {TermColor.DIM}{s['description']}{TermColor.RESET}")
    print(f"\n  {TermColor.DIM}Format : fichier JSON {{\"name\":…, \"cooldown_s\":60, \"steps\":[{{\"stress\":\"cpu_only_quick\"}}, "
          f"{{\"bench\":\"gpu_fryer\",\"params\":{{\"time_s\":600}}}}, {{\"fio\":\"<profil>\",\"path\":\"/chemin/du/disque\"}}]}}{TermColor.RESET}\n")


def list_profiles_cli():
    from core.stress_profiles import get_all_profiles as stress_profiles
    from core.fio_profiles import get_all_profiles as fio_profiles
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Profils stress-ng (étape {{\"stress\": ID}}) :{TermColor.RESET}")
    for p in stress_profiles():
        print(f"  {TermColor.BOLD}{p['id']:<26}{TermColor.RESET} {p['name']}")
    from core.fio_profiles import FIO_TABS, profile_tab, profile_standard
    print(f"\n{TermColor.BOLD}{TermColor.CYAN}▕ Profils fio, classés par fonction (étape {{\"fio\": ID, \"path\": …}}) :{TermColor.RESET}")
    profs = fio_profiles()
    for tid, ico, lab in FIO_TABS:
        grp = [p for p in profs if profile_tab(p) == tid]
        if not grp:
            continue
        print(f"  {TermColor.BOLD}{ico} {lab}{TermColor.RESET}")
        for p in grp:
            std = profile_standard(p)
            print(f"    {TermColor.BOLD}{p['id']:<28}{TermColor.RESET} {('[' + std + '] ') if std else ''}{p['name']}")
    print()


def build_parser():
    """Analyseur d'arguments historique (options longues). Utilisé aussi pour obtenir les valeurs par défaut de la nouvelle interface."""
    parser = argparse.ArgumentParser(
        description="BenchSuite CLI (Headless) — bancs de test serveurs/machines à la chaîne, en terminal.",
        formatter_class=argparse.RawTextHelpFormatter,
        epilog=("Exemples :\n"
                "  ./cli.py --suite recette_gpu --client ACME --order CMD-1234 --operator Operateur\n"
                "  ./cli.py -b gpu_fryer -p gpu_fryer.time_s=120\n"
                "  ./cli.py --info-report -s MA-MACHINE          # rapport infos système, sans aucun test\n"
                "  ./cli.py --resume ~/BenchmarkResults/ACME/SN123/20260930_0923_recette_gpu\n"
                "Codes de sortie : 0 OK · 1 échec d'un test · 2 panne matérielle · 3 erreur d'usage/préflight · 130 interrompu"))

    group = parser.add_mutually_exclusive_group()
    group.add_argument("-l", "--list", action="store_true", help="Lister les benchmarks du catalogue")
    group.add_argument("--suites", action="store_true", help="Lister les séquences de tests (--suite)")
    group.add_argument("--profiles", action="store_true", help="Lister les profils stress-ng et fio")
    group.add_argument("-i", "--info", action="store_true", help="Afficher les informations matérielles (best-effort)")
    group.add_argument("--info-report", action="store_true",
                       help="Rapport « informations système » SANS test : fiche matériel + inventaire en HTML / PDF / JSON / CSV dans -o "
                            "(formats : -e ; identité : -s / --client / --serial…)")
    group.add_argument("--install-guide", action="store_true",
                       help="Guide d'installation adapté à cette machine (lecture seule : n'installe rien)")
    group.add_argument("--optimize-html", metavar="FICHIER", nargs="+",
                       help="Régénère un ou plusieurs rapports HTML existants (même anciens, lourds) avec l'exporteur actuel : séries allégées, "
                            "ouverture rapide ; écrit <nom>_optimise.html à côté du fichier, l'original n'est pas modifié")
    group.add_argument("--fleet", metavar="DOSSIER", help="Synthèse d'un lot de machines : dernier rapport de chaque machine, écarts vs médiane (CSV + HTML)")
    group.add_argument("--suite", metavar="ID|FICHIER", help="Lancer une séquence de tests (voir --suites)")
    group.add_argument("--resume", metavar="DOSSIER", help="Reprendre une campagne interrompue (crash, coupure, Ctrl-C)")
    group.add_argument("-a", "--all", action="store_true", help="Lancer tous les benchmarks du catalogue")
    group.add_argument("-b", "--bench", nargs="+", metavar="ID", help="Lancer un ou plusieurs benchmarks par identifiant")
    group.add_argument("-c", "--category", nargs="+", metavar="CAT", help="Lancer toutes les catégories indiquées (ex: GPU Stockage)")

    parser.add_argument("-p", "--param", nargs="+", metavar="test_id.param=val",
                        help="Surcharger un paramètre (ex: gpu_fryer.time_s=120)")
    parser.add_argument("-o", "--output-dir", default=os.path.expanduser("~/BenchmarkResults"),
                        help="Dossier racine des résultats (défaut : ~/BenchmarkResults)")
    parser.add_argument("-e", "--export", nargs="+", choices=["csv", "json", "pdf", "html", "all"], default=["all"],
                        help="Formats d'export (défaut : all)")
    parser.add_argument("--no-export", action="store_true", help="Ne générer aucun rapport")
    parser.add_argument("--new-report", action="store_true",
                        help="Ne pas ajouter ce lancement au rapport de la session en cours (même démarrage de la machine) : crée un nouveau "
                             "dossier et de nouveaux fichiers. Par défaut, tous les lancements depuis le démarrage alimentent le MÊME rapport")
    ident = parser.add_argument_group("identité de la machine / traçabilité")
    ident.add_argument("-s", "--sysname", default=None, help="Nom de la machine (défaut : modèle + n° de série DMI, sinon hostname)")
    ident.add_argument("--client", default=None, help="Client (sert aussi à ranger les résultats)")
    ident.add_argument("--operator", default=None, help="Opérateur")
    ident.add_argument("--order", default=None, help="Numéro de commande / dossier")
    ident.add_argument("--serial", default=None, help="Numéro de série (défaut : détecté via DMI)")
    ident.add_argument("--model", default=None, help="Modèle de la machine (défaut : détecté via DMI)")
    ident.add_argument("--ambient", default=None, help="Température ambiante (°C)")
    ident.add_argument("--fan", default=None, help="Ventilation / remarque")
    ver = parser.add_argument_group("verdict automatique (critères modifiables à chaque test)")
    ver.add_argument("--criteria", metavar="FICHIER", help="Fichier JSON de critères (modèle : --criteria-template)")
    ver.add_argument("--crit", nargs="+", metavar="EXPR", help="Critères en ligne, ex : gpu.tflops>=70 gpu.temp_hotspot_max_c<=95:warn")
    ver.add_argument("--reference", metavar="NOM", help="Comparer à une machine de référence enregistrée (--save-reference)")
    ver.add_argument("--tolerance", type=float, default=None, help="Tolérance en %% des scores vs référence (défaut : 5)")
    ver.add_argument("--save-reference", metavar="NOM", help="Enregistrer cette machine (si elle est validée) comme référence")
    ver.add_argument("--strict", action="store_true", help="Code de sortie 1 aussi pour « À VÉRIFIER »")
    ver.add_argument("--criteria-template", metavar="FICHIER", help="Écrire un modèle de fichier de critères et quitter")
    ver.add_argument("--references", action="store_true", help="Lister les références enregistrées et quitter")
    iv = parser.add_argument_group("inventaire matériel")
    iv.add_argument("--no-inventory", action="store_true", help="Ne pas faire l'inventaire avant/après les tests")
    iv.add_argument("--inventory", nargs="?", const="-", metavar="FICHIER", help="Afficher l'inventaire matériel (et l'enregistrer dans FICHIER) puis quitter")
    iv.add_argument("--inventory-diff", nargs=2, metavar=("AVANT", "APRES"), help="Comparer deux inventaires JSON puis quitter")
    mc = parser.add_argument_group("câblage MCIO")
    mc.add_argument("--mcio-scan", action="store_true", help="Lister les GPU avec root port PCI et MCIO associé, puis quitter")
    mc.add_argument("--mcio-set", nargs="+", metavar="CLÉ=MCIO", help="Associer un connecteur, ex : port:60:01.1=MCIO5 (vide = effacer)")
    mc.add_argument("--mcio-pin", action="store_true", help="Figer le mapping actuel par root port (insensible aux ajouts de SSD)")
    camp = parser.add_argument_group("exécution")
    camp.add_argument("--fio-path", default=None, help="Disque/répertoire de DONNÉES pour les étapes fio (jamais le disque système)")
    camp.add_argument("--cooldown", type=float, default=None, help="Pause entre deux tests en secondes (défaut : celle de la séquence)")
    camp.add_argument("--expected-gpus", type=int, default=None, help="Nombre de GPU attendus (vérification avant test)")
    camp.add_argument("--continue-on-fault", action="store_true", help="Ne pas arrêter la campagne sur une panne matérielle")
    camp.add_argument("--no-preflight", action="store_true", help="Ignorer les vérifications avant test")
    camp.add_argument("--preflight-only", action="store_true", help="Faire uniquement les vérifications avant test")

    parser.add_argument("--read-only", action="store_true",
                        help="Mode lecture seule : ne rien modifier sur la machine (pas de module chargé, de service DCGM démarré, "
                             "de mode persistance GPU). BenchSuite n'installe/désinstalle jamais rien, avec ou sans cette option")
    return parser


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if not _is_light(argv):
        _load_heavy()
    # Nouvelle interface : `./cli.py` seul (terminal interactif) ou une commande courte (`./cli.py cpu 5m`, `infos`, `liste`…).
    # Les anciennes options (-b, --suite, …) commencent par « - » et suivent le chemin historique ci-dessous, inchangé.
    try:
        from clitui import entry
        handled, rc = entry.maybe_handle(argv, sys.modules[__name__])
        if handled:
            return rc
    except ImportError:
        pass                                  # clitui absent : ancienne interface seule
    print_banner()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.read_only:
        os.environ["BENCHSUITE_READ_ONLY"] = "1"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    if args.install_guide:
        from core.install_advisor import print_install_guide
        return print_install_guide()
    if args.optimize_html:
        from exporters.export import reexport_html
        rc = EXIT_OK
        for f in args.optimize_html:
            try:
                before = os.path.getsize(f)
                out = reexport_html(f)
                print(f"{TermColor.GREEN}✓{TermColor.RESET} {f} ({before // 1024} Ko) → {out} ({os.path.getsize(out) // 1024} Ko)")
            except Exception as e:           # fichier illisible / pas un rapport BenchSuite : on continue avec les suivants
                print(f"{TermColor.RED}✗{TermColor.RESET} {f} : {e}")
                rc = 1
        return rc
    if args.no_inventory:
        os.environ["BENCHSUITE_NO_INVENTORY"] = "1"     # aussi l'inventaire avant/après de chaque test

    if args.list:
        list_benchmarks(); return EXIT_OK
    if args.suites:
        list_suites_cli(); return EXIT_OK
    if args.profiles:
        list_profiles_cli(); return EXIT_OK
    if args.info_report:
        return run_info_report(args)
    if args.info:
        print_hardware_info(); return EXIT_OK
    if args.inventory is not None or args.inventory_diff:
        from core import inventory as inv_mod
        if args.inventory_diff:
            try:
                a, b = (json.load(open(x, encoding="utf-8")) for x in args.inventory_diff)
            except (OSError, ValueError) as exc:
                print(f"{TermColor.RED}Erreur : {exc}{TermColor.RESET}"); return EXIT_USAGE
            d = inv_mod.diff_inventory(a, b)
            print(f"Avant : {inv_mod.summarize(a)}\nAprès : {inv_mod.summarize(b)}")
            print("\n".join(inv_mod.format_findings(d)) or f"{TermColor.GREEN}✓ identique{TermColor.RESET}")
            return EXIT_FAIL if d["counts"]["fail"] else EXIT_OK
        inv = inv_mod.collect_inventory()
        print(inv_mod.summarize(inv))
        if args.inventory != "-":
            _write_json(args.inventory, inv)
            print(f"Inventaire enregistré : {args.inventory}")
        else:
            print(json.dumps(inv, ensure_ascii=False, indent=1))
        return EXIT_OK
    if args.mcio_set or args.mcio_pin or args.mcio_scan:
        from core import system_info as si
        for item in (args.mcio_set or []):
            k, _, v = item.partition("=")
            try:
                print(f"  enregistré : {si.set_mcio_label(k, v.strip())} = {v.strip() or '(effacé)'}")
            except ValueError as e:
                print(f"{TermColor.RED}{e}{TermColor.RESET}"); return EXIT_USAGE
        if args.mcio_pin:
            for r in si.list_gpu_pci_topology():
                if r["port"] and r["label"]:
                    si.set_mcio_label("port:" + r["port"], r["label"].replace(" (supposé)", ""))
            print("  mapping figé par root port dans " + si._MCIO_USER_FILE)
        rows = si.list_gpu_pci_topology()
        print(f"{'GPU (bus)':<14}{'root':<6}{'root port':<11}MCIO")
        for r in rows:
            print(f"{r['bdf'][5:]:<14}{r['root']:<6}{r['port'] or '?':<11}{r['label'] or '—'}")
        if not rows:
            print("  aucun GPU PCI détecté")
        return EXIT_OK
    if args.fleet:
        from core import fleet
        rows = fleet.collect_fleet(args.fleet)
        if not rows:
            print(f"{TermColor.RED}Aucun rapport benchsuite_report_*.json trouvé sous {args.fleet}{TermColor.RESET}")
            return EXIT_USAGE
        fleet.flag_outliers(rows)
        paths = fleet.write_fleet(rows, args.fleet)
        print(f"{len(rows)} machine(s) — {sum(1 for r in rows if r['flags'])} hors lot")
        for r in rows:
            mark = f"{TermColor.RED}⚠ {' | '.join(r['flags'])}{TermColor.RESET}" if r["flags"] else f"{TermColor.GREEN}✓{TermColor.RESET}"
            print(f"  {r['machine']:<24} {r['verdict']:<13} {mark}")
        print(f"  CSV  : {paths['csv']}\n  HTML : {paths['html']}")
        return EXIT_OK
    if args.criteria_template:
        from core.verdict import CRITERIA_TEMPLATE
        with open(args.criteria_template, "w", encoding="utf-8") as f:
            json.dump(CRITERIA_TEMPLATE, f, ensure_ascii=False, indent=2)
        print(f"Modèle de critères écrit : {args.criteria_template}\n(tout est optionnel : supprimez les lignes inutiles)")
        return EXIT_OK
    if args.references:
        from core.verdict import list_references, REF_DIR
        refs = list_references()
        print("Références enregistrées (" + REF_DIR + ") :\n  " + ("\n  ".join(refs) if refs else "aucune"))
        return EXIT_OK

    if args.resume:
        if not os.path.isfile(os.path.join(args.resume, "state.json")):
            print(f"{TermColor.RED}Erreur : pas de state.json dans {args.resume}{TermColor.RESET}")
            return EXIT_USAGE
        return run_campaign([], args, resume_dir=args.resume)

    if args.suite:
        from core.runner import load_suite
        try:
            suite = load_suite(args.suite)
        except (ValueError, OSError) as exc:
            print(f"{TermColor.RED}Erreur : {exc}{TermColor.RESET}")
            return EXIT_USAGE
        cd = args.cooldown if args.cooldown is not None else float(suite.get("cooldown_s", 0))
        return run_campaign(suite["steps"], args, suite["id"], suite.get("name", suite["id"]), cooldown_s=cd)

    selected_ids = []
    if args.all:
        selected_ids = [item["id"] for item in CATALOG]
    elif args.bench:
        all_ids = {item["id"] for item in CATALOG}
        for bid in args.bench:
            if bid not in all_ids:
                print(f"{TermColor.RED}Erreur : Benchmark ID '{bid}' inconnu. Utilisez --list.{TermColor.RESET}")
                return EXIT_USAGE
            selected_ids.append(bid)
    elif args.category:
        valid_cats = {item["category"].lower(): item["category"] for item in CATALOG}
        for legacy in ("cpu", "ram", "cpu+ram"):       # alias historiques (CPU et RAM fusionnés)
            if "cpu + ram" in valid_cats:
                valid_cats.setdefault(legacy, valid_cats["cpu + ram"])
        for cat in args.category:
            if cat.lower() not in valid_cats:
                print(f"{TermColor.RED}Erreur : catégorie '{cat}' inconnue. Choix : {', '.join(CAT_ORDER)}{TermColor.RESET}")
                return EXIT_USAGE
            selected_ids.extend(i["id"] for i in CATALOG if i["category"] == valid_cats[cat.lower()])
    else:
        parser.print_help()
        return EXIT_OK

    overrides = parse_param_overrides(args.param)
    steps = [{"bench": bid, **({"params": overrides[bid]} if bid in overrides else {})} for bid in selected_ids]
    cd = args.cooldown if args.cooldown is not None else 0.0
    return run_campaign(steps, args, "manuel", "Sélection manuelle", cooldown_s=cd)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.stderr.write(f"\n{TermColor.BOLD}{TermColor.RED}Interruption par l'utilisateur.{TermColor.RESET}\n")
        sys.exit(130)
