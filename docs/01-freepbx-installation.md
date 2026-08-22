# Installation de FreePBX 17

> **Ce document n'a pas été exécuté sur une machine.** Contrairement à la documentation de
> `proof-of-concept/`, écrite en même temps que le code qu'elle décrit, celle-ci décrit une
> installation à faire. Les commandes viennent de la documentation officielle de Sangoma ;
> les chemins d'interface graphique peuvent différer de votre version. Corrigez ce fichier
> au fur et à mesure — c'est le seul moyen qu'il devienne fiable.

## Le système d'exploitation : Debian 12, pas Ubuntu

FreePBX 17 est développé et testé sur **Debian 12**. Il s'installe sur d'autres dérivés de
Debian, Ubuntu compris, mais vous seriez alors en configuration non supportée dès le
premier jour — et FreePBX 17 exige **PHP 8.2**, quand Ubuntu 24.04 livre PHP 8.3.

C'est le point qui décide : **repartez d'une VM Debian 12 vierge.** Si votre VM actuelle
est en Ubuntu 24.04 avec l'installation sur mesure, ne la convertissez pas — installez à
côté, migrez, puis éteignez l'ancienne. Vous gardez un chemin de retour tant que les deux
existent.

## Ressources

Pour une maison de cinq postes, FreePBX est très peu gourmand. Prévoyez large sur le
disque : les enregistrements d'appels et les sauvegardes s'accumulent.

| | Minimum confortable |
|---|---|
| vCPU | 2 |
| RAM | 2 Go |
| Disque | 20 Go |

## Installation

Sangoma publie un script d'installation pour Debian 12. La marche à suivre exacte et à
jour est dans leur documentation ; la reproduire ici garantirait qu'elle soit périmée dans
six mois :

**[How to Install FreePBX 17 on Debian 12](https://sangomakb.atlassian.net/wiki/spaces/FP/pages/10682545/How+to+Install+FreePBX+17+on+Debian+12)**

Grandes lignes : une Debian 12 minimale à jour, puis le script d'installation qui pose
Asterisk, PHP 8.2, MariaDB, Apache et les modules FreePBX. Comptez une trentaine de
minutes.

Deux choix à faire pendant l'installation :

- **La version d'Asterisk.** Le script en propose plusieurs. Prenez une version LTS plutôt
  que la plus récente : sur un système où c'est la plateforme qui décide des mises à jour,
  une LTS vous évite de suivre le rythme d'Asterisk.
- **Ne pas installer les modules commerciaux** au premier passage. Vous verrez plus tard
  si l'un d'eux est nécessaire — voir le provisionnement dans
  [02 — Configuration](02-freepbx-configuration.md#provisionner-les-ata).

## Premier accès

L'interface d'administration écoute sur le port 80/443 de la VM. Le premier accès demande
de créer le compte administrateur.

**Ne l'exposez pas.** C'est un panneau de contrôle complet du PBX, écrit en PHP, avec un
historique de vulnérabilités qui n'est pas anodin sur une machine reliée au réseau
téléphonique. Deux options :

- **Tunnel SSH**, comme pour l'interface du proof of concept :
  ```bash
  ssh -L 8443:127.0.0.1:443 vous@la-vm
  ```
- **Restreindre au VLAN d'administration** avec le module *Firewall* de FreePBX, en
  classant ce VLAN comme réseau de confiance et tout le reste comme non fiable.

Le second est plus commode au quotidien, le premier plus sûr. Choisissez en connaissance
de cause, et notez que le module Firewall de FreePBX gère aussi le pare-feu de la machine :
si vous posez des règles `ufw` à la main en parallèle, vous aurez deux sources de vérité
qui se contrediront.

## Ce qu'il faut régler tout de suite

**La langue et les sons.** *Settings → Asterisk SIP Settings*, et les paquets de sons
français via *Admin → Module Admin → Core Sound Packages*. Sans ça, le menu vocal parlera
anglais aux personnes qui appellent chez vous.

**Le fuseau et l'heure.** *Settings → Advanced Settings → System Timezone*. Les
enregistrements d'appels en dépendent, et une heure fausse rend un journal inexploitable.

**La zone de tonalité.** `fr`, pour que les combinés analogiques reconnaissent la tonalité
d'occupation. C'est le genre de réglage dont l'absence produit un symptôme incompréhensible :
le téléphone « ne raccroche pas tout seul ».

**Les sauvegardes.** Le module *Backup & Restore* est intégré et gratuit. Configurez-le
avant d'avoir quelque chose à perdre, pas après.

## Vérifier avant d'aller plus loin

```bash
sudo fwconsole ma list          # modules installés et leur version
sudo asterisk -rx "core show version"
sudo asterisk -rx "pjsip show transports"
```

`fwconsole` est l'outil en ligne de commande de FreePBX. Vous y reviendrez souvent :
`fwconsole reload` applique la configuration, `fwconsole restart` redémarre l'ensemble.

> **Ne modifiez jamais `/etc/asterisk/*.conf` à la main sur FreePBX.** Ces fichiers sont
> réécrits depuis la base MySQL à chaque `fwconsole reload`, et votre modification
> disparaîtra sans avertissement. Tout ce qui sort du cadre de l'interface passe par les
> fichiers `_custom.conf`, décrits dans
> [02 — Configuration](02-freepbx-configuration.md#sortir-du-cadre--les-fichiers-custom).

## Ensuite

[02 — Configuration](02-freepbx-configuration.md) : reproduire les postes, les trunks, le
failover et le menu vocal.
