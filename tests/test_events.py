"""Tests du pont Asterisk → Home Assistant.

Ce service ne tient qu'à une chose : traduire correctement des événements qu'on
ne contrôle pas. Les tests portent donc sur du texte AMI réel, tel qu'Asterisk
l'émet, et sur ce qui en sort.
"""

from __future__ import annotations

import json

import pytest

from app import events


@pytest.fixture()
def translator():
    return events.Translator({
        "salon": "Salon",
        "etage": "Étage",
        "grandstream-fxo": "Pont Freebox",
    })


def _by_topic(publications):
    return {p.topic: p for p in publications}


# --- Découpage du flux AMI --------------------------------------------------

def test_le_flux_est_decoupe_meme_quand_les_messages_arrivent_colles():
    stream = events.AmiStream()
    messages = stream.feed(
        "Event: DeviceStateChange\r\nDevice: PJSIP/salon\r\nState: RINGING\r\n\r\n"
        "Event: DeviceStateChange\r\nDevice: PJSIP/etage\r\nState: INUSE\r\n\r\n"
    )
    assert [m["device"] for m in messages] == ["PJSIP/salon", "PJSIP/etage"]


def test_un_message_coupe_en_deux_paquets_est_reconstitue():
    """TCP ne garantit aucun découpage : un événement peut arriver en morceaux."""
    stream = events.AmiStream()
    assert stream.feed("Event: DeviceStateChange\r\nDevi") == []
    messages = stream.feed("ce: PJSIP/salon\r\nState: RINGING\r\n\r\n")
    assert messages == [{
        "event": "DeviceStateChange", "device": "PJSIP/salon", "state": "RINGING",
    }]


def test_les_cles_sont_insensibles_a_la_casse():
    assert events.parse_message("EVENT: Ping\r\nActionID: 3\r\n") == {
        "event": "Ping", "actionid": "3",
    }


def test_une_valeur_contenant_un_deux_points_reste_entiere():
    """`Uri: sip:salon@10.0.90.31:5060` ne doit pas être tronqué au premier « : »."""
    parsed = events.parse_message("Uri: sip:salon@10.0.90.31:5060\r\n")
    assert parsed["uri"] == "sip:salon@10.0.90.31:5060"


# --- Identification du poste ------------------------------------------------

@pytest.mark.parametrize("canal, attendu", [
    ("PJSIP/salon", "salon"),
    ("PJSIP/salon-00000012", "salon"),
    # Un identifiant qui contient un tiret : le suffixe d'instance ne doit pas
    # être confondu avec la fin du nom.
    ("PJSIP/grandstream-fxo", "grandstream-fxo"),
    ("PJSIP/grandstream-fxo-0000001a", "grandstream-fxo"),
    # Les canaux Local apparaissent par paires pour un seul appel réel.
    ("Local/100@from-internal-00000001;1", None),
    ("Quectel/quectel0", None),
])
def test_extraction_du_poste_depuis_le_canal(canal, attendu):
    assert events._endpoint_of(canal) == attendu


# --- Traduction -------------------------------------------------------------

def test_changement_d_etat_publie_un_libelle_lisible(translator):
    publications = translator.translate({
        "event": "DeviceStateChange", "device": "PJSIP/salon", "state": "RINGING",
    })
    assert len(publications) == 1
    assert publications[0].topic == "telephonie/salon/etat"
    assert publications[0].payload == "sonne"
    # Retenu : Home Assistant doit connaître l'état au redémarrage, sans
    # attendre le prochain changement.
    assert publications[0].retain


def test_un_etat_inconnu_ne_fait_pas_planter(translator):
    publications = translator.translate({
        "event": "DeviceStateChange", "device": "PJSIP/salon", "state": "CE_QUE_VOUS_VOULEZ",
    })
    assert publications[0].payload == "inconnu"


def test_enregistrement_publie_la_joignabilite(translator):
    reachable = translator.translate({
        "event": "ContactStatus", "aor": "salon", "contactstatus": "Reachable",
    })
    unreachable = translator.translate({
        "event": "ContactStatus", "aor": "salon", "contactstatus": "Unreachable",
    })
    assert reachable[0].payload == "ON"
    assert unreachable[0].payload == "OFF"
    assert reachable[0].topic == "telephonie/salon/joignable"


def test_appel_entrant_porte_le_numero_de_l_appelant(translator):
    publications = translator.translate({
        "event": "DialBegin",
        "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002",
        "calleridnum": "0102030405",
        "calleridname": "Mamie",
    })
    assert len(publications) == 1
    assert publications[0].topic == "telephonie/salon/appel"

    payload = json.loads(publications[0].payload)
    assert payload == {
        "event_type": "appel_entrant",
        "numero": "0102030405",
        "nom": "Mamie",
        "poste": "Salon",
    }


def test_un_appel_entrant_n_est_jamais_retenu(translator):
    """Un événement retenu serait rejoué au redémarrage du courtier, et ferait
    annoncer par le satellite un appel terminé depuis longtemps."""
    publications = translator.translate({
        "event": "DialBegin", "destchannel": "PJSIP/salon-00000002",
        "calleridnum": "0102030405",
    })
    assert not publications[0].retain


def test_un_appelant_masque_ne_remonte_pas_le_mot_unknown(translator):
    """« <unknown> » lu à voix haute par un satellite vocal, c'est raté."""
    publications = translator.translate({
        "event": "DialBegin", "destchannel": "PJSIP/salon-00000002",
        "calleridnum": "<unknown>", "calleridname": "<unknown>",
    })
    payload = json.loads(publications[0].payload)
    assert payload["numero"] == ""
    assert payload["nom"] == ""


def test_un_poste_inconnu_de_la_base_est_ignore(translator):
    """Les canaux techniques ne doivent pas créer d'entités dans Home Assistant."""
    for event in (
        {"event": "DeviceStateChange", "device": "PJSIP/anonymous", "state": "INUSE"},
        {"event": "ContactStatus", "aor": "inconnu", "contactstatus": "Reachable"},
        {"event": "DialBegin", "destchannel": "Local/100@from-internal-00000001;1"},
    ):
        assert translator.translate(event) == []


def test_les_evenements_sans_interet_sont_silencieux(translator):
    for name in ("Newexten", "VarSet", "RTCPSent", "Ping"):
        assert translator.translate({"event": name}) == []


# --- Découverte -------------------------------------------------------------

def test_la_decouverte_declare_trois_entites_par_poste():
    publications = events.discovery({"salon": "Salon"})
    topics = [p.topic for p in publications]
    assert topics == [
        "homeassistant/binary_sensor/telephonie/salon_joignable/config",
        "homeassistant/sensor/telephonie/salon_etat/config",
        "homeassistant/event/telephonie/salon_appel/config",
    ]


def test_la_decouverte_est_retenue():
    """Home Assistant relit les configurations au démarrage : sans `retain`,
    les entités disparaîtraient jusqu'au prochain lancement du service."""
    assert all(p.retain for p in events.discovery({"salon": "Salon"}))


def test_chaque_entite_a_un_identifiant_unique_et_stable():
    publications = events.discovery({"salon": "Salon", "etage": "Étage"})
    uniques = [json.loads(p.payload)["unique_id"] for p in publications]
    assert len(uniques) == len(set(uniques))
    assert "telephonie_salon_joignable" in uniques


def test_les_sujets_d_etat_annonces_sont_ceux_reellement_publies():
    """Une divergence entre les deux donnerait des entités éternellement vides,
    sans la moindre erreur nulle part."""
    devices = {"salon": "Salon"}
    annonces = {
        json.loads(p.payload)["state_topic"] for p in events.discovery(devices)
    }
    translator = events.Translator(devices)
    publies = set()
    for event in (
        {"event": "DeviceStateChange", "device": "PJSIP/salon", "state": "INUSE"},
        {"event": "ContactStatus", "aor": "salon", "contactstatus": "Reachable"},
        {"event": "DialBegin", "destchannel": "PJSIP/salon-00000002"},
    ):
        publies.update(p.topic for p in translator.translate(event))

    assert annonces == publies


def test_toutes_les_entites_suivent_la_disponibilite_du_service():
    for publication in events.discovery({"salon": "Salon"}):
        payload = json.loads(publication.payload)
        assert payload["availability_topic"] == events.STATUS_TOPIC


def test_les_postes_desactives_ne_sont_pas_suivis(sample):
    sample.execute("UPDATE devices SET enabled = 0 WHERE slug = 'mobile'")
    labels = events.load_device_labels(sample)
    assert "mobile" not in labels
    assert labels["salon"] == "Salon"


# --- Poignée de main AMI ----------------------------------------------------

class FakeSocket:
    """Socket qui rend des morceaux prédéfinis, pour rejouer un vrai dialogue."""

    def __init__(self, chunks):
        self.chunks = list(chunks)
        self.sent = []

    def settimeout(self, _):
        pass

    def sendall(self, data):
        self.sent.append(data.decode())

    def recv(self, _size):
        return self.chunks.pop(0) if self.chunks else b""

    def close(self):
        pass


def _connected(chunks, monkeypatch):
    ami = events.AmiConnection("127.0.0.1", 5038, "u", "s")
    fake = FakeSocket(chunks)
    monkeypatch.setattr(events.socket, "create_connection", lambda *a, **k: fake)
    return ami, fake


def test_identification_reussie(monkeypatch):
    ami, fake = _connected([
        b"Asterisk Call Manager/9.0.0\r\n"
        b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n",
    ], monkeypatch)

    ami.connect()
    assert "Action: Login" in fake.sent[0]
    assert "Username: u" in fake.sent[0]


def test_identification_en_deux_paquets(monkeypatch):
    """La bannière et la réponse n'arrivent pas forcément ensemble."""
    ami, _ = _connected([
        b"Asterisk Call Manager/9.0.0\r\n",
        b"Response: Success\r\nMessage: Authentication accepted\r\n\r\n",
    ], monkeypatch)
    ami.connect()  # ne doit pas lever


def test_un_mauvais_secret_est_signale_clairement(monkeypatch):
    ami, _ = _connected([
        b"Asterisk Call Manager/9.0.0\r\n"
        b"Response: Error\r\nMessage: Authentication failed\r\n\r\n",
    ], monkeypatch)

    with pytest.raises(ConnectionError, match="Authentication failed"):
        ami.connect()


def test_une_connexion_fermee_pendant_l_identification_ne_boucle_pas(monkeypatch):
    """Sans ça, un Asterisk qui coupe la connexion ferait tourner une boucle
    infinie sur un `recv` qui rend toujours zéro octet."""
    ami, _ = _connected([b"Asterisk Call Manager/9.0.0\r\n"], monkeypatch)

    with pytest.raises(ConnectionError, match="fermée"):
        ami.connect()
