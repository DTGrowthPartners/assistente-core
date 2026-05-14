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

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import AlertaFabio, Pedido, ProductoCache, TarifaDomicilio
from app.db.repos import registrar_alerta_fabio
from app.equipo.directorio import config_escalacion, superior_para
from app.logging_setup import log
from app.shopify.client import ShopifyError, crear_draft_order as shopify_crear_draft
from app.whapi.client import enviar_archivo_local, enviar_imagen_url, enviar_texto

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
                             "bragas", "vestidos", "sets", "tops", "camisas",
                             "camisetas", "blusas", "body", "sueteres", "chalecos"],
                    "description": "camisas = formal/manga larga; camisetas = crop top, t-shirt informal",
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
            "NUNCA menciones nombres del equipo al cliente. El bot decide a qué persona "
            "del equipo enviar según el `area` (telas, envíos, mayorista, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "required": ["tipo", "mensaje"],
            "properties": {
                "tipo": {
                    "type": "string",
                    "enum": ["comprobante_pago", "ref_desconocida", "queja",
                             "pedido_confirmado", "duda_mayorista", "duda_tela_calidad",
                             "duda_envio", "duda_tecnica", "otro"],
                },
                "area": {
                    "type": "string",
                    "description": "Área temática para enrutar al superior correcto. Si dudas, omite.",
                    "enum": ["pagos", "pedidos", "telas_calidad", "mayorista",
                             "envios_nacionales", "tienda_fisica", "quejas",
                             "tecnico", "otro"],
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

    # Dedupe: no re-enviar fotos que ya mandamos en esta sesión
    productos_mostrados = set(ctx.get("productos_mostrados", []) or [])
    if ref in productos_mostrados and not args.get("forzar", False):
        log.info("tools.enviar_imagen.skip_duplicado", ref=ref)
        return {
            "enviado": False,
            "razon": "ya_mostrada_antes",
            "nota_para_modelo": (
                f"La foto del producto {ref} ya fue enviada al cliente antes en esta "
                "conversación. NO la vuelvas a mandar — el cliente ya la tiene. "
                "Responde refiriéndote a 'el jean que te mostré' o el nombre del producto."
            ),
        }

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

    # Registrar como mostrada (set mutado por referencia para el resto del turno)
    productos_mostrados.add(ref)
    ctx["productos_mostrados"] = list(productos_mostrados)
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
    # Datos textuales como fallback (cuando la imagen no carga)
    DATOS_BANCO = {
        "bancolombia": {
            "banco": "Bancolombia", "tipo": "Ahorros",
            "numero": "08500002185", "titular": "Comercializadora Marcas y Estilos",
            "nit": "900425072",
        },
        "davivienda": {
            "banco": "Davivienda", "tipo": "Ahorros",
            "numero": "036001083900", "titular": "Luis Tirado", "cc": "9098444",
        },
        "bbva": {
            "banco": "BBVA", "tipo": "Corriente",
            "numero": "835003732", "titular": "Comercializadora Marcas y Estilos",
            "nit": "900425072",
        },
        "colpatria": {
            "banco": "Colpatria", "tipo": "Corriente",
            "numero": "4251012380", "titular": "Comercializadora Marcas y Estilos",
            "nit": "900425072",
        },
        "banco_de_bogota": {
            "banco": "Banco de Bogotá", "tipo": "Corriente",
            "numero": "182298868", "titular": "Comercializadora Marcas y Estilos",
            "nit": "900425072",
        },
    }
    archivo = paths.get(banco)
    datos = DATOS_BANCO.get(banco)
    if not archivo or not datos:
        return {"enviado": False, "razon": f"Banco {banco} no reconocido"}

    full_path = f"{settings.bancos_dir}/{archivo}"

    # Intentar enviar la imagen del banco
    try:
        await enviar_archivo_local(
            cliente_numero,
            full_path,
            tipo="image",
            caption="Datos para transferencia. Cuando hagas el pago, envíame foto del comprobante.",
        )
        return {"enviado": True, "banco": banco, "via": "imagen"}
    except FileNotFoundError:
        log.warning("tools.enviar_imagen_banco.no_archivo", banco=banco, path=full_path)
    except Exception as e:
        log.error("tools.enviar_imagen_banco.fail", banco=banco, error=str(e))

    # Fallback: devolver los datos al modelo para que los incluya en su respuesta texto.
    # NOTA AL MODELO: no menciones "hubo un inconveniente". Simplemente da los datos
    # con tono natural ("acá te paso los datos para la transferencia: ...").
    return {
        "enviado": False,
        "via": "texto_fallback",
        "datos": datos,
        "instruccion_modelo": (
            "Imagen no disponible. Da los datos bancarios en texto normal, sin mencionar "
            "que hubo problemas. Formato sugerido: 'Acá te paso los datos para la transferencia: "
            f"Banco: {datos['banco']}, Cuenta {datos['tipo']}: {datos['numero']}, "
            f"Titular: {datos['titular']}. Cuando hagas el pago, envíame foto del comprobante.'"
        ),
    }


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
    """
    Registra el pedido:
      - Inserta en tabla `pedidos` con datos completos
      - Crea alerta para Fabio
      - Envía mensaje real a Fabio (vía whapi)
    """
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero")

    # Items: si el modelo los dio estructurados, los usamos; si no, vienen en el resumen
    items_raw = args.get("items") or []
    items_lista: list[dict] = []
    subtotal = Decimal("0")
    if items_raw:
        for item in items_raw:
            try:
                precio_unit = Decimal(str(item.get("precio_unit") or 0))
                cantidad = int(item.get("cantidad", 1))
                subtotal += precio_unit * cantidad
                items_lista.append({
                    "ref": item.get("ref"),
                    "talla": item.get("talla"),
                    "color": item.get("color"),
                    "cantidad": cantidad,
                    "precio_unit": str(precio_unit),
                })
            except Exception:
                continue

    # Si no hay items estructurados, registramos solo el resumen como item textual
    if not items_lista:
        items_lista = [{"descripcion": args.get("resumen", "")}]

    # Domicilio (opcional)
    try:
        domicilio = Decimal(str(args.get("domicilio") or 0))
    except Exception:
        domicilio = Decimal("0")

    try:
        total = Decimal(str(args.get("total"))) if args.get("total") else subtotal + domicilio
    except Exception:
        total = subtotal + domicilio

    # Insertar en pedidos
    pedido = Pedido(
        cliente_id=cliente_id,
        items=items_lista,
        subtotal=subtotal,
        domicilio=domicilio,
        total=total,
        estado="datos_completos",
        direccion_envio=args.get("direccion"),
        ciudad=args.get("ciudad"),
        barrio=args.get("barrio"),
        metodo_pago=args.get("metodo_pago"),
        notas=args.get("resumen"),
    )
    session.add(pedido)
    await session.flush()

    # Mensaje detallado para Fabio
    detalle = (
        f"PEDIDO NUEVO #{pedido.id}\n"
        f"Cliente: {args['nombre_cliente']} ({cliente_numero})\n"
        f"Ciudad: {args.get('ciudad', 'N/A')}\n"
        f"Barrio: {args.get('barrio', 'N/A')}\n"
        f"Dirección: {args.get('direccion', 'N/A')}\n"
        f"Pago: {args.get('metodo_pago', 'N/A')}\n"
        f"Subtotal: ${subtotal:,.0f}\n".replace(",", ".") +
        f"Domicilio: ${domicilio:,.0f}\n".replace(",", ".") +
        f"Total: ${total:,.0f}\n\n".replace(",", ".") +
        f"Detalle:\n{args.get('resumen', '')}"
    )

    # Crear alerta + enviar mensaje a Fabio
    alerta = await registrar_alerta_fabio(
        session, tipo="pedido_confirmado", mensaje=detalle, cliente_id=cliente_id
    )
    enviado_ok = await _enviar_alerta_a_fabio(alerta, detalle, session)

    return {
        "registrado": True,
        "pedido_id": pedido.id,
        "total": str(total),
        "fabio_notificado": enviado_ok,
        "info_cliente": "El equipo coordinará el despacho contigo en breve",
    }


async def _enviar_alerta_a_fabio(alerta: AlertaFabio, mensaje: str, session: AsyncSession) -> bool:
    """
    Envía a Fabio (fallback). Wrapper sobre _enviar_alerta_a_superior usando
    el directorio del equipo. Mantenemos el nombre por compatibilidad.
    """
    superior = superior_para("pedidos")  # pedidos = área típica de Fabio
    destino = superior.numero_whatsapp if superior else settings.fabio_phone
    return await _enviar_alerta_a_superior(alerta, mensaje, session, destino)


async def handler_escalar_a_equipo(args: dict, ctx: dict) -> dict:
    """
    Crea alerta en DB Y envía mensaje real al superior correcto vía whapi.

    Enrutamiento: usa `area` para encontrar el responsable en data/equipo.yaml.
    Si no hay área o el área no tiene responsable específico, cae al fallback
    (hoy: Fabio).
    """
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero") or "(número desconocido)"
    area = args.get("area")

    superior = superior_para(area)
    cfg = config_escalacion()
    prefijo = cfg.get("prefijo_mensajes_fabio", "[BOT ASISTENTE]")
    enviar_real = cfg.get("enviar_mensaje_real", True)

    alerta = await registrar_alerta_fabio(
        session,
        tipo=args["tipo"],
        mensaje=args["mensaje"],
        cliente_id=cliente_id,
        media_url=args.get("media_url"),
    )

    if not superior:
        log.error("tools.escalar.sin_superior", area=area)
        return {"escalado": False, "razon": "no hay miembro del equipo configurado"}

    mensaje = (
        f"{prefijo} [{args['tipo']}]"
        + (f" ({area})" if area else "")
        + f"\nCliente: {cliente_numero}\n\n"
        + args['mensaje']
    )
    if args.get("media_url"):
        mensaje += f"\n\nMedia: {args['media_url']}"

    enviado = False
    if enviar_real:
        enviado = await _enviar_alerta_a_superior(alerta, mensaje, session, superior.numero_whatsapp)

    return {
        "escalado": True,
        "tipo": args["tipo"],
        "area": area,
        "responsable": superior.nombre,
        "notificado_whatsapp": enviado,
    }


async def _enviar_alerta_a_superior(
    alerta: AlertaFabio,
    mensaje: str,
    session: AsyncSession,
    numero_destino: str,
) -> bool:
    """Envía mensaje real al superior correspondiente vía whapi."""
    try:
        await enviar_texto(numero_destino, mensaje)
        await session.execute(
            update(AlertaFabio)
            .where(AlertaFabio.id == alerta.id)
            .values(enviado_a_fabio_en=datetime.now(timezone.utc))
        )
        return True
    except Exception as e:
        log.error("tools.alerta.envio_fail", error=str(e), destino=numero_destino)
        return False


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
