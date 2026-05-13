"""
Humanización del comportamiento del bot — anti-detección de WhatsApp.

WhatsApp detecta cuentas automatizadas por:
  - Respuestas instantáneas (< 1s) → patrón no humano
  - Volumen alto en poco tiempo → patrón de spam
  - Actividad 24/7 → patrón de bot
  - Mismo formato de respuesta siempre

Este módulo implementa:
  1. Delays variables antes de responder (simula "tiempo de escribir")
  2. Typing indicator vía whapi presence API
  3. Hard rate limit (max N outbound por hora — global)
  4. Ventana horaria (no responder fuera de 8AM-10PM Bogotá)
"""

from __future__ import annotations

import asyncio
import random
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db.models import Conversacion
from app.logging_setup import log

settings = get_settings()

TZ_BOGOTA = ZoneInfo("America/Bogota")


# ────────────────────────────────────────────────────────────────────────────
# Ventana horaria
# ────────────────────────────────────────────────────────────────────────────


def hora_bogota() -> datetime:
    return datetime.now(TZ_BOGOTA)


def dentro_horario() -> bool:
    """¿Estamos en la ventana laboral (8AM-10PM Bogotá)?"""
    h = hora_bogota().hour
    return settings.horario_inicio_hora <= h < settings.horario_fin_hora


def proxima_hora_apertura() -> datetime:
    """Devuelve el próximo 8AM de Bogotá (hoy o mañana según hora actual)."""
    ahora = hora_bogota()
    apertura_hoy = ahora.replace(
        hour=settings.horario_inicio_hora, minute=0, second=0, microsecond=0
    )
    if ahora < apertura_hoy:
        return apertura_hoy
    # Si pasamos la hora de hoy, mañana
    return apertura_hoy + timedelta(days=1)


# ────────────────────────────────────────────────────────────────────────────
# Delay humano antes de enviar
# ────────────────────────────────────────────────────────────────────────────


def _delay_para(longitud: int) -> float:
    """
    Tiempo realista para 'escribir' un mensaje de N caracteres.

    Un humano escribe ~40-60 caracteres por segundo, pero también piensa
    antes de responder. Combinamos:
      - base random uniform [MIN, MIN+5]
      - + 0.04s por carácter (~25 cps, conservador)
      - + ruido aleatorio
      - clamp a [MIN, MAX]
    """
    base = random.uniform(settings.humanization_delay_min_s,
                          settings.humanization_delay_min_s + 5)
    por_chars = longitud * settings.humanization_delay_por_caracter_s
    ruido = random.uniform(-1.5, 2.5)
    total = base + por_chars + ruido
    return max(settings.humanization_delay_min_s,
               min(settings.humanization_delay_max_s, total))


async def sleep_humano(texto_a_enviar: str) -> float:
    """Espera un tiempo proporcional al largo del mensaje. Devuelve segundos esperados."""
    segundos = _delay_para(len(texto_a_enviar or ""))
    log.debug("humanizer.sleep", segundos=round(segundos, 2), chars=len(texto_a_enviar or ""))
    await asyncio.sleep(segundos)
    return segundos


# ────────────────────────────────────────────────────────────────────────────
# Rate limiting outbound (hard cap global por hora)
# ────────────────────────────────────────────────────────────────────────────


async def outbound_en_ultima_hora(session: AsyncSession) -> int:
    """Cuenta cuántos outbound del bot enviamos en los últimos 60 minutos."""
    desde = datetime.now(timezone.utc) - timedelta(hours=1)
    stmt = (
        select(func.count(Conversacion.id))
        .where(Conversacion.direccion == "outbound")
        .where(Conversacion.timestamp > desde)
    )
    return (await session.execute(stmt)).scalar_one()


async def puede_enviar(session: AsyncSession) -> tuple[bool, int, int]:
    """
    Devuelve (puede_enviar, enviados_ultima_hora, limite).

    Si no puede enviar, el flow debe decidir: esperar y reintentar, o silenciar.
    """
    enviados = await outbound_en_ultima_hora(session)
    limite = settings.rate_limit_outbound_por_hora
    return (enviados < limite, enviados, limite)


# ────────────────────────────────────────────────────────────────────────────
# Resumen del estado de humanización (para logs/dashboards)
# ────────────────────────────────────────────────────────────────────────────


async def estado(session: AsyncSession) -> dict:
    enviados, limite = (await outbound_en_ultima_hora(session)), settings.rate_limit_outbound_por_hora
    return {
        "hora_bogota": hora_bogota().strftime("%Y-%m-%d %H:%M:%S"),
        "dentro_horario": dentro_horario(),
        "outbound_ultima_hora": enviados,
        "limite_por_hora": limite,
        "capacidad_libre": max(0, limite - enviados),
    }
