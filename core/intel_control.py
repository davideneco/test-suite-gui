"""
intel_control.py — Lecture et réglage d'un GPU Intel (pilote xe) : équivalent « nvidia-smi » de la page GPU Intel.

Lecture sans privilège (sysfs / hwmon du pilote xe) :
  • puissance : limite soutenue PL1 (power1_max), limite de pointe PL2 (power1_cap), TDP par défaut (power1_rated_max), fenêtres
    (power1_max_interval / power1_cap_interval), limite critique I1, compteur d'énergie (puissance = delta / delta t) ;
  • températures (pkg = cœur, vram, mctrl, pcie, canaux VRAM) avec leurs seuils, ventilateurs ;
  • fréquences (tile*/gt*/freq0 : cur / act / min / max / rp0 / rpn) et raisons de bridage (freq0/throttle) ;
  • lien PCIe (pont amont pour les cartes à switch interne).

Réglages (élévation à la demande : root direct, sinon sudo -n / pkexec) :
  • PL1, PL2 (µW dans sysfs) ; plage de fréquences min / max ; retour aux valeurs d'origine ;
  • repli sur `xpu-smi config` quand le fichier sysfs n'existe pas.
Garde-fous : jamais d'écriture en mode lecture seule (BENCHSUITE_READ_ONLY) ; valeurs bornées (PL1 ≤ TDP par défaut, PL2 ≥ PL1, fréquences
entre RPn et RP0) ; relecture après écriture. Les réglages ne survivent pas à un redémarrage. Tout est best-effort, sans exception vers l'appelant.
Non vérifié sur matériel réel côté développement : seule la lecture a été observée sur une Arc Pro B70.
"""

import glob
import json
import os
import re
import shutil
import time
from typing import Dict, List, Optional, Tuple

DEFAULTS_FILE = os.path.expanduser("~/.benchsuite/intel_defaults.json")
SYSFS_ROOT = "/sys"        # modifiable pour les tests (faux arbre sysfs)

MIN_PL_RATIO = 0.25        # garde-fou BenchSuite (pas une spécification Intel) : PL1 ≥ 25 % du TDP par défaut
MAX_PL2_RATIO = 2.0        # garde-fou BenchSuite : PL2 ≤ 2 × TDP par défaut


# ─────────────────────────────────────────────────────────────────────────────
# Lecture
# ─────────────────────────────────────────────────────────────────────────────

def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read().strip()
    except Exception:
        return ""


def _num(path: str) -> Optional[float]:
    v = _read(path)
    try:
        return float(v.split()[0])
    except (ValueError, IndexError):
        return None


def find_intel_gpus() -> List[Dict]:
    """GPU Intel de la machine : [{'idx', 'bdf', 'name', 'xpu_id'}] (puces graphiques de BMC exclues). [] si aucun."""
    out = []
    try:
        from core.system_info import get_gpu_info, _normalize_pci_bus
        for i, g in enumerate(get_gpu_info()):
            if g.get("fabricant") == "Intel" and g.get("pci_bus"):
                out.append({"idx": i, "bdf": _normalize_pci_bus(g["pci_bus"]), "name": g.get("nom") or g.get("modele") or f"GPU {i}",
                            "xpu_id": g.get("xpu_device_id", "")})
    except Exception:
        pass
    return out


def _devdir(bdf: str, root: str = None) -> str:
    return os.path.join(root or SYSFS_ROOT, "bus", "pci", "devices", bdf)


def hwmon_dir(bdf: str, root: str = None) -> str:
    """Dossier hwmon du pilote xe pour ce GPU ('' si absent)."""
    base = os.path.join(_devdir(bdf, root), "hwmon")
    try:
        for n in sorted(os.listdir(base)):
            if n.startswith("hwmon"):
                return os.path.join(base, n)
    except Exception:
        pass
    return ""


def freq_dirs(bdf: str, root: str = None) -> List[str]:
    """Dossiers de fréquences (un par tuile / GT) : tile*/gt*/freq0, triés."""
    return sorted(glob.glob(os.path.join(_devdir(bdf, root), "tile*", "gt*", "freq0")))


def _temps(hw: str) -> Dict[str, Dict]:
    """Capteurs de température du hwmon : {étiquette: {'val', 'max', 'crit', 'emerg'}} (°C)."""
    res: Dict[str, Dict] = {}
    if not hw:
        return res
    try:
        names = sorted(os.listdir(hw))
    except Exception:
        return res
    for n in names:
        m = re.match(r"temp(\d+)_input$", n)
        if not m:
            continue
        k = m.group(1)
        label = _read(os.path.join(hw, f"temp{k}_label")).lower() or f"temp{k}"
        v = _num(os.path.join(hw, n))
        if v is None:
            continue
        ent = {"val": v / 1000.0}
        for key, suf in (("max", "max"), ("crit", "crit"), ("emerg", "emergency")):
            x = _num(os.path.join(hw, f"temp{k}_{suf}"))
            if x:
                ent[key] = x / 1000.0
        res[label] = ent
    return res


def read_state(bdf: str, root: str = None, with_pcie: bool = True) -> Dict:
    """État complet d'un GPU Intel (voir l'en-tête du module). Les clés absentes sont omises. Ne lève jamais d'exception."""
    st: Dict = {"bdf": bdf, "t": time.time()}
    try:
        hw = hwmon_dir(bdf, root)
        st["hwmon"] = hw
        if hw:
            for key, fn, div in (("pl1_w", "power1_max", 1e6), ("pl2_w", "power1_cap", 1e6), ("rated_w", "power1_rated_max", 1e6),
                                 ("crit_w", "power1_crit", 1e6), ("tau1_ms", "power1_max_interval", 1.0), ("tau2_ms", "power1_cap_interval", 1.0),
                                 ("energy_uj", "energy1_input", 1.0), ("pkg_energy_uj", "energy2_input", 1.0)):
                v = _num(os.path.join(hw, fn))
                if v is not None:
                    st[key] = v / div
            temps = _temps(hw)
            st["temps"] = temps
            ch = [v["val"] for k, v in temps.items() if k.startswith("vram_ch")]
            if ch:
                st["vram_ch_max_c"] = max(ch)
            fans = [_num(os.path.join(hw, f"fan{i}_input")) for i in (1, 2, 3)]
            fans = [f for f in fans if f is not None]
            if fans:
                st["fan_rpm"] = fans
        fr = freq_dirs(bdf, root)
        st["freq"] = []
        for d in fr:
            ent = {"dir": d, "tile_gt": "/".join(d.split(os.sep)[-3:-1])}
            for key, fn in (("cur", "cur_freq"), ("act", "act_freq"), ("min", "min_freq"), ("max", "max_freq"),
                            ("rp0", "rp0_freq"), ("rpe", "rpe_freq"), ("rpn", "rpn_freq")):
                v = _num(os.path.join(d, fn))
                if v is not None:
                    ent[key] = v
            th = os.path.join(d, "throttle")
            if os.path.isdir(th):
                reasons = []
                try:
                    for n in sorted(os.listdir(th)):
                        if n.startswith("reason_") and _read(os.path.join(th, n)) in ("1", "true", "yes"):
                            reasons.append(n[len("reason_"):])
                except Exception:
                    pass
                ent["throttle_status"] = _read(os.path.join(th, "status"))
                ent["throttle_reasons"] = reasons
            st["freq"].append(ent)
        st["devid"] = f"{_read(os.path.join(_devdir(bdf, root), 'vendor'))}:{_read(os.path.join(_devdir(bdf, root), 'device'))}"
        if with_pcie:                           # appel lspci éventuel : une seule fois (la page réutilise la valeur)
            try:
                from core.system_info import _get_pcie_info
                st["pcie"] = _get_pcie_info(bdf)
            except Exception:
                pass
    except Exception as e:                      # jamais de traceback vers l'interface
        st["erreur"] = str(e)
    return st


def power_from_energy(prev: Optional[Dict], cur: Dict) -> Optional[float]:
    """Puissance moyenne (W) entre deux relevés `read_state` d'après le compteur d'énergie de la carte ; None si indisponible."""
    try:
        if not prev or "energy_uj" not in prev or "energy_uj" not in cur:
            return None
        dt = cur["t"] - prev["t"]
        de = cur["energy_uj"] - prev["energy_uj"]
        if dt <= 0.2 or de < 0:
            return None
        return de / 1e6 / dt
    except Exception:
        return None


def xpu_smi_available() -> bool:
    return bool(shutil.which("xpu-smi"))


# ─────────────────────────────────────────────────────────────────────────────
# Valeurs d'origine (pour « réinitialiser »)
# ─────────────────────────────────────────────────────────────────────────────

def _load_defaults() -> Dict:
    try:
        with open(DEFAULTS_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception:
        try:                                   # fichier corrompu : sauvegardé à part au lieu d'être écrasé en silence
            os.replace(DEFAULTS_FILE, DEFAULTS_FILE + ".bak")
        except OSError:
            pass
        return {}


def remember_defaults(st: Dict) -> None:
    """Mémorise une seule fois par GPU (adresse PCI + identifiant du périphérique) les réglages observés avant tout changement fait
    par BenchSuite : PL1, PL2 (0 = limite désactivée), TDP, fenêtres, plage de fréquences de chaque GT. Une carte remplacée dans le
    même emplacement (autre identifiant) remplace l'ancienne entrée."""
    try:
        d = _load_defaults()
        key = st.get("bdf", "")
        if not key or (key in d and d[key].get("devid") == st.get("devid")):
            return
        ent = {k: st[k] for k in ("pl1_w", "pl2_w", "rated_w", "tau1_ms", "tau2_ms") if k in st}
        ent["devid"] = st.get("devid", "")
        ent["freq"] = {f["tile_gt"]: [f.get("min"), f.get("max")] for f in st.get("freq", []) if f.get("min") is not None}
        d[key] = ent
        os.makedirs(os.path.dirname(DEFAULTS_FILE), exist_ok=True)
        with open(DEFAULTS_FILE, "w", encoding="utf-8") as f:
            json.dump(d, f, indent=1)
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Écriture : tout est validé AVANT la moindre écriture, puis exécuté en une seule élévation
# ─────────────────────────────────────────────────────────────────────────────

_POWER_FILES = ("power1_max", "power1_cap")
_FREQ_FILES = ("min_freq", "max_freq")


def _safe_sysfs_path(path: str) -> bool:
    """Seuls les fichiers de réglage connus du GPU sont écrits : chemin canonique sous /sys (ou la racine de test), nom dans une liste
    blanche et dossier parent cohérent (hwmon* pour la puissance, freq0 pour les fréquences). Aucun chemin arbitraire."""
    try:
        real = os.path.realpath(path)
        base, parent = os.path.basename(real), os.path.basename(os.path.dirname(real))
        if not real.startswith(os.path.realpath(SYSFS_ROOT) + os.sep):
            return False
        return (base in _POWER_FILES and parent.startswith("hwmon")) or (base in _FREQ_FILES and parent == "freq0")
    except Exception:
        return False


def _guard() -> Optional[str]:
    from core.safety import system_changes_allowed, MSG_PRESERVATION
    return None if system_changes_allowed() else MSG_PRESERVATION


def _write_many(items: List[Tuple[str, str]]) -> Tuple[bool, str]:
    """Écrit [(chemin, valeur)] dans l'ordre. Directement si tous les fichiers sont inscriptibles, sinon UNE seule commande élevée
    (une seule demande d'authentification, sans seconde tentative par pkexec si sudo a déjà répondu). S'arrête à la 1re erreur."""
    for path, value in items:
        if not _safe_sysfs_path(path) or not re.fullmatch(r"\d{1,12}", value):
            return False, f"écriture refusée ({os.path.basename(path)} = {value})"
    if all(os.access(p, os.W_OK) for p, _ in items):
        for path, value in items:
            try:
                with open(path, "w") as f:
                    f.write(value)
            except OSError as e:
                return False, f"{os.path.basename(path)} : {e}"
        return True, ""
    try:
        from core.privileged import run_elevated
        flat = [x for pair in items for x in (pair[1], pair[0])]
        script = 'while [ $# -ge 2 ]; do printf "%s" "$1" > "$2" || { echo "échec : $2" >&2; exit 1; }; shift 2; done'
        res = run_elevated(["sh", "-c", script, "sh"] + flat, timeout=60, retry_on_failure=False)
        if res.returncode == 0:
            return True, ""
        return False, ((res.stderr or res.stdout or "").strip() or f"code {res.returncode}")[:240]
    except Exception as e:
        return False, str(e)[:240]


def _xpu_config(args: List[str]) -> Tuple[bool, str]:
    """`xpu-smi config …` avec élévation (repli quand le fichier sysfs n'existe pas)."""
    if not xpu_smi_available():
        return False, "ni fichier sysfs de réglage ni xpu-smi disponibles pour ce GPU"
    try:
        from core.privileged import run_elevated
        res = run_elevated(["xpu-smi", "config"] + args, timeout=60, retry_on_failure=False)
        txt = ((res.stdout or "") + (res.stderr or "")).strip()
        if res.returncode == 0 and "fail" not in txt.lower() and "error" not in txt.lower():
            return True, txt[:200]
        return False, (txt or f"code {res.returncode}")[:200]
    except Exception as e:
        return False, str(e)[:200]


def plan_changes(st: Dict, pl1: float = None, pl2: float = None, fmin: float = None, fmax: float = None,
                 restore: bool = False) -> Tuple[Optional[List[Tuple[str, str, str]]], str]:
    """Valide les réglages demandés par rapport à l'état `st` et retourne ([(chemin, valeur, libellé)], '') dans un ordre sûr,
    ou (None, message) si UNE SEULE valeur est refusée (rien n'est alors écrit). `restore=True` : retour aux valeurs d'origine
    (autorise PL2 = 0 = limite désactivée si c'était l'état d'origine). Fréquences : bornées pour chaque GT à ses propres RPn / RP0."""
    hw = st.get("hwmon", "")
    rated, cur1, cur2 = st.get("rated_w"), st.get("pl1_w"), st.get("pl2_w")
    items: List[Tuple[str, str, str]] = []
    if pl1 is not None or pl2 is not None:
        n1 = pl1 if pl1 is not None else cur1
        n2 = pl2 if pl2 is not None else cur2
        for val, name in ((pl1, "PL1"), (pl2, "PL2")):
            if val is not None and val <= 0 and not (restore and name == "PL2" and val == 0):
                return None, f"{name} : valeur invalide (0 désactiverait la limite ; utilisez « Valeurs d'origine »)"
        unlimited2 = restore and pl2 == 0
        if pl1 is not None:
            if hw == "" or cur1 is None:
                return None, "PL1 : fichier hwmon power1_max absent (noyau < 6.5 ou pilote sans ce réglage)"
            top = rated or max((x for x in (cur1, cur2) if x), default=None)
            if not top:
                return None, "PL1 : maximum inconnu (TDP non exposé et limite actuelle désactivée) ; réglage refusé"
            if pl1 > top + 0.5:
                return None, f"PL1 limitée à {top:.0f} W ({'TDP par défaut' if rated else 'valeur maximale actuelle'})"
            if rated and pl1 < rated * MIN_PL_RATIO and not restore:
                return None, f"PL1 trop basse (garde-fou BenchSuite : ≥ {rated * MIN_PL_RATIO:.0f} W soit {int(MIN_PL_RATIO * 100)} % du TDP)"
        if pl2 is not None:
            if hw == "" or cur2 is None:
                return None, "PL2 : fichier hwmon power1_cap absent (noyau < 6.15 ou pilote sans ce réglage)"
            if not unlimited2 and not restore:
                base2 = rated or max((x for x in (cur1, cur2) if x), default=None)
                if not base2:
                    return None, "PL2 : maximum inconnu (TDP non exposé et limites actuelles désactivées) ; réglage refusé"
                top2 = base2 * MAX_PL2_RATIO
                if pl2 > top2 + 0.5:
                    return None, f"PL2 trop haute (garde-fou BenchSuite : ≤ {top2:.0f} W)"
        if n1 is not None and n2 not in (None, 0) and not unlimited2 and n1 > n2 + 0.5:
            return None, f"PL1 ({n1:.0f} W) ne peut pas dépasser PL2 ({n2:.0f} W)"
        p1 = ("PL1 (soutenue)", os.path.join(hw, "power1_max"), pl1)
        p2 = ("PL2 (pointe)", os.path.join(hw, "power1_cap"), pl2)
        # ordre : on ne passe jamais par un état PL1 > PL2 (PL2 d'abord si elle reste ≥ la PL1 actuelle, sinon PL1 d'abord)
        if unlimited2:
            raise_pl2_first = True
        elif pl1 is None or pl2 is None:
            raise_pl2_first = pl2 is not None and (cur2 in (None, 0) or pl2 >= cur2)
        else:
            raise_pl2_first = cur1 is None or pl2 >= cur1
        for lab, path, val in ((p2, p1) if raise_pl2_first else (p1, p2)):
            if val is not None:
                items.append((path, str(int(round(val * 1e6))), f"{lab} = {'désactivée' if val == 0 else '%.0f W' % val}"))
    if fmin is not None and fmax is not None:
        if fmin > fmax:
            return None, "fréquence min supérieure à la fréquence max"
        gts = st.get("freq") or []
        if not gts:
            return None, "dossier de fréquences (freq0) absent pour ce GPU"
        for ent in gts:
            lo, hi = ent.get("rpn"), ent.get("rp0")
            a = max(fmin, lo) if lo else fmin                 # chaque GT est bornée à SES limites matérielles
            b = min(fmax, hi) if hi else fmax
            if a > b:
                return None, f"{ent['tile_gt']} : plage {fmin:.0f}–{fmax:.0f} MHz incompatible avec ses limites matérielles ({lo or '?'}–{hi or '?'} MHz)"
            order = [("max_freq", b), ("min_freq", a)] if b >= (ent.get("max") or 0) else [("min_freq", a), ("max_freq", b)]
            for fn, val in order:
                items.append((os.path.join(ent["dir"], fn), str(int(val)), f"{ent['tile_gt']} {fn.replace('_freq', '')} = {int(val)} MHz"))
    return items, ""


def apply_changes(bdf: str, pl1: float = None, pl2: float = None, fmin: float = None, fmax: float = None,
                  root: str = None, xpu_id: str = "", restore: bool = False) -> Tuple[bool, str]:
    """Valide puis applique les réglages demandés (seuls ceux non None) en une seule élévation, relit et vérifie. (succès, message)."""
    g = _guard()
    if g:
        return False, g
    st = read_state(bdf, root, with_pcie=False)
    items, err = plan_changes(st, pl1, pl2, fmin, fmax, restore)
    if items is None:
        # repli xpu-smi pour PL1 / fréquences si les fichiers sysfs n'existent pas
        if xpu_id != "" and not st.get("hwmon") and (pl1 is not None or fmin is not None):
            msgs, ok_all = [], True
            if pl1 is not None:
                ok, m = _xpu_config(["-d", str(xpu_id), "--powerlimit", str(int(round(pl1)))])
                ok_all &= ok; msgs.append(f"PL1 {pl1:.0f} W via xpu-smi" if ok else m)
            if ok_all and fmin is not None and fmax is not None:
                ok, m = _xpu_config(["-d", str(xpu_id), "-t", "0", "--frequencyrange", f"{int(fmin)},{int(fmax)}"])
                ok_all &= ok; msgs.append(f"plage {int(fmin)}–{int(fmax)} MHz via xpu-smi" if ok else m)
            return ok_all, " ; ".join(msgs)
        return False, err
    if not items:
        return True, "aucun changement à appliquer"
    ok, msg = _write_many([(p, v) for p, v, _ in items])
    after = read_state(bdf, root, with_pcie=False)
    # vérification par relecture
    bad = []
    if pl1 is not None and after.get("pl1_w") is not None and abs(after["pl1_w"] - pl1) > 1.0:
        bad.append(f"PL1 relue à {after['pl1_w']:.0f} W")
    if pl2 is not None and after.get("pl2_w") is not None and abs(after["pl2_w"] - pl2) > 1.0:
        bad.append(f"PL2 relue à {after['pl2_w']:.0f} W")
    if not ok:
        return False, f"{msg} — état actuel : PL1 {_f(after.get('pl1_w'))}, PL2 {_f(after.get('pl2_w'))}, " \
                      f"fréquences {', '.join(_fr(e) for e in after.get('freq', [])) or '—'} (appliqué partiellement ?)"
    if bad:
        return False, "valeur écrite mais corrigée par le firmware : " + ", ".join(bad)
    return True, "appliqué : " + " ; ".join(lab for _, _, lab in items)


def _f(v) -> str:
    return "—" if v is None else f"{v:.0f} W"


def _fr(e: Dict) -> str:
    if e.get("min") is None or e.get("max") is None:
        return str(e.get("tile_gt", "?"))
    return f"{e.get('tile_gt', '?')} {e['min']:.0f}–{e['max']:.0f} MHz"


def set_power_limit(bdf: str, which: str, watts: float, root: str = None, xpu_id: str = "") -> Tuple[bool, str]:
    """Règle PL1 (`which`='pl1', soutenue) ou PL2 (`which`='pl2', pointe) en W."""
    if which not in ("pl1", "pl2"):
        return False, "limite inconnue"
    return apply_changes(bdf, pl1=watts if which == "pl1" else None, pl2=watts if which == "pl2" else None, root=root, xpu_id=xpu_id)


def set_freq_range(bdf: str, fmin: float, fmax: float, root: str = None, xpu_id: str = "") -> Tuple[bool, str]:
    """Règle la plage de fréquences (MHz) de toutes les GT du GPU, chacune bornée à ses propres limites matérielles."""
    return apply_changes(bdf, fmin=fmin, fmax=fmax, root=root, xpu_id=xpu_id)


def reset_defaults(bdf: str, root: str = None, xpu_id: str = "") -> Tuple[bool, str]:
    """Retour aux valeurs d'origine mémorisées à la première ouverture : PL1 et PL2 d'origine (PL1 = TDP par défaut à défaut de
    mémoire), fréquences de chaque GT à leurs minimum / maximum matériels (RPn / RP0)."""
    g = _guard()
    if g:
        return False, g
    st = read_state(bdf, root, with_pcie=False)
    saved = _load_defaults().get(bdf, {})
    if not isinstance(saved, dict):
        saved = {}
    pl1 = saved.get("pl1_w") if saved.get("pl1_w") else st.get("rated_w")
    pl2 = saved.get("pl2_w") if "pl2_w" in saved else None
    fr = st.get("freq") or []
    lo = min((e["rpn"] for e in fr if e.get("rpn")), default=None)
    hi = max((e["rp0"] for e in fr if e.get("rp0")), default=None)
    if st.get("pl1_w") is None:
        pl1 = None
    if st.get("pl2_w") is None:
        pl2 = None
    if pl1 is None and pl2 is None and not (lo and hi):
        return False, "rien à réinitialiser (aucun réglage lisible pour ce GPU)"
    return apply_changes(bdf, pl1=pl1, pl2=pl2, fmin=lo, fmax=hi, root=root, xpu_id=xpu_id, restore=True)


def equivalent_commands(st: Dict, pl1: float = None, pl2: float = None, fmin: float = None, fmax: float = None, xpu_id: str = "") -> List[str]:
    """Commandes équivalentes (à copier) pour les réglages demandés : sysfs et xpu-smi."""
    out = []
    hw = st.get("hwmon", "")
    if hw and pl2:
        out.append(f"echo {int(round(pl2 * 1e6))} | sudo tee {hw}/power1_cap        # PL2 (pointe), µW")
    if hw and pl1:
        out.append(f"echo {int(round(pl1 * 1e6))} | sudo tee {hw}/power1_max        # PL1 (soutenue), µW")
    for ent in st.get("freq") or []:
        if fmin and fmax:
            out.append(f"echo {int(fmax)} | sudo tee {ent['dir']}/max_freq ; echo {int(fmin)} | sudo tee {ent['dir']}/min_freq")
            break
    if xpu_id != "":
        if pl1:
            out.append(f"sudo xpu-smi config -d {xpu_id} --powerlimit {int(round(pl1))}")
        if fmin and fmax:
            out.append(f"sudo xpu-smi config -d {xpu_id} -t 0 --frequencyrange {int(fmin)},{int(fmax)}")
    return out
