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
from app.db.models import (
    AlertaFabio,
    Cliente,
    Conversacion,
    Pedido,
    ProductoCache,
    Sesion,
    TarifaDomicilio,
)
from app.db.repos import registrar_alerta_fabio
from app.equipo.directorio import config_escalacion, superior_para, superiores_para
from app.logging_setup import log
from app.shopify.client import ShopifyError, crear_draft_order as shopify_crear_draft
from app.whapi.client import (
    enviar_archivo_local,
    enviar_imagen_bytes,
    enviar_imagen_url,
    enviar_texto,
)

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
                "cedula": {"type": "string", "description": "Cédula del cliente (solo dígitos). Requerida para Shopify draft order."},
                "email": {"type": "string", "description": "Email del cliente. Requerido para Shopify draft order (Shopify lo usa para notificar el pago)."},
            },
        },
    },
    {
        "name": "tomar_pedido_manual",
        "description": (
            "Registra un pedido EN LA TABLA `pedidos` del sistema y escala al equipo. "
            "DEBES llamarla cuando el cliente CONFIRMA un pedido completo (productos "
            "elegidos + dirección + método pago) — no esperes al comprobante. "
            "Pasa los `items` ESTRUCTURADOS con ref/talla/cantidad/precio_unit, y los "
            "totales calculados como números. Sin esto, el pedido queda sin total y "
            "no aparece bien en reportes."
        ),
        "input_schema": {
            "type": "object",
            "required": ["items", "nombre_cliente", "subtotal", "total"],
            "properties": {
                "items": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Lista de productos del pedido. CADA item debe tener ref+talla+cantidad+precio_unit.",
                    "items": {
                        "type": "object",
                        "required": ["ref", "cantidad", "precio_unit"],
                        "properties": {
                            "ref": {"type": "string", "description": "Referencia del producto (INN3684, etc.)"},
                            "talla": {"type": "string"},
                            "color": {"type": "string"},
                            "cantidad": {"type": "integer", "minimum": 1},
                            "precio_unit": {
                                "type": "number",
                                "description": "Precio unitario en COP (NÚMERO sin punto de miles). Ej: 60000 para $60.000",
                            },
                        },
                    },
                },
                "nombre_cliente": {"type": "string"},
                "ciudad": {"type": "string"},
                "direccion": {"type": "string"},
                "barrio": {"type": "string"},
                "cedula": {
                    "type": "string",
                    "description": "Cédula de ciudadanía del cliente (solo dígitos, sin puntos). Requerida para envíos junto con email (al menos uno de los dos).",
                },
                "email": {
                    "type": "string",
                    "description": "Correo electrónico del cliente. Requerido para envíos junto con cédula (al menos uno de los dos).",
                },
                "subtotal": {
                    "type": "number",
                    "description": "Suma de items (cantidad × precio_unit), SIN domicilio. Número en COP.",
                },
                "domicilio": {"type": "number", "description": "Valor del envío en COP. 0 si no aplica (web/contraentrega-nacional)."},
                "total": {
                    "type": "number",
                    "description": "subtotal + domicilio. Número en COP.",
                },
                "metodo_pago": {"type": "string", "description": "transferencia_bancolombia, addi, contraentrega_cartagena, etc."},
                "notas": {"type": "string"},
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

    # Si el cliente especificó talla, recordarla para próximos turnos
    if talla := args.get("talla"):
        await _update_sesion(session, ctx.get("cliente_id"), talla_interes=str(talla))

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

    # Recordar el barrio que mencionó el cliente (versión raw — el cliente
    # escribió esa, no la normalizada). Si después la tarifa hace match
    # difuso a otro nombre, actualizamos abajo a la versión canónica.
    await _update_sesion(session, ctx.get("cliente_id"), barrio=barrio_raw)

    # Búsqueda exacta primero
    stmt = select(TarifaDomicilio).where(TarifaDomicilio.barrio_normalizado == barrio_norm)
    tarifa = (await session.execute(stmt)).scalar_one_or_none()

    # Fuzzy si no hay exact match. Buscamos también por ILIKE substring para
    # capturar casos tipo "Olaya" → "Olaya Herrera - Sector X". Limitamos a 15.
    if not tarifa:
        from sqlalchemy import or_ as sa_or
        stmt = (
            select(TarifaDomicilio)
            .where(
                sa_or(
                    TarifaDomicilio.barrio_normalizado.op("%")(barrio_norm),
                    TarifaDomicilio.barrio_normalizado.ilike(f"%{barrio_norm}%"),
                )
            )
            .limit(15)
        )
        candidatos = (await session.execute(stmt)).scalars().all()
        if candidatos:
            # ¿Todos los candidatos tienen el MISMO precio? Si sí, cotizar
            # directo sin pedirle al modelo que pregunte cuál sector — la
            # tarifa es la misma. Caso real Olaya Herrera: 13 sectores
            # todos a $6.000. NO interrogar al cliente sobre sector si
            # cobramos lo mismo.
            precios_unicos = {c.precio for c in candidatos if c.precio is not None}
            if len(precios_unicos) == 1:
                t = candidatos[0]
                return {
                    "encontrado": True,
                    "barrio_buscado": barrio_raw,
                    "barrio_match": t.barrio,
                    "precio": str(t.precio),
                    "tipo": t.tipo,
                    "zona": t.zona,
                    "nota": (
                        f"'{barrio_raw}' tiene {len(candidatos)} sectores en "
                        f"nuestra tabla y TODOS valen ${int(t.precio):,}. "
                        f"Cotiza directo SIN preguntar al cliente cuál sector. "
                        f"Si necesitas la dirección exacta para el domiciliario, "
                        f"pídela pero NO bloquees la cotización por eso."
                    ).replace(",", "."),
                }
            # Precios distintos → necesita aclaración del cliente.
            tarifa = candidatos[0]
            return {
                "encontrado": True,
                "barrio_buscado": barrio_raw,
                "barrio_match": tarifa.barrio,
                "precio": str(tarifa.precio) if tarifa.precio else None,
                "tipo": tarifa.tipo,
                "nota": (
                    f"Match aproximado de '{barrio_raw}' a '{tarifa.barrio}'. "
                    f"Hay {len(candidatos)} candidatos con tarifas distintas; "
                    "confirma con el cliente cuál es exactamente antes de cotizar."
                ),
                "alternativas": [
                    {"barrio": c.barrio, "precio": str(c.precio) if c.precio else None}
                    for c in candidatos[1:]
                ],
            }
        # Barrio no encontrado en la tabla. Política del dueño (2026-05-15):
        # NO inventar tarifa. Decirle al cliente "consulto con el equipo y te
        # confirmo en un momento" y escalar con escalar_a_equipo(tipo='duda_envio').
        # Mientras el equipo confirma, NO seguir tomando datos del pedido.
        log.info(
            "tools.cotizar_envio.barrio_desconocido_escalar",
            barrio=barrio_raw,
        )
        return {
            "encontrado": False,
            "barrio_buscado": barrio_raw,
            "instruccion_modelo": (
                f"El barrio '{barrio_raw}' NO está en la tabla de tarifas y NO hay "
                f"match aproximado. NO inventes una tarifa. Dile al cliente: "
                f"'Déjame confirmar con el equipo la tarifa exacta a {barrio_raw} "
                f"y te aviso en un momento.' Luego LLAMA `escalar_a_equipo` con "
                f"tipo='duda_envio' y un mensaje detallado para Fabio. NO tomes "
                f"el pedido hasta tener la tarifa confirmada."
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

    # Persistir el último producto mostrado en Sesion — si después el cliente
    # dice "lo quiero" sin más, "lo" se refiere a ESTE producto.
    await _update_sesion(session, ctx.get("cliente_id"), producto_actual_ref=ref)
    return {"enviado": True, "ref": ref, "caption": caption}


_BANCO_PATHS = {
    "bancolombia": "bancolombia.webp",
    "davivienda": "davivienda.webp",
    "bbva": "bbva.webp",
    "colpatria": "colpatria.webp",
    "banco_de_bogota": "banco de bogota.webp",
}

_DATOS_BANCO = {
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


async def handler_enviar_imagen_banco(args: dict, ctx: dict) -> dict:
    from datetime import timedelta
    banco = args["banco"]
    cliente_numero = ctx["cliente_numero"]
    cliente_id = ctx.get("cliente_id")
    session: AsyncSession = ctx["session"]

    archivo = _BANCO_PATHS.get(banco)
    datos = _DATOS_BANCO.get(banco)
    if not archivo or not datos:
        return {"enviado": False, "razon": f"Banco {banco} no reconocido"}

    # Dedupe 1: si ya hay alerta abierta de comprobante_pago, el cliente ya
    # pagó — NO reenvíes los datos del banco.
    if cliente_id:
        ventana = datetime.now(timezone.utc) - timedelta(minutes=10)
        alerta_pago = (await session.execute(
            select(AlertaFabio).where(
                AlertaFabio.cliente_id == cliente_id,
                AlertaFabio.tipo == "comprobante_pago",
                AlertaFabio.created_at >= ventana,
            ).limit(1)
        )).scalar_one_or_none()
        if alerta_pago:
            log.info(
                "tools.enviar_imagen_banco.dedupe_skip_pago_recibido",
                cliente_id=cliente_id,
                alerta_id=alerta_pago.id,
            )
            return {
                "enviado": False,
                "razon": (
                    "El cliente ya envió comprobante recientemente. "
                    "NO reenvíes los datos del banco. Avanza con el pedido."
                ),
                "alerta_existente_id": alerta_pago.id,
            }

        # Dedupe 2: si ya enviamos los datos del banco hace <8 min, NO reenvíes.
        # Esto evita el loop donde el modelo ante una pregunta de precio
        # reenvía los datos en lugar de responder con texto.
        cliente_q = (await session.execute(
            select(Cliente).where(Cliente.id == cliente_id).limit(1)
        )).scalar_one_or_none()
        meta = (cliente_q.metadata_ or {}) if cliente_q else {}
        ultimo_iso = meta.get(f"last_banco_{banco}_ts")
        if ultimo_iso:
            try:
                ultimo_ts = datetime.fromisoformat(ultimo_iso)
                if ultimo_ts.tzinfo is None:
                    ultimo_ts = ultimo_ts.replace(tzinfo=timezone.utc)
                ventana_banco = datetime.now(timezone.utc) - timedelta(minutes=8)
                if ultimo_ts >= ventana_banco:
                    log.info(
                        "tools.enviar_imagen_banco.dedupe_skip_reciente",
                        cliente_id=cliente_id,
                        banco=banco,
                        ultimo_envio=ultimo_iso,
                    )
                    return {
                        "enviado": False,
                        "razon": (
                            f"Ya enviaste los datos de {banco} a este cliente "
                            f"hace pocos minutos. NO los reenvíes. RESPONDE CON "
                            f"TEXTO a la pregunta del cliente (cálculo del total, "
                            f"tiempo de entrega, etc.) usando los datos textuales."
                        ),
                        "datos_textuales": datos,
                    }
            except Exception:
                pass

    full_path = f"{settings.bancos_dir}/{archivo}"

    # Intentar enviar la imagen del banco
    try:
        await enviar_archivo_local(
            cliente_numero,
            full_path,
            tipo="image",
            caption="Datos para transferencia. Cuando hagas el pago, envíame foto del comprobante.",
        )
        # Registrar el timestamp del envío en cliente.metadata para que el
        # dedupe de 8 min funcione en próximos turnos.
        if cliente_id:
            now_iso = datetime.now(timezone.utc).isoformat()
            cliente_now = (await session.execute(
                select(Cliente).where(Cliente.id == cliente_id).limit(1)
            )).scalar_one_or_none()
            if cliente_now:
                new_meta = dict(cliente_now.metadata_ or {})
                new_meta[f"last_banco_{banco}_ts"] = now_iso
                cliente_now.metadata_ = new_meta
        # Persistir banco/método en Sesion para próximos turnos
        await _update_sesion(
            session, cliente_id,
            metodo_pago_elegido="transferencia",
            banco_elegido=banco,
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

    # Persistir en Sesion: el cliente eligió pagar por link Shopify
    await _update_sesion(
        session, ctx.get("cliente_id"),
        estado="esperando_pago",
        metodo_pago_elegido="link_shopify",
    )
    return {"creado": True, "items": productos_resueltos, "shopify_response": result}


async def handler_tomar_pedido_manual(args: dict, ctx: dict) -> dict:
    """
    Registra el pedido:
      - Inserta en tabla `pedidos` con datos completos
      - Crea alerta para Fabio
      - Envía mensaje real a Fabio (vía whapi)

    Dedupe: si ya hay un pedido reciente (10 min) del mismo cliente con el
    mismo total, se devuelve ese sin crear duplicado ni re-alertar.
    """
    from datetime import timedelta
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero")

    # Dedupe rápido por (cliente_id, total, ventana 10 min). Evita que dos
    # turnos consecutivos (ej. cliente reenviando comprobante) generen dos
    # pedidos idénticos para el equipo.
    if cliente_id:
        try:
            total_arg = Decimal(str(args.get("total") or 0))
        except Exception:
            total_arg = Decimal("0")
        if total_arg > 0:
            ventana_p = datetime.now(timezone.utc) - timedelta(minutes=10)
            existente_p = (await session.execute(
                select(Pedido).where(
                    Pedido.cliente_id == cliente_id,
                    Pedido.total == total_arg,
                    Pedido.created_at >= ventana_p,
                ).order_by(Pedido.id.desc()).limit(1)
            )).scalar_one_or_none()
            if existente_p:
                log.info(
                    "tools.tomar_pedido.dedupe_skip",
                    cliente_id=cliente_id,
                    pedido_existente_id=existente_p.id,
                    total=str(total_arg),
                )
                return {
                    "registrado": False,
                    "razon": "ya existe pedido reciente con el mismo total",
                    "pedido_existente_id": existente_p.id,
                }

    # Items estructurados (el tool schema los requiere). Construir lista con
    # subtotal calculado a partir de cantidad × precio_unit por seguridad.
    items_raw = args.get("items") or []
    items_lista: list[dict] = []
    subtotal_calculado = Decimal("0")
    for item in items_raw:
        try:
            precio_unit = Decimal(str(item.get("precio_unit") or 0))
            cantidad = int(item.get("cantidad", 1))
            subtotal_calculado += precio_unit * cantidad
            items_lista.append({
                "ref": item.get("ref"),
                "talla": item.get("talla"),
                "color": item.get("color"),
                "cantidad": cantidad,
                "precio_unit": str(precio_unit),
                "subtotal": str(precio_unit * cantidad),
            })
        except Exception as e:
            log.warning("tomar_pedido.item_invalido", item=item, error=str(e))

    # Fallback defensivo: si Claude por error no pasó items, registrar la nota libre
    if not items_lista and args.get("notas"):
        items_lista = [{"descripcion": args.get("notas")}]

    # Tomar subtotal/domicilio/total que pasó Claude (NÚMEROS); si no vienen, usar
    # los calculados a partir de items.
    def _to_decimal(v, default: Decimal = Decimal("0")) -> Decimal:
        if v is None or v == "":
            return default
        try:
            return Decimal(str(v))
        except Exception:
            return default

    subtotal = _to_decimal(args.get("subtotal"), subtotal_calculado)
    domicilio = _to_decimal(args.get("domicilio"), Decimal("0"))
    total = _to_decimal(args.get("total"), subtotal + domicilio)

    # Si subtotal sigue en 0 pero hubo items, usar lo calculado
    if subtotal == 0 and subtotal_calculado > 0:
        subtotal = subtotal_calculado
        total = subtotal + domicilio

    # Normalizar cedula (solo dígitos) y email (lower + strip)
    cedula_raw = (args.get("cedula") or "").strip()
    cedula = "".join(c for c in cedula_raw if c.isdigit()) or None
    email_raw = (args.get("email") or "").strip().lower()
    email = email_raw if "@" in email_raw else None

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
        cedula_cliente=cedula,
        email_cliente=email,
        notas=args.get("resumen") or args.get("notas"),
    )
    session.add(pedido)
    await session.flush()

    # Enriquecer datos del cliente: si nombre/ciudad/barrio/cedula/email
    # del cliente están vacíos, completarlos con lo que dio en el pedido.
    # NO sobreescribimos valores que ya existen.
    cliente_obj = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if cliente_obj:
        cambios: dict = {}
        if not cliente_obj.nombre and args.get("nombre_cliente"):
            cambios["nombre"] = args["nombre_cliente"]
        if not cliente_obj.ciudad and args.get("ciudad"):
            cambios["ciudad"] = args["ciudad"]
        if not cliente_obj.barrio and args.get("barrio"):
            cambios["barrio"] = args["barrio"]
        if not cliente_obj.cedula and cedula:
            cambios["cedula"] = cedula
        if not cliente_obj.email and email:
            cambios["email"] = email
        if cambios:
            await session.execute(
                update(Cliente).where(Cliente.id == cliente_id).values(**cambios)
            )
            log.info("tools.cliente.enriquecido", cliente_id=cliente_id, cambios=list(cambios.keys()))

    # Actualizar Sesion con datos del pedido para que Claude los vea en próximos turnos
    await _update_sesion(
        session, cliente_id,
        estado="datos_completos",
        direccion_envio=args.get("direccion"),
        barrio=args.get("barrio"),
        metodo_pago_elegido=args.get("metodo_pago"),
    )

    # Mensaje detallado para Fabio
    detalle = (
        f"PEDIDO NUEVO #{pedido.id}\n"
        f"Cliente: {args['nombre_cliente']} ({cliente_numero})\n"
        f"Cédula: {cedula or 'N/A'}\n"
        f"Email: {email or 'N/A'}\n"
        f"Ciudad: {args.get('ciudad', 'N/A')}\n"
        f"Barrio: {args.get('barrio', 'N/A')}\n"
        f"Dirección: {args.get('direccion', 'N/A')}\n"
        f"Pago: {args.get('metodo_pago', 'N/A')}\n"
        f"Subtotal: ${subtotal:,.0f}\n".replace(",", ".") +
        f"Domicilio: ${domicilio:,.0f}\n".replace(",", ".") +
        f"Total: ${total:,.0f}\n\n".replace(",", ".") +
        f"Detalle:\n{args.get('resumen', '')}"
    )

    # Crear alerta + encolar envío a Fabio (drenará post-commit).
    # Antes este envío salía ANTES del commit → si la transacción rollback,
    # Fabio recibía "PEDIDO NUEVO #X" pero el pedido no quedaba en BD.
    alerta = await registrar_alerta_fabio(
        session, tipo="pedido_confirmado", mensaje=detalle, cliente_id=cliente_id
    )
    # Notificar a TODOS los admins (Fabio + Stiven), no solo al primero
    superiores = superiores_para("pedidos")
    destinos = [s.numero_whatsapp for s in superiores] or [settings.fabio_phone]
    for destino in destinos:
        _enqueue_text(ctx, to=destino, text=detalle, alerta_id=alerta.id)

    return {
        "registrado": True,
        "pedido_id": pedido.id,
        "total": str(total),
        "fabio_notificado": "encolado (sale después del commit)",
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

    Dedupe: si ya hay una alerta abierta del mismo tipo para este cliente en
    los últimos 5 minutos, NO escala de nuevo. Devuelve la alerta existente.
    Esto evita spam a Fabio cuando el cliente manda 3 screenshots seguidas
    del mismo comprobante.
    """
    from datetime import timedelta
    session: AsyncSession = ctx["session"]
    cliente_id = ctx.get("cliente_id")
    cliente_numero = ctx.get("cliente_numero") or "(número desconocido)"
    area = args.get("area")

    superiores = superiores_para(area)
    superior = superiores[0] if superiores else None
    cfg = config_escalacion()
    prefijo = cfg.get("prefijo_mensajes_fabio", "[BOT ASISTENTE]")
    enviar_real = cfg.get("enviar_mensaje_real", True)

    # Dedupe: NO escalar si ya hay alerta abierta O alerta resuelta reciente
    # del mismo tipo para este cliente. Si está abierta → el equipo ya sabe,
    # solo decir "estamos verificando". Si está resuelta → el admin ya dio la
    # info por el chat humano y está en el historial; el bot debe USAR ESA INFO
    # y NO volver a escalar.
    #
    # Ventana 12h para resueltas (alertas viejas no aplican),
    # 6h para abiertas (los admins pueden tardar en responder).
    if cliente_id:
        v_abierta = datetime.now(timezone.utc) - timedelta(hours=6)
        v_resuelta = datetime.now(timezone.utc) - timedelta(hours=12)
        existente = (await session.execute(
            select(AlertaFabio).where(
                AlertaFabio.cliente_id == cliente_id,
                AlertaFabio.tipo == args["tipo"],
                (
                    (AlertaFabio.resuelto.is_(False) & (AlertaFabio.created_at >= v_abierta))
                    | (AlertaFabio.resuelto.is_(True) & (AlertaFabio.created_at >= v_resuelta))
                ),
            ).order_by(AlertaFabio.id.desc()).limit(1)
        )).scalar_one_or_none()
        if existente:
            ya_resuelta = bool(existente.resuelto)
            log.info(
                "tools.escalar.dedupe_skip",
                cliente_id=cliente_id,
                tipo=args["tipo"],
                alerta_existente_id=existente.id,
                ya_resuelta=ya_resuelta,
            )
            if ya_resuelta:
                razon = (
                    "El admin ya respondió a una consulta similar hace poco "
                    "(alerta resuelta hace menos de 12h). Esa respuesta está "
                    "en el historial reciente del cliente (busca mensajes "
                    "anteriores tuyos o de la asesora). USA ESA INFO para "
                    "contestar — NO vuelvas a escalar al equipo. Si el "
                    "cliente pregunta por precio/talla que el admin ya "
                    "confirmó, repite lo confirmado y avanza al siguiente "
                    "paso (datos, pago, dirección)."
                )
            else:
                razon = (
                    "Ya hay una alerta ABIERTA con el equipo sobre este tema "
                    "para este cliente. NO crear otra. Dile al cliente: 'Ya "
                    "estamos verificando con el equipo, en cuanto tengamos "
                    "la información te confirmo. Gracias por la paciencia.' "
                    "— y NO digas que vas a escalar otra vez."
                )
            return {
                "escalado": False,
                "razon": razon,
                "alerta_existente_id": existente.id,
                "alerta_resuelta": ya_resuelta,
                "tipo": args["tipo"],
            }

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

    imagen_reenviada = False
    if enviar_real:
        # Encolar el texto al outbox para CADA admin (Fabio + Stiven).
        for sup in superiores:
            _enqueue_text(ctx, to=sup.numero_whatsapp, text=mensaje, alerta_id=alerta.id)

        # Si el cliente envió imagen (comprobante de pago / queja), también
        # encolarla para CADA admin.
        inbound_bytes = ctx.get("inbound_imagen_bytes")
        if inbound_bytes and args["tipo"] in ("comprobante_pago", "queja"):
            for sup in superiores:
                _enqueue_image_bytes(
                    ctx,
                    to=sup.numero_whatsapp,
                    data=inbound_bytes,
                    mime=ctx.get("inbound_imagen_mime") or "image/jpeg",
                    caption=f"Imagen del cliente {cliente_numero} ({args['tipo']})",
                )
            imagen_reenviada = True
            log.info(
                "tools.escalar.imagen_encolada",
                destinos=[s.numero_whatsapp for s in superiores],
                tipo=args["tipo"],
                bytes=len(inbound_bytes),
            )

    return {
        "escalado": True,
        "tipo": args["tipo"],
        "area": area,
        "responsables": [s.nombre for s in superiores],
        "notificado_whatsapp": f"encolado a {len(superiores)} admin(s) (sale después del commit)",
        "imagen_reenviada": imagen_reenviada,
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
# HELPERS DE SESIÓN — persistir preferencias del cliente para próximos turnos
# ════════════════════════════════════════════════════════════════════════════

def _enqueue_text(ctx: dict, *, to: str, text: str, alerta_id: int | None = None) -> None:
    """Encola un mensaje de texto outbound para drenar DESPUÉS del session.commit().

    Resuelve el bug de inconsistencia transaccional: si entre el flush y el
    commit hay rollback, los mensajes que YA salieron por whapi quedan
    huérfanos (Fabio recibe 'PEDIDO NUEVO #X' pero la BD no tiene el pedido).
    """
    ctx.setdefault("outbox", []).append({
        "kind": "text",
        "to": to,
        "text": text,
        "alerta_id": alerta_id,
    })


def _enqueue_image_bytes(
    ctx: dict, *, to: str, data: bytes, mime: str = "image/jpeg",
    caption: str | None = None,
) -> None:
    """Encola una imagen (bytes) outbound para drenar DESPUÉS del commit."""
    ctx.setdefault("outbox", []).append({
        "kind": "image_bytes",
        "to": to,
        "data": data,
        "mime": mime,
        "caption": caption,
    })


async def _update_sesion(
    session: AsyncSession,
    cliente_id: int | None,
    **campos: object,
) -> None:
    """Actualiza la fila de sesiones del cliente con los campos no-None que llegan.

    Se llama desde los handlers cuando se aprende algo nuevo del cliente
    (talla, color, barrio, banco elegido, etc.) para que el próximo turno
    Claude lo vea inyectado en el system prompt vía
    `_construir_contexto_cliente`. Sin esto, la BD lo guarda pero el modelo
    nunca lo ve y le sigue preguntando lo mismo.

    No-op si cliente_id es None o no se pasa nada.
    """
    if not cliente_id or not campos:
        return
    valores = {k: v for k, v in campos.items() if v is not None}
    if not valores:
        return
    try:
        await session.execute(
            update(Sesion).where(Sesion.cliente_id == cliente_id).values(**valores)
        )
    except Exception as e:
        log.warning("tools._update_sesion.fail", cliente_id=cliente_id, error=str(e))


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
