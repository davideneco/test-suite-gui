# Outils et dépendances

BenchSuite **n'installe jamais rien** : ce document (généré depuis `core/tools_catalog.py`) liste ce qu'il faut, à quoi cela sert, les commandes à copier et les liens officiels. Pour des commandes adaptées à votre machine : `./cli.py --install-guide`. Dans l'interface : menu « Outils d'installation ».

## Interface (obligatoire)

Sans eux le logiciel ne démarre pas.

### GTK 4 + Libadwaita (PyGObject) — *requis*

Fenêtre et widgets de l'interface graphique.

Utilisé par : Interface

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0
```

Liens : [GTK](https://www.gtk.org/) · [Libadwaita](https://gnome.pages.gitlab.gnome.org/libadwaita/) · [PyGObject](https://pygobject.gnome.org/)

### Pont GTK ↔ cairo (python3-gi-cairo) — *recommandé*

Dessin des graphiques. Sans lui, les graphiques sont désactivés.

Utilisé par : Graphiques temps réel, Séquence automatique

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y python3-gi-cairo python3-cairo
```

Liens : [pycairo](https://pycairo.readthedocs.io/)

### psutil — *requis*

Lecture de l'utilisation CPU / RAM et des processus.

Utilisé par : Télémétrie

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y python3-psutil
```

**Sans droits root (pip)**

```bash
python3 -m pip install --user psutil
```

Liens : [psutil](https://github.com/giampaolo/psutil)

### ReportLab — *requis*

Génération des rapports PDF.

Utilisé par : Exports PDF

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y python3-reportlab
```

**Sans droits root (pip)**

```bash
python3 -m pip install --user reportlab
```

Liens : [ReportLab](https://www.reportlab.com/opensource/)

## Mesure du système

Lecture des capteurs, des disques, de la RAM, du PCIe et du BMC. Un outil absent masque seulement la mesure concernée.

### pciutils (lspci) — *recommandé*

Liste des périphériques PCIe, liens et GPU.

Utilisé par : Infos système, Inventaire, GPU

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y pciutils
```

Liens : [pciutils](https://mj.ucw.cz/sw/pciutils/)

### dmidecode — *recommandé*

Carte mère, BIOS, barrettes de RAM (nécessite root). Le nombre d'emplacements RAM est le nombre de « Memory Device » (type 17) listés, vides compris — pas le champ « Number Of Devices » des « Physical Memory Array », que certains firmwares remplissent avec une valeur générique (ex. 16 pour 6 supports réels). Le canal mémoire n'est pas fourni par le SMBIOS : BenchSuite n'évalue donc pas le remplissage par canal.

Utilisé par : Infos système, Traçabilité (n° de série)

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y dmidecode
```

Liens : [dmidecode](https://www.nongnu.org/dmidecode/)

### lm-sensors — *recommandé*

Températures du processeur et de la carte mère.

Utilisé par : Télémétrie CPU

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y lm-sensors
```

**Détection des capteurs (optionnel, écrit dans /etc)**

```bash
sudo sensors-detect --auto
```

Liens : [lm-sensors](https://github.com/lm-sensors/lm-sensors)

### ipmitool — *recommandé*

Capteurs, journal d'événements (SEL) et informations du BMC.

Utilisé par : Télémétrie serveur, Inventaire

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y ipmitool
```

Nécessite un BMC (serveur) ; le chargement des modules IPMI se fait au lancement de BenchSuite.

Liens : [ipmitool](https://github.com/ipmitool/ipmitool)

### smartmontools (smartctl) — *recommandé*

Santé SMART des disques SATA / SAS.

Utilisé par : Diagnostic SSD / stockage

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y smartmontools
```

Liens : [smartmontools](https://www.smartmontools.org/)

### nvme-cli — *recommandé*

Santé et journal SMART des SSD NVMe (erreurs, throttling, usure).

Utilisé par : Diagnostic SSD / stockage, Profils fio Stabilité et Chauffe

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y nvme-cli
```

Liens : [nvme-cli](https://github.com/linux-nvme/nvme-cli)

### ethtool — *recommandé*

Vitesse, pilote et firmware des cartes réseau.

Utilisé par : Infos système, Réseau

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y ethtool
```

Liens : [ethtool](https://mirrors.edge.kernel.org/pub/software/network/ethtool/)

### powerstat — *optionnel*

Puissance du système en temps réel (si ni RAPL ni capteur direct).

Utilisé par : Télémétrie puissance

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y powerstat
```

Liens : [powerstat](https://github.com/ColinIanKing/powerstat)

## Moteurs de test (CPU, RAM, stockage, réseau)

Ces outils font réellement la charge ; le test correspondant est désactivé s'ils manquent.

### stress-ng — *requis*

Charge CPU et RAM (profils 5 min, 30 min, 4 h, personnalisés).

Utilisé par : CPU + RAM

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y stress-ng
```

Liens : [Projet](https://github.com/ColinIanKing/stress-ng) · [Page de manuel](https://manpages.ubuntu.com/manpages/noble/man1/stress-ng.1.html)

### fio — *requis*

Tests de stockage : profils NVMe / SATA / HDD, JEDEC, SNIA, OCP, stabilité, chauffe.

Utilisé par : Stockage FIO NVMe, Stockage FIO HDD/SATA, Séquence automatique

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y fio
```

fio 3.20 ou plus récent recommandé ; io_uring demande un noyau récent.

Liens : [Projet](https://github.com/axboe/fio) · [Documentation](https://fio.readthedocs.io/)

### iperf3 — *recommandé*

Débit réseau TCP / UDP (jitter, pertes).

Utilisé par : Réseau

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y iperf3
```

Liens : [Projet](https://github.com/esnet/iperf) · [Site](https://iperf.fr/)

### gcc (compilateur C) — *recommandé*

Compilation automatique de STREAM (bande passante RAM).

Utilisé par : RAM — Bande passante (STREAM)

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y build-essential
```

Liens : [STREAM](https://www.cs.virginia.edu/stream/)

## Compilation (pour gpu-burn, nvbandwidth…)

Seulement si vous compilez vous-même certains outils GPU.

### git — *optionnel*

Récupérer les sources de gpu-burn, nvbandwidth…

Utilisé par : Compilation d'outils GPU

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y git
```

Liens : [git](https://git-scm.com/)

### make — *optionnel*

Compiler gpu-burn.

Utilisé par : gpu-burn

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y build-essential
```

Liens : [GNU make](https://www.gnu.org/software/make/)

### cmake — *optionnel*

Compiler nvbandwidth.

Utilisé par : nvbandwidth

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y cmake libboost-program-options-dev
```

Liens : [CMake](https://cmake.org/)

### Rust (cargo) — *optionnel*

Installer gpu-fryer.

Utilisé par : GPU Compute fryer

**Sans droits root (rustup, installe dans ~/.cargo)**

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh
source ~/.cargo/env
```

Liens : [rustup](https://rustup.rs/)

## GPU NVIDIA

Le pilote NVIDIA n'est jamais installé ni modifié par BenchSuite.

### Pilote NVIDIA (nvidia-smi) — *recommandé*

Température, puissance, fréquences et état des GPU NVIDIA.

Utilisé par : Tous les tests GPU NVIDIA

**Ubuntu / Debian (apt) — pilote recommandé pour votre carte**

```bash
ubuntu-drivers devices          # liste les pilotes
sudo ubuntu-drivers install     # installe le recommandé
```

BenchSuite ne touche jamais au pilote. Si nvidia-smi répond « API mismatch », le module noyau et les bibliothèques n'ont pas la même version : sudo dkms autoinstall && sudo reboot.

Liens : [Téléchargement des pilotes](https://www.nvidia.com/Download/index.aspx)

### Toolkit CUDA (nvcc) — *optionnel*

Compiler gpu-burn / nvbandwidth. Blackwell (RTX PRO 6000, 50xx) : CUDA 12.8 ou plus.

Utilisé par : Compilation de gpu-burn / nvbandwidth

**Ubuntu / Debian (apt) — dépôt NVIDIA puis toolkit seul**

```bash
distro=ubuntu$(. /etc/os-release; echo ${VERSION_ID//./})   # ex. ubuntu2204 ou ubuntu2404
wget https://developer.download.nvidia.com/compute/cuda/repos/$distro/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt update
apt-cache search '^cuda-toolkit-[0-9]+-[0-9]+$'   # versions disponibles
sudo apt install -y cuda-toolkit-<X>-<Y>
```

⚠ Installez uniquement « cuda-toolkit-X-Y ». Les méta-paquets « cuda », « cuda-X-Y » et « cuda-drivers » remplacent le pilote. La version du toolkit doit rester ≤ au CUDA maximal affiché par nvidia-smi.

Liens : [Téléchargements CUDA](https://developer.nvidia.com/cuda-downloads) · [Compatibilité pilote ↔ CUDA](https://docs.nvidia.com/deploy/cuda-compatibility/)

### gpu-burn — *optionnel*

Stress CUDA (GFLOPS + erreurs mémoire) et intégrité VRAM.

Utilisé par : GPU gpu-burn, GPU — Intégrité VRAM

**Compilation sans droits root (dans votre dossier)**

```bash
mkdir -p ~/.benchsuite/tools && cd ~/.benchsuite/tools
git clone --depth 1 https://github.com/wilicc/gpu-burn && cd gpu-burn
nvidia-smi --query-gpu=compute_cap --format=csv,noheader     # 7.5 → COMPUTE=75, 8.6 → 86, 8.9 → 89, 12.0 → 120
make COMPUTE=<arch> CUDAPATH=/usr/local/cuda-<X.Y>
ldd ./gpu_burn | grep 'not found'     # ne doit rien afficher
ls compare.*                          # compare.ptx ou compare.fatbin
```

Le binaire gpu_burn et son fichier compare.* doivent venir du même « make ». Éviter la version snap (confinée : elle ne voit pas le pilote). Erreur « libcublas.so.NN introuvable » : le toolkit CUDA utilisé à la compilation n'est pas celui installé.

Liens : [Projet](https://github.com/wilicc/gpu-burn)

### gpu-fryer — *optionnel*

Stress de calcul GPU multi-GPU (TFLOPS).

Utilisé par : GPU Compute fryer

**Via cargo (installe dans ~/.cargo/bin)**

```bash
cargo install gpu-fryer
```

NVIDIA uniquement (confirmé à l'usage sur une machine Intel Arc : ne fonctionne pas). Pour Intel / AMD : vkpeak / clpeak en mode Chauffe.

Liens : [Projet](https://github.com/huggingface/gpu-fryer)

### nvbandwidth — *optionnel*

Bande passante CPU ↔ GPU et GPU ↔ GPU.

Utilisé par : GPU nvbandwidth, GPU — Bande passante VRAM

**Compilation sans droits root (nécessite le toolkit CUDA, cmake, Boost)**

```bash
sudo apt install -y cmake libboost-program-options-dev git
mkdir -p ~/.benchsuite/tools && cd ~/.benchsuite/tools
git clone --depth 1 https://github.com/NVIDIA/nvbandwidth && cd nvbandwidth
cmake -S . -B build && cmake --build build -j$(nproc)
```

Liens : [Projet](https://github.com/NVIDIA/nvbandwidth)

### DCGM (dcgmi diag) — *optionnel*

Diagnostic matériel NVIDIA : mémoire, calcul, PCIe, bande passante, stress.

Utilisé par : GPU — Diagnostic DCGM

**Ubuntu / Debian (apt) — dépôt NVIDIA puis paquet correspondant au CUDA du pilote**

```bash
distro=ubuntu$(. /etc/os-release; echo ${VERSION_ID//./})   # ex. ubuntu2204 ou ubuntu2404
wget https://developer.download.nvidia.com/compute/cuda/repos/$distro/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt update
sudo apt install -y datacenter-gpu-manager-4-cuda<N>     # N = CUDA majeur affiché par nvidia-smi (12 ou 13)
sudo systemctl enable --now nvidia-dcgm
```

⚠ Pas le snap « dcgm » : confiné, il ne voit aucun GPU (à désinstaller à la main s'il est présent).

Liens : [Page DCGM](https://developer.nvidia.com/dcgm) · [Documentation](https://docs.nvidia.com/datacenter/dcgm/latest/)

## GPU Intel (Arc, Data Center GPU)

xpu-smi donne températures, puissance, fréquence et utilisation ; le runtime OpenCL permet les tests de calcul.

### xpu-smi (Intel XPU Manager) — *recommandé*

Nom, mémoire, températures cœur / mémoire, puissance, fréquence, utilisation des GPU Intel.

Utilisé par : Télémétrie GPU Intel

**Paquet du dépôt APT Intel GPU (ajouter d'abord le dépôt de votre distribution, voir le lien)**

```bash
sudo apt install -y xpu-smi
```

BenchSuite l'appelle en « sudo -n » puis en direct : il faut root ou une règle sudo sans mot de passe pour xpu-smi.

Liens : [Documentation Intel GPU (dépôts, pilotes)](https://dgpu-docs.intel.com/) · [Projet XPU Manager](https://github.com/intel/xpumanager)

### Runtime OpenCL Intel (compute-runtime) — *recommandé*

Permet à clpeak et clinfo de voir le GPU Intel.

Utilisé par : Compute OpenCL

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y intel-opencl-icd
```

Une carte récente (Arc Pro B-series / Battlemage) demande un runtime récent (version 26.18 ou plus d'après un retour d'utilisateur) : utilisez le dépôt Intel GPU plutôt que celui de la distribution si le GPU n'est pas vu.

Liens : [Projet](https://github.com/intel/compute-runtime) · [Documentation Intel GPU](https://dgpu-docs.intel.com/)

## GPU AMD

Optionnel : températures hotspot / VRAM.

### LACT — *optionnel*

Températures hotspot et VRAM des GPU AMD (pas exposées par le noyau).

Utilisé par : Télémétrie GPU AMD

**Paquet .deb de la page des versions**

```bash
# télécharger le .deb correspondant à votre distribution, puis :
sudo apt install ./lact-*.deb
```

Liens : [Projet / versions](https://github.com/ilya-zlobintsev/LACT/releases)

## Calcul OpenCL / Vulkan (toutes marques)

Tests de calcul et de bande passante sur n'importe quel GPU.

### clinfo + chargeur OpenCL — *recommandé*

Liste les plateformes et GPU OpenCL (sert à choisir -p N).

Utilisé par : Compute OpenCL

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y clinfo ocl-icd-libopencl1
```

« clinfo -l » indique le numéro de plateforme du GPU. Un pilote OpenCL d'une autre marque peut rester listé en plateforme 0 sans gêner BenchSuite.

Liens : [clinfo](https://github.com/Oblomov/clinfo)

### clpeak — *optionnel*

Calcul (SP / HP / DP / entiers) et bande passante mémoire / transferts via OpenCL.

Utilisé par : Compute OpenCL

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y clpeak
```

**Alternative (snap)**

```bash
sudo snap install clpeak --classic
```

Sur une machine avec plusieurs plateformes OpenCL, lancez « clpeak -p N -d 0 » (BenchSuite le fait tout seul).

Liens : [Projet](https://github.com/krrishnarraj/clpeak)

### oclburn (burn OpenCL vérifié, fourni avec BenchSuite) — *optionnel*

Burn à 100 % de tout GPU OpenCL (Intel, AMD, NVIDIA) : calcul entier / FP32 / FP16 et VRAM en continu, avec vérification des résultats.

Utilisé par : GPU — Burn OpenCL vérifié

**Rien à installer : BenchSuite compile sa propre source au premier lancement (gcc + pilote OpenCL requis)**

```bash
sudo apt install -y gcc ocl-icd-libopencl1 clinfo     # + pilote OpenCL du GPU (Intel compute-runtime, ROCm/rocm-opencl, NVIDIA)
clinfo -l     # doit lister le GPU
# compilation manuelle (facultative) :
gcc -O2 -pthread third_party/benchsuite_oclburn/oclburn.c -o ~/.benchsuite/tools/oclburn/oclburn -ldl
```

Aucun en-tête OpenCL nécessaire (bibliothèque chargée dynamiquement). Détecte les erreurs de calcul (entiers exacts comparés à une référence CPU et GPU) et de mémoire (motifs écrits puis relus). La puissance maximale dépend du GPU : BenchSuite affiche la puissance atteinte en % de la limite de la carte.

Liens : [Source](third_party/benchsuite_oclburn/oclburn.c)

### vulkan-tools + chargeur Vulkan — *optionnel*

Liste les GPU Vulkan.

Utilisé par : Compute Vulkan

**Ubuntu / Debian (apt)**

```bash
sudo apt install -y vulkan-tools libvulkan1
```

Serveur NVIDIA : ajouter libnvidia-gl-<version du pilote>.

Liens : [Vulkan-Tools](https://github.com/KhronosGroup/Vulkan-Tools)

### vkpeak — *optionnel*

Calcul (FP32 / FP16 / INT) via Vulkan.

Utilisé par : Compute Vulkan

**Binaire Linux de la page des versions (sans droits root)**

```bash
# télécharger l'archive Linux, l'extraire, puis :
mkdir -p ~/.local/bin && mv vkpeak ~/.local/bin/ && chmod +x ~/.local/bin/vkpeak
```

Liens : [Projet / versions](https://github.com/nihui/vkpeak/releases)
