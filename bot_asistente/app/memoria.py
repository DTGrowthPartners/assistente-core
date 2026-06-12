"""Memoria evolutiva del bot — pilar openclaw.

El bot recuerda entre conversaciones: reglas que el equipo le enseñó, hechos
sobre contactos, preferencias, etc. Estas memorias se INYECTAN al system prompt
de cada turn según el scope (general o por contacto), y el bot puede agregarlas
él mismo vía la tool `aprender_regla` cuando le dan una directiva duradera.

Este módulo expone helpers asincrónicos que usan los flujos y las tools.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import and_, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Memoria, Recordatorio


# ─── MEMORIAS ────────────────────────────────────────────────────────────────


async def cargar_relevantes(
    session: AsyncSession,
    *,
    contacto_id: int | None = None,
    limite: int = 30,
) -> list[Memoria]:
    """Trae las memorias activas relevantes para este turn.

    Incluye: todas las `general` y las `contacto` que coincidan con `contacto_id`.
    Las `equipo` se incluyen siempre (la usa el flujo operativo principalmente).
    """
    conds = [Memoria.scope.in_(["general", "equipo"])]
    if contacto_id is not None:
        conds.append(and_(Memoria.scope == "contacto", Memoria.contacto_id == contacto_id))
    stmt = (
        select(Memoria)
        .where(Memoria.activa.is_(True))
        .where(or_(*conds))
        .order_by(Memoria.updated_at.desc())
        .limit(limite)
    )
    return list((await session.execute(stmt)).scalars().all())


def formatear_para_prompt(memorias: list[Memoria]) -> str:
    """Devuelve un bloque listo para inyectar como `extra_system` del prompt."""
    if not memorias:
        return ""
    grupos: dict[str, list[Memoria]] = {"general": [], "equipo": [], "contacto": []}
    for m in memorias:
        grupos.setdefault(m.scope, []).append(m)

    partes = ["## MEMORIA EVOLUTIVA (cosas aprendidas — RESPÉTALAS)"]
    for clave, etiqueta in (("general", "Generales"), ("equipo", "Equipo"), ("contacto", "Sobre este contacto")):
        items = grupos.get(clave) or []
        if not items:
            continue
        partes.append(f"\n### {etiqueta}")
        for m in items:
            partes.append(f"- **{m.titulo}** — {m.contenido}")
    partes.append("\nSi una nueva directiva contradice una memoria, llama `olvidar_regla` con el id antes de crear la nueva.")
    return "\n".join(partes)


async def guardar(
    session: AsyncSession,
    *,
    titulo: str,
    contenido: str,
    scope: str = "general",
    contacto_id: int | None = None,
    tipo: str = "regla",
    creado_por: str | None = None,
    tags: list[str] | None = None,
) -> Memoria:
    """Crea una memoria nueva. Valida coherencia scope ↔ contacto_id."""
    if scope == "contacto" and not contacto_id:
        raise ValueError("scope=contacto requiere contacto_id")
    if scope in ("general", "equipo") and contacto_id:
        contacto_id = None  # ignora si llega
    m = Memoria(
        scope=scope,
        contacto_id=contacto_id,
        titulo=titulo[:180],
        contenido=contenido,
        tipo=tipo,
        creado_por=creado_por,
        tags=tags or [],
        activa=True,
    )
    session.add(m)
    await session.flush()
    return m


async def desactivar(session: AsyncSession, memoria_id: int) -> bool:
    """Marca una memoria como `activa=false`. Devuelve True si la encontró."""
    res = await session.execute(
        update(Memoria).where(Memoria.id == memoria_id).values(activa=False)
    )
    return res.rowcount > 0


# ─── RECORDATORIOS ───────────────────────────────────────────────────────────


async def programar(
    session: AsyncSession,
    *,
    accion: str,
    vence_en: datetime,
    contacto_id: int | None = None,
    motivo: str | None = None,
    origen: str = "manual",
    creado_por: str | None = None,
) -> Recordatorio:
    """Crea un recordatorio pendiente. `vence_en` debe ser timezone-aware."""
    if vence_en.tzinfo is None:
        vence_en = vence_en.replace(tzinfo=timezone.utc)
    r = Recordatorio(
        contacto_id=contacto_id,
        accion=accion,
        motivo=motivo,
        vence_en=vence_en,
        origen=origen,
        creado_por=creado_por,
        estado="pendiente",
    )
    session.add(r)
    await session.flush()
    return r


async def vencidos(session: AsyncSession, ventana_horas: int = 24) -> list[Recordatorio]:
    """Recordatorios pendientes con `vence_en <= now()`, opcionalmente desde
    hace `ventana_horas` (para no procesar muy antiguos)."""
    ahora = datetime.now(timezone.utc)
    desde = ahora - timedelta(hours=ventana_horas)
    rows = (await session.execute(
        select(Recordatorio).where(
            Recordatorio.estado == "pendiente",
            Recordatorio.vence_en <= ahora,
            Recordatorio.vence_en >= desde,
        ).order_by(Recordatorio.vence_en.asc())
    )).scalars().all()
    return list(rows)


async def marcar_atendido(
    session: AsyncSession,
    recordatorio_id: int,
    notas: str | None = None,
) -> bool:
    res = await session.execute(
        update(Recordatorio).where(Recordatorio.id == recordatorio_id).values(
            estado="atendido",
            atendido_en=datetime.now(timezone.utc),
            atendido_notas=notas,
        )
    )
    return res.rowcount > 0


async def descartar(session: AsyncSession, recordatorio_id: int) -> bool:
    res = await session.execute(
        update(Recordatorio).where(Recordatorio.id == recordatorio_id).values(estado="descartado")
    )
    return res.rowcount > 0
