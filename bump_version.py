#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bump_version.py — Incrémente automatiquement la version du projet BenchSuite
dans tous les fichiers sources concernés (main.py, exporters/export.py, cli.py, README.md).
"""

import re
import sys
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

FILES_TO_UPDATE = [
    ("main.py", r'(VERSION\s*=\s*")[^"]+(")'),
    ("exporters/export.py", r'(APP_VERSION\s*=\s*")[^"]+(")'),
    ("cli.py", r'(Version CLI \(Headless\) )[^<{\s]+'),
    ("README.md", r'(Version actuelle : \*\*)[^*]+(\*\*)'),
    ("windows_viewer/benchsuite_viewer.py", r'(VIEWER_VERSION\s*=\s*")[^"]+(")'),
    ("csv2html.py", r'("version":\s*")[^"]+(")'),
    ("csv2html.py", r'(Version\s*:\s*v)[0-9.]+'),
]

def bump_ver_str(ver: str, part: str = "patch") -> str:
    # Cas 1: ex "1.2-beta.2" -> "1.2-beta.3"
    m_beta = re.match(r"^(\d+\.\d+(?:\.\d+)?-[a-zA-Z]+\.)(\d+)$", ver)
    if m_beta:
        prefix, num = m_beta.groups()
        return f"{prefix}{int(num) + 1}"
    
    # Cas 2: ex "1.2.3"
    m_sem = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?$", ver)
    if m_sem:
        major, minor, patch = m_sem.groups()
        major = int(major)
        minor = int(minor)
        patch = int(patch) if patch is not None else 0
        if part == "major":
            return f"{major + 1}.0.0"
        elif part == "minor":
            return f"{major}.{minor + 1}.0"
        else:
            return f"{major}.{minor}.{patch + 1}"

    # Fallback si format inconnu : tenter d'incrémenter le dernier nombre
    m_last = re.search(r"(\d+)$", ver)
    if m_last:
        num = int(m_last.group(1))
        return ver[:m_last.start()] + str(num + 1)
    return ver + ".1"

def main():
    part = sys.argv[1] if len(sys.argv) > 1 else "patch"
    
    # Lecture version courante dans main.py
    main_path = os.path.join(ROOT, "main.py")
    with open(main_path, "r", encoding="utf-8") as f:
        content = f.read()
    
    m = re.search(r'VERSION\s*=\s*"([^"]+)"', content)
    if not m:
        print("Erreur : impossible de lire VERSION dans main.py")
        sys.exit(1)
        
    old_ver = m.group(1)
    # « ./bump_version.sh 1.0.0 » : version imposée (passage à une version majeure / stable) ; sinon patch | minor | major
    new_ver = part if re.match(r"^\d+\.\d+(\.\d+)?(-[A-Za-z]+\.\d+)?$", part) else bump_ver_str(old_ver, part)
    
    print(f"Version actuelle : {old_ver} → Nouvelle version : {new_ver}")
    
    for rel_path, pattern in FILES_TO_UPDATE:
        filepath = os.path.join(ROOT, rel_path)
        if not os.path.exists(filepath):
            continue
        with open(filepath, "r", encoding="utf-8") as f:
            fcontent = f.read()
            
        if "cli.py" in rel_path:
            nfcontent = re.sub(pattern, rf"\g<1>{new_ver}", fcontent)
        elif re.compile(pattern).groups >= 2:
            nfcontent = re.sub(pattern, rf"\g<1>{new_ver}\g<2>", fcontent)
        else:
            nfcontent = re.sub(pattern, rf"\g<1>{new_ver}", fcontent)
            
        if nfcontent != fcontent:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(nfcontent)
            print(f"  ✓ Mis à jour : {rel_path}")
            
    # Retourner la nouvelle version en sortie pour les scripts bash
    with open(os.path.join(ROOT, ".last_version"), "w", encoding="utf-8") as f:
        f.write(new_ver)

if __name__ == "__main__":
    main()
