# Home Assistant : état des lignes et notification d'appel

## Ce que ça donne

Un appareil « Téléphonie maison » apparaît dans Home Assistant, avec trois entités par
poste :

| Entité | Type | Ce qu'elle dit |
|---|---|---|
| `binary_sensor.<poste>_joignable` | connectivité | Asterisk sait où joindre l'appareil — il est branché et enregistré |
| `sensor.<poste>_etat` | texte | `libre`, `sonne`, `en ligne`, `occupé`, `injoignable` |
| `event.<poste>_appel_entrant` | événement | un appel arrive, avec le numéro et le nom de l'appelant |

L'entité `event` est celle qui déclenche une automatisation — annoncer l'appelant sur un
satellite vocal, par exemple.

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

## Automatisation : annoncer l'appelant

L'entité `event` porte les attributs `numero`, `nom` et `poste`. Une automatisation qui
fait parler un satellite vocal :

```yaml
automation:
  - alias: Annoncer les appels entrants
    triggers:
      - trigger: state
        entity_id: event.salon_appel_entrant
    conditions:
      # Un `event` change d'état à chaque occurrence : on écarte le premier
      # rendu après un redémarrage, qui n'est pas un vrai appel.
      - condition: template
        value_template: "{{ trigger.from_state.state not in ['unknown', 'unavailable'] }}"
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

---

## Ce que le pont ne fait pas

**Il ne pilote rien.** Le compte AMI est en lecture seule : impossible de lancer un appel
depuis Home Assistant par ce chemin. Pour ça, l'API JSON existante a déjà `/api/call` et
`/api/notify` — voir [04 — Interface](04-interface.md#api-json). Les deux mécanismes sont
volontairement séparés : celui qui observe ne peut pas agir.

**Il ne suit pas les appels sortants.** Seul `DialBegin` vers un poste de la maison est
traduit. Un appel sortant fait bien changer l'état du poste (`en ligne`), mais ne produit
pas d'événement.

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
| Un appel annoncé deux fois | Deux postes sonnent pour le même appel (groupe, ou personne avec plusieurs postes) : chacun émet son événement. Filtrez sur un seul poste dans l'automatisation |
