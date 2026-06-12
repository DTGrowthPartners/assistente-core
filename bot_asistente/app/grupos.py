"""Gestión de grupos de WhatsApp donde está el bot (Dairo).

- `refrescar_grupos`: consulta whapi y sincroniza la tabla `grupos_whatsapp`.
- `listar_grupos_activos`: para crons que quieren mandar a grupos opt-in.
- `enviar_a_grupo_*`: wrappers simples sobre whapi/client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.logging_setup import log
from app.whapi.client import enviar_texto, enviar_imagen_bytes, _headers

settings = get_settings()


# ─── Refresh desde whapi ──────────────────────────────────────────────────


async def refrescar_grupos(session: AsyncSession) -> dict[str, Any]:
    """Llama whapi GET /groups y upserta la tabla local.

    Devuelve estadísticas: {ok, total_whapi, creados, actualizados}.
    Mantiene el flag `activo` y `tags` si ya existen (no los sobrescribe).
    """
    url = f"{settings.whapi_base_url}/groups"
    try:
        async with httpx.AsyncClient(timeout=30) as c:
            r = await c.get(url, headers=_headers(), params={"count": 500})
        if r.status_code >= 400:
            log.warning("grupos.refresh_fail", status=r.status_code, body=r.text[:200])
            return {"ok": False, "error": f"HTTP {r.status_code}"}
        payload = r.json() or {}
        grupos = payload.get("groups") or []
    except Exception as e:
        log.exception("grupos.refresh_exc", error=str(e))
        return {"ok": False, "error": str(e)[:200]}

    creados = 0
    actualizados = 0
    ahora = datetime.now(timezone.utc)
    for g in grupos:
        gid = g.get("id")
        if not gid:
            continue
        nombre = g.get("name") or g.get("subject") or ""
        descripcion = (g.get("description") or "")[:1000]
        participantes = g.get("participants") or []
        n_part = len(participantes)
        soy_admin = any(
            (p.get("rank") in ("admin", "creator")) and p.get("is_me")
            for p in participantes
            if isinstance(p, dict)
        )
        res = await session.execute(sa_text("""
            INSERT INTO grupos_whatsapp (group_id, nombre, descripcion,
                                          participantes_count, soy_admin,
                                          refrescado_en, updated_at)
            VALUES (:gid, :nom, :desc, :pc, :adm, :now, :now)
            ON CONFLICT (group_id) DO UPDATE SET
              nombre = EXCLUDED.nombre,
              descripcion = EXCLUDED.descripcion,
              participantes_count = EXCLUDED.participantes_count,
              soy_admin = EXCLUDED.soy_admin,
              refrescado_en = EXCLUDED.refrescado_en,
              updated_at = EXCLUDED.updated_at
            RETURNING (xmax = 0) AS inserted
        """), {"gid": gid, "nom": nombre, "desc": descripcion,
               "pc": n_part, "adm": soy_admin, "now": ahora})
        row = res.first()
        if row and row[0]:
            creados += 1
        else:
            actualizados += 1

    log.info("grupos.refreshed", total=len(grupos), creados=creados, actualizados=actualizados)
    return {"ok": True, "total_whapi": len(grupos), "creados": creados, "actualizados": actualizados}


# ─── Helpers para crons / scripts ─────────────────────────────────────────


async def listar_grupos_activos(
    session: AsyncSession, tag: str | None = None
) -> list[dict[str, Any]]:
    """Devuelve grupos con activo=true. Filtrable por tag (LIKE)."""
    sql = "SELECT group_id, nombre, tags, participantes_count FROM grupos_whatsapp WHERE activo = TRUE"
    params: dict[str, Any] = {}
    if tag:
        sql += " AND (tags ILIKE :tag)"
        params["tag"] = f"%{tag}%"
    sql += " ORDER BY nombre"
    rows = (await session.execute(sa_text(sql), params)).fetchall()
    return [
        {"group_id": r[0], "nombre": r[1], "tags": r[2], "participantes": r[3]}
        for r in rows
    ]


async def buscar_grupo_por_nombre(
    session: AsyncSession, query: str
) -> dict[str, Any] | None:
    """Busca un grupo por nombre (ILIKE). Devuelve el primero o None."""
    row = (await session.execute(sa_text(
        "SELECT group_id, nombre FROM grupos_whatsapp "
        "WHERE nombre ILIKE :q AND activo = TRUE ORDER BY participantes_count DESC LIMIT 1"
    ), {"q": f"%{query}%"})).first()
    if not row:
        return None
    return {"group_id": row[0], "nombre": row[1]}


# ─── Envío a grupo ────────────────────────────────────────────────────────


async def enviar_texto_a_grupo(group_id: str, texto: str) -> dict[str, Any]:
    """Envía mensaje de texto al group_id (ej: '120363...@g.us')."""
    if not group_id.endswith("@g.us"):
        group_id = group_id + "@g.us" if "@" not in group_id else group_id
    return await enviar_texto(group_id, texto)


async def enviar_imagen_a_grupo(
    group_id: str, data: bytes, *,
    mime: str = "image/jpeg", caption: str | None = None,
    filename: str = "image.jpg",
) -> dict[str, Any]:
    """Envía una imagen al grupo."""
    if not group_id.endswith("@g.us"):
        group_id = group_id + "@g.us" if "@" not in group_id else group_id
    return await enviar_imagen_bytes(
        group_id, data, mime=mime, caption=caption, filename=filename
    )
