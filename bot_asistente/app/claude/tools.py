"""
Tools (function calling) que el asistente puede invocar.

Cada tool tiene:
  - definición (schema JSON) que se le pasa a Claude
  - handler async que ejecuta la acción y devuelve un dict

Los handlers reciben:
  - `args`: lo que Claude le pasó
  - `ctx`: contexto del flow (session DB, cliente_id, etc.)
y devuelven un dict serializable que se le devuelve a Claude.
"""

from __future__ import annotations

import unicodedata
from decimal import Decimal
from typing import Any, Awaitable, Callable

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import ProductoCache, TarifaDomicilio
from app.db.repos import registrar_alerta_fabio
from app.logging_setup import log
from app.shopify.client import ShopifyError, crear_draft_order as shopify_crear_draft
from app.whapi.client import enviar_archivo_local, enviar_imagen_url

settings = get_settings()


# ════════════════════════════════════════════════════════════════════════════
# DEFINICIONES (lo que ve Claude)
# ════════════════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: list[dict] = [
    {
        "name": "buscar_productos",
        "description": (
            "Busca productos en el catálogo por categoría, referencia o texto libre. "
            "Úsalo SIEMPRE antes de mencionar un producto al cliente. "
            "Devuelve hasta 5 productos con ref, nombre, precio, tallas, colores e imagen_url."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categoria": {
                    "type": "string",
                    "enum": ["shorts", "bermudas", "jeans", "pantalones", "faldas",
                             "bragas", "vestidos", "sets", "tops", "camisetas",
                             "blusas", "chalecos"],
                },
                "ref": {"type": "string", "description": "Referencia exacta tipo INN3684"},
                "talla": {"type": "string"},
                "color": {"type": "string"},
                "texto_libre": {"type": "string"},
                "max_resultados": {"type": "integer", "default": 5},
            },
        },
    },
    {
        "name": "cotizar_envio_cartagena",
        "description": (
            "Busca la tarifa exacta de domicilio para un barrio de Cartagena. "
            "SIEMPRE pregunta el barrio al cliente antes de llamar esta tool."
        ),
        "input_schema": {
            "type": "object",
            "required": ["barrio"],
            "properties": {
                "barrio": {"type": "string", "description": "Nombre del barrio en Cartagena"},
            },
        },
    },
    {
        "name": "enviar_imagen_producto",
        "description": (
            "Envía la foto de un producto al cliente. Llámala JUNTO con la mención "
            "de la prenda en tu respuesta. NO menciones un producto sin enviarle foto."
        ),
        "input_schema": {
            "type": "object",
            "required": ["ref"],
            "properties": {
                "ref": {"type": "string"},
                "incluir_caption": {"type": "boolean", "default": True},
            },
        },
    },
    {
        "name": "enviar_imagen_banco",
        "description": "Envía la imagen con los datos bancarios cuando el cliente elige transferencia.",
        "input_schema": {
            "type": "object",
            "required": ["banco"],
            "properties": {
                "banco": {
                    "type": "string",
                    "enum": ["bancolombia", "davivienda", "bbva", "colpatria", "banco_de_bogota"],
                },
            },
        },
    },
    {
        "name": "crear_draft_order",
        "description": (
            "Crea un borrador de pedido en Shopify y envía link de pago automático al cliente. "
            "SOLO funciona para productos con origen='shopify' (que tienen variant_id). "
            "Si el producto es origen='html_catalogo' o no tiene variant_id, usa "
            "`tomar_pedido_manual` en su lugar. Usa esta tool SOLO cuando el cliente confirmó "
            "productos + tallas + dirección."
        ),
        "input_schema": {
            "type": "object",
            "required": ["items", "nombre_cliente"],
            "properties": {
                "items": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["ref", "talla", "cantidad"],
                        "properties": {
                            "ref": {"type": "string"},
                            "talla": {"type": "string"},
                            "cantidad": {"type": "integer", "minimum": 1},
                        },
                    },
                },
                "nombre_cliente": {"type": "string"},
                "ciudad": {"type": "string"},
                "direccion": {"type": "string"},
            },
        },
    },
    {
        "name": "tomar_pedido_manual",
        "description": (
            "Registra un pedido manualmente y escala al equipo para que lo confirme/despache. "
            "Usar cuando `crear_draft_order` no aplica (producto sin variant_id Shopify, "
            "API caída, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["resumen", "nombre_cliente"],
            "properties": {
                "resumen": {"type": "string", "description": "Texto detallado del pedido (refs, tallas, cantidades, total)"},
                "nombre_cliente": {"type": "string"},
                "ciudad": {"type": "string"},
                "direccion": {"type": "string"},
                "barrio": {"type": "string"},
                "metodo_pago": {"type": "string"},
            },
        },
    },
    {
        "name": "escalar_a_equipo",
        "description": (
            "Notifica al equipo interno sobre algo que requiere intervención humana. "
            "NUNCA menciones nombres del equipo al cliente. Casos: comprobante de pago, "
            "ref desconocida, queja seria, duda mayorista específica, dirección de tienda "
            "que no conoces."
        ),
        "input_schema": {
            "type": "object",
            "required": ["tipo", "mensaje"],
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["comprobante_pago", "ref_desconocida", "queja",
                             "pedido_confirmado", "duda_mayorista", "otro"],
                },
                "mensaje": {"type": "string", "description": "Resumen interno para el equipo (no va al cliente)"},
                "media_url": {"type": "string", "description": "URL del comprobante u otra evidencia si aplica"},
            },
        },
    },
    {
        "name": "programar_seguimiento",
        "description": (
            "Programa recordatorio para reabrir la conversación si el cliente no responde. "
            "Usar tras enviar opciones, tras pasar datos bancarios, o cuando el cliente "
            "dice 'lo pienso'."
        ),
        "input_schema": {
            "type": "object",
            "required": ["horas"],
            "properties": {
                "horas": {"type": "number", "enum": [2, 4, 24]},
                "razon": {"type": "string"},
            },
        },
    },
]


# ════════════════════════════════════════════════════════════════════════════
# HELPERS
# ════════════════════════════════════════════════════════════════════════════


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower().strip()


# ════════════════════════════════════════════════════════════════════════════
# HANDLERS
# ════════════════════════════════════════════════════════════════════════════


async def handler_buscar_productos(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    stmt = select(ProductoCache).where(ProductoCache.activo.is_(True))

    if ref := args.get("ref"):
        stmt = stmt.where(ProductoCache.ref.ilike(f"%{ref.upper()}%"))
    if categoria := args.get("categoria"):
        stmt = stmt.where(ProductoCache.categoria == categoria)
    if texto := args.get("texto_libre"):
        stmt = stmt.where(ProductoCache.nombre.ilike(f"%{texto}%"))

    stmt = stmt.limit(args.get("max_resultados", 5))
    productos = (await session.execute(stmt)).scalars().all()

    if not productos:
        return {
            "encontrados": 0,
            "productos": [],
            "nota": "No hay productos que coincidan con esos filtros. Sugiere al cliente otra categoría o pregunta más detalles.",
        }

    talla_filtro = args.get("talla")
    return {
        "encontrados": len(productos),
        "productos": [
            {
                "ref": p.ref,
                "nombre": p.nombre,
                "categoria": p.categoria,
                "precio_detal": str(p.precio_detal) if p.precio_detal else None,
                "precio_mayor": str(p.precio_mayor) if p.precio_mayor else None,
                "tallas": p.tallas,
                "colores": p.colores,
                "imagen_url": p.imagen_url,
                "origen": p.origen,
                "tiene_variant_id": bool(p.variants),
                "talla_solicitada_disponible": (
                    (talla_filtro in (p.tallas or [])) if talla_filtro else None
                ),
            }
            for p in productos
        ],
    }


async def handler_cotizar_envio(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    barrio_raw = args["barrio"]
    barrio_norm = _norm(barrio_raw)

    # Búsqueda exacta primero
    stmt = select(TarifaDomicilio).where(TarifaDomicilio.barrio_normalizado == barrio_norm)
    tarifa = (await session.execute(stmt)).scalar_one_or_none()

    # Fuzzy si no hay exact match
    if not tarifa:
        stmt = (
            select(TarifaDomicilio)
            .where(TarifaDomicilio.barrio_normalizado.op("%")(barrio_norm))
            .limit(3)
        )
        candidatos = (await session.execute(stmt)).scalars().all()
        if candidatos:
            tarifa = candidatos[0]
            return {
                "encontrado": True,
                "barrio_buscado": barrio_raw,
                "barrio_match": tarifa.barrio,
                "precio": str(tarifa.precio) if tarifa.precio else None,
                "tipo": tarifa.tipo,
                "nota": (
                    f"Match aproximado de '{barrio_raw}' a '{tarifa.barrio}'. "
                    "Confirma con el cliente si es la zona correcta antes de cotizar."
                ),
                "alternativas": [
                    {"barrio": c.barrio, "precio": str(c.precio) if c.precio else None}
                    for c in candidatos[1:]
                ],
            }
        return {
            "encontrado": False,
            "barrio_buscado": barrio_raw,
            "nota": (
                "El barrio no aparece en la tabla. Sugiere al cliente que confirme "
                "el nombre del barrio o escala al equipo para evaluar cobertura."
            ),
        }

    return {
        "encontrado": True,
        "barrio_buscado": barrio_raw,
        "barrio_match": tarifa.barrio,
        "precio": str(tarifa.precio) if tarifa.precio else None,
        "tipo": tarifa.tipo,
        "zona": tarifa.zona,
        "notas": tarifa.notas,
    }


async def handler_enviar_imagen_producto(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    ref = args["ref"].upper()

    stmt = select(ProductoCache).where(ProductoCache.ref == ref)
    prod = (await session.execute(stmt)).scalar_one_or_none()
    if not prod:
        return {"enviado": False, "razon": f"Ref {ref} no encontrada en catálogo"}

    cliente_numero = ctx["cliente_numero"]
    caption = None
    if args.get("incluir_caption", True) and prod.precio_detal:
        caption = f"{prod.nombre} ({prod.ref}) - ${prod.precio_detal:,.0f}".replace(",", ".")

    try:
        if prod.imagen_url:
            await enviar_imagen_url(cliente_numero, prod.imagen_url, caption=caption)
        elif prod.foto_local:
            await enviar_archivo_local(cliente_numero, f"{settings.catalogo_dir}/{prod.foto_local}",
                                       tipo="image", caption=caption)
        else:
            return {"enviado": False, "razon": f"Producto {ref} sin foto disponible"}
    except Exception as e:
        log.error("tools.enviar_imagen.fail", ref=ref, error=str(e))
        return {"enviado": False, "razon": f"Error de red al enviar: {e}"}

    return {"enviado": True, "ref": ref, "caption": caption}


async def handler_enviar_imagen_banco(args: dict, ctx: dict) -> dict:
    banco = args["banco"]
    cliente_numero = ctx["cliente_numero"]

    paths = {
        "bancolombia": "bancolombia.webp",
        "davivienda": "davivienda.webp",
        "bbva": "bbva.webp",
        "colpatria": "colpatria.webp",
        "banco_de_bogota": "banco de bogota.webp",
    }
    archivo = paths.get(banco)
    if not archivo:
        return {"enviado": False, "razon": f"Banco {banco} no reconocido"}

    full_path = f"{settings.bancos_dir}/{archivo}"
    try:
        await enviar_archivo_local(
            cliente_numero,
            full_path,
            tipo="image",
            caption="Datos para transferencia. Cuando hagas el pago, envíame foto del comprobante.",
        )
    except Exception as e:
        log.error("tools.enviar_imagen_banco.fail", banco=banco, error=str(e))
        return {"enviado": False, "razon": f"Error: {e}"}

    return {"enviado": True, "banco": banco}


async def handler_crear_draft_order(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    cliente_numero = ctx["cliente_numero"]

    # Resolver variant_id de cada item
    productos_resueltos = []
    items_con_variant = []
    for item in args["items"]:
        ref = item["ref"].upper()
        talla = item["talla"]
        cantidad = item.get("cantidad", 1)

        prod = (await session.execute(
            select(ProductoCache).where(ProductoCache.ref == ref)
        )).scalar_one_or_none()
        if not prod:
            return {"creado": False, "razon": f"Ref {ref} no encontrada"}
        if prod.origen != "shopify" or not prod.variants:
            return {
                "creado": False,
                "razon": f"Ref {ref} es de fuente {prod.origen} y no tiene variant_id. "
                         "Usa `tomar_pedido_manual` en su lugar.",
            }

        # Buscar variant con esa talla
        variant_id = None
        for v in prod.variants:
            if str(v.get("talla")) == str(talla):
                variant_id = v.get("variant_id")
                break
        if not variant_id:
            return {
                "creado": False,
                "razon": f"Talla {talla} no disponible para {ref}. Tallas: {prod.tallas}",
            }

        productos_resueltos.append({"ref": ref, "talla": talla, "cantidad": cantidad,
                                    "precio_unit": str(prod.precio_detal)})
        items_con_variant.append({"variant_id": variant_id, "quantity": cantidad})

    try:
        result = await shopify_crear_draft(
            cliente_numero=cliente_numero,
            nombre_cliente=args["nombre_cliente"],
            items=items_con_variant,
            enviar_link_whatsapp=True,
        )
    except ShopifyError as e:
        log.error("tools.draft_order.fail", error=str(e))
        return {
            "creado": False,
            "razon": str(e),
            "sugerencia": (
                "El sistema de pago automático está fallando. Usa `tomar_pedido_manual` "
                "y escala al equipo con `escalar_a_equipo`."
            ),
        }

    return {"creado": True, "items": productos_resueltos, "shopify_response": result}


async def handler_tomar_pedido_manual(args: dict, ctx: dict) -> dict:
    """Registra el pedido en alertas_fabio y devuelve OK."""
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    detalle = (
        f"📦 PEDIDO MANUAL\n"
        f"Cliente: {args['nombre_cliente']}\n"
        f"Ciudad: {args.get('ciudad', 'N/A')}\n"
        f"Barrio: {args.get('barrio', 'N/A')}\n"
        f"Dirección: {args.get('direccion', 'N/A')}\n"
        f"Pago: {args.get('metodo_pago', 'N/A')}\n\n"
        f"Detalle:\n{args['resumen']}"
    )
    await registrar_alerta_fabio(
        session, tipo="pedido_confirmado", mensaje=detalle, cliente_id=cliente_id
    )
    return {"registrado": True, "info_cliente": "El equipo coordinará el despacho contigo en breve"}


async def handler_escalar_a_equipo(args: dict, ctx: dict) -> dict:
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    await registrar_alerta_fabio(
        session,
        tipo=args["tipo"],
        mensaje=args["mensaje"],
        cliente_id=cliente_id,
        media_url=args.get("media_url"),
    )
    return {"escalado": True, "tipo": args["tipo"]}


async def handler_programar_seguimiento(args: dict, ctx: dict) -> dict:
    """Por ahora solo log + actualizar sesión. La ejecución real es Fase 2 (cron)."""
    log.info(
        "tools.seguimiento_programado",
        cliente_id=ctx.get("cliente_id"),
        horas=args["horas"],
        razon=args.get("razon"),
    )
    # TODO Fase 2: actualizar sesiones.proximo_seguimiento
    return {"programado": True, "horas": args["horas"]}


# ════════════════════════════════════════════════════════════════════════════
# DISPATCHER
# ════════════════════════════════════════════════════════════════════════════

Handler = Callable[[dict, dict], Awaitable[dict]]

HANDLERS: dict[str, Handler] = {
    "buscar_productos": handler_buscar_productos,
    "cotizar_envio_cartagena": handler_cotizar_envio,
    "enviar_imagen_producto": handler_enviar_imagen_producto,
    "enviar_imagen_banco": handler_enviar_imagen_banco,
    "crear_draft_order": handler_crear_draft_order,
    "tomar_pedido_manual": handler_tomar_pedido_manual,
    "escalar_a_equipo": handler_escalar_a_equipo,
    "programar_seguimiento": handler_programar_seguimiento,
}


async def ejecutar_tool(name: str, args: dict, ctx: dict) -> dict:
    """Ejecuta el handler correspondiente. Maneja excepciones."""
    handler = HANDLERS.get(name)
    if not handler:
        return {"error": f"Tool desconocida: {name}"}

    try:
        result = await handler(args, ctx)
        log.info("tools.ejecutada", tool=name, ok=True)
        return result
    except Exception as e:
        log.exception("tools.error", tool=name, error=str(e))
        return {"error": f"Error ejecutando {name}: {e}"}
