"""Tests du pont Asterisk → Home Assistant.

Ce service ne tient qu'à une chose : traduire correctement des événements qu'on
ne contrôle pas. Les tests portent donc sur du texte AMI réel, tel qu'Asterisk
l'émet, et sur ce qui en sort.
"""

from __future__ import annotations

import json

import pytest

from app import events


POSTES = {
    "salon": events.Poste("Salon", trunk=False),
    "etage": events.Poste("Étage", trunk=False),
    # Le pont FXO est un endpoint de la base comme un autre, mais il regarde
    # vers l'extérieur : c'est ce qui distingue un appel entrant d'un interne.
    "grandstream-fxo": events.Poste("Pont Freebox", trunk=True),
}


@pytest.fixture()
def translator():
    return events.Translator(POSTES, now=lambda: "2026-08-21T01:32:03+02:00")


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


@pytest.mark.parametrize("dialstring, attendu", [
    ("0102030405@grandstream-fxo", "0102030405"),   # PJSIP vers le pont FXO
    ("quectel0/+33102030405", "+33102030405"),      # chan-quectel vers le GSM
    ("101", "101"),
    ("", ""),
])
def test_numero_sortant_extrait_de_la_chaine_de_dial(dialstring, attendu):
    assert events.number_from_dialstring(dialstring) == attendu


# --- États ------------------------------------------------------------------

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


def test_l_etat_du_pont_fxo_est_suivi_comme_les_autres(translator):
    """Savoir que la Freebox est injoignable vaut autant qu'un poste éteint."""
    publications = translator.translate({
        "event": "ContactStatus", "aor": "grandstream-fxo", "contactstatus": "Unreachable",
    })
    assert publications[0].topic == "telephonie/grandstream-fxo/joignable"


# --- Appels -----------------------------------------------------------------

def _charge(publications):
    assert len(publications) == 1
    return json.loads(publications[0].payload)


def test_appel_entrant(translator):
    """L'appel arrive par le pont FXO et fait sonner le salon."""
    publications = translator.translate({
        "event": "DialBegin",
        "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002",
        "calleridnum": "0102030405",
        "calleridname": "Mamie",
        "uniqueid": "1787095923.1",
    })
    assert publications[0].topic == "telephonie/salon/appel"
    assert _charge(publications) == {
        "event_type": "entrant",
        "numero": "0102030405",
        "nom": "Mamie",
        "poste": "Salon",
        "horodatage": "2026-08-21T01:32:03+02:00",
    }


def test_appel_sortant(translator):
    """Le salon compose un numéro : le sens s'inverse, et le numéro appelé vient
    de la chaîne de `Dial()` — Asterisk ne le publie pas ailleurs."""
    publications = translator.translate({
        "event": "DialBegin",
        "channel": "PJSIP/salon-00000003",
        "destchannel": "PJSIP/grandstream-fxo-00000004",
        "dialstring": "0102030405@grandstream-fxo",
        "calleridnum": "100",
        "uniqueid": "1787095999.3",
    })
    assert publications[0].topic == "telephonie/salon/appel"
    charge = _charge(publications)
    assert charge["event_type"] == "sortant"
    assert charge["numero"] == "0102030405"
    assert charge["poste"] == "Salon"


def test_appel_sortant_par_le_gsm(translator):
    """Le trunk de secours n'est pas un endpoint de la base : c'est l'appelant
    connu qui suffit à décider du sens."""
    charge = _charge(translator.translate({
        "event": "DialBegin",
        "channel": "PJSIP/salon-00000003",
        "destchannel": "Quectel/quectel0-00000005",
        "dialstring": "quectel0/+33102030405",
        "uniqueid": "1787096000.3",
    }))
    assert charge["event_type"] == "sortant"
    assert charge["numero"] == "+33102030405"


def test_appel_interne(translator):
    """Poste à poste : annoncé sur celui qui sonne, pas sur celui qui compose."""
    publications = translator.translate({
        "event": "DialBegin",
        "channel": "PJSIP/salon-00000006",
        "destchannel": "PJSIP/etage-00000007",
        "calleridnum": "100",
        "calleridname": "Salon",
        "uniqueid": "1787096100.6",
    })
    assert publications[0].topic == "telephonie/etage/appel"
    charge = _charge(publications)
    assert charge["event_type"] == "interne"
    assert charge["poste"] == "Étage"
    assert charge["numero"] == "100"


def test_un_appel_entrant_n_est_jamais_retenu(translator):
    """Un événement retenu serait rejoué au redémarrage du courtier, et ferait
    annoncer par le satellite un appel terminé depuis longtemps."""
    publications = translator.translate({
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002", "calleridnum": "0102030405",
    })
    assert not publications[0].retain


def test_un_appelant_masque_ne_remonte_pas_le_mot_unknown(translator):
    """« <unknown> » lu à voix haute par un satellite vocal, c'est raté."""
    charge = _charge(translator.translate({
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002",
        "calleridnum": "<unknown>", "calleridname": "<unknown>",
    }))
    assert charge["numero"] == ""
    assert charge["nom"] == ""


def test_l_horodatage_est_pose_a_la_reception(translator):
    charge = _charge(translator.translate({
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002",
    }))
    assert charge["horodatage"] == "2026-08-21T01:32:03+02:00"


def test_l_horodatage_reel_porte_un_fuseau():
    """Sans décalage, Home Assistant interpréterait l'heure comme de l'UTC."""
    from datetime import datetime
    horodatage = events._horodatage()
    assert datetime.fromisoformat(horodatage).tzinfo is not None


# --- Doublons ---------------------------------------------------------------

def test_un_sortant_qui_bascule_sur_le_trunk_de_secours_n_est_annonce_qu_une_fois(translator):
    """Le failover relance un `Dial()` pour le même appel : deux DialBegin, un
    seul appel réel. Le canal appelant, lui, ne change pas."""
    premier = {
        "event": "DialBegin", "channel": "PJSIP/salon-00000003",
        "destchannel": "PJSIP/grandstream-fxo-00000004",
        "dialstring": "0102030405@grandstream-fxo", "uniqueid": "1787095999.3",
    }
    second = dict(premier, destchannel="Quectel/quectel0-00000005",
                  dialstring="quectel0/+33102030405")

    assert len(translator.translate(premier)) == 1
    assert translator.translate(second) == []


def test_un_groupe_qui_fait_sonner_deux_postes_annonce_les_deux(translator):
    """Même appel, mais deux téléphones sonnent vraiment : deux annonces."""
    commun = {
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "calleridnum": "0102030405", "uniqueid": "1787095923.1",
    }
    vers_salon = translator.translate(dict(commun, destchannel="PJSIP/salon-00000002"))
    vers_etage = translator.translate(dict(commun, destchannel="PJSIP/etage-00000003"))

    assert vers_salon[0].topic == "telephonie/salon/appel"
    assert vers_etage[0].topic == "telephonie/etage/appel"


def test_le_meme_appel_reste_annoncable_apres_la_fenetre(translator):
    """Sans purge, un rappel au même numéro plus tard serait avalé."""
    translator.fenetre_doublon = 0
    event = {
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "PJSIP/salon-00000002", "uniqueid": "1787095923.1",
    }
    assert len(translator.translate(event)) == 1
    assert len(translator.translate(event)) == 1


# --- Ce qui doit rester silencieux ------------------------------------------

def test_un_appel_entre_deux_trunks_n_est_pas_annonce(translator):
    """Ni l'un ni l'autre n'est un téléphone de la maison."""
    assert translator.translate({
        "event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
        "destchannel": "Quectel/quectel0-00000002",
    }) == []


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
    publications = events.discovery({"salon": events.Poste("Salon", trunk=False)})
    assert [p.topic for p in publications] == [
        "homeassistant/binary_sensor/telephonie/salon_joignable/config",
        "homeassistant/sensor/telephonie/salon_etat/config",
        "homeassistant/event/telephonie/salon_appel/config",
        # Plus une entité globale pour les appels terminés, tous postes confondus.
        "homeassistant/event/telephonie/cdr/config",
    ]


def test_un_trunk_n_a_pas_d_entite_d_appel():
    """Il n'est jamais le poste d'un événement : l'entité resterait vide."""
    topics = [p.topic for p in events.discovery(
        {"grandstream-fxo": events.Poste("Pont Freebox", trunk=True)}
    )]
    assert "homeassistant/event/telephonie/grandstream-fxo_appel/config" not in topics
    assert any("binary_sensor/" in t for t in topics)


def test_les_types_d_evenement_declares_couvrent_les_trois_sens():
    config = json.loads(events.discovery(
        {"salon": events.Poste("Salon", trunk=False)}
    )[2].payload)
    assert set(config["event_types"]) == {"entrant", "sortant", "interne"}


def test_la_decouverte_est_retenue():
    """Home Assistant relit les configurations au démarrage : sans `retain`,
    les entités disparaîtraient jusqu'au prochain lancement du service."""
    assert all(p.retain for p in events.discovery(POSTES))


def test_chaque_entite_a_un_identifiant_unique_et_stable():
    uniques = [json.loads(p.payload)["unique_id"] for p in events.discovery(POSTES)]
    assert len(uniques) == len(set(uniques))
    assert "telephonie_salon_joignable" in uniques


def test_les_sujets_annonces_sont_ceux_reellement_publies():
    """Une divergence entre les deux donnerait des entités éternellement vides,
    sans la moindre erreur nulle part."""
    annonces = {json.loads(p.payload)["state_topic"] for p in events.discovery(POSTES)}

    translator = events.Translator(POSTES)
    publies = set()
    for event in (
        {"event": "DeviceStateChange", "device": "PJSIP/salon", "state": "INUSE"},
        {"event": "DeviceStateChange", "device": "PJSIP/etage", "state": "INUSE"},
        {"event": "DeviceStateChange", "device": "PJSIP/grandstream-fxo", "state": "INUSE"},
        {"event": "ContactStatus", "aor": "salon", "contactstatus": "Reachable"},
        {"event": "ContactStatus", "aor": "etage", "contactstatus": "Reachable"},
        {"event": "ContactStatus", "aor": "grandstream-fxo", "contactstatus": "Reachable"},
        {"event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
         "destchannel": "PJSIP/salon-00000002", "uniqueid": "a"},
        {"event": "DialBegin", "channel": "PJSIP/grandstream-fxo-00000001",
         "destchannel": "PJSIP/etage-00000003", "uniqueid": "b"},
        {"event": "Cdr", "channel": "PJSIP/grandstream-fxo-00000001",
         "destinationchannel": "PJSIP/salon-00000002",
         "disposition": "ANSWERED", "uniqueid": "c"},
    ):
        publies.update(p.topic for p in translator.translate(event))

    assert annonces == publies


def test_toutes_les_entites_suivent_la_disponibilite_du_service():
    for publication in events.discovery(POSTES):
        assert json.loads(publication.payload)["availability_topic"] == events.STATUS_TOPIC


def test_le_pont_fxo_est_charge_comme_passerelle(sample):
    postes = events.load_postes(sample)
    assert postes["fxo"].trunk is True
    assert postes["salon"].trunk is False


def test_les_postes_desactives_ne_sont_pas_suivis(sample):
    sample.execute("UPDATE devices SET enabled = 0 WHERE slug = 'mobile'")
    postes = events.load_postes(sample)
    assert "mobile" not in postes
    assert postes["salon"].label == "Salon"


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


# --- Enregistrements d'appel (cdr_manager) ----------------------------------

CDR_ENTRANT = {
    "event": "Cdr",
    "source": "0102030405",
    "destination": "100",
    "channel": "PJSIP/grandstream-fxo-00000001",
    "destinationchannel": "PJSIP/salon-00000002",
    "starttime": "2026-08-21 01:32:03",
    "answertime": "2026-08-21 01:32:09",
    "endtime": "2026-08-21 01:34:21",
    "duration": "138",
    "billableseconds": "132",
    "disposition": "ANSWERED",
    "uniqueid": "1787095923.1",
}


def test_un_cdr_donne_la_duree_et_l_issue(translator):
    publications = translator.translate(CDR_ENTRANT)
    assert publications[0].topic == "telephonie/cdr"

    charge = json.loads(publications[0].payload)
    assert charge["event_type"] == "repondu"
    assert charge["sens"] == "entrant"
    assert charge["source"] == "0102030405"
    assert charge["poste"] == "Salon"
    assert charge["duree"] == 138
    assert charge["duree_conversation"] == 132
    assert charge["fin"] == "2026-08-21 01:34:21"


def test_un_appel_sans_reponse_est_quand_meme_annonce(translator):
    """C'est même le cas le plus intéressant — et celui où `destinationchannel`
    est vide, puisque personne n'a décroché."""
    charge = json.loads(translator.translate(dict(
        CDR_ENTRANT, destinationchannel="", answertime="", billableseconds="0",
        disposition="NO ANSWER",
    ))[0].payload)

    assert charge["event_type"] == "sans_reponse"
    assert charge["sens"] == "entrant"      # déduit du seul côté appelant
    assert charge["duree_conversation"] == 0


def test_le_sens_sortant_est_reconnu_sur_un_cdr(translator):
    charge = json.loads(translator.translate({
        "event": "Cdr", "source": "100", "destination": "0102030405",
        "channel": "PJSIP/salon-00000003",
        "destinationchannel": "PJSIP/grandstream-fxo-00000004",
        "disposition": "ANSWERED", "duration": "42", "billableseconds": "30",
        "uniqueid": "1787095999.3",
    })[0].payload)

    assert charge["sens"] == "sortant"
    assert charge["poste"] == "Salon"
    assert charge["destination"] == "0102030405"


@pytest.mark.parametrize("disposition, attendu", [
    ("ANSWERED", "repondu"),
    ("NO ANSWER", "sans_reponse"),
    ("BUSY", "occupe"),
    ("FAILED", "echec"),
    ("CONGESTION", "congestion"),
    ("QUELQUE CHOSE", "inconnu"),
])
def test_toutes_les_issues_sont_traduites(translator, disposition, attendu):
    charge = json.loads(translator.translate(dict(
        CDR_ENTRANT, disposition=disposition, uniqueid=disposition,
    ))[0].payload)
    assert charge["event_type"] == attendu


def test_une_duree_illisible_ne_perd_pas_l_evenement(translator):
    """Mieux vaut une durée à zéro qu'un appel jamais annoncé."""
    charge = json.loads(translator.translate(dict(
        CDR_ENTRANT, duration="", billableseconds="n/a",
    ))[0].payload)
    assert charge["duree"] == 0
    assert charge["duree_conversation"] == 0


def test_les_cdr_des_canaux_techniques_sont_ecartes(translator):
    """`cdr.conf` produit un enregistrement par tronçon : ceux des canaux Local
    doublent celui du vrai canal."""
    assert translator.translate({
        "event": "Cdr", "channel": "Local/100@from-internal-00000001;1",
        "destinationchannel": "Local/100@from-internal-00000001;2",
        "disposition": "ANSWERED", "uniqueid": "x",
    }) == []


def test_un_meme_cdr_n_est_annonce_qu_une_fois(translator):
    assert len(translator.translate(CDR_ENTRANT)) == 1
    assert translator.translate(CDR_ENTRANT) == []


def test_l_entite_des_appels_termines_est_unique_et_globale():
    """Un appel sans réponse n'a pas de poste : le rattacher à l'un d'eux
    ferait disparaître exactement les appels qu'on veut voir."""
    configs = [p for p in events.discovery(POSTES) if p.topic.endswith("/cdr/config")]
    assert len(configs) == 1

    config = json.loads(configs[0].payload)
    assert config["state_topic"] == "telephonie/cdr"
    assert "sans_reponse" in config["event_types"]
    assert "repondu" in config["event_types"]


def test_les_issues_publiees_sont_toutes_declarees(translator):
    """Un `event_type` absent de la découverte est rejeté par Home Assistant."""
    config = json.loads(
        [p for p in events.discovery(POSTES) if p.topic.endswith("/cdr/config")][0].payload
    )
    declares = set(config["event_types"])

    for index, disposition in enumerate(
        list(events.DISPOSITIONS) + ["VALEUR INATTENDUE"]
    ):
        charge = json.loads(translator.translate(dict(
            CDR_ENTRANT, disposition=disposition, uniqueid=f"u{index}",
        ))[0].payload)
        assert charge["event_type"] in declares, disposition
