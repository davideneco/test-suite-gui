# 🌡️ Télémétrie, Capteurs et Diagnostic Matériel

Ce document explique le fonctionnement du moteur de télémétrie de BenchSuite, la gestion multi-GPU 24 capteurs, le mapping matériel MCIO, la surveillance du throttling et le calcul du $\Delta T$.

---

## 1. Moteur de Télémétrie Multi-GPU

Lorsqu'un test GPU est en cours, un thread d'échantillonnage dédié (`_run_gpu_poll`) s'exécute à une fréquence de 1 Hz. Il synchronise plusieurs sources de télémétrie :

1. **`nvidia-smi` (NVIDIA) :**
   - Requête instantanée par bus PCIe :
     ```bash
     nvidia-smi --query-gpu=pci.bus_id,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory,clocks_throttle_reasons.sw_thermal_slowdown,clocks_throttle_reasons.hw_thermal_slowdown --format=csv,noheader,nounits
     ```
   - Données collectées : Température Cœur (°C), Consommation énergétique (W), Fréquence GPU (MHz), Fréquence Mémoire (MHz), Registres de bridage matériel.

2. **`LACT` (Linux AMD & NVIDIA Custom Telemetry) :**
   - Commande CLI : `lact cli -g <gpu_id> stats`.
   - Extraction ciblée : Température de jonction (**Hotspot**) et température mémoire (**VRAM**).

3. **Fallbacks sans privilège (`sysfs`) :**
   - En l'absence des utilitaires constructeurs, lecture directe dans `/sys/class/drm/card*/device/hwmon/` ou `/sys/class/thermal/`.

---

## 2. Cartographie Matérielle des Connecteurs MCIO

Sur les cartes mères pour serveurs multi-GPU (par exemple grappe 8x NVIDIA RTX Pro ou serveurs bi-socket EPYC), chaque carte graphique est physiquement connectée via des faisceaux de câbles MCIO (Mini Cool Edge IO).

BenchSuite associe chaque GPU à ses connecteurs physiques via son **root complex PCI** (premier octet du bus : `00`, `10`, `60`, `70`, `80`, `90`, `e0`, `f0`), lu dans `/sys/devices/pciXXXX:RR/…`. Le root complex reste identique même quand le BIOS ou un périphérique décale la numérotation des bus en aval (ex. GPU passant de `01:00.0` à `07:00.0`). Ordre de priorité :
1. **Fichier utilisateur** `~/.benchsuite_mcio.json`, clés `root:RR` (root complex) ou `bb:dd.f` (GPU précis) :
```json
{
  "root:00": "MCIO1 + MCIO2",
  "root:10": "MCIO3 + MCIO4",
  "61:00.0": "MCIO5 + MCIO6"
}
```
2. **Profil intégré** du serveur 8 GPU de référence (`root 00→MCIO1+2`, `10→3+4`, `60→5+6`, `70→7+8`, `80→11+12`, `90→13+14`, `e0→19+20`, `f0→17+18`), activé **uniquement** si au moins 3 GPU de la machine sont sur ces root complex.
3. Sinon, aucun libellé MCIO n'est affiché (pas de « N/A »).

Ce libellé apparaît directement dans les légendes, le titre des courbes et les tableaux d'inventaire, permettant d'identifier immédiatement un GPU physique défectueux sur le châssis serveur.

---

## 3. Détection Active du Throttling Matériel

Contrairement aux solutions comparant une température à un seuil arbitraire, BenchSuite interroge directement les compteurs internes du GPU :
- `clocks_throttle_reasons.sw_thermal_slowdown` (abaissement logiciel imposé par le pilote).
- `clocks_throttle_reasons.hw_thermal_slowdown` (abaissement d'urgence déclenché par les sécurités matérielles du silicium).

Dans les rapports :
- Si aucun bridage n'est survenu : `🛡️ Fréquences nominales (Aucun throttling)` (Vert).
- Si un bridage est survenu : `⚠️ Throttling thermique détecté (GPU X · N échantillons)` (Rouge).

---

## 4. Calcul Automatique du $\Delta T$ (Hotspot ↔ Cœur)

L'écart thermique $\Delta T$ est un indicateur crucial de la qualité du montage thermique :
$$\Delta T = T_{\text{hotspot}} - T_{\text{cœur}}$$

### Barème de qualification :
- **$\Delta T \le 20\,^\circ\text{C}$ [Optimal] :** Excellent transfert thermique entre le die et le radiateur.
- **$20\,^\circ\text{C} < \Delta T \le 25\,^\circ\text{C}$ [Élevé] :** Échauffement localisé, flux d'air à surveiller.
- **$\Delta T > 25\,^\circ\text{C}$ [Alerte contact / pâte] :** Dégradation probable du pad thermique / de la pâte ou pression de contact asymétrique du ventirad.

---

## 5. Référence Officielle : NVIDIA System Management Interface (nvidia-smi)

BenchSuite s'appuie sur les interfaces documentées par NVIDIA pour la collecte des métriques et des registres de performances :

- 🔗 **Documentation officielle NVIDIA :** [NVIDIA System Management Interface Documentation](https://docs.nvidia.com/deploy/nvidia-smi/index.html)

### Requêtes exploitées dans le code :
- **Télémétrie instantanée (1 Hz) :**
  ```bash
  nvidia-smi --query-gpu=pci.bus_id,temperature.gpu,power.draw,clocks.current.graphics,clocks.current.memory,clocks_throttle_reasons.sw_thermal_slowdown,clocks_throttle_reasons.hw_thermal_slowdown --format=csv,noheader,nounits
  ```
- **Inventaire matériel & Numéros de série (SN) :**
  ```bash
  nvidia-smi --query-gpu=pci.bus_id,serial,name,vbios_version --format=csv,noheader
  ```
- **Audit approfondi des capteurs thermiques et énergétiques :**
  ```bash
  nvidia-smi -q -d TEMPERATURE,POWER
  ```
- **Aide interactive sur tous les champs de mesure disponibles :**
  ```bash
  nvidia-smi --help-query-gpu
  ```



---

## 6. Statistiques « en charge stabilisée »

Les séries contiennent aussi le repos (avant/après test). Les moyennes, le ΔT hotspot et les fréquences min/max sont donc calculés par `core/gpu_stats.py` uniquement sur la fenêtre de charge :
- **Charge** = puissance ≥ 50 % de la puissance max observée (repli sur la température si la puissance est absente) ;
- les **10 premières secondes** de la charge (montée en régime) sont ignorées ;
- les **pics** (`*_max_c`) restent calculés sur toute la série ;
- statut ΔT basé sur la **moyenne en charge** : > 20 °C avertissement, > 25 °C critique (le P95 est aussi exporté).

Cela évite de signaler comme « critique » un écart transitoire lors du refroidissement ou du démarrage.
