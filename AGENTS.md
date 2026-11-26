# BenchSuite — Instructions pour les agents IA (AGENTS.md)

Directives, architecture et règles de comportement de tout agent IA (Claude, Codex…) qui travaille sur **BenchSuite**.
Langue de travail : **français** (échanges, docstrings, commentaires, commits, changelog).

---

## 1. Philosophie

- **Open source uniquement :** les tests s'appuient sur des outils libres (stress-ng, fio, gpu-burn, gpu-fryer, nvbandwidth, DCGM, clpeak, vkpeak, STREAM, iperf3…). But : bencher des serveurs et machines **à la chaîne**.
- **Compatibilité universelle :** Intel, AMD, NVIDIA, ARM ; NVMe/SATA/HDD ; NIC 1/10/100 GbE. Aucune exclusive de marque.
- **Best-effort, zéro crash :** root absent, binaire manquant (`nvme-cli`, `ipmitool`, `stress-ng`, `dcgmi`…), GPU absent → intercepter, replier sur une alternative ou masquer proprement la section. Jamais de traceback utilisateur.
- **Limite inconnue ≠ échec :** sans spécification fiable, un critère est « non évalué ». Ne jamais inventer un seuil constructeur.
- **UI dynamique :** n'afficher que les métriques détectées ; pas de placeholders vides ni de « N/A » inutiles.
- **Options cliquables :** préférer listes, cases à cocher et menus aux zones de texte ; la ligne de commande libre reste dans « Options avancées ».
- **Dépôt logiciel léger :** uniquement le logiciel. Documentation des outils tiers, binaires et gros fichiers vivent ailleurs (branche `docs-outils`). Éviter toute croissance inutile du dépôt.
  **Règle :** toute documentation d'un outil tiers trouvée en cours de travail (README, guide, page de manuel, doc noyau, notes de recherche) est poussée sur la branche **`docs-outils`** (dossier `outils/<outil>/`, en-tête `<!-- Source officielle : URL — récupéré le AAAA-MM-JJ -->`, ligne ajoutée dans `outils/README.md`), jamais sur la branche de travail.
- **Honnêteté :** dire ce qui n'a pas pu être testé (pas de GPU/disque réel dans l'environnement de développement) ; ne jamais présenter une hypothèse comme un fait (ex. nom des connecteurs MCIO).

---

## 1 bis. OS cibles et priorité des tests

- **OS principal : Linux — Ubuntu.** Toute fonctionnalité, commande d'installation et message d'erreur doit être valable sur la famille `apt` (Ubuntu / Debian). Les autres distributions (CachyOS, Arch…) restent supportées en best-effort mais ne sont pas la cible.
- **Priorité des tests : chauffe / stabilité / erreurs.** Thermique (températures, throttling, puissance), stabilité sous charge longue, erreurs matérielles (Xid, MCE, AER, ECC, NVMe, OOM, CRC). C'est ce qui guide le choix des tests par défaut, l'ordre des séquences, le verdict et la mise en avant dans les rapports.
- **Performance en second temps, uniquement si demandée.** Débits, bandes passantes, scores : ne pas ajouter de test de performance aux séquences par défaut ni le placer avant les tests chauffe/stabilité/erreurs sans demande explicite ; ne jamais sacrifier la capture thermique ou la détection d'erreurs au profit d'une mesure de performance.
- **Conséquence pour les propositions (§2) et les revues :** les points chauffe / stabilité / erreurs (robustesse, métriques matérielles) passent avant les gains de performance.

---

## 2. Avant de modifier : proposer des améliorations

Pour une demande **ouverte** (nouvelle fonctionnalité sans cahier des charges précis), analyser le code cible et proposer **3 à 5 améliorations concrètes** avant d'implémenter :
1. **Robustesse** : repli universel, timeouts (`subprocess.run(timeout=…)`), parsing de `stderr`.
2. **UI/UX GTK4** : spinners, barres de progression, design compact, pas de débordement.
3. **Métriques matérielles** : throttling, limites thermiques/puissance, erreurs CRC/ECC.
4. **Exports** : cohérence JSON / CSV / PDF / HTML dans `exporters/`.
5. **Performance & concurrence** : découplage strict I/O ↔ UI.

Si la demande de l'utilisateur est déjà précise et détaillée, l'exécuter directement et signaler seulement les risques ou points non couverts.

---

## 3. Architecture

```
main.py              Interface GTK4/Libadwaita (accueil, pages de tests, Options avancées)
cli.py               Mode CLI : séquences, reprise, verdict, MCIO, fleet, codes de sortie
core/
  benchmarks.py      Façade rétrocompatible + CATALOG, stress/FIO, rapport matériel
  bench_utils.py     Utilitaires d'exécution, cache d'outils, infos système, séries GPU
  bench_sensors.py   Collecte capteurs CPU/GPU/SSD/BMC/powerstat pendant les tests
  bench_net.py       Tests réseau iperf3 TCP/UDP et détection boucle locale
  bench_nvidia.py    Tests GPU NVIDIA (gpu-burn, gpu-fryer, nvbandwidth, intégrité VRAM)
  bench_compute.py   Tests compute/bande passante (STREAM, VRAM BW, DCGM, clpeak, vkpeak, OpenCL burn)
  changelog.py       Historique des versions affiché sur la page d'accueil (CHANGELOG_UPDATES)
  runner.py          Séquences (suites/*.json), CampaignRunner (étapes en série ou groupes `parallel`), état/reprise (state.json)
  campaign.py        Modèle « blocs » de l'onglet Séquence ↔ étapes du moteur, durées estimées, rapports de fin de campagne
  stress_profiles.py Profils stress-ng (grille CPU/RAM/mixte × 5 min/30 min/4 h, Personnalisé, CLI)
  fio_profiles.py    Profils fio (existants + « pro » JESD219/SNIA/CDM/IOMeter/HDD) et commande
  fio_runner.py      Exécution fio (Popen, arrêt réel, parsing JSON, suites)
  verdict.py         Verdict automatique, critères, machine de référence
  fault_monitor.py   Surveillance des pannes matérielles (décorateur with_fault_watch)
  safety.py          Protection du disque système
  preflight.py       Vérifications avant test
  identity.py        Identité machine (DMI) et traçabilité
  gpu_stats.py       Statistiques GPU en charge stabilisée (ΔT, P95…)
  fleet.py           Synthèse d'un lot de machines
  inventory.py       Inventaire matériel avant/après (PCI, liens, AER/ECC, NIC, SEL) et comparaison
  electrical.py      Relevé passif des capteurs électroniques pendant chaque test (BMC, hwmon, PMBus, RAPL)
  theme.py           Thème clair / sombre / auto : la feuille claire est DÉDUITE de la sombre (luminosité inversée) ; réglage ~/.benchsuite/settings.json
  system_info.py     Infos matériel, LACT, mapping MCIO (par root port PCI)
  tools_catalog.py   Catalogue des outils/dépendances (rôle, détection, commandes à copier, liens) → page « Outils & dépendances » et documentation/OUTILS_ET_DEPENDANCES.md (regénérer avec `to_markdown()`)
  install_advisor.py Guide d'installation adapté à la machine (`./cli.py --install-guide`, lecture seule)
  privileged.py      Exécution avec privilèges (sudo -n sinon pkexec), une seule exécution par commande
  redfish_client.py  Capteurs BMC par Redfish (identifiants validés sur /Chassis, sonde mémorisée)
ui/                  Pages GTK : séquence automatique, stress, fio, ssd, nvidia, cpu, dmesg, infos système
exporters/
  export.py          Façade rétrocompatible + JSON, CSV, dmesg, export_all
  common.py          Métadonnées rapport, résumés matériels, compactage JSON
  history.py         Historique local ~/.benchsuite_history.json
  pdf_report.py      Génération PDF (ReportLab) et graphiques vectoriels
  html_report.py     Génération HTML (Chart.js), thèmes clair/sombre, ré-export
suites/              Séquences JSON livrées
tests/               Tests unitaires (`python3 -m unittest discover -s tests -v`)
third_party/         Sources tierces minimales (STREAM, Chart.js) — voir THIRD_PARTY_NOTICES.md
windows_viewer/      Compagnon Windows (BDD SQLite benchsuite.db + visionneuse web locale)
documentation/       Docs utilisateur (CLI, benchmarks, architecture, exports…)
```

Règles :
- Tout nouveau test = une fonction `bench_*` + une entrée `CATALOG` (id, nom, catégorie, paramètres) ; ajouter `@with_fault_watch` pour les tests longs.
- Un test long accepte `cb` (progression), `kill_event` ou `stop_flag_fn` (arrêt propre) et alimente la télémétrie par séries.
- Un résultat porte le **nom du profil** exécuté (pas un nom générique).
- La recherche d'un outil passe par `tool_exists()` / `_find_tool_path()` (résultat mémorisé ; `forget_tool_cache()` après une installation). Les commandes d'installation viennent de `core/tools_catalog.py` (apt) : ne pas réécrire de commandes ailleurs.
- La collecte d'infos système (`collect_full_system_info`) exécute chaque section en parallèle avec un budget de temps : toute nouvelle section y est ajoutée avec son budget.
- Les anciens identifiants de profils/étapes doivent continuer à fonctionner (compatibilité des séquences existantes) : masquer plutôt que supprimer.

---

## 4. Interface GTK4 / Libadwaita

- **Compacte, sans débordement :** grille à 2 colonnes (`Gtk.Grid`) ou `Gtk.FlowBox` plutôt qu'une pile verticale de 8+ lignes. Boutons d'action (`▶ Lancer`, `⏹ Stopper`, `💾 Exporter`) épinglés en haut ou dans une barre fixe. Sidebars de 200 à 220 px.
- **Identité visuelle (thème sombre/orange) :** fond `#0d0d0d`, cartes `#141414`, bordures `#2a2a2a`, accent `#ff6600` / `#f97316`, succès `#a6e3a1`, erreur `#f38ba8`, avertissement `#f9e2af`.
- **Markup Pango :** utiliser `<tt>`, `<b>`, `<i>` — **jamais `<code>`**. Échapper les `&` (`&amp;`). Valider les textes d'accueil avec `Pango.parse_markup`.
- **Thème choisissable (sombre par défaut) :** bouton « 🌙 Sombre / ☀ Clair / 🖥 Auto » dans l'en-tête, mémorisé (`core/theme.py`). Une règle CSS ajoutée au thème sombre est reprise automatiquement en clair : ne pas écrire de seconde feuille à la main ; pour un texte qui doit rester blanc sur fond d'accent, l'ajouter à `LIGHT_EXTRA`. Éviter les couleurs claires codées en dur dans le markup Pango (utiliser une classe CSS). Le rapport HTML a le même bouton (feuille claire dérivée du gabarit, `_add_light_theme`).
- **Thème forcé sombre par défaut** (`Adw.ColorScheme.FORCE_DARK` dans `BenchApp._on_activate`) : sinon Ubuntu (thème clair) laisse des zones blanches. Menu latéral : recherche, groupes repliables, icônes, libellés tronqués avec info-bulle ; pages de test : **en-tête fixe** avec Lancer / Stopper / Exporter ; outil manquant → commande copiable. Tout `set_draw_func` est gardé par le drapeau cairo (sans `python3-gi-cairo`, GTK lèverait une erreur à chaque dessin).
- **Compatibilité Python 3.10+** : pas de f-string imbriquée avec mêmes guillemets ; vérifier avec l'interpréteur le plus ancien supporté.

---

## 5. Concurrence

- **Zéro blocage GTK :** toute commande système, I/O ou parsing lourd s'exécute dans `threading.Thread(daemon=True)`.
- **Synchronisation UI :** toute modification de widget depuis un thread secondaire passe par `GLib.idle_add(func, …)`.
- Chaque appel `subprocess` : `try/except` + **timeout explicite** ; arrêt propre des arbres de processus.

---

## 6. Privilèges et sécurité

1. **Lecture sans privilège d'abord** (`journalctl -k` plutôt que `dmesg`, `/sys/class/thermal/…`).
2. **Élévation à la demande** (`pkexec` / `sudo -n`) via un bouton « Mode Admin », sans bloquer le mode lecture seule.
3. **Jamais d'écriture destructive sur le disque de l'OS ni sur un disque monté.** Passer par `core/safety.py` (`check_targets`, `is_write_mode`) avant tout test d'écriture ; vérifier `os.access(path, os.W_OK)`.
4. **Binaires absents :** `shutil.which` / `tool_exists()` ; désactiver le lancement et afficher la commande d'installation (`sudo apt install …`).
5. **Pas de snap pour DCGM** (confinement : ne voit pas les GPU) — paquet apt NVIDIA `datacenter-gpu-manager-4-cudaNN` correspondant au CUDA du pilote.

---

## 7. Spécificités matérielles à respecter

- **GPU :** statistiques en charge stabilisée (puissance ≥ 50 % du max, 10 premières secondes ignorées) ; multi-GPU obligatoire (liste cliquable des GPU cibles).
- **MCIO :** Linux ne connaît pas les connecteurs. Étiquettes issues du **root port PCI** (`~/.benchsuite_mcio.json`, `--mcio-scan/pin/set`) ; la table intégrée est marquée « (supposé) ».
- **DCGM :** seuil `pcie.min_pci_width` adapté à la liaison réelle (x4/x8 MCIO) ; erreur « No available testing entities » → réessai avec `-i` explicite, puis diagnostic (service périmé, paquet cudaNN incompatible, snap).
- **fio :** profils « pro » = standards publics (JEDEC JESD219, SNIA PTS, CrystalDiskMark, IOMeter…) ; les réglages avancés d'un profil (`bssplit`, `random_distribution`, `loops`, `fio_extra`…) doivent survivre au passage par l'interface.
- **Pannes :** le moniteur coupe le test sur Xid/MCE/AER/ECC/NVMe/OOM ; exit code `2` en CLI.
- **GPU Intel :** `core/intel_gpu.py` interroge `xpu-smi` (JSON `-j` ou tableau, `sudo -n` puis direct) pour nom, mémoire, UUID, températures cœur/VRAM, puissance, fréquence, utilisation ; sans xpu-smi, repli sysfs et remarque `intel_xpu`. Ne jamais afficher la température du CPU comme celle d'un GPU.
- **Relevés passifs :** `with_fault_watch` ajoute à chaque test (le plus externe seulement) le relevé électronique (`metrics.electrical`), les liens PCIe relevés pendant le test (`metrics.pcie_links`, `core/pcie_links.py`) et l'inventaire avant/après (`metrics.inventory_diff`) ; ce ne sont pas des tests : aucune note. Le relevé électronique et les courbes de séquence restent dans le JSON mais ne sont **pas affichés** dans HTML / PDF / CSV (`EXPORT_SEQUENCE_ELECTRONICS = False` dans `exporters/export.py`) ; l'inventaire avant/après reste affiché.

---

## 8. Checklist de modification

**Dépendances & erreurs**
- [ ] Présence des outils vérifiée ; message d'installation si absent, avec la commande **Ubuntu (`apt`)**.
- [ ] Chauffe / stabilité / erreurs traitées avant toute considération de performance (§1 bis).
- [ ] Chaque `subprocess` : `try/except` + timeout.

**Multi-périphériques & exports**
- [ ] Fonctionne avec plusieurs GPU / disques / interfaces réseau.
- [ ] Exports cohérents (`exporters/export.py`), historique `~/.benchsuite_history.json`.

**Qualité**
- [ ] Docstrings et commentaires en français pour toute nouvelle fonction/classe.
- [ ] Lisible comme le code voisin (densité de commentaires, nommage).
- [ ] Documentation mise à jour (`documentation/` ; `CLI.md` si option CLI).

---

## 9. Protocole de validation, version et livraison

Une tâche n'est terminée qu'après :

1. **Syntaxe :** `python3 -m py_compile <fichiers modifiés>` (et avec l'interpréteur le plus ancien supporté si disponible).
2. **Environnement :** `./check_environment.py` — aucune régression (les blocages dus à des outils absents du conteneur de dev sont attendus).
3. **Instanciation GTK :** sous `xvfb-run`, instancier la fenêtre, les pages de tests du catalogue (`BenchPage`), `StressPage` et `FioPage` sans erreur ; valider le markup Pango du changelog.
4. **Test fonctionnel avec outils factices** quand le matériel réel est absent (faux `nvidia-smi`, `dcgmi`, `fio`…) : montrer la commande construite et le parsing.
5. **Version & accueil :** `./bump_version.sh patch`, puis **ajouter l'entrée** correspondante en tête de la liste `CHANGELOG_UPDATES` dans `core/changelog.py` (affichée dans le bloc changelog de `main.py` : nouveautés + objectif de la version).
6. **Commit & push :** commit explicite en français (`feat:`, `fix:`, `refactor:`, `docs:`) **incluant le numéro de version**, poussé sur la **branche de travail courante** (`git push -u origin <branche>`). Ne jamais pousser sur une autre branche sans accord ; ne pas ouvrir de pull request sans demande explicite.

6 bis. **Tag de version :** après le push, `./tag_release.sh` crée et pousse le tag `v<version>` (lue dans `main.py`). GitHub fournit alors `…/archive/refs/tags/v<version>.zip`, dont le nom de fichier contient la version (le « Download ZIP » d'une branche garde toujours le même nom). Ne jamais déplacer un tag existant : incrémenter la version.

7. **Pas d'archive à joindre :** le push sur GitHub suffit. Ne pas envoyer de zip ; `./make_zip.sh` (→ `dist/BenchSuite-v<version>.zip`, ignoré par git) reste disponible sur demande.

Messages de commit : terminer par les lignes d'attribution demandées par l'environnement de session.

---

## 10. Communication avec l'utilisateur

- Réponses en français, claires, sans jargon inutile ; commandes à copier dans des blocs de code.
- Dire d'abord ce qui est établi, puis ce qui reste une hypothèse ; demander la sortie exacte (`lspci`, `dcgmi discovery -l`, `nvidia-smi -L`…) plutôt que de deviner.
- Signaler honnêtement les limites de test (aucun GPU/SSD réel côté développement).

