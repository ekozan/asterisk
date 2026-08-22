"""API JSON pour les intégrations machine (Home Assistant, scripts).

Protégée par un jeton statique passé en en-tête `X-API-Token`, et désactivée
tant que TELEPHONIE_API_TOKEN est vide. Elle reste volontairement minuscule :
trois actions, aucune écriture en base, aucune gestion d'utilisateurs.
"""

from __future__ import annotations

import hmac
import re
import sqlite3

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from . import asterisk, config, db, generator
from .webutil import get_conn

router = APIRouter(prefix="/api")

NUMBER_RE = re.compile(r"^\+?[0-9]{2,15}$")
SOUND_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def require_token(x_api_token: str = Header(default="")) -> None:
    if not config.API_TOKEN:
        raise HTTPException(status_code=404, detail="API désactivée (TELEPHONIE_API_TOKEN vide)")
    if not hmac.compare_digest(x_api_token, config.API_TOKEN):
        raise HTTPException(status_code=401, detail="Jeton invalide")


class NotifyRequest(BaseModel):
    number: str = Field(..., description="Numéro à appeler, format national ou E.164")
    sound: str = Field(..., description="Nom du son dans sounds/custom, sans extension")


class CallRequest(BaseModel):
    device: str = Field(..., description="Identifiant du poste à faire sonner")
    number: str = Field(..., description="Numéro à joindre une fois le poste décroché")


@router.get("/status", dependencies=[Depends(require_token)])
def status(conn: sqlite3.Connection = Depends(get_conn)):
    states = asterisk.endpoint_status()
    return {
        "asterisk": bool(states) or asterisk.is_running(),
        "active_calls": asterisk.active_channels(),
        "config_dirty": generator.is_dirty(conn),
        "endpoints": states,
    }


@router.post("/notify", dependencies=[Depends(require_token)])
def notify(payload: NotifyRequest):
    """Appelle un numéro et joue deux fois un message pré-enregistré."""
    if not NUMBER_RE.match(payload.number):
        raise HTTPException(status_code=422, detail="Numéro invalide")
    if not SOUND_RE.match(payload.sound):
        raise HTTPException(status_code=422, detail="Nom de son invalide")

    result = asterisk.originate(
        f"Local/{payload.number}@from-internal", "notify", payload.sound
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.output or "Asterisk injoignable")
    return {"ok": True, "output": result.output}


@router.post("/call", dependencies=[Depends(require_token)])
def call(payload: CallRequest, conn: sqlite3.Connection = Depends(get_conn)):
    """Click-to-call : fait sonner un poste de la maison, puis compose le numéro."""
    if not NUMBER_RE.match(payload.number):
        raise HTTPException(status_code=422, detail="Numéro invalide")

    device = db.one(
        conn, "SELECT slug FROM devices WHERE slug = ? AND enabled = 1", (payload.device,)
    )
    if device is None:
        raise HTTPException(status_code=404, detail="Poste inconnu ou désactivé")

    result = asterisk.originate(
        f"PJSIP/{device['slug']}", "from-internal", payload.number
    )
    if not result.ok:
        raise HTTPException(status_code=502, detail=result.output or "Asterisk injoignable")
    return {"ok": True, "output": result.output}
