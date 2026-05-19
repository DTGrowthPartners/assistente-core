"""Cliente whapi para enviar mensajes (texto + media)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()


def _headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {settings.whapi_token}"}


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
