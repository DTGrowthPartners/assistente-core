"""ModelViews del panel /admin. Una clase por modelo."""

from __future__ import annotations

from sqladmin import ModelView

from app.db.models import (
    AlertaFabio,
    Cliente,
    Conversacion,
    EquipoMiembro,
    IntervencionHumana,
    NumeroInterno,
    Pedido,
    ProductoCache,
    Sesion,
    TarifaDomicilio,
)


# ─── EQUIPO ────────────────────────────────────────────────────────────────


class EquipoMiembroAdmin(ModelView, model=EquipoMiembro):
    name = "Miembro del equipo"
    name_plural = "Equipo"
    icon = "fa-solid fa-users-cog"
    category = "Configuración"

    column_list = [
        EquipoMiembro.nombre,
        EquipoMiembro.numero_whatsapp,
        EquipoMiembro.rol,
        EquipoMiembro.areas,
        EquipoMiembro.es_fallback,
        EquipoMiembro.activo,
    ]
    column_searchable_list = [EquipoMiembro.nombre, EquipoMiembro.numero_whatsapp]
    column_sortable_list = [EquipoMiembro.id, EquipoMiembro.nombre, EquipoMiembro.activo]
    column_default_sort = [(EquipoMiembro.es_fallback, True), (EquipoMiembro.nombre, False)]
    form_columns = [
        EquipoMiembro.nombre,
        EquipoMiembro.numero_whatsapp,
        EquipoMiembro.rol,
        EquipoMiembro.areas,
        EquipoMiembro.es_fallback,
        EquipoMiembro.horario_lunes_sabado,
        EquipoMiembro.horario_domingo,
        EquipoMiembro.activo,
        EquipoMiembro.notas,
    ]


class NumeroInternoAdmin(ModelView, model=NumeroInterno):
    name = "Número interno"
    name_plural = "Números internos"
    icon = "fa-solid fa-shield-halved"
    category = "Configuración"

    column_list = [
        NumeroInterno.numero_whatsapp,
        NumeroInterno.nombre,
        NumeroInterno.razon,
        NumeroInterno.activo,
    ]
    column_searchable_list = [NumeroInterno.numero_whatsapp, NumeroInterno.nombre]
    column_sortable_list = [NumeroInterno.id, NumeroInterno.numero_whatsapp]
    form_columns = [
        NumeroInterno.numero_whatsapp,
        NumeroInterno.nombre,
        NumeroInterno.razon,
        NumeroInterno.activo,
    ]


# ─── CLIENTES ──────────────────────────────────────────────────────────────


class ClienteAdmin(ModelView, model=Cliente):
    name = "Cliente"
    name_plural = "Clientes"
    icon = "fa-solid fa-user"
    category = "Operación"

    column_list = [
        Cliente.id,
        Cliente.numero_whatsapp,
        Cliente.nombre,
        Cliente.ciudad,
        Cliente.barrio,
        Cliente.es_mayorista,
        Cliente.bloqueado,
        Cliente.ultimo_contacto,
    ]
    column_searchable_list = [
        Cliente.numero_whatsapp, Cliente.nombre, Cliente.ciudad, Cliente.barrio,
    ]
    column_sortable_list = [Cliente.id, Cliente.ultimo_contacto, Cliente.primer_contacto]
    column_default_sort = [("ultimo_contacto", True)]
    page_size = 50


class PedidoAdmin(ModelView, model=Pedido):
    name = "Pedido"
    name_plural = "Pedidos"
    icon = "fa-solid fa-receipt"
    category = "Operación"

    column_list = [
        Pedido.id,
        Pedido.cliente_id,
        Pedido.total,
        Pedido.estado,
        Pedido.ciudad,
        Pedido.barrio,
        Pedido.metodo_pago,
        Pedido.created_at,
    ]
    column_sortable_list = [Pedido.id, Pedido.created_at, Pedido.total, Pedido.estado]
    column_default_sort = [("created_at", True)]
    column_searchable_list = [Pedido.ciudad, Pedido.barrio]
    column_filters = [Pedido.estado, Pedido.metodo_pago]
    page_size = 50


class ConversacionAdmin(ModelView, model=Conversacion):
    name = "Conversación"
    name_plural = "Conversaciones"
    icon = "fa-solid fa-comments"
    category = "Operación"

    column_list = [
        Conversacion.id,
        Conversacion.cliente_id,
        Conversacion.timestamp,
        Conversacion.direccion,
        Conversacion.tipo,
        Conversacion.intent,
        Conversacion.contenido,
        Conversacion.costo_usd,
    ]
    column_sortable_list = [Conversacion.id, Conversacion.timestamp]
    column_default_sort = [("timestamp", True)]
    column_searchable_list = [Conversacion.contenido]
    column_filters = [Conversacion.direccion, Conversacion.intent, Conversacion.tipo]
    can_create = False
    can_edit = False
    page_size = 100


class AlertaFabioAdmin(ModelView, model=AlertaFabio):
    name = "Alerta a equipo"
    name_plural = "Alertas a equipo"
    icon = "fa-solid fa-triangle-exclamation"
    category = "Operación"

    column_list = [
        AlertaFabio.id,
        AlertaFabio.tipo,
        AlertaFabio.cliente_id,
        AlertaFabio.mensaje,
        AlertaFabio.enviado_a_fabio_en,
        AlertaFabio.resuelto,
        AlertaFabio.created_at,
    ]
    column_sortable_list = [AlertaFabio.id, AlertaFabio.created_at]
    column_default_sort = [("created_at", True)]
    column_filters = [AlertaFabio.tipo, AlertaFabio.resuelto]
    form_columns = [
        AlertaFabio.tipo,
        AlertaFabio.mensaje,
        AlertaFabio.resuelto,
    ]
    page_size = 50


class SesionAdmin(ModelView, model=Sesion):
    name = "Sesión activa"
    name_plural = "Sesiones"
    icon = "fa-solid fa-clock"
    category = "Operación"

    column_list = [
        Sesion.cliente_id,
        Sesion.estado,
        Sesion.producto_actual_ref,
        Sesion.talla_interes,
        Sesion.barrio,
        Sesion.ultima_interaccion,
    ]
    column_sortable_list = [Sesion.ultima_interaccion]
    column_default_sort = [("ultima_interaccion", True)]
    column_filters = [Sesion.estado]
    page_size = 50


class IntervencionHumanaAdmin(ModelView, model=IntervencionHumana):
    name = "Pausa por humano"
    name_plural = "Pausas por humano"
    icon = "fa-solid fa-hand"
    category = "Operación"

    column_list = [
        IntervencionHumana.cliente_id,
        IntervencionHumana.pausado_hasta,
        IntervencionHumana.razon,
        IntervencionHumana.activado_en,
    ]
    column_sortable_list = [IntervencionHumana.pausado_hasta]


# ─── CATÁLOGO ──────────────────────────────────────────────────────────────


class ProductoCacheAdmin(ModelView, model=ProductoCache):
    name = "Producto"
    name_plural = "Productos"
    icon = "fa-solid fa-tag"
    category = "Catálogo"

    column_list = [
        ProductoCache.ref,
        ProductoCache.nombre,
        ProductoCache.categoria,
        ProductoCache.precio_detal,
        ProductoCache.tallas,
        ProductoCache.origen,
        ProductoCache.activo,
    ]
    column_searchable_list = [ProductoCache.ref, ProductoCache.nombre]
    column_sortable_list = [ProductoCache.ref, ProductoCache.precio_detal, ProductoCache.sincronizado_en]
    column_filters = [ProductoCache.categoria, ProductoCache.origen, ProductoCache.activo]
    page_size = 50


class TarifaDomicilioAdmin(ModelView, model=TarifaDomicilio):
    name = "Tarifa de domicilio"
    name_plural = "Tarifas (Cartagena)"
    icon = "fa-solid fa-truck"
    category = "Catálogo"

    column_list = [
        TarifaDomicilio.barrio,
        TarifaDomicilio.zona,
        TarifaDomicilio.precio,
        TarifaDomicilio.tipo,
    ]
    column_searchable_list = [TarifaDomicilio.barrio, TarifaDomicilio.zona]
    column_sortable_list = [TarifaDomicilio.barrio, TarifaDomicilio.precio]
    column_filters = [TarifaDomicilio.tipo, TarifaDomicilio.zona]
    page_size = 100


ALL_VIEWS = [
    EquipoMiembroAdmin,
    NumeroInternoAdmin,
    ClienteAdmin,
    PedidoAdmin,
    ConversacionAdmin,
    AlertaFabioAdmin,
    SesionAdmin,
    IntervencionHumanaAdmin,
    ProductoCacheAdmin,
    TarifaDomicilioAdmin,
]
