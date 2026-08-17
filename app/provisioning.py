"""Provisionnement automatique des ATA Grandstream.

Principe : l'appareil va chercher son fichier de configuration au démarrage, à
une URL dérivée de son adresse MAC. Plus besoin de saisir l'identifiant et le
mot de passe SIP dans son interface web, ni de recommencer après un retour aux
réglages d'usine ou une régénération de mot de passe.

    HT801 démarre ──▶ GET http://10.0.90.20:8081/cfg000b82aabbcc.xml
                              │
                              ▼
                    app/prov.py lit la base et rend le XML

─────────────────────────────────────────────────────────────────────────────
AVERTISSEMENT SUR LES NUMÉROS DE P-VALUE

Grandstream désigne chaque réglage par un numéro (« P-value ») plutôt que par
un nom, et ces numéros varient d'un modèle et d'une version de firmware à
l'autre. Ceux du profil ci-dessous sont les valeurs couramment documentées
pour la série HT80x, mais ils ne sont PAS vérifiés sur votre matériel.

Un numéro erroné ne produit aucune erreur : l'appareil ignore silencieusement
la ligne et garde son ancien réglage. Un ATA qui ne s'enregistre pas après
provisionnement vient presque toujours de là.

D'où `verified = False` ci-dessous, l'avertissement affiché dans l'interface,
et surtout scripts/prov-import.py, qui déduit les bons numéros d'un appareil
que vous avez déjà configuré à la main. Voir docs/10-provisionnement.md.
─────────────────────────────────────────────────────────────────────────────
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from . import db

MAC_RE = re.compile(r"^[0-9a-f]{12}$")

# Correspondance codec -> identifiant de vocodeur Grandstream, qui reprend les
# numéros de charge utile RTP standard.
VOCODER_IDS = {
    "ulaw": "0",
    "pcmu": "0",
    "g726": "2",
    "alaw": "8",
    "pcma": "8",
    "g722": "9",
    "g729": "18",
}


@dataclass
class ProvProfile:
    """Un modèle d'appareil : comment traduire nos réglages en P-values."""

    key: str
    label: str
    vendor: str
    # nom logique -> numéro de P-value (sans le « P »)
    fields: dict[str, str]
    # False tant que les P-values n'ont pas été confirmées sur le matériel réel.
    verified: bool = False
    notes: str = ""
    # Réglages que ce profil ne touche volontairement pas.
    untouched: list[str] = field(default_factory=list)


GRANDSTREAM_HT80X = ProvProfile(
    key="grandstream-ht80x",
    label="Grandstream HT801 / HT802 (port FXS)",
    vendor="grandstream",
    fields={
        # Compte SIP du port FXS — le strict nécessaire pour s'enregistrer.
        "account_active": "271",
        "account_name": "270",
        "sip_server": "47",
        "outbound_proxy": "48",
        "sip_user_id": "35",
        "auth_id": "36",
        "auth_password": "34",
        "display_name": "3",
        # Codecs, dans l'ordre de préférence.
        "vocoder_1": "57",
        "vocoder_2": "58",
        "vocoder_3": "59",
        # Mot de passe d'administration de l'ATA (optionnel).
        "admin_password": "2",
    },
    verified=False,
    notes=(
        "Numéros issus de la documentation courante de la série HT80x, non "
        "confirmés sur votre firmware. Validez-les avec scripts/prov-import.py "
        "avant de provisionner plusieurs appareils."
    ),
    untouched=[
        "Zone de tonalité et affichage du numéro d'appelant",
        "Réglages FXO du HT813 (seuil de détection de raccroché notamment) : "
        "un mauvais réglage bloque la ligne, il reste manuel volontairement",
    ],
)

PROFILES: dict[str, ProvProfile] = {p.key: p for p in (GRANDSTREAM_HT80X,)}
DEFAULT_PROFILE = GRANDSTREAM_HT80X.key


# --- Adresses MAC -----------------------------------------------------------

def normalize_mac(raw: str | None) -> str | None:
    """Ramène `00:0B:82:AA:BB:CC`, `00-0b-82-aa-bb-cc`, `000B82AABBCC` à une
    forme unique : 12 caractères hexadécimaux minuscules, comme dans le nom de
    fichier que réclame l'appareil. Retourne None si ce n'est pas une MAC."""
    if not raw:
        return None
    hexes = re.sub(r"[^0-9a-fA-F]", "", raw).lower()
    return hexes if MAC_RE.match(hexes) else None


def format_mac(mac: str | None) -> str:
    """Forme lisible `00:0b:82:aa:bb:cc`, pour l'affichage seulement."""
    if not mac or not MAC_RE.match(mac):
        return "—"
    return ":".join(mac[i:i + 2] for i in range(0, 12, 2))


def config_filename(mac: str) -> str:
    return f"cfg{mac}.xml"


# --- Génération du fichier de configuration ---------------------------------

def _pvalues_for_device(device: sqlite3.Row | dict, settings: dict[str, str],
                        profile: ProvProfile) -> list[tuple[str, str, str]]:
    """Construit la liste (P-value, valeur, commentaire) pour un appareil."""
    f = profile.fields
    out: list[tuple[str, str, str]] = []

    def put(logical: str, value: str, comment: str) -> None:
        pnum = f.get(logical)
        if pnum and value != "":
            out.append((pnum, value, comment))

    put("account_active", "1", "compte actif")
    put("account_name", device["label"], "nom du compte")
    put("display_name", device["label"], "nom affiché")
    put("sip_server", settings.get("prov_sip_server", ""), "serveur SIP")
    put("sip_user_id", device["slug"], "identifiant SIP")
    put("auth_id", device["slug"], "identifiant d'authentification")
    put("auth_password", device["secret"], "mot de passe SIP")

    # L'appareil parle directement à Asterisk : pas de proxy sortant. On pousse
    # une valeur vide pour effacer un proxy qui traînerait d'une config
    # précédente — c'est une cause classique d'ATA muet après recyclage.
    pnum = f.get("outbound_proxy")
    if pnum:
        out.append((pnum, "", "proxy sortant (vidé volontairement)"))

    codecs = [c.strip().lower() for c in (device["codecs"] or "").split(",") if c.strip()]
    for index, codec in enumerate(codecs[:3], start=1):
        vocoder = VOCODER_IDS.get(codec)
        if vocoder:
            put(f"vocoder_{index}", vocoder, f"codec {index} : {codec}")

    put("admin_password", settings.get("prov_admin_password", ""),
        "mot de passe d'administration de l'ATA")

    return out


def _comment_safe(text: str) -> str:
    """Rend un texte utilisable dans un commentaire XML.

    Un commentaire ne peut ni contenir `--` ni se terminer par `-` : un poste
    nommé « Salon -- essai » produirait un fichier invalide, que l'appareil
    rejetterait en entier plutôt qu'en partie. Les entités ne sont pas
    interprétées dans un commentaire, donc on assainit au lieu d'échapper.
    """
    cleaned = re.sub(r"-{2,}", "-", str(text or "")).replace("<", "(").replace(">", ")")
    return cleaned.rstrip("-").strip() or "sans nom"


def build_config(device: sqlite3.Row | dict, settings: dict[str, str],
                 profile: ProvProfile | None = None) -> str:
    """Rend le XML de provisionnement d'un appareil."""
    profile = profile or PROFILES.get(
        (device["prov_profile"] or DEFAULT_PROFILE), GRANDSTREAM_HT80X
    )
    pvalues = _pvalues_for_device(device, settings, profile)

    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!--",
        f"  Configuration générée pour {_comment_safe(device['label'])} "
        f"({_comment_safe(device['slug'])}).",
        f"  Profil : {_comment_safe(profile.label)}",
        "  Fichier produit à la volée par l'interface de gestion : toute",
        "  modification faite ici serait perdue au prochain démarrage de",
        "  l'appareil. Modifiez le poste dans l'interface.",
        "-->",
        '<gs_provision version="1">',
        f"  <mac>{escape(device['mac'])}</mac>",
        '  <config version="1">',
    ]
    for pnum, value, comment in pvalues:
        lines.append(f"    <!-- {_comment_safe(comment)} -->")
        lines.append(f"    <P{pnum}>{escape(value)}</P{pnum}>")
    lines += ["  </config>", "</gs_provision>", ""]
    return "\n".join(lines)


# --- Recherche d'un appareil ------------------------------------------------

def device_by_mac(conn: sqlite3.Connection, mac: str) -> sqlite3.Row | None:
    """Poste actif portant cette MAC. Un poste désactivé n'est pas servi :
    couper un poste dans l'interface doit vraiment le couper."""
    return db.one(
        conn,
        "SELECT * FROM devices WHERE mac = ? AND enabled = 1 AND mac IS NOT NULL",
        (mac,),
    )


def provisionable_devices(conn: sqlite3.Connection) -> list[dict]:
    """Tous les postes, avec leur état de provisionnement, pour l'écran dédié."""
    rows = []
    for row in conn.execute("SELECT * FROM devices ORDER BY COALESCE(extension, slug)"):
        item = dict(row)
        item["mac_display"] = format_mac(row["mac"])
        item["profile"] = PROFILES.get(row["prov_profile"] or "")
        item["filename"] = config_filename(row["mac"]) if row["mac"] else None
        rows.append(item)
    return rows
