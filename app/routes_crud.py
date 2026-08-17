"""Écrans de gestion : postes, personnes, groupes, abrégés, routes sortantes.

Toutes ces vues n'écrivent QUE dans la base. Rien n'arrive à Asterisk tant que
l'écran « Appliquer » n'a pas été validé — ce qui permet de préparer plusieurs
changements et de les pousser d'un coup, en voyant leur diff avant.
"""

from __future__ import annotations

import sqlite3

from fastapi import APIRouter, Depends, Form, Request

from . import db, generator, security
from .webutil import (
    clean, clean_or_none, current_session, form_int, get_conn, page, redirect, require_csrf,
)

router = APIRouter()

DEVICE_KINDS = {
    "fxs": "Poste analogique (ATA FXS)",
    "dect": "Base DECT",
    "mobile": "Application mobile",
    "fxo": "Pont vers ligne externe (FXO)",
    "console": "Combiné USB (chan_console)",
}


def _integrity_message(exc: sqlite3.IntegrityError) -> str:
    text = str(exc)
    if "devices.slug" in text:
        return "Cet identifiant de poste est déjà pris."
    if "extension" in text:
        return "Ce numéro est déjà attribué."
    if "menu_digit" in text:
        return "Cette touche de menu est déjà attribuée."
    if "voicemail_box" in text:
        return "Cette boîte vocale est déjà attribuée."
    if "code" in text:
        return "Ce code abrégé existe déjà."
    if "slug" in text:
        return "Cet identifiant est déjà pris."
    return f"Enregistrement refusé : {text}"


# ============================================================================
# Postes
# ============================================================================

@router.get("/devices")
def devices_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    edit_id = request.query_params.get("edit")
    editing = db.one(conn, "SELECT * FROM devices WHERE id = ?", (edit_id,)) if edit_id else None
    return page(
        request, conn, session, "devices.html",
        devices=generator.load_devices(conn, only_enabled=False),
        kinds=DEVICE_KINDS,
        editing=editing,
    )


@router.post("/devices")
def device_create(
    request: Request,
    csrf: str = Form(""),
    slug: str = Form(...),
    label: str = Form(...),
    kind: str = Form("fxs"),
    extension: str = Form(""),
    codecs: str = Form("alaw,ulaw"),
    max_contacts: str = Form("1"),
    mailbox: str = Form(""),
    dial_mode: str = Form("direct"),
    hotline_target: str = Form(""),
    ring_time: str = Form("30"),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    slug_value = clean(slug).lower()
    if not generator.SLUG_RE.match(slug_value):
        return redirect("/devices", err="Identifiant invalide : minuscules, chiffres et tirets, "
                                        "2 à 32 caractères.")
    if kind not in DEVICE_KINDS:
        return redirect("/devices", err="Type de poste inconnu.")

    extension_value = clean_or_none(extension)
    # Par défaut, la boîte vocale porte le numéro du poste : c'est ce que
    # suppose le code de service *97.
    mailbox_value = clean_or_none(mailbox) or extension_value

    try:
        conn.execute(
            "INSERT INTO devices (slug, label, kind, extension, secret, codecs, max_contacts, "
            "mailbox, dial_mode, hotline_target, ring_time, notes) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                slug_value, clean(label), kind, extension_value,
                security.generate_secret(), clean(codecs) or "alaw,ulaw",
                form_int(max_contacts, 1, 1, 10), mailbox_value,
                dial_mode if dial_mode in ("direct", "hotline") else "direct",
                clean_or_none(hotline_target), form_int(ring_time, 30, 5, 120),
                clean_or_none(notes),
            ),
        )
    except sqlite3.IntegrityError as exc:
        return redirect("/devices", err=_integrity_message(exc))

    db.audit(conn, session["username"], "device-create", slug_value)
    conn.commit()
    return redirect("/devices", ok=f"Poste « {clean(label)} » créé. "
                                   "Relevez son mot de passe SIP pour configurer l'ATA.")


@router.post("/devices/{device_id}/edit")
def device_update(
    device_id: int,
    request: Request,
    csrf: str = Form(""),
    label: str = Form(...),
    kind: str = Form("fxs"),
    extension: str = Form(""),
    codecs: str = Form("alaw,ulaw"),
    max_contacts: str = Form("1"),
    mailbox: str = Form(""),
    dial_mode: str = Form("direct"),
    hotline_target: str = Form(""),
    ring_time: str = Form("30"),
    notes: str = Form(""),
    enabled: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    try:
        conn.execute(
            "UPDATE devices SET label=?, kind=?, extension=?, codecs=?, max_contacts=?, "
            "mailbox=?, dial_mode=?, hotline_target=?, ring_time=?, notes=?, enabled=?, "
            "updated_at=datetime('now') WHERE id=?",
            (
                clean(label), kind if kind in DEVICE_KINDS else "fxs",
                clean_or_none(extension), clean(codecs) or "alaw,ulaw",
                form_int(max_contacts, 1, 1, 10), clean_or_none(mailbox),
                dial_mode if dial_mode in ("direct", "hotline") else "direct",
                clean_or_none(hotline_target), form_int(ring_time, 30, 5, 120),
                clean_or_none(notes), 1 if enabled == "1" else 0, device_id,
            ),
        )
    except sqlite3.IntegrityError as exc:
        return redirect("/devices", err=_integrity_message(exc))

    db.audit(conn, session["username"], "device-update", str(device_id))
    conn.commit()
    return redirect("/devices", ok="Poste mis à jour.")


@router.post("/devices/{device_id}/secret")
def device_regenerate_secret(
    device_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    conn.execute(
        "UPDATE devices SET secret = ?, updated_at = datetime('now') WHERE id = ?",
        (security.generate_secret(), device_id),
    )
    db.audit(conn, session["username"], "device-secret", str(device_id))
    conn.commit()
    return redirect("/devices", ok="Nouveau mot de passe SIP généré. Le poste restera "
                                   "injoignable tant que l'ATA n'aura pas été mis à jour "
                                   "et la configuration appliquée.")


@router.post("/devices/{device_id}/delete")
def device_delete(
    device_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    row = db.one(conn, "SELECT slug FROM devices WHERE id = ?", (device_id,))
    conn.execute("DELETE FROM devices WHERE id = ?", (device_id,))
    db.audit(conn, session["username"], "device-delete", row["slug"] if row else str(device_id))
    conn.commit()
    return redirect("/devices", ok="Poste supprimé.")


# ============================================================================
# Personnes
# ============================================================================

@router.get("/users")
def users_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    edit_id = request.query_params.get("edit")
    editing = None
    editing_devices: list[int] = []
    if edit_id:
        editing = db.one(conn, "SELECT * FROM users WHERE id = ?", (edit_id,))
        if editing:
            editing_devices = [
                r["device_id"] for r in
                db.query(conn, "SELECT device_id FROM user_devices WHERE user_id = ?",
                         (editing["id"],))
            ]
    return page(
        request, conn, session, "users.html",
        users=generator.load_users(conn, only_enabled=False),
        devices=generator.load_devices(conn, only_enabled=False),
        editing=editing,
        editing_devices=editing_devices,
    )


def _set_user_devices(conn: sqlite3.Connection, user_id: int, device_ids: list[str]) -> None:
    conn.execute("DELETE FROM user_devices WHERE user_id = ?", (user_id,))
    for raw in device_ids:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO user_devices (user_id, device_id) VALUES (?, ?)",
                (user_id, int(raw)),
            )
        except ValueError:
            continue


@router.post("/users")
def user_create(
    request: Request,
    csrf: str = Form(""),
    name: str = Form(...),
    extension: str = Form(""),
    menu_digit: str = Form(""),
    voicemail_box: str = Form(""),
    email: str = Form(""),
    device_ids: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    extension_value = clean_or_none(extension)
    box = clean_or_none(voicemail_box) or extension_value
    pin = security.generate_pin()

    try:
        cursor = conn.execute(
            "INSERT INTO users (name, extension, menu_digit, voicemail_box, voicemail_pin, email) "
            "VALUES (?,?,?,?,?,?)",
            (clean(name), extension_value, clean_or_none(menu_digit), box, pin,
             clean_or_none(email)),
        )
        _set_user_devices(conn, cursor.lastrowid, device_ids)
    except sqlite3.IntegrityError as exc:
        return redirect("/users", err=_integrity_message(exc))

    db.audit(conn, session["username"], "user-create", clean(name))
    conn.commit()
    message = f"Personne « {clean(name)} » créée."
    if box:
        message += f" PIN de messagerie initial : {pin} (à changer via *97)."
    return redirect("/users", ok=message)


@router.post("/users/{user_id}/edit")
def user_update(
    user_id: int,
    request: Request,
    csrf: str = Form(""),
    name: str = Form(...),
    extension: str = Form(""),
    menu_digit: str = Form(""),
    voicemail_box: str = Form(""),
    email: str = Form(""),
    enabled: str = Form("0"),
    device_ids: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    try:
        conn.execute(
            "UPDATE users SET name=?, extension=?, menu_digit=?, voicemail_box=?, email=?, "
            "enabled=?, updated_at=datetime('now') WHERE id=?",
            (clean(name), clean_or_none(extension), clean_or_none(menu_digit),
             clean_or_none(voicemail_box), clean_or_none(email),
             1 if enabled == "1" else 0, user_id),
        )
        _set_user_devices(conn, user_id, device_ids)
    except sqlite3.IntegrityError as exc:
        return redirect("/users", err=_integrity_message(exc))

    db.audit(conn, session["username"], "user-update", str(user_id))
    conn.commit()
    return redirect("/users", ok="Personne mise à jour.")


@router.post("/users/{user_id}/pin")
def user_reset_pin(
    user_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    pin = security.generate_pin()
    conn.execute("UPDATE users SET voicemail_pin = ? WHERE id = ?", (pin, user_id))
    db.audit(conn, session["username"], "user-pin", str(user_id))
    conn.commit()
    return redirect("/users", ok=f"Nouveau PIN de messagerie : {pin}")


@router.post("/users/{user_id}/delete")
def user_delete(
    user_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.audit(conn, session["username"], "user-delete", str(user_id))
    conn.commit()
    return redirect("/users", ok="Personne supprimée.")


# ============================================================================
# Groupes d'appel
# ============================================================================

@router.get("/groups")
def groups_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    edit_id = request.query_params.get("edit")
    editing = None
    editing_devices: list[int] = []
    if edit_id:
        editing = db.one(conn, "SELECT * FROM ring_groups WHERE id = ?", (edit_id,))
        if editing:
            editing_devices = [
                r["device_id"] for r in
                db.query(conn, "SELECT device_id FROM ring_group_members WHERE group_id = ?",
                         (editing["id"],))
            ]
    return page(
        request, conn, session, "groups.html",
        groups=generator.load_groups(conn, only_enabled=False),
        devices=generator.load_devices(conn, only_enabled=False),
        editing=editing,
        editing_devices=editing_devices,
    )


def _set_group_devices(conn: sqlite3.Connection, group_id: int, device_ids: list[str]) -> None:
    conn.execute("DELETE FROM ring_group_members WHERE group_id = ?", (group_id,))
    for raw in device_ids:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO ring_group_members (group_id, device_id) VALUES (?, ?)",
                (group_id, int(raw)),
            )
        except ValueError:
            continue


@router.post("/groups")
def group_create(
    request: Request,
    csrf: str = Form(""),
    slug: str = Form(...),
    label: str = Form(...),
    extension: str = Form(""),
    ring_time: str = Form("30"),
    voicemail_box: str = Form(""),
    device_ids: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    slug_value = clean(slug).lower()
    if not generator.SLUG_RE.match(slug_value):
        return redirect("/groups", err="Identifiant invalide : minuscules, chiffres et tirets.")
    try:
        cursor = conn.execute(
            "INSERT INTO ring_groups (slug, label, extension, ring_time, voicemail_box) "
            "VALUES (?,?,?,?,?)",
            (slug_value, clean(label), clean_or_none(extension),
             form_int(ring_time, 30, 5, 120), clean_or_none(voicemail_box)),
        )
        _set_group_devices(conn, cursor.lastrowid, device_ids)
    except sqlite3.IntegrityError as exc:
        return redirect("/groups", err=_integrity_message(exc))

    db.audit(conn, session["username"], "group-create", slug_value)
    conn.commit()
    return redirect("/groups", ok="Groupe créé.")


@router.post("/groups/{group_id}/edit")
def group_update(
    group_id: int,
    request: Request,
    csrf: str = Form(""),
    label: str = Form(...),
    extension: str = Form(""),
    ring_time: str = Form("30"),
    voicemail_box: str = Form(""),
    enabled: str = Form("0"),
    device_ids: list[str] = Form([]),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    try:
        conn.execute(
            "UPDATE ring_groups SET label=?, extension=?, ring_time=?, voicemail_box=?, enabled=? "
            "WHERE id=?",
            (clean(label), clean_or_none(extension), form_int(ring_time, 30, 5, 120),
             clean_or_none(voicemail_box), 1 if enabled == "1" else 0, group_id),
        )
        _set_group_devices(conn, group_id, device_ids)
    except sqlite3.IntegrityError as exc:
        return redirect("/groups", err=_integrity_message(exc))

    db.audit(conn, session["username"], "group-update", str(group_id))
    conn.commit()
    return redirect("/groups", ok="Groupe mis à jour.")


@router.post("/groups/{group_id}/delete")
def group_delete(
    group_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    conn.execute("DELETE FROM ring_groups WHERE id = ?", (group_id,))
    db.audit(conn, session["username"], "group-delete", str(group_id))
    conn.commit()
    return redirect("/groups", ok="Groupe supprimé.")


# ============================================================================
# Numéros abrégés
# ============================================================================

@router.get("/speed-dials")
def speed_dials_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    return page(
        request, conn, session, "speed_dials.html",
        speed_dials=db.query(conn, "SELECT * FROM speed_dials ORDER BY code"),
    )


@router.post("/speed-dials")
def speed_dial_create(
    request: Request,
    csrf: str = Form(""),
    code: str = Form(...),
    number: str = Form(...),
    label: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    try:
        conn.execute(
            "INSERT INTO speed_dials (code, number, label) VALUES (?,?,?)",
            (clean(code), clean(number), clean_or_none(label)),
        )
    except sqlite3.IntegrityError as exc:
        return redirect("/speed-dials", err=_integrity_message(exc))
    db.audit(conn, session["username"], "speeddial-create", clean(code))
    conn.commit()
    return redirect("/speed-dials", ok="Numéro abrégé ajouté.")


@router.post("/speed-dials/{speed_dial_id}/delete")
def speed_dial_delete(
    speed_dial_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    conn.execute("DELETE FROM speed_dials WHERE id = ?", (speed_dial_id,))
    db.audit(conn, session["username"], "speeddial-delete", str(speed_dial_id))
    conn.commit()
    return redirect("/speed-dials", ok="Numéro abrégé supprimé.")


# ============================================================================
# Routes sortantes (trunks)
# ============================================================================

@router.get("/trunks")
def trunks_list(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    session = current_session(request, conn)
    edit_id = request.query_params.get("edit")
    editing = db.one(conn, "SELECT * FROM trunks WHERE id = ?", (edit_id,)) if edit_id else None
    return page(
        request, conn, session, "trunks.html",
        trunks=generator.load_trunks(conn, only_enabled=False),
        editing=editing,
    )


@router.post("/trunks")
def trunk_create(
    request: Request,
    csrf: str = Form(""),
    slug: str = Form(...),
    label: str = Form(...),
    dial_template: str = Form(...),
    number_format: str = Form("national"),
    priority: str = Form("10"),
    timeout: str = Form("30"),
    notes: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)

    template = clean(dial_template)
    if "{num}" not in template:
        return redirect("/trunks", err="Le gabarit doit contenir {num}, "
                                       "par exemple PJSIP/{num}@grandstream-fxo")
    try:
        conn.execute(
            "INSERT INTO trunks (slug, label, dial_template, number_format, priority, timeout, notes) "
            "VALUES (?,?,?,?,?,?,?)",
            (clean(slug).lower(), clean(label), template,
             number_format if number_format in ("national", "e164") else "national",
             form_int(priority, 10, 1, 999), form_int(timeout, 30, 5, 120),
             clean_or_none(notes)),
        )
    except sqlite3.IntegrityError as exc:
        return redirect("/trunks", err=_integrity_message(exc))
    db.audit(conn, session["username"], "trunk-create", clean(slug))
    conn.commit()
    return redirect("/trunks", ok="Route sortante ajoutée.")


@router.post("/trunks/{trunk_id}/edit")
def trunk_update(
    trunk_id: int,
    request: Request,
    csrf: str = Form(""),
    label: str = Form(...),
    dial_template: str = Form(...),
    number_format: str = Form("national"),
    priority: str = Form("10"),
    timeout: str = Form("30"),
    notes: str = Form(""),
    enabled: str = Form("0"),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    template = clean(dial_template)
    if "{num}" not in template:
        return redirect("/trunks", err="Le gabarit doit contenir {num}.")
    conn.execute(
        "UPDATE trunks SET label=?, dial_template=?, number_format=?, priority=?, timeout=?, "
        "notes=?, enabled=? WHERE id=?",
        (clean(label), template,
         number_format if number_format in ("national", "e164") else "national",
         form_int(priority, 10, 1, 999), form_int(timeout, 30, 5, 120),
         clean_or_none(notes), 1 if enabled == "1" else 0, trunk_id),
    )
    db.audit(conn, session["username"], "trunk-update", str(trunk_id))
    conn.commit()
    return redirect("/trunks", ok="Route sortante mise à jour.")


@router.post("/trunks/{trunk_id}/delete")
def trunk_delete(
    trunk_id: int, request: Request, csrf: str = Form(""),
    conn: sqlite3.Connection = Depends(get_conn),
):
    session = current_session(request, conn)
    require_csrf(session, csrf)
    conn.execute("DELETE FROM trunks WHERE id = ?", (trunk_id,))
    db.audit(conn, session["username"], "trunk-delete", str(trunk_id))
    conn.commit()
    return redirect("/trunks", ok="Route sortante supprimée.")
