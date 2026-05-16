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
from app.admin.chats import router as chats_router
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
from app.whapi.client import enviar_imagen_bytes, enviar_texto
from app.whapi.parser import MensajeWhapi, parsear_payload
from sqlalchemy import update as sa_update
from app.db.models import AlertaFabio
from datetime import datetime, timezone

settings = get_settings()


# Set global de tareas de background (procesamiento de webhook fuera del
# request). Lo usamos en shutdown para esperar a que terminen las tareas
# en curso — sin esto, un restart cancela mensajes que están en mitad de
# la humanización (60-180s delay) y el cliente queda sin respuesta.
_background_tasks: "set[asyncio.Task]" = set()


def _track_task(task: "asyncio.Task") -> "asyncio.Task":
    """Agrega un task al set y lo limpia al terminar."""
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


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
    # Graceful shutdown: esperar a que los tasks de procesamiento en curso
    # terminen su humanización + envío antes de matar el proceso.
    # Timeout 200s = un poco más que el delay máximo de humanización (180s).
    if _background_tasks:
        log.info("asistente.shutdown.waiting_tasks", count=len(_background_tasks))
        try:
            await asyncio.wait_for(
                asyncio.gather(*_background_tasks, return_exceptions=True),
                timeout=200,
            )
            log.info("asistente.shutdown.tasks_done")
        except asyncio.TimeoutError:
            log.warning("asistente.shutdown.tasks_timeout", pendientes=len(_background_tasks))
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

# Dashboard custom + acciones admin + chats (deben registrarse antes de SQLAdmin)
app.include_router(dashboard_router)
app.include_router(actions_router)
app.include_router(chats_router)

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
    # JS: aplica tema dark/light al SQLAdmin + agrega header sticky con
    # navegación a Dashboard / Chats. Auto-redirect /admin/ → /admin/dashboard.
    '<script>'
    '(function(){'
    '  // Auto-redirect: si estamos en /admin exacto (home de SQLAdmin), ir al dashboard.'
    '  var p = location.pathname.replace(/\\/$/, "");'
    '  if (p === "/admin") { location.replace("/admin/dashboard"); return; }'
    '  // Aplicar tema desde localStorage ANTES de que renderice'
    '  try { var saved = localStorage.getItem("theme");'
    '        document.documentElement.setAttribute("data-theme", saved === "dark" ? "dark" : "light"); } catch(e) {}'
    '})();'
    'document.addEventListener("DOMContentLoaded", function() {'
    '  // Inyectar header sticky en todas las páginas SQLAdmin'
    '  if (document.getElementById("custom-admin-header")) return;'
    '  var saved = localStorage.getItem("theme") || "light";'
    '  var themeLabel = saved === "dark" ? "☀ Modo claro" : "🌙 Modo oscuro";'
    '  var header = document.createElement("div");'
    '  header.id = "custom-admin-header";'
    '  header.innerHTML = '
    '    \'<div class="cah-inner">\''
    '    + \'<a href="/admin/dashboard" class="cah-brand"><span class="cah-logo">L</span><span class="cah-name">Laura · Innovación</span></a>\''
    '    + \'<nav class="cah-nav">\''
    '      + \'<a href="/admin/dashboard">Dashboard</a>\''
    '      + \'<a href="/admin/chats">Chats</a>\''
    '      + \'<a href="/admin/cliente/list">Clientes</a>\''
    '      + \'<a href="/admin/pedido/list">Pedidos</a>\''
    '      + \'<a href="/admin/alerta-fabio/list">Alertas</a>\''
    '    + \'</nav>\''
    '    + \'<button id="cah-theme" class="cah-btn">\' + themeLabel + \'</button>\''
    '    + \'</div>\';'
    '  document.body.insertBefore(header, document.body.firstChild);'
    '  document.body.classList.add("with-custom-header");'
    '  document.getElementById("cah-theme").addEventListener("click", function() {'
    '    var cur = document.documentElement.getAttribute("data-theme") || "light";'
    '    var nxt = cur === "dark" ? "light" : "dark";'
    '    document.documentElement.setAttribute("data-theme", nxt);'
    '    localStorage.setItem("theme", nxt);'
    '    this.textContent = nxt === "dark" ? "☀ Modo claro" : "🌙 Modo oscuro";'
    '  });'
    '});'
    '</script>'
)


class AdminCSSInjector(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        # Redirect server-side: /admin o /admin/ → /admin/dashboard.
        # Si hay cookie 'session' (sessión activa), asumimos autenticado y
        # redirigimos. Si no, dejamos pasar para que SQLAdmin muestre login.
        # No accedemos request.session aquí porque SessionMiddleware todavía
        # no procesó la cookie (corre DESPUÉS).
        if path in ("/admin", "/admin/"):
            if request.cookies.get("session"):
                from starlette.responses import RedirectResponse
                return RedirectResponse(url="/admin/dashboard", status_code=303)

        response = await call_next(request)
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
            _track_task(asyncio.create_task(_procesar_equipo_async(miembro, msg)))
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
            # Ignorar metadatos (reactions, read-receipts, edits, polls, etc.).
            # whapi los manda como type=reaction/etc → parser los normaliza a
            # tipo="desconocido" sin texto ni media. NO son comunicación real
            # con el cliente y NO deben pausar el bot.
            es_metadato = (
                msg.tipo == "desconocido"
                and not (msg.texto or "").strip()
                and not msg.media_url
            )
            if es_metadato:
                log.debug(
                    "webhook.humano_metadato_ignorado",
                    cliente=msg.from_number,
                    msg_id=msg.id,
                    raw_type=(msg.raw or {}).get("type"),
                )
                resultados.append({"id": msg.id, "status": "human_metadata_ignored"})
                continue

            cliente = await get_or_create_cliente(session, msg.from_number)
            await pausar_bot(session, cliente.id, horas=1, razon="asesora humana intervino")
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

        # KILL SWITCH: el admin pausó al bot globalmente. No procesamos
        # mensajes de clientes, solo los persistimos para no perder historia.
        # Bot equipo (Fabio/Stiven) ya pasó la condición arriba, así que ellos
        # SÍ pueden seguir hablando con el bot (incluso para reactivarlo).
        if await _bot_global_pausado():
            cliente = await get_or_create_cliente(session, msg.from_number)
            await guardar_conversacion(
                session, cliente_id=cliente.id, direccion="inbound",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
                metadata={"bot_global_pausado": True},
            )
            log.info("webhook.bot_global_pausado_ignorado", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "bot_global_pausado"})
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
        _track_task(asyncio.create_task(_procesar_async(cliente_id, cliente_numero, msg)))

    return {"status": "ok", "procesados": resultados}


# Kill switch global: cache de 5s para no hacer query por cada webhook.
_bot_estado_cache: dict[str, Any] = {"activo": True, "checked_at": 0.0}


async def _bot_global_pausado() -> bool:
    """¿Está pausado el bot globalmente vía tabla bot_estado?

    Cache de 5 segundos para no hacer query Postgres por cada webhook entrante.
    Cuando el admin llama pausar_bot_global, hay un delay max de ~5s hasta que
    el bot deja de responder.
    """
    import time
    from sqlalchemy import text as sa_text
    now = time.time()
    if now - _bot_estado_cache["checked_at"] < 5.0:
        return not _bot_estado_cache["activo"]
    try:
        async with async_session_factory() as session:
            row = (await session.execute(sa_text(
                "SELECT activo FROM bot_estado WHERE id=1"
            ))).first()
        activo = bool(row[0]) if row else True
        _bot_estado_cache["activo"] = activo
        _bot_estado_cache["checked_at"] = now
        return not activo
    except Exception:
        log.exception("webhook.bot_estado_check_fail")
        return False  # defensivo: si falla, asumir activo


# Locks por cliente_id — serializan mensajes del mismo cliente para evitar
# DeadlockDetectedError en Postgres + duplicados en escalar/pedido cuando
# llegan varios webhooks casi simultáneos (cliente manda 2-3 fotos seguidas).
_cliente_locks: dict[int, asyncio.Lock] = {}


def _lock_for_cliente(cliente_id: int) -> asyncio.Lock:
    lock = _cliente_locks.get(cliente_id)
    if lock is None:
        lock = asyncio.Lock()
        _cliente_locks[cliente_id] = lock
    return lock


async def _drain_outbox(outbox: list[dict]) -> None:
    """Despacha los mensajes encolados por los handlers de tools.

    Se llama DESPUÉS de session.commit() — garantiza consistencia:
    lo que sale por whapi === lo que quedó persistido en BD. Si una
    transacción hace rollback, el outbox NO se drena → no hay mensajes
    huérfanos a Fabio.

    Falla por item no aborta el resto: cada envío se aísla y se loggea.
    """
    if not outbox:
        return
    alertas_enviadas: list[int] = []
    for item in outbox:
        kind = item.get("kind")
        try:
            if kind == "text":
                await enviar_texto(item["to"], item["text"])
            elif kind == "image_bytes":
                await enviar_imagen_bytes(
                    item["to"],
                    item["data"],
                    mime=item.get("mime") or "image/jpeg",
                    caption=item.get("caption"),
                )
            else:
                log.warning("flow.outbox.unknown_kind", kind=kind)
                continue
            if item.get("alerta_id"):
                alertas_enviadas.append(int(item["alerta_id"]))
        except Exception as e:
            log.exception("flow.outbox.fail", kind=kind, to=item.get("to"), error=str(e))

    # Marcar alertas como enviadas en una transacción aparte (la del flow ya
    # cerró). No es crítico si esto falla — solo es metadata para Fabio.
    if alertas_enviadas:
        try:
            async with async_session_factory() as session2:
                await session2.execute(
                    sa_update(AlertaFabio)
                    .where(AlertaFabio.id.in_(alertas_enviadas))
                    .values(enviado_a_fabio_en=datetime.now(timezone.utc))
                )
                await session2.commit()
        except Exception:
            log.exception("flow.outbox.mark_alertas_fail", ids=alertas_enviadas)


async def _procesar_async(cliente_id: int, cliente_numero: str, msg: MensajeWhapi) -> None:
    """Procesa el mensaje fuera del request — abre su propia session DB.

    Toma un lock por cliente_id para serializar los mensajes del mismo
    cliente. Sin esto, dos webhooks concurrentes del mismo cliente pueden
    chocar en Postgres (deadlock) y/o duplicar escalaciones/pedidos.

    Después del commit drena el outbox (mensajes a Fabio/equipo) — patrón
    outbox para evitar mensajes huérfanos cuando hay rollback.
    """
    lock = _lock_for_cliente(cliente_id)
    outbox: list[dict] = []
    async with lock:
        async with async_session_factory() as session:
            try:
                outbox = await procesar_mensaje_inbound(
                    session=session,
                    cliente_id=cliente_id,
                    cliente_numero=cliente_numero,
                    msg=msg,
                ) or []
                await session.commit()
            except Exception:
                await session.rollback()
                log.exception("background.flow_fail", cliente=cliente_numero)
                # Importante: NO drenar outbox si hubo rollback — sería
                # exactamente el bug que el patrón outbox previene.
                return
    # Commit OK → ahora sí enviar mensajes al equipo. Fuera del lock para que
    # no bloquee otros mensajes del mismo cliente mientras hacemos I/O whapi.
    await _drain_outbox(outbox)


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
