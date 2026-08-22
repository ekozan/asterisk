"""Tests du provisionnement automatique des ATA.

Ce qui est vérifié ici, c'est le mécanisme : normalisation des MAC, contenu du
fichier servi, et surtout les refus — un appareil non déclaré ou désactivé ne
doit rien obtenir. La correspondance des numéros de P-value avec un firmware
réel, elle, ne peut pas être testée sans le matériel : c'est le rôle de
scripts/prov-import.py.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db, provisioning
from app.prov import app as prov_app


# --- Adresses MAC -----------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "000b82aabbcc",
    "00:0b:82:aa:bb:cc",
    "00-0B-82-AA-BB-CC",
    "000B82AABBCC",
    "00 0b 82 aa bb cc",
    "000b.82aa.bbcc",
])
def test_mac_normalisee_quel_que_soit_le_format(raw):
    assert provisioning.normalize_mac(raw) == "000b82aabbcc"


@pytest.mark.parametrize("raw", ["", None, "pas une mac", "000b82aabb", "000b82aabbccdd", "zz0b82aabbcc"])
def test_mac_invalide_refusee(raw):
    assert provisioning.normalize_mac(raw) is None


def test_mac_affichee_lisiblement():
    assert provisioning.format_mac("000b82aabbcc") == "00:0b:82:aa:bb:cc"
    assert provisioning.format_mac(None) == "—"


# --- Contenu du fichier de configuration ------------------------------------

def _config_for(conn, slug="salon"):
    device = db.one(conn, "SELECT * FROM devices WHERE slug = ?", (slug,))
    return provisioning.build_config(device, db.get_settings(conn))


def test_le_fichier_porte_les_identifiants_du_poste(sample):
    sample.execute("UPDATE settings SET value = '10.0.90.20' WHERE key = 'prov_sip_server'")
    xml = _config_for(sample)

    assert "<P35>salon</P35>" in xml       # identifiant SIP
    assert "<P36>salon</P36>" in xml       # identifiant d'authentification
    assert "<P34>secret1</P34>" in xml     # mot de passe SIP
    assert "<P47>10.0.90.20</P47>" in xml  # serveur SIP
    assert "<mac>000b82aabbcc</mac>" in xml


def test_les_codecs_sont_traduits_en_vocodeurs(sample):
    # alaw -> 8, ulaw -> 0, conformément aux numéros de charge utile RTP.
    xml = _config_for(sample)
    assert "<P57>8</P57>" in xml
    assert "<P58>0</P58>" in xml


def test_le_proxy_sortant_est_vide_explicitement(sample):
    """Un proxy hérité d'une configuration précédente rend l'ATA muet sans
    message : on l'efface au lieu de le laisser tel quel."""
    assert "<P48></P48>" in _config_for(sample)


def test_le_mot_de_passe_admin_n_est_pousse_que_s_il_est_defini(sample):
    assert "<P2>" not in _config_for(sample)
    sample.execute("UPDATE settings SET value = 'secret-admin' WHERE key = 'prov_admin_password'")
    assert "<P2>secret-admin</P2>" in _config_for(sample)


def test_les_caracteres_speciaux_sont_echappes(sample):
    """Un nom de poste avec une esperluette produirait un XML invalide, que
    l'appareil rejetterait en bloc."""
    from xml.etree import ElementTree

    sample.execute("UPDATE devices SET label = 'Salon & Séjour <test>' WHERE slug = 'salon'")
    xml = _config_for(sample)
    assert "Salon &amp; Séjour &lt;test&gt;" in xml   # dans la valeur : échappé
    ElementTree.fromstring(xml)


@pytest.mark.parametrize("label", [
    "Salon -- essai",      # `--` est interdit dans un commentaire XML
    "Bureau -",            # un commentaire ne peut pas finir par `-`
    "Cave <b>&</b>",
    "-----",
])
def test_un_nom_de_poste_hostile_ne_casse_pas_le_fichier(sample, label):
    """Le nom passe aussi dans un commentaire d'en-tête, où les règles ne sont
    pas celles d'une valeur : un fichier invalide serait rejeté en entier."""
    from xml.etree import ElementTree

    sample.execute("UPDATE devices SET label = ? WHERE slug = 'salon'", (label,))
    ElementTree.fromstring(_config_for(sample))


def test_le_fichier_est_un_xml_valide(sample):
    from xml.etree import ElementTree
    root = ElementTree.fromstring(_config_for(sample))
    assert root.tag == "gs_provision"


# --- Service exposé aux ATA -------------------------------------------------

@pytest.fixture()
def prov_client(sample):
    sample.execute("UPDATE settings SET value = '10.0.90.20' WHERE key = 'prov_sip_server'")
    sample.commit()
    with TestClient(prov_app) as client:
        yield client


def test_un_appareil_declare_recoit_sa_configuration(prov_client):
    response = prov_client.get("/cfg000b82aabbcc.xml")
    assert response.status_code == 200
    assert "text/xml" in response.headers["content-type"]
    assert "<P34>secret1</P34>" in response.text


def test_une_mac_inconnue_n_obtient_rien(prov_client):
    assert prov_client.get("/cfg001122334455.xml").status_code == 404


def test_une_mac_mal_formee_n_obtient_rien(prov_client):
    assert prov_client.get("/cfgnimportequoi.xml").status_code == 404


def test_un_poste_desactive_n_est_plus_servi(prov_client, sample):
    """Désactiver un poste dans l'interface doit vraiment le couper, y compris
    pour le provisionnement — sinon l'appareil se reconfigure tout seul."""
    sample.execute("UPDATE devices SET enabled = 0 WHERE slug = 'salon'")
    sample.commit()
    assert prov_client.get("/cfg000b82aabbcc.xml").status_code == 404


def test_un_poste_sans_mac_n_est_pas_servi(prov_client, sample):
    sample.execute("UPDATE devices SET mac = NULL WHERE slug = 'salon'")
    sample.commit()
    assert prov_client.get("/cfg000b82aabbcc.xml").status_code == 404


def test_le_fichier_commun_ne_contient_aucun_identifiant(prov_client):
    """cfg.xml est servi à n'importe quel appareil, sans vérification."""
    response = prov_client.get("/cfg.xml")
    assert response.status_code == 200
    assert "secret1" not in response.text
    assert "<P34>" not in response.text


def test_les_requetes_sont_tracees(prov_client):
    prov_client.get("/cfg000b82aabbcc.xml")
    prov_client.get("/cfg001122334455.xml")

    conn = db.connect()
    try:
        actions = [r["action"] for r in
                   conn.execute("SELECT action FROM audit_log WHERE action LIKE 'prov-%'")]
    finally:
        conn.close()
    assert "prov-servi" in actions
    assert "prov-inconnu" in actions


def test_le_service_de_provisionnement_n_expose_pas_l_administration(prov_client):
    """Le VLAN voix ne doit voir que la lecture d'un fichier de configuration."""
    for path in ("/", "/devices", "/settings", "/login", "/api/status"):
        assert prov_client.get(path).status_code == 404, path
