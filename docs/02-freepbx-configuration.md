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

### Les importer plutôt que les saisir

FreePBX importe les **extensions** par CSV, avec le module *Bulk Handler* — gratuit et
installé d'office. Les trunks, routes sortantes et menus vocaux, eux, ne s'importent pas :
ils restent à créer dans l'interface. Pour cinq postes contre trois trunks et deux menus,
c'est bien la partie répétitive qui est automatisable.

Un script produit ce CSV depuis la base du proof of concept, mots de passe SIP compris :

```bash
scripts/export-freepbx-extensions.py /var/lib/telephonie/telephonie.db
```

**Mais commencez par lui donner le bon en-tête.** Les colonnes attendues varient selon la
version du module ; un en-tête approximatif produit un import qui échoue, ou pire, qui
réussit en ignorant en silence ce qu'il ne reconnaît pas — le même mode de panne que les
P-values Grandstream. La parade est la même : demander à la machine.

1. Créez **un** poste à la main dans FreePBX.
2. *Admin → Bulk Handler → Export → Extensions*. Vous obtenez un CSV.
3. Relancez le script avec ce fichier :

```bash
scripts/export-freepbx-extensions.py /var/lib/telephonie/telephonie.db \
    --template export-freepbx.csv -o extensions.csv
```

Il adopte alors exactement vos colonnes, remplit celles qu'il sait remplir, et vous dit
lesquelles il a laissées vides. Puis *Bulk Handler → Import*.

Deux choses que le script fait délibérément :

- **il exclut les ponts FXO** — dans FreePBX ce sont des trunks, et les importer comme
  extensions créerait un poste fantôme qui répondrait aux appels internes ;
- **il reprend les mots de passe SIP tels quels**, donc vos ATA n'ont pas à être
  reconfigurés de ce côté-là. Seul l'identifiant change, voir l'encadré plus bas.

### Ou les créer à la main

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

> **`SIP Server Address` reste vide, et FreePBX vous le dira** : *« This is ignored when
> Registration is set to Receive »*. C'est normal, et c'est même la confirmation que vous
> êtes dans le bon mode — en `Receive`, FreePBX apprend l'adresse du HT813 au moment où
> celui-ci s'enregistre, il n'a donc rien à connaître d'avance.
>
> **À ne pas confondre avec le champ *SIP Server* du HT813**, plus bas : deux champs de nom
> voisin, sur deux appareils différents. Celui de l'appareil doit contenir l'adresse de la
> VM FreePBX, et lui n'est pas facultatif. Le sens de la flèche est le même des deux côtés —
> l'appareil va vers le serveur — mais chacun n'a besoin que de sa moitié de l'information.

**Une alternative, si votre HT813 a une adresse fixe** : se passer d'enregistrement. Mettez
*Registration* sur `None`, *Authentication* sur `Outbound`, et renseignez l'adresse de
l'appareil dans *Match (Permit)*. FreePBX identifie alors le trunk par son IP. C'est plus
robuste — aucun enregistrement à expirer, aucun AOR à devenir obsolète — mais ça suppose
que l'adresse ne bouge jamais. Avec du DHCP, restez en `Receive`.

### Côté HT813

Le HT813 a deux ports : un **FXS** (pour brancher un téléphone) et un **FXO** (pour se
brancher *sur* une ligne). C'est le FXO qui nous intéresse — il se comporte comme un
téléphone décroché sur la prise de la Freebox.

Interface web de l'appareil, section *FXO Port* :

| Réglage | Valeur |
|---|---|
| **SIP Server** | l'adresse de la VM FreePBX |
| **SIP User ID** | le *Username* du trunk, ex. `freebox-fxo` |
| **Authenticate ID** | le même |
| **Authenticate Password** | le *Secret* du trunk |
| **Preferred Vocoder** | `PCMA`, puis `PCMU` |

> **Bonne nouvelle par rapport aux postes : un trunk garde un nom.** L'obligation d'utiliser
> un numéro d'extension ne vaut que pour les extensions. `freebox-fxo` reste donc valable
> tel quel, et le HT813 n'a pas à être renommé lors de la migration — contrairement aux ATA
> des postes.

**Appels entrants**, toujours dans *FXO Port* :

| Réglage | Valeur |
|---|---|
| **Number of Rings Before Pickup** | `1` — décrocher dès la première sonnerie |
| **Wait for Dial Tone** | `No` |
| **Unconditional Call Forward to VOIP → User ID** | `s` |

Le `s` arrive dans le contexte `from-pstn` sans numéro appelé. Une *Inbound Route* dont le
champ **DID Number** est laissé **vide** capte tout ce qui n'a pas de destination
explicite : c'est elle qui enverra l'appel vers votre menu vocal.

Si vous préférez quelque chose de plus lisible — utile le jour où un second trunk arrive —
mettez votre numéro de ligne fixe dans *User ID* plutôt que `s`, et créez une Inbound Route
sur ce DID précis.

### La détection du raccroché — le réglage qui compte

Sans détection fiable, le HT813 garde la ligne décrochée après que le correspondant a
raccroché : **la ligne Freebox reste occupée, et plus aucun appel ne passe** jusqu'au
redémarrage de l'appareil. Le symptôme se découvre toujours au mauvais moment.

| Réglage | Valeur |
|---|---|
| **Enable Current Disconnect** | `Yes` |
| **Current Disconnect Threshold** | commencer à `200 ms`, ajuster entre 100 et 400 |
| **Enable Call Progress Tones (Busy Tone) Disconnect** | `Yes`, en filet de sécurité |

Le test à faire explicitement, et à refaire après chaque changement de seuil : appelez un
mobile depuis un poste, raccrochez **côté mobile**, puis vérifiez que la ligne se libère.

```bash
sudo asterisk -rx "core show channels"   # aucun canal ne doit subsister
```

Si un canal persiste, montez le seuil par paliers de 50 ms. Ce réglage se fait appareil en
main, par essais successifs, et FreePBX n'y change rien.

### Deux réglages à ne pas oublier

**Le port FXS du HT813 est libre.** Vous pouvez y brancher un téléphone de plus : il devient
alors une extension ordinaire, à créer comme les autres, avec le numéro comme identifiant
SIP. Ou le laisser inutilisé.

**Coupez la mise à jour automatique du firmware** — *Maintenance → Upgrade and Provisioning*,
*Automatic Upgrade* sur `No`, et videz le chemin de serveur de provisionnement s'il pointe
encore vers celui du proof of concept. Deux sources de configuration sur un même appareil,
c'est une reconfiguration silencieuse un matin, et une ligne fixe muette sans explication.

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

## Les numéros abrégés

Composer `5` pour appeler Mamie. Deux écrans, sans une ligne de dialplan.

1. *Applications → Misc Destinations* : un nom (`Mamie`) et le numéro complet
   (`0102030405`). Ça crée une destination utilisable ailleurs dans FreePBX.
2. *Applications → Misc Applications* : le code composé (`5`) vers cette destination.

L'appel repart par vos routes sortantes habituelles, donc avec le failover et les filtres.

> Si vous observez une seconde d'attente avant que ça compose, c'est qu'Asterisk hésite
> entre `5` et un motif plus long qui commencerait par 5. Passez à un code à deux chiffres,
> ou préfixez d'une étoile — `*5`, qui ne peut être le début d'aucun numéro.

### Le téléphone à cadran

Un cadran n'émet pas de DTMF : il envoie des impulsions. Si votre ATA fait la conversion,
les numéros abrégés fonctionnent parfaitement, et c'est de loin la façon la plus simple
d'appeler quelqu'un depuis ce poste.

**Mais vérifiez ce que la conversion couvre exactement.** Sur beaucoup d'ATA, la détection
d'impulsions n'est active que **pendant la numérotation**, avant l'établissement de
l'appel. Une fois en communication, tourner le cadran ne produit alors plus rien
d'exploitable.

La différence compte dès qu'un menu vocal est en jeu :

| Usage | Nécessite | Fonctionne avec une conversion « à la numérotation seule » |
|---|---|---|
| Composer `5`, ou `101` | numérotation | oui |
| Naviguer dans un menu vocal (« tapez 1 ») | DTMF en cours d'appel | **non** |
| Consulter sa messagerie (`*97` puis un code) | DTMF en cours d'appel | **non** |

Le test prend une minute : appelez `*43` (test d'écho) depuis le cadran, puis, une fois en
ligne, composez un chiffre. Si Asterisk ne le voit pas — `asterisk -rvvv` l'affichera —,
c'est que la conversion s'arrête à l'établissement de l'appel.

Ça n'affecte pas le menu vocal des appels **entrants**, destiné aux gens qui vous appellent
depuis leur propre téléphone. Ça affecte en revanche tout menu que vous voudriez proposer
*au* poste à cadran — et c'est là, et seulement là, que la commande vocale reprend
l'avantage sur un numéro abrégé.

---

## Messagerie vocale

Activée par extension, à la création. Pour l'envoi par courriel : *Settings → Voicemail
Admin → Settings*, avec l'adresse d'expédition et le serveur SMTP.

**Attention au réglage « supprimer après envoi »** : pratique pour ne pas remplir le
disque, mais si le courriel se perd, le message est perdu avec lui.

---

## Un poste IP Cisco 8851

La question se pose dès qu'on croise un CP-8851 d'occasion : c'est un très bon téléphone,
vendu une fraction de son prix neuf. La réponse dépend entièrement du **micrologiciel**, et
la référence imprimée sur le carton la donne en grande partie.

| Référence | Micrologiciel d'origine | Avec FreePBX |
|---|---|---|
| `CP-8851-3PCC-K9` | Multiplatform (MPP / 3PCC) | Fonctionne, c'est un poste SIP ordinaire |
| `CP-8851-K9` | Entreprise (pour CUCM) | **Ne s'enregistre pas** tant qu'il n'est pas converti |

Le `-K9` ne désigne pas le micrologiciel — il désigne la cryptographie forte, et on le
trouve sur les deux. C'est l'absence de `3PCC` qui indique un poste Entreprise.

### Vérifier ce qui tourne réellement

Un poste d'occasion a pu être converti par son propriétaire précédent, dans un sens comme
dans l'autre. Seul le téléphone dit la vérité :

    Applications (⚙) → Status → Product information

Le champ *Software version* tranche : un nom contenant `3PCC` ou `MPP` (par exemple
`sip88xx.12-0-7MPP…`) est un poste utilisable ; un nom en `…SIP…` seul est un poste
Entreprise.

Si le poste sort d'une autre installation, remettez-le d'usine avant tout essai : il garde
sinon l'adresse de son ancien serveur d'appels. Débranchez l'alimentation, rebranchez en
maintenant `#`, puis composez `123456789*0#`.

### Si c'est un poste MPP

Il se configure comme n'importe quel poste SIP, par son interface web :
`http://<ip-du-poste>/admin/advanced`, onglet *Voice → Ext 1*.

| Champ | Valeur |
|---|---|
| Line Enable | Yes |
| Proxy | l'adresse du FreePBX |
| Register | Yes |
| User ID | le numéro d'extension, `105` par exemple |
| Password | le secret SIP du poste |
| Auth ID | le même numéro d'extension |
| Display Name | le libellé du poste |

Créez d'abord l'extension côté FreePBX, en `pjsip`, exactement comme les autres. Rien de
particulier n'est à prévoir côté PBX : pour Asterisk, c'est un téléphone SIP de plus.

L'Endpoint Manager sait provisionner ces modèles, mais pour un seul poste la saisie à la
main est plus rapide que la mise en place du provisionnement.

### Si c'est un poste Entreprise

La conversion vers MPP est possible mais **payante et administrative** : elle passe par le
*Cloud Upgrader* de Cisco et exige une licence de migration par téléphone, commandée chez
un partenaire ou un distributeur Cisco. Il n'existe pas de micrologiciel MPP à télécharger
librement pour contourner l'étape.

Pour une maison, c'est le point qui décide : le coût de la licence et le temps passé
dépassent le prix d'un poste SIP neuf d'entrée de gamme, alors que le PBX ne verra aucune
différence entre les deux.

Deux réserves supplémentaires, même en cas de conversion réussie :

- La note de terrain Cisco **FN74296** signale que certaines versions matérielles des 8811,
  8841, 8851, 8851NR et 8861 fabriquées entre 2013 et 2019 environ montrent des
  performances dégradées sous micrologiciel MPP. Il n'existe pas de contournement.
- Cisco a annoncé la **fin du support de migration** de ces séries. La fenêtre pour
  convertir un poste ne s'élargira pas.

### L'alimentation

Le 8851 est un poste PoE de classe 3 et **l'alimentation secteur n'est pas fournie**. Sans
commutateur PoE, il faut un injecteur ou le bloc secteur Cisco correspondant, à compter en
plus du prix du téléphone.

---

## Un poste IP Fanvil V64

C'est le cas inverse du Cisco : un poste SIP ouvert, sans micrologiciel à débloquer ni
licence à acheter. Il s'enregistre sur FreePBX comme n'importe quel autre poste `pjsip`.

Écran couleur 3,5″ (480×320), 12 comptes SIP, 7 touches de ligne et une touche de page
donnant 21 touches programmables, deux ports gigabit, PoE, Wi-Fi et Bluetooth intégrés,
prise casque EHS. Pour une maison, c'est très au-delà du nécessaire — mais rien de tout
cela ne complique la mise en service.

### La configuration à la main

Créez l'extension côté FreePBX, en `pjsip`, comme les autres. Puis sur le téléphone,
`http://<ip-du-poste>/`, onglet *Line → SIP* :

| Champ | Valeur |
|---|---|
| Server Address | l'adresse du FreePBX |
| Server Port | `5060` |
| Username | le numéro d'extension, `105` par exemple |
| Authentication User | le même numéro |
| Authentication Password | le secret SIP du poste |
| Display Name | le libellé du poste |
| Activate | coché |

**Changez le mot de passe d'administration du téléphone** avant de le laisser sur le
réseau : Fanvil livre ses postes avec un couple par défaut connu de tout le monde, et
l'interface web du téléphone donne accès au secret SIP de l'extension.

### Le provisionnement, cette fois sans acheter de module

Contrairement aux ATA Grandstream, le provisionnement Fanvil ne demande rien de
particulier : le téléphone télécharge en HTTP un fichier nommé d'après sa propre adresse
MAC, `0C383E5F1A63.cfg`, et l'URL accepte la variable `$mac` — `http://serveur/$mac.cfg`.
On la lui donne soit dans son interface, soit par l'**option DHCP 66**.

C'est exactement la forme que sert déjà le service de provisionnement du proof of concept,
décrit dans
[proof-of-concept/docs/10-provisionnement.md](../proof-of-concept/docs/10-provisionnement.md) :
un fichier par adresse MAC, servi par HTTP. Il y a donc là une troisième voie, entre la
saisie à la main et les 199 $ de l'Endpoint Manager.

### Le port PC et la segmentation réseau

Les deux ports gigabit servent à chaîner un ordinateur derrière le téléphone, avec un seul
câble jusqu'au bureau. **C'est un pont entre deux réseaux.** Si vous avez séparé la
téléphonie du reste — la recommandation de
[proof-of-concept/docs/06-securite.md](../proof-of-concept/docs/06-securite.md) — brancher
un ordinateur sur ce port le place dans le VLAN téléphonie, et annule la séparation, sauf à
configurer l'étiquetage VLAN du poste (*Network → Advanced*, VLAN voix et VLAN données
distincts).

Le plus simple, pour une maison : ne rien brancher sur le port PC.

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
