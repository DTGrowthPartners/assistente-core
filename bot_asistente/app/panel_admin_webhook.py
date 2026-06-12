"""Webhooks salientes hacia la plataforma admin externa.

Cuando algo relevante pasa en el bot (estado cambió, cita agendada, alerta
abierta, etc.), si está configurado `PANEL_ADMIN_WEBHOOK_URL`, el bot manda
un POST a esa URL con el evento.

La plataforma admin lo recibe y actualiza su UI / dispara notificaciones.

Body shape:
    {
        "event": "bot.estado_cambiado" | "bot.cita_agendada" | "bot.alerta_abierta" | ...,
        "ts": "2026-06-10T22:50:00-05:00",
        "bot": "dairo-bot",
        "data": { ... event-specific ... }
    }

Headers:
    X-Bot-Source: dairo-bot
    X-Bot-Signature: <hmac-sha256 del body con panel_admin_webhook_secret>  (si hay secret)

Fallos NO bloquean al bot — solo se loggean. La plataforma admin debe ser
robusta a duplicados (el bot puede reintentar).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.config import get_settings
from app.logging_setup import log

_TIMEOUT = 10  # corto — no bloquear nada


def _configurado() -> bool:
    s = get_settings()
    return bool((s.panel_admin_webhook_url or "").strip())


def _firmar(body_bytes: bytes, secret: str) -> str:
    return hmac.new(secret.encode("utf-8"), body_bytes, hashlib.sha256).hexdigest()


async def emitir_evento(event: str, data: dict[str, Any] | None = None) -> None:
    """POST asincrónico a la plataforma admin. Nunca lanza.

    Si no hay URL configurada, no-op silencioso.
    """
    if not _configurado():
        return
    s = get_settings()
    body = {
        "event": event,
        "ts": datetime.now(ZoneInfo(s.tz)).isoformat(),
        "bot": "dairo-bot",
        "data": data or {},
    }
    body_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json; charset=utf-8",
        "X-Bot-Source": "dairo-bot",
    }
    if s.panel_admin_webhook_secret:
        headers["X-Bot-Signature"] = _firmar(body_bytes, s.panel_admin_webhook_secret)

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(s.panel_admin_webhook_url, content=body_bytes,
                             headers=headers)
        if r.status_code >= 400:
            log.warning("panel_admin_webhook.http_error",
                        event=event, status=r.status_code, body=r.text[:200])
        else:
            log.info("panel_admin_webhook.sent",
                     event=event, status=r.status_code)
    except Exception as e:
        log.warning("panel_admin_webhook.fail", event=event, error=str(e)[:200])
