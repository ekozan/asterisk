#!/bin/bash
# Installe l'UI de gestion, et — sur demande explicite — déploie les fichiers de
# configuration Asterisk de ce dépôt.
#
#   sudo ./scripts/install.sh                      # UI seulement
#   sudo ./scripts/install.sh --with-asterisk-conf # + fichiers /etc/asterisk
#
# Ce script ne compile pas Asterisk : voir docs/02-installation.md pour cette
# partie, qui se fait une fois et mérite d'être suivie à la main.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PREFIX=/opt/telephonie
DATA=/var/lib/telephonie
ASTERISK_ETC=/etc/asterisk
WITH_CONF=0

for arg in "$@"; do
  case "$arg" in
    --with-asterisk-conf) WITH_CONF=1 ;;
    *) echo "Option inconnue : $arg" >&2; exit 2 ;;
  esac
done

if [[ $EUID -ne 0 ]]; then
  echo "À lancer avec sudo." >&2
  exit 1
fi

if ! id asterisk >/dev/null 2>&1; then
  echo "L'utilisateur système 'asterisk' n'existe pas. Installez Asterisk d'abord" >&2
  echo "(docs/02-installation.md)." >&2
  exit 1
fi

echo "==> Dépendances système"
apt-get update -qq
apt-get install -y -qq python3-venv python3-pip sox

echo "==> Copie de l'application dans $PREFIX"
install -d -o asterisk -g asterisk "$PREFIX"
# Un rsync par répertoire, pour que --delete ne s'applique qu'aux répertoires
# copiés : le venv, qui vit à côté, ne doit jamais être emporté.
for dir in app scripts docs; do
  rsync -a --delete --exclude '__pycache__' "$REPO/$dir/" "$PREFIX/$dir/"
done
install -m 0644 "$REPO/requirements.txt" "$PREFIX/requirements.txt"
chown -R asterisk:asterisk "$PREFIX/app" "$PREFIX/scripts" "$PREFIX/docs" \
  "$PREFIX/requirements.txt"

echo "==> Environnement Python"
if [[ ! -d "$PREFIX/venv" ]]; then
  python3 -m venv "$PREFIX/venv"
fi
"$PREFIX/venv/bin/pip" install --quiet --upgrade pip
"$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt"
chown -R asterisk:asterisk "$PREFIX/venv"

echo "==> Répertoires de données"
install -d -o asterisk -g asterisk -m 0750 "$DATA"
install -d -o asterisk -g asterisk -m 0750 "$ASTERISK_ETC/generated"
install -d -o asterisk -g asterisk -m 0755 /var/lib/asterisk/sounds/custom

if [[ $WITH_CONF -eq 1 ]]; then
  BACKUP="$ASTERISK_ETC/backup-$(date +%Y%m%d-%H%M%S)"
  echo "==> Sauvegarde de la configuration actuelle dans $BACKUP"
  install -d -m 0750 "$BACKUP"
  for file in pjsip.conf extensions.conf voicemail.conf logger.conf rtp.conf \
              cdr.conf cdr_manager.conf manager.conf; do
    [[ -f "$ASTERISK_ETC/$file" ]] && cp -a "$ASTERISK_ETC/$file" "$BACKUP/"
  done

  echo "==> Déploiement des fichiers de configuration"
  for file in pjsip.conf extensions.conf voicemail.conf logger.conf rtp.conf \
              cdr.conf cdr_manager.conf manager.conf; do
    install -o asterisk -g asterisk -m 0640 "$REPO/asterisk/$file" "$ASTERISK_ETC/$file"
  done

  # Les fichiers générés doivent exister dès le premier démarrage, sinon les
  # #include font échouer le chargement des modules.
  for file in pjsip_endpoints.conf extensions_generated.conf voicemail_generated.conf; do
    if [[ ! -f "$ASTERISK_ETC/generated/$file" ]]; then
      printf '; placeholder — sera écrit par l UI au premier « Appliquer »\n' \
        > "$ASTERISK_ETC/generated/$file"
      chown asterisk:asterisk "$ASTERISK_ETC/generated/$file"
      chmod 0640 "$ASTERISK_ETC/generated/$file"
    fi
  done
  echo "    Ancienne configuration conservée dans $BACKUP"
fi

echo "==> Compte AMI du pont Home Assistant"
# Le secret n'est pas dans le dépôt : versionné, il serait le même partout. Il
# est tiré une fois et jamais renouvelé automatiquement — le régénérer à chaque
# installation couperait le pont sans que rien ne le dise.
AMI_ACCOUNT=$ASTERISK_ETC/manager.d/telephonie-events.conf
if [[ -f "$AMI_ACCOUNT" ]]; then
  echo "    $AMI_ACCOUNT existe déjà, secret conservé"
  # La classe `cdr` a été ajoutée après coup : sans elle, les enregistrements
  # d'appel sont émis par Asterisk mais jamais délivrés, en silence.
  if ! grep -q '^read = .*cdr' "$AMI_ACCOUNT"; then
    sed -i 's/^read = .*/read = system,call,dialplan,cdr/' "$AMI_ACCOUNT"
    echo "    classe « cdr » ajoutée au compte AMI"
  fi
else
  install -d -o asterisk -g asterisk -m 0750 "$ASTERISK_ETC/manager.d"
  AMI_SECRET=$(head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32)
  umask 027
  cat > "$AMI_ACCOUNT" <<EOF
; Compte AMI du service telephonie-events. Écrit par scripts/install.sh.
; Lecture seule et limité à la boucle locale : ce compte ne peut pas originer
; d'appel ni recharger la configuration.
[telephonie-events]
secret = $AMI_SECRET
deny = 0.0.0.0/0
permit = 127.0.0.1/32
read = system,call,dialplan,cdr
write =
EOF
  chown asterisk:asterisk "$AMI_ACCOUNT"
  chmod 0640 "$AMI_ACCOUNT"
  echo "    compte créé dans $AMI_ACCOUNT"
fi

echo "==> Services systemd"
install -m 0644 "$REPO/systemd/telephonie-ui.service" /etc/systemd/system/
install -m 0644 "$REPO/systemd/telephonie-prov.service" /etc/systemd/system/
install -m 0644 "$REPO/systemd/telephonie-events.service" /etc/systemd/system/
install -d -m 0755 /etc/telephonie

# Gabarit d'environnement du pont, créé vide de courtier : sans
# TELEPHONIE_MQTT_HOST, le service refuse de démarrer plutôt que de tourner à
# vide. C'est à l'exploitant de renseigner son courtier.
if [[ ! -f /etc/telephonie/events.env ]]; then
  {
    echo "# Pont vers Home Assistant. Renseignez le courtier MQTT puis :"
    echo "#   sudo systemctl enable --now telephonie-events"
    echo "# Voir docs/11-home-assistant.md"
    echo "TELEPHONIE_MQTT_HOST="
    echo "TELEPHONIE_MQTT_PORT=1883"
    echo "TELEPHONIE_MQTT_USER="
    echo "TELEPHONIE_MQTT_PASSWORD="
    echo "TELEPHONIE_AMI_USER=telephonie-events"
    echo "TELEPHONIE_AMI_SECRET=$(sed -n 's/^secret = //p' "$AMI_ACCOUNT")"
  } > /etc/telephonie/events.env
  chown root:asterisk /etc/telephonie/events.env
  chmod 0640 /etc/telephonie/events.env
fi

systemctl daemon-reload
systemctl enable --now telephonie-ui.service
# Le service de provisionnement écoute sur la boucle locale tant que
# /etc/telephonie/prov.env ne lui donne pas l'IP du VLAN voix : aucun ATA ne
# peut donc l'atteindre avant que ce soit un choix explicite.
systemctl enable --now telephonie-prov.service

echo
echo "Terminé."
echo "  UI       : http://127.0.0.1:8080 (créez le compte admin au premier accès)"
echo "  Tunnel   : ssh -L 8080:127.0.0.1:8080 <vous>@<vm>"
echo "  Données  : $DATA/telephonie.db"
echo "  Provis.  : écoute sur 127.0.0.1:8081 — pour l'ouvrir au VLAN voix, voir"
echo "             docs/10-provisionnement.md"
echo "  Home Ass.: renseignez le courtier MQTT dans /etc/telephonie/events.env,"
echo "             puis: sudo systemctl enable --now telephonie-events"
echo
echo "Pour partir de l'installation de référence plutôt que d'une base vide :"
echo "  sudo -u asterisk $PREFIX/venv/bin/python $PREFIX/scripts/seed.py"
