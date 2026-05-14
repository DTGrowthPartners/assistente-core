"""Entry point — FastAPI + webhook + health check."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession
from sqladmin import Admin
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware

from app.admin.actions import router as actions_router
from app.admin.auth import AdminAuth
from app.admin.dashboard import router as dashboard_router
from app.admin.views import ALL_VIEWS
from app.config import get_settings
from app.db.repos import (
    bot_pausado,
    cliente_esta_bloqueado,
    get_or_create_cliente,
    guardar_conversacion,
    marcar_procesado,
    pausar_bot,
    registrar_alerta_fabio,
    ya_procesado,
)
from app.db.session import async_session_factory, engine, get_session
from app.equipo.directorio import es_miembro_equipo, es_numero_interno
from app.flows.conversation import procesar_mensaje_inbound
from app.flows.equipo import procesar_mensaje_equipo
from app.logging_setup import log, setup_logging
from app.whapi.parser import MensajeWhapi, parsear_payload

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    log.info(
        "asistente.startup",
        env=settings.bot_env,
        port=settings.bot_port,
        model=settings.claude_model_principal,
    )
    yield
    await engine.dispose()
    log.info("asistente.shutdown")


app = FastAPI(
    title="Bot Asistente — Innovación Fashion Outlet",
    version="0.1.0",
    lifespan=lifespan,
)

# ─── Admin panel ────────────────────────────────────────────────────────────
# Sessions middleware (necesario para SQLAdmin auth)
app.add_middleware(SessionMiddleware, secret_key=settings.admin_session_secret)

# Static custom para el admin (CSS Tabler-style del diseno.md)
_admin_dir = Path(__file__).parent / "admin"
app.mount(
    "/admin-static",
    StaticFiles(directory=str(_admin_dir / "static")),
    name="admin_static",
)

# Dashboard custom + acciones admin (deben registrarse antes de SQLAdmin)
app.include_router(dashboard_router)
app.include_router(actions_router)

# SQLAdmin: CRUD automático sobre todos los modelos
admin = Admin(
    app,
    engine,
    title="Asistente — Admin",
    authentication_backend=AdminAuth(secret_key=settings.admin_session_secret),
    base_url="/admin",
)
for view in ALL_VIEWS:
    admin.add_view(view)


# Middleware que inyecta nuestro CSS y fuente Inter en cualquier HTML del admin.
# Más simple que sobreescribir templates de Jinja (que requiere conocer la
# herencia interna de SQLAdmin).
_ADMIN_CSS_INJECT = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">'
    '<link rel="stylesheet" href="/admin-static/custom.css">'
)


class AdminCSSInjector(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        response = await call_next(request)
        path = request.url.path
        if not path.startswith("/admin") or path.startswith("/admin-static"):
            return response
        ct = response.headers.get("content-type", "")
        if "text/html" not in ct:
            return response

        body = b""
        async for chunk in response.body_iterator:
            body += chunk
        try:
            text = body.decode("utf-8")
            text = text.replace("</head>", _ADMIN_CSS_INJECT + "</head>", 1)
            new_body = text.encode("utf-8")
        except Exception:
            new_body = body

        headers = {
            k: v for k, v in response.headers.items()
            if k.lower() not in ("content-length", "content-encoding")
        }
        return Response(
            content=new_body,
            status_code=response.status_code,
            headers=headers,
            media_type="text/html",
        )


app.add_middleware(AdminCSSInjector)


# ─── Health checks ──────────────────────────────────────────────────────────


@app.get("/")
@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "service": "asistente-bot", "env": settings.bot_env}


@app.get("/webhook")
async def webhook_get() -> dict[str, str]:
    """whapi puede hacer GET para validar la URL."""
    return {"status": "ready", "method": "GET", "note": "El webhook real recibe POST"}


# ─── Webhook principal ──────────────────────────────────────────────────────


@app.post("/webhook")
async def webhook(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """
    Recibe payloads de whapi. Hace dedupe + persistencia rápido (devuelve 200 a whapi
    en <1s) y procesa la respuesta en background para no bloquear el callback.
    """
    try:
        payload = await request.json()
    except Exception as e:
        log.warning("webhook.body_invalido", error=str(e))
        return {"status": "bad_request"}

    if "messages" not in payload:
        return {"status": "ignored", "reason": "no_messages_array"}

    mensajes = parsear_payload(payload)
    if not mensajes:
        return {"status": "ignored", "reason": "no_parseable_messages"}

    resultados: list[dict[str, Any]] = []
    para_procesar: list[tuple[int, str, MensajeWhapi]] = []

    for msg in mensajes:
        if await ya_procesado(session, msg.id):
            resultados.append({"id": msg.id, "status": "duplicate"})
            continue
        await marcar_procesado(session, msg.id)

        # 🚨 PRIORIDAD 1 — Outbound del PROPIO BOT (eco retransmitido por whapi).
        # Este check va ANTES de todo lo demás para evitar loops infinitos:
        # cuando el bot le responde a un miembro del equipo, whapi retransmite
        # ese outbound al webhook. Sin este check, el bot procesaría su propio
        # mensaje como nuevo inbound del equipo → respondería → loop.
        if msg.is_from_bot:
            log.debug("webhook.own_outbound_ignored", msg_id=msg.id)
            resultados.append({"id": msg.id, "status": "own_outbound_ignored"})
            continue

        # ¿Es un MIEMBRO del equipo (Fabio o supervisor)? → flow equipo
        miembro = es_miembro_equipo(msg.from_number)
        if miembro:
            log.info("webhook.inbound_equipo", miembro=miembro.nombre, from_=msg.from_number)
            resultados.append({"id": msg.id, "status": "team_routed", "miembro": miembro.nombre})
            # Procesar en background con su propia session
            asyncio.create_task(_procesar_equipo_async(miembro, msg))
            continue

        # Número interno NO-miembro (asesoras, bodegas) → ignorar silencioso
        if es_numero_interno(msg.from_number) and msg.from_number != settings.dueno_phone_blocked:
            log.info("webhook.numero_interno_ignorado", from_=msg.from_number)
            resultados.append({"id": msg.id, "status": "internal_team_ignored"})
            continue

        # Bloqueo del número del dueño (alerta a Fabio)
        if msg.from_number == settings.dueno_phone_blocked:
            log.warning("webhook.numero_bloqueado", from_=msg.from_number)
            await registrar_alerta_fabio(
                session,
                tipo="mensaje_dueno",
                mensaje=(
                    f"Llegó un mensaje del número del dueño ({msg.from_number}): "
                    f"{(msg.texto or '')[:200]}"
                ),
            )
            resultados.append({"id": msg.id, "status": "blocked_dueno"})
            continue

        # Outbound de asesora humana → pausar bot
        if msg.is_from_human:
            cliente = await get_or_create_cliente(session, msg.from_number)
            await pausar_bot(session, cliente.id, horas=4, razon="asesora humana intervino")
            await guardar_conversacion(
                session,
                cliente_id=cliente.id,
                direccion="humano",
                tipo=msg.tipo,
                contenido=msg.texto,
                whapi_message_id=msg.id,
                media_url=msg.media_url,
            )
            log.info("webhook.humano_interviene", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "human_paused_bot"})
            continue

        # Outbound del propio bot (eco)
        if msg.is_from_bot:
            resultados.append({"id": msg.id, "status": "own_outbound"})
            continue

        # Inbound del cliente
        if await cliente_esta_bloqueado(session, msg.from_number):
            resultados.append({"id": msg.id, "status": "blocked_client"})
            continue

        cliente = await get_or_create_cliente(session, msg.from_number)

        # ¿Bot pausado por intervención humana?
        if settings.feature_human_takeover and await bot_pausado(session, cliente.id):
            await guardar_conversacion(
                session,
                cliente_id=cliente.id,
                direccion="inbound",
                tipo=msg.tipo,
                contenido=msg.texto,
                whapi_message_id=msg.id,
                media_url=msg.media_url,
                metadata={"bot_pausado": True},
            )
            resultados.append({"id": msg.id, "status": "paused_human"})
            continue

        await guardar_conversacion(
            session,
            cliente_id=cliente.id,
            direccion="inbound",
            tipo=msg.tipo,
            contenido=msg.texto,
            whapi_message_id=msg.id,
            media_url=msg.media_url,
        )
        log.info(
            "webhook.inbound",
            cliente=msg.from_number,
            tipo=msg.tipo,
            preview=(msg.texto or "")[:80],
        )
        para_procesar.append((cliente.id, msg.from_number, msg))
        resultados.append({"id": msg.id, "status": "queued"})

    # 🔒 COMMIT EXPLÍCITO antes de spawn los tasks — sin esto, los background tasks
    # abren su propia session y no ven los clientes recién creados (FK violation).
    await session.commit()

    # Procesar fuera del request. asyncio.create_task corre en el mismo loop
    # y es más predecible que BackgroundTasks de FastAPI.
    for cliente_id, cliente_numero, msg in para_procesar:
        asyncio.create_task(_procesar_async(cliente_id, cliente_numero, msg))

    return {"status": "ok", "procesados": resultados}


async def _procesar_async(cliente_id: int, cliente_numero: str, msg: MensajeWhapi) -> None:
    """Procesa el mensaje fuera del request — abre su propia session DB."""
    async with async_session_factory() as session:
        try:
            await procesar_mensaje_inbound(
                session=session,
                cliente_id=cliente_id,
                cliente_numero=cliente_numero,
                msg=msg,
            )
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("background.flow_fail", cliente=cliente_numero)


async def _procesar_equipo_async(miembro, msg: MensajeWhapi) -> None:
    """Procesa mensaje de un miembro del equipo (Fabio, supervisor) en background."""
    async with async_session_factory() as session:
        try:
            await procesar_mensaje_equipo(session=session, miembro=miembro, msg=msg)
            await session.commit()
        except Exception:
            await session.rollback()
            log.exception("background.flow_equipo_fail", miembro=miembro.nombre)


# ─── Entry point local (dev) ────────────────────────────────────────────────


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "app.main:app",
        host=settings.bot_host,
        port=settings.bot_port,
        reload=settings.bot_env == "development",
    )
