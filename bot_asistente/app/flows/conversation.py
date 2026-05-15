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

import base64
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.claude.client import RespuestaClaude, conversar
from app.claude.intent import clasificar
from app.config import get_settings
from app.db.models import AlertaFabio, Cliente, Pedido, Sesion
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
from app.whapi.client import auth_headers, enviar_paused, enviar_texto, enviar_typing
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
        # Imagen sin caption: típicamente es un comprobante de pago o foto de
        # producto. La pasamos a Claude con un placeholder textual para que
        # decida (vía visión + contexto de conversación).
        if msg.tipo == "imagen" and msg.media_url:
            contenido_usuario = "[El cliente envió una imagen sin texto adjunto.]"
        else:
            log.info("flow.inbound_sin_texto", cliente=cliente_numero, tipo=msg.tipo)
            return

    # 1. Sesión + historial
    sesion = await get_or_create_sesion(session, cliente_id)
    historial_db = await ultimos_mensajes(session, cliente_id, n=20)

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

    # 3. Si el cliente envió imagen, descargarla para pasarla a Claude (multimodal)
    imagen_b64: str | None = None
    imagen_mime: str | None = None
    imagen_bytes: bytes | None = None  # crudo, para reenvío al equipo (comprobante)
    if msg.tipo in ("imagen",) and msg.media_url:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(msg.media_url, headers=auth_headers())
                if r.status_code < 400:
                    raw = r.content
                    if len(raw) <= 5 * 1024 * 1024:  # max 5MB
                        imagen_b64 = base64.b64encode(raw).decode("ascii")
                        imagen_mime = msg.media_mime or "image/jpeg"
                        imagen_bytes = raw
                        log.info("flow.imagen.descargada",
                                 cliente=cliente_numero, bytes=len(raw), mime=imagen_mime)
                    else:
                        log.warning("flow.imagen.muy_grande", bytes=len(raw))
        except Exception as e:
            log.warning("flow.imagen.fail_download", error=str(e))

    # 4. Llamar a Claude con tools (incluye productos ya mostrados para dedupe)
    # Si el cliente estuvo inactivo >30 min, resetar productos_mostrados:
    # asumir nueva conversación, permitir re-enviar fotos.
    productos_mostrados_efectivos: list[str] = list(sesion.productos_mostrados or [])
    if sesion.ultima_interaccion:
        ahora = datetime.now(timezone.utc)
        # ultima_interaccion puede venir como naive o aware; normalizamos
        ultima = sesion.ultima_interaccion
        if ultima.tzinfo is None:
            ultima = ultima.replace(tzinfo=timezone.utc)
        if ahora - ultima > timedelta(minutes=30):
            log.info(
                "flow.sesion_reset_dedupe",
                cliente=cliente_numero,
                minutos_inactivo=int((ahora - ultima).total_seconds() / 60),
            )
            productos_mostrados_efectivos = []

    ctx = {
        "session": session,
        "cliente_id": cliente_id,
        "cliente_numero": cliente_numero,
        "intent": intent,
        "productos_mostrados": productos_mostrados_efectivos,
        # Imagen entrante (bytes + mime) — usada por escalar_a_equipo para
        # reenviar comprobantes de pago al equipo.
        "inbound_imagen_bytes": imagen_bytes,
        "inbound_imagen_mime": imagen_mime,
    }

    # Bloque dinámico de contexto: datos conocidos del cliente + pedido en
    # curso + si ya pagó. Se inyecta como bloque NO CACHEADO del system
    # prompt para que Claude lo tenga muy presente turno a turno y no le
    # vuelva a preguntar al cliente datos que ya dio.
    extra_system = await _construir_contexto_cliente(session, cliente_id)

    respuesta = await conversar(
        historial=historial_claude,
        mensaje_usuario=contenido_usuario,
        ctx=ctx,
        imagen_base64=imagen_b64,
        imagen_mime=imagen_mime,
        extra_system=extra_system,
    )

    # 4.1. Persistir productos mostrados en la sesión (para próximos turnos)
    nuevos_mostrados = ctx.get("productos_mostrados", [])
    if nuevos_mostrados:
        await session.execute(
            update(Sesion)
            .where(Sesion.cliente_id == cliente_id)
            .values(productos_mostrados=nuevos_mostrados)
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


async def _construir_contexto_cliente(session: AsyncSession, cliente_id: int) -> str:
    """Bloque dinámico que inyectamos al system para que Claude no le pida
    al cliente datos que ya dio (nombre, ciudad, barrio, pedido en curso,
    comprobante recibido)."""
    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        return ""

    lineas: list[str] = ["## ESTADO ACTUAL DEL CLIENTE (úsalo, NO vuelvas a preguntar lo que ya está aquí)"]
    lineas.append(f"- Número: {cliente.numero_whatsapp}")
    if cliente.nombre:
        lineas.append(f"- Nombre: {cliente.nombre}")
    if cliente.ciudad:
        lineas.append(f"- Ciudad: {cliente.ciudad}")
    if cliente.barrio:
        lineas.append(f"- Barrio: {cliente.barrio}")
    if cliente.es_mayorista:
        lineas.append("- Es mayorista (aplica precio mayorista en cotizaciones).")

    # Pedido en curso: el último pedido del cliente en los últimos 60 min,
    # estado NO 'cancelado' ni 'despachado'.
    ventana = datetime.now(timezone.utc) - timedelta(minutes=60)
    pedido = (await session.execute(
        select(Pedido).where(
            Pedido.cliente_id == cliente_id,
            Pedido.created_at >= ventana,
        ).order_by(Pedido.id.desc()).limit(1)
    )).scalar_one_or_none()
    if pedido:
        lineas.append(
            f"- **Pedido en curso #{pedido.id}**: total ${int(pedido.total):,} "
            f"({pedido.estado}). Método: {pedido.metodo_pago or 'no definido'}."
        )
        if pedido.items:
            refs = []
            for it in pedido.items[:5]:
                ref = it.get("ref") or it.get("descripcion") or "?"
                talla = it.get("talla")
                qty = it.get("cantidad", 1)
                refs.append(f"{ref}" + (f" talla {talla}" if talla else "") + (f" x{qty}" if qty != 1 else ""))
            if refs:
                lineas.append(f"  Items: {'; '.join(refs)}")

    # ¿Ya envió comprobante? Si hay alerta abierta de tipo comprobante_pago
    # en últimos 30 min, lo decimos explícito.
    ventana_comp = datetime.now(timezone.utc) - timedelta(minutes=30)
    alerta_comp = (await session.execute(
        select(AlertaFabio).where(
            AlertaFabio.cliente_id == cliente_id,
            AlertaFabio.tipo == "comprobante_pago",
            AlertaFabio.created_at >= ventana_comp,
        ).limit(1)
    )).scalar_one_or_none()
    if alerta_comp:
        lineas.append(
            "- **YA ENVIÓ COMPROBANTE DE PAGO** (escalado a equipo, alerta abierta). "
            "NO le vuelvas a mandar los datos del banco. Si pregunta por el pago, "
            "dile que el equipo está verificándolo."
        )

    if len(lineas) == 1:
        # Solo el header, sin datos útiles. No inyectamos nada.
        return ""

    lineas.append("")
    lineas.append("Si el cliente ya respondió algo arriba o en el historial, NO se lo vuelvas a preguntar.")
    return "\n".join(lineas)
