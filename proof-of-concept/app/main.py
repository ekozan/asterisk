"""Point d'entrée de l'UI de gestion téléphonie.

Lancement en développement :
    TELEPHONIE_DATA_DIR=./var TELEPHONIE_ASTERISK_DIR=./var/etc \
    TELEPHONIE_RELOAD=0 TELEPHONIE_TTS=0 \
    uvicorn app.main:app --reload

En production, c'est systemd qui s'en charge (systemd/telephonie-ui.service).
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import asterisk, config, db, generator, security
from .routes_api import router as api_router
from .routes_crud import router as crud_router
from .routes_ops import router as ops_router
from .webutil import current_session, get_conn, page, redirect, templates

@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.init_db()
    with db.connect() as conn:
        security.purge_expired_sessions(conn)
    yield


app = FastAPI(title="Téléphonie maison", docs_url=None, redoc_url=None, lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
app.include_router(crud_router)
app.include_router(ops_router)
app.include_router(api_router)


def _has_admin(conn: sqlite3.Connection) -> bool:
    return db.one(conn, "SELECT 1 FROM admins LIMIT 1") is not None


# --- Première configuration -------------------------------------------------

@app.get("/setup")
def setup_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if _has_admin(conn):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        request, "setup.html", {"err_msg": request.query_params.get("err")}
    )


@app.post("/setup")
def setup_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    password2: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    # Une fois un compte créé, cette route est définitivement fermée.
    if _has_admin(conn):
        return RedirectResponse("/login", status_code=303)
    if len(password) < 10:
        return redirect("/setup", err="Le mot de passe doit faire au moins 10 caractères.")
    if password != password2:
        return redirect("/setup", err="Les deux mots de passe diffèrent.")

    conn.execute(
        "INSERT INTO admins (username, password_hash) VALUES (?, ?)",
        (username.strip(), security.hash_password(password)),
    )
    db.audit(conn, username.strip(), "setup", "création du premier compte")
    conn.commit()
    return redirect("/login", ok="Compte créé, vous pouvez vous connecter.")


# --- Connexion --------------------------------------------------------------

@app.get("/login")
def login_form(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not _has_admin(conn):
        return RedirectResponse("/setup", status_code=303)
    return templates.TemplateResponse(
        request,
        "login.html",
        {
            "ok_msg": request.query_params.get("ok"),
            "err_msg": request.query_params.get("err"),
        },
    )


@app.post("/login")
def login_submit(
    username: str = Form(...),
    password: str = Form(...),
    conn: sqlite3.Connection = Depends(get_conn),
):
    row = db.one(conn, "SELECT * FROM admins WHERE username = ?", (username.strip(),))
    if row is None or not security.verify_password(password, row["password_hash"]):
        db.audit(conn, username.strip(), "login-failed", "")
        conn.commit()
        return redirect("/login", err="Identifiant ou mot de passe incorrect.")

    token, _ = security.create_session(conn, row["id"])
    db.audit(conn, row["username"], "login", "")
    conn.commit()

    response = RedirectResponse("/", status_code=303)
    response.set_cookie(
        security.SESSION_COOKIE, token,
        httponly=True, samesite="lax", secure=config.COOKIE_SECURE,
        max_age=config.SESSION_HOURS * 3600, path="/",
    )
    return response


@app.post("/logout")
def logout(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    security.destroy_session(conn, request.cookies.get(security.SESSION_COOKIE))
    conn.commit()
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(security.SESSION_COOKIE, path="/")
    return response


# --- Tableau de bord --------------------------------------------------------

@app.get("/")
def dashboard(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)

    states = asterisk.endpoint_status()
    devices = []
    for device in generator.load_devices(conn, only_enabled=False):
        device["state"] = states.get(device["slug"], "inconnu" if not states else "Unavailable")
        devices.append(device)

    last_revision = db.one(
        conn, "SELECT id, created_at, author, summary FROM revisions ORDER BY id DESC LIMIT 1"
    )

    return page(
        request, conn, session, "dashboard.html",
        devices=devices,
        users=generator.load_users(conn, only_enabled=False),
        groups=generator.load_groups(conn, only_enabled=False),
        trunks=generator.load_trunks(conn, only_enabled=False),
        asterisk_up=bool(states) or asterisk.is_running(),
        active_calls=asterisk.active_channels(),
        problems=generator.validate(conn),
        last_revision=last_revision,
    )
