"""
Wrapper de cliente Anthropic con fallback Dario → API directa.

Por qué existe: la facturación de Anthropic tiene fricciones; usamos Dario
(proxy local que enruta vía la suscripción Claude Max) como provider primario,
y dejamos la API directa como red de seguridad si Dario está caído, agotó
rate, o devuelve overage 503.

Settings:
  - claude_provider="fallback"  → Dario primario + retry con API directa.
  - claude_provider="dario"     → solo Dario.
  - claude_provider="direct"    → solo API directa.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anthropic
from anthropic import AsyncAnthropic

from app.config import get_settings
from app.logging_setup import log


_FALLBACK_STATUS = {429, 500, 502, 503, 504}


def _build_dario_client() -> AsyncAnthropic:
    s = get_settings()
    return AsyncAnthropic(base_url=s.dario_base_url, api_key=s.dario_api_key)


def _build_direct_client() -> AsyncAnthropic | None:
    s = get_settings()
    if not s.anthropic_api_key:
        return None
    return AsyncAnthropic(api_key=s.anthropic_api_key)


class _MessagesProxy:
    """Expone .create() con la misma firma que AsyncAnthropic.messages.create()."""

    def __init__(self, parent: "AnthropicWithFallback"):
        self._parent = parent

    async def create(self, **kwargs: Any) -> Any:
        return await self._parent._invoke("messages.create", **kwargs)


class AnthropicWithFallback:
    """Drop-in replacement de AsyncAnthropic con política de fallback configurable."""

    def __init__(self) -> None:
        s = get_settings()
        self._provider = s.claude_provider
        self._primary = _build_dario_client() if self._provider in ("dario", "fallback") else None
        self._secondary = _build_direct_client() if self._provider in ("direct", "fallback") else None
        if self._provider == "direct":
            self._primary = self._secondary
            self._secondary = None
        self.messages = _MessagesProxy(self)

    async def _invoke(self, _path: str, **kwargs: Any) -> Any:
        if self._primary is None:
            raise RuntimeError("No hay cliente Anthropic configurado (revisa claude_provider y keys)")

        try:
            return await self._primary.messages.create(**kwargs)
        except Exception as e:
            if self._secondary is None:
                raise
            if not _is_fallback_eligible(e):
                raise
            log.warning(
                "anthropic.dario_failed_fallback",
                error_type=type(e).__name__,
                error=str(e)[:200],
            )

        # Dame un respiro antes de retry (evita cascada inmediata)
        await asyncio.sleep(0.5)
        return await self._secondary.messages.create(**kwargs)


def _is_fallback_eligible(exc: BaseException) -> bool:
    """¿Vale la pena reintentar con API directa, o el error también afectaría allá?"""
    # Conexión: Dario no responde
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    # Rate / overage / 5xx del proxy o del upstream visto a través del proxy
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code in _FALLBACK_STATUS
    # 401/403/400 son del cliente — no se arreglan reintentando
    return False


_singleton: AnthropicWithFallback | None = None


def get_anthropic_client() -> AnthropicWithFallback:
    global _singleton
    if _singleton is None:
        _singleton = AnthropicWithFallback()
        s = get_settings()
        log.info(
            "anthropic.client_init",
            provider=s.claude_provider,
            dario_base_url=s.dario_base_url if s.claude_provider in ("dario", "fallback") else None,
            direct_key_present=bool(s.anthropic_api_key),
        )
    return _singleton
