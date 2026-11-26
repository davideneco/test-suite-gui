#!/usr/bin/env bash
# make_zip.sh — Crée l'archive de livraison  dist/BenchSuite-v<version>.zip  (dossier racine BenchSuite-v<version>/).
# Contenu = fichiers suivis par git au dernier commit (donc à lancer APRÈS le commit de la version). Le nom du fichier
# porte toujours la version lue dans main.py ; les droits d'exécution (benchsuite.sh, cli.py…) sont conservés.
# Usage : ./make_zip.sh
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VER="$(grep -m1 -oP '^VERSION\s*=\s*"\K[^"]+' main.py | awk '{print $1}')"
[ -n "$VER" ] || { echo "✗ version introuvable dans main.py" >&2; exit 1; }
if ! git diff --quiet HEAD -- 2>/dev/null; then
  echo "⚠ des modifications non commitées existent : elles ne sont PAS dans l'archive (git archive prend le dernier commit)." >&2
fi
mkdir -p dist
OUT="dist/BenchSuite-v${VER}.zip"
rm -f "$OUT"
git archive --format=zip --prefix="BenchSuite-v${VER}/" -o "$OUT" HEAD
echo "✓ $OUT ($(du -h "$OUT" | cut -f1)) — commit $(git rev-parse --short HEAD)"
