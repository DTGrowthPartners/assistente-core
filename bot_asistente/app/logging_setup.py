"""Configura structlog para JSON estructurado en producción, pretty en dev."""

from __future__ import annotations

import logging
import sys

import structlog

from app.config import get_settings


def setup_logging() -> None:
    settings = get_settings()
    level = getattr(logging, settings.bot_log_level.upper(), logging.INFO)

    procesadores_compartidos: list = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    if settings.is_production:
        # Producción: JSON estructurado a stdout (systemd captura)
        renderer = structlog.processors.JSONRenderer()
        procesadores = procesadores_compartidos + [renderer]
    else:
        # Dev: legible humano
        renderer = structlog.dev.ConsoleRenderer(colors=True)
        procesadores = procesadores_compartidos + [renderer]

    structlog.configure(
        processors=procesadores,
        wrapper_class=structlog.make_filtering_bound_logger(level),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )

    # Domar logs de librerías que usan logging stdlib (uvicorn, sqlalchemy, anthropic)
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=level,
    )
    for noisy in ("uvicorn.access", "httpx", "httpcore", "sqlalchemy.engine"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


log = structlog.get_logger("asistente")
