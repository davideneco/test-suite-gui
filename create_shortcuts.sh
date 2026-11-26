#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  BenchSuite — Création des raccourcis (bureau + menu Applications)
#  Lancer UNE SEULE FOIS après avoir extrait l'archive
# ─────────────────────────────────────────────────────────────────────────────

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PYTHON="$(which python3)"

echo "════════════════════════════════════════════════"
echo "  ⚡ BenchSuite — Installation des raccourcis"
echo "════════════════════════════════════════════════"
echo ""
echo "  Répertoire : $SCRIPT_DIR"
echo "  Python     : $PYTHON"
echo ""

# ── 1. Rendre le lanceur exécutable ──────────────────────────────────────────
chmod +x "$SCRIPT_DIR/benchsuite.sh"
echo "▸ [1/3] Lanceur shell rendu exécutable"

# ── 2. Icône + Entrée menu Applications ──────────────────────────────────────
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
mkdir -p "$ICON_DIR"
if [ -f "$SCRIPT_DIR/assets/io.benchsuite.app.svg" ]; then
    cp -f "$SCRIPT_DIR/assets/io.benchsuite.app.svg" "$ICON_DIR/io.benchsuite.app.svg"
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
fi

APPS_DIR="$HOME/.local/share/applications"
mkdir -p "$APPS_DIR"
rm -f "$APPS_DIR/benchsuite.desktop"

cat > "$APPS_DIR/io.benchsuite.app.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=BenchSuite
GenericName=Benchmark Suite
Comment=Bench et recette matérielle — CPU, RAM, GPU, stockage, réseau
Exec="$SCRIPT_DIR/benchsuite.sh"
Icon=io.benchsuite.app
Terminal=false
Categories=System;Monitor;Utility;
Keywords=benchmark;performance;cpu;gpu;ram;
StartupWMClass=io.benchsuite.app
StartupNotify=true
EOF

chmod +x "$APPS_DIR/io.benchsuite.app.desktop"
update-desktop-database "$APPS_DIR" 2>/dev/null || true
echo "▸ [2/3] Menu Applications : $APPS_DIR/io.benchsuite.app.desktop"

# ── 3. Raccourci bureau ───────────────────────────────────────────────────────
DESKTOP_DIR="$HOME/Bureau"
# Essayer aussi le nom anglais si le dossier français n'existe pas
if [ ! -d "$DESKTOP_DIR" ]; then
    DESKTOP_DIR="$HOME/Desktop"
fi

if [ -d "$DESKTOP_DIR" ]; then
    cat > "$DESKTOP_DIR/BenchSuite.desktop" << EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=BenchSuite
GenericName=Benchmark Suite
Comment=Bench et recette matérielle — CPU, RAM, GPU, stockage, réseau
Exec="$SCRIPT_DIR/benchsuite.sh"
Icon=io.benchsuite.app
Terminal=false
Categories=System;Monitor;Utility;
Keywords=benchmark;performance;cpu;gpu;ram;
StartupWMClass=io.benchsuite.app
StartupNotify=true
EOF
    chmod +x "$DESKTOP_DIR/BenchSuite.desktop"
    # Marquer comme approuvé (nécessaire sur GNOME pour être cliquable)
    gio set "$DESKTOP_DIR/BenchSuite.desktop" metadata::trusted true 2>/dev/null || true
    echo "▸ [3/3] Raccourci bureau  : $DESKTOP_DIR/BenchSuite.desktop"
else
    echo "▸ [3/3] Dossier Bureau introuvable — raccourci bureau ignoré"
    echo "         (crée manuellement un dossier ~/Bureau ou ~/Desktop)"
fi

echo ""
echo "════════════════════════════════════════════════"
echo "  ✓ Terminé !"
echo ""
echo "  Tu peux maintenant lancer BenchSuite :"
echo "  • Double-clic sur BenchSuite.desktop (bureau)"
echo "  • Menu Applications → Système → BenchSuite"
echo "  • En ligne de commande : $SCRIPT_DIR/benchsuite.sh"
echo "════════════════════════════════════════════════"
