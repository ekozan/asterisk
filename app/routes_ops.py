"""Écrans d'exploitation : réglages, application de la config, révisions,
journal d'appels et journal d'audit."""

from __future__ import annotations

import csv
import sqlite3
from io import StringIO

from fastapi import APIRouter, Depends, Form, Request

from fastapi.responses import PlainTextResponse

from . import asterisk, config, db, generator, provisioning
from .webutil import clean, current_session, get_conn, page, redirect, require_csrf

router = APIRouter()

# Colonnes du CSV produit par cdr_csv, dans l'ordre.
CDR_COLUMNS = [
    "accountcode", "src", "dst", "dcontext", "clid", "channel", "dstchannel",
    "lastapp", "lastdata", "start", "answer", "end", "duration", "billsec",
    "disposition", "amaflags", "uniqueid", "userfield",
]

# Réglages modifiables depuis l'écran « Réglages ».
EDITABLE_SETTINGS = [
    ("site_name", "Nom de l'installation", "text"),
    ("language", "Langue des messages Asterisk", "text"),
    ("tonezone", "Zone de tonalité", "text"),
    ("country_code", "Indicatif pays (sans +)", "text"),
    ("international_allowed", "Autoriser les appels internationaux (00…)", "bool"),
    ("block_premium", "Bloquer les numéros surtaxés (089x, 3xxx)", "bool"),
    ("emergency_callerid", "Numéro présenté sur les appels d'urgence", "text"),
    ("outbound_ring_time", "Durée de sonnerie par défaut (s)", "number"),
    ("ivr_enabled", "Menu vocal sur les appels entrants", "bool"),
    ("ivr_greeting", "Son du menu d'accueil", "text"),
    ("ivr_people_prompt", "Son du menu « joindre une personne »", "text"),
    ("ivr_family_group", "Groupe appelé par la touche 1", "text"),
    ("ivr_timeout", "Délai d'attente d'un choix au menu (s)", "number"),
    ("hotline_prompt", "Son joué au décroché d'un poste « hotline »", "text"),
    ("general_voicemail", "Boîte vocale générale", "text"),
    ("prov_sip_server", "Provisionnement : serveur SIP écrit dans les ATA", "text"),
    ("prov_server_url", "Provisionnement : URL du service (pour les ATA)", "text"),
    ("prov_admin_password", "Provisionnement : mot de passe admin des ATA", "text"),
]


# ============================================================================
# Réglages
# ============================================================================

@router.get("/settings")
def settings_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    return page(request, conn, session, "settings.html", fields=EDITABLE_SETTINGS)


@router.post("/settings")
async def settings_save(request: Request):
    # Seule route asynchrone de l'application, parce que la lecture d'un
    # formulaire à champs variables passe par `await request.form()`. Elle
    # ouvre donc sa connexion elle-même : une connexion SQLite ne peut être
    # utilisée que dans le thread qui l'a créée, et la dépendance `get_conn`
    # s'exécute, elle, dans le pool de threads de Starlette.
    form = await request.form()
    conn = db.connect()
    try:
        session = current_session(request, conn)
        require_csrf(session, form.get("csrf"))

        for key, _label, kind in EDITABLE_SETTINGS:
            if kind == "bool":
                # Une case décochée n'est pas envoyée : son absence vaut « non ».
                db.set_setting(conn, key, "1" if form.get(key) else "0")
            else:
                db.set_setting(conn, key, clean(form.get(key)))

        db.audit(conn, session["username"], "settings-update", "")
        conn.commit()
    finally:
        conn.close()
    return redirect("/settings", ok="Réglages enregistrés. Pensez à appliquer la configuration.")


# ============================================================================
# Application de la configuration
# ============================================================================

@router.get("/apply")
def apply_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    try:
        bundle = generator.build(conn)
        diff_text = generator.diff(bundle)
        build_error = None
    except Exception as exc:  # gabarit ou donnée inattendue
        bundle, diff_text, build_error = {}, "", str(exc)

    return page(
        request, conn, session, "apply.html",
        problems=generator.validate(conn),
        diff=diff_text,
        bundle=bundle,
        build_error=build_error,
        asterisk_up=asterisk.is_running(),
    )


@router.post("/apply")
def apply_submit(
    request: Request,
    csrf: str = Form(""),
    summary: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    result = generator.apply(
        conn, session["username"], clean(summary) or "modification depuis l'UI"
    )
    if not result["ok"]:
        errors = [p.message for p in result["problems"] if p.level == "error"]
        detail = errors[0] if errors else "le rechargement d'Asterisk a signalé une anomalie"
        return redirect("/apply", err=f"Application incomplète : {detail}. "
                                      "Voir le détail dans les révisions.")
    return redirect("/apply", ok="Configuration appliquée et Asterisk rechargé.")


@router.get("/revisions")
def revisions_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    detail_id = request.query_params.get("detail")
    detail = db.one(conn, "SELECT * FROM revisions WHERE id = ?", (detail_id,)) if detail_id else None
    return page(
        request, conn, session, "revisions.html",
        revisions=db.query(
            conn,
            "SELECT id, created_at, author, summary, applied FROM revisions "
            "ORDER BY id DESC LIMIT 50",
        ),
        detail=detail,
    )


@router.post("/revisions/{revision_id}/rollback")
def revision_rollback(
    revision_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    result = generator.rollback(conn, revision_id, session["username"])
    if not result["ok"]:
        return redirect("/revisions", err=f"Retour arrière incomplet : {result['log'][:200]}")
    return redirect("/revisions", ok=f"Fichiers de la révision {revision_id} réappliqués. "
                                     "Attention : la base n'a pas été modifiée, un nouvel "
                                     "« Appliquer » réécrira la version courante.")


# ============================================================================
# Journal d'appels (CDR, lecture seule)
# ============================================================================

def _read_cdr(limit: int = 100) -> list[dict]:
    path = config.CDR_CSV
    if not path.exists():
        return []
    try:
        # On ne lit que la fin du fichier : Master.csv n'est jamais tronqué et
        # peut peser plusieurs dizaines de mégaoctets après quelques années.
        with path.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            window = min(size, 256 * 1024)
            handle.seek(size - window)
            raw = handle.read().decode("utf-8", errors="replace")
    except OSError:
        return []

    if window < size:
        raw = raw.split("\n", 1)[-1]  # jette la première ligne, sans doute coupée

    rows = []
    for parts in csv.reader(StringIO(raw)):
        if len(parts) < len(CDR_COLUMNS):
            continue
        rows.append(dict(zip(CDR_COLUMNS, parts)))
    return list(reversed(rows))[:limit]


@router.get("/calls")
def calls_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    return page(
        request, conn, session, "calls.html",
        calls=_read_cdr(),
        cdr_path=str(config.CDR_CSV),
    )


# ============================================================================
# Provisionnement automatique des ATA
# ============================================================================

@router.get("/provisioning")
def provisioning_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    settings = db.get_settings(conn)
    devices = provisioning.provisionable_devices(conn)

    return page(
        request, conn, session, "provisioning.html",
        devices=devices,
        profiles=provisioning.PROFILES,
        unverified=[p for p in provisioning.PROFILES.values() if not p.verified],
        sip_server_missing=not settings.get("prov_sip_server"),
        recent=db.query(
            conn,
            "SELECT * FROM audit_log WHERE action LIKE 'prov-%' ORDER BY id DESC LIMIT 20",
        ),
    )


@router.get("/provisioning/{device_id}/preview", response_class=PlainTextResponse)
def provisioning_preview(
    device_id: int, request: Request, conn: sqlite3.Connection = Depends(get_conn)
):
    """Aperçu authentifié du XML qui sera servi à l'appareil.

    Sert aussi à vérifier ce qu'on pousse AVANT de redémarrer un ATA — un
    fichier erroné laisse l'appareil injoignable sans message d'erreur.
    """
    current_session(request, conn)
    device = db.one(conn, "SELECT * FROM devices WHERE id = ?", (device_id,))
    if device is None or not device["mac"]:
        return PlainTextResponse("Poste inconnu ou sans adresse MAC.", status_code=404)
    return PlainTextResponse(
        provisioning.build_config(device, db.get_settings(conn)),
        media_type="text/xml",
    )


@router.get("/audit")
def audit_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    return page(
        request, conn, session, "audit.html",
        entries=db.query(conn, "SELECT * FROM audit_log ORDER BY id DESC LIMIT 200"),
    )
