"""Tests du générateur de configuration.

L'enjeu : une erreur ici produit un dialplan syntaxiquement correct mais qui se
comporte mal en production, et ça ne se voit qu'au moment où quelqu'un décroche.
"""

from __future__ import annotations

from app import generator


def _sections(conf: str) -> list[tuple[str, str | None, str]]:
    """Découpe un .conf en (nom, gabarit hérité, corps).

    Les doublons de nom sont conservés : PJSIP autorise un endpoint et un aor
    portant le même nom, et c'est justement ce qu'on veut vérifier.
    """
    out: list[tuple[str, str | None, str]] = []
    header: tuple[str, str | None] | None = None
    body: list[str] = []
    for line in conf.splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and "]" in stripped:
            if header is not None:
                out.append((*header, "\n".join(body)))
            close = stripped.index("]")
            header = (stripped[1:close], stripped[close + 1:].strip("()") or None)
            body = []
        elif header is not None:
            body.append(line)
    if header is not None:
        out.append((*header, "\n".join(body)))
    return out


def test_aor_porte_le_nom_de_l_endpoint(sample):
    """Sur un REGISTER, Asterisk cherche un `aor` dont le NOM est la partie
    utilisateur de l'en-tête `To:` — donc l'identifiant SIP du poste.

    Un aor nommé autrement (`salon-aor`) fait échouer tout enregistrement avec
    « AOR '' not found for endpoint 'salon' », un message qui désigne l'appareil
    alors que la faute est dans la configuration générée.
    """
    sections = _sections(generator.build(sample)["pjsip_endpoints.conf"])
    aor_names = {name for name, _, body in sections if "type=aor" in body}
    endpoints = [(name, body) for name, template, body in sections if template]
    assert endpoints, "aucun endpoint généré"

    for name, body in endpoints:
        declared = next(
            line.split("=", 1)[1].strip()
            for line in body.splitlines()
            if line.startswith("aors=")
        )
        assert declared == name, (
            f"endpoint {name} : aors={declared}, alors que l'appareil enverra "
            f"To: <sip:{name}@…> — l'enregistrement échouera"
        )
        assert declared in aor_names, f"aucun objet aor nommé {declared}"


def test_endpoints_contiennent_auth_aor_et_codecs(sample):
    conf = generator.build(sample)["pjsip_endpoints.conf"]

    assert "[salon](endpoint-internal)" in conf
    assert "[salon-auth]" in conf
    assert "aors=salon" in conf
    assert "password=secret1" in conf
    assert "mailboxes=100@default" in conf

    # Le pont FXO doit hériter du gabarit trunk, qui interdit le transfert.
    assert "[fxo](endpoint-trunk)" in conf

    # Codecs propres au poste mobile, précédés d'un disallow=all.
    mobile = conf.split("[mobile](")[1].split("\n\n")[0]
    assert "disallow=all" in mobile
    assert "allow=opus" in mobile
    assert "max_contacts=2" in generator.build(sample)["pjsip_endpoints.conf"]


def test_failover_chaine_les_trunks_dans_l_ordre(sample):
    conf = generator.build(sample)["extensions_generated.conf"]

    # Le premier trunk saute vers le second, le second vers `nomore`.
    assert "same => n(trunk1),NoOp(Essai via Freebox)" in conf
    assert 'GotoIf($["${DIALSTATUS}" = "CHANUNAVAIL"]?trunk2)' in conf
    assert "same => n(trunk2),NoOp(Essai via GSM)" in conf
    assert 'GotoIf($["${DIALSTATUS}" = "CHANUNAVAIL"]?nomore)' in conf
    assert "same => n(nomore)" in conf

    # Gabarits correctement substitués.
    assert "Dial(PJSIP/${NUM}@fxo,30)" in conf
    assert "Dial(Quectel/quectel0/${NUM},30)" in conf


def test_trunk_e164_reecrit_sauf_numeros_courts(sample):
    conf = generator.build(sample)["extensions_generated.conf"]

    # Le trunk GSM est en E.164 : réécriture conditionnée à COURT = 0.
    assert "ExecIf($[${COURT} = 0]?Set(NUM=+33${NUM_NAT:1}):Set(NUM=${NUM_NAT}))" in conf
    # Le trunk Freebox est en national : aucune réécriture.
    assert "same => n,Set(NUM=${NUM_NAT})" in conf


def test_urgences_presentes_et_non_filtrees(sample):
    conf = generator.build(sample)["extensions_generated.conf"]
    for number in ("15", "17", "18", "112"):
        assert f"exten => {number},1,NoOp(APPEL URGENCE" in conf


def test_international_bloque_par_defaut(sample):
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "exten => _00.,1,NoOp(International bloque ${EXTEN})" in conf

    sample.execute("UPDATE settings SET value = '1' WHERE key = 'international_allowed'")
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "exten => _00.,1,NoOp(Sortant international ${EXTEN})" in conf


def test_personne_sonne_tous_ses_postes(sample):
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "GoSub(sub-ring,s,1(PJSIP/mobile&PJSIP/salon,30,201))" in conf


def test_menu_vocal_liste_les_personnes(sample):
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "exten => 1,1,NoOp(Demande de Camille)" in conf


def test_boites_vocales_generees(sample):
    conf = generator.build(sample)["voicemail_generated.conf"]
    assert "201 => 4821,Camille" in conf   # PIN personnel, tiré au hasard à la création
    assert conf.count("=>") == 4           # 100 (poste), 199 (groupe + générale), 201


def test_pin_de_repli_est_stable(sample):
    """Un PIN qui changerait à chaque génération rendrait la boîte inutilisable."""
    first = generator.build(sample)["voicemail_generated.conf"]
    second = generator.build(sample)["voicemail_generated.conf"]
    assert first == second


def test_aucun_pin_de_repli_n_est_trivial(sample):
    """Le calcul déterministe doit éviter 1234, 0000 et compagnie : ce sont les
    premiers codes essayés par qui tombe sur le menu de messagerie."""
    from app import security

    for box in [str(n) for n in range(100, 400)] + ["199", "0", ""]:
        assert not security.is_trivial_pin(generator._stable_pin(box)), box


def test_callerid_urgence_absent_quand_le_reglage_est_vide(sample):
    """Émettre Set(CALLERID(num)=) effacerait le numéro présenté aux secours."""
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "Set(CALLERID(num)=)" not in conf

    sample.execute("UPDATE settings SET value = '0102030405' WHERE key = 'emergency_callerid'")
    conf = generator.build(sample)["extensions_generated.conf"]
    assert "Set(CALLERID(num)=0102030405)" in conf


def test_generation_deterministe(sample):
    """Deux générations identiques : sans ça, le diff de l'écran « Appliquer »
    serait bruité à chaque visite et deviendrait illisible."""
    assert generator.build(sample) == generator.build(sample)


# --- Validation -------------------------------------------------------------

def _messages(problems, level=None):
    return [p.message for p in problems if level is None or p.level == level]


def test_validation_refuse_un_numero_en_double(sample):
    sample.execute("UPDATE users SET extension = '100' WHERE name = 'Camille'")
    errors = _messages(generator.validate(sample), "error")
    assert any("attribué deux fois" in m for m in errors)


def test_validation_refuse_un_numero_d_urgence(sample):
    sample.execute("UPDATE devices SET extension = '112' WHERE slug = 'salon'")
    errors = _messages(generator.validate(sample), "error")
    assert any("numéro d'urgence" in m for m in errors)


def test_validation_refuse_un_gabarit_sans_placeholder(sample):
    sample.execute("UPDATE trunks SET dial_template = 'PJSIP/fxo' WHERE slug = 'freebox'")
    errors = _messages(generator.validate(sample), "error")
    assert any("{num}" in m for m in errors)


def test_validation_signale_une_personne_sans_poste(sample):
    sample.execute("DELETE FROM user_devices")
    warnings = _messages(generator.validate(sample), "warning")
    assert any("n'a aucun poste actif" in m for m in warnings)


def test_validation_signale_l_absence_de_trunk(conn):
    warnings = _messages(generator.validate(conn), "warning")
    assert any("Aucun trunk sortant" in m for m in warnings)


def test_poste_desactive_disparait_de_la_config(sample):
    sample.execute("UPDATE devices SET enabled = 0 WHERE slug = 'salon'")
    bundle = generator.build(sample)
    assert "[salon](" not in bundle["pjsip_endpoints.conf"]
    # Et la personne ne sonne plus que sur son poste restant.
    assert "GoSub(sub-ring,s,1(PJSIP/mobile,30,201))" in bundle["extensions_generated.conf"]


# --- Écriture et diff -------------------------------------------------------

def test_apply_ecrit_les_fichiers_et_archive_une_revision(sample):
    result = generator.apply(sample, "test", "jeu d'essai")
    assert result["ok"] is True

    for name in generator.GENERATED_FILES:
        path = generator.config.GENERATED_DIR / name
        assert path.exists() and path.read_text(encoding="utf-8")

    revision = sample.execute("SELECT * FROM revisions ORDER BY id DESC LIMIT 1").fetchone()
    assert revision["summary"] == "jeu d'essai"
    assert generator.is_dirty(sample) is False


def test_apply_refuse_quand_la_validation_echoue(sample):
    sample.execute("UPDATE devices SET extension = '15' WHERE slug = 'salon'")
    result = generator.apply(sample, "test", "config cassée")
    assert result["ok"] is False
    assert "refusée" in result["log"]


def test_diff_montre_les_ajouts(sample):
    generator.apply(sample, "test", "état initial")
    sample.execute(
        "INSERT INTO devices (slug, label, kind, extension, secret) "
        "VALUES ('garage', 'Garage', 'fxs', '102', 'secret4')"
    )
    sample.commit()

    diff = generator.diff(generator.build(sample))
    assert "+[garage](endpoint-internal)" in diff
    assert generator.is_dirty(sample) is True


def test_rollback_restaure_les_fichiers_precedents(sample):
    generator.apply(sample, "test", "état initial")
    revision_id = sample.execute("SELECT id FROM revisions ORDER BY id DESC LIMIT 1").fetchone()["id"]

    sample.execute(
        "INSERT INTO devices (slug, label, kind, extension, secret) "
        "VALUES ('garage', 'Garage', 'fxs', '102', 'secret4')"
    )
    sample.commit()
    generator.apply(sample, "test", "ajout garage")
    assert "[garage](" in (generator.config.GENERATED_DIR / "pjsip_endpoints.conf").read_text()

    generator.rollback(sample, revision_id, "test")
    assert "[garage](" not in (generator.config.GENERATED_DIR / "pjsip_endpoints.conf").read_text()
