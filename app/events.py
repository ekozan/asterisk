"""Pont entre les événements d'Asterisk et Home Assistant, via MQTT.

Pourquoi un service séparé, et pas un `CURL()` dans le dialplan : parce que le
dialplan est dans le chemin de l'appel. Une requête HTTP synchrone vers Home
Assistant éteint ferait attendre Asterisk pendant que le téléphone devrait
sonner. Ici, Asterisk émet ses événements et ne sait pas qui écoute ; si ce
service ou le courtier MQTT tombe, les appels ne s'en aperçoivent pas.

Le découpage suit cette idée : tout ce qui décide quelque chose est une fonction
pure — `parse_message`, `Translator.translate` —, et la partie qui parle au
réseau ne fait que du transport. C'est aussi ce qui rend l'ensemble testable
sans Asterisk ni courtier.

Lancement : `python -m app.events` (voir systemd/telephonie-events.service).
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sqlite3
import time
from dataclasses import dataclass, field

from . import db

log = logging.getLogger("telephonie.events")

# --- Réglages, tous par variables d'environnement ---------------------------

AMI_HOST = os.environ.get("TELEPHONIE_AMI_HOST", "127.0.0.1")
AMI_PORT = int(os.environ.get("TELEPHONIE_AMI_PORT", "5038"))
AMI_USER = os.environ.get("TELEPHONIE_AMI_USER", "telephonie-events")
AMI_SECRET = os.environ.get("TELEPHONIE_AMI_SECRET", "")

MQTT_HOST = os.environ.get("TELEPHONIE_MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("TELEPHONIE_MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("TELEPHONIE_MQTT_USER", "")
MQTT_PASSWORD = os.environ.get("TELEPHONIE_MQTT_PASSWORD", "")

# Racine des sujets d'état, et racine de la découverte Home Assistant.
PREFIX = os.environ.get("TELEPHONIE_MQTT_PREFIX", "telephonie").strip("/")
DISCOVERY_PREFIX = os.environ.get(
    "TELEPHONIE_MQTT_DISCOVERY_PREFIX", "homeassistant"
).strip("/")

STATUS_TOPIC = f"{PREFIX}/status"

# États remontés par Asterisk, traduits pour l'affichage dans Home Assistant.
DEVICE_STATES = {
    "NOT_INUSE": "libre",
    "INUSE": "en ligne",
    "RINGING": "sonne",
    "RINGINUSE": "en ligne",
    "ONHOLD": "en attente",
    "BUSY": "occupé",
    "UNAVAILABLE": "injoignable",
    "INVALID": "inconnu",
    "UNKNOWN": "inconnu",
}

# `ContactStatus` dit si Asterisk sait où joindre l'appareil. C'est la réponse à
# « est-ce que le téléphone est branché », pas à « est-ce qu'il est occupé ».
REACHABLE = {"Reachable", "Created"}


# --- Protocole AMI ----------------------------------------------------------

def parse_message(block: str) -> dict[str, str]:
    """Transforme un bloc AMI en dictionnaire.

    Un message est une suite de `Clé: valeur`, terminée par une ligne vide. Les
    clés sont insensibles à la casse selon les versions d'Asterisk : on les
    normalise en minuscules pour ne pas dépendre de ça.
    """
    fields: dict[str, str] = {}
    for line in block.splitlines():
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip().lower()] = value.strip()
    return fields


class AmiStream:
    """Découpe un flux d'octets AMI en messages, sans supposer de découpage réseau.

    Les messages arrivent collés ou coupés au milieu selon la taille des
    paquets : on accumule et on ne rend que ce qui est complet.
    """

    def __init__(self) -> None:
        self._buffer = ""

    def feed(self, chunk: str) -> list[dict[str, str]]:
        self._buffer += chunk.replace("\r\n", "\n")
        messages = []
        while "\n\n" in self._buffer:
            block, _, self._buffer = self._buffer.partition("\n\n")
            fields = parse_message(block)
            if fields:
                messages.append(fields)
        return messages


def _endpoint_of(device: str) -> str | None:
    """`PJSIP/salon` ou `PJSIP/salon-00000012` → `salon`.

    Tout ce qui n'est pas un canal PJSIP est ignoré : les canaux `Local/`
    apparaissent par paires pour un seul appel réel et fausseraient l'état.
    """
    if not device.startswith("PJSIP/"):
        return None
    name = device.split("/", 1)[1]
    # Le suffixe d'instance de canal est un tiret suivi de huit chiffres hexa.
    head, dash, tail = name.rpartition("-")
    if dash and len(tail) == 8 and all(c in "0123456789abcdefABCDEF" for c in tail):
        return head
    return name


# --- Traduction vers MQTT ---------------------------------------------------

@dataclass(frozen=True)
class Publication:
    topic: str
    payload: str
    retain: bool = False


@dataclass
class Translator:
    """Convertit un événement AMI en publications MQTT.

    `devices` associe l'identifiant SIP à son libellé lisible. Un événement qui
    concerne un endpoint inconnu de la base est ignoré : c'est le cas des canaux
    techniques, et publier une entité par canal noierait Home Assistant.
    """

    devices: dict[str, str] = field(default_factory=dict)

    def translate(self, event: dict[str, str]) -> list[Publication]:
        name = event.get("event", "")
        if name == "DeviceStateChange":
            return self._device_state(event)
        if name == "ContactStatus":
            return self._contact_status(event)
        if name == "DialBegin":
            return self._dial_begin(event)
        return []

    def _device_state(self, event: dict[str, str]) -> list[Publication]:
        slug = _endpoint_of(event.get("device", ""))
        if slug not in self.devices:
            return []
        state = DEVICE_STATES.get(event.get("state", "").upper(), "inconnu")
        return [Publication(f"{PREFIX}/{slug}/etat", state, retain=True)]

    def _contact_status(self, event: dict[str, str]) -> list[Publication]:
        # `aor` porte le nom de l'AOR, qui est aussi celui de l'endpoint (voir
        # le générateur : c'est ce que le REGISTER exige).
        slug = event.get("aor") or event.get("endpointname", "")
        if slug not in self.devices:
            return []
        joignable = event.get("contactstatus", "") in REACHABLE
        return [Publication(
            f"{PREFIX}/{slug}/joignable", "ON" if joignable else "OFF", retain=True
        )]

    def _dial_begin(self, event: dict[str, str]) -> list[Publication]:
        """Un poste commence à sonner : c'est le signal que le satellite attend.

        `DialBegin` porte le canal appelant et le canal appelé. On publie sur le
        poste appelé, avec l'identité de l'appelant.
        """
        slug = _endpoint_of(event.get("destchannel", ""))
        if slug not in self.devices:
            return []

        numero = event.get("calleridnum", "")
        # Asterisk écrit `<unknown>` quand l'appelant est masqué ; le laisser
        # passer tel quel afficherait « <unknown> » sur le satellite vocal.
        if numero in ("<unknown>", "unknown"):
            numero = ""
        nom = event.get("calleridname", "")
        if nom in ("<unknown>", "unknown"):
            nom = ""

        payload = json.dumps({
            "event_type": "appel_entrant",
            "numero": numero,
            "nom": nom,
            "poste": self.devices[slug],
        }, ensure_ascii=False)
        # Sans `retain` : un événement rejoué au redémarrage du courtier
        # ferait sonner une notification pour un appel terminé depuis longtemps.
        return [Publication(f"{PREFIX}/{slug}/appel", payload, retain=False)]


# --- Découverte Home Assistant ----------------------------------------------

def _device_block() -> dict:
    """Bloc `device` commun : regroupe toutes les entités sous un seul appareil."""
    return {
        "identifiers": [PREFIX],
        "name": "Téléphonie maison",
        "manufacturer": "Asterisk",
        "model": "Téléphonie maison",
    }


def discovery(devices: dict[str, str]) -> list[Publication]:
    """Configurations de découverte, une par entité, retenues par le courtier.

    Retenues parce que Home Assistant les relit à chaque redémarrage : sans
    `retain`, les entités disparaîtraient jusqu'au prochain redémarrage d'ici.
    """
    out: list[Publication] = []
    for slug, label in sorted(devices.items()):
        common = {
            "device": _device_block(),
            "availability_topic": STATUS_TOPIC,
            "payload_available": "online",
            "payload_not_available": "offline",
        }

        out.append(Publication(
            f"{DISCOVERY_PREFIX}/binary_sensor/{PREFIX}/{slug}_joignable/config",
            json.dumps({
                **common,
                "name": f"{label} joignable",
                "unique_id": f"{PREFIX}_{slug}_joignable",
                "state_topic": f"{PREFIX}/{slug}/joignable",
                "device_class": "connectivity",
            }, ensure_ascii=False),
            retain=True,
        ))

        out.append(Publication(
            f"{DISCOVERY_PREFIX}/sensor/{PREFIX}/{slug}_etat/config",
            json.dumps({
                **common,
                "name": f"{label} état",
                "unique_id": f"{PREFIX}_{slug}_etat",
                "state_topic": f"{PREFIX}/{slug}/etat",
                "icon": "mdi:phone",
            }, ensure_ascii=False),
            retain=True,
        ))

        out.append(Publication(
            f"{DISCOVERY_PREFIX}/event/{PREFIX}/{slug}_appel/config",
            json.dumps({
                **common,
                "name": f"{label} appel entrant",
                "unique_id": f"{PREFIX}_{slug}_appel",
                "state_topic": f"{PREFIX}/{slug}/appel",
                "event_types": ["appel_entrant"],
                "icon": "mdi:phone-incoming",
            }, ensure_ascii=False),
            retain=True,
        ))
    return out


def load_device_labels(conn: sqlite3.Connection) -> dict[str, str]:
    """{identifiant SIP: libellé} des postes actifs."""
    return {
        row["slug"]: row["label"]
        for row in conn.execute("SELECT slug, label FROM devices WHERE enabled = 1")
    }


# --- Transport --------------------------------------------------------------

class AmiConnection:
    """Connexion AMI : identification, puis lecture des événements."""

    def __init__(self, host: str, port: int, user: str, secret: str) -> None:
        self.host, self.port, self.user, self.secret = host, port, user, secret
        self._socket: socket.socket | None = None
        self._stream = AmiStream()

    def connect(self) -> None:
        self._socket = socket.create_connection((self.host, self.port), timeout=10)
        self._socket.settimeout(30)
        self._send(f"Action: Login\r\nUsername: {self.user}\r\nSecret: {self.secret}\r\n\r\n")
        # La réponse peut arriver après la bannière, dans le même paquet ou dans
        # un autre : on lit jusqu'à l'obtenir plutôt que de parier sur le
        # découpage réseau. `_read` lève si la connexion se ferme.
        while True:
            for message in self._read():
                if message.get("response") == "Success":
                    log.info("AMI : connecté à %s:%s", self.host, self.port)
                    return
                if message.get("response") == "Error":
                    raise ConnectionError(
                        message.get("message", "identification refusée")
                    )

    def _send(self, text: str) -> None:
        assert self._socket is not None
        self._socket.sendall(text.encode("utf-8"))

    def _read(self) -> list[dict[str, str]]:
        assert self._socket is not None
        chunk = self._socket.recv(65536)
        if not chunk:
            raise ConnectionError("AMI : connexion fermée par Asterisk")
        return self._stream.feed(chunk.decode("utf-8", errors="replace"))

    def events(self):
        """Générateur d'événements, jusqu'à rupture de la connexion."""
        last_ping = time.monotonic()
        while True:
            try:
                for message in self._read():
                    yield message
            except socket.timeout:
                # Silence prolongé : on vérifie que le lien est encore vivant
                # plutôt que d'attendre indéfiniment un événement qui ne
                # viendra pas.
                pass
            if time.monotonic() - last_ping > 30:
                self._send("Action: Ping\r\n\r\n")
                last_ping = time.monotonic()

    def close(self) -> None:
        if self._socket is not None:
            try:
                self._socket.close()
            finally:
                self._socket = None


def _mqtt_client():
    """Client MQTT configuré, avec testament : le courtier annonce notre panne.

    Sans `will_set`, un arrêt brutal de ce service laisserait Home Assistant
    afficher les derniers états connus comme s'ils étaient à jour.
    """
    import paho.mqtt.client as mqtt  # import tardif : l'UI n'en a pas besoin

    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2, client_id=f"{PREFIX}-events"
    )
    if MQTT_USER:
        client.username_pw_set(MQTT_USER, MQTT_PASSWORD)
    client.will_set(STATUS_TOPIC, "offline", retain=True)
    return client


def run() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )
    if not MQTT_HOST:
        raise SystemExit(
            "TELEPHONIE_MQTT_HOST n'est pas défini : renseignez "
            "/etc/telephonie/events.env (voir docs/11-home-assistant.md)."
        )

    with db.connect() as conn:
        devices = load_device_labels(conn)
    log.info("%d postes suivis : %s", len(devices), ", ".join(sorted(devices)))

    client = _mqtt_client()
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    for publication in discovery(devices):
        client.publish(publication.topic, publication.payload, retain=publication.retain)
    client.publish(STATUS_TOPIC, "online", retain=True)

    translator = Translator(devices)
    backoff = 1
    try:
        while True:
            ami = AmiConnection(AMI_HOST, AMI_PORT, AMI_USER, AMI_SECRET)
            try:
                ami.connect()
                backoff = 1
                for event in ami.events():
                    for publication in translator.translate(event):
                        client.publish(
                            publication.topic, publication.payload,
                            retain=publication.retain,
                        )
            except (OSError, ConnectionError) as exc:
                # Asterisk redémarre, ou n'est pas encore prêt : on retente sans
                # jamais abandonner, l'appareil MQTT restant marqué indisponible.
                log.warning("AMI : %s — nouvelle tentative dans %ss", exc, backoff)
                client.publish(STATUS_TOPIC, "offline", retain=True)
                time.sleep(backoff)
                backoff = min(backoff * 2, 60)
            finally:
                ami.close()
    finally:
        client.publish(STATUS_TOPIC, "offline", retain=True)
        client.loop_stop()
        client.disconnect()


if __name__ == "__main__":
    run()
