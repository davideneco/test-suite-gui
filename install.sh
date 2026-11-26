#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
#  BenchSuite — installation des dépendances (Ubuntu/Debian, Arch)
#
#  Principe : on N'INSTALLE QUE CE QUI MANQUE. Rien de ce qui fonctionne déjà
#  n'est réinstallé, mis à jour, supprimé ni écrasé (paquets, pilote NVIDIA,
#  snap, gpu-burn, fichiers de configuration). Le script peut être relancé
#  sans risque.
#
#  Machine déjà configurée ? Utilisez plutôt  ./cli.py --install-guide : guide en lecture seule,
#  adapté à ce qui est installé (n'installe rien).
#
#  Options :  --dry-run   affiche ce qui serait fait, sans rien modifier
#             --no-gpu    ignore les outils GPU (gpu-burn, gpu-fryer)
#             --no-build  ne compile rien (gpu-burn) et n'utilise pas cargo
# ─────────────────────────────────────────────────────────────────────────────
set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR" || exit 1

DRY=0; NO_GPU=0; NO_BUILD=0
for a in "$@"; do
  case "$a" in
    --dry-run)  DRY=1 ;;
    --no-gpu)   NO_GPU=1 ;;
    --no-build) NO_BUILD=1 ;;
    -h|--help)  sed -n '2,15p' "$0"; exit 0 ;;
    *) printf 'Option inconnue : %s\n' "$a" >&2; exit 3 ;;
  esac
done

say()  { printf '%s\n' "$*"; }
have() { command -v "$1" >/dev/null 2>&1; }
SUDO=""; [ "$(id -u)" -ne 0 ] && have sudo && SUDO="sudo"
# Exécute une commande (ou l'affiche en --dry-run)
run()  { if [ "$DRY" -eq 1 ]; then say "    [dry-run] $*"; return 0; fi; "$@"; }
TODO=()   # points à traiter à la main, rappelés en fin de script

say "════════════════════════════════════════════════"
say "  ⚡ BenchSuite — Installation (ne touche pas à l'existant)"
[ "$DRY" -eq 1 ] && say "  Mode simulation : rien n'est modifié"
say "════════════════════════════════════════════════"

if ! have python3; then
  say "✗ python3 est requis. Installe Python 3.10+ puis relance ce script."
  exit 1
fi

# ── Gestionnaire de paquets ─────────────────────────────────────────────────
PM=""
if have apt-get; then PM="apt"; elif have pacman; then PM="pacman"; fi

pkg_installed() {   # $1 = nom de paquet
  case "$PM" in
    apt)    dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q "install ok installed" ;;
    pacman) pacman -Qi "$1" >/dev/null 2>&1 ;;
    *)      return 1 ;;
  esac
}
pkg_available() {   # le paquet existe-t-il dans les dépôts ?
  case "$PM" in
    apt)    apt-cache show "$1" >/dev/null 2>&1 ;;
    pacman) pacman -Si "$1" >/dev/null 2>&1 ;;
    *)      return 1 ;;
  esac
}

# Installe uniquement les paquets absents ; --no-upgrade : ne met rien d'autre à jour
install_missing() {
  local missing=() p
  for p in "$@"; do
    pkg_installed "$p" && continue
    if pkg_available "$p"; then missing+=("$p"); else say "  ⊘ $p : introuvable dans les dépôts (ignoré)"; fi
  done
  if [ "${#missing[@]}" -eq 0 ]; then say "  ✓ déjà présents : $*"; return 0; fi
  say "  → à installer : ${missing[*]}"
  case "$PM" in
    apt)
      run $SUDO apt-get update -qq >/dev/null 2>&1 || say "  ⚠ apt update a échoué (dépôt tiers ?), on continue avec le cache"
      # un paquet en échec ne doit pas bloquer les autres : on réessaie un par un
      run $SUDO apt-get install -y --no-upgrade "${missing[@]}" || {
        say "  ⚠ installation groupée échouée, essai paquet par paquet…"
        for p in "${missing[@]}"; do run $SUDO apt-get install -y --no-upgrade "$p" || say "  ⚠ $p non installé"; done
      } ;;
    pacman) run $SUDO pacman -S --noconfirm --needed "${missing[@]}" || say "  ⚠ certains paquets n'ont pas pu être installés" ;;
    *)      say "  ⚠ gestionnaire de paquets inconnu : installe à la main : ${missing[*]}"; TODO+=("Installer : ${missing[*]}") ;;
  esac
}

# ── [1/5] Dépendances système ───────────────────────────────────────────────
say ""
say "▸ [1/5] Dépendances système (GTK4, Python, outils matériel)…"
pymod() { python3 -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('$1') else 1)" 2>/dev/null; }
if [ "$PM" = "apt" ]; then
  # Un module Python déjà importable (apt, pip, venv…) n'est pas réinstallé par un second paquet
  PYPKGS=()
  pymod gi        || PYPKGS+=(python3-gi python3-gi-cairo)
  pymod cairo     || PYPKGS+=(python3-cairo)
  python3 -c "import gi; gi.require_foreign('cairo')" 2>/dev/null || PYPKGS+=(python3-gi-cairo)   # pont GTK↔cairo
  pymod psutil    || PYPKGS+=(python3-psutil)
  pymod reportlab || PYPKGS+=(python3-reportlab)
  install_missing "${PYPKGS[@]}" \
    gir1.2-gtk-4.0 gir1.2-adw-1 libadwaita-1-0 xdg-utils desktop-file-utils \
    pciutils usbutils ipmitool nvme-cli smartmontools ethtool dmidecode lm-sensors powerstat
elif [ "$PM" = "pacman" ]; then
  install_missing python-gobject python-cairo python-psutil python-reportlab gtk4 libadwaita \
    xdg-utils desktop-file-utils pciutils usbutils ipmitool nvme-cli smartmontools ethtool dmidecode lm_sensors
else
  say "  ⚠ Gestionnaire de paquets introuvable : installe GTK4, Libadwaita, PyGObject, psutil, reportlab, ipmitool…"
  TODO+=("Installer à la main GTK4, Libadwaita, PyGObject, psutil, reportlab, ipmitool")
fi

# ── [1.5/5] IPMI : on ne remplace jamais une configuration existante ────────
say ""
say "▸ [1.5/5] Modules IPMI…"
if [ -e /etc/modules-load.d/ipmi.conf ]; then
  say "  ✓ configuration IPMI déjà en place (non modifiée)"
elif [ -d /etc/modules-load.d ] && have ipmitool; then
  if [ "$DRY" -eq 1 ]; then say "    [dry-run] création de /etc/modules-load.d/ipmi.conf"
  else
    printf 'ipmi_si\nipmi_devintf\nipmi_msghandler\n' | $SUDO tee /etc/modules-load.d/ipmi.conf >/dev/null \
      && say "  ✓ modules IPMI chargés au démarrage" || say "  ⚠ écriture de ipmi.conf impossible"
  fi
  run $SUDO modprobe ipmi_devintf 2>/dev/null || true
  run $SUDO modprobe ipmi_si 2>/dev/null || true
else
  say "  ⊘ ignoré (ipmitool ou /etc/modules-load.d absent)"
fi

# ── [2/5] Python : seulement les modules manquants ──────────────────────────
say ""
say "▸ [2/5] Modules Python manquants…"
PYMISS=$(python3 - <<'PY'
import importlib.util
print(" ".join(m for m in ("psutil", "reportlab") if importlib.util.find_spec(m) is None))
PY
)
if [ -z "$PYMISS" ]; then
  say "  ✓ psutil et reportlab déjà disponibles"
else
  say "  → manquants : $PYMISS"
  # --user : jamais dans le Python du système ; si Python est « géré » (PEP 668) on prévient au lieu de forcer
  # shellcheck disable=SC2086
  run python3 -m pip install --user $PYMISS 2>/dev/null \
    || { say "  ⚠ pip refusé (environnement géré) : utilise les paquets du système python3-psutil / python3-reportlab (apt)"; TODO+=("Installer en Python : $PYMISS"); }
fi

# ── [3/5] Outils de benchmark ───────────────────────────────────────────────
say ""
say "▸ [3/5] Outils de benchmark (stress-ng, fio, iperf3)…"
# Si l'outil est déjà là (même installé hors paquet), on n'y touche pas
BENCH_PKGS=()
for t in stress-ng fio iperf3; do
  if have "$t"; then say "  ✓ $t déjà présent ($(command -v "$t"))"; else BENCH_PKGS+=("$t"); fi
done
[ "${#BENCH_PKGS[@]}" -gt 0 ] && install_missing "${BENCH_PKGS[@]}"
# Outils optionnels (calcul OpenCL / Vulkan) : seulement s'ils existent dans les dépôts
OPT_PKGS=()
have clinfo || OPT_PKGS+=(clinfo)
have vulkaninfo || OPT_PKGS+=(vulkan-tools)
if [ "${#OPT_PKGS[@]}" -gt 0 ] && [ "$PM" = "apt" ]; then install_missing "${OPT_PKGS[@]}"; fi
if [ "$PM" = "apt" ]; then
  have clpeak || TODO+=("clpeak (optionnel) : sudo apt install clpeak, ou https://github.com/krrishnarraj/clpeak")
else
  have clpeak || TODO+=("clpeak (optionnel) : à compiler depuis https://github.com/krrishnarraj/clpeak (pas de paquet connu sur cette distribution)")
fi
have vkpeak || TODO+=("vkpeak (optionnel) : binaire à récupérer sur https://github.com/nihui/vkpeak")

# ── [4/5] Outils GPU ────────────────────────────────────────────────────────
say ""
say "▸ [4/5] Outils GPU…"
if [ "$NO_GPU" -eq 1 ]; then
  say "  ⊘ ignoré (--no-gpu)"
else
  # Le pilote NVIDIA n'est JAMAIS installé ni modifié par ce script.
  if have nvidia-smi; then
    if nvidia-smi >/dev/null 2>&1; then
      say "  ✓ pilote NVIDIA opérationnel (nvidia-smi répond)"
    else
      say "  ⚠ nvidia-smi échoue (« API mismatch » = module noyau et bibliothèques de versions différentes) :"
      say "      sudo dkms autoinstall && sudo reboot"
      TODO+=("Pilote NVIDIA cassé : sudo dkms autoinstall && sudo reboot (rien n'a été modifié)")
    fi
  else
    say "  ⊘ pas de nvidia-smi : pas de GPU NVIDIA (ou pilote non installé) — gpu-burn/DCGM ignorés"
  fi

  # --- gpu-burn : on compile dans /opt/gpu-burn SEULEMENT si aucun gpu-burn sain n'existe.
  #     Un gpu-burn existant n'est jamais supprimé ni écrasé (le snap reste en place :
  #     BenchSuite préfère /opt/gpu-burn au snap).
  burn_ok() {   # binaire exécutable ET toutes ses bibliothèques résolues
    local b="$1"
    [ -x "$b" ] || return 1
    case "$b" in /snap/*) return 1 ;; esac    # le snap ne voit pas le pilote : jamais considéré « sain »
    ls "$(dirname "$b")"/compare.* >/dev/null 2>&1 || return 1   # noyau de comparaison (compare.ptx / .fatbin) à côté du binaire
    have ldd && ldd "$b" 2>/dev/null | grep -q "not found" && return 1
    return 0
  }
  BURN_FOUND=""
  for b in /opt/gpu-burn/gpu_burn /usr/local/bin/gpu_burn /usr/local/bin/gpu-burn \
           "$SCRIPT_DIR/gpu_burn" "$(command -v gpu_burn 2>/dev/null)" "$(command -v gpu-burn 2>/dev/null)"; do
    [ -n "$b" ] && burn_ok "$b" && { BURN_FOUND="$b"; break; }
  done
  if [ -n "$BURN_FOUND" ]; then
    say "  ✓ gpu-burn déjà fonctionnel : $BURN_FOUND (non modifié)"
  elif ! have nvidia-smi; then
    say "  ⊘ gpu-burn : pas de GPU NVIDIA détecté, non compilé"
  elif [ "$NO_BUILD" -eq 1 ]; then
    say "  ⊘ gpu-burn absent ou non fonctionnel (--no-build : non compilé)"
    TODO+=("gpu-burn : compiler avec CUDA (git clone https://github.com/wilicc/gpu-burn && make COMPUTE=<arch>)")
  else
    NVCC="$(command -v nvcc 2>/dev/null)"
    [ -z "$NVCC" ] && NVCC="$(ls -d /usr/local/cuda*/bin/nvcc /opt/cuda*/bin/nvcc 2>/dev/null | sort -V | tail -1)"
    if [ -z "$NVCC" ] || ! have git || ! have make; then
      say "  ⊘ gpu-burn à compiler mais nvcc (toolkit CUDA), git ou make absent"
      TODO+=("gpu-burn : installer le toolkit CUDA (Ubuntu : sudo apt install cuda-toolkit-<X>-<Y>, dépôt NVIDIA) + git + make, puis relancer ./install.sh")
    else
      CUDA_DIR=$(dirname "$(dirname "$(readlink -f "$NVCC")")")
      # Plus petite « compute capability » des GPU présents (75 RTX 20xx · 86 RTX 30xx · 89 RTX 40xx · 120 Blackwell)
      CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader 2>/dev/null | tr -d '. \r' | grep -E '^[0-9]+$' | sort -n | head -1)
      say "  → compilation de gpu-burn (CUDA $CUDA_DIR, architecture ${CC:-auto})"
      TMP="$(mktemp -d /tmp/gpu-burn-src.XXXXXX)"
      if [ "$DRY" -eq 1 ]; then say "    [dry-run] git clone + make + copie dans /opt/gpu-burn"
      elif git clone --depth 1 https://github.com/wilicc/gpu-burn "$TMP" \
        && make -C "$TMP" CUDAPATH="$CUDA_DIR" ${CC:+COMPUTE=$CC} \
        && $SUDO mkdir -p /opt/gpu-burn \
        && $SUDO cp "$TMP/gpu_burn" /opt/gpu-burn/ \
        && $SUDO cp "$TMP"/compare.* /opt/gpu-burn/; then   # compare.ptx ou compare.fatbin selon la version
        say "  ✓ gpu-burn installé dans /opt/gpu-burn"
        # La bibliothèque cuBLAS du toolkit utilisé doit être trouvable au lancement : on le vérifie
        if ldd /opt/gpu-burn/gpu_burn 2>/dev/null | grep -q "not found"; then
          say "  ⚠ bibliothèques CUDA introuvables au lancement : $(ldd /opt/gpu-burn/gpu_burn | grep 'not found' | tr -s ' \t' ' ' | head -2 | tr '\n' ';')"
          TODO+=("gpu-burn : ajouter $CUDA_DIR/lib64 au chargeur : echo $CUDA_DIR/lib64 | sudo tee /etc/ld.so.conf.d/cuda.conf && sudo ldconfig")
        fi
      else
        say "  ⚠ compilation de gpu-burn échouée (aucun fichier existant modifié)"
        TODO+=("gpu-burn : compilation échouée, voir documentation/BENCHMARKS.md")
      fi
      rm -rf "$TMP"
    fi
  fi

  # --- gpu-fryer (cargo) : seulement s'il est absent
  if have gpu-fryer || [ -x "$HOME/.cargo/bin/gpu-fryer" ]; then
    say "  ✓ gpu-fryer déjà présent (non modifié)"
  elif [ "$NO_BUILD" -eq 1 ]; then
    say "  ⊘ gpu-fryer absent (--no-build)"
  elif have cargo; then
    say "  → installation de gpu-fryer (cargo)"
    run cargo install gpu-fryer || { say "  ⚠ gpu-fryer non installé"; TODO+=("gpu-fryer : cargo install gpu-fryer a échoué"); }
  else
    say "  ⊘ gpu-fryer absent et Rust/Cargo absent"
    TODO+=("gpu-fryer (optionnel) : installer Rust (https://rustup.rs) puis cargo install gpu-fryer")
  fi

  # --- Outils jamais installés automatiquement (dépendent du pilote / CUDA de la machine)
  have dcgmi || TODO+=("DCGM (optionnel) : paquet NVIDIA datacenter-gpu-manager-4-cudaNN correspondant au CUDA du pilote (dépôt NVIDIA apt ; pas le snap)")
  have nvbandwidth || TODO+=("nvbandwidth (optionnel) : https://github.com/NVIDIA/nvbandwidth (cmake)")
fi

# ── [5/5] Lanceur et raccourci : écrase uniquement notre propre fichier ─────
say ""
say "▸ [5/5] Lanceur et raccourci menu…"
for f in benchsuite.sh check_environment.py create_shortcuts.sh cli.py; do
  [ -f "$SCRIPT_DIR/$f" ] && run chmod +x "$SCRIPT_DIR/$f"
done
if [ "$DRY" -eq 1 ]; then
  say "    [dry-run] installation de l'icône ~/.local/share/icons/hicolor/scalable/apps/io.benchsuite.app.svg"
  say "    [dry-run] écriture de ~/.local/share/applications/io.benchsuite.app.desktop"
else
  ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
  mkdir -p "$ICON_DIR" "$HOME/.local/share/applications"
  if [ -f "$SCRIPT_DIR/assets/io.benchsuite.app.svg" ]; then
    cp -f "$SCRIPT_DIR/assets/io.benchsuite.app.svg" "$ICON_DIR/io.benchsuite.app.svg"
    gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
  fi
  rm -f "$HOME/.local/share/applications/benchsuite.desktop"
  cat > "$HOME/.local/share/applications/io.benchsuite.app.desktop" <<DESK
[Desktop Entry]
Version=1.0
Type=Application
Name=BenchSuite
GenericName=Benchmark Suite
Comment=Bench et recette matérielle — CPU, RAM, GPU, Stockage, Réseau
Exec="$SCRIPT_DIR/benchsuite.sh"
Icon=io.benchsuite.app
Terminal=false
Categories=System;Monitor;Utility;
Keywords=benchmark;performance;cpu;gpu;ram;storage;
StartupWMClass=io.benchsuite.app
StartupNotify=true
DESK
  chmod +x "$HOME/.local/share/applications/io.benchsuite.app.desktop"
  update-desktop-database "$HOME/.local/share/applications" 2>/dev/null || true
  say "  ✓ icône et raccourci mis à jour"
fi

say ""
say "▸ Vérification finale…"
[ "$DRY" -eq 1 ] || python3 "$SCRIPT_DIR/check_environment.py" || true

say ""
say "════════════════════════════════════════════════"
if [ "${#TODO[@]}" -gt 0 ]; then
  say "  À faire à la main (rien n'a été modifié pour ces points) :"
  for t in "${TODO[@]}"; do say "   • $t"; done
  say "────────────────────────────────────────────────"
fi
say "  ✓ Terminé — Lancer : $SCRIPT_DIR/benchsuite.sh"
say "════════════════════════════════════════════════"
exit 0
