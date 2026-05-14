"""SQLAlchemy 2.0 models — mapeo de las tablas creadas por schema.sql."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Cliente(Base):
    __tablename__ = "clientes"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero_whatsapp: Mapped[str] = mapped_column(String(20), unique=True, index=True)
    nombre: Mapped[str | None] = mapped_column(String(255))
    ciudad: Mapped[str | None] = mapped_column(String(100))
    barrio: Mapped[str | None] = mapped_column(String(150))
    primer_contacto: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ultimo_contacto: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    es_mayorista: Mapped[bool] = mapped_column(Boolean, default=False)
    bloqueado: Mapped[bool] = mapped_column(Boolean, default=False)
    razon_bloqueo: Mapped[str | None] = mapped_column(Text)
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class Conversacion(Base):
    __tablename__ = "conversaciones"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("clientes.id", ondelete="CASCADE"), index=True)
    whapi_message_id: Mapped[str | None] = mapped_column(String(100))
    direccion: Mapped[str] = mapped_column(String(10))  # inbound | outbound | humano
    tipo: Mapped[str] = mapped_column(String(20))
    contenido: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    media_path_local: Mapped[str | None] = mapped_column(Text)
    intent: Mapped[str | None] = mapped_column(String(50))
    tokens_input: Mapped[int | None] = mapped_column(Integer)
    tokens_output: Mapped[int | None] = mapped_column(Integer)
    cache_read_tokens: Mapped[int | None] = mapped_column(Integer)
    cache_create_tokens: Mapped[int | None] = mapped_column(Integer)
    costo_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    modelo: Mapped[str | None] = mapped_column(String(50))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)


class WebhookProcesado(Base):
    __tablename__ = "webhooks_procesados"

    message_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    procesado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IntervencionHumana(Base):
    __tablename__ = "intervencion_humana"

    cliente_id: Mapped[int] = mapped_column(
        ForeignKey("clientes.id", ondelete="CASCADE"), primary_key=True
    )
    pausado_hasta: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    razon: Mapped[str | None] = mapped_column(Text)
    activado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activado_por: Mapped[str | None] = mapped_column(String(50))


class Sesion(Base):
    __tablename__ = "sesiones"

    cliente_id: Mapped[int] = mapped_column(
        ForeignKey("clientes.id", ondelete="CASCADE"), primary_key=True
    )
    estado: Mapped[str] = mapped_column(String(50), default="inicial")
    producto_actual_ref: Mapped[str | None] = mapped_column(String(50))
    productos_mostrados: Mapped[list[str]] = mapped_column(JSONB, default=list)
    talla_interes: Mapped[str | None] = mapped_column(String(20))
    color_interes: Mapped[str | None] = mapped_column(String(50))
    metodo_pago_elegido: Mapped[str | None] = mapped_column(String(50))
    banco_elegido: Mapped[str | None] = mapped_column(String(50))
    barrio: Mapped[str | None] = mapped_column(String(150))
    direccion_envio: Mapped[str | None] = mapped_column(Text)
    notas_internas: Mapped[str | None] = mapped_column(Text)
    ultima_interaccion: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    proximo_seguimiento: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Pedido(Base):
    __tablename__ = "pedidos"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int] = mapped_column(ForeignKey("clientes.id"))
    shopify_draft_order_id: Mapped[int | None] = mapped_column(BigInteger)
    shopify_draft_invoice_url: Mapped[str | None] = mapped_column(Text)
    items: Mapped[list[dict]] = mapped_column(JSONB)
    subtotal: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    domicilio: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=0)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    estado: Mapped[str] = mapped_column(String(30), default="cotizacion")
    direccion_envio: Mapped[str | None] = mapped_column(Text)
    ciudad: Mapped[str | None] = mapped_column(String(100))
    barrio: Mapped[str | None] = mapped_column(String(150))
    metodo_pago: Mapped[str | None] = mapped_column(String(50))
    banco: Mapped[str | None] = mapped_column(String(50))
    comprobante_url: Mapped[str | None] = mapped_column(Text)
    notas: Mapped[str | None] = mapped_column(Text)
    notificado_a_fabio_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmado_por_fabio_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TarifaDomicilio(Base):
    __tablename__ = "tarifas_domicilio"

    id: Mapped[int] = mapped_column(primary_key=True)
    barrio: Mapped[str] = mapped_column(String(200))
    barrio_normalizado: Mapped[str] = mapped_column(String(200), unique=True)
    zona: Mapped[str | None] = mapped_column(String(80))
    precio: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    tipo: Mapped[str] = mapped_column(String(20))
    notas: Mapped[str | None] = mapped_column(Text)


class ProductoCache(Base):
    __tablename__ = "productos_cache"

    ref: Mapped[str] = mapped_column(String(100), primary_key=True)
    origen: Mapped[str] = mapped_column(String(30), default="shopify")
    shopify_product_id: Mapped[int | None] = mapped_column(BigInteger)
    shopify_handle: Mapped[str | None] = mapped_column(String(255))
    fuente_url: Mapped[str | None] = mapped_column(Text)
    nombre: Mapped[str | None] = mapped_column(String(255))
    descripcion: Mapped[str | None] = mapped_column(Text)
    categoria: Mapped[str | None] = mapped_column(String(100))
    precio_detal: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    precio_mayor: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    tallas: Mapped[list[str]] = mapped_column(JSONB, default=list)
    colores: Mapped[list[str]] = mapped_column(JSONB, default=list)
    variants: Mapped[list[dict]] = mapped_column(JSONB, default=list)
    imagen_url: Mapped[str | None] = mapped_column(Text)
    imagen_url_extras: Mapped[list[str]] = mapped_column(JSONB, default=list)
    foto_local: Mapped[str | None] = mapped_column(String(255))
    video_local: Mapped[str | None] = mapped_column(String(255))
    asumir_disponible: Mapped[bool] = mapped_column(Boolean, default=True)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    sincronizado_en: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    notas: Mapped[str | None] = mapped_column(Text)


class AlertaFabio(Base):
    __tablename__ = "alertas_fabio"

    id: Mapped[int] = mapped_column(primary_key=True)
    cliente_id: Mapped[int | None] = mapped_column(ForeignKey("clientes.id", ondelete="SET NULL"))
    tipo: Mapped[str] = mapped_column(String(50))
    mensaje: Mapped[str] = mapped_column(Text)
    media_url: Mapped[str | None] = mapped_column(Text)
    whapi_message_id: Mapped[str | None] = mapped_column(String(100))
    enviado_a_fabio_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resuelto: Mapped[bool] = mapped_column(Boolean, default=False)
    resuelto_en: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class EquipoMiembro(Base):
    """Superior del equipo que recibe escalaciones del bot."""
    __tablename__ = "equipo_miembros"

    id: Mapped[int] = mapped_column(primary_key=True)
    nombre: Mapped[str] = mapped_column(String(100))
    numero_whatsapp: Mapped[str] = mapped_column(String(20), unique=True)
    rol: Mapped[str | None] = mapped_column(String(50))
    areas: Mapped[list[str]] = mapped_column(JSONB, default=list)
    es_fallback: Mapped[bool] = mapped_column(Boolean, default=False)
    horario_lunes_sabado: Mapped[str | None] = mapped_column(String(30))
    horario_domingo: Mapped[str | None] = mapped_column(String(30))
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    notas: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return f"{self.nombre} ({self.numero_whatsapp})"


class NumeroInterno(Base):
    """Número del equipo que el bot ignora silenciosamente."""
    __tablename__ = "numeros_internos"

    id: Mapped[int] = mapped_column(primary_key=True)
    numero_whatsapp: Mapped[str] = mapped_column(String(20), unique=True)
    nombre: Mapped[str | None] = mapped_column(String(100))
    razon: Mapped[str | None] = mapped_column(Text)
    activo: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    def __str__(self) -> str:
        return f"{self.numero_whatsapp} — {self.nombre or '?'}"
