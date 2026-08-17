"""Tests de bout en bout de l'UI : authentification, CRUD, application.

Ils exercent le vrai routage FastAPI, y compris la protection CSRF — c'est
justement le genre de garde-fou qu'on casse sans s'en rendre compte.
"""

from __future__ import annotations

import re

import pytest
from fastapi.testclient import TestClient

from app import generator
from app.main import app


def _csrf(html: str) -> str:
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match, "aucun jeton CSRF dans la page"
    return match.group(1)


@pytest.fixture()
def client(sample):
    with TestClient(app) as test_client:
        response = test_client.post(
            "/setup",
            data={"username": "admin", "password": "motdepasse-long", "password2": "motdepasse-long"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        response = test_client.post(
            "/login",
            data={"username": "admin", "password": "motdepasse-long"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        yield test_client


def test_acces_anonyme_redirige_vers_login(sample):
    with TestClient(app) as anonymous:
        response = anonymous.get("/devices", follow_redirects=False)
        assert response.status_code == 303
        assert response.headers["location"] == "/login"


def test_setup_ne_fonctionne_qu_une_fois(client):
    response = client.post(
        "/setup",
        data={"username": "pirate", "password": "encore-un-long", "password2": "encore-un-long"},
        follow_redirects=False,
    )
    assert response.headers["location"] == "/login"


def test_mauvais_mot_de_passe_refuse(sample):
    with TestClient(app) as anonymous:
        anonymous.post("/setup", data={"username": "admin", "password": "motdepasse-long",
                                       "password2": "motdepasse-long"})
        response = anonymous.post("/login", data={"username": "admin", "password": "faux"},
                                  follow_redirects=False)
        assert "err=" in response.headers["location"]


@pytest.mark.parametrize(
    "path",
    ["/", "/devices", "/users", "/groups", "/speed-dials", "/trunks",
     "/settings", "/apply", "/revisions", "/calls", "/audit", "/provisioning"],
)
def test_toutes_les_pages_repondent(client, path):
    """Garde-fou contre une erreur de gabarit qui ne se verrait qu'à l'usage."""
    response = client.get(path)
    assert response.status_code == 200, response.text[:400]
    assert "<table" in response.text or "<form" in response.text


def test_pages_d_edition_prechargent_le_formulaire(client):
    assert 'value="Salon"' in client.get("/devices?edit=1").text
    assert 'value="Camille"' in client.get("/users?edit=1").text
    assert 'value="Toute la maison"' in client.get("/groups?edit=1").text
    assert 'value="PJSIP/{num}@fxo"' in client.get("/trunks?edit=1").text


def test_tableau_de_bord_liste_les_postes(client):
    html = client.get("/").text
    assert "Salon" in html
    assert "Toute la maison" in html


def test_creation_de_poste(client):
    csrf = _csrf(client.get("/devices").text)
    response = client.post(
        "/devices",
        data={
            "csrf": csrf, "slug": "garage", "label": "Garage", "kind": "fxs",
            "extension": "102", "codecs": "alaw,ulaw", "max_contacts": "1",
            "mailbox": "", "dial_mode": "direct", "hotline_target": "",
            "ring_time": "30", "notes": "HT801",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]

    html = client.get("/devices").text
    assert "garage" in html
    # La boîte vocale prend par défaut le numéro du poste : c'est ce que
    # suppose le code de service *97.
    row = sample_row(client, "SELECT mailbox FROM devices WHERE slug = 'garage'")
    assert row == "102"


def sample_row(client, sql):
    from app import db
    conn = db.connect()
    try:
        return conn.execute(sql).fetchone()[0]
    finally:
        conn.close()


def test_creation_refusee_sans_csrf(client):
    response = client.post(
        "/devices",
        data={"csrf": "faux", "slug": "pirate", "label": "Pirate", "kind": "fxs"},
        follow_redirects=False,
    )
    assert response.status_code == 400


def test_slug_invalide_refuse(client):
    csrf = _csrf(client.get("/devices").text)
    response = client.post(
        "/devices",
        data={"csrf": csrf, "slug": "Salon Principal!", "label": "X", "kind": "fxs"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]


def test_slug_en_double_refuse_proprement(client):
    csrf = _csrf(client.get("/devices").text)
    response = client.post(
        "/devices",
        data={"csrf": csrf, "slug": "salon", "label": "Doublon", "kind": "fxs"},
        follow_redirects=False,
    )
    assert "err=" in response.headers["location"]
    assert "d%C3%A9j%C3%A0+pris" in response.headers["location"]


def test_regeneration_du_mot_de_passe_sip(client):
    before = sample_row(client, "SELECT secret FROM devices WHERE slug = 'salon'")
    csrf = _csrf(client.get("/devices").text)
    client.post("/devices/1/secret", data={"csrf": csrf}, follow_redirects=False)
    after = sample_row(client, "SELECT secret FROM devices WHERE slug = 'salon'")
    assert before != after


def test_creation_de_personne_avec_postes(client):
    csrf = _csrf(client.get("/users").text)
    response = client.post(
        "/users",
        data={"csrf": csrf, "name": "Alex", "extension": "202", "menu_digit": "2",
              "voicemail_box": "", "email": "", "device_ids": ["1", "2"]},
        follow_redirects=False,
    )
    assert "err=" not in response.headers["location"]
    # Un PIN de messagerie est communiqué à la création, pas laissé à 0000.
    assert "PIN+de+messagerie+initial" in response.headers["location"]


def test_ecran_appliquer_montre_le_diff_puis_ecrit(client):
    html = client.get("/apply").text
    assert "[salon](endpoint-internal)" in html

    csrf = _csrf(html)
    response = client.post("/apply", data={"csrf": csrf, "summary": "premier jet"},
                           follow_redirects=False)
    assert "ok=" in response.headers["location"]

    written = (generator.config.GENERATED_DIR / "pjsip_endpoints.conf").read_text()
    assert "[salon](endpoint-internal)" in written

    # Plus rien à appliquer : le bandeau d'avertissement disparaît.
    assert "ne sont pas encore actives" not in client.get("/").text


def test_reglages_bool_decoche_vaut_non(client):
    csrf = _csrf(client.get("/settings").text)
    # `international_allowed` n'est pas transmis : la case est décochée.
    client.post("/settings", data={"csrf": csrf, "site_name": "Maison", "ivr_enabled": "1"},
                follow_redirects=False)
    assert sample_row(client,
                      "SELECT value FROM settings WHERE key = 'international_allowed'") == "0"
    assert sample_row(client, "SELECT value FROM settings WHERE key = 'ivr_enabled'") == "1"


def test_journal_d_audit_trace_les_actions(client):
    html = client.get("/audit").text
    assert "login" in html
    assert "setup" in html


# --- Provisionnement --------------------------------------------------------

def _device_form(csrf, **overrides):
    data = {
        "csrf": csrf, "slug": "garage", "label": "Garage", "kind": "fxs",
        "extension": "102", "codecs": "alaw,ulaw", "max_contacts": "1",
        "mailbox": "", "dial_mode": "direct", "hotline_target": "",
        "ring_time": "30", "notes": "", "mac": "", "prov_profile": "grandstream-ht80x",
    }
    data.update(overrides)
    return data


def test_mac_saisie_dans_n_importe_quel_format_est_normalisee(client):
    csrf = _csrf(client.get("/devices").text)
    response = client.post("/devices", data=_device_form(csrf, mac="00-1A-2B-3C-4D-5E"),
                           follow_redirects=False)
    assert "err=" not in response.headers["location"]
    assert sample_row(client, "SELECT mac FROM devices WHERE slug = 'garage'") == "001a2b3c4d5e"


def test_mac_invalide_refusee_avec_un_message_clair(client):
    csrf = _csrf(client.get("/devices").text)
    response = client.post("/devices", data=_device_form(csrf, mac="pas-une-mac"),
                           follow_redirects=False)
    assert "err=" in response.headers["location"]
    assert "MAC+invalide" in response.headers["location"]


def test_mac_en_double_refusee(client):
    csrf = _csrf(client.get("/devices").text)
    # 000b82aabbcc est déjà celle du salon (voir la fixture).
    response = client.post("/devices", data=_device_form(csrf, mac="00:0b:82:aa:bb:cc"),
                           follow_redirects=False)
    assert "err=" in response.headers["location"]


def test_sans_mac_aucun_profil_n_est_enregistre(client):
    """Un profil sans MAC ne sert à rien et laisserait croire, sur l'écran de
    provisionnement, que l'appareil est pris en charge."""
    csrf = _csrf(client.get("/devices").text)
    client.post("/devices", data=_device_form(csrf, mac=""), follow_redirects=False)
    assert sample_row(client, "SELECT COUNT(*) FROM devices WHERE slug = 'garage' "
                              "AND prov_profile IS NULL") == 1


def test_apercu_du_xml_reserve_aux_connectes(client, sample):
    anonymous_status = None
    with TestClient(app) as anonymous:
        anonymous_status = anonymous.get("/provisioning/1/preview",
                                         follow_redirects=False).status_code
    assert anonymous_status == 303

    response = client.get("/provisioning/1/preview")
    assert response.status_code == 200
    assert "<gs_provision" in response.text


def test_apercu_refuse_pour_un_poste_sans_mac(client, sample):
    sample.execute("UPDATE devices SET mac = NULL WHERE id = 1")
    sample.commit()
    assert client.get("/provisioning/1/preview").status_code == 404


def test_l_ecran_avertit_quand_le_serveur_sip_n_est_pas_regle(client):
    html = client.get("/provisioning").text
    assert "est vide" in html
    assert "non vérifiés" in html   # avertissement sur les P-values


# --- API JSON ---------------------------------------------------------------

def test_api_refuse_sans_jeton(client):
    assert client.get("/api/status").status_code == 401


def test_api_status_avec_jeton(client):
    response = client.get("/api/status", headers={"X-API-Token": "jeton-de-test"})
    assert response.status_code == 200
    assert response.json()["config_dirty"] in (True, False)


def test_api_refuse_un_numero_invalide(client):
    response = client.post(
        "/api/notify",
        headers={"X-API-Token": "jeton-de-test"},
        json={"number": "0600; rm -rf /", "sound": "alerte"},
    )
    assert response.status_code == 422


def test_api_refuse_un_nom_de_son_invalide(client):
    response = client.post(
        "/api/notify",
        headers={"X-API-Token": "jeton-de-test"},
        json={"number": "0600000000", "sound": "../../etc/passwd"},
    )
    assert response.status_code == 422
