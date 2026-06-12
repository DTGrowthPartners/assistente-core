"""Funciones de acceso a DB usadas por flows."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertaFabio,
    Cliente,
    Conversacion,
    IntervencionHumana,
    Sesion,
    WebhookProcesado,
)


# ── DEDUPE ────────────────────────────────────────────────────────────────────


async def ya_procesado(session: AsyncSession, message_id: str) -> bool:
    """True si este message_id ya pasó por el webhook antes."""
    stmt = select(WebhookProcesado.message_id).where(WebhookProcesado.message_id == message_id)
    return (await session.execute(stmt)).scalar_one_or_none() is not None


async def marcar_procesado(session: AsyncSession, message_id: str) -> None:
    """Inserta el message_id en la tabla de dedupe (ignora si ya existe)."""
    stmt = (
        insert(WebhookProcesado)
        .values(message_id=message_id)
        .on_conflict_do_nothing(index_elements=["message_id"])
    )
    await session.execute(stmt)


# ── CLIENTES ──────────────────────────────────────────────────────────────────


async def get_or_create_cliente(
    session: AsyncSession,
    numero: str,
    nombre: str | None = None,
) -> Cliente:
    """Get-or-create por número de WhatsApp.

    Si el cliente ya existe pero no tiene `nombre` guardado y se pasa uno
    nuevo (típicamente el pushname de WhatsApp), lo rellena. NUNCA
    sobreescribe un nombre ya guardado — preserva ediciones manuales y
    nombres reales que el cliente haya dado.
    """
    nombre_limpio = _limpiar_pushname(nombre)
    stmt = select(Cliente).where(Cliente.numero_whatsapp == numero)
    cliente = (await session.execute(stmt)).scalar_one_or_none()
    if cliente:
        cliente.ultimo_contacto = datetime.now(timezone.utc)
        if nombre_limpio and not (cliente.nombre or "").strip():
            cliente.nombre = nombre_limpio
        return cliente

    cliente = Cliente(numero_whatsapp=numero, nombre=nombre_limpio)
    session.add(cliente)
    await session.flush()  # para tener el id
    return cliente


_EMOJI_RE = __import__("re").compile(
    "["
    "\U0001F300-\U0001F9FF"
    "\U0001FA00-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0001F000-\U0001F2FF"
    "\U0001F600-\U0001F64F"
    "\U0001F680-\U0001F6FF"
    "‍️"
    "]+",
    flags=__import__("re").UNICODE,
)


def _limpiar_pushname(raw: str | None) -> str | None:
    """Pushname de whapi → string seguro para BD. None si queda vacío."""
    if not raw:
        return None
    s = _EMOJI_RE.sub("", str(raw))
    s = " ".join(s.split())  # colapsa espacios + trim
    if not s:
        return None
    return s[:100]


async def cliente_esta_bloqueado(session: AsyncSession, numero: str) -> bool:
    stmt = select(Cliente.bloqueado).where(Cliente.numero_whatsapp == numero)
    bloqueado = (await session.execute(stmt)).scalar_one_or_none()
    return bool(bloqueado)


# ── INTERVENCIÓN HUMANA ───────────────────────────────────────────────────────


async def bot_pausado(session: AsyncSession, cliente_id: int) -> bool:
    """¿Hay una asesora humana atendiendo este chat?"""
    stmt = select(IntervencionHumana.pausado_hasta).where(
        IntervencionHumana.cliente_id == cliente_id
    )
    pausa = (await session.execute(stmt)).scalar_one_or_none()
    if pausa is None:
        return False
    return pausa > datetime.now(timezone.utc)


async def bot_pausado_por_numero(
    session: AsyncSession, numero: str
) -> tuple[bool, int | None]:
    """¿Está pausado el bot para este número específico?

    Devuelve (pausado, cliente_id). El cliente_id puede ser None si el contacto
    aún no existe en BD. Útil para chequear pausa ANTES de rutear (al flow de
    equipo, prospecto, etc.) sin tener que crear el cliente primero.
    """
    stmt = (
        select(Cliente.id, IntervencionHumana.pausado_hasta)
        .select_from(Cliente)
        .join(
            IntervencionHumana,
            IntervencionHumana.cliente_id == Cliente.id,
            isouter=True,
        )
        .where(Cliente.numero_whatsapp == numero)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        return False, None
    cli_id, pausa = row
    if pausa is None:
        return False, cli_id
    return (pausa > datetime.now(timezone.utc)), cli_id


async def estado_chat_por_numero(
    session: AsyncSession, numero: str
) -> tuple[int | None, str | None, bool]:
    """Single-query helper para enrutar mensajes.

    Devuelve (cliente_id, etiqueta, pausado_indiv).
    - cliente_id: None si el contacto aún no existe.
    - etiqueta: 'cliente'|'prospecto'|'equipo'|'personal'|None.
    - pausado_indiv: True si hay intervencion_humana activa.

    Usado por el webhook antes de rutear: si etiqueta='personal' (el admin
    explícitamente marcó el contacto como personal), debe VETAR el flujo de
    bot operativo aunque el número aparezca en equipo_miembros / contactos_wl.
    """
    stmt = (
        select(Cliente.id, Cliente.etiqueta, IntervencionHumana.pausado_hasta)
        .select_from(Cliente)
        .join(
            IntervencionHumana,
            IntervencionHumana.cliente_id == Cliente.id,
            isouter=True,
        )
        .where(Cliente.numero_whatsapp == numero)
    )
    row = (await session.execute(stmt)).first()
    if not row:
        return None, None, False
    cli_id, etiqueta, pausa = row
    pausado = bool(pausa and pausa > datetime.now(timezone.utc))
    return cli_id, etiqueta, pausado


async def pausar_bot(
    session: AsyncSession,
    cliente_id: int,
    horas: int = 1,
    razon: str = "asesora humana intervino",
) -> None:
    """Marca el cliente como en intervención humana."""
    hasta = datetime.now(timezone.utc) + timedelta(hours=horas)
    stmt = (
        insert(IntervencionHumana)
        .values(cliente_id=cliente_id, pausado_hasta=hasta, razon=razon)
        .on_conflict_do_update(
            index_elements=["cliente_id"],
            set_={"pausado_hasta": hasta, "razon": razon},
        )
    )
    await session.execute(stmt)


# ── CONVERSACIONES ────────────────────────────────────────────────────────────


async def guardar_conversacion(
    session: AsyncSession,
    cliente_id: int,
    direccion: str,
    tipo: str,
    contenido: str | None,
    whapi_message_id: str | None = None,
    media_url: str | None = None,
    media_path_local: str | None = None,
    intent: str | None = None,
    tokens_input: int | None = None,
    tokens_output: int | None = None,
    cache_read_tokens: int | None = None,
    cache_create_tokens: int | None = None,
    modelo: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> Conversacion:
    """Persiste un mensaje (inbound, outbound o humano)."""
    conv = Conversacion(
        cliente_id=cliente_id,
        direccion=direccion,
        tipo=tipo,
        contenido=contenido,
        whapi_message_id=whapi_message_id,
        media_url=media_url,
        media_path_local=media_path_local,
        intent=intent,
        tokens_input=tokens_input,
        tokens_output=tokens_output,
        cache_read_tokens=cache_read_tokens,
        cache_create_tokens=cache_create_tokens,
        modelo=modelo,
        metadata_=metadata or {},
    )
    session.add(conv)
    await session.flush()
    return conv


async def ultimos_mensajes(
    session: AsyncSession,
    cliente_id: int,
    n: int = 10,
    horas_max: int = 24,
) -> list[Conversacion]:
    """
    Últimos N mensajes del cliente DE LAS ÚLTIMAS `horas_max` HORAS,
    ordenados cronológicamente.

    Por qué el límite temporal: si el cliente vuelve después de varios días
    con un simple "Hola", el modelo puede asumir contexto viejo y alucinar.
    Subimos a 24h (un día comercial) y dejamos que el caller marque mensajes
    >4h con un separador visible para que el modelo entienda el gap sin
    inventar continuidad.
    """
    desde = datetime.now(timezone.utc) - timedelta(hours=horas_max)
    stmt = (
        select(Conversacion)
        .where(Conversacion.cliente_id == cliente_id)
        .where(Conversacion.timestamp > desde)
        .order_by(Conversacion.timestamp.desc())
        .limit(n)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(reversed(rows))


# ── SESIONES ──────────────────────────────────────────────────────────────────


async def get_or_create_sesion(session: AsyncSession, cliente_id: int) -> Sesion:
    stmt = select(Sesion).where(Sesion.cliente_id == cliente_id)
    s = (await session.execute(stmt)).scalar_one_or_none()
    if s:
        return s
    s = Sesion(cliente_id=cliente_id)
    session.add(s)
    await session.flush()
    return s


# ── ALERTAS A FABIO ───────────────────────────────────────────────────────────


async def registrar_alerta_fabio(
    session: AsyncSession,
    tipo: str,
    mensaje: str,
    cliente_id: int | None = None,
    media_url: str | None = None,
    whapi_message_id: str | None = None,
) -> AlertaFabio:
    alerta = AlertaFabio(
        cliente_id=cliente_id,
        tipo=tipo,
        mensaje=mensaje,
        media_url=media_url,
        whapi_message_id=whapi_message_id,
    )
    session.add(alerta)
    await session.flush()
    # Avisar a la plataforma admin externa (no bloqueante)
    try:
        import asyncio as _aio
        from app.panel_admin_webhook import emitir_evento as _emit
        _aio.create_task(_emit("bot.alerta_abierta", {
            "alerta_id": alerta.id,
            "tipo": tipo,
            "mensaje": mensaje[:500],
            "cliente_id": cliente_id,
        }))
    except Exception:
        pass
    return alerta
