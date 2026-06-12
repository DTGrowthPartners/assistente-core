"""/admin/seguimiento — Prospectos que no han agendado, con acciones de seguimiento.

Muestra contactos etiquetados como `prospecto` que NO tienen cita activa
(agendada/reprogramada/completada). Permite:

- Filtrar por estado, días sin contacto, ciudad, sector.
- Buscar por nombre/negocio.
- Enviar mensaje individual.
- Mandar **campaña masiva** a un set seleccionado (con delay humanizado entre
  envíos para no spamear y respetando pausas activas).

Diseñado para que el equipo retome leads "fríos" después de unos días sin
respuesta o sin que llegaran a agendar.
"""

from __future__ import annotations

import asyncio
import html
import random
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.admin._ui_helpers import (
    PILL_STYLES, avatar_color, format_phone, format_relative_date, get_initials,
)
from app.config import get_settings
from app.db.repos import bot_pausado, guardar_conversacion
from app.db.session import async_session_factory, get_session
from app.logging_setup import log
from app.whapi.client import enviar_texto

router = APIRouter(prefix="/admin/seguimiento", tags=["admin-seguimiento"])
settings = get_settings()


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _es_ajax(request: Request) -> bool:
    return (
        "application/json" in (request.headers.get("accept") or "")
        or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")
    )


# ─── GET vista ────────────────────────────────────────────────────────────


_QUERY = """
SELECT
    c.id, c.numero_whatsapp, c.nombre, c.bloqueado, c.ultimo_contacto,
    p.negocio, p.sector, p.ciudad, p.necesidad, p.estado AS estado_prospecto,
    p.ya_pauta, p.tiene_web,
    (SELECT MAX(timestamp) FROM conversaciones
        WHERE cliente_id = c.id AND direccion = 'inbound') AS ult_inbound,
    (SELECT MAX(timestamp) FROM conversaciones
        WHERE cliente_id = c.id AND direccion IN ('outbound','humano')) AS ult_outbound,
    (SELECT COUNT(*) FROM conversaciones WHERE cliente_id = c.id) AS total_msgs,
    EXISTS(SELECT 1 FROM intervencion_humana
        WHERE cliente_id = c.id AND pausado_hasta > now()) AS pausado
FROM clientes c
LEFT JOIN prospectos p ON p.cliente_id = c.id
WHERE c.etiqueta = 'prospecto'
  AND c.bloqueado = FALSE
  AND NOT EXISTS (
      SELECT 1 FROM citas
      WHERE cliente_id = c.id
        AND estado IN ('agendada', 'reprogramada', 'completada')
  )
"""


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def vista_seguimiento(
    request: Request,
    q: str = "",
    estado: str = "todos",
    dias_min: int = 0,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)

    sql = _QUERY
    params: dict[str, Any] = {}

    if q.strip():
        sql += """ AND (LOWER(COALESCE(c.nombre,'')) ILIKE :q
                       OR c.numero_whatsapp ILIKE :q
                       OR LOWER(COALESCE(p.negocio,'')) ILIKE :q
                       OR LOWER(COALESCE(p.sector,'')) ILIKE :q)"""
        params["q"] = f"%{q.strip().lower()}%"

    if estado in ("nuevo", "calificando", "cerrado_perdido"):
        sql += " AND p.estado = :est"
        params["est"] = estado
    elif estado == "sin_prospecto":
        sql += " AND p.cliente_id IS NULL"

    if dias_min and dias_min > 0:
        sql += " AND (c.ultimo_contacto < now() - (:dias || ' days')::interval OR c.ultimo_contacto IS NULL)"
        params["dias"] = str(int(dias_min))

    sql += " ORDER BY c.ultimo_contacto DESC NULLS LAST LIMIT 200"

    rows = (await session.execute(sa_text(sql), params)).fetchall()

    # Tags asignados a cada cliente (M2M cliente_tags ↔ tags) — una sola query
    cliente_ids = [r[0] for r in rows]
    tags_por_cliente: dict[int, list[tuple[str, str]]] = {}
    if cliente_ids:
        tag_rows = (await session.execute(sa_text("""
            SELECT ct.cliente_id, t.nombre, t.color
              FROM cliente_tags ct
              JOIN tags t ON t.id = ct.tag_id
             WHERE ct.cliente_id = ANY(:ids)
             ORDER BY t.orden ASC, t.nombre ASC
        """), {"ids": cliente_ids})).fetchall()
        for tr in tag_rows:
            tags_por_cliente.setdefault(tr.cliente_id, []).append((tr.nombre, tr.color))

    # Conteos para los chips
    counts_sql = _QUERY.replace(
        "SELECT\n    c.id, c.numero_whatsapp, c.nombre, c.bloqueado, c.ultimo_contacto,\n    p.negocio, p.sector, p.ciudad, p.necesidad, p.estado AS estado_prospecto,\n    p.ya_pauta, p.tiene_web,\n    (SELECT MAX(timestamp) FROM conversaciones\n        WHERE cliente_id = c.id AND direccion = 'inbound') AS ult_inbound,\n    (SELECT MAX(timestamp) FROM conversaciones\n        WHERE cliente_id = c.id AND direccion IN ('outbound','humano')) AS ult_outbound,\n    (SELECT COUNT(*) FROM conversaciones WHERE cliente_id = c.id) AS total_msgs,\n    EXISTS(SELECT 1 FROM intervencion_humana\n        WHERE cliente_id = c.id AND pausado_hasta > now()) AS pausado",
        "SELECT COUNT(*)"
    )
    total = (await session.execute(sa_text(counts_sql))).scalar_one()
    nuevos = (await session.execute(sa_text(counts_sql + " AND p.estado = 'nuevo'"))).scalar_one()
    calificando = (await session.execute(sa_text(counts_sql + " AND p.estado = 'calificando'"))).scalar_one()
    perdidos = (await session.execute(sa_text(counts_sql + " AND p.estado = 'cerrado_perdido'"))).scalar_one()
    sin_prospecto = (await session.execute(sa_text(counts_sql + " AND p.cliente_id IS NULL"))).scalar_one()

    # Renderizar filas
    rows_html: list[str] = []
    for r in rows:
        (cli_id, numero, nombre, bloqueado, ult_contacto,
         negocio, sector, ciudad, necesidad, estado_p, ya_pauta, tiene_web,
         ult_inbound, ult_outbound, total_msgs, pausado) = r

        nombre_disp = nombre or "(sin nombre)"
        seed = (nombre or numero or "?").strip()
        bg, fg = avatar_color(seed)
        initials = get_initials(nombre, fallback=(numero or "?")[-2:])

        estado_p_label = {
            "nuevo": "Nuevo",
            "calificando": "Calificando",
            "agendado": "Agendado",
            "cerrado_perdido": "Perdido",
            "cerrado_ganado": "Ganado",
        }.get((estado_p or "").lower(), estado_p or "—")

        pausado_chip = '<span class="pill pill--bloqueado">PAUSADO</span>' if pausado else ""

        ya_pauta_chip = ""
        if ya_pauta is True:
            ya_pauta_chip = '<span class="pill" style="background:#FEF3C7;color:#92400E;">Ya pauta</span>'
        elif ya_pauta is False:
            ya_pauta_chip = '<span class="pill" style="background:#F3F4F6;color:#374151;">No pauta</span>'

        web_chip = ""
        if tiene_web is True:
            web_chip = '<span class="pill" style="background:#DBEAFE;color:#1E40AF;">Web</span>'
        elif tiene_web is False:
            web_chip = '<span class="pill" style="background:#F3F4F6;color:#374151;">Sin web</span>'

        ult_inbound_rel = format_relative_date(ult_inbound) if ult_inbound else "—"
        nombre_safe = html.escape(nombre_disp)
        negocio_disp = (negocio or "—")
        contexto = " · ".join(filter(None, [
            html.escape(negocio_disp) if negocio_disp != "—" else "",
            html.escape(sector or ""),
            html.escape(ciudad or ""),
        ])) or "<span style='color:var(--c-text-3);'>Sin info del negocio</span>"

        necesidad_html = (
            f'<div class="row-need" title="{html.escape(necesidad)}">"{html.escape((necesidad or "")[:120])}"</div>'
            if necesidad else ""
        )

        # Chips de tags de seguimiento
        tags_cli = tags_por_cliente.get(cli_id, [])
        tags_chips_html = ""
        if tags_cli:
            tags_chips_html = '<div class="row-tags">' + "".join(
                f'<span class="row-tag-chip" style="background:{tcolor};">{html.escape(tname)}</span>'
                for tname, tcolor in tags_cli
            ) + '</div>'

        rows_html.append(f"""
        <div class="prospecto-card" data-cid="{cli_id}" data-numero="{html.escape(numero)}" data-nombre="{nombre_safe}">
          <input type="checkbox" class="row-check" data-cid="{cli_id}"/>
          <div class="row-avatar" style="background:{bg};color:{fg};">{html.escape(initials)}</div>
          <div class="row-body">
            <div class="row-top">
              <a class="row-name" href="/admin/chats/{cli_id}" target="_blank">{nombre_safe}</a>
              <span class="row-estado">{html.escape(estado_p_label)}</span>
              {ya_pauta_chip}{web_chip}{pausado_chip}
            </div>
            <div class="row-context">{contexto}</div>
            {tags_chips_html}
            {necesidad_html}
            <div class="row-meta">
              <span>📱 {html.escape(format_phone(numero))}</span>
              <span>💬 {total_msgs or 0} mensajes</span>
              <span>⏱ Último: {html.escape(ult_inbound_rel)}</span>
            </div>
          </div>
          <div class="row-actions">
            <button type="button" class="row-btn primary" data-action="enviar">Enviar mensaje</button>
            <a class="row-btn ghost" href="/admin/chats/{cli_id}" target="_blank">Ver chat →</a>
          </div>
        </div>""")

    rows_str = "".join(rows_html) or '<div class="empty">No hay prospectos sin agenda con esos filtros. 🎉</div>'

    body = _TEMPLATE
    body = body.replace("__SHELL_STYLES__", SHELL_STYLES)
    body = body.replace("__PILL_STYLES__", PILL_STYLES)
    body = body.replace("__ICON_SPRITE__", ICON_SPRITE)
    body = body.replace("__SIDEBAR__", sidebar_html(active="seguimiento"))
    body = body.replace("__THEME_JS__", THEME_TOGGLE_JS)
    body = body.replace("{{rows}}", rows_str)
    body = body.replace("{{count_total}}", str(total))
    body = body.replace("{{count_nuevos}}", str(nuevos))
    body = body.replace("{{count_calif}}", str(calificando))
    body = body.replace("{{count_perdidos}}", str(perdidos))
    body = body.replace("{{count_sin}}", str(sin_prospecto))
    body = body.replace("{{q}}", html.escape(q))
    body = body.replace("{{dias_min}}", str(dias_min) if dias_min else "")
    body = body.replace("{{active_todos}}", "active" if estado == "todos" else "")
    body = body.replace("{{active_nuevos}}", "active" if estado == "nuevo" else "")
    body = body.replace("{{active_calif}}", "active" if estado == "calificando" else "")
    body = body.replace("{{active_perdidos}}", "active" if estado == "cerrado_perdido" else "")
    body = body.replace("{{active_sin}}", "active" if estado == "sin_prospecto" else "")
    return HTMLResponse(body)


# ─── POST: envío individual ──────────────────────────────────────────────


@router.post("/{cliente_id}/enviar")
async def enviar_individual(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    texto = (form.get("mensaje") or "").strip() if isinstance(form.get("mensaje"), str) else ""
    if not texto:
        return JSONResponse({"ok": False, "error": "vacío"}, 400)

    row = (await session.execute(sa_text(
        "SELECT numero_whatsapp, nombre FROM clientes WHERE id = :id"
    ), {"id": cliente_id})).first()
    if not row:
        return JSONResponse({"ok": False, "error": "cliente no encontrado"}, 404)
    numero, nombre = row[0], row[1]

    if await bot_pausado(session, cliente_id):
        return JSONResponse({"ok": False, "error": "el chat está pausado — reactiva primero"}, 409)

    try:
        await enviar_texto(numero, texto)
    except Exception as e:
        log.exception("admin.seguimiento.enviar_fail", cliente_id=cliente_id, error=str(e))
        return JSONResponse({"ok": False, "error": f"whapi fail: {str(e)[:150]}"}, 502)

    autor = request.session.get("admin_user", "admin")
    await guardar_conversacion(
        session, cliente_id=cliente_id, direccion="humano",
        tipo="texto", contenido=texto,
        metadata={"via": "seguimiento", "autor": autor},
    )
    await session.commit()
    log.info("admin.seguimiento.enviado", cliente_id=cliente_id, numero=numero, autor=autor, chars=len(texto))
    return {"ok": True}


# ─── POST: campaña masiva ────────────────────────────────────────────────


@router.post("/campania")
async def campania_masiva(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Envía el mismo mensaje a una lista de prospectos con delay humanizado.

    Body multipart:
      - mensaje (str): el texto a enviar.
      - ids (str): cliente_ids separados por coma.

    Respeta:
      - Pausa individual (skip).
      - Delay aleatorio 6-18s entre envíos para no spammear.
      - Personalización mínima: {{nombre}} en el mensaje se reemplaza por el
        primer nombre del contacto (o "Hola" si no hay).
    """
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    texto_tpl = (form.get("mensaje") or "").strip() if isinstance(form.get("mensaje"), str) else ""
    ids_str = form.get("ids") or ""
    if not texto_tpl:
        return JSONResponse({"ok": False, "error": "mensaje vacío"}, 400)
    try:
        ids = [int(x.strip()) for x in str(ids_str).split(",") if x.strip()]
    except Exception:
        return JSONResponse({"ok": False, "error": "ids inválidos"}, 400)
    if not ids:
        return JSONResponse({"ok": False, "error": "lista vacía"}, 400)
    if len(ids) > 100:
        return JSONResponse({"ok": False, "error": "máx 100 por campaña"}, 400)

    autor = request.session.get("admin_user", "admin")

    # Lanzamos en background — la lista de errores la dejamos en logs.
    async def _run():
        async with async_session_factory() as s:
            enviados = 0
            pausados = 0
            fallos: list[str] = []
            for cid in ids:
                row = (await s.execute(sa_text(
                    "SELECT numero_whatsapp, nombre FROM clientes WHERE id = :id"
                ), {"id": cid})).first()
                if not row:
                    fallos.append(f"{cid}: no existe")
                    continue
                numero, nombre = row[0], row[1]

                if await bot_pausado(s, cid):
                    pausados += 1
                    continue

                primer_nombre = (nombre or "").split(" ")[0] or "Hola"
                texto_personal = texto_tpl.replace("{{nombre}}", primer_nombre)

                try:
                    await enviar_texto(numero, texto_personal)
                except Exception as e:
                    fallos.append(f"{cid}: {str(e)[:80]}")
                    continue

                await guardar_conversacion(
                    s, cliente_id=cid, direccion="humano",
                    tipo="texto", contenido=texto_personal,
                    metadata={"via": "campania", "autor": autor},
                )
                await s.commit()
                enviados += 1

                # Delay humanizado entre envíos (6-18s)
                await asyncio.sleep(random.uniform(6.0, 18.0))

            log.warning(
                "admin.seguimiento.campania_done",
                autor=autor, total=len(ids), enviados=enviados,
                pausados=pausados, fallos=len(fallos),
            )
            if fallos:
                log.warning("admin.seguimiento.campania_fallos", fallos=fallos[:20])

    asyncio.create_task(_run())
    log.warning("admin.seguimiento.campania_lanzada", autor=autor, total=len(ids))
    return {"ok": True, "lanzada": True, "total": len(ids)}


# ─── Template HTML ───────────────────────────────────────────────────────


_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Seguimiento de prospectos — Dairo</title>
__SHELL_STYLES__
__PILL_STYLES__
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
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 18px; }

  .toolbar { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; flex-wrap: wrap; }
  .toolbar input[type=text], .toolbar input[type=number] {
    padding: 9px 14px; border: 1px solid var(--c-border); border-radius: 10px;
    background: var(--c-card); color: var(--c-text); font: inherit; font-size: 14px;
  }
  .toolbar input[type=text] { flex: 1; min-width: 240px; }
  .toolbar input[type=number] { width: 90px; }
  .toolbar label { font-size: 12px; color: var(--c-text-2); }
  .toolbar button[type=submit] {
    background: var(--c-purple); color: #fff; border: none;
    padding: 9px 14px; border-radius: 10px; font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .filter-chip {
    padding: 6px 12px; border-radius: 999px; font-size: 12px; font-weight: 500;
    background: var(--c-card); border: 1px solid var(--c-border); color: var(--c-text-2);
    text-decoration: none; display: inline-flex; align-items: center; gap: 5px;
  }
  .filter-chip:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .filter-chip.active { background: var(--c-purple); color: #fff; border-color: var(--c-purple); }
  .filter-chip .cnt {
    background: rgba(255,255,255,.25); color: inherit;
    padding: 1px 7px; border-radius: 999px; font-size: 11px; font-weight: 600;
  }
  .filter-chip:not(.active) .cnt { background: var(--c-border-soft); color: var(--c-text-3); }

  /* Barra de acciones bulk */
  .bulk-bar {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 12px; padding: 10px 14px; margin-bottom: 12px;
    display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    position: sticky; top: 10px; z-index: 10;
    box-shadow: 0 2px 8px rgba(15,23,42,.04);
  }
  .bulk-count { font-size: 13px; color: var(--c-text-2); }
  .bulk-count strong { color: var(--c-text); }
  .bulk-btn {
    padding: 7px 14px; border-radius: 8px; font-size: 13px; font-weight: 600;
    border: none; cursor: pointer;
  }
  .bulk-btn.primary { background: var(--c-purple); color: #fff; }
  .bulk-btn.primary:hover { background: var(--c-purple-hover); }
  .bulk-btn.ghost { background: var(--c-card); color: var(--c-text-2); border: 1px solid var(--c-border); }
  .bulk-btn:disabled { opacity: .5; cursor: not-allowed; }

  /* Cards de prospectos */
  .prospectos-list { display: flex; flex-direction: column; gap: 10px; }
  .prospecto-card {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 12px; padding: 14px 16px;
    display: grid; grid-template-columns: 22px 44px minmax(0,1fr) auto;
    gap: 12px; align-items: flex-start;
    transition: border-color .12s;
  }
  .prospecto-card:hover { border-color: var(--c-purple); }
  .prospecto-card.selected { background: var(--c-purple-softer); border-color: var(--c-purple); }
  .row-check { margin-top: 14px; width: 16px; height: 16px; cursor: pointer; }
  .row-avatar {
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 15px; flex-shrink: 0;
  }
  .row-body { min-width: 0; }
  .row-top { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  .row-name { font-weight: 600; font-size: 15px; color: var(--c-text); text-decoration: none; }
  .row-name:hover { color: var(--c-purple); }
  .row-estado {
    font-size: 11px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
    background: var(--c-purple-softer); color: var(--c-purple);
    text-transform: uppercase; letter-spacing: .04em;
  }
  .row-context { font-size: 13px; color: var(--c-text-2); margin-top: 4px; }
  .row-tags { display: flex; flex-wrap: wrap; gap: 5px; margin-top: 6px; }
  .row-tag-chip {
    display: inline-flex; align-items: center;
    padding: 2px 9px; border-radius: 999px;
    font-size: 10px; font-weight: 700; color: #fff;
    text-shadow: 0 1px 1px rgba(0,0,0,.1);
    text-transform: uppercase; letter-spacing: 0.2px;
  }
  .row-need {
    font-size: 12px; color: var(--c-text-2); margin-top: 4px;
    font-style: italic; padding-left: 8px; border-left: 2px solid var(--c-border);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .row-meta { display: flex; gap: 16px; font-size: 11px; color: var(--c-text-3); margin-top: 8px; flex-wrap: wrap; }
  .row-actions { display: flex; flex-direction: column; gap: 6px; align-self: center; }
  .row-btn {
    padding: 6px 12px; border-radius: 8px; font-size: 12px; font-weight: 500;
    border: 1px solid var(--c-border); background: var(--c-card); color: var(--c-text-2);
    cursor: pointer; text-decoration: none; text-align: center;
  }
  .row-btn:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .row-btn.primary { background: var(--c-purple); color: #fff; border-color: var(--c-purple); }
  .row-btn.primary:hover { background: var(--c-purple-hover); color: #fff; }
  .empty { padding: 40px; text-align: center; color: var(--c-text-3);
           border: 1px dashed var(--c-border); border-radius: 12px; background: var(--c-card); }

  /* Modal */
  .modal {
    position: fixed; inset: 0; z-index: 10000;
    background: rgba(0,0,0,.6); backdrop-filter: blur(4px);
    display: none; align-items: center; justify-content: center; padding: 20px;
  }
  .modal.open { display: flex; }
  .modal-card {
    background: var(--c-card); border-radius: 14px;
    width: 100%; max-width: 580px; padding: 22px;
    box-shadow: 0 20px 60px rgba(0,0,0,.4);
  }
  .modal-card h3 { margin: 0 0 4px; color: var(--c-text); font-size: 16px; }
  .modal-card .target { font-size: 12px; color: var(--c-text-2); margin-bottom: 12px; }
  .modal-card textarea {
    width: 100%; min-height: 110px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 10px;
    padding: 10px 12px; font: inherit; font-size: 14px;
    background: var(--c-card); color: var(--c-text); box-sizing: border-box;
  }
  .modal-card textarea:focus { outline: none; border-color: var(--c-purple); }
  .modal-card .hint { font-size: 11px; color: var(--c-text-3); margin-top: 6px; }
  .modal-card .actions { display: flex; gap: 10px; justify-content: flex-end; margin-top: 14px; }
  .modal-card .actions button {
    border: none; padding: 9px 16px; border-radius: 10px;
    font-size: 13px; font-weight: 600; cursor: pointer;
  }
  .modal-card .btn-cancel { background: var(--c-card); color: var(--c-text-2); border: 1px solid var(--c-border); }
  .modal-card .btn-send { background: var(--c-purple); color: #fff; }
  .modal-card .btn-send:disabled { opacity: .6; cursor: wait; }
  .modal-state { font-size: 12px; margin-top: 8px; color: var(--c-text-3); }
  .modal-state.ok { color: var(--c-success); }
  .modal-state.err { color: var(--c-danger); }

  .toast-stack { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 8px; z-index: 9999; }
  .toast { padding: 10px 16px; border-radius: 10px; font-size: 13px; color: #fff;
           background: var(--c-success); box-shadow: 0 4px 12px rgba(15,23,42,.1);
           transition: opacity .3s, transform .3s; min-width: 180px; }
  .toast.error { background: var(--c-danger); }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Seguimiento de prospectos</h1>
    <p class="page-subtitle">Contactos etiquetados como prospecto que <strong>no han agendado</strong>. Retomalos con un mensaje individual o lanza una campaña a varios.</p>

    <form class="toolbar" method="GET">
      <input type="text" name="q" value="{{q}}" placeholder="Buscar por nombre, negocio, sector…" autocomplete="off"/>
      <label>Inactivos hace
        <input type="number" name="dias_min" value="{{dias_min}}" min="0" max="365" placeholder="días"/>
      </label>
      <button type="submit">Buscar</button>
    </form>

    <div class="filters">
      <a href="?estado=todos" class="filter-chip {{active_todos}}"><span>Todos</span><span class="cnt">{{count_total}}</span></a>
      <a href="?estado=nuevo" class="filter-chip {{active_nuevos}}"><span>Nuevos</span><span class="cnt">{{count_nuevos}}</span></a>
      <a href="?estado=calificando" class="filter-chip {{active_calif}}"><span>Calificando</span><span class="cnt">{{count_calif}}</span></a>
      <a href="?estado=cerrado_perdido" class="filter-chip {{active_perdidos}}"><span>Perdidos</span><span class="cnt">{{count_perdidos}}</span></a>
      <a href="?estado=sin_prospecto" class="filter-chip {{active_sin}}"><span>Sin info del bot</span><span class="cnt">{{count_sin}}</span></a>
    </div>

    <div class="bulk-bar">
      <label style="display:flex;align-items:center;gap:8px;font-size:13px;color:var(--c-text-2);">
        <input type="checkbox" id="check-all"/> Seleccionar todos
      </label>
      <span class="bulk-count" id="bulk-count"><strong>0</strong> seleccionados</span>
      <button type="button" class="bulk-btn primary" id="btn-campania" disabled>Enviar campaña</button>
      <button type="button" class="bulk-btn ghost" id="btn-clear" disabled>Limpiar</button>
    </div>

    <div class="prospectos-list" id="prospectos-list">
      {{rows}}
    </div>
  </main>
</div>

<!-- Modal individual -->
<div class="modal" id="modal-single">
  <div class="modal-card">
    <h3>Enviar mensaje</h3>
    <div class="target" id="single-target">—</div>
    <textarea id="single-text" placeholder="Ej: Hola {{nombre}}, te quería preguntar cómo va lo de la pizzería…"></textarea>
    <div class="hint">Usa <code>{{nombre}}</code> para personalizar (se reemplaza por el primer nombre). El mensaje se manda como humano y aparece en su chat.</div>
    <div class="actions">
      <button type="button" class="btn-cancel" id="single-cancel">Cancelar</button>
      <button type="button" class="btn-send" id="single-send">Enviar</button>
    </div>
    <div class="modal-state" id="single-state"></div>
  </div>
</div>

<!-- Modal campaña -->
<div class="modal" id="modal-bulk">
  <div class="modal-card">
    <h3>Campaña a <span id="bulk-target">0</span> prospectos</h3>
    <div class="target">Se envía secuencialmente con delay 6-18s entre envíos para no spammear. Los chats pausados se omiten.</div>
    <textarea id="bulk-text" placeholder="Hola {{nombre}}, te queríamos hacer seguimiento sobre la conversación que tuvimos. ¿Sigues interesado en…?"></textarea>
    <div class="hint">Usa <code>{{nombre}}</code> para personalizar con el primer nombre de cada contacto.</div>
    <div class="actions">
      <button type="button" class="btn-cancel" id="bulk-cancel">Cancelar</button>
      <button type="button" class="btn-send" id="bulk-send">Lanzar campaña</button>
    </div>
    <div class="modal-state" id="bulk-state"></div>
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
    setTimeout(function(){ el.style.opacity='0'; el.style.transform='translateY(20px)'; }, 2600);
    setTimeout(function(){ try { stack.removeChild(el); } catch(e){} }, 2900);
  }

  // ── Multi-selección ─────────────────────────────────────────────
  var checks = document.querySelectorAll('.row-check');
  var checkAll = document.getElementById('check-all');
  var bulkCount = document.getElementById('bulk-count');
  var btnCampania = document.getElementById('btn-campania');
  var btnClear = document.getElementById('btn-clear');

  function actualizarCount(){
    var seleccionados = document.querySelectorAll('.row-check:checked').length;
    bulkCount.innerHTML = '<strong>' + seleccionados + '</strong> seleccionados';
    btnCampania.disabled = seleccionados === 0;
    btnClear.disabled = seleccionados === 0;
    document.querySelectorAll('.prospecto-card').forEach(function(card){
      var c = card.querySelector('.row-check');
      card.classList.toggle('selected', c && c.checked);
    });
  }

  checks.forEach(function(c){ c.addEventListener('change', actualizarCount); });
  checkAll.addEventListener('change', function(){
    checks.forEach(function(c){ c.checked = checkAll.checked; });
    actualizarCount();
  });
  btnClear.addEventListener('click', function(){
    checks.forEach(function(c){ c.checked = false; });
    checkAll.checked = false;
    actualizarCount();
  });

  // ── Modal individual ────────────────────────────────────────────
  var modalSingle = document.getElementById('modal-single');
  var singleTarget = document.getElementById('single-target');
  var singleText = document.getElementById('single-text');
  var singleState = document.getElementById('single-state');
  var singleSend = document.getElementById('single-send');
  var singleCancel = document.getElementById('single-cancel');
  var currentCid = null;

  document.querySelectorAll('.row-btn[data-action="enviar"]').forEach(function(btn){
    btn.addEventListener('click', function(){
      var card = btn.closest('.prospecto-card');
      currentCid = card.dataset.cid;
      singleTarget.textContent = card.dataset.nombre + ' · ' + card.dataset.numero;
      singleText.value = ''; singleState.textContent = '';
      modalSingle.classList.add('open');
      setTimeout(function(){ singleText.focus(); }, 50);
    });
  });
  singleCancel.addEventListener('click', function(){ modalSingle.classList.remove('open'); });
  modalSingle.addEventListener('click', function(e){ if (e.target === modalSingle) modalSingle.classList.remove('open'); });
  singleSend.addEventListener('click', async function(){
    if (!currentCid) return;
    var texto = singleText.value.trim();
    if (!texto) { singleState.textContent = 'Vacío'; singleState.className = 'modal-state err'; return; }
    // Reemplazar {{nombre}} en el preview también, pero el server lo hace
    var card = document.querySelector('.prospecto-card[data-cid="' + currentCid + '"]');
    var primerNombre = card.dataset.nombre.split(' ')[0] || 'Hola';
    var textoFinal = texto.replace(/\\{\\{nombre\\}\\}/g, primerNombre);

    singleSend.disabled = true;
    singleState.textContent = 'Enviando…'; singleState.className = 'modal-state';
    try {
      var fd = new FormData(); fd.append('mensaje', textoFinal);
      var r = await fetch('/admin/seguimiento/' + currentCid + '/enviar', {
        method: 'POST', body: fd,
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        singleState.textContent = '✓ Enviado'; singleState.className = 'modal-state ok';
        toast('Mensaje enviado a ' + primerNombre);
        setTimeout(function(){ modalSingle.classList.remove('open'); }, 800);
      } else {
        singleState.textContent = 'Error: ' + (d.error || r.status);
        singleState.className = 'modal-state err';
      }
    } catch(err) {
      singleState.textContent = 'Error de red'; singleState.className = 'modal-state err';
    }
    singleSend.disabled = false;
  });

  // ── Modal campaña ───────────────────────────────────────────────
  var modalBulk = document.getElementById('modal-bulk');
  var bulkTarget = document.getElementById('bulk-target');
  var bulkText = document.getElementById('bulk-text');
  var bulkState = document.getElementById('bulk-state');
  var bulkSend = document.getElementById('bulk-send');
  var bulkCancel = document.getElementById('bulk-cancel');

  btnCampania.addEventListener('click', function(){
    var n = document.querySelectorAll('.row-check:checked').length;
    if (n === 0) return;
    bulkTarget.textContent = n;
    bulkText.value = ''; bulkState.textContent = '';
    modalBulk.classList.add('open');
    setTimeout(function(){ bulkText.focus(); }, 50);
  });
  bulkCancel.addEventListener('click', function(){ modalBulk.classList.remove('open'); });
  modalBulk.addEventListener('click', function(e){ if (e.target === modalBulk) modalBulk.classList.remove('open'); });
  bulkSend.addEventListener('click', async function(){
    var seleccionados = Array.from(document.querySelectorAll('.row-check:checked')).map(function(c){ return c.dataset.cid; });
    if (seleccionados.length === 0) return;
    var texto = bulkText.value.trim();
    if (!texto) { bulkState.textContent = 'Vacío'; bulkState.className = 'modal-state err'; return; }
    if (!confirm('¿Lanzar campaña a ' + seleccionados.length + ' prospectos? Tarda varios minutos (6-18s entre cada envío).')) return;

    bulkSend.disabled = true;
    bulkState.textContent = 'Lanzando…'; bulkState.className = 'modal-state';
    try {
      var fd = new FormData();
      fd.append('mensaje', texto);
      fd.append('ids', seleccionados.join(','));
      var r = await fetch('/admin/seguimiento/campania', {
        method: 'POST', body: fd,
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        bulkState.textContent = '✓ Campaña lanzada (' + d.total + '). Se envía en background con delay.';
        bulkState.className = 'modal-state ok';
        toast('Campaña lanzada a ' + d.total + ' contactos');
        setTimeout(function(){ modalBulk.classList.remove('open'); }, 1500);
      } else {
        bulkState.textContent = 'Error: ' + (d.error || r.status);
        bulkState.className = 'modal-state err';
      }
    } catch(err) {
      bulkState.textContent = 'Error de red'; bulkState.className = 'modal-state err';
    }
    bulkSend.disabled = false;
  });
})();
</script>
</body></html>"""
