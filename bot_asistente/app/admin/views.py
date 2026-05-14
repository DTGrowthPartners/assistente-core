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

# Nota técnica: SQLAdmin requiere `column_filters` como list[str] con el
# nombre del atributo, NO como columnas SQLAlchemy. Lo mismo para
# `column_sortable_list` y `column_default_sort` cuando se pasa al constructor.


# ─── EQUIPO ────────────────────────────────────────────────────────────────


class EquipoMiembroAdmin(ModelView, model=EquipoMiembro):
    name = "Miembro del equipo"
    name_plural = "Equipo"
    icon = "fa-solid fa-users-cog"
    category = "Configuración"

    column_list = [
        "nombre",
        "numero_whatsapp",
        "rol",
        "areas",
        "es_fallback",
        "activo",
    ]
    column_searchable_list = ["nombre", "numero_whatsapp"]
    column_sortable_list = ["id", "nombre", "activo"]
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
        "numero_whatsapp",
        "nombre",
        "razon",
        "activo",
    ]
    column_searchable_list = ["numero_whatsapp", "nombre"]
    column_sortable_list = ["id", "numero_whatsapp"]
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
        "id",
        "numero_whatsapp",
        "nombre",
        "ciudad",
        "barrio",
        "es_mayorista",
        "bloqueado",
        "ultimo_contacto",
    ]
    column_searchable_list = ["numero_whatsapp", "nombre", "ciudad", "barrio"]
    column_sortable_list = ["id", "ultimo_contacto", "primer_contacto"]
    page_size = 50


class PedidoAdmin(ModelView, model=Pedido):
    name = "Pedido"
    name_plural = "Pedidos"
    icon = "fa-solid fa-receipt"
    category = "Operación"

    column_list = [
        "id",
        "cliente_id",
        "total",
        "estado",
        "ciudad",
        "barrio",
        "metodo_pago",
        "created_at",
    ]
    column_sortable_list = ["id", "created_at", "total", "estado"]
    column_searchable_list = ["ciudad", "barrio"]
    page_size = 50


class ConversacionAdmin(ModelView, model=Conversacion):
    name = "Conversación"
    name_plural = "Conversaciones"
    icon = "fa-solid fa-comments"
    category = "Operación"

    column_list = [
        "id",
        "cliente_id",
        "timestamp",
        "direccion",
        "tipo",
        "intent",
        "contenido",
        "costo_usd",
    ]
    column_sortable_list = ["id", "timestamp"]
    column_searchable_list = ["contenido"]
    can_create = False
    can_edit = False
    page_size = 100


class AlertaFabioAdmin(ModelView, model=AlertaFabio):
    name = "Alerta a equipo"
    name_plural = "Alertas a equipo"
    icon = "fa-solid fa-triangle-exclamation"
    category = "Operación"

    column_list = [
        "id",
        "tipo",
        "cliente_id",
        "mensaje",
        "enviado_a_fabio_en",
        "resuelto",
        "created_at",
    ]
    column_sortable_list = ["id", "created_at"]
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
        "cliente_id",
        "estado",
        "producto_actual_ref",
        "talla_interes",
        "barrio",
        "ultima_interaccion",
    ]
    column_sortable_list = ["ultima_interaccion"]
    page_size = 50


class IntervencionHumanaAdmin(ModelView, model=IntervencionHumana):
    name = "Pausa por humano"
    name_plural = "Pausas por humano"
    icon = "fa-solid fa-hand"
    category = "Operación"

    column_list = [
        "cliente_id",
        "pausado_hasta",
        "razon",
        "activado_en",
    ]
    column_sortable_list = ["pausado_hasta"]


# ─── CATÁLOGO ──────────────────────────────────────────────────────────────


class ProductoCacheAdmin(ModelView, model=ProductoCache):
    name = "Producto"
    name_plural = "Productos"
    icon = "fa-solid fa-tag"
    category = "Catálogo"

    column_list = [
        "ref",
        "nombre",
        "categoria",
        "precio_detal",
        "tallas",
        "origen",
        "activo",
    ]
    column_searchable_list = ["ref", "nombre"]
    column_sortable_list = ["ref", "precio_detal", "sincronizado_en"]
    page_size = 50


class TarifaDomicilioAdmin(ModelView, model=TarifaDomicilio):
    name = "Tarifa de domicilio"
    name_plural = "Tarifas (Cartagena)"
    icon = "fa-solid fa-truck"
    category = "Catálogo"

    column_list = [
        "barrio",
        "zona",
        "precio",
        "tipo",
    ]
    column_searchable_list = ["barrio", "zona"]
    column_sortable_list = ["barrio", "precio"]
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
