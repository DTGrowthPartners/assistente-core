"""/admin/automatizaciones — UI para crear/editar/ejecutar tareas programadas."""

from __future__ import annotations

import html as _html
import json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.automatizaciones.acciones import ACCIONES_DISPONIBLES, ejecutar_accion
from app.automatizaciones.scheduler import calcular_proxima
from app.db.session import get_session
from app.logging_setup import log

router = APIRouter(prefix="/admin/automatizaciones", tags=["admin-automatizaciones"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _fmt_dt(dt) -> str:
    if not dt:
        return "—"
    try:
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(dt)[:16]


def _resumen_resultado(r) -> str:
    if not r:
        return "—"
    if isinstance(r, str):
        try:
            r = json.loads(r)
        except Exception:
            return r[:120]
    ok = r.get("ok")
    if r.get("skip"):
        return f"⏭ {r.get('razon', 'skip')}"[:120]
    icono = "✅" if ok else "❌"
    info = r.get("preview") or r.get("error") or json.dumps({k: v for k, v in r.items() if k not in ("preview",)})[:120]
    return f"{icono} {info}"[:200]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def lista(request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    rows = (await session.execute(sa_text(
        """SELECT id, nombre, cron, zona_horaria, accion, parametros, activo,
                  ultima_ejecucion, proxima_ejecucion, ultimo_resultado, creado_por
           FROM tareas_programadas ORDER BY activo DESC, proxima_ejecucion NULLS LAST"""
    ))).fetchall()

    flash = ""
    msg = request.query_params.get("msg")
    if msg == "creada":
        flash = '<div class="flash">Tarea creada.</div>'
    elif msg == "editada":
        flash = '<div class="flash">Tarea editada.</div>'
    elif msg == "ejecutada":
        flash = '<div class="flash">Tarea ejecutada ahora.</div>'
    elif msg == "borrada":
        flash = '<div class="flash">Tarea eliminada.</div>'
    elif msg == "toggle":
        flash = '<div class="flash">Estado actualizado.</div>'

    items_html: list[str] = []
    for r in rows:
        (tid, nombre, cron, tz, accion, params, activo, ultima, proxima, ultimo_res, por) = r
        estado_pill = '<span class="pill on">Activa</span>' if activo else '<span class="pill off">Pausada</span>'
        toggle_label = "Pausar" if activo else "Reactivar"
        params_pretty = json.dumps(params or {}, ensure_ascii=False)[:120]
        items_html.append(f"""
        <div class="auto-item">
          <div class="auto-body">
            <div class="auto-top">
              <span class="auto-nombre">{_html.escape(nombre)} {estado_pill}</span>
              <span class="auto-cron"><code>{_html.escape(cron)}</code> · {_html.escape(tz)}</span>
            </div>
            <div class="auto-meta">
              <span class="badge">acción: <code>{_html.escape(accion)}</code></span>
              <span>Próx: {_fmt_dt(proxima)}</span>
              <span>Últ: {_fmt_dt(ultima)}</span>
            </div>
            <div class="auto-params">params: <code>{_html.escape(params_pretty)}</code></div>
            <div class="auto-resultado">{_html.escape(_resumen_resultado(ultimo_res))}</div>
          </div>
          <div class="auto-actions">
            <form method="POST" action="/admin/automatizaciones/{tid}/ejecutar" style="margin:0;display:inline;">
              <button class="btn-ghost" title="Ejecutar ahora (no afecta cron)">▶ Ejecutar</button>
            </form>
            <form method="POST" action="/admin/automatizaciones/{tid}/toggle" style="margin:0;display:inline;">
              <button class="btn-ghost">{toggle_label}</button>
            </form>
            <a href="/admin/automatizaciones/{tid}/editar" class="btn-ghost">Editar</a>
            <form method="POST" action="/admin/automatizaciones/{tid}/borrar" style="margin:0;display:inline;"
                  onsubmit="return confirm('¿Borrar la tarea \\'{_html.escape(nombre)}\\'?');">
              <button class="btn-danger">×</button>
            </form>
          </div>
        </div>""")

    # Acciones disponibles para el dropdown
    options_acc = "".join(
        f'<option value="{a}">{a} — {meta["descripcion"]}</option>'
        for a, meta in ACCIONES_DISPONIBLES.items()
    )

    # Cron presets
    presets = [
        ("0 19 * * *", "Diario 7:00 PM"),
        ("0 9 * * *", "Diario 9:00 AM"),
        ("0 9 * * 1", "Lunes 9:00 AM (semanal)"),
        ("0 19 * * 5", "Viernes 7:00 PM"),
        ("0 */3 8-20 * * *", "Cada 3h en horario hábil"),
    ]
    presets_html = "".join(f'<option value="{c}">{lbl} — <code>{c}</code></option>' for c, lbl in presets)

    html_resp = _TEMPLATE_LISTA \
        .replace("__SHELL_STYLES__", SHELL_STYLES) \
        .replace("__EXTRA_STYLES__", _EXTRA_STYLES) \
        .replace("__ICON_SPRITE__", ICON_SPRITE) \
        .replace("__SIDEBAR__", sidebar_html(active="automatizaciones")) \
        .replace("__THEME_JS__", THEME_TOGGLE_JS) \
        .replace("{{items}}", "".join(items_html) or '<p class="empty">No hay tareas. Crea la primera arriba.</p>') \
        .replace("{{opciones_accion}}", options_acc) \
        .replace("{{presets_cron}}", presets_html) \
        .replace("{{flash}}", flash) \
        .replace("{{total}}", str(len(rows)))
    return HTMLResponse(html_resp)


@router.post("/crear")
async def crear(
    request: Request,
    nombre: str = Form(...),
    cron: str = Form(...),
    accion: str = Form(...),
    parametros_json: str = Form("{}"),
    zona_horaria: str = Form("America/Bogota"),
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    if accion not in ACCIONES_DISPONIBLES:
        return RedirectResponse(f"/admin/automatizaciones?msg=accion_invalida", 303)
    try:
        params = json.loads(parametros_json or "{}")
    except Exception:
        return RedirectResponse(f"/admin/automatizaciones?msg=json_invalido", 303)
    proxima = calcular_proxima(cron, zona_horaria)
    if not proxima:
        return RedirectResponse(f"/admin/automatizaciones?msg=cron_invalido", 303)

    creador = request.session.get("admin_user", "admin")
    await session.execute(sa_text(
        """INSERT INTO tareas_programadas (nombre, cron, zona_horaria, accion, parametros, proxima_ejecucion, creado_por)
           VALUES (:n, :c, :z, :a, :p::jsonb, :px, :u)"""
    ), {"n": nombre, "c": cron, "z": zona_horaria, "a": accion,
        "p": json.dumps(params), "px": proxima, "u": creador})
    await session.commit()
    log.info("admin.automatizacion.creada", nombre=nombre, accion=accion, por=creador)
    return RedirectResponse(f"/admin/automatizaciones?msg=creada", 303)


@router.get("/{tid}/editar", response_class=HTMLResponse)
async def editar_form(tid: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text(
        "SELECT nombre, cron, zona_horaria, accion, parametros FROM tareas_programadas WHERE id=:i"
    ), {"i": tid})).first()
    if not row:
        return RedirectResponse("/admin/automatizaciones?msg=no_existe", 303)

    options_acc = "".join(
        f'<option value="{a}"{" selected" if a==row[3] else ""}>{a} — {meta["descripcion"]}</option>'
        for a, meta in ACCIONES_DISPONIBLES.items()
    )
    params_pretty = json.dumps(row[4] or {}, ensure_ascii=False, indent=2)

    html_resp = _TEMPLATE_EDITAR \
        .replace("__SHELL_STYLES__", SHELL_STYLES) \
        .replace("__EXTRA_STYLES__", _EXTRA_STYLES) \
        .replace("__ICON_SPRITE__", ICON_SPRITE) \
        .replace("__SIDEBAR__", sidebar_html(active="automatizaciones")) \
        .replace("__THEME_JS__", THEME_TOGGLE_JS) \
        .replace("{{tid}}", str(tid)) \
        .replace("{{nombre}}", _html.escape(row[0])) \
        .replace("{{cron}}", _html.escape(row[1])) \
        .replace("{{tz}}", _html.escape(row[2])) \
        .replace("{{opciones_accion}}", options_acc) \
        .replace("{{params_json}}", _html.escape(params_pretty))
    return HTMLResponse(html_resp)


@router.post("/{tid}/editar")
async def editar_post(
    tid: int, request: Request,
    nombre: str = Form(...), cron: str = Form(...), accion: str = Form(...),
    parametros_json: str = Form("{}"), zona_horaria: str = Form("America/Bogota"),
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    try:
        params = json.loads(parametros_json or "{}")
    except Exception:
        return RedirectResponse(f"/admin/automatizaciones/{tid}/editar?msg=json_invalido", 303)
    proxima = calcular_proxima(cron, zona_horaria)
    if not proxima:
        return RedirectResponse(f"/admin/automatizaciones/{tid}/editar?msg=cron_invalido", 303)
    await session.execute(sa_text(
        """UPDATE tareas_programadas
           SET nombre=:n, cron=:c, zona_horaria=:z, accion=:a, parametros=:p::jsonb,
               proxima_ejecucion=:px, updated_at=now()
           WHERE id=:i"""
    ), {"n": nombre, "c": cron, "z": zona_horaria, "a": accion,
        "p": json.dumps(params), "px": proxima, "i": tid})
    await session.commit()
    log.info("admin.automatizacion.editada", id=tid)
    return RedirectResponse("/admin/automatizaciones?msg=editada", 303)


@router.post("/{tid}/toggle")
async def toggle(tid: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text("SELECT activo, cron, zona_horaria FROM tareas_programadas WHERE id=:i"), {"i": tid})).first()
    if not row:
        return RedirectResponse("/admin/automatizaciones", 303)
    nuevo = not row[0]
    proxima = calcular_proxima(row[1], row[2]) if nuevo else None
    await session.execute(sa_text(
        "UPDATE tareas_programadas SET activo=:a, proxima_ejecucion=:p, updated_at=now() WHERE id=:i"
    ), {"a": nuevo, "p": proxima, "i": tid})
    await session.commit()
    return RedirectResponse("/admin/automatizaciones?msg=toggle", 303)


@router.post("/{tid}/borrar")
async def borrar(tid: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    await session.execute(sa_text("DELETE FROM tareas_programadas WHERE id=:i"), {"i": tid})
    await session.commit()
    return RedirectResponse("/admin/automatizaciones?msg=borrada", 303)


@router.post("/{tid}/ejecutar")
async def ejecutar_ahora(tid: int, request: Request, session: AsyncSession = Depends(get_session)):
    """Dispara la tarea inmediatamente sin afectar el cron normal."""
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text(
        "SELECT accion, parametros FROM tareas_programadas WHERE id=:i"
    ), {"i": tid})).first()
    if not row:
        return RedirectResponse("/admin/automatizaciones", 303)
    resultado = await ejecutar_accion(row[0], session, row[1] or {})
    await session.execute(sa_text(
        """UPDATE tareas_programadas
           SET ultima_ejecucion=now(), ultimo_resultado=:r::jsonb, updated_at=now()
           WHERE id=:i"""
    ), {"r": json.dumps(resultado, default=str), "i": tid})
    await session.commit()
    return RedirectResponse("/admin/automatizaciones?msg=ejecutada", 303)


# ────────────────────────────────────────────────────────────────────────────
# Templates HTML
# ────────────────────────────────────────────────────────────────────────────


_EXTRA_STYLES = """
<style>
  .page-title { font-size: 22px; font-weight: 600; margin: 0 0 4px; color: var(--text-primary); }
  .page-subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
  .flash { background: var(--accent-positive-bg); color: var(--accent-positive);
           border: 1px solid var(--accent-positive); padding: 8px 12px;
           border-radius: 8px; font-size: 13px; margin-bottom: 14px; }
  .crear-card { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
                padding: 18px; margin-bottom: 18px; box-shadow: var(--shadow-card); }
  .crear-card h3 { margin: 0 0 12px; font-size: 14px; color: var(--text-primary); }
  .field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 10px; }
  .field-row.full { grid-template-columns: 1fr; }
  .field-row label { display: block; font-size: 12px; color: var(--text-secondary); margin-bottom: 4px; }
  .field-row input, .field-row select, .field-row textarea {
    width: 100%; padding: 8px 10px; border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg-card); color: var(--text-primary); font: inherit; font-size: 13px;
    box-sizing: border-box;
  }
  .field-row textarea { min-height: 80px; font-family: ui-monospace, monospace; font-size: 12px; }
  .hint { font-size: 11px; color: var(--text-tertiary); margin-top: 4px; }
  .btn-primary { background: var(--btn-primary-bg); color: var(--btn-primary-text); border: none;
                 padding: 9px 16px; border-radius: 8px; font: inherit; font-weight: 600;
                 font-size: 13px; cursor: pointer; }
  .btn-ghost { background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border);
               padding: 5px 10px; border-radius: 6px; font: inherit; font-size: 12px;
               cursor: pointer; text-decoration: none; display: inline-block; }
  .btn-danger { background: var(--accent-negative-bg); color: var(--accent-negative); border: none;
                width: 28px; height: 28px; border-radius: 6px; cursor: pointer; font-size: 16px;
                line-height: 1; }
  .auto-list { display: flex; flex-direction: column; gap: 10px; }
  .auto-item { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px;
               padding: 14px 16px; display: flex; gap: 14px; align-items: flex-start;
               box-shadow: var(--shadow-card); }
  .auto-body { flex: 1; min-width: 0; }
  .auto-top { display: flex; justify-content: space-between; gap: 12px; margin-bottom: 6px; flex-wrap: wrap; }
  .auto-nombre { font-weight: 600; color: var(--text-primary); font-size: 14px; }
  .auto-cron { font-size: 12px; color: var(--text-secondary); }
  .auto-meta { display: flex; gap: 14px; font-size: 12px; color: var(--text-secondary); flex-wrap: wrap; margin-bottom: 4px; }
  .auto-params { font-size: 11px; color: var(--text-tertiary); margin: 4px 0; word-break: break-all; }
  .auto-resultado { font-size: 12px; color: var(--text-secondary); margin-top: 4px; }
  .auto-actions { display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
  .pill { font-size: 10px; padding: 2px 8px; border-radius: 999px; font-weight: 600; margin-left: 6px; }
  .pill.on { background: var(--accent-positive-bg); color: var(--accent-positive); }
  .pill.off { background: var(--bg-soft); color: var(--text-tertiary); }
  .badge { background: var(--bg-soft); padding: 2px 8px; border-radius: 6px; font-size: 11px; }
  code { background: var(--bg-soft); padding: 1px 6px; border-radius: 4px; font-size: 11.5px; }
  .empty { padding: 28px; text-align: center; color: var(--text-tertiary); font-size: 13px;
           background: var(--bg-card); border: 1px dashed var(--border); border-radius: 12px; }
</style>
"""


_TEMPLATE_LISTA = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Automatizaciones — Laura</title>
__SHELL_STYLES__
__EXTRA_STYLES__
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Automatizaciones</h1>
    <p class="page-subtitle">{{total}} tareas. El scheduler corre cada 60s.</p>
    {{flash}}

    <div class="crear-card">
      <h3>+ Nueva tarea</h3>
      <form method="POST" action="/admin/automatizaciones/crear">
        <div class="field-row">
          <div>
            <label>Nombre</label>
            <input name="nombre" required placeholder="Ej: Reporte ventas diario"/>
          </div>
          <div>
            <label>Acción</label>
            <select name="accion" required>{{opciones_accion}}</select>
          </div>
        </div>
        <div class="field-row">
          <div>
            <label>Cron (5 campos: min hora dia_mes mes dia_semana)</label>
            <input name="cron" required placeholder="0 19 * * *" list="preset-cron"/>
            <datalist id="preset-cron">{{presets_cron}}</datalist>
            <div class="hint">Ejemplos: <code>0 19 * * *</code>=diario 7pm, <code>0 9 * * 1</code>=lunes 9am, <code>*/30 * * * *</code>=cada 30min</div>
          </div>
          <div>
            <label>Zona horaria</label>
            <input name="zona_horaria" value="America/Bogota"/>
          </div>
        </div>
        <div class="field-row full">
          <div>
            <label>Parámetros (JSON)</label>
            <textarea name="parametros_json" placeholder='{"destino_tipo":"grupo","destino_id":"120363425539154194","periodo":"hoy","top_n":3}'>{}</textarea>
            <div class="hint">Cada acción acepta parámetros distintos. Ver descripciones en el dropdown.</div>
          </div>
        </div>
        <button type="submit" class="btn-primary">Crear tarea</button>
      </form>
    </div>

    <div class="auto-list">
      {{items}}
    </div>
  </main>
</div>
__THEME_JS__
</body></html>"""


_TEMPLATE_EDITAR = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Editar tarea — Laura</title>
__SHELL_STYLES__
__EXTRA_STYLES__
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <div style="margin-bottom: 14px;"><a href="/admin/automatizaciones" class="btn-ghost">← Atrás</a></div>
    <h1 class="page-title">Editar tarea #{{tid}}</h1>

    <div class="crear-card">
      <form method="POST" action="/admin/automatizaciones/{{tid}}/editar">
        <div class="field-row">
          <div>
            <label>Nombre</label>
            <input name="nombre" required value="{{nombre}}"/>
          </div>
          <div>
            <label>Acción</label>
            <select name="accion" required>{{opciones_accion}}</select>
          </div>
        </div>
        <div class="field-row">
          <div>
            <label>Cron</label>
            <input name="cron" required value="{{cron}}"/>
          </div>
          <div>
            <label>Zona horaria</label>
            <input name="zona_horaria" value="{{tz}}"/>
          </div>
        </div>
        <div class="field-row full">
          <div>
            <label>Parámetros (JSON)</label>
            <textarea name="parametros_json" style="min-height:160px;">{{params_json}}</textarea>
          </div>
        </div>
        <button type="submit" class="btn-primary">Guardar cambios</button>
      </form>
    </div>
  </main>
</div>
__THEME_JS__
</body></html>"""
