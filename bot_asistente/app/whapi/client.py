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
