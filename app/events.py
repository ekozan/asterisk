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
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

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


@dataclass(frozen=True)
class Poste:
    """Un endpoint de la base, tel qu'il compte pour ce service.

    `trunk` sépare les postes de la maison des passerelles vers l'extérieur
    (pont FXO, GSM). Sans cette distinction, un appel venu de la Freebox
    arriverait par un canal connu de la base et passerait pour un appel interne.
    """

    label: str
    trunk: bool


def _horodatage() -> str:
    """Instant local avec décalage, au format ISO 8601.

    Estampillé à la réception de l'événement, pas par Asterisk : l'AMI ne
    date ses messages que si `timestampevents` est activé, et le trajet par le
    socket local se compte en fractions de milliseconde.
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


def number_from_dialstring(dialstring: str) -> str:
    """Extrait le numéro composé de la chaîne de `Dial()`.

    Asterisk ne publie pas le numéro sortant dans un champ à lui : il faut le
    tirer de `DialString`, dont la forme dépend de la technologie du trunk.

        0102030405@grandstream-fxo   (PJSIP vers le pont FXO)  -> 0102030405
        quectel0/+33102030405        (chan-quectel vers le GSM) -> +33102030405

    Si vous ajoutez un trunk d'une autre technologie, vérifiez ce que produit
    `python -m app.events --dump` avant de vous fier à ce qui remonte.
    """
    number = dialstring.split("@", 1)[0]
    return number.rsplit("/", 1)[-1].strip()


def _clean_identity(value: str) -> str:
    """Asterisk écrit `<unknown>` pour un appelant masqué.

    Le laisser passer ferait annoncer « appel de inférieur unknown supérieur »
    par un satellite vocal ; une chaîne vide est plus facile à tester.
    """
    return "" if value in ("<unknown>", "unknown", "") else value


@dataclass
class Translator:
    """Convertit un événement AMI en publications MQTT.

    Un événement qui ne concerne aucun endpoint de la base est ignoré : les
    canaux techniques sont nombreux, et publier une entité par canal noierait
    Home Assistant.
    """

    postes: dict[str, Poste] = field(default_factory=dict)
    now: Callable[[], str] = _horodatage
    #: Un appel sortant qui bascule d'un trunk à l'autre produit un second
    #: `DialBegin` avec le même canal appelant. On ne l'annonce qu'une fois.
    fenetre_doublon: float = 60.0
    _vus: dict[tuple[str, str], float] = field(default_factory=dict, repr=False)

    # --- aiguillage ---

    def translate(self, event: dict[str, str]) -> list[Publication]:
        name = event.get("event", "")
        if name == "DeviceStateChange":
            return self._device_state(event)
        if name == "ContactStatus":
            return self._contact_status(event)
        if name == "DialBegin":
            return self._appel(event)
        return []

    def _maison(self, slug: str | None) -> bool:
        poste = self.postes.get(slug or "")
        return poste is not None and not poste.trunk

    # --- états ---

    def _device_state(self, event: dict[str, str]) -> list[Publication]:
        slug = _endpoint_of(event.get("device", ""))
        if slug not in self.postes:
            return []
        state = DEVICE_STATES.get(event.get("state", "").upper(), "inconnu")
        return [Publication(f"{PREFIX}/{slug}/etat", state, retain=True)]

    def _contact_status(self, event: dict[str, str]) -> list[Publication]:
        # `aor` porte le nom de l'AOR, qui est aussi celui de l'endpoint (voir
        # le générateur : c'est ce que le REGISTER exige).
        slug = event.get("aor") or event.get("endpointname", "")
        if slug not in self.postes:
            return []
        joignable = event.get("contactstatus", "") in REACHABLE
        return [Publication(
            f"{PREFIX}/{slug}/joignable", "ON" if joignable else "OFF", retain=True
        )]

    # --- appels ---

    def _appel(self, event: dict[str, str]) -> list[Publication]:
        """Un `Dial()` démarre : quelqu'un sonne quelque part.

        Le sens se déduit des deux extrémités. C'est plus robuste que de
        reconnaître les noms de canaux des trunks, qui changent avec la
        technologie employée.
        """
        appelant = _endpoint_of(event.get("channel", ""))
        appele = _endpoint_of(event.get("destchannel", ""))

        if self._maison(appele) and not self._maison(appelant):
            sens, slug = "entrant", appele
            numero = _clean_identity(event.get("calleridnum", ""))
            nom = _clean_identity(event.get("calleridname", ""))
        elif self._maison(appelant) and not self._maison(appele):
            sens, slug = "sortant", appelant
            numero = number_from_dialstring(event.get("dialstring", ""))
            nom = ""
        elif self._maison(appelant) and self._maison(appele):
            # Poste à poste : on annonce sur celui qui sonne, pas sur celui qui
            # compose — c'est le téléphone dont on veut être prévenu.
            sens, slug = "interne", appele
            numero = _clean_identity(event.get("calleridnum", ""))
            nom = _clean_identity(event.get("calleridname", ""))
        else:
            return []

        if self._deja_vu(event.get("uniqueid", ""), slug):
            return []

        payload = json.dumps({
            "event_type": sens,
            "numero": numero,
            "nom": nom,
            "poste": self.postes[slug].label,
            "horodatage": self.now(),
        }, ensure_ascii=False)
        # Sans `retain` : un événement rejoué au redémarrage du courtier ferait
        # annoncer un appel terminé depuis longtemps.
        return [Publication(f"{PREFIX}/{slug}/appel", payload, retain=False)]

    def _deja_vu(self, uniqueid: str, slug: str) -> bool:
        """Vrai si ce même appel a déjà été annoncé pour ce poste.

        La clé associe l'appel et le poste concerné, et ce couple fait
        exactement ce qu'il faut dans les deux cas : le basculement d'un trunk
        vers le suivant garde le même canal appelant, donc le même couple, et
        n'est annoncé qu'une fois ; un groupe qui fait sonner trois postes
        produit trois couples distincts, donc trois annonces — une par
        téléphone qui sonne réellement.
        """
        if not uniqueid:
            return False
        maintenant = time.monotonic()
        self._vus = {
            cle: vu for cle, vu in self._vus.items()
            if maintenant - vu < self.fenetre_doublon
        }
        cle = (uniqueid, slug)
        if cle in self._vus:
            return True
        self._vus[cle] = maintenant
        return False


# --- Découverte Home Assistant ----------------------------------------------

def _device_block() -> dict:
    """Bloc `device` commun : regroupe toutes les entités sous un seul appareil."""
    return {
        "identifiers": [PREFIX],
        "name": "Téléphonie maison",
        "manufacturer": "Asterisk",
        "model": "Téléphonie maison",
    }


def discovery(postes: dict[str, Poste]) -> list[Publication]:
    """Configurations de découverte, une par entité, retenues par le courtier.

    Retenues parce que Home Assistant les relit à chaque redémarrage : sans
    `retain`, les entités disparaîtraient jusqu'au prochain lancement d'ici.
    """
    out: list[Publication] = []
    for slug, poste in sorted(postes.items()):
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
                "name": f"{poste.label} joignable",
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
                "name": f"{poste.label} état",
                "unique_id": f"{PREFIX}_{slug}_etat",
                "state_topic": f"{PREFIX}/{slug}/etat",
                "icon": "mdi:phone",
            }, ensure_ascii=False),
            retain=True,
        ))

        # Un trunk ne « reçoit » ni ne « passe » d'appel de son propre point de
        # vue : il n'est jamais le poste d'un événement, donc pas d'entité.
        if poste.trunk:
            continue

        out.append(Publication(
            f"{DISCOVERY_PREFIX}/event/{PREFIX}/{slug}_appel/config",
            json.dumps({
                **common,
                "name": f"{poste.label} appel",
                "unique_id": f"{PREFIX}_{slug}_appel",
                "state_topic": f"{PREFIX}/{slug}/appel",
                "event_types": ["entrant", "sortant", "interne"],
                "icon": "mdi:phone-in-talk",
            }, ensure_ascii=False),
            retain=True,
        ))
    return out


def load_postes(conn: sqlite3.Connection) -> dict[str, Poste]:
    """Les endpoints actifs, avec ce qui distingue un poste d'une passerelle."""
    return {
        row["slug"]: Poste(label=row["label"], trunk=row["kind"] == "fxo")
        for row in conn.execute("SELECT slug, label, kind FROM devices WHERE enabled = 1")
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


def _connexion_ami() -> AmiConnection:
    return AmiConnection(AMI_HOST, AMI_PORT, AMI_USER, AMI_SECRET)


def dump() -> None:
    """Affiche les événements bruts au lieu de publier — `--dump`.

    Les noms de champs de l'AMI varient d'une version d'Asterisk à l'autre, et
    `DialString` dépend de la technologie du trunk. Plutôt que de faire
    confiance à la documentation, on regarde ce que la machine émet vraiment :
    passez un appel dans chaque sens et lisez.
    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    interessants = {"DialBegin", "DialEnd", "DeviceStateChange", "ContactStatus"}

    ami = _connexion_ami()
    ami.connect()
    print("Passez un appel entrant puis un appel sortant. Ctrl-C pour arrêter.\n")
    try:
        for event in ami.events():
            if event.get("event") in interessants:
                print(f"--- {event['event']}")
                for key, value in event.items():
                    if key != "event":
                        print(f"    {key:24} {value}")
    except KeyboardInterrupt:
        pass
    finally:
        ami.close()


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
        postes = load_postes(conn)
    maison = sorted(s for s, p in postes.items() if not p.trunk)
    log.info("%d postes suivis (%s) et %d passerelles",
             len(maison), ", ".join(maison), len(postes) - len(maison))

    client = _mqtt_client()
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_start()

    for publication in discovery(postes):
        client.publish(publication.topic, publication.payload, retain=publication.retain)
    client.publish(STATUS_TOPIC, "online", retain=True)

    translator = Translator(postes)
    backoff = 1
    try:
        while True:
            ami = _connexion_ami()
            try:
                ami.connect()
                backoff = 1
                client.publish(STATUS_TOPIC, "online", retain=True)
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
    import sys

    dump() if "--dump" in sys.argv else run()
