"""
Cliente Anthropic — wrapper que:
  - Mantiene prompt caching en los bloques fijos (identidad + empresa + guía ventas).
  - Implementa el loop de tool use (Claude pide tool → ejecutamos → le devolvemos → ...).
  - Cuenta tokens y costo aproximado por request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from anthropic import AsyncAnthropic

from app.claude.prompts import construir_system_prompt
from app.claude.tools import TOOL_DEFINITIONS, ejecutar_tool
from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

_client = AsyncAnthropic(api_key=settings.anthropic_api_key)


# Precios aproximados de Sonnet 4.6 (USD por 1M tokens) — para tracking interno
# (ajusta cuando cambie pricing)
PRECIO_INPUT_USD = Decimal("3.00") / Decimal("1000000")
PRECIO_OUTPUT_USD = Decimal("15.00") / Decimal("1000000")
PRECIO_CACHE_READ_USD = Decimal("0.30") / Decimal("1000000")
PRECIO_CACHE_WRITE_USD = Decimal("3.75") / Decimal("1000000")


@dataclass
class RespuestaClaude:
    texto: str
    tokens_input: int = 0
    tokens_output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    costo_usd: Decimal = Decimal("0")
    modelo: str = ""
    tools_usadas: list[str] = field(default_factory=list)
    raw_messages: list[dict] = field(default_factory=list)


def _calcular_costo(usage: Any) -> Decimal:
    """Estimación de costo USD a partir del usage de Anthropic."""
    if usage is None:
        return Decimal("0")
    inp = getattr(usage, "input_tokens", 0) or 0
    out = getattr(usage, "output_tokens", 0) or 0
    cr = getattr(usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(usage, "cache_creation_input_tokens", 0) or 0
    return (
        Decimal(inp) * PRECIO_INPUT_USD
        + Decimal(out) * PRECIO_OUTPUT_USD
        + Decimal(cr) * PRECIO_CACHE_READ_USD
        + Decimal(cw) * PRECIO_CACHE_WRITE_USD
    )


async def conversar(
    *,
    historial: list[dict],
    mensaje_usuario: str,
    ctx: dict,
    max_loops: int = 5,
    imagen_base64: str | None = None,
    imagen_mime: str | None = None,
    extra_system: str | None = None,
) -> RespuestaClaude:
    """
    Conversación con Claude usando tool use loop.

    `historial`: lista de mensajes previos en formato Anthropic
    `mensaje_usuario`: el último texto del cliente
    `imagen_base64` (opcional): si el cliente envió una imagen, se la pasamos a Claude
        como content block multimodal. Permite al bot "ver" la foto.
    `imagen_mime`: tipo MIME de la imagen (image/jpeg, image/png, image/webp)
    `ctx`: contexto que se pasa a los handlers de tools (session DB, cliente, etc).
    """
    # Construir el último user message — puede tener imagen + texto
    if imagen_base64:
        last_msg_content: list[dict] | str = [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": imagen_mime or "image/jpeg",
                    "data": imagen_base64,
                },
            },
            {
                "type": "text",
                "text": (
                    f"(Imagen enviada por el cliente. Mira la foto para entender qué producto/comprobante muestra.)\n\n"
                    f"Texto del cliente: {mensaje_usuario}" if mensaje_usuario.strip()
                    else "Texto del cliente: (sin texto, solo imagen)"
                ),
            },
        ]
    else:
        last_msg_content = mensaje_usuario

    messages = list(historial) + [{"role": "user", "content": last_msg_content}]
    system = construir_system_prompt()
    if extra_system:
        # Bloque dinámico (datos del cliente, pedido en curso). Va al final y
        # SIN cache_control — cambia turno a turno y rompería el cache del resto.
        system = list(system) + [{"type": "text", "text": extra_system}]
    tools_usadas: list[str] = []
    respuesta = RespuestaClaude(texto="", modelo=settings.claude_model_principal)

    for ronda in range(max_loops):
        try:
            resp = await _client.messages.create(
                model=settings.claude_model_principal,
                max_tokens=settings.claude_max_tokens_output,
                system=system,
                tools=TOOL_DEFINITIONS,
                messages=messages,
            )
        except Exception as e:
            log.exception("claude.api_fail", error=str(e), ronda=ronda)
            respuesta.texto = "Disculpa, tuvimos un problema técnico. Dame un momento y te respondo."
            return respuesta

        # Acumular costos
        usage = getattr(resp, "usage", None)
        respuesta.tokens_input += getattr(usage, "input_tokens", 0) or 0
        respuesta.tokens_output += getattr(usage, "output_tokens", 0) or 0
        respuesta.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        respuesta.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        respuesta.costo_usd += _calcular_costo(usage)

        # Procesar blocks de la respuesta
        text_chunks: list[str] = []
        tool_uses: list[dict] = []

        for block in resp.content:
            btype = getattr(block, "type", None)
            if btype == "text":
                text_chunks.append(block.text)
            elif btype == "tool_use":
                tool_uses.append({
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })

        stop_reason = getattr(resp, "stop_reason", None)

        # Si Claude terminó (sin más tools que ejecutar) → devolver texto
        if stop_reason != "tool_use" or not tool_uses:
            respuesta.texto = "\n".join(t.strip() for t in text_chunks if t and t.strip())
            respuesta.tools_usadas = tools_usadas
            return respuesta

        # Hay tools que ejecutar — agregamos el assistant message tal cual
        assistant_content: list[dict] = []
        for block in resp.content:
            if getattr(block, "type", None) == "text":
                assistant_content.append({"type": "text", "text": block.text})
            elif getattr(block, "type", None) == "tool_use":
                assistant_content.append({
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                })
        messages.append({"role": "assistant", "content": assistant_content})

        # Ejecutar cada tool y armar el user message con tool_result
        tool_results: list[dict] = []
        for tu in tool_uses:
            log.info("claude.tool_call", tool=tu["name"], input=tu["input"])
            tools_usadas.append(tu["name"])
            result = await ejecutar_tool(tu["name"], tu["input"], ctx)
            tool_results.append({
                "type": "tool_result",
                "tool_use_id": tu["id"],
                "content": _stringify(result),
            })

        messages.append({"role": "user", "content": tool_results})
        # loop sigue para que Claude responda con los resultados

    # Si llegamos aquí, agotamos los loops
    log.warning("claude.max_loops_alcanzado", loops=max_loops)
    respuesta.texto = (
        respuesta.texto
        or "Dame un momento, estoy verificando esa información con el equipo."
    )
    respuesta.tools_usadas = tools_usadas
    return respuesta


def _stringify(obj: Any) -> str:
    """Convierte el resultado de un tool a string (Anthropic acepta string o list)."""
    import json
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return str(obj)
