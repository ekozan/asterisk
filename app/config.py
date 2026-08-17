"""Configuration de l'application, entièrement pilotée par variables d'environnement.

Les valeurs par défaut correspondent à une installation faite avec
scripts/install.sh sur la VM Asterisk. En développement, il suffit de pointer
TELEPHONIE_DATA_DIR et TELEPHONIE_ASTERISK_DIR vers des répertoires locaux.
"""

from __future__ import annotations

import os
from pathlib import Path


def _env_path(name: str, default: str) -> Path:
    return Path(os.environ.get(name, default)).expanduser()


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Répertoire de données de l'application (base SQLite, révisions).
DATA_DIR: Path = _env_path("TELEPHONIE_DATA_DIR", "/var/lib/telephonie")
DB_PATH: Path = Path(os.environ.get("TELEPHONIE_DB", str(DATA_DIR / "telephonie.db")))

# Répertoire de configuration d'Asterisk et sous-répertoire des fichiers générés.
ASTERISK_DIR: Path = _env_path("TELEPHONIE_ASTERISK_DIR", "/etc/asterisk")
GENERATED_DIR: Path = Path(
    os.environ.get("TELEPHONIE_GENERATED_DIR", str(ASTERISK_DIR / "generated"))
)

# Binaire Asterisk utilisé pour les rechargements et les commandes CLI.
ASTERISK_BIN: str = os.environ.get("TELEPHONIE_ASTERISK_BIN", "/usr/sbin/asterisk")

# Quand False, generator.apply() écrit les fichiers mais ne recharge pas Asterisk.
# Utile en développement, sur une machine sans Asterisk installé.
RELOAD_ENABLED: bool = _env_bool("TELEPHONIE_RELOAD", True)

# Script de génération des messages vocaux (TTS Piper). Optionnel.
TTS_SCRIPT: str = os.environ.get("TELEPHONIE_TTS_SCRIPT", "/opt/piper-voices/generate.sh")
TTS_ENABLED: bool = _env_bool("TELEPHONIE_TTS", True)

# Fichier CDR au format CSV, lu en lecture seule pour l'écran "Journal d'appels".
CDR_CSV: Path = _env_path("TELEPHONIE_CDR_CSV", "/var/log/asterisk/cdr-csv/Master.csv")

# Jeton d'API pour les intégrations machine (Home Assistant...). Vide = API désactivée.
API_TOKEN: str = os.environ.get("TELEPHONIE_API_TOKEN", "")

# Durée de vie d'une session UI, en heures.
SESSION_HOURS: int = int(os.environ.get("TELEPHONIE_SESSION_HOURS", "12"))

# Cookie de session marqué Secure (à activer derrière un reverse proxy HTTPS).
COOKIE_SECURE: bool = _env_bool("TELEPHONIE_COOKIE_SECURE", False)

# Nombre de révisions de configuration conservées.
REVISIONS_KEPT: int = int(os.environ.get("TELEPHONIE_REVISIONS_KEPT", "50"))

# Valeurs par défaut des réglages stockés en base (table settings).
DEFAULT_SETTINGS: dict[str, str] = {
    "site_name": "Téléphonie maison",
    "language": "fr",
    "tonezone": "fr",
    "international_allowed": "0",
    "block_premium": "1",
    "national_prefix": "0",
    "country_code": "33",
    "emergency_callerid": "",
    "ivr_enabled": "1",
    "ivr_greeting": "custom/menu-niveau1",
    "ivr_family_group": "famille",
    "ivr_people_prompt": "custom/menu-personne",
    "ivr_timeout": "10",
    "hotline_prompt": "custom/compose-1-pour-appeler",
    "general_voicemail": "199",
    "voicemail_email_from": "asterisk@localhost",
    "outbound_ring_time": "30",
}
