# Interface de gestion

## Accès

L'interface n'écoute que sur `127.0.0.1:8080`. Elle ne s'atteint donc pas directement
depuis le réseau, ce qui est délibéré : c'est le seul composant qui détient les mots de
passe SIP en clair, et l'exposer sur un VLAN reviendrait à annuler le bénéfice d'avoir
supprimé l'AMI.

```bash
ssh -L 8080:127.0.0.1:8080 vous@10.0.5.20
# puis http://127.0.0.1:8080
```

Au premier accès, une page de création de compte s'affiche. Elle se ferme
**définitivement** dès qu'un compte existe : personne ne peut en créer un second par cette
voie. Pour ajouter ou réinitialiser un compte ensuite, voir
[05 — Exploitation](05-exploitation.md#réinitialiser-le-compte-dadministration).

## Le cycle de travail

Toutes les modifications s'accumulent en base sans toucher à Asterisk. Un bandeau orange
apparaît en haut de chaque page tant que la base et les fichiers en place divergent :

> Des modifications ne sont pas encore actives sur Asterisk. **Voir le diff et appliquer**

C'est volontaire. On peut préparer plusieurs changements — créer un poste, l'affecter à
une personne, ajouter un abrégé — puis relire l'ensemble d'un coup et le pousser en une
opération.

```
   Écrans de saisie          Écran « Appliquer »              Asterisk
   ────────────────          ───────────────────              ────────
   Postes                    contrôles préalables
   Personnes        ──▶      diff des trois fichiers   ──▶    écriture
   Groupes                   aperçu complet                   + reload
   Abrégés                   bouton de validation             + révision archivée
   Routes / Réglages
```

## Les écrans

### Tableau de bord

Quatre indicateurs en haut : Asterisk joignable ou non, nombre d'appels en cours, postes
enregistrés sur postes actifs, date de la dernière application.

En dessous, les **points de vigilance** — les mêmes contrôles que ceux de l'écran
Appliquer, affichés en permanence pour qu'un problème de configuration ne se découvre pas
au moment où on en a besoin.

Puis trois tableaux : l'état SIP de chaque poste, la liste « qui sonne quand » (groupes et
personnes avec leurs postes), et l'ordre des routes sortantes.

Un poste marqué **non enregistré** signifie qu'Asterisk ne sait pas où le joindre :
l'appareil est éteint, mal configuré, ou son mot de passe ne correspond plus. Un poste en
**état inconnu** signifie qu'Asterisk lui-même est injoignable.

### Postes

Un « poste » est tout ce qui s'enregistre en SIP : un port d'ATA, une base DECT,
l'application mobile, ou le pont FXO vers la Freebox.

| Champ | À quoi il sert |
|---|---|
| Identifiant SIP | Nom d'utilisateur SIP et nom de l'endpoint. **Non modifiable ensuite** — il apparaît dans les ATA, les journaux et les CDR. |
| Nom affiché | Ce que voient les autres postes en identification d'appelant. |
| Type | Détermine le gabarit PJSIP appliqué : `fxo` reçoit le gabarit trunk, tout le reste le gabarit interne. |
| Numéro interne | Ce qu'on compose pour le joindre. Vide pour un pont FXO. |
| Boîte vocale | Vide = pas de messagerie. Par défaut, égale au numéro interne, ce que suppose le code `*97`. |
| Codecs | Dans l'ordre de préférence. `alaw,ulaw` pour l'analogique, `opus,alaw,ulaw` pour le mobile. |
| Contacts simultanés | 1 pour un ATA. 2 ou plus pour un mobile enregistré depuis plusieurs appareils. |
| Mode de décroché | `hotline` pour un téléphone à cadran (voir plus bas). |

Le **mot de passe SIP** est généré à la création et se consulte en dépliant la colonne
correspondante. Le bouton *Régénérer* en produit un nouveau — le poste reste alors
injoignable tant que l'ATA n'a pas été mis à jour **et** la configuration appliquée.

#### Poste en mode hotline

Pour un téléphone à cadran, qui ne sait composer ni `*` ni `#` :

1. Passer le poste en mode **hotline**, avec `*9` comme numéro de hotline.
2. Côté ATA, régler « Offhook Auto-Dial » (ou équivalent) sur `*9`.

Au décroché, l'ATA compose `*9` tout seul, Asterisk répond par une annonce, et les
chiffres composés ensuite tombent sur les numéros abrégés et les postes internes.

### Personnes

Une personne regroupe plusieurs postes : l'appeler les fait tous sonner en même temps.
C'est la différence avec un poste, qui désigne un appareil unique.

- **Numéro personnel** — composable depuis toute la maison. Utilisez la plage `2xx` :
  les numéros à trois chiffres commençant par 1 entrent en conflit avec les urgences.
- **Touche du menu vocal** — la touche à composer dans le menu « joindre une personne »
  des appels entrants. Laissée vide, la personne n'y apparaît pas.
- **Courriel** — renseigné, les messages vocaux y sont envoyés en pièce jointe.

À la création, un PIN de messagerie est tiré au hasard et affiché **une seule fois** dans
le bandeau de confirmation. Notez-le. Le bouton *Nouveau PIN* en génère un autre.

Modifier la liste des personnes ayant une touche de menu déclenche, à l'application
suivante, la régénération de l'annonce vocale correspondante par Piper : personne à
enregistrer à la main.

### Groupes

Un groupe fait sonner plusieurs postes ensemble et bascule vers une boîte vocale de repli
si personne ne décroche.

Le groupe dont l'identifiant figure dans **Réglages → Groupe appelé par la touche 1** est
celui que joint le premier choix du menu des appels entrants — par défaut `famille`.

### Numéros abrégés

Un code court composé depuis un poste déclenche un appel externe en suivant l'ordre
habituel des routes. Sur un téléphone à cadran, c'est ce qui rend l'appareil réellement
utilisable.

Attention aux collisions : un abrégé `1` empêche de composer un numéro commençant par 1.
Le contrôle avant application signale tout numéro attribué deux fois.

### Routes sortantes

L'ordre d'essai des trunks, par priorité croissante.

Le **gabarit d'appel** doit contenir `{num}`, remplacé par le numéro composé :

| Cas | Gabarit |
|---|---|
| Pont FXO vers la Freebox | `PJSIP/{num}@grandstream-fxo` |
| Module GSM `chan-quectel` | `Quectel/quectel0/{num}` |
| Groupe de modules GSM | `Quectel/g1/{num}` |
| Trunk SIP d'un opérateur | `PJSIP/{num}@nom-du-trunk` |

Le **format des numéros** indique si la route veut du national (`0XXXXXXXXX`) ou de
l'E.164 (`+33XXXXXXXXX`). La conversion est faite dans le dialplan, et les numéros courts
en sont exclus.

### Réglages

Les valeurs qui alimentent le dialplan : langue, zone de tonalité, indicatif pays, durée
de sonnerie par défaut, sons du menu vocal, boîte vocale générale.

Deux réglages de sécurité méritent une lecture attentive :

- **Autoriser les appels internationaux** — désactivé par défaut. Un dialplan qui laisse
  passer le `00` est la porte d'entrée de la fraude à la tonalité.
- **Bloquer les numéros surtaxés** — actif par défaut, filtre les `089x` et les services
  `3xxx`.

Le bas de page rappelle la configuration du service (chemins, rechargement, API), qui vient
des variables d'environnement systemd et ne se modifie pas ici.

### Appliquer

Trois parties :

1. **Contrôles préalables.** Les *erreurs* bloquent l'application — numéro attribué deux
   fois, numéro d'urgence utilisé comme poste, gabarit de route sans `{num}`. Les
   *avertissements* informent sans bloquer.
2. **Le diff** entre les fichiers en place et ceux qui seraient écrits, au format unifié.
   C'est le moment de vérifier qu'on obtient bien ce qu'on croit.
3. **Le bouton de validation**, qui écrit les trois fichiers, lance
   `dialplan reload`, `pjsip reload` et `voicemail reload`, régénère l'annonce du menu, et
   archive une révision.

Ces rechargements ne coupent pas les appels en cours et ne déconnectent pas les postes
enregistrés.

### Révisions

L'historique des applications, avec le journal de chaque rechargement.

Le bouton *Revenir à cette version* réécrit les fichiers archivés et recharge Asterisk.
**Il ne touche pas à la base** : c'est un dépannage immédiat, pas une annulation. Après un
retour arrière, corrigez la donnée fautive dans l'interface, sinon la prochaine
application réintroduira le problème.

### Fichiers

L'écran qui complète les formulaires. Une installation a deux moitiés :
`generated/*.conf`, projetés depuis la base par les autres écrans, et les fichiers écrits
à la main — transports SIP, gabarits d'endpoint, contextes de dialplan. Cet écran édite la
seconde moitié, celle qui demandait jusqu'ici un accès SSH.

Les fichiers générés **n'y figurent pas**, et c'est délibéré : les y modifier serait sans
effet, le prochain « Appliquer » les réécrit sans prévenir. Pour un réglage propre à un
poste, le champ « Configuration PJSIP supplémentaire » de l'écran Postes est le bon
endroit — il vit dans la base, donc il survit à la régénération.

Trois choses se passent à chaque enregistrement :

1. **Un contrôle de forme** — en-têtes de section, lignes `option=valeur`, directives
   connues. Une faute de frappe est refusée sans que rien n'atteigne le disque. Ce n'est
   pas une validation Asterisk : il n'existe pas de mode « vérifier sans charger ».
2. **L'archivage du contenu précédent**, consultable et restaurable en bas de l'écran.
   Les vingt derniers états sont conservés par fichier.
3. **Le rechargement du module concerné**, puis la lecture des lignes que Asterisk vient
   d'écrire dans son journal. Si l'une d'elles se plaint de ce fichier, **la version
   précédente est remise en place et rechargée** — l'installation ne reste pas dans un
   état que personne n'a choisi. Le message d'Asterisk est affiché tel quel.

`modules.conf` fait exception : aucun rechargement à chaud n'existe pour lui, il faut
redémarrer Asterisk. L'écran le dit.

> Le service doit pouvoir écrire dans `/etc/asterisk` — c'est le `ReadWritePaths` de
> `telephonie-ui.service`. Réduire ce chemin à `/etc/asterisk/generated` désactive
> proprement cet écran : l'écriture échoue avec un message explicite, et le reste de
> l'interface continue de fonctionner.

### Journal d'appels

Les 100 derniers enregistrements CDR, en lecture seule. Utile pour répondre à « est-ce que
ça a sonné ? » : les appels sans réponse y figurent aussi.

### Journal d'administration

Les 200 dernières actions faites depuis l'interface, échecs de connexion compris.

## API JSON

Désactivée tant que `TELEPHONIE_API_TOKEN` est vide dans l'unité systemd. Une fois un
jeton défini, trois routes sont disponibles, authentifiées par l'en-tête `X-API-Token`.

```bash
# État général
curl -H "X-API-Token: $JETON" http://127.0.0.1:8080/api/status

# Appeler un numéro et lui jouer un message pré-enregistré
curl -H "X-API-Token: $JETON" -H 'Content-Type: application/json' \
     -d '{"number":"0600000000","sound":"alerte-eau"}' \
     http://127.0.0.1:8080/api/notify

# Click-to-call : faire sonner un poste, puis composer le numéro
curl -H "X-API-Token: $JETON" -H 'Content-Type: application/json' \
     -d '{"device":"salon","number":"0600000000"}' \
     http://127.0.0.1:8080/api/call
```

Le message de `/api/notify` doit exister dans `/var/lib/asterisk/sounds/custom/`, généré
au préalable :

```bash
sudo /opt/piper-voices/generate.sh "Fuite d'eau détectée au sous-sol." alerte-eau
```

Exemple d'intégration Home Assistant :

```yaml
rest_command:
  telephone_alerte:
    url: http://10.0.5.20:8080/api/notify
    method: POST
    headers:
      X-API-Token: !secret telephonie_token
    content_type: application/json
    payload: '{"number":"0600000000","sound":"{{ son }}"}'
```

Home Assistant tournant sur une autre machine, il faut alors faire écouter l'interface sur
l'IP du VLAN 5 (`--host 10.0.5.20` dans l'unité systemd) et ouvrir le port correspondant.
C'est un vrai élargissement de la surface exposée : à ne faire que si le besoin est réel,
et jamais sur le VLAN voix.
