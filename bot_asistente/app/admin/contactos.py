"""/admin/contactos — Vista de contactos (todos los clientes en BD).

Diferencias con /admin/chats:
  - /admin/chats lista contactos que TIENEN al menos una conversación.
  - /admin/contactos lista TODOS los clientes (incluyendo los 17.6k importados
    que aún no han escrito), con búsqueda y filtros por etiqueta.
"""

from __future__ import annotations

import html
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.admin._ui_helpers import (
    avatar_color, format_phone, format_relative_date, get_initials,
    pill_etiqueta, PILL_STYLES,
)
from app.db.models import Cliente
from app.db.session import get_session

router = APIRouter(prefix="/admin/contactos", tags=["admin-contactos"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _fmt_fecha(dt: datetime | None) -> str:
    if not dt:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


ETIQUETAS = [
    ("todos", "Todos", None),
    ("cliente", "Clientes", "cliente"),
    ("prospecto", "Prospectos", "prospecto"),
    ("equipo", "Equipo", "equipo"),
    ("personal", "Personal", "personal"),
    ("sin_clasificar", "Sin clasificar", "__NULL__"),
    ("bloqueado", "Bloqueados", "__BLOQ__"),
]


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def lista_contactos(
    request: Request,
    q: str = "",
    etiqueta: str = "todos",
    page: int = 1,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    per_page = 200
    page = max(1, page)
    offset = (page - 1) * per_page

    # Filtros
    stmt = select(Cliente)
    count_stmt = select(func.count(Cliente.id))

    if q.strip():
        qclean = q.strip().lower()
        like = f"%{qclean}%"
        cond = or_(
            func.lower(func.coalesce(Cliente.nombre, "")).like(like),
            func.lower(Cliente.numero_whatsapp).like(like),
        )
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    et_filter = next((e[2] for e in ETIQUETAS if e[0] == etiqueta), None)
    if et_filter == "__NULL__":
        stmt = stmt.where(Cliente.etiqueta.is_(None))
        count_stmt = count_stmt.where(Cliente.etiqueta.is_(None))
    elif et_filter == "__BLOQ__":
        stmt = stmt.where(Cliente.bloqueado.is_(True))
        count_stmt = count_stmt.where(Cliente.bloqueado.is_(True))
    elif et_filter:
        stmt = stmt.where(Cliente.etiqueta == et_filter)
        count_stmt = count_stmt.where(Cliente.etiqueta == et_filter)

    total = (await session.execute(count_stmt)).scalar_one()
    rows = (await session.execute(
        stmt.order_by(desc(Cliente.ultimo_contacto), desc(Cliente.id)).offset(offset).limit(per_page)
    )).scalars().all()

    # Conteos por etiqueta (para los chips)
    counts_q = await session.execute(select(
        func.count().filter(Cliente.id.isnot(None)).label("total"),
        func.count().filter(Cliente.etiqueta == "cliente").label("cliente"),
        func.count().filter(Cliente.etiqueta == "prospecto").label("prospecto"),
        func.count().filter(Cliente.etiqueta == "equipo").label("equipo"),
        func.count().filter(Cliente.etiqueta == "personal").label("personal"),
        func.count().filter(Cliente.etiqueta.is_(None)).label("sin_clasificar"),
        func.count().filter(Cliente.bloqueado.is_(True)).label("bloqueado"),
    ))
    cr = counts_q.first()
    counts = {
        "todos": cr.total if cr else 0,
        "cliente": cr.cliente if cr else 0,
        "prospecto": cr.prospecto if cr else 0,
        "equipo": cr.equipo if cr else 0,
        "personal": cr.personal if cr else 0,
        "sin_clasificar": cr.sin_clasificar if cr else 0,
        "bloqueado": cr.bloqueado if cr else 0,
    }

    # Construir HTML de chips
    chips_html = []
    for key, label, _ in ETIQUETAS:
        cnt = counts.get(key, 0)
        active = "active" if etiqueta == key else ""
        qs = f"?etiqueta={key}"
        if q.strip():
            qs += f"&q={html.escape(q.strip())}"
        chips_html.append(
            f'<a href="{qs}" class="filter-chip {active}" data-et="{key}">'
            f'<span>{html.escape(label)}</span><span class="filter-chip-count">{cnt}</span></a>'
        )

    # Filas
    rows_html = []
    for c in rows:
        nombre_real = c.nombre or ""
        nombre_es_vacio = not nombre_real.strip()
        nombre_display = nombre_real if not nombre_es_vacio else "(sin nombre)"
        seed = (nombre_real or c.numero_whatsapp or "?").strip()
        bg, fg = avatar_color(seed)
        initials = get_initials(nombre_real, fallback=(c.numero_whatsapp or "?")[-2:])
        et_pill = pill_etiqueta(c.etiqueta, bloqueado=c.bloqueado)
        nombre_html = (
            f'<span class="row-name-text nombre-vacio">{html.escape(nombre_display)}</span>'
            if nombre_es_vacio
            else f'<span class="row-name-text">{html.escape(nombre_display)}</span>'
        )
        rows_html.append(f"""
            <a href="/admin/chats/{c.id}" class="contact-row">
              <div class="row-avatar" style="background:{bg};color:{fg};">{html.escape(initials)}</div>
              <div class="row-main">
                <div class="row-name">{nombre_html}</div>
                <div class="row-num">{html.escape(format_phone(c.numero_whatsapp))}</div>
              </div>
              <div class="row-tag">{et_pill}</div>
              <div class="row-date">{html.escape(format_relative_date(c.ultimo_contacto))}</div>
            </a>
        """)

    pages = max(1, (total + per_page - 1) // per_page)
    pag_html = ""
    if pages > 1:
        prev_qs = f"?etiqueta={etiqueta}&page={max(1, page-1)}"
        next_qs = f"?etiqueta={etiqueta}&page={min(pages, page+1)}"
        if q.strip():
            prev_qs += f"&q={html.escape(q.strip())}"
            next_qs += f"&q={html.escape(q.strip())}"
        pag_html = f"""
          <div class="pag">
            {'<a class="pag-btn" href="' + prev_qs + '">← Anterior</a>' if page > 1 else ''}
            <span class="pag-info">Página {page} de {pages} · {total} contactos</span>
            {'<a class="pag-btn" href="' + next_qs + '">Siguiente →</a>' if page < pages else ''}
          </div>
        """

    body = _CONTACTOS_TEMPLATE
    body = body.replace("__SHELL_STYLES__", SHELL_STYLES)
    body = body.replace("__PILL_STYLES__", PILL_STYLES)
    body = body.replace("__ICON_SPRITE__", ICON_SPRITE)
    body = body.replace("__SIDEBAR__", sidebar_html(active="contactos"))
    body = body.replace("__THEME_JS__", THEME_TOGGLE_JS)
    body = body.replace("{{q}}", html.escape(q))
    body = body.replace("{{chips}}", "".join(chips_html))
    body = body.replace("{{rows}}", "".join(rows_html) or '<div class="empty">No hay contactos que coincidan.</div>')
    body = body.replace("{{total}}", str(total))
    body = body.replace("{{pagination}}", pag_html)
    return HTMLResponse(body)


_CONTACTOS_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Contactos — Dairo</title>
__SHELL_STYLES__
__PILL_STYLES__
<style>
  :root {
    --c-purple: #6366f1;
    --c-purple-hover: #4f46e5;
    --c-purple-soft: #ede9fe;
    --c-purple-softer: #f5f3ff;
    --c-text: #0f172a;
    --c-text-2: #475569;
    --c-text-3: #94a3b8;
    --c-border: #e5e7eb;
    --c-border-soft: #f1f5f9;
    --c-card: #ffffff;
    --c-danger: #ef4444;
  }
  [data-theme="dark"] {
    --c-text: #e2e8f0; --c-text-2: #94a3b8; --c-text-3: #64748b;
    --c-border: #1e293b; --c-border-soft: #1e293b; --c-card: #0f172a;
    --c-purple-soft: #312e81; --c-purple-softer: #1e1b4b;
  }
  .main { padding: 24px 28px; }
  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -0.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 18px; }

  .toolbar {
    display: flex; gap: 12px; align-items: center;
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 14px; padding: 12px 16px; margin-bottom: 14px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }
  .toolbar svg { color: var(--c-text-3); flex-shrink: 0; }
  .toolbar input {
    flex: 1; border: none; outline: none; background: transparent;
    color: var(--c-text); font: inherit; font-size: 14px;
  }

  .filters {
    display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 14px;
  }
  .filter-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 12px; border-radius: 999px;
    background: var(--c-card); border: 1px solid var(--c-border);
    color: var(--c-text-2); font-size: 12px; font-weight: 500;
    text-decoration: none; transition: all .12s;
  }
  .filter-chip:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .filter-chip.active {
    background: var(--c-purple); border-color: var(--c-purple); color: #fff;
  }
  .filter-chip-count {
    background: rgba(255,255,255,.25); color: inherit;
    padding: 1px 7px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
  }
  .filter-chip:not(.active) .filter-chip-count {
    background: var(--c-border-soft); color: var(--c-text-3);
  }

  .contact-table {
    background: var(--c-card); border: 1px solid var(--c-border);
    border-radius: 14px; overflow: hidden;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
  }
  .contact-row {
    display: grid;
    grid-template-columns: 42px minmax(0, 1fr) 120px 160px;
    gap: 14px; align-items: center;
    padding: 12px 18px; border-bottom: 1px solid var(--c-border-soft);
    text-decoration: none; color: inherit;
    transition: background .12s;
  }
  .contact-row:hover { background: var(--c-purple-softer); }
  .contact-row:last-child { border-bottom: none; }
  .row-avatar {
    width: 44px; height: 44px; border-radius: 50%;
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 15px; flex-shrink: 0;
  }
  .row-main { min-width: 0; }
  .row-name {
    font-weight: 600; font-size: 14px; color: var(--c-text);
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    display: flex; align-items: center; gap: 6px;
  }
  .row-name-text.nombre-vacio { font-style: italic; color: var(--c-text-3); font-weight: 500; }
  .row-num { font-size: 12px; color: var(--c-text-2); margin-top: 2px; }
  .row-tag { justify-self: start; }
  .row-date { font-size: 12px; color: var(--c-text-3); text-align: right; }
  .row-badge {
    font-size: 10px; font-weight: 700; padding: 1px 6px; border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.04em;
  }
  .row-badge.danger { background: #fee2e2; color: #b91c1c; }

  .cp-tag {
    display: inline-block; font-size: 11px; font-weight: 600;
    padding: 3px 10px; border-radius: 999px;
    background: var(--c-purple-softer); color: var(--c-purple);
    border: 1px solid color-mix(in srgb, var(--c-purple) 18%, transparent);
  }
  .cp-tag.cliente { background: #ecfdf5; color: #059669; border-color: #a7f3d0; }
  .cp-tag.prospecto { background: #eff6ff; color: #2563eb; border-color: #bfdbfe; }
  .cp-tag.equipo { background: #fef3c7; color: #b45309; border-color: #fde68a; }
  .cp-tag.personal { background: #f1f5f9; color: #475569; border-color: #cbd5e1; }
  .cp-tag.ninguna { background: #fef3c7; color: #92400e; border-color: #fde68a; }

  .pag {
    display: flex; justify-content: space-between; align-items: center;
    padding: 14px 4px; gap: 12px; font-size: 13px;
  }
  .pag-btn {
    padding: 8px 14px; border-radius: 8px;
    background: var(--c-card); border: 1px solid var(--c-border);
    color: var(--c-text); font-weight: 500; text-decoration: none;
  }
  .pag-btn:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .pag-info { color: var(--c-text-2); }
  .empty { padding: 40px 20px; text-align: center; color: var(--c-text-3); }
  @media (max-width: 768px) {
    .contact-row { grid-template-columns: 42px 1fr auto; gap: 10px; }
    .row-tag, .row-date { display: none; }
  }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Contactos</h1>
    <p class="page-subtitle">Todos los contactos en BD ({{total}} totales). Click para abrir el chat.</p>

    <form method="GET" class="toolbar">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/></svg>
      <input type="text" name="q" value="{{q}}" placeholder="Buscar por nombre o número…" autofocus autocomplete="off"/>
      <button type="submit" style="background:var(--c-purple);color:#fff;border:none;padding:6px 14px;border-radius:8px;font-size:13px;cursor:pointer;">Buscar</button>
    </form>

    <div class="filters">{{chips}}</div>

    <div class="contact-table">
      {{rows}}
    </div>

    {{pagination}}
  </main>
</div>
__THEME_JS__
</body></html>"""
