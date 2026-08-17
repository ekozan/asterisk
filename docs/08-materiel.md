# Matériel — réglages des appareils

Les libellés exacts varient d'une version de firmware à l'autre. Les noms donnés ici sont
ceux des interfaces courantes ; cherchez l'équivalent le plus proche si le vôtre diffère.

## Réglages communs à tous les appareils SIP

| Paramètre | Valeur |
|---|---|
| SIP Server / Registrar | l'IP du vNIC VLAN 90 de la VM, ex. `10.0.90.20` |
| Port SIP | `5060` |
| SIP User ID / Authenticate ID | l'**identifiant SIP** du poste dans l'interface (ex. `salon`) |
| Password | le **mot de passe SIP** affiché dans l'écran Postes |
| Transport | UDP |
| Codecs préférés | G.711 A-law (`PCMA`) en premier, µ-law en second |
| DTMF | RFC2833 |
| NAT traversal | désactivé — postes et serveur sont sur le même VLAN |
| Register Expiration | 120 secondes |

Trois pièges récurrents :

- **L'identifiant est sensible à la casse** et ne doit contenir aucun espace.
- **Le domaine SIP** doit être l'IP du serveur, pas un nom qui ne résout pas sur le VLAN 90.
- **G.722 ou Opus sur un port FXS** n'apporte rien : le combiné analogique reste en bande
  étroite, et le transcodage consomme du CPU pour rien.

---

## Grandstream HT801 — poste du garage

Interface web sur le port 80, mot de passe par défaut `admin` (à changer immédiatement).

- *FXS Port* → **SIP Server** : `10.0.90.20`
- *FXS Port* → **SIP User ID** / **Authenticate ID** : `garage`
- *FXS Port* → **Authenticate Password** : le mot de passe généré
- *FXS Port* → **Preferred Vocoder** : `PCMA`, puis `PCMU`
- *FXS Port* → **Caller ID Scheme** : `Bellcore/Telcordia` ou `ETSI-FSK`, selon ce que
  le combiné affiche réellement — à tester
- *Basic Settings* → **Tone Settings** : profil France, sinon la tonalité d'occupation
  n'est pas reconnue par le combiné

> Rappel de câblage : entre deux bâtiments, toujours de l'Ethernet ou de la fibre jusqu'à
> l'ATA, jamais du cuivre analogique. Une paire téléphonique enterrée entre deux bâtiments
> est un excellent capteur de surtension d'orage.

---

## Grandstream HT813 — pont vers la ligne Freebox

Le HT813 a deux ports : un **FXS** (pour brancher un téléphone) et un **FXO** (pour se
brancher *sur* une ligne). C'est le port FXO qui nous intéresse — il se comporte comme un
téléphone décroché sur la prise de la Freebox.

### Enregistrement

- *FXO Port* → **SIP Server** : `10.0.90.20`
- *FXO Port* → **SIP User ID** / **Authenticate ID** : `grandstream-fxo`
- *FXO Port* → **Authenticate Password** : le mot de passe généré

### Appels entrants

- **Number of Rings Before Pickup** : `1` — décrocher au premier coup de sonnerie
- **Wait for Dial Tone** : `No` côté entrant
- **Unconditional Call Forward to VOIP** → **User ID** : `s`

Le `s` fait arriver l'appel sur l'extension `s` du contexte `from-external-incoming`,
c'est-à-dire directement sur le menu vocal. Le dialplan généré accepte aussi n'importe
quel autre numéro comme point d'entrée (`exten => _.` renvoie sur `s`), ce qui évite une
panne si le firmware envoie autre chose.

### Détection du raccroché — le réglage le plus important

Sans détection fiable, le HT813 garde la ligne décrochée après que le correspondant a
raccroché : la ligne Freebox reste occupée, et plus aucun appel ne passe jusqu'au
redémarrage de l'ATA.

- **Enable Current Disconnect** : `Yes`
- **Current Disconnect Threshold** : commencer à `200 ms`, ajuster entre 100 et 400 ms
- **Enable Call Progress Tones (Busy Tone) Disconnect** : `Yes`, en filet de sécurité

Test à faire explicitement après installation : appeler un mobile depuis un poste de la
maison, raccrocher **côté mobile**, puis vérifier que la ligne se libère.

```bash
sudo asterisk -rx "core show channels"   # plus aucun canal ne doit subsister
```

Si un canal persiste, augmentez le seuil par paliers de 50 ms.

---

## Yeastar TA200 — salon et étage

Deux ports FXS, configurés indépendamment. Chaque port correspond à un poste distinct dans
l'interface (`salon` et `etage`).

- *Gateway* → *FXS Port 1* → **Registration** : identifiants du poste `salon`
- *Gateway* → *FXS Port 2* → **Registration** : identifiants du poste `etage`

### Port 1 — téléphone à cadran

Deux réglages spécifiques, l'un et l'autre indispensables :

- **Pulse Dial / Rotary Dial Detection** : activé. Sans lui, le cadran n'envoie rien
  d'exploitable.
- **Hotline** (parfois *Offhook Auto-Dial* ou *Immediate Dial*) : `*9`.

Au décroché, l'ATA compose `*9` tout seul, Asterisk répond par l'annonce
« Composez votre numéro » et attend une numérotation courte. Côté interface, le poste
`salon` doit être en mode **hotline** avec `*9` comme cible — les deux réglages vont par
paire.

Comme le cadran ne produit que les chiffres 1 à 0, tout ce qui doit être joignable depuis
ce poste passe par des numéros abrégés courts (écran **Abrégés**).

---

## Gigaset N510 IP PRO + combinés Maxwell C

La base porte un seul compte SIP côté Asterisk, et distribue l'appel à ses combinés.

- *Telephony* → *Connections* → **IP1** :
  - Registrar : `10.0.90.20`
  - Username / Authentication name : `dect`
  - Password : le mot de passe généré
- *Telephony* → *Number Assignment* : rattacher tous les combinés à la connexion IP1, en
  réception **et** en émission

Dans l'interface, le poste `dect` doit avoir **Contacts simultanés** à `3` ou plus : la
base peut ouvrir plusieurs enregistrements selon le firmware, et une valeur à `1` provoque
des désenregistrements en cascade difficiles à diagnostiquer.

---

## Waveshare SIM7600G-H — secours GSM

### Côté hyperviseur

Passthrough USB vers la VM, voir
[02 — Installation](02-installation.md#1-hyperviseur--passthrough-usb-esxi).

### Côté VM

```bash
lsusb
ls -l /dev/serial/by-id/
```

Le module expose plusieurs ports série. Deux nous intéressent : celui des commandes AT et
celui de l'audio. Leurs suffixes (`-if02-port0`, `-if04-port0`) varient selon le firmware —
c'est le seul point à vérifier appareil en main.

Reporter les chemins dans `/etc/asterisk/quectel.conf`, puis :

```bash
sudo asterisk -rx "quectel show devices"
```

### Vérifier l'audio

Le SIM7600G-H peut router la voix soit par une carte son USB (UAC), soit par un bus
matériel absent ici. En cas d'appel établi mais muet, interroger le module :

```
AT+QCFG="usbcfg"      # la configuration doit inclure l'audio USB
AT+CSDVC?             # périphérique audio sélectionné
```

### Signal

```bash
sudo asterisk -rx "quectel show device state quectel0"
```

Un RSSI faible se traduit par des appels de secours inaudibles au pire moment. Si le signal
est mauvais dans le local technique, une antenne déportée coûte peu et change tout.

---

## Combiné USB Poly Calisto — optionnel

Piloté par `chan_console`, qui est un canal audio local et non un endpoint SIP. Il ne se
gère donc pas depuis l'interface, qui ne connaît que des endpoints PJSIP.

```bash
sudo asterisk -rx "console show devices"
evtest        # identifier le crochet commutateur, si le modèle le remonte en HID
```

Le mode d'emploi complet de `chan_console` sort du périmètre de cette installation : c'est
un confort, pas un poste de la maison. Si le crochet commutateur n'est pas détecté en HID,
le combiné reste utilisable en décrochant depuis la console Asterisk.
