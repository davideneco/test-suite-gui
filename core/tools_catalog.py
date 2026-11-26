"""
tools_catalog.py — Catalogue des outils et dépendances de BenchSuite : à quoi ils servent, comment les détecter, comment les
installer (commandes à copier) et où trouver leur documentation officielle.

Données seulement, lecture seule : BenchSuite n'installe JAMAIS rien lui-même. La page « Outils & dépendances » affiche ces
fiches, détecte ce qui est présent (core.install_advisor pour les chemins et versions) et propose les commandes à copier.
Pour des commandes adaptées à la machine (CUDA déjà présent, dépôt NVIDIA déjà configuré…) : ./cli.py --install-guide.
"""

import glob
import importlib.util
import os
from typing import Dict, List, Tuple

# Niveaux d'importance
REQUIS, RECOMMANDE, OPTIONNEL = "requis", "recommandé", "optionnel"

_APT = "Ubuntu / Debian (apt)"
_KEYRING = ("distro=ubuntu$(. /etc/os-release; echo ${VERSION_ID//./})   # ex. ubuntu2204 ou ubuntu2404\n"
            "wget https://developer.download.nvidia.com/compute/cuda/repos/$distro/x86_64/cuda-keyring_1.1-1_all.deb\n"
            "sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt update")

# Groupes, dans l'ordre d'affichage : (identifiant, titre, description)
GROUPS: List[Tuple[str, str, str]] = [
    ("ui", "Interface (obligatoire)", "Sans eux le logiciel ne démarre pas."),
    ("system", "Mesure du système", "Lecture des capteurs, des disques, de la RAM, du PCIe et du BMC. Un outil absent masque seulement la mesure concernée."),
    ("engines", "Moteurs de test (CPU, RAM, stockage, réseau)", "Ces outils font réellement la charge ; le test correspondant est désactivé s'ils manquent."),
    ("build", "Compilation (pour gpu-burn, nvbandwidth…)", "Seulement si vous compilez vous-même certains outils GPU."),
    ("nvidia", "GPU NVIDIA", "Le pilote NVIDIA n'est jamais installé ni modifié par BenchSuite."),
    ("intel", "GPU Intel (Arc, Data Center GPU)", "xpu-smi donne températures, puissance, fréquence et utilisation ; le runtime OpenCL permet les tests de calcul."),
    ("amd", "GPU AMD", "Optionnel : températures hotspot / VRAM."),
    ("compute", "Calcul OpenCL / Vulkan (toutes marques)", "Tests de calcul et de bande passante sur n'importe quel GPU."),
]


def _t(tid, group, name, role, need, used_by, check, install, links, notes="", apt=None):
    return {"id": tid, "group": group, "name": name, "role": role, "need": need, "used_by": used_by, "check": check,
            "install": list(install), "links": links, "notes": notes, "apt": apt or []}


def install_hint(tool: str) -> list:
    """Procédures d'installation [(libellé, commande)] de l'outil `tool` (nom de binaire ou identifiant du catalogue) ; [] si inconnu.
    Sert au message « outil manquant » des pages de test."""
    for t in TOOLS:
        if t["id"] == tool or tool in (t["check"].get("bins") or []):
            return [(i.get("label", ""), i.get("cmd", "")) for i in t["install"] if i.get("cmd")]
    return []


TOOLS: List[Dict] = [
    # ── Interface ─────────────────────────────────────────────────────────
    _t("gtk", "ui", "GTK 4 + Libadwaita (PyGObject)", "Fenêtre et widgets de l'interface graphique.", REQUIS, ["Interface"],
       {"py": "gi"},
       [{"label": _APT, "cmd": "sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0"}],
       [("GTK", "https://www.gtk.org/"), ("Libadwaita", "https://gnome.pages.gitlab.gnome.org/libadwaita/"), ("PyGObject", "https://pygobject.gnome.org/")],
       apt=["python3-gi", "gir1.2-gtk-4.0", "gir1.2-adw-1", "libadwaita-1-0"]),
    _t("gicairo", "ui", "Pont GTK ↔ cairo (python3-gi-cairo)", "Dessin des graphiques. Sans lui, les graphiques sont désactivés.", RECOMMANDE,
       ["Graphiques temps réel", "Séquence automatique"], {"py": "gi_cairo"},
       [{"label": _APT, "cmd": "sudo apt install -y python3-gi-cairo python3-cairo"}],
       [("pycairo", "https://pycairo.readthedocs.io/")], apt=["python3-gi-cairo", "python3-cairo"]),
    _t("psutil", "ui", "psutil", "Lecture de l'utilisation CPU / RAM et des processus.", REQUIS, ["Télémétrie"], {"py": "psutil"},
       [{"label": _APT, "cmd": "sudo apt install -y python3-psutil"},
        {"label": "Sans droits root (pip)", "cmd": "python3 -m pip install --user psutil"}],
       [("psutil", "https://github.com/giampaolo/psutil")], apt=["python3-psutil"]),
    _t("reportlab", "ui", "ReportLab", "Génération des rapports PDF.", REQUIS, ["Exports PDF"], {"py": "reportlab"},
       [{"label": _APT, "cmd": "sudo apt install -y python3-reportlab"},
        {"label": "Sans droits root (pip)", "cmd": "python3 -m pip install --user reportlab"}],
       [("ReportLab", "https://www.reportlab.com/opensource/")], apt=["python3-reportlab"]),

    # ── Mesure du système ──────────────────────────────────────────────────
    _t("lspci", "system", "pciutils (lspci)", "Liste des périphériques PCIe, liens et GPU.", RECOMMANDE, ["Infos système", "Inventaire", "GPU"],
       {"bins": ["lspci"]}, [{"label": _APT, "cmd": "sudo apt install -y pciutils"}], [("pciutils", "https://mj.ucw.cz/sw/pciutils/")], apt=["pciutils"]),
    _t("dmidecode", "system", "dmidecode", "Carte mère, BIOS, barrettes de RAM (nécessite root). Le nombre d'emplacements RAM est le nombre de « Memory Device » (type 17) listés, vides compris — pas le champ « Number Of Devices » des « Physical Memory Array », que certains firmwares remplissent avec une valeur générique (ex. 16 pour 6 supports réels). Le canal mémoire n'est pas fourni par le SMBIOS : BenchSuite n'évalue donc pas le remplissage par canal.", RECOMMANDE, ["Infos système", "Traçabilité (n° de série)"],
       {"bins": ["dmidecode"]}, [{"label": _APT, "cmd": "sudo apt install -y dmidecode"}], [("dmidecode", "https://www.nongnu.org/dmidecode/")], apt=["dmidecode"]),
    _t("sensors", "system", "lm-sensors", "Températures du processeur et de la carte mère.", RECOMMANDE, ["Télémétrie CPU"],
       {"bins": ["sensors"]},
       [{"label": _APT, "cmd": "sudo apt install -y lm-sensors"},
        {"label": "Détection des capteurs (optionnel, écrit dans /etc)", "cmd": "sudo sensors-detect --auto"}],
       [("lm-sensors", "https://github.com/lm-sensors/lm-sensors")], apt=["lm-sensors"]),
    _t("ipmitool", "system", "ipmitool", "Capteurs, journal d'événements (SEL) et informations du BMC.", RECOMMANDE, ["Télémétrie serveur", "Inventaire"],
       {"bins": ["ipmitool"]}, [{"label": _APT, "cmd": "sudo apt install -y ipmitool"}], [("ipmitool", "https://github.com/ipmitool/ipmitool")],
       "Nécessite un BMC (serveur) ; le chargement des modules IPMI se fait au lancement de BenchSuite.", apt=["ipmitool"]),
    _t("smartctl", "system", "smartmontools (smartctl)", "Santé SMART des disques SATA / SAS.", RECOMMANDE, ["Diagnostic SSD / stockage"],
       {"bins": ["smartctl"]}, [{"label": _APT, "cmd": "sudo apt install -y smartmontools"}], [("smartmontools", "https://www.smartmontools.org/")], apt=["smartmontools"]),
    _t("nvme", "system", "nvme-cli", "Santé et journal SMART des SSD NVMe (erreurs, throttling, usure).", RECOMMANDE,
       ["Diagnostic SSD / stockage", "Profils fio Stabilité et Chauffe"], {"bins": ["nvme"]},
       [{"label": _APT, "cmd": "sudo apt install -y nvme-cli"}], [("nvme-cli", "https://github.com/linux-nvme/nvme-cli")], apt=["nvme-cli"]),
    _t("ethtool", "system", "ethtool", "Vitesse, pilote et firmware des cartes réseau.", RECOMMANDE, ["Infos système", "Réseau"],
       {"bins": ["ethtool"]}, [{"label": _APT, "cmd": "sudo apt install -y ethtool"}],
       [("ethtool", "https://mirrors.edge.kernel.org/pub/software/network/ethtool/")], apt=["ethtool"]),
    _t("powerstat", "system", "powerstat", "Puissance du système en temps réel (si ni RAPL ni capteur direct).", OPTIONNEL, ["Télémétrie puissance"],
       {"bins": ["powerstat"]}, [{"label": _APT, "cmd": "sudo apt install -y powerstat"}], [("powerstat", "https://github.com/ColinIanKing/powerstat")], apt=["powerstat"]),

    # ── Moteurs de test ─────────────────────────────────────────────────────
    _t("stress-ng", "engines", "stress-ng", "Charge CPU et RAM (profils 5 min, 30 min, 4 h, personnalisés).", REQUIS, ["CPU + RAM"],
       {"bins": ["stress-ng"]}, [{"label": _APT, "cmd": "sudo apt install -y stress-ng"}],
       [("Projet", "https://github.com/ColinIanKing/stress-ng"), ("Page de manuel", "https://manpages.ubuntu.com/manpages/noble/man1/stress-ng.1.html")], apt=["stress-ng"]),
    _t("fio", "engines", "fio", "Tests de stockage : profils NVMe / SATA / HDD, JEDEC, SNIA, OCP, stabilité, chauffe.", REQUIS,
       ["Stockage FIO NVMe", "Stockage FIO HDD/SATA", "Séquence automatique"], {"bins": ["fio"]},
       [{"label": _APT, "cmd": "sudo apt install -y fio"}],
       [("Projet", "https://github.com/axboe/fio"), ("Documentation", "https://fio.readthedocs.io/")],
       "fio 3.20 ou plus récent recommandé ; io_uring demande un noyau récent.", apt=["fio"]),
    _t("iperf3", "engines", "iperf3", "Débit réseau TCP / UDP (jitter, pertes).", RECOMMANDE, ["Réseau"], {"bins": ["iperf3"]},
       [{"label": _APT, "cmd": "sudo apt install -y iperf3"}], [("Projet", "https://github.com/esnet/iperf"), ("Site", "https://iperf.fr/")], apt=["iperf3"]),
    _t("gcc", "engines", "gcc (compilateur C)", "Compilation automatique de STREAM (bande passante RAM).", RECOMMANDE, ["RAM — Bande passante (STREAM)"],
       {"bins": ["gcc"]}, [{"label": _APT, "cmd": "sudo apt install -y build-essential"}], [("STREAM", "https://www.cs.virginia.edu/stream/")], apt=["build-essential"]),

    # ── Compilation ──────────────────────────────────────────────────────────
    _t("git", "build", "git", "Récupérer les sources de gpu-burn, nvbandwidth…", OPTIONNEL, ["Compilation d'outils GPU"], {"bins": ["git"]},
       [{"label": _APT, "cmd": "sudo apt install -y git"}], [("git", "https://git-scm.com/")], apt=["git"]),
    _t("make", "build", "make", "Compiler gpu-burn.", OPTIONNEL, ["gpu-burn"], {"bins": ["make"]},
       [{"label": _APT, "cmd": "sudo apt install -y build-essential"}], [("GNU make", "https://www.gnu.org/software/make/")], apt=["build-essential"]),
    _t("cmake", "build", "cmake", "Compiler nvbandwidth.", OPTIONNEL, ["nvbandwidth"], {"bins": ["cmake"]},
       [{"label": _APT, "cmd": "sudo apt install -y cmake libboost-program-options-dev"}], [("CMake", "https://cmake.org/")], apt=["cmake", "libboost-program-options-dev"]),
    _t("cargo", "build", "Rust (cargo)", "Installer gpu-fryer.", OPTIONNEL, ["GPU Compute fryer"], {"bins": ["cargo"]},
       [{"label": "Sans droits root (rustup, installe dans ~/.cargo)", "cmd": "curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh\nsource ~/.cargo/env"}],
       [("rustup", "https://rustup.rs/")]),

    # ── NVIDIA ───────────────────────────────────────────────────────────────
    _t("nvidia-smi", "nvidia", "Pilote NVIDIA (nvidia-smi)", "Température, puissance, fréquences et état des GPU NVIDIA.", RECOMMANDE, ["Tous les tests GPU NVIDIA"],
       {"bins": ["nvidia-smi"]},
       [{"label": _APT + " — pilote recommandé pour votre carte", "cmd": "ubuntu-drivers devices          # liste les pilotes\nsudo ubuntu-drivers install     # installe le recommandé"}],
       [("Téléchargement des pilotes", "https://www.nvidia.com/Download/index.aspx")],
       "BenchSuite ne touche jamais au pilote. Si nvidia-smi répond « API mismatch », le module noyau et les bibliothèques n'ont pas la même version : "
       "sudo dkms autoinstall && sudo reboot."),
    _t("nvcc", "nvidia", "Toolkit CUDA (nvcc)", "Compiler gpu-burn / nvbandwidth. Blackwell (RTX PRO 6000, 50xx) : CUDA 12.8 ou plus.", OPTIONNEL,
       ["Compilation de gpu-burn / nvbandwidth"], {"bins": ["nvcc"], "glob": ["/usr/local/cuda*/bin/nvcc", "/opt/cuda*/bin/nvcc"]},
       [{"label": _APT + " — dépôt NVIDIA puis toolkit seul", "cmd": _KEYRING + "\napt-cache search '^cuda-toolkit-[0-9]+-[0-9]+$'   # versions disponibles\n"
                                                                        "sudo apt install -y cuda-toolkit-<X>-<Y>"}],
       [("Téléchargements CUDA", "https://developer.nvidia.com/cuda-downloads"), ("Compatibilité pilote ↔ CUDA", "https://docs.nvidia.com/deploy/cuda-compatibility/")],
       "⚠ Installez uniquement « cuda-toolkit-X-Y ». Les méta-paquets « cuda », « cuda-X-Y » et « cuda-drivers » remplacent le pilote. "
       "La version du toolkit doit rester ≤ au CUDA maximal affiché par nvidia-smi."),
    _t("gpu-burn", "nvidia", "gpu-burn", "Stress CUDA (GFLOPS + erreurs mémoire) et intégrité VRAM.", OPTIONNEL, ["GPU gpu-burn", "GPU — Intégrité VRAM"],
       {"bins": ["gpu_burn", "gpu-burn"], "glob": ["/opt/gpu-burn/gpu_burn", "~/.benchsuite/tools/gpu-burn/gpu_burn"]},
       [{"label": "Compilation sans droits root (dans votre dossier)",
         "cmd": "mkdir -p ~/.benchsuite/tools && cd ~/.benchsuite/tools\ngit clone --depth 1 https://github.com/wilicc/gpu-burn && cd gpu-burn\n"
                "nvidia-smi --query-gpu=compute_cap --format=csv,noheader     # 7.5 → COMPUTE=75, 8.6 → 86, 8.9 → 89, 12.0 → 120\n"
                "make COMPUTE=<arch> CUDAPATH=/usr/local/cuda-<X.Y>\nldd ./gpu_burn | grep 'not found'     # ne doit rien afficher\nls compare.*                          # compare.ptx ou compare.fatbin"}],
       [("Projet", "https://github.com/wilicc/gpu-burn")],
       "Le binaire gpu_burn et son fichier compare.* doivent venir du même « make ». Éviter la version snap (confinée : elle ne voit pas le pilote). "
       "Erreur « libcublas.so.NN introuvable » : le toolkit CUDA utilisé à la compilation n'est pas celui installé."),
    _t("gpu-fryer", "nvidia", "gpu-fryer", "Stress de calcul GPU multi-GPU (TFLOPS).", OPTIONNEL, ["GPU Compute fryer"],
       {"bins": ["gpu-fryer"]}, [{"label": "Via cargo (installe dans ~/.cargo/bin)", "cmd": "cargo install gpu-fryer"}],
       [("Projet", "https://github.com/huggingface/gpu-fryer")], "NVIDIA uniquement (confirmé à l'usage sur une machine Intel Arc : ne fonctionne pas). Pour Intel / AMD : vkpeak / clpeak en mode Chauffe."),
    _t("nvbandwidth", "nvidia", "nvbandwidth", "Bande passante CPU ↔ GPU et GPU ↔ GPU.", OPTIONNEL, ["GPU nvbandwidth", "GPU — Bande passante VRAM"],
       {"bins": ["nvbandwidth"], "glob": ["~/.benchsuite/tools/nvbandwidth/build/nvbandwidth", "~/nvbandwidth/build/nvbandwidth"]},
       [{"label": "Compilation sans droits root (nécessite le toolkit CUDA, cmake, Boost)",
         "cmd": "sudo apt install -y cmake libboost-program-options-dev git\nmkdir -p ~/.benchsuite/tools && cd ~/.benchsuite/tools\n"
                "git clone --depth 1 https://github.com/NVIDIA/nvbandwidth && cd nvbandwidth\ncmake -S . -B build && cmake --build build -j$(nproc)"}],
       [("Projet", "https://github.com/NVIDIA/nvbandwidth")]),
    _t("dcgmi", "nvidia", "DCGM (dcgmi diag)", "Diagnostic matériel NVIDIA : mémoire, calcul, PCIe, bande passante, stress.", OPTIONNEL, ["GPU — Diagnostic DCGM"],
       {"bins": ["dcgmi"]},
       [{"label": _APT + " — dépôt NVIDIA puis paquet correspondant au CUDA du pilote",
         "cmd": _KEYRING + "\nsudo apt install -y datacenter-gpu-manager-4-cuda<N>     # N = CUDA majeur affiché par nvidia-smi (12 ou 13)\n"
                           "sudo systemctl enable --now nvidia-dcgm"}],
       [("Page DCGM", "https://developer.nvidia.com/dcgm"), ("Documentation", "https://docs.nvidia.com/datacenter/dcgm/latest/")],
       "⚠ Pas le snap « dcgm » : confiné, il ne voit aucun GPU (à désinstaller à la main s'il est présent)."),

    # ── Intel ─────────────────────────────────────────────────────────────────
    _t("xpu-smi", "intel", "xpu-smi (Intel XPU Manager)", "Nom, mémoire, températures cœur / mémoire, puissance, fréquence, utilisation des GPU Intel.", RECOMMANDE,
       ["Télémétrie GPU Intel"], {"bins": ["xpu-smi"]},
       [{"label": "Paquet du dépôt APT Intel GPU (ajouter d'abord le dépôt de votre distribution, voir le lien)", "cmd": "sudo apt install -y xpu-smi"}],
       [("Documentation Intel GPU (dépôts, pilotes)", "https://dgpu-docs.intel.com/"), ("Projet XPU Manager", "https://github.com/intel/xpumanager")],
       "BenchSuite l'appelle en « sudo -n » puis en direct : il faut root ou une règle sudo sans mot de passe pour xpu-smi."),
    _t("intel-opencl", "intel", "Runtime OpenCL Intel (compute-runtime)", "Permet à clpeak et clinfo de voir le GPU Intel.", RECOMMANDE, ["Compute OpenCL"],
       {"glob": ["/etc/OpenCL/vendors/intel*.icd"]},
       [{"label": _APT, "cmd": "sudo apt install -y intel-opencl-icd"}],
       [("Projet", "https://github.com/intel/compute-runtime"), ("Documentation Intel GPU", "https://dgpu-docs.intel.com/")],
       "Une carte récente (Arc Pro B-series / Battlemage) demande un runtime récent (version 26.18 ou plus d'après un retour d'utilisateur) : "
       "utilisez le dépôt Intel GPU plutôt que celui de la distribution si le GPU n'est pas vu."),

    # ── AMD ───────────────────────────────────────────────────────────────────
    _t("lact", "amd", "LACT", "Températures hotspot et VRAM des GPU AMD (pas exposées par le noyau).", OPTIONNEL, ["Télémétrie GPU AMD"],
       {"bins": ["lact"]}, [{"label": "Paquet .deb de la page des versions", "cmd": "# télécharger le .deb correspondant à votre distribution, puis :\nsudo apt install ./lact-*.deb"}],
       [("Projet / versions", "https://github.com/ilya-zlobintsev/LACT/releases")]),

    # ── OpenCL / Vulkan ───────────────────────────────────────────────────────
    _t("clinfo", "compute", "clinfo + chargeur OpenCL", "Liste les plateformes et GPU OpenCL (sert à choisir -p N).", RECOMMANDE, ["Compute OpenCL"],
       {"bins": ["clinfo"]}, [{"label": _APT, "cmd": "sudo apt install -y clinfo ocl-icd-libopencl1"}], [("clinfo", "https://github.com/Oblomov/clinfo")],
       "« clinfo -l » indique le numéro de plateforme du GPU. Un pilote OpenCL d'une autre marque peut rester listé en plateforme 0 sans gêner BenchSuite.",
       apt=["clinfo", "ocl-icd-libopencl1"]),
    _t("clpeak", "compute", "clpeak", "Calcul (SP / HP / DP / entiers) et bande passante mémoire / transferts via OpenCL.", OPTIONNEL, ["Compute OpenCL"],
       {"bins": ["clpeak"]},
       [{"label": _APT, "cmd": "sudo apt install -y clpeak"}, {"label": "Alternative (snap)", "cmd": "sudo snap install clpeak --classic"}],
       [("Projet", "https://github.com/krrishnarraj/clpeak")],
       "Sur une machine avec plusieurs plateformes OpenCL, lancez « clpeak -p N -d 0 » (BenchSuite le fait tout seul).", apt=["clpeak"]),
    _t("oclburn", "compute", "oclburn (burn OpenCL vérifié, fourni avec BenchSuite)",
       "Burn à 100 % de tout GPU OpenCL (Intel, AMD, NVIDIA) : calcul entier / FP32 / FP16 et VRAM en continu, avec vérification des résultats.",
       OPTIONNEL, ["GPU — Burn OpenCL vérifié"],
       {"bins": ["oclburn"], "glob": ["~/.benchsuite/tools/oclburn/oclburn"]},
       [{"label": "Rien à installer : BenchSuite compile sa propre source au premier lancement (gcc + pilote OpenCL requis)",
         "cmd": "sudo apt install -y gcc ocl-icd-libopencl1 clinfo     # + pilote OpenCL du GPU (Intel compute-runtime, ROCm/rocm-opencl, NVIDIA)\nclinfo -l     # doit lister le GPU\n"
                "# compilation manuelle (facultative) :\ngcc -O2 -pthread third_party/benchsuite_oclburn/oclburn.c -o ~/.benchsuite/tools/oclburn/oclburn -ldl"}],
       [("Source", "third_party/benchsuite_oclburn/oclburn.c")],
       "Aucun en-tête OpenCL nécessaire (bibliothèque chargée dynamiquement). Détecte les erreurs de calcul (entiers exacts comparés à une référence CPU et GPU) "
       "et de mémoire (motifs écrits puis relus). La puissance maximale dépend du GPU : BenchSuite affiche la puissance atteinte en % de la limite de la carte.",
       apt=["gcc", "ocl-icd-libopencl1", "clinfo"]),
    _t("vulkaninfo", "compute", "vulkan-tools + chargeur Vulkan", "Liste les GPU Vulkan.", OPTIONNEL, ["Compute Vulkan"], {"bins": ["vulkaninfo"]},
       [{"label": _APT, "cmd": "sudo apt install -y vulkan-tools libvulkan1"}], [("Vulkan-Tools", "https://github.com/KhronosGroup/Vulkan-Tools")],
       "Serveur NVIDIA : ajouter libnvidia-gl-<version du pilote>.", apt=["vulkan-tools", "libvulkan1"]),
    _t("vkpeak", "compute", "vkpeak", "Calcul (FP32 / FP16 / INT) via Vulkan.", OPTIONNEL, ["Compute Vulkan"],
       {"bins": ["vkpeak"]},
       [{"label": "Binaire Linux de la page des versions (sans droits root)",
         "cmd": "# télécharger l'archive Linux, l'extraire, puis :\nmkdir -p ~/.local/bin && mv vkpeak ~/.local/bin/ && chmod +x ~/.local/bin/vkpeak"}],
       [("Projet / versions", "https://github.com/nihui/vkpeak/releases")]),
]


def _expand(path: str) -> List[str]:
    return glob.glob(os.path.expanduser(path))


def detect(tool: Dict) -> Tuple[bool, str, str]:
    """(présent, chemin ou détail, version) d'un outil du catalogue. Lecture seule, jamais d'exception."""
    check = tool.get("check", {})
    try:
        if "py" in check:
            mod = check["py"]
            if mod == "gi":
                import gi
                gi.require_version("Gtk", "4.0")
                gi.require_version("Adw", "1")
                from gi.repository import Gtk, Adw  # noqa: F401
                return True, "PyGObject, GTK 4, Libadwaita", ""
            if mod == "gi_cairo":
                import gi
                gi.require_foreign("cairo")
                return True, "python3-gi-cairo", ""
            spec = importlib.util.find_spec(mod)
            return spec is not None, (spec.origin or "") if spec else "", ""
        if "bins" in check:
            from core.install_advisor import _which, _version
            for b in check["bins"]:
                p = _which(b)
                if p:
                    return True, p, _version(b, p)
        for pat in check.get("glob", []):
            m = [x for x in _expand(pat) if os.path.exists(x)]
            if m:
                return True, m[0], ""
    except Exception:
        pass
    return False, "", ""


def apt_command_for_missing(missing_ids: List[str]) -> str:
    """Commande apt unique pour les paquets simples des outils manquants (sans mise à jour des paquets présents)."""
    pk: List[str] = []
    for t in TOOLS:
        if t["id"] in missing_ids:
            for p in t.get("apt", []):
                if p not in pk:
                    pk.append(p)
    return ("sudo apt install -y --no-upgrade " + " ".join(pk)) if pk else ""


# Groupes de GPU conditionnés par la présence d'un GPU de la marque : identifiant de groupe → (identifiant PCI constructeur, nom)
GROUP_VENDOR = {"nvidia": ("10de", "NVIDIA"), "intel": ("8086", "Intel"), "amd": ("1002", "AMD")}


def detect_gpu_vendors() -> set:
    """Constructeurs de GPU présents sur la machine (identifiants PCI 10de / 8086 / 1002) d'après lspci ; vide si lspci est absent.
    Sert à marquer « non concerné » les outils d'une marque absente (sans les compter comme manquants)."""
    found = set()
    try:
        from core.install_advisor import _run
        rc, out = _run(["lspci", "-nn"], 8)
        import re
        for line in out.splitlines():
            if re.search(r"\[03(00|02|80)\]|\[1200\]", line):
                for vid in ("10de", "8086", "1002"):
                    if f"[{vid}:" in line.lower():
                        found.add(vid)
    except Exception:
        pass
    return found


def to_markdown() -> str:
    """Document de référence généré depuis le catalogue (documentation/OUTILS_ET_DEPENDANCES.md) : même contenu que la page de l'interface."""
    out = ["# Outils et dépendances", "",
           "BenchSuite **n'installe jamais rien** : ce document (généré depuis `core/tools_catalog.py`) liste ce qu'il faut, à quoi cela sert, "
           "les commandes à copier et les liens officiels. Pour des commandes adaptées à votre machine : `./cli.py --install-guide`. "
           "Dans l'interface : menu « Outils d'installation ».", ""]
    for gid, title, desc in GROUPS:
        out += [f"## {title}", "", desc, ""]
        for t in [x for x in TOOLS if x["group"] == gid]:
            out += [f"### {t['name']} — *{t['need']}*", "", t["role"], ""]
            if t["used_by"]:
                out += ["Utilisé par : " + ", ".join(t["used_by"]), ""]
            for m in t["install"]:
                out += [f"**{m['label']}**", "", "```bash", m["cmd"], "```", ""]
            if t["notes"]:
                out += [t["notes"], ""]
            if t["links"]:
                out += ["Liens : " + " · ".join(f"[{l}]({u})" for l, u in t["links"]), ""]
    return "\n".join(out)
