"""Authentification de l'UI : hachage scrypt, sessions en base, CSRF.

Volontairement sans dépendance externe (scrypt et hmac viennent de la stdlib) :
une brique de moins à maintenir sur une VM qui tourne des années sans supervision.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import config

SESSION_COOKIE = "telephonie_session"

# Paramètres scrypt : ~100 ms sur un cœur de VM, largement suffisant ici.
_N, _R, _P = 2**14, 8, 1


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return "scrypt${}${}${}${}${}".format(
        _N, _R, _P,
        base64.b64encode(salt).decode(),
        base64.b64encode(digest).decode(),
    )


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, hash_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        digest = hashlib.scrypt(
            password.encode(),
            salt=base64.b64decode(salt_b64),
            n=int(n), r=int(r), p=int(p),
            dklen=len(base64.b64decode(hash_b64)),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, base64.b64decode(hash_b64))


def generate_secret(length: int = 24) -> str:
    """Mot de passe SIP : alphabet sans caractère ambigu ni caractère spécial.

    Les ATA (HT801, TA200...) se saisissent parfois au clavier téléphonique ou
    dans une interface web capricieuse ; on évite donc les caractères qui
    posent problème dans un fichier .conf ou dans une URL SIP.
    """
    alphabet = "abcdefghijkmnopqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(length))


# PIN refusés : ce sont les premiers essayés par quiconque tombe sur le menu
# de messagerie, et `maxlogins=3` ne protège pas contre trois essais bien visés.
TRIVIAL_PINS = frozenset({
    "0000", "1111", "2222", "3333", "4444", "5555", "6666", "7777", "8888",
    "9999", "1234", "4321", "0123", "1212", "1122", "2580",
})


def is_trivial_pin(pin: str) -> bool:
    return pin in TRIVIAL_PINS


def generate_pin(length: int = 4) -> str:
    """PIN de messagerie, en évitant les suites triviales."""
    while True:
        pin = "".join(secrets.choice("0123456789") for _ in range(length))
        if not is_trivial_pin(pin):
            return pin


# --- Sessions --------------------------------------------------------------

def create_session(conn: sqlite3.Connection, admin_id: int) -> tuple[str, str]:
    """Crée une session, retourne (token, csrf)."""
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    expires = datetime.now(timezone.utc) + timedelta(hours=config.SESSION_HOURS)
    conn.execute(
        "INSERT INTO sessions (token, admin_id, csrf, expires_at) VALUES (?, ?, ?, ?)",
        (token, admin_id, csrf, expires.strftime("%Y-%m-%d %H:%M:%S")),
    )
    return token, csrf


def load_session(conn: sqlite3.Connection, token: str | None) -> sqlite3.Row | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT s.token, s.csrf, s.expires_at, a.id AS admin_id, a.username "
        "FROM sessions s JOIN admins a ON a.id = s.admin_id "
        "WHERE s.token = ? AND s.expires_at > datetime('now')",
        (token,),
    ).fetchone()
    return row


def destroy_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def purge_expired_sessions(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM sessions WHERE expires_at <= datetime('now')")


def check_csrf(session: sqlite3.Row, submitted: str | None) -> bool:
    return bool(submitted) and hmac.compare_digest(session["csrf"], submitted or "")
