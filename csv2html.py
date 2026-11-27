#!/usr/bin/env python3
"""Convertisseur de fichiers CSV vers un rapport HTML BenchSuite (mêmes graphiques, même charte, bouton thème clair / sombre).

Pensé pour des CSV de toute provenance (export Excel français ou anglais, journal d'alimentation, capteurs, sortie d'outil…) :
  - encodage : UTF-8 (avec ou sans BOM), UTF-16, Windows-1252 (Excel Windows), Latin-1 ;
  - séparateur : « ; » « , » « tabulation » « | » (détecté sur la régularité du nombre de colonnes) ;
  - lignes de commentaires / métadonnées avant le tableau (« # … », lignes à un seul champ) ignorées ;
  - en-tête présent ou absent ; colonnes nommées d'après l'en-tête ;
  - nombres : virgule ou point décimal, séparateurs de milliers, unité collée (« 350,5 W », « 41 °C », « 87 % ») ;
  - colonne de temps détectée (dates ISO, JJ/MM/AAAA, heure seule, secondes, millisecondes, époque Unix) → axe X en secondes ;
  - colonnes de texte ignorées (listées) ; valeurs manquantes complétées par la dernière valeur connue (et comptées dans le résultat).
Une colonne est rangée dans l'onglet qui correspond à son nom (puissance, température, fréquence, charge, débit, latence) ;
les autres vont dans « Valeurs ». Rien n'est inventé : les colonnes inconnues gardent leur nom d'origine.

Usage :  python csv2html.py fichier.csv [autre.csv …] [-o dossier] [-s "Nom de la machine"] [--theme dark|light|auto]
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))
sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

try:
    from core.result import BenchmarkResult
    from exporters.export import export_html
    HAS_BENCHSUITE = True
except ImportError:
    HAS_BENCHSUITE = False

    @dataclass
    class BenchmarkResult:  # type: ignore[no-redef]
        name: str
        category: str
        score: Optional[float]
        unit: str
        duration_s: float
        raw_output: str
        metrics: dict = field(default_factory=dict)
        status: str = "ok"
        timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
        system_info: dict = field(default_factory=dict)
        notes: str = ""
        command: str = ""

        def to_dict(self) -> dict:
            return asdict(self)

DELIMS = [";", ",", "\t", "|"]
TIME_NAME = re.compile(r"^\W*(time|timestamp|date|datetime|date.?time|heure|temps|horodatage|seconds?|secs?|elapsed|epoch|t)\W*($|[\s_(\[])", re.I)
MISSING = {"", "-", "--", "n/a", "na", "nan", "null", "none", "?", "—"}
_NUM = re.compile(r"^[-+]?(\d[\d\s.,]*|[.,]\d+)([eE][-+]?\d+)?")

# nom de colonne → (préfixe de clé reconnu par le rapport, onglet)
_KINDS = [
    ("power", re.compile(r"power|watt|puissance|\bpin\b|\bpout\b|\bw\b|\(w\)|\[w\]|pertes?|loss", re.I)),
    ("temp", re.compile(r"temp|°c|celsius|degc|\bt°", re.I)),
    ("cpu_freq", re.compile(r"freq|mhz|ghz|clock|horloge", re.I)),
    ("cpu_pct", re.compile(r"load|charge|util|usage|cpu.?%|\bcpu\b", re.I)),
    ("lat", re.compile(r"lat[eé]nc|latency|\bping\b|\(ms\)|\[ms\]|\bms\b|await|jitter", re.I)),
    ("throughput", re.compile(r"mb\s*/?\s*s|mib|gb\s*/?\s*s|gib|iops|throughput|d[eé]bit|bandwidth|bande|bit/?s", re.I)),
]
GENERIC_COL_NAME = re.compile(r"^(val|value|valeur|valeurs|data|donn[eé]es?|y|mesure|mesures?|ch\s*\d+|channel\s*\d+|colonne\s*\d+)$", re.I)


# ── Lecture du fichier ────────────────────────────────────────────────────────

def read_text(path: str) -> Tuple[str, str]:
    with open(path, "rb") as f:
        raw = f.read()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", "replace"), "utf-8-sig"
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", "replace"), "utf-16"
    if b"\x00" in raw[:200]:                       # UTF-16 sans BOM (un octet nul sur deux)
        for enc in ("utf-16-le", "utf-16-be"):
            try:
                return raw.decode(enc), enc
            except UnicodeDecodeError:
                pass
    for enc in ("utf-8", "cp1252"):
        try:
            return raw.decode(enc), enc
        except UnicodeDecodeError:
            pass
    return raw.decode("latin-1"), "latin-1"


def detect_delimiter(lines: List[str]) -> str:
    """Séparateur dont le nombre de colonnes est le plus régulier sur les premières lignes (≥ 2 colonnes) ; « , » par défaut."""
    best, best_score = ",", 0.0
    sample = [l for l in lines if l.strip() and not l.lstrip().startswith(("#", "//"))][:60]
    for d in DELIMS:
        try:
            counts = [len(r) for r in csv.reader(sample, delimiter=d)]
        except csv.Error:
            continue
        counts = [c for c in counts if c]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        if mode < 2:
            continue
        score = (counts.count(mode) / len(counts)) * (1 + min(mode, 12) / 100.0)
        if score > best_score:
            best, best_score = d, score
    return best


def parse_number(s: str, delim: str) -> Optional[float]:
    """Nombre d'une cellule (virgule ou point décimal, milliers, unité collée) ou None si ce n'est pas un nombre."""
    s = (s or "").strip().replace(" ", " ")
    if s.lower() in MISSING:
        return None
    m = _NUM.match(s)
    if not m:
        return None
    tail = s[m.end():].strip()
    if tail and not re.match(r"^[%°a-zA-Zµμ/²³. ()\[\]-]*$", tail):
        return None                                  # « 12:30:00 », « 2026-11-04 »… ne sont pas des nombres simples
    num = (m.group(1) or "").replace(" ", "")
    if not num or num in ".,":
        return None
    if "," in num and "." in num:                    # le dernier séparateur est le décimal
        dec = "," if num.rfind(",") > num.rfind(".") else "."
        thou = "." if dec == "," else ","
        num = num.replace(thou, "").replace(dec, ".")
    elif "," in num:
        if delim == "," and re.fullmatch(r"\d{1,3}(,\d{3})+", num):
            num = num.replace(",", "")               # CSV à virgules : « 1,234 » = milliers
        else:
            num = num.replace(",", ".")              # virgule décimale (Excel français)
    elif num.count(".") > 1 and re.fullmatch(r"\d{1,3}(\.\d{3})+", num):
        num = num.replace(".", "")
    try:
        return float(num + (m.group(2) or ""))
    except ValueError:
        return None


_DT_FORMATS = ["%Y%m%dT%H%M%S", "%Y%m%d_%H%M%S", "%Y%m%d%H%M%S", "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S",
               "%d/%m/%Y %H:%M:%S", "%d/%m/%Y %H:%M:%S.%f", "%d/%m/%Y %H:%M", "%d-%m-%Y %H:%M:%S", "%d.%m.%Y %H:%M:%S",
               "%m/%d/%Y %H:%M:%S", "%m/%d/%Y %I:%M:%S %p", "%d/%m/%Y %I:%M:%S %p", "%H:%M:%S.%f", "%H:%M:%S", "%H:%M"]


_LAST_FMT: list = []


def parse_datetime(s: str, month_first: bool = False) -> Optional[datetime]:
    """Date / heure d'une cellule (formats courants, jour d'abord par défaut) ou None."""
    s = (s or "").strip().replace(" ", " ")
    if not s or not re.search(r"\d[:/.\-]\d|^\d{8}[T_]?\d{6}$", s):
        return None
    s = re.sub(r"Z$", "", s)
    s = re.sub(r"[+-]\d{2}:?\d{2}$", "", s) if re.search(r"\d{2}:\d{2}[+-]\d{2}:?\d{2}$", s) else s
    fmts = list(_DT_FORMATS)
    if month_first:
        fmts.sort(key=lambda f: 0 if f.startswith("%m/") else 1)
    if _LAST_FMT and _LAST_FMT[0] in fmts:               # le format qui a marché sur la ligne précédente d'abord (fichiers de 40 000 lignes)
        fmts.remove(_LAST_FMT[0])
        fmts.insert(0, _LAST_FMT[0])
    for f in fmts:
        try:
            d = datetime.strptime(s, f)
            _LAST_FMT[:] = [f]
            return d
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


# ── Analyse du tableau ────────────────────────────────────────────────────────

def load_table(path: str) -> Tuple[List[str], List[List[str]], dict]:
    """(en-têtes, lignes, infos) : saute les métadonnées avant le tableau, détecte l'en-tête. Lève ValueError si rien d'exploitable."""
    text, enc = read_text(path)
    text = text.replace("\x00", "")                  # remplissage par octets nuls en fin de fichier (journal interrompu, disque préalloué)
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    delim = detect_delimiter(lines)
    rows = [r for r in csv.reader((l for l in lines), delimiter=delim) if any(c.strip() for c in r)]
    rows = [r for r in rows if not (r and r[0].lstrip().startswith(("#", "//")))]
    if not rows:
        raise ValueError("fichier vide")
    counts = [len(r) for r in rows]
    mode = max(set(counts), key=counts.count)
    if mode < 1:
        raise ValueError("fichier vide")
    first = next(i for i, c in enumerate(counts) if c == mode)
    skipped = first
    body = [r for r in rows[first:] if len(r) >= 1]
    ncol = mode
    body = [(r + [""] * ncol)[:ncol] for r in body]

    # En-tête : première ligne avec du texte alors que les lignes suivantes sont numériques ou datées
    def kind(c):
        return "num" if parse_number(c, delim) is not None else ("date" if parse_datetime(c) else ("empty" if c.strip().lower() in MISSING else "text"))
    head_kinds = [kind(c) for c in body[0]]
    next_kinds = [kind(c) for c in body[1]] if len(body) > 1 else head_kinds
    has_header = any(k == "text" for k in head_kinds) and any(k in ("num", "date") for k in next_kinds)
    if not has_header and all(k == "text" for k in head_kinds):
        has_header = True                                  # tableau purement textuel : la première ligne nomme les colonnes
    if has_header:
        headers = [c.strip() or f"Colonne {i + 1}" for i, c in enumerate(body[0])]
        body = body[1:]
    else:
        headers = [f"Colonne {i + 1}" for i in range(ncol)]
    seen: Dict[str, int] = {}
    for i, h in enumerate(headers):                        # noms uniques
        n = seen.get(h, 0)
        seen[h] = n + 1
        if n:
            headers[i] = f"{h} ({n + 1})"
    return headers, body, {"encodage": enc, "separateur": {"\t": "tabulation"}.get(delim, delim), "en_tete": has_header,
                           "lignes_ignorees_avant": skipped, "delim": delim}


def find_time_column(headers: List[str], body: List[List[str]], delim: str) -> Optional[int]:
    """Indice de la colonne de temps (nom évocateur ou valeurs datées / heures), sinon None."""
    sample = body[:200]
    for i, h in enumerate(headers):
        col = [r[i] for r in sample]
        nums = [parse_number(c, delim) for c in col]
        dates = [parse_datetime(c) for c in col]
        filled = [c for c in col if c.strip().lower() not in MISSING]
        if not filled:
            continue
        if sum(d is not None for d in dates) >= 0.9 * len(filled):
            return i
        if TIME_NAME.match(h + " ") and sum(n is not None for n in nums) >= 0.9 * len(filled):
            vals = [n for n in nums if n is not None]
            if all(b >= a for a, b in zip(vals, vals[1:])):       # un temps ne recule pas
                return i
    return None


def time_axis(headers: List[str], body: List[List[str]], idx: Optional[int], delim: str) -> Tuple[List[float], str]:
    """Axe X en secondes depuis le début + description. Sans colonne de temps : 1 point = 1 seconde (dit explicitement)."""
    n = len(body)
    if idx is None:
        return [float(i) for i in range(n)], "aucune colonne de temps : 1 ligne = 1 seconde"
    col = [r[idx] for r in body]
    # JJ/MM ou MM/JJ ? Jour d'abord (usage français) sauf si une valeur prouve le contraire (un « 13 » en 2e position)
    firsts = [int(m.group(1)) for c in col[:500] for m in [re.match(r"^\s*(\d{1,2})/(\d{1,2})/", c)] if m]
    seconds = [int(m.group(2)) for c in col[:500] for m in [re.match(r"^\s*(\d{1,2})/(\d{1,2})/", c)] if m]
    month_first = bool(seconds) and max(seconds) > 12 and max(firsts) <= 12
    sample = [c for c in col[:100] if c.strip().lower() not in MISSING]
    sample_has_dt = any(parse_datetime(c, month_first) is not None for c in sample) if sample else False
    dts = [parse_datetime(c, month_first) for c in col] if sample_has_dt else []
    if dts and sum(d is not None for d in dts) >= 0.9 * max(1, sum(c.strip().lower() not in MISSING for c in col)):
        out, last, day = [], None, 0
        base = next(d for d in dts if d is not None)
        for d in dts:
            if d is None:
                out.append(out[-1] if out else 0.0)
                continue
            if d.year == 1900 and last is not None and d < last:      # heure seule : passage de minuit
                day += 1
            last = d
            out.append((d - base).total_seconds() + 86400 * day)
        return out, f"début {base.strftime('%Y-%m-%d %H:%M:%S') if base.year != 1900 else base.strftime('%H:%M:%S')}"
    vals = [parse_number(c, delim) for c in col]
    first = next((v for v in vals if v is not None), 0.0)
    h = headers[idx].lower()
    scale = 1.0
    if first > 1e11 or "ms" in h or "milli" in h:
        scale = 1e-3
    out, prev = [], 0.0
    for v in vals:
        prev = (v - first) * scale if v is not None else prev
        out.append(prev)
    return out, f"colonne « {headers[idx]} »" + (" (millisecondes)" if scale != 1.0 else "")


def classify(name: str) -> str:
    """Préfixe de clé reconnu par les onglets du rapport, d'après le nom de la colonne (« other » si inconnu)."""
    for prefix, rx in _KINDS:
        if rx.search(name):
            return prefix
    return "other"


def parse_csv(path: str, quiet: bool = False) -> Optional[BenchmarkResult]:
    """Lit un CSV et retourne un résultat BenchmarkResult prêt à exporter (None si rien d'exploitable)."""
    say = (lambda *a: None) if quiet else print
    say(f"\nLecture de {path}…")
    try:
        headers, body, info = load_table(path)
    except Exception as e:
        say(f" ✗ {e}")
        return None
    delim = info["delim"]
    say(f" → encodage {info['encodage']}, séparateur « {info['separateur']} », {len(body)} ligne(s), "
        f"{'en-tête détecté' if info['en_tete'] else 'sans en-tête (colonnes numérotées)'}"
        + (f", {info['lignes_ignorees_avant']} ligne(s) de métadonnées ignorée(s)" if info["lignes_ignorees_avant"] else ""))
    if not body:
        say(" ✗ aucune ligne de données")
        return None

    tcol = find_time_column(headers, body, delim)
    t, t_desc = time_axis(headers, body, tcol, delim)
    say(f" → axe du temps : {t_desc}")

    series: Dict[str, List[float]] = {}
    labels: Dict[str, str] = {}
    ignored, filled_total = [], 0
    hint = os.path.basename(path).lower()
    for i, h in enumerate(headers):
        if i == tcol:
            continue
        raw = [r[i] for r in body]
        vals = [parse_number(c, delim) for c in raw]
        present = [c for c in raw if c.strip().lower() not in MISSING]
        nums = [v for v in vals if v is not None]
        if len(nums) < 2 or len(nums) < 0.6 * max(1, len(present)):
            ignored.append(h)
            continue
        out, last, missing = [], None, 0
        first_val = nums[0]
        for v in vals:
            if v is None:
                missing += 1
                out.append(last if last is not None else first_val)
            else:
                last = v
                out.append(v)
        filled_total += missing
        generic_header = not info["en_tete"] or bool(GENERIC_COL_NAME.match(h.strip()))
        only_col = len(headers) - (1 if tcol is not None else 0) == 1
        if only_col and generic_header:
            stem_clean = os.path.splitext(os.path.basename(path))[0]
            stem_short = re.sub(r"[_\-.]\d{4}[-_]?\d{2}[-_]?\d{2}[T_]?\d{2}[-_]?\d{2}[-_]?\d{2}.*$", "", stem_clean, flags=re.I)
            if stem_short == stem_clean:
                stem_short = re.sub(r"[_\-.]\d{8}[T_]?\d{6}.*$", "", stem_clean, flags=re.I)
            if stem_short == stem_clean:
                stem_short = re.split(r"[_\-.]\d", stem_clean)[0]
            h = stem_short.strip("_- ").capitalize() or h
        kind = classify(h)
        if kind == "other" and generic_header:
            if re.search(r"pin|pout|power|watt", hint):
                kind = "power"
            elif re.search(r"temp|degc|°c", hint):
                kind = "temp"
            elif re.search(r"freq|clock|mhz|ghz", hint):
                kind = "cpu_freq"
            elif re.search(r"load|charge|cpu|usage", hint):
                kind = "cpu_pct"
        key = f"{kind}_c{i + 1}"
        series[key] = out
        labels[key] = h
    if not series:
        say(" ✗ aucune colonne numérique exploitable")
        return None
    say(f" → {len(series)} colonne(s) tracée(s) : {', '.join(labels.values())}")
    if ignored:
        say(f" → colonne(s) de texte ignorée(s) : {', '.join(ignored)}")
    if filled_total:
        say(f" ⚠ {filled_total} valeur(s) manquante(s) complétée(s) par la dernière valeur connue")

    series["t"] = [round(x, 3) for x in t]
    duration = max(t) if t else len(body)
    metrics = {"series": series, "series_labels": labels, "source_csv": os.path.basename(path), "temps": t_desc,
               "encodage": info["encodage"], "colonnes_ignorees": ignored, "valeurs_completees": filled_total}
    name, category, score, unit = os.path.basename(path), "Import CSV", None, ""
    psu = psu_metrics(labels, series, t)
    if psu:
        metrics.update(psu)
        ts = re.search(r"\d{8}T\d{6}", os.path.basename(path))
        name = f"Alimentation — {ts.group(0)}" if ts else f"Alimentation — {os.path.splitext(os.path.basename(path))[0]}"      # renommée PSU 1, PSU 2… dans main()
        category, score, unit = "Alimentation & Redondance", psu.get("pin_max_w"), "W"
        say(f" → banc d'alimentation détecté (Pin / Pout) : Pin max {psu.get('pin_max_w')} W, rendement moyen {psu.get('rendement_moyen_pct', '—')} %, "
            f"énergie {psu.get('energie_consommee_kwh', '—')} kWh")
    return BenchmarkResult(name=name, category=category, score=score, unit=unit, duration_s=float(duration),
                           raw_output=f"Importé de {path}", metrics=metrics, status="ok")


# ── Bancs d'alimentation (PSU) : colonnes Pin / Pout ─────────────────────────

def _pct(vals: List[float], q: float) -> float:
    """Percentile q (0-100) par interpolation linéaire."""
    v = sorted(vals)
    if not v:
        return 0.0
    k = (len(v) - 1) * q / 100.0
    lo, hi = int(k), min(int(k) + 1, len(v) - 1)
    return v[lo] + (v[hi] - v[lo]) * (k - lo)


def _energy_kwh(power: List[float], t: List[float]) -> float:
    """Énergie (kWh) = intégrale de la puissance sur le temps réel entre échantillons (règle des trapèzes ; les trous restent des trous)."""
    e = 0.0
    for i in range(1, min(len(power), len(t))):
        e += 0.5 * (power[i] + power[i - 1]) * max(0.0, t[i] - t[i - 1])
    return e / 3.6e6


def psu_metrics(labels: Dict[str, str], series: Dict[str, List[float]], t: List[float]) -> Optional[dict]:
    """Synthèse d'un banc d'alimentation si le CSV a des colonnes Pin (entrée AC) et Pout (sortie DC) : max / moyenne / P50 / P95 / min,
    pertes, rendement moyen et intégré, énergies. Pertes et rendement sont recalculés s'ils ne sont pas dans le fichier. None sinon."""
    by = {re.sub(r"[^a-z]", "", lab.lower()): k for k, lab in labels.items()}
    kin = next((by[n] for n in ("pin", "pinw", "puissanceentree", "pentree", "pinput") if n in by), None)
    kout = next((by[n] for n in ("pout", "poutw", "puissancesortie", "psortie", "poutput") if n in by), None)
    if not kin or not kout:
        return None
    pin, pout = series[kin], series[kout]
    n = min(len(pin), len(pout))
    pin, pout, tt = pin[:n], pout[:n], t[:n]
    kl = next((by[x] for x in ("pertes", "pertesw", "losses", "loss") if x in by), None)
    ke = next((by[x] for x in ("rendement", "rendementpct", "efficiency", "eff") if x in by), None)
    loss = series[kl][:n] if kl else [a - b for a, b in zip(pin, pout)]
    eff = series[ke][:n] if ke else [100.0 * b / a if a > 0 else 0.0 for a, b in zip(pin, pout)]
    m: dict = {"duree_effective_s": round(max(tt) - min(tt), 1) if tt else 0.0, "echantillons_total": n}
    for tag, vals in (("pin", pin), ("pout", pout), ("dissipation", loss)):
        if not vals:
            continue
        m[f"{tag}_max_w"] = round(max(vals), 2)
        m[f"{tag}_moy_w"] = round(sum(vals) / len(vals), 2)
        if tag != "dissipation":
            m[f"{tag}_p50_w"] = round(_pct(vals, 50), 2)
            m[f"{tag}_p95_w"] = round(_pct(vals, 95), 2)
        m[f"{tag}_min_w"] = round(min(vals), 2)
    valid = [e for e in eff if e > 0]
    if valid:
        m["rendement_moyen_pct"] = round(sum(valid) / len(valid), 2)
        m["rendement_p50_pct"] = round(_pct(valid, 50), 2)
        m["rendement_min_pct"] = round(min(valid), 2)
    ein, eout = _energy_kwh(pin, tt), _energy_kwh(pout, tt)
    if ein > 0:
        m["energie_consommee_kwh"] = round(ein, 3)
        m["energie_restituee_kwh"] = round(eout, 3)
        m["dissipation_totale_kwh"] = round(ein - eout, 3)
        m["rendement_integre_global_pct"] = round(100.0 * eout / ein, 2)
    return m


# ── Optimisation & Décimation pour grands jeux de données ──────────────────

def _compute_detailed_stats(r: BenchmarkResult) -> dict:
    """Calcule les statistiques exactes (min, max, moyenne, nombre d'échantillons)
    sur l'ensemble des données brutes avant toute décimation pour l'affichage."""
    ser = r.metrics.get("series", {})
    lbls = r.metrics.get("series_labels", {})
    stats = {}
    for k, v in ser.items():
        if k == "t" or not isinstance(v, list) or not v:
            continue
        lab = lbls.get(k, k)
        nums = [x for x in v if isinstance(x, (int, float))]
        if not nums:
            continue
        stats[k] = {
            "label": lab,
            "count": len(nums),
            "min": min(nums),
            "max": max(nums),
            "moy": sum(nums) / len(nums),
        }
    r.metrics["detailed_stats"] = stats
    return stats


def decimate_series_indices(series_dict: Dict[str, List[float]], max_points: int = 1500) -> List[int]:
    """Sélectionne au plus `max_points` indices représentatifs en préservant fidèlement
    les extrema locaux (pics et creux) de chaque courbe, ainsi que la synchronisation temporelle."""
    t_arr = series_dict.get("t", [])
    n = len(t_arr)
    if n <= max_points or max_points <= 0:
        return list(range(n))

    numeric_keys = [k for k, v in series_dict.items() if k != "t" and isinstance(v, list) and len(v) == n and v]
    if not numeric_keys:
        step = max(1, n // max_points)
        return list(range(0, n, step))

    m = min(len(numeric_keys), 6)
    pts_per_bucket = max(2, 2 * m)
    num_buckets = max(10, max_points // pts_per_bucket)
    bucket_size = max(1, n // num_buckets)

    chosen = {0, n - 1}
    for b in range(num_buckets):
        s_idx = b * bucket_size
        e_idx = min(n, (b + 1) * bucket_size)
        if s_idx >= e_idx:
            break
        chosen.add(s_idx)
        for k in numeric_keys[:8]:
            col = series_dict[k]
            sub = col[s_idx:e_idx]
            if not sub:
                continue
            vmin = min(sub)
            vmax = max(sub)
            chosen.add(s_idx + sub.index(vmin))
            chosen.add(s_idx + sub.index(vmax))

    return sorted(chosen)


def slim_result_series(res: BenchmarkResult, max_points: int = 1500) -> Tuple[BenchmarkResult, int, int]:
    """Réduit le nombre de points des séries d'un BenchmarkResult pour la fluidité d'affichage HTML/Chart.js.
    Retourne (res, nb_points_bruts, nb_points_decimes).
    Ne modifie pas les scalaires ni les calculs énergétiques déjà effectués."""
    ser = res.metrics.get("series")
    if not isinstance(ser, dict) or "t" not in ser:
        return res, 0, 0

    if "detailed_stats" not in res.metrics:
        _compute_detailed_stats(res)

    t_arr = ser.get("t", [])
    n = len(t_arr)
    if n <= max_points or max_points <= 0:
        res.metrics["echantillons_bruts"] = n
        res.metrics["points_affiches"] = n
        return res, n, n

    idx = decimate_series_indices(ser, max_points)
    for k in list(ser.keys()):
        v = ser[k]
        if isinstance(v, list) and len(v) == n:
            ser[k] = [v[i] for i in idx]

    res.metrics["echantillons_bruts"] = n
    res.metrics["points_affiches"] = len(idx)
    return res, n, len(idx)


# ── Rapport ───────────────────────────────────────────────────────────────────

def _fmt(v, nd=1) -> str:
    """Nombre lisible (espace comme séparateur de milliers)."""
    try:
        return f"{float(v):,.{nd}f}".replace(",", " ")
    except (TypeError, ValueError):
        return "—"


def psu_overview_html(units: List[BenchmarkResult]) -> str:
    """Page de synthèse d'un banc d'alimentation (une ou plusieurs alimentations mesurées) : 4 cartes + tableau par alimentation.
    Remplace le tableau de bord générique de BenchSuite (verdict / diagnostic matériel / catégories), qui n'a pas de sens pour un CSV."""
    def card(color, label, big, unit, chips):
        return (f'<div class="stat-card"><div class="stat-label"><span class="stat-dot" style="color:{color};background:{color}"></span>{label}</div>'
                f'<div class="stat-val">{big}<span class="stat-unit"> {unit}</span></div><div class="stat-range">' + "".join(f"<span>{c}</span>" for c in chips) + "</div></div>")
    names = [u.name.split(" — ")[0] for u in units]
    ms = [u.metrics for u in units]
    tot = lambda key: sum((m.get(key) or 0) for m in ms)
    ein, eout = tot("energie_consommee_kwh"), tot("energie_restituee_kwh")
    multi = len(units) > 1
    cards = [
        card("#3b82f6", "Puissance d'entrée moyenne (AC)" + (" — total" if multi else ""), _fmt(tot("pin_moy_w"), 0), "W", [f"{n} : {_fmt(m.get('pin_moy_w'), 0)} W" for n, m in zip(names, ms)]),
        card("#10b981", "Puissance de sortie moyenne (DC)" + (" — total" if multi else ""), _fmt(tot("pout_moy_w"), 0), "W", [f"{n} : {_fmt(m.get('pout_moy_w'), 0)} W" for n, m in zip(names, ms)]),
        card("#f59e0b", "Rendement" + (" global" if multi else ""), _fmt(100.0 * eout / ein if ein else 0, 2), "%", [f"{n} : {_fmt(m.get('rendement_integre_global_pct'), 2)} %" for n, m in zip(names, ms)]),
        card("#f43f5e", "Énergie consommée" + (" — total" if multi else ""), _fmt(ein, 2), "kWh", [f"{n} : {_fmt(m.get('energie_consommee_kwh'), 2)} kWh" for n, m in zip(names, ms)]
             + [f"Pertes : {_fmt(tot('dissipation_totale_kwh'), 2)} kWh"]),
    ]
    rows = [("Durée (s)", "duree_effective_s", 0), ("Échantillons", "echantillons_total", 0),
            ("Pin max (W)", "pin_max_w", 1), ("Pin moyenne (W)", "pin_moy_w", 1), ("Pin P50 (W)", "pin_p50_w", 1), ("Pin P95 (W)", "pin_p95_w", 1), ("Pin min (W)", "pin_min_w", 1),
            ("Pout max (W)", "pout_max_w", 1), ("Pout moyenne (W)", "pout_moy_w", 1), ("Pout P50 (W)", "pout_p50_w", 1), ("Pout P95 (W)", "pout_p95_w", 1), ("Pout min (W)", "pout_min_w", 1),
            ("Pertes max (W)", "dissipation_max_w", 1), ("Pertes moyenne (W)", "dissipation_moy_w", 1), ("Pertes min (W)", "dissipation_min_w", 1),
            ("Rendement moyen (%)", "rendement_moyen_pct", 2), ("Rendement P50 (%)", "rendement_p50_pct", 2), ("Rendement min (%)", "rendement_min_pct", 2),
            ("Rendement intégré (Eout / Ein, %)", "rendement_integre_global_pct", 2),
            ("Énergie consommée (kWh)", "energie_consommee_kwh", 3), ("Énergie restituée (kWh)", "energie_restituee_kwh", 3), ("Pertes totales (kWh)", "dissipation_totale_kwh", 3)]
    head = "<tr><th>Mesure</th>" + "".join(f"<th>{_html_escape(n)}<br><span class='muted'>{_html_escape(u.name.split(' — ', 1)[-1])}</span></th>" for n, u in zip(names, units)) + "</tr>"
    body = "".join("<tr><td>" + lab + "</td>" + "".join(f"<td>{_fmt(m.get(key), nd)}</td>" for m in ms) + "</tr>" for lab, key, nd in rows)
    raw_samples = max((u.metrics.get("echantillons_bruts") or 0) for u in units) if units else 0
    decimated_points = max((u.metrics.get("points_affiches") or 0) for u in units) if units else 0
    opt_note = ""
    if raw_samples > decimated_points and decimated_points > 0:
        opt_note = f" Courbes temporelles optimisées pour l'affichage ({_fmt(decimated_points, 0)} points conservés sur {_fmt(raw_samples, 0)} échantillons bruts, pics et creux préservés)."
    note = ("Les valeurs « total » additionnent les alimentations. " if multi else "") + (
        "Calculée sur toutes les lignes du CSV : pertes et rendement recalculés (Pin − Pout, Pout / Pin) s'ils ne sont pas dans le fichier ; énergies = intégrale "
        "de la puissance sur le temps réel entre échantillons ; rendement intégré = énergie restituée / énergie consommée."
    ) + opt_note
    return ('<div class="stat-grid">' + "".join(cards) + '</div><div class="card"><h2>Synthèse par alimentation</h2>'
            f'<p class="muted">{note}</p><table>{head}{body}</table></div>')


def _html_escape(t: str) -> str:
    return t.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def postprocess(html_path: str, units: Optional[List[BenchmarkResult]] = None) -> None:
    """Retire du rapport BenchSuite ce qui n'a pas de sens pour un CSV importé (verdict, diagnostic matériel, CPU / GPU / stockage, système).
    CSV quelconque : seul l'onglet « Graphique » reste. Banc d'alimentation (`units`) : Synthèse, Graphique, Résultats détaillés, JSON brut."""
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()
    tabs = [("overview", "Synthèse")] if units else []
    tabs += [("charts", "Graphique")] + ([("benches", "Résultats détaillés")] if units else []) + ([("json", "JSON brut")] if units else [])
    first = tabs[0][0]
    bar = '<div class="tabs" id="mainTabs">' + "".join(f'<button class="tab{" active" if i == 0 else ""}" data-view="{v}">{lab}</button>' for i, (v, lab) in enumerate(tabs)) + "</div>"
    html, n1 = re.subn(r'<div class="tabs" id="mainTabs">.*?</div>', lambda m: bar, html, count=1, flags=re.S)
    if not n1:
        print(" ⚠ barre d'onglets non trouvée : le rapport complet est conservé")
        return
    overview = psu_overview_html(units) if units else ""
    html = re.sub(r'<section id="overview" class="view[^"]*">.*?</section>',
                  lambda m: f'<section id="overview" class="view">{overview}</section>', html, count=1, flags=re.S)
    html = re.sub(r'<section id="%s" class="view[^"]*">' % first, lambda m: f'<section id="{first}" class="view active">', html, count=1)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html)


_CHARTJS_ASSETS = (
    ("chart.umd.min.js", "https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"),
    ("hammer.min.js", "https://cdn.jsdelivr.net/npm/hammerjs@2.0.8/hammer.min.js"),
    ("chartjs-plugin-zoom.min.js", "https://cdn.jsdelivr.net/npm/chartjs-plugin-zoom@2.0.1/dist/chartjs-plugin-zoom.min.js"),
)
_CHARTJS_SCRIPTS_CACHE: Optional[str] = None


def _chart_scripts_html() -> str:
    """Retourne les balises <script> Chart.js 4.4.1 + Hammer.js 2.0.8 + chartjs-plugin-zoom 2.0.1.
    Si les fichiers sont présents dans third_party/chartjs/ (mode local), ils sont embarqués
    directement dans le HTML pour un affichage 100 % autonome et hors ligne.
    Sinon (script isolé sous Windows), des liens CDN stables sont injectés."""
    global _CHARTJS_SCRIPTS_CACHE
    if _CHARTJS_SCRIPTS_CACHE is not None:
        return _CHARTJS_SCRIPTS_CACHE
    root = os.path.dirname(os.path.abspath(__file__))
    asset_dir = os.path.join(root, "third_party", "chartjs")
    tags = []
    for fname, cdn_url in _CHARTJS_ASSETS:
        fpath = os.path.join(asset_dir, fname)
        content = ""
        if os.path.isfile(fpath):
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                content = ""
        if content.strip():
            safe_js = re.sub(r"</script", r"<\\/script", content, flags=re.I)
            tags.append(f"<script>\n{safe_js}\n</script>")
        else:
            tags.append(f'<script src="{cdn_url}"></script>')
    _CHARTJS_SCRIPTS_CACHE = "\n".join(tags)
    return _CHARTJS_SCRIPTS_CACHE


def export_html_standalone(results: List[BenchmarkResult], outdir: str, meta: dict,
                           units: Optional[List[BenchmarkResult]] = None) -> str:
    """Génère un rapport HTML interactif autonome (Chart.js via CDN, thème clair / sombre,
    synthèse d'alimentation, onglets de navigation) utilisable même sans le reste du dépôt BenchSuite."""
    os.makedirs(outdir, exist_ok=True)
    sys_name = meta.get("system_name") or meta.get("machine") or "Import CSV"
    ts_now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    theme = os.environ.get("BENCHSUITE_THEME", "dark")

    payload = {
        "results": [r.to_dict() if hasattr(r, "to_dict") else r.__dict__ for r in results],
        "meta": {"system_name": sys_name, "exported_at": ts_now, "version": "1.0.22", "source": "csv2html"},
    }
    payload_json = json.dumps(payload, ensure_ascii=False)

    raw_samples = max((r.metrics.get("echantillons_bruts") or 0) for r in results) if results else 0
    decimated_points = max((r.metrics.get("points_affiches") or 0) for r in results) if results else 0
    opt_badge = ""
    if raw_samples > decimated_points and decimated_points > 0:
        opt_badge = f' · <span style="background:rgba(255,102,0,0.15);color:var(--accent);padding:2px 8px;border-radius:12px;font-weight:600;font-size:12px;">⚡ Affichage optimisé : {_fmt(decimated_points, 0)} pts/courbe ({_fmt(raw_samples, 0)} éch. analysés)</span>'

    if len(results) == 1:
        stem = re.sub(r"[^\w\-. ]+", "_", os.path.splitext(results[0].metrics.get("source_csv") or results[0].name)[0]).strip() or "rapport"
        html_filename = f"{stem}_rapport.html"
    else:
        kind = "alimentation" if any(r.category.startswith("Alimentation") for r in results) else "csv"
        html_filename = f"rapport_{kind}_{len(results) - 1}_fichiers_{datetime.now().strftime('%Y%m%d_%H%M%S')}.html"

    html_path = os.path.join(outdir, html_filename)

    tabs = [("overview", "Synthèse")] if units else []
    tabs += [("charts", "Graphique"), ("benches", "Résultats détaillés"), ("json", "JSON brut")]
    first_tab = tabs[0][0]
    tabs_html = '<div class="tabs" id="mainTabs">' + "".join(
        f'<button class="tab{" active" if i == 0 else ""}" data-view="{v}">{lab}</button>'
        for i, (v, lab) in enumerate(tabs)
    ) + '</div>'

    overview_html = psu_overview_html(units) if units else ""

    bench_options_html = "".join(
        f'<option value="{i}">{_html_escape(r.name)}</option>'
        for i, r in enumerate(results)
    )

    detailed_rows = []
    for r in results:
        stats = r.metrics.get("detailed_stats")
        if stats:
            for k, s in stats.items():
                detailed_rows.append(
                    f"<tr><td><b>{_html_escape(r.name)}</b></td><td>{_html_escape(s['label'])}</td>"
                    f"<td>{_fmt(s['count'], 0)}</td><td>{_fmt(s['min'], 2)}</td><td>{_fmt(s['moy'], 2)}</td><td>{_fmt(s['max'], 2)}</td></tr>"
                )
        else:
            ser = r.metrics.get("series", {})
            lbls = r.metrics.get("series_labels", {})
            for k, v in ser.items():
                if k == "t" or not isinstance(v, list) or not v:
                    continue
                lab = lbls.get(k, k)
                nums = [x for x in v if isinstance(x, (int, float))]
                if not nums:
                    continue
                vmin = min(nums)
                vmax = max(nums)
                vmoy = sum(nums) / len(nums)
                detailed_rows.append(
                    f"<tr><td><b>{_html_escape(r.name)}</b></td><td>{_html_escape(lab)}</td>"
                    f"<td>{_fmt(len(nums), 0)}</td><td>{_fmt(vmin, 2)}</td><td>{_fmt(vmoy, 2)}</td><td>{_fmt(vmax, 2)}</td></tr>"
                )
    detailed_table_html = "".join(detailed_rows) if detailed_rows else "<tr><td colspan='6'>Aucune donnée</td></tr>"

    html_content = f"""<!DOCTYPE html>
<html lang="fr" data-theme="{theme}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BenchSuite — {_html_escape(sys_name)} ({len(results)} résultat{'s' if len(results) > 1 else ''})</title>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=Fira+Code:wght@500&display=swap" rel="stylesheet">
{_chart_scripts_html()}
<style>
:root {{
  --bg-base: #0d0d0d;
  --bg-surface: #141414;
  --bg-card: #141414;
  --border-color: #2a2a2a;
  --text-main: #f0f0f0;
  --text-muted: #888888;
  --accent: #ff6600;
  --ok: #4caf50;
  --bad: #ff3333;
  --warn: #ff9966;
  --grid: rgba(255, 255, 255, 0.08);
}}
[data-theme="light"] {{
  --bg-base: #f8fafc;
  --bg-surface: #ffffff;
  --bg-card: #f1f5f9;
  --border-color: #e2e8f0;
  --text-main: #0f172a;
  --text-muted: #64748b;
  --accent: #ea580c;
  --ok: #16a34a;
  --bad: #dc2626;
  --warn: #d97706;
  --grid: rgba(0, 0, 0, 0.08);
}}
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{
  background: var(--bg-base);
  color: var(--text-main);
  font-family: 'Inter', system-ui, -apple-system, sans-serif;
  font-size: 14px;
  min-height: 100vh;
  padding: 30px;
  -webkit-font-smoothing: antialiased;
}}
header {{ margin-bottom: 24px; }}
h1 {{ font-size: 24px; font-weight: 700; margin-bottom: 6px; letter-spacing: -0.02em; }}
h2 {{ font-size: 18px; margin: 0 0 14px; }}
.subtitle, .muted {{ color: var(--text-muted); font-size: 13px; }}
.topbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: flex-start; flex-wrap: wrap; }}
.top-actions {{ display: flex; gap: 8px; flex-wrap: wrap; margin-top: 8px; }}
.note-btn {{
  background: var(--bg-surface);
  color: var(--text-main);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 7px 12px;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  transition: all .15s ease;
}}
.note-btn:hover {{ border-color: var(--accent); color: var(--accent); }}
.tabs {{
  display: inline-flex;
  gap: 8px;
  background: var(--bg-surface);
  padding: 6px;
  border-radius: 10px;
  border: 1px solid var(--border-color);
  margin: 20px 0 24px;
  flex-wrap: wrap;
}}
.subtabs {{ margin: 0 0 16px; }}
.tab {{
  padding: 8px 16px;
  border: none;
  background: transparent;
  color: var(--text-muted);
  border-radius: 6px;
  cursor: pointer;
  font-size: 13px;
  font-weight: 600;
  font-family: inherit;
  transition: all .2s ease;
}}
.tab:hover:not(.active) {{ color: var(--text-main); background: rgba(255,255,255,.05); }}
.tab.active {{ background: var(--accent); color: #fff; box-shadow: 0 2px 4px rgba(0,0,0,.2); }}
.view {{ display: none; }}
.view.active {{ display: block; }}
.stat-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 24px;
}}
.stat-card, .card {{
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 18px 20px;
  box-shadow: 0 4px 10px -2px rgba(0,0,0,.15);
}}
.stat-card {{ display: flex; flex-direction: column; min-height: 125px; }}
.stat-label {{ font-size: 12px; color: var(--text-muted); font-weight: 600; display: flex; align-items: center; gap: 8px; margin-bottom: 10px; }}
.stat-dot {{ width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; box-shadow: 0 0 8px currentColor; }}
.stat-val {{ font-size: 26px; font-weight: 700; font-family: 'Fira Code', monospace; margin-bottom: 6px; }}
.stat-unit {{ font-size: 13px; color: var(--text-muted); font-family: 'Inter', sans-serif; font-weight: 500; }}
.stat-range {{ display: flex; justify-content: space-between; gap: 8px; color: var(--text-muted); font-size: 12px; margin-top: auto; flex-wrap: wrap; }}
.stat-range span {{ background: rgba(255,255,255,.06); padding: 3px 7px; border-radius: 5px; }}
[data-theme="light"] .stat-range span {{ background: rgba(0,0,0,.05); }}
.stat-sub {{ font-size: 11px; color: var(--text-muted); margin-top: -4px; margin-bottom: 6px; }}
#chartStatsGrid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}}
#chartStatsGrid .stat-card {{
  min-height: 95px;
  padding: 12px 14px;
}}
#chartStatsGrid .stat-val {{
  font-size: 20px;
  margin-bottom: 4px;
}}
.chart-container {{
  background: var(--bg-surface);
  border: 1px solid var(--border-color);
  border-radius: 12px;
  padding: 22px;
  margin-bottom: 24px;
  box-shadow: 0 4px 10px -2px rgba(0,0,0,.15);
}}
.chart-toolbar {{
  display: flex;
  justify-content: space-between;
  gap: 14px;
  flex-wrap: wrap;
  align-items: center;
  margin-bottom: 16px;
}}
.select {{
  background: var(--bg-base);
  color: var(--text-main);
  border: 1px solid var(--border-color);
  border-radius: 8px;
  padding: 8px 12px;
  font: inherit;
  font-size: 13px;
  font-weight: 600;
  outline: none;
}}
.chart-wrap {{ position: relative; width: 100%; height: 440px; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 10px 18px; margin-bottom: 16px; }}
.legend-item {{ display: inline-flex; align-items: center; gap: 8px; font-size: 13px; font-weight: 600; color: var(--text-muted); cursor: pointer; user-select: none; transition: .2s; }}
.legend-item:hover {{ color: var(--text-main); }}
.legend-item.muted {{ opacity: .4; filter: grayscale(100%); }}
.legend-line {{ width: 24px; height: 4px; border-radius: 2px; flex-shrink: 0; display: inline-block; vertical-align: middle; }}
.quick-filters {{ display: inline-flex; gap: 6px; flex-wrap: wrap; align-items: center; }}
.filter-btn {{ background: var(--bg-base); color: var(--text-muted); border: 1px solid var(--border-color); border-radius: 6px; padding: 5px 10px; font-size: 12px; font-weight: 700; cursor: pointer; transition: all .15s ease; display: inline-flex; align-items: center; gap: 4px; font-family: inherit; }}
.filter-btn:hover {{ color: var(--text-main); border-color: var(--accent); background: rgba(255,102,0,0.1); }}
.filter-btn.active {{ background: rgba(255,102,0,0.2); color: var(--accent); border-color: var(--accent); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }}
th, td {{ border-bottom: 1px solid var(--border-color); padding: 9px 12px; text-align: left; vertical-align: middle; }}
th {{ color: var(--text-main); background: rgba(255,255,255,.03); font-weight: 700; }}
[data-theme="light"] th {{ background: rgba(0,0,0,.03); }}
pre {{
  white-space: pre-wrap;
  background: var(--bg-base);
  border: 1px solid var(--border-color);
  border-radius: 10px;
  padding: 14px;
  color: var(--accent);
  max-height: 480px;
  overflow: auto;
  font-family: 'Fira Code', monospace;
  font-size: 12px;
}}
</style>
</head>
<body>
<script type="application/json" id="payload">{payload_json}</script>
<header>
  <div class="topbar">
    <div>
      <h1>BenchSuite — Rapport d'importation CSV</h1>
      <p class="subtitle">Système testé : <b>{_html_escape(sys_name)}</b> · Source : <b>csv2html (mode autonome)</b> · Export : {ts_now} · Version : v1.0.22{opt_badge}</p>
    </div>
    <div class="top-actions">
      <button class="note-btn" id="themeBtn" type="button" onclick="cycleTheme()">🌙 Sombre</button>
    </div>
  </div>
  {tabs_html}
</header>

<main>
  {'<section id="overview" class="view' + (' active' if first_tab == 'overview' else '') + '">' + overview_html + '</section>' if units else ''}

  <section id="charts" class="view{' active' if first_tab == 'charts' else ''}">
    <div class="chart-container" style="margin-bottom:18px;">
      <div class="chart-toolbar" style="margin-bottom:0;">
        <div>
          <h2>Analyse par benchmark</h2>
          <p class="subtitle">Sélectionnez le fichier à visualiser, puis filtrez par type de métrique.</p>
        </div>
        <select class="select" id="benchSelect" onchange="onBenchSelect(this.value)">
          {bench_options_html}
        </select>
      </div>
    </div>
    <div class="tabs subtabs" id="metricTabs"></div>
    <div id="chartStatsGrid"></div>
    <div class="chart-container">
      <div class="chart-toolbar" style="margin-bottom:14px;">
        <div>
          <h3 id="chartTitle" style="font-size:16px;">Courbes temporelles</h3>
          <p id="chartHelp" class="muted" style="margin-top:4px;font-size:12px;">Clic-gauche glisser pour zoomer · Ctrl + Molette · Shift + Glisser pour déplacer · Double-clic pour réinitialiser · Alt + Clic sur une légende pour isoler</p>
        </div>
        <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;">
          <div id="chartQuickFilters" class="quick-filters" style="display:none;"></div>
          <button class="note-btn" id="resetZoomBtn" type="button" style="display:none;font-size:12px;padding:6px 10px;background:rgba(255,102,0,0.15);color:var(--accent);border-color:var(--accent);" onclick="resetChartZoom()">Réinitialiser le zoom</button>
          <button class="note-btn" id="copyChartBtn" type="button" style="font-size:12px;padding:6px 10px;" title="Copie le graphique (titre, légende, courbes) comme image : collez-la dans Word, Teams, un mail…" onclick="copyChartImage(this)">📷 Copier le graphique</button>
          <button class="note-btn" id="downloadChartBtn" type="button" style="font-size:12px;padding:6px 10px;" title="Enregistre le graphique en PNG" onclick="downloadChartPNG(this)">💾 PNG</button>
        </div>
      </div>
      <div class="legend" id="legendRow"></div>
      <div class="chart-wrap">
        <canvas id="mainChart"></canvas>
      </div>
    </div>
  </section>

  <section id="benches" class="view{' active' if first_tab == 'benches' else ''}">
    <div class="card">
      <h2>Résultats détaillés des séries de mesures</h2>
      <p class="muted">Valeurs min, max, moyenne et nombre d'échantillons calculées sur l'ensemble de chaque fichier.</p>
      <table>
        <thead>
          <tr><th>Benchmark / Fichier</th><th>Métrique</th><th>Échantillons</th><th>Min</th><th>Moyenne</th><th>Max</th></tr>
        </thead>
        <tbody>
          {detailed_table_html}
        </tbody>
      </table>
    </div>
  </section>

  <section id="json" class="view{' active' if first_tab == 'json' else ''}">
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;">
        <h2>Données JSON brutes</h2>
        <button class="note-btn" onclick="copyJson()">📋 Copier le JSON</button>
      </div>
      <p class="muted" style="margin-bottom:10px;">Le JSON est formaté à la demande pour garantir une ouverture instantanée du rapport.</p>
      <pre id="jsonPre">Cliquez sur cet onglet pour afficher les données JSON brutes formatées...</pre>
    </div>
  </section>
</main>

<script>
const COLORS = ["#ff6600","#3b82f6","#10b981","#f59e0b","#ef4444","#8b5cf6","#ec4899","#06b6d4","#84cc16","#f97316"];
let currentTheme = localStorage.getItem("benchsuite_theme") || document.documentElement.dataset.theme || "dark";
applyTheme(currentTheme);

function applyTheme(t) {{
  currentTheme = t;
  document.documentElement.dataset.theme = t;
  const btn = document.getElementById("themeBtn");
  if (btn) btn.textContent = t === "light" ? "☀️ Clair" : "🌙 Sombre";
  localStorage.setItem("benchsuite_theme", t);
  if (window.chartInstance) updateChartColors();
}}

function cycleTheme() {{
  applyTheme(currentTheme === "light" ? "dark" : "light");
}}

document.querySelectorAll("#mainTabs .tab").forEach(tab => {{
  tab.onclick = () => {{
    document.querySelectorAll("#mainTabs .tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll("main .view").forEach(v => v.classList.remove("active"));
    tab.classList.add("active");
    const target = document.getElementById(tab.dataset.view);
    if (target) target.classList.add("active");
    if (tab.dataset.view === "charts" && window.chartInstance) {{
      setTimeout(() => window.chartInstance.resize(), 50);
    }}
    if (tab.dataset.view === "json") {{
      loadJsonTab();
    }}
  }};
}});

const payload = JSON.parse(document.getElementById("payload").textContent || "{{}}");
const results = payload.results || [];
let activeBenchIdx = 0;
let activeGroup = "all";
let hiddenSeries = {{}};
window.chartInstance = null;

function escapeHtml(s) {{
  return String(s).replace(/[&<>"']/g, c => ({{"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}}[c]));
}}

function fmtNum(v) {{
  if (!Number.isFinite(v)) return "-";
  if (Math.abs(v) >= 1000) return v.toFixed(0);
  if (Math.abs(v) >= 10) return v.toFixed(1);
  return v.toFixed(2);
}}

function fmtDuration(sec) {{
  if (!Number.isFinite(sec)) return "-";
  if (Math.abs(sec) < 60) return sec.toFixed(1) + " s";
  const s = Math.max(0, Math.round(sec));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const parts = [];
  if (d > 0) parts.push(d + "j");
  if (h > 0 || d > 0) parts.push(h + "h");
  parts.push(String(m).padStart(2, "0") + "m");
  parts.push(String(r).padStart(2, "0") + "s");
  return sec.toFixed(1) + " s (" + parts.join(" ") + ")";
}}

function onBenchSelect(idx) {{
  activeBenchIdx = parseInt(idx, 10) || 0;
  hiddenSeries = {{}};
  const btn = document.getElementById("resetZoomBtn");
  if (btn) btn.style.display = "none";
  buildMetricSubtabs();
  renderChart();
}}

function getMetricGroup(key) {{
  if (/power|watt|puissance|pin|pout/i.test(key)) return "power";
  if (/temp|°c|celsius/i.test(key)) return "temp";
  return "other";
}}

function getGroupLabel(g) {{
  if (g === "power") return "Puissance (W)";
  if (g === "temp") return "Température (°C)";
  if (g === "other") return "Valeurs";
  return "Toutes";
}}

function getGroupUnit(g) {{
  if (g === "power") return "W";
  if (g === "temp") return "°C";
  return "";
}}

function getSeriesUnit(key, label) {{
  const g = getMetricGroup(key);
  if (g === "power") return "W";
  if (g === "temp") return "°C";
  const m = String(label || "").match(/\\(([^)]+)\\)\\s*$/);
  return m ? m[1] : "";
}}

function buildMetricSubtabs() {{
  const res = results[activeBenchIdx];
  if (!res || !res.metrics || !res.metrics.series) return;
  const ser = res.metrics.series;
  const groups = new Set(["all"]);
  for (const k of Object.keys(ser)) {{
    if (k === "t") continue;
    groups.add(getMetricGroup(k));
  }}
  const container = document.getElementById("metricTabs");
  container.innerHTML = "";
  if (groups.size <= 2) {{
    activeGroup = "all";
    return;
  }}
  if (!groups.has(activeGroup)) {{
    activeGroup = "all";
  }}
  groups.forEach(g => {{
    const btn = document.createElement("button");
    btn.className = "tab" + (g === activeGroup ? " active" : "");
    btn.textContent = getGroupLabel(g);
    btn.onclick = () => {{
      activeGroup = g;
      hiddenSeries = {{}};
      container.querySelectorAll(".tab").forEach(b => b.classList.remove("active"));
      btn.classList.add("active");
      const rBtn = document.getElementById("resetZoomBtn");
      if (rBtn) rBtn.style.display = "none";
      renderChart();
    }};
    container.appendChild(btn);
  }});
}}

function isChartZoomed() {{
  const rBtn = document.getElementById("resetZoomBtn");
  return !!(rBtn && rBtn.style.display !== "none" && window.chartInstance);
}}

function updateZoomStats(xMin, xMax) {{
  const grid = document.getElementById("chartStatsGrid");
  if (!grid) return;
  const ch = window.chartInstance;
  const res = results[activeBenchIdx];
  if (!ch || !ch.data || !ch.data.datasets || !ch.data.datasets.length) {{
    grid.innerHTML = "";
    return;
  }}
  const isZoomed = (xMin !== undefined && xMax !== undefined && Number.isFinite(xMin) && Number.isFinite(xMax));
  const rawStats = (res && res.metrics && res.metrics.detailed_stats) || null;
  const cards = [];
  if (isZoomed) {{
    const span = Math.max(0, xMax - xMin);
    cards.push(
      '<div class="stat-card" style="border-color:var(--accent);">' +
      '<div class="stat-label"><span class="stat-dot" style="color:var(--accent);background:var(--accent);"></span>Fenêtre zoomée</div>' +
      '<div class="stat-val" style="color:var(--accent);font-size:18px;">' + escapeHtml(fmtDuration(span)) + '</div>' +
      '<div class="stat-range"><span>Début : ' + fmtNum(xMin) + ' s</span><span>Fin : ' + fmtNum(xMax) + ' s</span></div>' +
      '</div>'
    );
  }}
  let visibleCount = 0;
  ch.data.datasets.forEach((ds, idx) => {{
    if (!ch.isDatasetVisible(idx)) return;
    visibleCount++;
    if (visibleCount > 12) return;
    let mn = Infinity, mx = -Infinity, sum = 0, cnt = 0;
    if (!isZoomed && rawStats && ds.seriesKey && rawStats[ds.seriesKey]) {{
      const s = rawStats[ds.seriesKey];
      mn = s.min;
      mx = s.max;
      sum = s.moy * s.count;
      cnt = s.count;
    }} else {{
      const pts = ds.data || [];
      for (let i = 0; i < pts.length; i++) {{
        const pt = pts[i];
        if (!pt || !Number.isFinite(pt.y)) continue;
        if (isZoomed && (pt.x < xMin || pt.x > xMax)) continue;
        if (pt.y < mn) mn = pt.y;
        if (pt.y > mx) mx = pt.y;
        sum += pt.y;
        cnt++;
      }}
    }}
    if (cnt === 0) return;
    const avg = sum / cnt;
    const col = ds.borderColor || "#ff6600";
    const unit = ds.seriesUnit || getGroupUnit(activeGroup);
    const scope = isZoomed ? ("moy. zone (" + cnt + " pts)") : "moy. globale";
    cards.push(
      '<div class="stat-card">' +
      '<div class="stat-label" title="' + escapeHtml(ds.label || "") + '"><span class="stat-dot" style="color:' + col + ';background:' + col + ';"></span>' + escapeHtml(ds.label || "") + '</div>' +
      '<div class="stat-val" style="color:' + col + ';">' + fmtNum(avg) + '<span class="stat-unit"> ' + escapeHtml(unit) + ' ' + scope + '</span></div>' +
      '<div class="stat-range"><span>Min : ' + fmtNum(mn) + '</span><span>Max : ' + fmtNum(mx) + '</span></div>' +
      '</div>'
    );
  }});
  grid.innerHTML = cards.join("");
}}

function applyLegendVisibility() {{
  const ch = window.chartInstance;
  if (ch && ch.data && ch.data.datasets) {{
    ch.data.datasets.forEach((ds, idx) => {{
      ch.setDatasetVisibility(idx, !hiddenSeries[ds.seriesKey]);
    }});
    ch.update("none");
  }}
  renderLegend();
  if (isChartZoomed()) {{
    updateZoomStats(window.chartInstance.scales.x.min, window.chartInstance.scales.x.max);
  }} else {{
    updateZoomStats();
  }}
}}

function renderLegend() {{
  const ch = window.chartInstance;
  const row = document.getElementById("legendRow");
  const filterBox = document.getElementById("chartQuickFilters");
  if (!row || !ch || !ch.data || !ch.data.datasets) return;
  const datasets = ch.data.datasets;
  row.innerHTML = datasets.map((ds, i) => {{
    const isMuted = !!hiddenSeries[ds.seriesKey];
    const col = ds.borderColor || COLORS[i % COLORS.length];
    return '<span class="legend-item' + (isMuted ? ' muted' : '') + '" data-series-key="' + escapeHtml(ds.seriesKey) + '" title="Clic : afficher/masquer · Alt + Clic : isoler">' +
      '<span class="legend-line" style="background:' + col + ';"></span>' + escapeHtml(ds.label) + '</span>';
  }}).join("");

  row.querySelectorAll("[data-series-key]").forEach(el => {{
    el.onclick = e => {{
      const k = el.dataset.seriesKey;
      if (e.altKey || e.ctrlKey || e.metaKey) {{
        const allOtherHidden = datasets.every(ds => ds.seriesKey === k || !!hiddenSeries[ds.seriesKey]);
        if (allOtherHidden && !hiddenSeries[k]) {{
          hiddenSeries = {{}};
        }} else {{
          hiddenSeries = {{}};
          datasets.forEach(ds => {{
            if (ds.seriesKey !== k) hiddenSeries[ds.seriesKey] = true;
          }});
        }}
      }} else {{
        hiddenSeries[k] = !hiddenSeries[k];
      }}
      applyLegendVisibility();
    }};
  }});

  if (filterBox) {{
    if (datasets.length > 1) {{
      filterBox.style.display = "inline-flex";
      const total = datasets.length;
      const pCount = datasets.filter(ds => ds.seriesGroup === "power").length;
      const tCount = datasets.filter(ds => ds.seriesGroup === "temp").length;
      const noneHidden = datasets.every(ds => !hiddenSeries[ds.seriesKey]);
      const allHidden = datasets.every(ds => !!hiddenSeries[ds.seriesKey]);
      const onlyPower = pCount > 0 && datasets.every(ds => ds.seriesGroup === "power" ? !hiddenSeries[ds.seriesKey] : !!hiddenSeries[ds.seriesKey]);
      const onlyTemp = tCount > 0 && datasets.every(ds => ds.seriesGroup === "temp" ? !hiddenSeries[ds.seriesKey] : !!hiddenSeries[ds.seriesKey]);
      let btns = '<button type="button" class="filter-btn' + (noneHidden ? ' active' : '') + '" id="qfAll">🔘 Tous (' + total + ')</button>';
      if (activeGroup === "all" && pCount > 0 && pCount < total) {{
        btns += '<button type="button" class="filter-btn' + (onlyPower ? ' active' : '') + '" id="qfPower">⚡ Puissance (' + pCount + ')</button>';
      }}
      if (activeGroup === "all" && tCount > 0 && tCount < total) {{
        btns += '<button type="button" class="filter-btn' + (onlyTemp ? ' active' : '') + '" id="qfTemp">🌡️ Température (' + tCount + ')</button>';
      }}
      btns += '<button type="button" class="filter-btn' + (allHidden ? ' active' : '') + '" id="qfNone">👁️ Masquer tout</button>';
      filterBox.innerHTML = btns;
      document.getElementById("qfAll").onclick = () => {{
        hiddenSeries = {{}};
        applyLegendVisibility();
      }};
      if (document.getElementById("qfPower")) {{
        document.getElementById("qfPower").onclick = () => {{
          hiddenSeries = {{}};
          datasets.forEach(ds => {{ if (ds.seriesGroup !== "power") hiddenSeries[ds.seriesKey] = true; }});
          applyLegendVisibility();
        }};
      }}
      if (document.getElementById("qfTemp")) {{
        document.getElementById("qfTemp").onclick = () => {{
          hiddenSeries = {{}};
          datasets.forEach(ds => {{ if (ds.seriesGroup !== "temp") hiddenSeries[ds.seriesKey] = true; }});
          applyLegendVisibility();
        }};
      }}
      document.getElementById("qfNone").onclick = () => {{
        datasets.forEach(ds => {{ hiddenSeries[ds.seriesKey] = true; }});
        applyLegendVisibility();
      }};
    }} else {{
      filterBox.style.display = "none";
      filterBox.innerHTML = "";
    }}
  }}
}}

function resetChartZoom() {{
  const btn = document.getElementById("resetZoomBtn");
  if (btn) btn.style.display = "none";
  if (window.chartInstance && typeof window.chartInstance.resetZoom === "function") {{
    window.chartInstance.resetZoom();
  }}
  updateZoomStats();
}}

function renderChart() {{
  const res = results[activeBenchIdx];
  if (!res || !res.metrics || !res.metrics.series) return;
  const ser = res.metrics.series;
  const lbls = res.metrics.series_labels || {{}};
  const t = ser.t || [];

  const titleEl = document.getElementById("chartTitle");
  if (titleEl) {{
    titleEl.textContent = (res.name || "Courbes temporelles") + (activeGroup !== "all" ? " — " + getGroupLabel(activeGroup) : "");
  }}

  const datasets = [];
  let colorIdx = 0;
  for (const [k, v] of Object.entries(ser)) {{
    if (k === "t") continue;
    const g = getMetricGroup(k);
    if (activeGroup !== "all" && g !== activeGroup) continue;
    const label = lbls[k] || k;
    const col = COLORS[colorIdx % COLORS.length];
    colorIdx++;
    datasets.push({{
      label: label,
      seriesKey: k,
      seriesGroup: g,
      seriesUnit: getSeriesUnit(k, label),
      data: t.map((timeVal, idx) => ({{ x: timeVal, y: v[idx] }})),
      borderColor: col,
      backgroundColor: col,
      borderWidth: 2,
      pointRadius: 0,
      pointHoverRadius: 4,
      tension: 0.1,
      hidden: !!hiddenSeries[k],
    }});
  }}

  const canvas = document.getElementById("mainChart");
  const ctx = canvas.getContext("2d");
  if (window.chartInstance) {{
    window.chartInstance.destroy();
  }}
  if (typeof Chart === "undefined") {{
    canvas.parentElement.innerHTML = '<p class="muted">Chart.js non disponible (hors ligne sans script local) : tableaux utilisables, graphiques indisponibles.</p>';
    return;
  }}

  const isLight = currentTheme === "light";
  const gridColor = isLight ? "rgba(0,0,0,0.08)" : "rgba(255,255,255,0.08)";
  const textColor = isLight ? "#64748b" : "#94a3b8";
  let xMin0 = Infinity, xMax0 = -Infinity;
  for (let i = 0; i < t.length; i++) {{
    const tv = +t[i];
    if (Number.isFinite(tv)) {{
      if (tv < xMin0) xMin0 = tv;
      if (tv > xMax0) xMax0 = tv;
    }}
  }}
  const hasRange = Number.isFinite(xMin0) && Number.isFinite(xMax0) && xMax0 > xMin0;
  const safeMinX = hasRange ? xMin0 : (t.length ? +t[0] : 0);
  const safeMaxX = hasRange ? xMax0 : (t.length ? +t[t.length - 1] : undefined);
  const minZoomSpan = hasRange ? Math.max(0.05, Math.min(1, (safeMaxX - safeMinX) / 20)) : 0.1;

  function onViewportChanged({{ chart }}) {{
    let vMin = chart.scales.x.min;
    let vMax = chart.scales.x.max;
    if (Number.isFinite(safeMinX) && vMin < safeMinX) vMin = safeMinX;
    if (Number.isFinite(safeMaxX) && vMax > safeMaxX) vMax = safeMaxX;
    const atFull = hasRange && vMin <= safeMinX + 1e-6 && vMax >= safeMaxX - 1e-6;
    const btn = document.getElementById("resetZoomBtn");
    if (btn) btn.style.display = atFull ? "none" : "inline-block";
    if (atFull) {{
      updateZoomStats();
    }} else {{
      updateZoomStats(vMin, vMax);
    }}
  }}

  window.chartInstance = new Chart(ctx, {{
    type: "line",
    data: {{ datasets: datasets }},
    options: {{
      animation: false,
      responsive: true,
      maintainAspectRatio: false,
      interaction: {{ mode: "index", intersect: false }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            title: items => "Temps : " + (items[0] ? fmtDuration(items[0].parsed.x) : ""),
            label: item => {{
              const unit = item.dataset.seriesUnit || getGroupUnit(activeGroup);
              return item.dataset.label + " : " + fmtNum(item.parsed.y) + (unit ? " " + unit : "");
            }}
          }}
        }},
        zoom: {{
          limits: {{
            x: {{
              min: Number.isFinite(safeMinX) ? safeMinX : "original",
              max: Number.isFinite(safeMaxX) ? safeMaxX : "original",
              minRange: minZoomSpan
            }}
          }},
          pan: {{
            enabled: true,
            mode: "x",
            modifierKey: "shift",
            onPanComplete: onViewportChanged
          }},
          zoom: {{
            drag: {{
              enabled: true,
              threshold: 5,
              backgroundColor: "rgba(59, 130, 246, 0.18)",
              borderColor: "rgba(59, 130, 246, 0.55)",
              borderWidth: 1
            }},
            wheel: {{
              enabled: true,
              modifierKey: "ctrl"
            }},
            pinch: {{
              enabled: true
            }},
            mode: "x",
            onZoomStart: ({{ chart, point }}) => {{
              const a = chart.chartArea;
              if (!a || !point) return true;
              return point.x >= a.left && point.x <= a.right && point.y >= a.top && point.y <= a.bottom;
            }},
            onZoomComplete: onViewportChanged
          }}
        }}
      }},
      scales: {{
        x: {{
          type: "linear",
          min: safeMinX,
          max: safeMaxX,
          title: {{ display: true, text: "Temps (secondes)", color: textColor }},
          grid: {{ color: gridColor }},
          ticks: {{ color: textColor, maxTicksLimit: 12 }}
        }},
        y: {{
          title: {{ display: true, text: getGroupLabel(activeGroup), color: textColor }},
          grid: {{ color: gridColor }},
          ticks: {{ color: textColor }}
        }}
      }}
    }}
  }});

  canvas.ondblclick = () => resetChartZoom();
  const rBtn = document.getElementById("resetZoomBtn");
  if (rBtn) rBtn.style.display = "none";
  renderLegend();
  updateZoomStats();
}}

function updateChartColors() {{
  if (!window.chartInstance) return;
  const isLight = currentTheme === "light";
  const gridColor = isLight ? "rgba(0,0,0,0.08)" : "rgba(255,255,255,0.08)";
  const textColor = isLight ? "#64748b" : "#94a3b8";
  window.chartInstance.options.scales.x.grid.color = gridColor;
  window.chartInstance.options.scales.x.ticks.color = textColor;
  window.chartInstance.options.scales.x.title.color = textColor;
  window.chartInstance.options.scales.y.grid.color = gridColor;
  window.chartInstance.options.scales.y.ticks.color = textColor;
  window.chartInstance.options.scales.y.title.color = textColor;
  window.chartInstance.update();
}}

function renderChartToCanvas() {{
  const canvas = document.getElementById("mainChart");
  if (!canvas || !canvas.width) return null;
  const isLight = currentTheme === "light";
  const bg = isLight ? "#ffffff" : "#141414";
  const fg = isLight ? "#0f172a" : "#f0f0f0";
  const mu = isLight ? "#64748b" : "#94a3b8";
  const sc = canvas.width / Math.max(1, canvas.clientWidth || canvas.width);
  const pad = Math.round(20 * sc);
  const fT = Math.round(18 * sc);
  const fS = Math.round(12 * sc);
  const lineW = Math.round(24 * sc);
  const gap = Math.round(16 * sc);
  const rowH = Math.round(20 * sc);
  const title = (document.getElementById("chartTitle") || {{}}).textContent || "BenchSuite — Graphique";
  let sub = "";
  const rBtn = document.getElementById("resetZoomBtn");
  if (rBtn && rBtn.style.display !== "none" && window.chartInstance) {{
    const s = window.chartInstance.scales.x;
    sub = "Zone zoomée : " + fmtNum(s.min) + " s à " + fmtNum(s.max) + " s";
  }}
  const items = [];
  const legEl = document.getElementById("legendRow");
  if (legEl) {{
    legEl.querySelectorAll(".legend-item").forEach(el => {{
      if (el.classList.contains("muted")) return;
      const ln = el.querySelector(".legend-line");
      let col = "#ff6600";
      if (ln) {{
        col = ln.style.backgroundColor || getComputedStyle(ln).backgroundColor || "#ff6600";
      }}
      items.push({{ t: el.textContent.trim(), c: col }});
    }});
  }}
  const W = canvas.width + 2 * pad;
  const c2 = document.createElement("canvas");
  c2.width = W;
  c2.height = 100;
  const x = c2.getContext("2d");
  function layout(draw) {{
    let yy = pad;
    if (draw) {{ x.fillStyle = bg; x.fillRect(0, 0, c2.width, c2.height); }}
    x.textBaseline = "top";
    x.font = "700 " + fT + "px Inter, Arial, sans-serif";
    if (draw) {{ x.fillStyle = fg; x.fillText(title, pad, yy); }}
    yy += Math.round(fT * 1.45);
    if (sub) {{
      x.font = "400 " + fS + "px Inter, Arial, sans-serif";
      if (draw) {{ x.fillStyle = mu; x.fillText(sub, pad, yy); }}
      yy += Math.round(fS * 1.7);
    }}
    if (items.length) {{
      x.font = "600 " + fS + "px Inter, Arial, sans-serif";
      let xx = pad;
      items.forEach(it => {{
        const w = lineW + Math.round(6 * sc) + x.measureText(it.t).width;
        if (xx > pad && xx + w > W - pad) {{ xx = pad; yy += rowH; }}
        if (draw) {{
          x.fillStyle = it.c;
          x.fillRect(xx, yy + Math.round(fS / 2) - Math.round(1.5 * sc), lineW, Math.max(2, Math.round(3 * sc)));
          x.fillStyle = fg;
          x.fillText(it.t, xx + lineW + Math.round(6 * sc), yy);
        }}
        xx += w + gap;
      }});
      yy += rowH + Math.round(6 * sc);
    }}
    return yy + Math.round(6 * sc);
  }}
  const h0 = layout(false);
  c2.height = h0 + canvas.height + pad;
  layout(true);
  x.drawImage(canvas, pad, h0);
  return c2;
}}

function copyChartImage(btn) {{
  const c2 = renderChartToCanvas();
  if (!c2) return;
  const orig = btn ? btn.innerHTML : "";
  const done = msg => {{
    if (!btn) return;
    btn.innerHTML = msg;
    setTimeout(() => {{ btn.innerHTML = orig; }}, 2000);
  }};
  c2.toBlob(blob => {{
    if (!blob) {{ done("❌ Échec"); return; }}
    if (!(navigator.clipboard && window.ClipboardItem)) {{
      downloadChartPNG(btn);
      return;
    }}
    navigator.clipboard.write([new ClipboardItem({{ "image/png": blob }})]).then(
      () => done("✅ Copié (Ctrl+V)"),
      () => downloadChartPNG(btn)
    );
  }}, "image/png");
}}

function downloadChartPNG(btn) {{
  const c2 = renderChartToCanvas();
  if (!c2) return;
  const title = ((document.getElementById("chartTitle") || {{}}).textContent || "graphique_benchsuite").replace(/[^\\w\\-]+/g, "_").slice(0, 60);
  const a = document.createElement("a");
  a.href = c2.toDataURL("image/png");
  a.download = (title || "graphique_benchsuite") + ".png";
  document.body.appendChild(a);
  a.click();
  setTimeout(() => a.remove(), 0);
  if (btn) {{
    const orig = btn.innerHTML;
    btn.innerHTML = "💾 PNG téléchargé";
    setTimeout(() => {{ btn.innerHTML = orig; }}, 2000);
  }}
}}

function loadJsonTab() {{
  const pre = document.getElementById("jsonPre");
  if (pre && !pre.dataset.loaded) {{
    pre.textContent = JSON.stringify(payload, null, 2);
    pre.dataset.loaded = "1";
  }}
}}

function copyJson() {{
  const text = JSON.stringify(payload, null, 2);
  navigator.clipboard.writeText(text).then(() => alert("JSON copié dans le presse-papier !"));
}}

if (document.querySelector("#json.active")) {{
  loadJsonTab();
}}

if (results.length > 0) {{
  buildMetricSubtabs();
  renderChart();
}}
</script>
</body>
</html>"""

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    return html_path


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="CSV (tout type) → rapport HTML BenchSuite avec bouton thème clair / sombre.")
    ap.add_argument("csv_files", nargs="*", help="un ou plusieurs fichiers CSV (défaut : tous les *.csv du dossier courant)")
    ap.add_argument("-o", "--outdir", default=".", help="dossier de sortie (défaut : dossier courant)")
    ap.add_argument("-s", "--system", default="Import CSV", help="nom affiché dans le rapport")
    ap.add_argument("--noms", default="", help="noms des alimentations dans l'ordre des fichiers, séparés par des virgules (défaut : PSU 1, PSU 2…)")
    ap.add_argument("--theme", choices=["dark", "light", "auto"], help="thème à l'ouverture du rapport (le bouton en haut permet de changer ; défaut : sombre)")
    ap.add_argument("--max-points", type=int, default=1500,
                    help="nombre maximum de points par courbe affichée dans le HTML (défaut : 1500, 0 = aucune réduction). Les calculs d'énergie et statistiques restent calculés sur 100%% des données.")
    args = ap.parse_args(argv)
    if args.theme:
        os.environ["BENCHSUITE_THEME"] = args.theme

    # Gestion des jokers (*.csv) sous Windows cmd.exe et détection automatique si aucun argument fourni
    raw_files = args.csv_files
    if not raw_files:
        # Déduplication insensible à la casse (Windows NTFS retourne les mêmes fichiers pour *.csv et *.CSV)
        seen_norm = {}
        for p in sorted(glob.glob("*.csv") + glob.glob("*.CSV")):
            k = os.path.normcase(os.path.abspath(p))
            seen_norm.setdefault(k, p)
        found = sorted(seen_norm.values())
        if not found:
            print("✗ Aucun fichier CSV spécifié et aucun fichier *.csv trouvé dans le dossier courant.")
            print("\nUsage : python csv2html.py [fichier.csv ...] [-s 'Nom de la machine']")
            print("Astuce : Copiez csv2html.py dans un dossier contenant des .csv et lancez-le simplement !")
            return 1
        print(f"ℹ Détection automatique : {len(found)} fichier(s) CSV trouvé(s) dans le dossier courant.")
        raw_files = found

    files = []
    for item in raw_files:
        if any(c in item for c in ("*", "?", "[")):
            matched = sorted(glob.glob(item))
            if matched:
                files.extend(matched)
            else:
                print(f"✗ Aucun fichier ne correspond au motif : {item}")
        else:
            files.append(item)

    names = [x for x in (args.noms or "").split(",")]
    results, combined, labels, longest = [], {}, {}, 0.0
    for f in files:
        if not os.path.isfile(f):
            print(f"✗ fichier introuvable : {f}")
            continue
        res = parse_csv(f)
        if res is None:
            continue
        results.append(res)
        stem = os.path.splitext(os.path.basename(f))[0]
        if res.category.startswith("Alimentation"):
            k = sum(r.category.startswith("Alimentation") for r in results) - 1
            label = names[k].strip() if k < len(names) and names[k].strip() else f"PSU {k + 1}"
            res.name = res.name.replace("Alimentation", label, 1)
            stem = label
        ser = res.metrics["series"]
        for k, v in ser.items():
            if k == "t":
                continue
            ck = f"{k.split('_c')[0]}_f{len(results)}c{k.split('_c')[1]}"      # préfixe d'onglet conservé, unique par fichier
            combined[ck] = v
            sub_lbl = res.metrics['series_labels'][k]
            if stem.lower().startswith(sub_lbl.lower()) or sub_lbl.lower().startswith(stem.lower()):
                labels[ck] = sub_lbl
            else:
                labels[ck] = f"{stem} — {sub_lbl}"
        if res.duration_s >= longest:
            longest, t_longest = res.duration_s, ser["t"]
    if not results:
        print("Aucun fichier exploitable.")
        return 1
    if len(results) > 1:        # plusieurs CSV : un résultat superposant tous les fichiers (axe = le plus long)
        combined["t"] = t_longest
        comb_psu = psu_metrics(labels, combined, t_longest)
        comb_cat = "Alimentation & Redondance" if comb_psu else "Import CSV"
        comb_score = comb_psu.get("pin_max_w") if comb_psu else None
        comb_unit = "W" if comb_psu else ""
        comb_metrics = {"series": combined, "series_labels": labels}
        if comb_psu:
            comb_metrics.update(comb_psu)
            comb_name = "Alimentation — Synthèse globale"
        else:
            comb_name = "Comparaison superposée (tous les CSV)"
        results.insert(0, BenchmarkResult(name=comb_name, category=comb_cat, score=comb_score, unit=comb_unit,
                                          duration_s=longest, raw_output="Vue combinée de tous les fichiers",
                                          metrics=comb_metrics, status="ok"))

    # Calcul des statistiques détaillées sur 100 % des données brutes
    for r in results:
        _compute_detailed_stats(r)

    # Réduction intelligente des séries pour l'affichage (HTML / Chart.js fluide)
    max_pts = args.max_points
    os.environ["BENCHSUITE_HTML_MAX_POINTS"] = str(max_pts)
    if max_pts > 0:
        total_raw = 0
        total_dec = 0
        for r in results:
            _, raw_n, dec_n = slim_result_series(r, max_pts)
            total_raw = max(total_raw, raw_n)
            total_dec = max(total_dec, dec_n)
        if total_raw > total_dec:
            print(f"\n⚡ Optimisation d'affichage activée : max {max_pts} points par courbe conservés (sur {total_raw:,} échantillons bruts).".replace(",", " "))
            print("   ↳ Tous les calculs (min, max, moyennes, intégrales d'énergie kWh, rendement) sont 100 % exacts (faits sur les données brutes).")

    units = [r for r in results if r.category.startswith("Alimentation")]
    try:
        html_path = export_html_standalone(results, args.outdir, {"system_name": args.system, "machine": args.system}, units or None)
    except Exception as e:
        print(f"❌ Erreur lors de la création du HTML : {e}")
        return 1

    print(f"\n✅ Rapport créé : {html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
