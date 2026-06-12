"""Fish Audio — voz del bot.

Dos capacidades con la misma API key:
  - TTS: generar una nota de voz con la voz colombiana del bot (para responder).
  - ASR: transcribir notas de voz entrantes a texto.

Config: FISH_AUDIO_API_KEY, FISH_AUDIO_REFERENCE_ID, FISH_AUDIO_TTS_MODEL.
Devuelven dicts/bytes estructurados; nunca lanzan al caller.
"""

from __future__ import annotations

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

_TIMEOUT = 60


def configurado() -> bool:
    return bool(settings.fish_audio_api_key)


def _headers(extra: dict | None = None) -> dict[str, str]:
    h = {"Authorization": f"Bearer {settings.fish_audio_api_key}"}
    if extra:
        h.update(extra)
    return h


async def tts(texto: str) -> dict:
    """Texto → audio (opus/ogg). Return {"ok": True, "audio": bytes, "mime": "audio/ogg"}."""
    if not configurado():
        return {"ok": False, "error": "Fish Audio no configurado"}
    if not texto.strip():
        return {"ok": False, "error": "texto vacío"}

    url = f"{settings.fish_audio_base_url}/tts"
    body = {
        "text": texto,
        "reference_id": settings.fish_audio_reference_id,
        "temperature": 0.7,
        "top_p": 0.7,
        "prosody": {"speed": 1, "volume": 0, "normalize_loudness": True},
        "format": "opus",
        "sample_rate": 48000,
        "latency": "normal",
    }
    headers = _headers({"Content-Type": "application/json", "model": settings.fish_audio_tts_model})
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.post(url, json=body, headers=headers)
        if r.status_code >= 400:
            log.warning("fish.tts.http_error", status=r.status_code, body=r.text[:200])
            return {"ok": False, "error": f"TTS {r.status_code}"}
        return {"ok": True, "audio": r.content, "mime": "audio/ogg"}
    except Exception as e:
        log.exception("fish.tts.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}


async def asr(audio_bytes: bytes, idioma: str = "es") -> dict:
    """Audio → texto. Return {"ok": True, "texto": "..."}."""
    if not configurado():
        return {"ok": False, "pending": True, "razon": "Fish Audio no configurado"}

    url = f"{settings.fish_audio_base_url}/asr"
    # Fish Audio ASR acepta el audio como multipart (campo `audio`) + `language`.
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            files = {"audio": ("audio.ogg", audio_bytes, "audio/ogg")}
            data = {"language": idioma, "ignore_timestamps": "true"}
            r = await c.post(url, headers=_headers(), files=files, data=data)
        if r.status_code >= 400:
            log.warning("fish.asr.http_error", status=r.status_code, body=r.text[:200])
            return {"ok": False, "error": f"ASR {r.status_code}"}
        payload = r.json()
        texto = (payload.get("text") or "").strip()
        if not texto:
            return {"ok": False, "error": "transcripción vacía"}
        return {"ok": True, "texto": texto}
    except Exception as e:
        log.exception("fish.asr.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}
