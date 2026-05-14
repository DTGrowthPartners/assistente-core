"""
Directorio del equipo interno — lee data/equipo.yaml en runtime.

Permite agregar/quitar superiores y números internos editando el YAML, sin
tocar código ni reiniciar el servicio.

API:
    superior_para(area: str) -> Miembro | None
    es_numero_interno(numero: str) -> bool
    fabio_phone() -> str
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml  # type: ignore

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()


@dataclass(frozen=True)
class Miembro:
    nombre: str
    numero_whatsapp: str
    rol: str
    areas: tuple[str, ...]
    es_fallback: bool
    activo: bool
    notas: str | None = None


# ────────────────────────────────────────────────────────────────────────────
# Cache de archivo con mtime check (no recargamos si no cambió)
# ────────────────────────────────────────────────────────────────────────────

_cache: dict[str, Any] = {"mtime": 0, "miembros": [], "numeros_internos": set(), "config": {}}


def _ruta_yaml() -> Path:
    """Ruta al archivo equipo.yaml. Permite override por env var EQUIPO_YAML."""
    custom = os.environ.get("EQUIPO_YAML")
    if custom:
        return Path(custom)
    # Default: <data_dir>/equipo.yaml. En VPS: /home/asistente/data/equipo.yaml
    return Path(settings.data_dir) / "equipo.yaml"


def _cargar_si_cambio() -> None:
    """Recarga el YAML si su mtime cambió desde el último load."""
    path = _ruta_yaml()
    if not path.exists():
        if not _cache["miembros"]:  # primera vez y no existe → log warning
            log.warning("equipo.yaml_no_existe", path=str(path))
        return

    mtime = path.stat().st_mtime
    if mtime <= _cache["mtime"]:
        return

    try:
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception as e:
        log.error("equipo.yaml_parse_fail", error=str(e))
        return

    miembros_raw = data.get("miembros") or []
    miembros: list[Miembro] = []
    for m in miembros_raw:
        if not m.get("activo", True):
            continue
        miembros.append(Miembro(
            nombre=m.get("nombre", "?"),
            numero_whatsapp=m.get("numero_whatsapp", ""),
            rol=m.get("rol", ""),
            areas=tuple(m.get("areas") or []),
            es_fallback=bool(m.get("es_fallback", False)),
            activo=True,
            notas=m.get("notas"),
        ))

    nums_internos: set[str] = set()
    for entry in (data.get("numeros_internos") or []):
        num = entry.get("numero")
        if num:
            nums_internos.add(num)

    _cache["mtime"] = mtime
    _cache["miembros"] = miembros
    _cache["numeros_internos"] = nums_internos
    _cache["config"] = data.get("config") or {}
    log.info("equipo.recargado", miembros=len(miembros), numeros_internos=len(nums_internos))


# ────────────────────────────────────────────────────────────────────────────
# API pública
# ────────────────────────────────────────────────────────────────────────────


def superior_para(area: str | None = None) -> Miembro | None:
    """Devuelve el miembro responsable de un área. Cae al fallback si no encaja."""
    _cargar_si_cambio()
    miembros: list[Miembro] = _cache["miembros"]
    if not miembros:
        return None

    # Match exacto por área
    if area:
        for m in miembros:
            if area in m.areas:
                return m

    # Fallback
    for m in miembros:
        if m.es_fallback:
            return m

    # Último recurso: primer miembro activo
    return miembros[0] if miembros else None


def es_numero_interno(numero: str) -> bool:
    """¿Este número pertenece al equipo interno (no es cliente)?"""
    _cargar_si_cambio()
    return numero in _cache["numeros_internos"]


def fabio_phone() -> str:
    """Compatibilidad: devuelve el número del fallback (hoy Fabio)."""
    m = superior_para()
    return m.numero_whatsapp if m else settings.fabio_phone


def todos_los_miembros() -> list[Miembro]:
    _cargar_si_cambio()
    return list(_cache["miembros"])


def config_escalacion() -> dict[str, Any]:
    _cargar_si_cambio()
    return dict(_cache["config"])
