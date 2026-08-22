#!/usr/bin/env python3
"""Déduit les bons numéros de P-value depuis un appareil déjà configuré.

Grandstream désigne chaque réglage par un numéro qui change selon le modèle et
la version de firmware. Plutôt que de les deviner — un numéro erroné est ignoré
en silence par l'appareil —, on les lit sur un ATA qu'on a configuré à la main.

    # 1. Configurer UN ATA à la main, puis exporter sa configuration depuis son
    #    interface web (Maintenance -> Upgrade and Provisioning -> Download
    #    device configuration), ou récupérer le fichier servi actuellement.
    #
    # 2. Chercher où sont passées les valeurs qu'on connaît :
    scripts/prov-import.py config.xml --find garage --find 10.0.90.20

    # 3. Comparer avec le profil du dépôt :
    scripts/prov-import.py config.xml --device garage

La sortie donne les numéros à corriger dans app/provisioning.py.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, provisioning  # noqa: E402

P_TAG = re.compile(r"^P(\d+)$")


def read_pvalues(path: Path) -> dict[str, str]:
    """Extrait {numéro: valeur} d'un export Grandstream."""
    try:
        root = ElementTree.parse(path).getroot()
    except ElementTree.ParseError as exc:
        raise SystemExit(f"Fichier XML illisible : {exc}")

    values: dict[str, str] = {}
    for element in root.iter():
        match = P_TAG.match(element.tag)
        if match:
            values[match.group(1)] = (element.text or "").strip()
    if not values:
        raise SystemExit(
            "Aucune balise <Pxxx> trouvée. Ce fichier est-il bien un export de "
            "configuration Grandstream au format XML ?"
        )
    return values


def cmd_find(values: dict[str, str], needles: list[str]) -> None:
    print(f"{len(values)} P-values lues.\n")
    for needle in needles:
        matches = [(num, val) for num, val in values.items() if needle.lower() in val.lower()]
        if matches:
            print(f"« {needle} » apparaît dans :")
            for num, val in sorted(matches, key=lambda x: int(x[0])):
                print(f"    P{num:<6} = {val}")
        else:
            print(f"« {needle} » : introuvable.")
            print("    Le réglage porte peut-être une autre valeur sur l'appareil,")
            print("    ou l'export ne contient que les valeurs non nulles.")
        print()


def cmd_compare(values: dict[str, str], device_slug: str) -> int:
    """Compare l'export au fichier que nous générerions pour ce poste."""
    conn = db.connect()
    device = db.one(conn, "SELECT * FROM devices WHERE slug = ?", (device_slug,))
    if device is None:
        raise SystemExit(f"Poste « {device_slug} » introuvable dans la base.")
    settings = db.get_settings(conn)

    profile = provisioning.PROFILES.get(
        device["prov_profile"] or provisioning.DEFAULT_PROFILE, provisioning.GRANDSTREAM_HT80X
    )
    # On rejoue la génération pour obtenir les couples (P, valeur) attendus.
    row = dict(device)
    row.setdefault("mac", row.get("mac") or "000000000000")
    expected = provisioning._pvalues_for_device(row, settings, profile)

    print(f"Profil : {profile.label}")
    print(f"Poste  : {device['label']} ({device['slug']})\n")
    print(f"{'P-value':<10} {'attendu':<28} {'sur l appareil':<28} verdict")
    print("-" * 88)

    problems = 0
    suspects: list[str] = []
    for pnum, value, comment in expected:
        actual = values.get(pnum)
        if actual is None:
            verdict = "absent de l export"
            problems += 1
        elif actual == value:
            verdict = "identique"
        else:
            verdict = "DIFFÉRENT"
            problems += 1
        shown_expected = value if value else "(vide)"
        shown_actual = "—" if actual is None else (actual or "(vide)")
        print(f"P{pnum:<9} {shown_expected[:27]:<28} {shown_actual[:27]:<28} {verdict}")
        print(f"{'':<10} {comment}")

        # Recoupement automatique : si la valeur attendue existe sous un AUTRE
        # numéro, c'est le signe le plus net que notre carte pointe au mauvais
        # endroit — bien plus parlant qu'un simple « différent ».
        if actual != value and value:
            elsewhere = sorted(
                (n for n, v in values.items() if v == value and n != pnum), key=int
            )
            if elsewhere:
                found = ", ".join(f"P{n}" for n in elsewhere)
                print(f"{'':<10} ↳ cette valeur est en {found} sur l'appareil : "
                      f"notre numéro est probablement faux")
                suspects.append(f"{comment} : P{pnum} → {found}")

    print()
    if problems == 0:
        print("Tout concorde : la carte de P-values est bonne pour ce firmware.")
        print("Passez `verified=True` sur le profil dans app/provisioning.py.")
        return 0

    print(f"{problems} écart(s).")
    if suspects:
        print("\nNuméros à corriger dans app/provisioning.py :")
        for line in suspects:
            print(f"  - {line}")
    else:
        print("Aucune valeur attendue retrouvée ailleurs : les écarts viennent")
        print("probablement de valeurs légitimement différentes (mot de passe")
        print("régénéré, réglage jamais poussé). Utilisez --find pour confirmer.")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Valide la carte de P-values Grandstream contre un appareil réel.",
        epilog="Voir docs/10-provisionnement.md pour la procédure complète.",
    )
    parser.add_argument("export", type=Path, help="export XML de la config de l'ATA")
    parser.add_argument("--find", action="append", default=[], metavar="VALEUR",
                        help="cherche dans quel P-value se trouve cette valeur "
                             "(répétable : identifiant SIP, IP du serveur…)")
    parser.add_argument("--device", metavar="SLUG",
                        help="compare l'export à ce que nous génèrerions pour ce poste")
    parser.add_argument("--dump", action="store_true",
                        help="affiche toutes les P-values non vides de l'export")
    args = parser.parse_args()

    if not args.export.exists():
        raise SystemExit(f"Fichier introuvable : {args.export}")

    values = read_pvalues(args.export)

    if args.dump:
        for num in sorted(values, key=int):
            if values[num]:
                print(f"P{num:<6} = {values[num]}")
        return 0

    if args.find:
        cmd_find(values, args.find)
    if args.device:
        return cmd_compare(values, args.device)
    if not args.find:
        parser.error("précisez au moins --find, --device ou --dump")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
