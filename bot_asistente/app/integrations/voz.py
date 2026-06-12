"""Transcripción de notas de voz de WhatsApp → texto.

El bot recibe muchos audios. Cuando llega un mensaje tipo 'audio', el flujo
descarga los bytes y llama aquí para obtener el texto y procesarlo como si
fuera un mensaje escrito.

Backend genérico vía HTTP (config: VOZ_API_URL, VOZ_API_KEY). Pensado para
apuntar a un endpoint compatible con OpenAI Whisper (`/v1/audio/transcriptions`,
multipart con campo `file`) o a un proxy propio de Google STT. Si no está
configurado, devuelve {"ok": False, "pending": True} y el flujo hace fallback
(pedir el mensaje por texto), nunca rompe.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

_TIMEOUT = 60


async def transcribir(audio_bytes: bytes, mime: str = "audio/ogg") -> dict:
    """Devuelve {"ok": True, "texto": "..."} o {"ok": False, "pending"/"error": ...}.

    Prioridad: Fish Audio ASR (si hay API key) → backend STT genérico → pendiente.
    """
    if not settings.feature_transcripcion_voz:
        return {"ok": False, "pending": True, "razon": "transcripción deshabilitada"}

    # 1) Fish Audio ASR (misma key que el TTS)
    from app.integrations import fish_audio
    if fish_audio.configurado():
        res = await fish_audio.asr(audio_bytes, idioma=settings.voz_idioma)
        if res.get("ok") or res.get("error"):
            return res
        # si devolvió pending (no configurado), cae al genérico

    # 2) Backend STT genérico (Whisper-like) si está configurado
    if not settings.voz_api_url:
        return {"ok": False, "pending": True, "razon": "sin backend de transcripción (Fish Audio ni VOZ_API_URL)"}

    ext = "ogg"
    if "mpeg" in mime or "mp3" in mime:
        ext = "mp3"
    elif "wav" in mime:
        ext = "wav"
    elif "mp4" in mime or "m4a" in mime:
        ext = "m4a"

    headers = {}
    if settings.voz_api_key:
        headers["Authorization"] = f"Bearer {settings.voz_api_key}"

    files = {"file": (f"audio.{ext}", audio_bytes, mime)}
    data = {"model": "whisper-1", "language": settings.voz_idioma}
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(settings.voz_api_url, headers=headers, files=files, data=data)
        if r.status_code >= 400:
            log.warning("voz.http_error", status=r.status_code, body=r.text[:200])
            return {"ok": False, "error": f"STT {r.status_code}"}
        payload = r.json()
        texto = (payload.get("text") or payload.get("transcript") or "").strip()
        if not texto:
            return {"ok": False, "error": "transcripción vacía"}
        return {"ok": True, "texto": texto}
    except Exception as e:
        log.exception("voz.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}
