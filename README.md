# BenchSuite

**Suite de bench et de recette matérielle pour serveurs et stations Linux — 100 % logiciels libres, utilisable à la chaîne.**

BenchSuite regroupe, dans une interface GTK4/Libadwaita et en ligne de commande, les meilleurs outils open source de test (stress-ng, fio, gpu-burn, gpu-fryer, nvbandwidth, DCGM, clpeak, vkpeak, STREAM, iperf3…). Il lance les tests, surveille capteurs et pannes matérielles pendant l'exécution, rend un **verdict automatique** et produit des rapports exploitables (HTML, PDF, JSON, CSV).

Version actuelle : **1.0.24**

---

## Pour qui, pour quoi

- **Recette / qualification** d'une machine neuve ou réparée : une séquence de tests, un rapport, un verdict.
- **Banc à la chaîne** : traçabilité client / n° de commande / n° de série, reprise après crash, synthèse d'un lot de machines.
- **Diagnostic** : charge CPU/RAM/GPU/disque avec télémétrie (température, puissance, fréquences, throttling) et détection des erreurs matérielles.

Cible et priorités : **Linux Ubuntu** (et dérivés Debian) en OS principal ; tests **chauffe / stabilité / erreurs** en priorité, **performance** en second temps, à la demande.

Principes : compatibilité universelle (Intel, AMD, NVIDIA, ARM ; NVMe, SATA, HDD), **best-effort** (jamais de crash si un outil ou les droits root manquent : la section est masquée ou un repli est utilisé) et interface dynamique (seules les métriques détectées sont affichées).

---

## Fonctions principales

| Zone | Contenu |
|---|---|
| **CPU** | stress-ng en grille de profils : tous les threads / cœurs physiques × 5 min · 30 min · 4 h, single-core, AVX, profil personnalisé et CLI |
| **RAM** | burn stress-ng (5 min · 30 min · 4 h), profil mixte CPU + RAM, bande passante **STREAM**, test mémoire logiciel |
| **Stockage** | fio : profils NVMe/HDD, suites CrystalDiskMark, profils « pro » (JEDEC JESD219, SNIA PTS, IOMeter, OLTP, journal etcd, zones HDD, lecture complète), SMART/NVMe |
| **GPU** | **burn OpenCL vérifié** (`oclburn`, Intel / AMD / NVIDIA : calcul + VRAM à 100 %, erreurs détectées) ; GPU Intel via **xpu-smi** (nom, mémoire, températures cœur/mémoire, puissance, fréquence, utilisation) ; gpu-burn, gpu-fryer, **DCGM** (`dcgmi diag`), nvbandwidth, bande passante et intégrité **VRAM**, clpeak (OpenCL), vkpeak (Vulkan), multi-GPU, LACT (hotspot/VRAM) |
| **Électronique (relevé)** | pendant chaque test : tensions, courants, puissances, ventilateurs, températures de carte (BMC, capteurs carte, PMBus, RAPL) collectés et conservés dans le JSON ; **non affichés** dans les rapports HTML / PDF / CSV (drapeau `EXPORT_SEQUENCE_ELECTRONICS` dans `exporters/export.py`) |
| **Réseau** | iperf3 TCP/UDP (débit, jitter, pertes) |
| **Séquence automatique** | onglet pour composer des blocs de tests enchaînés ou **en parallèle** (ex. burn CPU + gpu-fryer + profil SSD en même temps), durée par test, modèles, enregistrement, courbes **température / puissance** en direct et dans les rapports |
| **Verdict** | CONFORME / À VÉRIFIER / NON CONFORME ; règles universelles (écart entre GPU, throttling, ΔT, pannes), critères modifiables, comparaison à une machine de référence ; limite inconnue → « non évalué », jamais un échec |
| **Sécurité** | protection du disque système (aucune écriture brute sur l'OS ni sur un disque monté), vérifications avant test (préflight) |
| **Pannes matérielles** | surveillance en direct des Xid NVIDIA, MCE, AER, ECC, NVMe, OOM : arrêt immédiat du test et alerte dans le rapport |
| **Inventaire avant/après** | à chaque test et chaque campagne : périphériques PCI, liens (largeur/vitesse), erreurs AER/ECC, réseau, journal BMC (SEL) ; toute dérive entre dans le verdict |
| **Traçabilité** | client, commande, n° de série (détecté via DMI), rangement `~/BenchmarkResults/<client>/<série>/…` |
| **Exports** | HTML (graphiques Chart.js), PDF (ReportLab), JSON, CSV ; synthèse de lot (`--fleet`) |

---

## Installation

Ubuntu / Debian :

```bash
git clone https://github.com/davideneco/test-suite-gui.git
cd test-suite-gui
chmod +x install.sh benchsuite.sh check_environment.py cli.py
./install.sh            # n'installe QUE ce qui manque (ne touche pas à l'existant) ; --dry-run pour simuler
./check_environment.py  # vérifie ce qui est présent / manquant
./benchsuite.sh         # interface graphique
```

**Téléchargement avec la version dans le nom du fichier :** le bouton « Download ZIP » d'une branche donne toujours le même nom (`test-suite-gui-<branche>.zip`). Chaque version publiée porte aussi un tag `v<version>` ; son archive s'appelle `test-suite-gui-<version>.zip` :

```
https://github.com/davideneco/test-suite-gui/archive/refs/tags/v1.1.82.zip     # remplacer par la version voulue
```

(ou page *Tags* du dépôt → « zip »). Les mainteneurs créent le tag avec `./tag_release.sh` après le push de la version.

Outils optionnels non disponibles en paquet (installation guidée dans l'interface et dans `documentation/GUIDE_UTILISATION.md`) : gpu-burn, gpu-fryer, nvbandwidth, clpeak, vkpeak, DCGM (`datacenter-gpu-manager-4-cudaNN`, **pas le snap**).

`install.sh` est relançable sans risque : il ne réinstalle, ne met à jour ni n'écrase rien de ce qui fonctionne (paquets, pilote NVIDIA, snap, gpu-burn, configuration IPMI). Options : `--dry-run` (simulation), `--no-gpu`, `--no-build`. Les points à traiter à la main sont listés à la fin.

Les outils absents désactivent simplement le test concerné et affichent la commande d'installation.

## Mode ligne de commande

```bash
./cli.py                                            # écran interactif plein écran (comme la version graphique)
./cli.py cpu 5m   |   ./cli.py fio perf <dossier-de-données>   |   ./cli.py gpu oclburn 10m   |   ./cli.py net serveur
./cli.py infos   |   ./cli.py liste   |   ./cli.py dernier   # commandes courtes (détails : documentation/CLI.md)
./cli.py --suites                                   # séquences disponibles
./cli.py --profiles                                 # profils stress-ng / fio
./cli.py --suite recette_gpu --client ACME --order CMD-1234
./cli.py --resume ~/BenchmarkResults/ACME/SN123/<campagne>   # reprise après crash
./cli.py -b gpu_fryer -p gpu_fryer.time_s=120       # un seul test
./cli.py --fleet ~/BenchmarkResults                 # synthèse d'un lot de machines
./cli.py --mcio-scan                                # câblage MCIO des GPU
```

Codes de sortie : `0` OK · `1` test échoué / non conforme · `2` panne matérielle · `3` usage ou préflight · `130` interrompu. Détails : [`documentation/CLI.md`](./documentation/CLI.md).

## Machines déjà configurées

BenchSuite **n'installe et ne désinstalle jamais rien** ; il lance seulement ce dont les tests ont besoin (modules IPMI, service DCGM, mode persistance GPU). `--read-only` / `BENCHSUITE_READ_ONLY=1` interdit même cela. `./cli.py --install-guide` affiche, sans rien installer, comment ajouter ce qui manque en tenant compte de l'existant (pilote, CUDA, DCGM, snap…). Détails : [`documentation/CLI.md`](./documentation/CLI.md).

## Fichiers utilisateur

| Fichier | Rôle |
|---|---|
| `~/BenchmarkResults/` | rapports exportés |
| `~/.benchsuite_history.json` | historique local |
| `~/.benchsuite_mcio.json` | étiquettes de connecteurs MCIO (voir `--mcio-scan`) |
| `~/.benchsuite_stress_profiles.json`, `~/.benchsuite_fio_profiles.json` | profils personnalisés |
| `~/.benchsuite/suites/` | vos séquences de tests |

## Consulter tous les rapports sur Windows

Le dossier [`windows_viewer/`](./windows_viewer/README_WINDOWS.md) fournit une **visionneuse locale avec base SQLite (`benchsuite.db`)** sans dépendance externe (Python 3.10+ stdlib uniquement) : double-cliquer sur `Lancer_BenchSuite_Viewer.bat`, sélectionner le dossier de rapports, puis filtrer, ouvrir `.html`/`.pdf`/`.csv`/`dmesg_*.txt` et visualiser les `.json` complets hors ligne dans votre navigateur.

---

## Limites connues

- Support GPU surtout **NVIDIA** (gpu-burn, gpu-fryer, DCGM, nvbandwidth) ; clpeak et vkpeak couvrent les autres marques en calcul.
- Linux ne connaît pas les connecteurs physiques (MCIO…) : les étiquettes du profil intégré sont « supposées » tant qu'elles ne sont pas confirmées (`--mcio-pin` / `--mcio-set`).
- SNIA PTS : pas de détection automatique d'état stable (lecture sur la courbe).
- SMART/NVMe : lecture dépendante du firmware ; certaines sondes peuvent être indisponibles.
- Une partie des mesures (DCGM, nvbandwidth, LACT) n'a pu être validée que sur du matériel précis (8 × RTX PRO 6000) : signalez les écarts.

## Documentation

- [Outils et dépendances (quoi installer, comment, liens)](./documentation/OUTILS_ET_DEPENDANCES.md)
- [Guide d'utilisation & installation](./documentation/GUIDE_UTILISATION.md)
- [Mode CLI, séquences, MCIO](./documentation/CLI.md)
- [Catalogue des benchmarks et profils](./documentation/BENCHMARKS.md)
- [Architecture & concurrence](./documentation/ARCHITECTURE.md)
- [Télémétrie, capteurs & throttling](./documentation/TELEMETRIE_ET_CAPTEURS.md)
- [Exports & rapports](./documentation/EXPORTS_ET_RAPPORTS.md)
- Référence nvidia-smi et documentation des outils tiers : branche `docs-outils` (`outils/<outil>/`)

Documentation officielle des outils tiers : branche `docs-outils` (volontairement hors de la branche logicielle pour la garder légère). Licences des composants tiers : [`THIRD_PARTY_NOTICES.md`](./THIRD_PARTY_NOTICES.md).

## Licence

Voir [`LICENSE`](./LICENSE).
