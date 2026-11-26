# Mode CLI — bancs de test à la chaîne (sans interface graphique)

## Le plus simple : l'écran interactif et les commandes courtes

```bash
./cli.py                  # écran interactif plein écran, calqué sur la version graphique
./cli.py cpu 5m           # stress CPU 5 minutes (cpu 30m · cpu 4h · cpu phys 30m = cœurs physiques)
./cli.py ram 30m          # burn mémoire        (mix 30m = CPU + RAM ensemble)
./cli.py fio perf <dossier-de-données>   # stockage : profil ou onglet (perf lat stab heat endu app hdd) sur un disque de DONNÉES
./cli.py gpu oclburn 10m  # test GPU            (./cli.py gpu = liste des tests GPU)
./cli.py net serveur      # cette machine fait serveur iperf3 (affiche l'IP à donner, les commandes de pare-feu)
./cli.py net client <IP-du-serveur>    # test iperf3 vers l'autre machine
./cli.py net client <IP-du-serveur> bind=<IP-de-ce-PC>   # idem, en forçant le port d'envoi (adresse locale du port, iperf3 -B)
./cli.py net ports [N]    # ports Ethernet physiques et commandes pour donner une adresse temporaire ; N = n° de CE PC (1 sur un PC, 2 sur l'autre)
./cli.py infos            # informations de la machine à l'écran ;  ./cli.py rapport = fichiers HTML / PDF / JSON / CSV, sans test
./cli.py liste [cpu|fio|gpu|net]    # tout ce qu'on peut lancer, avec les noms courts
./cli.py lancer gpu_oclburn vram_pct=80 time_s=600    # n'importe quel élément de la liste, avec ses réglages
./cli.py seq               # séquences automatiques disponibles ;  ./cli.py seq rapide_cpu_ram  ·  ./cli.py seq recette_nvme <dossier-de-données> pause=60
./cli.py outils            # outils manquants sur cette machine et commandes d'installation (apt), lecture seule
./cli.py dernier          # résultats et rapport de ce démarrage de la machine
```

Après la commande, toutes les options habituelles restent valables : `-s NOM` (nom de la machine), `--client`, `--operator`, `-o DOSSIER`,
`-e html pdf json csv`, `--read-only`, `--new-report`. Les anciennes options (`-b`, `--suite`, `--resume`, `--help`…) fonctionnent comme avant.

### Écran interactif (`./cli.py`)
Menu à gauche (Séquences, CPU + RAM, Stockage, GPU, Réseau, Outils, Infos système, Rapports), liste de profils au milieu, réglages en dessous, comme dans la
version graphique. **↑↓** choisir · **→ / Entrée** ouvrir ou modifier · **← / Échap** revenir · **r** lancer · **?** aide · **q** quitter.
- Seuls les réglages **modifiés** sont envoyés au test ; le pied de page affiche la **commande courte équivalente**.
- Un outil manquant désactive le lancement et affiche la commande d'installation **Ubuntu / Debian (apt)**.
- Stockage : seuls les disques de **données** sont proposés (**Tab**) ; le disque système est refusé par `core/safety.py`.
- Le test s'exécute dans le terminal normal (suivi en direct, verdict) ; **Ctrl-C** l'arrête proprement, puis retour à l'écran.
- Page **Rapports** : identité du rapport (client, opérateur, n° de série… mémorisée dans `~/.benchsuite/cli_identity.json`) et **g** pour
  régénérer les fichiers. Page **Infos système** : fiche matériel, **e** pour l'exporter.
- Terminal trop petit (< 72×20), sans TTY ou sans `curses` : menu simple à numéros, mêmes fonctions. Sans TTY (script, cron) :
  l'aide des commandes courtes s'affiche.
- **Suivi d'un test :** barre, %, temps écoulé et restant estimé, mesures en direct. Sortie redirigée (journal) : une ligne par palier de 5 %.
- **Un seul test CLI à la fois** sur la machine (`BENCHSUITE_ALLOW_PARALLEL=1` pour forcer).
- Pas encore dans cet écran : réglages qui écrivent sur la machine (limites de puissance GPU / CPU) — utilisez l'interface graphique.

## Options complètes

```bash
./cli.py --suites                     # séquences disponibles
./cli.py --profiles                   # profils stress-ng et fio disponibles
./cli.py --suite recette_gpu --client ACME --order CMD-1234 --operator Operateur
./cli.py --resume ~/BenchmarkResults/ACME/SN123/20260930_0923_recette_gpu
./cli.py -b gpu_fryer -p gpu_fryer.time_s=120      # un seul test du catalogue
./cli.py --info-report -s MA-MACHINE               # rapport « informations système », sans aucun test
```

## Un seul rapport par démarrage de la machine (session)
Tant que la machine **n'a pas redémarré**, tous les lancements de `./cli.py` alimentent le **même rapport** : les nouveaux résultats
s'ajoutent à ceux des lancements précédents et les mêmes fichiers sont régénérés (au lieu d'un nouveau dossier à chaque fois).

```
<sortie>/<client>/<machine>/session_<AAAAMMJJ_HHMMSS du démarrage>/
    benchsuite_report.html / .pdf / .json / .csv   rapport unique, mis à jour à chaque lancement
    dmesg.txt                                      journal noyau (dernier lancement)
    session.json                                   résultats cumulés + inventaire de départ de la session
    runs/<horodatage>_<séquence>/                  détail de chaque lancement (état, reprise, inventaires avant/après)
```

- Le verdict est calculé sur **tous** les résultats de la session ; l'inventaire « avant / après » compare l'état d'avant le **premier**
  test depuis le démarrage à celui d'après le dernier.
- Un **redémarrage** (nouveau `boot_id` du noyau), un autre `--client` / numéro de série ou **`--new-report`** ouvre une nouvelle session
  (ancien comportement : un dossier horodaté par lancement). `--resume` garde son propre dossier.
- `./cli.py --info-report` écrit les informations système (fiche matériel + inventaire) dans le **même rapport** : si des tests ont déjà été
  lancés depuis le démarrage, leurs résultats sont conservés. Formats : `-e html pdf json csv` (défaut : tous) ; `--no-export` : rien d'écrit.

## Séquences (suites)
Un fichier JSON dans `suites/` (livré avec le logiciel) ou `~/.benchsuite/suites/` (vos séquences) :

```json
{
  "name": "Recette serveur",
  "cooldown_s": 60,
  "steps": [
    {"stress": "cpu_ram_main", "time_s": 600},
    {"bench": "gpu_fryer", "params": {"time_s": 600}},
    {"pause": 60},
    {"fio": "peak_seq1m_q8t1_read", "path": "/mnt/data"}
  ]
}
```
| Étape | Rôle |
|---|---|
| `{"bench": ID, "params": {...}}` | test du catalogue (`--list`) |
| `{"stress": PROFIL, "time_s": N}` | profil stress-ng (le **nom du profil** devient le nom du résultat) |
| `{"fio": PROFIL, "path": "/mnt/data"}` | profil fio sur un disque de **données** (`--fio-path` par défaut) |
| `{"parallel": [étape, étape, …]}` | plusieurs tests **en même temps** (au moins 2 ; pas de pause ni de groupe imbriqué) |
| `{"pause": secondes}` | temps de refroidissement |

## Traçabilité
`--client`, `--operator`, `--order`, `--serial`, `--model`, `--ambient`, `--fan`. Le modèle et le n° de série sont détectés via DMI
(`/sys/class/dmi/id`, sinon `sudo -n dmidecode`). Les résultats sont rangés :
`~/BenchmarkResults/<client>/<n° de série>/<date>_<séquence>/` (rapports CSV/JSON/PDF/HTML, dmesg, `state.json`).

## Reprise après crash / coupure
L'état est écrit après chaque test (`state.json`) et un point de contrôle toutes les 30 s pendant un test.
`--resume DOSSIER` : les tests terminés sont conservés, le test interrompu apparaît dans le rapport comme
« (interrompu) » avec ses données partielles, puis il est rejoué.
Ctrl-C / SIGTERM : arrêt propre, rapports partiels générés, reprise possible.

## Sécurité
- Aucune écriture brute sur le disque système ni sur un disque monté (fio/dd refusés).
- Surveillance matérielle pendant les tests (Xid NVIDIA, MCE, AER, ECC, NVMe, OOM) : panne critique → test arrêté,
  campagne stoppée (sauf `--continue-on-fault`), alerte dans le rapport.
- Vérifications avant test (`--preflight-only` pour les lancer seules, `--no-preflight` pour les ignorer) : espace disque,
  GPU qui répondent, pilote cohérent, GPU froids, tests déjà en cours, droits.

## Verdict automatique (CONFORME / À VÉRIFIER / NON CONFORME / NON ÉVALUÉ)
Les critères changent selon le modèle et certaines limites ne sont écrites nulle part : **tout est optionnel et une limite
absente donne « non évalué » — jamais un échec**. Trois sources, cumulables :

1. **Règles universelles** (toujours actives, sans spec) : test en erreur, panne matérielle, throttling, écart d'un GPU à la
   **médiane** des autres (TFLOPS > 5 % → à vérifier, > 10 % → non conforme ; hotspot +10 / +20 °C), ΔT hotspot−cœur > 20 °C.
2. **Critères** à chaque test : fichier (`--criteria`, modèle via `--criteria-template f.json`) et/ou en ligne :
   `--crit gpu.tflops>=70 "gpu.temp_hotspot_max_c<=95:warn" "test:gpu-fryer.score>=600"`.
   Grandeurs GPU : `tflops, temp_core_max_c, temp_hotspot_max_c, temp_vram_max_c, delta_hotspot_moy_c, power_moy_w, freq_min_mhz…`.
   Matériel attendu (`expected`) : `gpus, gpu_model, gpu_pcie, dimms, ram_gb, nvme, cpu_model, sockets`.
3. **Référence** : lancez une machine validée avec `--save-reference MODELE_X`, puis les suivantes avec `--reference MODELE_X`
   (`--tolerance 5` = ±5 % sur les scores ; hotspot/ΔT comparés en °C). `--references` liste celles enregistrées.

Le verdict apparaît en tête des rapports HTML/PDF, dans le JSON et le CSV. Code de sortie 1 si « NON CONFORME »
(et aussi « À VÉRIFIER » avec `--strict`). L'interface graphique applique les règles universelles à l'export.

## Alléger un rapport HTML existant

```bash
./cli.py --optimize-html rapport_lourd.html [autre.html ...]
```
Régénère chaque rapport (même ancien) avec l'exporteur actuel : courbes réduites à ≈ 800 points (pics et creux conservés), valeurs arrondies, JavaScript rapide (graphiques construits à l'affichage, JSON brut à l'ouverture). Écrit `<nom>_optimise.html` à côté du fichier ; l'original n'est pas modifié. Rien n'est relancé : les données sont relues dans le rapport (résultats, métriques, notes, verdict, alertes matérielles d'origine, sorties brutes, scores). Exemple mesuré : 6,4 Mo → 0,85 Mo, ouverture 2,5 s → 0,3 s, « Résultats détaillés » 9 s → 0,1 s. L'onglet « Électronique » des anciens rapports n'est pas repris (retiré des rapports).

## Synthèse d'un lot de machines
`./cli.py --fleet ~/BenchmarkResults/ACME` : prend le **dernier rapport de chaque machine** (clé = n° de série), affiche le verdict
de chacune et signale celles qui sortent du lot (score > 5 % sous la médiane, hotspot > +5 °C) ; écrit `fleet_summary.csv`
(ouvrable dans un tableur) et `fleet_summary.html`. Il faut au moins 3 machines pour comparer à la médiane.

## Codes de sortie
| Code | Signification |
|---|---|
| 0 | campagne terminée, aucun échec |
| 1 | au moins un test en échec |
| 2 | panne matérielle détectée |
| 3 | erreur d'usage ou vérification avant test bloquante |
| 130 | interrompu (Ctrl-C / SIGTERM) — reprise avec `--resume` |

## Câblage MCIO (étiquette des connecteurs)

Le connecteur d'un GPU est déduit de sa position dans la topologie PCI (et non du numéro de bus, qui change quand on ajoute des SSD ou après un redémarrage).
Priorité : `bb:dd.f` (GPU précis) > `port:bb:dd.f` (root port, recommandé) > `root:xx` (root complex) > profil intégré.

```
./cli.py --mcio-scan                          # GPU, root port et MCIO actuel
./cli.py --mcio-pin                           # fige le mapping actuel par root port
./cli.py --mcio-set port:60:01.1=MCIO5        # corrige/ajoute (valeur vide = efface)
```
Fichier : `~/.benchsuite_mcio.json`. À refaire seulement si le câblage physique change.

## Inventaire et relevés passifs

| Option | Rôle |
|---|---|
| `--inventory [FICHIER]` | affiche l'inventaire matériel (JSON) ou l'enregistre dans FICHIER, puis quitte |
| `--inventory-diff AVANT APRES` | compare deux inventaires (ex. avant/après un redémarrage ou un recâblage) ; code 1 si anomalie |
| `--no-inventory` | pas d'inventaire avant/après (campagne et chaque test) |

Pendant chaque test, les capteurs électroniques (tensions, courants, puissances, ventilateurs, températures de carte) sont relevés
en tâche de fond et repris dans les rapports (`BENCHSUITE_NO_ELECTRICAL=1` pour désactiver).


## Tests en parallèle (séquence automatique)

Un groupe `parallel` lance ses tests simultanément ; la durée de chaque test peut être forcée avec `time_s` (profil stress-ng, test GPU
à `time_s`, profil fio à durée) pour qu'ils chauffent ensemble :

```json
{"name": "Burn complet", "cooldown_s": 60, "steps": [
  {"parallel": [
    {"stress": "mix_30m"},
    {"bench": "gpu_fryer", "time_s": 1800},
    {"fio": "stress_nvme_1h", "time_s": 1800}
  ]},
  {"pause": 60},
  {"bench": "gpu_vram_test", "params": {"time_s": 300}}
]}
```
Exemple livré : `./cli.py --suite burn_parallele --fio-path /mnt/data`. Les mêmes séquences se composent dans l'interface (onglet
« Séquence automatique ») et s'enregistrent dans `~/.benchsuite/suites/`. Un seul relevé électronique + inventaire couvre le groupe ; une
panne matérielle critique ou un arrêt stoppe tous les tests du groupe. Deux tests GPU, ou deux tests fio sur le même disque, dans un même
groupe se partagent la ressource : les mesures sont faussées (avertissement affiché).

## Machine déjà configurée : aucune installation, guide d'installation

**Règle : BenchSuite n'installe et ne désinstalle jamais rien** (paquets, pilotes, toolkits CUDA, snap). Il ne remplace ni ne supprime aucun outil ou dépendance existants ; ses seules écritures sont dans votre dossier personnel (`~/BenchmarkResults`, `~/.benchsuite_*`, `~/.benchsuite/tools`) et dans les dossiers de test que vous choisissez (protection du disque système conservée). Aucun `__pycache__` n'est créé dans son dossier.

En revanche il **lance ce dont les tests ont besoin** : chargement des modules IPMI, démarrage du service DCGM, activation du mode persistance GPU, et les réglages GPU/CPU que vous demandez dans l'interface. Pour tout interdire (machine à ne surtout pas toucher) : `BENCHSUITE_READ_ONLY=1 ./benchsuite.sh` ou `./cli.py --read-only …` ; ces actions sont alors refusées avec un message expliquant la commande à lancer soi-même.

**Guide d'installation adapté à la machine** (lecture seule, n'installe rien) :

```bash
./cli.py --install-guide
```

Il observe l'existant (distribution, droits, pilote NVIDIA et CUDA maximal accepté, GPU et leur architecture, toolkits CUDA présents, gpu-burn et ses bibliothèques, snap, DCGM et son service, dépôt NVIDIA déjà configuré, OpenCL/Vulkan) et ne propose de commandes que pour ce qui manque ou ne marche pas : paquets `apt` avec `--no-upgrade`, compilation de gpu-burn / nvbandwidth dans `~/.benchsuite/tools/` sans droits root (BenchSuite les y retrouve), `cargo install gpu-fryer`, paquet DCGM `-cudaNN` correspondant au pilote, jamais de paquet qui remplace le pilote.
