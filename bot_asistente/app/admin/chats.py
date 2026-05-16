"""
/admin/chats — Vista tipo WhatsApp: lista de clientes con último mensaje,
y al click ver el hilo completo de la conversación.

Mejor que la vista de "Conversaciones" plana (es ilegible con muchas filas).
"""

from __future__ import annotations

import html
from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.db.models import Cliente, Conversacion, EquipoMiembro
from app.db.repos import guardar_conversacion, pausar_bot
from app.db.session import get_session
from app.logging_setup import log
from app.whapi.client import enviar_texto

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

    # Inner join: solo clientes que YA tienen al menos un mensaje en `conversaciones`.
    # Contactos importados (sin conversación) NO aparecen aquí — están en /admin/cliente/list.
    stmt = (
        select(Cliente, subq.c.ultima_ts, subq.c.total_msgs)
        .join(subq, subq.c.cliente_id == Cliente.id)
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

    # Set de números que son admins (para mostrar badge "ADMIN" en la lista)
    admins_numeros = set((await session.execute(
        select(EquipoMiembro.numero_whatsapp).where(EquipoMiembro.activo.is_(True))
    )).scalars().all())

    items_html: list[str] = []
    for cliente, ultima_ts, total_msgs in rows:
        direccion, preview = last_msgs.get(cliente.id, ("", "Sin mensajes"))
        prefix = "📤 " if direccion == "outbound" else ("👤 " if direccion == "inbound" else "")
        avatar_initial = (cliente.nombre or cliente.numero_whatsapp or "?")[0].upper()
        nombre_mostrar = cliente.nombre or "(sin nombre)"
        bloqueado_badge = '<span class="badge badge-blocked">bloqueado</span>' if cliente.bloqueado else ''
        admin_badge = '<span class="badge badge-admin">ADMIN</span>' if cliente.numero_whatsapp in admins_numeros else ''

        items_html.append(f"""
        <a href="/admin/chats/{cliente.id}" class="chat-item">
          <div class="avatar">{html.escape(avatar_initial)}</div>
          <div class="chat-body">
            <div class="chat-top">
              <span class="chat-name">{html.escape(nombre_mostrar)} {admin_badge} {bloqueado_badge}</span>
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
                 .replace("__SHELL_STYLES__", SHELL_STYLES)
                 .replace("__EXTRA_STYLES__", _CHATS_EXTRA_STYLES)
                 .replace("__ICON_SPRITE__", ICON_SPRITE)
                 .replace("__SIDEBAR__", sidebar_html(active="chats"))
                 .replace("__THEME_JS__", THEME_TOGGLE_JS)
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

    # ¿Bot pausado para este cliente? (intervención humana activa)
    from sqlalchemy import text as sa_text
    pausa_row = (await session.execute(sa_text(
        "SELECT pausado_hasta, razon FROM intervencion_humana "
        "WHERE cliente_id = :cid AND pausado_hasta > now()"
    ), {"cid": cliente_id})).first()
    pausa_banner = ""
    if pausa_row:
        from datetime import datetime, timezone
        ph = pausa_row[0]
        if ph.tzinfo is None:
            ph = ph.replace(tzinfo=timezone.utc)
        minutos_restantes = max(0, int((ph - datetime.now(timezone.utc)).total_seconds() / 60))
        pausa_banner = f"""
        <div style="background:var(--accent-negative-bg);border:1px solid var(--accent-negative);
                    border-radius:8px;padding:10px 14px;margin-bottom:12px;
                    display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;">
          <div style="color:var(--accent-negative);">
            <strong>Laura está pausada en este chat.</strong>
            Restan ~{minutos_restantes} min. Razón: {html.escape(pausa_row[1] or 'asesora humana intervino')}
          </div>
          <form method="POST" action="/admin/actions/cliente/{cliente_id}/reactivar-laura" style="margin:0;">
            <button type="submit" class="btn-primary" style="background:var(--accent-positive);color:#fff;">
              Reactivar Laura
            </button>
          </form>
        </div>"""

    flash = ""
    if request.query_params.get("msg") == "sent_ok":
        flash = '<div class="flash">Mensaje enviado. Laura queda pausada 1 hora para que tú manejes la conversación.</div>'
    elif request.query_params.get("msg") == "reactivado":
        flash = '<div class="flash">Laura reactivada. Ya responderá al cliente automáticamente.</div>'

    # El pausa banner se inyecta arriba del thread reusando el placeholder de flash
    flash = pausa_banner + flash

    html_resp = (_HILO_TEMPLATE
                 .replace("__SHELL_STYLES__", SHELL_STYLES)
                 .replace("__EXTRA_STYLES__", _CHATS_EXTRA_STYLES)
                 .replace("__ICON_SPRITE__", ICON_SPRITE)
                 .replace("__SIDEBAR__", sidebar_html(active="chats"))
                 .replace("__THEME_JS__", THEME_TOGGLE_JS))
    repls = {
        "{{nombre}}": html.escape(nombre),
        "{{numero}}": html.escape(cliente.numero_whatsapp),
        "{{total}}": str(total),
        "{{cliente_id}}": str(cliente.id),
        "{{ciudad}}": html.escape(cliente.ciudad or "—"),
        "{{barrio}}": html.escape(cliente.barrio or "—"),
        "{{ultimo_contacto}}": _fmt_fecha(cliente.ultimo_contacto),
        "{{bloqueado_chip}}": bloqueado_chip,
        "{{burbujas}}": "".join(burbujas) or '<p style="text-align:center;color:var(--text-tertiary);">Sin mensajes.</p>',
        "{{flash}}": flash,
    }
    for k, v in repls.items():
        html_resp = html_resp.replace(k, v)
    return HTMLResponse(html_resp)


@router.post("/cliente/{cliente_id}/send")
async def enviar_mensaje_manual(
    cliente_id: int,
    request: Request,
    mensaje: str = Form(...),
    session: AsyncSession = Depends(get_session),
):
    """Envía un mensaje manual al cliente desde el admin.

    Pausa el bot 4 horas (registramos como direccion='humano') para que
    el bot no responda encima mientras el operador maneja la conversación.
    """
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    texto = (mensaje or "").strip()
    if not texto:
        return RedirectResponse(f"/admin/chats/cliente/{cliente_id}", status_code=303)

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    try:
        await enviar_texto(cliente.numero_whatsapp, texto)
    except Exception as e:
        log.error("admin.chats.enviar_fail", cliente_id=cliente_id, error=str(e))
        raise HTTPException(502, f"Falló envío whapi: {e}")

    await guardar_conversacion(
        session,
        cliente_id=cliente_id,
        direccion="humano",
        tipo="texto",
        contenido=texto,
        metadata={"via": "admin_chats"},
    )
    await pausar_bot(session, cliente_id, horas=1, razon="enviado desde admin/chats")
    await session.commit()

    log.info(
        "admin.chats.enviado",
        cliente_id=cliente_id,
        numero=cliente.numero_whatsapp,
        chars=len(texto),
    )
    return RedirectResponse(f"/admin/chats/cliente/{cliente_id}?msg=sent_ok", status_code=303)


# ────────────────────────────────────────────────────────────────────────────
# Templates HTML inline
# ────────────────────────────────────────────────────────────────────────────


_CHATS_EXTRA_STYLES = """
<style>
  .page-title { font-size: 22px; font-weight: 600; margin: 0 0 4px; color: var(--text-primary); }
  .page-subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 20px; }

  .chat-list { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden; box-shadow: var(--shadow-card); }
  .chat-item {
    display: flex; align-items: center; gap: 14px; padding: 14px 18px;
    border-bottom: 1px solid var(--border-subtle); text-decoration: none; color: inherit;
    transition: background 0.15s;
  }
  .chat-item:hover { background: var(--bg-soft); }
  .chat-item:last-child { border-bottom: none; }
  .avatar {
    width: 42px; height: 42px; border-radius: 50%;
    background: var(--chip-blue-bg); color: var(--chip-blue);
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 16px; flex-shrink: 0;
  }
  .chat-body { flex: 1; min-width: 0; }
  .chat-top { display: flex; justify-content: space-between; align-items: baseline; }
  .chat-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .chat-time { font-size: 11px; color: var(--text-tertiary); flex-shrink: 0; }
  .chat-bottom { display: flex; justify-content: space-between; margin-top: 2px; }
  .chat-preview { font-size: 13px; color: var(--text-secondary); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 600px; }
  .chat-count {
    background: var(--chip-green-bg); color: var(--chip-green);
    font-size: 10px; font-weight: 600; padding: 2px 7px; border-radius: 999px;
  }
  .chat-meta { font-size: 11px; color: var(--text-tertiary); margin-top: 2px; }

  /* Hilo (vista detalle de chat) */
  .header-info { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 18px 20px; margin-bottom: 16px; box-shadow: var(--shadow-card); }
  .header-info .name { font-weight: 700; font-size: 18px; color: var(--text-primary); }
  .header-info .meta-row { display: flex; gap: 24px; margin-top: 8px; font-size: 12px; color: var(--text-secondary); flex-wrap: wrap; }
  .header-info .meta-row strong { color: var(--text-primary); font-weight: 500; }
  .actions { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }
  .actions a {
    display: inline-block; padding: 6px 12px; border-radius: 8px; text-decoration: none;
    font-size: 12px; font-weight: 500; background: var(--chip-blue-bg); color: var(--chip-blue);
  }
  .actions a.danger { background: var(--accent-negative-bg); color: var(--accent-negative); }

  .thread {
    background: var(--bg-soft); border-radius: 12px; padding: 18px 14px;
    min-height: 400px; max-height: 70vh; overflow-y: auto;
    border: 1px solid var(--border);
  }
  .date-sep {
    text-align: center; font-size: 11px; color: var(--text-tertiary);
    background: var(--bg-card); display: inline-block;
    padding: 3px 14px; border-radius: 999px; margin: 14px auto 6px;
    left: 50%; position: relative; transform: translateX(-50%);
    border: 1px solid var(--border-subtle);
  }
  .msg { margin: 8px 0; display: flex; flex-direction: column; max-width: 75%; }
  .msg-in { align-items: flex-start; margin-right: auto; }
  .msg-out { align-items: flex-end; margin-left: auto; }
  .msg-bubble {
    padding: 10px 14px; border-radius: 12px; font-size: 14px; line-height: 1.45;
    word-wrap: break-word; white-space: pre-wrap;
  }
  .msg-in .msg-bubble { background: var(--bg-card); color: var(--text-primary); border: 1px solid var(--border); border-bottom-left-radius: 4px; }
  .msg-out .msg-bubble { background: var(--chip-green-bg); color: var(--text-primary); border: 1px solid var(--chip-green-bg); border-bottom-right-radius: 4px; }
  .msg-meta { font-size: 10px; color: var(--text-tertiary); margin-top: 3px; padding: 0 4px; }
  .msg-author { font-size: 10px; color: var(--text-tertiary); font-style: italic; margin-bottom: 2px; padding: 0 4px; }

  .send-box { background: var(--bg-card); border: 1px solid var(--border); border-radius: 12px; padding: 14px; margin-top: 14px; box-shadow: var(--shadow-card); }
  .send-box textarea {
    width: 100%; min-height: 60px; resize: vertical;
    border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; font: inherit; font-size: 13px;
    background: var(--bg-card); color: var(--text-primary);
    box-sizing: border-box;
  }
  .send-box .send-row { display: flex; justify-content: space-between; align-items: center; margin-top: 8px; gap: 8px; }
  .send-box .hint { font-size: 11px; color: var(--text-tertiary); flex: 1; }
  .send-btn { background: var(--accent-positive); color: #fff; border: none; padding: 8px 16px; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer; }
  .send-btn:hover { opacity: .9; }
  .flash {
    background: var(--accent-positive-bg); color: var(--accent-positive);
    border: 1px solid var(--accent-positive); padding: 8px 12px;
    border-radius: 8px; font-size: 13px; margin-bottom: 12px;
  }
  .stats { margin-bottom: 12px; font-size: 13px; color: var(--text-secondary); }
</style>
"""

_LISTA_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chats — Laura</title>
__SHELL_STYLES__
__EXTRA_STYLES__
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Chats</h1>
    <p class="page-subtitle">{{total}} conversaciones activas · Para ver contactos importados sin conversación, abre <a href="/admin/cliente/list" style="color:var(--chip-blue);">Clientes</a>.</p>
    <div class="chat-list">
      {{items}}
    </div>
  </main>
</div>
__THEME_JS__
<script>
  // Auto-refresh cada 15s para ver chats nuevos
  setTimeout(function(){ location.reload(); }, 15000);
</script>
</body></html>"""

_HILO_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chat — {{numero}}</title>
__SHELL_STYLES__
__EXTRA_STYLES__
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <div style="margin-bottom: 16px;">
      <a href="/admin/chats" class="btn-ghost">
        <svg class="ico" width="12" height="12"><use href="#i-back"/></svg> Chats
      </a>
    </div>
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
      <a href="/admin/actions/cliente/{{cliente_id}}/nuke-form" class="danger">Eliminar cliente completo</a>
    </div>
  </div>
  {{flash}}
  <div class="thread">
    {{burbujas}}
  </div>
  <div class="send-box">
    <form method="POST" action="/admin/chats/cliente/{{cliente_id}}/send">
      <textarea name="mensaje" placeholder="Escribe un mensaje al cliente (Laura queda pausada 1h)..." required></textarea>
      <div class="send-row">
        <span class="hint">Se envía vía whapi como mensaje humano. Laura queda pausada 1 h. Si quieres que Laura retome antes, usa el botón "Reactivar Laura" del banner.</span>
        <button type="submit" class="send-btn">Enviar</button>
      </div>
    </form>
  </div>
  </main>
</div>
__THEME_JS__
<script>
  // Scroll al final del hilo
  const t = document.querySelector('.thread');
  if (t) t.scrollTop = t.scrollHeight;
  // Auto-refresh cada 12s para ver mensajes nuevos del cliente y respuestas
  // del bot que llegan después de la humanización (60-180s).
  setTimeout(function(){ location.reload(); }, 12000);
</script>
</body></html>"""
