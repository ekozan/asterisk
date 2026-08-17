# Installation, de la VM vierge à l'installation complète

## Le raccourci : `bootstrap.sh`

Tout ce que décrit ce document est automatisé par un script. Sur une VM Ubuntu Server
24.04 LTS neuve :

```bash
sudo git clone <ce-dépôt> /root/telephonie
cd /root/telephonie

# 1. Regarder ce qui serait fait, sans rien modifier
sudo ./scripts/bootstrap.sh --dry-run --with-gsm --with-hardening \
     --voip-net 10.0.90.0/24 --voip-iface ens192 \
     --admin-net 10.0.5.0/24 --admin-iface ens160

# 2. Lancer pour de vrai (mêmes options, sans --dry-run)
sudo ./scripts/bootstrap.sh --with-gsm --with-hardening \
     --voip-net 10.0.90.0/24 --voip-iface ens192 \
     --admin-net 10.0.5.0/24 --admin-iface ens160
```

Trois propriétés qui comptent sur une procédure de vingt minutes :

- **Reprenable.** Chaque étape réussie est notée dans
  `/var/lib/telephonie/.bootstrap/`. Une coupure réseau en pleine compilation ne coûte
  que de relancer la même commande : le script repart où il s'était arrêté. `--force`
  refait tout.
- **`--dry-run` est réel, pas approximatif.** Toute commande qui modifie la machine passe
  par une fonction unique ; en mode simulation, aucune ne s'exécute.
- **Le durcissement ne vous verrouille pas dehors.** SSH est autorisé dans le pare-feu
  *avant* son activation, et l'authentification par mot de passe n'est désactivée que si
  une clé publique existe déjà quelque part dans un `authorized_keys`.

Options principales (`--help` pour la liste complète) :

| Option | Effet |
|---|---|
| `--with-gsm` | compile chan-quectel pour le trunk GSM de secours |
| `--with-hardening` | ufw, fail2ban, SSH par clé, mises à jour automatiques |
| `--voip-iface` / `--voip-ip` | restreint l'écoute SIP au VLAN voix |
| `--without-config` | n'écrit pas `/etc/asterisk` — pour reprendre une install existante |
| `--without-tts` | n'installe pas Piper |
| `--dry-run`, `--yes`, `--force` | simulation, non interactif, réexécution |

Le script laisse volontairement quatre choses à faire à la main, et les rappelle à la fin :
créer le compte d'administration, reporter les mots de passe SIP dans les ATA, et valider
l'écran « Appliquer ».

Journal complet dans `/var/log/telephonie-install.log`.

> **Le reste de ce document décrit la même procédure, étape par étape.** Lisez-le si vous
> préférez comprendre et exécuter vous-même, si le script échoue quelque part, ou si votre
> installation s'écarte du cas nominal.

> Vous avez déjà un Asterisk 22 qui tourne ? Sautez aux étapes [6](#6-configuration-asterisk)
> et suivantes. La section [Reprendre une installation existante](#reprendre-une-installation-existante)
> en fin de document explique quoi vérifier avant de remplacer la configuration en place.

---

# Procédure détaillée

Comptez une heure, dont une vingtaine de minutes de compilation.

---

## 1. Hyperviseur — passthrough USB (ESXi)

Le module GSM se présente à la VM comme un périphérique USB. Sous ESXi, ce passthrough est
plus contraignant qu'ailleurs, et mieux vaut le savoir avant de câbler.

Activer l'arbitre USB sur l'hôte, souvent désactivé par défaut :

```bash
# en SSH sur l'hôte ESXi
/etc/init.d/usbarbitrator start
chkconfig usbarbitrator on      # pour survivre au redémarrage de l'hôte
```

Puis, dans vSphere Client : *Edit Settings* → *Add New Device* → *USB Device* → sélectionner
le SIM7600G-H.

**Deux limites structurelles à connaître :**

- Le passthrough ESXi est lié au **port physique de l'hôte**, pas à un couple
  `vendor:product` comme sous libvirt ou Proxmox. Rebrancher le module sur un autre port
  oblige à retirer puis rajouter le périphérique dans la configuration de la VM.
- **vMotion casse le passthrough.** Sur un hôte unique, aucun souci. En cluster, il faut
  soit exclure cette VM du DRS, soit accepter qu'elle reste clouée sur un hôte.

Vérification côté VM, une fois démarrée :

```bash
lsusb                      # le module doit apparaître
ls -l /dev/serial/by-id/   # chemins stables des ports série
```

Utilisez toujours les chemins `/dev/serial/by-id/…` : les `/dev/ttyUSB*` changent d'index
au moindre rebranchement, et un `quectel.conf` qui pointe le mauvais port produit une
panne silencieuse — le trunk de secours ne répond plus, ce qui ne se remarque que le jour
où on en a besoin.

---

## 2. Système de base

```bash
sudo apt update && sudo apt full-upgrade -y
# Le dépôt est cloné dès maintenant : plusieurs étapes suivantes y puisent des
# fichiers (configuration Asterisk, script de synthèse vocale, unités systemd).
sudo git clone <ce-dépôt> /root/telephonie
sudo apt install -y build-essential git wget curl pkg-config \
  libjansson-dev libxml2-dev libsqlite3-dev uuid-dev libedit-dev libssl-dev \
  libsrtp2-dev sox alsa-utils usbutils cmake python3-venv python3-pip \
  sqlite3 rsync
sudo timedatectl set-timezone Europe/Paris
```

L'heure juste n'est pas cosmétique : elle date les messages vocaux, les enregistrements
d'appels et les journaux que fail2ban corrèle.

### Réseau — deux interfaces

La VM porte deux vNIC (voir [01 — Architecture](01-architecture.md)) : le VLAN 90 pour la
voix, le VLAN 5 pour l'administration. Exemple `netplan`, à adapter à vos plages :

```yaml
# /etc/netplan/01-telephonie.yaml
network:
  version: 2
  ethernets:
    ens160:                      # VLAN 5 — administration
      addresses: [10.0.5.20/24]
      routes:
        - to: default
          via: 10.0.5.1
      nameservers:
        addresses: [10.0.5.1]
    ens192:                      # VLAN 90 — voix
      addresses: [10.0.90.20/24]
```

```bash
sudo chmod 600 /etc/netplan/01-telephonie.yaml
sudo netplan apply
```

Une seule route par défaut, sur le VLAN d'administration : le VLAN voix ne doit pas être
un chemin de sortie vers Internet.

---

## 3. Compilation d'Asterisk 22

Asterisk 22 est la branche **LTS** : quatre ans de support complet puis un an de
correctifs de sécurité. Pour une installation domestique qui tourne sans surveillance,
c'est le bon compromis — la branche 23, plus récente, demande de suivre un cycle de mise à
jour bien plus fréquent pour des apports (WebRTC, DTLS) qui ne servent pas ici.

```bash
cd /usr/src
sudo wget https://downloads.asterisk.org/pub/telephony/asterisk/asterisk-22-current.tar.gz
sudo tar xf asterisk-22-current.tar.gz
cd asterisk-22*/
sudo contrib/scripts/install_prereq install
sudo ./configure --with-jansson-bundled
sudo make menuselect.makeopts
```

### Sélection des modules et des sons français

C'est l'étape la plus facile à bâcler, et celle qui coûte le plus cher ensuite : sans les
paquets de sons français, toutes les annonces sortent en anglais.

```bash
sudo menuselect/menuselect \
  --enable chan_pjsip --enable app_voicemail --enable app_directory \
  --enable res_pjsip --enable cdr_csv --enable chan_console \
  --enable CORE-SOUNDS-FR-ALAW --enable CORE-SOUNDS-FR-WAV \
  --enable EXTRA-SOUNDS-FR-ALAW \
  menuselect.makeopts
```

`CORE-SOUNDS-FR-ALAW` est le format joué tel quel sur les postes analogiques, sans
transcodage. `EXTRA-SOUNDS-FR-ALAW` apporte les messages que le menu vocal utilise
(`ss-noservice`, `all-circuits-busy-now`…).

```bash
sudo make -j"$(nproc)"
sudo make install
sudo make samples          # dépose des .conf d'exemple, que nous remplacerons
sudo make install-logrotate
sudo ldconfig
```

> `make samples` écrit une configuration d'exemple complète dans `/etc/asterisk`. Elle est
> utile comme référence, mais **ne doit pas rester active** : elle ouvre notamment un
> contexte `[default]` permissif. L'étape 6 la remplace par la configuration de ce dépôt.

### Utilisateur dédié

Asterisk ne doit jamais tourner en `root` : un service qui écoute sur le réseau et exécute
un dialplan est exactement le genre de chose qu'on veut voir confiné.

```bash
sudo useradd -r -d /var/lib/asterisk -s /usr/sbin/nologin asterisk 2>/dev/null || true
sudo chown -R asterisk:asterisk \
  /var/lib/asterisk /var/log/asterisk /var/spool/asterisk /etc/asterisk /var/run/asterisk
```

Dans `/etc/asterisk/asterisk.conf`, section `[options]` :

```ini
runuser = asterisk
rungroup = asterisk
```

Installer l'unité systemd fournie par ce dépôt, plus explicite que le script d'init
historique :

```bash
sudo install -m 0644 /root/telephonie/systemd/asterisk.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable asterisk
```

---

## 4. Module GSM — `chan-quectel`

Ce canal n'est pas packagé : il se compile contre les en-têtes de l'Asterisk installé,
donc **après** l'étape précédente et **de nouveau** après chaque mise à jour majeure
d'Asterisk.

```bash
cd /usr/src
sudo git clone https://github.com/RoEdAl/asterisk-chan-quectel.git
cd asterisk-chan-quectel
sudo cmake -B build -DCMAKE_BUILD_TYPE=Release
sudo cmake --build build -j"$(nproc)"
sudo cmake --install build
```

Configuration :

```bash
sudo install -o asterisk -g asterisk -m 0640 \
  /root/telephonie/asterisk/quectel.conf.sample /etc/asterisk/quectel.conf
sudoedit /etc/asterisk/quectel.conf     # renseigner les chemins /dev/serial/by-id/…
```

Vérification, une fois Asterisk démarré :

```bash
sudo asterisk -rx "quectel show devices"
```

Le module doit apparaître en état `Free`. S'il reste en `Not initialized`, voir
[07 — Dépannage](07-depannage.md#le-trunk-gsm-ne-sinitialise-pas).

---

## 5. Messages vocaux — Piper

Piper synthétise les annonces du menu vocal. Il tourne directement sur la VM : c'est un
paquet Python autonome, et les fichiers produits doivent de toute façon atterrir ici.

```bash
sudo python3 -m venv /opt/piper
sudo /opt/piper/bin/pip install piper-tts
sudo ln -sf /opt/piper/bin/piper /usr/local/bin/piper

sudo mkdir -p /opt/piper-voices && cd /opt/piper-voices
# Une voix = deux fichiers, le modèle et sa description. Les deux sont obligatoires.
BASE=https://huggingface.co/rhasspy/piper-voices/resolve/main/fr/fr_FR/siwis/medium
sudo wget "$BASE/fr_FR-siwis-medium.onnx"
sudo wget "$BASE/fr_FR-siwis-medium.onnx.json"

sudo install -m 0755 /root/telephonie/scripts/generate-prompts.sh /opt/piper-voices/generate.sh
```

> La liste des voix disponibles évolue ; si le lien ci-dessus échoue, parcourez
> <https://huggingface.co/rhasspy/piper-voices/tree/main/fr/fr_FR> et prenez le dossier
> d'une autre voix française. Le chemin du modèle se surcharge ensuite par la variable
> `PIPER_VOICE`.

Générer les annonces de base :

```bash
sudo /opt/piper-voices/generate.sh \
  "Bonjour. Tapez 1 pour joindre la famille. Tapez 2 pour joindre une personne." \
  menu-niveau1

sudo /opt/piper-voices/generate.sh \
  "Composez votre numéro." \
  compose-1-pour-appeler
```

L'annonce `menu-personne` n'est pas à écrire à la main : l'interface la régénère toute
seule à chaque changement dans la liste des personnes.

---

## 6. Configuration Asterisk

```bash
cd /root/telephonie
sudo ./scripts/install.sh --with-asterisk-conf
```

Le script sauvegarde la configuration existante dans
`/etc/asterisk/backup-AAAAMMJJ-HHMMSS/` avant de déposer celle du dépôt, installe
l'interface dans `/opt/telephonie`, crée `/etc/asterisk/generated/` et démarre le service.

Les fichiers déposés sont décrits un par un dans
[03 — Configuration Asterisk](03-configuration-asterisk.md). Une seule valeur est à
adapter tout de suite, dans `/etc/asterisk/pjsip.conf` :

```ini
[transport-udp]
bind=10.0.90.20:5060        ; l'IP du vNIC VLAN 90, plutôt que 0.0.0.0
local_net=10.0.90.0/24
```

Puis :

```bash
sudo systemctl start asterisk
sudo asterisk -rx "core show version"
```

---

## 7. Données de départ et première application

```bash
sudo -u asterisk /opt/telephonie/venv/bin/python /opt/telephonie/scripts/seed.py
```

Cette commande crée les cinq postes, le pont FXO, le groupe « toute la maison » et les
deux routes sortantes décrits dans l'architecture — avec un mot de passe SIP tiré au
hasard pour chacun. Elle ne fait rien si la base contient déjà des postes.

Accès à l'interface, par tunnel SSH depuis votre poste :

```bash
ssh -L 8080:127.0.0.1:8080 vous@10.0.5.20
```

Puis <http://127.0.0.1:8080> : le premier accès demande de créer le compte
d'administration, et cette page se ferme définitivement ensuite.

Dans l'interface : écran **Postes**, relever le mot de passe SIP de chaque poste pour le
reporter dans les ATA ([08 — Matériel](08-materiel.md)), puis écran **Appliquer**,
vérifier le diff et valider.

---

## 8. Durcissement

### Pare-feu local

En complément d'OPNsense — un pare-feu sur la machine elle-même reste utile le jour où une
règle de VLAN est modifiée par erreur.

```bash
sudo apt install -y ufw
sudo ufw default deny incoming
sudo ufw default allow outgoing
sudo ufw allow in on ens160 from 10.0.5.0/24 to any port 22 proto tcp     # SSH
sudo ufw allow in on ens192 from 10.0.90.0/24 to any port 5060 proto udp  # SIP
sudo ufw allow in on ens192 from 10.0.90.0/24 to any port 10000:10200 proto udp  # RTP
sudo ufw enable
```

Rien à ouvrir pour l'interface de gestion ni pour l'AMI : la première n'écoute que sur
`127.0.0.1` et s'atteint par tunnel SSH, le second est désactivé.

### fail2ban

```bash
sudo apt install -y fail2ban
sudo tee /etc/fail2ban/jail.d/asterisk.local >/dev/null <<'EOF'
[asterisk]
enabled  = true
port     = 5060,5061
filter   = asterisk
logpath  = /var/log/asterisk/messages
maxretry = 5
findtime = 600
bantime  = 3600
EOF
sudo systemctl restart fail2ban
sudo fail2ban-client status asterisk
```

Le `logger.conf` déposé par ce dépôt envoie bien les `NOTICE` — le niveau auquel Asterisk
journalise les échecs d'authentification — dans `/var/log/asterisk/messages`. Sans ça, la
prison ne verrait jamais rien passer et donnerait une fausse impression de protection.

### SSH et mises à jour

```bash
sudo sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sudo sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
sudo systemctl restart ssh

sudo apt install -y unattended-upgrades
sudo dpkg-reconfigure -plow unattended-upgrades
```

Les mises à jour automatiques restent limitées aux correctifs de sécurité. Asterisk étant
compilé à la main, `apt` n'y touchera pas : sa mise à jour est un geste manuel décrit dans
[05 — Exploitation](05-exploitation.md#mettre-à-jour-asterisk).

---

## 9. Vérifications finales

```bash
sudo asterisk -rx "pjsip show endpoints"     # les postes doivent être « Not in use »
sudo asterisk -rx "dialplan show from-internal" | head -30
sudo asterisk -rx "voicemail show users"
sudo asterisk -rx "quectel show devices"
systemctl status asterisk telephonie-ui --no-pager
```

Puis, depuis un poste : composer `*65` (il annonce son numéro), `*43` (test d'écho), un
autre poste, et enfin un numéro externe.

---

## Reprendre une installation existante

Si un Asterisk 22 tourne déjà avec une configuration écrite à la main, la bascule mérite
d'être préparée plutôt que subie.

1. **Relever ce qui existe**, en particulier ce que la base ne saura pas deviner :

   ```bash
   sudo asterisk -rx "pjsip show endpoints"   > /root/avant-endpoints.txt
   sudo asterisk -rx "dialplan show"          > /root/avant-dialplan.txt
   sudo cp -a /etc/asterisk /root/etc-asterisk-avant
   ```

2. **Noter les mots de passe SIP existants.** Le script `seed.py` en génère de nouveaux,
   ce qui oblige à reconfigurer chaque ATA. Pour éviter ce chantier, créez plutôt les
   postes depuis l'interface puis remplacez le mot de passe généré par l'ancien
   directement en base :

   ```bash
   sudo -u asterisk sqlite3 /var/lib/telephonie/telephonie.db \
     "UPDATE devices SET secret = 'ancien-mot-de-passe' WHERE slug = 'salon';"
   ```

3. **Déployer sans écraser tout de suite** : lancez `bootstrap.sh --without-config` (ou
   `install.sh` *sans* `--with-asterisk-conf`), saisissez vos postes dans l'interface,
   puis comparez le dialplan généré — visible dans l'écran **Appliquer** — avec l'ancien
   avant de basculer. Le jour où vous basculez, `bootstrap.sh` sans `--without-config`
   sauvegarde l'existant dans `/etc/asterisk/backup-<date>/` et demande confirmation.

4. **Prendre un instantané de la VM** avant la bascule. C'est le filet le plus rapide à
   dérouler, et le seul qui rattrape une erreur côté système et pas seulement côté
   configuration.
