"""Accès SQLite : connexion, initialisation du schéma, helpers de requête."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable

from . import config

_SCHEMA = Path(__file__).with_name("schema.sql")


def connect() -> sqlite3.Connection:
    """Ouvre une connexion configurée (clés étrangères actives, lignes nommées)."""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


def init_db() -> None:
    """Crée le schéma s'il manque et insère les réglages par défaut absents."""
    with connect() as conn:
        conn.executescript(_SCHEMA.read_text(encoding="utf-8"))
        for key, value in config.DEFAULT_SETTINGS.items():
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (key, value),
            )


def query(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, tuple(params)).fetchall()


def one(conn: sqlite3.Connection, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
    return conn.execute(sql, tuple(params)).fetchone()


def get_settings(conn: sqlite3.Connection) -> dict[str, str]:
    """Réglages en base, complétés par les valeurs par défaut du code."""
    values = dict(config.DEFAULT_SETTINGS)
    for row in conn.execute("SELECT key, value FROM settings"):
        values[row["key"]] = row["value"]
    return values


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def audit(conn: sqlite3.Connection, actor: str, action: str, detail: str = "") -> None:
    conn.execute(
        "INSERT INTO audit_log (actor, action, detail) VALUES (?, ?, ?)",
        (actor, action, detail),
    )
