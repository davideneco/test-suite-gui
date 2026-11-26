#!/usr/bin/env python3
"""
Vérification rapide de l'environnement BenchSuite.
Ne lance aucun benchmark lourd : contrôle seulement Python, GTK/Adw et outils externes.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys

REQUIRED_PYTHON = (3, 10)
PYTHON_MODULES = [
    ("gi", "python3-gi"),
    ("cairo", "python3-cairo / python3-gi-cairo"),
    ("psutil", "python3-psutil ou pip install psutil"),
    ("reportlab", "python3-reportlab ou pip install reportlab"),
]
HARDWARE_TOOLS = [
    ("lspci", "Inventaire PCI/PCIe"),
    ("ethtool", "Infos réseau, pilote, bus PCI"),
    ("nvme", "Infos NVMe SMART/firmware"),
    ("ipmitool", "Infos BMC/IPMI"),
    ("smartctl", "Santé SMART SSD/HDD"),
    ("dmidecode", "DMI carte mère/RAM/BIOS"),
]

BENCH_TOOLS = [
    ("stress-ng", "Stress CPU"),
    ("fio", "Stockage avancé"),
    ("nvidia-smi", "Infos/capteurs NVIDIA"),
    ("gpu-burn", "Stress CUDA gpu-burn"),
    ("gpu-fryer", "Stress CUDA/ROCm gpu-fryer"),
]


def mark(ok: bool) -> str:
    return "✓" if ok else "✗"


def main() -> int:
    print("BenchSuite — contrôle de santé")
    print("=" * 42)

    py_ok = sys.version_info >= REQUIRED_PYTHON
    print(f"{mark(py_ok)} Python {sys.version.split()[0]} (minimum {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]})")

    errors = 0 if py_ok else 1
    print("\nModules Python / GTK")
    for mod, hint in PYTHON_MODULES:
        ok = importlib.util.find_spec(mod) is not None
        print(f"{mark(ok)} {mod:<10}  {'' if ok else 'à installer: ' + hint}")
        errors += 0 if ok else 1

    # Contrôle GI plus strict : Gtk 4 + Adw 1 sont indispensables à l'interface.
    try:
        import gi  # type: ignore
        gi.require_version("Gtk", "4.0")
        gi.require_version("Adw", "1")
        from gi.repository import Gtk, Adw  # noqa: F401  # type: ignore
        print("✓ Gtk 4 + Libadwaita disponibles")
    except Exception as exc:
        errors += 1
        print(f"✗ Gtk 4 + Libadwaita indisponibles: {exc}")
    # pycairo seul ne suffit pas : sans python3-gi-cairo les graphiques lèvent « foreign struct converter for cairo.Context »
    try:
        import gi  # type: ignore
        gi.require_foreign("cairo")
        print("✓ pont GTK↔cairo (python3-gi-cairo) disponible")
    except Exception:
        print("⚠ pont GTK↔cairo absent : graphiques désactivés (sudo apt install python3-gi-cairo)")

    print("\nOutils inventaire matériel")
    missing_optional = []
    for tool, label in HARDWARE_TOOLS:
        ok = shutil.which(tool) is not None
        if tool == "gpu-burn":
            ok = ok or shutil.which("gpu_burn") is not None or os.path.exists("/snap/bin/gpu-burn") or os.path.exists("/snap/bin/gpu_burn")
        print(f"{mark(ok)} {tool:<13}  {label}")
        if not ok:
            missing_optional.append(tool)

    print("\nOutils benchmark")
    for tool, label in BENCH_TOOLS:
        ok = shutil.which(tool) is not None
        if tool == "gpu-burn":
            ok = ok or shutil.which("gpu_burn") is not None or os.path.exists("/snap/bin/gpu-burn") or os.path.exists("/snap/bin/gpu_burn")
        print(f"{mark(ok)} {tool:<13}  {label}")
        if not ok:
            missing_optional.append(tool)

    print("\nDossiers")
    out_dir = os.path.expanduser("~/BenchmarkResults")
    try:
        os.makedirs(out_dir, exist_ok=True)
        print(f"✓ Résultats: {out_dir}")
    except Exception as exc:
        errors += 1
        print(f"✗ Impossible d'écrire dans {out_dir}: {exc}")

    if missing_optional:
        print("\nNote: les outils benchmark manquants ne bloquent pas le lancement; les tests concernés seront marqués comme indisponibles.")

    print("\nRésultat")
    if errors:
        print(f"✗ {errors} problème(s) bloquant(s) détecté(s). Lance ./install.sh puis réessaie.")
        return 1
    print("✓ Environnement prêt pour lancer ./benchsuite.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
