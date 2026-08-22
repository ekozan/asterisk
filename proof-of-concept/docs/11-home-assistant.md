# Home Assistant

L'intégration [`TECH7Fox/asterisk-hass-integration`](https://github.com/TECH7Fox/asterisk-hass-integration),
installée par HACS, découvre les postes et suit leur état. Elle tourne **dans** Home
Assistant et se connecte à l'AMI d'Asterisk par le réseau.

Ce document couvre ce qu'il faut préparer **côté Asterisk** : c'est la seule moitié dont ce
dépôt est responsable.

---

## Ce que l'intégration donne

Une fois configurée, elle crée par poste :

| Entité | Ce qu'elle dit | Source |
|---|---|---|
| état de l'appareil | libre, occupé, sonne… | `DeviceStateChange` |
| enregistré | l'appareil est joignable | `DeviceStateChange` |
| ligne connectée | le numéro en conversation, avec l'identité de l'appelant en attribut | `NewConnectedLine`, `Newchannel`, `Hangup` |
| DTMF émis / reçu | les touches, avec horodatage | `DTMFBegin` |

Plus une entité globale « AMI connecté », et un service `asterisk.send_action` qui envoie
une action AMI arbitraire.

**Ce qu'elle ne donne pas** : les enregistrements d'appel. Ni durée, ni issue, ni « sans
réponse ». Ça reste dans le CDR (`/var/log/asterisk/cdr-csv/Master.csv`), que l'écran
**Journal** de l'interface affiche déjà. Si vous voulez un historique des appels dans Home
Assistant, il faudra le construire à partir de l'entité « ligne connectée », qui ne dit pas
si l'appel a abouti.

---

## Ce qu'il faut ouvrir, et pourquoi

L'intégration tourne dans Home Assistant. **L'AMI doit donc être joignable depuis le
réseau**, contrairement au socket de contrôle local qu'utilise l'interface de gestion. Et
comme elle exécute `SIPpeers` et `PJSIPShowEndpoints` pour découvrir les postes, un compte
en lecture seule ne suffit pas.

C'est le principal coût de ce choix, et il vaut mieux le regarder en face : **un port de
pilotage du PBX devient joignable depuis le réseau d'administration.** Trois barrières
l'encadrent.

### 1. Rien tant que l'adresse n'est pas donnée

`install.sh` n'écrit le compte que si vous passez l'adresse de Home Assistant. Sans elle,
`manager.d/` reste vide et personne ne peut s'identifier, quel que soit le `bindaddr` :

```bash
sudo ./scripts/install.sh --with-asterisk-conf --ha-ip 10.0.5.10
```

Le script affiche à la fin l'hôte, le port et le mot de passe tiré au hasard, à recopier
dans Home Assistant. Le secret n'est pas dans le dépôt : versionné, il serait le même sur
toutes les installations.

### 2. Une ACL sur la seule adresse de Home Assistant

```ini
deny = 0.0.0.0/0
permit = 10.0.5.10/32
```

Écrit par le script. Relancer avec une autre `--ha-ip` met la ligne à jour sans changer le
mot de passe.

### 3. Des classes taillées au besoin réel

```ini
read  = system,call,dtmf,reporting
write = system,reporting
```

`read` gouverne les **événements reçus**, `write` les **actions autorisées** — deux choses
distinctes, souvent confondues, et c'est pour ça qu'on lit partout `read = all, write =
all`. Ici :

- `call` apporte `DeviceStateChange`, `Newchannel`, `Hangup`, `NewConnectedLine` ;
- `dtmf` apporte `DTMFBegin` ;
- `system` et `reporting` autorisent `SIPpeers` et `PJSIPShowEndpoints`.

**`write` ne contient ni `originate` ni `call`.** En l'état, `asterisk.send_action` peut
interroger, pas lancer d'appel. Si Home Assistant est compromis, l'attaquant lit l'état de
votre téléphonie ; il ne passe pas d'appels surtaxés à l'étranger — c'est exactement le
scénario contre lequel le reste de l'installation se protège (voir
[06 — Sécurité](06-securite.md#la-fraude-à-la-tonalité)).

Si vous voulez malgré tout pouvoir appeler depuis Home Assistant, **ne touchez pas à ce
compte** : l'interface expose déjà `/api/call` et `/api/notify`, avec un jeton dédié et une
validation stricte du numéro. Un seul chemin d'écriture, contrôlé au même endroit.

### Le pare-feu

L'ACL d'Asterisk refuse la connexion, mais le port répond quand même. Fermez-le en amont :

```bash
sudo ufw allow in from 10.0.5.10 to any port 5038 proto tcp
sudo ufw deny 5038/tcp
```

Et si vous connaissez l'adresse du vNIC d'administration, restreignez aussi l'écoute — une
barrière de plus, modifiable depuis l'écran **Fichiers** de l'interface :

```ini
[general]
bindaddr = 10.0.5.20
```

---

## Vérifier

**Que l'AMI répond et que le compte est chargé :**

```bash
sudo asterisk -rx "manager show settings"    # Enabled: Yes, le bon bindaddr
sudo asterisk -rx "manager show users"       # doit lister « homeassistant »
```

**Qu'une action précise est bien autorisée** — Asterisk dit lui-même quelle classe elle
exige, ce qui évite de deviner :

```bash
sudo asterisk -rx "manager show command PJSIPShowEndpoints"
```

**Depuis Home Assistant**, une fois l'intégration ajoutée : les postes doivent apparaître
comme appareils. Décrochez un combiné, l'état doit suivre.

---

## Dépannage

| Symptôme | Piste |
|---|---|
| Home Assistant : « échec d'authentification » | Mot de passe mal recopié, ou `permit` ne contient pas l'adresse réelle de HA. `sudo asterisk -rx "manager show users"` puis `grep permit /etc/asterisk/manager.d/homeassistant.conf` |
| Connexion refusée, sans trace côté Asterisk | Le pare-feu, ou `bindaddr` sur une adresse que HA n'atteint pas. `sudo ss -lntp \| grep 5038` |
| Connecté, mais aucun appareil découvert | `PJSIPShowEndpoints` refusé faute de droits. `sudo asterisk -rx "manager show command PJSIPShowEndpoints"` donne la classe exigée, à ajouter à `write` |
| Appareils présents, états figés | La classe `call` manque à `read` : l'action de découverte passe, les événements non |
| Pas de DTMF | La classe `dtmf` manque à `read`. L'intégration demande aussi le « SIP-INFO DTMF-Relay » côté appareil, alors que nos endpoints sont en `rfc4733` (voir `pjsip.conf`) |
| Des échecs d'identification en boucle dans les journaux | Quelqu'un d'autre tape sur le 5038. Vérifiez la règle ufw ; fail2ban voit ces NOTICE comme les échecs SIP (voir [06](06-securite.md#fail2ban)) |

---

## Ce qui a été écarté

Un pont maison publiant vers MQTT a existé dans ce dépôt (service `telephonie-events`,
commits `5811b90` à `cb6230f`). Il gardait l'AMI sur `127.0.0.1` avec un compte sans aucun
droit d'écriture, et publiait les enregistrements d'appel complets — durée et issue —
via `cdr_manager`.

Il a été retiré au profit de l'intégration HACS : moins de pièces à maintenir, pas de
courtier MQTT, une configuration par l'interface de Home Assistant, et des capteurs DTMF
utiles pour un portier. Le prix payé est celui décrit plus haut — un AMI joignable depuis
le réseau, avec des droits d'exécution — et la perte des enregistrements d'appel.

`git show 5811b90` le fait revenir si le compromis cesse de convenir. Voir aussi
[09 — Choix techniques](09-choix-techniques.md).
