# -*- coding: utf-8 -*-
"""Benchmarks réseau iperf3 (TCP et UDP) avec détection de boucle locale."""

import json
import os
import re
import signal
import subprocess
import threading
import time

from core.bench_utils import (
    BenchmarkResult,
    ProgressCb,
    _run,
    get_system_info,
    tool_exists,
    with_fault_watch,
)

def _iperf3_common_metrics(data: dict, udp: bool = False) -> tuple[dict, float | None, str]:
    metrics = {}
    end = data.get("end") or {}
    start = data.get("start") or {}
    connected = start.get("connected") or []
    if connected:
        c0 = connected[0]
        for src, dst in [("local_host", "local_host"), ("remote_host", "remote_host"),
                         ("local_port", "local_port"), ("remote_port", "remote_port")]:
            if src in c0:
                metrics[dst] = c0[src]
    if "system_info" in start:
        metrics["iperf_system_info"] = start.get("system_info")

    if udp:
        s = end.get("sum") or end.get("sum_received") or end.get("sum_sent") or {}
        bps = s.get("bits_per_second")
        if bps is not None:
            metrics["debit_Mbit_s"] = round(float(bps) / 1e6, 2)
        for src, dst in [("jitter_ms", "jitter_ms"), ("lost_packets", "lost_packets"),
                         ("packets", "packets"), ("lost_percent", "lost_percent")]:
            if src in s:
                metrics[dst] = round(float(s[src]), 3) if isinstance(s[src], float) else s[src]
        score = metrics.get("debit_Mbit_s")
        unit = "Mbit/s"
    else:
        s = end.get("sum_received") or end.get("sum") or end.get("sum_sent") or {}
        bps = s.get("bits_per_second")
        if bps is not None:
            metrics["debit_Mbit_s"] = round(float(bps) / 1e6, 2)
        rt = s.get("retransmits")
        if rt is None:                                   # TCP : les retransmissions sont côté émetteur (sum_sent)
            rt = (end.get("sum_sent") or {}).get("retransmits")
        if rt is not None:
            metrics["retransmits"] = rt
        cpu = end.get("cpu_utilization_percent") or {}
        for src, dst in [("host_total", "cpu_host_total_pct"), ("remote_total", "cpu_remote_total_pct")]:
            if src in cpu:
                metrics[dst] = round(float(cpu[src]), 2)
        score = metrics.get("debit_Mbit_s")
        unit = "Mbit/s"
    return metrics, score, unit


def _iperf3_loopback(host: str, parsed: dict) -> str:
    """Raison pour laquelle un test iperf3 n'a PAS traversé le réseau (chaîne vide sinon) : l'adresse visée est celle de cette machine.
    Linux envoie alors le trafic par l'interface interne (lo) sans passer par le câble : le débit mesuré (souvent supérieur à la vitesse du lien)
    est celui de la machine, pas celui du réseau. Détecté par : adresse de bouclage, adresse locale = adresse distante (réponse d'iperf3), ou
    adresse visée portée par une interface de cette machine."""
    h = (host or "").strip().lower()
    local = str((parsed or {}).get("local_host") or "").strip()
    remote = str((parsed or {}).get("remote_host") or "").strip()
    mine = False
    if h == "localhost" or h.startswith("127.") or h == "::1":
        mine = True
    elif local and remote and local == remote:
        mine = True
    else:
        try:
            from core.iperf_server import local_addresses
            mine = h in {a["ip"] for a in local_addresses()}
        except Exception:
            pass
    if not mine:
        return ""
    return (f"l'adresse visée ({host}) est une adresse de CETTE machine : le trafic est passé par l'interface interne (lo), pas par le câble ; "
            "le débit mesuré n'est PAS celui du lien réseau. Lancez le client sur l'autre machine, avec l'adresse du serveur.")


def _iperf3_run(cmd: list, time_s: int, kill_event=None) -> tuple:
    """Lance un client iperf3 (JSON) ; arrêt propre sur `kill_event`. Retourne (rc, stdout, stderr, arrêté_par_l_utilisateur)."""
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, start_new_session=True)
    except Exception as e:
        return -1, "", str(e), False
    t0, killed = time.time(), False
    while proc.poll() is None:
        if kill_event is not None and kill_event.is_set():
            killed = True
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except Exception:
                proc.kill()
            break
        if time.time() - t0 > time_s + 40:               # garde-fou : serveur muet / réseau coupé
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except Exception:
                proc.kill()
            return -2, "", f"délai dépassé ({time_s + 40} s) : pas de réponse du serveur iperf3", False
        time.sleep(0.3)
    try:
        out, err = proc.communicate(timeout=5)
    except Exception:
        out, err = "", ""
    return proc.returncode, out, err, killed


def _iperf3_error_text(out: str, err: str) -> str:
    """Message lisible d'un échec iperf3 : champ « error » du JSON, sinon dernière ligne de stderr."""
    try:
        e = json.loads(out).get("error")
        if e:
            return str(e)
    except Exception:
        pass
    lines = [l.strip() for l in (err or "").splitlines() if l.strip()]
    return lines[-1] if lines else "échec d'iperf3"


def _iperf3_hint(msg: str, host: str, port: int) -> str:
    """Piste de diagnostic pour les erreurs de connexion les plus courantes (serveur absent, pare-feu)."""
    m = (msg or "").lower()
    if "refused" in m:
        return (f"Connexion refusée par {host}:{port} : lancez le serveur sur l'autre machine (page « Serveur iperf3 » de BenchSuite "
                "ou `iperf3 -s`) et vérifiez que le port est ouvert dans le pare-feu.")
    if "unable to connect" in m or "no route" in m or "unreachable" in m or "timed out" in m or "délai" in m:
        return f"{host}:{port} injoignable : vérifiez l'adresse IP, le câblage / le switch et le pare-feu (ufw) du serveur."
    if "busy" in m:
        return "Le serveur fait déjà un test avec un autre client : attendez la fin ou relancez."
    return ""


def _bench_net_iperf(udp: bool, host, port, time_s, parallel, reverse, bandwidth, cb, kill_event, bind: str = "") -> BenchmarkResult:
    """Corps commun des tests iperf3 TCP / UDP (client)."""
    t0 = time.time()
    name = "Réseau iperf3 UDP" if udp else "Réseau iperf3 TCP"
    if not tool_exists("iperf3"):
        return BenchmarkResult(name, "Réseau", None, "Mbit/s", 0, "", status="skipped",
                               metrics={"erreur": "iperf3 non installé : sudo apt install -y iperf3"})
    host = (host or "127.0.0.1").strip()
    try:
        port = int(port or 5201); time_s = max(1, int(time_s or 10)); parallel = max(1, int(parallel or 1))
    except Exception:
        port, time_s, parallel = 5201, 10, 1
    cmd = ["iperf3", "-J", "-c", host, "-p", str(port), "-t", str(time_s), "-P", str(parallel)]
    metrics = {"host": host, "port": port, "time_s": time_s, "parallel": parallel}
    bind = (bind or "").strip()
    if bind:       # port d'envoi : adresse IPv4 d'une interface de CETTE machine (iperf3 -B) — force le test sur une carte précise
        if not re.fullmatch(r"\d{1,3}(\.\d{1,3}){3}", bind):
            return BenchmarkResult(name, "Réseau", None, "Mbit/s", 0, "", status="error", metrics={**metrics, "erreur": f"port d'envoi invalide : « {bind} » (adresse IPv4 d'une interface de cette machine attendue)"})
        cmd += ["-B", bind]
        metrics["bind"] = bind
    if udp:
        bandwidth = (bandwidth or "100M").strip()
        cmd[2:2] = ["-u", "-b", bandwidth]
        metrics["bandwidth"] = bandwidth
    elif reverse:
        cmd.append("-R")
        metrics["reverse"] = True
    metrics["command"] = " ".join(cmd)
    if cb: cb(10, f"iperf3 {'UDP' if udp else 'TCP'} → {host}:{port} ({time_s} s)…")
    rc, out, err, killed = _iperf3_run(cmd, time_s, kill_event)
    dur = time.time() - t0
    raw = out or err
    if killed:
        return BenchmarkResult(name, "Réseau", None, "Mbit/s", dur, raw, status="stopped",
                               notes="arrêté avant la fin", metrics=metrics, command=metrics["command"])
    if rc != 0:
        msg = _iperf3_error_text(out, err)
        metrics["erreur"] = msg
        hint = _iperf3_hint(msg, host, port)
        if hint:
            metrics["piste"] = hint
        if cb: cb(100, "Erreur iperf3")
        return BenchmarkResult(name, "Réseau", None, "Mbit/s", dur, raw, status="error", notes=(hint or msg)[:200],
                               metrics=metrics, command=metrics["command"])
    try:
        parsed, score, unit = _iperf3_common_metrics(json.loads(out), udp=udp)
        metrics.update(parsed)
        status = "ok" if score is not None else "error"
        loop = _iperf3_loopback(host, parsed)
        if loop and status == "ok":
            metrics["boucle_locale"] = True
            metrics["avertissement"] = loop
    except Exception as e:
        score, unit, status = None, "Mbit/s", "error"
        metrics["erreur"] = f"JSON iperf3 invalide : {e}"
    if cb: cb(100, "Terminé")
    return BenchmarkResult(name, "Réseau", score, unit, dur, raw, status=status, metrics=metrics,
                           notes=("⚠ " + metrics["avertissement"]) if metrics.get("avertissement") else "",
                           system_info=get_system_info(), command=metrics["command"])


@with_fault_watch
def bench_net_iperf_tcp(
    host: str = "127.0.0.1",
    port: int = 5201,
    time_s: int = 10,
    parallel: int = 1,
    reverse: bool = False,
    bind: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    """Débit TCP vers un serveur iperf3 (autre machine : `iperf3 -s` ou page « Serveur iperf3 »). `bind` = IP locale d'envoi (port précis)."""
    return _bench_net_iperf(False, host, port, time_s, parallel, reverse, None, cb, kill_event, bind)


@with_fault_watch
def bench_net_iperf_udp(
    host: str = "127.0.0.1",
    port: int = 5201,
    time_s: int = 10,
    bandwidth: str = "100M",
    parallel: int = 1,
    bind: str = "",
    cb: ProgressCb = None,
    kill_event: "threading.Event | None" = None,
) -> BenchmarkResult:
    """Débit, gigue et pertes UDP vers un serveur iperf3. `bind` = IP locale d'envoi (port précis)."""
    return _bench_net_iperf(True, host, port, time_s, parallel, False, bandwidth, cb, kill_event, bind)
