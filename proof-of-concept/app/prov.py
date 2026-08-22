"""Service de provisionnement, exposé aux ATA sur le VLAN voix.

C'est une application distincte de l'interface de gestion, et c'est délibéré :
un ATA ne sait pas s'authentifier, donc ces routes sont forcément ouvertes à
qui atteint le port. En les isolant dans leur propre service, le VLAN voix ne
voit QUE la lecture d'un fichier de configuration — jamais les écrans
d'administration, qui restent sur la boucle locale.

Lancement (unité systemd telephonie-prov.service) :
    uvicorn app.prov:app --host <ip-du-vlan-voix> --port 8081
"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import PlainTextResponse, Response

from . import db, provisioning

app = FastAPI(title="Provisionnement ATA", docs_url=None, redoc_url=None,
              openapi_url=None)


def _log(request: Request, mac: str, outcome: str) -> None:
    """Trace chaque requête : « est-ce que l'ATA a seulement demandé sa
    configuration ? » est la première question du dépannage, et sans trace
    elle reste sans réponse."""
    client = request.client.host if request.client else "?"
    try:
        with db.connect() as conn:
            db.audit(conn, f"ata:{client}", f"prov-{outcome}", mac)
    except Exception:
        # Une base momentanément verrouillée ne doit pas empêcher un appareil
        # de récupérer sa configuration au démarrage.
        pass


@app.get("/health", response_class=PlainTextResponse)
def health() -> str:
    return "ok"


@app.get("/cfg.xml")
def global_config() -> Response:
    """Configuration commune que Grandstream lit avant le fichier par MAC.

    Volontairement vide de tout identifiant : ce fichier est servi à
    n'importe quel appareil qui le demande, y compris un inconnu.
    """
    body = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        "<!-- Configuration commune : aucun identifiant ici, ce fichier est\n"
        "     servi sans vérification de MAC. Les comptes SIP sont dans le\n"
        "     fichier cfg<mac>.xml propre à chaque appareil. -->\n"
        '<gs_provision version="1">\n'
        '  <config version="1">\n'
        "  </config>\n"
        "</gs_provision>\n"
    )
    return Response(content=body, media_type="text/xml")


@app.get("/cfg{mac}.xml")
def device_config(mac: str, request: Request) -> Response:
    normalized = provisioning.normalize_mac(mac)
    if normalized is None:
        _log(request, mac[:32], "mac-invalide")
        return Response(status_code=404)

    with db.connect() as conn:
        device = provisioning.device_by_mac(conn, normalized)
        if device is None:
            # Même réponse qu'une MAC mal formée : un appareil non déclaré
            # n'apprend rien sur ce qui existe ou non.
            _log(request, normalized, "inconnu")
            return Response(status_code=404)
        settings = db.get_settings(conn)

    body = provisioning.build_config(device, settings)
    _log(request, normalized, "servi")
    return Response(
        content=body,
        media_type="text/xml",
        headers={"Cache-Control": "no-store"},
    )
