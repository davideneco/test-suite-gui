# -*- coding: utf-8 -*-
"""Fonctions communes aux exports BenchSuite (métadonnées, synthèses GPU/système, verdict, compactage JSON)."""

import json
import os
import re
from datetime import datetime
from typing import Dict, List, Optional

from core.result import BenchmarkResult, collect_dmesg_log

EXPORT_SEQUENCE_ELECTRONICS = False


def _app_version() -> str:
    """Version lue dans exporters.export (source unique mise à jour par bump_version.py)."""
    try:
        from exporters import export as _ex
        return str(_ex.APP_VERSION)
    except Exception:
        return "1.0.1"


def _seq_elec_enabled() -> bool:
    """Lit EXPORT_SEQUENCE_ELECTRONICS depuis exporters.export si chargé."""
    try:
        from exporters import export as _ex
        return bool(_ex.EXPORT_SEQUENCE_ELECTRONICS)
    except Exception:
        return bool(EXPORT_SEQUENCE_ELECTRONICS)


def _ts() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")

def _safe_text(v):
    if v is None:
        return ""
    if isinstance(v, (dict, list, tuple)):
        try:
            return json.dumps(v, ensure_ascii=False)
        except Exception:
            return str(v)
    return str(v)


def _report_system_name(report_meta: dict = None) -> str:
    if report_meta and report_meta.get("system_name"):
        return str(report_meta["system_name"]).strip()
    env = os.environ.get("BENCHSUITE_SYSTEM_NAME", "").strip()
    if env:
        return env
    cfg = os.path.expanduser("~/.config/benchsuite/report_meta.json")
    try:
        if os.path.exists(cfg):
            meta = json.load(open(cfg, "r", encoding="utf-8"))
            if meta.get("system_name"):
                return meta["system_name"]
    except Exception:
        pass
    # Fallback
    old_cfg = os.path.expanduser("~/.config/benchsuite/system_name")
    try:
        if os.path.exists(old_cfg):
            val = open(old_cfg, "r", encoding="utf-8").read().strip()
            if val:
                return val
    except Exception:
        pass
    try:
        import socket
        return socket.gethostname()
    except Exception:
        return "Système testé"


def save_report_meta(meta: dict) -> str:
    """Sauvegarde les métadonnées (nom système, testeur, client) du rapport."""
    cfg_dir = os.path.expanduser("~/.config/benchsuite")
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "report_meta.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)
    # Compatibility
    try:
        with open(os.path.join(cfg_dir, "system_name"), "w", encoding="utf-8") as f:
            f.write(meta.get("system_name", "").strip())
    except Exception:
        pass
    return path


def load_report_meta() -> dict:
    cfg = os.path.expanduser("~/.config/benchsuite/report_meta.json")
    if os.path.exists(cfg):
        try:
            return json.load(open(cfg, "r", encoding="utf-8"))
        except Exception:
            pass
    old_cfg = os.path.expanduser("~/.config/benchsuite/system_name")
    name = ""
    try:
        if os.path.exists(old_cfg):
            name = open(old_cfg, "r", encoding="utf-8").read().strip()
    except Exception:
        pass
    return {"system_name": name, "operator": "", "client": ""}


# ─────────────────────────────────────────────────────────────────────────────
# Synthèses GPU / système partagées par les exports PDF, HTML et CSV
# ─────────────────────────────────────────────────────────────────────────────

_GPU_METRIC_RE = re.compile(r"^gpu(\d+)_(.+)$")


def _gpu_list_from(system_info_data: dict = None, payload: dict = None) -> list:
    """Liste des GPU du snapshot (clé « gpus », ou « gpu » pour les anciens rapports)."""
    snap = {}
    if payload:
        snap = (payload.get("system") or {}).get("snapshot_at_start") or {}
    for src in (snap, system_info_data or {}):
        gl = src.get("gpus") or src.get("gpu")
        if isinstance(gl, list) and gl:
            return [g for g in gl if isinstance(g, dict)]
    return []


def _is_compute_gpu(g: dict) -> bool:
    """Vrai pour une carte de calcul/graphique réelle (hors VGA BMC type ASPEED)."""
    if g.get("vram_total_mo") or g.get("serial"):
        return True
    name = str(g.get("nom") or g.get("modele") or "").lower()
    return not any(x in name for x in ("aspeed", "matrox", "vga compatible"))


def _gpu_labels(gpu_list: list) -> dict:
    """{index: 'GPU 0 · 07:00.0 · MCIO1 + MCIO2'} à partir du snapshot système."""
    labels = {}
    real = [g for g in gpu_list if _is_compute_gpu(g)]
    for pos, g in enumerate(real):
        try:
            idx = int(g.get("index", pos))
        except (TypeError, ValueError):
            idx = pos
        bus = str(g.get("pci_bus") or "")
        short = _pci_label(bus)
        label = f"GPU {idx}"
        if short:
            label += f" · {short.lower()}"
        if g.get("mcio"):
            label += f" · {g['mcio']}"
        labels[idx] = label
    return labels


_GPU_EXTRA_KEYS = ("util_moy_pct", "util_max_pct", "mem_used_max_mib", "mem_util_max_pct", "temp_vr_max_c",
                   "temp_mctrl_max_c", "temp_pcie_max_c", "temp_vram_ch_max_c", "fan_rpm_max")


def _gpu_extra_text(d: dict) -> str:
    """Activité et capteurs supplémentaires d'un GPU (utilisation, mémoire utilisée, VR / contrôleur mémoire / PCIe / canaux VRAM,
    ventilateur) sous forme de texte compact ; '' si aucune de ces mesures n'existe (GPU NVIDIA : colonne absente)."""
    parts = []
    if d.get("util_moy_pct") is not None:
        parts.append(f"utilisation {_fmt_num(d['util_moy_pct'], 0)} % (max {_fmt_num(d.get('util_max_pct'), 0)} %)")
    if d.get("mem_used_max_mib") is not None:
        parts.append(f"VRAM utilisée max {_fmt_num(float(d['mem_used_max_mib']) / 1024, 1)} Go")
    sec = []
    for key, lab in (("temp_vr_max_c", "régulateur VR"), ("temp_mctrl_max_c", "contrôleur mémoire"),
                     ("temp_pcie_max_c", "interface PCIe"), ("temp_vram_ch_max_c", "canaux VRAM")):
        if d.get(key) is not None:
            sec.append(f"{lab} {_fmt_num(d[key], 0)} °C")
    if sec:
        parts.append("capteurs secondaires : " + ", ".join(sec))
    if d.get("fan_rpm_max") is not None:
        parts.append(f"ventilateur {_fmt_num(d['fan_rpm_max'], 0)} tr/min")
    return " · ".join(parts)


def _gpu_rows_for_result(result, labels: dict = None) -> list:
    """Une ligne de synthèse par GPU (valeurs en charge stabilisée) pour un résultat."""
    m = result.metrics or {}
    per = {}
    for k, v in m.items():
        mt = _GPU_METRIC_RE.match(str(k))
        if mt and not isinstance(v, (list, dict)):
            per.setdefault(int(mt.group(1)), {})[mt.group(2)] = v
    rows = []
    for idx in sorted(per):
        d = per[idx]
        rows.append({
            "index": idx,
            "label": (labels or {}).get(idx, f"GPU {idx}"),
            "gflops": d.get("Gflops"),
            "temp_max": d.get("temp_max_c"), "temp_moy": d.get("temp_moy_c"),
            "hs_max": d.get("temp_hotspot_max_c"), "hs_moy": d.get("temp_hotspot_moy_c"),
            "vram_max": d.get("temp_vram_max_c"),
            "delta_moy": d.get("delta_hotspot_moy_c"), "delta_p95": d.get("delta_hotspot_p95_c"),
            "delta_max": d.get("delta_hotspot_max_c"), "delta_status": d.get("delta_hotspot_status"),
            "power_moy": d.get("power_moy_w"), "power_max": d.get("power_max_w"),
            "fmin": d.get("freq_graphics_min_mhz", d.get("freq_min_mhz")),
            "fmax": d.get("freq_graphics_max_mhz", d.get("freq_max_mhz")),
            "vram_bw": d.get("vram_bw_gbs"),
            "extra": _gpu_extra_text(d),
            "extras": {k: d[k] for k in _GPU_EXTRA_KEYS if d.get(k) is not None},
            "throttle": bool(d.get("throttle_thermal_detected")),
            "throttle_n": d.get("throttle_thermal_samples", 0),
        })
    return rows


def _fmt_num(v, nd=1, unit=""):
    """Formate un nombre de façon homogène (vide si absent, pas de .0 superflu)."""
    if v is None or v == "":
        return "—"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return str(v)
    txt = f"{f:.{nd}f}".rstrip("0").rstrip(".") if nd else f"{f:.0f}"
    return f"{txt}{unit}"


_GPU_TABLE_HEADERS = ["GPU", "TFLOPS", "Cœur max/moy °C", "Hotspot max/moy °C", "VRAM max °C",
                      "dT hotspot moy (P95) °C", "Puiss. moy W", "Fréq. MHz min–max", "Throttling"]


def _gpu_table(rows: list):
    """(en-têtes, lignes de cellules) de la table multi-GPU. Colonnes dynamiques : « VRAM GB/s » si mesurée, « Hotspot » et
    « dT hotspot » seulement si un GPU en a, « Activité & capteurs » seulement si disponible (GPU Intel via xpu-smi)."""
    with_vram = any(r.get("vram_bw") is not None for r in rows)
    with_hs = any(r.get("hs_max") is not None or r.get("delta_moy") is not None for r in rows)
    with_extra = any(r.get("extra") for r in rows)
    headers = list(_GPU_TABLE_HEADERS)
    cells = [_gpu_table_cells(r) for r in rows]
    if not with_hs:                       # retire « Hotspot max/moy » (3) et « dT hotspot » (5) : colonnes vides sur Intel / AMD sans LACT
        for idx in (5, 3):
            del headers[idx]
            for c in cells:
                del c[idx]
    if with_vram:
        headers.insert(2, "VRAM GB/s")
        for c, r in zip(cells, rows):
            c.insert(2, _fmt_num(r.get("vram_bw"), 0) if r.get("vram_bw") is not None else "—")
    if with_extra:
        headers.append("Activité & capteurs")
        for c, r in zip(cells, rows):
            c.append(r.get("extra") or "—")
    return headers, cells, with_vram


def _gpu_col(headers: list, prefix: str) -> int:
    """Index de la première colonne dont l'en-tête commence par `prefix` ; -1 si absente."""
    return next((i for i, h in enumerate(headers) if h.startswith(prefix)), -1)

def _gpu_table_cells(row: dict) -> list:
    """Cellules texte d'une ligne de la table multi-GPU (colonnes = _GPU_TABLE_HEADERS)."""
    tf = f"{float(row['gflops']) / 1000:.1f}" if row.get("gflops") not in (None, "") else "—"
    delta = "—"
    if row.get("delta_moy") is not None:
        delta = f"+{_fmt_num(row['delta_moy'])}"
        if row.get("delta_p95") is not None:
            delta += f" ({_fmt_num(row['delta_p95'])})"
        if row.get("delta_status") in ("critique", "avertissement"):
            delta += " !" if row["delta_status"] == "avertissement" else " !!"
    return [
        row["label"], tf,
        f"{_fmt_num(row['temp_max'])} / {_fmt_num(row['temp_moy'])}",
        f"{_fmt_num(row['hs_max'])} / {_fmt_num(row['hs_moy'])}",
        _fmt_num(row["vram_max"]), delta, _fmt_num(row["power_moy"]),
        f"{_fmt_num(row['fmin'], 0)}–{_fmt_num(row['fmax'], 0)}",
        f"oui ({row['throttle_n']})" if row["throttle"] else "non",
    ]


_FIO_KEY_RE = re.compile(r"^(?P<test>.+?) (?P<side>Lecture|Écriture)(?: latence (?P<stat>moyenne|p99))? \((?P<unit>MiB/s|IOPS|ms)\)$")
_FIO_COLS = [("Lecture", "MiB/s", "Lect. MiB/s"), ("Écriture", "MiB/s", "Écr. MiB/s"), ("Lecture", "IOPS", "Lect. IOPS"), ("Écriture", "IOPS", "Écr. IOPS"),
             ("Lecture", "moyenne", "Lat. lect. moy (ms)"), ("Lecture", "p99", "Lat. lect. p99 (ms)"),
             ("Écriture", "moyenne", "Lat. écr. moy (ms)"), ("Écriture", "p99", "Lat. écr. p99 (ms)")]


def _fio_matrix(metrics: dict):
    """Tableau « un test par ligne » d'une SUITE fio (SNIA, Peak, CrystalDiskMark…), au lieu de dizaines de lignes « clé / valeur » :
    retourne (en-têtes, lignes, clés consommées) ou None si ce n'est pas une suite. Les clés (p99.9, max…) non affichées dans le tableau ne sont PAS
    consommées : elles restent dans la liste des métriques (rien ne disparaît). Accepte aussi les anciens rapports (« … Lecture Lecture (MiB/s) »)."""
    tests: Dict[str, dict] = {}
    order: List[str] = []
    used = set()
    for k, v in (metrics or {}).items():
        m = _FIO_KEY_RE.match(str(k))
        if not m or isinstance(v, (list, dict, bool)) or not isinstance(v, (int, float)):
            continue
        test = m.group("test")
        if test.rstrip().lower().endswith(m.group("side").lower()):       # ancien format : « Seq1M Q8T1 Lecture Lecture (MiB/s) »
            test = test.rstrip()[: -len(m.group("side"))].rstrip()
        col = (m.group("side"), m.group("stat") or m.group("unit"))
        if test not in tests:
            tests[test] = {}
            order.append(test)
        tests[test][col] = v
        used.add(k)
    if len(order) < 2:
        return None
    present = [c for c in _FIO_COLS if any((c[0], c[1]) in t for t in tests.values())]
    heads = ["Test"] + [c[2] for c in present]
    rows = []
    for t in order:
        rows.append([t] + [(_fmt_cell(tests[t].get((c[0], c[1])))) for c in present])
    return heads, rows, used


_HIDDEN_METRIC_KEYS = {"cwd"}      # internes : jamais utiles à la lecture d'un rapport


def _pci_label(bus: str) -> str:
    """Adresse PCI lisible : « 01:00.0 » si le domaine est 0000, sinon avec le domaine (« 000c:01:00.0 ») — sur ARM
    multi-segments deux GPU peuvent avoir le même « 01:00.0 » dans des domaines différents."""
    b = re.sub(r"^0{4}([0-9a-fA-F]{4}):", r"\1:", (bus or "").strip()).lower()
    return b[5:] if b.startswith("0000:") else b


def _fmt_cell(v) -> str:
    """Valeur de tableau : entiers sans décimales, sinon 2 décimales ; « — » si absente."""
    if v is None:
        return "—"
    return f"{v:,.0f}".replace(",", " ") if abs(v) >= 100 else (f"{v:.2f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v))


def _non_gpu_metric_items(metrics: dict) -> dict:
    """Métriques hors séries temporelles et hors clés `gpuN_*` (affichées dans la table GPU)."""
    return {k: v for k, v in (metrics or {}).items()
            if not _GPU_METRIC_RE.match(str(k)) and not str(k).startswith("series")
            and not isinstance(v, (list, dict)) and v not in ("", None)}


_SYS_LABELS = {
    "os": "Système d'exploitation", "distro": "Distribution", "kernel": "Noyau", "architecture": "Architecture",
    "hostname": "Nom d'hôte", "uptime": "Disponibilité", "modele": "Modèle", "coeurs_phys": "Cœurs physiques",
    "threads": "Threads", "freq_max_mhz": "Fréquence max", "freq_cur_mhz": "Fréquence actuelle",
    "cache_l3": "Cache L3", "socket": "Sockets", "microcode": "Microcode", "total_go": "RAM totale (Go)", "emplacements": "Emplacements RAM",
    "disponible_go": "RAM disponible (Go)", "utilise_go": "RAM utilisée (Go)", "utilisation_pct": "Utilisation RAM (%)",
    "type": "Type", "vitesse_mhz": "Vitesse", "ecc": "ECC", "fabricant": "Fabricant", "version": "Version",
    "bios": "BIOS", "bios_date": "Date BIOS", "chipset": "Chipset", "form_factor": "Format",
    "present": "Présent", "device_id": "ID périphérique", "manufacturer": "Fabricant", "product_id": "ID produit",
    "firmware_revision": "Firmware", "ipmi_version": "Version IPMI", "mac": "Adresse MAC",
    "ip_address": "Adresse IP", "channel": "Canal", "sel_status": "Journal SEL", "redfish_present": "Redfish",
    "redfish_url": "URL Redfish", "numa_noeuds": "Nœuds NUMA", "mode_boot": "Mode de démarrage", "iommu": "IOMMU",
}


def _system_sections(snapshot: dict, gpu_list: list = None) -> list:
    """Découpe le snapshot système en sections compactes, sans valeurs vides.

    Retourne [(titre, en-têtes|None, lignes)] : les sections OS/CPU/RAM/Carte mère/BMC sont en
    clé/valeur, GPU/stockage/réseau en tableau (une ligne par périphérique).
    """
    snap = snapshot or {}
    secs = []

    def kv(title, data, skip=()):
        rows = []
        for k, v in (data or {}).items():
            if k in skip or v in (None, "", [], {}) or isinstance(v, (list, dict)):
                continue
            rows.append([_SYS_LABELS.get(k, k), _safe_text(v)])
        if rows:
            secs.append((title, None, rows))

    kv("Système d'exploitation", snap.get("os"))
    kv("Processeur", snap.get("cpu"))
    ram = snap.get("ram") or {}
    kv("Mémoire", ram)
    sticks = ram.get("barrettes") or []
    if sticks:
        rows = [[st.get("slot", f"#{i}"), " · ".join(x for x in (st.get("taille"), st.get("type"), st.get("vitesse"),
                st.get("fabricant"), st.get("ref")) if x)] for i, st in enumerate(sticks)]
        secs.append(("Barrettes mémoire", None, rows))
    kv("Carte mère", snap.get("carte_mere"))
    kv("BMC / IPMI", snap.get("bmc"), skip=("notes",))

    gpus = [g for g in (snap.get("gpus") or snap.get("gpu") or gpu_list or []) if isinstance(g, dict)]
    compute = [g for g in gpus if _is_compute_gpu(g)]
    other = [g for g in gpus if not _is_compute_gpu(g)]
    if compute:
        rows = []
        for pos, g in enumerate(compute):
            vram = g.get("vram_total_mo")
            try:
                vram = f"{float(vram) / 1024:.0f} Go" if vram else ""
            except (TypeError, ValueError):
                pass
            pcie = f"Gen{g['pcie_gen']} x{g['pcie_largeur']}" if g.get("pcie_gen") else ""
            rows.append([str(g.get("index", pos)), g.get("nom") or g.get("modele") or "", g.get("pci_bus", ""),
                         g.get("mcio", ""), g.get("serial", ""), vram, pcie,
                         " ".join(x for x in (g.get("pilote"), g.get("driver_version")) if x)])
        secs.append(("Cartes graphiques / accélérateurs",
                     ["#", "Modèle", "Bus PCI", "MCIO", "N° de série", "VRAM", "PCIe", "Pilote"], rows))
    if other:
        secs.append(("Autres adaptateurs graphiques (non testés)", None,
                     [[g.get("pci_bus", ""), g.get("nom") or g.get("modele") or ""] for g in other]))
    disks = snap.get("stockage") or []
    if disks:
        rows = [[d.get("nom", ""), d.get("modele", ""), d.get("taille", ""), d.get("type") or d.get("interface", ""),
                 (f"Gen{d['pcie_gen']} x{d['pcie_largeur']}" if d.get("pcie_gen") else ""),
                 d.get("serial", ""), (f"{d['temperature_c']} °C" if d.get("temperature_c") else ""),
                 d.get("sante", "")] for d in disks]
        secs.append(("Stockage", ["Disque", "Modèle", "Taille", "Type", "PCIe", "N° de série", "Temp.", "Santé"], rows))
    nics = snap.get("reseau") or []
    if nics:
        rows = [[n.get("nom", ""), n.get("etat", ""), n.get("vitesse", ""), n.get("modele", ""), n.get("pilote", ""),
                 (f"Gen{n['pcie_gen']} x{n['pcie_largeur']}" if n.get("pcie_gen") and n.get("pcie_largeur") else ""),
                 ", ".join(a for a in (n.get("adresses") or []) if ":" not in a.split("/")[0])] for n in nics]
        secs.append(("Réseau", ["Interface", "État", "Débit", "Modèle", "Pilote", "PCIe", "IPv4"], rows))
    from core.system_info import group_pcie_devices, name_pcie_devices
    pcie = group_pcie_devices(name_pcie_devices([d for d in (snap.get("pcie") or []) if isinstance(d, dict)],
                                                [g for g in (snap.get("gpus") or []) if isinstance(g, dict)]))   # une ligne par carte
    if pcie:
        def _lk(gen, width):
            return f"Gen{gen} x{width}" if gen and width else (f"Gen{gen}" if gen else "")
        rows = []
        for d in pcie:
            etat = d.get("etat", "")
            if d.get("note"):
                etat = f"{etat} — {d['note']}" if etat else d["note"]
            aer = "/".join(str(d.get(k, 0)) for k in ("aer_cor", "aer_nonfatal", "aer_fatal"))
            nom = d.get("nom", "") + (f" ({d['fonctions']} fonctions)" if d.get("fonctions") else "")
            rows.append([d.get("bus", ""), d.get("type", ""), nom, d.get("pilote", ""),
                         _lk(d.get("gen"), d.get("largeur")), _lk(d.get("gen_max"), d.get("largeur_max")), etat,
                         str(d["numa"]) if d.get("numa") is not None else "", aer])
        secs.append(("Périphériques PCIe (liens ; AER = erreurs corrigées / non fatales / fatales)",
                     ["Bus", "Type", "Périphérique", "Pilote", "Lien actuel", "Lien max", "État", "NUMA", "AER"], rows))
    return secs


def _build_telemetry_summary(results: List[BenchmarkResult], gpu_list: list = None) -> dict:
    """Synthèse télémétrique par GPU : pics, moyennes en charge, ΔT hotspot et throttling.

    Les moyennes/ΔT/fréquences viennent de `core.gpu_stats` (charge stabilisée, repos exclu).
    """
    labels = _gpu_labels(gpu_list or [])
    summaries = {}
    moyennes: dict = {}   # (gpu, clé) → moyennes de chaque test : la synthèse moyenne tous les tests, pas seulement le dernier
    for r in results:
        for row in _gpu_rows_for_result(r, labels):
            g = summaries.setdefault(row["index"], {
                "gpu_index": row["index"], "label": row["label"],
                "temp_core_max_c": None, "temp_core_moy_c": None,
                "temp_hotspot_max_c": None, "temp_hotspot_moy_c": None,
                "temp_vram_max_c": None,
                "delta_hotspot_moy_c": None, "delta_hotspot_p95_c": None, "delta_hotspot_status": None,
                "power_max_w": None, "power_moy_w": None,
                "freq_min_mhz": None, "freq_max_mhz": None,
                "throttle_detected": False, "throttle_samples": 0,
            })
            def upd_max(key, val):
                if val is not None:
                    g[key] = max(g[key] if g[key] is not None else float(val), float(val))
            upd_max("temp_core_max_c", row["temp_max"]); upd_max("temp_hotspot_max_c", row["hs_max"])
            upd_max("temp_vram_max_c", row["vram_max"]); upd_max("power_max_w", row["power_max"])
            upd_max("freq_max_mhz", row["fmax"]); upd_max("delta_hotspot_p95_c", row["delta_p95"])
            for key, val in (("temp_core_moy_c", row["temp_moy"]), ("temp_hotspot_moy_c", row["hs_moy"]),
                             ("delta_hotspot_moy_c", row["delta_moy"]), ("power_moy_w", row["power_moy"])):
                if val is not None:
                    moyennes.setdefault((row["index"], key), []).append(float(val))
            if row["fmin"] is not None:
                g["freq_min_mhz"] = float(row["fmin"]) if g["freq_min_mhz"] is None else min(g["freq_min_mhz"], float(row["fmin"]))
            if row["delta_status"]:
                g["delta_hotspot_status"] = row["delta_status"]
            for key, val in (row.get("extras") or {}).items():
                g[key] = max(g.get(key, float(val)), float(val))
            g["throttle_detected"] = g["throttle_detected"] or row["throttle"]
            g["throttle_samples"] += int(row["throttle_n"] or 0)
    for (idx, key), vals in moyennes.items():
        summaries[idx][key] = round(sum(vals) / len(vals), 1)
    return {"gpus": [summaries[i] for i in sorted(summaries)]}


def _hardware_fault_alerts(results: List[BenchmarkResult]) -> list:
    """Alertes « panne matérielle » construites à partir de la surveillance en direct des tests."""
    alerts = []
    for r in results:
        evs = (r.metrics or {}).get("hardware_events")
        if not isinstance(evs, list) or not evs:
            continue
        crit = [e for e in evs if e.get("severity") == "critical"]
        for sev, group in (("error", crit), ("warning", [e for e in evs if e.get("severity") != "critical"])):
            if not group:
                continue
            cats = {}
            for e in group:
                cats.setdefault(e.get("category", "Événement matériel"), []).append(e)
            for cat, items in cats.items():
                first = items[0]
                alerts.append({
                    "type": sev,
                    "category": f"Surveillance matérielle — {r.name}",
                    "title": f"{cat} ({len(items)} événement{'s' if len(items) > 1 else ''}"
                             + (", test interrompu" if sev == "error" else "") + ")",
                    "desc": f"À t+{first.get('t', '?')} s : {first.get('line', '')}",
                    "devices": sorted({e.get("device", "") for e in items if e.get("device")}),
                })
    return alerts

def _verdict_for(results, hw, meta: dict):
    """Verdict fourni par l'appelant (CLI : critères/référence) sinon règles universelles seules."""
    if not results:
        return None          # rapport « informations système » seul : aucun test, donc pas de verdict de test
    v = (meta or {}).get("verdict")
    if isinstance(v, dict) and v.get("status"):
        return v
    try:
        from core.verdict import compute_verdict
        return compute_verdict(results, hw if isinstance(hw, dict) else {})
    except Exception:
        return None


def _dur_label(sec) -> str:
    """60 → « 60 s » ; 1800 → « 30 min » ; 5400 → « 1 h 30 »."""
    try:
        n = int(round(float(sec)))
    except (TypeError, ValueError):
        return ""
    if n < 120:
        return f"{n} s"
    if n < 7200 and n % 60 == 0:
        return f"{n // 60} min"
    h, m = divmod(n // 60, 60)
    return f"{h} h {m:02d}" if m else f"{h} h"


def unique_result_names(results: List[BenchmarkResult]) -> List[BenchmarkResult]:
    """Rend distincts les noms de résultats identiques (ex. deux « GPU gpu-burn » de 60 s et 30 min dans la même séquence).

    Sans cela, le rapport ne distinguait plus les deux : « Tests couverts » en listait un seul, le verdict répétait « test GPU gpu-burn »
    sans dire lequel, et la comparaison HTML les confondait. Suffixe = durée demandée si elle les distingue (« GPU gpu-burn (30 min) »),
    sinon leur rang (« #2 »). Les résultats uniques ne changent pas ; les autres sont des copies (l'objet d'origine reste intact)."""
    import dataclasses
    groups: dict = {}
    for i, r in enumerate(results):
        groups.setdefault(r.name, []).append(i)
    if all(len(ix) == 1 for ix in groups.values()):
        return list(results)
    out = list(results)
    for name, ix in groups.items():
        if len(ix) == 1:
            continue
        def req(r):
            m = r.metrics or {}
            return m.get("time_s") or m.get("duration_s") or m.get("runtime_s")
        durs = [req(results[i]) for i in ix]
        by_dur = all(d for d in durs) and len({str(d) for d in durs}) == len(ix)
        order = sorted(ix, key=lambda i: results[i].timestamp or "")
        for rank, i in enumerate(order, 1):
            suffix = f" ({_dur_label(req(results[i]))})" if by_dur else f" #{rank}"
            try:
                out[i] = dataclasses.replace(results[i], name=f"{name}{suffix}")
            except Exception:
                pass
    return out


def _report_payload(results: List[BenchmarkResult], report_meta: dict = None,
                    system_info_data: dict = None) -> dict:
    try:
        if system_info_data is not None:
            hw = system_info_data
        else:
            try:
                from core.benchmarks import get_hardware_report
                hw = get_hardware_report()
            except Exception:
                hw = {}
    except Exception as e:
        hw = {"erreur": str(e)}
        
    meta = load_report_meta()
    if report_meta:
        meta.update(report_meta)
        
    return {
        "benchsuite_version": _app_version(),
        "exported_at": datetime.now().isoformat(),
        "metadata": {
            "system_name": _report_system_name(meta),
            "operator": meta.get("operator", ""),
            "client": meta.get("client", ""),
            "ambient_temp": meta.get("ambient_temp", ""),
            "fan_speed": meta.get("fan_speed", ""),
            "order_no": meta.get("order_no", ""),
            "serial": meta.get("serial", ""),
            "model": meta.get("model", ""),
        },
        "system": {
            "name": _report_system_name(meta),
            "snapshot_at_start": hw,
        },
        "summary": {
            "bench_count": len(results),
            "ok": sum(1 for r in results if r.status == "ok"),
            "failed": sum(1 for r in results if r.status in ("error", "failed")),
            "stopped": sum(1 for r in results if r.status == "stopped"),      # interrompus : pas des échecs
            "skipped": sum(1 for r in results if r.status == "skipped"),      # outil absent, etc.
        },
        "telemetry_summary": _build_telemetry_summary(results, _gpu_list_from(hw if isinstance(hw, dict) else None)),
        "verdict": _verdict_for(results, hw, meta),
        "inventory_campaign": meta.get("inventory_diff") if isinstance(meta.get("inventory_diff"), dict) else None,
        "sequence_series": meta.get("sequence_series") if isinstance(meta.get("sequence_series"), dict) else None,
        "results": [r.to_dict() for r in results],
    }


def _flatten_system_rows(prefix, obj):
    rows = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            key = f"{prefix}.{k}" if prefix else str(k)
            rows.extend(_flatten_system_rows(key, v))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            key = f"{prefix}[{i}]"
            rows.extend(_flatten_system_rows(key, v))
    else:
        rows.append((prefix, _safe_text(obj)))
    return rows


def compact_raw_output(raw: str, limit: int, head: int = 3000) -> str:
    """Sortie brute lisible et plafonnée à `limit` caractères.

    1. Lignes consécutives identiques regroupées (« ligne  (×N) ») : gpu-burn répète sa ligne de progression chaque seconde,
       ce qui noyait 30 min de test sous plusieurs Mo de texte.
    2. Si c'est encore trop long : on garde le DÉBUT (identité du GPU, options) et la FIN (résumé final : « GPU 0: OK / FAULTY »,
       métriques finales), jamais seulement le début comme avant — c'est la fin qui dit si le test a réussi."""
    if not raw:
        return ""
    out, prev, n = [], None, 0
    for line in raw.splitlines():
        if line == prev:
            n += 1
            continue
        if n:
            out[-1] = f"{prev}  (×{n + 1})"
        out.append(line); prev, n = line, 0
    if n:
        out[-1] = f"{prev}  (×{n + 1})"
    txt = "\n".join(out)
    if len(txt) <= limit:
        return txt
    head = min(head, limit // 4)
    omitted = len(txt) - limit
    return txt[:head] + f"\n[... {omitted} caractères omis au milieu ...]\n" + txt[-(limit - head):]


RAW_OUTPUT_JSON_MAX = 50000   # caractères conservés de la sortie brute dans le JSON (fin de sortie)


def _dumps_compact_json(obj) -> str:
    """JSON indenté dont les listes de nombres tiennent sur une seule ligne.

    Les séries temporelles (centaines de points × dizaines de capteurs) pèsent ~2× moins
    qu'avec un nombre par ligne, sans changer le contenu ni la structure.
    """
    txt = json.dumps(obj, indent=2, ensure_ascii=False)
    num = r"-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?|null|true|false"
    return re.sub(rf"\[\s*((?:{num})(?:,\s*(?:{num}))*)\s*\]",
                  lambda m: "[" + re.sub(r",\s+", ", ", m.group(1)) + "]", txt)


def _trim_payload_for_json(payload: dict) -> dict:
    """Copie du payload avec sortie brute plafonnée et séries de throttling nulles omises."""
    out = json.loads(json.dumps(payload, ensure_ascii=False))
    for item in out.get("results", []):
        item["raw_output"] = compact_raw_output(item.get("raw_output") or "", RAW_OUTPUT_JSON_MAX)
        ser = (item.get("metrics") or {}).get("series")
        if isinstance(ser, dict):
            for k in [k for k, v in ser.items() if k.endswith("_throttle_thermal") and isinstance(v, list) and not any(v)]:
                ser.pop(k)   # tout à 0 : le compteur gpuN_throttle_thermal_samples suffit
    return out

def _metric_rows(metrics: dict, max_rows: int = 26):
    rows = []
    for k, v in (metrics or {}).items():
        if isinstance(v, list):
            if len(v) > 0:
                rows.append([k, f"série {len(v)} points"])
        elif isinstance(v, dict):
            rows.append([k, json.dumps(v, ensure_ascii=False)[:180]])
        else:
            rows.append([k, _safe_text(v)[:180]])
        if len(rows) >= max_rows:
            break
    return rows or [["-", "-"]]


def _series_for_result(r: BenchmarkResult):
    m = r.metrics or {}
    s_dict = m.get("series") if isinstance(m.get("series"), dict) else (getattr(r, "series", None) if isinstance(getattr(r, "series", None), dict) else {})
    t = m.get("series_t") or m.get("t") or s_dict.get("t") or []
    if not isinstance(t, list) or len(t) < 2:
        return None
    candidates = [
        ("series_temp", "Température °C"), ("series_power", "Puissance W"),
        ("disk_temp", "Température Disque °C"), ("disk_power", "Puissance Disque W"),
        ("series_disk_temp", "Température Disque °C"), ("series_disk_power", "Puissance Disque W"),
        ("series_freq_graphics", "Fréquence GPU MHz"), ("series_freq_memory", "Fréquence mémoire MHz"),
        ("ram_used_gb", "RAM utilisée Go"), ("ram_pct", "RAM %"),
        ("ram_write_mibs", "Écriture Mo/s"), ("ram_read_mibs", "Lecture Mo/s"),
        ("ram_errors", "Erreurs RAM"), ("ram_temp", "Température RAM °C"), ("ram_power", "Puissance RAM W"),
    ]
    found = []
    for k, label in candidates:
        v = m.get(k) or s_dict.get(k.replace("series_", "")) or s_dict.get(k)
        if v is None and ("disk_temp" in k or k == "series_temp"):
            v = s_dict.get("temp") or s_dict.get("disk_temp") or s_dict.get("ssd_temp")
        if v is None and ("disk_power" in k or k == "series_power"):
            v = s_dict.get("power") or s_dict.get("disk_power") or s_dict.get("ssd_power")
        if isinstance(v, list) and len(v) >= 2:
            found.append((k, label, v))
    # GPU multi: gpu0_temp, gpu1_power, etc.
    for k, v in m.items():
        if isinstance(v, list) and len(v) >= 2 and k.startswith("gpu"):
            if k not in [x[0] for x in found]:
                mg = re.match(r"^gpu(\d+)_(.+)$", k)
                if mg:
                    g_idx = int(mg.group(1))
                    s_type = mg.group(2)
                    s_name = {"temp": "Cœur °C", "temp_hotspot": "Hotspot °C", "temp_vram": "VRAM °C",
                              "power": "Puissance W", "freq_graphics": "Fréq GPU MHz", "freq_memory": "Fréq VRAM MHz"}.get(s_type, s_type)
                    label = f"GPU {g_idx} {s_name}"
                else:
                    label = k
                found.append((k, label, v))
    return {"t": t, "series": found[:8]} if found else None


_ELEC_CAT_LABELS = {"voltage": "Tension", "current": "Courant", "power": "Puissance", "fan": "Ventilateur",
                    "temperature": "Température carte"}


_PCIE_MAIN_CLASSES = ("01", "02", "03", "0b", "12")        # stockage, réseau, GPU, co-processeur, accélérateur : affichés d'office


def _pcie_link_rows(pl: dict, main_only: bool = None, category: str = "") -> list:
    """Lignes [périphérique, type, maximum, liens observés (part du temps), % au maximum, remarque] des liens PCIe relevés pendant un test.
    main_only=True : anomalies + classes principales ; False : le reste ; None : tout."""
    rows = []
    for d in (pl or {}).get("periphériques", []):
        is_main = bool(d.get("anomalies")) or str(d.get("classe", ""))[:2] in _PCIE_MAIN_CLASSES
        if main_only is not None and is_main != main_only:
            continue
        gm, wm = d.get("gen_max"), d.get("largeur_max")
        mx = f"Gen{gm} x{wm}" if (gm or wm) else "inconnu (non évalué)"
        obs = ", ".join(f"Gen{l['gen']} x{l['largeur']} ({l['pct']:.0f} %)" for l in d.get("liens", [])[:4])
        pct = f"{d['pct_au_maximum']:.0f} %" if d.get("pct_au_maximum") is not None else "—"
        from core.verdict import PCIE_IDLE_HINT, pcie_speed_is_warn
        targeted = pcie_speed_is_warn(category, d.get("classe", ""))
        rem = " · ".join((f.get("label", "").replace(PCIE_IDLE_HINT, "") if (targeted and f.get("code") == "vitesse") else f.get("label", ""))
                         for f in d.get("anomalies", []))
        label = f"{d['nom']} ({d.get('bus', '')})" if d.get("nom") else d.get("bus", "?")
        rows.append([label, d.get("type", ""), mx, obs, pct, rem])
    return rows

def _observations(results, meta: dict = None) -> list:
    """Liste [(titre, electrical, inventory_diff, hardware_events)] : campagne d'abord, puis chaque test."""
    out = []
    camp = (meta or {}).get("inventory_diff")
    if isinstance(camp, dict):
        out.append(("Campagne complète (inventaire de départ / de fin)", {}, camp, []))
    for r in results:
        m = r.metrics or {}
        el = m.get("electrical") if isinstance(m.get("electrical"), dict) else {}
        inv = m.get("inventory_diff") if isinstance(m.get("inventory_diff"), dict) else {}
        evs = m.get("hardware_events") if isinstance(m.get("hardware_events"), list) else []
        if el or inv or evs:
            out.append((r.name, el, inv, evs))
    return out


def _observation_alerts(results, meta: dict = None) -> list:
    """Alertes du tableau de bord : rails hors tolérance, dérives d'inventaire, nouveaux événements BMC."""
    alerts = []
    for title, el, inv, _ in _observations(results, meta):
        for a in ((el or {}).get("alerts", []) if _seq_elec_enabled() else []):
            alerts.append({"type": "warning", "category": f"Électronique — {title}", "title": "Tension hors tolérance",
                           "desc": a})
        for f in (inv or {}).get("findings", []):
            if f["level"] in ("fail", "warn"):
                alerts.append({"type": "error" if f["level"] == "fail" else "warning",
                               "category": f"Inventaire avant/après — {title}", "title": f["label"],
                               "desc": f.get("detail", "")})
    return alerts


def _pcie_alerts(results) -> list:
    """Alertes du tableau de bord : lien PCIe sous le maximum du périphérique pendant un test (largeur durablement réduite)."""
    alerts = []
    for r in results:
        pl = (r.metrics or {}).get("pcie_links") if isinstance(getattr(r, "metrics", None), dict) else None
        for d in (pl or {}).get("periphériques", []):
            for f in d.get("anomalies", []):
                if f.get("level") == "warn":
                    alerts.append({"type": "warning", "category": f"Liens PCIe — {r.name}",
                                   "title": "Lien réduit : " + (f"{d['nom']} ({d.get('bus', '')})" if d.get("nom") else d.get("bus", "?")), "desc": f.get("label", "")})
    return alerts


def _electrical_rows(el: dict) -> list:
    """Lignes [capteur, source, type, min, moy, max, unité, remarque] d'un relevé électronique."""
    rows = []
    for x in (el or {}).get("sensors", []):
        note = x.get("flag") or (f"rail {x['rail']} · variation {x.get('variation_pct', 0):.1f} %" if x.get("rail") else "")
        nd = 0 if x["category"] == "fan" else (2 if x["category"] in ("power", "temperature") else 3)
        rows.append([x["name"], x["source"], _ELEC_CAT_LABELS.get(x["category"], x["category"]),
                     f"{x['min']:.{nd}f}", f"{x['mean']:.{nd}f}", f"{x['max']:.{nd}f}", x.get("unit", ""), note])
    return rows


def _sequence_entity(label: str) -> str:
    """Entité d'une courbe (GPU 0, CPU, Disque…) : même couleur pour cœur / hotspot / puissance."""
    return label.replace(" cœur", "").replace(" hotspot", "")

def _safe_folder_part(value: str) -> str:
    value = (value or "").strip() or "rapport"
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value)
    return value.strip("._-")[:80] or "rapport"


def format_report_dir_name(report_meta: dict = None, suffix: str = "", dt: datetime = None) -> str:
    """Construit le nom standardisé d'un dossier de résultats après test :
    BenchSuite_v<version>_<AAAA-MM-JJ_HH-MM-SS>_<nom_serveur>[_<suffixe>].
    """
    when = dt or datetime.now()
    ver = _safe_folder_part(_app_version())
    date_str = when.strftime("%Y-%m-%d_%H-%M-%S")
    srv = _safe_folder_part(_report_system_name(report_meta))
    name = f"BenchSuite_v{ver}_{date_str}_{srv}"
    if suffix:
        name = f"{name}_{_safe_folder_part(suffix)}"
    return name
