#!/usr/bin/env bash
# tag_release.sh — Crée (et pousse) le tag git  v<version>  sur le dernier commit, pour que GitHub propose un téléchargement
# dont le NOM contient la version : https://github.com/<dépôt>/archive/refs/tags/v<version>.zip  →  <dépôt>-<version>.zip
# (le bouton « Download ZIP » d'une branche donne toujours <dépôt>-<branche>.zip, sans version : on ne peut pas le changer.)
# La version est lue dans main.py (comme make_zip.sh). À lancer APRÈS le commit de la version et son push.
# Usage : ./tag_release.sh            # crée + pousse le tag
#         ./tag_release.sh --local    # crée le tag sans le pousser
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"
VER="$(grep -m1 -oP '^VERSION\s*=\s*"\K[^"]+' main.py | awk '{print $1}')"
[ -n "$VER" ] || { echo "✗ version introuvable dans main.py" >&2; exit 1; }
TAG="v${VER}"
HEAD_SHA="$(git rev-parse HEAD)"

if git diff --quiet HEAD -- 2>/dev/null; then :; else
  echo "⚠ des modifications non commitées existent : elles ne sont pas dans le tag (il pointe sur le dernier commit)." >&2
fi

if git rev-parse -q --verify "refs/tags/${TAG}" >/dev/null; then
  TAG_SHA="$(git rev-list -n1 "${TAG}")"
  if [ "$TAG_SHA" != "$HEAD_SHA" ]; then
    echo "✗ le tag ${TAG} existe déjà sur un autre commit (${TAG_SHA:0:7}). Incrémentez la version (./bump_version.sh patch) avant de re-tagger." >&2
    exit 1
  fi
  echo "• tag ${TAG} déjà présent sur ce commit."
else
  git tag -a "${TAG}" -m "BenchSuite ${VER}" "${HEAD_SHA}"
  echo "✓ tag ${TAG} créé sur ${HEAD_SHA:0:7}"
fi

if [ "${1:-}" = "--local" ]; then
  echo "• tag non poussé (--local)."; exit 0
fi

ok=0
for d in 0 2 4 8 16; do
  sleep "$d"
  if git push origin "refs/tags/${TAG}" 2>&1; then ok=1; break; fi
done
[ "$ok" = 1 ] || { echo "✗ push du tag impossible (réseau ou droits)." >&2; exit 1; }

URL="$(git remote get-url origin | sed -E 's#^git@github.com:#https://github.com/#; s#\.git$##')"
echo "✓ téléchargement versionné : ${URL}/archive/refs/tags/${TAG}.zip   (fichier : $(basename "$URL")-${VER}.zip)"
