# 🏗️ Architecture Logicielle & Concurrence

Ce document détaille les choix d'architecture logicielle, les flux de données et les règles de concurrence appliquées dans BenchSuite.

---

## 1. Vue d'Ensemble des Composants

```
┌─────────────────────────────────────────────────────────────┐
│                      Interface GTK4                         │
│(main.py / core/changelog.py / ui/*.py / Adw.ApplicationWindow)│
└──────────────────────────────▲──────────────────────────────┘
                               │ GLib.idle_add()
                               │ (Signaux & callbacks de progression)
┌──────────────────────────────┴──────────────────────────────┐
│                  Moteur d'Exécution Asynchrone              │
│       threading.Thread(daemon=True) + Événements de stop    │
└──────────────────────────────▲──────────────────────────────┘
                               │
            ┌──────────────────┴──────────────────┐
            │                                     │
┌───────────┴──────────────┐          ┌───────────┴──────────────┐
│   core/benchmarks.py     │          │   exporters/export.py    │
│   (façade publique)      │          │   (façade publique)      │
│  - bench_utils / sensors │          │  - common / history      │
│  - bench_net / nvidia    │          │  - html_report (Chart.js)│
│  - bench_compute         │          │  - pdf_report (ReportLab)│
└──────────────────────────┘          └──────────────────────────┘
```

---

## 2. Principes Fondamentaux de Concurrence

1. **Zéro Blocage de la Boucle Événementielle GTK (Main Loop) :**
   - Aucune commande shell synchrone (`subprocess.run()`, accès disque lent, requêtes réseau) ne doit être exécutée dans le thread UI principal.
   - Tous les benchmarks et threads de télémétrie sont lancés sous la forme :
     ```python
     threading.Thread(target=self._run_bench_thread, daemon=True).start()
     ```

2. **Mises à Jour d'Interface Sécurisées via `GLib.idle_add` :**
   - Modifier des composants GTK depuis un thread d'arrière-plan provoque des instabilités de mémoire et des corruptions d'affichage.
   - Toute mise à jour de barre de progression, de label ou de graphique en direct est encapsulée :
     ```python
     GLib.idle_add(self._update_progress, pct, message)
     ```

3. **Annulation et Arrêt Propre (Graceful Kill) :**
   - Chaque exécution est associée à un `threading.Event()` pour signaler l'arrêt demandé par l'utilisateur.
   - Les sous-processus système sont exécutés dans un nouveau groupe de processus (`start_new_session=True` ou `os.setsid`) afin que l'interruption envoie `SIGTERM` (puis `SIGKILL` si délai dépassé) à l'arborescence complète du processus.

---

## 3. Ergonomie et Anti-Overflow GTK4 (Libadwaita)

- **Grilles à 2 colonnes (`Gtk.Grid` / `Gtk.FlowBox`) :** Pour éviter un empilement vertical infini des formulaires de paramètres, les champs de configuration sont disposés de manière équilibrée pour diviser la hauteur d'affichage par deux.
- **Boutons Épinglés (Sticky Action Bar) :** Les commandes `▶ Lancer`, `⏹ Stopper` et `💾 Exporter` sont situées en en-tête ou dans une zone dédiée sans forcer l'utilisateur à faire défiler la fenêtre.
- **Design Sombre & Orange Signature :** Respect de la charte visuelle sombre (`#0d0d0d` pour le fond, `#141414` pour les cartes, `#ff6600` en accentuation).
