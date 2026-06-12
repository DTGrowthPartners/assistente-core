"""Parseo de payload entrante de whapi → estructura interna."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from app.logging_setup import log

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
    # Atribución de anuncio (Meta "click-to-WhatsApp"): whapi entrega un objeto
    # `referral` cuando el chat se inició desde un anuncio. Señal confiable de
    # que la conversación viene de la pauta. None si no vino de un anuncio.
    referral: dict[str, Any] | None = None


# whapi payload events:
#   - "messages.post" → array de mensajes inbound y outbound
#   - "statuses.post" → estados de delivery
TIPOS_WHAPI_A_INTERNO = {
    "text": "texto",
    "link_preview": "texto",   # mensaje de texto con URL + preview
    "link": "texto",
    "image": "imagen",
    "gif": "imagen",            # whapi a veces clasifica GIFs como tipo aparte
    "animated_image": "imagen",
    "audio": "audio",
    "voice": "audio",
    "ptt": "audio",
    "video": "video",
    "document": "pdf",
    "sticker": "sticker",
    "location": "ubicacion",
    "contact": "contacto",
    # Eventos de Meta / interacciones que no son comunicación real:
    "action": "desconocido",     # tap de botón Meta (FAQ del Business Profile)
    "unknown": "desconocido",    # tipos no documentados (reactions, edits, etc.)
    "reaction": "desconocido",
    "system": "desconocido",
    "edited": "desconocido",
    "deleted": "desconocido",
    "poll": "desconocido",
    "album": "imagen",           # álbumes de fotos
}

# Tipos para los que un mensaje "sin texto y sin media_url" es NORMAL — no
# debe spammear warnings. Caso típico: voice sin caption, image sin caption
# (caption es opcional en WhatsApp), tap de botón Meta (sin contenido).
_TIPOS_SILENCIOSOS_SIN_TEXTO = {
    "voice", "audio", "ptt", "image", "video", "sticker", "gif",
    "animated_image", "album", "document",
    "action", "unknown", "reaction", "system", "edited", "deleted", "poll",
    "location", "contact",
}


def _extraer_texto_fallback(msg: dict[str, Any]) -> str | None:
    """Intenta recuperar el texto de un mensaje cuando el extractor por tipo no
    lo encontró (típico en mensajes con URL/link_preview que whapi entrega con
    una forma distinta). Escanea las ubicaciones comunes."""
    # 1) body directo
    v = msg.get("body")
    if isinstance(v, str) and v.strip():
        return v
    # 2) objetos anidados que suelen traer el texto (text/link_preview/link/action)
    for key in ("text", "link_preview", "link", "action", "extended_text"):
        obj = msg.get(key)
        if isinstance(obj, dict):
            for campo in ("body", "text", "caption", "preview", "description", "title", "url", "canonical_url"):
                val = obj.get(campo)
                if isinstance(val, str) and val.strip():
                    return val
        elif isinstance(obj, str) and obj.strip():
            return obj
    return None


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

    if raw_tipo in ("text", "link_preview", "link"):
        # whapi: {"text": {"body": "..."}} — o variantes para links
        body = msg.get("text") or {}
        if isinstance(body, dict):
            texto = body.get("body")
        elif isinstance(body, str):
            texto = body
        if not texto:
            texto = _extraer_texto_fallback(msg)
    elif raw_tipo in ("image", "video", "audio", "voice", "ptt", "document", "sticker", "gif", "animated_image"):
        media = msg.get(raw_tipo) or {}
        if isinstance(media, dict):
            media_url = media.get("link") or media.get("url") or media.get("file_path")
            # Notas de voz: whapi a veces solo entrega `{"id": "..."}` sin URL.
            # Construimos la URL al endpoint de descarga de whapi /media/{id}.
            if not media_url and media.get("id"):
                from app.config import get_settings as _gs
                _base = _gs().whapi_base_url.rstrip("/")
                media_url = f"{_base}/media/{media['id']}"
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
        texto = _extraer_texto_fallback(msg)  # fallback genérico (tipos no mapeados)

    # Defensa final: si quedó sin texto y sin media (mensaje raro / tipo nuevo),
    # intentar recuperar el texto y, si no se puede, loguear para diagnosticar.
    if not texto and not media_url:
        texto = _extraer_texto_fallback(msg)
        if not texto:
            # Si el tipo es esperado (audio sin caption, action Meta, etc.) →
            # debug. Solo warning si es algo NUEVO que no manejamos.
            if raw_tipo in _TIPOS_SILENCIOSOS_SIN_TEXTO:
                log.debug(
                    "whapi.parser.sin_texto_ok",
                    raw_tipo=raw_tipo, msg_id=msg_id,
                )
            else:
                log.warning(
                    "whapi.parser.sin_texto",
                    raw_tipo=raw_tipo,
                    keys=sorted(msg.keys()),
                    msg_id=msg_id,
                )

    raw_from = msg.get("from")
    raw_chat_id = msg.get("chat_id")
    # Caso 1: INBOUND → `from` es el autor (cliente que escribió).
    # Caso 2: OUTBOUND 1:1 (chat con cliente) → `chat_id` es el destinatario
    #   (numero_cliente). Lo mantenemos como `from_number` por compatibilidad
    #   con el resto del flow que asume "outbound.from_number = cliente al que
    #   le hablamos".
    # Caso 3: OUTBOUND en GRUPO → `chat_id` es el group_id, NO un cliente.
    #   Usar el campo `from` que whapi trae con el autor real (= número del
    #   bot). Si no viene, caer al número del bot configurado.
    if direccion == "inbound":
        from_n = normalizar_numero(raw_from)
    elif isinstance(raw_chat_id, str) and raw_chat_id.endswith("@g.us"):
        # Outbound en grupo: el "from" del autor es el bot, NO el group_id.
        if raw_from:
            from_n = normalizar_numero(raw_from)
        else:
            from app.config import get_settings as _gs
            from_n = _gs().whapi_numero_bot or None
    else:
        # Outbound 1:1: chat_id es el destinatario (preservar comportamiento).
        from_n = normalizar_numero(raw_chat_id)
    to_n = normalizar_numero(msg.get("to") or raw_chat_id)

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

    # Atribución de anuncio (Meta click-to-WhatsApp). whapi puede traerlo como
    # `referral` o anidado en `context.referral`.
    referral = msg.get("referral")
    if not isinstance(referral, dict):
        ctx_ref = ctx.get("referral") if isinstance(ctx, dict) else None
        referral = ctx_ref if isinstance(ctx_ref, dict) else None

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
        referral=referral,
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
