# Configuration Asterisk — anatomie

## Le partage statique / généré

`/etc/asterisk` contient deux familles de fichiers, et il faut savoir laquelle on
regarde avant de modifier quoi que ce soit.

```
/etc/asterisk/
├── pjsip.conf              ← STATIQUE   transports, gabarits, sécurité
├── extensions.conf         ← STATIQUE   sous-routines, codes de service
├── voicemail.conf          ← STATIQUE   réglages généraux de la messagerie
├── logger.conf             ← STATIQUE
├── rtp.conf                ← STATIQUE
├── cdr.conf                ← STATIQUE
├── manager.conf            ← STATIQUE   AMI, pour Home Assistant
├── quectel.conf            ← STATIQUE   module GSM
└── generated/
    ├── pjsip_endpoints.conf        ← GÉNÉRÉ  un bloc par poste
    ├── extensions_generated.conf   ← GÉNÉRÉ  dialplan qui dépend des données
    └── voicemail_generated.conf    ← GÉNÉRÉ  une ligne par boîte
```

Les fichiers **statiques** sont dans ce dépôt, sous `asterisk/`. On les édite à la main,
rarement, et on les versionne. Les fichiers **générés** sont produits par l'interface à
partir de la base : les éditer n'a aucun effet durable, la prochaine application les
écrase. Chacun commence d'ailleurs par un bandeau qui le rappelle.

Ce découpage évite l'écueil habituel des PBX à interface graphique, où l'outil réécrit
l'intégralité de la configuration et où le moindre réglage fin se perd au premier
enregistrement.

## Le mécanisme `#include`

Asterisk traite `#include` comme une insertion textuelle, au point du fichier où la
directive apparaît. Deux conséquences que la configuration exploite directement.

**Dans `pjsip.conf`**, l'inclusion arrive *après* les gabarits :

```ini
[endpoint-internal](!)      ; le (!) marque un gabarit, jamais instancié seul
type=endpoint
disallow=all
allow=alaw
…

#include generated/pjsip_endpoints.conf
```

Ce qui permet aux endpoints générés de tenir en quelques lignes :

```ini
[salon](endpoint-internal)  ; hérite de tout le gabarit
aors=salon
auth=salon-auth
context=from-internal
```

Déplacer le `#include` avant les gabarits casserait le chargement : un gabarit n'existe
qu'après sa déclaration.

> **L'objet `aor` porte exactement l'identifiant SIP du poste**, pas un nom dérivé.
> Sur un REGISTER, Asterisk compare la partie utilisateur de l'en-tête `To:` au *nom* de
> l'`aor` : un `aor` appelé `salon-aor` ne serait jamais trouvé, et l'appareil resterait
> « non enregistré » avec, côté serveur, un `AOR '' not found for endpoint 'salon'` qui
> semble accuser l'appareil. L'objet `auth`, lui, est désigné explicitement par la ligne
> `auth=` : son nom est libre. Un test du générateur verrouille cette règle.

**Dans `voicemail.conf`**, l'inclusion est placée *à l'intérieur* d'une section :

```ini
[default]
#include generated/voicemail_generated.conf
```

Les lignes `100 => 5670,Salon,…` du fichier inclus appartiennent donc bien au contexte de
messagerie `default`, sans que le fichier généré ait à redéclarer la section.

## `pjsip.conf`

### Sécurité des appels anonymes

```ini
[global]
type=global
endpoint_identifier_order=ip,username,anonymous
```

PJSIP n'a **pas** d'équivalent aux options `allowguest=no` et `alwaysauthreject=yes` que
l'on croise dans beaucoup de documentations : celles-ci appartiennent à `chan_sip`,
supprimé depuis Asterisk 21. Les recopier dans un `pjsip.conf` ne produit aucune erreur
visible et aucune protection non plus — le piège est là.

Le comportement équivalent est acquis par construction : un appel non authentifié est
rejeté tant qu'aucun endpoint nommé `anonymous` n'existe. Il suffit de ne jamais en créer.

### Les deux gabarits

| | `endpoint-internal` | `endpoint-trunk` |
|---|---|---|
| Contexte d'entrée | `from-internal` | `from-external-incoming` |
| Transfert d'appel | autorisé | **interdit** |
| Souscriptions (BLF, MWI) | autorisées | non |
| Appliqué à | ATA, DECT, mobile | pont FXO, passerelles |

`allow_transfer=no` sur les trunks bloque le scénario classique de fraude : un appel
entrant est transféré vers un numéro surtaxé à l'étranger, et c'est la ligne de la maison
qui paie la communication.

### NAT et média

```ini
direct_media=no
rtp_symmetric=yes
force_rport=yes
rewrite_contact=yes
```

`direct_media=no` force l'audio à transiter par Asterisk plutôt qu'en direct entre deux
postes. On y perd un peu de latence, on y gagne un point de passage unique : les
enregistrements, la mise en attente et le transfert fonctionnent, et le diagnostic d'un
problème audio se fait en un seul endroit. Sur un réseau domestique, le coût est nul.

`rewrite_contact` fait confiance à l'adresse d'où le paquet arrive plutôt qu'à celle que
l'appareil annonce — indispensable pour des ATA qui se déclarent souvent avec une adresse
inutilisable.

## `extensions.conf`

### Ce qui reste statique

- `[sub-ring]` — sonner une ou plusieurs destinations, puis basculer sur la messagerie.
  Trois arguments : la chaîne `Dial`, la durée, la boîte vocale (éventuellement vide).
- `[features]` — les codes de service `*97`, `*98`, `*43`, `*60`, `*65`.
- `[default]` — **volontairement vide**. Un canal qui atterrit là par erreur de
  configuration ne peut rien composer, au lieu d'hériter d'un droit d'appel sortant. La
  configuration d'exemple livrée par `make samples` fait l'inverse, et c'est une cause
  classique de facture surprise.

```ini
[general]
static=yes
writeprotect=yes
```

`writeprotect=yes` empêche la commande CLI `dialplan save` d'écraser un fichier écrit à la
main.

### Ce qui est généré

`generated/extensions_generated.conf` contient les contextes qui dépendent des données :
`[globals]`, `[sub-dial-number]`, `[from-internal]`, `[from-external-incoming]`,
`[menu-personne]` et `[notify]`.

Le fichier est **du dialplan pur** : aucun `AGI`, aucun appel HTTP, aucune lecture de base
pendant un appel. L'interface de gestion peut être arrêtée, plantée ou en cours de mise à
jour, la téléphonie continue.

### Le failover sortant en détail

Généré à partir de la table des routes, dans l'ordre de priorité :

```ini
[sub-dial-number]
exten => s,1,NoOp(Sortant vers ${ARG1})
 same => n,Set(NUM_NAT=${ARG1})
 same => n,Set(COURT=$[${LEN(${ARG1})} <= 6])
 same => n(trunk1),NoOp(Essai via Ligne Freebox (FXO))
 same => n,Set(NUM=${NUM_NAT})
 same => n,Dial(PJSIP/${NUM}@grandstream-fxo,30)
 same => n,GotoIf($["${DIALSTATUS}" = "CHANUNAVAIL"]?trunk2)
 same => n,GotoIf($["${DIALSTATUS}" = "CONGESTION"]?trunk2)
 same => n,Return()
 same => n(trunk2),NoOp(Essai via Secours GSM (SIM7600G-H))
 same => n,ExecIf($[${COURT} = 0]?Set(NUM=+33${NUM_NAT:1}):Set(NUM=${NUM_NAT}))
 same => n,Dial(Quectel/quectel0/${NUM},30)
 …
 same => n(nomore),Playback(all-circuits-busy-now)
```

Trois points méritent qu'on s'y arrête.

**Seuls `CHANUNAVAIL` et `CONGESTION` déclenchent la bascule.** Un `NOANSWER` ou un `BUSY`
signifie que la ligne a parfaitement fonctionné et que le correspondant n'a pas répondu :
rappeler par le GSM ne ferait que payer une seconde tentative inutile.

**La détection est quasi instantanée.** Quand Internet tombe, le HT813 perd son
enregistrement SIP ; `Dial()` n'a alors aucun endpoint joignable et rend la main en une à
deux secondes. Le délai de 30 secondes n'est consommé que si la ligne fonctionne.

**Les numéros courts échappent à la réécriture.** La variable `COURT` protège les numéros
d'urgence : transformer `112` en `+33112` produirait un appel qui n'aboutit nulle part, et
c'est le seul cas où l'échec n'est pas rattrapable.

### Numérotation entrante et postes « hotline »

Un téléphone à cadran ne sait composer ni `*` ni `#`, et taper dix chiffres y est une
épreuve. Le poste concerné est donc réglé en mode **hotline** : l'ATA est configuré pour
composer tout seul `*9` dès le décroché, et Asterisk répond par une annonce puis attend
une numérotation courte.

```ini
exten => *9,1,NoOp(Decroche hotline)
 same => n,Answer()
 same => n,Wait(1)
 same => n,Background(custom/compose-1-pour-appeler)
 same => n,WaitExten(10)
```

Comme cette extension vit dans `[from-internal]`, les chiffres composés ensuite tombent
naturellement sur les numéros abrégés et les postes internes : un seul contexte, pas de
duplication.

## `voicemail.conf`

Les PIN sont tirés au hasard, jamais laissés à une valeur partagée. Deux cas :

- **Boîte d'une personne** : PIN aléatoire généré à la création, stocké en base, affiché
  une fois dans l'interface et réinitialisable d'un bouton.
- **Boîte d'un poste ou d'un groupe** : PIN dérivé de façon déterministe du numéro de
  boîte. Un tirage aléatoire à chaque génération changerait le code à chaque application,
  ce qui rendrait la boîte inutilisable. Les valeurs triviales (`0000`, `1234`…) sont
  exclues du calcul.

Chacun change son code en composant `*97` puis `0`.

## `rtp.conf`

```ini
rtpstart=10000
rtpend=10200
strictrtp=yes
```

Deux cents ports, soit une centaine d'appels simultanés — très au-delà des besoins d'une
maison, et bien plus facile à autoriser précisément dans un pare-feu que la plage
10000-20000 par défaut. `strictrtp` ignore les paquets audio qui ne viennent pas de la
source négociée.

## `manager.conf`

**L'interface ne s'en sert pas.** Elle pilote Asterisk par `asterisk -rx`, en passant par
le socket de contrôle local — accessible parce que le service tourne sous l'utilisateur
`asterisk`. Ni mot de passe, ni ACL à maintenir pour ça.

L'AMI est activé pour un seul usage : l'intégration Home Assistant, qui tourne chez elle
et se connecte donc **depuis le réseau**. Voir [11 — Home Assistant](11-home-assistant.md).

Trois barrières l'encadrent :

- **aucun compte tant que `install.sh --ha-ip` n'a pas été passé.** Sans lui, `manager.d/`
  est vide et personne ne peut s'identifier, quel que soit le `bindaddr` ;
- `deny = 0.0.0.0/0` puis `permit` sur la seule adresse de Home Assistant ;
- des classes taillées au besoin : `read = system,call,dtmf,reporting`,
  `write = system,reporting`. Pas de `originate`, donc ce compte **ne peut pas lancer
  d'appel** — le scénario de fraude reste hors de portée même si Home Assistant tombe.

`read` gouverne les événements reçus, `write` les actions autorisées : deux choses
distinctes, ce qui explique qu'on lise si souvent `read = all, write = all`. Pour savoir ce
qu'une action exige, Asterisk le dit :

```bash
asterisk -rx "manager show command PJSIPShowEndpoints"
```

Le compte lui-même n'est ni dans ce fichier ni dans le dépôt : `scripts/install.sh` écrit
`manager.d/homeassistant.conf` avec un secret tiré au hasard, inclus par la dernière ligne
de `manager.conf`. Un secret versionné serait le même sur toutes les installations.
