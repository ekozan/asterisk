#!/bin/bash
# Sauvegarde tout ce qui ne se reconstruit pas : la base de l'UI, la
# configuration Asterisk, les messages vocaux enregistrés et les sons générés.
#
#   sudo ./scripts/backup.sh /mnt/nas/telephonie
#
# Le binaire Asterisk et le venv Python ne sont pas sauvegardés : ils se
# réinstallent en suivant docs/02-installation.md.

set -euo pipefail

DEST="${1:?usage: backup.sh <répertoire de destination>}"
STAMP="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="$DEST/telephonie-$STAMP.tar.gz"

if [[ $EUID -ne 0 ]]; then
  echo "À lancer avec sudo (lecture de /var/spool/asterisk)." >&2
  exit 1
fi

mkdir -p "$DEST"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Copie cohérente de SQLite : `.backup` gère le mode WAL, contrairement à un
# simple cp qui peut capturer une base à moitié écrite.
if [[ -f /var/lib/telephonie/telephonie.db ]]; then
  sqlite3 /var/lib/telephonie/telephonie.db ".backup '$WORK/telephonie.db'"
fi

tar czf "$ARCHIVE" \
  -C "$WORK" $([[ -f "$WORK/telephonie.db" ]] && echo telephonie.db) \
  -C / \
  etc/asterisk \
  var/lib/asterisk/sounds/custom \
  $([[ -d /var/spool/asterisk/voicemail ]] && echo var/spool/asterisk/voicemail)

chmod 0600 "$ARCHIVE"
echo "Archive : $ARCHIVE"
echo "Elle contient des mots de passe SIP en clair — stockez-la en conséquence."
