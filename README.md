# Téléphonie maison

Installation téléphonique domestique : ATA analogiques dont un téléphone à cadran, base
DECT, application mobile, ligne Freebox par pont FXO, et un secours mobile qui survit à une
coupure Internet.

Le PBX retenu est **FreePBX 17**. Ce dépôt documente sa configuration pour ce matériel
précis, et l'intégration avec Home Assistant.

## Documentation

| Document | Contenu |
|---|---|
| [01 — Installation FreePBX](docs/01-freepbx-installation.md) | De la VM vierge à un FreePBX qui démarre |
| [02 — Configuration](docs/02-freepbx-configuration.md) | Postes, trunks, failover, menu vocal, messagerie, provisionnement |
| [03 — Home Assistant](docs/03-home-assistant.md) | Compte AMI, intégration HACS, entités et automatisations |

Le matériel et ses réglages restent décrits dans
[proof-of-concept/docs/08-materiel.md](proof-of-concept/docs/08-materiel.md) : ATA
Grandstream, base DECT, module GSM. Cette partie ne dépend pas du PBX.

## Plan de numérotation

Il ne dépend pas non plus du PBX, et c'est lui qu'il faut poser avant de configurer quoi
que ce soit.

```
15 17 18 112 114 115 119 196 197   Urgences — jamais filtrées, et interdites
                                    comme numéro interne

100  Salon                         Postes : un numéro par appareil
101  Étage
102  Garage
103  DECT
104  Mobile

199  Toute la maison               Groupe : fait sonner plusieurs postes
2xx  Personnes                     Font sonner tous les postes d'une personne
```

Les numéros à trois chiffres commençant par `1` sont **réservés**. Un poste interne qui
porterait `112`, `114`, `115` ou `119` rendrait ce numéro d'urgence injoignable depuis la
maison, et personne ne s'en apercevrait avant le jour où il servirait.

## `proof-of-concept/`

Une implémentation sur mesure a précédé ce choix : Asterisk 22 nu, une base SQLite comme
source de vérité, une interface web générant les fichiers de configuration, le
provisionnement des ATA Grandstream, et un pont d'événements vers Home Assistant.

Elle fonctionne, elle est testée (118 tests), et elle est **archivée** : FreePBX fait la
même chose sans code à maintenir. Elle reste dans le dépôt parce que sa documentation
explique le *pourquoi* de décisions qui restent vraies sous FreePBX — la fraude à la
tonalité, le contexte `[default]` vide, la détection du raccroché sur un port FXO, la
segmentation réseau.

À lire en particulier :

- [09 — Choix techniques](proof-of-concept/docs/09-choix-techniques.md) — ce qui a été
  décidé et écarté, y compris le passage à FreePBX
- [06 — Sécurité](proof-of-concept/docs/06-securite.md) — fraude téléphonique, pare-feu,
  fail2ban
- [07 — Dépannage](proof-of-concept/docs/07-depannage.md) — symptôme → cause → commande

Son `README.md` explique comment la faire tourner si le besoin revient.
