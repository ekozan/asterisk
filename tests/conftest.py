"""Isolation des tests : base et fichiers générés dans un répertoire temporaire.

Les variables d'environnement doivent être posées AVANT l'import de app.config,
qui les lit une fois pour toutes au chargement du module.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="telephonie-tests-"))

os.environ["TELEPHONIE_DATA_DIR"] = str(_TMP / "data")
os.environ["TELEPHONIE_DB"] = str(_TMP / "data" / "test.db")
os.environ["TELEPHONIE_ASTERISK_DIR"] = str(_TMP / "etc")
os.environ["TELEPHONIE_GENERATED_DIR"] = str(_TMP / "etc" / "generated")
os.environ["TELEPHONIE_RELOAD"] = "0"      # pas d'Asterisk sur la machine de test
os.environ["TELEPHONIE_TTS"] = "0"         # pas de Piper non plus
os.environ["TELEPHONIE_API_TOKEN"] = "jeton-de-test"

import pytest  # noqa: E402

from app import db  # noqa: E402


@pytest.fixture()
def conn():
    """Base vierge pour chaque test."""
    if Path(os.environ["TELEPHONIE_DB"]).exists():
        Path(os.environ["TELEPHONIE_DB"]).unlink()
    db.init_db()
    connection = db.connect()
    yield connection
    connection.close()


@pytest.fixture()
def sample(conn):
    """Un jeu de données minimal mais représentatif : deux postes, une
    personne qui en couvre deux, un groupe, deux trunks en failover."""
    conn.executescript(
        """
        INSERT INTO devices (slug, label, kind, extension, secret, mailbox)
          VALUES ('salon', 'Salon', 'fxs', '100', 'secret1', '100');
        INSERT INTO devices (slug, label, kind, extension, secret, codecs, max_contacts)
          VALUES ('mobile', 'Mobile', 'mobile', '104', 'secret2', 'opus,alaw', 2);
        INSERT INTO devices (slug, label, kind, secret)
          VALUES ('fxo', 'Pont Freebox', 'fxo', 'secret3');

        INSERT INTO users (name, extension, menu_digit, voicemail_box, voicemail_pin)
          VALUES ('Camille', '201', '1', '201', '4821');
        INSERT INTO user_devices (user_id, device_id) VALUES (1, 1), (1, 2);

        INSERT INTO ring_groups (slug, label, extension, voicemail_box)
          VALUES ('famille', 'Toute la maison', '199', '199');
        INSERT INTO ring_group_members (group_id, device_id) VALUES (1, 1), (1, 2);

        INSERT INTO trunks (slug, label, dial_template, number_format, priority)
          VALUES ('freebox', 'Freebox', 'PJSIP/{num}@fxo', 'national', 10);
        INSERT INTO trunks (slug, label, dial_template, number_format, priority)
          VALUES ('gsm', 'GSM', 'Quectel/quectel0/{num}', 'e164', 20);

        INSERT INTO speed_dials (code, number, label) VALUES ('5', '0600000000', 'Mamie');
        """
    )
    conn.commit()
    return conn
