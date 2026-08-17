#!/usr/bin/env python3
"""Remplit une base vide avec l'installation décrite dans docs/01-architecture.md.

À lancer une seule fois, juste après l'installation, pour ne pas repartir d'une
UI vide. Le script est sans effet si des postes existent déjà.

    /opt/telephonie/venv/bin/python scripts/seed.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db, security  # noqa: E402

DEVICES = [
    # slug, label, kind, extension, codecs, max_contacts, dial_mode, hotline, notes
    ("salon", "Salon", "fxs", "100", "alaw,ulaw", 1, "hotline", "*9",
     "Yeastar TA200 port 1 — téléphone à cadran"),
    ("etage", "Étage", "fxs", "101", "alaw,ulaw", 1, "direct", None,
     "Yeastar TA200 port 2"),
    ("garage", "Garage", "fxs", "102", "alaw,ulaw", 1, "direct", None,
     "Grandstream HT801 — liaison Ethernet, jamais de cuivre entre bâtiments"),
    ("dect", "DECT", "dect", "103", "alaw,ulaw", 3, "direct", None,
     "Base Gigaset N510 + combinés Maxwell C"),
    ("mobile", "Mobile", "mobile", "104", "opus,alaw,ulaw", 2, "direct", None,
     "Linphone via Flexisip — sortant uniquement, pas de push iOS"),
    ("grandstream-fxo", "Pont Freebox", "fxo", None, "alaw,ulaw", 1, "direct", None,
     "Grandstream HT813, port FXO relié à la prise téléphone de la Freebox"),
]

TRUNKS = [
    ("freebox", "Ligne Freebox (FXO)", "PJSIP/{num}@grandstream-fxo", "national", 10, 30,
     "Gratuite. Devient CHANUNAVAIL dès que le HT813 perd son enregistrement, "
     "ce qui déclenche la bascule GSM en une à deux secondes."),
    ("gsm", "Secours GSM (SIM7600G-H)", "Quectel/quectel0/{num}", "national", 20, 30,
     "Facturé à l'usage. Seule route qui survit à une coupure Internet complète."),
]

GROUPS = [
    ("famille", "Toute la maison", "199", 30, "199",
     ["salon", "etage", "garage", "dect"]),
]


def main() -> int:
    db.init_db()
    conn = db.connect()

    if db.one(conn, "SELECT 1 FROM devices LIMIT 1"):
        print("La base contient déjà des postes : rien à faire.")
        return 0

    for slug, label, kind, extension, codecs, contacts, mode, hotline, notes in DEVICES:
        conn.execute(
            "INSERT INTO devices (slug, label, kind, extension, secret, codecs, max_contacts, "
            "mailbox, dial_mode, hotline_target, notes) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (slug, label, kind, extension, security.generate_secret(), codecs, contacts,
             extension, mode, hotline, notes),
        )
        print(f"poste  {slug:<18} {label}")

    for slug, label, template, number_format, priority, timeout, notes in TRUNKS:
        conn.execute(
            "INSERT INTO trunks (slug, label, dial_template, number_format, priority, timeout, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (slug, label, template, number_format, priority, timeout, notes),
        )
        print(f"route  {slug:<18} priorité {priority}")

    for slug, label, extension, ring_time, mailbox, members in GROUPS:
        cursor = conn.execute(
            "INSERT INTO ring_groups (slug, label, extension, ring_time, voicemail_box) "
            "VALUES (?,?,?,?,?)",
            (slug, label, extension, ring_time, mailbox),
        )
        for member in members:
            row = db.one(conn, "SELECT id FROM devices WHERE slug = ?", (member,))
            if row:
                conn.execute(
                    "INSERT INTO ring_group_members (group_id, device_id) VALUES (?, ?)",
                    (cursor.lastrowid, row["id"]),
                )
        print(f"groupe {slug:<18} {len(members)} postes")

    db.audit(conn, "seed", "seed", "installation de référence")
    conn.commit()
    conn.close()

    print(
        "\nBase initialisée. Ouvrez l'UI, relevez le mot de passe SIP de chaque poste "
        "pour le reporter dans les ATA, puis validez l'écran « Appliquer »."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
