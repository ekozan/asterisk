# Choix techniques

Ce document explique les décisions structurantes, et ce qu'elles écartent. Il sert aussi
de journal des écarts par rapport à l'architecture initialement esquissée.

---

## 1. La base est la source de vérité, le dialplan en est dérivé

**Décision.** L'interface écrit dans SQLite, un générateur produit trois fichiers `.conf`,
et Asterisk les relit. Aucun appel ne consulte la base.

**L'alternative écartée : le dialplan interroge une API pendant l'appel** — via un script
AGI qui appelle `http://127.0.0.1:8000/users/by-digit/{digit}`, comme le prévoyait la
première version de l'architecture.

Ce qu'elle coûte :

- **Un point de panne supplémentaire sur le chemin de l'appel.** Si l'API est arrêtée,
  plantée ou en cours de mise à jour, le menu vocal des appels entrants ne fonctionne
  plus. Un service web se redémarre plusieurs fois par an ; une installation
  téléphonique, non.
- **De la latence à chaque appel** : démarrage d'un interpréteur Python, requête HTTP,
  requête SQL, le tout pendant que l'appelant écoute le silence.
- **Un diagnostic à deux étages.** `dialplan show` ne montre plus ce qui va réellement se
  passer, puisque la destination est calculée ailleurs. Il faut lire le code du script AGI
  en parallèle.

Ce que la génération apporte :

- `asterisk -rx "dialplan show menu-personne"` affiche la vérité, littéralement.
- L'écran **Appliquer** montre un diff avant d'agir — on voit ce qu'on change.
- Chaque application est archivée, donc réversible.
- L'interface peut être arrêtée sans conséquence sur la téléphonie.

**Ce qu'on perd** : un changement n'est plus actif instantanément, il faut passer par
« Appliquer ». Sur une installation domestique, où l'on ajoute un poste quelques fois par
an, c'est un non-sujet — et le temps de relecture du diff est du temps bien employé.

---

## 2. `asterisk -rx` plutôt que l'AMI

**Décision.** L'interface pilote Asterisk par le socket de contrôle local. `manager.conf`
désactive l'AMI.

**Pourquoi.** Le service tourne déjà sous l'utilisateur `asterisk` : il a accès au socket
sans identifiant supplémentaire. L'AMI apporterait, pour exactement les mêmes actions
(recharger, lister les endpoints, lancer un appel) :

- un port TCP de plus en écoute,
- un mot de passe de plus à générer, stocker, protéger et faire tourner,
- une section de `manager.conf` de plus à maintenir cohérente avec des ACL.

**Ce qu'on perd** : les événements en temps réel. L'AMI diffuse un flux d'événements
(décroché, raccroché, changement d'état) qu'une CLI ne sait pas donner. Un tableau de bord
temps réel en aurait besoin. Ici, l'état est rafraîchi au chargement de la page, ce qui
suffit largement.

Le fichier `manager.conf` documente la configuration minimale acceptable si un outil tiers
en exige un plus tard.

---

## 3. SQLite plutôt que PostgreSQL

**Décision.** Un fichier, colocalisé avec Asterisk.

**Pourquoi.** Le volume se compte en dizaines de lignes, l'écriture est mono-utilisateur,
et la donnée n'a de sens qu'à côté d'Asterisk. Pointer vers un PostgreSQL centralisé
réintroduirait précisément la dépendance réseau inter-VLAN que le reste de l'architecture
s'attache à éliminer : une base injoignable rendrait l'interface inutilisable, au moment
même — panne réseau — où l'on veut consulter la configuration.

Le mode WAL est activé, et la sauvegarde passe par `sqlite3 .backup` plutôt que par un
`cp`, qui pourrait capturer une base à moitié écrite.

---

## 4. Les fichiers générés sont isolés dans un sous-répertoire

**Décision.** `/etc/asterisk/generated/`, écrit par l'interface. `/etc/asterisk/*.conf`
reste hors de sa portée, en lecture seule pour le service
(`ReadWritePaths` de l'unité systemd).

**Pourquoi.** C'est ce qui permet de garder les réglages fins — transports, gabarits,
sécurité — écrits à la main et versionnés, sans qu'un outil ne les écrase. C'est le
reproche principal fait aux PBX à interface graphique : ils réécrivent tout, et le moindre
paramètre pointu se perd au premier enregistrement.

Accessoirement, une compromission de l'interface ne donne pas la main sur `pjsip.conf`.

---

## 5. Un seul contexte interne, plutôt qu'un par poste

**Décision.** Tous les postes entrent dans `[from-internal]`. Le menu de décroché du
téléphone à cadran y vit aussi, sur l'extension `*9`.

**L'alternative écartée** — un contexte par appareil (`from-ata-bas`, `from-ata-haut`,
`from-ata-garage`) — oblige à dupliquer chaque règle : les urgences, les numéros abrégés,
le filtrage de l'international, les codes de service. Trois copies qui divergent
lentement, et un correctif de sécurité à appliquer trois fois.

**Ce qu'on perd** : la possibilité de donner des droits différents selon le poste — par
exemple interdire les appels externes depuis un poste précis. Si le besoin apparaît, la
bonne réponse sera un contexte restreint qui `include => from-internal` en surchargeant
les motifs concernés, pas trois contextes parallèles.

---

## 6. Les numéros d'urgence sont générés et protégés

**Décision.** `15 17 18 112 114 115 119 196 197` sont écrits en tête du contexte interne,
exclus de tout filtrage et de toute réécriture, et l'interface refuse de les attribuer
comme numéro de poste.

**Pourquoi c'est un ajout et pas un détail.** Le plan de numérotation initial utilisait
`100`, `101`, `102` pour les postes. Rien n'empêchait d'attribuer plus tard `112` à un
nouveau poste — et de rendre le numéro d'urgence européen injoignable depuis toute la
maison, sans le moindre message d'erreur. C'est le genre de panne qu'on découvre au pire
moment possible.

C'est aussi la raison pour laquelle les personnes sont numérotées en `2xx` et non en `1xx`.

La réécriture E.164 des numéros courts est bloquée par la même occasion : transformer
`112` en `+33112` produirait un appel qui n'aboutit nulle part.

---

## 7. L'international et les numéros surtaxés sont bloqués par défaut

**Décision.** `_00.` et `_089XXXXXX` jouent un message d'erreur et raccrochent. Deux
réglages permettent de lever chaque blocage.

**Pourquoi par défaut.** La fraude à la tonalité est le risque numéro un d'un PBX, loin
devant la fuite de données. Le coût d'un blocage par défaut est un aller-retour dans les
réglages le jour où l'on veut appeler à l'étranger ; le coût de l'inverse est une facture.

---

## 8. Piper installé sur la VM, pas dans un conteneur

**Décision.** Un venv Python dans `/opt/piper`, appelé par un script shell.

**Pourquoi.** Les fichiers produits doivent atterrir dans `/var/lib/asterisk/sounds/custom`
sur cette machine précise. Passer par un service distant ajouterait un transfert de
fichiers et une dépendance réseau pour une opération qui se déclenche quelques fois par an.

La régénération de l'annonce du menu est déclenchée automatiquement à chaque application :
sans cela, ajouter quelqu'un le rendrait joignable mais muet — l'annonce ne citerait pas
son prénom.

---

## 9. Pas de dépendance JavaScript ni CDN

**Décision.** Pages rendues côté serveur, une feuille de style, aucun script.

**Pourquoi.** La VM n'a pas besoin d'accès Internet pour que l'interface s'affiche. Un CDN
injoignable — réseau coupé, justement le moment où l'on veut regarder la configuration —
donnerait une page cassée. Quatre dépendances Python au total, et rien à reconstruire.

---

## 10. Asterisk 22 (LTS) plutôt que 23

**Décision.** Branche 22.

**Pourquoi.** Quatre ans de support complet puis un an de correctifs de sécurité, contre
un cycle bien plus court pour la branche standard. Les apports de la 23 concernent surtout
WebRTC et DTLS, non utilisés ici. Pour une installation qui doit tourner des années sans
supervision, la stabilité prime.

---

## Écarts par rapport à l'architecture initiale

| Sujet | Version initiale | Ici | Motif |
|---|---|---|---|
| Menu « personne » | Script AGI appelant l'API | Dialplan généré | Supprime l'API du chemin de l'appel |
| AMI | Activé sur `127.0.0.1`, compte `api-user` | Désactivé | `asterisk -rx` suffit, un secret de moins |
| Port de l'interface | 8000 | 8080 | Évite la collision avec d'autres services courants |
| `allowguest` / `alwaysauthreject` | Repris dans `pjsip.conf` | Retirés | Options `chan_sip`, sans effet en PJSIP — voir [06 — Sécurité](06-securite.md) |
| Numéros d'urgence | Non traités | Générés et protégés | Collision possible avec le plan `1xx` |
| Numérotation des personnes | `200+`, chevauchant les postes | `2xx` distinct, validé | Un numéro attribué deux fois est silencieusement ambigu |
| PIN de messagerie | Valeurs fixes dans le fichier | Tirés au hasard, hors du dépôt | Les PIN ne sont plus versionnés |
| International | Blocage donné en exemple | Réglage actif par défaut | Anti-fraude |
| Sons français | Non mentionnés | Étape explicite de compilation | Sans eux, toutes les annonces sont en anglais |
| Détection du raccroché FXO | Listée comme « à valider » | Procédure de test documentée | Panne bloquante et facile à manquer |
| Contextes par ATA | Un par appareil | Un seul, `from-internal` | Évite trois copies divergentes des mêmes règles |

---

## Ce qui reste ouvert

- [ ] Vérifier la compatibilité de `chan-quectel` avec Asterisk 22 sur votre machine, et
      la revérifier après chaque mise à jour d'Asterisk.
- [ ] Calibrer le *Current Disconnect Threshold* du HT813 par le test décrit dans
      [08 — Matériel](08-materiel.md#détection-du-raccroché--le-réglage-le-plus-important).
- [ ] Confirmer le comportement `AT+QCFG="usbcfg"` du SIM7600G-H reçu.
- [ ] Choisir un forfait SIM pour le secours GSM, et tester une bascule réelle.
- [ ] Décider du sort du second port FXS du TA200 une fois le DECT en place.
- [ ] Builder et héberger l'image Flexisip, créer l'enregistrement DNS `sip.ffd.link`.
- [ ] Trancher sur le compte SIP Free (Freephonie) : s'il est utilisable, il devient une
      troisième route sortante, à insérer entre la Freebox et le GSM.
- [ ] Prévoir un onduleur si la téléphonie doit survivre à une coupure de courant.
