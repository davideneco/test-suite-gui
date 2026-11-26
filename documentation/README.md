# 📚 Documentation BenchSuite

Bienvenue dans la documentation officielle de **BenchSuite**, suite logicielle d'évaluation matérielle, de stress test et de qualification de serveurs et stations de travail sous Linux (Ubuntu, Debian, CachyOS, Arch Linux).

---

## 🗂️ Sommaire de la documentation

| Document | Description |
| :--- | :--- |
| **[GUIDE_UTILISATION.md](./GUIDE_UTILISATION.md)** | Guide d'installation, lancement de l'interface GTK4 ou du mode CLI headless, et dépendances système. |
| **[ARCHITECTURE.md](./ARCHITECTURE.md)** | Architecture logicielle, modèle multithread non-bloquant, intégration GTK4 / Libadwaita et gestion asynchrone des I/O. |
| **[BENCHMARKS.md](./BENCHMARKS.md)** | Catalogue complet des tests disponibles (CPU, RAM, Stockage NVMe/SATA, GPU CUDA/ROCm/Vulkan/OpenCL, Réseau 1-100GbE). |
| **[TELEMETRIE_ET_CAPTEURS.md](./TELEMETRIE_ET_CAPTEURS.md)** | Télémétrie haute précision, support 24 capteurs multi-GPU, intégration LACT, mapping MCIO, détection active du throttling et calcul du $\Delta T$. |
| **[EXPORTS_ET_RAPPORTS.md](./EXPORTS_ET_RAPPORTS.md)** | Spécifications des 4 formats de rapport exportés (HTML interactif Chart.js, PDF ReportLab, JSON structuré et CSV). |
| **[CLI.md](./CLI.md)** | Mode ligne de commande : séquences de tests, traçabilité par client/série, reprise après crash, sécurité, codes de sortie. |

---

## 🔗 Liens & Références Officielles Constructeur
- **NVIDIA SMI Documentation Officielle :** [https://docs.nvidia.com/deploy/nvidia-smi/index.html](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
- **NVIDIA Management Library (NVML) :** [https://docs.nvidia.com/deploy/nvml-api/index.html](https://docs.nvidia.com/deploy/nvml-api/index.html)

---

## 🚀 Démarrage Rapide

### Mode Graphique (GUI)
```bash
./benchsuite.sh
```

### Mode Ligne de Commande (CLI / Headless)
```bash
./cli.py --list
./cli.py --run gpu_burn,storage_fio_nvme --export-all
```

---

## ⚙️ Normes et Bonnes Pratiques
- **Zéro blocage d'interface :** Tout processus lourd s'exécute dans un thread isolé (`threading.Thread(daemon=True)`) et met à jour les widgets GTK via `GLib.idle_add()`.
- **Résilience matérielle :** Gestion sécurisée des privilèges (lecture seule sans root via `sysfs`, élévation à la demande via Polkit / `sudo -n`, interdiction absolue d'écriture destructive sur disques OS).

> La documentation officielle des outils tiers (stress-ng, fio, LACT…) est conservée à part, sur la branche `docs-outils`, pour garder le logiciel léger.
