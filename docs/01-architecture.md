# Architecture

## Vue d'ensemble

```mermaid
flowchart TB

    subgraph INTERNET["🌐 Internet"]
        BOX["Box Internet (Freebox)<br/>sortie RJ11"]
    end

    subgraph OPN["OPNsense"]
        FW["Routeur / Firewall<br/>Trunk 802.1Q — VLAN 5, 10, 30, 90"]
    end

    subgraph ESXI["ESXi — Hyperviseur"]
        subgraph VM["VM Asterisk — Ubuntu 24.04"]
            direction LR
            NIC5["vNIC1 : VLAN 5<br/>management / SSH"]
            CORE["Asterisk 22"]
            UI["UI de gestion<br/>FastAPI + SQLite<br/>127.0.0.1:8080"]
            GEN[("/etc/asterisk/generated<br/>fichiers .conf")]
            PIPER["Piper TTS<br/>messages vocaux"]
            NIC90["vNIC2 : VLAN 90"]
            USBPT["USB passthrough"]
            UI -- "génère" --> GEN
            GEN -- "#include" --> CORE
            UI -- "asterisk -rx reload" --> CORE
            UI --- PIPER
            NIC5 --- UI
            CORE --- NIC90
            CORE --- USBPT
        end
    end

    subgraph SORTIE["📞 Routes sortantes, dans l'ordre"]
        HT813["1 · HT813 (FXS+FXO)<br/>port FXO → ligne Freebox<br/>gratuit"]
        SIM["2 · SIM7600G-H<br/>GSM/4G, USB passthrough<br/>secours, facturé"]
        HT813 -. "CHANUNAVAIL / CONGESTION" .-> SIM
    end

    subgraph V90["VLAN 90 — postes SIP"]
        TA200A["TA200 port 1<br/>Salon (cadran, hotline)"]
        TA200B["TA200 port 2<br/>Étage"]
        HT801["HT801<br/>Garage"]
        N510["Gigaset N510<br/>base DECT → Maxwell C"]
    end

    subgraph MOBILE["App mobile"]
        FLEXI["Flexisip (K3s)<br/>sip.ffd.link"]
        SOFT["Linphone<br/>sortant uniquement"]
        SOFT --- FLEXI
    end

    BOX -- "RJ11, ligne analogique" --> HT813
    BOX -- "Fibre" --> FW
    FW == "Trunk VLAN 5/10/30/90" ==> ESXI

    NIC90 --- HT813
    NIC90 --- TA200A
    NIC90 --- TA200B
    NIC90 --- HT801
    NIC90 --- N510
    USBPT --- SIM

    FLEXI -. "signalisation" .-> NIC90

    CORE == "SORTANT" ==> HT813
    SIM == "secours" ==> DEST["📱 Destinataire"]

    HT813 -. "ENTRANT" .-> CORE
    SIM -. "ENTRANT" .-> CORE
    CORE -. "IVR 2 niveaux" .-> V90

    classDef sortie fill:#2d5,stroke:#164,color:#fff
    classDef secours fill:#e83,stroke:#a52,color:#fff
    classDef local fill:#666,stroke:#333,color:#fff

    class HT813 sortie
    class SIM secours
    class UI,GEN,PIPER local
```

## Lecture du schéma

Quatre blocs empilés : le physique (box, ligne analogique), le réseau (OPNsense et son
trunk 802.1Q), la virtualisation (la VM et tout ce qui y tourne), puis les périphériques.

Le point important est **en gris, dans la VM** : l'interface de gestion, la base et Piper
sont colocalisés avec Asterisk et ne communiquent que par la boucle locale et par le
système de fichiers. L'interface écrit dans `/etc/asterisk/generated/`, puis demande à
Asterisk de relire ses fichiers. Aucun port supplémentaire n'est ouvert sur le réseau, et
— surtout — **aucun appel ne traverse l'interface**.

Un seul composant s'écarte de ce schéma : `telephonie-events`, qui lit le flux d'événements
d'Asterisk par l'AMI (`127.0.0.1:5038`, compte en lecture seule) pour le republier vers
Home Assistant. Il observe et ne décide de rien : arrêté, les appels se déroulent
exactement pareil.

## Chaîne de traitement d'un appel

### Appel sortant depuis un poste

```
Décroché ──▶ [from-internal] ──▶ motif _0[1-9]XXXXXXXX
                                       │
                                       ▼
                          GoSub(sub-dial-number, numéro, durée)
                                       │
                          ┌────────────┴────────────┐
                          ▼                         │
        Dial(PJSIP/…@grandstream-fxo)               │
                          │                         │
     ┌────────────────────┼─────────────────┐       │
     │ ANSWER             │ CHANUNAVAIL     │ NOANSWER / BUSY
     ▼                    ▼                 ▼
  conversation    Dial(Quectel/quectel0/…)  Return() — pas de bascule
                          │
                   ┌──────┴──────┐
                   ▼             ▼
             conversation   Playback(all-circuits-busy-now)
```

Le détail qui fait tout fonctionner : quand Internet tombe, le HT813 perd son
enregistrement SIP et `Dial()` échoue en une à deux secondes avec `CHANUNAVAIL`, sans
consommer le délai de sonnerie. À l'inverse, un correspondant qui ne décroche pas renvoie
`NOANSWER` — qui ne déclenche **pas** la bascule GSM, puisqu'il n'y a aucune raison de
rappeler la même personne par un chemin payant.

### Appel entrant

```
Ligne Freebox ──▶ HT813 (FXO) ──▶ [from-external-incoming]
                                          │
                                    Answer + menu vocal
                                          │
                    ┌─────────────────────┴───────────────┐
                    ▼ touche 1                            ▼ touche 2
        groupe « toute la maison »                  [menu-personne]
                    │                                     │
         tous les postes sonnent                 une touche par personne
                    │                                     │
          sans réponse : boîte 199          sans réponse : boîte de la personne
```

## Matériel

| Rôle | Matériel | Raccordement | Remarque |
|---|---|---|---|
| Serveur PBX | VM Ubuntu 24.04 LTS sur ESXi | — | Asterisk 22, chan-quectel, UI de gestion |
| Salon + Étage | Yeastar TA200 (2 ports FXS) | Réseau, VLAN 90 | Le port 1 accueille le téléphone à cadran |
| Garage | Grandstream HT801 (1 port FXS) | Réseau jusqu'au bâtiment | Jamais de cuivre analogique entre bâtiments : risque foudre |
| Pont Freebox | Grandstream HT813 (FXS+FXO) | Réseau + prise tél. Freebox | Le port FXO se comporte comme un téléphone branché sur la ligne |
| Secours GSM | Waveshare SIM7600G-H + SIM | USB passthrough | `chan-quectel`, voix par UAC |
| DECT | Gigaset Maxwell C + base N510 | Réseau, VLAN 90 | Jusqu'à trois combinés sur la base |
| Mobile | Linphone via Flexisip | Internet | Sortant seulement — le push VoIP iOS demande un compte Apple Developer |

## Plan de numérotation

```
15 17 18 112 114 115 119 196 197   Urgences — toujours joignables, jamais filtrées
                                    et interdites comme numéro interne

100  Salon                         Postes : un numéro par appareil
101  Étage
102  Garage
103  DECT
104  Mobile

199  Toute la maison               Groupes : font sonner plusieurs postes
2xx  Personnes                     Font sonner tous les postes d'une personne

*97  Ma messagerie                 Codes de service (voir extensions.conf)
*98  Une autre messagerie
*43  Test d'écho
*60  Annonce de l'heure
*65  Annonce du numéro du poste
*9   Menu de décroché des postes « hotline »
```

Les numéros à trois chiffres commençant par 1 sont **réservés** : `112`, `114`, `115` et
`119` sont des numéros d'urgence, et un poste interne qui porterait l'un d'eux les rendrait
inaccessibles. L'interface refuse leur saisie, et le contrôle avant application le
rappelle. C'est la raison pour laquelle les personnes sont numérotées en `2xx`.

## Ce qui reste hors périmètre de ce dépôt

- **Flexisip** (passerelle SIP publique pour l'application mobile) reste déployé sur K3s.
  Ce dépôt décrit l'endpoint `mobile` côté Asterisk, pas les manifestes Flexisip.
- **Le push VoIP iOS** n'est pas implémenté : il demande un compte Apple Developer Program
  et un fork compilé de Linphone. L'application ne reçoit donc pas d'appel quand elle est
  fermée — usage sortant uniquement.
- **Le passthrough USB ESXi** est décrit dans [02 — Installation](02-installation.md) mais
  se configure côté hyperviseur, pas depuis la VM.
