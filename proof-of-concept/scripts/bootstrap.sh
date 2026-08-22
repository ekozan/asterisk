#!/bin/bash
# Installation complète, d'une VM Ubuntu 24.04 vierge à une téléphonie qui marche.
#
#   sudo ./scripts/bootstrap.sh --help
#
# Le script est REPRENABLE : chaque étape réussie est notée, et une seconde
# exécution reprend là où la première s'est arrêtée. C'est ce qui compte sur une
# procédure de vingt minutes de compilation qu'une coupure réseau peut
# interrompre au milieu.
#
# Il délègue la partie applicative à install.sh, qui reste utilisable seul pour
# mettre à jour l'interface sans retoucher au système.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG=/var/log/telephonie-install.log
STATE_DIR=/var/lib/telephonie/.bootstrap
ASTERISK_BRANCH=22
ASTERISK_SRC=/usr/src

# --- Options ----------------------------------------------------------------
WITH_GSM=0
WITH_TTS=1
WITH_HARDENING=0
WITH_CONF=1
ASSUME_YES=0
DRY_RUN=0
FORCE=0
VOIP_NET=""
ADMIN_NET=""
VOIP_IFACE=""
ADMIN_IFACE=""
VOIP_IP=""

usage() {
  cat <<'EOF'
Installation complète de la téléphonie maison (Ubuntu 24.04, Asterisk 22).

  sudo ./scripts/bootstrap.sh [options]

Étapes exécutées, dans l'ordre :
  1. paquets système
  2. compilation et installation d'Asterisk 22, avec les sons français
  3. utilisateur système, unité systemd, répertoires
  4. chan-quectel            (seulement avec --with-gsm)
  5. Piper (synthèse vocale) (par défaut, désactivable)
  6. interface de gestion et configuration Asterisk
  7. durcissement            (seulement avec --with-hardening)
  8. données de départ et démarrage des services

Options :
  --with-gsm              compile chan-quectel pour le module GSM de secours
  --without-tts           n'installe pas Piper (pas d'annonces générées)
  --without-config        n'écrit pas les fichiers de /etc/asterisk
                          (à utiliser pour reprendre une install existante)
  --with-hardening        ufw, fail2ban, SSH par clé, mises à jour auto.
                          Exige --admin-net et --voip-net.
  --voip-net CIDR         réseau des postes SIP,      ex. 10.0.90.0/24
  --admin-net CIDR        réseau d'administration,    ex. 10.0.5.0/24
  --voip-iface NOM        interface du VLAN voix,     ex. ens192
  --admin-iface NOM       interface d'administration, ex. ens160
  --voip-ip IP            IP d'écoute SIP. Déduite de --voip-iface si absente ;
                          sans l'une ni l'autre, Asterisk écoute sur toutes
                          les interfaces, VLAN d'administration compris.
  --force                 réexécute les étapes déjà réussies
  --yes                   ne pose aucune question
  --dry-run               affiche ce qui serait fait, sans rien modifier
  --help                  cette aide

Exemple complet :
  sudo ./scripts/bootstrap.sh --with-gsm --with-hardening \
       --voip-net 10.0.90.0/24 --voip-iface ens192 \
       --admin-net 10.0.5.0/24 --admin-iface ens160
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --with-gsm)        WITH_GSM=1 ;;
    --without-tts)     WITH_TTS=0 ;;
    --without-config)  WITH_CONF=0 ;;
    --with-hardening)  WITH_HARDENING=1 ;;
    --voip-net)        VOIP_NET="${2:?--voip-net attend un CIDR}"; shift ;;
    --admin-net)       ADMIN_NET="${2:?--admin-net attend un CIDR}"; shift ;;
    --voip-iface)      VOIP_IFACE="${2:?--voip-iface attend un nom}"; shift ;;
    --voip-ip)         VOIP_IP="${2:?--voip-ip attend une adresse}"; shift ;;
    --admin-iface)     ADMIN_IFACE="${2:?--admin-iface attend un nom}"; shift ;;
    --force)           FORCE=1 ;;
    --yes|-y)          ASSUME_YES=1 ;;
    --dry-run)         DRY_RUN=1 ;;
    --help|-h)         usage; exit 0 ;;
    *) echo "Option inconnue : $1" >&2; echo; usage >&2; exit 2 ;;
  esac
  shift
done

# --- Sortie -----------------------------------------------------------------
if [[ -t 1 ]]; then
  B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else
  B=""; G=""; Y=""; R=""; N=""
fi

log()  { printf '%s\n' "$*" | tee -a "$LOG" >/dev/null; }
say()  { printf '%s\n' "$*"; log "$*"; }
step() { printf '\n%s==> %s%s\n' "$B" "$*" "$N"; log "==> $*"; }
ok()   { printf '    %s✓%s %s\n' "$G" "$N" "$*"; log "  OK $*"; }
warn() { printf '    %s!%s %s\n' "$Y" "$N" "$*"; log "  WARN $*"; }
die()  { printf '\n%serreur :%s %s\n' "$R" "$N" "$*" >&2; log "ERREUR $*"; exit 1; }

# Toute commande qui modifie la machine passe par run(), ce qui rend --dry-run
# réellement sûr plutôt qu'approximatif.
run() {
  log "\$ $*"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    %s[dry-run]%s %s\n' "$Y" "$N" "$*"
    return 0
  fi
  "$@" >>"$LOG" 2>&1 || die "échec de : $*  (détail dans $LOG)"
}

# Variante pour les commandes dont on veut voir la sortie à l'écran.
run_verbose() {
  log "\$ $*"
  if [[ $DRY_RUN -eq 1 ]]; then
    printf '    %s[dry-run]%s %s\n' "$Y" "$N" "$*"
    return 0
  fi
  "$@" 2>&1 | tee -a "$LOG" || die "échec de : $*"
}

confirm() {
  [[ $ASSUME_YES -eq 1 || $DRY_RUN -eq 1 ]] && return 0
  local answer
  read -r -p "    $1 [o/N] " answer
  [[ "$answer" =~ ^[oOyY]$ ]]
}

# --- Reprise ----------------------------------------------------------------
done_step()  { [[ $FORCE -eq 0 && -f "$STATE_DIR/$1" ]]; }
mark_done()  { [[ $DRY_RUN -eq 1 ]] || { mkdir -p "$STATE_DIR"; touch "$STATE_DIR/$1"; }; }

# ============================================================================
# 0. Contrôles préalables
# ============================================================================
preflight() {
  step "Contrôles préalables"

  [[ $EUID -eq 0 ]] || die "à lancer avec sudo."
  [[ $DRY_RUN -eq 1 ]] || { mkdir -p "$(dirname "$LOG")"; touch "$LOG"; chmod 0640 "$LOG"; }

  if [[ -r /etc/os-release ]]; then
    # shellcheck disable=SC1091
    . /etc/os-release
    [[ "${ID:-}" == "ubuntu" ]] || warn "distribution ${ID:-inconnue} : procédure prévue pour Ubuntu."
    [[ "${VERSION_ID:-}" == "24.04" ]] || warn "Ubuntu ${VERSION_ID:-?} : testé sur 24.04."
  fi

  local free_mb
  [[ $DRY_RUN -eq 1 ]] || mkdir -p "$ASTERISK_SRC"
  free_mb=$(df -Pm "$ASTERISK_SRC" 2>/dev/null | awk 'NR==2 {print $4}')
  [[ ${free_mb:-0} -ge 3000 ]] || die "moins de 3 Go libres sur /usr/src (${free_mb} Mo) : la compilation échouera."
  ok "espace disque : ${free_mb} Mo"

  getent hosts downloads.asterisk.org >/dev/null 2>&1 \
    || die "résolution DNS de downloads.asterisk.org impossible : vérifiez le réseau."
  ok "réseau et DNS"

  if [[ $WITH_HARDENING -eq 1 ]]; then
    [[ -n "$ADMIN_NET" ]] || die "--with-hardening exige --admin-net (sinon le pare-feu vous coupe SSH)."
    [[ -n "$VOIP_NET"  ]] || die "--with-hardening exige --voip-net."
  fi

  # L'IP d'écoute SIP se déduit de l'interface voix quand elle n'est pas donnée.
  if [[ -z "$VOIP_IP" && -n "$VOIP_IFACE" && $DRY_RUN -eq 0 ]]; then
    VOIP_IP=$(ip -4 -o addr show dev "$VOIP_IFACE" 2>/dev/null \
              | awk '{print $4}' | cut -d/ -f1 | head -1)
    [[ -n "$VOIP_IP" ]] && ok "IP d'écoute SIP déduite de $VOIP_IFACE : $VOIP_IP"
  fi
  if [[ -z "$VOIP_IP" ]]; then
    warn "aucune IP d'écoute SIP : Asterisk écoutera sur toutes les interfaces."
    warn "  Utilisez --voip-ip ou --voip-iface pour le restreindre au VLAN voix."
  fi

  [[ $DRY_RUN -eq 1 ]] && warn "mode dry-run : aucune modification ne sera faite"
  ok "prêt — journal détaillé dans $LOG"
}

# ============================================================================
# 1. Paquets système
# ============================================================================
install_packages() {
  if done_step packages; then ok "paquets système déjà installés (--force pour refaire)"; return; fi
  step "Paquets système"

  run env DEBIAN_FRONTEND=noninteractive apt-get update
  run env DEBIAN_FRONTEND=noninteractive apt-get install -y \
    build-essential git wget curl pkg-config ca-certificates \
    libjansson-dev libxml2-dev libsqlite3-dev uuid-dev libedit-dev libssl-dev \
    libsrtp2-dev sox alsa-utils usbutils cmake \
    python3-venv python3-pip sqlite3 rsync

  run timedatectl set-timezone Europe/Paris
  ok "paquets installés, fuseau horaire Europe/Paris"
  mark_done packages
}

# ============================================================================
# 2. Asterisk
# ============================================================================
asterisk_installed_version() {
  command -v asterisk >/dev/null 2>&1 || return 1
  asterisk -V 2>/dev/null | sed -n 's/^Asterisk \([0-9][0-9.]*\).*/\1/p'
}

install_asterisk() {
  local current
  current="$(asterisk_installed_version || true)"

  if [[ -n "$current" && "$current" == "$ASTERISK_BRANCH".* && $FORCE -eq 0 ]]; then
    ok "Asterisk $current déjà installé (--force pour recompiler)"
    mark_done asterisk
    return
  fi
  if done_step asterisk; then ok "Asterisk déjà compilé"; return; fi

  step "Compilation d'Asterisk $ASTERISK_BRANCH — comptez 15 à 25 minutes"
  [[ -n "$current" ]] && warn "version en place : $current, elle va être remplacée"

  local tarball="asterisk-${ASTERISK_BRANCH}-current.tar.gz"
  run bash -c "cd '$ASTERISK_SRC' && wget -q -N 'https://downloads.asterisk.org/pub/telephony/asterisk/${tarball}'"
  run bash -c "cd '$ASTERISK_SRC' && tar xf '${tarball}'"

  local src
  if [[ $DRY_RUN -eq 1 ]]; then
    src="$ASTERISK_SRC/asterisk-${ASTERISK_BRANCH}.x.y"
  else
    src="$(find "$ASTERISK_SRC" -maxdepth 1 -type d -name "asterisk-${ASTERISK_BRANCH}.*" | sort -V | tail -1)"
    [[ -n "$src" ]] || die "sources Asterisk introuvables après extraction."
  fi
  ok "sources : $src"

  run bash -c "cd '$src' && contrib/scripts/install_prereq install"
  run bash -c "cd '$src' && ./configure --with-jansson-bundled"
  run bash -c "cd '$src' && make menuselect.makeopts"

  # Les sons français sont l'oubli classique : sans eux, toutes les annonces
  # sortent en anglais et il faut tout recompiler pour s'en apercevoir.
  step "Sélection des modules et des sons français"
  local opts=(
    chan_pjsip res_pjsip app_voicemail app_directory cdr_csv chan_console
    CORE-SOUNDS-FR-ALAW CORE-SOUNDS-FR-WAV EXTRA-SOUNDS-FR-ALAW
  )
  for opt in "${opts[@]}"; do
    if [[ $DRY_RUN -eq 1 ]]; then
      printf '    %s[dry-run]%s menuselect --enable %s\n' "$Y" "$N" "$opt"
    elif (cd "$src" && menuselect/menuselect --enable "$opt" menuselect.makeopts) >>"$LOG" 2>&1; then
      ok "$opt"
    else
      # Un nom d'option qui disparaît d'une version à l'autre ne doit pas
      # faire échouer une compilation de vingt minutes.
      warn "$opt indisponible dans cette version — ignoré"
    fi
  done

  step "make — c'est la partie longue"
  run_verbose bash -c "cd '$src' && make -j\"\$(nproc)\" >/dev/null && echo 'compilation terminée'"
  run bash -c "cd '$src' && make install"
  run bash -c "cd '$src' && make install-logrotate"
  run ldconfig

  # `make samples` n'est volontairement PAS lancé : il déposerait une
  # configuration d'exemple avec un contexte [default] permissif, que la
  # configuration de ce dépôt remplace de toute façon.
  ok "Asterisk installé sans configuration d'exemple"
  mark_done asterisk
}

# ============================================================================
# 3. Utilisateur, répertoires, unité systemd
# ============================================================================
setup_asterisk_system() {
  if done_step asterisk-system; then ok "utilisateur et service Asterisk déjà en place"; return; fi
  step "Utilisateur système et service Asterisk"

  if id asterisk >/dev/null 2>&1; then
    ok "utilisateur asterisk déjà présent"
  else
    run useradd -r -d /var/lib/asterisk -s /usr/sbin/nologin asterisk
    ok "utilisateur asterisk créé"
  fi

  for dir in /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /etc/asterisk /var/run/asterisk; do
    run install -d -o asterisk -g asterisk "$dir"
  done
  run install -d -o asterisk -g asterisk -m 0755 /var/lib/asterisk/sounds/custom

  # runuser/rungroup : sans ça, Asterisk tourne en root et son socket de
  # contrôle devient inaccessible à l'interface de gestion.
  if [[ -f /etc/asterisk/asterisk.conf ]]; then
    run bash -c "sed -i 's|^;*\s*runuser\s*=.*|runuser = asterisk|; s|^;*\s*rungroup\s*=.*|rungroup = asterisk|' /etc/asterisk/asterisk.conf"
    if ! grep -q '^runuser' /etc/asterisk/asterisk.conf 2>/dev/null && [[ $DRY_RUN -eq 0 ]]; then
      printf '\n[options]\nrunuser = asterisk\nrungroup = asterisk\n' >> /etc/asterisk/asterisk.conf
    fi
  else
    run bash -c "printf '[directories](!)\nastetcdir => /etc/asterisk\n\n[options]\nrunuser = asterisk\nrungroup = asterisk\n' > /etc/asterisk/asterisk.conf"
  fi
  run chown asterisk:asterisk /etc/asterisk/asterisk.conf
  ok "Asterisk tournera sous l'utilisateur asterisk"

  run install -m 0644 "$REPO/systemd/asterisk.service" /etc/systemd/system/asterisk.service
  run systemctl daemon-reload
  run systemctl enable asterisk
  ok "unité systemd installée"
  mark_done asterisk-system
}

# ============================================================================
# 4. chan-quectel (module GSM)
# ============================================================================
install_quectel() {
  [[ $WITH_GSM -eq 1 ]] || { ok "module GSM non demandé (--with-gsm pour l'ajouter)"; return; }
  if done_step quectel; then ok "chan-quectel déjà compilé"; return; fi
  step "chan-quectel — trunk GSM de secours"

  local src="$ASTERISK_SRC/asterisk-chan-quectel"
  if [[ -d "$src/.git" ]]; then
    run git -C "$src" pull --ff-only
  else
    run git clone --depth 1 https://github.com/RoEdAl/asterisk-chan-quectel.git "$src"
  fi

  run cmake -S "$src" -B "$src/build" -DCMAKE_BUILD_TYPE=Release
  run cmake --build "$src/build" -j"$(nproc)"
  run cmake --install "$src/build"

  if [[ -f /etc/asterisk/quectel.conf ]]; then
    ok "quectel.conf existant conservé"
  else
    run install -o asterisk -g asterisk -m 0640 \
      "$REPO/asterisk/quectel.conf.sample" /etc/asterisk/quectel.conf
    warn "quectel.conf déposé depuis l'exemple : renseignez les chemins /dev/serial/by-id/…"
    warn "  ls -l /dev/serial/by-id/"
  fi
  mark_done quectel
}

# ============================================================================
# 5. Piper (synthèse vocale)
# ============================================================================
install_piper() {
  [[ $WITH_TTS -eq 1 ]] || { ok "Piper non demandé (--without-tts)"; return; }
  if done_step piper; then ok "Piper déjà installé"; return; fi
  step "Piper — génération des annonces vocales"

  run python3 -m venv /opt/piper
  run /opt/piper/bin/pip install --quiet --upgrade pip
  run /opt/piper/bin/pip install --quiet piper-tts
  run ln -sf /opt/piper/bin/piper /usr/local/bin/piper

  run install -d /opt/piper-voices
  local base="https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium"
  local voice_ok=1
  # Une voix, c'est DEUX fichiers : le modèle et sa description. Piper échoue
  # silencieusement s'il n'en trouve qu'un.
  for f in fr_FR-siwis-medium.onnx fr_FR-siwis-medium.onnx.json; do
    if [[ $DRY_RUN -eq 1 ]]; then
      printf '    %s[dry-run]%s wget %s/%s\n' "$Y" "$N" "$base" "$f"
    elif wget -q -O "/opt/piper-voices/$f" "$base/$f" >>"$LOG" 2>&1; then
      ok "voix : $f"
    else
      rm -f "/opt/piper-voices/$f"
      voice_ok=0
    fi
  done

  if [[ $voice_ok -eq 0 ]]; then
    warn "téléchargement de la voix impossible."
    warn "  Parcourez https://huggingface.co/rhasspy/piper-voices/tree/main/fr/fr_FR"
    warn "  puis déposez le .onnx et le .onnx.json dans /opt/piper-voices/."
    warn "  L'interface fonctionnera, mais l'annonce du menu ne sera pas régénérée."
  fi

  run install -m 0755 "$REPO/scripts/generate-prompts.sh" /opt/piper-voices/generate.sh

  if [[ $voice_ok -eq 1 && $DRY_RUN -eq 0 ]]; then
    step "Génération des annonces de base"
    for pair in \
      "Bonjour. Tapez 1 pour joindre la famille. Tapez 2 pour joindre une personne.|menu-niveau1" \
      "Composez votre numéro.|compose-1-pour-appeler"
    do
      local text="${pair%|*}" name="${pair#*|}"
      if /opt/piper-voices/generate.sh "$text" "$name" >>"$LOG" 2>&1; then
        ok "annonce $name"
      else
        warn "génération de $name échouée — voir $LOG"
      fi
    done
  fi
  mark_done piper
}

# ============================================================================
# 6. Interface de gestion et configuration Asterisk
# ============================================================================
install_app() {
  step "Interface de gestion et configuration Asterisk"

  local args=()
  [[ $WITH_CONF -eq 1 ]] && args+=(--with-asterisk-conf)

  if [[ $WITH_CONF -eq 1 && -f /etc/asterisk/pjsip.conf ]]; then
    warn "une configuration Asterisk existe déjà dans /etc/asterisk."
    warn "elle sera sauvegardée dans /etc/asterisk/backup-<date>/ avant d'être remplacée."
    confirm "Remplacer la configuration en place ?" || {
      warn "configuration conservée — l'interface est installée sans elle"
      args=()
    }
  fi

  run_verbose "$REPO/scripts/install.sh" "${args[@]}"

  # Adapter pjsip.conf au réseau réel : sans ça, Asterisk écoute sur toutes les
  # interfaces, y compris celle d'administration.
  if [[ $WITH_CONF -eq 1 ]]; then
    if [[ -n "$VOIP_NET" ]]; then
      run bash -c "sed -i 's|^local_net=10\\.0\\.90\\.0/24|local_net=${VOIP_NET}|' /etc/asterisk/pjsip.conf"
      ok "local_net réglé sur $VOIP_NET"
    fi
    if [[ -n "$VOIP_IP" ]]; then
      run bash -c "sed -i 's|^bind=0\\.0\\.0\\.0:5060|bind=${VOIP_IP}:5060|' /etc/asterisk/pjsip.conf"
      ok "écoute SIP restreinte à ${VOIP_IP}:5060"
    fi
  fi

  # Provisionnement des ATA : le service n'est joignable depuis le VLAN voix
  # que si on lui donne une adresse d'écoute. Sans --voip-ip, il reste sur la
  # boucle locale et aucun appareil ne peut l'atteindre.
  if [[ -n "$VOIP_IP" ]]; then
    run install -d -m 0755 /etc/telephonie
    run bash -c "printf 'TELEPHONIE_PROV_HOST=%s\nTELEPHONIE_PROV_PORT=8081\n' '${VOIP_IP}' > /etc/telephonie/prov.env"
    run systemctl restart telephonie-prov
    ok "provisionnement des ATA joignable sur ${VOIP_IP}:8081"
  else
    warn "provisionnement des ATA laissé sur 127.0.0.1 : sans --voip-ip, aucun"
    warn "  appareil ne peut le joindre. Voir docs/10-provisionnement.md."
  fi
}

# ============================================================================
# 7. Durcissement
# ============================================================================
harden() {
  [[ $WITH_HARDENING -eq 1 ]] || { ok "durcissement non demandé (--with-hardening)"; return; }
  if done_step hardening; then ok "durcissement déjà appliqué"; return; fi
  step "Durcissement : pare-feu, fail2ban, SSH, mises à jour"

  run env DEBIAN_FRONTEND=noninteractive apt-get install -y ufw fail2ban unattended-upgrades

  # --- Pare-feu. L'ordre compte : SSH est autorisé AVANT l'activation, sinon
  # la session en cours est coupée net.
  run ufw --force reset
  run ufw default deny incoming
  run ufw default allow outgoing
  local admin_rule=(ufw allow in from "$ADMIN_NET" to any port 22 proto tcp)
  [[ -n "$ADMIN_IFACE" ]] && admin_rule=(ufw allow in on "$ADMIN_IFACE" from "$ADMIN_NET" to any port 22 proto tcp)
  run "${admin_rule[@]}"
  ok "SSH autorisé depuis $ADMIN_NET"

  local sip_rule=(ufw allow in from "$VOIP_NET" to any port 5060 proto udp)
  local rtp_rule=(ufw allow in from "$VOIP_NET" to any port 10000:10200 proto udp)
  if [[ -n "$VOIP_IFACE" ]]; then
    sip_rule=(ufw allow in on "$VOIP_IFACE" from "$VOIP_NET" to any port 5060 proto udp)
    rtp_rule=(ufw allow in on "$VOIP_IFACE" from "$VOIP_NET" to any port 10000:10200 proto udp)
  fi
  run "${sip_rule[@]}"
  run "${rtp_rule[@]}"

  # Provisionnement : ouvert au VLAN voix seulement si le service y écoute.
  if [[ -n "$VOIP_IP" ]]; then
    local prov_rule=(ufw allow in from "$VOIP_NET" to any port 8081 proto tcp)
    [[ -n "$VOIP_IFACE" ]] && prov_rule=(ufw allow in on "$VOIP_IFACE" from "$VOIP_NET" to any port 8081 proto tcp)
    run "${prov_rule[@]}"
    ok "provisionnement ouvert au VLAN voix (8081/tcp)"
  fi

  run ufw --force enable
  ok "pare-feu actif : SIP et RTP depuis $VOIP_NET uniquement"

  # --- fail2ban. La prison doit lire le fichier qui contient réellement les
  # NOTICE, sans quoi elle ne verrait jamais un échec d'authentification.
  run bash -c "cat > /etc/fail2ban/jail.d/asterisk.local <<'JAIL'
[asterisk]
enabled  = true
port     = 5060,5061
filter   = asterisk
logpath  = /var/log/asterisk/messages
maxretry = 5
findtime = 600
bantime  = 3600
JAIL"
  run systemctl restart fail2ban
  ok "fail2ban actif sur /var/log/asterisk/messages"

  # --- SSH. Refuser le mot de passe sans qu'aucune clé ne soit déposée
  # reviendrait à se verrouiller dehors : on vérifie avant d'agir.
  local keys_found=0
  while IFS=: read -r user _ uid _ _ home _; do
    [[ ${uid:-0} -ge 1000 || "$user" == "root" ]] || continue
    [[ -s "$home/.ssh/authorized_keys" ]] && keys_found=1
  done < /etc/passwd

  if [[ $keys_found -eq 1 ]]; then
    run bash -c "sed -i 's|^#\?PasswordAuthentication .*|PasswordAuthentication no|; s|^#\?PermitRootLogin .*|PermitRootLogin no|' /etc/ssh/sshd_config"
    run systemctl restart ssh
    ok "SSH par clé uniquement"
  else
    warn "aucune clé SSH trouvée dans un authorized_keys : authentification par"
    warn "  mot de passe CONSERVÉE, pour ne pas vous verrouiller dehors."
    warn "  Déposez votre clé, puis relancez avec --with-hardening --force."
  fi

  run systemctl enable --now unattended-upgrades
  ok "mises à jour de sécurité automatiques"
  mark_done hardening
}

# ============================================================================
# 8. Démarrage et données de départ
# ============================================================================
start_services() {
  step "Démarrage des services"

  run systemctl restart asterisk
  if [[ $DRY_RUN -eq 0 ]]; then
    for _ in $(seq 1 20); do
      asterisk -rx "core show version" >/dev/null 2>&1 && break
      sleep 1
    done
    if asterisk -rx "core show version" >/dev/null 2>&1; then
      ok "Asterisk répond : $(asterisk -rx 'core show version' | head -1)"
    else
      warn "Asterisk ne répond pas encore. Diagnostic :"
      warn "  journalctl -u asterisk -n 50 --no-pager"
      warn "  asterisk -cvvvvv"
    fi
  fi

  if [[ $DRY_RUN -eq 0 ]] && sudo -u asterisk sqlite3 /var/lib/telephonie/telephonie.db \
       "SELECT 1 FROM devices LIMIT 1" 2>/dev/null | grep -q 1; then
    ok "base déjà peuplée, données de départ non réinstallées"
  else
    if confirm "Créer l'installation de référence (5 postes, pont FXO, 2 routes) ?"; then
      run sudo -u asterisk /opt/telephonie/venv/bin/python /opt/telephonie/scripts/seed.py
      ok "données de départ créées"
    else
      ok "base laissée vide — tout se crée depuis l'interface"
    fi
  fi

  run systemctl restart telephonie-ui
  ok "interface de gestion démarrée"

  # Pré-remplir les réglages de provisionnement, maintenant que la base existe
  # (elle est créée au démarrage du service). On n'écrase jamais une valeur
  # déjà saisie dans l'interface.
  if [[ -n "$VOIP_IP" && $DRY_RUN -eq 0 ]]; then
    sudo -u asterisk sqlite3 /var/lib/telephonie/telephonie.db <<SQL >>"$LOG" 2>&1 || true
UPDATE settings SET value = '${VOIP_IP}'
  WHERE key = 'prov_sip_server' AND (value IS NULL OR value = '');
UPDATE settings SET value = 'http://${VOIP_IP}:8081'
  WHERE key = 'prov_server_url' AND (value IS NULL OR value = '');
SQL
    ok "réglages de provisionnement pré-remplis avec ${VOIP_IP}"
  fi
}

# ============================================================================
# Récapitulatif
# ============================================================================
summary() {
  local ip
  ip=$(hostname -I 2>/dev/null | awk '{print $1}')

  cat <<EOF

${B}Installation terminée.${N}

  Interface   http://127.0.0.1:8080
  Tunnel      ssh -L 8080:127.0.0.1:8080 $(logname 2>/dev/null || echo utilisateur)@${ip:-<ip-de-la-vm>}
  Journal     $LOG
  Base        /var/lib/telephonie/telephonie.db

${B}Ce qui reste à faire à la main :${N}
EOF

  local n=0
  if [[ -z "$VOIP_IP" ]]; then
    n=$((n + 1))
    cat <<EOF

  $n. /etc/asterisk/pjsip.conf : remplacer bind=0.0.0.0:5060 par l'IP de votre
     vNIC voix, pour qu'Asterisk n'écoute pas sur le VLAN d'administration.
     Puis : systemctl reload asterisk
EOF
  fi

  n=$((n + 1)); cat <<EOF

  $n. Ouvrir l'interface et créer le compte d'administration
     (la page se ferme définitivement ensuite).
EOF

  n=$((n + 1)); cat <<EOF

  $n. Écran « Postes » : relever le mot de passe SIP de chaque poste et le
     reporter dans les ATA — voir docs/08-materiel.md.
EOF

  n=$((n + 1)); cat <<EOF

  $n. Écran « Appliquer » : vérifier le diff, valider.

  Test rapide depuis un poste : *65 annonce son numéro, *43 teste l'audio.
EOF

  [[ $WITH_GSM -eq 1 ]] && cat <<EOF

  Module GSM : renseignez les chemins dans /etc/asterisk/quectel.conf
    ls -l /dev/serial/by-id/
    asterisk -rx "quectel show devices"
EOF

  [[ $WITH_HARDENING -eq 0 ]] && cat <<EOF

  Durcissement non appliqué. Pour l'ajouter :
    sudo ./scripts/bootstrap.sh --with-hardening \\
         --admin-net <votre-reseau-admin> --voip-net <votre-reseau-voix>
EOF
  return 0
}

# ============================================================================
main() {
  preflight
  install_packages
  install_asterisk
  setup_asterisk_system
  install_quectel
  install_piper
  install_app
  harden
  start_services
  summary
}

main
