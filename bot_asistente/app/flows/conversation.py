"""
Orquestador principal de una conversación.

Recibe un MensajeWhapi inbound + sesión DB y produce la respuesta:
  1. Construye historial reciente
  2. Clasifica intent con Haiku
  3. Llama a Claude (Sonnet) con tool use
  4. Valida la respuesta contra reglas inquebrantables
  5. Si no pasa, reescribe con feedback (máx 1 reintento)
  6. Envía a whapi y persiste todo
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.claude.client import RespuestaClaude, conversar
from app.claude.intent import clasificar
from app.config import get_settings
from app.db.repos import (
    get_or_create_sesion,
    guardar_conversacion,
    ultimos_mensajes,
)
from app.logging_setup import log
from app.utils.humanizer import (
    dentro_horario,
    proxima_hora_apertura,
    puede_enviar,
    sleep_humano,
)
from app.validators.output_rules import (
    construir_feedback_para_regenerar,
    stripear_emojis,
    validar,
)
from app.whapi.client import enviar_paused, enviar_texto, enviar_typing
from app.whapi.parser import MensajeWhapi

settings = get_settings()


async def procesar_mensaje_inbound(
    *,
    session: AsyncSession,
    cliente_id: int,
    cliente_numero: str,
    msg: MensajeWhapi,
) -> None:
    """
    Procesa un mensaje inbound (ya persistido por el webhook) y manda respuesta.

    No devuelve nada — todo el efecto es persistencia + envío de mensajes.
    """
    contenido_usuario = msg.texto or ""
    if not contenido_usuario.strip():
        # Mensaje sin texto procesable (ej. sticker sin caption). Por ahora ignoramos.
        log.info("flow.inbound_sin_texto", cliente=cliente_numero, tipo=msg.tipo)
        return

    # 1. Sesión + historial
    await get_or_create_sesion(session, cliente_id)
    historial_db = await ultimos_mensajes(session, cliente_id, n=10)

    historial_claude: list[dict] = []
    for h in historial_db[:-1]:  # excluimos el último (que es el actual inbound)
        if h.direccion == "inbound" and h.contenido:
            historial_claude.append({"role": "user", "content": h.contenido})
        elif h.direccion in ("outbound", "humano") and h.contenido:
            historial_claude.append({"role": "assistant", "content": h.contenido})

    # 2. Clasificar intent (paralelo opcional — por ahora secuencial)
    contexto_para_intent = [h.contenido or "" for h in historial_db[-3:] if h.contenido]
    intent = await clasificar(contenido_usuario, contexto_reciente=contexto_para_intent)
    log.info("flow.intent", cliente=cliente_numero, intent=intent)

    # Si es spam, ignoramos
    if intent == "spam":
        log.info("flow.spam_ignorado", cliente=cliente_numero)
        return

    # 3. Llamar a Claude con tools
    ctx = {
        "session": session,
        "cliente_id": cliente_id,
        "cliente_numero": cliente_numero,
        "intent": intent,
    }
    respuesta = await conversar(
        historial=historial_claude,
        mensaje_usuario=contenido_usuario,
        ctx=ctx,
    )

    # 4. Validar — si falla, reintentamos 1 vez
    texto_final = await _validar_y_reescribir_si_necesario(
        respuesta=respuesta,
        historial=historial_claude,
        mensaje_usuario=contenido_usuario,
        ctx=ctx,
    )

    if not texto_final.strip():
        log.warning("flow.respuesta_vacia", cliente=cliente_numero)
        return

    # 4.5. Strip de emojis (post-proceso barato — política del negocio: cero emojis)
    texto_final = stripear_emojis(texto_final)
    if not texto_final.strip():
        log.warning("flow.respuesta_vacia_post_strip", cliente=cliente_numero)
        return

    # 4.6. HUMANIZACIÓN — anti-detección de WhatsApp
    if settings.feature_humanizacion:
        # Ventana horaria
        if not dentro_horario():
            apertura = proxima_hora_apertura()
            log.info(
                "flow.fuera_de_horario",
                cliente=cliente_numero,
                proxima_apertura=apertura.isoformat(),
            )
            # No enviamos automático fuera de horario. Persistimos pendiente.
            await guardar_conversacion(
                session,
                cliente_id=cliente_id,
                direccion="outbound",
                tipo="texto",
                contenido=texto_final,
                intent=intent,
                tokens_input=respuesta.tokens_input,
                tokens_output=respuesta.tokens_output,
                cache_read_tokens=respuesta.cache_read,
                cache_create_tokens=respuesta.cache_write,
                modelo=respuesta.modelo,
                metadata={
                    "tools_usadas": respuesta.tools_usadas,
                    "costo_usd": str(respuesta.costo_usd),
                    "no_enviado": True,
                    "razon": "fuera_de_horario",
                    "programado_para": apertura.isoformat(),
                },
            )
            return

        # Rate limit global
        ok, enviados, limite = await puede_enviar(session)
        if not ok:
            log.warning(
                "flow.rate_limit",
                cliente=cliente_numero,
                enviados_ultima_hora=enviados,
                limite=limite,
            )
            await guardar_conversacion(
                session,
                cliente_id=cliente_id,
                direccion="outbound",
                tipo="texto",
                contenido=texto_final,
                intent=intent,
                modelo=respuesta.modelo,
                metadata={"no_enviado": True, "razon": "rate_limit"},
            )
            return

        # Typing indicator + delay realista
        if settings.humanization_typing_indicator:
            await enviar_typing(cliente_numero)
        segundos = await sleep_humano(texto_final)
        log.debug("flow.humanizacion.sleep", cliente=cliente_numero, segundos=round(segundos, 2))

    # 5. Enviar a whapi
    try:
        await enviar_texto(cliente_numero, texto_final)
        # Limpia el typing indicator (whapi expira solo, esto es solo por buena onda)
        if settings.feature_humanizacion and settings.humanization_typing_indicator:
            await enviar_paused(cliente_numero)
    except Exception as e:
        log.exception("flow.enviar_whapi_fail", error=str(e))
        return

    # 6. Persistir outbound
    await guardar_conversacion(
        session,
        cliente_id=cliente_id,
        direccion="outbound",
        tipo="texto",
        contenido=texto_final,
        intent=intent,
        tokens_input=respuesta.tokens_input,
        tokens_output=respuesta.tokens_output,
        cache_read_tokens=respuesta.cache_read,
        cache_create_tokens=respuesta.cache_write,
        modelo=respuesta.modelo,
        metadata={"tools_usadas": respuesta.tools_usadas, "costo_usd": str(respuesta.costo_usd)},
    )
    log.info(
        "flow.respondido",
        cliente=cliente_numero,
        tokens_in=respuesta.tokens_input,
        tokens_out=respuesta.tokens_output,
        cache_read=respuesta.cache_read,
        costo_usd=str(respuesta.costo_usd),
        tools=respuesta.tools_usadas,
    )


async def _validar_y_reescribir_si_necesario(
    *,
    respuesta: RespuestaClaude,
    historial: list[dict],
    mensaje_usuario: str,
    ctx: dict,
) -> str:
    """Aplica validadores. Si falla con severidad crítica, pide reescritura una vez."""
    texto = respuesta.texto.strip()
    if not texto:
        return ""

    issues = validar(texto)
    criticos = [i for i in issues if i.severity == "critico"]
    if not criticos:
        if issues:
            log.warning("flow.validador.warnings", issues=[i.rule for i in issues])
        return texto

    log.warning("flow.validador.criticos", issues=[i.rule for i in criticos])

    # Reescritura: añadimos un mensaje user con el feedback y le pedimos a Claude reescribir
    feedback = construir_feedback_para_regenerar(criticos)
    historial_extra = list(historial) + [
        {"role": "user", "content": mensaje_usuario},
        {"role": "assistant", "content": texto},
    ]
    try:
        retry = await conversar(
            historial=historial_extra,
            mensaje_usuario=feedback,
            ctx=ctx,
            max_loops=2,
        )
    except Exception as e:
        log.exception("flow.reescritura_fail", error=str(e))
        return texto  # mejor enviar lo original que nada

    nuevo_texto = retry.texto.strip()
    if not nuevo_texto:
        return texto

    # Validamos otra vez. Si vuelve a fallar crítico, NO enviamos y escalamos.
    issues_2 = [i for i in validar(nuevo_texto) if i.severity == "critico"]
    if issues_2:
        log.error("flow.validador.fallo_doble", issues=[i.rule for i in issues_2])
        return ""  # mejor silencio que filtrar info crítica

    # Acumular costos del retry en la respuesta original (para tracking)
    respuesta.tokens_input += retry.tokens_input
    respuesta.tokens_output += retry.tokens_output
    respuesta.cache_read += retry.cache_read
    respuesta.cache_write += retry.cache_write
    respuesta.costo_usd += retry.costo_usd
    respuesta.tools_usadas.extend(retry.tools_usadas)

    return nuevo_texto
