"""pmbus_i2c.py — Gestion et surveillance PMBus via communication I2C/SMBus directe sous Linux.

Ce module interagit directement avec les fichiers de périphériques /dev/i2c-N
en utilisant des appels ioctl systèmes natifs (sans dépendance externe type smbus/smbus2),
ce qui le rend robuste et compatible avec n'importe quelle distribution Linux.
"""

import os
import fcntl
import struct
import ctypes

# Constantes de l'ioctl I2C sous Linux
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

# Structures C ctypes de l'ioctl I2C
class I2CMsg(ctypes.Structure):
    _fields_ = [
        ("addr", ctypes.c_uint16),
        ("flags", ctypes.c_uint16),
        ("len", ctypes.c_uint16),
        ("buf", ctypes.c_void_p),
    ]

class I2CRdwrData(ctypes.Structure):
    _fields_ = [
        ("msgs", ctypes.POINTER(I2CMsg)),
        ("nmsgs", ctypes.c_uint32),
    ]


def read_byte(fd: int, addr: int, reg: int) -> int:
    """Lit un octet depuis un registre I2C en combinant écriture de l'adresse et lecture."""
    write_buf = ctypes.create_string_buffer(bytes([reg]))
    read_buf = ctypes.create_string_buffer(1)

    msgs = (I2CMsg * 2)()
    
    msgs[0].addr = addr
    msgs[0].flags = 0  # Écriture
    msgs[0].len = 1
    msgs[0].buf = ctypes.cast(write_buf, ctypes.c_void_p)
    
    msgs[1].addr = addr
    msgs[1].flags = I2C_M_RD  # Lecture
    msgs[1].len = 1
    msgs[1].buf = ctypes.cast(read_buf, ctypes.c_void_p)

    data = I2CRdwrData()
    data.msgs = msgs
    data.nmsgs = 2

    fcntl.ioctl(fd, I2C_RDWR, data)
    return ord(read_buf.raw[0])


def read_word(fd: int, addr: int, reg: int) -> int:
    """Lit un mot de 16 bits (Word) depuis un registre I2C."""
    write_buf = ctypes.create_string_buffer(bytes([reg]))
    read_buf = ctypes.create_string_buffer(2)

    msgs = (I2CMsg * 2)()
    
    msgs[0].addr = addr
    msgs[0].flags = 0  # Écriture
    msgs[0].len = 1
    msgs[0].buf = ctypes.cast(write_buf, ctypes.c_void_p)
    
    msgs[1].addr = addr
    msgs[1].flags = I2C_M_RD  # Lecture
    msgs[1].len = 2
    msgs[1].buf = ctypes.cast(read_buf, ctypes.c_void_p)

    data = I2CRdwrData()
    data.msgs = msgs
    data.nmsgs = 2

    fcntl.ioctl(fd, I2C_RDWR, data)
    return struct.unpack("<H", read_buf.raw[:2])[0]


def read_block(fd: int, addr: int, reg: int, max_len: int = 32) -> str:
    """Lit un bloc de octets au format SMBus (longueur en premier octet)."""
    write_buf = ctypes.create_string_buffer(bytes([reg]))
    read_buf = ctypes.create_string_buffer(max_len + 1)

    msgs = (I2CMsg * 2)()
    
    msgs[0].addr = addr
    msgs[0].flags = 0  # Écriture
    msgs[0].len = 1
    msgs[0].buf = ctypes.cast(write_buf, ctypes.c_void_p)
    
    msgs[1].addr = addr
    msgs[1].flags = I2C_M_RD  # Lecture
    msgs[1].len = max_len + 1
    msgs[1].buf = ctypes.cast(read_buf, ctypes.c_void_p)

    data = I2CRdwrData()
    data.msgs = msgs
    data.nmsgs = 2

    try:
        fcntl.ioctl(fd, I2C_RDWR, data)
        length = ord(read_buf.raw[0])
        length = min(length, max_len)
        chars = [chr(c) for c in read_buf.raw[1:1+length] if 32 <= c <= 126]
        return "".join(chars).strip()
    except Exception:
        return ""


def parse_linear11(val: int) -> float:
    """Décode le format PMBus Linear11 (exposant de 5 bits, mantisse de 11 bits, complément à 2)."""
    # Exposant (bits 11-15)
    exponent_raw = (val >> 11) & 0x1F
    if exponent_raw & 0x10:
        exponent = exponent_raw - 32
    else:
        exponent = exponent_raw

    # Mantisse (bits 0-10)
    mantisse_raw = val & 0x7FF
    if mantisse_raw & 0x400:
        mantisse = mantisse_raw - 2048
    else:
        mantisse = mantisse_raw

    return float(mantisse * (2 ** exponent))


def parse_linear16(val: int, exponent: int = -12) -> float:
    """Décode le format PMBus Linear16 (mantisse non signée de 16 bits, exposant partagé)."""
    return float(val * (2 ** exponent))


def scan_pmbus_devices(max_seconds: float = 20.0) -> list:
    """Scanne les bus I2C du système pour trouver des périphériques répondant au protocole PMBus.

    `max_seconds` : durée maximale du balayage (20 s). Il lit 112 adresses par bus avec des ioctl que rien ne peut interrompre ;
    sur un serveur avec de nombreux bus (BMC, GPU, modules mémoire) ou un bus qui étire l'horloge, chaque adresse peut attendre le
    délai du pilote et le balayage durait des minutes. Au-delà du budget il s'arrête et rend ce qui a été trouvé."""
    import time
    deadline = time.time() + max(1.0, float(max_seconds))
    devices = []
    
    # Trouver tous les adaptateurs i2c dans le sysfs
    try:
        adapters = [int(f.replace("i2c-", "")) for f in os.listdir("/sys/class/i2c-adapter/") if f.startswith("i2c-")]
    except Exception:
        # Fallback si sysfs non disponible
        import glob
        adapters = []
        for p in glob.glob("/dev/i2c-*"):
            try:
                adapters.append(int(p.replace("/dev/i2c-", "")))
            except Exception:
                pass

    for bus in sorted(adapters):
        if time.time() > deadline:
            break
        bus_path = f"/dev/i2c-{bus}"
        if not os.path.exists(bus_path):
            continue
        
        try:
            fd = os.open(bus_path, os.O_RDWR)
        except Exception:
            continue  # Pas les permissions ou bus occupé
        
        try:
            # Gamme standard des adresses I2C utilisateur de confiance
            for addr in range(0x08, 0x78):
                if time.time() > deadline:
                    break
                try:
                    # PMBUS_REVISION (0x98) est un registre standard sûr à lire
                    rev = read_byte(fd, addr, 0x98)
                    if rev in (0x11, 0x12, 0x22, 0x33): # révisions standards PMBus 1.1, 1.2, 1.3
                        devices.append((bus, addr))
                    else:
                        # Si rev est un octet valide non-zéro, on le garde aussi
                        if rev > 0:
                            devices.append((bus, addr))
                except Exception:
                    pass
        finally:
            os.close(fd)
            
    return devices


def get_pmbus_telemetry(bus: int, addr: int) -> dict:
    """Récupère l'intégralité de la télémétrie d'un périphérique PMBus en I2C direct."""
    path = f"/dev/i2c-{bus}"
    if not os.path.exists(path):
        return {}
    
    try:
        fd = os.open(path, os.O_RDWR)
    except Exception:
        return {}
        
    try:
        # Métadonnées
        mfr_id = read_block(fd, addr, 0x99)
        mfr_model = read_block(fd, addr, 0x9A)
        rev = read_byte(fd, addr, 0x98)
        
        # Déterminer l'exposant VOUT_MODE (0x20)
        vout_exp = -12
        try:
            mode = read_byte(fd, addr, 0x20)
            exp_raw = mode & 0x1F
            if exp_raw & 0x10:
                vout_exp = exp_raw - 32
            else:
                vout_exp = exp_raw
        except Exception:
            pass

        def get_l11(reg):
            try:
                w = read_word(fd, addr, reg)
                return round(parse_linear11(w), 3)
            except Exception:
                return None

        def get_l16(reg, exp):
            try:
                w = read_word(fd, addr, reg)
                return round(parse_linear16(w, exp), 3)
            except Exception:
                return None

        return {
            "bus": bus,
            "address": f"0x{addr:02X}",
            "manufacturer": mfr_id or "Inconnu",
            "model": mfr_model or "Inconnu",
            "revision": f"0x{rev:02X}" if rev else "Inconnu",
            "vin": get_l11(0x88),        # Tension entrée (V)
            "iin": get_l11(0x89),        # Courant entrée (A)
            "pin": get_l11(0x97),        # Puissance entrée (W)
            "vout": get_l16(0x8B, vout_exp), # Tension sortie (V)
            "iout": get_l11(0x8C),       # Courant sortie (A)
            "pout": get_l11(0x96),       # Puissance sortie (W)
            "temp1": get_l11(0x8D),      # Température 1 (°C)
            "temp2": get_l11(0x8E),      # Température 2 (°C)
            "fan1": get_l11(0x90),       # Ventilateur 1 (RPM)
        }
    finally:
        os.close(fd)
