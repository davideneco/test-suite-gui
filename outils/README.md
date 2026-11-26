# Documentation officielle des outils utilisés par BenchSuite

Copies locales des documents de référence des outils appelés par BenchSuite (README, guides, pages de manuel),
pour consultation hors ligne. Chaque fichier commence par l'URL de sa source officielle et la date de récupération.

| Outil | Usage dans BenchSuite | Fichiers |
|---|---|---|
| **CPU / RAM — stress-ng** | Profils CPU + RAM, Burn RAM (`--vm`, `--vm-method`, `--vm-populate`, `--vm-keep`) | [README.md](./stress-ng/README.md), [stress-ng.txt](./stress-ng/stress-ng.txt) |
| **CPU — powerstat** | Consommation électrique mesurée | [README.md](./powerstat/README.md) |
| **CPU — sysbench** | Benchmark CPU événements/s | [README.md](./sysbench/README.md) |
| **CPU — turbostat** | Fréquences, C-states, puissance CPU | [turbostat.txt](./turbostat/turbostat.txt) |
| **Capteurs — ipmitool** | BMC / IPMI | [README](./ipmitool/README) |
| **Capteurs — lm-sensors** | Températures, ventilateurs, tensions (`sensors`) | [README.md](./lm-sensors/README.md), [sensors.txt](./lm-sensors/sensors.txt) |
| **GPU — LACT** | Hotspot / VRAM / télémétrie AMD-NVIDIA via `lact cli` | [API.md](./lact/API.md), [CONFIG.md](./lact/CONFIG.md), [README.md](./lact/README.md) |
| **GPU — clpeak** | Calcul et bande passante OpenCL | [README.md](./clpeak/README.md) |
| **GPU — glmark2** | Benchmark OpenGL | [README](./glmark2/README) |
| **GPU — gpu-burn** | Stress CUDA NVIDIA (GFLOPS + erreurs mémoire) | [README.md](./gpu-burn/README.md) |
| **GPU — gpu-fryer** | Stress GPU NVIDIA (Hugging Face) — ne fonctionne pas sur Intel Arc (constaté le 12/10/2026) | [README.md](./gpu-fryer/README.md) |
| **GPU — nvbandwidth** | Bande passante / latence NVLink & PCIe (NVIDIA) | [README.md](./nvbandwidth/README.md) |
| **GPU — vkpeak** | Calcul Vulkan (FP32/FP16/FP64/INT) | [README.md](./vkpeak/README.md) |
| **GPU Intel — xpu-smi** | Télémétrie Intel (températures, puissance, fréquence) | [smi_user_guide.md](./xpu-smi/smi_user_guide.md) |
| **GPU Intel — capteurs xe (hwmon)** | `pkg` = cœur ; `vram`, `mctrl`, `pcie`, `vram_ch_N` | [README.md](./xe-hwmon/README.md), [doc noyau](./xe-hwmon/sysfs-driver-intel-xe-hwmon.txt) |
| **GPU Intel — ze_peak (level-zero-tests)** | Mesure de crête Level Zero (pas de vérification) | [ze_peak_README.md](./level-zero-tests/ze_peak_README.md) |
| **GPU — memtest_vulkan** | Test d'erreurs VRAM Vulkan (Intel/AMD/NVIDIA), non intégré | [README.md](./memtest_vulkan/README.md), [LICENSE](./memtest_vulkan/LICENSE) |
| **GPU AMD — ROCm Validation Suite** | Stress GEMM / mémoire / puissance AMD, non intégré | [ug1main.md](./rocm-validation-suite/ug1main.md) |
| **Notes — burn GPU Intel/AMD** | Recherche d'outils avec détection d'erreurs de calcul | [NOTES_burn_gpu_intel_amd.md](./NOTES_burn_gpu_intel_amd.md) |
| **Réseau — iperf3** | Débit TCP/UDP | [README.md](./iperf3/README.md), [iperf3.txt](./iperf3/iperf3.txt) |
| **Stockage — fio** | Tests NVMe / SATA / HDD (IOPS, débit, latence) | [HOWTO.rst](./fio/HOWTO.rst), [README.rst](./fio/README.rst) |
| **Stockage — nvme-cli** | SMART NVMe (`nvme smart-log`) | [README.md](./nvme-cli/README.md) |
| **Stockage — smartmontools** | SMART SATA/HDD (`smartctl`) | [README.md](./smartmontools/README.md), [smartctl.txt](./smartmontools/smartctl.txt) |
| **GPU — nvidia-smi** | Requêtes CSV, limites de puissance, verrouillage de fréquences, drain / reset (page Configuration NVIDIA, relevés GPU) | [REFERENCE.md](./nvidia-smi/REFERENCE.md) |

Voir aussi : [NVIDIA_SMI_REFERENCE.md](../NVIDIA_SMI_REFERENCE.md) pour `nvidia-smi`.

## Notes
- `ipmitool`, `nvme-cli`, `smartmontools`, `glmark2` et `lm-sensors` : README du projet (+ page de manuel quand elle est disponible) — pas de guide plus détaillé en ligne brute.
- `lact cli` n'a pas de documentation dédiée dans le dépôt LACT : on trouve le README, la configuration (`CONFIG.md`) et l'API du démon (`API.md`). Utiliser `lact cli --help` pour les sous-commandes.
- Pages de manuel : `stress-ng.txt` est très volumineux (liste complète des stressors) — utiliser `grep -n` pour y naviguer.
- `smartctl.txt` est généré depuis un modèle (`.8.in`) : quelques variables de compilation peuvent apparaître sous la forme `@...@`.
- Pour mettre à jour ces copies, relancer la récupération depuis les URL indiquées en tête de chaque fichier.
