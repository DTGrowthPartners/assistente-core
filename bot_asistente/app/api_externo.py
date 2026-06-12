"""API externa para sistemas terceros (panel admin de bots, monitor Meta, etc).

Todos los endpoints van bajo `/api/externo/*` y requieren header
`X-API-Key: <API_EXTERNO_KEY>` (se configura en .env).

ENDPOINTS DISPONIBLES
=====================

POST /api/externo/enviar          → enviar mensaje WhatsApp a un destino
GET  /api/externo/estado          → estado actual del bot (activo, modo, stats rápidas)
POST /api/externo/estado          → cambiar estado (toggle, modo, razón)
GET  /api/externo/stats           → estadísticas detalladas del día/mes
GET  /api/externo/health          → healthcheck simple (sin auth)

Documentación completa: docs/API_EXTERNO.md
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Header, HTTPException, Request
from sqlalchemy import text as sa_text

from app.config import get_settings
from app.db.session import async_session_factory
from app.logging_setup import log
from app.whapi.client import WhapiError, enviar_texto

router = APIRouter(prefix="/api/externo", tags=["api-externo"])
settings = get_settings()


# ─── Helpers ────────────────────────────────────────────────────────────────


def _check_auth(x_api_key: str | None, request: Request) -> None:
    """Valida X-API-Key. Lanza HTTPException si falla."""
    api_key_esperada = (settings.api_externo_key or "").strip()
    if not api_key_esperada:
        raise HTTPException(503, "API_EXTERNO_KEY no configurada")
    if (x_api_key or "").strip() != api_key_esperada:
        log.warning(
            "api_externo.unauthorized",
            cliente_ip=request.client.host if request.client else "?",
            path=request.url.path,
        )
        raise HTTPException(401, "unauthorized")


def _destino_valido(dest: str) -> bool:
    if not dest:
        return False
    if dest.endswith("@g.us"):
        return len(dest) > 6
    if dest.endswith("@s.whatsapp.net"):
        return len(dest) > 16
    d = dest.lstrip("+")
    return d.isdigit() and 7 <= len(d) <= 15


def _now_bogota() -> datetime:
    return datetime.now(ZoneInfo(settings.tz))


# ─── /health (sin auth) ─────────────────────────────────────────────────────


@router.get("/health")
async def health():
    """Healthcheck público. Útil para uptime checks externos."""
    return {
        "ok": True,
        "service": "bot-dairo",
        "env": settings.bot_env,
        "ts": _now_bogota().isoformat(),
    }


# ─── /enviar ────────────────────────────────────────────────────────────────


@router.post("/enviar")
async def enviar_mensaje_externo(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Envía un mensaje WhatsApp a un destino (número o grupo)."""
    _check_auth(x_api_key, request)

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")

    destino = (body.get("destino") or "").strip()
    mensaje = (body.get("mensaje") or "").strip()
    origen = (body.get("origen") or "externo").strip()[:60]

    if not _destino_valido(destino):
        raise HTTPException(400, f"destino inválido: {destino!r}")
    if not mensaje:
        raise HTTPException(400, "mensaje vacío")
    if len(mensaje) > 4000:
        raise HTTPException(400, "mensaje > 4000 chars")

    try:
        res = await enviar_texto(destino, mensaje)
    except WhapiError as e:
        log.error("api_externo.whapi_fail", origen=origen, destino=destino,
                  error=str(e)[:200])
        raise HTTPException(502, f"whapi: {str(e)[:200]}")
    except Exception as e:
        log.exception("api_externo.exception", origen=origen, destino=destino)
        raise HTTPException(500, f"interno: {str(e)[:200]}")

    whapi_id = None
    try:
        sent = (res or {}).get("sent") or {}
        if isinstance(sent, list) and sent:
            whapi_id = sent[0].get("id")
        elif isinstance(sent, dict):
            whapi_id = sent.get("id")
        whapi_id = whapi_id or (res or {}).get("message", {}).get("id")
    except Exception:
        pass

    log.info("api_externo.enviado", origen=origen, destino=destino,
             chars=len(mensaje), whapi_id=whapi_id)
    return {"ok": True, "whapi_id": whapi_id, "destino": destino}


# ─── /estado (GET + POST) ───────────────────────────────────────────────────


async def _leer_estado_db() -> dict[str, Any]:
    """Lee bot_estado.activo, modo, razón, etc."""
    async with async_session_factory() as session:
        row = (await session.execute(sa_text("""
            SELECT activo, COALESCE(modo,'todos') AS modo,
                   pausado_por, pausado_en, razon, actualizado_en
              FROM bot_estado WHERE id = 1
        """))).first()
    if not row:
        return {"activo": True, "modo": "todos", "pausado_por": None,
                "pausado_en": None, "razon": None, "actualizado_en": None}
    return {
        "activo": bool(row.activo),
        "modo": row.modo,
        "pausado_por": row.pausado_por,
        "pausado_en": row.pausado_en.isoformat() if row.pausado_en else None,
        "razon": row.razon,
        "actualizado_en": row.actualizado_en.isoformat() if row.actualizado_en else None,
    }


@router.get("/estado")
async def get_estado(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Estado actual del bot + stats rápidas (sin pegarle fuerte a la BD)."""
    _check_auth(x_api_key, request)
    estado = await _leer_estado_db()
    async with async_session_factory() as session:
        # Mensajes entrantes y salientes en las últimas 24h
        row = (await session.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE direccion='inbound')  AS inbound_24h,
              COUNT(*) FILTER (WHERE direccion='outbound') AS outbound_24h
            FROM conversaciones
            WHERE timestamp > now() - interval '24 hours'
        """))).first()
    return {
        "ok": True,
        "ts": _now_bogota().isoformat(),
        "estado": estado,
        "trafico_24h": {
            "inbound":  int(row.inbound_24h) if row else 0,
            "outbound": int(row.outbound_24h) if row else 0,
        },
    }


@router.post("/estado")
async def set_estado(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Cambia el estado del bot.

    Body acepta:
      { "activo": true|false }                          ← simple on/off
      { "modo": "todos"|"solo_prospectos"|"off" }       ← cambio de modo
      { "activo": ..., "modo": ..., "razon": "...", "por": "..." }

    'modo=off' implica activo=false.
    'modo=todos' implica activo=true.
    """
    _check_auth(x_api_key, request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON inválido")

    modo_nuevo = body.get("modo")
    activo_nuevo = body.get("activo")
    razon = (body.get("razon") or "Cambio vía API externa").strip()[:240]
    por = (body.get("por") or "api_externo").strip()[:80]

    if modo_nuevo is not None and modo_nuevo not in ("todos", "solo_prospectos", "off"):
        raise HTTPException(400, "modo inválido (usar: todos | solo_prospectos | off)")

    # Resolver el destino final
    if modo_nuevo == "off":
        activo_final, modo_final = False, "off"
    elif modo_nuevo == "todos":
        activo_final, modo_final = True, "todos"
    elif modo_nuevo == "solo_prospectos":
        activo_final = True if activo_nuevo is None else bool(activo_nuevo)
        modo_final = "solo_prospectos"
    else:
        # Solo cambio de `activo`
        if activo_nuevo is None:
            raise HTTPException(400, "Debes enviar `activo` o `modo`")
        activo_final = bool(activo_nuevo)
        # Leer modo actual para no pisarlo
        estado_actual = await _leer_estado_db()
        modo_final = estado_actual.get("modo", "todos")

    async with async_session_factory() as session:
        await session.execute(sa_text("""
            UPDATE bot_estado SET
              activo         = :a,
              modo           = :m,
              pausado_por    = CASE WHEN :a THEN NULL ELSE :p END,
              pausado_en     = CASE WHEN :a THEN NULL ELSE now() END,
              razon          = CASE WHEN :a THEN NULL ELSE :r END,
              actualizado_en = now()
            WHERE id = 1
        """), {"a": activo_final, "m": modo_final, "p": por, "r": razon})
        await session.commit()

    # Invalidar cache para que el cambio surta efecto YA
    try:
        from app.main import invalidar_bot_estado_cache
        invalidar_bot_estado_cache()
    except Exception:
        pass

    log.warning("api_externo.estado_cambiado",
                activo=activo_final, modo=modo_final, por=por, razon=razon)

    # Notificar a la plataforma admin (no bloqueante)
    import asyncio as _asyncio
    from app.panel_admin_webhook import emitir_evento as _emit
    _asyncio.create_task(_emit("bot.estado_cambiado", {
        "activo": activo_final, "modo": modo_final, "por": por, "razon": razon,
    }))

    return {
        "ok": True,
        "estado": await _leer_estado_db(),
    }


# ─── /stats ─────────────────────────────────────────────────────────────────


@router.get("/stats")
async def get_stats(
    request: Request,
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
):
    """Stats agregadas para mostrar en el panel externo.

    Incluye conteos de hoy y del mes en curso (zona Bogotá).
    """
    _check_auth(x_api_key, request)

    tz = ZoneInfo(settings.tz)
    ahora = datetime.now(tz)
    hoy_00 = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    mes_00 = hoy_00.replace(day=1)

    async with async_session_factory() as session:
        # Conversaciones (in/out) hoy y mes
        conv = (await session.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE direccion='inbound'  AND timestamp >= :hoy) AS inbound_hoy,
              COUNT(*) FILTER (WHERE direccion='outbound' AND timestamp >= :hoy) AS outbound_hoy,
              COUNT(*) FILTER (WHERE direccion='inbound'  AND timestamp >= :mes) AS inbound_mes,
              COUNT(*) FILTER (WHERE direccion='outbound' AND timestamp >= :mes) AS outbound_mes,
              COUNT(DISTINCT cliente_id) FILTER (WHERE timestamp >= :hoy) AS clientes_unicos_hoy,
              COUNT(DISTINCT cliente_id) FILTER (WHERE timestamp >= :mes) AS clientes_unicos_mes
            FROM conversaciones
        """), {"hoy": hoy_00, "mes": mes_00})).first()

        # Prospectos: total, agendados, no_fit, en seguimiento
        pros = (await session.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE estado = 'nuevo')          AS prospectos_nuevos,
              COUNT(*) FILTER (WHERE estado = 'calificando')    AS prospectos_calificando,
              COUNT(*) FILTER (WHERE estado = 'agendado')       AS prospectos_agendados,
              COUNT(*) FILTER (WHERE estado = 'no_fit')         AS prospectos_no_fit,
              COUNT(*) FILTER (WHERE estado = 'cliente')        AS prospectos_clientes,
              COUNT(*)                                          AS prospectos_total
            FROM prospectos
        """))).first()

        # Citas: agendadas, completadas (hoy, mes), totales
        citas = (await session.execute(sa_text("""
            SELECT
              COUNT(*) FILTER (WHERE estado IN ('agendada','reprogramada')) AS citas_activas,
              COUNT(*) FILTER (WHERE estado = 'completada')                 AS citas_completadas,
              COUNT(*) FILTER (WHERE estado = 'cancelada')                  AS citas_canceladas,
              COUNT(*) FILTER (WHERE fecha_inicio >= :hoy)                  AS citas_hoy,
              COUNT(*) FILTER (WHERE fecha_inicio >= :mes)                  AS citas_mes,
              COUNT(*)                                                      AS citas_total
            FROM citas
        """), {"hoy": hoy_00, "mes": mes_00})).first()

        # Costos Claude del día (tokens + USD) — del campo metadata si existe
        try:
            costo_row = (await session.execute(sa_text("""
                SELECT
                  COALESCE(SUM(tokens_input),  0) AS tokens_in,
                  COALESCE(SUM(tokens_output), 0) AS tokens_out,
                  COALESCE(SUM(cache_read_tokens), 0) AS cache_read,
                  COALESCE(SUM(cache_create_tokens), 0) AS cache_write
                FROM conversaciones
                WHERE timestamp >= :hoy
                  AND direccion = 'outbound'
            """), {"hoy": hoy_00})).first()
        except Exception:
            costo_row = None

        # Alertas abiertas (problemas sin resolver)
        try:
            alertas = (await session.execute(sa_text("""
                SELECT COUNT(*) AS abiertas
                FROM alertas_fabio
                WHERE estado = 'abierta'
            """))).first()
            alertas_abiertas = int(alertas.abiertas) if alertas else 0
        except Exception:
            alertas_abiertas = 0

    estado = await _leer_estado_db()

    return {
        "ok": True,
        "ts": ahora.isoformat(),
        "tz": settings.tz,
        "estado": estado,
        "conversaciones": {
            "inbound_hoy":  int(conv.inbound_hoy)  if conv else 0,
            "outbound_hoy": int(conv.outbound_hoy) if conv else 0,
            "inbound_mes":  int(conv.inbound_mes)  if conv else 0,
            "outbound_mes": int(conv.outbound_mes) if conv else 0,
            "clientes_unicos_hoy": int(conv.clientes_unicos_hoy) if conv else 0,
            "clientes_unicos_mes": int(conv.clientes_unicos_mes) if conv else 0,
        },
        "prospectos": {
            "nuevos":      int(pros.prospectos_nuevos)       if pros else 0,
            "calificando": int(pros.prospectos_calificando)  if pros else 0,
            "agendados":   int(pros.prospectos_agendados)    if pros else 0,
            "no_fit":      int(pros.prospectos_no_fit)       if pros else 0,
            "clientes":    int(pros.prospectos_clientes)     if pros else 0,
            "total":       int(pros.prospectos_total)        if pros else 0,
        },
        "citas": {
            "activas":      int(citas.citas_activas)     if citas else 0,
            "completadas":  int(citas.citas_completadas) if citas else 0,
            "canceladas":   int(citas.citas_canceladas)  if citas else 0,
            "hoy":          int(citas.citas_hoy)         if citas else 0,
            "mes":          int(citas.citas_mes)         if citas else 0,
            "total":        int(citas.citas_total)       if citas else 0,
        },
        "claude_hoy": {
            "tokens_input":  int(costo_row.tokens_in)  if costo_row else 0,
            "tokens_output": int(costo_row.tokens_out) if costo_row else 0,
            "cache_read":    int(costo_row.cache_read) if costo_row else 0,
            "cache_write":   int(costo_row.cache_write) if costo_row else 0,
        },
        "alertas_abiertas": alertas_abiertas,
    }
