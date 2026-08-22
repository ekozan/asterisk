"""Tests de l'éditeur de fichiers de configuration.

L'écran écrit dans /etc/asterisk et recharge le service : c'est la partie de
l'interface dont une erreur coûte le plus cher. On vérifie surtout ce qui doit
être *impossible*.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import config, db, textfiles
from app.main import app


@pytest.fixture()
def etc(tmp_path, monkeypatch):
    """Un /etc/asterisk jetable, avec un pjsip.conf plausible."""
    monkeypatch.setattr(config, "ASTERISK_DIR", tmp_path)
    monkeypatch.setattr(config, "ASTERISK_LOG", tmp_path / "messages")
    (tmp_path / "pjsip.conf").write_text("[global]\ntype=global\n", encoding="utf-8")
    return tmp_path


# --- Catalogue --------------------------------------------------------------

def test_aucun_fichier_genere_n_est_editable():
    """Éditer un fichier généré serait sans effet : le prochain « Appliquer »
    le réécrit. Le proposer serait un piège."""
    noms = {spec.filename for spec in textfiles.EDITABLE.values()}
    assert not noms & {
        "pjsip_endpoints.conf", "extensions_generated.conf", "voicemail_generated.conf",
    }


def test_une_cle_inconnue_ne_designe_aucun_chemin():
    for key in ("../../etc/shadow", "/etc/passwd", "pjsip.conf", ""):
        with pytest.raises(KeyError):
            textfiles.path_for(key)


def test_les_chemins_restent_dans_le_repertoire_asterisk(etc):
    for key in textfiles.EDITABLE:
        assert textfiles.path_for(key).parent == config.ASTERISK_DIR


# --- Contrôle de forme ------------------------------------------------------

def test_syntaxe_accepte_une_configuration_normale():
    texte = (
        "; un commentaire\n"
        "[transport-udp]\n"
        "type=transport\n"
        "bind=0.0.0.0:5060\n"
        "\n"
        "[endpoint-internal](!)\n"
        "type=endpoint\n"
        "#include generated/pjsip_endpoints.conf\n"
    )
    assert textfiles.check_syntax(texte) == []


@pytest.mark.parametrize("ligne, attendu", [
    ("[transport-udp", "en-tête de section"),
    ("[a][b]", "en-tête de section"),
    ("bind 0.0.0.0:5060", "affectation"),
    ("#inclure autre.conf", "directive inconnue"),
])
def test_syntaxe_refuse_les_fautes_courantes(ligne, attendu):
    erreurs = textfiles.check_syntax(f"[ok]\ntype=global\n{ligne}\n")
    assert erreurs, f"{ligne!r} aurait dû être refusé"
    assert attendu in erreurs[0]


def test_une_faute_de_syntaxe_n_atteint_pas_le_disque(etc, conn):
    avant = (etc / "pjsip.conf").read_text(encoding="utf-8")
    result = textfiles.save(conn, "pjsip", "[global\ntype=global\n", "admin")

    assert not result["ok"]
    assert (etc / "pjsip.conf").read_text(encoding="utf-8") == avant
    assert db.one(conn, "SELECT COUNT(*) AS n FROM file_revisions")["n"] == 0


# --- Écriture et historique -------------------------------------------------

def test_ecriture_archive_le_contenu_precedent(etc, conn):
    avant = (etc / "pjsip.conf").read_text(encoding="utf-8")
    result = textfiles.save(conn, "pjsip", "[global]\ntype=global\nuser_agent=PBX\n", "admin")

    assert result["ok"], result["message"]
    assert "user_agent=PBX" in (etc / "pjsip.conf").read_text(encoding="utf-8")

    archive = db.one(conn, "SELECT * FROM file_revisions ORDER BY id DESC LIMIT 1")
    assert archive["content"] == avant
    assert archive["filename"] == "pjsip.conf"
    assert archive["author"] == "admin"


def test_restaurer_remet_le_contenu_archive(etc, conn):
    origine = (etc / "pjsip.conf").read_text(encoding="utf-8")
    textfiles.save(conn, "pjsip", "[global]\ntype=global\nuser_agent=PBX\n", "admin")
    archive = db.one(conn, "SELECT id FROM file_revisions ORDER BY id DESC LIMIT 1")

    result = textfiles.restore(conn, archive["id"], "admin")

    assert result["ok"], result["message"]
    assert (etc / "pjsip.conf").read_text(encoding="utf-8") == origine


def test_ecriture_identique_ne_cree_pas_de_version(etc, conn):
    """Sinon l'historique se remplit de doublons et le vrai changement s'y perd."""
    inchange = (etc / "pjsip.conf").read_text(encoding="utf-8")
    result = textfiles.save(conn, "pjsip", inchange, "admin")

    assert result["ok"]
    assert db.one(conn, "SELECT COUNT(*) AS n FROM file_revisions")["n"] == 0


def test_les_fins_de_ligne_windows_sont_normalisees(etc, conn):
    textfiles.save(conn, "pjsip", "[global]\r\ntype=global\r\n", "admin")
    assert "\r" not in (etc / "pjsip.conf").read_text(encoding="utf-8")


def test_l_historique_est_borne(etc, conn):
    for index in range(textfiles.HISTORY_KEPT + 5):
        textfiles.save(conn, "pjsip", f"[global]\ntype=global\nuser_agent=v{index}\n", "admin")
    total = db.one(conn, "SELECT COUNT(*) AS n FROM file_revisions")["n"]
    assert total == textfiles.HISTORY_KEPT


# --- Rechargement refusé ----------------------------------------------------

def test_une_version_refusee_par_asterisk_est_remise_en_etat(etc, conn, monkeypatch):
    """Le cas qui justifie l'écran : Asterisk se plaint, et on ne laisse pas
    l'installation dans un état que personne n'a choisi."""
    origine = (etc / "pjsip.conf").read_text(encoding="utf-8")

    monkeypatch.setattr(config, "RELOAD_ENABLED", True)
    monkeypatch.setattr(
        textfiles.asterisk, "run_cli",
        lambda command, timeout=15: textfiles.asterisk.CommandResult(command, True, ""),
    )
    # Asterisk écrit sa plainte dans le journal, pas sur la sortie du CLI.
    monkeypatch.setattr(
        textfiles, "_log_since",
        lambda offset: "[2026-08-21] ERROR[1] config.c: Parse error in "
                       "/etc/asterisk/pjsip.conf, line 3\n",
    )

    result = textfiles.save(conn, "pjsip", "[global]\ntype=global\nbogue=oui\n", "admin")

    assert not result["ok"]
    assert "Parse error" in result["log"]
    assert (etc / "pjsip.conf").read_text(encoding="utf-8") == origine


def test_un_journal_qui_parle_d_un_autre_fichier_ne_declenche_rien(etc, conn, monkeypatch):
    """Le journal mélange tous les modules : ne retenir que les lignes citant le
    fichier édité évite d'annuler une modification correcte."""
    monkeypatch.setattr(config, "RELOAD_ENABLED", True)
    monkeypatch.setattr(
        textfiles.asterisk, "run_cli",
        lambda command, timeout=15: textfiles.asterisk.CommandResult(command, True, ""),
    )
    monkeypatch.setattr(
        textfiles, "_log_since",
        lambda offset: "[2026-08-21] ERROR[1] loader.c: cdr_pgsql declined to load.\n",
    )

    result = textfiles.save(conn, "pjsip", "[global]\ntype=global\nuser_agent=PBX\n", "admin")

    assert result["ok"], result["message"]
    assert "user_agent=PBX" in (etc / "pjsip.conf").read_text(encoding="utf-8")


# --- Bout en bout par l'interface -------------------------------------------

@pytest.fixture()
def client(sample):
    with TestClient(app) as test_client:
        test_client.post("/setup", data={
            "username": "admin", "password": "motdepasse-long",
            "password2": "motdepasse-long",
        }, follow_redirects=False)
        test_client.post("/login", data={
            "username": "admin", "password": "motdepasse-long",
        }, follow_redirects=False)
        yield test_client


def _csrf(html: str) -> str:
    import re
    match = re.search(r'name="csrf" value="([^"]+)"', html)
    assert match
    return match.group(1)


def test_l_ecran_liste_les_fichiers_et_pas_les_generes(client, etc):
    html = client.get("/files").text
    assert "pjsip.conf" in html
    assert "pjsip_endpoints.conf" not in html


def test_une_cle_inconnue_dans_l_url_est_ignoree(client, etc):
    """`?edit=` vient du client : il ne doit jamais servir à désigner un chemin."""
    response = client.get("/files?edit=../../../etc/shadow")
    assert response.status_code == 200
    assert "shadow" not in response.text


def test_enregistrement_sans_jeton_csrf_refuse(client, etc):
    response = client.post("/files/pjsip", data={"csrf": "faux", "content": "[global]\n"},
                           follow_redirects=False)
    assert response.status_code == 400
    assert (etc / "pjsip.conf").read_text(encoding="utf-8").startswith("[global]\ntype=global")


def test_enregistrement_depuis_l_interface(client, etc):
    csrf = _csrf(client.get("/files?edit=pjsip").text)
    response = client.post(
        "/files/pjsip",
        data={"csrf": csrf, "content": "[global]\ntype=global\nuser_agent=PBX\n"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert "err=" not in response.headers["location"]
    assert "user_agent=PBX" in (etc / "pjsip.conf").read_text(encoding="utf-8")


def test_un_fichier_hors_catalogue_est_refuse_par_la_route(client, etc):
    csrf = _csrf(client.get("/files?edit=pjsip").text)
    response = client.post("/files/shadow", data={"csrf": csrf, "content": "x"},
                           follow_redirects=False)
    assert response.status_code == 303
    assert "err=" in response.headers["location"]
