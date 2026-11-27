"""
install_advisor.py — Guide d'installation adapté à la machine où BenchSuite est lancé.

LECTURE SEULE : ce module n'installe rien, n'écrit rien et ne modifie aucune dépendance. Il observe ce qui est
déjà présent (outils, versions, pilote NVIDIA, CUDA, DCGM, dépôts apt…) et ne propose des commandes que pour ce
qui manque ou ne fonctionne pas, en tenant compte de l'existant (ex. toolkit CUDA déjà installé, dépôt NVIDIA
déjà configuré, snap à ne pas utiliser). Usage : ./cli.py --install-guide
"""

import glob
import os
import re
import shutil
import subprocess
from typing import Dict, List, Optional, Tuple

from core.safety import nvidia_gpu_present

TOOLS_DIR = "~/.benchsuite/tools"          # compilations sans droits root, retrouvées automatiquement par BenchSuite

# Outils appelés avec une option de version sans danger (jamais gpu-burn : il lancerait une charge)
_VERSION_CMDS = {
    "stress-ng": ["--version"], "fio": ["--version"], "iperf3": ["--version"], "ipmitool": ["-V"],
    "smartctl": ["--version"], "nvme": ["version"], "ethtool": ["--version"], "dmidecode": ["--version"],
    "lspci": ["--version"], "sensors": ["-v"], "dcgmi": ["--version"], "clinfo": ["--version"],
    "vulkaninfo": ["--version"], "cargo": ["--version"], "cmake": ["--version"], "nvcc": ["--version"],
}
# Paquet apt de chaque outil système
_APT = {"stress-ng": "stress-ng", "fio": "fio", "iperf3": "iperf3", "ipmitool": "ipmitool", "smartctl": "smartmontools",
        "nvme": "nvme-cli", "ethtool": "ethtool", "dmidecode": "dmidecode", "lspci": "pciutils", "sensors": "lm-sensors",
        "clinfo": "clinfo", "vulkaninfo": "vulkan-tools"}
_ROLE = {"stress-ng": "CPU/RAM", "fio": "stockage", "iperf3": "réseau", "ipmitool": "BMC/IPMI", "smartctl": "SMART SATA",
         "nvme": "SMART NVMe", "ethtool": "liens réseau", "dmidecode": "DMI (carte mère, RAM)", "lspci": "bus PCIe",
         "sensors": "températures CPU", "clinfo": "OpenCL", "vulkaninfo": "Vulkan"}


def _run(cmd: List[str], timeout: int = 6) -> Tuple[int, str]:
    """Exécute une commande de lecture avec timeout ; (code, sortie) ; jamais d'exception."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception as e:
        return 127, str(e)


def _which(name: str) -> str:
    """Chemin d'un outil : PATH puis dossiers utilisateur usuels (cargo, ~/.local/bin, ~/.benchsuite/tools)."""
    p = shutil.which(name)
    if p:
        return p
    for d in ("~/.cargo/bin", "~/.local/bin", f"{TOOLS_DIR}/{name}", f"{TOOLS_DIR}/{name}/build"):
        c = os.path.join(os.path.expanduser(d), name)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return ""


def _version(name: str, path: str) -> str:
    """Première ligne de version d'un outil (vide si inconnue)."""
    if name not in _VERSION_CMDS or not path:
        return ""
    rc, out = _run([path] + _VERSION_CMDS[name])
    line = next((l.strip() for l in out.splitlines() if l.strip()), "")
    return line[:80] if rc in (0, 1) else ""


def _dpkg_installed(pkg: str) -> bool:
    rc, out = _run(["dpkg-query", "-W", "-f=${Status}", pkg])
    return rc == 0 and "install ok installed" in out


def _dpkg_list(pattern: str) -> List[str]:
    """Paquets installés dont le nom correspond au motif (avec version)."""
    rc, out = _run(["dpkg-query", "-W", "-f=${Package} ${Version} ${db:Status-Abbrev}\n", pattern])
    res = []
    for l in out.splitlines():
        p = l.split()
        if len(p) >= 3 and p[2].startswith("ii"):
            res.append(f"{p[0]} {p[1]}")
    return res


def _ver_tuple(v: str) -> Tuple[int, int]:
    m = re.search(r"(\d+)\.(\d+)", v or "")
    return (int(m.group(1)), int(m.group(2))) if m else (0, 0)


def _min_toolkit(cc: Tuple[int, int]) -> Tuple[int, int]:
    """Plus ancien CUDA capable de compiler pour une compute capability (connaissances publiques NVIDIA)."""
    if cc >= (12, 0):
        return (12, 8)       # Blackwell (RTX PRO 6000, RTX 50xx)
    if cc >= (10, 0):
        return (12, 8)       # Blackwell data-center
    if cc >= (9, 0):
        return (11, 8)       # Hopper
    if cc >= (8, 9):
        return (11, 8)       # Ada
    if cc >= (8, 6):
        return (11, 1)       # Ampere GA10x
    if cc >= (7, 5):
        return (10, 0)       # Turing (RTX 20xx)
    return (9, 0)


def collect() -> Dict:
    """Observe la machine (lecture seule) et retourne un dictionnaire d'état."""
    st: Dict = {"root": os.geteuid() == 0, "sudo": bool(shutil.which("sudo"))}

    # Système
    osr: Dict[str, str] = {}
    try:
        with open("/etc/os-release") as f:
            for l in f:
                if "=" in l:
                    k, v = l.rstrip().split("=", 1)
                    osr[k] = v.strip('"')
    except Exception:
        pass
    st["os"] = osr
    st["pm"] = "apt" if shutil.which("apt-get") else ("pacman" if shutil.which("pacman") else "")
    vid = osr.get("VERSION_ID", "").replace(".", "")
    st["cuda_repo_distro"] = f"{osr.get('ID', '')}{vid}" if osr.get("ID") in ("ubuntu", "debian") else ""
    st["arch"] = os.uname().machine

    # Modules Python
    mods = {}
    for m in ("psutil", "reportlab", "cairo"):
        try:
            __import__(m)
            mods[m] = True
        except Exception:
            mods[m] = False
    try:    # pycairo seul ne suffit pas : GTK a besoin du pont python3-gi-cairo
        import gi
        gi.require_foreign("cairo")
        mods["gi_cairo"] = True
    except Exception:
        mods["gi_cairo"] = False
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw  # noqa: F401
        mods["gi"] = True
    except Exception:
        mods["gi"] = False
    st["python_modules"] = mods

    # Outils système
    tools = {}
    for t in list(_APT) + ["gpu-fryer", "dcgmi", "nvbandwidth", "clpeak", "vkpeak", "cargo", "cmake", "git", "make", "nvcc", "nv-hostengine", "xpu-smi", "axdevice", "axmonitor", "axrunmodel"]:
        p = _which(t)
        if not p and t in ("axdevice", "axmonitor", "axrunmodel"):
            try:
                from core import axelera_aipu
                p = axelera_aipu.find_tool(t)
            except Exception:
                pass
        tools[t] = {"path": p, "version": _version(t, p)}
    st["tools"] = tools

    # GPU Intel et accélérateurs Axelera (1f9d) présents sur le bus PCI
    lspci_lines = (_run(["lspci", "-nn"])[1] if shutil.which("lspci") else "").splitlines()
    st["intel_gpu"] = any("intel" in l.lower() and re.search(r"\[03(00|02|80)\]", l) for l in lspci_lines)
    st["axelera_aipu"] = any("[1f9d:" in l.lower() or ("axelera" in l.lower() and re.search(r"\[1200\]", l)) for l in lspci_lines)

    # NVIDIA
    nv: Dict = {"present": bool(shutil.which("nvidia-smi")) and nvidia_gpu_present(), "ok": False, "driver": "", "cuda_max": "", "caps": [], "names": [], "error": ""}
    if nv["present"]:
        rc, out = _run(["nvidia-smi"], 15)
        if rc == 0:
            nv["ok"] = True
            m = re.search(r"Driver Version:\s*([\d.]+)", out)
            nv["driver"] = m.group(1) if m else ""
            m = re.search(r"CUDA (?:UMD )?Version:\s*([\d.]+)", out)
            nv["cuda_max"] = m.group(1) if m else ""
            rc2, o2 = _run(["nvidia-smi", "--query-gpu=name,compute_cap", "--format=csv,noheader"], 15)
            for l in o2.splitlines():
                if "," in l:
                    n, c = [x.strip() for x in l.rsplit(",", 1)]
                    nv["names"].append(n)
                    nv["caps"].append(c)
        else:
            nv["error"] = " ".join(out.split())[:200]
    st["nvidia"] = nv

    # Toolkits CUDA installés
    tk = []
    for nvcc in sorted(glob.glob("/usr/local/cuda*/bin/nvcc") + glob.glob("/opt/cuda*/bin/nvcc")):
        rc, out = _run([nvcc, "--version"])
        m = re.search(r"release (\d+\.\d+)", out)
        tk.append({"nvcc": nvcc, "root": os.path.dirname(os.path.dirname(nvcc)), "version": m.group(1) if m else ""})
    p = tools["nvcc"]["path"]
    if p and not any(os.path.realpath(p) == os.path.realpath(t["nvcc"]) for t in tk):
        m = re.search(r"release (\d+\.\d+)", tools["nvcc"]["version"] or "") or re.search(r"release (\d+\.\d+)", _run([p, "--version"])[1])
        tk.append({"nvcc": p, "root": os.path.dirname(os.path.dirname(os.path.realpath(p))), "version": m.group(1) if m else ""})
    st["toolkits"] = tk
    libs = sorted(set(os.path.basename(f) for f in glob.glob("/usr/local/cuda*/lib64/libcublas.so.*") +
                      glob.glob("/usr/lib/x86_64-linux-gnu/libcublas.so.*") + glob.glob("/usr/lib/aarch64-linux-gnu/libcublas.so.*")))
    st["cublas_libs"] = libs

    # gpu-burn : emplacements connus, santé du binaire (ldd, sans l'exécuter)
    burn = []
    for path in ["/opt/gpu-burn/gpu_burn", "/usr/local/bin/gpu_burn", "/usr/local/bin/gpu-burn",
                 os.path.expanduser(f"{TOOLS_DIR}/gpu-burn/gpu_burn"), os.path.expanduser("~/.local/bin/gpu_burn"),
                 shutil.which("gpu_burn") or "", shutil.which("gpu-burn") or ""]:
        if path and os.path.isfile(path) and os.access(path, os.X_OK) and path not in [b["path"] for b in burn]:
            missing = []
            if not path.startswith("/snap/") and shutil.which("ldd"):
                rc, out = _run(["ldd", path])
                missing = [l.split("=>")[0].strip() for l in out.splitlines() if "not found" in l]
            d = os.path.dirname(os.path.realpath(path))
            if not path.startswith("/snap/") and not glob.glob(os.path.join(d, "compare.*")):
                missing.append("compare.ptx / compare.fatbin (à côté du binaire)")
            burn.append({"path": path, "snap": path.startswith("/snap/"), "missing": missing})
    st["gpu_burn"] = burn
    st["snap_gpu_burn"] = bool(shutil.which("snap")) and _run(["snap", "list", "gpu-burn"])[0] == 0

    # DCGM
    st["dcgm"] = {"packages": _dpkg_list("datacenter-gpu-manager*"),
                  "snap": bool(shutil.which("snap")) and _run(["snap", "list", "dcgm"])[0] == 0,
                  "service": _run(["systemctl", "is-active", "nvidia-dcgm"])[1].strip() if shutil.which("systemctl") else ""}

    # Dépôts NVIDIA déjà configurés ? (lecture des fichiers apt)
    repo = False
    for f in ["/etc/apt/sources.list"] + glob.glob("/etc/apt/sources.list.d/*"):
        try:
            with open(f, errors="ignore") as fh:
                if "developer.download.nvidia.com/compute/cuda" in fh.read():
                    repo = True
                    break
        except Exception:
            pass
    st["nvidia_cuda_repo"] = repo
    st["boost_po"] = _dpkg_installed("libboost-program-options-dev") if st["pm"] == "apt" else None

    # OpenCL / Vulkan
    st["opencl_icd"] = sorted(glob.glob("/etc/OpenCL/vendors/*.icd"))
    st["vulkan_icd"] = sorted(glob.glob("/usr/share/vulkan/icd.d/*.json") + glob.glob("/etc/vulkan/icd.d/*.json"))
    return st


def _sudo(st: Dict) -> str:
    return "" if st["root"] else "sudo "


def _toolkit_choice(st: Dict) -> Tuple[Optional[Dict], Tuple[int, int], Tuple[int, int], str]:
    """Toolkit déjà installé utilisable (version ≥ besoin du GPU et ≤ CUDA du pilote) ; (toolkit, min, max, cc)."""
    nv = st["nvidia"]
    caps = [_ver_tuple(c) for c in nv["caps"]]
    cc_min = min(caps) if caps else (0, 0)
    cc_max = max(caps) if caps else (0, 0)
    need = _min_toolkit(cc_max) if caps else (0, 0)
    top = _ver_tuple(nv["cuda_max"]) if nv["cuda_max"] else (99, 0)
    usable = [t for t in st["toolkits"] if t["version"] and need <= _ver_tuple(t["version"]) <= top]
    best = max(usable, key=lambda t: _ver_tuple(t["version"])) if usable else None
    cc = f"{cc_min[0]}{cc_min[1]}" if caps else ""
    return best, need, top, cc


def render(st: Dict) -> str:
    """Construit le guide texte à partir de l'état observé."""
    L: List[str] = []
    ok, bad, warn = "✓", "✗", "⚠"
    sudo = _sudo(st)
    osr = st["os"]
    L += ["GUIDE D'INSTALLATION — adapté à cette machine (aucune commande n'a été exécutée)", "=" * 70,
          f"Système : {osr.get('PRETTY_NAME', 'inconnu')} · {st['arch']} · gestionnaire : {st['pm'] or 'inconnu'} · "
          f"{'root' if st['root'] else 'utilisateur simple' + (' (sudo disponible)' if st['sudo'] else ' (sans sudo)')}",
          "Règle : on ne touche à rien de ce qui fonctionne. Les lignes ✓ ne demandent aucune action.", ""]

    # 1. Python / GTK
    L += ["1. Interface (Python, GTK4)", "-" * 40]
    mods = st["python_modules"]
    names = {"gi": ("GTK4 + Libadwaita (PyGObject)", "python3-gi python3-gi-cairo gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0"),
             "cairo": ("pycairo (graphiques)", "python3-cairo"),
             "gi_cairo": ("pont GTK↔cairo (graphiques sans erreur « foreign struct converter »)", "python3-gi-cairo"),
             "psutil": ("psutil", "python3-psutil"), "reportlab": ("reportlab (PDF)", "python3-reportlab")}
    miss_apt: List[str] = []
    for m, (label, pkgs) in names.items():
        if mods.get(m):
            L.append(f"  {ok} {label}")
        else:
            L.append(f"  {bad} {label} manquant")
            miss_apt.append(pkgs)
    if miss_apt and st["pm"] == "apt":
        L += [f"    → {sudo}apt-get install -y --no-upgrade {' '.join(miss_apt)}",
              "      (--no-upgrade : n'altère aucun paquet déjà installé)"]
        if not mods.get("psutil") or not mods.get("reportlab"):
            L.append("    Sans droits root : python3 -m pip install --user psutil reportlab (n'écrit que dans votre dossier personnel)")
    elif miss_apt:
        L.append("    → installez l'équivalent de : " + " ".join(miss_apt))
    L.append("")

    # 2. Outils système
    L += ["2. Outils de mesure système", "-" * 40]
    missing_pkgs: List[str] = []
    for t, pkg in _APT.items():
        info = st["tools"][t]
        if info["path"]:
            L.append(f"  {ok} {t:<10} {_ROLE[t]:<22} {info['version'] or info['path']}")
        else:
            L.append(f"  {bad} {t:<10} {_ROLE[t]:<22} absent")
            missing_pkgs.append(pkg)
    if missing_pkgs and st["pm"] == "apt":
        L.append(f"    → {sudo}apt-get install -y --no-upgrade {' '.join(sorted(set(missing_pkgs)))}")
    elif missing_pkgs:
        L.append("    → installez : " + " ".join(sorted(set(missing_pkgs))))
    L.append("    Un outil absent désactive seulement le test concerné ; rien d'autre n'est affecté.")
    L.append("")

    # 3a. GPU Intel (Arc, Data Center GPU Flex / Max) : xpu-smi
    if st.get("intel_gpu"):
        L += ["3. GPU Intel (xpu-smi)", "-" * 40]
        xp = st["tools"]["xpu-smi"]
        if xp["path"]:
            L.append(f"  {ok} xpu-smi présent : {xp['path']} — BenchSuite l'utilise (sudo -n puis direct) pour le nom, la mémoire, "
                     "les températures cœur/mémoire, la puissance, la fréquence et l'utilisation.")
            L.append("    Si les mesures manquent : sudo xpu-smi discovery   et   sudo xpu-smi stats -d 0   doivent répondre ; "
                     "sans mot de passe : règle sudoers pour xpu-smi.")
        else:
            L += [f"  {bad} xpu-smi absent : seule la température exposée par le noyau est lue (sysfs).",
                  "    → paquet « xpu-smi » du dépôt APT Intel GPU (https://dgpu-docs.intel.com/ : ajouter le dépôt de votre distribution,",
                  f"      puis {sudo}apt-get install -y --no-upgrade xpu-smi). Rien n'est installé automatiquement."]
        L.append("")

    # 3a-bis. Accélérateurs Axelera AI (Metis)
    if st.get("axelera_aipu"):
        L += ["3a. Accélérateurs Axelera AI (Metis)", "-" * 40]
        ax_drv = os.path.isfile("/sys/class/metis/version")
        if ax_drv:
            try:
                with open("/sys/class/metis/version") as f:
                    dver = f.read().strip()
            except Exception:
                dver = "?"
            L.append(f"  {ok} module noyau metis chargé (version {dver})")
        else:
            L += [f"  {bad} module noyau metis absent (/sys/class/metis/version introuvable)",
                  f"    → {sudo}apt install -y linux-headers-$(uname -r) dkms && axdevice driver --install"]
        for tname in ("axdevice", "axrunmodel", "axmonitor"):
            ti = st["tools"].get(tname, {})
            if ti.get("path"):
                L.append(f"  {ok} {tname:<12} {ti['path']}")
            else:
                L.append(f"  {bad} {tname:<12} absent (activer ou installer Voyager SDK : https://github.com/axelera-ai-hub/voyager-sdk)")
        L.append("")

    # 3b. GPU NVIDIA
    L += []
    nv = st["nvidia"]
    L += ["3b. GPU NVIDIA", "-" * 40]
    if not nv["present"]:
        L += [f"  ⊘ nvidia-smi absent : pas de GPU NVIDIA géré ici (ou pilote non installé). Les tests GPU NVIDIA sont ignorés.", ""]
        return "\n".join(L)
    if not nv["ok"]:
        L += [f"  {bad} nvidia-smi échoue : {nv['error']}",
              "    Le pilote est en cause : aucun test GPU ne peut marcher tant que nvidia-smi ne répond pas.",
              "    « API mismatch / version mismatch » = module noyau et bibliothèques de versions différentes :",
              "    → cat /proc/driver/nvidia/version        (version du module chargé)",
              f"    → {sudo}dkms status                       (module construit pour ce noyau ?)",
              f"    → {sudo}dkms autoinstall && {sudo}reboot   (reconstruit le module)",
              "    BenchSuite ne modifie jamais le pilote.", ""]
        return "\n".join(L)
    best, need, top, cc = _toolkit_choice(st)
    L.append(f"  {ok} pilote {nv['driver']} · CUDA maximal accepté : {nv['cuda_max'] or '?'} · GPU : "
             + ", ".join(sorted(set(f"{n} (cc {c})" for n, c in zip(nv["names"], nv["caps"])))))
    if st["toolkits"]:
        L.append("  Toolkits CUDA présents : " + ", ".join(f"{t['version'] or '?'} ({t['root']})" for t in st["toolkits"]))
    else:
        L.append("  Aucun toolkit CUDA (nvcc) détecté — normal si vous n'avez pas besoin de compiler.")
    L.append(f"  Besoin pour compiler pour ces GPU : toolkit CUDA ≥ {need[0]}.{need[1]} et ≤ {top[0]}.{top[1]} (limite du pilote)")
    L.append("")

    # 3a. gpu-burn
    L += ["  gpu-burn", "  " + "-" * 38]
    healthy = [b for b in st["gpu_burn"] if not b["snap"] and not b["missing"]]
    if healthy:
        L.append(f"    {ok} fonctionnel : {healthy[0]['path']} (à ne pas toucher)")
    else:
        for b in st["gpu_burn"]:
            if b["missing"]:
                L.append(f"    {warn} {b['path']} : bibliothèques introuvables → {', '.join(b['missing'])}")
        if st["snap_gpu_burn"] or any(b["snap"] for b in st["gpu_burn"]):
            L.append(f"    {warn} version snap présente : elle ne voit pas le pilote NVIDIA (libcuda.so.1 introuvable). "
                     "Ne la supprimez pas : une version compilée est utilisée en priorité.")
        if not st["gpu_burn"] and not st["snap_gpu_burn"]:
            L.append(f"    {bad} gpu-burn absent")
        L.append("    → à compiler dans votre dossier personnel (aucun droit root, aucun fichier système modifié) :")
        if best:
            L += [f"      Toolkit utilisable déjà installé : CUDA {best['version']} ({best['root']}) — rien à installer.",
                  f"      mkdir -p {TOOLS_DIR} && cd {TOOLS_DIR}",
                  "      git clone --depth 1 https://github.com/wilicc/gpu-burn && cd gpu-burn",
                  f"      make COMPUTE={cc} CUDAPATH={best['root']}",
                  "      ldd ./gpu_burn | grep 'not found'      # ne doit rien afficher",
                  "      ls compare.*                             # compare.ptx ou compare.fatbin : doit exister à côté de gpu_burn",
                  "      BenchSuite le retrouve tout seul dans ce dossier."]
            if st["toolkits"] and any(_ver_tuple(t["version"]) < need for t in st["toolkits"] if t["version"]):
                L.append("      (les toolkits plus anciens que le minimum ci-dessus sont ignorés : ils ne savent pas cibler ces GPU)")
        else:
            L.append(f"      Aucun toolkit CUDA adapté (il faut entre {need[0]}.{need[1]} et {top[0]}.{top[1]}). Installez-en un SANS toucher au pilote :")
            if st["pm"] == "apt" and st["cuda_repo_distro"]:
                if not st["nvidia_cuda_repo"]:
                    L += [f"      wget https://developer.download.nvidia.com/compute/cuda/repos/{st['cuda_repo_distro']}/{st['arch']}/cuda-keyring_1.1-1_all.deb",
                          f"      {sudo}dpkg -i cuda-keyring_1.1-1_all.deb && {sudo}apt-get update"]
                else:
                    L.append("      (dépôt NVIDIA CUDA déjà configuré : pas besoin de cuda-keyring)")
                L += ["      apt-cache search '^cuda-toolkit-[0-9]+-[0-9]+$'      # versions disponibles",
                      f"      {sudo}apt-get install --no-install-recommends cuda-toolkit-<X>-<Y>",
                      f"        (<X>-<Y> entre {need[0]}-{need[1]} et {top[0]}-{top[1]} ; ex. cuda-toolkit-{need[0]}-{need[1]})",
                      "      ⚠ jamais « cuda », « cuda-13-x » ni « cuda-drivers » : ces méta-paquets remplacent le pilote.",
                      "      ⚠ lisez la liste proposée par apt avant de valider : aucun paquet nvidia-driver-* / nvidia-kernel-* ne doit y figurer."]
            else:
                L.append("      Téléchargez le toolkit sur https://developer.nvidia.com/cuda-downloads (installeur « runfile » : "
                         "--toolkit --silent, sans --driver).")
            L.append("      puis relancez ./cli.py --install-guide : il vous donnera la commande de compilation exacte.")
    L.append("")

    # 3b. gpu-fryer
    L += ["  gpu-fryer", "  " + "-" * 38]
    gf = st["tools"]["gpu-fryer"]
    if gf["path"]:
        L.append(f"    {ok} présent : {gf['path']}")
    elif st["tools"]["cargo"]["path"]:
        L += [f"    {bad} absent", "    → cargo install gpu-fryer      (installe dans ~/.cargo/bin, rien de système)"]
    else:
        L += [f"    {bad} absent (Rust/Cargo absent aussi)",
              "    → Rust sans droits root : curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh   (dans ~/.cargo)",
              "    → puis : cargo install gpu-fryer"]
    L.append("")

    # 3c. DCGM
    L += ["  DCGM (dcgmi diag)", "  " + "-" * 38]
    d = st["dcgm"]
    maj = _ver_tuple(nv["cuda_max"])[0] if nv["cuda_max"] else 0
    want = f"datacenter-gpu-manager-4-cuda{maj}" if maj else "datacenter-gpu-manager-4-cuda<N>"
    if d["snap"]:
        L.append(f"    {warn} version snap détectée : confinée, elle ne voit pas les GPU. À remplacer par le paquet apt : "
                 f"{sudo}snap remove dcgm (non fait automatiquement)")
    if st["tools"]["dcgmi"]["path"] and not d["snap"]:
        L.append(f"    {ok} dcgmi présent : {st['tools']['dcgmi']['version'] or st['tools']['dcgmi']['path']}")
        if d["packages"]:
            L.append("      paquets : " + ", ".join(d["packages"]))
            mm = re.search(r"cuda(\d+)", " ".join(d["packages"]))
            if mm and maj and int(mm.group(1)) != maj:
                L.append(f"      {warn} paquet cuda{mm.group(1)} alors que le pilote est en CUDA {maj} : installez {want} si DCGM ne voit pas les GPU")
        if d["service"] and d["service"] != "active":
            L.append(f"    {warn} service nvidia-dcgm : {d['service']} → {sudo}systemctl enable --now nvidia-dcgm "
                     "(BenchSuite le démarre lui-même sauf en mode lecture seule)")
        else:
            L.append(f"    {ok} service nvidia-dcgm actif")
    elif st["pm"] == "apt":
        L.append(f"    {bad} absent")
        if not st["nvidia_cuda_repo"] and st["cuda_repo_distro"]:
            L += [f"    → wget https://developer.download.nvidia.com/compute/cuda/repos/{st['cuda_repo_distro']}/{st['arch']}/cuda-keyring_1.1-1_all.deb",
                  f"    → {sudo}dpkg -i cuda-keyring_1.1-1_all.deb && {sudo}apt-get update"]
        elif st["nvidia_cuda_repo"]:
            L.append("    (dépôt NVIDIA CUDA déjà configuré)")
        L += [f"    → {sudo}apt-get install -y {want}      # cuda{maj or '<N>'} = CUDA maximal du pilote",
              f"    → {sudo}systemctl enable --now nvidia-dcgm"]
    L.append("")

    # 3d. nvbandwidth
    L += ["  nvbandwidth", "  " + "-" * 38]
    nb = st["tools"]["nvbandwidth"]
    if nb["path"]:
        L.append(f"    {ok} présent : {nb['path']}")
    else:
        L.append(f"    {bad} absent → compilation dans votre dossier personnel :")
        need_pkgs = []
        if not st["tools"]["cmake"]["path"]:
            need_pkgs.append("cmake")
        if st["boost_po"] is False:
            need_pkgs.append("libboost-program-options-dev")
        if not st["tools"]["git"]["path"]:
            need_pkgs.append("git")
        if need_pkgs:
            L.append(f"      {sudo}apt-get install -y --no-upgrade {' '.join(need_pkgs)}")
        if best:
            L += [f"      mkdir -p {TOOLS_DIR} && cd {TOOLS_DIR}",
                  "      git clone --depth 1 https://github.com/NVIDIA/nvbandwidth && cd nvbandwidth",
                  f"      cmake -DCMAKE_CUDA_COMPILER={best['nvcc']} . && make -j$(nproc)",
                  "      BenchSuite le retrouve dans ce dossier."]
        else:
            L.append("      nécessite un toolkit CUDA (voir gpu-burn ci-dessus), puis relancez ce guide.")
    L.append("")

    # 4. OpenCL / Vulkan
    L += ["4. Compute OpenCL / Vulkan (clpeak, vkpeak)", "-" * 40]
    for t in ("clpeak", "vkpeak"):
        i = st["tools"][t]
        L.append(f"  {ok if i['path'] else bad} {t:<8} {i['path'] or 'absent'}")
    L.append(f"  {ok if st['opencl_icd'] else bad} pilote OpenCL : {', '.join(os.path.basename(x) for x in st['opencl_icd']) or 'aucun .icd'}"
             + ("" if st['opencl_icd'] else f"   → {sudo}apt-get install -y --no-upgrade ocl-icd-libopencl1 (+ libnvidia-compute-<version du pilote>)"))
    L.append(f"  {ok if st['vulkan_icd'] else bad} pilote Vulkan : {len(st['vulkan_icd'])} fichier(s) ICD"
             + ("" if st['vulkan_icd'] else f"   → {sudo}apt-get install -y --no-upgrade libvulkan1"))
    if not st["tools"]["clpeak"]["path"]:
        L.append(f"  clpeak : {sudo}apt-get install -y --no-upgrade clpeak   (si introuvable : https://github.com/krrishnarraj/clpeak)")
    if not st["tools"]["vkpeak"]["path"]:
        L.append(f"  vkpeak : binaire Linux sur https://github.com/nihui/vkpeak/releases → mkdir -p ~/.local/bin && "
                 "mv vkpeak ~/.local/bin/ && chmod +x ~/.local/bin/vkpeak (sans root)")
    L += ["", "Pour vérifier ensuite : ./check_environment.py   (lecture seule)",
          "BenchSuite n'installe ni ne désinstalle jamais rien ; il peut lancer ce dont les tests ont besoin (module IPMI, service DCGM, "
          "mode persistance GPU). Option --read-only / BENCHSUITE_READ_ONLY=1 pour l'interdire."]
    return "\n".join(L)


def print_install_guide() -> int:
    """Affiche le guide d'installation de cette machine (lecture seule)."""
    print(render(collect()))
    return 0
