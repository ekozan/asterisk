# Home Assistant : état des lignes et notification d'appel

## Ce que ça donne

Un appareil « Téléphonie maison » apparaît dans Home Assistant, avec trois entités par
poste :

| Entité | Type | Ce qu'elle dit |
|---|---|---|
| `binary_sensor.<poste>_joignable` | connectivité | Asterisk sait où joindre l'appareil — il est branché et enregistré |
| `sensor.<poste>_etat` | texte | `libre`, `sonne`, `en ligne`, `occupé`, `injoignable` |
| `event.<poste>_appel` | événement | un appel commence, dans un sens ou dans l'autre |
| `event.telephonie_appel_termine` | événement | un appel s'achève : durée, issue, numéros — une seule entité pour toute l'installation |

L'entité `event` est celle qui déclenche une automatisation. Son `event_type` porte le
sens de l'appel, et ses attributs le reste :

| `event_type` | Quand | `numero` |
|---|---|---|
| `entrant` | l'extérieur fait sonner un poste | l'appelant |
| `sortant` | un poste compose un numéro | le numéro composé |
| `interne` | un poste en appelle un autre | le poste appelant |

```json
{
  "event_type": "entrant",
  "numero": "0102030405",
  "nom": "Mamie",
  "poste": "Salon",
  "horodatage": "2026-08-21T01:32:03+02:00"
}
```

L'horodatage est posé à la réception de l'événement, avec le décalage horaire local — sans
lui, Home Assistant lirait l'heure comme de l'UTC. L'AMI ne date ses propres messages que
si `timestampevents` est activé ; le trajet par le socket local se comptant en fractions de
milliseconde, on s'en passe.

**Le sens se déduit des deux extrémités de l'appel**, pas du nom des canaux : un poste de
la maison d'un côté et rien de connu de l'autre donnent un entrant ou un sortant selon la
place. C'est ce qui permet au trunk GSM, qui n'est pas un endpoint de la base, de compter
comme « l'extérieur » sans configuration supplémentaire. Le pont FXO, lui, *est* dans la
base : il est marqué comme passerelle (`kind = fxo`), sinon un appel venu de la Freebox
passerait pour un appel interne.

Un appel sortant qui bascule du pont FXO vers le GSM produit deux `Dial()` pour un seul
appel réel : le second n'est pas annoncé. Un groupe qui fait sonner trois postes, en
revanche, produit bien trois annonces — trois téléphones sonnent vraiment.

---

## Pourquoi un service séparé

Le réflexe serait de mettre un `CURL()` dans le dialplan, à l'endroit où l'appel arrive.
C'est le piège : **le dialplan est dans le chemin de l'appel**. Home Assistant éteint, la
requête HTTP attend son délai d'expiration pendant que le téléphone devrait sonner. Un
service de domotique en panne ferait alors rater des appels, ce qui est exactement
l'inverse de ce qu'on cherche.

`telephonie-events` est donc un processus à part :

```
Asterisk ──AMI──▶ telephonie-events ──MQTT──▶ Home Assistant ──▶ satellite vocal
   │                                 (127.0.0.1)
   └── continue de traiter les appels si tout ce qui est à droite est éteint
```

Asterisk émet ses événements sans savoir qui écoute. Si ce service, le courtier MQTT ou
Home Assistant tombe, les appels ne s'en aperçoivent pas. La seule chose perdue, c'est la
notification.

Le pont ne sonde pas : il reçoit. L'API JSON existante (`/api/status`) interroge Asterisk
par `asterisk -rx`, ce qui lance un processus à chaque appel — acceptable pour un tableau
de bord rafraîchi toutes les minutes, hors de question pour détecter une sonnerie qui doit
arriver en moins d'une seconde.

---

## Mise en service

### 1. Le compte AMI

`scripts/install.sh` s'en charge : il écrit `/etc/asterisk/manager.d/telephonie-events.conf`
avec un mot de passe tiré au hasard, et recopie ce mot de passe dans
`/etc/telephonie/events.env`. Le secret n'est pas dans le dépôt — versionné, il serait le
même sur toutes les installations.

Le compte est **en lecture seule** (`write =` vide) et n'accepte que `127.0.0.1` : il peut
écouter les événements, pas originer un appel ni recharger la configuration.

> L'AMI n'est actif que si `manager.conf` contient la ligne `#include manager.d/*.conf`.
> C'est le cas du fichier livré, déployé par `install.sh --with-asterisk-conf`. Si vous
> avez gardé votre propre `manager.conf`, ajoutez l'inclusion et `enabled = yes` — l'écran
> **Fichiers** de l'interface sait le faire.

### 2. Le courtier MQTT

Renseignez-le dans `/etc/telephonie/events.env` :

```bash
sudo tee /etc/telephonie/events.env >/dev/null <<'EOF'
TELEPHONIE_MQTT_HOST=10.0.5.10
TELEPHONIE_MQTT_PORT=1883
TELEPHONIE_MQTT_USER=telephonie
TELEPHONIE_MQTT_PASSWORD=…
TELEPHONIE_AMI_USER=telephonie-events
TELEPHONIE_AMI_SECRET=…          # celui de manager.d/telephonie-events.conf
EOF
sudo chown root:asterisk /etc/telephonie/events.env
sudo chmod 0640 /etc/telephonie/events.env

sudo systemctl enable --now telephonie-events
sudo systemctl status telephonie-events
```

Tant que `TELEPHONIE_MQTT_HOST` est vide, le service s'arrête immédiatement avec un
message explicite plutôt que de tourner à vide.

### 3. Vérifier

Les entités doivent apparaître seules dans Home Assistant, par la découverte MQTT. Si ce
n'est pas le cas, regardez ce qui est publié :

```bash
mosquitto_sub -h 10.0.5.10 -u telephonie -P … -v -t 'telephonie/#'
```

Décrochez un poste : vous devez voir `telephonie/salon/etat en ligne` passer.

### Vérifier ce qu'Asterisk émet vraiment

Les noms de champs de l'AMI changent d'une version à l'autre, et le numéro composé sur un
appel sortant se lit dans `DialString`, dont la forme dépend de la technologie du trunk.
Plutôt que de faire confiance à la documentation :

```bash
sudo -u asterisk TELEPHONIE_AMI_SECRET=… /opt/telephonie/venv/bin/python \
     -m app.events --dump
```

Le service affiche les événements bruts au lieu de publier. Passez un appel dans chaque
sens et lisez : c'est la même méthode que pour les P-values Grandstream — la machine dit
la vérité, la documentation dit ce qui était vrai quelque part.

Si `numero` remonte vide sur les sortants, c'est là que vous verrez pourquoi : comparez le
`dialstring` affiché à ce que `number_from_dialstring` sait découper
(`app/events.py`).

---

## Réglages

Tous par variables d'environnement, dans `/etc/telephonie/events.env`.

| Variable | Défaut | Rôle |
|---|---|---|
| `TELEPHONIE_MQTT_HOST` | *(vide)* | Courtier. Vide = service désactivé |
| `TELEPHONIE_MQTT_PORT` | `1883` | |
| `TELEPHONIE_MQTT_USER` / `_PASSWORD` | *(vide)* | Authentification du courtier |
| `TELEPHONIE_MQTT_PREFIX` | `telephonie` | Racine des sujets d'état |
| `TELEPHONIE_MQTT_DISCOVERY_PREFIX` | `homeassistant` | Racine de la découverte |
| `TELEPHONIE_AMI_HOST` / `_PORT` | `127.0.0.1` / `5038` | |
| `TELEPHONIE_AMI_USER` / `_SECRET` | `telephonie-events` / *(vide)* | Compte AMI |

---

## Les appels terminés

Asterisk **n'a pas de backend CDR vers MQTT**, et n'en aura pas. La façon prévue de sortir
les enregistrements en temps réel est de les émettre sur l'AMI et de laisser un pont
externe les traduire : c'est le module `cdr_manager`, que `install.sh --with-asterisk-conf`
active, et que le même service traduit sur la connexion AMI déjà ouverte.

`DialBegin` dit qu'un téléphone sonne ; le CDR dit ce que l'appel est devenu :

```json
{
  "event_type": "sans_reponse",
  "sens": "entrant",
  "source": "0102030405",
  "destination": "100",
  "poste": "Salon",
  "debut": "2026-08-21 01:32:03",
  "reponse": "",
  "fin": "2026-08-21 01:32:33",
  "duree": 30,
  "duree_conversation": 0,
  "horodatage": "2026-08-21T01:32:33+02:00"
}
```

`event_type` porte l'issue : `repondu`, `sans_reponse`, `occupe`, `echec`, `congestion`.
`duree` compte depuis le début de l'appel, `duree_conversation` depuis le décroché — leur
écart, c'est le temps de sonnerie.

**Une seule entité pour toute l'installation**, pas une par poste : un appel sans réponse
n'a pas de poste, et c'est précisément celui qu'on veut voir passer.

Deux choses à savoir :

- **Le compte AMI doit porter la classe `cdr`** dans son `read`. Sans elle, Asterisk émet
  les enregistrements et l'AMI ne les délivre pas — sans le moindre message. `install.sh`
  ajoute la classe aux comptes existants.
- **Un appel produit un enregistrement par tronçon**, selon les réglages de `cdr.conf`
  (`unanswered=yes` en ajoute). Les tronçons techniques (canaux `Local/`) sont écartés et
  chaque identifiant n'est annoncé qu'une fois, mais si vous voyez encore des doublons,
  `--dump` montre exactement ce qu'Asterisk émet.

Le CSV (`/var/log/asterisk/cdr-csv/Master.csv`) continue de tourner en parallèle : c'est la
trace durable, lisible même quand le courtier et Home Assistant sont éteints. MQTT en est
une copie volatile.

---

## Automatisation : annoncer l'appelant

L'entité `event` porte les attributs `numero`, `nom` et `poste`. Une automatisation qui
fait parler un satellite vocal :

```yaml
automation:
  - alias: Annoncer les appels entrants
    triggers:
      - trigger: state
        entity_id: event.salon_appel
    conditions:
      # Un `event` change d'état à chaque occurrence : on écarte le premier
      # rendu après un redémarrage, qui n'est pas un vrai appel.
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
      # Le filtre de sens est ici, et pas dans le déclencheur : `event_type` ne
      # « change » pas entre deux appels entrants successifs, donc un
      # déclencheur sur cet attribut raterait le second.
      - condition: template
        value_template: "{{ trigger.to_state.attributes.event_type == 'entrant' }}"
    actions:
      - action: assist_satellite.announce
        target:
          entity_id: assist_satellite.cuisine
        data:
          message: >
            {% set nom = trigger.to_state.attributes.nom %}
            {% set numero = trigger.to_state.attributes.numero %}
            {% if nom %}Appel de {{ nom }}
            {% elif numero %}Appel du {{ numero | regex_replace('(\d)', '\\1 ') }}
            {% else %}Appel d'un numéro masqué
            {% endif %}
```

> Le `regex_replace` espace les chiffres pour que la synthèse les énonce un par un plutôt
> que de lire « zéro un milliard deux cent trois millions… ».

Sans la seconde condition, l'annonce se déclencherait aussi quand vous décrochez pour
appeler.

### Journaliser tous les appels

Une seule entité suffit — celle des appels terminés couvre toute l'installation, et porte
la durée que l'événement de début ne connaît pas encore :

```yaml
automation:
  - alias: Journal des appels
    triggers:
      - trigger: state
        entity_id: event.telephonie_appel_termine
    conditions:
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
    actions:
      - action: logbook.log
        data:
          name: Téléphone
          message: >
            {% set a = trigger.to_state.attributes %}
            {{ a.sens }} — {{ a.source or 'masqué' }} → {{ a.destination }} —
            {{ a.event_type }}
            {% if a.duree_conversation %} ({{ a.duree_conversation }} s){% endif %}
```

> Rien à étendre quand vous ajoutez un poste, contrairement à une automatisation bâtie sur
> les entités par poste. La source complète et sans trou reste malgré tout le CDR en CSV,
> que l'écran **Journal** de l'interface affiche déjà.

---

## Ce que le pont ne fait pas

**Il ne pilote rien.** Le compte AMI est en lecture seule : impossible de lancer un appel
depuis Home Assistant par ce chemin. Pour ça, l'API JSON existante a déjà `/api/call` et
`/api/notify` — voir [04 — Interface](04-interface.md#api-json). Les deux mécanismes sont
volontairement séparés : celui qui observe ne peut pas agir.

**Il ne publie pas les changements d'état pendant l'appel.** Mise en attente, transfert,
conférence : rien de tout ça n'est traduit. L'état du poste (`en ligne`) et les deux
bornes de l'appel suffisent à l'usage visé.

**Il ne remonte pas la messagerie vocale.** Les nouveaux messages sont visibles dans
l'interface et envoyés par courriel ; les publier sur MQTT demanderait de suivre
l'événement `MessageWaiting`, ce qui n'est pas implémenté.

---

## Dépannage

| Symptôme | Piste |
|---|---|
| Aucune entité dans Home Assistant | `systemctl status telephonie-events`. Puis `mosquitto_sub -t 'homeassistant/#' -v` : les configurations de découverte sont-elles publiées ? |
| Entités présentes mais « indisponible » | Le service ne tourne plus : le testament MQTT a publié `offline` sur `telephonie/status`. C'est le comportement voulu — mieux vaut « indisponible » qu'un état figé qui ment |
| `AMI : identification refusée` | Le secret de `events.env` ne correspond plus à celui de `manager.d/telephonie-events.conf` |
| `AMI : connexion fermée` en boucle | `manager.conf` contient-il `enabled = yes` et l'inclusion `manager.d/*.conf` ? `asterisk -rx "manager show settings"` |
| L'état reste `injoignable` | C'est l'état réel : le poste n'est pas enregistré. Voir [07 — Dépannage](07-depannage.md#un-poste-napparaît-pas-comme-enregistré) |
| Un appel annoncé deux fois | Deux postes sonnent pour le même appel (groupe, ou personne avec plusieurs postes) : chacun émet son événement, c'est voulu. Filtrez sur un seul poste dans l'automatisation |
| Aucun événement d'appel terminé | La classe `cdr` manque au compte AMI : `grep read /etc/asterisk/manager.d/telephonie-events.conf`. Vérifiez aussi `enabled = yes` dans `cdr_manager.conf` |
| `numero` vide sur les appels sortants | `DialString` a une forme que le découpage ne reconnaît pas. `python -m app.events --dump`, passez un appel sortant, lisez le champ `dialstring` |
| Un appel entrant annoncé comme « interne » | Le pont FXO n'est pas marqué `kind = fxo` dans la base. Écran **Postes**, type « Pont vers ligne externe (FXO) » |
