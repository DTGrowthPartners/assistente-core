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
from app.db.repos import get_or_create_cliente, guardar_conversacion, pausar_bot
from app.logging_setup import log
from app.utils.humanizer import sleep_humano
from app.validators.output_rules import stripear_emojis
from app.whapi.client import enviar_imagen_url, enviar_paused, enviar_texto, enviar_typing


# ════════════════════════════════════════════════════════════════════════════
# DEFINICIONES (las que ve Claude en modo equipo)
# ════════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS_EQUIPO: list[dict] = [
    {
        "name": "responder_a_cliente",
        "description": (
            "Envía un mensaje al WhatsApp de un cliente. Usar cuando el admin "
            "dice 'dile a X...', 'responde al cliente Y...', 'continúa la "
            "conversación con Z'. Identifica el número por el contexto si te "
            "dan solo el nombre.\n\n"
            "IMPORTANTE — `pausar_chat`:\n"
            "- Por defecto FALSE: el bot Laura sigue respondiendo automáticamente "
            "  cuando el cliente conteste. Es lo que el admin quiere casi siempre.\n"
            "- Solo TRUE si el admin dice EXPLÍCITAMENTE algo como 'yo sigo el "
            "  chat', 'no le contestes más, lo manejo yo', 'tomo yo esta "
            "  conversación', o si la situación es muy delicada y el admin "
            "  necesita pleno control."
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
                "pausar_chat": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "Si TRUE, pausa Laura 1h para este cliente (admin toma "
                        "el control). Default FALSE: Laura sigue respondiendo "
                        "cuando el cliente conteste."
                    ),
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
        "name": "consultar_chats_sin_responder",
        "description": (
            "Lista los chats donde el ÚLTIMO mensaje fue del cliente (inbound) "
            "y nadie le ha respondido todavía (ni el bot ni una asesora). Es "
            "decir, clientes esperando respuesta. ÚSALO cuando el admin pregunte "
            "'tienes mensajes pendientes', 'qué chats sin contestar tengo', "
            "'qué falta', 'a quién le debo respuesta'. Ordena por antigüedad "
            "(el más viejo primero, urgente)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "max_resultados": {"type": "integer", "description": "Default 15", "default": 15},
                "horas_max": {"type": "integer", "description": "Solo chats con último msg en últimas N horas (default 48)", "default": 48},
            },
        },
    },
    {
        "name": "consultar_chat_cliente",
        "description": (
            "Lee el historial reciente del chat de un cliente (inbound, outbound "
            "del bot, y mensajes humanos del admin). ÚSALO cuando el admin "
            "pregunte cosas como 'revisa el chat de X', '¿qué le dijimos a Y?', "
            "'estamos esperando pago de Z?'. Devuelve los últimos N mensajes "
            "ordenados cronológicamente con dirección (cliente/bot/humano) y "
            "timestamp."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero": {"type": "string", "description": "Número del cliente, ej +573135536355"},
                "nombre_parcial": {"type": "string", "description": "Alternativa: nombre parcial del cliente"},
                "max_mensajes": {"type": "integer", "description": "Máximo de mensajes a traer (default 25)"},
            },
        },
    },
    {
        "name": "consultar_equipo",
        "description": (
            "Devuelve la lista de miembros activos del equipo interno "
            "(administradores, asesoras). Útil cuando el admin pregunta "
            "'¿cuántos administradores tienes?' o '¿quién está en el equipo?'."
        ),
        "input_schema": {"type": "object", "properties": {}},
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
        "name": "enviar_foto_producto_a_cliente",
        "description": (
            "Envía la foto de un producto al cliente. Busca el producto por "
            "referencia (ej INN5658, SD0017) en el catálogo y manda su imagen "
            "por WhatsApp con un caption opcional. ÚSALO cuando el admin diga "
            "cosas como 'envíale las fotos de las bermudas X, Y, Z'. Puedes "
            "llamarla varias veces (una por foto) si son varias prendas.\n\n"
            "Por defecto NO pausa el bot — Laura sigue respondiendo cuando el "
            "cliente conteste. Solo pasa `pausar_chat=true` si el admin dijo "
            "que él va a manejar la conversación."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_cliente": {"type": "string", "description": "Número +57..."},
                "ref": {"type": "string", "description": "Referencia del producto, ej INN5658"},
                "caption": {"type": "string", "description": "Texto opcional bajo la foto (1-2 líneas)"},
                "pausar_chat": {
                    "type": "boolean",
                    "default": False,
                    "description": "Si TRUE, pausa Laura 1h para este cliente. Default FALSE.",
                },
            },
            "required": ["numero_cliente", "ref"],
        },
    },
    {
        "name": "marcar_numero_interno",
        "description": (
            "Marca un número como INTERNO (bodega, asesora, sistema, etc.) "
            "para que el bot Laura NUNCA le responda como si fuera cliente. "
            "ÚSALO cuando el admin diga 'ignora a +57XXX', 'agrega a internos', "
            "'ese número es de bodega', 'no le respondas más a X', etc. "
            "Inserta en la tabla numeros_internos y refresca el cache para "
            "que tome efecto en <1 segundo. Si ya existe, lo reactiva. "
            "Bonus: pausa al cliente 24h para cancelar cualquier respuesta "
            "pendiente del humanizer (delay 60-180s)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["numero"],
            "properties": {
                "numero": {"type": "string", "description": "Número E.164, ej +573004602945"},
                "nombre": {"type": "string", "description": "Descripción ej 'Bodega Innovación Centro'"},
                "razon": {"type": "string", "description": "Por qué se marca interno (queda en BD para auditoría)"},
            },
        },
    },
    {
        "name": "crear_pedido_manual",
        "description": (
            "Registra retroactivamente un pedido en la BD cuando la venta se "
            "cerró conversacionalmente con un cliente (asesora humana) y NO "
            "pasó por el flujo automático del bot. Útil cuando el admin dice "
            "'ya cerré la venta de X, registrala' o 'el pago de Y ya entró, "
            "confirma su pedido'. Devuelve el pedido_id creado. Para items, "
            "pasa una lista de objetos {nombre, talla, color, precio, cantidad}."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "numero_cliente": {"type": "string", "description": "Número +57..."},
                "items": {
                    "type": "array",
                    "description": "Lista de items del pedido",
                    "items": {
                        "type": "object",
                        "properties": {
                            "nombre": {"type": "string"},
                            "ref": {"type": "string"},
                            "talla": {"type": "string"},
                            "color": {"type": "string"},
                            "precio": {"type": "number"},
                            "cantidad": {"type": "number"},
                        },
                        "required": ["nombre", "precio"],
                    },
                },
                "subtotal": {"type": "number"},
                "domicilio": {"type": "number", "description": "Valor del envío. 0 si lo cobra la transportadora al destinatario."},
                "total": {"type": "number"},
                "estado": {
                    "type": "string",
                    "description": "datos_completos|esperando_pago|confirmado|despachado|entregado",
                    "default": "confirmado",
                },
                "direccion_envio": {"type": "string"},
                "ciudad": {"type": "string"},
                "barrio": {"type": "string"},
                "metodo_pago": {"type": "string", "description": "contraentrega|transferencia_bancolombia|transferencia_nequi|consignacion|addi|otro"},
                "banco": {"type": "string"},
                "transportadora": {"type": "string", "description": "Coordinadora|Envia|Interrapidisimo|Servientrega|domicilio_local|etc."},
                "cedula_cliente": {"type": "string"},
                "email_cliente": {"type": "string"},
                "notas": {"type": "string"},
            },
            "required": ["numero_cliente", "items", "total"],
        },
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

    # Pausa OPCIONAL: solo si el admin lo pidió explícitamente vía pausar_chat.
    # Default False — Laura sigue respondiendo automáticamente.
    pausar = bool(args.get("pausar_chat", False))
    if pausar:
        await pausar_bot(
            session,
            cliente_id=cliente.id,
            horas=1,
            razon=f"admin {ctx.get('miembro_nombre','equipo')} tomó el chat vía bot equipo",
        )

    # Resolver alertas abiertas del mismo cliente (asumimos que al responder
    # el admin está cerrando el ciclo de duda). Si necesita escalar otra cosa
    # nueva, el bot principal creará una alerta fresca con detalles distintos.
    from sqlalchemy import text as sa_text
    resueltas = await session.execute(sa_text("""
        UPDATE alertas_fabio
        SET resuelto = true, resuelto_en = now()
        WHERE cliente_id = :cid AND resuelto = false
        RETURNING id
    """), {"cid": cliente.id})
    ids_cerradas = [r[0] for r in resueltas.fetchall()]
    if ids_cerradas:
        log.info("tools_equipo.alertas_auto_resueltas", cliente_id=cliente.id, ids=ids_cerradas)

    return {
        "enviado": True,
        "cliente": numero,
        "preview": mensaje[:80] + ("..." if len(mensaje) > 80 else ""),
        "bot_pausado": pausar,
        "alertas_resueltas": ids_cerradas,
    }


async def _notificar_pedido_al_grupo(session: AsyncSession, pedido: Pedido) -> bool:
    """Envía un mensaje al grupo configurado con el resumen del pedido confirmado.

    Idempotente: si pedido.notificado_grupo_en ya está seteado, no hace nada.
    Errores de whapi se loggean pero NO rompen el flujo del handler.
    Devuelve True si envió, False si saltó o falló.
    """
    from app.config import get_settings
    s = get_settings()
    grupo = (s.grupo_pedidos_confirmados_id or "").strip()
    if not grupo:
        return False
    if pedido.notificado_grupo_en:
        return False  # ya notificado antes

    # Resolver cliente (nombre + número)
    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == pedido.cliente_id)
    )).scalar_one_or_none()
    nombre = (cliente.nombre if cliente else None) or "(sin nombre)"
    numero = cliente.numero_whatsapp if cliente else "(?)"

    # Resumen de items
    items = pedido.items or []
    lineas_items = []
    for it in items[:5]:
        ref = (it.get("ref") or "").strip()
        nom = (it.get("nombre") or "").strip()
        talla = it.get("talla") or ""
        color = it.get("color") or ""
        cant = it.get("cantidad") or 1
        partes = [p for p in [f"{cant}x" if cant != 1 else None, ref or nom, f"T{talla}" if talla else None, color] if p]
        lineas_items.append("- " + " ".join(partes))
    if len(items) > 5:
        lineas_items.append(f"- ... +{len(items) - 5} más")

    envio = "—"
    if pedido.ciudad:
        envio = pedido.ciudad
        if pedido.barrio:
            envio += f" / {pedido.barrio}"

    total_fmt = f"${int(pedido.total or 0):,}".replace(",", ".")
    mensaje = (
        f"PEDIDO CONFIRMADO  #{pedido.id}\n"
        f"Cliente: {nombre} ({numero})\n"
        f"Envío: {envio}\n"
        f"Pago: {pedido.metodo_pago or '—'}\n"
        f"Total: {total_fmt}\n"
        + ("\n".join(lineas_items) if lineas_items else "")
    )

    try:
        await enviar_texto(grupo, mensaje)
    except Exception as e:
        log.warning("tools_equipo.notif_grupo.fail", pedido_id=pedido.id, error=str(e))
        return False

    # Marcar notificado_grupo_en
    pedido.notificado_grupo_en = datetime.now(timezone.utc)
    log.info("tools_equipo.notif_grupo.ok", pedido_id=pedido.id, grupo=grupo)
    return True


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

    # Notificar al grupo si pasó a confirmado (solo si no se había notificado antes)
    notificado = False
    if nuevo_estado == "confirmado" and estado_anterior != "confirmado":
        # recargar el pedido con los valores nuevos
        await session.refresh(pedido)
        notificado = await _notificar_pedido_al_grupo(session, pedido)

    return {
        "actualizado": True,
        "pedido_id": pedido_id,
        "estado_anterior": estado_anterior,
        "estado_nuevo": nuevo_estado,
        "notificado_al_grupo": notificado,
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


async def handler_enviar_foto_producto_a_cliente(args: dict, ctx: dict) -> dict:
    """Envía la foto de un producto al cliente desde el bot equipo.

    Útil cuando el admin dice 'mándale la foto de la INN5658 a +573...'.
    Pausa el bot principal 1h para evitar que responda encima al cliente.
    """
    session: AsyncSession = ctx["session"]
    numero = args["numero_cliente"]
    ref = args["ref"].upper().strip()
    caption_extra = (args.get("caption") or "").strip()

    prod = (await session.execute(
        select(ProductoCache).where(ProductoCache.ref == ref)
    )).scalar_one_or_none()
    if not prod:
        return {"enviado": False, "razon": f"Ref {ref} no encontrada en catálogo"}
    if not prod.imagen_url:
        return {"enviado": False, "razon": f"Producto {ref} sin imagen URL"}

    # Caption: precio + extra del admin
    precio_str = f" — ${int(prod.precio_detal):,}".replace(",", ".") if prod.precio_detal else ""
    base = f"{prod.nombre} ({prod.ref}){precio_str}"
    caption = f"{base}\n\n{caption_extra}" if caption_extra else base
    caption = stripear_emojis(caption)

    cliente = await get_or_create_cliente(session, numero)

    try:
        await enviar_imagen_url(numero, prod.imagen_url, caption=caption)
    except Exception as e:
        log.exception("tools_equipo.foto_producto.fail", ref=ref, numero=numero, error=str(e))
        return {"enviado": False, "razon": f"Error whapi: {e}"}

    await guardar_conversacion(
        session,
        cliente_id=cliente.id,
        direccion="outbound",
        tipo="imagen",
        contenido=caption,
        media_url=prod.imagen_url,
        intent="instruccion_equipo",
        modelo="via_equipo",
        metadata={
            "via": "equipo_admin",
            "ref": ref,
            "miembro_equipo": ctx.get("miembro_nombre"),
        },
    )

    # Pausa OPCIONAL — solo si el admin pidió tomar el control
    pausar = bool(args.get("pausar_chat", False))
    if pausar:
        await pausar_bot(
            session,
            cliente_id=cliente.id,
            horas=1,
            razon=f"admin {ctx.get('miembro_nombre','equipo')} tomó el chat vía bot equipo (foto)",
        )

    log.info("tools_equipo.foto_producto.enviada", ref=ref, numero=numero, cliente_id=cliente.id, pausado=pausar)

    return {
        "enviado": True,
        "ref": ref,
        "cliente": numero,
        "precio": float(prod.precio_detal) if prod.precio_detal else None,
        "bot_pausado": pausar,
    }


async def handler_marcar_numero_interno(args: dict, ctx: dict) -> dict:
    """Marca un número como interno (bodega/asesora/sistema) y pausa 24h al
    cliente para cancelar respuestas pendientes del humanizer."""
    session: AsyncSession = ctx["session"]
    numero = (args.get("numero") or "").strip()
    if not numero:
        return {"ok": False, "razon": "numero vacío"}
    # Normalizar: asegurar prefijo +
    if not numero.startswith("+"):
        numero = "+" + numero.lstrip("+ ")

    nombre = (args.get("nombre") or "").strip() or "Número interno (sin nombre)"
    razon = (args.get("razon") or "").strip() or f"Marcado interno por {ctx.get('miembro_nombre','equipo')} via bot equipo"

    from sqlalchemy import text as sa_text
    res = await session.execute(sa_text("""
        INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo)
        VALUES (:n, :nom, :raz, true)
        ON CONFLICT (numero_whatsapp) DO UPDATE
        SET nombre = COALESCE(EXCLUDED.nombre, numeros_internos.nombre),
            razon = COALESCE(EXCLUDED.razon, numeros_internos.razon),
            activo = true
        RETURNING id
    """), {"n": numero, "nom": nombre, "raz": razon})
    row = res.fetchone()
    interno_id = row[0] if row else None

    # Invalidar cache del directorio para que tome efecto inmediato
    try:
        from app.equipo.directorio import invalidar_cache
        invalidar_cache()
    except Exception:
        pass

    # Pausar 24h al cliente si existe — esto cancela respuestas pendientes
    # del humanizer que pudieran estar en cola.
    cliente = (await session.execute(
        select(Cliente).where(Cliente.numero_whatsapp == numero)
    )).scalar_one_or_none()
    pausa_aplicada = False
    if cliente:
        await pausar_bot(
            session,
            cliente_id=cliente.id,
            horas=24,
            razon=f"marcado como interno: {nombre}",
        )
        pausa_aplicada = True

    log.info(
        "tools_equipo.marcar_interno",
        numero=numero,
        interno_id=interno_id,
        cliente_id=cliente.id if cliente else None,
        miembro=ctx.get("miembro_nombre"),
    )

    return {
        "ok": True,
        "numero": numero,
        "interno_id": interno_id,
        "pausa_cliente_24h": pausa_aplicada,
        "nota": "Cache del directorio invalidado. El bot dejará de responder a este número en menos de 1 segundo.",
    }


async def handler_crear_pedido_manual(args: dict, ctx: dict) -> dict:
    """Registra retroactivamente un pedido cerrado vía conversación humana.

    Idempotente: si ya hay un pedido del mismo cliente con mismo total en
    los últimos 24h, devuelve el existente sin duplicar.
    """
    session: AsyncSession = ctx["session"]
    numero = args["numero_cliente"]
    total = float(args["total"])
    items = args["items"]
    if not items:
        return {"creado": False, "error": "items vacío"}

    cliente = await get_or_create_cliente(session, numero)

    # Dedupe: pedido del mismo cliente con mismo total en últimas 24h
    ventana = datetime.now(timezone.utc) - timedelta(hours=24)
    existente = (await session.execute(
        select(Pedido).where(
            Pedido.cliente_id == cliente.id,
            Pedido.total == total,
            Pedido.created_at >= ventana,
        ).order_by(Pedido.id.desc()).limit(1)
    )).scalar_one_or_none()
    if existente:
        return {
            "creado": False,
            "ya_existia_pedido_id": existente.id,
            "estado_actual": existente.estado,
            "razon": "Ya hay un pedido de este cliente con el mismo total en las últimas 24h",
        }

    cedula = (args.get("cedula_cliente") or "").strip()
    cedula_digits = "".join(ch for ch in cedula if ch.isdigit()) or None
    email = (args.get("email_cliente") or "").strip().lower() or None

    pedido = Pedido(
        cliente_id=cliente.id,
        items=items,
        subtotal=args.get("subtotal") or total,
        domicilio=args.get("domicilio") or 0,
        total=total,
        estado=args.get("estado") or "confirmado",
        direccion_envio=args.get("direccion_envio"),
        ciudad=args.get("ciudad"),
        barrio=args.get("barrio"),
        metodo_pago=args.get("metodo_pago"),
        banco=args.get("banco"),
        cedula_cliente=cedula_digits,
        email_cliente=email,
        notas=(args.get("notas") or "")
              + (f" [Transportadora: {args['transportadora']}]" if args.get("transportadora") else "")
              + f" [Registrado retroactivamente por {ctx.get('miembro_nombre','equipo')}]",
        confirmado_por_fabio_en=datetime.now(timezone.utc)
            if (args.get("estado") or "confirmado") in ("confirmado", "despachado", "entregado")
            else None,
    )
    session.add(pedido)
    await session.flush()

    # Backfill datos del cliente si faltan
    if cedula_digits and not cliente.cedula:
        cliente.cedula = cedula_digits
    if email and not cliente.email:
        cliente.email = email
    if args.get("ciudad") and not cliente.ciudad:
        cliente.ciudad = args["ciudad"]
    if args.get("barrio") and not cliente.barrio:
        cliente.barrio = args["barrio"]

    log.info(
        "tools_equipo.pedido_manual_creado",
        pedido_id=pedido.id,
        cliente_id=cliente.id,
        total=total,
        miembro=ctx.get("miembro_nombre"),
    )

    # Si el pedido nace ya confirmado/despachado/entregado, notificar al grupo
    notificado = False
    if pedido.estado in ("confirmado", "despachado", "entregado"):
        notificado = await _notificar_pedido_al_grupo(session, pedido)

    return {
        "creado": True,
        "pedido_id": pedido.id,
        "cliente": numero,
        "total": total,
        "estado": pedido.estado,
        "items_count": len(items),
        "notificado_al_grupo": notificado,
    }


async def handler_consultar_chats_sin_responder(args: dict, ctx: dict) -> dict:
    """Lista chats donde el último mensaje es del cliente (esperando respuesta)."""
    session: AsyncSession = ctx["session"]
    max_resultados = int(args.get("max_resultados") or 15)
    horas_max = int(args.get("horas_max") or 48)
    from sqlalchemy import text as sa_text
    rows = (await session.execute(sa_text("""
        WITH ultimo_por_cliente AS (
            SELECT DISTINCT ON (cliente_id)
                cliente_id, direccion, contenido, timestamp, tipo
            FROM conversaciones
            WHERE timestamp > NOW() - (:horas || ' hours')::interval
            ORDER BY cliente_id, timestamp DESC
        )
        SELECT u.cliente_id, c.numero_whatsapp, c.nombre, u.timestamp, u.tipo,
               LEFT(u.contenido, 200), c.bloqueado
        FROM ultimo_por_cliente u
        JOIN clientes c ON c.id = u.cliente_id
        WHERE u.direccion = 'inbound'
          AND c.bloqueado = false
        ORDER BY u.timestamp ASC
        LIMIT :lim
    """), {"horas": str(horas_max), "lim": max_resultados})).fetchall()

    chats = [
        {
            "cliente_id": r[0],
            "numero": r[1],
            "nombre": r[2] or "(sin nombre)",
            "ultimo_mensaje_ts": r[3].isoformat() if r[3] else None,
            "tipo_ultimo": r[4],
            "preview": (r[5] or "[media]").strip()[:200],
        }
        for r in rows
    ]
    return {"total": len(chats), "chats": chats, "horas_max": horas_max}


async def handler_consultar_chat_cliente(args: dict, ctx: dict) -> dict:
    """Trae los últimos N mensajes del chat del cliente para que el bot equipo
    pueda informar al admin del estado real de la conversación."""
    session: AsyncSession = ctx["session"]
    max_msgs = int(args.get("max_mensajes") or 25)
    max_msgs = max(1, min(max_msgs, 60))

    stmt = select(Cliente)
    if numero := args.get("numero"):
        stmt = stmt.where(Cliente.numero_whatsapp == numero)
    elif nombre := args.get("nombre_parcial"):
        stmt = stmt.where(Cliente.nombre.ilike(f"%{nombre}%"))
    else:
        return {"error": "Pasa `numero` o `nombre_parcial`"}

    cliente = (await session.execute(stmt.limit(1))).scalar_one_or_none()
    if not cliente:
        return {"error": "Cliente no encontrado"}

    msgs = (await session.execute(
        select(Conversacion)
        .where(Conversacion.cliente_id == cliente.id)
        .order_by(Conversacion.timestamp.desc())
        .limit(max_msgs)
    )).scalars().all()
    # Ordenar cronológico (más viejo → más reciente)
    msgs = list(reversed(msgs))

    return {
        "cliente": {
            "id": cliente.id,
            "numero": cliente.numero_whatsapp,
            "nombre": cliente.nombre,
            "ciudad": cliente.ciudad,
            "barrio": cliente.barrio,
        },
        "total_mensajes_traidos": len(msgs),
        "mensajes": [
            {
                "ts": m.timestamp.isoformat() if m.timestamp else None,
                "de": (
                    "cliente" if m.direccion == "inbound"
                    else "humano_admin" if m.direccion == "humano"
                    else "bot"
                ),
                "tipo": m.tipo,
                "contenido": (m.contenido or "")[:500],
                "intent": m.intent,
            }
            for m in msgs
        ],
    }


async def handler_consultar_equipo(args: dict, ctx: dict) -> dict:
    """Devuelve la lista completa de miembros activos del equipo."""
    from app.equipo.directorio import listar_miembros_equipo
    miembros = listar_miembros_equipo()
    return {
        "total": len(miembros),
        "miembros": [
            {
                "nombre": m.nombre,
                "numero": m.numero_whatsapp,
                "rol": m.rol,
                "areas": m.areas,
                "fallback": m.es_fallback,
            }
            for m in miembros
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
    "consultar_chat_cliente": handler_consultar_chat_cliente,
    "consultar_chats_sin_responder": handler_consultar_chats_sin_responder,
    "crear_pedido_manual": handler_crear_pedido_manual,
    "marcar_numero_interno": handler_marcar_numero_interno,
    "enviar_foto_producto_a_cliente": handler_enviar_foto_producto_a_cliente,
    "consultar_producto": handler_consultar_producto,
    "pausar_bot_global": handler_pausar_bot_global,
    "reanudar_bot_global": handler_reanudar_bot_global,
    "consultar_estado_bot": handler_consultar_estado_bot,
    "consultar_equipo": handler_consultar_equipo,
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
