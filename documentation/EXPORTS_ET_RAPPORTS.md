# 📊 Exports et Rapports de Qualification

Ce document décrit les spécifications et fonctionnalités des 4 formats de rapport générés par le module `exporters/export.py`.

---

## 1. Rapport Web Interactif (HTML + Chart.js)

Le rapport HTML est un fichier autonome (`.html`) embarquant ses feuilles de style ainsi que Chart.js et son greffon de zoom directement dans le document (`third_party/chartjs/`, avec repli CDN si absent) : tous les graphiques fonctionnent hors ligne sur des serveurs isolés sans accès Internet.

### Fonctionnalités avancées :
- **Onglet Vue d'Ensemble :** Synthèse du statut des tests, audit d'intégrité matérielle et bandeau de **Synthèse Thermique & Fréquences**.
- **Graphiques Interactifs :**
  - Zoom interactif au clic-glisser et réinitialisation au double-clic.
  - Séries différentiées par GPU physique (couleur unique par GPU, style de tracé selon la sonde : trait plein pour Cœur, tirets pour Hotspot, pointillés pour VRAM).
  - Filtres rapides en un clic : `🔘 Tous`, `🔥 Cœurs`, `⚡ Hotspots`, `💾 VRAM`, `👁️ Masquer tout`.
  - Bouton interactif **`⚡ Seuil constructeur (Off / Actif)`** permettant d'afficher ou masquer la ligne rouge de référence à la demande.
- **Traçabilité des Commandes :** Chaque benchmark affiche la commande système exacte exécutée (`fio`, `gpu-fryer`, etc.) avec un bouton de copie rapide dans le presse-papiers (`📸 Copier`).
- **Légèreté (multi-GPU, longues durées) :** les courbes du HTML sont réduites à ≈ 800 points (un échantillon par paquet, celui qui s'écarte le plus de la moyenne : pics et creux conservés), arrondies à 2 décimales, sans doublons ; les graphiques sont sans animation ni lissage, ceux de « Résultats détaillés » ne se construisent qu'à l'affichage, et l'onglet « JSON brut » n'est construit qu'à l'ouverture (tableaux numériques résumés). Mesuré sur 8 GPU × 12 capteurs × 1 h × 3 tests : 11,2 Mo → 1,1 Mo, ouverture 4,5 s → 0,3 s, onglet Graphique 4,9 s → 0,2 s, JSON brut 25 s → 0,1 s. Les séries **complètes** restent dans le fichier `.json` exporté. Variable `BENCHSUITE_HTML_MAX_POINTS` (800 par défaut, `0` = aucune réduction).
- **Éditeur de Notes Intégré :** Champ de notes persistant via `localStorage` avec téléchargement du rapport annoté (`benchsuite_report_notes.html`).
- **Outil de Comparaison Multi-Rapports :** Permet d'importer plusieurs fichiers HTML pour superposer les courbes temporelles de plusieurs serveurs ou avant/après modification.

---

## 2. Rapport Professionnel PDF (ReportLab)

Généré pour les livrables clients et les dossiers d'homologation :
- **Thème Sombre & Orange Signature :** Mise en page exécutive aux normes visuelles BenchSuite.
- **Tableau Récapitulatif :** Métriques synthétiques, durées et statuts de validation.
- **Graphiques Temporels Intégrés :** Rendu vectoriel haute fidélité via ReportLab `Drawing` et `PolyLine`.
- **Alignement Rigoureux des Échelles :** Calcul mathématique exact des bornes sans distorsion des courbes par rapport à la grille.

---

## 3. Données Structurées JSON

Destiné à l'automatisation, à l'ingestion dans des bases de données de métrologie ou aux pipelines CI/CD :
```json
{
  "benchsuite_version": "1.1.5",
  "exported_at": "2026-09-29T16:15:00",
  "metadata": {
    "system_name": "Serveur-Calcul-01",
    "operator": "Laboratoire",
    "client": "Production"
  },
  "summary": {
    "bench_count": 5,
    "ok": 5,
    "failed": 0
  },
  "telemetry_summary": {
    "gpus": [
      {
        "gpu_index": 0,
        "temp_core_max_c": 64.2,
        "temp_hotspot_max_c": 78.5,
        "temp_vram_max_c": 68.0,
        "delta_hotspot_max_c": 14.3,
        "power_max_w": 298.5,
        "throttle_detected": false,
        "throttle_samples": 0
      }
    ]
  },
  "results": [...]
}
```

---

## 4. Données Tabulaires CSV

Fichier structuré en quatre sections (`metadata`, `system`, `telemetry_summary`, `benchmark` / `metric`) directement ouvrable dans LibreOffice Calc ou Microsoft Excel.
- Support du mode standard (avec séries temporelles) ou du mode condensé (`condensed=True`) pour des analyses statistiques directes.

## Thème clair / sombre
- **Interface graphique :** bouton « 🌙 Sombre / ☀ Clair / 🖥 Auto » dans l'en-tête (cycle), choix mémorisé dans `~/.benchsuite/settings.json`. `BENCHSUITE_THEME=dark|light|auto` l'impose sans le modifier. La feuille claire est déduite de la sombre (luminosité de chaque couleur inversée, orange d'accent conservé) : aucune règle en double. Les graphiques gardent leur fond sombre.
- **Rapport HTML :** même bouton en haut de page ; choix mémorisé dans le navigateur, ouverture dans le thème choisi dans le logiciel. Le fichier « HTML avec notes » est toujours enregistré avec les couleurs d'origine.

## Onglets de métriques (HTML)
- **Débit**, **IOPS** et **Latence** sont séparés par l'**unité** de la valeur : `(MiB/s)` → Débit ; `(IOPS)` → IOPS (échelle propre) ; `(ms)` → Latence. Une suite (SNIA…) enregistre pour chaque sous-test débit, IOPS **et** latences moyenne / p99 / p99.9 / max.
- **Tests RAM :** les courbes sont nommées « RAM — … » ou « CPU — … » ; l'onglet indique s'il contient la RAM, le CPU ou les deux (« Puissance CPU (pas de capteur RAM) » quand aucun capteur de puissance des barrettes n'existe). `ram_vitesse_mts` = vitesse **configurée** lue dans le firmware (dmidecode, root), pas une mesure.
- **RAM utilisée :** les tests qui chargent la mémoire (`stress-ng --vm`, profils RAM et mixtes) relèvent la RAM utilisée chaque seconde (`series_ram_used_gb`) et son maximum (`ram_used_max_gb`, `ram_pct_max`) : onglet « RAM ».

## Copier un graphique (rapports HTML)
Bouton **📷 Copier le graphique** (et **💾 PNG**) au-dessus de chaque graphique : onglet « Graphique », cartes de chaque test, comparaison. L'image contient le titre, la légende et les courbes sur un fond uni du thème courant (clair ou sombre) ; collez-la directement (Ctrl+V) dans Word, Teams, un mail… Si le navigateur refuse l'accès au presse-papiers, l'image est téléchargée en PNG. Aucune bibliothèque supplémentaire.

## `csv2html.py` — CSV quelconque → rapport HTML
`python csv2html.py fichier.csv [autre.csv …] [-o dossier] [-s "Nom"] [--theme dark|light|auto]` (mêmes graphiques, bouton thème et bouton de copie que les rapports BenchSuite).
- **Encodage** : UTF-8 (avec ou sans BOM), UTF-16, Windows-1252 (Excel), Latin-1. **Séparateur** : `;` `,` tabulation `|` (détecté). Lignes de commentaires / métadonnées avant le tableau ignorées.
- **En-tête** présent ou non ; **nombres** à virgule ou point décimal, milliers, unité collée (`350,5 W`, `41 °C`) ; **colonne de temps** détectée (date ISO, JJ/MM/AAAA, heure seule — passage de minuit géré —, secondes, millisecondes, époque Unix) ; sans colonne de temps : 1 ligne = 1 seconde (dit à l'écran).
- Chaque colonne va dans l'onglet de son nom (puissance, température, fréquence, charge, débit, latence) ; les autres dans « Valeurs » ; les colonnes de texte sont ignorées (listées). Les valeurs manquantes sont complétées par la dernière valeur connue **et comptées** à l'écran.
- Un seul CSV → `<nom>_rapport.html` ; plusieurs → un rapport avec une vue superposée. Fichiers Excel : enregistrer en CSV d'abord.
- **Banc d'alimentation (PSU) :** si le CSV a des colonnes `Pin` et `Pout`, onglets Synthèse / Graphique / Résultats détaillés / JSON brut ; synthèse par alimentation (PSU 1, PSU 2… ; `--noms "PSU A,PSU B"` pour les nommer ; max, moyenne, P50, P95, min, pertes, rendement moyen et intégré, énergies en kWh par intégration sur le temps réel). Les dates compactes `AAAAMMJJTHHMMSS` et les octets nuls en fin de fichier sont acceptés.

## Résultats fio d'une suite (SNIA, Peak, CrystalDiskMark…)
- HTML, PDF et CLI affichent un tableau **« un test par ligne »** : débits (MiB/s), IOPS, latences moyenne et p99 (ms) en lecture et en écriture. Les autres latences (p99.9, max) restent listées dans les métriques ; CSV et JSON gardent toutes les clés.
- La **vraie commande fio** est enregistrée (`metrics.command`, une par sous-test pour une suite). Rien n'est reconstitué pour un ancien rapport sans commande enregistrée.
- `jobs_details` (comparaison de plusieurs disques) n'est produit que s'il y a plusieurs disques ; avec un seul disque ses valeurs étaient un doublon des métriques de tête.


## Historique (`~/.benchsuite_history.json`)
Chaque test terminé est ajouté à l'historique (500 entrées au plus, sortie brute plafonnée à 20 000 caractères, écriture atomique ; un fichier illisible est renommé `….illisible-<date>` au lieu d'être écrasé).
Page **Rapports → Historique des exports** : une case à cocher par ligne (ou Ctrl / Maj + clic, ou « Tout sélectionner ») pour **supprimer plusieurs tests à la fois** ou **exporter en HTML seulement les tests cochés** ; sans sélection, l'export prend tout l'historique. L'export tourne en arrière-plan ; en cas d'échec, le message s'affiche et le détail est écrit dans `BenchmarkResults/export_historique_erreur.log`.

## Lecture d'un rapport : ce qui est regroupé ou masqué
- **Noms distincts :** deux tests de même nom dans un rapport reçoivent leur durée (« GPU gpu-burn (60 s) » / « (30 min) »), sinon leur rang (« #2 »).
- **Verdict :** une alerte identique relevée pendant plusieurs tests (lien PCIe, événement BMC) n'apparaît qu'une fois, avec la liste des tests concernés.
- **Compteurs :** « Échecs » = tests en erreur ; les tests interrompus ou ignorés ont leur propre carte « Interrompus ».
- **Sortie brute :** lignes consécutives identiques regroupées (« ligne (×N) ») ; au-delà de 30 000 caractères (HTML) on garde le début et la fin, jamais seulement le début.
- **Mesures partagées :** quand plusieurs disques sont testés ensemble (un seul fio), les liens PCIe, l'inventaire et le relevé électronique sont attachés au premier disque ; les autres portent la mention `observations_groupe`.
- **Tableaux de métriques :** pas de ligne vide ni de doublon exact ; le « score » d'une suite fio est le meilleur débit d'un sous-test (lecture ou écriture).
- **Horloge :** si un événement de la BMC est daté après l'instant du relevé, une alerte signale que l'horloge de la machine ou celle de la BMC est fausse.

---

## Consultation centralisée sous Windows (`windows_viewer/`)

Pour regrouper et explorer sous Windows tout un dossier de rapports BenchSuite (`.json`, `.html`, `.pdf`, `.csv`, `dmesg_*.txt`, `fleet_summary.*`, `state.json`), utilisez [`windows_viewer/benchsuite_viewer.py`](../windows_viewer/benchsuite_viewer.py) (voir [`windows_viewer/README_WINDOWS.md`](../windows_viewer/README_WINDOWS.md)) :
- **Aucune dépendance externe** (Python 3.10+ standard uniquement ; lanceur double-clic `Lancer_BenchSuite_Viewer.bat`).
- Maintient automatiquement une base SQLite **`benchsuite.db`** dans le dossier choisi (indexation incrémentale des rapports, benchmarks et fichiers).
- Ouvre une interface locale sur `127.0.0.1` mettant en avant verdicts, pannes matérielles, throttling, températures max, aperçu `dmesg` (erreurs surlignées), aperçu CSV (`UTF-8` / `Windows-1252`), graphiques des séries JSON complètes (Chart.js hors ligne) et export CSV parc / catalogue HTML autonome.
