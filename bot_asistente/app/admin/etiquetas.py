"""/admin/etiquetas — CRUD de tags de seguimiento.

Los tags son etiquetas LIBRES que el equipo asigna a clientes para hacer
seguimiento (p.ej. "propuesta enviada", "falta agendar", "reunión hecha").

Son independientes de `cliente.etiqueta` (que clasifica routing del bot:
cliente/prospecto/equipo/personal). Un cliente puede tener N tags.
"""

from __future__ import annotations

import html
import re

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.db.models import ClienteTag, Tag
from app.db.session import get_session
from app.logging_setup import log

router = APIRouter(prefix="/admin/etiquetas", tags=["admin-etiquetas"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


_HEX_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")


def _normalizar_color(raw: str | None) -> str:
    raw = (raw or "").strip()
    if _HEX_RE.match(raw):
        return raw.upper()
    return "#7C3AED"  # morado DTGP por defecto


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def lista_etiquetas(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)

    # Tags + count de clientes por tag
    rows = (await session.execute(sa_text("""
        SELECT t.id, t.nombre, t.color, t.descripcion, t.orden, t.created_at, t.created_by,
               COALESCE((SELECT COUNT(*) FROM cliente_tags ct WHERE ct.tag_id = t.id), 0) AS n_clientes
          FROM tags t
         ORDER BY t.orden ASC, t.nombre ASC
    """))).all()

    items_html: list[str] = []
    for r in rows:
        items_html.append(f"""
        <div class="tag-card" data-tag="{r.id}">
          <div class="tag-card-left">
            <span class="tag-chip" style="background:{r.color};">{html.escape(r.nombre)}</span>
            <span class="tag-count">{r.n_clientes} cliente{'s' if r.n_clientes != 1 else ''}</span>
          </div>
          <div class="tag-desc">{html.escape(r.descripcion or '—')}</div>
          <div class="tag-actions">
            <button class="tag-btn" data-action="edit" data-tag="{r.id}"
                    data-nombre="{html.escape(r.nombre)}"
                    data-color="{r.color}"
                    data-desc="{html.escape(r.descripcion or '')}"
                    data-orden="{r.orden}">Editar</button>
            <button class="tag-btn danger" data-action="delete" data-tag="{r.id}"
                    data-nombre="{html.escape(r.nombre)}"
                    data-n="{r.n_clientes}">Eliminar</button>
          </div>
        </div>
        """)

    sidebar = sidebar_html(active="etiquetas")
    body_html = _TEMPLATE \
        .replace("__SHELL_STYLES__", SHELL_STYLES) \
        .replace("__ICON_SPRITE__", ICON_SPRITE) \
        .replace("__SIDEBAR__", sidebar) \
        .replace("__THEME_JS__", THEME_TOGGLE_JS) \
        .replace("{{items}}", "".join(items_html) or '<div class="empty-state">Aún no hay etiquetas. Crea la primera arriba.</div>') \
        .replace("{{total}}", str(len(rows)))
    return HTMLResponse(body_html)


@router.get("/list.json")
async def lista_etiquetas_json(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """JSON de tags — usado por la lista de chats y el panel para autocomplete."""
    if not _check_auth(request):
        raise HTTPException(401)
    rows = (await session.execute(
        select(Tag).order_by(Tag.orden, Tag.nombre)
    )).scalars().all()
    return JSONResponse({
        "tags": [
            {"id": t.id, "nombre": t.nombre, "color": t.color,
             "descripcion": t.descripcion, "orden": t.orden}
            for t in rows
        ]
    })


@router.post("/crear")
async def crear_etiqueta(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    nombre = (str(form.get("nombre", "")).strip())[:50]
    color = _normalizar_color(str(form.get("color", "")))
    descripcion = (str(form.get("descripcion", "")).strip() or None)
    try:
        orden = int(form.get("orden") or 0)
    except (TypeError, ValueError):
        orden = 0

    if not nombre:
        return RedirectResponse("/admin/etiquetas?err=nombre", status_code=303)

    # ¿Ya existe (case-insensitive)?
    exists = (await session.execute(sa_text(
        "SELECT id FROM tags WHERE LOWER(nombre) = LOWER(:n)"
    ), {"n": nombre})).first()
    if exists:
        return RedirectResponse("/admin/etiquetas?err=dup", status_code=303)

    autor = request.session.get("admin_user", "admin")
    await session.execute(sa_text("""
        INSERT INTO tags (nombre, color, descripcion, orden, created_by)
        VALUES (:n, :c, :d, :o, :a)
    """), {"n": nombre, "c": color, "d": descripcion, "o": orden, "a": autor})
    await session.commit()
    log.info("admin.etiquetas.creada", nombre=nombre, autor=autor)
    return RedirectResponse("/admin/etiquetas?ok=creada", status_code=303)


@router.post("/{tag_id}/actualizar")
async def actualizar_etiqueta(
    tag_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    nombre = (str(form.get("nombre", "")).strip())[:50]
    color = _normalizar_color(str(form.get("color", "")))
    descripcion = (str(form.get("descripcion", "")).strip() or None)
    try:
        orden = int(form.get("orden") or 0)
    except (TypeError, ValueError):
        orden = 0

    if not nombre:
        return RedirectResponse("/admin/etiquetas?err=nombre", status_code=303)

    # ¿Otro tag con ese nombre?
    other = (await session.execute(sa_text(
        "SELECT id FROM tags WHERE LOWER(nombre) = LOWER(:n) AND id <> :id"
    ), {"n": nombre, "id": tag_id})).first()
    if other:
        return RedirectResponse("/admin/etiquetas?err=dup", status_code=303)

    res = await session.execute(sa_text("""
        UPDATE tags SET nombre=:n, color=:c, descripcion=:d, orden=:o
         WHERE id = :id
    """), {"n": nombre, "c": color, "d": descripcion, "o": orden, "id": tag_id})
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(404, "Tag no encontrado")
    log.info("admin.etiquetas.actualizada", tag_id=tag_id, nombre=nombre)
    return RedirectResponse("/admin/etiquetas?ok=actualizada", status_code=303)


@router.post("/{tag_id}/borrar")
async def borrar_etiqueta(
    tag_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    # CASCADE elimina las asignaciones cliente_tags
    res = await session.execute(sa_text("DELETE FROM tags WHERE id = :id"), {"id": tag_id})
    await session.commit()
    if res.rowcount == 0:
        raise HTTPException(404)
    log.warning("admin.etiquetas.borrada", tag_id=tag_id)
    return RedirectResponse("/admin/etiquetas?ok=borrada", status_code=303)


_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Etiquetas — Dairo DTGP</title>
__SHELL_STYLES__
<style>
  .page-title { font-size: 24px; font-weight: 700; color: var(--text-primary); margin: 0 0 4px; }
  .page-subtitle { font-size: 13px; color: var(--text-secondary); margin: 0 0 20px; }
  .flash { padding: 10px 14px; border-radius: 10px; font-size: 13px; margin-bottom: 14px; }
  .flash.ok  { background: rgba(16,185,129,.12); color: #047857; border: 1px solid rgba(16,185,129,.3); }
  .flash.err { background: rgba(239,68,68,.10); color: #b91c1c; border: 1px solid rgba(239,68,68,.3); }

  .crear-card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 18px; margin-bottom: 18px;
    box-shadow: var(--shadow-card);
  }
  .crear-card h2 { margin: 0 0 12px; font-size: 14px; color: var(--text-primary); font-weight: 600; }
  .field-row { display: grid; grid-template-columns: 2fr 1fr 1fr; gap: 10px; margin-bottom: 10px; }
  .field-row.full { grid-template-columns: 1fr; }
  .field-row label { display: block; font-size: 11px; color: var(--text-secondary); margin-bottom: 4px; text-transform: uppercase; letter-spacing: 0.4px; }
  .field-row input, .field-row textarea {
    width: 100%; border: 1px solid var(--border); background: var(--bg-card);
    color: var(--text-primary); border-radius: 8px; padding: 8px 12px;
    font: inherit; font-size: 13px; outline: none; box-sizing: border-box;
  }
  .field-row input:focus, .field-row textarea:focus {
    border-color: var(--chip-purple);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--chip-purple) 15%, transparent);
  }
  .field-row input[type="color"] { height: 34px; padding: 2px; cursor: pointer; }
  .field-row textarea { resize: vertical; min-height: 60px; }
  .btn-row { display: flex; gap: 8px; justify-content: flex-end; margin-top: 6px; }
  .btn-primary {
    background: var(--chip-purple); color: #fff; border: none;
    border-radius: 8px; padding: 8px 16px; font-weight: 500;
    font-size: 13px; cursor: pointer;
  }
  .btn-primary:hover { background: color-mix(in srgb, var(--chip-purple) 88%, black); }
  .btn-secondary {
    background: transparent; color: var(--text-secondary);
    border: 1px solid var(--border); border-radius: 8px;
    padding: 8px 14px; font-size: 13px; cursor: pointer;
  }
  .btn-secondary:hover { background: var(--bg-soft); }

  .tags-list {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden;
    box-shadow: var(--shadow-card);
  }
  .tag-card {
    display: grid; grid-template-columns: 220px 1fr auto;
    gap: 14px; padding: 12px 16px; align-items: center;
    border-bottom: 1px solid var(--border);
  }
  .tag-card:last-child { border-bottom: none; }
  .tag-card-left { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .tag-chip {
    display: inline-flex; align-items: center;
    padding: 4px 10px; border-radius: 999px;
    color: #fff; font-size: 12px; font-weight: 600;
    text-shadow: 0 1px 1px rgba(0,0,0,.1);
  }
  .tag-count { font-size: 11px; color: var(--text-tertiary); }
  .tag-desc { font-size: 13px; color: var(--text-secondary); }
  .tag-actions { display: flex; gap: 6px; flex-shrink: 0; }
  .tag-btn {
    background: transparent; border: 1px solid var(--border);
    color: var(--text-secondary); border-radius: 6px;
    padding: 5px 10px; font-size: 12px; cursor: pointer;
  }
  .tag-btn:hover { background: var(--bg-soft); color: var(--text-primary); border-color: var(--text-tertiary); }
  .tag-btn.danger { color: #ef4444; }
  .tag-btn.danger:hover { background: rgba(239,68,68,.08); border-color: #ef4444; color: #ef4444; }

  .empty-state { text-align: center; padding: 40px 20px; color: var(--text-tertiary); font-size: 14px; }

  /* Modal de edición */
  .modal-overlay {
    position: fixed; inset: 0; background: rgba(0,0,0,.5);
    display: none; align-items: center; justify-content: center;
    z-index: 1000;
  }
  .modal-overlay.open { display: flex; }
  .modal {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 24px;
    width: 100%; max-width: 500px; margin: 20px;
    box-shadow: 0 20px 60px rgba(0,0,0,.3);
  }
  .modal h3 { margin: 0 0 16px; font-size: 16px; color: var(--text-primary); }

  @media (max-width: 700px) {
    .field-row { grid-template-columns: 1fr; }
    .tag-card { grid-template-columns: 1fr; align-items: flex-start; gap: 8px; }
    .tag-actions { width: 100%; }
  }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Etiquetas de seguimiento</h1>
    <p class="page-subtitle">{{total}} etiqueta(s) configurada(s). Úsalas en los chats para marcar fases del embudo (propuesta enviada, reunión hecha, cerrado, etc.).</p>

    <div id="flash-area"></div>

    <div class="crear-card">
      <h2>Nueva etiqueta</h2>
      <form method="POST" action="/admin/etiquetas/crear">
        <div class="field-row">
          <div>
            <label>Nombre</label>
            <input name="nombre" maxlength="50" required placeholder="Ej. Propuesta enviada"/>
          </div>
          <div>
            <label>Color</label>
            <input name="color" type="color" value="#7C3AED"/>
          </div>
          <div>
            <label>Orden (lower = primero)</label>
            <input name="orden" type="number" value="100" min="0" max="9999"/>
          </div>
        </div>
        <div class="field-row full">
          <div>
            <label>Descripción (opcional)</label>
            <textarea name="descripcion" rows="2" placeholder="Explica para qué se usa esta etiqueta"></textarea>
          </div>
        </div>
        <div class="btn-row">
          <button type="submit" class="btn-primary">Crear etiqueta</button>
        </div>
      </form>
    </div>

    <div class="tags-list">
      {{items}}
    </div>
  </main>
</div>

<!-- Modal de edición -->
<div class="modal-overlay" id="modal-edit">
  <div class="modal">
    <h3>Editar etiqueta</h3>
    <form method="POST" id="form-edit">
      <div class="field-row">
        <div>
          <label>Nombre</label>
          <input name="nombre" id="m-nombre" maxlength="50" required/>
        </div>
        <div>
          <label>Color</label>
          <input name="color" type="color" id="m-color"/>
        </div>
        <div>
          <label>Orden</label>
          <input name="orden" type="number" id="m-orden" min="0" max="9999"/>
        </div>
      </div>
      <div class="field-row full">
        <div>
          <label>Descripción</label>
          <textarea name="descripcion" id="m-desc" rows="2"></textarea>
        </div>
      </div>
      <div class="btn-row">
        <button type="button" class="btn-secondary" onclick="cerrarModal()">Cancelar</button>
        <button type="submit" class="btn-primary">Guardar</button>
      </div>
    </form>
  </div>
</div>

__THEME_JS__
<script>
  // Flash messages desde query string
  (function(){
    var p = new URLSearchParams(location.search);
    var flash = document.getElementById('flash-area');
    var mapas = {
      ok: {creada:'Etiqueta creada.', actualizada:'Cambios guardados.', borrada:'Etiqueta eliminada.'},
      err: {nombre:'El nombre es obligatorio.', dup:'Ya existe una etiqueta con ese nombre.'}
    };
    var ok = p.get('ok'), err = p.get('err');
    if (ok && mapas.ok[ok]) flash.innerHTML = '<div class="flash ok">'+mapas.ok[ok]+'</div>';
    else if (err && mapas.err[err]) flash.innerHTML = '<div class="flash err">'+mapas.err[err]+'</div>';
    if (ok || err) setTimeout(function(){ history.replaceState({}, '', '/admin/etiquetas'); flash.innerHTML=''; }, 3500);
  })();

  // Editar
  var modal = document.getElementById('modal-edit');
  var formEdit = document.getElementById('form-edit');
  function abrirEdit(b){
    document.getElementById('m-nombre').value = b.dataset.nombre || '';
    document.getElementById('m-color').value  = b.dataset.color  || '#7C3AED';
    document.getElementById('m-orden').value  = b.dataset.orden  || '0';
    document.getElementById('m-desc').value   = b.dataset.desc   || '';
    formEdit.action = '/admin/etiquetas/' + b.dataset.tag + '/actualizar';
    modal.classList.add('open');
  }
  function cerrarModal(){ modal.classList.remove('open'); }
  modal.addEventListener('click', function(e){ if (e.target === modal) cerrarModal(); });

  document.querySelectorAll('.tag-btn').forEach(function(btn){
    btn.addEventListener('click', function(){
      if (btn.dataset.action === 'edit') return abrirEdit(btn);
      if (btn.dataset.action === 'delete') {
        var nombre = btn.dataset.nombre || '';
        var n = parseInt(btn.dataset.n || '0', 10);
        var msg = '¿Eliminar la etiqueta "' + nombre + '"?';
        if (n > 0) msg += '\\n\\n' + n + ' cliente(s) la tienen aplicada — se quitará de todos.';
        if (!confirm(msg)) return;
        var f = document.createElement('form');
        f.method = 'POST';
        f.action = '/admin/etiquetas/' + btn.dataset.tag + '/borrar';
        document.body.appendChild(f); f.submit();
      }
    });
  });
</script>
</body></html>"""
