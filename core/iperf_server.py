"""Serveur iperf3 piloté depuis BenchSuite : une machine fait serveur, l'autre lance le test client (page « Serveur iperf3 »).

Principe : aucun protocole entre deux BenchSuite — le serveur est un simple processus `iperf3 -s`. Ce module le démarre / l'arrête
proprement (groupe de processus, nettoyage à la sortie), expose les adresses IP à donner au client et les dernières lignes du journal.
Best-effort : iperf3 absent, port occupé ou droits manquants → message clair, jamais d'exception vers l'interface.
"""
import atexit
import collections
import os
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
from typing import Dict, List, Tuple

DEFAULT_PORT = 5201
INSTALL_CMDS = [("Ubuntu / Debian (apt)", "sudo apt install -y iperf3")]


def iperf3_available() -> bool:
    """Vrai si le binaire `iperf3` est dans le PATH."""
    return bool(shutil.which("iperf3"))


def local_addresses() -> List[Dict]:
    """Adresses IPv4 utilisables par un client : [{iface, ip, speed_mbps}], hors loopback, interfaces actives d'abord."""
    out: List[Dict] = []
    try:
        import psutil
        stats = psutil.net_if_stats()
        for iface, addrs in psutil.net_if_addrs().items():
            if iface == "lo":
                continue
            for a in addrs:
                if a.family == socket.AF_INET and a.address and not a.address.startswith("127."):
                    st = stats.get(iface)
                    out.append({"iface": iface, "ip": a.address, "up": bool(st and st.isup),
                                "speed_mbps": int(st.speed) if st and st.speed and st.speed > 0 else 0})
    except Exception:
        pass
    if not out:                      # repli sans psutil : `hostname -I`
        try:
            r = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=3)
            for ip in r.stdout.split():
                if re.match(r"^\d+\.\d+\.\d+\.\d+$", ip) and not ip.startswith("127."):
                    out.append({"iface": "?", "ip": ip, "up": True, "speed_mbps": 0})
        except Exception:
            pass
    out.sort(key=lambda d: (not d["up"], d["ip"].startswith("169.254."), d["iface"]))
    return out


SYSFS_NET = "/sys/class/net"
TEST_NET = "192.168.100"        # réseau de test proposé pour relier deux machines en direct (changé s'il est déjà utilisé ici)


def _rd(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except Exception:
        return ""


def ethernet_ports(base: str = None, ipv4_fn=None) -> List[Dict]:
    """Ports Ethernet PHYSIQUES de la machine (cartes réseau, ports de la carte mère), même sans adresse IP ni câble.

    Chaque port : {iface, mac, link (câble branché : True / False / None si inconnu), speed_mbps, driver, pci, usb, ipv4: [{ip, prefix}]}.
    Les interfaces virtuelles (pont, veth, docker, VLAN, tunnel…) n'ont pas de matériel derrière (`device`) et sont ignorées, de même que le Wi-Fi.
    `usb` = port vu comme périphérique USB : souvent l'interface « hôte » du BMC (Redfish over USB), pas un vrai port à câbler.
    Lecture sysfs sans droits ; `ipv4_fn` (tests) remplace la lecture des adresses par psutil. Jamais d'exception."""
    base = base or SYSFS_NET
    addr_by_if: Dict[str, List[Dict]] = {}
    try:
        if ipv4_fn is not None:
            addr_by_if = ipv4_fn()
        else:
            import ipaddress
            import psutil
            for iface, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family == socket.AF_INET and a.address:
                        try:
                            prefix = ipaddress.IPv4Network(f"0.0.0.0/{a.netmask}").prefixlen if a.netmask else 0
                        except Exception:
                            prefix = 0
                        addr_by_if.setdefault(iface, []).append({"ip": a.address, "prefix": prefix})
    except Exception:
        addr_by_if = {}
    out: List[Dict] = []
    try:
        names = sorted(os.listdir(base))
    except Exception:
        return out
    for iface in names:
        d = os.path.join(base, iface)
        try:
            if iface == "lo" or not os.path.exists(os.path.join(d, "device")):
                continue
            if os.path.isdir(os.path.join(d, "wireless")) or _rd(os.path.join(d, "type")) not in ("", "1"):
                continue                      # Wi-Fi, InfiniBand… : pas de l'Ethernet
            carrier = _rd(os.path.join(d, "carrier"))       # illisible quand l'interface est désactivée
            speed = _rd(os.path.join(d, "speed"))
            dev = os.path.realpath(os.path.join(d, "device"))
            out.append({"iface": iface, "mac": _rd(os.path.join(d, "address")),
                        "link": (carrier == "1") if carrier in ("0", "1") else None,
                        "speed_mbps": int(speed) if speed.lstrip("-").isdigit() and int(speed) > 0 else 0,
                        "driver": os.path.basename(os.path.realpath(os.path.join(d, "device", "driver"))) if os.path.exists(os.path.join(d, "device", "driver")) else "",
                        "pci": os.path.basename(dev), "usb": "/usb" in dev,
                        "ipv4": addr_by_if.get(iface, [])})
        except Exception:
            continue
    out.sort(key=lambda p: (p["usb"], p["pci"], p["iface"]))
    return out


def suggest_test_network(ports: List[Dict], taken: List[str] = None) -> str:
    """Préfixe « a.b.c » d'un réseau /24 de test non utilisé sur cette machine (192.168.100 par défaut, puis 101, 102…) et absent de `taken`
    (réseaux déjà proposés à un autre port : deux ports d'une même machine ne doivent pas partager le même réseau)."""
    used = set(taken or [])
    for p in ports:
        for a in p.get("ipv4", []):
            parts = a["ip"].split(".")
            if len(parts) == 4:
                used.add(".".join(parts[:3]))
    for n in range(100, 120):
        pref = f"192.168.{n}"
        if pref not in used:
            return pref
    return TEST_NET


def port_commands(iface: str, net: str = None, last: int = 1) -> List[Tuple[str, str]]:
    """Commandes (à copier) pour donner une adresse TEMPORAIRE à un port (perdue au redémarrage), le repérer et la retirer."""
    net = net or TEST_NET
    return [("Donner une adresse (temporaire)", f"sudo ip addr add {net}.{int(last)}/24 dev {iface} && sudo ip link set {iface} up"),
            ("Repérer le port (LED qui clignote 10 s)", f"sudo ethtool -p {iface} 10"),
            ("Retirer l'adresse", f"sudo ip addr del {net}.{int(last)}/24 dev {iface}")]


def port_is_free(port: int) -> bool:
    """Vrai si le port TCP est libre sur cette machine (un serveur iperf3 déjà lancé l'occupe)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind(("0.0.0.0", int(port)))
        return True
    except Exception:
        return False


def firewall_commands(port: int) -> List[Tuple[str, str]]:
    """Commandes (à copier) pour ouvrir le port TCP + UDP du serveur."""
    return [("Ubuntu / Debian (ufw)", f"sudo ufw allow {int(port)}")]


def client_command(ip: str, port: int) -> str:
    """Commande iperf3 à lancer sur la machine cliente (hors BenchSuite)."""
    return f"iperf3 -c {ip} -p {int(port)}"


def client_command_generic(port: int) -> str:
    """Même commande avec une valeur à remplacer : l'adresse du SERVEUR (celle du port relié par le câble, pas celle du réseau de gestion)."""
    return f"iperf3 -c <IP-du-serveur> -p {int(port)}"


def valid_net(prefix: str) -> bool:
    """Vrai pour un préfixe de réseau /24 « a.b.c » (chaque nombre de 0 à 255)."""
    parts = (prefix or "").strip().split(".")
    return len(parts) == 3 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)


def other_number(n: int) -> int:
    """Numéro de l'AUTRE machine quand on relie deux machines (1 ↔ 2)."""
    return 2 if int(n) == 1 else 1


class IperfServer:
    """Un serveur iperf3 (processus enfant) : démarrage, arrêt, journal et compteur de connexions."""

    def __init__(self):
        self._proc = None
        self._lock = threading.Lock()
        self.port = DEFAULT_PORT
        self.started_at = 0.0
        self.log = collections.deque(maxlen=300)
        self.connections = 0
        self.last_client = ""

    def is_running(self) -> bool:
        p = self._proc
        return p is not None and p.poll() is None

    def _reader(self, proc):
        """Lit la sortie d'iperf3 (thread daemon) : journal + détection des connexions."""
        try:
            for line in proc.stdout:
                line = line.rstrip("\n")
                if not line:
                    continue
                self.log.append(time.strftime("%H:%M:%S ") + line)
                m = re.search(r"Accepted connection from ([0-9a-fA-F:.]+)", line)
                if m:
                    self.connections += 1
                    self.last_client = m.group(1)
        except Exception:
            pass

    def start(self, port: int = DEFAULT_PORT) -> Tuple[bool, str]:
        """Démarre `iperf3 -s`. Retourne (ok, message) ; le serveur reste actif jusqu'à stop() ou la fermeture de BenchSuite."""
        with self._lock:
            if self.is_running():
                return True, f"serveur déjà actif sur le port {self.port}"
            if not iperf3_available():
                return False, "iperf3 n'est pas installé : " + " ou ".join(c for _, c in INSTALL_CMDS)
            try:
                port = int(port)
                if not 1 <= port <= 65535:
                    raise ValueError
            except Exception:
                return False, "port invalide (1 – 65535)"
            if not port_is_free(port):
                return False, f"le port {port} est déjà utilisé (un autre serveur iperf3 tourne peut-être) : choisissez un autre port"
            self.log.clear()
            self.connections, self.last_client = 0, ""
            for extra in (["--forceflush"], []):          # --forceflush : journal en direct ; retiré si iperf3 trop ancien
                try:
                    proc = subprocess.Popen(["iperf3", "-s", "-p", str(port)] + extra,
                                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
                                            start_new_session=True)
                except Exception as e:
                    return False, f"lancement impossible : {e}"
                time.sleep(0.4)
                if proc.poll() is None:
                    self._proc, self.port, self.started_at = proc, port, time.time()
                    threading.Thread(target=self._reader, args=(proc,), daemon=True).start()
                    return True, f"serveur iperf3 démarré sur le port {port}"
                msg = (proc.stdout.read() or "").strip()
                if extra and "unrecognized option" in msg.lower():
                    continue
                return False, "iperf3 s'est arrêté : " + (msg.splitlines()[-1] if msg else f"code {proc.returncode}")
            return False, "iperf3 n'a pas pu démarrer"

    def stop(self) -> None:
        """Arrête le serveur (SIGTERM au groupe de processus, puis SIGKILL si besoin). Sans effet s'il n'est pas lancé."""
        with self._lock:
            p, self._proc = self._proc, None
        if p is None or p.poll() is not None:
            return
        try:
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    def uptime_s(self) -> int:
        return int(time.time() - self.started_at) if self.is_running() else 0


SERVER = IperfServer()
atexit.register(SERVER.stop)         # jamais de serveur orphelin après la fermeture de BenchSuite
