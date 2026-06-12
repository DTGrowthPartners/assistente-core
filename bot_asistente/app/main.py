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
from app.admin.contactos import router as contactos_router
from app.admin.servicios import router as servicios_router
from app.admin.grupos import router as grupos_router
from app.admin.seguimiento import router as seguimiento_router
from app.admin.etiquetas import router as etiquetas_router
from app.admin.dashboard import router as dashboard_router
from app.admin.stories import router as stories_router
from app.admin.automatizaciones import router as automatizaciones_router
from app.admin.views import ALL_VIEWS
from app.config import get_settings
from app.db.repos import (
    bot_pausado,
    bot_pausado_por_numero,
    estado_chat_por_numero,
    cliente_esta_bloqueado,
    get_or_create_cliente,
    guardar_conversacion,
    marcar_procesado,
    pausar_bot,
    registrar_alerta_fabio,
    ya_procesado,
)
from app.db.session import async_session_factory, engine, get_session
from app.equipo.directorio import (
    es_miembro_equipo,
    es_numero_interno,
    listar_miembros_equipo,
    whitelist_cliente,
)
from app.identidades import (
    Identidad,
    por_key as _identidad_por_key,
    principal as _identidad_principal,
)
from app.flows.conversation import procesar_mensaje_inbound
from app.flows.equipo import procesar_mensaje_equipo
from app.logging_setup import log, setup_logging
from app.whapi.client import enviar_imagen_bytes, enviar_texto
from app.whapi.parser import MensajeWhapi, parsear_payload
from sqlalchemy import select, update as sa_update
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
    # Arrancar scheduler de tareas programadas
    from app.automatizaciones import iniciar_scheduler, detener_scheduler
    iniciar_scheduler()
    yield
    # Detener scheduler primero (deja de tomar tareas nuevas)
    await detener_scheduler()
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
    title="Dairo — DT Growth Partners",
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
app.include_router(contactos_router)
app.include_router(servicios_router)
app.include_router(stories_router)
app.include_router(grupos_router)
app.include_router(seguimiento_router)
app.include_router(etiquetas_router)
app.include_router(automatizaciones_router)

# API externa para sistemas terceros (ej: monitor cuentas Meta → grupos)
from app.api_externo import router as api_externo_router
app.include_router(api_externo_router)

# SQLAdmin: CRUD automático sobre todos los modelos
admin = Admin(
    app,
    engine,
    title="Dairo — Admin",
    authentication_backend=AdminAuth(secret_key=settings.admin_session_secret),
    base_url="/admin",
)
for view in ALL_VIEWS:
    admin.add_view(view)


# Middleware que inyecta nuestro CSS y fuente Inter en cualquier HTML del admin.
# Más simple que sobreescribir templates de Jinja (que requiere conocer la
# herencia interna de SQLAdmin).
# Inyección de shell completo (sidebar + theme) en todas las vistas SQLAdmin.
# Aprovecha SHELL_STYLES/ICON_SPRITE/sidebar_html ya usados por dashboard/chats
# para que TODA la app /admin/* luzca igual. Oculta el navbar-vertical nativo
# de SQLAdmin y desplaza el contenido a la derecha del sidebar custom.
def _build_admin_inject() -> str:
    from app.admin._shell import SHELL_STYLES, ICON_SPRITE, sidebar_html, THEME_TOGGLE_JS
    extra_css = """
<style id="admin-shell-overrides">
  /* Sidebar custom (overlay fixed). SQLAdmin no usa CSS Grid como dashboard. */
  body > aside.sidebar.injected {
    position: fixed; left: 0; top: 0; bottom: 0; width: 240px; z-index: 50;
    background: var(--bg-sidebar) !important; border-right: 1px solid var(--border);
    padding: 20px 14px; display: flex; flex-direction: column;
    overflow-y: auto; height: 100vh;
    transition: width .2s ease;
  }

  /* Empujar TODO el contenido del body 240px a la derecha (robusto vs
     selectores frágiles de Tabler/SQLAdmin). Solo cuando JS marcó el body. */
  body.with-injected-sidebar { padding-left: 240px; transition: padding-left .2s ease; }
  body.with-injected-sidebar.collapsed-sidebar { padding-left: 64px; }
  body.with-injected-sidebar.collapsed-sidebar > aside.sidebar.injected { width: 64px; padding: 20px 8px; }
  body.with-injected-sidebar.collapsed-sidebar .brand-name,
  body.with-injected-sidebar.collapsed-sidebar .nav-group-label,
  body.with-injected-sidebar.collapsed-sidebar aside.sidebar.injected .nav-item span,
  body.with-injected-sidebar.collapsed-sidebar #theme-label,
  body.with-injected-sidebar.collapsed-sidebar .new-btn { display: none !important; }
  body.with-injected-sidebar.collapsed-sidebar aside.sidebar.injected .nav-item { justify-content: center; padding: 10px; }
  body.with-injected-sidebar.collapsed-sidebar .sidebar-collapse-btn svg { transform: rotate(180deg); }

  @media (max-width: 768px) {
    body.with-injected-sidebar { padding-left: 0 !important; }
    body > aside.sidebar.injected {
      transform: translateX(-100%);
      width: 260px;
      transition: transform .25s ease;
    }
    body > aside.sidebar.injected.open { transform: translateX(0); box-shadow: 0 0 24px rgba(0,0,0,.2); }

    /* Reducir padding del wrapper para aprovechar pantalla */
    body > .page-wrapper { padding: 12px 10px !important; }
    .page-header { padding: 12px 0 6px !important; }
    .page-title { font-size: 20px !important; }
    .page-pretitle, .page-subtitle { font-size: 11px !important; }

    /* Headers de página: botones (Crear/Exportar/etc.) que no exploten */
    .page-header .btn-list, .page-header .btn-group {
      flex-wrap: wrap !important; gap: 6px !important;
    }
    .page-header .btn { padding: 6px 10px !important; font-size: 12px !important; }

    /* ── Tablas SQLAdmin responsive ────────────────────────────────────── */
    .card { margin-bottom: 12px !important; }
    .card-body, .card-header, .card-footer { padding: 12px !important; }
    /* Filtros de la barra (search/dropdowns): apilar verticalmente */
    .card-header .row, .card-header > div {
      flex-wrap: wrap !important; gap: 8px !important;
    }
    .card-header .form-select, .card-header input.form-control {
      width: 100% !important; max-width: 100% !important; font-size: 13px !important;
    }
    /* La tabla: scroll horizontal limpio, sin text-nowrap obligatorio */
    .table-responsive {
      overflow-x: auto !important;
      -webkit-overflow-scrolling: touch;
      margin: 0 -4px;   /* gana algo de ancho contra el padding del card */
    }
    .table { font-size: 12px !important; margin-bottom: 0 !important; }
    .table.text-nowrap { white-space: nowrap !important; }  /* mantiene Tabler */
    .table th, .table td {
      padding: 6px 8px !important;
      font-size: 12px !important;
      vertical-align: middle !important;
    }
    .table th { font-size: 11px !important; text-transform: uppercase; letter-spacing: 0.3px; }
    .table .btn { padding: 4px 8px !important; font-size: 11px !important; }
    .table .badge { font-size: 10px !important; padding: 3px 6px !important; }
    /* Checkboxes/iconos primeras 2 columnas más compactos */
    .table th.w-1, .table td.w-1 { padding: 4px !important; }

    /* Paginación: que no se desborde */
    .card-footer .pagination { flex-wrap: wrap !important; gap: 2px !important; }
    .pagination .page-item .page-link { padding: 4px 8px !important; font-size: 12px !important; }
    /* Esconder texto de "Mostrar X de Y registros" larguísimo si lo hay */
    .card-footer .text-muted { font-size: 11px !important; }

    /* Formularios de detalle: campos full width */
    .form-control, .form-select, .form-textarea {
      font-size: 14px !important;
    }
    .row > [class*="col-"] { padding-left: 6px !important; padding-right: 6px !important; }
  }

  /* ── Tablas SQLAdmin: estilo limpio (desktop + mobile) ───────────────── */
  body.with-injected-sidebar .card {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 12px !important;
    box-shadow: var(--shadow-card) !important;
    overflow: hidden;
  }
  body.with-injected-sidebar .card-header {
    background: var(--bg-card) !important;
    border-bottom: 1px solid var(--border) !important;
    padding: 14px 18px !important;
  }
  body.with-injected-sidebar .card-title {
    color: var(--text-primary) !important;
    font-weight: 600 !important; font-size: 15px !important;
  }
  body.with-injected-sidebar .card-footer {
    background: var(--bg-soft) !important;
    border-top: 1px solid var(--border) !important;
    padding: 10px 16px !important;
  }

  /* Tabla en sí */
  body.with-injected-sidebar .table {
    color: var(--text-primary) !important;
    margin-bottom: 0 !important;
  }
  body.with-injected-sidebar .table > :not(caption) > * > * {
    background: transparent !important;
    border-bottom-color: var(--border) !important;
  }
  body.with-injected-sidebar .table thead th {
    background: var(--bg-soft) !important;
    color: var(--text-secondary) !important;
    font-weight: 600 !important;
    font-size: 11px !important;
    text-transform: uppercase;
    letter-spacing: 0.5px;
    padding: 10px 12px !important;
    border-bottom: 1px solid var(--border) !important;
    border-top: none !important;
    white-space: nowrap;
  }
  body.with-injected-sidebar .table thead th a {
    color: var(--text-secondary) !important;
    text-decoration: none;
  }
  body.with-injected-sidebar .table thead th a:hover {
    color: var(--chip-purple) !important;
  }
  body.with-injected-sidebar .table tbody td {
    padding: 10px 12px !important;
    font-size: 13px !important;
    vertical-align: middle !important;
    color: var(--text-primary) !important;
  }
  body.with-injected-sidebar .table tbody tr:hover td {
    background: var(--bg-soft) !important;
  }
  body.with-injected-sidebar .table tbody tr:last-child td {
    border-bottom: none !important;
  }
  body.with-injected-sidebar .table .text-nowrap {
    white-space: nowrap !important;
  }

  /* Botones de acción (ver/editar/borrar) — chips suaves en lugar de íconos pelados */
  body.with-injected-sidebar .table td a[href*="/details/"],
  body.with-injected-sidebar .table td a[href*="/edit/"],
  body.with-injected-sidebar .table td a[href*="/delete/"] {
    display: inline-flex; align-items: center; justify-content: center;
    width: 30px; height: 30px; border-radius: 8px;
    color: var(--text-tertiary) !important;
    transition: all .12s;
    margin-right: 2px;
    border: 1px solid transparent;
  }
  body.with-injected-sidebar .table td a[href*="/details/"]:hover {
    color: var(--chip-blue) !important; background: var(--chip-blue-bg) !important;
  }
  body.with-injected-sidebar .table td a[href*="/edit/"]:hover {
    color: var(--chip-purple) !important; background: var(--chip-purple-bg) !important;
  }
  body.with-injected-sidebar .table td a[href*="/delete/"]:hover {
    color: #ef4444 !important; background: rgba(239,68,68,.08) !important;
  }

  /* Badges (estado, tipo) — más legibles, sin gradient feo de Tabler */
  body.with-injected-sidebar .badge {
    font-weight: 500 !important;
    font-size: 11px !important;
    padding: 4px 8px !important;
    border-radius: 6px !important;
    background: var(--bg-soft) !important;
    color: var(--text-secondary) !important;
    border: 1px solid var(--border) !important;
  }

  /* Search input / filtros */
  body.with-injected-sidebar .form-control,
  body.with-injected-sidebar .form-select {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    color: var(--text-primary) !important;
    border-radius: 8px !important;
    font-size: 13px !important;
    padding: 7px 12px !important;
    box-shadow: none !important;
  }
  body.with-injected-sidebar .form-control:focus,
  body.with-injected-sidebar .form-select:focus {
    border-color: var(--chip-purple) !important;
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--chip-purple) 15%, transparent) !important;
  }
  body.with-injected-sidebar .form-control::placeholder {
    color: var(--text-tertiary) !important;
  }

  /* Botones principales (Crear, Exportar, etc.) */
  body.with-injected-sidebar .btn {
    border-radius: 8px !important;
    font-weight: 500 !important;
    font-size: 13px !important;
    padding: 7px 14px !important;
    border: 1px solid var(--border) !important;
    background: var(--bg-card) !important;
    color: var(--text-primary) !important;
    box-shadow: none !important;
  }
  body.with-injected-sidebar .btn:hover {
    background: var(--bg-soft) !important; border-color: var(--text-tertiary) !important;
  }
  body.with-injected-sidebar .btn-primary {
    background: var(--chip-purple) !important; color: #fff !important; border-color: var(--chip-purple) !important;
  }
  body.with-injected-sidebar .btn-primary:hover {
    background: color-mix(in srgb, var(--chip-purple) 90%, black) !important;
    border-color: color-mix(in srgb, var(--chip-purple) 90%, black) !important;
  }
  body.with-injected-sidebar .btn-danger {
    background: #ef4444 !important; color: #fff !important; border-color: #ef4444 !important;
  }

  /* Paginación */
  body.with-injected-sidebar .pagination .page-link {
    color: var(--text-secondary) !important;
    background: transparent !important;
    border: 1px solid var(--border) !important;
    border-radius: 6px !important;
    margin: 0 1px;
    font-size: 13px !important;
  }
  body.with-injected-sidebar .pagination .page-item.active .page-link {
    background: var(--chip-purple) !important;
    color: #fff !important;
    border-color: var(--chip-purple) !important;
  }
  body.with-injected-sidebar .pagination .page-link:hover {
    background: var(--bg-soft) !important;
    color: var(--text-primary) !important;
  }

  /* Checkbox cabecera/filas más limpios */
  body.with-injected-sidebar .form-check-input {
    border: 1.5px solid var(--border) !important;
    background-color: var(--bg-card) !important;
    cursor: pointer;
  }
  body.with-injected-sidebar .form-check-input:checked {
    background-color: var(--chip-purple) !important;
    border-color: var(--chip-purple) !important;
  }

  /* Página de detalle (vista de un registro): keys en gris claro */
  body.with-injected-sidebar dt, body.with-injected-sidebar .form-label {
    font-weight: 500 !important;
    color: var(--text-secondary) !important;
    font-size: 12px !important;
    text-transform: uppercase;
    letter-spacing: 0.4px;
  }
  body.with-injected-sidebar dd { color: var(--text-primary) !important; }

  /* Ocultar el navbar-vertical nativo de SQLAdmin (Tabler) */
  .navbar-vertical,
  .navbar-vertical.navbar-expand-lg,
  body > .page > .navbar-vertical,
  body > .page > aside.navbar { display: none !important; }

  /* SQLAdmin envuelve todo en .page con grid de 2 columnas (sidebar + main).
     Ya ocultamos el sidebar nativo, pero el grid sigue. Lo reseteamos. */
  body > .page { display: block !important; min-height: auto !important; }
  body > .page > .page-wrapper { margin-left: 0 !important; padding-top: 0 !important; }

  /* Tabler aplica .page-wrapper { margin-left: 15rem } para acomodar su
     navbar-vertical de 15rem. Como ocultamos ese navbar, el margin queda
     como hueco en blanco (240px extra) entre nuestro sidebar y el
     contenido. Anulamos los margins horizontales del page-wrapper en
     cualquier nivel. */
  .page-wrapper { margin-left: 0 !important; margin-right: 0 !important; }
  body > .page-wrapper { padding: 16px 24px !important; }
  .page-header { padding: 16px 0 8px !important; margin-bottom: 0 !important; }
  .page-body { padding-top: 8px !important; }

  /* Fondo correcto en todas las vistas */
  body, .page, .page-wrapper, .page-body, .page-header {
    background: var(--bg-canvas) !important;
  }
  .page-header { border-bottom: none !important; padding-top: 24px !important; }

  /* ── Login page (body.no-shell) — reskin shadcn ───────────────────────── */
  body.no-shell aside.sidebar.injected,
  body.no-shell .mobile-bar,
  body.no-shell .sidebar-backdrop { display: none !important; }
  body.no-shell .page { margin-left: 0 !important; }
  body.no-shell {
    background: var(--bg-canvas) !important;
    min-height: 100vh;
    display: flex; align-items: center; justify-content: center;
  }
  body.no-shell .vh-100 {
    height: auto !important; min-height: 100vh;
    padding: 24px 16px;
  }
  body.no-shell form.card,
  body.no-shell .card.card-md {
    background: var(--bg-card) !important;
    border: 1px solid var(--border) !important;
    border-radius: 14px !important;
    box-shadow: 0 10px 25px rgba(0,0,0,0.04), 0 2px 8px rgba(0,0,0,0.06) !important;
    max-width: 420px !important; width: 100%;
    padding: 8px;
  }
  body.no-shell .card-body { padding: 32px 28px !important; }
  body.no-shell .card-title {
    font-size: 20px !important; font-weight: 700 !important;
    color: var(--text-primary) !important; margin-bottom: 8px !important;
    text-align: left !important;
  }
  body.no-shell .card-title::before {
    content: ""; display: block; width: 44px; height: 44px;
    background: var(--chip-orange-bg); color: var(--chip-orange);
    border-radius: 10px; margin-bottom: 16px;
    background-image: url('data:image/svg+xml;utf8,<svg xmlns="http://www.w3.org/2000/svg" width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="%23EA580C" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>');
    background-repeat: no-repeat; background-position: center;
  }
  body.no-shell .card-title::after {
    content: "Ingresa con tu cuenta de administrador.";
    display: block; font-size: 13px; color: var(--text-secondary);
    font-weight: 400; margin-top: 6px; margin-bottom: 20px;
  }
  body.no-shell .form-label {
    font-size: 12.5px !important; font-weight: 600 !important;
    color: var(--text-secondary) !important; margin-bottom: 6px !important;
    text-transform: none !important; letter-spacing: 0 !important;
  }
  body.no-shell .form-control,
  body.no-shell input[type=text],
  body.no-shell input[type=password] {
    background: var(--bg-card) !important; color: var(--text-primary) !important;
    border: 1px solid var(--border) !important; border-radius: 8px !important;
    padding: 10px 14px !important; font-size: 14px !important;
  }
  body.no-shell .form-control:focus,
  body.no-shell input:focus {
    border-color: var(--btn-primary-bg) !important;
    box-shadow: 0 0 0 3px rgba(0,0,0,.05) !important;
  }
  /* Tabler envuelve el input password en .input-group input-group-flat
     y aplica `.input-group > .form-control { width: 1% }` esperando que
     flex lo expanda con un botón al lado. Como no hay botón, el input se
     ve cuadrado/comprimido. Anulamos esto para que el input use full width. */
  body.no-shell .input-group,
  body.no-shell .input-group-flat { display: block !important; width: 100% !important; }
  body.no-shell .input-group > .form-control,
  body.no-shell .input-group-flat > .form-control,
  body.no-shell .input-group input[type="password"],
  body.no-shell .input-group input[type="text"] {
    width: 100% !important;
    flex: none !important;
    min-width: 0 !important;
    border-radius: 8px !important;
  }
  body.no-shell .btn-primary {
    background: var(--btn-primary-bg) !important;
    color: var(--btn-primary-text) !important;
    border: none !important; border-radius: 8px !important;
    padding: 11px 16px !important; font-size: 14px !important;
    font-weight: 600 !important; margin-top: 8px;
  }
  body.no-shell .form-footer { margin-top: 20px !important; }

  /* Cards de SQLAdmin con look shadcn */
  .card { background: var(--bg-card) !important; border: 1px solid var(--border) !important;
          border-radius: 12px !important; box-shadow: var(--shadow-card) !important; }
  .card-header { background: var(--bg-card) !important;
                 border-bottom: 1px solid var(--border-subtle) !important; }
  .card-body { background: var(--bg-card) !important; }

  /* Tabla */
  .table { background: var(--bg-card) !important; color: var(--text-primary) !important; }
  .table thead th { background: var(--bg-soft) !important; color: var(--text-tertiary) !important;
                    font-size: 11px !important; text-transform: uppercase; letter-spacing: .5px;
                    border-bottom: 1px solid var(--border) !important; }
  .table tbody tr { background: var(--bg-card) !important; }
  .table tbody tr:hover { background: var(--bg-soft) !important; }
  .table tbody td { color: var(--text-primary) !important;
                    border-color: var(--border-subtle) !important;
                    border-top: none !important;
                    border-bottom: 1px solid var(--border-subtle) !important; }
  .table tbody td a { color: var(--chip-blue) !important; }

  /* Form controls */
  .form-control, input[type=text], input[type=search], input[type=email],
  input[type=number], input[type=password], select, textarea {
    background: var(--bg-card) !important; color: var(--text-primary) !important;
    border: 1px solid var(--border) !important; border-radius: 8px !important;
  }
  .form-label, label { color: var(--text-secondary) !important; }

  /* Botones */
  .btn-primary { background: var(--btn-primary-bg) !important; color: var(--btn-primary-text) !important;
                 border: none !important; border-radius: 8px !important; }
  .btn-outline-secondary, .btn-secondary {
    background: var(--bg-card) !important; color: var(--text-primary) !important;
    border: 1px solid var(--border) !important; border-radius: 8px !important; }
  .btn-danger { border-radius: 8px !important; }

  /* Paginación */
  .pagination .page-link { background: var(--bg-card) !important; color: var(--text-secondary) !important;
                           border: 1px solid var(--border) !important; border-radius: 8px !important;
                           margin: 0 2px; }
  .pagination .page-item.active .page-link {
    background: var(--btn-primary-bg) !important; color: var(--btn-primary-text) !important;
    border-color: var(--btn-primary-bg) !important; }

  /* Dropdowns + modales */
  .dropdown-menu, .modal-content { background: var(--bg-card) !important;
    color: var(--text-primary) !important; border-color: var(--border) !important; }
  .dropdown-item { color: var(--text-primary) !important; }
  .dropdown-item:hover { background: var(--bg-soft) !important; }
</style>
"""
    init_script = """
<script>
(function(){
  // Auto-redirect /admin → /admin/dashboard
  var p = location.pathname.replace(/\\/$/, "");
  if (p === "/admin") { location.replace("/admin/dashboard"); return; }
  // Tema dark/light desde localStorage (también ya lo hace SHELL_STYLES head-script,
  // pero por seguridad lo repetimos)
  try {
    var saved = localStorage.getItem("theme");
    document.documentElement.setAttribute("data-theme", saved === "dark" ? "dark" : "light");
  } catch(e) {}
})();
</script>
"""
    # JS que inyecta el sidebar al DOM cuando carga la página + theme toggle
    inject_js = """
<script>
document.addEventListener("DOMContentLoaded", function() {
  // No inyectar shell en la página de login (form sin session todavía)
  var isLogin = /\\/admin\\/login\\b/.test(location.pathname);
  if (isLogin) {
    document.body.classList.add("no-shell");
    return;
  }
  if (document.querySelector("aside.sidebar.injected")) return;

  // Determinar el item activo según la URL actual
  var path = location.pathname;
  var active = "";
  if (path.indexOf("/admin/dashboard") === 0) active = "dashboard";
  else if (path.indexOf("/admin/chats") === 0) active = "chats";
  else if (path.indexOf("/admin/cliente") === 0) active = "clientes";
  else if (path.indexOf("/admin/pedido") === 0) active = "pedidos";
  else if (path.indexOf("/admin/alerta") === 0) active = "alertas";
  else if (path.indexOf("/admin/equipo") === 0) active = "equipo";
  else if (path.indexOf("/admin/numero") === 0) active = "internos";
  else if (path.indexOf("/admin/producto") === 0) active = "productos";
  else if (path.indexOf("/admin/tarifa") === 0) active = "tarifas";

  // Marcar el item activo via clase
  setTimeout(function(){
    document.querySelectorAll('aside.sidebar.injected .nav-item').forEach(function(a){
      a.classList.remove('active');
      if (active && a.getAttribute('href') && a.getAttribute('href').indexOf('/admin/' + active.replace('clientes','cliente').replace('pedidos','pedido').replace('alertas','alerta').replace('equipo','equipo-miembro').replace('internos','numero-interno').replace('productos','producto-cache').replace('tarifas','tarifa-domicilio')) === 0) {
        a.classList.add('active');
      }
    });
  }, 10);

  // Theme toggle handler (el botón está dentro del sidebar inyectado)
  var btn = document.getElementById("theme-toggle");
  if (btn) {
    var lbl = document.getElementById("theme-label");
    if (lbl) lbl.textContent =
      (document.documentElement.getAttribute("data-theme") === "dark" ? "Modo claro" : "Modo oscuro");
    btn.addEventListener("click", function(){
      var cur = document.documentElement.getAttribute("data-theme") || "light";
      var nxt = cur === "dark" ? "light" : "dark";
      document.documentElement.setAttribute("data-theme", nxt);
      localStorage.setItem("theme", nxt);
      if (lbl) lbl.textContent = nxt === "dark" ? "Modo claro" : "Modo oscuro";
    });
  }
});
</script>
"""
    # Variante del sidebar con clase 'injected' para que solo el inyectado
    # use las posiciones overlay (no rompe a dashboard/chats que ya usan grid).
    sidebar = sidebar_html(active="").replace(
        '<aside class="sidebar">', '<aside class="sidebar injected">'
    )
    # Convertimos el HTML del sidebar en algo que body.insertBefore pueda usar
    sidebar_js_safe = sidebar.replace("\\", "\\\\").replace("`", "\\`")
    inject_sidebar_js = f"""
<script>
document.addEventListener("DOMContentLoaded", function() {{
  if (/\\/admin\\/login\\b/.test(location.pathname)) {{
    document.body.classList.add("no-shell");
    return;
  }}
  // Si la página ya trae su propio sidebar (dashboard/chats que usan el
  // template inline con __SIDEBAR__), NO inyectamos nada — ya tienen su
  // layout .app grid completo. Solo aplica para vistas SQLAdmin.
  if (document.querySelector("aside.sidebar")) return;

  var html = `{sidebar_js_safe}`;
  var t = document.createElement("template");
  t.innerHTML = html.trim();
  document.body.insertBefore(t.content.firstChild, document.body.firstChild);
  document.body.classList.add("with-injected-sidebar");
  // Restaurar estado collapsed si estaba guardado
  if (localStorage.getItem("sidebar") === "collapsed") {{
    document.body.classList.add("collapsed-sidebar");
  }}
  // Inyectar el SVG sprite si no existe
  if (!document.getElementById("admin-icon-sprite")) {{
    var s = document.createElement("div");
    s.id = "admin-icon-sprite";
    s.innerHTML = `{ICON_SPRITE.replace("`", "\\`")}`;
    s.style.display = "none";
    document.body.appendChild(s);
  }}
}});
</script>
"""

    # THEME_TOGGLE_JS auto-inyecta mobile-bar, sidebar-collapse-btn y maneja
    # theme/collapse/hamburger. Va al final para que encuentre el sidebar ya
    # inyectado en el DOM (inject_sidebar_js corre antes en DOMContentLoaded).
    return (
        SHELL_STYLES + extra_css + init_script
        + inject_sidebar_js + inject_js + THEME_TOGGLE_JS
    )


_ADMIN_CSS_INJECT = _build_admin_inject()


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
    return {"status": "ok", "service": "dtgp-bot", "env": settings.bot_env}


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

        # 🎙 PRIORIDAD 1.45 — Nota de voz → transcribir con Whisper (OpenAI).
        # IMPORTANTE: este hook DEBE correr ANTES del routing de grupos (1.5)
        # y de cualquier otro routing, para que `msg.texto` quede mutado y
        # todos los flows (prospecto, equipo, cliente WL, self-chat) reciban
        # el audio como texto. Falla silencioso: si no se pudo transcribir,
        # el flow detecta `texto` vacío y hace fallback.
        # Detecta audio por: tipo == "audio" O media_mime empieza con "audio/"
        # (defensa contra parser que clasifica audios como "desconocido").
        _mime_lc = (msg.media_mime or "").lower()
        _es_audio = (
            msg.tipo == "audio"
            or _mime_lc.startswith("audio/")
            or _mime_lc in ("application/ogg",)
        )
        if _es_audio and msg.media_url and not (msg.texto or "").strip():
            log.info("webhook.audio_detectado",
                     cliente=msg.from_number, tipo=msg.tipo, mime=msg.media_mime)
            try:
                from app.integrations.whisper import transcribir_audio
                transcripcion = await transcribir_audio(msg.media_url, msg.media_mime)
                if transcripcion:
                    msg.texto = transcripcion
                    log.info("webhook.audio_transcrito",
                             cliente=msg.from_number, chars=len(transcripcion))
                else:
                    log.warning("webhook.audio_no_transcrito",
                                cliente=msg.from_number)
            except Exception as e:
                log.warning("webhook.audio_transcribir_exc",
                            cliente=msg.from_number, error=str(e)[:200])
        elif (
            # Diagnóstico temporal: cuando no hay texto y hay media, loguear
            # el tipo/mime para confirmar qué clasifica el parser.
            not (msg.texto or "").strip() and msg.media_url
        ):
            log.info("webhook.media_no_audio",
                     cliente=msg.from_number, tipo=msg.tipo, mime=msg.media_mime)
        elif not (msg.texto or "").strip():
            # Diagnóstico extra: mensaje sin texto y SIN media_url — dumpear
            # los campos clave + las keys del raw para entender qué entrega whapi.
            import json as _json
            try:
                raw_keys = list((msg.raw or {}).keys())
                raw_preview = _json.dumps(msg.raw or {}, ensure_ascii=False)[:800]
            except Exception:
                raw_keys, raw_preview = [], ""
            log.warning("webhook.msg_sin_texto_sin_media",
                        cliente=msg.from_number, tipo=msg.tipo,
                        mime=msg.media_mime, media_url=msg.media_url,
                        raw_keys=raw_keys, raw_preview=raw_preview)

        # 🚨 PRIORIDAD 1.5 — Mensajes de GRUPOS de WhatsApp.
        # Por default ignoramos todos los grupos. EXCEPCIÓN: el grupo del
        # EQUIPO DTGP (settings.equipo_dtgp_group_id) — ahí SÍ procesamos
        # mensajes de miembros del equipo y respondemos AL GRUPO (no al
        # chat personal). Es el canal "operativo" del bot.
        if msg.chat_id and msg.chat_id.endswith("@g.us"):
            es_grupo_equipo = (
                settings.equipo_dtgp_group_id
                and msg.chat_id == settings.equipo_dtgp_group_id
            )
            if es_grupo_equipo:
                miembro = es_miembro_equipo(msg.from_number)
                # CASO especial: el operador físico (Dairo/Stiven/Edgardo) escribió
                # en el grupo desde la app de WhatsApp del CELULAR del bot.
                # whapi marca eso como from_me=true source=mobile (is_from_human).
                # No es eco de la API (eso sería is_from_bot). Necesitamos
                # procesar ese mensaje como instrucción del equipo, no descartarlo.
                if (
                    not miembro
                    and msg.is_from_human
                    and msg.from_number == settings.whapi_numero_bot
                ):
                    fallbacks = [
                        m for m in listar_miembros_equipo() if m.es_fallback
                    ] or listar_miembros_equipo()
                    miembro = fallbacks[0] if fallbacks else None
                    if miembro:
                        log.info("webhook.grupo_equipo_msg_via_celular_bot",
                                 from_=msg.from_number, atribuido_a=miembro.nombre)
                if not miembro:
                    log.debug("webhook.grupo_equipo_externo_ignorado", from_=msg.from_number)
                    resultados.append({"id": msg.id, "status": "group_external_ignored"})
                    continue

                # ── ¿Es click de botón en una alerta del bot? ────────────
                # Cuando alguien clickea un botón quick-reply, WhatsApp envía
                # un mensaje normal con el TEXTO del botón citando el original.
                # Si el quoted matchea una alerta nuestra, ejecutamos la acción
                # directa sin pasar por Claude (más rápido + más barato).
                accion_resuelta = await _resolver_click_alerta(session, msg, miembro)
                if accion_resuelta:
                    resultados.append({"id": msg.id, "status": accion_resuelta})
                    continue

                log.info("webhook.grupo_equipo_msg", miembro=miembro.nombre, from_=msg.from_number)
                resultados.append({"id": msg.id, "status": "group_routed", "miembro": miembro.nombre})
                _track_task(asyncio.create_task(
                    _procesar_equipo_async(
                        miembro, msg, _identidad_principal(),
                        responder_a=settings.equipo_dtgp_group_id,
                    )
                ))
                continue
            log.info("webhook.grupo_ignorado", chat_id=msg.chat_id, from_=msg.from_number)
            resultados.append({"id": msg.id, "status": "group_ignored"})
            continue

        # 🚨 PRIORIDAD 1.7 — Eventos sin contenido real (reactions, taps de
        # botones Meta sin texto, read receipts, edits vacíos, polls vacías).
        # Se ignoran ANTES de cualquier routing porque no aportan información
        # útil y saturaban el chat del admin con "[desconocido]".
        if msg.tipo == "desconocido" and not (msg.texto or "").strip() and not msg.media_url:
            log.debug(
                "webhook.evento_sin_contenido_ignorado",
                cliente=msg.from_number, msg_id=msg.id,
                raw_type=(msg.raw or {}).get("type"),
            )
            resultados.append({"id": msg.id, "status": "evento_sin_contenido_ignorado"})
            continue

        # 🚨 PRIORIDAD 1.8 — Self-chat: Dairo desde el celular del bot abre
        # "Mensaje a ti mismo" y le escribe a Claude. whapi lo reporta con
        # chat_id = <numero_bot>@s.whatsapp.net y from_me=true.
        # - source=mobile/web (is_from_human) → es Dairo escribiendo manual →
        #   procesar como mensaje del equipo, responder al MISMO self-chat.
        # - source=api (is_from_bot) → es eco del bot enviando respuesta →
        #   ya se filtra abajo en `is_from_bot`, no entra aquí.
        bot_num_clean = (settings.whapi_numero_bot or "").lstrip("+")
        es_self_chat = bool(
            bot_num_clean
            and msg.from_number == settings.whapi_numero_bot
            and msg.chat_id
            and msg.chat_id.split("@", 1)[0] == bot_num_clean
        )
        if es_self_chat and msg.is_from_human:
            fallbacks = [
                m for m in listar_miembros_equipo() if m.es_fallback
            ] or listar_miembros_equipo()
            miembro_self = fallbacks[0] if fallbacks else None
            if miembro_self:
                log.info("webhook.self_chat", from_=msg.from_number, atribuido=miembro_self.nombre)
                resultados.append({"id": msg.id, "status": "self_chat_routed"})
                _track_task(asyncio.create_task(
                    _procesar_equipo_async(
                        miembro_self, msg, _identidad_principal(),
                        responder_a=msg.chat_id,
                    )
                ))
                continue
            log.warning("webhook.self_chat.sin_miembro_fallback")

        # 🚨 PRIORIDAD 2 — Outbound desde el celular humano (no del bot API).
        # Si Dairo (o quien tenga el celular) escribe directamente desde la app
        # móvil, whapi lo marca con source=mobile/web. Debe persistirse como
        # `direccion=humano` (no inbound) y, por defecto, pausar el bot 1h.
        # EXCEPCIÓN: mensajes en grupos los maneja el routing de grupos (más
        # abajo), no este bloque — sino Dairo no podría hablarle al bot desde
        # el grupo EQUIPO DTGP.
        if msg.is_from_human and not (msg.chat_id and msg.chat_id.endswith("@g.us")):
            es_metadato = (
                msg.tipo == "desconocido"
                and not (msg.texto or "").strip()
                and not msg.media_url
            )
            if es_metadato:
                log.debug(
                    "webhook.humano_metadato_ignorado",
                    cliente=msg.from_number, msg_id=msg.id,
                    raw_type=(msg.raw or {}).get("type"),
                )
                resultados.append({"id": msg.id, "status": "human_metadata_ignored"})
                continue

            cliente = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)
            destino_es_equipo = bool(es_miembro_equipo(msg.from_number))
            if not destino_es_equipo:
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
            log.info(
                "webhook.humano_interviene",
                cliente=msg.from_number, destino_equipo=destino_es_equipo,
            )
            resultados.append({"id": msg.id, "status": "human_paused_bot" if not destino_es_equipo else "human_to_team"})
            continue

        # Estado del chat (cliente_id, etiqueta, pausado) — single query.
        # Lo necesitamos ANTES del routing para que un admin pueda VETAR el
        # flujo operativo marcando un contacto como `personal` aunque esté en
        # equipo_miembros o contactos_whitelist (caso real: contacto importado
        # como WL pero que en la práctica es solo amigo personal).
        cli_id_pre, etiqueta_pre, pausado_indiv_pre = await estado_chat_por_numero(
            session, msg.from_number,
        )

        # ¿Es un MIEMBRO del equipo DTGP escribiendo a su chat personal?
        # POLÍTICA: el bot SOLO atiende al equipo en el grupo EQUIPO DTGP.
        # En chats personales del equipo → silencio (solo persistir).
        # Si quieren operar con el bot, lo hacen en el grupo.
        miembro = es_miembro_equipo(msg.from_number)
        if miembro:
            cliente_id_persist = cli_id_pre
            if cliente_id_persist is None:
                _cli_tmp = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)
                cliente_id_persist = _cli_tmp.id
            await guardar_conversacion(
                session, cliente_id=cliente_id_persist, direccion="inbound",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
                metadata={"chat_personal_silenciado": True, "miembro": miembro.nombre},
            )
            log.info(
                "webhook.miembro_chat_personal_silenciado",
                miembro=miembro.nombre, from_=msg.from_number,
            )
            resultados.append({"id": msg.id, "status": "team_personal_silenced"})
            continue

        # ¿Es un CLIENTE whitelisted? → flujo operativo con permisos scoped (rol=cliente)
        cliente_wl = whitelist_cliente(msg.from_number)
        if cliente_wl:
            # Modo global "solo_prospectos" o "off" → silenciar también a la WL.
            if await _bot_bloqueado_para_whitelist():
                if cli_id_pre is not None:
                    await guardar_conversacion(
                        session, cliente_id=cli_id_pre, direccion="inbound",
                        tipo=msg.tipo, contenido=msg.texto,
                        whapi_message_id=msg.id, media_url=msg.media_url,
                        metadata={"bot_global_modo_block": True, "es_cliente_wl": True},
                    )
                log.info("webhook.cliente_wl_silenciado_por_modo", cliente=cliente_wl.nombre, from_=msg.from_number)
                resultados.append({"id": msg.id, "status": "cliente_wl_silenciado_modo"})
                continue
            # Mismo veto: etiqueta=personal manda sobre la whitelist.
            if etiqueta_pre == "personal":
                if cli_id_pre is not None:
                    await guardar_conversacion(
                        session, cliente_id=cli_id_pre, direccion="inbound",
                        tipo=msg.tipo, contenido=msg.texto,
                        whapi_message_id=msg.id, media_url=msg.media_url,
                        metadata={"routing_vetoed_by_personal": True, "era_cliente_wl": True},
                    )
                log.warning(
                    "webhook.cliente_wl_route_vetoed_personal",
                    cliente=cliente_wl.nombre, from_=msg.from_number,
                )
                resultados.append({"id": msg.id, "status": "cliente_wl_vetoed_personal"})
                continue
            # Pausa por chat individual prevalece.
            if pausado_indiv_pre:
                if cli_id_pre is not None:
                    await guardar_conversacion(
                        session, cliente_id=cli_id_pre, direccion="inbound",
                        tipo=msg.tipo, contenido=msg.texto,
                        whapi_message_id=msg.id, media_url=msg.media_url,
                        metadata={"bot_pausado": True, "es_cliente_wl": True},
                    )
                log.info("webhook.cliente_wl_chat_paused", cliente=cliente_wl.nombre, from_=msg.from_number)
                resultados.append({"id": msg.id, "status": "cliente_wl_chat_paused"})
                continue
            log.info("webhook.inbound_cliente_wl", cliente=cliente_wl.nombre, from_=msg.from_number)
            resultados.append({"id": msg.id, "status": "client_routed", "cliente": cliente_wl.nombre})
            _track_task(asyncio.create_task(_procesar_equipo_async(cliente_wl, msg, _identidad_principal())))
            continue

        # Número interno NO-miembro (asesoras, bodegas) → ignorar silencioso
        if es_numero_interno(msg.from_number) and msg.from_number != settings.dueno_phone_blocked:
            log.info("webhook.numero_interno_ignorado", from_=msg.from_number)
            resultados.append({"id": msg.id, "status": "internal_team_ignored"})
            continue

        # Bloqueo del número del dueño (alerta a Fabio, dedupada)
        if msg.from_number == settings.dueno_phone_blocked:
            log.warning("webhook.numero_bloqueado", from_=msg.from_number)
            # Dedupe: si ya hay alerta mensaje_dueno abierta o creada en las
            # últimas 6h, NO crear otra. El dueño puede mandar ráfagas y no
            # queremos spamear 60 alertas idénticas a Fabio.
            from datetime import timedelta as _td
            from sqlalchemy import text as _sa_text
            ventana = datetime.now(timezone.utc) - _td(hours=6)
            ya_hay = (await session.execute(
                _sa_text(
                    "SELECT 1 FROM alertas_fabio "
                    "WHERE tipo='mensaje_dueno' "
                    "AND created_at >= :v "
                    "LIMIT 1"
                ),
                {"v": ventana},
            )).first()
            if not ya_hay:
                await registrar_alerta_fabio(
                    session,
                    tipo="mensaje_dueno",
                    mensaje=(
                        f"Llegó un mensaje del número del dueño ({msg.from_number}): "
                        f"{(msg.texto or '')[:200]}"
                    ),
                )
                from app.notif_equipo import notificar_equipo
                await notificar_equipo(
                    f"🔔 *Mensaje del dueño bloqueado*\n\n"
                    f"De: {msg.from_number}\n\n"
                    f"{(msg.texto or '')[:300]}\n\n"
                    f"_Cualquiera puede atender._"
                )
            resultados.append({"id": msg.id, "status": "blocked_dueno"})
            continue

        # (El bloque is_from_human ya se procesó arriba, antes del routing
        # a equipo/whitelist — los outbounds humanos llegan aquí solo si por
        # alguna razón no entraron antes; en ese caso, segunda ronda defensiva.)
        if msg.is_from_human:
            cliente = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)
            await guardar_conversacion(
                session, cliente_id=cliente.id, direccion="humano",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
            )
            resultados.append({"id": msg.id, "status": "human_late_fallback"})
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
            cliente = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)
            await guardar_conversacion(
                session, cliente_id=cliente.id, direccion="inbound",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
                metadata={"bot_global_pausado": True},
            )
            log.info("webhook.bot_global_pausado_ignorado", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "bot_global_pausado"})
            continue

        cliente = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)

        # ── Etiquetado de contacto (auto al primer mensaje) ──────────────────
        if cliente.etiqueta is None:
            # Si llega con atribución de anuncio Meta → prospecto.
            if msg.referral:
                cliente.etiqueta = "prospecto"
                cliente.etiqueta_actualizada_en = datetime.now(timezone.utc)
                cliente.etiqueta_actualizada_por = "auto:referral"

        # ── Mensajes "canned" de pauta (Meta CTWA / botones FAQ) ─────────────
        # Auto-etiqueta el contacto como prospecto y deja que el flow normal lo
        # procese: Dairo responde de una sin esperar la auto-respuesta de Meta
        # (la auto-respuesta del FAQ a veces no llega, no podemos depender de ella).
        from app.pauta import es_canned_pauta, es_saludo_simple
        es_pauta_canned = es_canned_pauta(msg.texto)
        if es_pauta_canned:
            if cliente.etiqueta is None or cliente.etiqueta == "prospecto":
                cliente.etiqueta = "prospecto"
                cliente.etiqueta_actualizada_en = datetime.now(timezone.utc)
                cliente.etiqueta_actualizada_por = "auto:canned_pauta"
            log.info("webhook.canned_pauta_detected", cliente=msg.from_number,
                     preview=(msg.texto or "")[:80])
            # NO hacemos continue: el mensaje sigue al flow de prospecto y se
            # persiste UNA sola vez en el bloque de procesamiento normal.

        # ── Saludo simple ('hola', 'buenas', 'info'...) sin canned/referral ──
        # Este número es público (pauta activa). Si alguien escribe un saludo
        # corto y no está etiquetado, asumimos que viene de la pauta aunque no
        # haya dado click al CTA de Meta. Lo etiquetamos prospecto para que
        # Dairo le responda en lugar de silenciarlo por política estricta.
        es_saludo = es_saludo_simple(msg.texto)
        if es_saludo and cliente.etiqueta is None:
            cliente.etiqueta = "prospecto"
            cliente.etiqueta_actualizada_en = datetime.now(timezone.utc)
            cliente.etiqueta_actualizada_por = "auto:saludo_simple"
            log.info("webhook.saludo_simple_detected", cliente=msg.from_number,
                     preview=(msg.texto or "")[:60])

        # ── Auto-reactivar pausa cuando vuelve un prospecto desde pauta ─────
        # Si vino con referral Meta o con canned-pauta, es señal clara de que
        # el cliente quiere atención (volvió a entrar por la pauta). Si había
        # pausa individual activa, la quitamos para que Dairo lo atienda.
        if (msg.referral or es_pauta_canned) and pausado_indiv_pre:
            from sqlalchemy import text as _sa_text
            await session.execute(_sa_text(
                "DELETE FROM intervencion_humana WHERE cliente_id = :cid"
            ), {"cid": cliente.id})
            log.warning(
                "webhook.pausa_auto_reactivada",
                cliente=msg.from_number,
                razon=("referral" if msg.referral else "canned_pauta"),
            )
            pausado_indiv_pre = False  # ya no está pausado para los checks que siguen

        # ── Eventos sin contenido real (reactions, taps de botones Meta sin
        # texto, read receipts, edits vacíos, polls vacías, etc.)
        # NO los persistimos: saturaban el chat del admin con "[desconocido]"
        # y no aportan información útil. Solo log para auditoría.
        if msg.tipo == "desconocido" and not (msg.texto or "").strip() and not msg.media_url:
            log.debug(
                "webhook.evento_sin_contenido_ignorado",
                cliente=msg.from_number, msg_id=msg.id,
                raw_type=(msg.raw or {}).get("type"),
            )
            resultados.append({"id": msg.id, "status": "evento_sin_contenido_ignorado"})
            continue

        # ── Silencio TOTAL si está etiquetado como personal ──────────────────
        # (Caso clave del canal de Dairo: contactos privados nunca obtienen bot.)
        if cliente.etiqueta == "personal":
            await guardar_conversacion(
                session, cliente_id=cliente.id, direccion="inbound",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
                metadata={"silencio": "etiqueta=personal"},
            )
            log.info("webhook.silencio_personal", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "silenced_personal"})
            continue

        # ── Política ESTRICTA (identidad principal lo define): si la identidad
        # exige etiqueta y este contacto está sin clasificar (NULL) y no viene
        # de pauta (sin referral) → silencio + alerta para clasificarlo. ────
        _ident_principal = _identidad_principal()
        if (
            _ident_principal.politica_estricta
            and cliente.etiqueta is None
            and not msg.referral
        ):
            from datetime import timedelta as _td
            from sqlalchemy import text as _sa_text
            await guardar_conversacion(
                session, cliente_id=cliente.id, direccion="inbound",
                tipo=msg.tipo, contenido=msg.texto,
                whapi_message_id=msg.id, media_url=msg.media_url,
                metadata={"silencio": "sin_clasificar_politica_estricta"},
            )
            ventana = datetime.now(timezone.utc) - _td(hours=6)
            ya_hay = (await session.execute(
                _sa_text(
                    "SELECT 1 FROM alertas_fabio WHERE tipo='pide_humano' "
                    "AND cliente_id=:c AND created_at >= :v LIMIT 1"
                ),
                {"c": cliente.id, "v": ventana},
            )).first()
            if not ya_hay:
                from app.notif_equipo import notificar_equipo_con_botones, notificar_equipo
                # Enviar alerta con botones (Prospecto / Personal / Ignorar) al
                # grupo. Al clickear, llega un mensaje texto al grupo citando
                # esta alerta → el routing del grupo ejecuta la acción.
                body_alerta = (
                    f"📱 {msg.from_number}\n"
                    f"👤 {msg.from_name or '?'}\n\n"
                    f"\"{(msg.texto or '')[:160]}\""
                )
                resp_alerta = await notificar_equipo_con_botones(
                    body=body_alerta,
                    header=f"🔔 Sin clasificar",
                    botones=[
                        ("clasif:prospecto", "Prospecto"),
                        ("clasif:personal",  "Personal"),
                        ("clasif:ignorar",   "Ignorar"),
                    ],
                )
                whapi_id_alerta = None
                if resp_alerta and isinstance(resp_alerta, dict):
                    whapi_id_alerta = (resp_alerta.get("message") or {}).get("id")
                # Si fallaron los botones (ej. error de whapi), caer a texto.
                if not whapi_id_alerta:
                    await notificar_equipo(
                        f"🔔 *Número sin clasificar*\n\n{body_alerta}\n\n"
                        f"_Responde 'prospecto' / 'personal' / 'ignorar' citando este mensaje._"
                    )
                await registrar_alerta_fabio(
                    session, tipo="pide_humano",
                    mensaje=(
                        f"Nuevo número SIN CLASIFICAR: {msg.from_number} "
                        f"({msg.from_name or '?'}). Preview: "
                        f"{(msg.texto or '')[:160]}."
                    ),
                    cliente_id=cliente.id,
                    whapi_message_id=whapi_id_alerta,
                )
            log.info("webhook.silencio_sin_clasificar", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "silenced_unclassified"})
            continue

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
        _track_task(asyncio.create_task(_procesar_async(cliente_id, cliente_numero, msg, _identidad_principal())))

    return {"status": "ok", "procesados": resultados}


@app.post("/webhooks/calcom")
async def webhook_calcom(
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Recibe eventos de Cal.com (BOOKING_CREATED/CANCELLED/RESCHEDULED) y
    sincroniza la tabla `citas`. Útil cuando el prospecto cancela/reprograma
    desde el link de Cal.com (fuera del chat)."""
    from app.integrations import calcom

    raw = await request.body()
    firma = request.headers.get("x-cal-signature-256")
    if not calcom.verificar_firma(raw, firma):
        log.warning("calcom_webhook.firma_invalida")
        return {"status": "invalid_signature"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "bad_request"}

    trigger = payload.get("triggerEvent")
    data = payload.get("payload", {}) or {}
    uid = data.get("uid") or data.get("bookingUid")
    log.info("calcom_webhook.recibido", trigger=trigger, uid=uid)

    if not uid:
        return {"status": "ignored", "reason": "sin uid"}

    from sqlalchemy import text as _sa_text
    if trigger == "BOOKING_CANCELLED":
        await session.execute(_sa_text(
            "UPDATE citas SET estado='cancelada', updated_at=now() WHERE calcom_uid=:u"
        ), {"u": uid})
    elif trigger == "BOOKING_RESCHEDULED":
        nuevo_inicio = data.get("startTime") or data.get("start")
        await session.execute(_sa_text(
            "UPDATE citas SET estado='reprogramada', "
            "fecha_inicio=COALESCE(:f, fecha_inicio), updated_at=now() WHERE calcom_uid=:u"
        ), {"f": nuevo_inicio, "u": uid})
    # BOOKING_CREATED: el bot ya las crea vía tool; las hechas por el link público
    # quedan registradas en Cal.com (se pueden conciliar luego). Solo logueamos.
    await session.commit()
    return {"status": "ok", "trigger": trigger}


@app.post("/webhook/{identidad_key}")
async def webhook_identidad(
    identidad_key: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Webhook por identidad (multi-canal). Hoy: identidad_key='dairo'.

    Aplica POLÍTICA ESTRICTA: solo atiende contactos con etiqueta cliente/prospecto/equipo.
    Personal y NULL (sin clasificar) → silencio (NULL además crea alerta para clasificar).
    """
    identidad = _identidad_por_key(identidad_key)
    if not identidad or not identidad.activa:
        log.info("webhook.identidad_inactiva", key=identidad_key, configured=bool(identidad))
        return {"status": "ignored", "reason": "identidad_inactiva"}
    if identidad.key == "principal":
        # La identidad principal vive en /webhook. Evita loop si alguien apunta acá.
        return {"status": "ignored", "reason": "usa /webhook para la identidad principal"}

    try:
        payload = await request.json()
    except Exception:
        return {"status": "bad_request"}
    if "messages" not in payload:
        return {"status": "ignored", "reason": "no_messages_array"}

    mensajes = parsear_payload(payload)
    if not mensajes:
        return {"status": "ignored", "reason": "no_parseable_messages"}

    resultados: list[dict[str, Any]] = []
    para_prospecto: list[tuple[int, str, MensajeWhapi]] = []

    for msg in mensajes:
        if await ya_procesado(session, msg.id):
            resultados.append({"id": msg.id, "status": "duplicate"})
            continue
        await marcar_procesado(session, msg.id)

        if msg.is_from_bot:
            resultados.append({"id": msg.id, "status": "own_outbound_ignored"})
            continue
        if msg.chat_id and msg.chat_id.endswith("@g.us"):
            resultados.append({"id": msg.id, "status": "group_ignored"})
            continue

        # Equipo whitelisted → operativo (con persona/canal Dairo)
        miembro = es_miembro_equipo(msg.from_number)
        if miembro:
            log.info("webhook_dairo.inbound_equipo", miembro=miembro.nombre)
            resultados.append({"id": msg.id, "status": "team_routed", "miembro": miembro.nombre})
            _track_task(asyncio.create_task(_procesar_equipo_async(miembro, msg, identidad)))
            continue

        # Cliente whitelisted → operativo scoped (canal Dairo)
        cliente_wl = whitelist_cliente(msg.from_number)
        if cliente_wl:
            log.info("webhook_dairo.inbound_cliente_wl", cliente=cliente_wl.nombre)
            resultados.append({"id": msg.id, "status": "client_routed", "cliente": cliente_wl.nombre})
            _track_task(asyncio.create_task(_procesar_equipo_async(cliente_wl, msg, identidad)))
            continue

        # Crear/obtener cliente
        cliente = await get_or_create_cliente(session, msg.from_number, nombre=msg.from_name)

        # Auto-etiqueta por referral
        if cliente.etiqueta is None and msg.referral:
            cliente.etiqueta = "prospecto"
            cliente.etiqueta_actualizada_en = datetime.now(timezone.utc)
            cliente.etiqueta_actualizada_por = "auto:referral"

        # Persistir inbound siempre (auditoría)
        await guardar_conversacion(
            session, cliente_id=cliente.id, direccion="inbound",
            tipo=msg.tipo, contenido=msg.texto, whapi_message_id=msg.id,
            media_url=msg.media_url,
            metadata={"identidad": identidad.key, "etiqueta_actual": cliente.etiqueta},
        )

        # Política estricta: solo atender etiqueta=prospecto.
        if cliente.etiqueta == "personal":
            log.info("webhook_dairo.silencio_personal", cliente=msg.from_number)
            resultados.append({"id": msg.id, "status": "silenced_personal"})
            continue

        if cliente.etiqueta is None:
            # Sin clasificar — silencio + alerta dedupada al equipo.
            log.info("webhook_dairo.silencio_sin_clasificar", cliente=msg.from_number)
            from datetime import timedelta as _td
            from sqlalchemy import text as _sa_text
            ventana = datetime.now(timezone.utc) - _td(hours=6)
            ya_hay = (await session.execute(
                _sa_text(
                    "SELECT 1 FROM alertas_fabio WHERE tipo='pide_humano' "
                    "AND cliente_id=:c AND created_at >= :v LIMIT 1"
                ),
                {"c": cliente.id, "v": ventana},
            )).first()
            if not ya_hay:
                await registrar_alerta_fabio(
                    session, tipo="pide_humano",
                    mensaje=(f"[CANAL DAIRO] Número nuevo SIN CLASIFICAR escribió a Dairo: "
                             f"{msg.from_number} ({msg.from_name or '?'}). Preview: "
                             f"{(msg.texto or '')[:160]}. Clasifícalo con `etiquetar_contacto`."),
                    cliente_id=cliente.id,
                )
                from app.notif_equipo import notificar_equipo
                await notificar_equipo(
                    f"🔔 *Número sin clasificar (canal Dairo)*\n\n"
                    f"📱 {msg.from_number}\n"
                    f"👤 {msg.from_name or '?'}\n\n"
                    f"_Preview:_ \"{(msg.texto or '')[:160]}\"\n\n"
                    f"_Clasifícalo en el admin para que el bot le pueda responder._"
                )
            resultados.append({"id": msg.id, "status": "silenced_unclassified"})
            continue

        if cliente.etiqueta == "prospecto":
            para_prospecto.append((cliente.id, msg.from_number, msg))
            resultados.append({"id": msg.id, "status": "queued_prospect"})
            continue

        # etiqueta cliente o equipo sin estar en whitelist → tratar como operativo (defensa).
        # Caso raro porque ambas se etiquetan via la whitelist normalmente.
        if cliente.etiqueta in ("cliente", "equipo"):
            # Fabricar un Miembro virtual para el flujo operativo.
            from app.equipo.directorio import Miembro
            virt = Miembro(
                nombre=cliente.nombre or msg.from_number,
                numero_whatsapp=msg.from_number,
                rol=cliente.etiqueta,
                areas=(), es_fallback=False, activo=True,
            )
            _track_task(asyncio.create_task(_procesar_equipo_async(virt, msg, identidad)))
            resultados.append({"id": msg.id, "status": "etiqueta_routed", "rol": cliente.etiqueta})
            continue

    await session.commit()
    for cliente_id, cliente_numero, msg in para_prospecto:
        _track_task(asyncio.create_task(_procesar_async(cliente_id, cliente_numero, msg, identidad)))

    return {"status": "ok", "identidad": identidad.key, "procesados": resultados}


# Kill switch global: cache de 5s para no hacer query por cada webhook.
# `modo` puede ser:
#   - 'todos'           → bot responde a equipo + whitelist + prospectos (default)
#   - 'solo_prospectos' → solo equipo + prospectos. Whitelist (clientes) silenciado.
#   - 'off'             → solo equipo. Todo lo demás silenciado.
_bot_estado_cache: dict[str, Any] = {"activo": True, "modo": "todos", "checked_at": 0.0}


def invalidar_bot_estado_cache() -> None:
    """Fuerza la próxima lectura a ir a la DB (sin cache).

    Lo llama `admin/actions.toggle_bot` después de actualizar la fila para
    que el cambio surta efecto inmediato y no quede atrapado por el TTL.
    """
    _bot_estado_cache["checked_at"] = 0.0


async def _refrescar_bot_estado() -> tuple[bool, str]:
    """Devuelve (activo, modo) usando cache corto (2s)."""
    import time
    from sqlalchemy import text as sa_text
    now = time.time()
    if now - _bot_estado_cache["checked_at"] < 2.0:
        return _bot_estado_cache["activo"], _bot_estado_cache["modo"]
    try:
        async with async_session_factory() as session:
            row = (await session.execute(sa_text(
                "SELECT activo, COALESCE(modo, 'todos') FROM bot_estado WHERE id=1"
            ))).first()
        activo = bool(row[0]) if row else True
        modo = (row[1] if row and row[1] in ("todos", "solo_prospectos", "off") else "todos")
        _bot_estado_cache["activo"] = activo
        _bot_estado_cache["modo"] = modo
        _bot_estado_cache["checked_at"] = now
        return activo, modo
    except Exception:
        log.exception("webhook.bot_estado_check_fail")
        return True, "todos"  # defensivo: si falla, asumir activo


async def _bot_global_pausado() -> bool:
    """¿Está pausado el bot globalmente para CLIENTES/PROSPECTOS?

    True si modo='off' o si activo=False. El routing del equipo se chequea
    aparte y no se ve afectado.
    """
    activo, modo = await _refrescar_bot_estado()
    return (not activo) or (modo == "off")


async def _bot_bloqueado_para_whitelist() -> bool:
    """True si el modo actual silencia a los clientes de la whitelist."""
    activo, modo = await _refrescar_bot_estado()
    return (not activo) or (modo in ("solo_prospectos", "off"))


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


async def _procesar_async(
    cliente_id: int,
    cliente_numero: str,
    msg: MensajeWhapi,
    identidad: "Identidad | None" = None,
) -> None:
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
                    identidad=identidad,
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


async def _resolver_click_alerta(
    session, msg: MensajeWhapi, miembro,
) -> str | None:
    """Detecta y ejecuta clicks de botones quick-reply en alertas del bot.

    Cuando alguien clickea un botón en una alerta del grupo equipo, WhatsApp
    envía un mensaje normal con el TEXTO del botón ("Prospecto", "Personal",
    etc.) citando el mensaje original con `quoted_message_id`. Si ese quoted
    matchea una `alertas_fabio.whapi_message_id`, sabemos qué cliente acción
    aplicar.

    Devuelve un status string si manejó el click, None si no aplica
    (el flow normal del equipo debe procesarlo).
    """
    if not msg.quoted_message_id:
        return None
    texto = (msg.texto or "").strip().lower()
    if not texto:
        return None

    from sqlalchemy import text as _sa_text
    alerta = (await session.execute(_sa_text("""
        SELECT id, cliente_id, tipo, resuelto
          FROM alertas_fabio
         WHERE whapi_message_id = :wid
         LIMIT 1
    """), {"wid": msg.quoted_message_id})).first()
    if not alerta or alerta.resuelto:
        return None

    # Mapear texto del botón → acción
    accion = None
    if "prospecto" in texto:
        accion = "etiquetar:prospecto"
    elif "personal" in texto:
        accion = "etiquetar:personal"
    elif "cliente" in texto:
        accion = "etiquetar:cliente"
    elif "equipo" in texto:
        accion = "etiquetar:equipo"
    elif "ignorar" in texto:
        accion = "ignorar"
    if not accion:
        return None

    cliente_id = alerta.cliente_id
    autor = miembro.nombre if miembro else "operador"
    log.warning("webhook.click_alerta",
                alerta_id=alerta.id, cliente_id=cliente_id, accion=accion, autor=autor)

    cli_row = None
    if cliente_id:
        from app.db.models import Cliente
        cli_row = (await session.execute(
            select(Cliente).where(Cliente.id == cliente_id)
        )).scalar_one_or_none()

    confirmacion = "✅ Listo."
    try:
        if accion.startswith("etiquetar:") and cli_row:
            valor = accion.split(":", 1)[1]
            await session.execute(
                sa_update(Cliente).where(Cliente.id == cliente_id).values(
                    etiqueta=valor,
                    etiqueta_actualizada_en=datetime.now(timezone.utc),
                    etiqueta_actualizada_por=f"grupo:{autor}",
                )
            )
            confirmacion = (
                f"✅ {cli_row.numero_whatsapp} → *{valor}* "
                f"(por {autor})"
            )
        elif accion == "ignorar" and cli_row:
            # "Ignorar" = marcar como interno (el bot no le vuelve a responder).
            from datetime import timedelta as _td
            await session.execute(_sa_text("""
                INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo)
                VALUES (:n, :nom, :raz, true)
                ON CONFLICT (numero_whatsapp) DO UPDATE
                SET activo = true, razon = EXCLUDED.razon
            """), {
                "n": cli_row.numero_whatsapp,
                "nom": cli_row.nombre or "Marcado desde alerta",
                "raz": f"Ignorado desde alerta por {autor}",
            })
            # Pausar 24h para cortar respuestas pendientes en humanizer
            hasta = datetime.now(timezone.utc) + _td(hours=24)
            await session.execute(_sa_text("""
                INSERT INTO intervencion_humana (cliente_id, pausado_hasta, razon)
                VALUES (:c, :h, :r)
                ON CONFLICT (cliente_id) DO UPDATE
                SET pausado_hasta = EXCLUDED.pausado_hasta, razon = EXCLUDED.razon
            """), {"c": cliente_id, "h": hasta, "r": f"ignorar desde alerta por {autor}"})
            try:
                from app.equipo.directorio import invalidar_cache
                invalidar_cache()
            except Exception:
                pass
            confirmacion = (
                f"✅ {cli_row.numero_whatsapp} → *ignorado* "
                f"(marcado como interno · por {autor})"
            )

        # Cerrar la alerta
        await session.execute(_sa_text("""
            UPDATE alertas_fabio SET resuelto = true, resuelto_en = now()
             WHERE id = :id
        """), {"id": alerta.id})
        await session.commit()
    except Exception as e:
        await session.rollback()
        log.exception("webhook.click_alerta.fail", error=str(e))
        confirmacion = f"⚠️ No se pudo aplicar la acción: {str(e)[:120]}"

    # Confirmar al grupo
    try:
        await enviar_texto(settings.equipo_dtgp_group_id, confirmacion)
    except Exception as e:
        log.warning("webhook.click_alerta.confirm_fail", error=str(e))

    return f"button_click_{accion}"


async def _procesar_equipo_async(
    miembro, msg: MensajeWhapi,
    identidad: "Identidad | None" = None,
    responder_a: str | None = None,
) -> None:
    """Procesa mensaje de un miembro del equipo en background.

    Si `responder_a` se pasa (ej: group_id@g.us), la respuesta va a ese chat
    en lugar del chat personal del miembro.
    """
    async with async_session_factory() as session:
        try:
            await procesar_mensaje_equipo(
                session=session, miembro=miembro, msg=msg,
                identidad=identidad, responder_a=responder_a,
            )
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
