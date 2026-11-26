"""Pont entre la nouvelle interface et le moteur de `cli.py` : arguments par défaut, identité du rapport, lancement, résumé de session."""
import glob
import json
import os
import time
from typing import Dict, List, Optional

IDENTITY_FILE = os.path.expanduser("~/.benchsuite/cli_identity.json")
IDENTITY_FIELDS = [("sysname", "Nom de la machine"), ("client", "Client"), ("operator", "Opérateur"), ("order", "N° de commande"),
                   ("serial", "N° de série"), ("ambient", "Température ambiante (°C)"), ("fan", "Ventilation / remarque")]


def load_identity() -> Dict[str, str]:
    """Identité saisie pour le rapport (mémorisée entre deux lancements)."""
    try:
        with open(IDENTITY_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return {k: str(d.get(k, "") or "") for k, _ in IDENTITY_FIELDS}
    except Exception:
        return {k: "" for k, _ in IDENTITY_FIELDS}


def save_identity(d: Dict[str, str]) -> None:
    try:
        os.makedirs(os.path.dirname(IDENTITY_FILE), exist_ok=True)
        with open(IDENTITY_FILE, "w", encoding="utf-8") as f:
            json.dump({k: d.get(k, "") for k, _ in IDENTITY_FIELDS}, f, ensure_ascii=False, indent=1)
    except Exception:
        pass


def make_args(cli, parsed=None, **over):
    """Namespace complet attendu par cli.run_campaign : valeurs par défaut du parseur historique + identité mémorisée + surcharges."""
    ns = parsed if parsed is not None else cli.build_parser().parse_args([])
    for k, v in load_identity().items():
        if v and not getattr(ns, k, None):
            setattr(ns, k, v)
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


def latest_session(output_dir: str) -> Optional[str]:
    """Dossier de la session (démarrage courant de la machine) la plus récente sous `output_dir`, None sinon."""
    from core import cli_session as cs
    best, best_t = None, 0.0
    for f in glob.glob(os.path.join(output_dir, "**", "session_*", cs.SESSION_FILE), recursive=True):
        try:
            if os.path.getmtime(f) > best_t and cs.load(os.path.dirname(f)):
                best, best_t = os.path.dirname(f), os.path.getmtime(f)
        except Exception:
            continue
    return best


def _num(v: float) -> str:
    """Nombre lisible sans notation scientifique : 16690 → « 16 690 », 12.3456 → « 12.35 », 0.0123 → « 0.0123 »."""
    a = abs(v)
    if a >= 100:
        return f"{v:,.0f}".replace(",", " ")
    if a >= 1:
        return f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{v:.3g}"


def result_rows(results: List) -> List[List[str]]:
    """Lignes [#, test, statut, durée, score, repères] d'un tableau de résultats (températures, puissance, fréquence si présentes)."""
    rows = []
    for i, r in enumerate(results, 1):
        m = r.metrics or {}
        marks = []
        for key, fmt in (("cpu0_temp_max_c", "{:.0f} °C"), ("cpu0_power_moy_w", "{:.0f} W"), ("cpu0_freq_moy_mhz", "{:.0f} MHz")):
            if isinstance(m.get(key), (int, float)):
                marks.append(fmt.format(m[key]))
        score = f"{_num(r.score)} {r.unit}" if isinstance(r.score, (int, float)) else "—"
        st = {"ok": "OK", "error": "ÉCHEC", "stopped": "arrêté", "skipped": "ignoré", "interrupted": "interrompu"}.get(r.status, r.status)
        rows.append([str(i), (r.name or "")[:60], st, f"{r.duration_s:.0f} s", score, " · ".join(marks)])
    return rows


def print_table(rows: List[List[str]], head: List[str], color=None) -> None:
    """Tableau texte aligné (sans dépendance)."""
    w = [max(len(h), *(len(r[i]) for r in rows)) if rows else len(h) for i, h in enumerate(head)]
    print("  " + "  ".join(h.ljust(w[i]) for i, h in enumerate(head)))
    print("  " + "  ".join("─" * w[i] for i in range(len(head))))
    for r in rows:
        line = "  " + "  ".join(c.ljust(w[i]) for i, c in enumerate(r))
        if color and len(r) > 2:
            col = {"OK": color.GREEN, "ÉCHEC": color.RED}.get(r[2], color.YELLOW)
            line = f"{col}{line}{color.RESET}"
        print(line)


def session_summary(cli, output_dir: str) -> None:
    """Résumé de la session en cours : tableau de tous les résultats depuis le démarrage + emplacement du rapport."""
    from core import cli_session as cs
    T = cli.TermColor
    sd = latest_session(output_dir)
    if not sd:
        print(f"\n  {T.DIM}Aucun résultat enregistré pour ce démarrage de la machine.{T.RESET}")
        return
    results = cs.results_of(cs.load(sd))
    print(f"\n{T.BOLD}{T.CYAN}▕ Résultats depuis le démarrage de la machine ({len(results)}){T.RESET}")
    print_table(result_rows(results), ["#", "Test", "Statut", "Durée", "Score", "Repères"], T)
    files = [e for e in ("html", "pdf", "json", "csv") if os.path.exists(os.path.join(sd, f"benchsuite_report.{e}"))]
    print(f"\n  Rapport : {os.path.join(sd, 'benchsuite_report.' + (files[0] if files else 'html'))}   {T.DIM}(fichiers : {', '.join(files) or 'aucun'}){T.RESET}")


def launch(cli, items, parsed=None, **over) -> int:
    """Lance un ou plusieurs éléments (étapes du moteur) ; affiche en-tête compact, suivi, verdict et résumé de session."""
    T = cli.TermColor
    action = [i for i in items if i.kind == "action"]
    if action:
        ref = action[0].ref
        if ref == "ports":
            return show_ports(cli)
        if ref == "deps":
            from core.install_advisor import print_install_guide
            return print_install_guide()
        return serve_iperf(cli, action[0])
    if items and items[0].kind == "suite":
        return launch_suite(cli, items[0], parsed, **over)
    errs = [(i, i.validate()) for i in items]
    errs = [(i, e) for i, e in errs if e]
    if errs:
        for i, e in errs:
            print(f"{T.RED}✗ {i.label} : {e}{T.RESET}")
            for tool in i.missing_tools():
                from clitui.model import install_commands
                for lab, cmd in install_commands(tool):
                    print(f"    {T.DIM}{lab} :{T.RESET} {cmd}")
        return cli.EXIT_USAGE
    args = make_args(cli, parsed, **over)
    steps = [i.step() for i in items]
    label = items[0].label if len(items) == 1 else f"{len(items)} tests"
    print(f"\n{T.BOLD}{T.rgb(249, 115, 22)}⚡ BenchSuite{T.RESET}  {T.BOLD}{label}{T.RESET}")
    rc = cli.run_campaign(steps, args, items[0].key if len(items) == 1 else "manuel", label)
    session_summary(cli, args.output_dir)
    return rc


def launch_suite(cli, item, parsed=None, **over) -> int:
    """Lance une séquence automatique (suites/*.json) : même moteur que `--suite`, avec le disque de données et la pause choisis."""
    T = cli.TermColor
    err = item.validate()
    if err:
        print(f"{T.RED}✗ {item.label} : {err}{T.RESET}")
        return cli.EXIT_USAGE
    from core.runner import load_suite
    try:
        suite = load_suite(item.ref)
    except (ValueError, OSError) as exc:
        print(f"{T.RED}✗ {exc}{T.RESET}")
        return cli.EXIT_USAGE
    args = make_args(cli, parsed, **over)
    fp = str(item.get("fio_path") or "").strip()
    if fp:
        args.fio_path = fp
    cd = item.get("cooldown")
    cd = float(cd) if isinstance(cd, int) else float(suite.get("cooldown_s", 0) or 0)
    if getattr(args, "cooldown", None) is not None:
        cd = float(args.cooldown)
    print(f"\n{T.BOLD}{T.rgb(249, 115, 22)}⚡ BenchSuite{T.RESET}  {T.BOLD}Séquence : {suite.get('name', item.ref)}{T.RESET}")
    rc = cli.run_campaign(suite["steps"], args, suite["id"], suite.get("name", suite["id"]), cooldown_s=cd)
    session_summary(cli, args.output_dir)
    return rc


def show_ports(cli, number: int = None, net: str = None) -> int:
    """Ports Ethernet physiques (même sans adresse ni câble) et, pour ceux sans adresse IPv4, les commandes à copier pour en donner une.
    Sans `number`, les commandes contiennent `<N>` à remplacer : 1 sur un PC, 2 sur l'autre (même réseau des deux côtés)."""
    from core import iperf_server as isrv
    T = cli.TermColor
    ports = isrv.ethernet_ports()
    if not ports:
        print(f"{T.YELLOW}Aucun port Ethernet physique détecté.{T.RESET}")
        return cli.EXIT_OK
    print(f"\n{T.BOLD}{T.rgb(249, 115, 22)}⚡ Ports Ethernet de cette machine{T.RESET}\n")
    net = net if (net and isrv.valid_net(net)) else isrv.suggest_test_network(ports)
    n_txt = str(number) if number else "<N>"
    for p in ports:
        link = (f"{T.GREEN}● lien actif{T.RESET}" + (f" · {p['speed_mbps']} Mb/s" if p["speed_mbps"] else "")) if p["link"] else \
               f"{T.DIM}○ pas de câble{'' if p['link'] is False else ' ou port désactivé'}{T.RESET}"
        print(f"  {T.BOLD}{p['iface']}{T.RESET}  {link}  {T.DIM}{' · '.join(x for x in (p['driver'], p['mac'], p['pci']) if x)}{T.RESET}")
        if p["ipv4"]:
            print("    adresse IPv4 : " + ", ".join(f"{T.BOLD}{a['ip']}{T.RESET}" + (f"/{a['prefix']}" if a.get("prefix") else "") for a in p["ipv4"]))
        elif p["usb"]:
            print(f"    {T.DIM}port vu comme périphérique USB : souvent l'interface « hôte » du BMC (Redfish), pas un port à câbler{T.RESET}")
        else:
            print("    aucune adresse IPv4. Pour relier deux machines en direct :")
            for lab, cmd in isrv.port_commands(p["iface"], net, 1):
                print(f"      {T.DIM}{lab} :{T.RESET} {cmd.replace(f'{net}.1/24', f'{net}.{n_txt}/24')}")
        print()
    print(f"  {T.DIM}Réseau de test : {net} (le même sur les deux machines reliées).{T.RESET}")
    if number:
        print(f"  {T.DIM}N° de CE PC : {number} ; sur l'autre PC : ./cli.py net ports {isrv.other_number(number)}{T.RESET}")
    else:
        print(f"  {T.DIM}Remplacez <N> : 1 sur un PC, 2 sur l'autre (jamais le même). Ou : ./cli.py net ports 1  /  ./cli.py net ports 2  (commandes prêtes à copier).{T.RESET}")
    print(f"  {T.DIM}Ensuite : ping {net}.<N de l'autre PC>, puis ./cli.py net client <IP-du-serveur> bind=<IP de ce port>{T.RESET}\n")
    return cli.EXIT_OK


def serve_iperf(cli, item) -> int:
    """Serveur iperf3 au premier plan : affiche l'adresse IP à donner à l'autre machine, journal des connexions, Ctrl-C pour arrêter."""
    from core import iperf_server as isrv
    T = cli.TermColor
    port = int(item.get("port") or isrv.DEFAULT_PORT)
    ok, msg = isrv.SERVER.start(port)
    if not ok:
        print(f"{T.RED}✗ {msg}{T.RESET}")
        return cli.EXIT_USAGE
    print(f"\n{T.BOLD}{T.rgb(249, 115, 22)}⚡ Serveur iperf3{T.RESET}  {T.GREEN}● actif sur le port {port}{T.RESET}")
    addrs = isrv.local_addresses()
    for a in addrs:
        print(f"  {T.BOLD}{a['ip']}{T.RESET}  {T.DIM}{a['iface']}{' · ' + str(a['speed_mbps']) + ' Mb/s' if a.get('speed_mbps') else ''}{T.RESET}")
    if addrs:
        print(f"\n  Sur l'autre machine, avec l'adresse du lien à tester (celle du port relié par le câble) :")
        print(f"    {T.BOLD}{isrv.client_command_generic(port)}{T.RESET}   ou   ./cli.py net client <IP-du-serveur>")
        for a in addrs:
            print(f"    {T.DIM}pour {a['iface']} :{T.RESET} {isrv.client_command(a['ip'], port)}")
    for lab, cmd in isrv.firewall_commands(port):
        print(f"  {T.DIM}Pare-feu ({lab}) : {cmd}{T.RESET}")
    bare = [p["iface"] for p in isrv.ethernet_ports() if not p["ipv4"] and not p["usb"]]
    if bare:
        print(f"  {T.DIM}Ports sans adresse IPv4 : {', '.join(bare)} — commandes pour leur en donner une : ./cli.py net ports{T.RESET}")
    print(f"\n  {T.DIM}Ctrl-C pour arrêter.{T.RESET}\n")
    shown = 0
    try:
        while isrv.SERVER.is_running():
            lines = list(isrv.SERVER.log)
            for l in lines[shown:]:
                print("  " + l)
            shown = len(lines)
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        isrv.SERVER.stop()
    print(f"\n{T.DIM}Serveur arrêté ({isrv.SERVER.connections} connexion(s)).{T.RESET}")
    return cli.EXIT_OK
