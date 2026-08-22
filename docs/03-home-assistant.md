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

Remplacez `10.0.5.10` par l'adresse réelle de votre Home Assistant, et générez le secret
plutôt que de l'inventer :

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

### Ouvrir le port

FreePBX gère son propre pare-feu. Passez par lui plutôt que par `ufw`, sinon vous aurez
deux jeux de règles qui se contredisent :

*Connectivity → Firewall → Services*, autoriser **AMI** pour le seul réseau contenant
Home Assistant. Si l'adresse est isolée, déclarez-la en *Trusted* dans *Networks*.

Vérifiez ensuite ce qui écoute réellement :

```bash
sudo ss -lntp | grep 5038
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

## Dépannage

| Symptôme | Piste |
|---|---|
| « Échec d'authentification » | Mot de passe mal recopié, ou `permit` ne contient pas l'adresse réelle de HA. `sudo asterisk -rx "manager show users"`, puis `grep permit /etc/asterisk/manager_custom.conf` |
| Connexion refusée, rien côté Asterisk | Pare-feu FreePBX, ou l'AMI n'écoute pas sur une adresse joignable. `sudo ss -lntp \| grep 5038` |
| Le compte a disparu après un changement | Il a été écrit dans `manager.conf` au lieu de `manager_custom.conf` : FreePBX a réécrit le fichier |
| Connecté, aucun appareil découvert | `PJSIPShowEndpoints` refusé faute de droits. `sudo asterisk -rx "manager show command PJSIPShowEndpoints"` donne la classe exigée, à ajouter à `write` |
| Appareils présents, états figés | La classe `call` manque à `read` : la découverte passe, les événements non |
| Pas de DTMF | La classe `dtmf` manque à `read`. L'intégration attend par ailleurs du « SIP-INFO DTMF-Relay », alors que les extensions sont en `RFC 4733` — les capteurs DTMF peuvent rester vides sans que le reste en souffre |
| Échecs d'identification en boucle | Quelqu'un d'autre tape sur le 5038. Resserrez la règle de pare-feu ; ces échecs sont journalisés en NOTICE, donc visibles de fail2ban |
