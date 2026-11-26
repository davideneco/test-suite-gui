# BenchSuite Viewer pour Windows (BDD SQLite + Visionneuse locale)

Outil léger sans dépendance externe pour consulter sur un PC Windows **tous les fichiers générés par BenchSuite** stockés dans un même dossier (`benchsuite_report_*.json`, `.html`, `.pdf`, `.csv`, `dmesg_*.txt`, `fleet_summary.*`, `state.json`, etc.).

---

## Prérequis sur le PC Windows

- **Python 3.10 ou plus récent** uniquement (aucun `pip install` requis : tout repose sur la bibliothèque standard Python).
  - Si vous n'avez pas les droits administrateur pour un installeur classique :
    - installez **Python** depuis le **Microsoft Store**, ou
    - décompressez **Python embeddable** (version portable sans installation).
- Un navigateur standard (Microsoft Edge, Google Chrome, Firefox).

---

## Utilisation rapide (Windows)

1. Copiez le dossier `windows_viewer\` (et idéalement `third_party\chartjs\` au même niveau parent si vous voulez les courbes JSON complètes 100 % hors ligne) sur votre PC Windows.
2. Double-cliquez sur **`Lancer_BenchSuite_Viewer.bat`**.
3. Sélectionnez le dossier contenant vos rapports BenchSuite (le choix est mémorisé pour la fois suivante).
4. L'outil crée/met à jour **`benchsuite.db`** (base SQLite) dans ce dossier et ouvre automatiquement votre navigateur sur `http://127.0.0.1:<port>/`.

Vous pouvez aussi passer directement le chemin du dossier en ligne de commande ou glisser-déposer le dossier sur le `.bat` :

```bat
python benchsuite_viewer.py "D:\Rapports_BenchSuite"
```

Options utiles :
- `--scan-only` : met à jour `benchsuite.db` sans lancer d'interface web (pratique en tâche planifiée).
- `--db "C:\chemin\ma_base.db"` : place la base SQLite ailleurs que dans le dossier des rapports (utile si le dossier des rapports est en lecture seule).
- `--export-html "D:\catalogue.html"` : génère un tableau de bord HTML statique autonome ouvrable sans Python.
- `--port 8765` : impose le port local sur `127.0.0.1`.

---

## Ce que vous obtenez

- **1 ligne = 1 rapport, avec un onglet dédié par rapport :** chaque ligne de la liste principale correspond à un rapport unique (même si plusieurs serveurs/nœuds partagent un même numéro de série châssis). Cliquez sur un rapport pour ouvrir son propre onglet contenant **uniquement les fichiers de ce rapport** (`.json`, `.html`, `.pdf`, `.csv`, `dmesg`), avec aperçu intégré sans jamais mélanger les fichiers. Vous pouvez ouvrir plusieurs onglets en parallèle, fermer un onglet avec `✕`, ou revenir à la liste avec `← Retour aux rapports`.
- **Tri et recherche instantanés :** cliquez sur n'importe quel en-tête de colonne (`Date`, `Machine / Dossier`, `N° Série / Client`, `Verdict`, `Chauffe & Erreurs`, `Tests / Fichiers`, `Ouvrir`) ou utilisez le menu `Tri : …` pour classer vos rapports dans l'ordre souhaité.
- **Priorité chauffe / stabilité / erreurs :** les serveurs `NON CONFORME` / `À VÉRIFIER`, pannes matérielles, throttling GPU, anomalies PCIe et écarts d'inventaire sont mis en avant dans la liste et triables d'un clic.
- **Base de données SQLite (`benchsuite.db`) :** indexation incrémentale (seuls les fichiers nouveaux ou modifiés sont relus), avec 3 tables principales (`reports`, `benchmarks`, `files`) exploitables aussi directement dans **DB Browser for SQLite** ou PowerBI/Excel.
- **Tous les fichiers visibles et ouvrables au même endroit :**
  - `.html` et `.pdf` : ouverture directe dans le navigateur ;
  - `.csv` : aperçu tabulaire avec détection automatique d'encodage (`UTF-8` / `Windows-1252`) et de séparateur (`;`, `,`, tabulation) ;
  - `dmesg_*.txt` : visionneuse de journal avec surlignage rouge des erreurs noyau (`Xid`, `MCE`, `AER`, `ECC`, `NVMe`, `I/O error`, `OOM`…) ;
  - `.json` complets : lecture du verdict, synthèse thermique GPU, commandes exactes des tests, courbes complètes (si `third_party/chartjs/` est présent) et JSON formaté ;
  - **Export CSV parc (`;`, UTF-8 BOM)** prêt pour Excel, et **Export Catalogue HTML autonome**.
