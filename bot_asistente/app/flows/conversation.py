"""
Flujo PROSPECTO del bot (cara comercial — Dairo).

Quien escribe es un número desconocido (no está en la whitelist): un prospecto
que llegó por publicidad. El bot entiende su negocio y, si hay encaje, agenda
una reunión de diagnóstico vía Cal.com.

Pipeline:
  1. Construye historial reciente
  2. Clasifica intent (Haiku)
  3. Llama a Claude (tool use loop) con las tools de prospecto
  4. Humaniza (typing + delay anti-detección Meta) y envía vía whapi
  5. Persiste todo

Devuelve el `outbox` (avisos al equipo) para drenar DESPUÉS del commit
(lo hace _procesar_async en main.py).
"""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.claude.client import conversar
from app.claude.intent import clasificar
from app.config import get_settings
from app.db.models import Cita, Cliente, Conversacion, Prospecto
from app.db.repos import guardar_conversacion, ultimos_mensajes
from app.logging_setup import log
from app.utils.humanizer import (
    dentro_horario,
    proxima_hora_apertura,
    puede_enviar,
    sleep_humano,
)
from app.whapi.client import (
    auth_headers,
    enviar_nota_voz_bytes,
    enviar_paused,
    enviar_texto,
    enviar_typing,
    set_token as set_whapi_token,
)
from app.whapi.parser import MensajeWhapi
from app.identidades import Identidad, dairo as _identidad_default

settings = get_settings()


async def procesar_mensaje_inbound(
    *,
    session: AsyncSession,
    cliente_id: int,
    cliente_numero: str,
    msg: MensajeWhapi,
    identidad: Identidad | None = None,
) -> list[dict]:
    """Procesa un inbound de un prospecto (ya persistido por el webhook) y responde."""
    ident = identidad or _identidad_default()
    set_whapi_token(ident.token)   # asegurar el token correcto en todo I/O whapi de esta tarea
    contenido_usuario = msg.texto or ""
    es_audio = msg.tipo == "audio"

    # Nota de voz → transcribir y tratarla como texto
    if not contenido_usuario.strip() and es_audio and msg.media_url:
        contenido_usuario = await _transcribir_nota_voz(msg)

    if not contenido_usuario.strip():
        if msg.tipo == "imagen" and msg.media_url:
            contenido_usuario = "[El prospecto envió una imagen sin texto.]"
        else:
            log.info("flow.inbound_sin_texto", cliente=cliente_numero, tipo=msg.tipo)
            return []

    # Mensaje citado (reply) → inyectar contexto del mensaje citado
    if msg.quoted_message_id:
        quoted_preview = msg.quoted_content or ""
        quoted_db = (await session.execute(
            select(Conversacion).where(
                Conversacion.whapi_message_id == msg.quoted_message_id
            ).limit(1)
        )).scalar_one_or_none()
        if quoted_db and quoted_db.contenido:
            quoted_preview = quoted_db.contenido
        if quoted_preview:
            contenido_usuario = (
                f"[El prospecto respondió/citó este mensaje anterior tuyo:\n"
                f"\"{quoted_preview[:500]}\"]\n\n"
                f"Su respuesta: {contenido_usuario}"
            )

    # 1. Historial (hasta 30 msgs / 48h)
    historial_db = await ultimos_mensajes(session, cliente_id, n=30, horas_max=48)
    ahora_utc = datetime.now(timezone.utc)
    umbral_gap = ahora_utc - timedelta(hours=12)
    historial_claude: list[dict] = []
    separador = False
    for h in historial_db[:-1]:
        ts = h.timestamp
        if ts and ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if not separador and ts and ts >= umbral_gap and historial_claude:
            historial_claude.append({
                "role": "user",
                "content": "[— Nota interna: pasaron varias horas. Retoma con coherencia usando lo que ya sabes del prospecto (nombre, negocio, lo conversado). —]",
            })
            separador = True
        if h.direccion == "inbound" and h.contenido:
            historial_claude.append({"role": "user", "content": h.contenido})
        elif h.direccion in ("outbound", "humano") and h.contenido:
            historial_claude.append({"role": "assistant", "content": h.contenido})

    # 2. Intent
    contexto_intent = [h.contenido or "" for h in historial_db[-3:] if h.contenido]
    intent = await clasificar(contenido_usuario, contexto_reciente=contexto_intent)
    log.info("flow.intent", cliente=cliente_numero, intent=intent)
    if intent == "spam":
        log.info("flow.spam_ignorado", cliente=cliente_numero)
        return []

    # 3. Imagen entrante → multimodal
    imagen_b64: str | None = None
    imagen_mime: str | None = None
    if msg.tipo == "imagen" and msg.media_url:
        try:
            async with httpx.AsyncClient(timeout=30) as c:
                r = await c.get(msg.media_url, headers=auth_headers())
                if r.status_code < 400 and len(r.content) <= 5 * 1024 * 1024:
                    imagen_b64 = base64.b64encode(r.content).decode("ascii")
                    imagen_mime = msg.media_mime or "image/jpeg"
        except Exception as e:
            log.warning("flow.imagen.fail_download", error=str(e))

    # 4. Contexto dinámico del prospecto + tool use loop
    ctx = {
        "session": session,
        "cliente_id": cliente_id,
        "cliente_numero": cliente_numero,
        "intent": intent,
    }
    extra_system = await _construir_contexto_prospecto(session, cliente_id, cliente_numero)

    respuesta = await conversar(
        historial=historial_claude,
        mensaje_usuario=contenido_usuario,
        ctx=ctx,
        imagen_base64=imagen_b64,
        imagen_mime=imagen_mime,
        extra_system=extra_system,
        persona_file=ident.persona_prompt_file,
    )

    texto_final = (respuesta.texto or "").strip()
    if not texto_final:
        log.warning("flow.respuesta_vacia", cliente=cliente_numero)
        return ctx.get("outbox", [])

    # Race-condition guard: si mientras procesábamos llegó OTRO mensaje del
    # mismo cliente, abortar este flow (no enviar) — el siguiente flow va a
    # procesar todo junto con contexto completo. Esto evita que el bot
    # responda 3 veces a un cliente que mandó 3 mensajes seguidos.
    from sqlalchemy import text as _sa_text
    msg_actual_id = msg.id
    nuevo_inbound = (await session.execute(_sa_text("""
        SELECT 1 FROM conversaciones
        WHERE cliente_id = :cid AND direccion = 'inbound'
          AND whapi_message_id IS NOT NULL
          AND whapi_message_id != :curr
          AND timestamp > (SELECT timestamp FROM conversaciones WHERE whapi_message_id = :curr LIMIT 1)
        LIMIT 1
    """), {"cid": cliente_id, "curr": msg_actual_id})).first()
    if nuevo_inbound:
        log.info(
            "flow.abortado_mensaje_mas_nuevo",
            cliente=cliente_numero, msg_id=msg_actual_id,
        )
        return ctx.get("outbox", [])

    # 5. Humanización (anti-detección Meta) — clave en un número con tráfico de ads
    if settings.feature_humanizacion:
        # Modo 24/7: el bot responde a cualquier hora. Configurable vía env
        # FEATURE_RESPONDER_24_7=true. Si está en false, respeta horario laboral
        # (8 am - 10 pm) y deja los mensajes pendientes para que el cron
        # `enviar_pendientes_apertura` los dispare a las 8 am del día siguiente.
        if not settings.feature_responder_24_7 and not dentro_horario():
            apertura = proxima_hora_apertura()
            log.info("flow.fuera_de_horario", cliente=cliente_numero, proxima=apertura.isoformat())
            await guardar_conversacion(
                session, cliente_id=cliente_id, direccion="outbound", tipo="texto",
                contenido=texto_final, intent=intent, modelo=respuesta.modelo,
                tokens_input=respuesta.tokens_input, tokens_output=respuesta.tokens_output,
                cache_read_tokens=respuesta.cache_read, cache_create_tokens=respuesta.cache_write,
                metadata={"no_enviado": True, "razon": "fuera_de_horario", "programado_para": apertura.isoformat()},
            )
            return ctx.get("outbox", [])

        ok, enviados, limite = await puede_enviar(session)
        if not ok:
            log.warning("flow.rate_limit", cliente=cliente_numero, enviados=enviados, limite=limite)
            await guardar_conversacion(
                session, cliente_id=cliente_id, direccion="outbound", tipo="texto",
                contenido=texto_final, intent=intent, modelo=respuesta.modelo,
                metadata={"no_enviado": True, "razon": "rate_limit"},
            )
            return ctx.get("outbox", [])

        if settings.humanization_typing_indicator:
            await enviar_typing(cliente_numero)
        await sleep_humano(texto_final)

    # 6. Enviar — si el prospecto mandó nota de voz, responder con voz (TTS)
    enviado_como_voz = False
    if es_audio and settings.feature_responder_voz:
        try:
            from app.integrations import fish_audio
            res_tts = await fish_audio.tts(texto_final)
            if res_tts.get("ok"):
                await enviar_nota_voz_bytes(cliente_numero, res_tts["audio"], mime=res_tts.get("mime", "audio/ogg"))
                enviado_como_voz = True
        except Exception as e:
            log.warning("flow.tts_voz_fail", error=str(e))  # cae a texto

    # Última red de seguridad: si el admin desactivó el bot DURANTE el
    # procesamiento, no enviar la respuesta al prospecto.
    try:
        from app.main import _bot_global_pausado
        if await _bot_global_pausado():
            log.warning("flow.envio_abortado_bot_pausado", cliente=cliente_numero)
            return ctx.get("outbox", [])
    except Exception:
        pass

    try:
        if not enviado_como_voz:
            await enviar_texto(cliente_numero, texto_final)
        if settings.feature_humanizacion and settings.humanization_typing_indicator:
            await enviar_paused(cliente_numero)
    except Exception as e:
        log.exception("flow.enviar_whapi_fail", error=str(e))
        return ctx.get("outbox", [])

    # 7. Persistir outbound
    await guardar_conversacion(
        session, cliente_id=cliente_id, direccion="outbound", tipo="texto",
        contenido=texto_final, intent=intent,
        tokens_input=respuesta.tokens_input, tokens_output=respuesta.tokens_output,
        cache_read_tokens=respuesta.cache_read, cache_create_tokens=respuesta.cache_write,
        modelo=respuesta.modelo,
        metadata={"tools_usadas": respuesta.tools_usadas, "costo_usd": str(respuesta.costo_usd),
                  "enviado_como_voz": enviado_como_voz},
    )
    log.info(
        "flow.respondido", cliente=cliente_numero,
        tokens_in=respuesta.tokens_input, tokens_out=respuesta.tokens_output,
        cache_read=respuesta.cache_read, costo_usd=str(respuesta.costo_usd),
        tools=respuesta.tools_usadas,
    )
    return ctx.get("outbox", [])


async def _transcribir_nota_voz(msg: MensajeWhapi) -> str:
    """Descarga el audio de whapi y lo transcribe. Devuelve el texto, o un
    placeholder si la transcripción no está disponible (no rompe el flujo)."""
    from app.integrations import voz
    try:
        async with httpx.AsyncClient(timeout=40) as c:
            r = await c.get(msg.media_url, headers=auth_headers())
        if r.status_code >= 400 or len(r.content) > 16 * 1024 * 1024:
            return ""
        res = await voz.transcribir(r.content, mime=msg.media_mime or "audio/ogg")
    except Exception as e:
        log.warning("flow.voz.fail", error=str(e))
        return ""
    if res.get("ok"):
        log.info("flow.voz.transcrita", chars=len(res["texto"]))
        return res["texto"]
    if res.get("pending"):
        # Transcripción no configurada: el bot pide el mensaje por texto.
        return "[El prospecto envió una nota de voz, pero no puedo escucharla. Pídele amablemente que te escriba el mensaje.]"
    return ""


async def _construir_contexto_prospecto(
    session: AsyncSession, cliente_id: int, cliente_numero: str
) -> str:
    """Bloque dinámico (no cacheado) con lo que ya sabemos del prospecto y su cita,
    para que el bot no vuelva a preguntar lo mismo."""
    from zoneinfo import ZoneInfo as _ZI

    _DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    _MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    _ahora = datetime.now(_ZI(settings.tz))
    _manana = _ahora + timedelta(days=1)
    _pasado = _ahora + timedelta(days=2)

    lineas: list[str] = [
        "## FECHA Y HORA ACTUAL — ÚSALA SIEMPRE (nunca inventes fechas)",
        f"- HOY es **{_DIAS[_ahora.weekday()]} {_ahora.day} de {_MESES[_ahora.month-1]} de {_ahora.year}** "
        f"({_ahora.strftime('%Y-%m-%d')}).",
        f"- Hora actual: **{_ahora.strftime('%H:%M')}** ({settings.tz}).",
        f"- MAÑANA es {_DIAS[_manana.weekday()]} {_manana.day} de {_MESES[_manana.month-1]} "
        f"({_manana.strftime('%Y-%m-%d')}).",
        f"- PASADO MAÑANA es {_DIAS[_pasado.weekday()]} {_pasado.day} de {_MESES[_pasado.month-1]} "
        f"({_pasado.strftime('%Y-%m-%d')}).",
        "- Cuando ofrezcas horarios, SIEMPRE calcula el día de la semana a partir "
        "de esta fecha. No digas 'mañana lunes' si mañana es otro día.",
        "",
        "## ESTADO DEL PROSPECTO (úsalo, NO vuelvas a preguntar lo que ya está aquí)",
    ]
    lineas.append(f"- Número: {cliente_numero}")

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if cliente and cliente.nombre:
        lineas.append(f"- Nombre: {cliente.nombre}")
    if cliente and cliente.email:
        lineas.append(f"- Email: {cliente.email}")

    pros = (await session.execute(
        select(Prospecto).where(Prospecto.cliente_id == cliente_id)
    )).scalar_one_or_none()
    if pros:
        if pros.negocio:
            lineas.append(f"- Negocio: {pros.negocio}")
        if pros.sector:
            lineas.append(f"- Sector: {pros.sector}")
        if pros.ciudad:
            lineas.append(f"- Ciudad: {pros.ciudad}")
        if pros.necesidad:
            lineas.append(f"- Necesidad: {pros.necesidad}")
        if pros.ya_pauta is not None:
            lineas.append(f"- ¿Ya hace pauta?: {'sí' if pros.ya_pauta else 'no'}")
        if pros.tiene_web is not None:
            lineas.append(f"- ¿Tiene web?: {'sí' if pros.tiene_web else 'no'}")
        # Calificación de fit — claves para decidir si OFRECER o NO la reunión.
        if pros.tipo_organizacion:
            lineas.append(f"- Tipo de organización: {pros.tipo_organizacion}")
        if pros.es_empresa is not None:
            lineas.append(f"- ¿Es empresa / negocio operando?: {'sí' if pros.es_empresa else 'NO (sin estructura)'}")
        if pros.presupuesto_mensual_cop is not None:
            lineas.append(f"- Presupuesto mensual declarado: ${pros.presupuesto_mensual_cop:,} COP")
        lineas.append(f"- Estado del funnel: {pros.estado}")

        # Diagnóstico de fit (visible para el modelo, NO para el prospecto).
        falta_pres = pros.presupuesto_mensual_cop is None
        falta_org = pros.es_empresa is None and not pros.tipo_organizacion
        bajo_pres = (pros.presupuesto_mensual_cop is not None
                     and pros.presupuesto_mensual_cop < 2_000_000)
        no_empresa = (pros.es_empresa is False
                      or pros.tipo_organizacion == "persona_natural")
        if falta_pres or falta_org:
            lineas.append(
                "- ⚠️ FIT INDEFINIDO: aún no sabemos presupuesto o tipo de organización. "
                "Antes de OFRECER la reunión, consíguelo de forma natural (no como interrogatorio)."
            )
        elif bajo_pres or no_empresa:
            lineas.append(
                "- 🚫 NO FIT: el prospecto NO cumple el mínimo (≥ $2.000.000 COP/mes y "
                "negocio operando). NO ofrezcas reunión. Cierra con honestidad — "
                "ver instrucciones en el playbook."
            )
        else:
            lineas.append(
                "- ✅ FIT OK: tiene estructura y presupuesto. Puedes ofrecer la reunión."
            )

    # Cita activa
    cita = (await session.execute(
        select(Cita).where(
            Cita.cliente_id == cliente_id,
            Cita.estado.in_(["agendada", "reprogramada"]),
        ).order_by(Cita.fecha_inicio.desc()).limit(1)
    )).scalar_one_or_none()
    if cita:
        from zoneinfo import ZoneInfo
        _tz = ZoneInfo(settings.tz)
        f = cita.fecha_inicio
        if f and f.tzinfo is None:
            f = f.replace(tzinfo=timezone.utc)
        lineas.append(
            f"- **YA TIENE CITA AGENDADA** para {f.astimezone(_tz).strftime('%Y-%m-%d %H:%M')} ({settings.tz}). "
            "NO vuelvas a agendar. Si pregunta, confírmasela. Si quiere cambiarla, escala al equipo."
        )

    # Tags actuales aplicados al prospecto + lista de tags disponibles para
    # aplicar_tag_seguimiento (la lista cambia raro, se carga aquí).
    from sqlalchemy import text as _sa_text
    tag_rows = (await session.execute(_sa_text("""
        SELECT t.nombre,
               (ct.cliente_id IS NOT NULL) AS asignado,
               t.descripcion
          FROM tags t
          LEFT JOIN cliente_tags ct
            ON ct.tag_id = t.id AND ct.cliente_id = :cid
         ORDER BY t.orden ASC, t.nombre ASC
    """), {"cid": cliente_id})).all()
    asignados = [r.nombre for r in tag_rows if r.asignado]
    if asignados:
        lineas.append(f"- Tags actuales: {', '.join(asignados)}")
    if tag_rows:
        bloque_tags = (
            "\n\n## TAGS DE SEGUIMIENTO DISPONIBLES (úsalos con aplicar_tag_seguimiento)\n"
            + "\n".join(
                f"- **{r.nombre}**" + (f" — {r.descripcion}" if r.descripcion else "")
                for r in tag_rows
            )
            + "\n\nAplica un tag cuando haya señal CLARA del estado. No tagees por cada mensaje."
        )
    else:
        bloque_tags = ""

    # Memoria evolutiva: cosas aprendidas sobre este prospecto y reglas generales.
    from app import memoria as mem
    memorias = await mem.cargar_relevantes(session, contacto_id=cliente_id)
    bloque_mem = mem.formatear_para_prompt(memorias)

    if len(lineas) <= 2 and not bloque_mem and not bloque_tags:
        return ""  # casi sin datos útiles
    lineas.append("")
    lineas.append("Si el prospecto ya respondió algo arriba o en el historial, NO se lo vuelvas a preguntar.")
    texto = "\n".join(lineas)
    if bloque_tags:
        texto += bloque_tags
    if bloque_mem:
        texto += "\n\n" + bloque_mem
    return texto
