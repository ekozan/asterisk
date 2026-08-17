# Dépannage

## La console, avant tout

La plupart des problèmes se voient en direct en regardant Asterisk travailler pendant
qu'on reproduit le symptôme :

```bash
sudo asterisk -rvvv
```

Elle affiche chaque priorité du dialplan exécutée, le `DIALSTATUS` de chaque tentative et
la raison des raccrochés. `Ctrl+C` quitte la console **sans** arrêter Asterisk.

Pour un problème de signalisation SIP :

```bash
sudo asterisk -rx "pjsip set logger on"
# reproduire, puis
sudo asterisk -rx "pjsip set logger off"
```

---

## Un poste n'apparaît pas comme enregistré

```bash
sudo asterisk -rx "pjsip show endpoint salon"
sudo asterisk -rx "pjsip show aors"
```

| Cause | Vérification |
|---|---|
| Configuration jamais appliquée | Le bandeau orange est-il présent dans l'interface ? |
| Mot de passe divergent | Comparer le mot de passe de l'écran Postes avec celui saisi dans l'ATA |
| L'ATA n'atteint pas Asterisk | `ping` depuis l'ATA, VLAN correct, `sudo ufw status` |
| Asterisk n'écoute pas où il faut | `sudo asterisk -rx "pjsip show transports"` |
| Identifiant SIP mal recopié | Il est sensible à la casse et sans espace |

Les échecs d'authentification laissent une trace explicite :

```bash
sudo grep -i "failed for" /var/log/asterisk/messages | tail -20
```

---

## Un appel sortant ne part pas

```bash
sudo asterisk -rvvv        # puis composer le numéro
```

**Le dialplan ne reconnaît pas le numéro** — message `Rejecting unknown extension`. Le
motif attendu est `_0[1-9]XXXXXXXX`, soit dix chiffres commençant par 0. Un numéro à neuf
chiffres ou un `+33` mal converti ne correspond pas :

```bash
sudo asterisk -rx "dialplan show from-internal" | head -40
```

**Le numéro est bloqué volontairement** — la console affiche `International bloque` ou
`Numero surtaxe bloque`. C'est le réglage correspondant qu'il faut lever, en connaissance
de cause.

**Aucune route ne répond** — `all-circuits-busy-now` est joué :

```bash
sudo asterisk -rx "pjsip show endpoint grandstream-fxo"
sudo asterisk -rx "quectel show devices"
```

---

## La bascule GSM ne se déclenche pas

Elle n'a lieu que sur `CHANUNAVAIL` ou `CONGESTION`. Un correspondant qui ne décroche pas
renvoie `NOANSWER`, un poste occupé renvoie `BUSY` : dans les deux cas la ligne a
fonctionné, et rappeler par un chemin payant n'aurait pas de sens.

Pour voir le statut réellement obtenu :

```bash
sudo asterisk -rvvv     # la console affiche « Dial ... status = ... »
```

Si le statut est bien `CHANUNAVAIL` mais que rien ne bascule, vérifier que le second trunk
est actif et que les étiquettes de saut sont bien chaînées :

```bash
sudo asterisk -rx "dialplan show sub-dial-number"
```

---

## Le trunk GSM ne s'initialise pas

```bash
sudo asterisk -rx "quectel show devices"
```

| État | Piste |
|---|---|
| Le module n'apparaît pas | `lsusb` dans la VM ; passthrough ESXi actif ? `usbarbitrator` démarré ? |
| `Not initialized` | Chemins `data=` / `audio=` erronés dans `quectel.conf` — utiliser `/dev/serial/by-id/…` |
| `Free` mais aucun son | Configuration audio USB du module : vérifier `AT+QCFG="usbcfg"` et `AT+CSDVC` |
| Le module a disparu après un redémarrage | Passthrough ESXi lié au port physique : retirer et rajouter le périphérique |

Après un redémarrage de l'hôte, refaire le tour :

```bash
ls -l /dev/serial/by-id/
sudo systemctl restart asterisk
```

---

## Les annonces sont en anglais

Les paquets de sons français n'ont pas été compilés. Reprendre la sélection des modules
([02 — Installation](02-installation.md#sélection-des-modules-et-des-sons-français)) avec
`CORE-SOUNDS-FR-ALAW`, `CORE-SOUNDS-FR-WAV` et `EXTRA-SOUNDS-FR-ALAW`, puis :

```bash
ls /var/lib/asterisk/sounds/fr/ | head
sudo asterisk -rx "core show translation" | head
```

Vérifier aussi que `language=fr` figure bien sur les gabarits d'endpoint.

---

## Une annonce personnalisée ne se joue pas

```bash
ls -l /var/lib/asterisk/sounds/custom/
sudo asterisk -rx "file convert /var/lib/asterisk/sounds/custom/menu-niveau1.wav /tmp/t.gsm"
```

Trois causes classiques :

- **Mauvais format.** Asterisk attend du 8 kHz mono 16 bits. Le script
  `generate-prompts.sh` s'en charge ; un fichier déposé à la main, non.
- **Extension incluse dans le dialplan.** `Playback(custom/menu-niveau1)` — sans `.wav`.
- **Droits.** Le fichier doit être lisible par l'utilisateur `asterisk`.

---

## Le message vocal du menu ne cite pas la nouvelle personne

La régénération passe par Piper, et le journal de la dernière application le dit :
**Interface → Révisions → Détail**.

```bash
sudo -u asterisk /opt/piper-voices/generate.sh "Test." essai
```

| Message | Cause |
|---|---|
| `Voix Piper introuvable` | Chemin du modèle : les deux fichiers `.onnx` **et** `.onnx.json` sont requis |
| `TTS désactivé ou script absent` | `TELEPHONIE_TTS_SCRIPT` pointe un chemin inexistant |
| Erreur d'écriture | `/var/lib/asterisk/sounds/custom` absent de `ReadWritePaths` dans l'unité systemd |

---

## Le script d'installation s'arrête en cours de route

Le script est reprenable : corrigez la cause, puis **relancez la même commande**. Les
étapes déjà réussies sont notées dans `/var/lib/telephonie/.bootstrap/` et seront sautées.

```bash
sudo tail -50 /var/log/telephonie-install.log     # la vraie erreur est ici
ls /var/lib/telephonie/.bootstrap/                # étapes déjà validées
```

| Symptôme | Cause |
|---|---|
| `moins de 3 Go libres sur /usr/src` | La compilation a besoin de place : agrandir le disque de la VM |
| `résolution DNS impossible` | Réseau ou DNS de la VM ; vérifier `netplan` et la route par défaut |
| Échec pendant `make` | Une dépendance manque : relancer `contrib/scripts/install_prereq install` puis le script |
| `--with-hardening exige --admin-net` | Garde-fou volontaire : sans réseau d'administration, le pare-feu couperait SSH |
| La voix Piper ne se télécharge pas | Le script continue et le signale ; déposer les deux fichiers à la main, voir l'étape 5 |

Pour repartir vraiment de zéro sur une étape :

```bash
sudo rm /var/lib/telephonie/.bootstrap/asterisk   # ou packages, quectel, piper, hardening
sudo ./scripts/bootstrap.sh ...
```

---

## L'interface ne démarre pas

```bash
sudo systemctl status telephonie-ui --no-pager
sudo journalctl -u telephonie-ui -n 60 --no-pager
```

| Erreur | Cause |
|---|---|
| `Permission denied` sur la base | Droits de `/var/lib/telephonie` — doit être `asterisk:asterisk` |
| `Read-only file system` | Chemin absent de `ReadWritePaths` dans l'unité |
| `Address already in use` | Un autre service occupe le port 8080 |
| `ModuleNotFoundError` | Le venv n'a pas été reconstruit après une mise à jour : relancer `install.sh` |

---

## L'application écrit les fichiers mais Asterisk ne recharge pas

Le journal de la révision affiche `Unable to connect to remote asterisk`.

```bash
systemctl is-active asterisk
ls -l /var/run/asterisk/asterisk.ctl
sudo -u asterisk /usr/sbin/asterisk -rx "core show version"
```

Le socket de contrôle doit appartenir à `asterisk:asterisk`. Si Asterisk a démarré en
`root` — par exemple lancé à la main —, le socket ne sera pas accessible au service web.
Vérifier `runuser`/`rungroup` dans `/etc/asterisk/asterisk.conf` et redémarrer proprement
par systemd.

---

## Asterisk refuse de démarrer après une modification

```bash
sudo asterisk -cvvvvv     # démarrage au premier plan, erreurs de parsing visibles
```

Causes fréquentes :

- Un fichier `generated/*.conf` absent alors qu'un `#include` le réclame. Recréer un
  fichier vide et relancer une application :

  ```bash
  sudo -u asterisk touch /etc/asterisk/generated/pjsip_endpoints.conf
  ```

- Un gabarit utilisé avant sa déclaration : le `#include` de `pjsip.conf` doit rester
  **après** les blocs `(!)`.

Le retour arrière le plus rapide reste l'écran **Révisions** de l'interface, ou la
sauvegarde déposée par `install.sh` dans `/etc/asterisk/backup-*/`.

---

## Audio à sens unique, ou pas d'audio du tout

Un problème de média, pas de signalisation — l'appel s'établit, mais le son manque.

```bash
sudo asterisk -rx "rtp set debug on"
sudo asterisk -rx "pjsip show endpoint salon" | grep -Ei 'direct_media|rtp_symmetric'
sudo ufw status | grep 10000
```

| Cause | Correctif |
|---|---|
| Plage RTP fermée au pare-feu | Ouvrir `10000-10200/udp` depuis le VLAN 90 |
| Média direct entre postes | `direct_media=no` doit être actif (il l'est dans les gabarits) |
| Codec non partagé | Comparer les codecs du poste et ceux configurés |
| Postes sur deux VLAN différents | Tous les appareils SIP doivent être sur le VLAN 90 |

---

## Un numéro composé fait autre chose que prévu

Deux entrées du dialplan se recouvrent : Asterisk retient la plus spécifique, ou la
première si elles sont de même spécificité.

```bash
sudo asterisk -rx "dialplan show 5@from-internal"
```

Le contrôle avant application signale les doublons entre postes, personnes, groupes et
abrégés. Il ne peut pas, en revanche, deviner qu'un abrégé `1` empêche de composer les
numéros commençant par 1 : c'est un choix de plan de numérotation, pas une erreur.

---

## Le téléphone à cadran ne compose rien

1. L'ATA convertit-il bien la numérotation décimale (*pulse dialing*) ? C'est un réglage
   explicite chez Yeastar et Grandstream, souvent désactivé par défaut.
2. Le poste est-il en mode **hotline** dans l'interface, avec `*9` comme cible ?
3. Le champ « Offhook Auto-Dial » de l'ATA contient-il bien `*9` ?

```bash
sudo asterisk -rvvv     # décrocher : le contexte from-internal doit recevoir *9
```

Le cadran n'émet que les chiffres 1 à 0 : tout ce qui doit être composable depuis ce poste
passe par des numéros abrégés courts.
