import json
import os
import threading

_CONFIG_PATH = os.path.expanduser("~/.benchsuite_sensors.json")
_config_lock = threading.Lock()
_sensors_cache = None

def load_sensor_config() -> dict:
    """Charge la configuration des capteurs sélectionnés (clé: nom, valeur: booléen)"""
    global _sensors_cache
    with _config_lock:
        if _sensors_cache is not None:
            return _sensors_cache
        if not os.path.exists(_CONFIG_PATH):
            _sensors_cache = {}
            return _sensors_cache
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                _sensors_cache = json.load(f)
                return _sensors_cache
        except Exception:
            _sensors_cache = {}
            return _sensors_cache

def save_sensor_config(config: dict):
    """Enregistre la configuration des capteurs sélectionnés"""
    global _sensors_cache
    with _config_lock:
        _sensors_cache = config.copy()
        try:
            with open(_CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(_sensors_cache, f, indent=4)
        except Exception:
            pass

def is_sensor_enabled(sensor_name: str) -> bool:
    """Renvoie True si le capteur doit être loggé/affiché. Par défaut, False."""
    config = load_sensor_config()
    return config.get(sensor_name, False)
