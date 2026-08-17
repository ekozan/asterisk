"""Briques communes aux routes HTML : templates, session, CSRF, redirections."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import urlencode

from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates

from . import config, db, security

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def get_conn() -> sqlite3.Connection:
    """Dépendance FastAPI : une connexion SQLite par requête."""
    conn = db.connect()
    try:
        yield conn
    finally:
        conn.close()


def current_session(request: Request, conn: sqlite3.Connection) -> sqlite3.Row:
    """Session valide, ou redirection 303 vers /login."""
    row = security.load_session(conn, request.cookies.get(security.SESSION_COOKIE))
    if row is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return row


def require_csrf(session: sqlite3.Row, token: str | None) -> None:
    if not security.check_csrf(session, token):
        raise HTTPException(status_code=400, detail="Jeton CSRF invalide, rechargez la page.")


def redirect(path: str, ok: str | None = None, err: str | None = None) -> RedirectResponse:
    """Redirection après POST, avec un message à afficher sur la page cible."""
    params = {k: v for k, v in (("ok", ok), ("err", err)) if v}
    url = f"{path}?{urlencode(params)}" if params else path
    return RedirectResponse(url, status_code=303)


def page(request: Request, conn: sqlite3.Connection, session: sqlite3.Row,
         name: str, **context):
    """Rend une page en injectant le contexte commun à toutes les vues."""
    from . import generator  # import tardif : évite une boucle d'import

    base = {
        "session": session,
        "csrf": session["csrf"],
        "settings": db.get_settings(conn),
        "ok_msg": request.query_params.get("ok"),
        "err_msg": request.query_params.get("err"),
        "dirty": generator.is_dirty(conn),
        "config": config,
    }
    base.update(context)
    return templates.TemplateResponse(request, name, base)


def form_int(value: str | None, default: int, minimum: int = 0, maximum: int = 10_000) -> int:
    try:
        return max(minimum, min(maximum, int(str(value).strip())))
    except (TypeError, ValueError):
        return default


def clean(value: str | None) -> str:
    return (value or "").strip()


def clean_or_none(value: str | None) -> str | None:
    """Chaîne nettoyée, ou None — pour que les colonnes UNIQUE acceptent
    plusieurs lignes « sans valeur » (en SQL, NULL n'entre pas en conflit,
    contrairement à une chaîne vide qui, elle, se dupliquerait)."""
    cleaned = clean(value)
    return cleaned or None
