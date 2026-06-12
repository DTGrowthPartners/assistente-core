"""/admin/stories — Estados de WhatsApp (texto + banco de imágenes).

Flujo nuevo:
  1. El admin sube imágenes al **banco** (galería local) cuando quiera.
  2. Las imágenes quedan ahí esperando — no se publican automáticamente.
  3. Click en una imagen del banco → modal → escribir caption (opcional)
     → "Publicar al estado". Recién ahí va a WhatsApp.
  4. Las imágenes se pueden eliminar del banco cuando ya no se necesiten.

También se mantiene la opción de publicar **solo texto** y el historial.
"""

from __future__ import annotations

import html
import mimetypes
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.config import get_settings
from app.db.session import get_session
from app.logging_setup import log
from app.whapi.client import (
    eliminar_mensaje,
    publicar_story_imagen_bytes,
    publicar_story_texto,
)

router = APIRouter(prefix="/admin/stories", tags=["admin-stories"])
settings = get_settings()


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _bog():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo("America/Bogota")
    except Exception:
        return timezone.utc


# ─── Banco local de imágenes ───────────────────────────────────────────────

# data/banco_imagenes/ (al lado de data/prompts/)
BANCO_DIR: Path = settings.prompts_path.parent / "banco_imagenes"


def _ensure_banco_dir() -> Path:
    BANCO_DIR.mkdir(parents=True, exist_ok=True)
    return BANCO_DIR


def _safe_filename(nombre: str) -> str:
    """Filename seguro: solo letras/dígitos/guiones, sin path traversal."""
    base = re.sub(r"[^A-Za-z0-9_.-]", "_", nombre)
    base = base.lstrip(".")  # no archivos ocultos
    if not base:
        base = "imagen.jpg"
    return base[:120]


def _ruta_segura(filename: str) -> Path:
    base = _ensure_banco_dir().resolve()
    candidato = (base / filename).resolve()
    if not str(candidato).startswith(str(base)):
        raise HTTPException(400, "Ruta no permitida")
    return candidato


def _listar_banco() -> list[dict]:
    """Lista los archivos del banco con metadata básica (más reciente arriba)."""
    base = _ensure_banco_dir()
    items: list[dict] = []
    for p in base.iterdir():
        if not p.is_file():
            continue
        mime, _ = mimetypes.guess_type(p.name)
        if not (mime or "").startswith("image/"):
            continue
        try:
            st = p.stat()
        except FileNotFoundError:
            continue
        items.append({
            "name": p.name,
            "mime": mime or "image/jpeg",
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mtime_str": datetime.fromtimestamp(st.st_mtime, _bog()).strftime("%Y-%m-%d %H:%M"),
        })
    items.sort(key=lambda x: x["mtime"], reverse=True)
    return items


# ─── Tabla de historial ────────────────────────────────────────────────────


async def _ensure_tabla(session: AsyncSession) -> None:
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


async def _registrar(
    session: AsyncSession,
    *,
    tipo: str,
    caption: str | None = None,
    imagen_url: str | None = None,
    msg_id: str | None = None,
    por: str | None = None,
    origen: str = "manual",
) -> None:
    await session.execute(sa_text("""
        INSERT INTO story_publicado (tipo, caption, imagen_url, whapi_message_id,
                                     publicado_por, origen)
        VALUES (:tipo, :cap, :iurl, :mid, :por, :origen)
    """), {"tipo": tipo, "cap": caption, "iurl": imagen_url,
           "mid": msg_id, "por": por, "origen": origen})


# ─── GET vista principal ──────────────────────────────────────────────────


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
        SELECT id, tipo, caption, imagen_url, publicado_por, publicado_en, origen
        FROM story_publicado
        ORDER BY publicado_en DESC LIMIT 30
    """))).fetchall()

    # Items del histórico
    items_html: list[str] = []
    for r in rows:
        id_, tipo, caption, imagen_url, por, ts, origen = r
        cap_short = (caption or "")[:120]
        thumb_html = ""
        if imagen_url:
            # imagen_url puede ser una ruta servida por el banco (/admin/stories/banco/<file>)
            # o una URL pública (legacy). Lo mostramos tal cual.
            thumb_html = f'<img src="{html.escape(imagen_url)}" class="hist-thumb" loading="lazy"/>'
        ts_str = ts.astimezone(_bog()).strftime("%Y-%m-%d %H:%M") if ts else ""
        items_html.append(f"""
        <div class="hist-item">
          {thumb_html}
          <div class="hist-body">
            <div class="hist-top">
              <span class="hist-tipo">{html.escape(tipo)}</span>
              <span class="hist-ts">{ts_str}</span>
            </div>
            <div class="hist-caption">{html.escape(cap_short)}</div>
            <div class="hist-meta">por {html.escape(por or '-')} · {html.escape(origen or '')}</div>
          </div>
          <form method="POST" action="/admin/stories/eliminar/{id_}" class="hist-del-form"
                onsubmit="return confirm('¿Eliminar este estado de WhatsApp? Si aún está vigente, se borra para todos. Esta acción no se puede deshacer.');">
            <button type="submit" class="btn-del" title="Eliminar este estado de WhatsApp">
              <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" style="margin-right:4px;"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              Eliminar
            </button>
          </form>
        </div>""")
    historial_html = "".join(items_html) or '<div class="empty">Aún no has publicado ningún estado.</div>'

    # Banco — grid de imágenes
    banco = _listar_banco()
    banco_html: list[str] = []
    for img in banco:
        src = f"/admin/stories/banco/{img['name']}"
        size_kb = f"{img['size'] // 1024} KB" if img['size'] >= 1024 else f"{img['size']} B"
        banco_html.append(f"""
        <div class="banco-card" data-name="{html.escape(img['name'])}" data-src="{src}">
          <img src="{src}" alt="{html.escape(img['name'])}" loading="lazy"/>
          <div class="banco-overlay">
            <button type="button" class="banco-btn primary" data-action="publicar">Publicar</button>
            <button type="button" class="banco-btn danger" data-action="eliminar" aria-label="Eliminar">×</button>
          </div>
          <div class="banco-name" title="{html.escape(img['name'])}">{html.escape(img['name'])}</div>
          <div class="banco-meta">{size_kb} · {html.escape(img['mtime_str'])}</div>
        </div>""")
    if not banco_html:
        banco_html.append('<div class="empty">El banco está vacío. Arrastra imágenes aquí o usa el botón.</div>')

    flash = ""
    msg = request.query_params.get("msg")
    if msg == "ok":
        flash = '<div class="flash">Estado publicado en WhatsApp.</div>'
    elif msg == "subido":
        flash = '<div class="flash">Imagen agregada al banco.</div>'
    elif msg == "eliminado":
        flash = '<div class="flash">Imagen eliminada del banco.</div>'
    elif msg == "fail":
        razon = html.escape(request.query_params.get("err", "Error desconocido"))
        flash = f'<div class="flash err">Falló: {razon}</div>'

    page = (_TEMPLATE
            .replace("__SHELL_STYLES__", SHELL_STYLES)
            .replace("__ICON_SPRITE__", ICON_SPRITE)
            .replace("__SIDEBAR__", sidebar_html(active="stories"))
            .replace("__THEME_JS__", THEME_TOGGLE_JS)
            .replace("{{flash}}", flash)
            .replace("{{banco}}", "".join(banco_html))
            .replace("{{historial}}", historial_html)
            .replace("{{banco_count}}", str(len(banco))))
    return HTMLResponse(page)


# ─── Servir imagen del banco (con auth) ───────────────────────────────────


@router.get("/banco/{filename}")
async def servir_banco(filename: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(401)
    path = _ruta_segura(filename)
    if not path.exists() or not path.is_file():
        raise HTTPException(404)
    mime, _ = mimetypes.guess_type(path.name)
    return FileResponse(path, media_type=mime or "image/jpeg")


# ─── Subir al banco ───────────────────────────────────────────────────────


@router.post("/banco/upload")
async def banco_upload(
    request: Request,
    archivo: UploadFile = File(...),
):
    if not _check_auth(request):
        raise HTTPException(401)
    mime = (archivo.content_type or "").lower()
    if not mime.startswith("image/"):
        return JSONResponse({"ok": False, "error": "solo imágenes"}, status_code=400)
    data = await archivo.read()
    if not data:
        return JSONResponse({"ok": False, "error": "archivo vacío"}, status_code=400)
    if len(data) > 16 * 1024 * 1024:
        return JSONResponse({"ok": False, "error": "máx 16 MB"}, status_code=400)

    # Nombre único con timestamp + prefijo random
    ts = datetime.now(_bog()).strftime("%Y%m%d_%H%M%S")
    suffix = Path(archivo.filename or "").suffix.lower() or {
        "image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(mime, ".jpg")
    if suffix not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        suffix = ".jpg"
    base_name = _safe_filename(Path(archivo.filename or "imagen").stem)[:40] or "imagen"
    fname = f"{ts}_{secrets.token_hex(3)}_{base_name}{suffix}"
    path = _ruta_segura(fname)
    path.write_bytes(data)

    autor = request.session.get("admin_user", "admin")
    log.info("admin.stories.banco_upload", archivo=fname, size=len(data), autor=autor)
    return {"ok": True, "name": fname, "src": f"/admin/stories/banco/{fname}", "size": len(data)}


# ─── Eliminar del banco ───────────────────────────────────────────────────


@router.post("/banco/{filename}/eliminar")
async def banco_eliminar(filename: str, request: Request):
    if not _check_auth(request):
        raise HTTPException(401)
    path = _ruta_segura(filename)
    if path.exists():
        try:
            path.unlink()
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)
    log.info("admin.stories.banco_eliminado", archivo=filename)
    return {"ok": True}


# ─── Publicar imagen del banco al estado ──────────────────────────────────


@router.post("/banco/{filename}/publicar")
async def banco_publicar(
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    caption = (form.get("caption") or "").strip() if isinstance(form.get("caption"), str) else ""
    eliminar_tras = (form.get("eliminar_tras") or "") in ("1", "true", "on")

    path = _ruta_segura(filename)
    if not path.exists():
        return JSONResponse({"ok": False, "error": "archivo no existe"}, status_code=404)
    data = path.read_bytes()
    mime, _ = mimetypes.guess_type(path.name)
    mime = mime or "image/jpeg"

    try:
        resp = await publicar_story_imagen_bytes(
            data, caption=caption or None, filename=filename, mime=mime,
        )
    except Exception as e:
        log.exception("admin.stories.banco_publicar.fail", archivo=filename, error=str(e))
        return JSONResponse({"ok": False, "error": str(e)[:200]}, status_code=502)

    msg_id = (resp.get("message") or {}).get("id")
    await _ensure_tabla(session)
    await _registrar(
        session, tipo="imagen", caption=caption or None,
        imagen_url=f"/admin/stories/banco/{filename}",
        msg_id=msg_id,
        por=request.session.get("admin_user", "admin"),
        origen="banco",
    )
    await session.commit()
    log.info("admin.stories.banco_publicar.ok", archivo=filename, msg_id=msg_id)

    eliminado = False
    if eliminar_tras:
        try:
            path.unlink()
            eliminado = True
        except Exception:
            pass

    return {"ok": True, "msg_id": msg_id, "eliminado_del_banco": eliminado}


# ─── Publicar texto puro ──────────────────────────────────────────────────


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
        log.exception("admin.stories.texto.fail", error=str(e))
        return RedirectResponse(f"/admin/stories?msg=fail&err={html.escape(str(e)[:120])}", 303)
    msg_id = (resp.get("message") or {}).get("id")
    await _ensure_tabla(session)
    await _registrar(session, tipo="texto", caption=caption, msg_id=msg_id,
                     por=request.session.get("admin_user") or "admin", origen="manual")
    await session.commit()
    return RedirectResponse("/admin/stories?msg=ok", 303)


# ─── Eliminar del historial ───────────────────────────────────────────────


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
    extra = ""
    if whapi_id:
        try:
            await eliminar_mensaje(whapi_id)
        except Exception as e:
            extra = "+(no+se+pudo+borrar+de+WA+—+puede+haber+expirado)"
            log.warning("admin.stories.eliminar.whapi_fail", id=story_id, error=str(e))
    await session.execute(sa_text("DELETE FROM story_publicado WHERE id = :i"), {"i": story_id})
    await session.commit()
    return RedirectResponse(f"/admin/stories?msg=ok{extra}", 303)


# ─── Template HTML ────────────────────────────────────────────────────────


_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Estados WA — Dairo</title>
__SHELL_STYLES__
<style>
  :root {
    --c-purple: #6366f1; --c-purple-hover: #4f46e5;
    --c-purple-soft: #ede9fe; --c-purple-softer: #f5f3ff;
    --c-text: #0f172a; --c-text-2: #475569; --c-text-3: #94a3b8;
    --c-border: #e5e7eb; --c-border-soft: #f1f5f9;
    --c-card: #ffffff; --c-success: #10b981; --c-danger: #ef4444;
  }
  [data-theme="dark"] {
    --c-text: #e2e8f0; --c-text-2: #94a3b8; --c-text-3: #64748b;
    --c-border: #1e293b; --c-border-soft: #1e293b; --c-card: #0f172a;
    --c-purple-soft: #312e81; --c-purple-softer: #1e1b4b;
  }
  .main { padding: 24px 28px; }
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -0.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 18px; }

  .flash {
    background: color-mix(in srgb, var(--c-success) 12%, transparent);
    color: var(--c-success); border: 1px solid var(--c-success);
    padding: 10px 14px; border-radius: 10px; margin-bottom: 14px; font-size: 13px;
  }
  .flash.err { background: color-mix(in srgb, var(--c-danger) 12%, transparent); color: var(--c-danger); border-color: var(--c-danger); }

  .section {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 14px; padding: 18px;
    margin-bottom: 18px; box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }
  .section-head { display: flex; align-items: center; justify-content: space-between; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .section-title { font-size: 16px; font-weight: 700; color: var(--c-text); margin: 0; display: flex; align-items: center; gap: 8px; }
  .section-sub { font-size: 12px; color: var(--c-text-2); margin-bottom: 12px; }
  .count-badge { background: var(--c-purple); color: #fff; font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px; }

  /* ─── BANCO ─────────────────────────────────────────────── */
  .banco-zone {
    border: 2px dashed var(--c-border);
    border-radius: 12px; padding: 16px;
    background: var(--c-purple-softer);
    transition: all .15s;
  }
  .banco-zone.drag-over { border-color: var(--c-purple); background: var(--c-purple-soft); }
  .banco-upload-row { display: flex; gap: 10px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
  .banco-upload-btn {
    background: var(--c-purple); color: #fff; border: none;
    padding: 9px 16px; border-radius: 10px; cursor: pointer;
    font-size: 13px; font-weight: 600;
    display: inline-flex; align-items: center; gap: 6px;
  }
  .banco-upload-btn:hover { background: var(--c-purple-hover); }
  .banco-hint { font-size: 12px; color: var(--c-text-2); }

  .banco-grid {
    display: grid; gap: 14px;
    grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  }
  .banco-card {
    position: relative; aspect-ratio: 9/16; border-radius: 10px;
    background: #000; overflow: hidden;
    border: 1px solid var(--c-border);
    cursor: pointer;
  }
  .banco-card img {
    width: 100%; height: 100%; object-fit: cover; display: block;
  }
  .banco-overlay {
    position: absolute; inset: 0;
    background: linear-gradient(180deg, rgba(0,0,0,.5) 0%, transparent 30%, transparent 60%, rgba(0,0,0,.8) 100%);
    opacity: 0; transition: opacity .15s;
    display: flex; flex-direction: column; justify-content: space-between;
    padding: 8px;
  }
  .banco-card:hover .banco-overlay { opacity: 1; }
  .banco-btn {
    border: none; cursor: pointer; font-weight: 600;
    border-radius: 8px; padding: 6px 10px; font-size: 12px;
    align-self: flex-start;
  }
  .banco-btn.primary {
    background: var(--c-purple); color: #fff;
    align-self: flex-end; width: 100%; padding: 8px 10px;
  }
  .banco-btn.primary:hover { background: var(--c-purple-hover); }
  .banco-btn.danger {
    background: rgba(239,68,68,.9); color: #fff;
    width: 28px; height: 28px; padding: 0; font-size: 16px; line-height: 1;
    align-self: flex-end; margin-left: auto;
  }
  .banco-btn.danger:hover { background: var(--c-danger); }
  .banco-name {
    font-size: 11px; color: #fff;
    position: absolute; bottom: 26px; left: 8px; right: 8px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
    text-shadow: 0 1px 2px rgba(0,0,0,.8);
  }
  .banco-meta {
    font-size: 10px; color: rgba(255,255,255,.75);
    position: absolute; bottom: 8px; left: 8px; right: 8px;
    text-shadow: 0 1px 2px rgba(0,0,0,.8);
  }
  .empty { padding: 28px; text-align: center; color: var(--c-text-3); font-size: 13px;
           border: 1px dashed var(--c-border); border-radius: 10px;
           grid-column: 1 / -1; }

  /* ─── TEXTO ─────────────────────────────────────────────── */
  .txt-form textarea {
    width: 100%; min-height: 90px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 10px 12px; font: inherit; font-size: 14px;
    background: var(--c-card); color: var(--c-text); box-sizing: border-box;
  }
  .txt-form textarea:focus { outline: none; border-color: var(--c-purple); box-shadow: 0 0 0 3px color-mix(in srgb, var(--c-purple) 15%, transparent); }
  .txt-form .btn-publicar {
    margin-top: 10px; background: var(--c-purple); color: #fff;
    border: none; padding: 9px 18px; border-radius: 10px;
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .txt-form .btn-publicar:hover { background: var(--c-purple-hover); }

  /* ─── HISTORIAL ─────────────────────────────────────────── */
  .hist-item {
    display: flex; gap: 14px; padding: 12px; margin-bottom: 8px;
    border: 1px solid var(--c-border-soft); border-radius: 10px;
    background: var(--c-card);
  }
  .hist-thumb { width: 56px; height: 56px; border-radius: 8px; object-fit: cover; flex-shrink: 0; background: var(--c-purple-softer); }
  .hist-body { flex: 1; min-width: 0; }
  .hist-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; flex-wrap: wrap; }
  .hist-tipo { font-weight: 600; font-size: 13px; color: var(--c-text); }
  .hist-ts { font-size: 11px; color: var(--c-text-3); }
  .hist-caption { font-size: 13px; color: var(--c-text-2); margin: 4px 0; line-height: 1.4; white-space: pre-wrap; }
  .hist-meta { font-size: 11px; color: var(--c-text-3); }
  .btn-del {
    padding: 6px 12px; border-radius: 8px;
    background: color-mix(in srgb, var(--c-danger) 10%, transparent);
    color: var(--c-danger);
    border: 1px solid color-mix(in srgb, var(--c-danger) 30%, transparent);
    cursor: pointer; font-size: 12px; font-weight: 600;
    display: inline-flex; align-items: center;
    flex-shrink: 0; align-self: center;
  }
  .btn-del:hover { background: var(--c-danger); color: #fff; border-color: var(--c-danger); }
  .hist-del-form { margin: 0; }

  /* ─── MODAL DE PUBLICACIÓN ──────────────────────────────── */
  .modal {
    position: fixed; inset: 0; z-index: 10000;
    background: rgba(0,0,0,.7); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center;
    padding: 20px;
  }
  .modal.open { display: flex; }
  .modal-card {
    background: var(--c-card); border-radius: 16px;
    padding: 20px; max-width: 760px; width: 100%;
    max-height: 90vh; overflow-y: auto;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
    display: grid; grid-template-columns: 1fr 1.4fr; gap: 18px;
  }
  @media (max-width: 760px) { .modal-card { grid-template-columns: 1fr; } }
  .modal-img { max-width: 100%; max-height: 60vh; border-radius: 10px; background: #000; object-fit: contain; }
  .modal-side h3 { font-size: 16px; font-weight: 700; color: var(--c-text); margin: 0 0 6px; }
  .modal-side .modal-name { font-size: 12px; color: var(--c-text-3); margin-bottom: 12px; word-break: break-all; }
  .modal-side label { display: block; font-size: 11px; font-weight: 600; color: var(--c-text-2); margin: 10px 0 4px; text-transform: uppercase; letter-spacing: .04em; }
  .modal-side textarea {
    width: 100%; min-height: 90px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 10px 12px; font: inherit; font-size: 14px;
    background: var(--c-card); color: var(--c-text); box-sizing: border-box;
  }
  .modal-checkbox { display: flex; align-items: center; gap: 8px; margin-top: 12px; font-size: 13px; color: var(--c-text-2); }
  .modal-actions { display: flex; gap: 8px; margin-top: 14px; flex-wrap: wrap; }
  .modal-btn {
    border: none; cursor: pointer; padding: 10px 16px;
    border-radius: 10px; font-size: 13px; font-weight: 600;
  }
  .modal-btn.primary { background: var(--c-purple); color: #fff; }
  .modal-btn.primary:hover { background: var(--c-purple-hover); }
  .modal-btn.primary:disabled { opacity: .6; cursor: wait; }
  .modal-btn.danger { background: color-mix(in srgb, var(--c-danger) 12%, transparent); color: var(--c-danger); border: 1px solid color-mix(in srgb, var(--c-danger) 30%, transparent); }
  .modal-btn.danger:hover { background: var(--c-danger); color: #fff; }
  .modal-btn.ghost { background: transparent; color: var(--c-text-2); border: 1px solid var(--c-border); }
  .modal-btn.ghost:hover { background: var(--c-purple-softer); color: var(--c-purple); }
  .modal-state { font-size: 12px; color: var(--c-text-3); margin-top: 8px; }
  .modal-state.ok { color: var(--c-success); }
  .modal-state.err { color: var(--c-danger); }

  /* Toast */
  .toast-stack { position: fixed; bottom: 16px; right: 16px; z-index: 9999; display: flex; flex-direction: column; gap: 8px; }
  .toast { padding: 10px 16px; border-radius: 10px; font-size: 13px; color: #fff; background: var(--c-success); box-shadow: 0 4px 12px rgba(0,0,0,.15); transition: opacity .3s, transform .3s; min-width: 180px; }
  .toast.error { background: var(--c-danger); }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Estados de WhatsApp</h1>
    <p class="page-subtitle">Sube imágenes al banco y decide cuándo publicarlas como estado. Los estados duran 24 h en WhatsApp.</p>
    {{flash}}

    <!-- Banco de imágenes -->
    <section class="section">
      <div class="section-head">
        <h2 class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="18" height="18" rx="2" ry="2"/><circle cx="9" cy="9" r="2"/><path d="m21 15-3.086-3.086a2 2 0 0 0-2.828 0L6 21"/></svg>
          Banco de imágenes
          <span class="count-badge">{{banco_count}}</span>
        </h2>
        <div class="banco-upload-row">
          <input type="file" id="upload-input" accept="image/*" multiple hidden/>
          <button type="button" class="banco-upload-btn" id="upload-btn">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="17 8 12 3 7 8"/><line x1="12" x2="12" y1="3" y2="15"/></svg>
            Subir imágenes
          </button>
          <span class="banco-hint">o arrastra y suelta aquí. Las imágenes quedan guardadas hasta que las publiques o las borres.</span>
        </div>
      </div>
      <div class="banco-zone" id="banco-zone">
        <div class="banco-grid" id="banco-grid">
          {{banco}}
        </div>
      </div>
    </section>

    <!-- Estado de texto -->
    <section class="section">
      <div class="section-head">
        <h2 class="section-title">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></svg>
          Estado de solo texto
        </h2>
      </div>
      <form class="txt-form" method="POST" action="/admin/stories/publicar/texto">
        <textarea name="caption" required placeholder="Ej: 🎉 Hoy promo especial. Escríbeme."></textarea>
        <button type="submit" class="btn-publicar">Publicar texto al estado</button>
      </form>
    </section>

    <!-- Historial -->
    <section class="section">
      <div class="section-head">
        <h2 class="section-title">Historial — últimos 30 estados</h2>
      </div>
      {{historial}}
    </section>
  </main>
</div>

<!-- Modal de publicación de imagen -->
<div class="modal" id="modal-pub" role="dialog" aria-modal="true">
  <div class="modal-card">
    <img class="modal-img" id="modal-img" alt=""/>
    <div class="modal-side">
      <h3>Publicar al estado</h3>
      <div class="modal-name" id="modal-name">—</div>
      <label>Caption (opcional)</label>
      <textarea id="modal-caption" placeholder="Texto que acompaña la imagen (opcional)"></textarea>
      <div class="modal-checkbox">
        <input type="checkbox" id="modal-eliminar-tras"/>
        <label for="modal-eliminar-tras" style="margin:0;text-transform:none;letter-spacing:0;font-weight:500;color:var(--c-text);">Eliminar del banco al publicar</label>
      </div>
      <div class="modal-actions">
        <button type="button" class="modal-btn primary" id="modal-publicar">Publicar al estado</button>
        <button type="button" class="modal-btn ghost" id="modal-cerrar">Cancelar</button>
        <button type="button" class="modal-btn danger" id="modal-eliminar">Eliminar del banco</button>
      </div>
      <div class="modal-state" id="modal-state"></div>
    </div>
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

  // ── Upload (click + drag-drop) ───────────────────────────
  var grid = document.getElementById('banco-grid');
  var zone = document.getElementById('banco-zone');
  var input = document.getElementById('upload-input');
  var btn = document.getElementById('upload-btn');

  btn.addEventListener('click', function(){ input.click(); });
  input.addEventListener('change', function(e){
    handleFiles(e.target.files);
    input.value = '';
  });
  ['dragenter','dragover'].forEach(function(ev){
    zone.addEventListener(ev, function(e){ e.preventDefault(); e.stopPropagation(); zone.classList.add('drag-over'); });
  });
  ['dragleave','drop'].forEach(function(ev){
    zone.addEventListener(ev, function(e){ e.preventDefault(); e.stopPropagation(); zone.classList.remove('drag-over'); });
  });
  zone.addEventListener('drop', function(e){
    if (e.dataTransfer && e.dataTransfer.files) handleFiles(e.dataTransfer.files);
  });

  async function handleFiles(files){
    if (!files || !files.length) return;
    var arr = Array.from(files).filter(function(f){ return f.type && f.type.startsWith('image/'); });
    if (!arr.length) { toast('Solo imágenes', true); return; }
    for (var i = 0; i < arr.length; i++) {
      var f = arr[i];
      if (f.size > 16 * 1024 * 1024) { toast('Muy grande: ' + f.name, true); continue; }
      try {
        var fd = new FormData(); fd.append('archivo', f);
        var r = await fetch('/admin/stories/banco/upload', { method: 'POST', body: fd });
        var d = await r.json();
        if (r.ok && d.ok) {
          insertarCard(d.name, d.src, f.size);
          toast('Subido: ' + d.name.slice(0, 40));
        } else {
          toast('Error: ' + (d.error || r.status), true);
        }
      } catch(err) { toast('Error de red', true); }
    }
    actualizarCount();
  }

  function actualizarCount(){
    var n = grid.querySelectorAll('.banco-card').length;
    var badge = document.querySelector('.count-badge');
    if (badge) badge.textContent = n;
    var empty = grid.querySelector('.empty');
    if (n === 0 && !empty) {
      grid.innerHTML = '<div class="empty">El banco está vacío. Arrastra imágenes aquí o usa el botón.</div>';
    } else if (n > 0 && empty) {
      empty.remove();
    }
  }

  function insertarCard(name, src, size){
    var empty = grid.querySelector('.empty'); if (empty) empty.remove();
    var sizeKb = size >= 1024 ? Math.floor(size/1024) + ' KB' : size + ' B';
    var now = new Date().toLocaleString('es-CO', { year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit' });
    var card = document.createElement('div');
    card.className = 'banco-card';
    card.dataset.name = name; card.dataset.src = src;
    card.innerHTML = ''
      + '<img src="' + src + '" alt="' + name + '" loading="lazy"/>'
      + '<div class="banco-overlay">'
      + '  <button type="button" class="banco-btn primary" data-action="publicar">Publicar</button>'
      + '  <button type="button" class="banco-btn danger" data-action="eliminar" aria-label="Eliminar">×</button>'
      + '</div>'
      + '<div class="banco-name" title="' + name + '">' + name + '</div>'
      + '<div class="banco-meta">' + sizeKb + ' · ' + now + '</div>';
    grid.insertBefore(card, grid.firstChild);
  }

  // ── Modal ────────────────────────────────────────────────
  var modal = document.getElementById('modal-pub');
  var mImg = document.getElementById('modal-img');
  var mName = document.getElementById('modal-name');
  var mCaption = document.getElementById('modal-caption');
  var mElimTras = document.getElementById('modal-eliminar-tras');
  var mState = document.getElementById('modal-state');
  var mBtnPub = document.getElementById('modal-publicar');
  var mBtnDel = document.getElementById('modal-eliminar');
  var mBtnClose = document.getElementById('modal-cerrar');
  var currentName = null;

  function abrirModal(name, src){
    currentName = name;
    mImg.src = src; mName.textContent = name;
    mCaption.value = ''; mElimTras.checked = false;
    mState.textContent = ''; mState.className = 'modal-state';
    modal.classList.add('open');
    document.body.style.overflow = 'hidden';
    setTimeout(function(){ mCaption.focus(); }, 50);
  }
  function cerrarModal(){
    modal.classList.remove('open');
    document.body.style.overflow = '';
    currentName = null;
  }
  mBtnClose.addEventListener('click', cerrarModal);
  modal.addEventListener('click', function(e){ if (e.target === modal) cerrarModal(); });
  document.addEventListener('keydown', function(e){
    if (e.key === 'Escape' && modal.classList.contains('open')) cerrarModal();
  });

  // Click en card → publicar abre modal; × elimina directo
  grid.addEventListener('click', async function(e){
    var btnEl = e.target.closest('button[data-action]');
    var card = e.target.closest('.banco-card');
    if (!card) return;
    if (btnEl) {
      e.stopPropagation();
      var action = btnEl.dataset.action;
      var name = card.dataset.name; var src = card.dataset.src;
      if (action === 'publicar') {
        abrirModal(name, src);
      } else if (action === 'eliminar') {
        if (!confirm('¿Eliminar "' + name + '" del banco?')) return;
        try {
          var r = await fetch('/admin/stories/banco/' + encodeURIComponent(name) + '/eliminar', { method: 'POST' });
          var d = await r.json();
          if (r.ok && d.ok) { card.remove(); toast('Eliminado'); actualizarCount(); }
          else toast('Error', true);
        } catch(err) { toast('Error de red', true); }
      }
      return;
    }
    // Click directo en la card (no en botón) → modal
    abrirModal(card.dataset.name, card.dataset.src);
  });

  mBtnPub.addEventListener('click', async function(){
    if (!currentName) return;
    mBtnPub.disabled = true;
    mState.textContent = 'Publicando…'; mState.className = 'modal-state';
    try {
      var fd = new FormData();
      fd.append('caption', mCaption.value);
      if (mElimTras.checked) fd.append('eliminar_tras', '1');
      var r = await fetch('/admin/stories/banco/' + encodeURIComponent(currentName) + '/publicar', {
        method: 'POST', body: fd,
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        mState.textContent = '✓ Publicado al estado de WhatsApp';
        mState.className = 'modal-state ok';
        toast('Estado publicado');
        if (d.eliminado_del_banco) {
          var card = grid.querySelector('.banco-card[data-name="' + CSS.escape(currentName) + '"]');
          if (card) { card.remove(); actualizarCount(); }
        }
        setTimeout(function(){ cerrarModal(); window.location.reload(); }, 1200);
      } else {
        mState.textContent = 'Error: ' + (d.error || r.status);
        mState.className = 'modal-state err';
        toast('Error al publicar', true);
      }
    } catch(err) {
      mState.textContent = 'Error de red: ' + err.message;
      mState.className = 'modal-state err';
    }
    mBtnPub.disabled = false;
  });

  mBtnDel.addEventListener('click', async function(){
    if (!currentName) return;
    if (!confirm('¿Eliminar "' + currentName + '" del banco?')) return;
    try {
      var r = await fetch('/admin/stories/banco/' + encodeURIComponent(currentName) + '/eliminar', { method: 'POST' });
      var d = await r.json();
      if (r.ok && d.ok) {
        var card = grid.querySelector('.banco-card[data-name="' + CSS.escape(currentName) + '"]');
        if (card) { card.remove(); actualizarCount(); }
        toast('Eliminado del banco');
        cerrarModal();
      } else toast('Error', true);
    } catch(err) { toast('Error de red', true); }
  });
})();
</script>
</body></html>"""
