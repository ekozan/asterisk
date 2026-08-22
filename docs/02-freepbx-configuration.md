# Configuration de FreePBX pour cette installation

> Même avertissement que pour l'installation : **ce document décrit une configuration à
> faire, pas une configuration observée.** Les noms d'écrans varient selon la version des
> modules. Corrigez au fur et à mesure.

Ce qui suit reproduit, dans FreePBX, l'installation décrite dans `proof-of-concept/` : cinq
postes, une ligne Freebox par pont FXO, un secours mobile, un menu vocal à deux niveaux et
une messagerie par poste.

---

## L'ordre des opérations

FreePBX ne demande jamais de redémarrer, mais **rien n'est actif tant que le bandeau rouge
« Apply Config » n'a pas été cliqué**. C'est l'équivalent exact du « Appliquer » du proof
of concept, et il crée le même piège : un poste créé mais non appliqué se présentera avec
un mot de passe qu'Asterisk ne connaît pas encore, et restera « non enregistré ».

En ligne de commande : `sudo fwconsole reload`.

---

## Les postes

*Applications → Extensions → Add Extension → Add New PJSIP Extension*

Un par appareil, en reprenant le plan de numérotation :

| Extension | Display Name | Appareil |
|---|---|---|
| 100 | Salon | Yeastar TA200 port 1 — téléphone à cadran |
| 101 | Étage | Yeastar TA200 port 2 |
| 102 | Garage | Grandstream HT801 |
| 103 | DECT | base Gigaset |
| 104 | Mobile | application mobile |

À régler sur chacun, onglet *Advanced* :

| Réglage | Valeur | Pourquoi |
|---|---|---|
| Codecs | `alaw` puis `ulaw` uniquement | Un combiné analogique reste en bande étroite ; G.722 ou Opus ne feraient que consommer du CPU en transcodage |
| DTMF Signaling | `RFC 4733` | Ce que vos ATA émettent |
| Max Contacts | `1`, sauf le mobile à `2` | Une application mobile peut être ouverte sur deux appareils |
| NAT | `No` | Postes et serveur sont sur le même VLAN |
| Voicemail | activé, avec l'adresse de courriel | |

> **L'identifiant SIP dans FreePBX, c'est le numéro d'extension**, pas un nom.
> Le poste `salon` du proof of concept devient l'utilisateur SIP `100`. Il faudra donc
> reconfigurer chaque ATA : *User Name*, *Authentication Name* et *From User* passent tous
> à `100`. Sur le Yeastar TA200, ces trois champs sont distincts et **les trois comptent** —
> un *User Name* vide produit un `To:` sans utilisateur et un rejet avec `AOR '' not found`
> côté serveur, message qui accuse l'appareil à tort.

---

## Le pont FXO vers la Freebox

Le HT813 est un boîtier SIP d'un côté, une prise téléphonique de l'autre. Il s'enregistre
donc sur FreePBX comme un trunk, pas comme une extension.

*Connectivity → Trunks → Add Trunk → Add SIP (chan_pjsip) Trunk*

- **Trunk Name** : `freebox-fxo`
- **Outbound CallerID** : votre numéro de ligne fixe
- **PJSIP Settings → General** : *Username* et *Secret* que vous recopierez dans le HT813,
  *Authentication* `Inbound`, *Registration* `Receive` — c'est l'appareil qui s'enregistre
  chez vous, pas l'inverse
- **Context** : laissez `from-pstn`, celui des appels venant de l'extérieur

Côté HT813, les réglages n'ont pas changé : voir
[proof-of-concept/docs/08-materiel.md](../proof-of-concept/docs/08-materiel.md).

> **Le seuil de détection du raccroché** (*Current Disconnect Threshold*) reste le réglage
> le plus important et le plus pénible du FXO. Mal calibré, la ligne Freebox reste occupée
> après chaque appel — panne qu'on ne remarque qu'au moment où quelqu'un essaie d'appeler.
> Il se règle appareil en main, par essais successifs, et FreePBX n'y change rien.

---

## Le secours mobile

Deux montages possibles, et le choix ne se joue pas sur le prix.

### Option A — passerelle GSM matérielle (recommandée)

Un boîtier SIP-vers-GSM sur le LAN, déclaré comme un trunk PJSIP ordinaire, exactement
comme le pont FXO ci-dessus. Rien à compiler, rien à recompiler.

**Exigez « VoLTE » écrit sur la référence exacte**, pas sur la gamme. Les opérateurs
français éteignent la 2G et la 3G : une passerelle sans VoLTE est morte-née, et beaucoup de
boîtiers d'entrée de gamme le sont encore. La 5G, en revanche, est un faux critère — la
voix sur 5G native n'est pas déployée, les terminaux rebasculent en VoLTE pour téléphoner.

### Option B — module GSM dans la VM (chan-quectel)

*Connectivity → Trunks → Add Trunk → Add Custom Trunk*, avec pour chaîne de composition :

```
Quectel/quectel0/$OUTNUM$
```

`$OUTNUM$` est le marqueur FreePBX pour le numéro composé.

Les appels entrants du module ne passent pas par l'interface : il faut un contexte dans
`/etc/asterisk/extensions_custom.conf` qui les renvoie vers `from-pstn`.

> **Le piège de cette option sur FreePBX.** `chan_quectel` se compile à la main contre une
> version précise d'Asterisk. Sur le proof of concept, ce n'était pas gênant : *vous*
> décidiez quand Asterisk changeait. Ici, c'est la plateforme et `apt` qui décident — et
> après une mise à jour, un module compilé contre l'ancienne version ne se charge plus,
> **en silence**, noyé parmi les autres `declined to load`. Vous découvririez la panne le
> jour où Internet tombe, c'est-à-dire le seul jour où ce trunk servait.
>
> C'est la raison principale de préférer l'option A.

---

## Le failover sortant

C'est le point où FreePBX fait mieux et plus simplement que le dialplan écrit à la main.

*Connectivity → Outbound Routes → Add Outbound Route*

- **Route Name** : `sortant`
- **Trunk Sequence for Matched Routes** : `freebox-fxo`, puis le trunk GSM
- **Dial Patterns** : `0XXXXXXXXX` pour les numéros français à dix chiffres

FreePBX bascule tout seul sur le trunk suivant quand le premier rend `CHANUNAVAIL` ou
`CONGESTION`. Aucun dialplan à générer.

**Créez une route séparée pour les urgences**, placée **avant** la route générale :

- **Route Name** : `urgences`
- **Dial Patterns** : `15`, `17`, `18`, `112`, `114`, `115`, `119`, `196`, `197`
- **Trunk Sequence** : la Freebox d'abord — c'est une vraie ligne d'opérateur, avec
  acheminement et localisation des secours —, le GSM ensuite

> Le GSM en secours d'urgence n'est pas un détail de confort : un appel au 112 par la voie
> radio est localisé par l'opérateur mobile, sans configuration. Un trunk SIP à bas coût,
> lui, exige une adresse déclarée et tombe avec le lien qui le porte.
>
> Et un secours d'urgence **ne se teste pas** — appeler le 112 pour vérifier n'est pas
> acceptable. Vous ne sauriez donc jamais qu'il est cassé. Raison de plus pour que le
> chemin normal soit une ligne d'opérateur.

**Vérifiez aussi ce qui sort.** *Settings → Advanced Settings* et vos Dial Patterns doivent
interdire l'international et les numéros surtaxés si vous n'en avez pas l'usage : c'est le
scénario de fraude classique, où un PBX compromis passe des nuits entières à appeler des
numéros à revenu partagé. Voir
[proof-of-concept/docs/06-securite.md](../proof-of-concept/docs/06-securite.md#la-fraude-à-la-tonalité).

---

## Les appels entrants et le menu vocal

### Le groupe « toute la maison »

*Applications → Ring Groups*

- **Ring Group Number** : `199`
- **Extension List** : `100`, `101`, `102`, `103`, `104`
- **Ring Strategy** : `ringall`
- **Destination if no answer** : la messagerie du groupe

### Le menu à deux niveaux

*Applications → IVR*, deux menus.

**Niveau 1** — l'accueil : touche `1` vers le groupe `199`, touche `2` vers le second menu.

**Niveau 2** — « joindre une personne » : une touche par personne, chacune vers un Ring
Group ne contenant que ses postes.

> Le proof of concept régénérait l'annonce vocale par synthèse à chaque ajout de personne,
> pour qu'elle cite les prénoms. FreePBX ne le fait pas : **l'annonce est un fichier son
> que vous devez réenregistrer** quand la liste change. *Admin → System Recordings*.
> C'est le genre de chose qu'on oublie, et le menu propose alors une touche muette.

### La route entrante

*Connectivity → Inbound Routes* : une route, destination le menu de niveau 1.

---

## Messagerie vocale

Activée par extension, à la création. Pour l'envoi par courriel : *Settings → Voicemail
Admin → Settings*, avec l'adresse d'expédition et le serveur SMTP.

**Attention au réglage « supprimer après envoi »** : pratique pour ne pas remplir le
disque, mais si le courriel se perd, le message est perdu avec lui.

---

## Provisionner les ATA

C'est là que FreePBX coûte de l'argent, et autant le savoir avant.

L'**Endpoint Manager** de Sangoma provisionne plus de 300 modèles, Grandstream compris,
avec des gabarits maintenus par le fabricant du logiciel — exactement le travail de
correspondance des P-values qu'il a fallu faire à la main dans le proof of concept. C'est
un **module commercial**, autour de 199 $ pour une licence longue durée.

Trois façons de s'en passer :

1. **Configurer les cinq appareils à la main.** Vous ne le referez qu'en cas de retour aux
   réglages d'usine ou de changement de mot de passe. Pour cinq appareils, c'est
   défendable.
2. **Réutiliser le service de provisionnement du proof of concept.** Il est indépendant du
   PBX : il sert un fichier XML à partir d'une adresse MAC. Il faudrait lui faire lire les
   identifiants depuis FreePBX plutôt que depuis sa propre base — voir
   [proof-of-concept/docs/10-provisionnement.md](../proof-of-concept/docs/10-provisionnement.md).
3. **Acheter le module.** Si vous prévoyez d'ajouter des postes régulièrement, il se paie
   vite en temps gagné.

---

## Sortir du cadre : les fichiers `_custom.conf`

FreePBX possède `/etc/asterisk/`. Chaque fichier généré a un compagnon `_custom.conf` que
FreePBX inclut sans jamais le réécrire : `extensions_custom.conf`, `pjsip_custom.conf`,
`manager_custom.conf`, et ainsi de suite.

**C'est le seul endroit où écrire à la main.** Tout ce que vous mettriez ailleurs
disparaîtrait au prochain `fwconsole reload`, sans avertissement.

Pour se greffer sur le dialplan généré, FreePBX prévoit des contextes de raccrochement au
nommage imposé — `from-internal-custom`, `macro-dialout-trunk-predial-hook` et leurs
semblables. C'est de la connaissance FreePBX, pas de la connaissance Asterisk, et c'est le
principal coût d'apprentissage du passage.

---

## Ce qu'il faut vérifier une fois tout appliqué

```bash
sudo asterisk -rx "pjsip show endpoints"    # les cinq postes enregistrés
sudo asterisk -rx "pjsip show registrations"
sudo fwconsole reload
```

Puis, dans l'ordre : un appel interne, un appel sortant par la Freebox, un appel entrant
qui déclenche le menu, un dépôt de message vocal, et **le failover** — débranchez le HT813
et vérifiez que l'appel sortant repart par le GSM.

Le dernier est celui qu'on ne pense jamais à tester, et c'est le seul qui compte le jour où
il sert.
