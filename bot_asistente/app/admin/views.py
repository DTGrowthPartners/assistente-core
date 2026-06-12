"""ModelViews del panel /admin. Una clase por modelo."""

from __future__ import annotations

from sqladmin import ModelView

from app.db.models import (
    AlertaFabio,
    Cita,
    Cliente,
    ContactoWhitelist,
    Conversacion,
    EquipoMiembro,
    IntervencionHumana,
    NumeroInterno,
    Prospecto,
    Sesion,
)

# Nota técnica: SQLAdmin requiere `column_filters`/`column_sortable_list` como
# list[str] con el nombre del atributo, NO columnas SQLAlchemy.


# Labels comunes (compartidas entre varias vistas) — fuente única de verdad.
COMMON_LABELS = {
    "id": "ID",
    "cliente_id": "Cliente",
    "numero_whatsapp": "Número",
    "nombre": "Nombre",
    "email": "Correo",
    "telefono": "Teléfono",
    "ciudad": "Ciudad",
    "barrio": "Barrio",
    "empresa": "Empresa",
    "nit": "NIT",
    "rol": "Rol",
    "areas": "Áreas",
    "es_fallback": "Por defecto",
    "activo": "Activo",
    "notas": "Notas",
    "razon": "Razón",
    "permisos": "Permisos",
    "meta_account_id": "Cuenta Meta",
    "dtos_client_id": "ID DTOS",
    "horario_lunes_sabado": "Horario L-S",
    "horario_domingo": "Horario domingo",
    "tipo": "Tipo",
    "mensaje": "Mensaje",
    "contenido": "Contenido",
    "resuelto": "Resuelto",
    "enviado_a_fabio_en": "Enviada",
    "created_at": "Creada",
    "updated_at": "Actualizada",
    "timestamp": "Fecha",
    "fecha_inicio": "Fecha",
    "fecha_fin": "Fin",
    "primer_contacto": "Primer contacto",
    "ultimo_contacto": "Último contacto",
    "ultima_interaccion": "Última interacción",
    "bloqueado": "Bloqueado",
    "etiqueta": "Etiqueta",
    "negocio": "Negocio",
    "sector": "Sector",
    "estado": "Estado",
    "ya_pauta": "¿Ya pauta?",
    "tiene_web": "¿Tiene web?",
    "presupuesto_meta": "Presupuesto Meta",
    "direccion": "Dirección",
    "intent": "Intent",
    "tokens_input": "Tokens entrada",
    "tokens_output": "Tokens salida",
    "costo_usd": "Costo USD",
    "modelo": "Modelo",
    "media_url": "Adjunto",
    "whapi_message_id": "WhAPI ID",
    "metadata_": "Metadata",
    "pausado_hasta": "Pausado hasta",
    "activado_en": "Activada",
    "etiqueta_actualizada_en": "Etiqueta actualizada",
    "etiqueta_actualizada_por": "Etiqueta por",
}


# ─── CONFIGURACIÓN ──────────────────────────────────────────────────────────


class EquipoMiembroAdmin(ModelView, model=EquipoMiembro):
    name = "Miembro del equipo"
    name_plural = "Equipo"
    icon = "fa-solid fa-users-cog"
    category = "Configuración"

    column_list = ["nombre", "numero_whatsapp", "rol", "areas", "es_fallback", "activo"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["nombre", "numero_whatsapp"]
    column_sortable_list = ["id", "nombre", "activo"]
    form_columns = [
        EquipoMiembro.nombre, EquipoMiembro.numero_whatsapp, EquipoMiembro.rol,
        EquipoMiembro.areas, EquipoMiembro.es_fallback, EquipoMiembro.horario_lunes_sabado,
        EquipoMiembro.horario_domingo, EquipoMiembro.activo, EquipoMiembro.notas,
    ]


class ContactoWhitelistAdmin(ModelView, model=ContactoWhitelist):
    name = "Contacto (whitelist)"
    name_plural = "Whitelist"
    icon = "fa-solid fa-address-book"
    category = "Configuración"

    column_list = ["nombre", "numero_whatsapp", "rol", "empresa", "meta_account_id", "activo"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["nombre", "numero_whatsapp", "empresa", "nit"]
    column_sortable_list = ["id", "rol", "empresa"]
    form_columns = [
        ContactoWhitelist.numero_whatsapp, ContactoWhitelist.rol, ContactoWhitelist.nombre,
        ContactoWhitelist.empresa, ContactoWhitelist.email, ContactoWhitelist.nit,
        ContactoWhitelist.dtos_client_id, ContactoWhitelist.meta_account_id,
        ContactoWhitelist.permisos, ContactoWhitelist.activo, ContactoWhitelist.notas,
    ]
    column_descriptions = {
        "rol": "equipo = acceso operativo total · cliente = acceso scoped a su propia cuenta",
        "meta_account_id": "act_... de Meta Ads, para reportes automáticos",
    }


class NumeroInternoAdmin(ModelView, model=NumeroInterno):
    name = "Número interno"
    name_plural = "Números internos"
    icon = "fa-solid fa-shield-halved"
    category = "Configuración"

    column_list = ["numero_whatsapp", "nombre", "razon", "activo"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["numero_whatsapp", "nombre"]
    column_sortable_list = ["id", "numero_whatsapp"]
    form_columns = [
        NumeroInterno.numero_whatsapp, NumeroInterno.nombre, NumeroInterno.razon, NumeroInterno.activo,
    ]


# ─── OPERACIÓN ──────────────────────────────────────────────────────────────


class ClienteAdmin(ModelView, model=Cliente):
    name = "Contacto"
    name_plural = "Contactos (chats)"
    icon = "fa-solid fa-user"
    category = "Operación"

    column_list = ["id", "numero_whatsapp", "nombre", "email", "ciudad", "etiqueta", "bloqueado", "ultimo_contacto"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["numero_whatsapp", "nombre", "email", "ciudad"]
    column_sortable_list = ["id", "ultimo_contacto", "primer_contacto"]
    page_size = 50
    can_create = True
    can_edit = True
    can_delete = True
    can_view_details = True


class ProspectoAdmin(ModelView, model=Prospecto):
    name = "Prospecto"
    name_plural = "Prospectos"
    icon = "fa-solid fa-user-plus"
    category = "Operación"

    column_list = ["cliente_id", "negocio", "sector", "ciudad", "estado", "ya_pauta", "tiene_web", "updated_at"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["negocio", "sector", "ciudad"]
    column_sortable_list = ["estado", "updated_at"]
    page_size = 50


class CitaAdmin(ModelView, model=Cita):
    name = "Cita"
    name_plural = "Citas"
    icon = "fa-solid fa-calendar-check"
    category = "Operación"

    column_list = ["id", "fecha_inicio", "nombre", "negocio", "email", "estado", "cliente_id"]
    column_labels = COMMON_LABELS
    column_searchable_list = ["nombre", "negocio", "email"]
    column_sortable_list = ["fecha_inicio", "estado"]
    page_size = 50


class ConversacionAdmin(ModelView, model=Conversacion):
    name = "Conversación"
    name_plural = "Conversaciones"
    icon = "fa-solid fa-comments"
    category = "Operación"

    column_list = ["id", "cliente_id", "timestamp", "direccion", "tipo", "intent", "contenido", "costo_usd"]
    column_labels = COMMON_LABELS
    column_sortable_list = ["id", "timestamp"]
    column_searchable_list = ["contenido"]
    can_create = False
    can_edit = False
    page_size = 100


class AlertaFabioAdmin(ModelView, model=AlertaFabio):
    name = "Alerta / pendiente"
    name_plural = "Alertas y pendientes"
    icon = "fa-solid fa-triangle-exclamation"
    category = "Operación"

    column_list = ["id", "tipo", "cliente_id", "mensaje", "resuelto", "created_at"]
    column_labels = COMMON_LABELS
    column_sortable_list = ["id", "created_at"]
    form_columns = [AlertaFabio.tipo, AlertaFabio.mensaje, AlertaFabio.resuelto]
    page_size = 50


class SesionAdmin(ModelView, model=Sesion):
    name = "Sesión activa"
    name_plural = "Sesiones"
    icon = "fa-solid fa-clock"
    category = "Operación"

    column_list = ["cliente_id", "estado", "ultima_interaccion"]
    column_labels = COMMON_LABELS
    column_sortable_list = ["ultima_interaccion"]
    page_size = 50


class IntervencionHumanaAdmin(ModelView, model=IntervencionHumana):
    name = "Pausa por humano"
    name_plural = "Pausas por humano"
    icon = "fa-solid fa-hand"
    category = "Operación"

    column_list = ["cliente_id", "pausado_hasta", "razon", "activado_en"]
    column_labels = COMMON_LABELS
    column_sortable_list = ["pausado_hasta"]


ALL_VIEWS = [
    EquipoMiembroAdmin,
    ContactoWhitelistAdmin,
    NumeroInternoAdmin,
    ClienteAdmin,
    ProspectoAdmin,
    CitaAdmin,
    ConversacionAdmin,
    AlertaFabioAdmin,
    SesionAdmin,
    IntervencionHumanaAdmin,
]
