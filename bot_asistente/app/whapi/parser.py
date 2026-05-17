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
    # Reply / quoted message: cuando el cliente cita un mensaje anterior (típicamente
    # uno del bot mostrando un producto) seleccionándolo y respondiendo.
    quoted_message_id: str | None = None  # whapi id del mensaje citado
    quoted_content: str | None = None     # texto/caption del mensaje citado (preview)
    quoted_from_me: bool | None = None    # True si el mensaje citado lo envió el bot
    # Pushname: nombre que el cliente configuró en su perfil de WhatsApp.
    # Solo viene en inbound. Útil para conocer el nombre antes de que lo diga.
    from_name: str | None = None


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
    #   - puede venir vacío, None, "unknown", etc. → asumimos BOT (defensivo)
    #
    # OJO: si from_me=True y NO podemos confirmar que fue humano, ASUMIR BOT.
    # Marcar como humano dispara una pausa de 4h. Marcar como bot solo causa
    # que se ignore. Es preferible el segundo (más seguro).
    source = (msg.get("source") or "").lower()
    HUMAN_SOURCES = {"mobile", "android", "ios", "web", "desktop", "phone"}

    if from_me:
        direccion = "outbound"
        # Solo es humano si source DICE EXPLÍCITAMENTE que viene de un cliente
        # de WhatsApp (mobile/android/ios/web/desktop). Cualquier otro caso
        # (api, vacío, unknown, etc.) → bot.
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

    # Reply/quoted message: whapi entrega el contexto del mensaje citado en
    # `context` (a veces como `context.quoted_id`, a veces como objeto entero).
    # Capturamos para que el flow pueda resolver "este me gusta" → producto X.
    quoted_id: str | None = None
    quoted_content: str | None = None
    quoted_from_me: bool | None = None
    ctx = msg.get("context") or {}
    if isinstance(ctx, dict) and ctx:
        quoted_id = (
            ctx.get("quoted_id")
            or ctx.get("quoted_message_id")
            or ctx.get("id")
        )
        quoted_from_me = ctx.get("from_me") if "from_me" in ctx else None
        # Preview del cuerpo citado: texto plano o caption
        qc = ctx.get("quoted_content") or ctx.get("quoted") or {}
        if isinstance(qc, dict):
            quoted_content = (
                qc.get("body")
                or qc.get("text")
                or qc.get("caption")
                or (qc.get("image") or {}).get("caption")
            )
        elif isinstance(qc, str):
            quoted_content = qc
        # Fallback: a veces el preview viene en context.body/text directamente
        if not quoted_content:
            quoted_content = ctx.get("body") or ctx.get("text") or ctx.get("caption")

    # Pushname (whapi lo manda como `from_name` solo en inbound de personas)
    from_name = msg.get("from_name") if direccion == "inbound" else None
    if isinstance(from_name, str):
        from_name = from_name.strip() or None
    else:
        from_name = None

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
        quoted_message_id=str(quoted_id) if quoted_id else None,
        quoted_content=quoted_content,
        quoted_from_me=quoted_from_me,
        from_name=from_name,
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
