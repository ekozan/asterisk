# Sécurité

## Ce qui est exposé, et à qui

| Service | Écoute sur | Joignable depuis | Authentification |
|---|---|---|---|
| SIP (PJSIP) | `10.0.90.20:5060/udp` | VLAN 90 uniquement | mot de passe par endpoint |
| RTP | `10000-10200/udp` | VLAN 90 uniquement | flux négocié, `strictrtp` |
| Interface de gestion | `127.0.0.1:8080` | tunnel SSH seulement | compte + session |
| SSH | `10.0.5.20:22` | VLAN 5 | clé uniquement |
| AMI | `127.0.0.1:5038` | la VM elle-même | compte en **lecture seule**, ACL `127.0.0.1/32` |
| Provisionnement | `127.0.0.1:8081` par défaut | VLAN 90 si ouvert | aucune — voir [10](10-provisionnement.md#sécurité) |

**Rien n'est exposé sur le WAN.** Le seul composant en frontal public est Flexisip, conçu
pour ça, qui relaie la signalisation de l'application mobile vers Asterisk en interne.
Asterisk lui-même n'est jamais joignable depuis Internet.

## La fraude à la tonalité

C'est le risque principal d'un PBX, très au-dessus de la fuite de données : un tiers qui
obtient le droit de passer un appel sortant compose des numéros surtaxés à l'étranger, et
la facture arrive. Cinq protections se cumulent ici.

**1. Aucun appel anonyme accepté.** En PJSIP, un appel non authentifié est rejeté tant
qu'aucun endpoint nommé `anonymous` n'existe. N'en créez jamais.

> Les options `allowguest=no` et `alwaysauthreject=yes` que l'on croise dans beaucoup de
> tutoriels appartiennent à `chan_sip`, retiré depuis Asterisk 21. Les écrire dans
> `pjsip.conf` ne produit **ni erreur, ni protection** — c'est le piège le plus courant sur
> une installation récente.

**2. Le contexte `[default]` est vide.** Un canal qui y atterrit par erreur de
configuration ne peut rien composer. La configuration d'exemple installée par
`make samples` fait l'inverse.

**3. Le transfert est interdit sur les trunks.** `allow_transfer=no` sur le gabarit
`endpoint-trunk` bloque le scénario où un appel entrant est renvoyé vers un numéro surtaxé.

**4. L'international est bloqué par défaut.** Le motif `_00.` joue un message d'erreur et
raccroche. À n'ouvrir, dans les réglages, que si le besoin est réel.

**5. Les numéros surtaxés sont filtrés.** Les `089x` et les services `3xxx` sont refusés.

Vérification :

```bash
sudo asterisk -rx "dialplan show from-internal" | grep -E '_00\.|_089|_3XXX'
```

## Mots de passe

**Mots de passe SIP.** Générés à la création, 24 caractères, alphabet sans caractère
ambigu ni caractère spécial — les interfaces web des ATA les digèrent mal. Ils sont
stockés **en clair** dans la base et dans `pjsip_endpoints.conf`, ce qui est inévitable :
l'authentification SIP `userpass` exige qu'Asterisk connaisse le secret. La protection
repose donc sur les droits d'accès :

```bash
sudo ls -l /var/lib/telephonie/telephonie.db      # 0640 asterisk:asterisk
sudo ls -ld /var/lib/telephonie                   # 0750
sudo ls -l /etc/asterisk/generated/               # 0640 asterisk:asterisk
```

Toute sauvegarde de ces fichiers hérite du même niveau de sensibilité.

**Compte de l'interface.** Haché en scrypt (paramètres `n=16384, r=8, p=1`, sel de
16 octets), sans dépendance externe. Dix caractères minimum imposés à la création.

**PIN de messagerie.** Tirés au hasard, jamais laissés à une valeur partagée. Les valeurs
triviales (`0000`, `1234`, `1111`…) sont explicitement exclues, y compris du calcul
déterministe utilisé pour les boîtes de postes et de groupes. Chacun change le sien au
premier accès, et le code réellement en vigueur ne transite alors plus jamais par
l'interface.

## Protections de l'interface web

- **Sessions** en base, jeton de 32 octets, expiration à 12 heures, purge au démarrage.
- **Cookie** `HttpOnly` et `SameSite=Lax`. Passez `TELEPHONIE_COOKIE_SECURE=1` si vous la
  placez derrière un reverse proxy HTTPS.
- **CSRF** : jeton par session, vérifié sur chaque formulaire, comparé en temps constant.
- **Journal d'audit** de toutes les actions, échecs de connexion compris.
- **API JSON** désactivée tant qu'aucun jeton n'est défini ; le jeton est comparé en temps
  constant, et les paramètres sont validés par expression régulière stricte avant d'être
  passés à Asterisk.

## Durcissement des services

L'unité `telephonie-ui.service` restreint le service au strict nécessaire :

```ini
ProtectSystem=strict
ReadWritePaths=/var/lib/telephonie /etc/asterisk /var/lib/asterisk/sounds/custom
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
```

Trois répertoires en écriture, le reste du système en lecture seule. Asterisk tourne sous
son propre utilisateur, jamais en `root`.

## Édition des fichiers depuis l'interface

L'écran **Fichiers** écrit dans `/etc/asterisk`, et ça mérite d'être dit franchement :
**c'est la porte la plus large de l'installation.**

Le dialplan d'Asterisk sait exécuter des commandes système (`System()`, `AGI()`). Qui peut
écrire `extensions.conf` peut donc exécuter du code sous l'identité `asterisk`. Une session
d'interface détournée ne se limite plus à une mauvaise configuration téléphonique : elle
vaut un accès shell sur la VM.

C'est un élargissement **volontaire** de `ReadWritePaths`, pas un oubli. Il se défend
parce que les autres barrières tiennent :

- l'interface n'écoute que sur `127.0.0.1`, jamais sur le réseau ;
- on ne l'atteint que par un tunnel SSH, donc après authentification SSH ;
- il faut ensuite un compte administrateur de l'interface ;
- chaque écriture est journalisée, et le contenu précédent archivé.

Autrement dit, l'attaquant qui peut s'en servir a déjà franchi SSH. Mais si vous exposez
un jour l'interface autrement — reverse proxy, VPN partagé, port ouvert —, cette porte
devient le premier point à reconsidérer.

**Pour la refermer**, retirez `/etc/asterisk` du `ReadWritePaths` de
`telephonie-ui.service` et laissez `/etc/asterisk/generated`. L'écran reste visible mais
toute écriture échoue avec un message explicite ; le reste de l'interface est intact, et
les fichiers redeviennent modifiables en SSH uniquement.

## Segmentation réseau

| Source | Destination | Ports | Règle |
|---|---|---|---|
| VLAN 90 (postes SIP) | VM, vNIC VLAN 90 | 5060/udp, 10000-10200/udp | autorisé |
| VLAN 5 | VM, vNIC VLAN 5 | 22/tcp | autorisé |
| VM | VLAN d'administration | — | **interdit** |
| WAN | VM | — | **interdit** |
| VM | Internet | DNS, NTP, SIP sortant | autorisé, sortant seulement |

Le VLAN 90 ne contient que des appareils SIP : sa surface d'attaque est réduite, et une
compromission n'y ouvre pas d'autre chemin vers la VM. L'interface et l'AMI ne sortent pas
du tout sur le réseau, ce qui supprime les règles inter-VLAN qu'une architecture avec API
distante imposerait.

## fail2ban

La prison `asterisk` lit `/var/log/asterisk/messages`, où le `logger.conf` de ce dépôt
envoie bien les `NOTICE` — le niveau des échecs d'authentification SIP. Une prison pointée
sur un fichier qui ne les contient pas donne une fausse impression de protection.

```bash
sudo fail2ban-client status asterisk
sudo grep -c "failed for" /var/log/asterisk/messages
```

Sur un VLAN voix fermé, elle ne devrait quasiment jamais se déclencher. Si elle bannit
régulièrement, c'est le signe qu'un appareil est mal configuré — ou que quelque chose
atteint le port SIP qui ne le devrait pas.

## Les numéros d'urgence

Ils sont générés en tête du contexte interne, exclus de tout filtrage et de toute
réécriture de numéro, et l'interface **refuse** de les attribuer comme numéro de poste.

Cela dit, il faut être lucide sur ce qu'une installation domestique garantit :

- Coupure de courant : la VM, l'hyperviseur, les ATA et le réseau s'arrêtent. Sans
  onduleur, il n'y a plus de téléphone.
- Panne Internet : la ligne fixe tombe, le secours GSM prend le relais — à condition que
  le forfait SIM soit actif et le module fonctionnel.
- Localisation : un appel passé par le GSM présente le numéro de la SIM, pas l'adresse du
  domicile.

**Gardez toujours un téléphone mobile chargé comme véritable moyen d'appel d'urgence.**
Un PBX maison est un confort, pas un service de sécurité.
