"""
Tools que SOLO usa el bot en modo EQUIPO (cuando Fabio le habla al bot).

Distintas de las tools_cliente porque:
- Aquí el "destinatario final" suele ser otro cliente (no quien escribe).
- Operan sobre estado del negocio: pedidos, alertas, no producto.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Awaitable, Callable

from sqlalchemy import desc, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import AlertaFabio, Cliente, Conversacion, Pedido, ProductoCache
from app.db.repos import get_or_create_cliente, guardar_conversacion
from app.logging_setup import log
from app.utils.humanizer import sleep_humano
from app.validators.output_rules import stripear_emojis
from app.whapi.client import enviar_paused, enviar_texto, enviar_typing


# ════════════════════════════════════════════════════════════════════════════
# DEFINICIONES (las que ve Claude en modo equipo)
# ════════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS_EQUIPO: list[dict] = [
    {
        "name": "responder_a_cliente",
        "description": (
            "Envía un mensaje al WhatsApp de un cliente específico. Usar cuando "
            "el miembro del equipo te pide 'dile a X que...' o 'responde al "
            "cliente Y'. Identifica el número por el contexto de alertas si "
            "te dan solo el nombre."
        ),
        "input_schema": {
            "type": "object",
            "required": ["numero_cliente", "mensaje"],
            "properties": {
                "numero_cliente": {
                    "type": "string",
                    "description": "Número E.164 del cliente (+57XXXXXXXXXX)",
                },
                "mensaje": {
                    "type": "string",
                    "description": "Texto a enviarle al cliente. Tono cálido, sin emojis.",
                },
            },
        },
    },
    {
        "name": "actualizar_pedido",
        "description": "Cambia el estado de un pedido en la DB.",
        "input_schema": {
            "type": "object",
            "required": ["pedido_id", "estado"],
            "properties": {
                "pedido_id": {"type": "integer"},
                "estado": {
                    "type": "string",
                    "enum": ["cotizacion", "datos_completos", "esperando_pago",
                             "comprobante_recibido", "confirmado", "despachado",
                             "entregado", "cancelado"],
                },
                "notas": {"type": "string", "description": "Notas opcionales del equipo"},
            },
        },
    },
    {
        "name": "marcar_alerta_resuelta",
        "description": "Marca una alerta de escalación como resuelta.",
        "input_schema": {
            "type": "object",
            "required": ["alerta_id"],
            "properties": {
                "alerta_id": {"type": "integer"},
            },
        },
    },
    {
        "name": "consultar_alertas_abiertas",
        "description": "Lista las alertas pendientes (no resueltas) recientes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "limite": {"type": "integer", "default": 10},
            },
        },
    },
    {
        "name": "consultar_pedidos",
        "description": "Lista pedidos del día, con filtros opcionales por estado.",
        "input_schema": {
            "type": "object",
            "properties": {
                "estado": {"type": "string"},
                "dias": {"type": "integer", "default": 1, "description": "Últimos N días"},
            },
        },
    },
    {
        "name": "consultar_cliente",
        "description": "Trae datos básicos de un cliente por número o nombre parcial.",
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string"},
                "nombre_parcial": {"type": "string"},
            },
        },
    },
    {
        "name": "pausar_bot_global",
        "description": (
            "DESACTIVA al bot Laura globalmente. Mientras esté pausado, "
            "el bot NO responderá a ningún cliente (los mensajes entrantes "
            "se persisten pero no se procesan). Útil cuando el admin necesita "
            "tomar el control manual, o si Laura empieza a comportarse mal. "
            "Solo lo llaman administradores. Devolverá el estado al admin. "
            "Para reactivar: llamar `reanudar_bot_global`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "razon": {"type": "string", "description": "Por qué se pausa (queda registrado en BD)"},
            },
        },
    },
    {
        "name": "reanudar_bot_global",
        "description": "REACTIVA al bot Laura. El bot vuelve a responder a clientes.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_estado_bot",
        "description": "Devuelve si el bot está activo o pausado, quién lo pausó y cuándo.",
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "consultar_producto",
        "description": (
            "Busca un producto en el catálogo por referencia (ej. SD0017, "
            "INN5682, REF-29686) o por texto en el nombre. Devuelve precio, "
            "tallas, origen (shopify/html_catalogo). Úsalo ANTES de pedirle "
            "al usuario un precio que probablemente está en el catálogo."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "ref": {"type": "string", "description": "Referencia exacta o parcial"},
                "nombre_parcial": {"type": "string", "description": "Texto a buscar en el nombre"},
            },
        },
    },
]


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════════════════


async def handler_responder_a_cliente(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    numero = args["numero_cliente"]
    mensaje = stripear_emojis(args["mensaje"])

    if not mensaje.strip():
        return {"enviado": False, "razon": "mensaje vacío tras limpiar emojis"}

    # Asegurar cliente en DB (puede ser nuevo)
    cliente = await get_or_create_cliente(session, numero)

    # Typing + delay humano antes de enviar
    try:
        await enviar_typing(numero)
        await sleep_humano(mensaje)
        await enviar_texto(numero, mensaje)
        await enviar_paused(numero)
    except Exception as e:
        log.exception("tools_equipo.responder_a_cliente.fail", error=str(e))
        return {"enviado": False, "razon": f"Error de whapi: {e}"}

    # Persistir outbound en el chat del cliente
    await guardar_conversacion(
        session,
        cliente_id=cliente.id,
        direccion="outbound",
        tipo="texto",
        contenido=mensaje,
        intent="instruccion_equipo",
        modelo="via_equipo",
        metadata={
            "via": "equipo_admin",
            "miembro_equipo": ctx.get("miembro_nombre"),
        },
    )

    return {
        "enviado": True,
        "cliente": numero,
        "preview": mensaje[:80] + ("..." if len(mensaje) > 80 else ""),
    }


async def handler_actualizar_pedido(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    pedido_id = int(args["pedido_id"])
    nuevo_estado = args["estado"]

    pedido = (await session.execute(
        select(Pedido).where(Pedido.id == pedido_id)
    )).scalar_one_or_none()
    if not pedido:
        return {"actualizado": False, "razon": f"Pedido #{pedido_id} no existe"}

    estado_anterior = pedido.estado
    valores: dict = {"estado": nuevo_estado}
    if args.get("notas"):
        valores["notas"] = args["notas"]
    if nuevo_estado == "confirmado":
        valores["confirmado_por_fabio_en"] = datetime.now(timezone.utc)

    await session.execute(
        update(Pedido).where(Pedido.id == pedido_id).values(**valores)
    )

    return {
        "actualizado": True,
        "pedido_id": pedido_id,
        "estado_anterior": estado_anterior,
        "estado_nuevo": nuevo_estado,
    }


async def handler_marcar_alerta_resuelta(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    alerta_id = int(args["alerta_id"])
    await session.execute(
        update(AlertaFabio)
        .where(AlertaFabio.id == alerta_id)
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
        .order_by(desc(AlertaFabio.created_at))
        .limit(limite)
    )).all()

    return {
        "alertas": [
            {
                "id": a.id,
                "tipo": a.tipo,
                "cliente_numero": c.numero_whatsapp if c else None,
                "cliente_nombre": c.nombre if c else None,
                "mensaje": (a.mensaje or "")[:300],
                "created_at": a.created_at.isoformat() if a.created_at else None,
            }
            for a, c in rows
        ],
    }


async def handler_consultar_pedidos(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    dias = int(args.get("dias", 1))
    desde = datetime.now(timezone.utc) - timedelta(days=dias)
    stmt = (
        select(Pedido, Cliente)
        .join(Cliente, Cliente.id == Pedido.cliente_id)
        .where(Pedido.created_at >= desde)
        .order_by(desc(Pedido.created_at))
        .limit(30)
    )
    if args.get("estado"):
        stmt = stmt.where(Pedido.estado == args["estado"])
    rows = (await session.execute(stmt)).all()

    return {
        "pedidos": [
            {
                "id": p.id,
                "cliente_numero": c.numero_whatsapp,
                "cliente_nombre": c.nombre,
                "total": str(p.total),
                "estado": p.estado,
                "ciudad": p.ciudad,
                "barrio": p.barrio,
                "metodo_pago": p.metodo_pago,
                "created_at": p.created_at.isoformat(),
            }
            for p, c in rows
        ],
    }


async def handler_consultar_cliente(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    stmt = select(Cliente)
    if numero := args.get("numero"):
        stmt = stmt.where(Cliente.numero_whatsapp == numero)
    elif nombre := args.get("nombre_parcial"):
        stmt = stmt.where(Cliente.nombre.ilike(f"%{nombre}%"))
    else:
        return {"clientes": [], "nota": "Pasa numero o nombre_parcial"}

    rows = (await session.execute(stmt.limit(5))).scalars().all()
    return {
        "clientes": [
            {
                "id": c.id,
                "numero": c.numero_whatsapp,
                "nombre": c.nombre,
                "ciudad": c.ciudad,
                "barrio": c.barrio,
                "bloqueado": c.bloqueado,
                "ultimo_contacto": c.ultimo_contacto.isoformat() if c.ultimo_contacto else None,
            }
            for c in rows
        ],
    }


async def handler_pausar_bot_global(args: dict, ctx: dict) -> dict:
    """Marca bot_estado.activo=false. El webhook handler chequea esto antes
    de procesar mensajes de cliente."""
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    razon = (args.get("razon") or "Pausado por administrador").strip()
    miembro_nombre = ctx.get("miembro_nombre") or "admin"
    await session.execute(sa_text(
        "UPDATE bot_estado SET activo=false, pausado_por=:p, "
        "pausado_en=now(), razon=:r, actualizado_en=now() WHERE id=1"
    ), {"p": miembro_nombre, "r": razon})
    log.warning("tools_equipo.bot_pausado", por=miembro_nombre, razon=razon)
    return {
        "pausado": True,
        "por": miembro_nombre,
        "razon": razon,
        "nota": "El bot dejará de responder a clientes. Para reactivar usa `reanudar_bot_global`.",
    }


async def handler_reanudar_bot_global(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    miembro_nombre = ctx.get("miembro_nombre") or "admin"
    await session.execute(sa_text(
        "UPDATE bot_estado SET activo=true, pausado_por=null, pausado_en=null, "
        "razon=null, actualizado_en=now() WHERE id=1"
    ))
    log.warning("tools_equipo.bot_reanudado", por=miembro_nombre)
    return {
        "reanudado": True,
        "por": miembro_nombre,
        "nota": "El bot vuelve a responder a clientes.",
    }


async def handler_consultar_estado_bot(args: dict, ctx: dict) -> dict:
    from sqlalchemy import text as sa_text
    session: AsyncSession = ctx["session"]
    row = (await session.execute(sa_text(
        "SELECT activo, pausado_por, pausado_en, razon FROM bot_estado WHERE id=1"
    ))).first()
    if not row:
        return {"activo": True, "nota": "Sin registro de estado, asumido activo."}
    return {
        "activo": bool(row[0]),
        "pausado_por": row[1],
        "pausado_en": row[2].isoformat() if row[2] else None,
        "razon": row[3],
    }


async def handler_consultar_producto(args: dict, ctx: dict) -> dict:
    """Busca productos en productos_cache por ref exacta/parcial o nombre."""
    session: AsyncSession = ctx["session"]
    ref = (args.get("ref") or "").strip()
    nombre = (args.get("nombre_parcial") or "").strip()

    if not ref and not nombre:
        return {"productos": [], "razon": "no se pasó ref ni nombre_parcial"}

    stmt = select(ProductoCache)
    filtros = []
    if ref:
        # Match exacto primero, sino ILIKE
        filtros.append(ProductoCache.ref.ilike(f"%{ref}%"))
    if nombre:
        filtros.append(ProductoCache.nombre.ilike(f"%{nombre}%"))
    if len(filtros) == 1:
        stmt = stmt.where(filtros[0])
    else:
        from sqlalchemy import or_ as sa_or
        stmt = stmt.where(sa_or(*filtros))

    rows = (await session.execute(stmt.limit(8))).scalars().all()
    return {
        "productos": [
            {
                "ref": p.ref,
                "nombre": p.nombre,
                "precio_detal": float(p.precio_detal) if p.precio_detal else None,
                "precio_mayor": float(p.precio_mayor) if p.precio_mayor else None,
                "tallas": p.tallas or [],
                "colores": p.colores or [],
                "origen": p.origen,
            }
            for p in rows
        ],
    }


# ════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

Handler = Callable[[dict, dict], Awaitable[dict]]

HANDLERS_EQUIPO: dict[str, Handler] = {
    "responder_a_cliente": handler_responder_a_cliente,
    "actualizar_pedido": handler_actualizar_pedido,
    "marcar_alerta_resuelta": handler_marcar_alerta_resuelta,
    "consultar_alertas_abiertas": handler_consultar_alertas_abiertas,
    "consultar_pedidos": handler_consultar_pedidos,
    "consultar_cliente": handler_consultar_cliente,
    "consultar_producto": handler_consultar_producto,
    "pausar_bot_global": handler_pausar_bot_global,
    "reanudar_bot_global": handler_reanudar_bot_global,
    "consultar_estado_bot": handler_consultar_estado_bot,
}


async def ejecutar_tool_equipo(name: str, args: dict, ctx: dict) -> dict:
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
