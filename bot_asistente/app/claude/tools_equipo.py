"""
Tools del modo OPERATIVO del bot (whitelist: equipo DTGP + clientes activos).

Quien escribe está en `contactos_whitelist`. El bot ejecuta operación interna:
- Genéricas (heredadas, agnósticas del vertical): responder a un contacto,
  consultar chats, marcar interno, pausar bot, alertas, ver equipo.
- DTGP: finanzas, tareas, terceros, CRM, cuentas de cobro, reportes Meta Ads,
  briefs — llamando a las APIs DT-OS / MetaSuite (se reusan tal cual).

ctx incluye: session (AsyncSession), miembro_nombre, miembro_numero.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AlertaFabio, Cliente, ContactoWhitelist, Conversacion
from app.db.repos import get_or_create_cliente, guardar_conversacion, pausar_bot
from app.integrations import dtos, metasuite
from app.logging_setup import log
from app.utils.humanizer import sleep_humano
from app.whapi.client import enviar_paused, enviar_texto, enviar_typing

settings = get_settings()

# Nota legal obligatoria en TODA cuenta de cobro (regla DTGP / MEMORY.md).
NOTA_LEGAL_CUENTA_COBRO = (
    "Cuenta de cobro emitida por persona natural no responsable de IVA. De "
    "conformidad con el artículo 383 del E.T., si el valor cobrado por servicios "
    "es inferior a $7.370.000 COP, favor abstenerse de aplicar retención en la "
    "fuente. Adicionalmente, considerar los dependientes económicos aplicables en "
    "la depuración de la base para el cálculo de la retención en la fuente "
    "mensualizada."
)


# ════════════════════════════════════════════════════════════════════════════
# DEFINICIONES (las que ve Claude en modo operativo)
# ════════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS_EQUIPO: list[dict] = [
    # ── Mensajería / chats ──────────────────────────────────────────────────
    {
        "name": "responder_a_cliente",
        "description": (
            "Envía un mensaje de WhatsApp a un contacto (cliente o prospecto) en nombre de Dairo (CEO de DTGP). "
            "Úsalo cuando el equipo te diga 'dile a X que...' o 'mándale el reporte a Y'. "
            "IDENTIDAD: cuando escribes a alguien externo, ERES Dairo Traslaviña (fundador). "
            "NUNCA te presentes como una asesora ni firmes como 'del equipo' — el bot habla COMO Dairo. "
            "Si tienes que saludar, di 'Soy Dairo de DTGP'. Mejor aún: no te presentes si no es el primer "
            "mensaje del chat o si el cliente no preguntó quién eres. "
            "RECUERDA la regla de destinatarios: a contactos de clientes NUNCA les mandes info técnica/interna."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_cliente": {"type": "string", "description": "Número E.164 del destinatario (+57...)."},
                "mensaje": {"type": "string"},
                "pausar_chat": {"type": "boolean", "description": "Si true, pausa el bot 1h para ese contacto."},
            },
            "required": ["numero_cliente", "mensaje"],
        },
    },
    {
        "name": "consultar_chat_cliente",
        "description": "Trae los últimos mensajes del chat con un contacto (por número o nombre).",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string"},
                "nombre_parcial": {"type": "string"},
                "max_mensajes": {"type": "integer", "description": "default 25, máx 60"},
            },
        },
    },
    {
        "name": "consultar_chats_sin_responder",
        "description": "Lista chats cuyo último mensaje es del contacto (esperando respuesta).",
        "input_schema": {
            "type": "object",
            "properties": {
                "max_resultados": {"type": "integer"},
                "horas_max": {"type": "integer"},
            },
        },
    },
    {
        "name": "marcar_numero_interno",
        "description": "Marca un número como interno (el bot lo ignora) y pausa 24h. Para bodegas, otros sistemas, etc.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string"},
                "nombre": {"type": "string"},
                "razon": {"type": "string"},
            },
            "required": ["numero"],
        },
    },
    # ── Alertas / estado del bot ────────────────────────────────────────────
    {
        "name": "consultar_alertas_abiertas",
        "description": "Lista las alertas/pendientes abiertas (no resueltas).",
        "input_schema": {"type": "object", "properties": {"limite": {"type": "integer"}}},
    },
    {
        "name": "marcar_alerta_resuelta",
        "description": "Marca una alerta como resuelta por su id.",
        "input_schema": {
            "type": "object",
            "properties": {"alerta_id": {"type": "integer"}},
            "required": ["alerta_id"],
        },
    },
    {
        "name": "consultar_equipo",
        "description": "Lista los miembros activos del equipo DTGP.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "pausar_bot_global",
        "description": "Apaga el bot para todos los clientes (kill switch). Pídelo solo si el equipo lo ordena.",
        "input_schema": {"type": "object", "properties": {"razon": {"type": "string"}}},
    },
    {
        "name": "reanudar_bot_global",
        "description": "Reactiva el bot tras una pausa global.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_estado_bot",
        "description": "Dice si el bot está activo o pausado globalmente.",
        "input_schema": {"type": "object", "properties": {}},
    },
    # ── Administración de grupos de WhatsApp ────────────────────────────────
    {
        "name": "remover_del_grupo",
        "description": (
            "Saca a un miembro de un grupo de WhatsApp donde el bot es admin. "
            "Acción IRREVERSIBLE — confirma claramente antes de ejecutar. "
            "Si el equipo te dice 'saca a X del grupo', primero verifica que sí "
            "es el contacto correcto (puede haber homónimos). Usa el grupo del "
            "equipo por defecto si no especifican cuál."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "Número E.164 del miembro a sacar (+57...)."},
                "group_id": {"type": "string", "description": "ID del grupo. Si vacío, usa el grupo EQUIPO DTGP por defecto."},
                "motivo": {"type": "string", "description": "Razón breve (para log)."},
            },
            "required": ["numero"],
        },
    },
    {
        "name": "agregar_al_grupo",
        "description": (
            "Agrega un número a un grupo de WhatsApp. Estrategia HÍBRIDA:\n"
            "1) Intenta agregar directo (funciona si privacidad del contacto lo permite).\n"
            "2) Si falla por privacidad estricta → envía AUTOMÁTICAMENTE el link "
            "de invitación al chat personal del contacto + le manda un mensaje "
            "corto explicando.\n"
            "Devuelve el resultado para que avises al equipo qué pasó. "
            "Si no especifican grupo, usa el del EQUIPO DTGP."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "Número E.164 del contacto a agregar."},
                "group_id": {"type": "string", "description": "Opcional. Default: grupo EQUIPO DTGP."},
                "mensaje_invitacion": {
                    "type": "string",
                    "description": "Opcional. Texto que se manda al chat personal si hay fallback a link. Si vacío, el bot arma uno cordial.",
                },
            },
            "required": ["numero"],
        },
    },
    {
        "name": "enviar_link_grupo",
        "description": (
            "Envía el link de invitación de un grupo al chat personal de un "
            "contacto, SIN intentar agregarlo directo. Útil cuando quieres que "
            "la persona decida si se une (no forzar). Por defecto usa el grupo "
            "EQUIPO DTGP."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "Número E.164 del destinatario."},
                "group_id": {"type": "string", "description": "Opcional. Default: EQUIPO DTGP."},
                "mensaje_acompaname": {"type": "string", "description": "Texto opcional que acompaña el link."},
            },
            "required": ["numero"],
        },
    },
    # ── DTGP: finanzas ──────────────────────────────────────────────────────
    {
        "name": "consultar_finanzas",
        "description": (
            "Consulta el resumen financiero de DTGP (ingresos/gastos, presupuesto, disponible) "
            "desde DT-OS. RECUERDA: los saldos 'disponible' vienen de Google Sheets y pueden "
            "estar desactualizados — adviértelo si reportas saldos."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "mes": {"type": "string", "description": "ej 'febrero' (opcional)"},
                "tipo": {"type": "string", "enum": ["receivable", "payable"], "description": "solo por cobrar / por pagar (opcional)"},
            },
        },
    },
    {
        "name": "registrar_gasto",
        "description": (
            "Registra un GASTO en Google Sheets (vía DT-OS). Antes de llamarlo, asegúrate de "
            "tener: categoría (pregúntala SIEMPRE), descripción con beneficiario, y el tercero "
            "REAL (nunca 'DT Growth Partners')."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                "importe": {"type": "number"},
                "descripcion": {"type": "string", "description": "Incluye el beneficiario"},
                "categoria": {"type": "string"},
                "cuenta": {"type": "string", "description": "Bancolombia, Nequi, Daviplata, Efectivo, etc."},
                "entidad": {"type": "string", "description": "El tercero real"},
                "terceroId": {"type": "string"},
            },
            "required": ["importe", "descripcion", "categoria", "entidad"],
        },
    },
    {
        "name": "registrar_ingreso",
        "description": "Registra un INGRESO en Google Sheets (vía DT-OS). Para transferencias de Dairo, confirma antes si es personal o de la empresa.",
        "input_schema": {
            "type": "object",
            "properties": {
                "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                "importe": {"type": "number"},
                "descripcion": {"type": "string"},
                "categoria": {"type": "string", "description": "típicamente 'PAGO DE CLIENTE'"},
                "cuenta": {"type": "string"},
                "entidad": {"type": "string"},
                "terceroId": {"type": "string"},
            },
            "required": ["importe", "descripcion", "categoria", "entidad"],
        },
    },
    {
        "name": "crear_cuenta_cobro",
        "description": (
            "Genera una cuenta de cobro (PDF) vía DT-OS y opcionalmente la envía por WhatsApp. "
            "UN SOLO servicio por cuenta (no dividas en líneas). La nota legal obligatoria se "
            "agrega automáticamente. Si pasas `enviar_a` (número del cliente), descarga el PDF "
            "y se lo manda directo. Si no, devuelve el id y el equipo decide qué hacer."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "nombre_cliente": {"type": "string"},
                "identificacion": {"type": "string", "description": "NIT o CC"},
                "fecha": {"type": "string", "description": "YYYY-MM-DD"},
                "concepto": {"type": "string"},
                "servicio_descripcion": {"type": "string"},
                "servicio_valor": {"type": "number"},
                "observaciones_extra": {"type": "string", "description": "se añade DESPUÉS de la nota legal (opcional)"},
                "enviar_a": {"type": "string", "description": "Número E.164 del destinatario (+57...). Si se pasa, envía el PDF por WhatsApp."},
                "mensaje_acompaname": {"type": "string", "description": "Texto que acompaña el PDF al enviarlo (opcional)."},
            },
            "required": ["nombre_cliente", "identificacion", "concepto", "servicio_descripcion", "servicio_valor"],
        },
    },
    {
        "name": "enviar_cuenta_cobro",
        "description": (
            "Envía por WhatsApp una cuenta de cobro YA CREADA en DT-OS, dado su id. "
            "Útil cuando creaste la cuenta antes sin enviar y ahora el equipo te pide mandarla."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "invoice_id": {"type": "string", "description": "ID de la cuenta en DT-OS"},
                "enviar_a": {"type": "string", "description": "Número E.164 del destinatario"},
                "mensaje_acompaname": {"type": "string", "description": "Texto que acompaña el PDF (opcional)"},
            },
            "required": ["invoice_id", "enviar_a"],
        },
    },
    # ── DTGP: tareas ─────────────────────────────────────────────────────────
    {
        "name": "consultar_tareas",
        "description": "Consulta tareas del equipo en DT-OS. Por usuario+estado, o todas (todas=true para el resumen 9 AM).",
        "input_schema": {
            "type": "object",
            "properties": {
                "usuario": {"type": "string", "enum": ["Lía", "Dairo", "Stiven", "Mariana", "Jose", "Anderson", "Edgardo", "Jhonathan"]},
                "estado": {"type": "string", "enum": ["TODO", "IN_PROGRESS", "DONE"]},
                "todas": {"type": "boolean"},
            },
        },
    },
    {
        "name": "crear_tarea",
        "description": "Crea una tarea en DT-OS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string"},
                "asignado": {"type": "string"},
                "proyecto": {"type": "string", "description": "match parcial, ej 'Tennis'"},
                "prioridad": {"type": "string", "enum": ["baja", "media", "alta"]},
                "fechaFin": {"type": "string", "description": "YYYY-MM-DD"},
                "descripcion": {"type": "string"},
            },
            "required": ["titulo"],
        },
    },
    {
        "name": "actualizar_tarea",
        "description": "Actualiza una tarea (estado, prioridad, título) en DT-OS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "tarea_id": {"type": "string"},
                "estado": {"type": "string", "enum": ["TODO", "IN_PROGRESS", "DONE"]},
                "prioridad": {"type": "string", "enum": ["baja", "media", "alta"]},
                "titulo": {"type": "string"},
            },
            "required": ["tarea_id"],
        },
    },
    # ── DTGP: terceros / CRM / clientes ──────────────────────────────────────
    {
        "name": "consultar_terceros",
        "description": "Lista/busca terceros (contactos, proveedores, empleados) en DT-OS.",
        "input_schema": {
            "type": "object",
            "properties": {
                "buscar": {"type": "string"},
                "tipo": {"type": "string", "enum": ["prospecto", "cliente", "proveedor", "empleado"]},
            },
        },
    },
    {
        "name": "consultar_crm",
        "description": "Resumen del pipeline CRM o lista de deals en DT-OS.",
        "input_schema": {"type": "object", "properties": {"deals": {"type": "boolean"}}},
    },
    {
        "name": "consultar_clientes_dtos",
        "description": "Lista/busca clientes de DTGP con sus servicios activos en DT-OS.",
        "input_schema": {"type": "object", "properties": {"search": {"type": "string"}}},
    },
    {
        "name": "consultar_brief",
        "description": "Lista briefs de clientes, o trae uno como markdown si pasas brief_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "search": {"type": "string"},
                "brief_id": {"type": "string"},
            },
        },
    },
    # ── DTGP: Meta Ads ───────────────────────────────────────────────────────
    {
        "name": "reporte_meta_ads",
        "description": (
            "Trae métricas de campañas de Meta Ads (MetaSuite). Pasa `empresa` (se resuelve el "
            "account_id desde la whitelist) o `account_id` directo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "empresa": {"type": "string", "description": "ej 'Equilibrio', 'Tennis', 'ACBFIT'"},
                "account_id": {"type": "string", "description": "act_... (opcional si pasas empresa)"},
                "date_preset": {
                    "type": "string",
                    "enum": ["today", "yesterday", "last_7d", "last_14d", "last_30d", "this_month", "last_month", "maximum"],
                },
            },
        },
    },
    # ── Memoria evolutiva + recordatorios (pilar openclaw) ────────────────────
    {
        "name": "aprender_regla",
        "description": (
            "Guarda una directiva o aprendizaje DURADERO para recordarlo en futuros "
            "turnos. Úsalo cuando te digan algo tipo: 'siempre que...', 'a partir de "
            "ahora...', 'recuerda que...', 'para X cliente, ...'. Si la directiva es "
            "sobre un contacto específico, pasa `contacto_numero`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string", "description": "Resumen corto de la regla (≤180 chars)"},
                "contenido": {"type": "string", "description": "La regla en detalle, con el porqué si lo sabes"},
                "scope": {"type": "string", "enum": ["general", "contacto", "equipo"], "description": "general=para todo, contacto=solo ese, equipo=del equipo DTGP"},
                "contacto_numero": {"type": "string", "description": "+57... si scope=contacto"},
                "tipo": {"type": "string", "enum": ["regla", "hecho", "preferencia", "aprendizaje"]},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["titulo", "contenido"],
        },
    },
    {
        "name": "olvidar_regla",
        "description": "Desactiva una memoria por id (si una directiva la contradice, primero olvida la vieja y luego aprende la nueva).",
        "input_schema": {
            "type": "object",
            "properties": {"memoria_id": {"type": "integer"}},
            "required": ["memoria_id"],
        },
    },
    {
        "name": "consultar_memorias",
        "description": "Lista las memorias activas. Filtra por scope o por contacto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["general", "contacto", "equipo"]},
                "contacto_numero": {"type": "string"},
                "limite": {"type": "integer"},
            },
        },
    },
    {
        "name": "programar_recordatorio",
        "description": (
            "Agenda un recordatorio futuro (promesa, follow-up, seguimiento). El "
            "heartbeat lo leerá cuando venza y decidirá si actuar. Ejemplos: "
            "'recordarle al cliente X mañana a las 10', 'hacer seguimiento a Y en 2 horas'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "accion": {"type": "string", "description": "Qué hacer cuando venza"},
                "vence_en": {"type": "string", "description": "ISO 8601 (ej '2026-05-29T10:00:00-05:00')"},
                "contacto_numero": {"type": "string", "description": "+57... si aplica"},
                "motivo": {"type": "string", "description": "Contexto/por qué"},
            },
            "required": ["accion", "vence_en"],
        },
    },
    {
        "name": "etiquetar_contacto",
        "description": (
            "Etiqueta a un contacto. La etiqueta decide cómo el bot lo trata en futuras "
            "conversaciones: cliente/prospecto/equipo → atiende; **personal → SILENCIO TOTAL** "
            "(el bot jamás responde). Úsalo cuando el equipo te diga 'X número es personal de "
            "Dairo, ignóralo' o 'tal número es cliente de tal empresa'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "+57..."},
                "etiqueta": {"type": "string", "enum": ["cliente", "prospecto", "equipo", "personal"]},
                "motivo": {"type": "string", "description": "(opcional) nota corta"},
            },
            "required": ["numero", "etiqueta"],
        },
    },
    {
        "name": "consultar_sin_clasificar",
        "description": "Lista contactos sin etiqueta que ya escribieron (cola para clasificar).",
        "input_schema": {
            "type": "object",
            "properties": {"limite": {"type": "integer"}},
        },
    },
    {
        "name": "consultar_recordatorios",
        "description": "Lista recordatorios pendientes. Opcional filtrar por vencidos o por contacto.",
        "input_schema": {
            "type": "object",
            "properties": {
                "solo_vencidos": {"type": "boolean"},
                "contacto_numero": {"type": "string"},
                "limite": {"type": "integer"},
            },
        },
    },
]


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS — genéricos (heredados, sin dependencias retail)
# ════════════════════════════════════════════════════════════════════════════


async def handler_responder_a_cliente(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    numero = args["numero_cliente"]
    mensaje = (args.get("mensaje") or "").strip()
    if not mensaje:
        return {"enviado": False, "razon": "mensaje vacío"}

    cliente = await get_or_create_cliente(session, numero)
    try:
        await enviar_typing(numero)
        await sleep_humano(mensaje)
        await enviar_texto(numero, mensaje)
        await enviar_paused(numero)
    except Exception as e:
        log.exception("tools_equipo.responder_a_cliente.fail", error=str(e))
        return {"enviado": False, "razon": f"Error de whapi: {e}"}

    await guardar_conversacion(
        session, cliente_id=cliente.id, direccion="outbound", tipo="texto",
        contenido=mensaje, intent="instruccion_equipo", modelo="via_equipo",
        metadata={"via": "equipo_admin", "miembro_equipo": ctx.get("miembro_nombre")},
    )

    if bool(args.get("pausar_chat", False)):
        await pausar_bot(session, cliente_id=cliente.id, horas=1,
                         razon=f"{ctx.get('miembro_nombre','equipo')} tomó el chat vía bot")
        pausado = True
    else:
        pausado = False

    from sqlalchemy import text as sa_text
    resueltas = await session.execute(sa_text(
        "UPDATE alertas_fabio SET resuelto=true, resuelto_en=now() "
        "WHERE cliente_id=:cid AND resuelto=false RETURNING id"
    ), {"cid": cliente.id})
    ids = [r[0] for r in resueltas.fetchall()]
    return {"enviado": True, "cliente": numero, "preview": mensaje[:80], "bot_pausado": pausado, "alertas_resueltas": ids}


async def handler_consultar_chat_cliente(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    max_msgs = max(1, min(int(args.get("max_mensajes") or 25), 60))
    stmt = select(Cliente)
    if numero := args.get("numero"):
        stmt = stmt.where(Cliente.numero_whatsapp == numero)
    elif nombre := args.get("nombre_parcial"):
        stmt = stmt.where(Cliente.nombre.ilike(f"%{nombre}%"))
    else:
        return {"error": "Pasa `numero` o `nombre_parcial`"}
    cliente = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if not cliente:
        return {"error": "Contacto no encontrado"}
    msgs = (await session.execute(
        select(Conversacion).where(Conversacion.cliente_id == cliente.id)
        .order_by(Conversacion.timestamp.desc()).limit(max_msgs)
    )).scalars().all()
    msgs = list(reversed(msgs))
    return {
        "cliente": {"id": cliente.id, "numero": cliente.numero_whatsapp, "nombre": cliente.nombre},
        "total_mensajes_traidos": len(msgs),
        "mensajes": [
            {
                "ts": m.timestamp.isoformat() if m.timestamp else None,
                "de": "contacto" if m.direccion == "inbound" else ("humano_admin" if m.direccion == "humano" else "bot"),
                "tipo": m.tipo, "contenido": (m.contenido or "")[:500], "intent": m.intent,
            } for m in msgs
        ],
    }


async def handler_consultar_chats_sin_responder(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    max_resultados = int(args.get("max_resultados") or 15)
    horas_max = int(args.get("horas_max") or 48)
    from sqlalchemy import text as sa_text
    rows = (await session.execute(sa_text("""
        WITH ultimo_por_cliente AS (
            SELECT DISTINCT ON (cliente_id) cliente_id, direccion, contenido, timestamp, tipo
            FROM conversaciones
            WHERE timestamp > NOW() - (:horas || ' hours')::interval
            ORDER BY cliente_id, timestamp DESC
        )
        SELECT u.cliente_id, c.numero_whatsapp, c.nombre, u.timestamp, u.tipo, LEFT(u.contenido, 200)
        FROM ultimo_por_cliente u JOIN clientes c ON c.id = u.cliente_id
        WHERE u.direccion = 'inbound' AND c.bloqueado = false
        ORDER BY u.timestamp ASC LIMIT :lim
    """), {"horas": str(horas_max), "lim": max_resultados})).fetchall()
    chats = [
        {"cliente_id": r[0], "numero": r[1], "nombre": r[2] or "(sin nombre)",
         "ultimo_mensaje_ts": r[3].isoformat() if r[3] else None, "tipo_ultimo": r[4],
         "preview": (r[5] or "[media]").strip()[:200]}
        for r in rows
    ]
    return {"total": len(chats), "chats": chats, "horas_max": horas_max}


async def handler_marcar_numero_interno(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "razon": "numero vacío"}
    if not numero.startswith("+"):
        numero = "+" + numero.lstrip("+ ")
    nombre = (args.get("nombre") or "").strip() or "Número interno (sin nombre)"
    razon = (args.get("razon") or "").strip() or f"Marcado interno por {ctx.get('miembro_nombre','equipo')}"
    from sqlalchemy import text as sa_text
    res = await session.execute(sa_text("""
        INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo)
        VALUES (:n, :nom, :raz, true)
        ON CONFLICT (numero_whatsapp) DO UPDATE
        SET nombre = COALESCE(EXCLUDED.nombre, numeros_internos.nombre),
            razon = COALESCE(EXCLUDED.razon, numeros_internos.razon), activo = true
        RETURNING id
    """), {"n": numero, "nom": nombre, "raz": razon})
    row = res.fetchone()
    try:
        from app.equipo.directorio import invalidar_cache
        invalidar_cache()
    except Exception:
        pass
    cliente = (await session.execute(
        select(Cliente).where(Cliente.numero_whatsapp == numero)
    )).scalar_one_or_none()
    pausa = False
    if cliente:
        await pausar_bot(session, cliente_id=cliente.id, horas=24, razon=f"marcado interno: {nombre}")
        pausa = True
    return {"ok": True, "numero": numero, "interno_id": row[0] if row else None, "pausa_cliente_24h": pausa}


async def handler_marcar_alerta_resuelta(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    alerta_id = int(args["alerta_id"])
    await session.execute(
        update(AlertaFabio).where(AlertaFabio.id == alerta_id)
        .values(resuelto=True, resuelto_en=datetime.now(timezone.utc))
    )
    return {"resuelta": True, "alerta_id": alerta_id}


async def handler_consultar_alertas_abiertas(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    limite = int(args.get("limite", 10))
    rows = (await session.execute(
        select(AlertaFabio, Cliente)
        .join(Cliente, Cliente.id == AlertaFabio.cliente_id, isouter=True)
        .where(AlertaFabio.resuelto.is_(False))
        .order_by(desc(AlertaFabio.created_at)).limit(limite)
    )).all()
    return {
        "alertas": [
            {"id": a.id, "tipo": a.tipo,
             "cliente_numero": c.numero_whatsapp if c else None,
             "cliente_nombre": c.nombre if c else None,
             "mensaje": (a.mensaje or "")[:300],
             "created_at": a.created_at.isoformat() if a.created_at else None}
            for a, c in rows
        ],
    }


async def handler_consultar_equipo(args: dict, ctx: dict) -> dict:
    from app.equipo.directorio import listar_miembros_equipo
    miembros = listar_miembros_equipo()
    return {
        "total": len(miembros),
        "miembros": [
            {"nombre": m.nombre, "numero": m.numero_whatsapp, "rol": m.rol,
             "areas": list(m.areas), "fallback": m.es_fallback}
            for m in miembros
        ],
    }


def _invalidar_cache_bot_estado_safe():
    try:
        from app.main import invalidar_bot_estado_cache
        invalidar_bot_estado_cache()
    except Exception:
        pass


async def handler_pausar_bot_global(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    razon = (args.get("razon") or "Pausado por administrador").strip()
    miembro = ctx.get("miembro_nombre") or "admin"
    await session.execute(sa_text(
        "UPDATE bot_estado SET activo=false, pausado_por=:p, pausado_en=now(), razon=:r, actualizado_en=now() WHERE id=1"
    ), {"p": miembro, "r": razon})
    _invalidar_cache_bot_estado_safe()
    log.warning("tools_equipo.bot_pausado", por=miembro, razon=razon)
    return {"pausado": True, "por": miembro, "razon": razon}


async def handler_reanudar_bot_global(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    miembro = ctx.get("miembro_nombre") or "admin"
    await session.execute(sa_text(
        "UPDATE bot_estado SET activo=true, pausado_por=null, pausado_en=null, razon=null, actualizado_en=now() WHERE id=1"
    ))
    _invalidar_cache_bot_estado_safe()
    log.warning("tools_equipo.bot_reanudado", por=miembro)
    return {"reanudado": True, "por": miembro}


async def handler_consultar_estado_bot(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    row = (await session.execute(sa_text(
        "SELECT activo, pausado_por, pausado_en, razon FROM bot_estado WHERE id=1"
    ))).first()
    if not row:
        return {"activo": True, "nota": "Sin registro, asumido activo."}
    return {"activo": bool(row[0]), "pausado_por": row[1],
            "pausado_en": row[2].isoformat() if row[2] else None, "razon": row[3]}


async def handler_remover_del_grupo(args: dict, ctx: dict) -> dict:
    """Saca un participante de un grupo. Solo el equipo puede pedirlo."""
    from app.config import get_settings as _gs
    from app.whapi.client import remover_participantes_grupo
    s = _gs()
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "error": "numero requerido"}
    group_id = (args.get("group_id") or "").strip() or s.equipo_dtgp_group_id
    if not group_id:
        return {"ok": False, "error": "no hay group_id ni grupo equipo configurado"}
    motivo = args.get("motivo") or "removido por el equipo"
    try:
        res = await remover_participantes_grupo(group_id, [numero])
        log.warning("tools_equipo.remover_del_grupo",
                    group=group_id, numero=numero, motivo=motivo,
                    autor=ctx.get("miembro_nombre"))
        return {"ok": True, "removido": numero, "grupo": group_id, "resultado": res}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "numero": numero, "grupo": group_id}


async def handler_agregar_al_grupo(args: dict, ctx: dict) -> dict:
    """Híbrido: intenta agregar directo; si falla por privacidad, manda link."""
    from app.config import get_settings as _gs
    from app.whapi.client import (
        agregar_participantes_grupo, enviar_invite_link_grupo,
        obtener_grupo, WhapiError,
    )
    s = _gs()
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "error": "numero requerido"}
    group_id = (args.get("group_id") or "").strip() or s.equipo_dtgp_group_id
    if not group_id:
        return {"ok": False, "error": "no hay group_id ni grupo equipo configurado"}

    # Nombre del grupo para el mensaje al chat personal
    nombre_grupo = "el grupo"
    try:
        info = await obtener_grupo(group_id)
        nombre_grupo = info.get("name") or nombre_grupo
    except Exception:
        pass

    # 1) Intento directo
    try:
        res = await agregar_participantes_grupo(group_id, [numero])
        # whapi devuelve por participante un resultado — checamos si entró
        added_ok = False
        if isinstance(res, dict):
            parts = res.get("participants") or res.get("results") or []
            if isinstance(parts, list) and parts:
                p0 = parts[0] if isinstance(parts[0], dict) else {}
                status = (p0.get("status") or p0.get("result") or "").lower()
                added_ok = status in ("added", "200", "ok", "success") or p0.get("ok") is True
            elif res.get("status") in (200, "200", "ok"):
                added_ok = True
        if added_ok:
            log.info("tools_equipo.agregar_al_grupo.directo", numero=numero, grupo=group_id)
            return {
                "ok": True, "modo": "directo", "numero": numero, "grupo": group_id,
                "nota_para_modelo": f"Agregado directo a {nombre_grupo}. Confirma al equipo.",
            }
        # whapi devolvió ok pero no agregó (probablemente privacidad) → fallback
    except WhapiError as e:
        log.info("tools_equipo.agregar_al_grupo.directo_fail",
                 numero=numero, grupo=group_id, error=str(e)[:120])

    # 2) Fallback: enviar link al chat personal
    mensaje = args.get("mensaje_invitacion") or (
        f"Hola, te quiero agregar al grupo *{nombre_grupo}* en WhatsApp. "
        "Acepta el siguiente link para entrar:"
    )
    try:
        await enviar_invite_link_grupo(numero, group_id, mensaje_acompaname=mensaje)
        log.info("tools_equipo.agregar_al_grupo.link_personal",
                 numero=numero, grupo=group_id)
        return {
            "ok": True, "modo": "link_chat_personal",
            "numero": numero, "grupo": group_id,
            "nota_para_modelo": (
                f"No pude agregar directo (probablemente privacidad estricta). "
                f"Mandé el link de invitación a {numero} a su chat personal. "
                "Avisa al equipo: el contacto debe aceptar el link para entrar."
            ),
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200], "numero": numero}


async def handler_enviar_link_grupo(args: dict, ctx: dict) -> dict:
    """Solo envía el link al chat personal — sin intentar agregar."""
    from app.config import get_settings as _gs
    from app.whapi.client import enviar_invite_link_grupo, obtener_grupo
    s = _gs()
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "error": "numero requerido"}
    group_id = (args.get("group_id") or "").strip() or s.equipo_dtgp_group_id
    if not group_id:
        return {"ok": False, "error": "no hay group_id"}

    nombre_grupo = "el grupo"
    try:
        info = await obtener_grupo(group_id)
        nombre_grupo = info.get("name") or nombre_grupo
    except Exception:
        pass

    msg = args.get("mensaje_acompaname") or (
        f"Te paso el link del grupo *{nombre_grupo}* — entra cuando quieras:"
    )
    try:
        await enviar_invite_link_grupo(numero, group_id, mensaje_acompaname=msg)
        return {
            "ok": True, "numero": numero, "grupo": group_id,
            "nota_para_modelo": f"Link de {nombre_grupo} enviado a {numero}.",
        }
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS — DTGP (DT-OS / MetaSuite)
# ════════════════════════════════════════════════════════════════════════════


async def handler_consultar_finanzas(args: dict, ctx: dict) -> dict:
    res = await dtos.finanzas(mes=args.get("mes"), tipo=args.get("tipo"))
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error"),
                "nota_para_modelo": "No pude consultar finanzas en DT-OS. Avísale al equipo, no inventes cifras."}
    return {"ok": True, "finanzas": res.get("data"),
            "nota_para_modelo": "Los saldos 'disponible' vienen de Sheets; si reportas saldos, advierte que pueden estar desactualizados."}


async def handler_registrar_gasto(args: dict, ctx: dict) -> dict:
    payload = {k: v for k, v in {
        "fecha": args.get("fecha") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "importe": args.get("importe"),
        "descripcion": args.get("descripcion"),
        "categoria": args.get("categoria"),
        "cuenta": args.get("cuenta"),
        "entidad": args.get("entidad"),
        "terceroId": args.get("terceroId"),
    }.items() if v is not None}
    res = await dtos.registrar_gasto(payload)
    return {"ok": res.get("ok"), "resultado": res.get("data"), "error": res.get("error")}


async def handler_registrar_ingreso(args: dict, ctx: dict) -> dict:
    payload = {k: v for k, v in {
        "fecha": args.get("fecha") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "importe": args.get("importe"),
        "descripcion": args.get("descripcion"),
        "categoria": args.get("categoria") or "PAGO DE CLIENTE",
        "cuenta": args.get("cuenta"),
        "entidad": args.get("entidad"),
        "terceroId": args.get("terceroId"),
    }.items() if v is not None}
    res = await dtos.registrar_ingreso(payload)
    return {"ok": res.get("ok"), "resultado": res.get("data"), "error": res.get("error")}


async def handler_crear_cuenta_cobro(args: dict, ctx: dict) -> dict:
    observaciones = NOTA_LEGAL_CUENTA_COBRO
    if args.get("observaciones_extra"):
        observaciones += "\n\n" + args["observaciones_extra"]
    payload = {
        "nombre_cliente": args.get("nombre_cliente"),
        "identificacion": args.get("identificacion"),
        "fecha": args.get("fecha") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "concepto": args.get("concepto"),
        "servicios": [{
            "descripcion": args.get("servicio_descripcion"),
            "cantidad": 1,
            "precio_unitario": args.get("servicio_valor"),
        }],
        "observaciones": observaciones,
    }
    res = await dtos.crear_cuenta_cobro(payload)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    data = res.get("data") or {}
    invoice_id = (data.get("id") or data.get("invoiceId") or data.get("invoice_id")
                  or (data.get("invoice") or {}).get("id"))
    numero = (data.get("number") or data.get("numero") or data.get("invoiceNumber")
              or (data.get("invoice") or {}).get("number"))

    resultado = {"ok": True, "cuenta": data, "invoice_id": invoice_id, "numero": numero}

    # ¿Enviar por WhatsApp?
    enviar_a = (args.get("enviar_a") or "").strip()
    if enviar_a and invoice_id:
        envio = await _enviar_cuenta_pdf(
            invoice_id=invoice_id, numero_destino=enviar_a,
            caption=args.get("mensaje_acompaname"),
            nombre_cliente=args.get("nombre_cliente"),
        )
        resultado["enviado"] = envio.get("ok", False)
        if not envio.get("ok"):
            resultado["envio_error"] = envio.get("error")
        resultado["nota_para_modelo"] = (
            f"Cuenta {numero or invoice_id} creada y enviada a {enviar_a}."
            if envio.get("ok") else
            f"Cuenta {numero or invoice_id} creada PERO falló el envío: "
            f"{envio.get('error', 'error desconocido')}. Dile al equipo."
        )
    else:
        resultado["nota_para_modelo"] = (
            f"Cuenta de cobro creada (id={invoice_id}, número={numero}). "
            "Si quieren que la envíe por WhatsApp, pídeme el número del destinatario "
            "y úsala con `enviar_cuenta_cobro(invoice_id, enviar_a)`."
        )
    return resultado


async def _enviar_cuenta_pdf(
    *,
    invoice_id: str,
    numero_destino: str,
    caption: str | None = None,
    nombre_cliente: str | None = None,
) -> dict:
    """Descarga el PDF de DT-OS y lo envía por whapi al destinatario."""
    from app.whapi.client import enviar_documento_bytes
    pdf = await dtos.descargar_pdf_cuenta_cobro(invoice_id)
    if not pdf.get("ok"):
        return {"ok": False, "error": f"no se pudo descargar: {pdf.get('error')}"}
    data = pdf.get("data")
    if not data:
        return {"ok": False, "error": "PDF vacío"}
    filename = pdf.get("filename") or f"cuenta_cobro_{invoice_id}.pdf"
    cap = caption or (
        f"Te envío la cuenta de cobro" + (f" a nombre de {nombre_cliente}." if nombre_cliente else ".")
    )
    try:
        await enviar_documento_bytes(
            numero_destino, data, mime="application/pdf",
            filename=filename, caption=cap,
        )
        log.info("tools.enviar_cuenta_cobro", invoice_id=invoice_id,
                 destino=numero_destino, bytes=len(data))
        return {"ok": True, "bytes": len(data), "filename": filename}
    except Exception as e:
        log.exception("tools.enviar_cuenta_cobro.fail",
                      invoice_id=invoice_id, destino=numero_destino, error=str(e))
        return {"ok": False, "error": str(e)[:200]}


async def handler_enviar_cuenta_cobro(args: dict, ctx: dict) -> dict:
    invoice_id = (args.get("invoice_id") or "").strip()
    enviar_a = (args.get("enviar_a") or "").strip()
    if not invoice_id or not enviar_a:
        return {"ok": False, "error": "faltan invoice_id o enviar_a"}
    res = await _enviar_cuenta_pdf(
        invoice_id=invoice_id, numero_destino=enviar_a,
        caption=args.get("mensaje_acompaname"),
    )
    return {
        "ok": res.get("ok", False),
        "error": res.get("error"),
        "bytes": res.get("bytes"),
        "nota_para_modelo": (
            f"Cuenta {invoice_id} enviada a {enviar_a}."
            if res.get("ok") else
            f"No se pudo enviar la cuenta {invoice_id}: {res.get('error')}"
        ),
    }


async def handler_consultar_tareas(args: dict, ctx: dict) -> dict:
    if args.get("todas"):
        res = await dtos.tareas_todas()
    else:
        res = await dtos.tareas(usuario=args.get("usuario"), estado=args.get("estado"))
    return {"ok": res.get("ok"), "tareas": res.get("data"), "error": res.get("error")}


async def handler_crear_tarea(args: dict, ctx: dict) -> dict:
    payload = {k: v for k, v in {
        "titulo": args.get("titulo"),
        "asignado": args.get("asignado"),
        "proyecto": args.get("proyecto"),
        "prioridad": args.get("prioridad"),
        "fechaFin": args.get("fechaFin"),
        "descripcion": args.get("descripcion"),
        "creador": ctx.get("miembro_nombre"),
    }.items() if v is not None}
    res = await dtos.crear_tarea(payload)
    return {"ok": res.get("ok"), "resultado": res.get("data"), "error": res.get("error")}


async def handler_actualizar_tarea(args: dict, ctx: dict) -> dict:
    payload = {k: v for k, v in {
        "estado": args.get("estado"),
        "prioridad": args.get("prioridad"),
        "titulo": args.get("titulo"),
    }.items() if v is not None}
    res = await dtos.actualizar_tarea(str(args.get("tarea_id")), payload)
    return {"ok": res.get("ok"), "resultado": res.get("data"), "error": res.get("error")}


async def handler_consultar_terceros(args: dict, ctx: dict) -> dict:
    res = await dtos.terceros(buscar=args.get("buscar"), tipo=args.get("tipo"))
    return {"ok": res.get("ok"), "terceros": res.get("data"), "error": res.get("error")}


async def handler_consultar_crm(args: dict, ctx: dict) -> dict:
    res = await dtos.crm(deals=bool(args.get("deals")))
    return {"ok": res.get("ok"), "crm": res.get("data"), "error": res.get("error")}


async def handler_consultar_clientes_dtos(args: dict, ctx: dict) -> dict:
    res = await dtos.clientes(search=args.get("search"))
    return {"ok": res.get("ok"), "clientes": res.get("data"), "error": res.get("error")}


async def handler_consultar_brief(args: dict, ctx: dict) -> dict:
    if args.get("brief_id"):
        res = await dtos.brief_markdown(str(args["brief_id"]))
        return {"ok": res.get("ok"), "brief": res.get("data"), "error": res.get("error")}
    res = await dtos.briefs(search=args.get("search"))
    return {"ok": res.get("ok"), "briefs": res.get("data"), "error": res.get("error")}


async def handler_reporte_meta_ads(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    account_id = args.get("account_id")
    if not account_id and args.get("empresa"):
        # Resolver account_id desde la whitelist por nombre de empresa (parcial)
        row = (await session.execute(
            select(ContactoWhitelist.meta_account_id, ContactoWhitelist.empresa)
            .where(ContactoWhitelist.empresa.ilike(f"%{args['empresa']}%"))
            .where(ContactoWhitelist.meta_account_id.isnot(None))
            .limit(1)
        )).first()
        if row:
            account_id = row[0]
    if not account_id:
        return {"ok": False, "error": "No encontré el account_id de Meta. Pasa `account_id` o una `empresa` con cuenta mapeada."}
    res = await metasuite.campañas(account_id, date_preset=args.get("date_preset", "last_30d"))
    return {"ok": res.get("ok"), "account_id": account_id, "campañas": res.get("data"), "error": res.get("error")}


# ── Memoria evolutiva + recordatorios ────────────────────────────────────────


async def _resolver_contacto_id(session: AsyncSession, numero: str | None) -> int | None:
    """Resuelve un cliente por número (sin crear). Devuelve None si no existe."""
    if not numero:
        return None
    if not numero.startswith("+"):
        numero = "+" + numero.lstrip("+ ")
    row = (await session.execute(
        select(Cliente.id).where(Cliente.numero_whatsapp == numero)
    )).scalar_one_or_none()
    return row


async def handler_aprender_regla(args: dict, ctx: dict) -> dict:
    from app import memoria as mem
    session: AsyncSession = ctx["session"]
    scope = args.get("scope") or ("contacto" if args.get("contacto_numero") else "general")
    contacto_id = await _resolver_contacto_id(session, args.get("contacto_numero"))
    if scope == "contacto" and not contacto_id:
        return {"ok": False, "error": "scope=contacto pero no encontré ese número en la BD."}
    try:
        m = await mem.guardar(
            session,
            titulo=args["titulo"],
            contenido=args["contenido"],
            scope=scope,
            contacto_id=contacto_id,
            tipo=args.get("tipo", "regla"),
            creado_por=ctx.get("miembro_nombre") or "maria",
            tags=args.get("tags") or [],
        )
        return {"ok": True, "memoria_id": m.id, "scope": scope,
                "nota_para_modelo": f"Memoria #{m.id} guardada. La aplicarás en futuros turnos automáticamente."}
    except Exception as e:
        log.exception("tools_equipo.aprender_regla.fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}


async def handler_olvidar_regla(args: dict, ctx: dict) -> dict:
    from app import memoria as mem
    session: AsyncSession = ctx["session"]
    ok = await mem.desactivar(session, int(args["memoria_id"]))
    return {"ok": ok, "memoria_id": args["memoria_id"]}


async def handler_consultar_memorias(args: dict, ctx: dict) -> dict:
    from app.db.models import Memoria
    session: AsyncSession = ctx["session"]
    scope = args.get("scope")
    limite = max(1, min(int(args.get("limite", 30)), 100))
    contacto_id = await _resolver_contacto_id(session, args.get("contacto_numero"))
    stmt = select(Memoria).where(Memoria.activa.is_(True))
    if scope:
        stmt = stmt.where(Memoria.scope == scope)
    if contacto_id is not None:
        stmt = stmt.where(Memoria.contacto_id == contacto_id)
    rows = (await session.execute(stmt.order_by(Memoria.updated_at.desc()).limit(limite))).scalars().all()
    return {
        "ok": True,
        "memorias": [
            {"id": m.id, "scope": m.scope, "contacto_id": m.contacto_id,
             "titulo": m.titulo, "contenido": m.contenido, "tipo": m.tipo,
             "creado_por": m.creado_por, "updated_at": m.updated_at.isoformat() if m.updated_at else None}
            for m in rows
        ],
    }


async def handler_programar_recordatorio(args: dict, ctx: dict) -> dict:
    from app import memoria as mem
    session: AsyncSession = ctx["session"]
    try:
        vence = datetime.fromisoformat(args["vence_en"])
    except Exception:
        return {"ok": False, "error": "vence_en inválido. Formato ISO 8601 (ej '2026-05-29T10:00:00-05:00')."}
    contacto_id = await _resolver_contacto_id(session, args.get("contacto_numero"))
    try:
        r = await mem.programar(
            session,
            accion=args["accion"],
            vence_en=vence,
            contacto_id=contacto_id,
            motivo=args.get("motivo"),
            origen="manual",
            creado_por=ctx.get("miembro_nombre") or "maria",
        )
        return {"ok": True, "recordatorio_id": r.id, "vence_en": vence.isoformat()}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


async def handler_etiquetar_contacto(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "error": "falta numero"}
    if not numero.startswith("+"):
        numero = "+" + numero.lstrip("+ ")
    etiqueta = args["etiqueta"]
    motivo = (args.get("motivo") or "").strip()
    # Upsert mínimo: si no existe el cliente, lo creamos para pre-etiquetar.
    cliente = await get_or_create_cliente(session, numero)
    cliente.etiqueta = etiqueta
    cliente.etiqueta_actualizada_en = datetime.now(timezone.utc)
    cliente.etiqueta_actualizada_por = ctx.get("miembro_nombre") or "equipo"
    log.info("tools_equipo.etiquetar", numero=numero, etiqueta=etiqueta, por=cliente.etiqueta_actualizada_por)
    return {
        "ok": True, "numero": numero, "etiqueta": etiqueta, "motivo": motivo,
        "nota_para_modelo": (
            "Etiqueta aplicada. Si fue 'personal', el bot NO volverá a responderle a este número."
            if etiqueta == "personal" else
            "Etiqueta aplicada. Influye en cómo se trata al contacto en futuros mensajes."
        ),
    }


async def handler_consultar_sin_clasificar(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    limite = max(1, min(int(args.get("limite", 20)), 100))
    rows = (await session.execute(sa_text("""
        SELECT c.id, c.numero_whatsapp, COALESCE(c.nombre,'-'), c.ultimo_contacto,
               (SELECT count(*) FROM conversaciones cv WHERE cv.cliente_id=c.id AND cv.direccion='inbound')
        FROM clientes c
        WHERE c.etiqueta IS NULL AND c.bloqueado=false
          AND EXISTS (SELECT 1 FROM conversaciones cv WHERE cv.cliente_id=c.id AND cv.direccion='inbound')
        ORDER BY c.ultimo_contacto DESC NULLS LAST
        LIMIT :lim
    """), {"lim": limite})).fetchall()
    return {
        "ok": True,
        "total": len(rows),
        "contactos": [
            {"id": r[0], "numero": r[1], "nombre": r[2],
             "ultimo_contacto": r[3].isoformat() if r[3] else None,
             "mensajes_inbound": int(r[4] or 0)}
            for r in rows
        ],
    }


async def handler_consultar_recordatorios(args: dict, ctx: dict) -> dict:
    from app.db.models import Recordatorio
    session: AsyncSession = ctx["session"]
    contacto_id = await _resolver_contacto_id(session, args.get("contacto_numero"))
    limite = max(1, min(int(args.get("limite", 30)), 100))
    solo_vencidos = bool(args.get("solo_vencidos"))
    stmt = select(Recordatorio).where(Recordatorio.estado == "pendiente")
    if contacto_id is not None:
        stmt = stmt.where(Recordatorio.contacto_id == contacto_id)
    if solo_vencidos:
        stmt = stmt.where(Recordatorio.vence_en <= datetime.now(timezone.utc))
    rows = (await session.execute(stmt.order_by(Recordatorio.vence_en.asc()).limit(limite))).scalars().all()
    return {
        "ok": True,
        "recordatorios": [
            {"id": r.id, "accion": r.accion, "vence_en": r.vence_en.isoformat() if r.vence_en else None,
             "contacto_id": r.contacto_id, "motivo": r.motivo, "origen": r.origen}
            for r in rows
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

from typing import Awaitable, Callable  # noqa: E402

Handler = Callable[[dict, dict], Awaitable[dict]]

HANDLERS_EQUIPO: dict[str, Handler] = {
    # genéricas
    "responder_a_cliente": handler_responder_a_cliente,
    "consultar_chat_cliente": handler_consultar_chat_cliente,
    "consultar_chats_sin_responder": handler_consultar_chats_sin_responder,
    "marcar_numero_interno": handler_marcar_numero_interno,
    "marcar_alerta_resuelta": handler_marcar_alerta_resuelta,
    "consultar_alertas_abiertas": handler_consultar_alertas_abiertas,
    "consultar_equipo": handler_consultar_equipo,
    "pausar_bot_global": handler_pausar_bot_global,
    "reanudar_bot_global": handler_reanudar_bot_global,
    "consultar_estado_bot": handler_consultar_estado_bot,
    "remover_del_grupo": handler_remover_del_grupo,
    "agregar_al_grupo": handler_agregar_al_grupo,
    "enviar_link_grupo": handler_enviar_link_grupo,
    # DTGP
    "consultar_finanzas": handler_consultar_finanzas,
    "registrar_gasto": handler_registrar_gasto,
    "registrar_ingreso": handler_registrar_ingreso,
    "crear_cuenta_cobro": handler_crear_cuenta_cobro,
    "enviar_cuenta_cobro": handler_enviar_cuenta_cobro,
    "consultar_tareas": handler_consultar_tareas,
    "crear_tarea": handler_crear_tarea,
    "actualizar_tarea": handler_actualizar_tarea,
    "consultar_terceros": handler_consultar_terceros,
    "consultar_crm": handler_consultar_crm,
    "consultar_clientes_dtos": handler_consultar_clientes_dtos,
    "consultar_brief": handler_consultar_brief,
    "reporte_meta_ads": handler_reporte_meta_ads,
    # memoria + recordatorios (auto-edición)
    "aprender_regla": handler_aprender_regla,
    "olvidar_regla": handler_olvidar_regla,
    "consultar_memorias": handler_consultar_memorias,
    "programar_recordatorio": handler_programar_recordatorio,
    "consultar_recordatorios": handler_consultar_recordatorios,
    # etiquetado de contactos (clave para canal de Dairo)
    "etiquetar_contacto": handler_etiquetar_contacto,
    "consultar_sin_clasificar": handler_consultar_sin_clasificar,
}


# Subconjunto de tools que un CLIENTE whitelisted puede usar (read-only, su cuenta).
# El resto son solo para el equipo interno. Gate de seguridad, no solo prompt.
CLIENT_ALLOWED_TOOLS = {
    "reporte_meta_ads",
    "consultar_brief",
    "consultar_chat_cliente",
}


async def ejecutar_tool_equipo(name: str, args: dict, ctx: dict) -> dict:
    # Gate por rol: los clientes solo acceden a un subconjunto read-only.
    if (ctx.get("rol") or "").lower() == "cliente" and name not in CLIENT_ALLOWED_TOOLS:
        log.warning("tools_equipo.bloqueado_por_rol", tool=name, rol="cliente", miembro=ctx.get("miembro_nombre"))
        return {"error": "Acción no permitida para clientes. Esto lo gestiona el equipo de DTGP."}

    handler = HANDLERS_EQUIPO.get(name)
    if not handler:
        return {"error": f"Tool de equipo desconocida: {name}"}
    try:
        result = await handler(args, ctx)
        log.info("tools_equipo.ejecutada", tool=name, ok=True)
        return result
    except Exception as e:
        log.exception("tools_equipo.error", tool=name, error=str(e))
        return {"error": f"Error ejecutando {name}: {e}"}
