# -*- coding: utf-8 -*-
"""Utilitaires communs aux benchmarks (exécution de processus, recherche d'outils, cache, GPU/RAM de base)."""

import json
import os
import platform
try:
    import psutil
except ImportError:
    psutil = None
import re
import shlex
import shutil
import signal
import subprocess
import threading
import time
from typing import Callable, Optional

from core.result import BenchmarkResult, collect_dmesg_log  # noqa: F401
from core.fault_monitor import with_fault_watch  # noqa: F401
from core.safety import nvidia_smi_usable

try:
    from core.bmc_sensors import read_ram_temperature_power
except Exception:
    read_ram_temperature_power = None

_OCLBURN_SRC = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "third_party", "benchsuite_oclburn", "oclburn.c",
)

ProgressCb = Optional[Callable[[int, str], None]]
SeriesCb   = Optional[Callable[[dict], None]]   # {"t", "temp", "power", "freq_graphics", "freq_memory"}
LOG_PRE_SECONDS = 5.0
LOG_POST_SECONDS = 5.0


def _log_window_wait(stop_event: "threading.Event", seconds: float) -> None:
    """Attend une fenêtre de log, interrompue si l'arrêt est demandé."""
    end = time.time() + max(0.0, float(seconds))
    while time.time() < end:
        if stop_event.wait(0.1):
            break

# ─────────────────────────────────────────────────────────────────────────────
# Utilitaires
# ─────────────────────────────────────────────────────────────────────────────

def _run(cmd: list, timeout: int = 600, kill_event: "threading.Event | None" = None) -> tuple:
    """Exécute une commande avec timeout ; interrompt proprement si `kill_event` est levé."""
    try:
        if kill_event is None:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            return r.returncode, r.stdout, r.stderr
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        deadline = time.time() + timeout
        while True:
            try:
                out, err = proc.communicate(timeout=0.5)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                if kill_event.is_set():
                    proc.terminate()
                    try:
                        out, err = proc.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        out, err = proc.communicate()
                    return -4, out or "", (err or "") + "\nInterrompu"
                if time.time() > deadline:
                    proc.kill()
                    proc.communicate()
                    return -2, "", "Timeout"
    except FileNotFoundError:
        return -1, "", f"Introuvable : {cmd[0]}"
    except subprocess.TimeoutExpired:
        return -2, "", "Timeout"
    except Exception as e:
        return -3, "", str(e)

def _watch_process(proc, kill_event, hard_limit_s, state: dict) -> None:
    """Surveille un processus lancé avec `start_new_session=True` : l'arrête (SIGTERM puis SIGKILL sur son groupe) si
    `kill_event` est levé (state["killed"]) ou s'il dépasse `hard_limit_s` secondes (state["timeout"])."""
    t_end = time.time() + hard_limit_s if hard_limit_s else None
    while proc.poll() is None:
        hit_kill = bool(kill_event is not None and kill_event.is_set())
        hit_time = bool(t_end and time.time() > t_end)
        if hit_kill or hit_time:
            state["killed"], state["timeout"] = hit_kill, (hit_time and not hit_kill)
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                time.sleep(0.8)
                if proc.poll() is None:
                    os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            return
        time.sleep(0.3)


def _normalize_gpu_list(raw) -> str:
    """Normalise une liste d'index GPU saisie par l'utilisateur ('0, 1' → '0,1').

    Ne conserve que les entiers ; chaîne vide = tous les GPU.
    """
    return ",".join(dict.fromkeys(re.findall(r"\d+", str(raw or ""))))


def _candidate_user_homes() -> list:
    homes = [os.path.expanduser("~")]
    sudo_user = os.environ.get("SUDO_USER")
    if sudo_user and sudo_user != "root":
        try:
            import pwd
            homes.append(pwd.getpwnam(sudo_user).pw_dir)
        except Exception:
            homes.append(f"/home/{sudo_user}")
    # dedupe en conservant l'ordre
    out = []
    for h in homes:
        if h and h not in out:
            out.append(h)
    return out

def get_custom_tool_path(name: str) -> str:
    """Récupère un chemin d'outil personnalisé défini par l'utilisateur."""
    try:
        path = os.path.expanduser("~/.benchsuite_settings.json")
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return data.get("custom_paths", {}).get(name, "")
    except Exception:
        pass
    return ""

def set_custom_tool_path(name: str, tool_path: str):
    """Enregistre un chemin d'outil personnalisé défini par l'utilisateur."""
    try:
        path = os.path.expanduser("~/.benchsuite_settings.json")
        data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        if "custom_paths" not in data:
            data["custom_paths"] = {}
        data["custom_paths"][name] = tool_path
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4)
        forget_tool_cache()
    except Exception:
        pass

def _find_in_folder_recursive(folder: str, filename: str, max_depth: int = 3) -> list:
    """Recherche un fichier exécutable de manière récursive légère pour ne pas pénaliser les performances."""
    results = []
    if not os.path.isdir(folder):
        return results
    ignored_dirs = {".git", ".cache", "node_modules", ".venv", "venv", ".local", ".cargo", "Library", "containers"}
    try:
        base_depth = folder.rstrip(os.path.sep).count(os.path.sep)
        for root, dirs, files in os.walk(folder, followlinks=False):
            # Éviter de descendre dans des dossiers système/cachés trop lourds
            dirs[:] = [d for d in dirs if not d.startswith('.') and d not in ignored_dirs]
            cur_depth = root.count(os.path.sep) - base_depth
            if cur_depth > max_depth:
                dirs[:] = []
                continue
            if filename in files:
                path = os.path.join(root, filename)
                if os.path.isfile(path) and os.access(path, os.X_OK) and _has_valid_exec_format(path):
                    results.append(path)
    except Exception:
        pass
    return results

# Résultat de la recherche d'un outil : {clé: (expiration, valeur)}. La recherche lance des commandes et parcourt des dossiers
# (dossier personnel compris) ; l'interface la demandait ~150 fois au démarrage pour une quinzaine d'outils.
# Trouvé : mémorisé 2 min ; absent : 15 s (un outil qu'on vient d'installer est vu rapidement). `forget_tool_cache()` vide tout.
_TOOL_CACHE: dict = {}
_TOOL_CACHE_LOCK = threading.Lock()
_TOOL_TTL_FOUND_S, _TOOL_TTL_MISSING_S = 120.0, 15.0


def forget_tool_cache() -> None:
    """Oublie les outils déjà cherchés (après une installation, un changement de chemin ou un clic sur « Actualiser »)."""
    with _TOOL_CACHE_LOCK:
        _TOOL_CACHE.clear()


def _tool_cached(key: str, finder):
    now = time.monotonic()
    with _TOOL_CACHE_LOCK:
        hit = _TOOL_CACHE.get(key)
        if hit and hit[0] > now:
            return hit[1]
    val = finder()
    with _TOOL_CACHE_LOCK:
        _TOOL_CACHE[key] = (now + (_TOOL_TTL_FOUND_S if val else _TOOL_TTL_MISSING_S), val)
    return val


def _find_tool_path(name: str) -> str:
    """Chemin de l'outil `name` ('' si absent), résultat mémorisé (voir _TOOL_CACHE)."""
    return _tool_cached("path:" + name, lambda: _find_tool_path_uncached(name))


def _find_tool_path_uncached(name: str) -> str:
    # 1. Chemin configuré personnalisé
    custom = get_custom_tool_path(name)
    if custom and os.path.isfile(custom) and os.access(custom, os.X_OK) and _has_valid_exec_format(custom):
        return custom

    # 2. Variable d'environnement (ex: NVBANDWIDTH_PATH, GPU_BURN_PATH)
    env_var = f"{name.upper().replace('-', '_')}_PATH"
    env_val = os.environ.get(env_var)
    if env_val and os.path.isfile(env_val) and os.access(env_val, os.X_OK) and _has_valid_exec_format(env_val):
        return env_val

    # 3. Recherche standard dans le PATH (sans lancer de processus)
    p = shutil.which(name)
    if p and _has_valid_exec_format(p):
        return p

    # 4. Chemins et dossiers spécifiques
    candidates = [f"/usr/local/bin/{name}", f"/usr/bin/{name}", f"/bin/{name}"]
    for home in _candidate_user_homes():
        candidates.append(os.path.join(home, ".cargo", "bin", name))
        candidates.append(os.path.join(home, ".local", "bin", name))
        # Compilations sans droits root (guide d'installation : ~/.benchsuite/tools/<outil>/)
        candidates.append(os.path.join(home, ".benchsuite", "tools", name, name))
        candidates.append(os.path.join(home, ".benchsuite", "tools", name, "build", name))
        # Dossiers de compilation et de clonage Git classiques de nvbandwidth
        candidates.append(os.path.join(home, "nvbandwidth", "build", name))
        candidates.append(os.path.join(home, "nvbandwidth", name))
        candidates.append(os.path.join(home, "Téléchargements", "nvbandwidth", "build", name))
        candidates.append(os.path.join(home, "Downloads", "nvbandwidth", "build", name))
        candidates.append(os.path.join(home, "Desktop", "nvbandwidth", "build", name))
    candidates.append(f"/opt/nvbandwidth/build/{name}")
    candidates.append(f"/opt/nvbandwidth/{name}")
    
    # Snap
    snap_aliases = {
        "gpu-burn": ["/snap/bin/gpu-burn", "/snap/bin/gpu_burn"],
        "gpu_burn": ["/snap/bin/gpu-burn", "/snap/bin/gpu_burn"],
    }
    candidates.extend(snap_aliases.get(name, []))
    
    # Recherche dans les candidats directs
    for path in candidates:
        if os.path.isfile(path) and os.access(path, os.X_OK) and _has_valid_exec_format(path):
            return path

    # 5. Recherche récursive légère dans le répertoire de l'application et les dossiers parents/voisins
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        cwd = os.getcwd()
        for base_dir in [app_dir, os.path.dirname(app_dir), cwd]:
            found_paths = _find_in_folder_recursive(base_dir, name, max_depth=3)
            if found_paths:
                return found_paths[0]
    except Exception:
        pass

    # 6. Recherche récursive légère dans les sous-dossiers utilisateurs pertinents
    for home in _candidate_user_homes():
        for sub in ["", "Téléchargements", "Downloads", "Desktop", "nvbandwidth"]:
            target_dir = os.path.join(home, sub) if sub else home
            if not os.path.isdir(target_dir):
                continue
            depth = 2 if not sub else 3
            found_paths = _find_in_folder_recursive(target_dir, name, max_depth=depth)
            if found_paths:
                return found_paths[0]

    return ""


def _has_valid_exec_format(path: str) -> bool:
    """Retourne False pour les faux binaires qui provoquent Errno 8 Exec format error.

    Cas frequent: /usr/local/bin/gpu-burn existe mais c'est un fichier texte,
    un wrapper casse, ou un binaire d'une autre arch. On evite alors de le
    selectionner et on laisse le fallback Snap (/snap/bin ou snap run) prendre le relais.
    """
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        # ELF natif ou script avec shebang. Les wrappers Snap sont des scripts.
        return head.startswith(b"\x7fELF") or head.startswith(b"#!")
    except Exception:
        return False


def tool_exists(name: str) -> bool:
    # Accepte aussi un chemin absolu
    if os.path.sep in name and os.path.isfile(name) and os.access(name, os.X_OK):
        return True
    if name in ("gpu-burn", "gpu_burn"):
        return bool(_find_gpu_burn_command())
    if name == "oclburn":
        # compilé au premier lancement depuis third_party/ : disponible dès que gcc et la source sont là
        return bool(_find_tool_path("oclburn")) or (os.path.isfile(_OCLBURN_SRC) and bool(shutil.which("gcc") or shutil.which("cc")))
    if name == "nvidia-smi":
        # Sans GPU NVIDIA sur le bus, nvidia-smi ne sert à rien et recharge le module noyau à chaque appel
        return nvidia_smi_usable()
    return bool(_find_tool_path(name))

def get_ram_total_mb() -> int:
    """Retourne la RAM totale en Mo."""
    try:
        return psutil.virtual_memory().total // (1024 * 1024)
    except Exception:
        try:
            with open("/proc/meminfo") as f:
                m = re.search(r"MemTotal:\s+(\d+)", f.read())
                return int(m.group(1)) // 1024 if m else 4096
        except Exception:
            return 4096

def get_system_info() -> dict:
    info = {
        "os":       platform.system() + " " + platform.release(),
        "machine":  platform.machine(),
        "hostname": platform.node(),
        "python":   platform.python_version(),
    }
    try:
        with open("/proc/cpuinfo") as f: raw = f.read()
        from core.system_info import cpu_model_name
        info["cpu_model"] = cpu_model_name()
        info["cpu_cores"] = raw.count("processor\t:")
        info["cpu_threads"] = os.cpu_count() or info["cpu_cores"]
    except Exception:
        pass
    try:
        with open("/proc/meminfo") as f: raw = f.read()
        m = re.search(r"MemTotal:\s+(\d+)", raw)
        if m:
            info["ram_kb"] = int(m.group(1))
            info["ram_gb"] = round(int(m.group(1)) / 1024 / 1024, 2)
    except Exception:
        pass
    # GPU via nvidia-smi (uniquement si un GPU NVIDIA est présent : l'outil charge sinon le module noyau pour rien)
    try:
        rc, out = (-1, "")
        if nvidia_smi_usable():
            rc, out, _ = _run(["nvidia-smi", "--query-gpu=name,memory.total,driver_version",
                               "--format=csv,noheader,nounits"], timeout=8)
        if rc == 0 and out.strip():
            parts = [p.strip() for p in out.strip().split(",")]
            if len(parts) >= 2:
                info["gpu_name"]   = parts[0]
                info["gpu_mem_mb"] = parts[1]
                if len(parts) >= 3:
                    info["gpu_driver"] = parts[2]
    except Exception:
        pass
    return info


def _drop_empty_series(series: dict) -> None:
    """Retire les séries de température / puissance entièrement nulles (capteur absent, ex. ARM sans sonde CPU) :
    pas de courbe ni de « 0 °C » trompeur dans les rapports."""
    for k in list(series):
        if k.startswith("_") or k in ("t", "freq", "cpu_pct"):
            continue
        if k.startswith(("temp", "ram_temp", "power", "ram_power")):
            vals = series[k]
            if isinstance(vals, list) and not any(isinstance(v, (int, float)) and v > 0 for v in vals):
                del series[k]


_RAM_SPEED_CACHE: list = []


def _ram_speed_mts() -> int:
    """Vitesse configurée des barrettes (MT/s, la plus basse si elles diffèrent), lue une seule fois (dmidecode, root requis) ; 0 si inconnue.
    C'est une valeur du firmware, pas une mesure : la fréquence réelle de la mémoire n'est lisible nulle part sans outil constructeur."""
    if _RAM_SPEED_CACHE:
        return _RAM_SPEED_CACHE[0]
    val = 0
    try:
        from core.system_info import get_ram_info
        info = get_ram_info() or {}
        texts = [str(b.get("vitesse_mhz") or b.get("vitesse") or "") for b in info.get("barrettes", [])] + [str(info.get("vitesse_mhz") or "")]
        nums = [int(n) for t in texts for n in re.findall(r"\d{3,5}", t)]
        nums = [n for n in nums if 400 <= n <= 20000]
        val = min(nums) if nums else 0
    except Exception:
        val = 0
    _RAM_SPEED_CACHE.append(val)
    return val


def _read_ram_temperature_power_best_effort() -> tuple:
    """Retourne (temperature_c, power_w) pour la RAM si disponible.

    Fait une vérification best-effort et fallback sur les capteurs CPU
    si les sondes RAM dédiées de la carte mère ne renvoient rien (0.0).
    """
    temp, power = 0.0, 0.0
    if read_ram_temperature_power:
        try:
            temp, power = read_ram_temperature_power()
        except Exception:
            pass

    return temp, power


def _is_shell_script(path: str) -> bool:
    try:
        with open(path, "rb") as f:
            return f.read(2) == b"#!"
    except Exception:
        return False

def _gpu_burn_cmd_for_path(path: str) -> list:
    """Construit une commande robuste pour gpu-burn.

    Les wrappers shell locaux sont volontairement lances via bash afin d'eviter
    Errno 8 et les differences de shebang. Pour /opt/gpu-burn/gpu_burn on garde
    le binaire direct.
    """
    if not path or not os.path.isfile(path) or not os.access(path, os.X_OK):
        return []
    if _is_shell_script(path):
        return ["bash", path]
    return [path]

def _probe_gpu_burn_command(cmd: list) -> bool:
    """Validation non destructive: ne lance plus un mini burn au demarrage.

    V5.2: certains gpu-burn interpretent --help comme une execution invalide
    et peuvent marquer les GPU en DIE. On verifie donc seulement que la commande
    existe, que le wrapper est un script valide, ou que le binaire est ELF.
    """
    if not cmd:
        return False
    if cmd[0] == "bash" and len(cmd) >= 2:
        path = cmd[1]
        if not os.path.isfile(path):
            return False
        rc, _, _ = _run(["bash", "-n", path], timeout=3)
        return rc == 0
    path = cmd[0]
    if not os.path.isfile(path) or not os.access(path, os.X_OK):
        return False
    try:
        with open(path, "rb") as f:
            head = f.read(4)
        return head.startswith(b"\x7fELF") or head.startswith(b"#!")
    except Exception:
        return False

def _find_gpu_burn_command() -> list:
    """Retourne une commande gpu-burn executable sans lancer de test pendant la detection (résultat mémorisé)."""
    return list(_tool_cached("cmd:gpu-burn", _find_gpu_burn_command_uncached) or [])


def _find_gpu_burn_command_uncached() -> list:
    candidates = []

    # Priorite au binaire classique installe dans /opt, car il doit souvent
    # etre lance depuis son dossier de build pour retrouver ses kernels CUDA/PTX.
    for path in ("/opt/gpu-burn/gpu_burn", "/opt/gpu-burn/gpu-burn",
                 "/usr/local/bin/gpu-burn", "/usr/local/bin/gpu_burn",
                 "./gpu-burn", "./gpu_burn"):
        cmd = _gpu_burn_cmd_for_path(path)
        if cmd:
            candidates.append(cmd)

    # Installation sans droits root (guide d'installation) : ~/.benchsuite/tools/gpu-burn
    for home in _candidate_user_homes():
        for path in (os.path.join(home, ".benchsuite", "tools", "gpu-burn", "gpu_burn"),):
            cmd = _gpu_burn_cmd_for_path(path)
            if cmd:
                candidates.append(cmd)

    for path in ("/snap/bin/gpu-burn", "/snap/bin/gpu_burn"):
        cmd = _gpu_burn_cmd_for_path(path)
        if cmd:
            candidates.append(cmd)

    for name in ("gpu-burn", "gpu_burn"):
        in_path = shutil.which(name)
        if in_path:
            cmd = _gpu_burn_cmd_for_path(in_path)
            if cmd:
                candidates.append(cmd)
        path = _find_tool_path(name)
        cmd = _gpu_burn_cmd_for_path(path) if path else []
        if cmd:
            candidates.append(cmd)

    seen = set()
    for cmd in candidates:
        key = tuple(cmd)
        if key in seen:
            continue
        seen.add(key)
        if _probe_gpu_burn_command(cmd):
            return cmd

    if shutil.which("snap"):
        rc, out, _ = _run(["snap", "list", "gpu-burn"], timeout=5)
        if rc == 0 and "gpu-burn" in out:
            return ["snap", "run", "gpu-burn"]
    return []


def _gpu_burn_workdir(cmd: list) -> str | None:
    """Retourne le dossier de travail correct pour gpu-burn classique.

    Le gpu-burn compile depuis GitHub depend souvent de fichiers generes dans
    son repertoire (/opt/gpu-burn). Si on le lance depuis ailleurs, il peut
    demarrer puis marquer les GPU en DIE sans chauffer.
    """
    try:
        joined = " ".join(cmd)
        # Installation conseillee: /opt/gpu-burn/gpu_burn + wrapper /usr/local/bin/gpu-burn
        if "/opt/gpu-burn" in joined and os.path.isdir("/opt/gpu-burn"):
            return "/opt/gpu-burn"
        # Binaire direct (~/.benchsuite/tools, ~/Downloads/gpu-burn…) : il charge compare.ptx / compare.fatbin
        # depuis le dossier courant, donc on se place à côté de lui s'il s'y trouve
        if cmd and os.path.isfile(cmd[0]):
            d = os.path.dirname(os.path.abspath(cmd[0]))
            if any(os.path.isfile(os.path.join(d, f)) for f in ("compare.fatbin", "compare.ptx")):
                return d
        # Wrapper shell classique: cd /opt/gpu-burn ; ./gpu_burn "$@"
        if len(cmd) >= 2 and cmd[0] == "bash" and os.path.isfile(cmd[1]):
            txt = open(cmd[1], "r", errors="ignore").read(4096)
            m = re.search(r"cd\s+(['\"]?)(/[^\n;]+?)\1(?:\s|;|$)", txt)
            if m and os.path.isdir(m.group(2).strip()):
                return m.group(2).strip()
    except Exception:
        pass
    return None

def _terminate_process_tree(proc, grace: float = 1.0) -> None:
    """Stoppe un process et ses enfants, même quand un wrapper bash lance gpu_burn.

    On démarre les benchmarks GPU avec start_new_session=True, donc proc.pid est
    aussi l'identifiant du process group. Cela permet de tuer le wrapper ET le
    vrai binaire CUDA lancé dessous.
    """
    if not proc or proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            pass
    deadline = time.time() + grace
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.05)
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _add_gpu_series_metrics(metrics: dict, series: dict) -> None:
    """Ajoute agrégats + séries temps réel dans les métriques/export rapport."""
    n_gpu = 0
    for k in series.keys():
        m = re.match(r"^gpu(\d+)_temp$", k)
        if m:
            n_gpu = max(n_gpu, int(m.group(1)) + 1)

    # Agrégats calculés en charge stabilisée (voir core/gpu_stats.py)
    from core.gpu_stats import gpu_load_metrics
    for i in range(n_gpu):
        metrics.update(gpu_load_metrics(series, i))

    # Compat export PDF historique : GPU0 en clés plates.
    metrics["series_t"] = series.get("t", [])
    metrics["series_temp"] = series.get("gpu0_temp", series.get("temp", []))
    metrics["series_power"] = series.get("gpu0_power", series.get("power", []))
    metrics["series_freq_graphics"] = series.get("gpu0_freq_graphics", series.get("freq_graphics", []))
    metrics["series_freq_memory"] = series.get("gpu0_freq_memory", series.get("freq_memory", []))

    # Séries complètes multi-GPU pour JSON et rapports futurs.
    # Si le monitoring contient déjà gpu0_*, gpu1_*, etc., on ne garde pas les
    # alias courts temp/power/freq_* dans le conteneur multi-GPU: ils dupliquent
    # GPU0 et créent une courbe fantôme dans les rapports.
    has_gpu_series = any(re.match(r"^gpu\d+_", str(k)) for k in series.keys())
    legacy_aliases = {"temp", "power", "freq_graphics", "freq_memory"}
    metrics["series"] = {
        k: v for k, v in series.items()
        if not (has_gpu_series and k in legacy_aliases)
    }
