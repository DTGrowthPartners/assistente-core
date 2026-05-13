"""Parseo de payload entrante de whapi → estructura interna."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

TipoMensaje = Literal[
    "texto", "imagen", "audio", "video", "pdf", "sticker", "documento",
    "ubicacion", "contacto", "desconocido"
]


@dataclass
class MensajeWhapi:
    """Estructura interna de un mensaje recibido de whapi."""

    id: str                          # whapi message id
    from_number: str                 # número del cliente (E.164)
    to_number: str | None            # número del bot
    direccion: Literal["inbound", "outbound"]
    is_from_bot: bool                # True si lo mandó el bot por API
    is_from_human: bool              # True si lo mandó una asesora humana desde la app
    tipo: TipoMensaje
    texto: str | None
    media_url: str | None
    media_mime: str | None
    caption: str | None
    timestamp: int                   # epoch seconds
    chat_id: str                     # whatsapp chat id
    raw: dict[str, Any]              # payload original


# whapi payload events:
#   - "messages.post" → array de mensajes inbound y outbound
#   - "statuses.post" → estados de delivery
TIPOS_WHAPI_A_INTERNO = {
    "text": "texto",
    "image": "imagen",
    "audio": "audio",
    "voice": "audio",
    "video": "video",
    "document": "pdf",
    "sticker": "sticker",
    "location": "ubicacion",
    "contact": "contacto",
}


def normalizar_numero(raw: str | None) -> str | None:
    """De '57302...@s.whatsapp.net' o '57302...' a '+57302...'."""
    if not raw:
        return None
    n = raw.split("@", 1)[0].replace(" ", "")
    if not n.startswith("+"):
        n = "+" + n
    return n


def parsear_mensaje(msg: dict[str, Any]) -> MensajeWhapi | None:
    """Convierte un mensaje whapi a MensajeWhapi. None si no es procesable."""
    msg_id = msg.get("id")
    if not msg_id:
        return None

    from_me = bool(msg.get("from_me", False))
    # whapi marca el origen del outbound:
    #   - "api"   → vino del bot (nosotros)
    #   - "mobile"/"web"/"android"/"ios" → vino de una asesora humana desde la app
    #   - puede venir vacío, None, "unknown", etc. → asumimos bot (defensivo,
    #     porque marcar como humano dispara una pausa de 4h del bot)
    source = (msg.get("source") or "").lower()
    HUMAN_SOURCES = {"mobile", "android", "ios", "web", "desktop", "phone"}

    if from_me:
        direccion = "outbound"
        is_from_human = source in HUMAN_SOURCES
        is_from_bot = not is_from_human
    else:
        direccion = "inbound"
        is_from_bot = False
        is_from_human = False

    # tipo + contenido (varía según whapi)
    raw_tipo = msg.get("type", "text")
    tipo_interno: TipoMensaje = TIPOS_WHAPI_A_INTERNO.get(raw_tipo, "desconocido")

    texto: str | None = None
    media_url: str | None = None
    media_mime: str | None = None
    caption: str | None = None

    if raw_tipo == "text":
        # whapi: {"text": {"body": "..."}}
        body = msg.get("text") or {}
        if isinstance(body, dict):
            texto = body.get("body")
        elif isinstance(body, str):
            texto = body
    elif raw_tipo in ("image", "video", "audio", "voice", "document", "sticker"):
        media = msg.get(raw_tipo) or {}
        if isinstance(media, dict):
            media_url = media.get("link") or media.get("url") or media.get("file_path")
            media_mime = media.get("mime_type")
            caption = media.get("caption")
            texto = caption  # usable para clasificación
    elif raw_tipo == "location":
        loc = msg.get("location") or {}
        texto = f"📍 lat={loc.get('latitude')} lng={loc.get('longitude')}"
    elif raw_tipo == "contact":
        c = msg.get("contact") or {}
        texto = f"👤 contacto: {c.get('name','')} {c.get('phone','')}"
    else:
        texto = msg.get("body")  # fallback genérico

    from_n = normalizar_numero(
        msg.get("from") if direccion == "inbound" else msg.get("chat_id")
    )
    to_n = normalizar_numero(msg.get("to") or msg.get("chat_id"))

    if not from_n:
        return None

    return MensajeWhapi(
        id=str(msg_id),
        from_number=from_n,
        to_number=to_n,
        direccion=direccion,
        is_from_bot=is_from_bot,
        is_from_human=is_from_human,
        tipo=tipo_interno,
        texto=texto,
        media_url=media_url,
        media_mime=media_mime,
        caption=caption,
        timestamp=int(msg.get("timestamp", 0)),
        chat_id=str(msg.get("chat_id", "")),
        raw=msg,
    )


def parsear_payload(payload: dict[str, Any]) -> list[MensajeWhapi]:
    """
    Recibe el payload completo de un webhook whapi y devuelve la lista de
    mensajes procesables. Ignora statuses, presences y otros eventos.
    """
    mensajes: list[MensajeWhapi] = []

    # whapi v1 estructura: { "messages": [...], "event": {...}, ... }
    raw_mensajes = payload.get("messages") or []
    if isinstance(raw_mensajes, dict):
        raw_mensajes = [raw_mensajes]

    for raw in raw_mensajes:
        if not isinstance(raw, dict):
            continue
        m = parsear_mensaje(raw)
        if m is not None:
            mensajes.append(m)

    return mensajes
