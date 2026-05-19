"""publicar_story_diario.py — Cron: publica un producto random como estado WA.

Política:
- Toma productos ACTIVOS con imagen_url no nula y precio_detal definido.
- Excluye los que se publicaron como story en los últimos 14 días (no repetir).
- Caption: "{nombre} ({ref}) — $XX.XXX".
- Loggea cada publicación en `story_publicado` con origen='cron'.

Llamado por systemd timer `publicar-story.timer` (2 veces al día):
  10:00 y 16:00 hora Colombia (15:00 y 21:00 UTC).
"""

from __future__ import annotations

import asyncio
import random
import sys
from pathlib import Path

# Permitir importar app.* desde scripts/
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select
from sqlalchemy import text as sa_text

from app.db.models import ProductoCache
from app.db.session import async_session_factory
from app.logging_setup import log
from app.whapi.client import publicar_story_imagen_url


async def elegir_producto() -> ProductoCache | None:
    """Devuelve un producto random elegible, o None si no hay candidatos."""
    async with async_session_factory() as s:
        # Refs publicadas en los últimos 14 días (NO repetir)
        publicadas_recientes = (await s.execute(sa_text("""
            SELECT DISTINCT ref_producto FROM story_publicado
            WHERE ref_producto IS NOT NULL
              AND publicado_en > NOW() - INTERVAL '14 days'
        """))).fetchall()
        refs_recientes = {r[0] for r in publicadas_recientes}

        stmt = (
            select(ProductoCache)
            .where(ProductoCache.activo.is_(True))
            .where(ProductoCache.imagen_url.is_not(None))
            .where(ProductoCache.precio_detal.is_not(None))
        )
        if refs_recientes:
            stmt = stmt.where(ProductoCache.ref.notin_(list(refs_recientes)))
        candidatos = (await s.execute(stmt)).scalars().all()
        if not candidatos:
            return None
        return random.choice(candidatos)


async def registrar(
    *, tipo: str, caption: str, imagen_url: str, ref: str, msg_id: str | None
) -> None:
    async with async_session_factory() as s:
        await s.execute(sa_text("""
            CREATE TABLE IF NOT EXISTS story_publicado (
                id SERIAL PRIMARY KEY,
                tipo VARCHAR(20) NOT NULL,
                caption TEXT,
                imagen_url TEXT,
                ref_producto VARCHAR(40),
                whapi_message_id VARCHAR(120),
                publicado_por VARCHAR(60),
                publicado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
                origen VARCHAR(20) NOT NULL DEFAULT 'manual',
                metadata JSONB DEFAULT '{}'::jsonb
            )
        """))
        await s.execute(sa_text("""
            INSERT INTO story_publicado (tipo, caption, imagen_url, ref_producto,
                                         whapi_message_id, publicado_por, origen)
            VALUES (:tipo, :cap, :iurl, :ref, :mid, 'cron', 'cron')
        """), {"tipo": tipo, "cap": caption, "iurl": imagen_url, "ref": ref, "mid": msg_id})
        await s.commit()


async def main() -> int:
    prod = await elegir_producto()
    if not prod:
        log.warning("cron.story.sin_candidatos",
                    nota="todos los productos elegibles se publicaron en últimos 14 días")
        print("Sin candidatos — todos publicados en últimos 14 días.")
        return 0

    precio = f"${int(prod.precio_detal):,}".replace(",", ".")
    caption = f"{prod.nombre} ({prod.ref}) — {precio}"
    try:
        resp = await publicar_story_imagen_url(prod.imagen_url, caption=caption)
    except Exception as e:
        log.exception("cron.story.publicar_fail", ref=prod.ref, error=str(e))
        print(f"FAIL al publicar {prod.ref}: {e}")
        return 1
    msg_id = (resp.get("message") or {}).get("id")
    await registrar(
        tipo="imagen",
        caption=caption,
        imagen_url=prod.imagen_url,
        ref=prod.ref,
        msg_id=msg_id,
    )
    log.info("cron.story.publicada", ref=prod.ref, msg_id=msg_id)
    print(f"OK publicada: {prod.ref} — {caption} — msg_id={msg_id}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
