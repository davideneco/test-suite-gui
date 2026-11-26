"""Commandes courtes : `./cli.py cpu 5m`, `./cli.py fio perf <dossier-de-données>`, `./cli.py gpu oclburn 10m`, `./cli.py infos`… (sans options cryptiques).

Les options historiques restent acceptées après la commande (`./cli.py cpu 30m --client ACME -s MA-MACHINE -o ~/Resultats --new-report`).
"""
import os
from typing import List, Tuple

from clitui import model
from clitui.model import Item, find_item, items_for, parse_duration

HELP = """\
BenchSuite — lancer sans option compliquée

  ./cli.py                       écran interactif (menus, profils, réglages, suivi) — comme la version graphique
  ./cli.py cpu 5m                stress CPU 5 minutes   (cpu 30m · cpu 4h · cpu phys 30m = cœurs physiques)
  ./cli.py ram 30m               burn mémoire           (mix 30m = CPU + RAM ensemble)
  ./cli.py fio perf <dossier>    stockage : profil ou onglet (perf lat stab heat endu app hdd) sur un disque de DONNÉES
  ./cli.py gpu oclburn 10m       test GPU (gpu = liste des tests GPU)
  ./cli.py net client <IP-du-serveur>   test réseau iperf3 vers l'autre machine   (net serveur = cette machine fait serveur ;
                                        net ports [N] = ports Ethernet et commandes d'adresse, N = n° de ce PC ; bind=<IP locale> force le port d'envoi)
  ./cli.py seq [nom] [<dossier>] séquence automatique (seq = liste ; ex. seq rapide_cpu_ram ; pause=60 entre les étapes)
  ./cli.py outils                outils manquants sur cette machine et commandes d'installation (apt)
  ./cli.py infos                 informations de la machine à l'écran      (rapport = fichiers HTML / PDF / JSON / CSV, sans test)
  ./cli.py liste [cpu|fio|gpu|net]      tout ce qu'on peut lancer, avec les noms courts
  ./cli.py lancer <nom> [réglage=valeur …]   n'importe quel élément de la liste (ex. lancer gpu_oclburn vram_pct=80 time_s=600)
  ./cli.py dernier               résultats et rapport de ce démarrage de la machine

Tous les lancements depuis le démarrage de la machine s'ajoutent au MÊME rapport (--new-report pour en repartir à zéro).
Options utiles après la commande : -s NOM · --client CLIENT · --operator NOM · -o DOSSIER · -e html pdf json csv · --read-only
Les anciennes options (-b, --suite, --resume, --help…) continuent de fonctionner.
"""

SUBCOMMANDS = ("cpu", "ram", "mix", "fio", "gpu", "net", "reseau", "seq", "sequence", "outils", "lancer", "infos", "rapport", "liste", "dernier", "menu", "aide")
_FIO_TAB_ALIASES = {"perf": "Performance", "lat": "Latence", "stab": "Stabilité", "heat": "Chauffe", "chauffe": "Chauffe",
                    "endu": "Endurance", "app": "Applicatif", "hdd": "HDD"}


def _apply(item: Item, tokens: List[str]) -> Tuple[bool, str]:
    """Applique les réglages `clé=valeur` et une durée éventuelle (« 10m ») à l'élément. Retourne (ok, message d'erreur)."""
    for t in tokens:
        if "=" in t:
            k, _, v = t.partition("=")
            p = next((x for x in item.params if x.key == k), None)
            if p is None:
                return False, f"réglage « {k} » inconnu pour {item.key} (disponibles : {', '.join(x.key for x in item.params) or 'aucun'})"
            ok, val = p.parse(v)
            if not ok:
                return False, f"{k} : {val}"
            p.value = val
        elif parse_duration(t) is not None and any(x.key in ("time_s", "runtime_s") for x in item.params):
            p = next(x for x in item.params if x.key in ("time_s", "runtime_s"))
            p.value = parse_duration(t)
        elif t.startswith("/") and any(x.key == "path" for x in item.params):
            next(x for x in item.params if x.key == "path").value = t
        else:
            return False, f"« {t} » n'est pas compris (réglage clé=valeur, durée comme 10m, ou chemin /…)"
    return True, ""


def _list(page: str = "", cli=None) -> int:
    T = cli.TermColor if cli else None
    pages = [(k, v) for k, v in model.PAGES if not page or page in (k, v.lower())]
    if not pages:
        print(f"Page « {page} » inconnue. Choix : " + ", ".join(k for k, _ in model.PAGES))
        return 3
    for key, title in pages:
        print(f"\n{T.BOLD if T else ''}{title}{T.RESET if T else ''}   (./cli.py {key if key != 'cpu' else 'cpu'} … ou ./cli.py lancer <nom>)")
        last = None
        for it in items_for(key):
            if it.group != last:
                print(f"  {T.DIM if T else ''}— {it.group or 'Divers'}{T.RESET if T else ''}")
                last = it.group
            miss = it.missing_tools()
            mark = f"{T.YELLOW}⚠ {', '.join(miss)} manquant{T.RESET}" if (miss and T) else ""
            print(f"    {it.key:<26} {it.label[:52]:<52} {mark}")
    return 0


def _stress(prefix: str, tokens: List[str], cli, parsed) -> int:
    """cpu / ram / mix : choisit le profil de la durée demandée (5m, 30m, 4h) ou le plus court avec la durée ajustée."""
    from clitui import run
    phys = prefix == "cpu" and any(t in ("phys", "coeurs", "cœurs") for t in tokens)
    tokens = [t for t in tokens if t not in ("phys", "coeurs", "cœurs")]
    base = {"cpu": "cpu_phys" if phys else "cpu_all", "ram": "ram_burn", "mix": "mix"}[prefix]
    secs = None
    rest = []
    for t in tokens:
        d = parse_duration(t)
        if d is not None and secs is None:
            secs = d
        else:
            rest.append(t)
    tag = {300: "5m", 1800: "30m", 14400: "4h"}.get(secs if secs is not None else 300, "5m")
    item = find_item(f"{base}_{tag}")
    if item is None:
        print("Profil introuvable (stress-ng : profils non chargés).")
        return cli.EXIT_USAGE
    if secs is not None and tag == "5m" and secs != 300:
        item.params[0].value = secs                       # durée libre : profil de 5 minutes dont la durée est ajustée
    ok, err = _apply(item, rest)
    if not ok:
        print(f"{cli.TermColor.RED}✗ {err}{cli.TermColor.RESET}")
        return cli.EXIT_USAGE
    return run.launch(cli, [item], parsed)


def dispatch(argv: List[str], cli) -> int:
    """Exécute une commande courte (argv[0] = mot-clé)."""
    from clitui import run
    cmd, rest = argv[0], argv[1:]
    if cmd == "aide":
        print(HELP)
        return cli.EXIT_OK
    parser = cli.build_parser()
    parsed, extra = parser.parse_known_args(rest)           # options historiques éventuelles (-s, --client, -o, -e, --read-only…)
    bad = [t for t in extra if t.startswith("-")]
    if bad:
        print(f"{cli.TermColor.RED}Option inconnue : {' '.join(bad)}{cli.TermColor.RESET}   (./cli.py aide)")
        return cli.EXIT_USAGE
    if parsed.read_only:
        os.environ["BENCHSUITE_READ_ONLY"] = "1"
    if parsed.no_inventory:
        os.environ["BENCHSUITE_NO_INVENTORY"] = "1"
    os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")
    tokens = extra
    args = run.make_args(cli, parsed)

    if cmd in ("cpu", "ram", "mix"):
        return _stress(cmd, tokens, cli, parsed)
    if cmd == "liste":
        return _list(tokens[0] if tokens else "", cli)
    if cmd == "infos":
        cli.print_hardware_info()
        print(f"\n  {cli.TermColor.DIM}Fichiers (HTML / PDF / JSON / CSV) : ./cli.py rapport{cli.TermColor.RESET}")
        return cli.EXIT_OK
    if cmd == "rapport":
        return cli.run_info_report(args)
    if cmd == "dernier":
        run.session_summary(cli, args.output_dir)
        return cli.EXIT_OK
    if cmd == "menu":
        from clitui import tui
        return tui.run(cli, parsed)
    if cmd == "lancer":
        if not tokens:
            return _list("", cli)
        item = find_item(tokens[0])
        if item is None:
            print(f"{cli.TermColor.RED}✗ « {tokens[0]} » inconnu ou ambigu.{cli.TermColor.RESET}   ./cli.py liste")
            return cli.EXIT_USAGE
        ok, err = _apply(item, tokens[1:])
        if not ok:
            print(f"{cli.TermColor.RED}✗ {err}{cli.TermColor.RESET}")
            return cli.EXIT_USAGE
        return run.launch(cli, [item], parsed)
    if cmd == "fio":
        if not tokens:
            return _list("fio", cli)
        key = tokens[0]
        item = find_item(key)
        if item is None and key.lower() in _FIO_TAB_ALIASES:         # « perf » : liste des profils de l'onglet
            group = _FIO_TAB_ALIASES[key.lower()]
            subset = [i for i in items_for("fio") if i.group == group]
            print(f"Profils « {group} » ({len(subset)}) — lancez l'un d'eux : ./cli.py fio <nom> /chemin/du/disque\n")
            for i in subset:
                print(f"    {i.key:<30} {i.label[:60]}")
            return cli.EXIT_OK
        if item is None or item.kind != "fio":
            print(f"{cli.TermColor.RED}✗ profil fio « {key} » inconnu.{cli.TermColor.RESET}   ./cli.py fio")
            return cli.EXIT_USAGE
        ok, err = _apply(item, tokens[1:])
        if not ok:
            print(f"{cli.TermColor.RED}✗ {err}{cli.TermColor.RESET}")
            return cli.EXIT_USAGE
        return run.launch(cli, [item], parsed)
    if cmd == "gpu":
        if not tokens:
            return _list("gpu", cli)
        item = find_item(tokens[0]) or find_item("gpu_" + tokens[0])
        if item is None or item.page != "gpu":
            print(f"{cli.TermColor.RED}✗ test GPU « {tokens[0]} » inconnu.{cli.TermColor.RESET}   ./cli.py gpu")
            return cli.EXIT_USAGE
        ok, err = _apply(item, tokens[1:])
        if not ok:
            print(f"{cli.TermColor.RED}✗ {err}{cli.TermColor.RESET}")
            return cli.EXIT_USAGE
        return run.launch(cli, [item], parsed)
    if cmd == "outils":
        from core.install_advisor import print_install_guide
        return print_install_guide()
    if cmd in ("seq", "sequence"):
        if not tokens:
            return _list("seq", cli)
        item = find_item("seq_" + tokens[0]) or find_item(tokens[0])
        if item is None or item.kind != "suite":
            print(f"{cli.TermColor.RED}✗ séquence « {tokens[0]} » inconnue{cli.TermColor.RESET}   (./cli.py seq pour la liste)")
            return cli.EXIT_USAGE
        for t in tokens[1:]:
            if t.startswith("/") and any(p.key == "fio_path" for p in item.params):
                next(p for p in item.params if p.key == "fio_path").value = t
            elif t.startswith("pause=") and t[6:].isdigit():
                next(p for p in item.params if p.key == "cooldown").value = int(t[6:])
            else:
                print(f"{cli.TermColor.RED}✗ réglage « {t} » non reconnu : /chemin/du/disque ou pause=<secondes>{cli.TermColor.RESET}")
                return cli.EXIT_USAGE
        if parsed.fio_path and any(p.key == "fio_path" for p in item.params) and not item.get("fio_path"):
            next(p for p in item.params if p.key == "fio_path").value = parsed.fio_path
        return run.launch(cli, [item], parsed)
    if cmd in ("net", "reseau"):
        if not tokens:
            return _list("net", cli)
        sub, r = tokens[0], tokens[1:]
        if sub in ("serveur", "server"):
            item = find_item("net_serveur")
            if r and r[0].isdigit():
                item.params[0].value = int(r[0])
            return run.launch(cli, [item], parsed)
        if sub == "client":
            if not r:
                print(f"{cli.TermColor.RED}✗ indiquez l'adresse IP du serveur : ./cli.py net client <IP-du-serveur>{cli.TermColor.RESET}")
                return cli.EXIT_USAGE
            udp = any(t.lower() == "udp" for t in r)
            item = find_item("net_iperf_udp" if udp else "net_iperf_tcp")
            item.params[[p.key for p in item.params].index("host")].value = r[0]
            ok, err = _apply(item, [t for t in r[1:] if t.lower() not in ("tcp", "udp")])
            if not ok:
                print(f"{cli.TermColor.RED}✗ {err}{cli.TermColor.RESET}")
                return cli.EXIT_USAGE
            return run.launch(cli, [item], parsed)
        if sub in ("ports", "port", "cartes"):
            num = next((int(t) for t in r if t.isdigit()), None)
            net = next((t.split("=", 1)[1] for t in r if t.startswith("reseau=")), None)
            return run.show_ports(cli, num, net)
        print(f"{cli.TermColor.RED}✗ « net {sub} » inconnu : net client <IP> · net serveur · net ports{cli.TermColor.RESET}")
        return cli.EXIT_USAGE
    print(HELP)
    return cli.EXIT_USAGE
