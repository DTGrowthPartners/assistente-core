"""/admin/stories — publica estados de WhatsApp (texto / imagen / producto).

Whapi soporta:
- /stories/send/text con caption (JSON)
- /stories/send/media con multipart (subir archivo) o data URL base64

Esta vista permite al admin:
- Publicar texto plano (promociones, avisos)
- Publicar imagen subiendo archivo desde el browser
- Publicar imagen de un producto del catálogo eligiendo ref
- Publicar imagen del banco (usa las imágenes locales del bot)

Historial: las últimas publicaciones quedan en BD (tabla `story_publicado`).
"""

from __future__ import annotations

import html
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, select
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.db.models import ProductoCache
from app.db.session import get_session
from app.logging_setup import log
from app.whapi.client import (
    eliminar_mensaje,
    publicar_story_imagen_bytes,
    publicar_story_imagen_url,
    publicar_story_texto,
)

router = APIRouter(prefix="/admin/stories", tags=["admin-stories"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


async def _ensure_tabla(session: AsyncSession) -> None:
    """Crea la tabla si no existe (no usamos Alembic para esto, es opcional)."""
    await session.execute(sa_text("""
        CREATE TABLE IF NOT EXISTS story_publicado (
            id SERIAL PRIMARY KEY,
            tipo VARCHAR(20) NOT NULL,
            caption TEXT,
            imagen_url TEXT,
            ref_producto VARCHAR(40),
            whapi_message_id VARCHAR(120),
            publicado_por VARCHAR(60),
            publicado_en TIMESTAMPTZ NOT NULL DEFAULT now(),
            origen VARCHAR(20) NOT NULL DEFAULT 'manual',
            metadata JSONB DEFAULT '{}'::jsonb
        )
    """))
    await session.execute(sa_text(
        "CREATE INDEX IF NOT EXISTS ix_story_publicado_publicado_en "
        "ON story_publicado (publicado_en DESC)"
    ))


# ─── Vista lista + form ──────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def vista_stories(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)

    await _ensure_tabla(session)
    await session.commit()

    # Historial
    rows = (await session.execute(sa_text("""
        SELECT id, tipo, caption, imagen_url, ref_producto, publicado_por,
               publicado_en, origen
        FROM story_publicado
        ORDER BY publicado_en DESC LIMIT 30
    """))).fetchall()

    # Productos con imagen (para selector "publicar producto")
    prods = (await session.execute(
        select(ProductoCache)
        .where(ProductoCache.activo.is_(True))
        .where(ProductoCache.imagen_url.is_not(None))
        .order_by(ProductoCache.nombre)
        .limit(80)
    )).scalars().all()

    flash = ""
    if request.query_params.get("msg") == "ok":
        flash = '<div class="flash">Estado publicado en WhatsApp.</div>'
    elif request.query_params.get("msg") == "fail":
        razon = html.escape(request.query_params.get("err", "Error desconocido"))
        flash = f'<div class="flash" style="background:var(--accent-negative-bg);color:var(--accent-negative);border-color:var(--accent-negative);">Falló: {razon}</div>'

    # Items del histórico
    items_html: list[str] = []
    for r in rows:
        id_, tipo, caption, imagen_url, ref, por, ts, origen = r
        cap_short = (caption or "")[:120]
        thumb = ""
        if imagen_url:
            thumb = f'<img src="{html.escape(imagen_url)}" class="story-thumb" loading="lazy"/>'
        ref_html = f' <span class="badge badge-blue">{html.escape(ref)}</span>' if ref else ''
        origen_html = f'<span class="badge badge-blue">{html.escape(origen)}</span>'
        ts_str = ts.astimezone(_bog()).strftime("%Y-%m-%d %H:%M") if ts else ""
        items_html.append(f"""
        <div class="story-item">
          {thumb}
          <div class="story-body">
            <div class="story-top">
              <span class="story-tipo">{html.escape(tipo)}{ref_html}</span>
              <span class="story-ts">{ts_str} · {origen_html}</span>
            </div>
            <div class="story-caption">{html.escape(cap_short)}</div>
            <div class="story-meta">por {html.escape(por or '-')}</div>
          </div>
          <form method="POST" action="/admin/stories/eliminar/{id_}" class="story-del-form"
                onsubmit="return confirm('¿Eliminar este estado? Se borra también del WhatsApp si aún está vigente.');">
            <button type="submit" class="btn-del" title="Eliminar">×</button>
          </form>
        </div>""")
    historial_html = "".join(items_html) or '<div class="empty">Aún no has publicado ningún estado.</div>'

    # Opciones de producto
    options_prod = "".join(
        f'<option value="{p.ref}">{html.escape(p.ref)} — {html.escape((p.nombre or "")[:60])} '
        f'(${int(p.precio_detal):,})</option>'.replace(",", ".")
        if p.precio_detal else f'<option value="{p.ref}">{html.escape(p.ref)} — {html.escape((p.nombre or "")[:60])}</option>'
        for p in prods
    )

    page = (_TEMPLATE
            .replace("__SHELL_STYLES__", SHELL_STYLES)
            .replace("__ICON_SPRITE__", ICON_SPRITE)
            .replace("__SIDEBAR__", sidebar_html(active="stories"))
            .replace("__THEME_JS__", THEME_TOGGLE_JS)
            .replace("{{flash}}", flash)
            .replace("{{options_producto}}", options_prod)
            .replace("{{historial}}", historial_html))
    return HTMLResponse(page)


def _bog():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Bogota")
    except Exception:
        return timezone.utc


# ─── Acciones POST ───────────────────────────────────────────────────────────


@router.post("/publicar/texto")
async def publicar_texto(
    request: Request,
    caption: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    caption = (caption or "").strip()
    if not caption:
        return RedirectResponse("/admin/stories?msg=fail&err=caption+vacio", 303)
    try:
        resp = await publicar_story_texto(caption)
    except Exception as e:
        log.exception("admin.story.texto.fail", error=str(e))
        return RedirectResponse(f"/admin/stories?msg=fail&err={html.escape(str(e)[:120])}", 303)
    msg_id = (resp.get("message") or {}).get("id")
    await _registrar(session, tipo="texto", caption=caption, msg_id=msg_id,
                    por=request.session.get("admin_user") or "admin", origen="manual")
    await session.commit()
    return RedirectResponse("/admin/stories?msg=ok", 303)


@router.post("/publicar/imagen")
async def publicar_imagen(
    request: Request,
    archivo: UploadFile = File(None),
    caption: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    if not archivo:
        return RedirectResponse("/admin/stories?msg=fail&err=sin+archivo", 303)
    data = await archivo.read()
    if not data:
        return RedirectResponse("/admin/stories?msg=fail&err=archivo+vacio", 303)
    mime = archivo.content_type or "image/jpeg"
    cap = (caption or "").strip() or None
    try:
        resp = await publicar_story_imagen_bytes(data, caption=cap, filename=archivo.filename or "story.jpg", mime=mime)
    except Exception as e:
        log.exception("admin.story.imagen.fail", error=str(e))
        return RedirectResponse(f"/admin/stories?msg=fail&err={html.escape(str(e)[:120])}", 303)
    msg_id = (resp.get("message") or {}).get("id")
    await _registrar(session, tipo="imagen", caption=cap, msg_id=msg_id,
                    por=request.session.get("admin_user") or "admin", origen="manual_archivo")
    await session.commit()
    return RedirectResponse("/admin/stories?msg=ok", 303)


@router.post("/publicar/producto")
async def publicar_producto(
    request: Request,
    ref: str = Form(...),
    caption_extra: str = Form(""),
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    ref = (ref or "").strip().upper()
    prod = (await session.execute(
        select(ProductoCache).where(ProductoCache.ref == ref)
    )).scalar_one_or_none()
    if not prod or not prod.imagen_url:
        return RedirectResponse("/admin/stories?msg=fail&err=producto+sin+imagen", 303)
    precio = f" — ${int(prod.precio_detal):,}".replace(",", ".") if prod.precio_detal else ""
    base = f"{prod.nombre} ({prod.ref}){precio}"
    cap = f"{base}\n\n{caption_extra}".strip() if caption_extra else base
    try:
        resp = await publicar_story_imagen_url(prod.imagen_url, caption=cap)
    except Exception as e:
        log.exception("admin.story.producto.fail", ref=ref, error=str(e))
        return RedirectResponse(f"/admin/stories?msg=fail&err={html.escape(str(e)[:120])}", 303)
    msg_id = (resp.get("message") or {}).get("id")
    await _registrar(session, tipo="imagen", caption=cap, imagen_url=prod.imagen_url,
                    ref=ref, msg_id=msg_id,
                    por=request.session.get("admin_user") or "admin", origen="manual_producto")
    await session.commit()
    return RedirectResponse("/admin/stories?msg=ok", 303)


@router.post("/eliminar/{story_id}")
async def eliminar_story(
    story_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text(
        "SELECT whapi_message_id FROM story_publicado WHERE id = :i"
    ), {"i": story_id})).first()
    if not row:
        return RedirectResponse("/admin/stories?msg=fail&err=story+no+encontrado", 303)
    whapi_id = row[0]
    whapi_msg = ""
    # Intentar borrar de whapi (puede fallar si ya expiró o el id no existe — no es crítico)
    if whapi_id:
        try:
            await eliminar_mensaje(whapi_id)
        except Exception as e:
            whapi_msg = "+(no+se+pudo+borrar+de+WA+—+puede+haber+expirado)"
            log.warning("admin.story.eliminar.whapi_fail", id=story_id, error=str(e))
    # Borrar del histórico BD
    await session.execute(sa_text("DELETE FROM story_publicado WHERE id = :i"), {"i": story_id})
    await session.commit()
    log.info("admin.story.eliminada", id=story_id, whapi_id=whapi_id)
    return RedirectResponse(f"/admin/stories?msg=ok{whapi_msg}", 303)


async def _registrar(
    session: AsyncSession,
    *,
    tipo: str,
    caption: str | None = None,
    imagen_url: str | None = None,
    ref: str | None = None,
    msg_id: str | None = None,
    por: str | None = None,
    origen: str = "manual",
) -> None:
    await session.execute(sa_text("""
        INSERT INTO story_publicado (tipo, caption, imagen_url, ref_producto,
                                     whapi_message_id, publicado_por, origen)
        VALUES (:tipo, :cap, :iurl, :ref, :mid, :por, :origen)
    """), {"tipo": tipo, "cap": caption, "iurl": imagen_url, "ref": ref,
           "mid": msg_id, "por": por, "origen": origen})


# ─── Template HTML ───────────────────────────────────────────────────────────


_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estados WA — Laura</title>
__SHELL_STYLES__
<style>
  .page-title { font-size: 22px; font-weight: 600; margin: 0 0 4px; color: var(--text-primary); }
  .page-subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }
  .flash { background: var(--accent-positive-bg); color: var(--accent-positive);
           border: 1px solid var(--accent-positive); padding: 10px 14px;
           border-radius: 8px; margin-bottom: 16px; font-size: 13px; }
  .tabs { display: flex; gap: 6px; margin-bottom: 14px; border-bottom: 1px solid var(--border); }
  .tab { padding: 8px 16px; background: transparent; border: none; cursor: pointer;
         color: var(--text-secondary); font-weight: 500; font-size: 13px;
         border-bottom: 2px solid transparent; }
  .tab.active { color: var(--text-primary); border-bottom-color: var(--text-primary); }
  .tab-pane { display: none; }
  .tab-pane.active { display: block; }
  .form-card { background: var(--bg-card); border: 1px solid var(--border);
               border-radius: 12px; padding: 18px; box-shadow: var(--shadow-card); }
  .form-card label { display: block; font-size: 12px; font-weight: 600;
                     color: var(--text-secondary); margin: 12px 0 6px; text-transform: uppercase; letter-spacing: .3px; }
  .form-card textarea, .form-card input[type=text], .form-card select, .form-card input[type=file] {
    width: 100%; padding: 9px 12px; font: inherit; font-size: 14px;
    border: 1px solid var(--border); border-radius: 8px;
    background: var(--bg-card); color: var(--text-primary);
    box-sizing: border-box;
  }
  .form-card textarea { min-height: 80px; resize: vertical; }
  .form-card .btn-publicar {
    margin-top: 14px; padding: 9px 18px;
    background: var(--btn-primary-bg); color: var(--btn-primary-text);
    border: none; border-radius: 8px; font: inherit; font-size: 13px; font-weight: 600;
    cursor: pointer;
  }
  .form-card .hint { font-size: 11px; color: var(--text-tertiary); margin-top: 4px; }

  h2.section-title { font-size: 16px; font-weight: 600; margin: 24px 0 12px; color: var(--text-primary); }
  .story-item {
    display: flex; gap: 14px; background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px; margin-bottom: 10px; box-shadow: var(--shadow-card);
  }
  .story-thumb { width: 64px; height: 64px; border-radius: 8px; object-fit: cover; flex-shrink: 0; background: var(--bg-soft); }
  .story-body { flex: 1; min-width: 0; }
  .story-top { display: flex; justify-content: space-between; gap: 8px; flex-wrap: wrap; }
  .story-tipo { font-weight: 600; font-size: 13px; color: var(--text-primary); }
  .story-ts { font-size: 11px; color: var(--text-tertiary); }
  .story-caption { font-size: 13px; color: var(--text-secondary); margin: 4px 0; line-height: 1.4; white-space: pre-wrap; }
  .story-meta { font-size: 11px; color: var(--text-tertiary); }
  .badge-blue { background: var(--chip-blue-bg); color: var(--chip-blue);
                font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
  .empty { padding: 28px; text-align: center; color: var(--text-tertiary); font-size: 13px;
           background: var(--bg-card); border: 1px dashed var(--border); border-radius: 12px; }
  .story-del-form { margin: 0; }
  .btn-del {
    width: 28px; height: 28px; border-radius: 6px;
    background: var(--accent-negative-bg); color: var(--accent-negative);
    border: 1px solid transparent; cursor: pointer; font-size: 18px; line-height: 1;
    padding: 0; display: grid; place-items: center;
    flex-shrink: 0; align-self: flex-start;
  }
  .btn-del:hover { background: var(--accent-negative); color: #fff; }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Estados de WhatsApp</h1>
    <p class="page-subtitle">Publica estados (stories) que tus contactos verán por 24 horas.</p>
    {{flash}}

    <div class="tabs">
      <button class="tab active" data-tab="prod">📦 Desde producto</button>
      <button class="tab" data-tab="img">🖼️ Imagen propia</button>
      <button class="tab" data-tab="txt">📝 Solo texto</button>
    </div>

    <div class="tab-pane active" id="pane-prod">
      <form class="form-card" method="POST" action="/admin/stories/publicar/producto">
        <label>Producto del catálogo</label>
        <select name="ref" required>
          <option value="">— elige una referencia —</option>
          {{options_producto}}
        </select>
        <div class="hint">Se usa la imagen y el caption se arma automáticamente con nombre + ref + precio. Puedes añadir texto extra abajo.</div>
        <label>Texto extra (opcional)</label>
        <textarea name="caption_extra" placeholder="Ej: ¡Promo solo hoy!"></textarea>
        <button type="submit" class="btn-publicar">Publicar estado</button>
      </form>
    </div>

    <div class="tab-pane" id="pane-img">
      <form class="form-card" method="POST" action="/admin/stories/publicar/imagen" enctype="multipart/form-data">
        <label>Imagen (JPG/PNG)</label>
        <input type="file" name="archivo" accept="image/*" required/>
        <label>Caption (opcional)</label>
        <textarea name="caption" placeholder="Texto que acompaña la imagen"></textarea>
        <button type="submit" class="btn-publicar">Publicar estado</button>
      </form>
    </div>

    <div class="tab-pane" id="pane-txt">
      <form class="form-card" method="POST" action="/admin/stories/publicar/texto">
        <label>Texto del estado</label>
        <textarea name="caption" required placeholder="Ej: 🎉 Hoy descuento 20% en bermudas. Escríbenos."></textarea>
        <button type="submit" class="btn-publicar">Publicar estado</button>
      </form>
    </div>

    <h2 class="section-title">Estados publicados (últimos 30)</h2>
    {{historial}}
  </main>
</div>
__THEME_JS__
<script>
  document.querySelectorAll('.tab').forEach(function(t){
    t.addEventListener('click', function(){
      document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
      document.querySelectorAll('.tab-pane').forEach(x => x.classList.remove('active'));
      t.classList.add('active');
      document.getElementById('pane-' + t.dataset.tab).classList.add('active');
    });
  });
</script>
</body></html>"""
