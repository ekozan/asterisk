-- Schéma de la base de gestion téléphonie.
-- La base est la SOURCE DE VÉRITÉ : les fichiers /etc/asterisk/generated/*.conf
-- en sont dérivés par app/generator.py. Ne jamais éditer les fichiers générés.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- Réglages globaux (clé/valeur, valeurs par défaut insérées au bootstrap)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- Postes : tout ce qui est un endpoint PJSIP (ATA, DECT, softphone, pont FXO)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS devices (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    slug           TEXT    NOT NULL UNIQUE,   -- nom de l'endpoint PJSIP (a-z0-9-)
    label          TEXT    NOT NULL,          -- "Salon", "Garage"...
    kind           TEXT    NOT NULL,          -- fxs | dect | mobile | fxo | console
    extension      TEXT    UNIQUE,            -- numéro interne (NULL pour un pont FXO)
    secret         TEXT    NOT NULL,          -- mot de passe SIP (généré)
    codecs         TEXT    NOT NULL DEFAULT 'alaw,ulaw',
    max_contacts   INTEGER NOT NULL DEFAULT 1,
    mailbox        TEXT,                      -- boîte vocale rattachée (ex: 100)
    dial_mode      TEXT    NOT NULL DEFAULT 'direct',  -- direct | hotline
    hotline_target TEXT,                      -- extension composée au décroché si hotline
    ring_time      INTEGER NOT NULL DEFAULT 30,
    notes          TEXT,
    -- Provisionnement automatique : adresse MAC normalisée (12 hex minuscules)
    -- et profil d'appareil. NULL = l'appareil se configure à la main.
    mac            TEXT    UNIQUE,
    prov_profile   TEXT,
    enabled        INTEGER NOT NULL DEFAULT 1,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (kind IN ('fxs','dect','mobile','fxo','console')),
    CHECK (dial_mode IN ('direct','hotline')),
    CHECK (max_contacts >= 1)
);

-- ---------------------------------------------------------------------------
-- Personnes : une personne peut avoir plusieurs postes (sonnerie simultanée)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    name          TEXT    NOT NULL,
    extension     TEXT    UNIQUE,        -- numéro personnel (ex: 201)
    menu_digit    TEXT    UNIQUE,        -- touche dans l'IVR "joindre une personne"
    voicemail_box TEXT    UNIQUE,
    voicemail_pin TEXT,
    email         TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    updated_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS user_devices (
    user_id   INTEGER NOT NULL REFERENCES users(id)   ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id) ON DELETE CASCADE,
    PRIMARY KEY (user_id, device_id)
);

-- ---------------------------------------------------------------------------
-- Groupes d'appel : "sonner partout", "rez-de-chaussée"...
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS ring_groups (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT    NOT NULL UNIQUE,
    label         TEXT    NOT NULL,
    extension     TEXT    UNIQUE,
    ring_time     INTEGER NOT NULL DEFAULT 30,
    voicemail_box TEXT,                  -- boîte de repli si personne ne répond
    enabled       INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS ring_group_members (
    group_id  INTEGER NOT NULL REFERENCES ring_groups(id) ON DELETE CASCADE,
    device_id INTEGER NOT NULL REFERENCES devices(id)     ON DELETE CASCADE,
    PRIMARY KEY (group_id, device_id)
);

-- ---------------------------------------------------------------------------
-- Numéros abrégés : composer 5 -> appelle 06...
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS speed_dials (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    code    TEXT    NOT NULL UNIQUE,     -- ce qu'on compose (ex: *1, 5)
    number  TEXT    NOT NULL,            -- numéro réel appelé
    label   TEXT,
    enabled INTEGER NOT NULL DEFAULT 1
);

-- ---------------------------------------------------------------------------
-- Routes sortantes : ordre d'essai des trunks (Freebox FXO puis GSM)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trunks (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT    NOT NULL UNIQUE,
    label         TEXT    NOT NULL,
    dial_template TEXT    NOT NULL,      -- ex: PJSIP/{num}@grandstream-fxo
    number_format TEXT    NOT NULL DEFAULT 'national',  -- national | e164
    priority      INTEGER NOT NULL DEFAULT 10,
    timeout       INTEGER NOT NULL DEFAULT 30,
    enabled       INTEGER NOT NULL DEFAULT 1,
    notes         TEXT,
    CHECK (number_format IN ('national','e164'))
);

-- ---------------------------------------------------------------------------
-- Comptes d'administration de l'UI
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS admins (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT    NOT NULL UNIQUE,
    password_hash TEXT    NOT NULL,      -- scrypt$n$r$p$salt_b64$hash_b64
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    admin_id   INTEGER NOT NULL REFERENCES admins(id) ON DELETE CASCADE,
    csrf       TEXT    NOT NULL,
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    expires_at TEXT    NOT NULL
);

-- ---------------------------------------------------------------------------
-- Historique des configurations générées (permet le rollback)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS revisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    author      TEXT    NOT NULL,
    summary     TEXT    NOT NULL,
    bundle      TEXT    NOT NULL,        -- JSON {nom_fichier: contenu}
    applied     INTEGER NOT NULL DEFAULT 0,
    reload_log  TEXT
);

CREATE TABLE IF NOT EXISTS audit_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    at     TEXT NOT NULL DEFAULT (datetime('now')),
    actor  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT
);

CREATE INDEX IF NOT EXISTS idx_audit_at      ON audit_log(at DESC);
CREATE INDEX IF NOT EXISTS idx_revisions_at  ON revisions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_exp  ON sessions(expires_at);
