"""Cliente whapi para enviar mensajes (texto + media).

Multi-identidad: cada tarea/handler puede setear el token activo con
`set_token(...)` (vía contextvar). Sin set, usa el token del canal principal
(default — Dairo). Permite que la misma instancia atienda múltiples canales
sin reescribir cada llamada de envío.
"""

from __future__ import annotations

import contextvars
from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

# Token whapi activo para la tarea en curso (asyncio.Task). Si None → canal principal.
_token_var: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "whapi_token", default=None
)


def set_token(token: str | None) -> None:
    """Setea el token whapi para la tarea actual. Llamar al inicio del handler/cron."""
    _token_var.set(token or None)


def _token() -> str:
    return _token_var.get() or settings.whapi_token


def _base_url() -> str:
    return settings.whapi_base_url


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {_token()}"}


def auth_headers() -> dict[str, str]:
    """Versión pública del header de auth para uso desde otros módulos."""
    return _headers()


def _to_e164(numero: str) -> str:
    """De '+573026041584' a '573026041584' (whapi acepta ambos pero prefiere sin +)."""
    return numero.lstrip("+")


class WhapiError(Exception):
    pass


async def enviar_texto(numero: str, texto: str) -> dict[str, Any]:
    """POST /messages/text."""
    url = f"{settings.whapi_base_url}/messages/text"
    payload = {"to": _to_e164(numero), "body": texto}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, json=payload, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_texto.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_botones(
    numero: str,
    *,
    body: str,
    botones: list[tuple[str, str]],
    header: str | None = None,
    footer: str | None = None,
) -> dict[str, Any]:
    """POST /messages/interactive con botones quick-reply.

    `botones` es lista de tuplas (id, titulo). El título es lo que ve el usuario;
    el id se devuelve junto al texto del botón cuando alguien lo clickea. Máx 3
    botones (límite de WhatsApp).

    Soporta envío a CHATS 1:1 y a GRUPOS (probado el 2026-06-04: WhatsApp ahora
    permite quick-reply en grupos).
    """
    if not (1 <= len(botones) <= 3):
        raise WhapiError("Se requieren 1-3 botones")
    url = f"{settings.whapi_base_url}/messages/interactive"
    payload: dict[str, Any] = {
        "to": _to_e164(numero),
        "type": "button",
        "body": {"text": body},
        "action": {
            "buttons": [
                {"type": "quick_reply", "title": titulo[:20], "id": bid}
                for bid, titulo in botones
            ],
        },
    }
    if header:
        payload["header"] = {"type": "text", "text": header[:60]}
    if footer:
        payload["footer"] = {"text": footer[:60]}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, json=payload, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_botones.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_imagen_url(numero: str, image_url: str, caption: str | None = None) -> dict[str, Any]:
    """POST /messages/image con URL pública (CDN Shopify, etc.)."""
    url = f"{settings.whapi_base_url}/messages/image"
    payload: dict[str, Any] = {"to": _to_e164(numero), "media": image_url}
    if caption:
        payload["caption"] = caption
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=payload, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_imagen_url.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_imagen_bytes(
    numero: str,
    data: bytes,
    mime: str = "image/jpeg",
    caption: str | None = None,
    filename: str = "image.jpg",
) -> dict[str, Any]:
    """POST /messages/image multipart con bytes en memoria.

    Útil para reenviar al equipo una imagen recibida de un cliente (ej.
    comprobante de pago) sin tocar el filesystem.
    """
    url = f"{settings.whapi_base_url}/messages/image"
    async with httpx.AsyncClient(timeout=120) as c:
        files = {"media": (filename, data, mime)}
        form: dict[str, Any] = {"to": _to_e164(numero)}
        if caption:
            form["caption"] = caption
        r = await c.post(url, data=form, files=files, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_imagen_bytes.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_video_bytes(
    numero: str,
    data: bytes,
    mime: str = "video/mp4",
    caption: str | None = None,
    filename: str = "video.mp4",
) -> dict[str, Any]:
    """POST /messages/video multipart — envía un video por WhatsApp."""
    url = f"{settings.whapi_base_url}/messages/video"
    async with httpx.AsyncClient(timeout=180) as c:
        files = {"media": (filename, data, mime)}
        form: dict[str, Any] = {"to": _to_e164(numero)}
        if caption:
            form["caption"] = caption
        r = await c.post(url, data=form, files=files, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_video_bytes.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_documento_bytes(
    numero: str,
    data: bytes,
    mime: str = "application/pdf",
    filename: str = "documento.pdf",
    caption: str | None = None,
) -> dict[str, Any]:
    """POST /messages/document multipart — envía un documento (PDF, doc, etc.)."""
    url = f"{settings.whapi_base_url}/messages/document"
    async with httpx.AsyncClient(timeout=120) as c:
        files = {"media": (filename, data, mime)}
        form: dict[str, Any] = {"to": _to_e164(numero), "filename": filename}
        if caption:
            form["caption"] = caption
        r = await c.post(url, data=form, files=files, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_documento_bytes.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_nota_voz_bytes(
    numero: str,
    data: bytes,
    mime: str = "audio/ogg",
    filename: str = "voz.ogg",
) -> dict[str, Any]:
    """POST /messages/voice multipart — envía un audio como NOTA DE VOZ (PTT).

    El audio debe ser ogg/opus para que WhatsApp lo muestre como nota de voz.
    Se usa para responder con voz (TTS de Fish Audio).
    """
    url = f"{settings.whapi_base_url}/messages/voice"
    async with httpx.AsyncClient(timeout=120) as c:
        files = {"media": (filename, data, mime)}
        form: dict[str, Any] = {"to": _to_e164(numero)}
        r = await c.post(url, data=form, files=files, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_nota_voz.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_archivo_local(
    numero: str,
    file_path: str | Path,
    tipo: str = "document",  # document | image | video | audio
    caption: str | None = None,
) -> dict[str, Any]:
    """POST /messages/{tipo} multipart con archivo del filesystem."""
    url = f"{settings.whapi_base_url}/messages/{tipo}"
    path = Path(file_path)
    if not path.exists():
        raise WhapiError(f"Archivo no existe: {path}")

    async with httpx.AsyncClient(timeout=120) as c:
        with open(path, "rb") as f:
            files = {"media": (path.name, f, _mime_for(path))}
            data: dict[str, Any] = {"to": _to_e164(numero)}
            if caption:
                data["caption"] = caption
            r = await c.post(url, data=data, files=files, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.enviar_archivo.fail", status=r.status_code, body=r.text[:200])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


def _mime_for(path: Path) -> str:
    ext = path.suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
        ".webp": "image/webp", ".gif": "image/gif",
        ".mp4": "video/mp4", ".mov": "video/quicktime",
        ".mp3": "audio/mpeg", ".ogg": "audio/ogg", ".m4a": "audio/mp4",
        ".pdf": "application/pdf",
    }.get(ext, "application/octet-stream")


# ─── GRUPOS — administración ────────────────────────────────────────────────


def _normalizar_group_id(group_id: str) -> str:
    """Acepta '120363...@g.us' o '120363...' y devuelve el formato completo."""
    g = (group_id or "").strip()
    if not g:
        raise WhapiError("group_id vacío")
    if not g.endswith("@g.us"):
        g = g + "@g.us"
    return g


def _normalizar_participantes(nums: list[str]) -> list[str]:
    """Para /groups/.../participants whapi exige SOLO dígitos (sin '+' ni espacios).

    Patrón requerido: `^([\\d]{7,15})?(@lid|@s.whatsapp.net)?$`.
    Acepta entrada en cualquier formato y devuelve `573...` (sin +).
    """
    out: list[str] = []
    for n in nums:
        if not n:
            continue
        s = n.split("@", 1)[0]
        # Quitar todo lo que no sea dígito
        s = "".join(ch for ch in s if ch.isdigit())
        if 7 <= len(s) <= 15:
            out.append(s)
    return out


async def obtener_grupo(group_id: str) -> dict[str, Any]:
    """GET /groups/{id} — detalle del grupo incluyendo participantes y avatar."""
    gid = _normalizar_group_id(group_id)
    url = f"{settings.whapi_base_url}/groups/{gid}"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def obtener_invite_link_grupo(group_id: str) -> str:
    """GET /groups/{id}/invite → devuelve el LINK COMPLETO de invitación.

    whapi devuelve `{"invite_code": "Hc1hGD6T..."}` — armamos el URL completo
    `https://chat.whatsapp.com/<code>` que es lo que se comparte por WhatsApp.
    """
    gid = _normalizar_group_id(group_id)
    url = f"{settings.whapi_base_url}/groups/{gid}/invite"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.get(url, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        data = r.json()
    code = (data.get("invite_code") or "").strip()
    if not code:
        raise WhapiError(f"whapi no devolvió invite_code: {data}")
    return f"https://chat.whatsapp.com/{code}"


async def revocar_invite_link_grupo(group_id: str) -> str:
    """DELETE /groups/{id}/invite — invalida el link actual y genera uno nuevo.

    Devuelve el NUEVO link (después de revocar, whapi devuelve el code nuevo).
    Útil si el link viejo se filtró.
    """
    gid = _normalizar_group_id(group_id)
    url = f"{settings.whapi_base_url}/groups/{gid}/invite"
    async with httpx.AsyncClient(timeout=15) as c:
        r = await c.delete(url, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
    # Tras revocar, pedir el nuevo
    return await obtener_invite_link_grupo(group_id)


async def enviar_invite_link_grupo(
    destino: str, group_id: str, *, mensaje_acompaname: str | None = None,
) -> dict[str, Any]:
    """Obtiene el link de invitación al grupo y se lo manda a `destino` (E.164)."""
    link = await obtener_invite_link_grupo(group_id)
    texto = link if not mensaje_acompaname else f"{mensaje_acompaname}\n\n{link}"
    return await enviar_texto(destino, texto)


async def agregar_participantes_grupo(
    group_id: str, participantes: list[str],
) -> dict[str, Any]:
    """POST /groups/{id}/participants — agrega uno o varios al grupo.

    Solo funciona si el bot es admin del grupo. Si el contacto tiene
    privacidad estricta, whapi NO puede agregarlo directamente; en ese caso
    el resultado lo indica y conviene usar `enviar_invite_link_grupo`.
    """
    gid = _normalizar_group_id(group_id)
    body = {"participants": _normalizar_participantes(participantes)}
    url = f"{settings.whapi_base_url}/groups/{gid}/participants"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.post(url, json=body, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.agregar_participantes.fail",
                      status=r.status_code, body=r.text[:300])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def remover_participantes_grupo(
    group_id: str, participantes: list[str],
) -> dict[str, Any]:
    """DELETE /groups/{id}/participants — quita uno o varios del grupo.

    Requiere que el bot sea admin. `participantes` puede ser uno o varios
    números en E.164 (+57...). Devuelve el resultado por participante.

    ⚠️ Acción irreversible. Confirmar antes de llamar.
    """
    gid = _normalizar_group_id(group_id)
    body = {"participants": _normalizar_participantes(participantes)}
    url = f"{settings.whapi_base_url}/groups/{gid}/participants"
    async with httpx.AsyncClient(timeout=20) as c:
        # whapi usa DELETE con body
        r = await c.request("DELETE", url, json=body, headers=_headers())
        if r.status_code >= 400:
            log.error("whapi.remover_participantes.fail",
                      status=r.status_code, body=r.text[:300])
            raise WhapiError(f"HTTP {r.status_code}: {r.text[:200]}")
        return r.json()


async def enviar_typing(numero: str, recording: bool = False) -> None:
    """
    Indica al cliente que estamos 'escribiendo'. Whapi expira el indicador solo.

    Endpoint: PUT /presences/{ChatID}  body={"presence":"typing"|"recording"|"paused"}
    Falla silenciosa: si whapi no responde, igual mandamos el mensaje después.
    """
    chat_id = _to_e164(numero)
    presence = "recording" if recording else "typing"
    url = f"{settings.whapi_base_url}/presences/{chat_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.put(url, json={"presence": presence, "delay": 25}, headers=_headers())
    except Exception as e:
        log.debug("whapi.typing.fail", error=str(e))


async def enviar_paused(numero: str) -> None:
    """Detiene el indicador de typing (opcional — whapi expira solo a los ~25s)."""
    chat_id = _to_e164(numero)
    url = f"{settings.whapi_base_url}/presences/{chat_id}"
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            await c.put(url, json={"presence": "paused"}, headers=_headers())
    except Exception:
        pass


async def descargar_media(media_url: str, destino: str | Path) -> Path:
    """Descarga un archivo de media de whapi (ej. audio de cliente) a disco."""
    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.get(media_url, headers=_headers())
        r.raise_for_status()
        destino.write_bytes(r.content)
    return destino


# ── Stories (estados de WhatsApp, 24h) ──────────────────────────────────────


async def publicar_story_texto(caption: str) -> dict[str, Any]:
    """Publica un story de SOLO texto."""
    url = f"{settings.whapi_base_url}/stories/send/text"
    payload = {"caption": caption}
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(url, json=payload, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"publicar_story_texto: {r.status_code} {r.text}")
        return r.json()


async def publicar_story_imagen_bytes(
    image_bytes: bytes,
    caption: str | None = None,
    filename: str = "story.jpg",
    mime: str = "image/jpeg",
) -> dict[str, Any]:
    """Publica una imagen como story via multipart upload."""
    url = f"{settings.whapi_base_url}/stories/send/media"
    files = {"media": (filename, image_bytes, mime)}
    data = {}
    if caption:
        data["caption"] = caption
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, files=files, data=data, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"publicar_story_imagen_bytes: {r.status_code} {r.text}")
        return r.json()


async def publicar_story_imagen_url(image_url: str, caption: str | None = None) -> dict[str, Any]:
    """Descarga la imagen desde URL y la publica como story (whapi no acepta
    URLs externas directas en /stories/send/media — necesita multipart o
    base64)."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(image_url)
        r.raise_for_status()
        image_bytes = r.content
        mime = r.headers.get("content-type") or "image/jpeg"
    return await publicar_story_imagen_bytes(image_bytes, caption=caption, mime=mime)


async def listar_stories(count: int = 30) -> dict[str, Any]:
    """GET /stories — lista stories publicados recientemente."""
    url = f"{settings.whapi_base_url}/stories"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, headers=_headers(), params={"count": count})
        if r.status_code >= 400:
            raise WhapiError(f"listar_stories: {r.status_code} {r.text}")
        return r.json()


async def eliminar_mensaje(message_id: str) -> dict[str, Any]:
    """DELETE /messages/{id} — borra un mensaje (sirve también para stories
    porque internamente son mensajes con chat_id='stories')."""
    url = f"{settings.whapi_base_url}/messages/{message_id}"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.delete(url, headers=_headers())
        if r.status_code >= 400:
            raise WhapiError(f"eliminar_mensaje: {r.status_code} {r.text}")
        return r.json()
