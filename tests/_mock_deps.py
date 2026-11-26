# -*- coding: utf-8 -*-
"""Aides communes pour exécuter les tests unitaires même sans paquets système optionnels."""

import sys
import types


def ensure_psutil_stub() -> None:
    """Injecte un stub minimal de `psutil` si absent du conteneur de test."""
    try:
        import psutil  # noqa: F401
        return
    except ImportError:
        pass
    m = types.ModuleType("psutil")
    m.cpu_count = lambda logical=True: 8
    m.cpu_freq = lambda: types.SimpleNamespace(current=2400.0, min=800.0, max=3500.0)
    m.cpu_percent = lambda interval=None, percpu=False: ([10.0] * 8 if percpu else 10.0)
    m.virtual_memory = lambda: types.SimpleNamespace(
        total=16 * 1024**3, available=12 * 1024**3, used=4 * 1024**3, percent=25.0
    )
    m.disk_partitions = lambda all=False: []
    m.net_if_addrs = lambda: {}
    m.net_if_stats = lambda: {}
    m.sensors_temperatures = lambda: {}
    m.Process = lambda pid=None: types.SimpleNamespace(
        children=lambda recursive=True: [], terminate=lambda: None, kill=lambda: None
    )
    sys.modules["psutil"] = m
