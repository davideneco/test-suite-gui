# Notes — burn GPU Intel / AMD avec détection d'erreurs (recherche du 2026-10-12)

Synthèse faite pour BenchSuite (outils libres uniquement). **Établi** = lu dans les sources ; **non vérifié** = à tester sur matériel réel.

## Constat
- gpu-burn (CUDA) et gpu-fryer ne fonctionnent qu'avec NVIDIA (gpu-fryer : confirmé à l'usage sur une machine Intel Arc Pro B70).
- Aucun outil libre prêt à l'emploi ne fait à la fois « charge de calcul maximale » et « vérification des résultats » sur Intel.

## Outils
| Outil | Marques | Vérifie les erreurs ? | Licence | Remarques |
|---|---|---|---|---|
| memtest_vulkan v0.5.0 | Intel, AMD, NVIDIA (Vulkan 1.1) | **VRAM seulement** (motifs écrits puis relus, adresses d'erreur) | zlib | `./memtest_vulkan <index>` = non interactif ; pas d'option de durée (boucle de 100 000 000 itérations → arrêt par BenchSuite) ; au moins 6 min (5-6 min de préchauffage), 2-3 h pour les erreurs rares ; verdict à lire dans la sortie (`PASSed` / `ERRORS FOUND`), code de retour = 64 + bits (le code « erreurs » n'est pas confirmé) ; **non vérifié sur Arc Pro B70 (Battlemage)** |
| ROCm Validation Suite (RVS) | AMD (Instinct, Radeon récentes ; RX 7900 non listée) | GST : cible GFLOPS, correction **optionnelle** (`self_check`, `accuracy_check`, défaut false) ; MEM : 11 tests de motifs ; IET : puissance | MIT | `rvs -c gst.conf -j` ; paquet `rocm-validation-suite` ; `target_stress: 0` évite l'échec sur la performance ; effet réel de `self_check` non vérifié |
| `xpu-smi diag` | Intel Data Center (Flex, Max) | test « Memory Error » existant | MIT | **absent sur Arc Pro (B60/B70)** avec xpu-smi 2.x (source secondaire : intel/gpu-ai-skills PR #15) ; `health -l` le remplace en partie |
| level-zero-tests `ze_peak` | Intel (Level Zero) | non (mesure) | MIT | `ze_peak -t sp_compute -i 50` |
| mixbench, hashcat -b, stress-ng --gpu, IGT | — | non vérifié | — | — |

## Approche maison envisagée (non écrite)
Petit programme C OpenCL (≈ 300 lignes, `third_party/`) : noyaux **entiers exacts** (reproductibles bit à bit) comparés à chaque itération, motif sur ~90 % de la VRAM, charge à régler d'après la puissance mesurée (xpu-smi / amd-smi). Extension Intel `cl_intel_subgroup_matrix_multiply_accumulate` (int8 exact) possible pour les unités matricielles : effet sur XMX Battlemage non confirmé.

## Sources
memtest_vulkan : https://github.com/GpuZelenograd/memtest_vulkan — RVS : https://github.com/ROCm/ROCmValidationSuite — xpumanager : https://github.com/intel/xpumanager — level-zero-tests : https://github.com/oneapi-src/level-zero-tests — OpenCL ext. : https://raw.githubusercontent.com/KhronosGroup/OpenCL-Docs/main/extensions/cl_intel_subgroup_matrix_multiply_accumulate.asciidoc
