"""
Directorio del equipo — lee de Postgres (tablas equipo_miembros + numeros_internos).

Edita desde /admin (UI web) o vía SQL directo.

Cache:
    Mantiene un cache en memoria con TTL de 30s para no hacer una query a la DB
    en cada mensaje. Cuando alguien edita un miembro desde /admin, el cache
    expira automáticamente en máximo 30 segundos.

API sincrónica (los handlers ya viven en contexto async pero esta API se
expone síncrona porque se llama desde tools/main donde a veces no hay
session disponible — usa SQLAlchemy sincrónico bajo el capó).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import EquipoMiembro, NumeroInterno
from app.logging_setup import log

settings = get_settings()

# Engine sincrónico aparte (no compite con el async del bot)
_sync_engine = create_engine(settings.database_url_sync, pool_size=2, max_overflow=2, pool_pre_ping=True)


@dataclass(frozen=True)
class Miembro:
    nombre: str
    numero_whatsapp: str
    rol: str | None
    areas: tuple[str, ...]
    es_fallback: bool
    activo: bool
    notas: str | None = None


# ────────────────────────────────────────────────────────────────────────────
# Cache con TTL
# ────────────────────────────────────────────────────────────────────────────

CACHE_TTL_SECONDS = 30

_cache: dict[str, Any] = {
    "loaded_at": 0.0,
    "miembros": [],
    "numeros_internos": set(),
}


def _cargar_si_caducado() -> None:
    """Recarga miembros y números internos si el cache caducó."""
    ahora = time.time()
    if ahora - _cache["loaded_at"] < CACHE_TTL_SECONDS:
        return

    try:
        with Session(_sync_engine) as session:
            miembros_rows = session.execute(
                select(EquipoMiembro).where(EquipoMiembro.activo.is_(True))
            ).scalars().all()
            internos_rows = session.execute(
                select(NumeroInterno.numero_whatsapp).where(NumeroInterno.activo.is_(True))
            ).scalars().all()
    except Exception as e:
        log.error("equipo.cache.load_fail", error=str(e))
        return

    miembros: list[Miembro] = []
    for m in miembros_rows:
        miembros.append(Miembro(
            nombre=m.nombre,
            numero_whatsapp=m.numero_whatsapp,
            rol=m.rol,
            areas=tuple(m.areas or []),
            es_fallback=bool(m.es_fallback),
            activo=bool(m.activo),
            notas=m.notas,
        ))

    _cache["loaded_at"] = ahora
    _cache["miembros"] = miembros
    _cache["numeros_internos"] = set(internos_rows)
    log.debug("equipo.cache.reloaded",
              miembros=len(miembros), numeros_internos=len(internos_rows))


def invalidar_cache() -> None:
    """Forzar recarga en la próxima consulta (útil tras edición en admin)."""
    _cache["loaded_at"] = 0.0


# ────────────────────────────────────────────────────────────────────────────
# API pública
# ────────────────────────────────────────────────────────────────────────────


def superior_para(area: str | None = None) -> Miembro | None:
    """
    Devuelve el miembro responsable de un área. Cae al fallback si no encaja.
    Devuelve None solo si no hay miembros activos en DB.
    """
    _cargar_si_caducado()
    miembros: list[Miembro] = _cache["miembros"]
    if not miembros:
        return None

    if area:
        for m in miembros:
            if area in m.areas:
                return m

    for m in miembros:
        if m.es_fallback:
            return m

    return miembros[0]


def es_numero_interno(numero: str) -> bool:
    """¿Este número pertenece al equipo interno (no es cliente)?"""
    _cargar_si_caducado()
    return numero in _cache["numeros_internos"]


def es_miembro_equipo(numero: str) -> Miembro | None:
    """
    ¿Este número es un MIEMBRO ACTIVO del equipo (recibe escalaciones, manda
    instrucciones)? Devuelve el Miembro si sí, None si no.

    Distinto a `es_numero_interno`:
    - es_numero_interno → asesoras, bodegas, otros bots — el bot las IGNORA
    - es_miembro_equipo → Fabio, supervisores — el bot HABLA con ellos
    """
    _cargar_si_caducado()
    for m in _cache["miembros"]:
        if m.numero_whatsapp == numero:
            return m
    return None


def fabio_phone() -> str:
    """Compatibilidad: devuelve el número del fallback (hoy Fabio)."""
    m = superior_para()
    return m.numero_whatsapp if m else settings.fabio_phone


def todos_los_miembros() -> list[Miembro]:
    _cargar_si_caducado()
    return list(_cache["miembros"])


def config_escalacion() -> dict[str, Any]:
    """
    Devuelve config global de escalación. Hoy hardcoded; en el futuro podría
    venir de una tabla `config_bot`.
    """
    return {
        "enviar_mensaje_real": True,
        "prefijo_mensajes_fabio": "[BOT ASISTENTE]",
        "reescalacion_tras_minutos": 60,
    }
