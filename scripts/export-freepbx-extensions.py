#!/usr/bin/env python3
"""Produit le CSV d'extensions à importer dans FreePBX, depuis la base du PoC.

FreePBX importe les **extensions** par CSV, via le module *Bulk Handler*
(gratuit, installé d'office). Les trunks, routes sortantes et menus vocaux ne
s'importent pas : ils restent à créer dans l'interface. Pour cinq postes contre
trois trunks et deux menus, c'est la partie répétitive qui est automatisée.

    scripts/export-freepbx-extensions.py /var/lib/telephonie/telephonie.db

Le piège, et la raison du mode `--template` : **les colonnes attendues varient
selon la version du module.** Écrire un en-tête « probable » produirait un
import qui échoue, ou pire, qui réussit en ignorant en silence les colonnes qu'il
ne reconnaît pas. La parade est la même que pour les P-values Grandstream :
demander à la machine plutôt que de faire confiance à la documentation.

    1. Dans FreePBX, créez UN poste à la main.
    2. Admin → Bulk Handler → Export → Extensions. Vous obtenez un CSV.
    3. Relancez ce script avec --template ce-fichier.csv

Le script adopte alors exactement les colonnes de votre FreePBX, remplit celles
qu'il sait remplir, et laisse les autres telles quelles.
"""

from __future__ import annotations

import argparse
import csv
import sqlite3
import sys
from pathlib import Path

# En-tête utilisé sans --template. Il correspond aux colonnes couramment
# acceptées par Bulk Handler, mais RIEN NE GARANTIT qu'il corresponde au vôtre :
# c'est un point de départ, pas une référence.
DEFAULT_COLUMNS = [
    "extension", "name", "description", "tech", "secret",
    "voicemail", "vm_password", "vm_email", "vm_attach", "vm_delete",
    "dial", "callerid", "outboundcid",
]

# Ce que le script sait remplir, quel que soit l'en-tête. Une colonne absente de
# ce tableau est laissée vide, et une colonne absente de l'en-tête est ignorée.
def _row_for(device: sqlite3.Row, users_by_box: dict[str, sqlite3.Row]) -> dict[str, str]:
    extension = device["extension"] or ""
    mailbox = device["mailbox"] or extension
    person = users_by_box.get(mailbox)

    return {
        "extension": extension,
        "name": device["label"],
        "description": device["notes"] or "",
        "tech": "pjsip",
        "secret": device["secret"],
        # La messagerie est activée dès qu'une boîte existe. Le PIN n'est pas
        # repris : celui du PoC est dérivé ou tiré au hasard, et FreePBX en
        # génère un de toute façon à la création.
        "voicemail": "enabled" if mailbox else "disabled",
        "vm_password": "",
        "vm_email": (person["email"] if person else "") or "",
        "vm_attach": "yes",
        "vm_delete": "no",       # un courriel perdu ne doit pas perdre le message
        "dial": f"PJSIP/{extension}" if extension else "",
        "callerid": f"{device['label']} <{extension}>" if extension else device["label"],
        "outboundcid": "",
    }


def load(db_path: Path) -> tuple[list[sqlite3.Row], dict[str, sqlite3.Row]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        # Les ponts FXO ne sont pas des postes : dans FreePBX ce sont des trunks,
        # créés à la main. Les exporter comme extensions créerait un poste
        # fantôme qui répondrait aux appels internes.
        devices = conn.execute(
            "SELECT * FROM devices WHERE enabled = 1 AND kind != 'fxo' "
            "AND extension IS NOT NULL ORDER BY extension"
        ).fetchall()
        users = {
            row["voicemail_box"]: row
            for row in conn.execute(
                "SELECT * FROM users WHERE enabled = 1 AND voicemail_box IS NOT NULL"
            )
        }
        return devices, users
    finally:
        conn.close()


def columns_from_template(path: Path) -> list[str]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        header = next(csv.reader(handle), None)
    if not header:
        sys.exit(f"{path} est vide : exportez au moins un poste depuis Bulk Handler.")
    return header


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Génère le CSV d'extensions FreePBX depuis la base du proof of concept.",
    )
    parser.add_argument("database", type=Path,
                        help="base SQLite du PoC (ex. /var/lib/telephonie/telephonie.db)")
    parser.add_argument("-o", "--output", type=Path, default=Path("extensions-freepbx.csv"))
    parser.add_argument("--template", type=Path,
                        help="CSV exporté depuis Bulk Handler, dont on reprend les colonnes")
    args = parser.parse_args()

    if not args.database.exists():
        return sys.exit(f"Base introuvable : {args.database}")

    columns = columns_from_template(args.template) if args.template else list(DEFAULT_COLUMNS)
    devices, users = load(args.database)
    if not devices:
        return sys.exit("Aucun poste actif à exporter.")

    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for device in devices:
            complete = _row_for(device, users)
            writer.writerow({key: complete.get(key, "") for key in columns})

    print(f"{len(devices)} postes écrits dans {args.output}")
    inconnues = [c for c in columns if c not in _row_for(devices[0], users)]
    if inconnues:
        print("Colonnes laissées vides (inconnues du script) : " + ", ".join(inconnues))
    if not args.template:
        print(
            "\nEn-tête par défaut, NON vérifié sur votre FreePBX.\n"
            "Exportez un poste depuis Admin → Bulk Handler → Export, puis relancez\n"
            "avec --template pour adopter ses colonnes exactes."
        )

    print(
        "\nRappels avant l'import :\n"
        "  - les mots de passe SIP sont repris tels quels : les ATA n'ont donc pas\n"
        "    besoin d'être reconfigurés côté mot de passe ;\n"
        "  - l'identifiant SIP devient le NUMÉRO d'extension. Sur chaque ATA,\n"
        "    User Name / Authentication Name / From User passent de « salon » à « 100 » ;\n"
        "  - les ponts FXO ne sont pas dans ce fichier : ce sont des trunks."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
