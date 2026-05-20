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
        .limit(500)
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

        # data-search: campo concatenado lowercase para filtrado client-side
        search_blob = " ".join([
            (cliente.nombre or "").lower(),
            (cliente.numero_whatsapp or "").lower(),
            # también sin el "+" para que "57301..." matchee
            (cliente.numero_whatsapp or "").lstrip("+").lower(),
        ])
        items_html.append(f"""
        <a href="/admin/chats/{cliente.id}" class="chat-item" data-search="{html.escape(search_blob)}">
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
        meta_dict = m.metadata_ or {}
        if m.direccion == "humano":
            autor = '<div class="msg-author">👤 asesora humana (WhatsApp directo)</div>'
        elif m.direccion == "outbound":
            via = meta_dict.get("via")
            miembro = meta_dict.get("miembro_equipo") or meta_dict.get("miembro")
            if via == "equipo_admin":
                etiqueta = f"vía admin ({miembro})" if miembro else "vía bot equipo"
                autor = f'<div class="msg-author">🧑‍💼 {html.escape(etiqueta)}</div>'
            elif (m.modelo or "").startswith("claude"):
                autor = '<div class="msg-author">🤖 Laura (bot)</div>'
            else:
                autor = '<div class="msg-author">🤖 bot</div>'

        contenido_texto = (m.contenido or "").strip()
        # Texto base escapado (caption o nada)
        if contenido_texto:
            contenido_html = html.escape(contenido_texto).replace("\n", "<br>")
        else:
            contenido_html = ""

        # Media inline (imagen/sticker/video). Las URLs de whapi son S3 público.
        media_html = ""
        if m.media_url:
            url = html.escape(m.media_url)
            tipo = m.tipo or "media"
            if tipo == "imagen":
                media_html = (
                    f'<a href="{url}" target="_blank" rel="noopener">'
                    f'<img class="msg-media" src="{url}" alt="imagen" loading="lazy"/>'
                    f'</a>'
                )
            elif tipo == "sticker":
                media_html = (
                    f'<img class="msg-sticker" src="{url}" alt="sticker" loading="lazy"/>'
                )
            elif tipo == "video":
                media_html = (
                    f'<video class="msg-media" src="{url}" controls preload="metadata"></video>'
                )
            elif tipo in ("audio",):
                media_html = (
                    f'<audio class="msg-audio" src="{url}" controls preload="none"></audio>'
                )
            else:
                # fallback: link descarga
                media_html = (
                    f'<a class="msg-file" href="{url}" target="_blank" rel="noopener">'
                    f'📎 {html.escape(tipo)}</a>'
                )
        # Si NO hay media pero el contenido es vacío (mensaje raro), mostrar placeholder
        if not media_html and not contenido_html:
            contenido_html = f'<span style="color:var(--text-tertiary);font-style:italic;">[{html.escape(m.tipo or "vacío")}]</span>'

        bubble_inner = media_html + contenido_html

        meta_parts = [_fmt_hora(m.timestamp)]
        if m.intent:
            meta_parts.append(f"intent: {html.escape(m.intent)}")
        if m.costo_usd:
            meta_parts.append(f"${m.costo_usd}")
        meta = " · ".join(meta_parts)

        burbujas.append(f"""
        <div class="msg msg-{side}">
          {autor}
          <div class="msg-bubble">{bubble_inner}</div>
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
    else:
        # Sin pausa activa → ofrecer botón para pausar manualmente
        pausa_banner = f"""
        <div style="background:var(--bg-card);border:1px solid var(--border);
                    border-radius:8px;padding:8px 14px;margin-bottom:12px;
                    display:flex;align-items:center;justify-content:space-between;gap:12px;font-size:13px;">
          <div style="color:var(--text-secondary);">
            <strong style="color:var(--accent-positive);">Laura activa.</strong>
            Responderá automáticamente cuando el cliente escriba.
          </div>
          <form method="POST" action="/admin/actions/cliente/{cliente_id}/pausar-laura" style="margin:0;">
            <button type="submit" class="btn-ghost" style="border:1px solid var(--accent-negative);color:var(--accent-negative);">
              Pausar Laura 1h
            </button>
          </form>
        </div>"""

    flash = ""
    if request.query_params.get("msg") == "sent_ok":
        flash = '<div class="flash">Mensaje enviado. Laura queda pausada 1 hora para que tú manejes la conversación.</div>'
    elif request.query_params.get("msg") == "reactivado":
        flash = '<div class="flash">Laura reactivada. Ya responderá al cliente automáticamente.</div>'
    elif request.query_params.get("msg") == "marcado_interno":
        flash = '<div class="flash">Número marcado como interno. El bot ya no le responderá. Pausa de 24h aplicada para cancelar respuestas pendientes.</div>'
    elif request.query_params.get("msg") == "pausado":
        flash = '<div class="flash">Laura pausada 1h en este chat. No responderá hasta que la reactives o pase la hora.</div>'

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
  .actions .action-btn-internal {
    display: inline-block; padding: 6px 12px; border-radius: 8px;
    background: var(--chip-orange-bg); color: var(--chip-orange);
    border: none; font: inherit; font-size: 12px; font-weight: 500;
    cursor: pointer;
  }
  .actions .action-btn-internal:hover { opacity: .85; }

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
  .msg-media {
    max-width: 280px; max-height: 360px; display: block;
    border-radius: 8px; margin: 2px 0; cursor: pointer;
    background: var(--bg-soft);
  }
  .msg-media + br + *, .msg-bubble .msg-media + * { margin-top: 6px; display: block; }
  .msg-sticker { width: 120px; height: 120px; display: block; }
  .msg-audio { display: block; min-width: 220px; }
  .msg-file { display: inline-block; color: var(--chip-blue); text-decoration: none; padding: 6px 10px;
              background: var(--bg-soft); border-radius: 8px; }
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
<style>
  .chat-search {
    display: flex; align-items: center; gap: 10px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 10px; padding: 10px 14px; margin-bottom: 14px;
    box-shadow: var(--shadow-card);
  }
  .chat-search input {
    flex: 1; border: none; outline: none; background: transparent;
    color: var(--text-primary); font: inherit; font-size: 14px;
  }
  .chat-search .ico-search { color: var(--text-tertiary); }
  .chat-search .filter-count { font-size: 12px; color: var(--text-tertiary); }
  .chat-item.hidden { display: none !important; }
  .empty-state {
    text-align: center; padding: 40px 20px; color: var(--text-tertiary);
    font-size: 14px;
  }
</style>
</head><body>
__ICON_SPRITE__
<div class="app">
  __SIDEBAR__
  <main class="main">
    <h1 class="page-title">Chats</h1>
    <p class="page-subtitle">{{total}} conversaciones activas · Para ver contactos importados sin conversación, abre <a href="/admin/cliente/list" style="color:var(--chip-blue);">Clientes</a>.</p>
    <div class="chat-search">
      <svg class="ico-search" width="16" height="16"><use href="#i-search"/></svg>
      <input type="text" id="chat-filter" placeholder="Buscar por nombre o número (ej: Maria, 31550, +573...)" autofocus autocomplete="off"/>
      <span class="filter-count" id="filter-count"></span>
    </div>
    <div class="chat-list" id="chat-list">
      {{items}}
    </div>
    <div class="empty-state" id="empty-state" style="display:none;">No hay coincidencias.</div>
  </main>
</div>
__THEME_JS__
<script>
  // Buscador client-side por nombre o número (data-search en cada chat-item)
  (function(){
    var input = document.getElementById('chat-filter');
    var items = Array.from(document.querySelectorAll('#chat-list .chat-item'));
    var count = document.getElementById('filter-count');
    var empty = document.getElementById('empty-state');
    var TOTAL = items.length;
    function filtrar() {
      var q = (input.value || '').trim().toLowerCase();
      var visibles = 0;
      items.forEach(function(it){
        var hay = it.getAttribute('data-search') || '';
        var match = !q || hay.indexOf(q) !== -1;
        it.classList.toggle('hidden', !match);
        if (match) visibles++;
      });
      count.textContent = q ? (visibles + ' de ' + TOTAL) : '';
      empty.style.display = (visibles === 0 && q) ? 'block' : 'none';
    }
    input.addEventListener('input', filtrar);
    // Auto-refresh solo si NO se está escribiendo en el buscador
    var lastInput = 0;
    input.addEventListener('input', function(){ lastInput = Date.now(); });
    setTimeout(function(){
      if (Date.now() - lastInput > 5000 && document.activeElement !== input) {
        location.reload();
      } else {
        // Reprogramar 5s más tarde si está activo
        setTimeout(function(){ if (document.activeElement !== input) location.reload(); }, 5000);
      }
    }, 15000);
  })();
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
      <form method="POST" action="/admin/actions/cliente/{{cliente_id}}/marcar-interno"
            style="display:inline;margin:0;"
            onsubmit="return confirm('Marcar +{{numero}} como número interno (bodega/asesora/sistema)? El bot dejará de responderle.');">
        <button type="submit" class="action-btn-internal">Marcar como interno</button>
      </form>
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
