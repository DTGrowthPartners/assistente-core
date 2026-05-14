"""
/admin/chats — Vista tipo WhatsApp: lista de clientes con último mensaje,
y al click ver el hilo completo de la conversación.

Mejor que la vista de "Conversaciones" plana (es ilegible con muchas filas).
"""

from __future__ import annotations

import html
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Cliente, Conversacion
from app.db.session import get_session

router = APIRouter(prefix="/admin/chats", tags=["admin-chats"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


def _fmt_hora(dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%H:%M")
    except Exception:
        return dt.strftime("%H:%M")


def _fmt_fecha(dt: datetime | None) -> str:
    if not dt:
        return ""
    try:
        from zoneinfo import ZoneInfo
        return dt.astimezone(ZoneInfo("America/Bogota")).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return dt.strftime("%Y-%m-%d %H:%M")


# ─── Lista de chats ────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def lista_chats(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    # Última conversación por cliente
    subq = (
        select(
            Conversacion.cliente_id,
            func.max(Conversacion.timestamp).label("ultima_ts"),
            func.count(Conversacion.id).label("total_msgs"),
        )
        .group_by(Conversacion.cliente_id)
        .subquery()
    )

    stmt = (
        select(Cliente, subq.c.ultima_ts, subq.c.total_msgs)
        .join(subq, subq.c.cliente_id == Cliente.id, isouter=True)
        .order_by(desc(subq.c.ultima_ts))
        .limit(200)
    )
    rows = (await session.execute(stmt)).all()

    # Para cada cliente, traer el último mensaje (texto preview)
    last_msgs: dict[int, tuple[str, str]] = {}
    for cliente, _, _ in rows:
        last_msg = (await session.execute(
            select(Conversacion.direccion, Conversacion.contenido)
            .where(Conversacion.cliente_id == cliente.id)
            .order_by(desc(Conversacion.timestamp))
            .limit(1)
        )).first()
        if last_msg:
            direccion, contenido = last_msg
            preview = (contenido or "[media]")[:80]
            last_msgs[cliente.id] = (direccion, preview)

    items_html: list[str] = []
    for cliente, ultima_ts, total_msgs in rows:
        direccion, preview = last_msgs.get(cliente.id, ("", "Sin mensajes"))
        prefix = "📤 " if direccion == "outbound" else ("👤 " if direccion == "inbound" else "")
        avatar_initial = (cliente.nombre or cliente.numero_whatsapp or "?")[0].upper()
        nombre_mostrar = cliente.nombre or "(sin nombre)"
        bloqueado_badge = '<span class="badge badge-blocked">bloqueado</span>' if cliente.bloqueado else ''

        items_html.append(f"""
        <a href="/admin/chats/{cliente.id}" class="chat-item">
          <div class="avatar">{html.escape(avatar_initial)}</div>
          <div class="chat-body">
            <div class="chat-top">
              <span class="chat-name">{html.escape(nombre_mostrar)} {bloqueado_badge}</span>
              <span class="chat-time">{_fmt_hora(ultima_ts) if ultima_ts else ''}</span>
            </div>
            <div class="chat-bottom">
              <span class="chat-preview">{html.escape(prefix)}{html.escape(preview)}</span>
              <span class="chat-count">{total_msgs or 0}</span>
            </div>
            <div class="chat-meta">{html.escape(cliente.numero_whatsapp)}</div>
          </div>
        </a>
        """)

    html_resp = (_LISTA_TEMPLATE
                 .replace("__BASE_STYLES__", _BASE_STYLES)
                 .replace("{{total}}", str(len(rows)))
                 .replace("{{items}}", "".join(items_html)))
    return HTMLResponse(html_resp)


# ─── Hilo de conversación de un cliente ────────────────────────────────────


@router.get("/{cliente_id}", response_class=HTMLResponse)
async def chat_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    mensajes = (await session.execute(
        select(Conversacion)
        .where(Conversacion.cliente_id == cliente_id)
        .order_by(Conversacion.timestamp)
    )).scalars().all()

    burbujas: list[str] = []
    fecha_actual = ""
    for m in mensajes:
        fecha_msg = m.timestamp.strftime("%Y-%m-%d") if m.timestamp else ""
        if fecha_msg != fecha_actual:
            burbujas.append(f'<div class="date-sep">{html.escape(fecha_msg)}</div>')
            fecha_actual = fecha_msg

        side = "out" if m.direccion in ("outbound", "humano") else "in"
        autor = ""
        if m.direccion == "humano":
            autor = '<div class="msg-author">— asesora humana</div>'
        elif m.direccion == "outbound":
            via = (m.metadata_ or {}).get("via")
            if via == "equipo_admin":
                autor = '<div class="msg-author">— enviado por equipo (via bot)</div>'

        contenido = m.contenido or ""
        if m.media_url:
            contenido = f"[{m.tipo or 'media'}] {contenido}".strip()
        contenido_html = html.escape(contenido).replace("\n", "<br>")

        meta_parts = [_fmt_hora(m.timestamp)]
        if m.intent:
            meta_parts.append(f"intent: {html.escape(m.intent)}")
        if m.costo_usd:
            meta_parts.append(f"${m.costo_usd}")
        meta = " · ".join(meta_parts)

        burbujas.append(f"""
        <div class="msg msg-{side}">
          {autor}
          <div class="msg-bubble">{contenido_html}</div>
          <div class="msg-meta">{meta}</div>
        </div>
        """)

    nombre = cliente.nombre or "(sin nombre)"
    total = len(mensajes)
    bloqueado_chip = '<span class="badge badge-blocked">BLOQUEADO</span>' if cliente.bloqueado else ''

    html_resp = _HILO_TEMPLATE.replace("__BASE_STYLES__", _BASE_STYLES)
    repls = {
        "{{nombre}}": html.escape(nombre),
        "{{numero}}": html.escape(cliente.numero_whatsapp),
        "{{total}}": str(total),
        "{{cliente_id}}": str(cliente.id),
        "{{ciudad}}": html.escape(cliente.ciudad or "—"),
        "{{barrio}}": html.escape(cliente.barrio or "—"),
        "{{ultimo_contacto}}": _fmt_fecha(cliente.ultimo_contacto),
        "{{bloqueado_chip}}": bloqueado_chip,
        "{{burbujas}}": "".join(burbujas) or '<p style="text-align:center;color:#9ca3af;">Sin mensajes.</p>',
    }
    for k, v in repls.items():
        html_resp = html_resp.replace(k, v)
    return HTMLResponse(html_resp)


# ────────────────────────────────────────────────────────────────────────────
# Templates HTML inline
# ────────────────────────────────────────────────────────────────────────────


_BASE_STYLES = """
<style>
  :root {
    --bg: #f4f6f9; --surface: #ffffff; --border: #e5e7eb;
    --text: #111827; --muted: #6b7280; --soft: #f3f4f6;
    --link: #1d4ed8; --link-bg: #eff6ff;
    --green: #d1fae5; --green-dark: #065f46;
  }
  * { box-sizing: border-box; }
  body { background: var(--bg); margin: 0; font-family: Inter, system-ui, -apple-system, sans-serif; color: var(--text); }
  .topbar { background: var(--surface); border-bottom: 1px solid var(--border); padding: 14px 24px; display: flex; align-items: center; gap: 16px; }
  .topbar a { color: var(--link); text-decoration: none; font-size: 13px; font-weight: 500; }
  .topbar h1 { font-size: 18px; font-weight: 700; margin: 0; }
  .container { max-width: 980px; margin: 0 auto; padding: 24px; }
  .badge { display: inline-block; font-size: 10px; font-weight: 600; padding: 2px 8px; border-radius: 999px; }
  .badge-blocked { background: #fee2e2; color: #991b1b; margin-left: 6px; }
</style>
"""

_LISTA_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Chats — Asistente</title>
__BASE_STYLES__
<style>
  .chat-list { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; }
  .chat-item {
    display: flex; align-items: center; gap: 14px; padding: 14px 18px;
    border-bottom: 1px solid var(--soft); text-decoration: none; color: inherit;
    transition: background 0.15s;
  }
  .chat-item:hover { background: #f9fafb; }
  .chat-item:last-child { border-bottom: none; }
  .avatar {
    width: 42px; height: 42px; border-radius: 50%; background: var(--link-bg);
    color: var(--link); display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 16px; flex-shrink: 0;
  }
  .chat-body { flex: 1; min-width: 0; }
  .chat-top { display: flex; justify-content: space-between; align-items: baseline; }
  .chat-name { font-weight: 600; font-size: 14px; color: var(--text); }
  .chat-time { font-size: 11px; color: var(--muted); flex-shrink: 0; }
  .chat-bottom { display: flex; justify-content: space-between; margin-top: 2px; }
  .chat-preview { font-size: 13px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 600px; }
  .chat-count { background: var(--green); color: var(--green-dark); font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px; }
  .chat-meta { font-size: 11px; color: #9ca3af; margin-top: 2px; font-family: 'Inter', monospace; }
  .stats { margin-bottom: 12px; font-size: 13px; color: var(--muted); }
</style>
</head><body>
<div class="topbar">
  <a href="/admin">← Volver al admin</a>
  <h1>Chats</h1>
</div>
<div class="container">
  <p class="stats">{{total}} clientes con conversación.</p>
  <div class="chat-list">
    {{items}}
  </div>
</div>
</body></html>"""

_HILO_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8"><title>Chat — {{numero}}</title>
__BASE_STYLES__
<style>
  .header-info { background: var(--surface); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; }
  .header-info .name { font-weight: 700; font-size: 18px; }
  .header-info .meta-row { display: flex; gap: 24px; margin-top: 8px; font-size: 12px; color: var(--muted); flex-wrap: wrap; }
  .header-info .meta-row strong { color: var(--text); font-weight: 500; }
  .actions { margin-top: 12px; display: flex; gap: 8px; }
  .actions a { display: inline-block; padding: 6px 12px; border-radius: 8px; text-decoration: none;
              font-size: 12px; font-weight: 500; background: var(--link-bg); color: var(--link); }
  .actions a.danger { background: #fef2f2; color: #dc2626; }

  .thread { background: #e7eaf0; border-radius: 12px; padding: 18px 14px; min-height: 400px; max-height: 75vh; overflow-y: auto; }
  .date-sep { text-align: center; font-size: 11px; color: var(--muted); background: #fff; display: inline-block;
              padding: 3px 14px; border-radius: 999px; margin: 14px auto 6px; left: 50%; position: relative; transform: translateX(-50%); }
  .msg { margin: 8px 0; display: flex; flex-direction: column; max-width: 75%; }
  .msg-in { align-items: flex-start; margin-right: auto; }
  .msg-out { align-items: flex-end; margin-left: auto; }
  .msg-bubble {
    padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.45;
    word-wrap: break-word; white-space: pre-wrap;
  }
  .msg-in .msg-bubble { background: #ffffff; color: var(--text); border-bottom-left-radius: 4px; }
  .msg-out .msg-bubble { background: #dcf8c6; color: #111827; border-bottom-right-radius: 4px; }
  .msg-meta { font-size: 10px; color: var(--muted); margin-top: 3px; padding: 0 4px; }
  .msg-author { font-size: 10px; color: #8a8f99; font-style: italic; margin-bottom: 2px; padding: 0 4px; }
</style>
</head><body>
<div class="topbar">
  <a href="/admin/chats">← Chats</a>
  <h1>{{nombre}}</h1>
</div>
<div class="container">
  <div class="header-info">
    <div class="name">{{nombre}} {{bloqueado_chip}}</div>
    <div class="meta-row">
      <span><strong>{{numero}}</strong></span>
      <span>Ciudad: {{ciudad}}</span>
      <span>Barrio: {{barrio}}</span>
      <span>Total mensajes: {{total}}</span>
      <span>Último contacto: {{ultimo_contacto}}</span>
    </div>
    <div class="actions">
      <a href="/admin/cliente/details/{{cliente_id}}">Editar datos</a>
      <a href="/admin/actions/cliente/{{cliente_id}}/reset-form" class="danger">Resetear conversación</a>
    </div>
  </div>
  <div class="thread">
    {{burbujas}}
  </div>
</div>
<script>
  // Scroll al final del hilo
  const t = document.querySelector('.thread');
  if (t) t.scrollTop = t.scrollHeight;
</script>
</body></html>"""
