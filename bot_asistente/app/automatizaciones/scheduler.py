"""Scheduler async para tareas_programadas.

Cada CHECK_INTERVAL_SEC (60s):
1. Consulta tareas con activo=true AND proxima_ejecucion <= now()
2. Por cada una: ejecuta la acción (con timeout) y registra resultado
3. Recalcula proxima_ejecucion con croniter sobre la zona horaria

Se inicia en lifespan de FastAPI y se detiene al apagar.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

try:
    from croniter import croniter
    from zoneinfo import ZoneInfo
    _HAS_CRONITER = True
except ImportError:
    _HAS_CRONITER = False

from sqlalchemy import text as sa_text

from app.automatizaciones.acciones import ejecutar_accion
from app.db.session import async_session_factory
from app.logging_setup import log


CHECK_INTERVAL_SEC = 60
TASK_TIMEOUT_SEC = 90

_scheduler_task: asyncio.Task | None = None
_running = False


def calcular_proxima(cron_expr: str, zona: str, desde: datetime | None = None) -> datetime | None:
    """Calcula próxima ejecución de un cron en una zona horaria."""
    if not _HAS_CRONITER:
        log.error("scheduler.croniter_missing")
        return None
    try:
        tz = ZoneInfo(zona or "America/Bogota")
    except Exception:
        tz = ZoneInfo("America/Bogota")
    base = (desde or datetime.now(timezone.utc)).astimezone(tz)
    try:
        it = croniter(cron_expr, base)
        nxt_local = it.get_next(datetime)  # naive en tz local
        # Asegurar tz-aware
        if nxt_local.tzinfo is None:
            nxt_local = nxt_local.replace(tzinfo=tz)
        return nxt_local.astimezone(timezone.utc)
    except Exception as e:
        log.warning("scheduler.cron_invalid", cron=cron_expr, error=str(e))
        return None


async def _ejecutar_tarea(tarea_row: dict) -> None:
    """Ejecuta una tarea y actualiza la BD con el resultado.

    PRE-AVANCE DEFENSIVO: actualizamos `proxima_ejecucion` ANTES de ejecutar la
    acción. Así, aunque la acción falle o el UPDATE del resultado falle, la
    tarea NUNCA puede entrar en loop (su próxima fecha ya quedó adelantada).
    """
    tid = tarea_row["id"]
    accion = tarea_row["accion"]
    params = tarea_row["parametros"] or {}
    cron = tarea_row["cron"]
    zona = tarea_row["zona_horaria"]

    ahora_utc = datetime.now(timezone.utc)
    proxima = calcular_proxima(cron, zona, ahora_utc)

    # 1) Avanzar proxima_ejecucion ANTES de ejecutar (defensa anti-loop).
    #    Si calcular_proxima devolvió None (cron inválido), avanzamos +1h como
    #    salvavidas para que igual no se reentre cada minuto.
    proxima_safe = proxima or (ahora_utc + timedelta(hours=1))
    try:
        async with async_session_factory() as s_pre:
            await s_pre.execute(sa_text(
                "UPDATE tareas_programadas SET proxima_ejecucion = :p, "
                "ultima_ejecucion = :u, updated_at = now() WHERE id = :i"
            ), {"p": proxima_safe, "u": ahora_utc, "i": tid})
            await s_pre.commit()
    except Exception:
        log.exception("scheduler.tarea.pre_advance_fail", id=tid)
        # Si ni siquiera podemos avanzar la fecha, NO ejecutamos: sería loop seguro.
        return

    log.info("scheduler.tarea.start", id=tid, accion=accion)
    try:
        async with asyncio.timeout(TASK_TIMEOUT_SEC):
            async with async_session_factory() as session:
                resultado = await ejecutar_accion(accion, session, params)
                await session.commit()
    except asyncio.TimeoutError:
        resultado = {"ok": False, "error": f"timeout >{TASK_TIMEOUT_SEC}s"}
    except Exception as e:
        log.exception("scheduler.tarea.fail", id=tid, accion=accion, error=str(e))
        resultado = {"ok": False, "error": str(e)[:300]}

    # 2) Guardar el resultado (informativo, no crítico para evitar loops).
    try:
        async with async_session_factory() as s2:
            await s2.execute(sa_text(
                "UPDATE tareas_programadas SET ultimo_resultado = CAST(:r AS jsonb), "
                "updated_at = now() WHERE id = :i"
            ), {"r": _jsonable(resultado), "i": tid})
            await s2.commit()
    except Exception:
        log.exception("scheduler.tarea.update_fail", id=tid)

    ok = resultado.get("ok", False)
    log.info("scheduler.tarea.done", id=tid, accion=accion, ok=ok, proxima=str(proxima_safe))


def _jsonable(obj: Any) -> str:
    import json
    try:
        return json.dumps(obj, default=str)
    except Exception:
        return json.dumps({"ok": False, "error": "no serializable"})


async def _loop() -> None:
    global _running
    _running = True
    log.info("scheduler.iniciado", interval_sec=CHECK_INTERVAL_SEC)
    # Backfill: recalcular proxima_ejecucion para tareas activas sin ella
    try:
        async with async_session_factory() as s:
            rows = (await s.execute(sa_text(
                "SELECT id, cron, zona_horaria FROM tareas_programadas "
                "WHERE activo = true AND proxima_ejecucion IS NULL"
            ))).fetchall()
            for r in rows:
                p = calcular_proxima(r[1], r[2])
                if p:
                    await s.execute(sa_text(
                        "UPDATE tareas_programadas SET proxima_ejecucion=:p WHERE id=:i"
                    ), {"p": p, "i": r[0]})
            await s.commit()
    except Exception:
        log.exception("scheduler.backfill_fail")

    while _running:
        try:
            await _tick()
        except Exception:
            log.exception("scheduler.tick_fail")
        # Sleep hasta el próximo check (interrumpible si se detiene)
        for _ in range(CHECK_INTERVAL_SEC):
            if not _running:
                break
            await asyncio.sleep(1)


async def _tick() -> None:
    async with async_session_factory() as s:
        rows = (await s.execute(sa_text("""
            SELECT id, nombre, cron, zona_horaria, accion, parametros
            FROM tareas_programadas
            WHERE activo = true AND proxima_ejecucion <= now()
            ORDER BY proxima_ejecucion ASC
            LIMIT 10
        """))).fetchall()

    if not rows:
        return

    for r in rows:
        tarea = {
            "id": r[0], "nombre": r[1], "cron": r[2],
            "zona_horaria": r[3], "accion": r[4], "parametros": r[5],
        }
        # Ejecutar en background (no bloquea el tick) pero secuencial para
        # no exceder rate limit whapi.
        await _ejecutar_tarea(tarea)


def iniciar_scheduler() -> None:
    """Llamar desde lifespan al arrancar la app."""
    global _scheduler_task
    if _scheduler_task and not _scheduler_task.done():
        log.warning("scheduler.ya_iniciado")
        return
    if not _HAS_CRONITER:
        log.error("scheduler.no_iniciado_sin_croniter")
        return
    _scheduler_task = asyncio.create_task(_loop())


async def detener_scheduler() -> None:
    """Llamar desde lifespan al apagar."""
    global _running, _scheduler_task
    _running = False
    if _scheduler_task:
        try:
            await asyncio.wait_for(_scheduler_task, timeout=3)
        except asyncio.TimeoutError:
            _scheduler_task.cancel()
    log.info("scheduler.detenido")
