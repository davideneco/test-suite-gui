# Capteurs hwmon du pilote xe (GPU Intel Arc / Battlemage)

Documentation noyau : [sysfs-driver-intel-xe-hwmon.txt](./sysfs-driver-intel-xe-hwmon.txt). Correspondance observée sur une Arc Pro B70 (`sensors`) :

| Label | Mesure (doc noyau) | Seuils vus (B70) |
|---|---|---|
| `pkg` (temp2) | « Package temperature » — **la puce GPU = température du cœur** | high 60, crit 100, emerg 125 °C |
| `vram` (temp3) | température de la VRAM | crit 105, emerg 120 °C |
| `mctrl` (temp4) | « Memory controller average temperature » | crit 100, emerg 125 °C |
| `pcie` (temp5) | « GPU PCIe temperature » | crit 100, emerg 125 °C |
| `vram_ch_N` (temp6…21) | un capteur par canal VRAM | crit 105, emerg 120 °C |

`card` = puissance de la carte (cap 290 W, crit 575 W sur B70). Le sens exact du seuil `high` de `pkg` (60 °C) n'est pas détaillé : non vérifié.
