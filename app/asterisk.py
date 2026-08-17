"""Dialogue avec Asterisk via la CLI (`asterisk -rx`).

Pourquoi pas l'AMI : le service tourne déjà sous l'utilisateur `asterisk`, il a
donc accès à /var/run/asterisk/asterisk.ctl sans identifiant supplémentaire.
Supprimer l'AMI, c'est supprimer un port en écoute, un mot de passe à gérer et
une section de manager.conf à maintenir — pour exactement les mêmes actions.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from dataclasses import dataclass

from . import config


@dataclass
class CommandResult:
    command: str
    ok: bool
    output: str


def run_cli(command: str, timeout: int = 15) -> CommandResult:
    """Exécute une commande CLI Asterisk et retourne son résultat."""
    try:
        proc = subprocess.run(
            [config.ASTERISK_BIN, "-rx", command],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except FileNotFoundError:
        return CommandResult(command, False, f"binaire introuvable : {config.ASTERISK_BIN}")
    except subprocess.TimeoutExpired:
        return CommandResult(command, False, "délai dépassé")
    except OSError as exc:  # permissions sur le socket de contrôle, etc.
        return CommandResult(command, False, str(exc))

    output = (proc.stdout + proc.stderr).strip()
    # `asterisk -rx` renvoie 0 même quand Asterisk n'est pas démarré : on
    # détecte le cas sur le message d'erreur du client CLI.
    ok = proc.returncode == 0 and "Unable to connect" not in output
    return CommandResult(command, ok, output)


def is_running() -> bool:
    return run_cli("core show version", timeout=5).ok


def reload_all() -> list[CommandResult]:
    """Recharge les sous-systèmes impactés par les fichiers générés.

    Ces trois rechargements sont non destructifs : les appels en cours ne sont
    pas coupés, et les endpoints déjà enregistrés le restent.
    """
    return [
        run_cli("dialplan reload", timeout=30),
        run_cli("pjsip reload", timeout=30),
        run_cli("voicemail reload", timeout=30),
    ]


_ENDPOINT_LINE = re.compile(
    r"^\s*Endpoint:\s+(?P<name>\S+?)\s+(?P<state>\S+)\s+(?P<channels>\d+)\s+of",
)


def endpoint_status() -> dict[str, str]:
    """État PJSIP de chaque endpoint : {slug: 'Not in use' | 'Unavailable' | ...}.

    Retourne un dictionnaire vide si Asterisk est injoignable — l'UI affiche
    alors « état inconnu » plutôt que de laisser croire que tout est éteint.
    """
    result = run_cli("pjsip show endpoints")
    if not result.ok:
        return {}

    states: dict[str, str] = {}
    for line in result.output.splitlines():
        match = _ENDPOINT_LINE.match(line)
        if match:
            states[match.group("name")] = match.group("state")
    return states


def active_channels() -> int | None:
    """Nombre d'appels actifs, ou None si Asterisk est injoignable."""
    result = run_cli("core show channels count")
    if not result.ok:
        return None
    match = re.search(r"(\d+)\s+active call", result.output)
    return int(match.group(1)) if match else 0


def originate(channel: str, context: str, extension: str) -> CommandResult:
    """Lance un appel sortant (click-to-call, notification vocale).

    Tout le paramétrage passe par l'extension appelée plutôt que par des
    variables de canal : `channel originate` ne sait pas transmettre de
    variables, et une variable globale posée juste avant l'appel serait
    écrasée par un second appel simultané.
    """
    target = f"{extension}@{context}"
    return run_cli(f"channel originate {shlex.quote(channel)} extension {shlex.quote(target)}",
                   timeout=20)
