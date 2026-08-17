"""Génération de la configuration Asterisk à partir de la base.

Principe : la base est la source de vérité, les fichiers de
/etc/asterisk/generated/ en sont une projection. On ne modifie jamais un
fichier généré à la main, et on ne lit jamais la base pendant un appel.

Cycle : build() -> diff() (aperçu) -> apply() (snapshot + écriture + reload).
Chaque apply() enregistre une révision, ce qui rend le retour arrière trivial.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import shlex
import sqlite3
import subprocess
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from . import asterisk, config, db, security

# Numéros d'urgence français : jamais bloqués par le filtrage sortant, et
# interdits comme numéro de poste interne (voir validate()).
EMERGENCY_NUMBERS = ["15", "17", "18", "112", "114", "115", "119", "196", "197"]

GENERATED_FILES = (
    "pjsip_endpoints.conf",
    "extensions_generated.conf",
    "voicemail_generated.conf",
)

_env = Environment(
    loader=FileSystemLoader(Path(__file__).parent / "confgen"),
    trim_blocks=True,
    lstrip_blocks=True,
    keep_trailing_newline=True,
    undefined=StrictUndefined,
    autoescape=False,  # on génère du .conf, pas du HTML
)

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,31}$")
EXTEN_RE = re.compile(r"^[0-9*#]{1,8}$")


# --- Lecture de la base, mise en forme pour les gabarits ---------------------

def _codec_list(raw: str) -> list[str]:
    return [c.strip() for c in (raw or "").split(",") if c.strip()]


def _device_context(kind: str) -> str:
    return "from-external-incoming" if kind == "fxo" else "from-internal"


def _device_template(kind: str) -> str:
    return "endpoint-trunk" if kind == "fxo" else "endpoint-internal"


def load_devices(conn: sqlite3.Connection, only_enabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM devices"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY COALESCE(extension, slug)"
    devices = []
    for row in conn.execute(sql):
        item = dict(row)
        item["codec_list"] = _codec_list(row["codecs"])
        item["context"] = _device_context(row["kind"])
        item["template"] = _device_template(row["kind"])
        devices.append(item)
    return devices


def load_users(conn: sqlite3.Connection, only_enabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM users"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY name"
    users = []
    for row in conn.execute(sql):
        item = dict(row)
        slugs = [
            r["slug"]
            for r in conn.execute(
                "SELECT d.slug FROM user_devices ud JOIN devices d ON d.id = ud.device_id "
                "WHERE ud.user_id = ? AND d.enabled = 1 ORDER BY d.slug",
                (row["id"],),
            )
        ]
        item["device_slugs"] = slugs
        item["dial_string"] = "&".join(f"PJSIP/{s}" for s in slugs)
        users.append(item)
    return users


def load_groups(conn: sqlite3.Connection, only_enabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM ring_groups"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY label"
    groups = []
    for row in conn.execute(sql):
        item = dict(row)
        slugs = [
            r["slug"]
            for r in conn.execute(
                "SELECT d.slug FROM ring_group_members m JOIN devices d ON d.id = m.device_id "
                "WHERE m.group_id = ? AND d.enabled = 1 ORDER BY d.slug",
                (row["id"],),
            )
        ]
        item["device_slugs"] = slugs
        item["dial_string"] = "&".join(f"PJSIP/{s}" for s in slugs)
        groups.append(item)
    return groups


def load_trunks(conn: sqlite3.Connection, only_enabled: bool = True) -> list[dict]:
    sql = "SELECT * FROM trunks"
    if only_enabled:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY priority, id"
    trunks = [dict(r) for r in conn.execute(sql)]

    # Chaînage des étiquettes de saut : chaque trunk connaît le suivant, le
    # dernier retombe sur `nomore`. C'est ce qui produit le failover.
    for index, trunk in enumerate(trunks):
        trunk["label_id"] = f"trunk{index + 1}"
        trunk["next_label"] = (
            f"trunk{index + 2}" if index + 1 < len(trunks) else "nomore"
        )
        trunk["dial_expr"] = trunk["dial_template"].replace("{num}", "${NUM}")
    return trunks


def load_speed_dials(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute("SELECT * FROM speed_dials WHERE enabled = 1 ORDER BY code")
    ]


def load_mailboxes(conn: sqlite3.Connection, settings: dict[str, str]) -> list[dict]:
    """Boîtes vocales : celles des postes, celles des personnes, la générale."""
    boxes: dict[str, dict] = {}

    for row in conn.execute(
        "SELECT mailbox, label FROM devices WHERE enabled = 1 AND mailbox IS NOT NULL AND mailbox != ''"
    ):
        boxes.setdefault(row["mailbox"], {"box": row["mailbox"], "name": row["label"],
                                          "pin": "", "email": ""})

    for row in conn.execute(
        "SELECT voicemail_box, voicemail_pin, name, email FROM users "
        "WHERE enabled = 1 AND voicemail_box IS NOT NULL AND voicemail_box != ''"
    ):
        boxes[row["voicemail_box"]] = {
            "box": row["voicemail_box"],
            "name": row["name"],
            "pin": row["voicemail_pin"] or "",
            "email": row["email"] or "",
        }

    for row in conn.execute(
        "SELECT voicemail_box, label FROM ring_groups "
        "WHERE enabled = 1 AND voicemail_box IS NOT NULL AND voicemail_box != ''"
    ):
        boxes.setdefault(row["voicemail_box"], {"box": row["voicemail_box"],
                                                "name": row["label"], "pin": "", "email": ""})

    general = settings.get("general_voicemail", "")
    if general:
        boxes.setdefault(general, {"box": general, "name": "General", "pin": "", "email": ""})

    # Les PIN manquants (boîtes de poste ou de groupe) sont complétés par les
    # PIN déjà stockés côté device, sinon par une valeur neutre non triviale.
    for box in boxes.values():
        if not box["pin"]:
            box["pin"] = _stable_pin(box["box"])

    return sorted(boxes.values(), key=lambda b: b["box"])


def _stable_pin(box: str) -> str:
    """PIN de repli déterministe, pour ne pas changer à chaque génération.

    Un PIN qui change à chaque `Appliquer` rendrait la messagerie inutilisable ;
    on dérive donc une valeur stable de l'identifiant de boîte. Ces boîtes
    (postes, groupes) sont des boîtes partagées de la maison, pas des boîtes
    personnelles — celles-ci ont un PIN tiré au hasard et stocké en base.
    """
    digits = "".join(ch for ch in box if ch.isdigit()) or "7"
    seed = sum(int(d) for d in digits)
    for round_number in range(1, 32):
        pin = f"{(seed * 3391 + 1279 * round_number) % 9000 + 1000}"
        if not security.is_trivial_pin(pin):
            return pin
    return "8347"  # inatteignable en pratique, mais on ne renvoie jamais None


# --- Construction du jeu de fichiers ----------------------------------------

def build(conn: sqlite3.Connection) -> dict[str, str]:
    """Rend les trois fichiers de configuration. Aucune écriture disque."""
    settings = db.get_settings(conn)
    devices = load_devices(conn)
    users = load_users(conn)
    groups = load_groups(conn)
    trunks = load_trunks(conn)

    family = next(
        (g for g in groups if g["slug"] == settings.get("ivr_family_group")), None
    )

    hotline_targets = sorted({
        d["hotline_target"]
        for d in devices
        if d["dial_mode"] == "hotline" and d["hotline_target"]
    })

    return {
        "pjsip_endpoints.conf": _env.get_template("pjsip_endpoints.conf.j2").render(
            devices=devices,
        ),
        "extensions_generated.conf": _env.get_template("extensions_generated.conf.j2").render(
            s=settings,
            devices=devices,
            users=users,
            groups=groups,
            trunks=trunks,
            speed_dials=load_speed_dials(conn),
            hotline_targets=hotline_targets,
            family_group=family,
            emergency_numbers=EMERGENCY_NUMBERS,
        ),
        "voicemail_generated.conf": _env.get_template("voicemail_generated.conf.j2").render(
            mailboxes=load_mailboxes(conn, settings),
            tonezone=settings.get("tonezone", "fr"),
        ),
    }


# --- Validation avant application -------------------------------------------

@dataclass
class Problem:
    level: str  # "error" bloque l'application, "warning" informe seulement
    message: str


def validate(conn: sqlite3.Connection) -> list[Problem]:
    problems: list[Problem] = []
    settings = db.get_settings(conn)
    devices = load_devices(conn)
    users = load_users(conn)
    groups = load_groups(conn)
    trunks = load_trunks(conn)

    for device in devices:
        if not SLUG_RE.match(device["slug"]):
            problems.append(Problem("error", f"Identifiant de poste invalide : {device['slug']}"))
        if not device["codec_list"]:
            problems.append(Problem("error", f"Poste {device['label']} : aucun codec"))

    # Un même numéro ne peut pas être servi par deux entrées du dialplan :
    # Asterisk prendrait silencieusement la première, ce qui est très pénible
    # à diagnostiquer une fois en production.
    used: dict[str, str] = {}
    def claim(number: str | None, owner: str) -> None:
        if not number:
            return
        if not EXTEN_RE.match(number):
            problems.append(Problem("error", f"Numéro invalide « {number} » ({owner})"))
            return
        if number in EMERGENCY_NUMBERS:
            problems.append(Problem(
                "error",
                f"{owner} utilise {number}, qui est un numéro d'urgence. "
                "Choisissez un autre numéro interne.",
            ))
            return
        if number in used:
            problems.append(Problem(
                "error", f"Numéro {number} attribué deux fois : {used[number]} et {owner}"
            ))
            return
        used[number] = owner

    for device in devices:
        claim(device["extension"], f"poste {device['label']}")
    for user in users:
        claim(user["extension"], f"personne {user['name']}")
    for group in groups:
        claim(group["extension"], f"groupe {group['label']}")
    for speed_dial in load_speed_dials(conn):
        claim(speed_dial["code"], f"abrégé {speed_dial['label'] or speed_dial['number']}")

    digits: dict[str, str] = {}
    for user in users:
        digit = user["menu_digit"]
        if not digit:
            continue
        if digit in digits:
            problems.append(Problem(
                "error", f"Touche {digit} du menu attribuée à {digits[digit]} et {user['name']}"
            ))
        digits[digit] = user["name"]
        if not user["dial_string"]:
            problems.append(Problem(
                "warning",
                f"{user['name']} est joignable au menu (touche {digit}) mais n'a aucun poste actif : "
                "l'appel basculera directement sur sa messagerie.",
            ))

    if not trunks:
        problems.append(Problem("warning", "Aucun trunk sortant actif : les appels vers "
                                           "l'extérieur échoueront."))
    for trunk in trunks:
        if "{num}" not in trunk["dial_template"]:
            problems.append(Problem(
                "error",
                f"Trunk {trunk['label']} : le gabarit doit contenir {{num}} "
                "(ex. PJSIP/{num}@grandstream-fxo)",
            ))

    family_slug = settings.get("ivr_family_group")
    if settings.get("ivr_enabled") == "1" and not any(g["slug"] == family_slug for g in groups):
        problems.append(Problem(
            "warning",
            f"L'IVR renvoie la touche 1 vers le groupe « {family_slug} », qui n'existe pas "
            "ou est désactivé.",
        ))

    for device in devices:
        if device["dial_mode"] == "hotline" and not device["hotline_target"]:
            problems.append(Problem(
                "error", f"Poste {device['label']} en mode hotline sans numéro de décroché"
            ))

    return problems


# --- Écriture, diff, application --------------------------------------------

def read_on_disk() -> dict[str, str]:
    current = {}
    for name in GENERATED_FILES:
        path = config.GENERATED_DIR / name
        current[name] = path.read_text(encoding="utf-8") if path.exists() else ""
    return current


def diff(bundle: dict[str, str]) -> str:
    """Diff unifié entre les fichiers en place et ceux qui seraient écrits."""
    current = read_on_disk()
    chunks = []
    for name in GENERATED_FILES:
        before = current.get(name, "").splitlines(keepends=True)
        after = bundle.get(name, "").splitlines(keepends=True)
        delta = list(difflib.unified_diff(
            before, after, fromfile=f"a/{name}", tofile=f"b/{name}", n=3
        ))
        if delta:
            chunks.append("".join(delta))
    return "\n".join(chunks)


def write_bundle(bundle: dict[str, str]) -> None:
    """Écrit les fichiers de façon atomique (rename), en 0640."""
    config.GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in bundle.items():
        target = config.GENERATED_DIR / name
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        os.chmod(tmp, 0o640)
        os.replace(tmp, target)


def regenerate_prompts(conn: sqlite3.Connection) -> str:
    """Régénère le message vocal du menu « joindre une personne » via Piper.

    Sans ça, ajouter quelqu'un depuis l'UI le rendrait joignable mais muet :
    l'annonce ne citerait pas son prénom.
    """
    if not config.TTS_ENABLED or not Path(config.TTS_SCRIPT).exists():
        return "TTS désactivé ou script absent : message du menu inchangé."

    users = [u for u in load_users(conn) if u["menu_digit"] and u["dial_string"]]
    if not users:
        return "Aucune personne au menu : message inchangé."

    users.sort(key=lambda u: u["menu_digit"])
    phrase = ". ".join(f"Tapez {u['menu_digit']} pour {u['name']}" for u in users)
    phrase += ". Étoile pour revenir au menu précédent."

    settings = db.get_settings(conn)
    name = settings.get("ivr_people_prompt", "custom/menu-personne").split("/")[-1]
    try:
        proc = subprocess.run(
            [config.TTS_SCRIPT, phrase, name],
            capture_output=True, text=True, timeout=120, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return f"Génération du message échouée : {exc}"
    if proc.returncode != 0:
        return f"Génération du message échouée : {(proc.stderr or proc.stdout).strip()}"
    return f"Message « {name} » régénéré : {shlex.quote(phrase)}"


def apply(conn: sqlite3.Connection, actor: str, summary: str) -> dict:
    """Génère, archive une révision, écrit sur disque puis recharge Asterisk."""
    problems = validate(conn)
    errors = [p for p in problems if p.level == "error"]
    if errors:
        return {
            "ok": False,
            "problems": problems,
            "log": "Application refusée : corrigez les erreurs ci-dessus.",
        }

    bundle = build(conn)
    write_bundle(bundle)

    logs: list[str] = []
    reload_ok = True
    if config.RELOAD_ENABLED:
        for result in asterisk.reload_all():
            logs.append(f"$ asterisk -rx {result.command!r}\n{result.output or '(pas de sortie)'}")
            reload_ok = reload_ok and result.ok
    else:
        logs.append("Rechargement désactivé (TELEPHONIE_RELOAD=0) : fichiers écrits seulement.")

    logs.append(regenerate_prompts(conn))
    log_text = "\n\n".join(logs)

    cursor = conn.execute(
        "INSERT INTO revisions (author, summary, bundle, applied, reload_log) "
        "VALUES (?, ?, ?, 1, ?)",
        (actor, summary, json.dumps(bundle, ensure_ascii=False), log_text),
    )
    db.audit(conn, actor, "apply", f"révision {cursor.lastrowid} — {summary}")
    _prune_revisions(conn)
    conn.commit()

    return {
        "ok": reload_ok,
        "problems": problems,
        "log": log_text,
        "revision_id": cursor.lastrowid,
    }


def rollback(conn: sqlite3.Connection, revision_id: int, actor: str) -> dict:
    """Réécrit les fichiers d'une révision passée et recharge."""
    row = db.one(conn, "SELECT * FROM revisions WHERE id = ?", (revision_id,))
    if row is None:
        return {"ok": False, "log": f"Révision {revision_id} introuvable."}

    bundle = json.loads(row["bundle"])
    write_bundle(bundle)

    logs = []
    reload_ok = True
    if config.RELOAD_ENABLED:
        for result in asterisk.reload_all():
            logs.append(f"$ asterisk -rx {result.command!r}\n{result.output or '(pas de sortie)'}")
            reload_ok = reload_ok and result.ok
    else:
        logs.append("Rechargement désactivé : fichiers écrits seulement.")

    db.audit(conn, actor, "rollback", f"retour à la révision {revision_id}")
    conn.commit()
    return {"ok": reload_ok, "log": "\n\n".join(logs)}


def is_dirty(conn: sqlite3.Connection) -> bool:
    """True si la base a divergé des fichiers actuellement en place."""
    try:
        return build(conn) != read_on_disk()
    except Exception:  # gabarit invalide : on force l'affichage de l'écran Appliquer
        return True


def _prune_revisions(conn: sqlite3.Connection) -> None:
    conn.execute(
        "DELETE FROM revisions WHERE id NOT IN ("
        "  SELECT id FROM revisions ORDER BY id DESC LIMIT ?"
        ")",
        (config.REVISIONS_KEPT,),
    )
