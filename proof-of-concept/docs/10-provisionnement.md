# Provisionnement automatique des ATA (Grandstream)

## Ce que ça change

Sans provisionnement, chaque poste se configure deux fois : dans l'interface de gestion,
puis à la main dans l'interface web de l'ATA. Et il faut recommencer à chaque retour aux
réglages d'usine ou à chaque régénération de mot de passe SIP.

Avec, l'appareil va chercher sa configuration au démarrage :

```
HT801 démarre
     │
     ▼  GET http://10.0.90.20:8081/cfg000b82aabbcc.xml
telephonie-prov ──lit──▶ SQLite ──rend──▶ XML avec identifiant et mot de passe
     │
     ▼
l'ATA s'enregistre sur Asterisk
```

Il ne reste qu'à saisir l'adresse MAC dans l'interface.

---

## ⚠ À lire avant de provisionner plusieurs appareils

Grandstream ne nomme pas ses réglages : il les numérote (`P35`, `P47`…), et **ces numéros
varient selon le modèle et la version de firmware**.

Les numéros livrés dans `app/provisioning.py` sont ceux couramment documentés pour la série
HT80x, mais ils **ne sont pas vérifiés sur votre matériel**. Le profil est donc marqué
`verified = False`, et l'interface l'affiche en rouge.

Le problème n'est pas qu'un mauvais numéro échoue — c'est qu'**il ne dit rien** : l'appareil
ignore la ligne qu'il ne comprend pas et garde son ancien réglage. Un ATA qui reste « non
enregistré » après provisionnement vient presque toujours de là, et rien dans les journaux
ne le désigne.

La section suivante vérifie la correspondance en quelques minutes, sur un seul appareil.

---

## Valider les numéros sur votre matériel

L'idée : configurer **un** ATA à la main — ce que vous faites de toute façon pour le premier
—, puis lui demander où il a rangé les valeurs.

### 1. Exporter la configuration de l'appareil

Interface web de l'ATA → *Maintenance* → *Upgrade and Provisioning* → **Download device
configuration**. Vous obtenez un fichier XML rempli de balises `<Pxxx>`.

Récupérez-le sur la VM (ou travaillez depuis votre poste, le script n'a besoin que du
fichier et de la base).

### 2. Chercher où sont les valeurs que vous connaissez

```bash
scripts/prov-import.py config.xml --find garage --find 10.0.90.20
```

```
1247 P-values lues.

« garage » apparaît dans :
    P35     = garage
    P36     = garage
    P270    = garage

« 10.0.90.20 » apparaît dans :
    P47     = 10.0.90.20
```

Vous venez de lire, sur votre firmware, les numéros de l'identifiant SIP, de l'identifiant
d'authentification, du nom de compte et du serveur. Comparez-les à ceux de
`app/provisioning.py` et corrigez ce qui diffère.

### 3. Comparer l'ensemble

```bash
scripts/prov-import.py config.xml --device garage
```

Le script affiche, ligne par ligne, ce que nous génèrerions et ce que l'appareil porte
réellement.

Un écart n'est pas forcément une erreur : la valeur peut légitimement différer. **Ce qui
doit alerter, c'est de retrouver la valeur attendue sous un autre numéro** — cela signifie
que notre carte pointe au mauvais endroit.

### 4. Marquer le profil comme vérifié

Une fois les numéros corrigés dans `app/provisioning.py` :

```python
GRANDSTREAM_HT80X = ProvProfile(
    ...
    verified=True,
)
```

L'avertissement rouge disparaît de l'interface. Redéployez avec
`sudo ./scripts/install.sh`.

### Autre voie : le gabarit officiel

Grandstream publie, sur la page de firmware de chaque modèle, un fichier de gabarit XML
listant tous les P-values du modèle. S'il est disponible pour votre version, il donne la
réponse directement — mais l'export de l'appareil reste la source la plus fiable, parce
qu'il correspond au firmware réellement installé.

---

## Mise en service

### Côté serveur

Le service `telephonie-prov` écoute par défaut sur `127.0.0.1:8081` : dans cet état,
**aucun ATA ne peut le joindre**. C'est délibéré — l'ouvrir est un choix explicite.

`bootstrap.sh --voip-ip 10.0.90.20` s'en charge. Manuellement :

```bash
sudo install -d /etc/telephonie
sudo tee /etc/telephonie/prov.env >/dev/null <<'EOF'
TELEPHONIE_PROV_HOST=10.0.90.20
TELEPHONIE_PROV_PORT=8081
EOF
sudo systemctl restart telephonie-prov
sudo ufw allow in on ens192 from 10.0.90.0/24 to any port 8081 proto tcp

curl -s http://10.0.90.20:8081/health     # doit répondre : ok
```

Puis, dans **Réglages** de l'interface :

| Réglage | Valeur |
|---|---|
| Provisionnement : serveur SIP écrit dans les ATA | `10.0.90.20` |
| Provisionnement : URL du service | `http://10.0.90.20:8081` |
| Provisionnement : mot de passe admin des ATA | optionnel — vide = inchangé |

### Côté appareil

Une seule fois, dans l'interface web de l'ATA — *Maintenance* → *Upgrade and
Provisioning* :

| Champ | Valeur |
|---|---|
| Upgrade Via | `HTTP` |
| Config Server Path | `10.0.90.20:8081` |
| Automatic Upgrade | activé, pour que l'appareil se resynchronise seul |

> Certains firmwares veulent le chemin **sans** `http://`, d'autres l'acceptent. En cas de
> doute, essayez les deux et regardez l'écran **Provisionnement** de l'interface : il liste
> les requêtes reçues.

### Zéro contact, avec DHCP option 66

Publier l'URL par l'option DHCP 66 depuis OPNsense évite même cette étape : un appareil
neuf branché sur le VLAN 90 récupère l'adresse du serveur, demande son fichier et se
configure seul. C'est le seul montage où un remplacement d'ATA après panne ne demande
aucune intervention — hormis saisir la nouvelle MAC dans l'interface.

---

## Utilisation courante

### Ajouter un poste provisionné

1. **Postes → Ajouter**, saisir l'adresse MAC (n'importe quel format : `00:0b:82:aa:bb:cc`,
   `000B82AABBCC`, `00-0b-82-aa-bb-cc` — elle est normalisée).
2. **Appliquer** — c'est là qu'Asterisk apprend le mot de passe.
3. Brancher l'ATA, ou le redémarrer s'il l'était déjà.

**L'ordre compte.** Le mot de passe servi à l'appareil vient de la base, celui qu'Asterisk
accepte vient des fichiers générés : les deux ne coïncident qu'après « Appliquer ». Dans
l'autre ordre, l'ATA se présente avec un mot de passe encore inconnu d'Asterisk et reste
« non enregistré » jusqu'à sa prochaine resynchronisation.

### Vérifier avant de redémarrer un appareil

L'écran **Provisionnement** propose « Voir le XML » pour chaque poste : c'est exactement ce
que l'appareil recevra. À regarder avant un redémarrage, surtout tant que le profil n'est
pas vérifié.

```bash
# Depuis la VM, la même chose en ligne de commande
curl -s http://10.0.90.20:8081/cfg000b82aabbcc.xml
```

### Changer un mot de passe SIP

Bouton *Régénérer le mot de passe*, puis **Appliquer**, puis redémarrer l'ATA. Plus rien à
saisir nulle part.

### Remplacer un appareil en panne

Saisir la MAC du nouvel appareil à la place de l'ancienne, appliquer, brancher. La
configuration part toute seule.

---

## Ce que le provisionnement ne fait pas

**Les réglages FXO du HT813 ne sont pas poussés.** Le seuil de détection de raccroché
(*Current Disconnect Threshold*) se calibre appareil en main, par essais successifs — voir
[08 — Matériel](08-materiel.md#détection-du-raccroché--le-réglage-le-plus-important). Le
pousser à l'aveugle laisserait la ligne Freebox occupée après chaque appel, et c'est
précisément le genre de panne qu'on ne remarque qu'au pire moment.

**Ni le Yeastar TA200, ni la base Gigaset.** Les deux savent se provisionner, mais dans des
formats qui n'ont rien à voir avec celui de Grandstream. Le mécanisme est prêt à les
accueillir : ajouter un `ProvProfile` dans `app/provisioning.py` et la fonction de rendu
correspondante. La partie longue reste la même — établir la correspondance sur le matériel
réel.

**La zone de tonalité et l'affichage du numéro** restent manuels : leurs P-values varient
beaucoup selon les modèles, et une tonalité d'occupation non reconnue par un combiné
analogique est difficile à diagnostiquer.

---

## Sécurité

Le fichier servi contient le mot de passe SIP en clair, sur HTTP non authentifié. C'est
inhérent au provisionnement d'ATA : l'appareil n'a pas d'identité avant d'être configuré,
il ne peut donc rien présenter pour s'authentifier. Quatre limites encadrent ça :

- **Service séparé.** Le VLAN voix ne voit que `telephonie-prov`, qui n'expose que la
  lecture d'un fichier de configuration. L'interface d'administration reste sur
  `127.0.0.1`, dans un autre processus. Les tests vérifient qu'aucune route
  d'administration n'est joignable par ce service.
- **Écoute fermée par défaut.** Sans `/etc/telephonie/prov.env`, le service reste sur la
  boucle locale.
- **Rien pour les inconnus.** Une MAC non déclarée ou un poste désactivé reçoivent un 404,
  identique à celui d'une MAC mal formée : un appareil non autorisé n'apprend même pas ce
  qui existe.
- **Tout est tracé.** Chaque requête est journalisée avec l'IP demandeuse, visible dans
  l'écran Provisionnement et dans le journal d'administration.

Le risque résiduel est qu'un appareil branché sur le VLAN 90 **et** connaissant la MAC d'un
poste puisse lire son mot de passe SIP. Sur un VLAN voix fermé qui ne contient que vos ATA,
c'est acceptable — c'est le même modèle de confiance que celui qui laisse ces appareils
s'enregistrer en SIP. Si ce VLAN venait à accueillir autre chose, il faudrait passer en
HTTPS avec configuration chiffrée (Grandstream le supporte), ce qui n'est pas implémenté
ici.

---

## Dépannage

| Symptôme | Piste |
|---|---|
| Aucune requête dans l'écran Provisionnement | L'appareil ne joint pas le service : `curl http://<ip>:8081/health` depuis le VLAN 90, `systemctl status telephonie-prov`, règle ufw sur 8081 |
| Requête reçue, marquée « MAC non déclarée » | La MAC saisie dans l'interface ne correspond pas à celle de l'appareil — l'écran affiche celle qui a été demandée, recopiez-la |
| Requête servie, mais l'ATA ne s'enregistre pas | Le cas typique d'un P-value erroné : refaites la validation ci-dessus. Vérifiez aussi que « Appliquer » a bien été passé après le dernier changement de mot de passe |
| L'ATA s'enregistre puis se déconfigure | Deux sources de configuration : désactivez l'*Automatic Upgrade* d'un autre serveur de provisionnement, ou une ancienne URL restée dans l'appareil |
| `curl` renvoie 404 sur une MAC correcte | Le poste est-il actif dans l'interface ? Un poste désactivé n'est pas servi, volontairement |
