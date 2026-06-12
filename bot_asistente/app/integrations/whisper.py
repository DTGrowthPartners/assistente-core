"""Transcripción de notas de voz vía la API de OpenAI (Whisper).

Patrón: el webhook detecta `tipo == "audio"` y llama a `transcribir_audio()`
para mutar `msg.texto` con la transcripción ANTES de routear al flow. Así
prospectos, cliente WL y equipo reciben el mensaje como si lo hubieran
escrito — el resto del pipeline (historial, panel, contexto del modelo) no
necesita saber que vino de un audio.

Falla silencioso: si no hay key, falla la descarga, el audio es muy grande o
la API devuelve error → `None` y el flow cae a fallback (pedir que escriban).
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.logging_setup import log
from app.whapi.client import auth_headers

settings = get_settings()

_OPENAI_TRANSCRIBE_URL = "https://api.openai.com/v1/audio/transcriptions"
_MAX_AUDIO_BYTES = 25 * 1024 * 1024  # límite de OpenAI

# OpenAI infiere el formato por el nombre de archivo → mapeamos mime → extensión.
_EXT_POR_MIME = {
    "audio/ogg": "ogg", "audio/opus": "ogg", "audio/oga": "ogg",
    "audio/mpeg": "mp3", "audio/mp3": "mp3",
    "audio/mp4": "mp4", "audio/m4a": "m4a", "audio/x-m4a": "m4a", "audio/aac": "m4a",
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/webm": "webm", "audio/flac": "flac",
}


async def transcribir_audio(media_url: str, mime: str | None = None) -> str | None:
    """Descarga el audio del gateway y lo transcribe. Devuelve texto o None."""
    if not settings.feature_transcribir_audio:
        return None
    if not settings.openai_api_key:
        log.warning("whisper.sin_api_key")
        return None

    # 1) Descargar el audio (whapi requiere auth en el media).
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(media_url, headers=auth_headers())
        if r.status_code >= 400 or not r.content:
            log.warning("whisper.download_fail", status=r.status_code)
            return None
        if len(r.content) > _MAX_AUDIO_BYTES:
            log.warning("whisper.audio_too_big", size=len(r.content))
            return None
        audio = r.content
        ctype = (mime or r.headers.get("content-type") or "audio/ogg")
        ctype = ctype.split(";")[0].strip().lower()
    except Exception as e:
        log.warning("whisper.download_exc", error=str(e))
        return None

    ext = _EXT_POR_MIME.get(ctype, "ogg")
    filename = f"audio.{ext}"

    # 2) Enviar a OpenAI (multipart). `language` mejora precisión y baja latencia.
    try:
        async with httpx.AsyncClient(timeout=120) as c:
            resp = await c.post(
                _OPENAI_TRANSCRIBE_URL,
                headers={"Authorization": f"Bearer {settings.openai_api_key}"},
                data={"model": settings.whisper_model, "language": settings.whisper_idioma},
                files={"file": (filename, audio, ctype)},
            )
        if resp.status_code >= 400:
            log.error("whisper.api_fail", status=resp.status_code, body=resp.text[:300])
            return None
        texto = (resp.json().get("text") or "").strip()
        return texto or None
    except Exception as e:
        log.exception("whisper.api_exc", error=str(e))
        return None
