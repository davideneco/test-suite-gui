#!/usr/bin/env bash
# BenchSuite — lanceur Linux
set -Eeuo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Evite les warnings EGL/DRI2/DRI3 GTK sur certains serveurs, BMC KVM, X11 distant
# ou sessions sans acceleration graphique complète. Mettre BENCHSUITE_ALLOW_GL=1
# pour forcer le renderer GL natif.
if [[ "${BENCHSUITE_ALLOW_GL:-0}" != "1" ]]; then
  export GSK_RENDERER="${GSK_RENDERER:-cairo}"
  export LIBGL_ALWAYS_SOFTWARE="${LIBGL_ALWAYS_SOFTWARE:-1}"
fi

# BenchSuite n'installe ni ne désinstalle jamais rien. Il charge en revanche les modules IPMI dont
# il a besoin (best-effort, silencieux) ; BENCHSUITE_READ_ONLY=1 désactive toute action de ce genre.
# Pas de __pycache__ écrit dans le dossier du logiciel.
export PYTHONDONTWRITEBYTECODE=1
if [[ "${BENCHSUITE_READ_ONLY:-0}" != "1" ]]; then
  sudo -n modprobe ipmi_si 2>/dev/null || true
  sudo -n modprobe ipmi_devintf 2>/dev/null || true
fi

if ! python3 - <<'PY' >/dev/null 2>&1
import psutil, reportlab
PY
then
  echo "BenchSuite: dépendances Python manquantes."
  echo "Sur Ubuntu/Debian, lance:"
  echo "  sudo apt install python3-psutil python3-reportlab"
  echo "ou relance l'installation complète:"
  echo "  ./install.sh"
  echo ""
  echo "Détail de l'environnement:"
  python3 "$SCRIPT_DIR/check_environment.py" || true
  exit 1
fi


if ! python3 - <<'PY' >/dev/null 2>&1
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
PY
then
  echo "BenchSuite: GTK4/Libadwaita n'est pas prêt. Lance d'abord: ./install.sh"
  echo "Détail de l'environnement:"
  python3 "$SCRIPT_DIR/check_environment.py" || true
  exit 1
fi

if [[ "${BENCHSUITE_DEBUG:-0}" == "1" ]]; then
  exec python3 "$SCRIPT_DIR/main.py" "$@"
fi

# Les consoles X11 distantes, BMC/KVM et certaines VM affichent souvent ces
# warnings EGL même quand GTK démarre correctement en rendu logiciel.
python3 "$SCRIPT_DIR/main.py" "$@" 2> >(
  grep -v -E '^(libEGL warning: DRI[23]|MESA: warning:|libGL error: failed to authenticate)' >&2
)
