# Home Assistant

L'intégration [`TECH7Fox/asterisk-hass-integration`](https://github.com/TECH7Fox/asterisk-hass-integration),
installée par HACS, découvre les postes et suit leur état. Elle est développée et
documentée contre FreePBX, ce qui en fait le chemin le mieux balisé.

> Comme les deux autres documents de ce répertoire, celui-ci décrit une configuration à
> faire. Ce qui suit sur les entités et les besoins de l'intégration a en revanche été
> vérifié dans son code source, pas supposé.

---

## Ce que ça donne

Un appareil par poste dans Home Assistant, avec :

| Entité | Ce qu'elle dit | Source côté Asterisk |
|---|---|---|
| état de l'appareil | libre, occupé, sonne… | `DeviceStateChange` |
| enregistré | l'appareil est joignable | `DeviceStateChange` |
| ligne connectée | le numéro en conversation, l'identité de l'appelant en attribut | `NewConnectedLine`, `Newchannel`, `Hangup` |
| DTMF émis / reçu | les touches, horodatées | `DTMFBegin` |

Plus une entité globale « AMI connecté », et un service `asterisk.send_action` qui envoie
une action AMI arbitraire.

**Ce que ça ne donne pas :** les enregistrements d'appel. Ni durée, ni issue, ni « sans
réponse ». Ça reste dans le CDR de FreePBX, consultable dans *Reports → CDR Reports*. Si
vous voulez un historique d'appels dans Home Assistant, il faudra le reconstruire à partir
de l'entité « ligne connectée », qui ne dit pas si l'appel a abouti.

**Ni les SMS.** Le SMS dans FreePBX est bâti autour d'opérateurs SIP — le module commercial
*SMS Plus* exige un abonnement Sangoma, l'alternative libre *SMS Connector* parle à Telnyx
ou Flowroute par API. **Aucun ne lit la carte SIM d'un module GSM.** Pour ces SMS-là, il
faut un script, quel que soit le PBX — ou une passerelle matérielle qui les traite
elle-même.

---

## Le compte AMI

L'intégration tourne **dans** Home Assistant. L'AMI doit donc être joignable depuis le
réseau, contrairement au socket local dont FreePBX se sert lui-même. Et comme elle exécute
`SIPpeers` et `PJSIPShowEndpoints` pour découvrir les postes, **un compte en lecture seule
ne suffit pas** : ce sont des actions, pas des événements.

C'est le coût de ce choix, et il vaut mieux le regarder en face : un port de pilotage du
PBX devient joignable depuis le réseau d'administration.

### Créer le compte

FreePBX réécrit `manager.conf` à chaque `fwconsole reload`. Le compte va donc dans le
fichier compagnon, que FreePBX inclut et ne touche jamais :

```bash
sudo tee -a /etc/asterisk/manager_custom.conf >/dev/null <<'EOF'

[homeassistant]
secret = <un mot de passe long, tiré au hasard>
deny = 0.0.0.0/0
permit = 10.0.5.10/32
read = system,call,dtmf,reporting
write = system,reporting
EOF

sudo fwconsole reload
sudo asterisk -rx "manager show users"     # doit lister homeassistant
```

`10.0.5.10` est un **exemple** : remplacez-le par l'adresse réelle de votre Home
Assistant. Toutes les adresses de cette documentation viennent du plan de référence
(`10.0.5.0/24` pour l'administration, `10.0.90.0/24` pour la voix) et ne correspondent
pas forcément à votre réseau.

Générez le secret plutôt que de l'inventer :

```bash
head -c 24 /dev/urandom | base64 | tr -d '/+=' | head -c 32
```

### Pourquoi ces classes, et pas `all`

```ini
read  = system,call,dtmf,reporting     ; les ÉVÉNEMENTS reçus
write = system,reporting               ; les ACTIONS autorisées
```

`read` et `write` gouvernent deux choses distinctes, et c'est cette confusion qui explique
qu'on lise partout `read = all, write = all`. Ici :

- `call` apporte `DeviceStateChange`, `Newchannel`, `Hangup`, `NewConnectedLine` ;
- `dtmf` apporte `DTMFBegin` ;
- `system` et `reporting` autorisent `SIPpeers` et `PJSIPShowEndpoints`.

**`write` ne contient ni `originate` ni `call`.** En l'état, `asterisk.send_action` peut
interroger, pas lancer d'appel. Si Home Assistant est compromis, l'attaquant lit l'état de
votre téléphonie ; il ne passe pas d'appels surtaxés à l'étranger. C'est exactement le
scénario contre lequel le reste de l'installation se protège.

Pour savoir ce qu'une action précise exige, Asterisk le dit lui-même — plus fiable que
n'importe quelle documentation :

```bash
sudo asterisk -rx "manager show command PJSIPShowEndpoints"
```

### Ouvrir le port — et surtout, où le fermer

> **Ne restreignez pas l'écoute d'Asterisk à l'adresse du VLAN.** C'est le réflexe naturel
> — « qu'il n'écoute que là où Home Assistant se trouve » — et il casse FreePBX.
>
> FreePBX parle à son propre Asterisk **par l'AMI, sur `127.0.0.1`**. Or `bindaddr`
> n'accepte qu'une seule adresse : le passer à celle du VLAN coupe FreePBX de son moteur,
> et l'interface d'administration cesse de fonctionner. Laissez `0.0.0.0`, et faites le
> filtrage ailleurs.

Trois barrières, chacune à sa place :

| Barrière | Où | Valeur |
|---|---|---|
| Écoute | `manager.conf` | `0.0.0.0` — `127.0.0.1` doit rester joignable pour FreePBX |
| Filtrage réseau | pare-feu FreePBX | 5038 depuis la seule adresse de Home Assistant |
| ACL Asterisk | `manager_custom.conf` | `deny = 0.0.0.0/0`, puis `permit = <ip>/32` |

Pour le pare-feu, passez par le module de FreePBX plutôt que par `ufw` : il gère
lui-même les règles de la machine, et deux sources de vérité finiraient par se
contredire. *Connectivity → Firewall → Services*, autoriser **AMI** pour la seule adresse
de Home Assistant, déclarée en *Trusted* dans *Networks*.

**Le masque compte.** `permit = 10.0.5.0/24` laisserait tout appareil de ce réseau tenter
de s'authentifier — vos ATA compris, dont le firmware ne vous appartient pas. Un `/32` sur
l'adresse exacte de Home Assistant, rien d'autre.

**Et regardez où se trouve Home Assistant.** S'il est sur le VLAN voix, l'AMI devient
atteignable par tous les ATA de ce VLAN. L'ACL tient, mais un VLAN voix devrait ne
contenir que des téléphones : si vous avez un VLAN d'administration, c'est là que Home
Assistant et l'AMI sont à leur place.

Vérifiez ensuite ce qui écoute réellement, et que la boucle locale y est bien :

```bash
sudo ss -lntp | grep 5038
sudo asterisk -rx "manager show connected"   # FreePBX doit y figurer, depuis 127.0.0.1
```

---

## Côté Home Assistant

1. **HACS → menu ⋮ → Custom repositories**, ajouter
   `https://github.com/TECH7Fox/Asterisk-integration/` en catégorie *Integration*.
2. Installer l'intégration, redémarrer Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration → Asterisk**, avec :

| Champ | Valeur |
|---|---|
| Host | l'adresse de la VM FreePBX |
| Port | `5038` |
| Username | `homeassistant` |
| Password | le secret généré plus haut |

Les postes doivent apparaître comme appareils. Décrochez un combiné : l'état doit suivre.

---

## Automatisation : annoncer l'appelant

L'entité « ligne connectée » du poste change d'état quand un appel s'y présente, et porte
l'identité de l'appelant en attribut.

```yaml
automation:
  - alias: Annoncer les appels entrants
    triggers:
      - trigger: state
        entity_id: sensor.salon_connected_line
    conditions:
      # Écarte le premier rendu après un redémarrage, qui n'est pas un appel.
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
      # Et les fins d'appel, où la ligne se vide.
      - condition: template
        value_template: "{{ trigger.to_state.state not in ['', 'unknown'] }}"
    actions:
      - action: assist_satellite.announce
        target:
          entity_id: assist_satellite.cuisine
        data:
          message: >
            Appel du {{ trigger.to_state.state | regex_replace('(\d)', '\\1 ') }}
```

> Le `regex_replace` espace les chiffres pour que la synthèse les énonce un par un, au lieu
> de lire « zéro un milliard deux cent trois millions… ».

**Vérifiez le nom exact de l'entité** dans *Outils de développement → États* avant de
copier : il dépend du nom donné à l'extension dans FreePBX. Et regardez la forme réelle de
l'état — c'est peut-être un numéro nu, peut-être quelque chose de plus verbeux, auquel cas
l'attribut `caller_id` sera plus utile que l'état lui-même.

---

## Déclencher un appel depuis Home Assistant

« Fais sonner le poste de l'étage, et quand quelqu'un décroche, appelle Mamie. » C'est
l'action AMI `Originate`, et elle demande deux choses : élargir le compte, et **encadrer ce
qu'il peut composer**.

### Comment ça se déroule, côté combiné

`Originate` sonne d'abord le **poste**. Quand la personne décroche, Asterisk exécute une
extension du dialplan, qui compose alors le contact. La personne entend donc son propre
téléphone sonner, décroche, puis entend la tonalité d'appel du correspondant. C'est
déroutant la première fois, mais c'est le fonctionnement normal.

### La liste fermée des destinations

**C'est la pièce importante.** Plutôt que de laisser Home Assistant composer un numéro
arbitraire, on l'oblige à désigner une entrée d'une liste écrite côté PBX.

Dans `/etc/asterisk/extensions_custom.conf` :

```ini
; Destinations joignables depuis Home Assistant. Liste FERMÉE : une destination
; absente d'ici n'existe pas, et l'Originate échoue. C'est ce qui fait qu'un
; Home Assistant compromis ne peut pas composer un numéro surtaxé.
;
; `Local/<numéro>@from-internal` fait repasser l'appel par vos routes sortantes
; habituelles : failover Freebox puis GSM, et vos filtres d'appels compris.
[ha-appel]
exten => mamie,1,NoOp(Home Assistant appelle Mamie)
 same => n,Dial(Local/0102030405@from-internal,30)
 same => n,Hangup()

exten => medecin,1,NoOp(Home Assistant appelle le médecin)
 same => n,Dial(Local/0102030406@from-internal,30)
 same => n,Hangup()
```

Pas de motif attrape-tout : une extension non déclarée n'existe simplement pas, l'action
échoue, et c'est exactement le comportement voulu. Ajouter un contact demande une ligne
ici et un `fwconsole reload` — c'est délibérément un geste d'administration, pas un champ
de formulaire.

### Élargir le compte AMI

```ini
write = system,reporting,originate
```

> **Avant de le faire, vérifiez `live_dangerously`.** L'action `Originate` accepte un
> paramètre `Application` : avec `Application: System`, elle exécute une commande sur la
> machine. Asterisk bloque ce cas par défaut, mais le réglage `live_dangerously` de
> `asterisk.conf` peut le rouvrir. Il doit rester à `no` :
>
> ```bash
> sudo asterisk -rx "core show settings" | grep -i dangerous
> ```
>
> Avec `live_dangerously = no` et la liste fermée ci-dessus, un Home Assistant compromis
> peut faire sonner vos téléphones et appeler Mamie. C'est tout.

Et vérifiez au passage ce que chaque action exige réellement chez vous — vous pourrez
peut-être retirer `system` de `write` :

```bash
sudo asterisk -rx "manager show command Originate"
sudo asterisk -rx "manager show command PJSIPShowEndpoints"
```

### Le script Home Assistant

```yaml
script:
  appeler_un_contact:
    alias: Appeler un contact depuis un poste
    fields:
      poste:
        selector: {select: {options: ["100", "101", "102", "103", "104"]}}
      contact:
        selector: {select: {options: [mamie, medecin]}}
    sequence:
      - action: asterisk.send_action
        data:
          action: Originate
          parameters:
            Channel: "PJSIP/{{ poste }}"
            Context: ha-appel
            Exten: "{{ contact }}"
            Priority: 1
            CallerID: "Maison <{{ poste }}>"
            Timeout: 30000
            Async: "true"
```

`Async: true` compte : sans lui, l'AMI reste bloqué le temps que l'appel aboutisse, et
Home Assistant considère l'action en échec au bout de son propre délai.

Les `selector` ne sont pas décoratifs : ils empêchent qu'une automatisation mal écrite, ou
une phrase mal reconnue par la synthèse vocale, envoie autre chose que les valeurs prévues.

### L'automatisation

```yaml
automation:
  - alias: Rappel du soir chez Mamie
    triggers:
      - trigger: time
        at: "19:00:00"
    conditions:
      - condition: time
        weekday: [sun]
    actions:
      - action: script.appeler_un_contact
        data:
          poste: "101"
          contact: mamie
```

### Depuis un satellite vocal

Le même script est la brique d'arrivée : une phrase personnalisée d'Assist (« appelle
{contact} ») remplace le déclencheur horaire. Whisper tourne déjà dans Home Assistant via
l'add-on Wyoming, il n'y a pas de moteur supplémentaire à installer.

**Faites reconnaître des noms, pas des chiffres.** « Appelle Mamie » se résout dans la
liste fermée ci-dessus ; « appelle le zéro six… » dépend d'une transcription qui peut se
tromper d'un chiffre — et un chiffre de travers peut donner un numéro surtaxé. La liste
fermée supprime toute cette classe de risque, et c'est la raison principale de sa
présence.

---

## Décrocher et dicter : Assist au bout du fil

Le montage précédent part d'un satellite vocal. Celui-ci part du **combiné** : on décroche,
on compose un numéro interne, Assist répond, on dit un nom, et l'appel est transféré.

C'est le seul montage qui rende un poste sans DTMF utilisable pour autre chose que
composer un numéro — voir
[02 — le téléphone à cadran](02-freepbx-configuration.md#le-téléphone-à-cadran).

### Le principe

L'add-on [**ha-sip**](https://github.com/arnonym/ha-plugins) s'enregistre sur FreePBX comme
un poste ordinaire, décroche, et branche l'audio sur le pipeline Assist — donc sur Whisper.

```
Poste 100 ──compose 199──▶ ha-sip (poste SIP) ──audio──▶ Assist / Whisper
                                    │
                                    └──transfer──▶ sip:mamie@pbx ──▶ Mamie
```

> **Aucune action AMI là-dedans.** Le transfert est fait par ha-sip, en SIP. Le compte AMI
> n'a donc besoin d'aucune classe supplémentaire — ni `originate`, ni `call`. C'est la
> raison de préférer ce montage à un `Redirect` par l'AMI : celui-ci exigerait de retrouver
> le nom exact du canal de l'appelant, qui change à chaque appel, et d'élargir les droits
> du compte.

### D'où Assist sait quel poste appelle

C'est l'add-on qui le lui dit. L'événement `incoming_call` porte :

| Champ | Contenu |
|---|---|
| `parsed_remote_uri` | le numéro de l'appelant — `100` pour le salon |
| `remote_uri` | l'URI SIP complète |
| `internal_id` | l'identifiant de l'appel, à réutiliser dans les commandes |

Vous y accédez par `{{ trigger.json.parsed_remote_uri }}`. Ça sert à trois choses :

- **personnaliser** l'accueil (« Bonjour, poste du salon ») ;
- **restreindre** l'usage à certains postes — un poste d'invité n'a pas à composer par la
  voix ;
- **varier la liste de contacts** selon le poste, si vous le souhaitez.

### La destination du transfert

`transfer` attend une URI SIP. Pour que FreePBX sache la router, la destination doit
exister dans le contexte du poste ha-sip — donc, en pratique, dans `from-internal-custom`,
que FreePBX inclut dans `from-internal` :

```ini
; /etc/asterisk/extensions_custom.conf
;
; Destinations en toutes lettres : aucun clavier téléphonique ne peut les
; composer, elles ne sont donc joignables que par un transfert de ha-sip.
[from-internal-custom]
exten => mamie,1,NoOp(Transfert vocal vers Mamie)
 same => n,Dial(Local/0102030405@from-internal,30)
 same => n,Hangup()
```

La liste reste fermée, pour la même raison que plus haut : la reconnaissance vocale ne peut
désigner qu'une entrée déclarée.

### Ce qu'il reste à éprouver

L'architecture ci-dessus s'appuie sur des éléments vérifiés dans la documentation de
l'add-on — les noms d'événements, les champs de charge utile et l'existence de la commande
`transfer`. Deux points demanderont de la mise au point sur votre matériel :

- **la forme exacte des paramètres de `transfer`** (`number` désigne l'appel actif,
  `transfer_to` la destination) : à confirmer contre la version que vous installez ;
- **la qualité de la transcription**. L'audio d'un appel est du 8 kHz en A-law, très en
  dessous d'un micro de satellite. Sur des prénoms dictés dans un combiné à cadran,
  attendez-vous à des erreurs — prévoyez une confirmation parlée (« j'appelle Mamie, c'est
  bien ça ? ») avant de transférer.

### Ce que ça ajoute comme dépendance

Ce numéro interne ne fonctionne que si Home Assistant et l'add-on tournent. Ce n'est pas
gênant tant que **la composition normale reste possible** : les chiffres et les numéros
abrégés ne passent pas par là. Considérez ce montage comme un confort supplémentaire, pas
comme le chemin d'appel principal — et surtout pas comme le chemin des urgences.

---

## Dépannage

| Symptôme | Piste |
|---|---|
| « Échec d'authentification » | Mot de passe mal recopié, ou `permit` ne contient pas l'adresse réelle de HA. `sudo asterisk -rx "manager show users"`, puis `grep permit /etc/asterisk/manager_custom.conf` |
| Connexion refusée, rien côté Asterisk | Pare-feu FreePBX, ou l'AMI n'écoute pas sur une adresse joignable. `sudo ss -lntp \| grep 5038` |
| Le compte a disparu après un changement | Il a été écrit dans `manager.conf` au lieu de `manager_custom.conf` : FreePBX a réécrit le fichier |
| Connecté, aucun appareil découvert | `PJSIPShowEndpoints` refusé faute de droits. `sudo asterisk -rx "manager show command PJSIPShowEndpoints"` donne la classe exigée, à ajouter à `write` |
| Appareils présents, états figés | La classe `call` manque à `read` : la découverte passe, les événements non |
| Pas de DTMF | La classe `dtmf` manque à `read`. L'intégration attend par ailleurs du « SIP-INFO DTMF-Relay », alors que les extensions sont en `RFC 4733` — les capteurs DTMF peuvent rester vides sans que le reste en souffre |
| `Originate` refusé (`Permission denied`) | La classe `originate` manque à `write`. Voir « Déclencher un appel » ci-dessus |
| L'appel sonne le poste puis raccroche | La destination n'existe pas dans `[ha-appel]`, ou `fwconsole reload` n'a pas été passé après l'avoir ajoutée |
| Échecs d'identification en boucle | Quelqu'un d'autre tape sur le 5038. Resserrez la règle de pare-feu ; ces échecs sont journalisés en NOTICE, donc visibles de fail2ban |
