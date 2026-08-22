"""Édition des fichiers de configuration Asterisk écrits à la main.

Une installation a deux moitiés : `generated/*.conf`, projection de la base, et
les fichiers ci-dessous, écrits par un humain. L'interface ne savait éditer que
la première, ce qui obligeait à passer par SSH pour tout ce que le schéma ne
prévoit pas — un transport, un gabarit d'endpoint, un contexte de dialplan.

Trois règles tiennent cet écran :

  - **Le client ne transmet jamais de chemin**, seulement une clé de `EDITABLE`.
    Un champ de formulaire ne peut donc pas désigner `/etc/shadow`.
  - **Les fichiers générés n'y figurent pas.** Les éditer serait sans effet : le
    prochain « Appliquer » les réécrit sans prévenir, et le travail disparaît.
  - **Le contenu précédent est archivé avant chaque écriture**, ce qui rend le
    retour arrière possible même quand Asterisk refuse de recharger.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from . import asterisk, config, db


@dataclass(frozen=True)
class EditableFile:
    filename: str
    reload_command: str  # vide = pas de rechargement à chaud possible
    description: str


# Les fichiers déployés par scripts/install.sh --with-asterisk-conf, plus
# modules.conf qui n'est pas déployé mais se règle à la main.
EDITABLE: dict[str, EditableFile] = {
    "pjsip": EditableFile(
        "pjsip.conf", "pjsip reload",
        "Transports SIP, gabarits d'endpoint, sécurité des appels anonymes.",
    ),
    "extensions": EditableFile(
        "extensions.conf", "dialplan reload",
        "Contextes statiques et inclusion du dialplan généré.",
    ),
    "voicemail": EditableFile(
        "voicemail.conf", "voicemail reload",
        "Réglages généraux de la messagerie vocale.",
    ),
    "logger": EditableFile(
        "logger.conf", "logger reload",
        "Niveaux de journalisation et fichiers produits.",
    ),
    "rtp": EditableFile(
        "rtp.conf", "module reload res_rtp_asterisk",
        "Plage de ports RTP, à ouvrir dans le pare-feu.",
    ),
    "cdr": EditableFile(
        "cdr.conf", "module reload cdr",
        "Journal d'appels : ce que l'écran « Journal » lit.",
    ),
    "cdr_manager": EditableFile(
        "cdr_manager.conf", "module reload cdr_manager",
        "Publication des enregistrements d'appel sur l'AMI, pour le pont "
        "Home Assistant.",
    ),
    "manager": EditableFile(
        "manager.conf", "manager reload",
        "Interface AMI, utilisée par le service d'événements.",
    ),
    "modules": EditableFile(
        "modules.conf", "",
        "Modules chargés au démarrage. Aucun rechargement à chaud : "
        "il faut redémarrer Asterisk.",
    ),
}

# Nombre d'états conservés par fichier.
HISTORY_KEPT = 20

_SECTION_RE = re.compile(r"^\[[^\]\[]+\]\s*(\([^)]*\))?\s*$")


def path_for(key: str) -> Path:
    """Chemin absolu du fichier, ou KeyError si la clé n'est pas au catalogue."""
    return config.ASTERISK_DIR / EDITABLE[key].filename


def read(key: str) -> str:
    path = path_for(key)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"; lecture impossible : {exc}\n"


def describe_all() -> list[dict]:
    """État de chaque fichier éditable, pour la liste de l'écran."""
    out = []
    for key, spec in EDITABLE.items():
        path = path_for(key)
        try:
            stat = path.stat()
            exists, size, mtime = True, stat.st_size, time.strftime(
                "%Y-%m-%d %H:%M", time.localtime(stat.st_mtime)
            )
        except OSError:
            exists, size, mtime = False, 0, ""
        out.append({
            "key": key, "filename": spec.filename, "description": spec.description,
            "reload_command": spec.reload_command,
            "exists": exists, "size": size, "mtime": mtime,
            "writable": os.access(path if exists else path.parent, os.W_OK),
        })
    return out


# --- Contrôle de forme avant écriture ---------------------------------------

def check_syntax(text: str) -> list[str]:
    """Erreurs de forme évidentes, une par ligne fautive.

    Asterisk n'a pas de mode « vérifier sans charger » : une erreur de syntaxe
    ne se découvre qu'au rechargement, et se traduit le plus souvent par un
    module qui décline en silence. Ce contrôle attrape les fautes de frappe
    courantes avant qu'elles atteignent le disque. Il ne remplace pas le
    rechargement : il évite seulement de casser la configuration pour un
    crochet oublié.
    """
    errors: list[str] = []
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(";"):
            continue
        if line.startswith("["):
            if not _SECTION_RE.match(line):
                errors.append(f"ligne {number} : en-tête de section mal formé « {line} »")
            continue
        if line.startswith("#"):
            directive = line.split()[0].lower()
            if directive not in ("#include", "#exec"):
                errors.append(f"ligne {number} : directive inconnue « {directive} »")
            continue
        if "=" not in line:
            errors.append(
                f"ligne {number} : « {line} » n'est ni un commentaire, ni une section, "
                "ni une affectation"
            )
    return errors


# --- Journal d'Asterisk, lu autour du rechargement --------------------------

def _log_size() -> int:
    try:
        return config.ASTERISK_LOG.stat().st_size
    except OSError:
        return 0


def _log_since(offset: int) -> str:
    """Lignes ajoutées au journal depuis `offset`.

    Si le fichier a rétréci entre-temps (rotation), on repart de zéro plutôt
    que de lire à côté.
    """
    try:
        with config.ASTERISK_LOG.open("rb") as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(0 if size < offset else offset)
            return handle.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _complaints(log_text: str, filename: str) -> list[str]:
    """Lignes du journal qui reprochent quelque chose à ce fichier précis."""
    return [
        line.strip()
        for line in log_text.splitlines()
        if filename in line and ("ERROR" in line or "WARNING" in line)
    ]


# --- Écriture ---------------------------------------------------------------

def _write(path: Path, content: str) -> None:
    """Écriture atomique, mêmes droits que les fichiers générés."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.chmod(tmp, 0o640)
    os.replace(tmp, path)


def save(conn: sqlite3.Connection, key: str, content: str, actor: str) -> dict:
    """Archive l'état courant, écrit, recharge — et restaure si ça se passe mal.

    Le retour porte `ok`, `message`, et `log` (sortie du rechargement et lignes
    de journal retenues), que l'écran affiche tel quel.
    """
    spec = EDITABLE[key]
    path = path_for(key)

    content = content.replace("\r\n", "\n")
    if content and not content.endswith("\n"):
        content += "\n"

    previous = read(key)
    if content == previous:
        return {"ok": True, "message": "Aucune modification à enregistrer.", "log": ""}

    errors = check_syntax(content)
    if errors:
        return {
            "ok": False,
            "message": "Rien n'a été écrit : " + errors[0],
            "log": "\n".join(errors),
        }

    # L'archive part avant l'écriture : si le processus meurt entre les deux,
    # on préfère une archive en trop qu'un état perdu.
    conn.execute(
        "INSERT INTO file_revisions (author, filename, content) VALUES (?, ?, ?)",
        (actor, spec.filename, previous),
    )
    _prune_history(conn, spec.filename)
    db.audit(conn, actor, "file-edit", spec.filename)
    conn.commit()

    try:
        _write(path, content)
    except OSError as exc:
        return {
            "ok": False,
            "message": f"Écriture refusée sur {path} : {exc}. Le service a-t-il le droit "
                       "d'écrire dans /etc/asterisk (ReadWritePaths) ?",
            "log": "",
        }

    if not config.RELOAD_ENABLED or not spec.reload_command:
        note = ("Fichier écrit. Aucun rechargement à chaud pour ce fichier : "
                "redémarrez Asterisk pour l'activer.") if not spec.reload_command else \
               "Fichier écrit. Rechargement désactivé (TELEPHONIE_RELOAD=0)."
        return {"ok": True, "message": note, "log": ""}

    offset = _log_size()
    result = asterisk.run_cli(spec.reload_command, timeout=30)
    time.sleep(0.4)  # laisse Asterisk finir d'écrire ses éventuelles plaintes
    complaints = _complaints(_log_since(offset), spec.filename)

    if result.ok and not complaints:
        return {
            "ok": True,
            "message": f"{spec.filename} enregistré et rechargé.",
            "log": result.output,
        }

    # Asterisk n'a pas voulu de cette version : on remet la précédente plutôt
    # que de laisser l'installation dans un état que personne n'a choisi.
    detail = "\n".join(complaints) or result.output or "rechargement en échec"
    try:
        _write(path, previous)
        asterisk.run_cli(spec.reload_command, timeout=30)
        restored = "La version précédente a été remise en place et rechargée."
    except OSError as exc:
        restored = (f"ATTENTION : la version précédente n'a pas pu être remise ({exc}). "
                    "Le fichier sur disque est celui que vous venez d'enregistrer.")

    return {
        "ok": False,
        "message": f"Asterisk a refusé cette version de {spec.filename}. {restored}",
        "log": detail,
    }


def restore(conn: sqlite3.Connection, revision_id: int, actor: str) -> dict:
    """Réécrit un contenu archivé, en repassant par toute la chaîne de save()."""
    row = db.one(conn, "SELECT * FROM file_revisions WHERE id = ?", (revision_id,))
    if row is None:
        return {"ok": False, "message": "Version introuvable.", "log": ""}

    key = next((k for k, spec in EDITABLE.items() if spec.filename == row["filename"]), None)
    if key is None:
        return {"ok": False, "message": f"{row['filename']} n'est plus éditable.", "log": ""}

    result = save(conn, key, row["content"], actor)
    if result["ok"]:
        result["message"] = f"Version du {row['created_at']} restaurée. " + result["message"]
    return result


def history(conn: sqlite3.Connection, filename: str) -> list[sqlite3.Row]:
    return db.query(
        conn,
        "SELECT id, created_at, author, length(content) AS taille FROM file_revisions "
        "WHERE filename = ? ORDER BY id DESC",
        (filename,),
    )


def _prune_history(conn: sqlite3.Connection, filename: str) -> None:
    conn.execute(
        "DELETE FROM file_revisions WHERE filename = ? AND id NOT IN ("
        "  SELECT id FROM file_revisions WHERE filename = ? ORDER BY id DESC LIMIT ?"
        ")",
        (filename, filename, HISTORY_KEPT),
    )
