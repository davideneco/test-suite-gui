# ⚡ Catalogue des Benchmarks et Stress Tests

Ce document répertorie l'ensemble des modules de test intégrés dans BenchSuite, leurs binaires sous-jacents, leurs paramètres et les métriques produites.

---

## 1. Catégorie « CPU + RAM »

### `cpu_stress` — CPU + RAM (stress-ng)
- **Outil :** `stress-ng`
- **Description :** Génère une charge intensive multi-cœur et multi-socket (CPU), une saturation mémoire avec test d'intégrité (RAM / Burn RAM), ainsi que des charges mixtes simultanées pour valider la stabilité sous charge maximale, mesurer les fréquences soutenues et tester la robustesse des barrettes mémoires.
- **Profils intégrés :**
  - **CPU :**
    - `cpu_only_quick` : Test Rapide (2 min) sur tous les threads.
    - `cpu_physical_only` : Cœurs Physiques Uniquement (sans SMT/HyperThreading).
    - `cpu_single_core` : Single-Core Boost Max (1 thread isolé).
    - `cpu_avx_matrix` : Calcul Flottant et Stress Vectoriel AVX (5 min).
    - `cpu_stability_15m` : Stabilité Thermique et Tctl (15 min).
    - `cpu_only_main` : Validation Complète (1 heure).
  - **RAM (Burn RAM) :**
    - `ram_burn_quick` : Burn-in Rapide (2 min) saturant ~85% de la RAM physique sans risque d'OOM.
    - `ram_burn_max` : Burn-in Mémoire Intensif (15 min) avec `--vm-populate` (allocation immédiate), `--vm-keep` (maintien continu en mémoire), et `--vm-method all` (cyclage de tous les motifs de bits DRAM).
    - `ram_burn_endurance_1h` : Endurance & Intégrité Mémoire (1 heure).
  - **Combiné :**
    - `cpu_ram_main` : Charge Mixte CPU + RAM (1 heure).
    - `cpu_burnin_4h` : Burn-in Serveur (4 heures).
  - **CLI :** Ligne de commande stress-ng personnalisée directe.
- **Métriques :** Score Bogo-Ops/s, Débit VM, Fréquences CPU (MHz), Températures moyennes et crêtes (°C), Charge système (%), Télémétrie RAM (température et puissance si BMC disponible).

---

## 2. Catégorie « Stockage »

### `storage_fio_nvme` & `storage_fio_hdd` — Test FIO multi-scénarios
- **Outil :** `fio`
- **Description :** Benchmark de stockage industriel évaluant la bande passante et la latence sur disques NVMe PCIe Gen4/Gen5 ou disques durs SATA/SAS.
- **Sécurité :** Vérification automatique des partitions montées (`has_system_mount()`) pour interdire toute écriture destructrice accidentelle sur le disque système.
- **Modes testés :** Lecture séquentielle 1M, Écriture séquentielle 1M, Lecture aléatoire 4K, Écriture aléatoire 4K.
- **Métriques :** Débit en Mo/s (ou Go/s), IOPS, Latence minimale/moyenne/maximale (ms ou µs), Percentiles de latence (95th / 99th).

---

## 3. Catégorie « GPU »

### `gpu_burn` — Stress thermique CUDA (gpu-burn)
- **Outil :** `gpu-burn`
- **Description :** Calcul matriciel intensif FP32/FP64 saturant les cœurs Tensor et CUDA de l'ensemble des accélérateurs NVIDIA détectés.
- **Paramètres :** Durée (secondes), précision (`float` ou `double`), sélection des GPU (`target_gpus`).
- **Télémétrie associée :** Capture en temps réel des températures Cœur, Hotspot, VRAM, Puissance en Watts, et détection du throttling matériel.

### `gpu_fryer` — Stress multi-plateforme (gpu-fryer)
- **Outil :** `gpu-fryer`
- **Description :** Stress test CUDA et ROCm alternatif optimisé pour pousser la consommation énergétique au plafond thermique.
- **Paramètres :** Nombre d'itérations, sélection GPU (`CUDA_VISIBLE_DEVICES`).

### `bench_clpeak` — Calcul GPU (clpeak)
- **Outil :** `clpeak`, en deux générations gérées automatiquement :
  - **ancienne (paquet apt 1.1.x, OpenCL seul)** : BenchSuite lit `clinfo -l` et lance `clpeak -p <plateforme> -d <device>` pour chaque GPU. Cela évite l'échec `clCreateContextFromType (-1)` quand une plateforme sans GPU (ex. « AMD Accelerated Parallel Processing ») est listée en premier. Les processeurs (CPU OpenCL) sont ignorés.
  - **récente (multi-backend, release GitHub)** : BenchSuite choisit le meilleur backend disponible (CUDA > ROCm > OpenCL > Vulkan) avec `--devices`. La variante « cuda » de la release fonctionne sans aucun pilote OpenCL.
- **Mesure :** calcul FP32/FP64/entiers, bande passante mémoire, transferts PCIe ; une mesure par GPU (`clpeak_devN_*`), score = médiane.
- **Prérequis ancienne version :** `sudo apt install clpeak ocl-icd-libopencl1 clinfo` (+ `libnvidia-compute-<version>` pour NVIDIA).

- **Chauffe (`time_s`, 0 = un seul passage) :** clpeak est relancé en boucle pendant `time_s` secondes par GPU (choisir « Calcul FP32 » pour la charge maximale). Métriques `clpeak_devN_burn_*` (voir « Test de chauffe » ci-dessous).

### `bench_gpu_oclburn` — Burn OpenCL vérifié (Intel, AMD, NVIDIA)
- **Outil :** `oclburn`, programme C fourni avec BenchSuite (`third_party/benchsuite_oclburn/oclburn.c`, ~450 lignes). Compilé au premier lancement avec **gcc seul** dans `~/.benchsuite/tools/oclburn/` (la bibliothèque OpenCL est chargée dynamiquement : pas besoin des en-têtes `CL/cl.h`) ; BenchSuite n'installe rien d'autre. Il faut un pilote OpenCL du GPU (`clinfo -l` doit le lister).
- **Charge :** par GPU, deux files de commandes en parallèle, sans temps mort (le lot suivant est toujours déjà en file) — **calcul** : entiers exacts, FP32 (FMA vectoriels, 8 accumulateurs indépendants) et FP16 si `cl_khr_fp16` ; **mémoire** : écriture puis relecture de motifs (adresse, inversion, constantes) sur `vram_pct` % de la VRAM (90 % par défaut, 30 % max pour un iGPU à mémoire partagée). Les lancements durent ~80 ms (calibrés) pour rester sous le watchdog du pilote. Tous les GPU OpenCL sont ciblés ensemble (« GPU cibles » = index dans `clinfo -l`) ; mode `all` / `compute` / `memory`.
- **Vérification :** chaque noyau est comparé à une référence : le noyau entier est exact (comparé à un calcul CPU sur 4096 éléments au démarrage) ; les noyaux flottants (opérandes pseudo-aléatoires pour faire basculer un maximum de bits, valeurs bornées) replient **tous les bits** de leurs accumulateurs dans une somme de contrôle entière, de sorte qu'une erreur transitoire ne peut pas être « effacée » par les calculs suivants, puis le résultat est comparé à leur 1er résultat (reproductible bit à bit sur un même GPU) ; les motifs mémoire sont relus et comptés. Réglage « Contrôle FP32 / FP16 » (`-F` d'oclburn) : *maximale* = repli à chaque série de FMA (aucune erreur manquée dans nos simulations d'inversion d'un bit), *équilibrée* = toutes les 2 séries (~5 % de bits de poids faible manqués), *charge maximale* = toutes les 4 séries (~12 % manqués, plus de FMA par seconde). Le repli entier réduit le débit FP32 affiché (≈ −30 % sur CPU/pocl). Métriques `compute_errors` / `memory_errors` ; **toute erreur fait échouer le verdict** (« GPU défectueux ou instable »). Sans erreur : règle « aucune erreur de calcul ni de mémoire ».
- **Puissance atteinte :** `gpuN_power_cap_pct` = **médiane** de la puissance soutenue (après 10 s ; le P95 `gpuN_power_p95_w` montre les pics) en % de la **vraie limite** de la carte (`power1_cap` hwmon pour AMD / Intel xe, `power.limit` de nvidia-smi ; jamais la puissance instantanée) pour les GPU effectivement chargés ; limite inconnue = « non évalué ». Le verdict avertit sous 85 % (charge insuffisante). Pas calculée en mode « mémoire seule ».
- **Choix des GPU :** « GPU cibles » = indices de la fiche GPU ; ils sont associés aux périphériques OpenCL par **adresse PCI** (`cl_khr_pci_bus_info`, ou extensions NVIDIA / AMD). Sans adresse PCI fournie par le pilote, les indices sont interprétés dans l'ordre d'OpenCL (`clinfo -l`) et un avertissement est ajouté au rapport.
- **Robustesse :** un lot qui échoue (reset du GPU, mémoire insuffisante) est détecté et arrête le test avec un message ; un arrêt demandé récupère le bilan partiel (`verifie` = faux, règle « non évalué ») ; un test arrêté n'est jamais affiché « sans erreur ».
- **Résultats :** `oclburn_devN_*` (GFLOPS FP32 / FP16, GIOPS, Go/s mémoire, Go testés, erreurs, stabilité `burn_*`). Score = médiane des GFLOPS FP32. La durée `-t` inclut ~10 s de calibration.
- **Limites (non vérifiées sur matériel réel côté développement) :** atteindre 100 % de la puissance dépend du GPU et du pilote (réglable avec `-w`, work-items par unité de calcul) ; les unités matricielles (XMX Intel, Tensor/WMMA) ne sont pas sollicitées (OpenCL générique) ; que le calcul et la mémoire s'exécutent réellement en même temps dépend du pilote. Testé ici avec pocl (OpenCL sur CPU) : calcul, mémoire, calibration et détection d'erreurs (injection de panne `OCLBURN_INJECT=1`) fonctionnent. Variable `BENCHSUITE_OCLBURN_CPU=1` : inclut les périphériques OpenCL CPU (tests sans GPU).
- **Séquence livrée :** `./cli.py --suite burn_gpu_verifie` (10 min) ; test isolé : `./cli.py -b gpu_oclburn -p gpu_oclburn.time_s=300`.

### `bench_vkpeak` — Calcul Vulkan
- **Outil :** `vkpeak`
- **Chauffe (`time_s`, 0 = un seul passage) :** vkpeak est relancé en boucle pendant `time_s` secondes par GPU (~300 W sur une Arc Pro B70) : c'est le moyen libre de « burner » un GPU Intel ou AMD (gpu-burn est réservé à NVIDIA). Séquence livrée : `./cli.py --suite burn_gpu_toutes_marques` (vkpeak 10 min, pause, clpeak FP32 10 min).
- **Test de chauffe — métriques :** `burn_passes`, `burn_scores`, `burn_score_first` (GPU froid), `burn_score_end` (moyenne du dernier tiers des passages), `burn_drop_pct` (chute en charge soutenue). Règle de verdict « burn_drop » : avertissement au-delà de 10 % (jamais bloquant par défaut), sans aucune spec constructeur. Courbes de température/puissance/fréquence continues sur toute la durée ; les mesures de référence du rapport sont celles du 1er passage.
- **Description :** Évaluation des performances de calcul via l'API Vulkan native (score = meilleur FP32). « GPU cibles » = index(s) de device Vulkan (ex. `0,1`) : un passage `vkpeak <index>` par device.

### `nvbandwidth` — Matrice d'Interconnexion NVIDIA
- **Outil :** `nvbandwidth`
- **Description :** Mesure de la bande passante et de la latence inter-GPU (NVLink / PCIe) ainsi que des transferts Host-to-Device (HtoD) et Device-to-Host (DtoH).
- **Rapport :** Génération d'une matrice thermique et d'un tableau croisé dans le rapport HTML.

---

## 4. Catégorie « Réseau »

### `net_iperf_tcp` / `net_iperf_udp` — Réseau iperf3 (client)
- **Outil :** `iperf3` (`sudo apt install -y iperf3`). Il faut **deux machines** : une fait serveur, l'autre client.
- **Serveur :** menu latéral **Réseau → « Serveur iperf3 (2 PC) »** de BenchSuite (démarre / arrête `iperf3 -s`, affiche les adresses IP et la commande à lancer côté client, le journal des connexions et la commande de pare-feu `ufw`) — ou simplement `iperf3 -s`. Un seul client à la fois.
- **Client (cette machine) :** test « Réseau iperf3 TCP » ou « UDP » avec l'adresse IP du serveur. Paramètres : hôte, port, durée, flux parallèles (`-P`), sens inversé `-R` (TCP), débit cible `-b` (UDP). CLI : `./cli.py -b net_iperf_tcp -p net_iperf_tcp.host=192.168.1.20`.
- **Métriques :** débit (Mbit/s), retransmissions TCP, charge CPU hôte / distante ; UDP : gigue (ms), paquets perdus (%). Aucun seuil n'est inventé : le débit n'est pas comparé à une limite sans réglage de l'utilisateur.
- **Erreurs :** serveur absent / port fermé → message avec la piste (serveur non lancé, pare-feu, adresse, câblage) ; arrêt manuel → statut « arrêté ».

---

## Mémoire : bande passante RAM et GPU

### `bench_ram_stream` — Bande passante mémoire (STREAM)
- **Outil :** STREAM de John D. McCalpin (`third_party/stream/stream.c`, non modifié), compilé au premier lancement avec `gcc` (cache `~/.cache/benchsuite/stream`) ; un binaire `stream` présent dans le PATH est utilisé en priorité.
- **Mesure :** Copy / Scale / Add / Triad en GB/s ; score = Triad. Tableaux ≥ 4× le cache L3 total (règle STREAM), bornés à 25 % de la RAM libre ; threads = cœurs physiques, épinglés (`OMP_PROC_BIND=spread`).
- **Note :** exécution non certifiée « Run Rules » : à citer comme « basé sur STREAM ».

### `bench_gpu_vram_bandwidth` — Bande passante VRAM
- **Outils :** `nvbandwidth -t device_local_copy` (NVIDIA, une valeur par GPU) et/ou `clpeak --global-bandwidth` (OpenCL, toutes marques, une valeur par périphérique OpenCL).
- **Méthode :** `auto` (nvbandwidth si présent, sinon clpeak) · `nvbandwidth` · `clpeak` · `both` (les deux, pour recouper : `clpeak_vs_nvbandwidth_ratio`).
- **Sortie :** `gpuN_vram_bw_gbs` (nvbandwidth prioritaire ; clpeak dans l'ordre OpenCL), `clpeak_devN_gbs`, score = médiane. Un GPU nettement sous la médiane déclenche une règle du verdict.

### `bench_gpu_vram_test` — Intégrité VRAM
- **Outil :** `gpu-burn -m 95%` : remplit la VRAM et compare chaque résultat ; toute erreur met le test en échec.

### `bench_gpu_dcgm` — Diagnostic NVIDIA DCGM
- **Outil :** `dcgmi diag -r <niveau> -j` (DCGM, Apache-2.0 ; installation : voir GUIDE_UTILISATION.md).
- **Niveaux :** 1 ≈ secondes · 2 ≈ 2 min · 3 ≈ 15 min (mémoire GPU, calcul, PCIe, bande passante, stress ciblé) · 4 plus long. `tests` permet des modules précis (ex. `memory,pcie`).
- **Résultat :** score = % de tests réussis ; `dcgm_<test>` = pass/fail/warn/skip ; `gpuN_dcgm_issues` ; échec d'un test → statut « error », avertissement → « À VÉRIFIER » dans le verdict.

---

## Profils stress-ng (CPU / RAM / mixte)

Grille homogène : **3 durées** (extra rapide 5 min · rapide 30 min · burn-in 4 h) pour chaque famille.

| Famille | Identifiants | Contenu |
|---|---|---|
| CPU · tous les threads | `cpu_all_5m`, `cpu_all_30m`, `cpu_all_4h` | tous les threads logiques (SMT/HT inclus), stressor `all` |
| CPU · cœurs physiques | `cpu_phys_5m`, `cpu_phys_30m`, `cpu_phys_4h` | un worker par cœur physique |
| RAM · burn | `ram_burn_5m`, `ram_burn_30m`, `ram_burn_4h` | ~85 % de la RAM, `--vm-populate --vm-keep --vm-method all` |
| CPU + RAM · mixte | `mix_5m`, `mix_30m`, `mix_4h` | tous les threads + ~70 % de la RAM |
| Personnalisé | `stress_custom` | tout est modifiable (panneau « Ajustements » actif) |
| CLI | `stress_ng_cli_only` | ligne de commande stress-ng libre |

Les anciens identifiants (`cpu_only_quick`, `ram_burn_quick`, `cpu_ram_main`…) fonctionnent toujours dans les séquences mais ne sont plus listés.

## Profils fio « pro »

Sources publiques : `examples/jesd219.fio` (fio), spécification SNIA SSS PTS Enterprise, CrystalDiskMark 8, profils IOMeter d'Intel, test `fdatasync` d'etcd. Il n'existe pas de fichier fio « officiel » publié par les fabricants de SSD/HDD : ces profils reprennent les standards et charges de référence que les constructeurs et la presse utilisent.

| Profil | Charge |
|---|---|
| `jesd219_enterprise_1h` / `_8h` | JEDEC JESD219 : 40 % lecture / 60 % écriture, mélange de blocs 512 o→64 Ko, zones chaudes, QD256×4 |
| `snia_pts_4k_steady_30m` | 4K aléatoire 100 % écriture (point de référence SNIA, après pré-conditionnement) |
| `snia_pts_iops_matrix` | 8 tailles de bloc × 7 mélanges R/W, 1 min chacun (56 points) |
| `snia_pts_latency`, `snia_pts_throughput` | latence QD1 (9 points) ; débit séquentiel 1M/128K |
| `cdm8_default_suite` | SEQ1M Q8T1, SEQ1M Q1T1, RND4K Q32T1, RND4K Q1T1 |
| `iometer_fileserver/webserver/workstation` | profils IOMeter classiques |
| `oltp_8k_70_30`, `oltp_16k_70_30` | bases de données (SQL/Oracle/PostgreSQL ; MySQL InnoDB) |
| `etcd_wal_fdatasync` | journal/WAL : écritures 2300 o + fdatasync (latence `sync_lat_p99_ms`) |
| `hdd_rand4k_qd1`, `hdd_rand4k_ncq` | temps d'accès et NCQ |
| `hdd_zone_outer_read`, `hdd_zone_inner_read`, `hdd_full_surface_read` | débit zone externe/interne, lecture complète |

Limites : SNIA simplifié (pas de détection automatique d'état stable : lire la courbe) ; les profils d'écriture **détruisent les données** du disque ciblé (BenchSuite refuse le disque système).

---

## Relevé électronique passif (pendant chaque test)

Ce n'est **pas un test** : pendant chaque test (interface ou CLI), BenchSuite relève en tâche de fond tout ce que la machine expose
— tensions, courants, puissances, ventilateurs, températures de carte (VRM, PSU, entrée/sortie d'air) — et l'ajoute au résultat.

**Sources lues en parallèle** (la carte mère n'expose pas toujours ses mesures au BMC) : Redfish, IPMI (SDR), DCMI, capteurs hwmon /
lm-sensors (Super-I/O, PMBus), RAPL (puissance CPU/DRAM). Les capteurs GPU restent dans la télémétrie GPU habituelle.
Relevé toutes les 2 s (hwmon, RAPL) et 5 s (BMC). Désactivable avec `BENCHSUITE_NO_ELECTRICAL=1`.

**Dans les rapports** (actuellement désactivé, `EXPORT_SEQUENCE_ELECTRONICS = False` dans `exporters/export.py`) : onglet HTML **« Séquence &amp; électronique »** (courbes par famille : rails 12 V / 5 V / 3,3 V, tensions
variables, puissance, courants, ventilateurs, températures de carte ; tableau min / moyenne / max par capteur), section dédiée dans le
PDF, lignes `electrical` dans le CSV, objet `metrics.electrical` dans le JSON.

**Information, pas de note** : seules les tensions des rails fixes lues par le BMC (sources calibrées) sont signalées si elles sortent de
la tolérance ATX ±5 % (alerte « à vérifier » du tableau de bord) ; les tensions Super-I/O/hwmon ont souvent un diviseur mal connu.
Sans capteur lisible, rien n'est ajouté (pistes : `ipmitool` + `modprobe ipmi_si ipmi_devintf`, Redfish, `sensors-detect`, PMBus).

## Liens PCIe pendant les tests

Un instantané au repos ne montre pas si un composant monte à son lien maximum : beaucoup de périphériques (GPU, NVMe) baissent leur vitesse
de lien quand ils ne travaillent pas. **Pendant chaque test**, BenchSuite relève donc toutes les ~2 s la vitesse (Gen) et la largeur actuelles
de chaque périphérique PCIe (sysfs, sans droits) et les compare à son **maximum matériel** (`max_link_speed` / `max_link_width`) et à celui de
son **port parent** (le plafond du slot / de la bifurcation). Résultat par périphérique : lien maximum, liens observés avec la part du temps
passée dans chacun, % du temps au maximum, transitions (`metrics.pcie_links` dans le JSON).

- **Largeur sous le maximum** pendant l'essentiel du test (≥ 80 % des relevés) : anomalie (« à vérifier » au verdict), avec la cause probable
  quand le port parent est lui-même limité (slot câblé x8, bifurcation, riser).
- **Vitesse maximale jamais atteinte** : informative (normale si le périphérique était au repos) ; elle n'est jugée « à vérifier » que pour
  le périphérique sollicité par le test (GPU pour un test GPU, NVMe pour un test stockage, carte réseau pour iperf3).
- **Maximum inconnu** : « non évalué », jamais d'échec. Aucun seuil inventé.
- Affichage : carte de chaque test dans le rapport HTML (+ alertes du tableau de bord), PDF (section par test), CSV (`pcie_links`), CLI (résumé
  et avertissements). `BENCHSUITE_NO_PCIE=1` désactive le relevé.

## Inventaire avant / après et journaux

**Chaque test** (interface ou CLI) compare l'inventaire matériel de départ et de fin ; **chaque campagne CLI** en fait un de plus pour
toute la séquence (`inventaire_avant.json`, `inventaire_apres.json`, `inventaire_diff.json` dans le dossier de campagne).
Les écarts et les **nouveaux événements du journal BMC (SEL)** apparaissent dans l'onglet « Séquence &amp; électronique », le PDF
(section par test), le CSV (`inventory`, `bmc_sel`) et le JSON (`metrics.inventory_diff`, `inventory_campaign`), ainsi que les
événements noyau relevés pendant le test (Xid, MCE, AER, ECC…).

Contenu de l'inventaire : CPU, mémoire (et barrettes si dmidecode accessible), périphériques PCI (GPU, NVMe, réseau, accélérateurs) avec largeur/vitesse de lien et compteurs d'erreurs AER, GPU NVIDIA (UUID, lien, ECC), NVMe, disques, interfaces réseau (vitesse, erreurs CRC), compteurs ECC (EDAC), événements BMC (SEL), versions BIOS/BMC/pilote.

La comparaison alimente le **verdict** : périphérique disparu, lien PCIe réduit, erreur non corrigeable = non conforme ; erreurs corrigées apparues (AER, ECC, réseau), nouvel événement SEL, firmware modifié = à vérifier. La vitesse de lien des GPU est ignorée (elle chute légitimement au repos).
`./cli.py --inventory [fichier]` affiche/enregistre un inventaire ; `./cli.py --inventory-diff avant.json apres.json` compare deux fichiers (utile après un redémarrage) ; `--no-inventory` (ou `BENCHSUITE_NO_INVENTORY=1`) désactive l'inventaire de campagne et celui de chaque test.


## Courbes de la séquence (température et puissance)

Pour une campagne (onglet **Séquence automatique** ou `cli.py --suite`), BenchSuite enregistre sur une même ligne de temps :
- **Températures :** cœur et hotspot de chaque GPU, CPU (par socket), RAM, disque ;
- **Puissances :** chaque GPU, CPU (package), RAM, disque.

Dans l'interface, onglet **« 📈 Suivi & graphiques »** : deux graphiques en direct (température en haut, puissance en bas), une couleur par
composant, hotspot en pointillés, repères jaunes numérotés au début de chaque étape ; la légende affiche la dernière valeur. À la fin,
l'image `courbes_sequence.png` est ajoutée au dossier de campagne. Les mêmes courbes sont dans les rapports : onglet HTML « Séquence &
électronique » (graphiques interactifs + min / moyenne / max / dernier), section « Courbes de la séquence » du PDF, lignes `sequence_courbes`
du CSV et objet `sequence_series` du JSON. En parallèle, chaque test garde ses propres courbes dans sa carte de résultat.
Le graphique en direct demande pycairo (`sudo apt install python3-gi-cairo`) ; sans lui, les courbes restent dans les rapports.

## gpu-burn : durées longues et GPU récents

- **Arrêt prématuré :** si gpu-burn se termine « proprement » avant la durée demandée, BenchSuite le relance pour la durée restante (segments d'au moins 20 s ; `burn_segments` et `burn_arrets_anticipes_s` dans les métriques, explication dans les notes). Une vraie erreur (`ERRORS FOUND`, `DIED`, code ≠ 0) n'est jamais relancée.
- **Bibliothèque CUDA introuvable** (`error while loading shared libraries: libcublas.so.12…`) : BenchSuite cherche la bibliothèque dans `/usr/local/cuda*`, `/usr/lib/x86_64-linux-gnu`, `/opt/gpu-burn`… et relance avec `LD_LIBRARY_PATH` ; sinon le message d'erreur donne la marche à suivre. Les GPU Blackwell (RTX PRO 6000) demandent un gpu-burn compilé avec CUDA ≥ 12.8 (`make COMPUTE=120`) ; le paquet snap `gpu-burn` est souvent trop ancien. gpu-fryer n'utilise pas ces bibliothèques, d'où son bon fonctionnement.

### gpu-burn : erreurs fréquentes

- **`Burning for 0 seconds` / `DIED`** : la syntaxe de gpu-burn est `gpu_burn [OPTIONS] [DURÉE]` ; la durée doit être le **dernier** argument. BenchSuite la place maintenant en dernier (avant, `-d` après la durée pouvait être lu comme durée 0).
- **`couldn't find compare kernel: compare.fatbin … named symbol not found`** : le noyau de comparaison (`compare.fatbin` ou `compare.ptx` selon la version) manque, vient d'une autre version que le binaire, ou n'a pas été compilé pour ce GPU. Reconstruire proprement (`make clean && make COMPUTE=<arch> CUDAPATH=/usr/local/cuda-X.Y`), garder `gpu_burn` et `compare.*` ensemble, supprimer les anciennes copies. BenchSuite lance gpu-burn depuis le dossier du binaire quand `compare.*` s'y trouve, et marque le test en erreur avec ce diagnostic.
- **`foreign struct converter for 'cairo.Context'`** (terminal) : paquet `python3-gi-cairo` absent. Les graphiques sont désormais désactivés proprement au lieu de répéter l'erreur ; `sudo apt install python3-gi-cairo` les réactive.

## Profils fio classés par fonction (onglets)

Le menu des profils fio est organisé en **onglets cliquables** selon ce que fait le profil ; chaque onglet liste ses profils, groupés par norme d'origine (OCP, SNIA, JEDEC…) :

| Onglet | Contenu |
|---|---|
| ⚡ Performance | Peak, CrystalDiskMark, SNIA PTS (matrice IOPS, débit séquentiel, état stable 4K) |
| ⏱ Latence | SNIA latence et histogramme de temps de réponse (percentiles jusqu'à 99,999 %), **OCP CTO-3** (aucune E/S > 8 s ; ≤ 7 E/S > 2 s par heure), journal fdatasync |
| 🛡 Stabilité | intégrité des données (écriture + relecture CRC), saturation du lien PCIe, déluge de petites E/S, endurance mixte 4 h |
| 🌡 Chauffe | écriture séquentielle continue, aléatoire mixte soutenu, lecture maximale, stress NVMe 1 h / 4 h |
| ⏳ Endurance | JEDEC JESD219 (30 min, 1 h, 8 h), **OCP ENDUD-2** (fin de vie, 80 % plein, 4K aléatoire / 128K séquentiel), remplissage séquentiel OCP (≤ 180 min), SNIA WSAT 6 h, pré-conditionnement SNIA |
| 🧾 Applicatif / 🧲 HDD / 👤 Mes profils | OLTP, IOMeter, journal ; disques durs ; profils de l'utilisateur (modifiables) |

En ligne de commande : `./cli.py --profiles` (liste par fonction) et la séquence `recette_nvme` (version courte : performance, latence OCP, stabilité, chauffe).

### Ce que mesurent « Stabilité » et « Chauffe »

Pour chaque disque NVMe testé, BenchSuite compare **avant / après** le test (`metrics.nvme_sante`, lecture seule, sans effet sur le disque) :
- **PCIe** : erreurs AER corrigées (à vérifier : lien marginal), non corrigeables (non conforme), perte de largeur du lien (non conforme) ;
- **NVMe** : erreurs média, avertissement critique, nouvelles entrées du journal d'erreurs ;
- **Chauffe** : température et puissance en courbe, transitions de gestion thermique (throttling) et temps au-dessus des seuils d'avertissement / critique (compteurs du journal SMART NVMe).
Un compteur que le firmware n'expose pas est « non évalué », jamais un échec. Il faut `nvme-cli` (et root ou `sudo -n`) pour le journal SMART ; les erreurs PCIe viennent de `sysfs` sans privilège. Sur périphérique brut (`/dev/...`) sans `sudo NOPASSWD`, BenchSuite ouvre un unique assistant privilégié (`core/priv_helper.py`) via `pkexec` pour toute la session : une seule authentification suffit pour toute la suite de tests, les tests suivants, l'arrêt et les relevés SMART associés.

### Sources et limites

- **OCP** : Datacenter NVMe SSD Specification v2.7 (ENDUD-2 §8.1, CTO-3 §4.7, section Meta 20). La spec définit aussi des cibles de performance propres à Meta (scripts FioSynth, CacheBench, BootBench) qui ne sont **pas** reprises : on garde fio seul.
- **SNIA** : SSS PTS 2.0.2 (WSAT §10, histogramme §10.2). L'état stable (5 rounds, plage ≤ 20 %, pente ≤ 10 %) n'est pas détecté automatiquement.
- **JEDEC** : JESD219 (déjà présent).
- Approximations : le « short stroke » OCP (réduction de capacité par firmware) est remplacé par une limite de zone testée (`--size=10%`) ; le nombre exact d'E/S > 2 s (CTO-3) n'est pas compté, seule la latence maximale est jugée ; `rwmixread` est exprimé en nombre d'E/S.
- Non testé sur un vrai SSD (aucun disque NVMe dans l'environnement de développement) : commandes fio construites et parsing vérifiés avec un faux fio.

## Informations système : liens PCIe

La page « Infos système » et les rapports (section « Périphériques PCIe (liens) », clé JSON `pcie`) listent chaque périphérique PCIe hors ponts : bus, type, nom (`lspci -mm -nn`, sinon `pci.ids`), pilote, **Gen et largeur actuelles / maximales**, état (« réduit : … » si le lien est inférieur au maximum), nœud NUMA et erreurs AER (corrigées / non fatales / fatales). Lecture sysfs sans droits. Pour un GPU Intel Arc Pro derrière le switch de sa carte, le lien affiché est celui du pont amont. Une Gen réduite sur un GPU au repos est normale (économie d'énergie) ; une largeur réduite ou des erreurs AER ne le sont pas. Autres ajouts : nœuds NUMA (CPU), mode de démarrage UEFI/BIOS et IOMMU (carte mère).

## Page « Configuration GPU Intel » (équivalent de nvidia-smi)

Menu *GPU → Configuration GPU Intel* (`ui/intel_page.py`, logique dans `core/intel_control.py`). **Lecture sans privilège** (sysfs / hwmon du pilote xe, rafraîchie toutes les 2 s tant que la page est affichée) : températures cœur (`pkg`) / VRAM / contrôleur mémoire / PCIe / canaux VRAM avec leurs seuils, ventilateur, puissance de la carte (delta du compteur d'énergie `energy1_input`), limites **PL1** (`power1_max`, soutenue), **PL2** (`power1_cap`, pointe), TDP par défaut (`power1_rated_max`), fenêtres, limite critique I1, fréquences (`tile*/gt*/freq0` : active, demandée, min, max, RP0, RPn), raisons de bridage (`freq0/throttle/reason_*`), lien PCIe.

**Réglages** (élévation à la demande : root direct, sinon `sudo -n`, sinon `pkexec`) : PL1, PL2, plage de fréquences min–max, « Valeurs d'origine ». Garde-fous : refus en mode lecture seule (`--read-only`) ; PL1 ≤ TDP par défaut et ≥ 25 % du TDP (garde-fou BenchSuite, pas une spécification Intel) ; PL2 ≥ PL1 et ≤ 2 × TDP ; fréquences entre RPn et RP0, min ≤ max ; 0 (limite désactivée) jamais proposé ; seuls les fichiers `power1_max`, `power1_cap`, `min_freq`, `max_freq` du GPU sont écrits ; relecture après écriture avec message si le firmware a corrigé la valeur. Repli sur `xpu-smi config -d N --powerlimit W` / `--frequencyrange min,max` quand le fichier sysfs n'existe pas. La page affiche les **commandes équivalentes** à copier (sysfs et xpu-smi). Les réglages sont perdus au redémarrage ; les valeurs d'origine sont mémorisées à la première ouverture dans `~/.benchsuite/intel_defaults.json`.

**À valider sur matériel réel :** seule la lecture a été observée sur une Arc Pro B70 ; l'écriture (support de PL1 / PL2 / fréquences par ce modèle et ce noyau, noms exacts des fichiers `throttle`) a été testée avec un faux arbre sysfs uniquement. Documentation : `outils/xe-hwmon/` et `outils/xpu-smi/` sur la branche `docs-outils`.

## GPU Intel : xpu-smi

Pour les GPU Intel (Arc, Data Center GPU Flex / Max), BenchSuite utilise **xpu-smi** (Intel XPU Manager) quand il est installé : nom exact, mémoire, UUID, limite de puissance, et pendant les tests température du cœur et de la mémoire, puissance, fréquence, utilisation du GPU et de la mémoire (courbes et agrégats `gpuN_*`). Appel en `sudo -n` puis direct (root recommandé) ; sans xpu-smi ou sans droits, repli sur sysfs/hwmon (une seule sonde en général) avec la remarque `intel_xpu` dans la fiche du GPU. La température du processeur n'est plus utilisée à la place de celle du GPU. Installation : paquet `xpu-smi` du dépôt Intel GPU (`./cli.py --install-guide`).

Format vérifié sur une Arc Pro B70 (`xpu-smi stats -d 0 -j`, tableau texte et `config -d 0`) : températures cœur / mémoire / **VR** (régulateurs), puissance, fréquence, utilisation, mémoire utilisée, débits mémoire et PCIe. La limite de puissance affichée est la limite soutenue si elle est connue, sinon la limite « burst » de la carte. Comme `xpu-smi discovery` ne donne pas la taille de la mémoire, elle est **estimée** (mémoire utilisée ÷ utilisation, arrondie au Gio, remarque `vram_note` dans la fiche) ; absente si l'estimation n'est pas fiable.

**Quelle température regarder (Intel Arc / pilote xe) :** la **température du cœur est le capteur `pkg`** (« Package temperature » dans la documentation noyau `sysfs-driver-intel-xe-hwmon`) : c'est la puce GPU, repérée « Cœur (PKG) » dans les courbes et utilisée pour les maximums, moyennes et le verdict. Les autres sont **secondaires** (courbes pointillées « (secondaire) », texte « capteurs secondaires » du rapport) : `vram` = mémoire (courbe « VRAM », capteur principal de la mémoire) ; `mctrl` = « Memory controller average temperature » (contrôleur mémoire, moyenne) ; `pcie` = « GPU PCIe temperature » (partie PCIe de la puce) ; `vram_ch_N` = un capteur par canal VRAM (le maximum est affiché). Chacun a ses propres seuils `crit`/`emergency` dans hwmon (ex. B70 : pkg crit 100 °C, vram 105 °C, mctrl et pcie 100 °C). Sans capteur `pkg` lisible, BenchSuite retombe sur la température cœur de xpu-smi.

**Capteurs hwmon du pilote xe** (sans droits, même sans xpu-smi) : températures du contrôleur mémoire, du lien PCIe et des canaux VRAM (maximum), ventilateur (0 tr/min = ignoré), limite thermique (crit) et limite de puissance de la carte. Dans les rapports, les colonnes « Hotspot » et « ΔT hotspot » n'apparaissent que si un GPU les mesure ; une colonne « Activité & capteurs » regroupe utilisation, VRAM utilisée, températures VR / contrôleur mémoire / PCIe / canaux VRAM et ventilateur quand ils existent. Un capteur VR qui renvoie 0 est traité comme absent.

**Lien PCIe d'une Arc Pro B70 (Battlemage)** : l'endpoint GPU annonce en permanence « 2.5 GT/s x1 » car il est derrière le switch interne de la carte (ponts Intel `e2ff` amont et `e2f0` aval). BenchSuite lit alors le lien **réel** sur le pont amont (sysfs, sans droits) et le signale dans `pcie_note`. Repère indépendant : `clpeak --transfer-bandwidth` (Gen1 x1 ≈ 0,25 Go/s ; Gen5 x16 ≈ 50–63 Go/s). La liste des ponts internes connus est dans `core/system_info.py` (`_CARD_BRIDGES_INTEL`) ; à compléter pour d'autres cartes à switch embarqué. Source : gist de mploschiavo (juin 2026) sur l'Arc Pro B70 et le firmware `igsc`.

## Fréquence CPU mesurée : demandée ou matérielle ?
Pendant les tests CPU, BenchSuite lit la fréquence de chaque cœur dans `cpuinfo_cur_freq` (valeur côté matériel, souvent réservée à root : lecture directe, sinon un `sudo -n` en lecture seule) et, à défaut, dans `scaling_cur_freq` (la fréquence que le noyau croit avoir demandée ; elle peut afficher le maximum alors que le firmware ou la carte bride le CPU). Le champ `freq_source` du résultat indique la source utilisée ; si les deux valeurs divergent d'au moins 8 % sur au moins un quart des relevés, une note signale un CPU probablement bridé.
