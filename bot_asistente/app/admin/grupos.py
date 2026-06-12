"""/admin/grupos — Gestión de grupos de WhatsApp.

- Lista los grupos donde el bot está (cacheados en BD desde whapi).
- Buscar / filtrar por nombre o tags.
- Marcar grupos como activos/inactivos para envío.
- Etiquetar grupos (free-text, separados por comas) — usado por crons.
- Enviar texto/imagen manualmente a un grupo.
- Refrescar la lista contra whapi.
"""

from __future__ import annotations

import html
from datetime import datetime

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.db.session import get_session
from app.grupos import (
    enviar_imagen_a_grupo,
    enviar_texto_a_grupo,
    refrescar_grupos,
)
from app.logging_setup import log

router = APIRouter(prefix="/admin/grupos", tags=["admin-grupos"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _es_ajax(request: Request) -> bool:
    return (
        "application/json" in (request.headers.get("accept") or "")
        or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")
    )


def _fmt(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


# ─── GET vista ────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def vista_grupos(
    request: Request,
    q: str = "",
    filtro: str = "todos",
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)

    # Filtros
    cond = ""
    params: dict = {}
    if q.strip():
        cond += " AND (nombre ILIKE :q OR tags ILIKE :q)"
        params["q"] = f"%{q.strip()}%"
    if filtro == "activos":
        cond += " AND activo = TRUE"
    elif filtro == "inactivos":
        cond += " AND activo = FALSE"
    elif filtro == "admin":
        cond += " AND soy_admin = TRUE"

    rows = (await session.execute(sa_text(f"""
        SELECT id, group_id, nombre, descripcion, participantes_count,
               soy_admin, activo, tags, refrescado_en
        FROM grupos_whatsapp
        WHERE 1=1 {cond}
        ORDER BY activo DESC, participantes_count DESC, nombre
        LIMIT 500
    """), params)).fetchall()

    total = (await session.execute(sa_text(
        "SELECT COUNT(*) FROM grupos_whatsapp"
    ))).scalar_one()
    activos = (await session.execute(sa_text(
        "SELECT COUNT(*) FROM grupos_whatsapp WHERE activo=true"
    ))).scalar_one()
    admin_count = (await session.execute(sa_text(
        "SELECT COUNT(*) FROM grupos_whatsapp WHERE soy_admin=true"
    ))).scalar_one()

    items_html: list[str] = []
    for r in rows:
        id_, gid, nombre, descripcion, n_part, soy_admin, activo, tags, refrescado = r
        nombre_safe = html.escape(nombre or "(sin nombre)")
        gid_safe = html.escape(gid)
        desc_short = html.escape((descripcion or "")[:80])
        tags_safe = html.escape(tags or "")
        admin_chip = '<span class="g-chip admin">ADMIN</span>' if soy_admin else ''
        activo_class = "active" if activo else "inactive"
        toggle_label = "Pausar" if activo else "Activar"

        items_html.append(f"""
        <div class="grupo-card {activo_class}" data-id="{id_}" data-gid="{html.escape(gid)}">
          <div class="g-header">
            <div class="g-info">
              <div class="g-name">{nombre_safe} {admin_chip}</div>
              <div class="g-sub">{n_part or 0} participantes · {html.escape(gid)}</div>
              {f'<div class="g-desc">{desc_short}</div>' if desc_short else ''}
            </div>
            <div class="g-actions">
              <button type="button" class="g-btn primary" data-action="send" title="Enviar mensaje al grupo">Enviar</button>
              <button type="button" class="g-btn" data-action="toggle">{toggle_label}</button>
            </div>
          </div>
          <div class="g-meta">
            <input type="text" class="g-tags-input" data-action="tags" value="{tags_safe}" placeholder="tags (ej: dtgp,clientes)" />
            <span class="g-refreshed">refresh: {_fmt(refrescado)}</span>
          </div>
        </div>""")

    body = _TEMPLATE
    body = body.replace("__SHELL_STYLES__", SHELL_STYLES)
    body = body.replace("__ICON_SPRITE__", ICON_SPRITE)
    body = body.replace("__SIDEBAR__", sidebar_html(active="grupos"))
    body = body.replace("__THEME_JS__", THEME_TOGGLE_JS)
    body = body.replace("{{items}}", "".join(items_html) or '<div class="empty">Sin grupos. Click en "Refrescar" para sincronizar con WhatsApp.</div>')
    body = body.replace("{{total}}", str(total))
    body = body.replace("{{activos}}", str(activos))
    body = body.replace("{{admin}}", str(admin_count))
    body = body.replace("{{q}}", html.escape(q))
    body = body.replace("{{filtro_todos}}", "active" if filtro == "todos" else "")
    body = body.replace("{{filtro_activos}}", "active" if filtro == "activos" else "")
    body = body.replace("{{filtro_inactivos}}", "active" if filtro == "inactivos" else "")
    body = body.replace("{{filtro_admin}}", "active" if filtro == "admin" else "")
    return HTMLResponse(body)


# ─── POST refresh ─────────────────────────────────────────────────────────


@router.post("/refresh")
async def refresh(request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    res = await refrescar_grupos(session)
    await session.commit()
    if _es_ajax(request):
        return res
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/grupos", 303)


# ─── POST toggle activo ──────────────────────────────────────────────────


@router.post("/{grupo_id}/toggle")
async def toggle(grupo_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    await session.execute(sa_text(
        "UPDATE grupos_whatsapp SET activo = NOT activo, updated_at = now() WHERE id = :id"
    ), {"id": grupo_id})
    await session.commit()
    if _es_ajax(request):
        return {"ok": True}
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/grupos", 303)


# ─── POST cambiar tags ───────────────────────────────────────────────────


@router.post("/{grupo_id}/tags")
async def cambiar_tags(grupo_id: int, request: Request, session: AsyncSession = Depends(get_session)):
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    tags = (form.get("tags") or "").strip()[:120] if isinstance(form.get("tags"), str) else ""
    await session.execute(sa_text(
        "UPDATE grupos_whatsapp SET tags = :t, updated_at = now() WHERE id = :id"
    ), {"id": grupo_id, "t": tags or None})
    await session.commit()
    if _es_ajax(request):
        return {"ok": True, "tags": tags}
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/grupos", 303)


# ─── POST send (texto + opcional imagen) ─────────────────────────────────


@router.post("/{grupo_id}/send")
async def enviar(
    grupo_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text(
        "SELECT group_id, nombre FROM grupos_whatsapp WHERE id = :id"
    ), {"id": grupo_id})).first()
    if not row:
        if _es_ajax(request):
            return JSONResponse({"ok": False, "error": "grupo no encontrado"}, 404)
        raise HTTPException(404)
    gid, nombre = row[0], row[1]

    form = await request.form()
    texto = (form.get("mensaje") or "").strip() if isinstance(form.get("mensaje"), str) else ""
    archivos = []
    for k in form.keys():
        if k != "files":
            continue
        for v in form.getlist(k):
            if hasattr(v, "filename") and hasattr(v, "read") and v.filename:
                archivos.append(v)

    if not texto and not archivos:
        if _es_ajax(request):
            return JSONResponse({"ok": False, "error": "vacío"}, 400)
        raise HTTPException(400, "vacío")

    autor = request.session.get("admin_user", "admin")
    errores: list[str] = []
    enviados = 0
    try:
        # Imágenes (cada una como mensaje aparte; caption en la primera)
        for idx, archivo in enumerate(archivos):
            data = await archivo.read()
            if not data:
                continue
            mime = (archivo.content_type or "image/jpeg").lower()
            fname = archivo.filename or "image.jpg"
            caption = texto if (idx == 0 and texto and mime.startswith("image/")) else None
            try:
                if mime.startswith("image/"):
                    await enviar_imagen_a_grupo(gid, data, mime=mime, caption=caption, filename=fname)
                    enviados += 1
                else:
                    # Documents/videos en grupos: por ahora pasa como imagen (todo lo demas se omite)
                    errores.append(f"{fname}: tipo no soportado aún en grupos")
            except Exception as e:
                errores.append(f"{fname}: {e}")
        # Texto suelto si no fue caption de imagen
        envio_texto_aparte = bool(texto) and not (archivos and any(
            (a.content_type or "").startswith("image/") for a in archivos
        ))
        if envio_texto_aparte:
            await enviar_texto_a_grupo(gid, texto)
            enviados += 1
    except Exception as e:
        log.exception("admin.grupos.send_fail", grupo_id=grupo_id, error=str(e))
        if _es_ajax(request):
            return JSONResponse({"ok": False, "error": str(e)[:200]}, 502)
        raise HTTPException(502, str(e)[:200])

    log.warning(
        "admin.grupos.enviado",
        grupo_id=grupo_id, gid=gid, nombre=nombre,
        autor=autor, enviados=enviados, errores=len(errores),
    )
    if _es_ajax(request):
        return {"ok": True, "enviados": enviados, "errores": errores, "nombre": nombre}
    from fastapi.responses import RedirectResponse
    return RedirectResponse("/admin/grupos", 303)


# ─── Template HTML ───────────────────────────────────────────────────────


_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Grupos WhatsApp — Dairo</title>
__SHELL_STYLES__
<style>
  :root {
    --c-purple: #6366F1; --c-purple-hover: #4F46E5;
    --c-purple-soft: #E0E7FF; --c-purple-softer: #EEF2FF;
    --c-text: #0F172A; --c-text-2: #475569; --c-text-3: #94A3B8;
    --c-border: #E5E7EB; --c-border-soft: #F1F5F9;
    --c-card: #FFFFFF; --c-success: #10B981; --c-danger: #EF4444;
  }
  [data-theme="dark"] {
    --c-text: #e2e8f0; --c-text-2: #94a3b8; --c-text-3: #64748b;
    --c-border: #1e293b; --c-border-soft: #1e293b; --c-card: #0f172a;
    --c-purple-soft: #312e81; --c-purple-softer: #1e1b4b;
  }
  .main { padding: 24px 28px; }
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -0.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 18px; }
  .stats-row { display: flex; gap: 18px; margin-bottom: 14px; font-size: 13px; color: var(--c-text-2); }
  .stats-row .stat strong { color: var(--c-text); }

  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .toolbar input[type=text] {
    flex: 1; min-width: 240px; padding: 9px 14px;
    border: 1px solid var(--c-border); border-radius: 10px;
    background: var(--c-card); color: var(--c-text); font: inherit; font-size: 14px;
  }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; }
  .filter-chip {
    padding: 6px 12px; border-radius: 999px; font-size: 12px; font-weight: 500;
    background: var(--c-card); border: 1px solid var(--c-border); color: var(--c-text-2);
    text-decoration: none; cursor: pointer;
  }
  .filter-chip:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .filter-chip.active { background: var(--c-purple); color: #fff; border-color: var(--c-purple); }
  .btn-refresh {
    background: var(--c-purple); color: #fff; border: none;
    padding: 9px 14px; border-radius: 10px; font-size: 13px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .btn-refresh:hover { background: var(--c-purple-hover); }
  .btn-refresh:disabled { opacity: .6; cursor: wait; }

  .grupos-list { display: flex; flex-direction: column; gap: 10px; }
  .grupo-card {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 12px; padding: 14px 16px;
    transition: border-color .12s;
  }
  .grupo-card.inactive { opacity: .55; }
  .grupo-card:hover { border-color: var(--c-purple); }
  .g-header { display: flex; gap: 14px; align-items: flex-start; }
  .g-info { flex: 1; min-width: 0; }
  .g-name { font-weight: 600; font-size: 14.5px; color: var(--c-text); }
  .g-sub { font-size: 12px; color: var(--c-text-3); margin-top: 3px;
           word-break: break-all; }
  .g-desc { font-size: 12px; color: var(--c-text-2); margin-top: 4px;
            overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .g-chip { font-size: 10px; font-weight: 700; padding: 1px 7px; border-radius: 999px;
            background: #FEF3C7; color: #92400E; margin-left: 6px; }
  .g-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .g-btn {
    padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 500;
    border: 1px solid var(--c-border); background: var(--c-card); color: var(--c-text-2);
    cursor: pointer;
  }
  .g-btn:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .g-btn.primary { background: var(--c-purple); color: #fff; border-color: var(--c-purple); }
  .g-btn.primary:hover { background: var(--c-purple-hover); color: #fff; }

  .g-meta { display: flex; gap: 12px; align-items: center; margin-top: 8px;
            padding-top: 8px; border-top: 1px solid var(--c-border-soft); }
  .g-tags-input {
    flex: 1; max-width: 360px; padding: 5px 10px;
    border: 1px solid var(--c-border); border-radius: 6px;
    background: var(--c-card); color: var(--c-text); font: inherit; font-size: 12px;
  }
  .g-refreshed { font-size: 11px; color: var(--c-text-3); margin-left: auto; }

  .empty { padding: 32px; text-align: center; color: var(--c-text-3); font-size: 13px;
           border: 1px dashed var(--c-border); border-radius: 12px; background: var(--c-card); }

  /* Modal de envío */
  .send-modal {
    position: fixed; inset: 0; z-index: 10000;
    background: rgba(0,0,0,.6); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; padding: 20px;
  }
  .send-modal.open { display: flex; }
  .send-card {
    background: var(--c-card); border-radius: 14px;
    width: 100%; max-width: 540px; padding: 22px;
    box-shadow: 0 20px 60px rgba(0,0,0,.4);
  }
  .send-card h3 { margin: 0 0 4px; color: var(--c-text); font-size: 16px; }
  .send-card .send-target { font-size: 12px; color: var(--c-text-2); margin-bottom: 14px; }
  .send-card textarea {
    width: 100%; min-height: 100px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 10px 12px; font: inherit; font-size: 14px;
    background: var(--c-card); color: var(--c-text); box-sizing: border-box;
  }
  .send-card textarea:focus { outline: none; border-color: var(--c-purple); }
  .send-card .file-row { margin-top: 10px; display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .send-card .file-row label {
    padding: 6px 12px; border-radius: 8px; border: 1px solid var(--c-border);
    cursor: pointer; font-size: 12px; color: var(--c-text-2);
  }
  .send-card .file-row .file-list { font-size: 12px; color: var(--c-text-2); }
  .send-card .send-actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }
  .send-card .send-actions button {
    border: none; padding: 9px 16px; border-radius: 10px;
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .send-card .send-actions .btn-cancelar {
    background: var(--c-card); color: var(--c-text-2); border: 1px solid var(--c-border);
  }
  .send-card .send-actions .btn-enviar {
    background: var(--c-purple); color: #fff;
  }
  .send-card .send-actions .btn-enviar:disabled { opacity: .6; cursor: wait; }
  .send-state { font-size: 12px; color: var(--c-text-3); margin-top: 8px; }
  .send-state.ok { color: var(--c-success); }
  .send-state.err { color: var(--c-danger); }

  .toast-stack { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
  .toast { padding: 10px 16px; border-radius: 10px; font-size: 13px; color: #fff;
           background: var(--c-success); box-shadow: 0 4px 12px rgba(0,0,0,.15);
           transition: opacity .3s, transform .3s; min-width: 180px; }
  .toast.error { background: var(--c-danger); }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Grupos de WhatsApp</h1>
    <p class="page-subtitle">Los grupos donde Dairo está. Marca como activos los que quieras usar para envíos automáticos (crons) o manuales.</p>

    <div class="stats-row">
      <span class="stat"><strong>{{total}}</strong> grupos · <strong>{{activos}}</strong> activos · <strong>{{admin}}</strong> donde soy admin</span>
    </div>

    <form class="toolbar" method="GET">
      <input type="text" name="q" value="{{q}}" placeholder="Buscar por nombre o tag…" autocomplete="off"/>
      <div class="filters">
        <a href="?filtro=todos" class="filter-chip {{filtro_todos}}">Todos</a>
        <a href="?filtro=activos" class="filter-chip {{filtro_activos}}">Activos</a>
        <a href="?filtro=inactivos" class="filter-chip {{filtro_inactivos}}">Inactivos</a>
        <a href="?filtro=admin" class="filter-chip {{filtro_admin}}">Soy admin</a>
      </div>
      <button type="button" class="btn-refresh" id="btn-refresh" title="Sincroniza la lista contra WhatsApp">
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>
        Refrescar
      </button>
    </form>

    <div class="grupos-list">
      {{items}}
    </div>
  </main>
</div>

<div class="send-modal" id="send-modal">
  <div class="send-card">
    <h3>Enviar al grupo</h3>
    <div class="send-target" id="send-target">—</div>
    <textarea id="send-text" placeholder="Texto del mensaje (puedes adjuntar imágenes; el texto va como caption de la primera)…"></textarea>
    <div class="file-row">
      <label for="send-files">📎 Adjuntar imagen(es)</label>
      <input type="file" id="send-files" accept="image/*" multiple hidden/>
      <span class="file-list" id="file-list"></span>
    </div>
    <div class="send-actions">
      <button type="button" class="btn-cancelar" id="send-cancelar">Cancelar</button>
      <button type="button" class="btn-enviar" id="send-enviar">Enviar</button>
    </div>
    <div class="send-state" id="send-state"></div>
  </div>
</div>
<div class="toast-stack" id="toast-stack"></div>

__THEME_JS__
<script>
(function(){
  var stack = document.getElementById('toast-stack');
  function toast(msg, err){
    var el = document.createElement('div');
    el.className = 'toast' + (err ? ' error' : '');
    el.textContent = msg; stack.appendChild(el);
    setTimeout(function(){ el.style.opacity='0'; el.style.transform='translateY(20px)'; }, 2400);
    setTimeout(function(){ try { stack.removeChild(el); } catch(e){} }, 2700);
  }

  // ── Refresh ─────────────────────────────────────────────────────
  var btnRefresh = document.getElementById('btn-refresh');
  btnRefresh.addEventListener('click', async function(){
    btnRefresh.disabled = true;
    btnRefresh.textContent = 'Refrescando…';
    try {
      var r = await fetch('/admin/grupos/refresh', {
        method: 'POST',
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        toast('Sincronizado: ' + (d.total_whapi || 0) + ' grupos (' + (d.creados || 0) + ' nuevos)');
        setTimeout(function(){ location.reload(); }, 800);
      } else {
        toast('Error: ' + (d.error || r.status), true);
        btnRefresh.disabled = false;
        btnRefresh.innerHTML = '↻ Refrescar';
      }
    } catch(err) { toast('Error de red', true); btnRefresh.disabled = false; }
  });

  // ── Toggle activo ───────────────────────────────────────────────
  document.querySelectorAll('.g-btn[data-action="toggle"]').forEach(function(btn){
    btn.addEventListener('click', async function(){
      var card = btn.closest('.grupo-card');
      var id = card.dataset.id;
      btn.disabled = true;
      try {
        var r = await fetch('/admin/grupos/' + id + '/toggle', {
          method: 'POST',
          headers: {'Accept':'application/json','X-Requested-With':'fetch'},
        });
        var d = await r.json();
        if (r.ok && d.ok) {
          card.classList.toggle('inactive');
          btn.textContent = card.classList.contains('inactive') ? 'Activar' : 'Pausar';
          toast('Actualizado');
        } else { toast('Error', true); }
      } catch(err) { toast('Error de red', true); }
      btn.disabled = false;
    });
  });

  // ── Cambiar tags (al perder foco) ───────────────────────────────
  document.querySelectorAll('.g-tags-input').forEach(function(input){
    var original = input.value;
    input.addEventListener('blur', async function(){
      if (input.value === original) return;
      var card = input.closest('.grupo-card');
      var id = card.dataset.id;
      try {
        var fd = new FormData(); fd.append('tags', input.value);
        var r = await fetch('/admin/grupos/' + id + '/tags', {
          method: 'POST', body: fd,
          headers: {'Accept':'application/json','X-Requested-With':'fetch'},
        });
        var d = await r.json();
        if (r.ok && d.ok) { original = input.value; toast('Tags guardados'); }
        else { input.value = original; toast('Error', true); }
      } catch(err) { input.value = original; toast('Error de red', true); }
    });
  });

  // ── Modal de envío ──────────────────────────────────────────────
  var modal = document.getElementById('send-modal');
  var sendTarget = document.getElementById('send-target');
  var sendText = document.getElementById('send-text');
  var sendState = document.getElementById('send-state');
  var sendBtn = document.getElementById('send-enviar');
  var sendCancelar = document.getElementById('send-cancelar');
  var sendFiles = document.getElementById('send-files');
  var fileList = document.getElementById('file-list');
  var currentId = null;

  document.querySelectorAll('.g-btn[data-action="send"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var card = btn.closest('.grupo-card');
      currentId = card.dataset.id;
      var name = card.querySelector('.g-name').textContent.trim();
      sendTarget.textContent = name + ' — ' + card.dataset.gid;
      sendText.value = ''; sendFiles.value = ''; fileList.textContent = '';
      sendState.textContent = ''; sendState.className = 'send-state';
      modal.classList.add('open');
      setTimeout(function(){ sendText.focus(); }, 50);
    });
  });
  sendCancelar.addEventListener('click', function(){ modal.classList.remove('open'); });
  modal.addEventListener('click', function(e){ if (e.target === modal) modal.classList.remove('open'); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && modal.classList.contains('open')) modal.classList.remove('open');
  });
  sendFiles.addEventListener('change', function(){
    var names = Array.from(sendFiles.files).map(function(f){ return f.name; });
    fileList.textContent = names.join(', ');
  });
  sendBtn.addEventListener('click', async function(){
    if (!currentId) return;
    var texto = sendText.value.trim();
    if (!texto && sendFiles.files.length === 0) { sendState.textContent = 'Vacío'; sendState.className = 'send-state err'; return; }
    sendBtn.disabled = true;
    sendState.textContent = 'Enviando…';
    sendState.className = 'send-state';
    try {
      var fd = new FormData();
      if (texto) fd.append('mensaje', texto);
      Array.from(sendFiles.files).forEach(function(f){ fd.append('files', f, f.name); });
      var r = await fetch('/admin/grupos/' + currentId + '/send', {
        method: 'POST', body: fd,
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        sendState.textContent = '✓ Enviado (' + (d.enviados || 0) + ' mensajes)';
        sendState.className = 'send-state ok';
        toast('Enviado a ' + d.nombre);
        setTimeout(function(){ modal.classList.remove('open'); }, 1000);
      } else {
        sendState.textContent = 'Error: ' + (d.error || r.status);
        sendState.className = 'send-state err';
        toast('Error al enviar', true);
      }
    } catch(err) {
      sendState.textContent = 'Error de red: ' + err.message;
      sendState.className = 'send-state err';
    }
    sendBtn.disabled = false;
  });
})();
</script>
</body></html>"""
