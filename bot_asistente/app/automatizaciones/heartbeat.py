"""Heartbeat dinámico (pilar openclaw).

Cada cierto rato (cron), Dairo "despierta", recoge el estado actual (alertas
abiertas, recordatorios vencidos, citas próximas, chats sin responder), y le
pide a Claude que decida UNA cosa útil que hacer — o silencio si nada amerita.

A diferencia de los crones fijos (reporte_ceo, etc.), aquí el AGENTE decide.
Para que sea seguro:
  - Toolset ACOTADO (4 tools): atender recordatorio, notificar al equipo,
    agregar a memoria, silencio. NO tiene acceso a finanzas / Sheets / etc.
  - Conservador por defecto: "silencio" si hay duda.
  - Una sola acción por invocación.
  - Respeta horario (no escribe a la madrugada).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import text as sa_text

from app.claude.anthropic_client import get_anthropic_client
from app.config import get_settings
from app.db.models import AlertaFabio, Cita, Cliente, Conversacion, Recordatorio
from app import memoria as mem
from app.logging_setup import log
from app.utils.humanizer import dentro_horario
from app.whapi.client import enviar_texto

settings = get_settings()


# ─── Recolección de estado ────────────────────────────────────────────────────


async def _recolectar_estado(session: AsyncSession) -> dict[str, Any]:
    """Resumen de "qué pasa ahora" para que el agente decida."""
    ahora = datetime.now(timezone.utc)

    # Alertas abiertas (top 8 más viejas)
    alertas_rows = (await session.execute(
        select(AlertaFabio, Cliente)
        .join(Cliente, Cliente.id == AlertaFabio.cliente_id, isouter=True)
        .where(AlertaFabio.resuelto.is_(False))
        .order_by(AlertaFabio.created_at.asc())
        .limit(8)
    )).all()
    alertas = [
        {
            "alerta_id": a.id,
            "tipo": a.tipo,
            "edad_horas": int((ahora - a.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 3600) if a.created_at else 0,
            "cliente_numero": c.numero_whatsapp if c else None,
            "cliente_nombre": c.nombre if c else None,
            "mensaje": (a.mensaje or "")[:200],
        }
        for a, c in alertas_rows
    ]

    # Recordatorios vencidos (pendientes con vence_en <= ahora)
    recs = await mem.vencidos(session, ventana_horas=72)
    # Hidratar con número del contacto
    recs_out = []
    for r in recs[:10]:
        numero, nombre = None, None
        if r.contacto_id:
            row = (await session.execute(
                select(Cliente.numero_whatsapp, Cliente.nombre).where(Cliente.id == r.contacto_id)
            )).first()
            if row:
                numero, nombre = row[0], row[1]
        recs_out.append({
            "recordatorio_id": r.id,
            "accion": r.accion,
            "motivo": r.motivo,
            "vence_en": r.vence_en.isoformat() if r.vence_en else None,
            "contacto_numero": numero,
            "contacto_nombre": nombre,
        })

    # Citas próximas (24 h)
    citas_rows = (await session.execute(
        select(Cita, Cliente)
        .join(Cliente, Cliente.id == Cita.cliente_id)
        .where(Cita.estado.in_(["agendada", "reprogramada"]))
        .where(Cita.fecha_inicio >= ahora)
        .where(Cita.fecha_inicio <= ahora + timedelta(hours=24))
        .order_by(Cita.fecha_inicio.asc())
        .limit(10)
    )).all()
    citas = [
        {
            "cita_id": c.id,
            "fecha": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "nombre": c.nombre or (cl.nombre if cl else None),
            "negocio": c.negocio,
            "estado": c.estado,
        }
        for c, cl in citas_rows
    ]

    # Chats con último mensaje inbound > 2 h sin respuesta
    rows = (await session.execute(sa_text("""
        WITH ult AS (
            SELECT DISTINCT ON (cliente_id) cliente_id, direccion, contenido, timestamp
            FROM conversaciones
            WHERE timestamp > NOW() - INTERVAL '24 hours'
            ORDER BY cliente_id, timestamp DESC
        )
        SELECT u.cliente_id, c.numero_whatsapp, c.nombre,
               EXTRACT(EPOCH FROM (NOW() - u.timestamp))/3600 AS horas,
               LEFT(u.contenido, 150)
        FROM ult u JOIN clientes c ON c.id = u.cliente_id
        WHERE u.direccion='inbound' AND c.bloqueado=false
          AND u.timestamp < NOW() - INTERVAL '2 hours'
        ORDER BY u.timestamp ASC LIMIT 8
    """))).fetchall()
    chats_pendientes = [
        {"cliente_id": r[0], "numero": r[1], "nombre": r[2] or None,
         "horas_sin_responder": round(float(r[3]), 1), "ultimo_mensaje": (r[4] or "").strip()}
        for r in rows
    ]

    from zoneinfo import ZoneInfo as _ZI
    from app.config import get_settings as _gs
    return {
        "hora_actual": ahora.astimezone(_ZI(_gs().tz)).isoformat(),
        "alertas_abiertas": alertas,
        "recordatorios_vencidos": recs_out,
        "citas_proximas_24h": citas,
        "chats_inbound_sin_responder_2h": chats_pendientes,
    }


# ─── Toolset acotado del heartbeat ───────────────────────────────────────────


HEARTBEAT_TOOLS: list[dict] = [
    {
        "name": "atender_recordatorio_enviando_mensaje",
        "description": (
            "Envía un mensaje de WhatsApp al contacto asociado a un recordatorio "
            "vencido y marca el recordatorio como atendido. Solo si el recordatorio "
            "tiene contacto_numero. El texto debe ser cálido, breve y útil."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "recordatorio_id": {"type": "integer"},
                "mensaje": {"type": "string", "description": "Texto cálido y breve para el contacto"},
            },
            "required": ["recordatorio_id", "mensaje"],
        },
    },
    {
        "name": "notificar_al_equipo",
        "description": (
            "Manda una nota interna al equipo (Dairo/Stiven) cuando hay algo que "
            "amerita atención humana. Ej: muchos chats sin responder, alerta vieja, "
            "patrón raro. NO envía al cliente — solo al equipo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "destinatario": {"type": "string", "description": "+57... (default: Dairo)"},
                "mensaje": {"type": "string"},
            },
            "required": ["mensaje"],
        },
    },
    {
        "name": "agregar_a_memoria",
        "description": "Guarda un aprendizaje general (scope=general) que quieras recordar en futuros heartbeats.",
        "input_schema": {
            "type": "object",
            "properties": {
                "titulo": {"type": "string"},
                "contenido": {"type": "string"},
            },
            "required": ["titulo", "contenido"],
        },
    },
    {
        "name": "silencio",
        "description": "No hacer nada en este ciclo. Úsalo si no hay nada que realmente amerite acción. Es la opción CONSERVADORA por defecto.",
        "input_schema": {
            "type": "object",
            "properties": {"razon": {"type": "string"}},
        },
    },
]


# ─── Ejecutor de la decisión ─────────────────────────────────────────────────


async def _ejecutar(name: str, args: dict, session: AsyncSession) -> dict:
    if name == "atender_recordatorio_enviando_mensaje":
        rid = int(args["recordatorio_id"])
        # Verificar que sigue pendiente + tiene contacto
        rec = (await session.execute(
            select(Recordatorio).where(Recordatorio.id == rid, Recordatorio.estado == "pendiente")
        )).scalar_one_or_none()
        if not rec or not rec.contacto_id:
            return {"ok": False, "error": "recordatorio inválido o sin contacto"}
        cli = (await session.execute(
            select(Cliente).where(Cliente.id == rec.contacto_id)
        )).scalar_one_or_none()
        if not cli:
            return {"ok": False, "error": "contacto no encontrado"}
        try:
            await enviar_texto(cli.numero_whatsapp, args["mensaje"])
        except Exception as e:
            return {"ok": False, "error": f"whapi fail: {e}"}
        await mem.marcar_atendido(session, rid, notas=f"heartbeat: {args['mensaje'][:80]}")
        return {"ok": True, "enviado_a": cli.numero_whatsapp}

    if name == "notificar_al_equipo":
        destino = args.get("destinatario") or settings.dairo_phone
        try:
            await enviar_texto(destino, "[Dairo · heartbeat]\n" + args["mensaje"])
        except Exception as e:
            return {"ok": False, "error": f"whapi fail: {e}"}
        return {"ok": True, "enviado_a": destino}

    if name == "agregar_a_memoria":
        m = await mem.guardar(
            session,
            titulo=args["titulo"],
            contenido=args["contenido"],
            scope="general",
            tipo="aprendizaje",
            creado_por="heartbeat",
        )
        return {"ok": True, "memoria_id": m.id}

    if name == "silencio":
        return {"ok": True, "silencio": True, "razon": args.get("razon", "nada amerita acción")}

    return {"ok": False, "error": f"tool desconocida: {name}"}


# ─── Entrypoint del cron ─────────────────────────────────────────────────────


SYSTEM_HEARTBEAT = """
Eres **Dairo en modo proactivo (heartbeat)**. Cada cierto rato te despiertan y te
muestran qué está pasando. Tu trabajo es decidir **UNA sola acción útil** — o
guardar silencio si nada amerita.

Reglas:
- Sé **conservadora**. Si dudas → `silencio`. No molestes al equipo ni a clientes sin razón.
- Solo actúa sobre cosas con señal CLARA:
  - Un recordatorio vencido con su acción definida → atiéndelo.
  - Una alerta MUY vieja (>12 h) sin movimiento → notifica al equipo.
  - Un chat inbound sin responder muchas horas → notifica si vale la pena.
- NO inventes información. NO escribas a clientes con "saludos" sin razón.
- Solo UNA tool por turno.
- Si vas a enviar mensaje a un contacto, escribe en tono cálido, breve, primera persona.
""".strip()


async def accion_heartbeat(session: AsyncSession, params: dict) -> dict:
    """Pilar openclaw — el agente decide qué hacer.

    params (opcionales):
      respetar_horario: bool (default True) — si False, ignora horario nocturno.
    """
    respetar_horario = bool(params.get("respetar_horario", True))
    if respetar_horario and not dentro_horario():
        return {"ok": True, "skip": True, "razon": "fuera de horario"}

    estado = await _recolectar_estado(session)
    nada_que_hacer = (
        not estado["alertas_abiertas"]
        and not estado["recordatorios_vencidos"]
        and not estado["chats_inbound_sin_responder_2h"]
    )
    if nada_que_hacer:
        return {"ok": True, "skip": True, "razon": "nada que atender"}

    client = get_anthropic_client()
    try:
        resp = await client.messages.create(
            model=settings.claude_model_principal,
            max_tokens=settings.claude_max_tokens_output,
            system=SYSTEM_HEARTBEAT,
            tools=HEARTBEAT_TOOLS,
            tool_choice={"type": "any"},   # obligado a llamar una tool
            messages=[{"role": "user", "content": "Estado actual:\n```json\n" + json.dumps(estado, default=str, ensure_ascii=False) + "\n```\n\nDecide UNA acción."}],
        )
    except Exception as e:
        log.exception("heartbeat.claude_fail", error=str(e))
        return {"ok": False, "error": str(e)[:200]}

    # Buscar el tool_use (forzado por tool_choice=any)
    tool_use = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
    if not tool_use:
        return {"ok": True, "skip": True, "razon": "el agente no eligió ninguna acción"}

    name = tool_use.name
    args = dict(tool_use.input or {})
    log.info("heartbeat.decision", tool=name, args=args)
    res = await _ejecutar(name, args, session)
    return {"ok": res.get("ok", False), "accion": name, "resultado": res, "estado_resumen": {
        "alertas": len(estado["alertas_abiertas"]),
        "recordatorios": len(estado["recordatorios_vencidos"]),
        "chats_pendientes": len(estado["chats_inbound_sin_responder_2h"]),
    }}
