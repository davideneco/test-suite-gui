<!-- Source officielle : https://docs.nvidia.com/deploy/nvidia-smi/index.html — synthèse interne déplacée du dépôt logiciel le 2026-11-14 -->
# 📗 Guide de Référence NVIDIA SMI (System Management Interface)

Ce document détaille les commandes, registres, requêtes et bonnes pratiques basés sur la documentation officielle :
🔗 **Documentation Officielle :** [NVIDIA System Management Interface (nvidia-smi)](https://docs.nvidia.com/deploy/nvidia-smi/index.html)

---

## 1. Introduction & Architecture NVML

`nvidia-smi` est un utilitaire en ligne de commande basé sur la bibliothèque **NVML** (NVIDIA Management Library). Il est destiné à la gestion, la surveillance et la configuration des accélérateurs graphiques NVIDIA en environnement station de travail, serveur de calcul (Data Center) et cluster HPC.

---

## 2. Commandes Fondamentales

### Activer le mode persistance (Recommandé en production / serveur)
Le mode persistance maintient le pilote NVIDIA chargé en mémoire même lorsqu'aucun processus CUDA n'est actif, réduisant ainsi la latence de démarrage des benchmarks :
```bash
sudo nvidia-smi -pm 1
```

### Visualisation synthétique en temps réel
```bash
nvidia-smi
```

### Surveillance continue en continu (dmon / pmon)
- **Monitoring des métriques par seconde (dmon) :**
  ```bash
  nvidia-smi dmon -s pucv -d 1
  ```
  *(p : puissance, u : utilisation, c : horloges/clocks, v : vram)*
- **Monitoring des processus par GPU (pmon) :**
  ```bash
  nvidia-smi pmon -d 1
  ```

---

## 3. Requêtes Ciblées (`--query-gpu`)

Le format CSV sans en-tête (`--format=csv,noheader,nounits`) permet à BenchSuite d'extraire des métriques numériques pures sans surcoût de parsing.

### Syntaxe générale :
```bash
nvidia-smi --query-gpu=[propriétés...] --format=csv,noheader,nounits
```

### Propriétés clés utilisées dans BenchSuite :

| Propriété | Description | Exemple |
| :--- | :--- | :--- |
| `pci.bus_id` | Identifiant du bus PCIe BDF complet | `00000000:01:00.0` |
| `serial` | Numéro de série unique du GPU physique | `1321423000123` |
| `name` | Nom commercial de la carte | `NVIDIA RTX 6000 Ada Generation` |
| `temperature.gpu` | Température du die / cœur (°C) | `65` |
| `power.draw` | Consommation instantanée de la carte (W) | `285.4` |
| `clocks.current.graphics` | Fréquence de fonctionnement du GPU (MHz) | `2505` |
| `clocks.current.memory` | Fréquence de fonctionnement de la VRAM (MHz)| `9001` |
| `vbios_version` | Version du firmware VBIOS | `96.02.48.00.01` |

---

## 4. Analyse Approfondie des Raisons de Throttling (`clocks_throttle_reasons`)

NVIDIA expose des registres binaires (masques de bits) indiquant avec précision pourquoi la fréquence d'une carte est abaissée :

| Registre | Type | Signification |
| :--- | :--- | :--- |
| `clocks_throttle_reasons.sw_thermal_slowdown` | Logiciel | Abaissement des fréquences imposé par le pilote car la température cible maximale est atteinte. |
| `clocks_throttle_reasons.hw_thermal_slowdown` | Matériel | Sécurité critique du silicium activée : bridage d'urgence immédiat pour éviter la fusion du die. |
| `clocks_throttle_reasons.sw_power_cap` | Logiciel | Fréquence régulée pour respecter la limite de puissance définie (Power Limit / TDP). |
| `clocks_throttle_reasons.hw_power_brake_slowdown` | Matériel | Alerte d'alimentation externe (ex: broche 12VHPWR ou alimentation redondante défaillante). |
| `clocks_throttle_reasons.gpu_idle` | Normal | Le GPU est au repos et réduit ses fréquences pour économiser l'énergie. |
| `clocks_throttle_reasons.applications_clocks_setting` | Normal | Fréquences fixées par la configuration logicielle de l'utilisateur. |

BenchSuite interroge `sw_thermal_slowdown` et `hw_thermal_slowdown` à chaque cycle pour garantir l'intégrité de vos stress tests thermiques.

---

## 5. Gestion et Configuration Avancée

### Vérification et réglage de la limite de puissance (Power Limit)
```bash
# Consulter les limites minimale, par défaut et maximale
nvidia-smi -q -d POWER

# Ajuster le TDP à 300 Watts (nécessite les droits root)
sudo nvidia-smi -pl 300
```

### Verrouillage des fréquences pour benchmark reproductible (Clock Locking)
Pour éliminer les fluctuations de boost et obtenir des résultats de calcul stricts :
```bash
# Verrouiller la fréquence GPU à 2100 MHz
sudo nvidia-smi -lgc 2100

# Réinitialiser la fréquence automatique
sudo nvidia-smi -rgc
```

### Vérification de la bande passante et des liaisons PCIe
```bash
nvidia-smi --query-gpu=pcie.link.gen.current,pcie.link.gen.max,pcie.link.width.current,pcie.link.width.max --format=csv
```

---

## 6. Liens & Documentation Associée
- 🔗 [Documentation Officielle NVIDIA SMI](https://docs.nvidia.com/deploy/nvidia-smi/index.html)
- 🔗 [NVIDIA NVML API Reference](https://docs.nvidia.com/deploy/nvml-api/index.html)
- 🔗 [Télémétrie et Capteurs BenchSuite](./TELEMETRIE_ET_CAPTEURS.md)
