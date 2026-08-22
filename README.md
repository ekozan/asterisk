# Téléphonie maison — Asterisk 22 documenté et pilotable

Installation téléphonique domestique sur Asterisk 22 (VM Ubuntu 24.04), avec une
interface web pour gérer les postes, les personnes et les routes sortantes sans
jamais éditer un fichier `.conf` à la main.

## Ce que ça fait

- **Postes de la maison** : ATA analogiques (dont un téléphone à cadran), base DECT,
  application mobile — tous décrits dans une base, pas dans un fichier.
- **Deux routes sortantes en cascade** : la ligne Freebox via un pont FXO (gratuite),
  et un module GSM 4G en secours qui survit à une coupure Internet complète.
- **Menu vocal à deux niveaux** sur les appels entrants : « toute la maison » ou
  « une personne en particulier », la liste des personnes se régénérant toute seule.
- **Messagerie vocale** par poste et par personne, avec envoi par courriel.
- **Interface web** : ajout d'un poste, d'une personne, d'un numéro abrégé, aperçu du
  diff avant application, historique des configurations et retour arrière.
- **Provisionnement des ATA Grandstream** : l'appareil récupère son compte SIP tout seul
  au démarrage, à partir de son adresse MAC — plus rien à recopier dans son interface web.
- **Édition des fichiers à la main, depuis l'interface** : les `.conf` que les formulaires
  ne couvrent pas s'éditent dans le navigateur, avec historique et retour arrière
  automatique si Asterisk refuse la nouvelle version.
- **Prêt pour Home Assistant** : compte AMI restreint, créé à l'installation pour la
  seule adresse de votre Home Assistant, à l'usage de l'intégration HACS qui suit l'état
  des postes et l'identité des appelants.

## Le principe à retenir

La base SQLite est la **source de vérité**. L'interface écrit dedans, puis *génère* les
fichiers de configuration Asterisk et recharge le service.

```
  Interface web ──écrit──▶ SQLite ──génère──▶ /etc/asterisk/generated/*.conf
                                                        │
                                                 asterisk -rx "…reload"
                                                        ▼
                                                    Asterisk
```

**Asterisk ne dépend jamais de l'interface pour traiter un appel.** Le dialplan produit
est du dialplan pur : si le service web est arrêté, en panne ou en cours de mise à jour,
le téléphone continue de sonner exactement pareil. C'est la principale différence avec
une approche où le dialplan interroge une API en AGI pendant l'appel.

## Démarrage rapide

Sur une VM Ubuntu 24.04 **vierge**, un seul script fait tout : paquets,
compilation d'Asterisk 22 avec les sons français, chan-quectel, Piper, interface,
configuration, durcissement.

```bash
git clone https://github.com/ekozan/asterisk /root/telephonie && cd /root/telephonie

# Voir ce qui serait fait, sans rien modifier
sudo ./scripts/bootstrap.sh --dry-run --with-gsm

# Puis pour de vrai
sudo ./scripts/bootstrap.sh --with-gsm --voip-iface ens192 \
     --with-hardening --admin-net 10.0.5.0/24 --voip-net 10.0.90.0/24
```

Comptez 20 à 30 minutes, dont l'essentiel en compilation. Le script est
**reprenable** : si quelque chose casse en route, relancez la même commande, il
repart de l'étape interrompue. `--help` liste toutes les options.

Si Asterisk est déjà installé et que vous voulez seulement l'interface :

```bash
sudo ./scripts/install.sh --with-asterisk-conf
sudo -u asterisk /opt/telephonie/venv/bin/python /opt/telephonie/scripts/seed.py
```

L'interface n'écoute que sur la boucle locale. On y accède par un tunnel SSH :

```bash
ssh -L 8080:127.0.0.1:8080 vous@la-vm
# puis http://127.0.0.1:8080 — le premier accès demande de créer le compte admin
```

## Documentation

| Document | Contenu |
|---|---|
| [01 — Architecture](docs/01-architecture.md) | Schéma d'ensemble, matériel, plan de numérotation |
| [02 — Installation](docs/02-installation.md) | De la VM vierge à l'installation complète, pas à pas |
| [03 — Configuration Asterisk](docs/03-configuration-asterisk.md) | Anatomie des fichiers, statique contre généré |
| [04 — Interface de gestion](docs/04-interface.md) | Chaque écran, et ce qu'il produit |
| [05 — Exploitation](docs/05-exploitation.md) | Gestes courants : ajouter un poste, sauvegarder, mettre à jour |
| [06 — Sécurité](docs/06-securite.md) | Surface exposée, fraude téléphonique, pare-feu, fail2ban |
| [07 — Dépannage](docs/07-depannage.md) | Symptôme → cause → commande |
| [08 — Matériel](docs/08-materiel.md) | Réglages des ATA, du DECT, du module GSM |
| [09 — Choix techniques](docs/09-choix-techniques.md) | Décisions prises, et ce qu'elles écartent |
| [10 — Provisionnement](docs/10-provisionnement.md) | Configuration automatique des ATA Grandstream |
| [11 — Home Assistant](docs/11-home-assistant.md) | Compte AMI pour l'intégration HACS |

## Arborescence

```
app/              interface web (FastAPI) et générateur de configuration
  confgen/        gabarits Jinja2 des fichiers Asterisk
  templates/      pages HTML
asterisk/         fichiers de configuration statiques, édités à la main
docs/             documentation
scripts/          bootstrap (install complète), install (UI seule), seed, TTS, sauvegarde
systemd/          unités des services (interface, provisionnement)
tests/            118 tests couvrant le générateur, l'interface et l'éditeur
```

## Tests

```bash
python3 -m venv .venv && ./.venv/bin/pip install -r requirements.txt pytest httpx
./.venv/bin/python -m pytest tests/ -q
```

Les tests tournent sans Asterisk installé : le rechargement et la synthèse vocale sont
désactivés par variables d'environnement dans `tests/conftest.py`.

## Développement

```bash
mkdir -p var/data var/etc/generated
TELEPHONIE_DATA_DIR=$PWD/var/data TELEPHONIE_DB=$PWD/var/data/dev.db \
TELEPHONIE_ASTERISK_DIR=$PWD/var/etc TELEPHONIE_GENERATED_DIR=$PWD/var/etc/generated \
TELEPHONIE_RELOAD=0 TELEPHONIE_TTS=0 \
./.venv/bin/uvicorn app.main:app --port 8080 --reload
```
