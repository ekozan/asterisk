#!/bin/bash
# Génère un message vocal avec Piper et le dépose au format attendu par Asterisk.
#
#   generate-prompts.sh "Texte à prononcer" nom-du-fichier
#
# Le fichier atterrit dans /var/lib/asterisk/sounds/custom/<nom>.wav et devient
# jouable par Playback(custom/<nom>) dans le dialplan.
#
# Ce script est aussi appelé automatiquement par l'UI (variable
# TELEPHONIE_TTS_SCRIPT) pour régénérer l'annonce du menu « joindre une
# personne » quand la liste des personnes change.

set -euo pipefail

TEXT="${1:?usage: generate-prompts.sh \"texte\" nom-du-fichier}"
NAME="${2:?usage: generate-prompts.sh \"texte\" nom-du-fichier}"

VOICE="${PIPER_VOICE:-/opt/piper-voices/fr_FR-siwis-medium.onnx}"
SOUNDS="${ASTERISK_SOUNDS:-/var/lib/asterisk/sounds/custom}"
RAW="$(mktemp -t piper-XXXXXX.wav)"
trap 'rm -f "$RAW"' EXIT

if [[ ! -f "$VOICE" ]]; then
  echo "Voix Piper introuvable : $VOICE" >&2
  echo "Téléchargez-en une depuis https://github.com/rhasspy/piper (voir docs/02-installation.md)." >&2
  exit 1
fi

mkdir -p "$SOUNDS"

echo "$TEXT" | piper --model "$VOICE" --output_file "$RAW"

# Asterisk attend du 8 kHz mono 16 bits pour les codecs G.711 ; sans cette
# conversion, le message est joué trop vite ou pas du tout. Le léger gain
# négatif évite la saturation, très audible sur un combiné analogique.
sox "$RAW" -r 8000 -c 1 -b 16 "$SOUNDS/$NAME.wav" gain -3

chmod 0644 "$SOUNDS/$NAME.wav"
echo "Généré : $SOUNDS/$NAME.wav"
