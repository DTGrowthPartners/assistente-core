"""Cliente Cal.com (API v2) para agendar reuniones con prospectos.

El bot usa esto en el flujo de prospecto para:
  - consultar slots reales disponibles (NUNCA inventa horarios)
  - crear una reserva
  - cancelar / reprogramar

Config en settings (.env): CALCOM_API_KEY, CALCOM_EVENT_TYPE_ID, CALCOM_BASE_URL.
Las credenciales se reusan de la integración Cal.com ya validada del endpoint
:8002 de Dairo (ver DAIRO_BOOKING_FLOW). Verificar endpoints/version contra esa
integración cuando se conecten las credenciales reales.

NOTA: Cal.com ha cambiado de versión de API varias veces. Centralizamos la
versión y los paths como constantes para ajustarlos fácil. Los métodos
devuelven dicts estructurados {"ok": bool, ...} y nunca lanzan al caller.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

# Versiones de la API v2 (header `cal-api-version`). Ajustar si la cuenta usa otra.
_SLOTS_API_VERSION = "2024-09-04"
_BOOKINGS_API_VERSION = "2024-08-13"

_TIMEOUT = 20


def _headers(api_version: str) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.calcom_api_key}",
        "cal-api-version": api_version,
        "Content-Type": "application/json",
    }


def _configurado() -> bool:
    return bool(settings.calcom_api_key and settings.calcom_event_type_id)


async def slots_disponibles(
    *,
    start: datetime,
    end: datetime,
    zona: str = "America/Bogota",
) -> dict[str, Any]:
    """Devuelve los slots disponibles entre `start` y `end`.

    Return: {"ok": True, "slots": ["2026-05-28T15:00:00-05:00", ...]} o
            {"ok": False, "error": "..."}.
    """
    if not _configurado():
        return {"ok": False, "error": "Cal.com no configurado (falta API key o event type)."}

    # Cal.com v2 acepta start/end como YYYY-MM-DD (en la zona pedida). Usamos
    # fechas (no datetime UTC) para no correr el día por el offset.
    try:
        tz = ZoneInfo(zona)
    except Exception:
        tz = ZoneInfo("America/Bogota")
    params = {
        "eventTypeId": settings.calcom_event_type_id,
        "start": start.astimezone(tz).date().isoformat(),
        "end": end.astimezone(tz).date().isoformat(),
        "timeZone": zona,
    }
    url = f"{settings.calcom_base_url}/slots"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=_headers(_SLOTS_API_VERSION), params=params)
        if r.status_code >= 400:
            log.warning("calcom.slots.http_error", status=r.status_code, body=r.text[:300])
            return {"ok": False, "error": f"Cal.com slots {r.status_code}"}
        data = r.json()
        # La forma del payload v2 suele ser {"data": {"YYYY-MM-DD": [{"start": "..."}]}}
        slots = _parse_slots(data)
        return {"ok": True, "slots": slots}
    except Exception as e:
        log.exception("calcom.slots.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}


def _parse_slots(data: dict) -> list[str]:
    """Aplana el payload de slots a una lista de ISO datetimes."""
    out: list[str] = []
    bloque = data.get("data", data) if isinstance(data, dict) else {}
    if isinstance(bloque, dict):
        for _dia, items in bloque.items():
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("start"):
                        out.append(it["start"])
                    elif isinstance(it, str):
                        out.append(it)
    return out


async def crear_reserva(
    *,
    inicio: datetime,
    nombre: str,
    email: str,
    telefono: str | None = None,
    negocio: str | None = None,
    zona: str = "America/Bogota",
    notas: str | None = None,
) -> dict[str, Any]:
    """Crea una reserva para el slot `inicio`.

    Return: {"ok": True, "booking_id": ..., "uid": ..., "inicio": iso} o
            {"ok": False, "error": "..."}.
    """
    if not _configurado():
        return {"ok": False, "error": "Cal.com no configurado."}

    payload: dict[str, Any] = {
        "eventTypeId": int(settings.calcom_event_type_id)
        if str(settings.calcom_event_type_id).isdigit()
        else settings.calcom_event_type_id,
        "start": inicio.astimezone(timezone.utc).isoformat(),
        "attendee": {
            "name": nombre,
            "email": email,
            "timeZone": zona,
            "language": "es",   # email de confirmación al guest en español
            **({"phoneNumber": telefono} if telefono else {}),
        },
    }
    metadata: dict[str, str] = {}
    if negocio:
        metadata["negocio"] = negocio
    if notas:
        payload["bookingFieldsResponses"] = {"notes": notas}
    if metadata:
        payload["metadata"] = metadata

    url = f"{settings.calcom_base_url}/bookings"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, headers=_headers(_BOOKINGS_API_VERSION), json=payload)
        if r.status_code >= 400:
            log.warning("calcom.booking.http_error", status=r.status_code, body=r.text[:300])
            return {"ok": False, "error": f"Cal.com booking {r.status_code}: {r.text[:150]}"}
        data = r.json().get("data", r.json())
        return {
            "ok": True,
            "booking_id": str(data.get("id") or ""),
            "uid": str(data.get("uid") or ""),
            "inicio": data.get("start") or inicio.isoformat(),
        }
    except Exception as e:
        log.exception("calcom.booking.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}


def verificar_firma(body: bytes, firma: str | None) -> bool:
    """Verifica el HMAC-SHA256 del webhook de Cal.com (header X-Cal-Signature-256).

    Si no hay secret configurado, no verifica (devuelve True) — útil en dev.
    """
    secret = settings.calcom_webhook_secret
    if not secret:
        return True
    if not firma:
        return False
    import hashlib
    import hmac
    esperado = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(esperado, firma)


async def crear_webhook(*, url: str, triggers: list[str] | None = None) -> dict[str, Any]:
    """Registra un webhook en Cal.com apuntando a `url`. One-shot de setup.

    triggers default: creación/cancelación/reprogramación de bookings.
    """
    if not settings.calcom_api_key:
        return {"ok": False, "error": "Cal.com no configurado."}
    payload = {
        "subscriberUrl": url,
        "triggers": triggers or ["BOOKING_CREATED", "BOOKING_CANCELLED", "BOOKING_RESCHEDULED"],
        "active": True,
        **({"secret": settings.calcom_webhook_secret} if settings.calcom_webhook_secret else {}),
    }
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                f"{settings.calcom_base_url}/webhooks",
                headers=_headers(_BOOKINGS_API_VERSION),
                json=payload,
            )
        if r.status_code >= 400:
            return {"ok": False, "error": f"{r.status_code}: {r.text[:200]}"}
        return {"ok": True, "data": r.json()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


async def cancelar_reserva(*, uid: str, motivo: str = "") -> dict[str, Any]:
    """Cancela una reserva por su uid."""
    if not settings.calcom_api_key:
        return {"ok": False, "error": "Cal.com no configurado."}
    url = f"{settings.calcom_base_url}/bookings/{uid}/cancel"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(
                url,
                headers=_headers(_BOOKINGS_API_VERSION),
                json={"cancellationReason": motivo or "Cancelado por el cliente"},
            )
        if r.status_code >= 400:
            return {"ok": False, "error": f"Cal.com cancel {r.status_code}"}
        return {"ok": True}
    except Exception as e:
        log.exception("calcom.cancel.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}
