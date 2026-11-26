# 📖 Guide d'Utilisation de BenchSuite

Ce document explique l'installation, les dépendances et l'utilisation quotidienne de BenchSuite en environnement de laboratoire ou serveur.

---

## 1. Prérequis & Installation

### Système d'exploitation supporté
- **OS principal : Ubuntu 22.04 / 24.04 LTS** — cible prioritaire de BenchSuite.
- **CachyOS / Arch Linux** et **Debian 12** : supportés en best-effort (noyaux et pilotes récents sur Arch).

### Priorité des tests
BenchSuite privilégie d'abord **la chauffe, la stabilité et les erreurs matérielles** (températures, throttling, puissance, charge longue, Xid / MCE / AER / ECC / NVMe / OOM). Les tests de **performance** (débits, bandes passantes, scores) viennent en second temps, à la demande.

### Installation des dépendances sous Arch Linux / CachyOS
```fish
# Dépendances système et bibliothèques graphiques
sudo pacman -S python python-gobject gtk4 libadwaita cairo pciutils ethtool smartmontools dmidecode

# Outils de benchmark
sudo pacman -S stress-ng fio iperf3

# Pilotes et outils GPU
sudo pacman -S nvidia-utils
yay -S lact-bin gpu-burn-git clpeak-git vkpeak-git
```

### Vérification de l'environnement
Exécutez le script d'audit environnemental inclus dans le projet :
```fish
./check_environment.py
```
Le script liste les outils détectés et affiche un statut vert si le système est prêt.

---

## 2. Lancement de l'Application

### Mode Interface Graphique (GTK4 + Libadwaita)
Le lancement s'effectue via le script lanceur :
```fish
./benchsuite.sh
```
Ou directement en Python :
```fish
python3 main.py
```

### Fonctionnalités de l'interface graphique :
- **Menu latéral, rangé par composant :** *Accueil*, *Séquence automatique*, puis un groupe par composant (`CPU + RAM`, `Stockage`, `GPU`, `Réseau`) contenant ses tests — chauffe / stabilité / erreurs d'abord, performance ensuite — et, sous un trait, ses pages de configuration ou de diagnostic (Configuration CPU, Diagnostic SSD, Configuration NVIDIA / GPU Intel, Serveur iperf3). Puis *Diagnostic* (dmesg, infos système), *Rapports* et *Outils & dépendances*. Le compteur `2/9` d'un groupe = tests prêts / tests du groupe ; un ⚠ signale un outil manquant (info-bulle). Les groupes repliés restent repliés au prochain lancement. **Ctrl+F** : rechercher dans le menu ; **F9** : afficher / masquer le menu.
- **Configuration NVIDIA :** colonne « Actuel (GPU) » relue en direct à côté de chaque réglage (orange = pas encore appliqué) ; seuls les réglages modifiés sont envoyés, puis chaque valeur est relue sur le GPU — c'est elle qui fait foi, pas le code retour de `nvidia-smi`.
- **Page Accueil & Changelog :** Vue d'ensemble des dernières améliorations de version et des objectifs de qualification.
- **Page Informations Système :** Inventaire matériel en direct (CPU, RAM, Disques, Cartes graphiques, MCIO, Réseau).
- **Panneau de Contrôle & Paramètres :** Chaque test dispose de ses propres réglages (durées, threads, filtres disques, sélection GPU, taquets de charge).
- **Actions rapides :** Boutons `▶ Lancer`, `⏹ Stopper` et `💾 Exporter` épinglés pour une réactivité immédiate sans défilement.

---

## 3. Mode Ligne de Commande (CLI Headless)

Pour les serveurs sans environnement graphique ou les chaînes d'intégration continue (CI/CD) :

```fish
# Afficher l'aide et les benchmarks disponibles
./cli.py --help
./cli.py --list

# Exécuter une suite de tests spécifique
./cli.py --run gpu_burn,cpu_stress,storage_fio_nvme

# Exporter automatiquement les résultats en HTML, PDF, JSON et CSV
./cli.py --run gpu_burn --export-all --out-dir ~/BenchmarkResults
```

---

## Installer NVIDIA DCGM (diagnostic matériel GPU)

DCGM (Data Center GPU Manager, Apache-2.0) fournit `dcgmi diag`, utilisé par le test « GPU — Diagnostic DCGM ».
Il cible les GPU NVIDIA **datacenter / serveur** (RTX PRO 6000 Server, H100, L40S, A100…).

```bash
# 1. Dépôt NVIDIA (Ubuntu 24.04 x86_64 ; adapter ubuntu2204 / sbsa pour 22.04 ou ARM)
wget https://developer.download.nvidia.com/compute/cuda/repos/ubuntu2404/x86_64/cuda-keyring_1.1-1_all.deb
sudo dpkg -i cuda-keyring_1.1-1_all.deb && sudo apt-get update

# 2. Choisir le paquet selon la version CUDA affichée par nvidia-smi (en haut à droite)
apt-cache search datacenter-gpu-manager          # liste les noms exacts disponibles
sudo apt-get install -y datacenter-gpu-manager-4-cuda13   # driver/CUDA 13.x (ex. driver 595)
#   ou : datacenter-gpu-manager-4-cuda12                   # driver/CUDA 12.x

# 3. Démarrer le service et vérifier
sudo systemctl --now enable nvidia-dcgm
dcgmi discovery -l                                # doit lister vos GPU
sudo dcgmi diag -r 1                              # test rapide de validation
```
Les noms de paquets changent selon les versions : se fier à `apt-cache search`. Référence : https://docs.nvidia.com/datacenter/dcgm/latest/user-guide/getting-started.html

BenchSuite tente de démarrer le service (`systemctl start nvidia-dcgm`, sinon `nv-hostengine`) s'il ne répond pas.
Niveaux : 1 ≈ secondes · 2 ≈ 2 min · 3 ≈ 15 min (mémoire GPU, calcul, PCIe, bande passante, stress) · 4 plus long.

## Collecte d'informations lente ou qui ne se termine pas
**Fonctionnement (depuis la 1.1.112) :** chaque section (OS, CPU, RAM, BMC, GPU, stockage, réseau, PCIe, PMBus I²C, télémétrie) s'exécute en parallèle avec son budget de temps (30 s par défaut ; 25 s PMBus, 40 à 45 s BMC / télémétrie / GPU / PCIe, 60 s stockage). Une section qui dépasse son budget est abandonnée, listée dans une alerte « Collecte » et dans `sections_incompletes` du JSON ; `durees_s` donne le temps de chacune. La page *Infos système* affiche les sections en cours.

**Causes habituelles d'une collecte très lente (par ordre de probabilité) :**
1. **Balayage I²C (PMBus)** : 112 adresses lues sur chaque bus ; très long avec de nombreux bus (serveur, GPU, BMC) ou un bus qui étire l'horloge. Borné à 20 s.
2. **BMC / IPMI muet** : plusieurs appels `ipmitool` de 8 à 15 s chacun, tous en attente. Vérifier `time timeout 20 ipmitool mc info`.
3. **`nvidia-smi` bloqué** (GPU tombé du bus, pilote en erreur) : `time timeout 20 nvidia-smi -L`.
4. **Disque ou montage réseau bloqué** (processus en état `D`, NFS / CIFS injoignable) : un tel processus ne peut pas être tué, même par un délai. Voir les commandes ci-dessous.
5. **Redfish** : adresse de BMC injoignable, plusieurs requêtes de 3 à 4 s.
6. **Résolution DNS** du nom de la machine (retirée en 1.1.109).

**Trouver le coupable sur la machine concernée** (depuis le dossier du logiciel). Ce script affiche le temps de chaque section et, toutes les 20 s, la ligne exacte où le programme est bloqué :
```bash
python3 - <<'PYEND'
import faulthandler, time
faulthandler.dump_traceback_later(20, repeat=True)
from core import system_info as si
for name in ("get_os_info", "get_cpu_info", "get_ram_info", "get_motherboard_info", "get_bmc_info", "get_gpu_info",
             "get_storage_info", "get_network_info", "get_pcie_devices", "get_pmbus_info"):
    t = time.time(); print(f"> {name} …", flush=True)
    getattr(si, name)()
    print(f"  {name} : {time.time() - t:.1f} s", flush=True)
PYEND
ls /sys/class/i2c-adapter | wc -l                       # nombre de bus I2C
ps -eo pid,stat,wchan:20,cmd | awk '$2 ~ /^D/'          # processus bloqués dans le noyau
grep -E ' (nfs|nfs4|cifs|fuse\.[a-z]+) ' /proc/mounts   # montages réseau
```

## Réseau iperf3 entre deux machines
1. **Machine serveur** : menu *Réseau → Serveur iperf3 (2 PC)* → **▶ Démarrer le serveur** ; noter l'adresse affichée.
2. **Machine client** : menu *Réseau → Réseau iperf3 TCP / UDP* → champ « Serveur iperf3 » = cette adresse (pas `127.0.0.1`, qui désigne la machine elle-même).
3. **Utiliser un port précis (carte réseau, port de la carte mère)** : le panneau « Ports Ethernet de cette machine » liste tous les ports physiques, même sans adresse IP. Relier deux ports par un câble. Dans le panneau, les deux seules valeurs qui changent d'un PC à l'autre sont **« Réseau de test »** (identique des deux côtés, ex. `192.168.100`) et **« N° de CE PC »** (`1` sur un PC, `2` sur l'autre) : les commandes à copier se mettent à jour. Copier « Donner une adresse (temporaire) » sur chaque machine, `ping` pour vérifier, **↻ Actualiser**, puis utiliser cette adresse comme serveur. Le choix « Port d'envoi » du test client force le port de cette machine (`iperf3 -B`).
- Les adresses données sont **temporaires** (perdues au redémarrage) et BenchSuite ne modifie jamais la configuration réseau : il affiche seulement les commandes à copier.
- Pare-feu du serveur : `sudo ufw allow 5201` (Ubuntu).
- **Test local détecté :** si l'adresse visée est une adresse de **cette** machine (ou si l'adresse locale vue par iperf3 est la même que l'adresse distante), le trafic passe par l'interface interne et non par le câble : le score s'affiche en jaune avec un avertissement, et le verdict passe à « À vérifier ». Lancez le client sur l'autre machine.

