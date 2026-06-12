"""
Tools del flujo PROSPECTO (Dairo — cara comercial de DTGP).

Quien escribe es un número desconocido que llegó por publicidad. El bot entiende
su negocio y, si hay encaje, agenda una reunión de diagnóstico vía Cal.com.

Cada tool:
  - definición (schema JSON) que ve Claude → TOOL_DEFINITIONS
  - handler async (args, ctx) -> dict

ctx incluye: session (AsyncSession), cliente_id, cliente_numero, y un `outbox`
(lista de mensajes a admins que se drena DESPUÉS del commit).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AlertaFabio, Cita, Cliente, Prospecto
from app.db.repos import registrar_alerta_fabio
from app.equipo.directorio import superiores_para
from app.integrations import calcom
from app.logging_setup import log

settings = get_settings()


# ════════════════════════════════════════════════════════════════════════════
# DEFINICIONES (lo que ve Claude)
# ════════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "guardar_info_prospecto",
        "description": (
            "Guarda lo que vas aprendiendo del negocio del prospecto a medida que conversan: "
            "datos básicos (negocio, sector, ciudad, qué necesita) y **calificación de fit** "
            "(tipo de organización, si es empresa, presupuesto mensual). "
            "Llámalo CADA VEZ que el prospecto te dé un dato nuevo de estos. Pasa solo los "
            "campos que ya sepas. Es lo que el equipo lee después y lo que el sistema usa para "
            "decidir si puedes ofrecer la reunión."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "negocio": {"type": "string", "description": "Nombre o tipo de negocio"},
                "sector": {"type": "string", "description": "Rubro: estética, gimnasio, retail, restaurante, etc."},
                "ciudad": {"type": "string"},
                "necesidad": {"type": "string", "description": "Qué quiere lograr (más ventas, web, pauta, etc.)"},
                "ya_pauta": {"type": "boolean", "description": "¿Ya invierte en publicidad de Meta?"},
                "tiene_web": {"type": "boolean", "description": "¿Tiene página web o tienda online?"},
                "tipo_organizacion": {
                    "type": "string",
                    "enum": ["empresa", "emprendimiento_estructurado", "persona_natural", "desconocido"],
                    "description": (
                        "empresa = empresa formal con RUT/NIT y al menos 1 empleado o operación estable. "
                        "emprendimiento_estructurado = negocio operando con cierto recorrido (≥6 meses) "
                        "y ventas regulares aunque sea unipersonal. "
                        "persona_natural = sin negocio formal o solo idea. "
                        "desconocido = aún no se ha aclarado."
                    ),
                },
                "es_empresa": {
                    "type": "boolean",
                    "description": (
                        "TRUE si tipo_organizacion es 'empresa' o 'emprendimiento_estructurado'. "
                        "FALSE si es 'persona_natural' o es solo una idea sin operación."
                    ),
                },
                "presupuesto_mensual_cop": {
                    "type": "integer",
                    "description": (
                        "Presupuesto mensual aproximado en COP que el prospecto puede invertir "
                        "en marketing (ads + fee DTGP). Convierte 'dos millones' = 2000000, "
                        "'500 mil' = 500000, etc. **Umbral mínimo de fit para agendar: 2_000_000.**"
                    ),
                },
            },
        },
    },
    {
        "name": "consultar_disponibilidad",
        "description": (
            "Consulta los horarios REALES disponibles para agendar la reunión de diagnóstico. "
            "Úsalo SIEMPRE antes de ofrecer horarios al prospecto. NUNCA inventes disponibilidad. "
            "Devuelve una lista de slots (fecha y hora) que puedes ofrecer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "dias_desde_hoy": {
                    "type": "integer",
                    "description": "Desde cuántos días a partir de hoy buscar (default 0 = hoy).",
                },
                "dias_rango": {
                    "type": "integer",
                    "description": "Cuántos días hacia adelante mirar (default 5).",
                },
            },
        },
    },
    {
        "name": "agendar_cita",
        "description": (
            "Agenda la reunión de diagnóstico en el horario elegido. Llámalo SOLO cuando ya "
            "tengas: el horario exacto que el prospecto confirmó (de consultar_disponibilidad), "
            "su nombre, su correo y el nombre del negocio. Crea la reserva real y notifica al equipo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "inicio_iso": {
                    "type": "string",
                    "description": "Fecha/hora de inicio en ISO 8601 EXACTA de un slot disponible (ej '2026-05-28T15:00:00-05:00').",
                },
                "nombre": {"type": "string"},
                "email": {"type": "string"},
                "negocio": {"type": "string"},
                "notas": {"type": "string", "description": "Contexto del negocio para el equipo (opcional)."},
            },
            "required": ["inicio_iso", "nombre", "email"],
        },
    },
    {
        "name": "recordar_sobre_prospecto",
        "description": (
            "Guarda un hecho o preferencia DURADERO sobre este prospecto, que quieras "
            "tener presente en próximas conversaciones (ej. 'prefiere reuniones por la mañana', "
            "'tiene 2 sucursales', 'mencionó que ya trabajó con otra agencia'). NO uses esto "
            "para datos estructurados (negocio/sector/ciudad) — para eso está `guardar_info_prospecto`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "description": "Resumen corto"},
                "contenido": {"type": "string", "description": "Hecho o preferencia, con su contexto"},
            },
            "required": ["titulo", "contenido"],
        },
    },
    {
        "name": "aplicar_tag_seguimiento",
        "description": (
            "Aplica o quita uno o varios tags de seguimiento al prospecto actual. "
            "Los tags reflejan el ESTADO del embudo y los usa el equipo para filtrar. "
            "Llama esto cuando reconozcas señales claras de avance/freno:\n"
            "  • 'Falta agendar': mostró interés pero aún no confirma cita.\n"
            "  • 'Cita agendada': se acaba de crear la cita (después de agendar_cita).\n"
            "  • 'Reunión hecha': el cliente menciona que ya tuvo la reunión con el equipo.\n"
            "  • 'Propuesta enviada': el equipo ya le envió una cotización formal.\n"
            "  • 'Cerrado / ganado': el cliente confirma que va a contratar.\n"
            "  • 'Perdido': dice que no le interesa, no responde varios días, o se va.\n"
            "  • 'No fit': el negocio claramente no calza con lo que hacemos.\n"
            "  • 'Seguir en X días': pide que le escribas más adelante.\n"
            "Usa los tags EXACTAMENTE como están listados en la lista de tags disponibles "
            "(case-sensitive). Solo aplica los que tengan sentido. No abuses: máximo 1-2 "
            "tags por turno cuando hay señal clara, no por cada mensaje."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "aplicar": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Nombres de tags a APLICAR al prospecto (case-sensitive).",
                },
                "quitar": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Nombres de tags a QUITAR del prospecto.",
                },
            },
        },
    },
    {
        "name": "escalar_a_equipo",
        "description": (
            "Avisa al equipo de DTGP (Dairo/Stiven) cuando hace falta intervención humana: "
            "el prospecto pide hablar con una persona, hay una queja, o es un caso fuera de tu "
            "alcance. NO lo uses para agendar (para eso está agendar_cita)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["pide_humano", "queja", "prospecto_caliente", "fuera_de_alcance", "otro"],
                },
                "mensaje": {"type": "string", "description": "Resumen claro para el equipo."},
            },
            "required": ["tipo", "mensaje"],
        },
    },
]


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════════════════


async def handler_guardar_info_prospecto(args: dict, ctx: dict) -> dict:
    """Upsert de la fila prospectos con lo aprendido. No-op si no hay cliente."""
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    if not cliente_id:
        return {"ok": False, "error": "sin cliente_id"}

    campos = {k: v for k, v in {
        "negocio": args.get("negocio"),
        "sector": args.get("sector"),
        "ciudad": args.get("ciudad"),
        "necesidad": args.get("necesidad"),
        "ya_pauta": args.get("ya_pauta"),
        "tiene_web": args.get("tiene_web"),
        "tipo_organizacion": args.get("tipo_organizacion"),
        "es_empresa": args.get("es_empresa"),
        "presupuesto_mensual_cop": args.get("presupuesto_mensual_cop"),
    }.items() if v is not None}

    # Upsert (crea la fila si no existe) y marca estado 'calificando' si seguía 'nuevo'.
    stmt = pg_insert(Prospecto).values(cliente_id=cliente_id, estado="calificando", **campos)
    update_set = dict(campos)
    update_set["updated_at"] = datetime.now(timezone.utc)
    stmt = stmt.on_conflict_do_update(index_elements=["cliente_id"], set_=update_set)
    await session.execute(stmt)
    log.info("tools.prospecto.guardado", cliente_id=cliente_id, campos=list(campos.keys()))
    return {"ok": True, "guardado": list(campos.keys())}


async def handler_consultar_disponibilidad(args: dict, ctx: dict) -> dict:
    """Consulta slots reales en Cal.com."""
    from zoneinfo import ZoneInfo as _ZI

    _DIAS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
    _MESES = ["enero", "febrero", "marzo", "abril", "mayo", "junio",
              "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"]
    _tz = _ZI(settings.tz)

    dias_desde = int(args.get("dias_desde_hoy", 0) or 0)
    dias_rango = int(args.get("dias_rango", 5) or 5)
    ahora = datetime.now(timezone.utc)
    start = ahora + timedelta(days=dias_desde)
    end = start + timedelta(days=dias_rango)

    res = await calcom.slots_disponibles(start=start, end=end, zona=settings.tz)
    if not res.get("ok"):
        return {
            "ok": False,
            "error": res.get("error"),
            "nota_para_modelo": (
                "No pude consultar la agenda en este momento. NO inventes horarios. "
                "Dile al prospecto que coordinas el horario con el equipo y escala con escalar_a_equipo."
            ),
        }
    slots = res.get("slots", [])[:8]

    ahora_local = ahora.astimezone(_tz)
    fecha_hoy_str = (
        f"{_DIAS[ahora_local.weekday()]} {ahora_local.day} de "
        f"{_MESES[ahora_local.month-1]} de {ahora_local.year} "
        f"({ahora_local.strftime('%Y-%m-%d')})"
    )

    slots_formateados = []
    for iso in slots:
        try:
            dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
            dt_local = dt.astimezone(_tz)
            humano = (
                f"{_DIAS[dt_local.weekday()]} {dt_local.day} de "
                f"{_MESES[dt_local.month-1]} a las {dt_local.strftime('%I:%M %p').lstrip('0').lower()}"
            )
            slots_formateados.append({"iso": iso, "humano": humano,
                                      "fecha": dt_local.strftime("%Y-%m-%d"),
                                      "dia_semana": _DIAS[dt_local.weekday()]})
        except Exception:
            slots_formateados.append({"iso": iso, "humano": iso})

    return {
        "ok": True,
        "fecha_actual_bogota": fecha_hoy_str,
        "slots": slots_formateados,
        "nota_para_modelo": (
            "Hoy es " + fecha_hoy_str + ". "
            "Ofrece 2-3 de los slots de arriba al prospecto usando el campo `humano` "
            "(ya está en hora de Bogotá y con el día de la semana CORRECTO — NO recalcules el día, "
            "úsalo tal cual viene en `dia_semana`). "
            "Cuando confirme uno, llama agendar_cita con el `iso` EXACTO del slot elegido."
            if slots_formateados else
            "No hay horarios disponibles en ese rango. Ofrece buscar más adelante."
        ),
    }


async def handler_agendar_cita(args: dict, ctx: dict) -> dict:
    """Crea la reserva en Cal.com, persiste la Cita y notifica al equipo."""
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero") or "(desconocido)"
    inicio_iso = args.get("inicio_iso")
    nombre = args.get("nombre") or ""
    email = args.get("email") or ""
    negocio = args.get("negocio")
    notas = args.get("notas")

    try:
        inicio = datetime.fromisoformat(inicio_iso)
    except Exception:
        return {"ok": False, "error": "inicio_iso inválido", "nota_para_modelo": "Vuelve a consultar_disponibilidad y usa un slot exacto."}

    # ── Gate de calificación de fit ─────────────────────────────────────────
    # Solo agendar con prospectos que (a) sean empresa o emprendimiento
    # estructurado, y (b) tengan presupuesto ≥ 2.000.000 COP/mes.
    # Razón: el bot estaba agendando muchas reuniones con emprendedores
    # individuales sin presupuesto, ensuciando la agenda. Si no calificó, el
    # tool DEVUELVE error con instrucción para que el modelo cierre con buena
    # cara y NO insista en agendar.
    PRESUPUESTO_MIN_COP = 2_000_000
    if cliente_id:
        pros = (await session.execute(
            select(Prospecto).where(Prospecto.cliente_id == cliente_id)
        )).scalar_one_or_none()
        pres = (pros.presupuesto_mensual_cop if pros else None)
        es_emp = (pros.es_empresa if pros else None)
        tipo_org = (pros.tipo_organizacion if pros else None)

        if pres is None:
            return {
                "ok": False,
                "error": "fit_indefinido_presupuesto",
                "nota_para_modelo": (
                    "FALTA confirmar el PRESUPUESTO MENSUAL antes de agendar. "
                    "El umbral es ≥ $2.000.000 COP/mes (ads + nuestros honorarios). "
                    "Pregúntalo con tacto, naturalidad y SIN dar el número primero. "
                    "Ej: '¿Cuánto estás pensando invertir mensualmente en marketing "
                    "para arrancar?'. Luego llama guardar_info_prospecto con "
                    "`presupuesto_mensual_cop`, y solo entonces intenta agendar otra vez."
                ),
            }
        if es_emp is None and tipo_org is None:
            return {
                "ok": False,
                "error": "fit_indefinido_organizacion",
                "nota_para_modelo": (
                    "FALTA saber si es EMPRESA / emprendimiento con operación. "
                    "Sin esa información no agendamos. Pregúntalo de forma natural "
                    "(ej: '¿Tu negocio ya está operando o estás arrancando con la idea?'), "
                    "luego guarda con guardar_info_prospecto (`tipo_organizacion`, `es_empresa`) "
                    "y vuelve a intentar."
                ),
            }
        if pres < PRESUPUESTO_MIN_COP:
            # Marcar como no_fit
            await session.execute(pg_insert(Prospecto).values(
                cliente_id=cliente_id, estado="no_fit",
            ).on_conflict_do_update(
                index_elements=["cliente_id"],
                set_={"estado": "no_fit", "updated_at": datetime.now(timezone.utc)},
            ))
            return {
                "ok": False,
                "error": "fit_no_alcanzado_presupuesto",
                "nota_para_modelo": (
                    f"NO AGENDAR: el presupuesto del prospecto ({pres:,} COP/mes) está por "
                    f"debajo del mínimo ({PRESUPUESTO_MIN_COP:,} COP/mes). "
                    "Cierra con HONESTIDAD pero SIN HUMILLAR — eres Dairo, hombre "
                    "(masculino: 'soy honesto', NO 'soy honesta'). "
                    "NUNCA digas frases tipo 'con $X no alcanza ni para...', 'eso no te va "
                    "a servir', 'es muy poco'. En su lugar usa: 'es un compromiso grande, lo "
                    "entiendo perfectamente', 'vas paso a paso, eso es lo correcto', "
                    "'cuando el negocio te dé margen para esa inversión, retomamos'. "
                    "NO ofrezcas la reunión. NO listes alternativas mediocres (no des "
                    "'guías genéricas'). Cierra cálido, con aliento, y aplica el tag "
                    "'Sin presupuesto'."
                ),
            }
        if es_emp is False or tipo_org == "persona_natural":
            await session.execute(pg_insert(Prospecto).values(
                cliente_id=cliente_id, estado="no_fit",
            ).on_conflict_do_update(
                index_elements=["cliente_id"],
                set_={"estado": "no_fit", "updated_at": datetime.now(timezone.utc)},
            ))
            return {
                "ok": False,
                "error": "fit_no_alcanzado_organizacion",
                "nota_para_modelo": (
                    "NO AGENDAR: el prospecto no tiene negocio formal/operando aún. "
                    "Cierra con honestidad pero SIN sonar despectivo. Eres Dairo, hombre "
                    "(masculino). Ejemplo: 'Lo que hacemos está pensado para negocios que "
                    "ya están operando con clientes. Cuando tengas algo en marcha y veas "
                    "tracción, me escribes y miramos cómo escalarlo juntos.' NO ofrezcas "
                    "la reunión. Aplica el tag 'Sin presupuesto' o el que más se ajuste."
                ),
            }

    res = await calcom.crear_reserva(
        inicio=inicio, nombre=nombre, email=email, telefono=cliente_numero,
        negocio=negocio, zona=settings.tz, notas=notas,
    )
    if not res.get("ok"):
        return {
            "ok": False,
            "error": res.get("error"),
            "nota_para_modelo": (
                "No se pudo crear la reserva (quizás el horario ya se tomó). Vuelve a "
                "consultar_disponibilidad y ofrece otro horario."
            ),
        }

    # Persistir la cita
    cita = Cita(
        cliente_id=cliente_id,
        nombre=nombre,
        email=email,
        negocio=negocio,
        fecha_inicio=inicio,
        calcom_booking_id=res.get("booking_id"),
        calcom_uid=res.get("uid"),
        estado="agendada",
        notas=notas,
    )
    session.add(cita)

    # Marcar prospecto como agendado (upsert defensivo)
    if cliente_id:
        stmt = pg_insert(Prospecto).values(cliente_id=cliente_id, estado="agendado", negocio=negocio)
        stmt = stmt.on_conflict_do_update(
            index_elements=["cliente_id"],
            set_={"estado": "agendado", "updated_at": datetime.now(timezone.utc)},
        )
        await session.execute(stmt)
        # Guardar email/nombre en el cliente
        await session.execute(
            update(Cliente).where(Cliente.id == cliente_id).values(
                nombre=nombre or Cliente.nombre, email=email or Cliente.email
            )
        )
        # Auto-tag "Cita agendada" + quitar "Falta agendar" si la tenía
        try:
            from sqlalchemy import text as _sa_text
            await session.execute(_sa_text("""
                INSERT INTO cliente_tags (cliente_id, tag_id, added_by)
                SELECT :c, t.id, 'bot:auto_cita'
                  FROM tags t
                 WHERE LOWER(t.nombre) = LOWER('Cita agendada')
                ON CONFLICT DO NOTHING
            """), {"c": cliente_id})
            await session.execute(_sa_text("""
                DELETE FROM cliente_tags
                 WHERE cliente_id = :c
                   AND tag_id IN (SELECT id FROM tags WHERE LOWER(nombre) = LOWER('Falta agendar'))
            """), {"c": cliente_id})
        except Exception as e:
            log.warning("tools.agendar_cita.auto_tag_fail", error=str(e))

        # Push al CRM de DT-OS (endpoint /api/webhook/bot/crm/deals).
        # Crea un Deal en la etapa "Nuevo Prospecto" con todo el contexto
        # rico que el bot ya recogió (sector, ciudad, presupuesto, etc.).
        # Si falla, NO rompe la cita — ya está en Cal.com y persistida.
        try:
            from app.integrations import dtos
            from zoneinfo import ZoneInfo as _ZI
            _tz = _ZI(settings.tz)
            fecha_cita_str = inicio.astimezone(_tz).strftime("%Y-%m-%d %H:%M")

            # Releer prospecto para tener TODOS los campos calificados
            pros_full = (await session.execute(
                select(Prospecto).where(Prospecto.cliente_id == cliente_id)
            )).scalar_one_or_none()

            partes_notas = [
                f"Cita confirmada: {fecha_cita_str} (hora Colombia).",
                f"Canal: Cal Video (link en Cal.com booking {res.get('uid','?')}).",
                f"WhatsApp: {cliente_numero}",
            ]
            if pros_full:
                if pros_full.sector:
                    partes_notas.append(f"Sector: {pros_full.sector}")
                if pros_full.ciudad:
                    partes_notas.append(f"Ciudad: {pros_full.ciudad}")
                if pros_full.necesidad:
                    partes_notas.append(f"Necesidad: {pros_full.necesidad}")
                if pros_full.ya_pauta is not None:
                    partes_notas.append(f"¿Ya pauta?: {'sí' if pros_full.ya_pauta else 'no'}")
                if pros_full.tiene_web is not None:
                    partes_notas.append(f"¿Tiene web?: {'sí' if pros_full.tiene_web else 'no'}")
                if pros_full.tipo_organizacion:
                    partes_notas.append(f"Tipo: {pros_full.tipo_organizacion}")
            if notas:
                partes_notas.append(f"Contexto: {notas}")

            presupuesto = (pros_full.presupuesto_mensual_cop if pros_full else None) or 0
            payload_deal = {
                "nombre": nombre,
                "empresa": negocio or "",
                "telefono": cliente_numero,
                "email": email,
                "valorEstimado": int(presupuesto),
                "servicio": "Marketing Digital",
                "etapa": "nuevo",
                "prioridad": "alta" if presupuesto >= 5_000_000 else "media",
                "fuente": "bot-whatsapp-dairo",
                "notas": "\n".join(partes_notas),
            }
            res_deal = await dtos.crear_deal(payload_deal)

            if res_deal.get("ok"):
                # Forma confirmada: {ok:true, data:{success, message, deal:{id,...}}}
                data_deal = res_deal.get("data") or {}
                deal_obj = data_deal.get("deal") or {}
                deal_id = (
                    deal_obj.get("id")
                    or data_deal.get("id")
                    or res_deal.get("id")
                )
                if deal_id:
                    await session.execute(
                        update(Prospecto).where(Prospecto.cliente_id == cliente_id)
                        .values(dtos_deal_id=str(deal_id)[:60])
                    )
                log.info("tools.cita.crm_push_ok", cliente_id=cliente_id, deal_id=deal_id)
            else:
                log.warning("tools.cita.crm_push_fail",
                            cliente_id=cliente_id,
                            error=str(res_deal.get("error"))[:200])
        except Exception as e:
            # NO bloquear la cita por un fallo del CRM
            log.exception("tools.cita.crm_push_exc", cliente_id=cliente_id, error=str(e))

    # Notificar al grupo del equipo DTGP (cualquiera atiende)
    from zoneinfo import ZoneInfo as _ZI
    fecha_legible = inicio.astimezone(_ZI(settings.tz)).strftime("%Y-%m-%d %H:%M")
    if settings.equipo_dtgp_group_id:
        aviso_grupo = (
            f"📅 *Nueva cita agendada*\n\n"
            f"👤 *Prospecto:* {nombre}\n"
            f"📱 {cliente_numero}\n"
            f"🏢 *Negocio:* {negocio or '—'}\n"
            f"📧 *Email:* {email or '—'}\n"
            f"🗓 *Fecha:* {fecha_legible}\n"
        )
        if notas:
            aviso_grupo += f"\n📝 *Contexto:*\n{notas}"
        _enqueue_text(ctx, to=settings.equipo_dtgp_group_id, text=aviso_grupo)
    else:
        # Fallback: si no hay grupo, individuos
        aviso = (
            f"📅 Nueva cita agendada\n"
            f"Prospecto: {nombre} ({cliente_numero})\n"
            f"Negocio: {negocio or '—'}\n"
            f"Fecha: {fecha_legible}\n"
            f"Email: {email or '—'}"
        )
        for sup in superiores_para("clientes"):
            _enqueue_text(ctx, to=sup.numero_whatsapp, text=aviso)

    log.info("tools.cita.agendada", cliente_id=cliente_id, inicio=inicio_iso, uid=res.get("uid"))
    # Avisar a la plataforma admin externa (no bloqueante)
    try:
        import asyncio as _aio
        from app.panel_admin_webhook import emitir_evento as _emit
        _aio.create_task(_emit("bot.cita_agendada", {
            "cliente_id": cliente_id,
            "cliente_numero": cliente_numero,
            "nombre": nombre,
            "email": email,
            "negocio": negocio,
            "fecha_inicio": inicio_iso,
            "calcom_uid": res.get("uid"),
        }))
    except Exception:
        pass
    return {
        "ok": True,
        "inicio": res.get("inicio"),
        "nota_para_modelo": (
            f"Cita confirmada para {fecha_legible}. Confírmasela al prospecto con la fecha/hora "
            "exacta y dile que le llegará la invitación al correo. Sé cálida y cierra bien."
        ),
    }


async def handler_recordar_sobre_prospecto(args: dict, ctx: dict) -> dict:
    """Guarda una memoria scope='contacto' para el prospecto actual."""
    from app import memoria as mem
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    if not cliente_id:
        return {"ok": False, "error": "sin cliente_id"}
    try:
        m = await mem.guardar(
            session,
            titulo=args["titulo"],
            contenido=args["contenido"],
            scope="contacto",
            contacto_id=cliente_id,
            tipo="hecho",
            creado_por="maria",
        )
        return {"ok": True, "memoria_id": m.id,
                "nota_para_modelo": "Recordado. Lo verás en futuros turnos con este prospecto."}
    except Exception as e:
        log.exception("tools.recordar_sobre_prospecto.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}


async def handler_escalar_a_equipo(args: dict, ctx: dict) -> dict:
    """Crea alerta + notifica a los admins fallback (Dairo/Stiven) vía outbox.

    Dedupe: no re-escala el mismo tipo para el mismo cliente en 6h.
    """
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero") or "(desconocido)"
    tipo = args.get("tipo", "otro")
    mensaje_eq = args.get("mensaje", "")

    if cliente_id:
        ventana = datetime.now(timezone.utc) - timedelta(hours=6)
        existente = (await session.execute(
            select(AlertaFabio).where(
                AlertaFabio.cliente_id == cliente_id,
                AlertaFabio.tipo == tipo,
                AlertaFabio.resuelto.is_(False),
                AlertaFabio.created_at >= ventana,
            ).limit(1)
        )).scalar_one_or_none()
        if existente:
            return {
                "escalado": False,
                "razon": "Ya hay una alerta abierta de este tipo. Dile al prospecto que el equipo lo contactará pronto.",
            }

    alerta = await registrar_alerta_fabio(
        session, tipo=tipo, mensaje=mensaje_eq, cliente_id=cliente_id,
    )
    aviso = (
        f"🚨 *Escalación [{tipo}]*\n\n"
        f"Prospecto: {cliente_numero}\n\n"
        f"{mensaje_eq}\n\n"
        f"_Cualquiera del equipo puede atender._"
    )
    # Notificar al GRUPO del equipo DTGP — el primero que tome, atiende.
    destinos = 0
    if settings.equipo_dtgp_group_id:
        _enqueue_text(ctx, to=settings.equipo_dtgp_group_id, text=aviso, alerta_id=alerta.id)
        destinos = 1
    else:
        # Fallback: si no hay grupo configurado, vamos a los individuos
        for sup in superiores_para("clientes"):
            _enqueue_text(ctx, to=sup.numero_whatsapp, text=aviso, alerta_id=alerta.id)
            destinos += 1

    log.info("tools.escalar", tipo=tipo, cliente_id=cliente_id, destinos=destinos)
    return {
        "escalado": True,
        "tipo": tipo,
        "nota_para_modelo": (
            "Avisaste al equipo. Dile al prospecto, cálida y breve, que una persona del equipo "
            "se comunicará con él pronto. No prometas tiempos exactos."
        ),
    }


# ════════════════════════════════════════════════════════════════════════════
# OUTBOX HELPERS (se drenan tras commit — ver flows/conversation.py)
# ════════════════════════════════════════════════════════════════════════════


def _enqueue_text(ctx: dict, *, to: str, text: str, alerta_id: int | None = None) -> None:
    ctx.setdefault("outbox", []).append({
        "kind": "text", "to": to, "text": text, "alerta_id": alerta_id,
    })


# ════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

Handler = Callable[[dict, dict], Awaitable[dict]]

async def handler_aplicar_tag_seguimiento(args: dict, ctx: dict) -> dict:
    """Aplica/quita tags de seguimiento al prospecto actual.

    Match por nombre case-insensitive. Si un nombre no existe en `tags`, lo
    reporta en `desconocidos` y sigue con los demás.
    """
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    if not cliente_id:
        return {"error": "no_cliente"}

    a_aplicar_nombres = [str(n).strip() for n in (args.get("aplicar") or []) if str(n).strip()]
    a_quitar_nombres = [str(n).strip() for n in (args.get("quitar") or []) if str(n).strip()]

    # Resolver nombres → ids (case-insensitive)
    todos = (await session.execute(sa_text(
        "SELECT id, nombre FROM tags"
    ))).all()
    por_nombre_lower = {r.nombre.lower(): r.id for r in todos}

    aplicados: list[str] = []
    desconocidos: list[str] = []
    for n in a_aplicar_nombres:
        tid = por_nombre_lower.get(n.lower())
        if tid is None:
            desconocidos.append(n)
            continue
        await session.execute(sa_text("""
            INSERT INTO cliente_tags (cliente_id, tag_id, added_by)
            VALUES (:c, :t, 'bot')
            ON CONFLICT DO NOTHING
        """), {"c": cliente_id, "t": tid})
        aplicados.append(n)

    quitados: list[str] = []
    for n in a_quitar_nombres:
        tid = por_nombre_lower.get(n.lower())
        if tid is None:
            desconocidos.append(n)
            continue
        res = await session.execute(sa_text(
            "DELETE FROM cliente_tags WHERE cliente_id = :c AND tag_id = :t"
        ), {"c": cliente_id, "t": tid})
        if res.rowcount > 0:
            quitados.append(n)

    log.info("tools.aplicar_tag_seguimiento", cliente_id=cliente_id,
             aplicados=aplicados, quitados=quitados, desconocidos=desconocidos)
    return {
        "ok": True,
        "aplicados": aplicados,
        "quitados": quitados,
        "desconocidos": desconocidos,
    }


HANDLERS: dict[str, Handler] = {
    "guardar_info_prospecto": handler_guardar_info_prospecto,
    "consultar_disponibilidad": handler_consultar_disponibilidad,
    "agendar_cita": handler_agendar_cita,
    "recordar_sobre_prospecto": handler_recordar_sobre_prospecto,
    "aplicar_tag_seguimiento": handler_aplicar_tag_seguimiento,
    "escalar_a_equipo": handler_escalar_a_equipo,
}


async def ejecutar_tool(name: str, args: dict, ctx: dict) -> dict:
    """Ejecuta el handler correspondiente. Maneja excepciones."""
    handler = HANDLERS.get(name)
    if not handler:
        return {"error": f"Tool desconocida: {name}"}
    try:
        result = await handler(args, ctx)
        log.info("tools.ejecutada", tool=name, ok=result.get("ok", result.get("escalado", True)))
        return result
    except Exception as e:
        log.exception("tools.error", tool=name, error=str(e))
        return {"error": f"Error ejecutando {name}: {e}"}
