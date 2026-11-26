#!/usr/bin/env bash
# bump_version.sh — Incrémente automatiquement la version dans tous les fichiers et synchronise avec Git
# Usage : ./bump_version.sh [patch|minor|major|X.Y.Z]

set -e
BUMP="${1:-patch}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python3 "$ROOT/bump_version.py" "$BUMP"

NEW_VER="(inconnue)"
if [ -f "$ROOT/.last_version" ]; then
    NEW_VER=$(cat "$ROOT/.last_version")
    rm -f "$ROOT/.last_version"
fi

echo "Terminé — version $NEW_VER"
