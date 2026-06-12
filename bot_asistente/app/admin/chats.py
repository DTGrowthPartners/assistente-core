"""
/admin/chats — Vista tipo WhatsApp: lista de clientes con último mensaje,
y al click ver el hilo completo de la conversación.

Mejor que la vista de "Conversaciones" plana (es ilegible con muchas filas).
"""

from __future__ import annotations

import html
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from sqlalchemy import desc, func, select, update
from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.admin._ui_helpers import (
    avatar_color, format_phone, format_relative_date,
    get_initials, pill_etiqueta, PILL_STYLES,
)
from app.config import get_settings
from app.db.models import Cliente, Conversacion, EquipoMiembro
from app.db.repos import guardar_conversacion, pausar_bot
from app.db.session import get_session
from app.identidades import principal as _identidad_principal
from app.logging_setup import log
from app.whapi.client import enviar_texto

_settings = get_settings()

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


# ─── Helpers de render (reusados por las vistas HTML y los endpoints JSON) ──


async def _construir_lista_items(session: AsyncSession) -> tuple[str, int]:
    """Devuelve (html de los chat-items, total). Reusado por la vista y el JSON poll."""
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
        .join(subq, subq.c.cliente_id == Cliente.id)
        .order_by(desc(subq.c.ultima_ts))
        .limit(500)
    )
    rows = (await session.execute(stmt)).all()

    last_msgs: dict[int, tuple[str, str]] = {}
    for cliente, _, _ in rows:
        last_msg = (await session.execute(
            select(Conversacion.direccion, Conversacion.contenido)
            .where(Conversacion.cliente_id == cliente.id)
            .order_by(desc(Conversacion.timestamp), desc(Conversacion.id))
            .limit(1)
        )).first()
        if last_msg:
            direccion, contenido = last_msg
            preview = (contenido or "[media]")[:80]
            last_msgs[cliente.id] = (direccion, preview)

    admins_numeros = set((await session.execute(
        select(EquipoMiembro.numero_whatsapp).where(EquipoMiembro.activo.is_(True))
    )).scalars().all())

    # Tags por cliente (una sola query, devuelve [(cliente_id, nombre, color)])
    cliente_ids = [c.id for c, _, _ in rows]
    tags_por_cliente: dict[int, list[tuple[str, str]]] = {}
    if cliente_ids:
        from sqlalchemy import text as _sa_text_local
        tag_rows = (await session.execute(_sa_text_local("""
            SELECT ct.cliente_id, t.id, t.nombre, t.color, t.orden
              FROM cliente_tags ct
              JOIN tags t ON t.id = ct.tag_id
             WHERE ct.cliente_id = ANY(:ids)
             ORDER BY t.orden ASC, t.nombre ASC
        """), {"ids": cliente_ids})).all()
        for tr in tag_rows:
            tags_por_cliente.setdefault(tr.cliente_id, []).append((tr.nombre, tr.color, tr.id))

    items_html: list[str] = []
    for cliente, ultima_ts, total_msgs in rows:
        direccion, preview = last_msgs.get(cliente.id, ("", "Sin mensajes"))
        # Iconito según último mensaje + abreviación si es media
        if preview.startswith("[") and preview.endswith("]"):
            preview_display = preview
        else:
            preview_display = preview
        if direccion == "outbound":
            prefix_ico = '<span class="prev-ico">📤</span>'
        elif direccion == "humano":
            prefix_ico = '<span class="prev-ico">👤</span>'
        elif direccion == "inbound":
            prefix_ico = ""
        else:
            prefix_ico = ""

        nombre_mostrar = cliente.nombre or "(sin nombre)"
        nombre_es_vacio = not (cliente.nombre or "").strip()
        seed = (cliente.nombre or cliente.numero_whatsapp or "?").strip()
        bg, fg = avatar_color(seed)
        initials = get_initials(cliente.nombre, fallback=(cliente.numero_whatsapp or "?")[-2:])

        admin_badge = '<span class="pill" style="background:#FEF3C7;color:#92400E;font-size:10px;">ADMIN</span>' if cliente.numero_whatsapp in admins_numeros else ''
        bloqueado_badge = '<span class="pill pill--bloqueado" style="font-size:10px;">BLOQ</span>' if cliente.bloqueado else ''
        etiq = (cliente.etiqueta or "").lower()
        et_pill = pill_etiqueta(cliente.etiqueta, bloqueado=False) if etiq else ""

        tags_cliente = tags_por_cliente.get(cliente.id, [])
        tags_chips_html = ""
        tag_ids_data = ""
        if tags_cliente:
            tag_ids_data = ",".join(str(tid) for _, _, tid in tags_cliente)
            tags_chips_html = '<div class="chat-tags">' + "".join(
                f'<span class="chat-tag-chip" style="background:{tcolor};">{html.escape(tname)}</span>'
                for tname, tcolor, _ in tags_cliente
            ) + '</div>'

        search_blob = " ".join([
            (cliente.nombre or "").lower(),
            (cliente.numero_whatsapp or "").lower(),
            (cliente.numero_whatsapp or "").lstrip("+").lower(),
            " ".join(tn.lower() for tn, _, _ in tags_cliente),
        ])
        data_estado = etiq or "ninguna"
        nombre_html = (f'<span class="chat-name nombre-vacio">{html.escape(nombre_mostrar)}</span>'
                       if nombre_es_vacio else
                       f'<span class="chat-name">{html.escape(nombre_mostrar)}</span>')
        items_html.append(f"""
        <a href="/admin/chats/{cliente.id}" class="chat-item" data-search="{html.escape(search_blob)}" data-estado="{data_estado}" data-cliente="{cliente.id}" data-nombre="{html.escape(nombre_mostrar)}" data-tags="{tag_ids_data}">
          <div class="avatar" style="background:{bg};color:{fg};">{html.escape(initials)}</div>
          <div class="chat-body">
            <div class="chat-top">
              {nombre_html}{admin_badge}{bloqueado_badge}
              <span class="chat-time">{format_relative_date(ultima_ts) if ultima_ts else ''}</span>
            </div>
            <div class="chat-bottom">
              <span class="chat-preview">{prefix_ico}{html.escape(preview_display)}</span>
              <span class="chat-count">{(str(total_msgs) if total_msgs and total_msgs < 100 else '99+') if total_msgs else '0'}</span>
            </div>
            <div class="chat-meta">{html.escape(format_phone(cliente.numero_whatsapp))} {et_pill}</div>
            {tags_chips_html}
          </div>
        </a>
        """)
    return "".join(items_html), len(rows)


def _burbuja_html(m: Conversacion, cliente_initial: str = "?") -> str:
    """Renderiza UNA conversación como burbuja con avatar + check de envío."""
    side = "out" if m.direccion in ("outbound", "humano") else "in"
    meta_dict = m.metadata_ or {}
    es_nota = bool(meta_dict.get("nota_interna"))

    # Avatar y autor según el tipo de mensaje.
    if es_nota:
        avatar_text = "📝"
        avatar_cls = "msg-avatar"
        autor_txt = f"Nota interna · {meta_dict.get('autor', 'admin')}"
        side = "out"  # las notas se muestran del lado del operador
    elif side == "in":
        avatar_text = html.escape(cliente_initial)
        avatar_cls = "msg-avatar msg-avatar-in"
        autor_txt = ""
    else:
        if m.direccion == "humano":
            avatar_text = "👤"
            avatar_cls = "msg-avatar msg-avatar-human"
            autor_txt = "Asesor humano"
        else:
            via = meta_dict.get("via")
            miembro = meta_dict.get("miembro_equipo") or meta_dict.get("miembro")
            if via in ("equipo_admin", "admin_chats"):
                avatar_text = "🧑‍💼"
                avatar_cls = "msg-avatar msg-avatar-admin"
                autor_txt = f"vía admin ({miembro})" if miembro else "vía admin"
            else:
                avatar_text = "D"
                avatar_cls = "msg-avatar msg-avatar-bot"
                autor_txt = "Dairo"

    contenido_texto = (m.contenido or "").strip()
    contenido_html = html.escape(contenido_texto).replace("\n", "<br>") if contenido_texto else ""

    media_html = ""
    tipo = m.tipo or "media"
    # Para todos los tipos de media inbound, whapi NO entrega link directo —
    # usamos el proxy del admin (resuelve el media_id automáticamente).
    proxy_src = f"/admin/chats/_media/{m.id}"
    es_inbound = side == "in"
    # Usar proxy si es inbound o si no hay media_url directa
    usar_proxy = es_inbound or not m.media_url
    # ¿Hay alguna fuente resoluble? Si no, no intentamos cargar la imagen
    # (evita 404 innecesarios). Outbound humano viejo sin local_path queda así.
    _meta = m.metadata_ or {}
    tiene_fuente = bool(
        m.media_url or _meta.get("media_id") or _meta.get("local_path")
        or (es_inbound and m.whapi_message_id)
    )

    if tipo == "audio":
        # Custom audio player wrapper (el JS del template lo decora)
        media_html = (
            f'<div class="audio-player" data-src="{proxy_src if usar_proxy else html.escape(m.media_url)}">'
            f'<button type="button" class="ap-play" aria-label="Reproducir">'
            f'<svg class="ap-ico-play" width="16" height="16" viewBox="0 0 24 24" fill="currentColor"><polygon points="6 4 20 12 6 20 6 4"/></svg>'
            f'<svg class="ap-ico-pause" width="16" height="16" viewBox="0 0 24 24" fill="currentColor" style="display:none"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
            f'</button>'
            f'<div class="ap-wave"><div class="ap-progress"></div></div>'
            f'<span class="ap-time">0:00</span>'
            f'</div>'
        )
    elif tipo in ("imagen", "sticker", "video"):
        if not tiene_fuente:
            # Sin fuente resoluble — mostrar placeholder sin pegar al proxy.
            media_html = f'<span class="msg-empty">[{html.escape(tipo)} sin archivo guardado]</span>'
        else:
            src = proxy_src if usar_proxy else html.escape(m.media_url)
            if tipo == "sticker":
                media_html = f'<img class="msg-sticker" src="{src}" alt="sticker" loading="lazy" onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{{className:\'msg-empty\',textContent:\'[sticker]\'}}))"/>'
            elif tipo == "video":
                media_html = f'<video class="msg-media" src="{src}" controls preload="metadata"></video>'
            else:  # imagen
                media_html = (
                    f'<a href="{src}" target="_blank" rel="noopener">'
                    f'<img class="msg-media" src="{src}" alt="imagen" loading="lazy" '
                    f'onerror="this.replaceWith(Object.assign(document.createElement(\'span\'),{{className:\'msg-empty\',textContent:\'[imagen no disponible]\'}}))"/>'
                    f'</a>'
                )
    elif tipo in ("pdf", "documento"):
        meta_dict_local = m.metadata_ or {}
        fname = meta_dict_local.get("filename") or "documento"
        media_html = (
            f'<a class="msg-file" href="{proxy_src if usar_proxy else html.escape(m.media_url or proxy_src)}" target="_blank" rel="noopener" download>'
            f'<span class="msg-file-ico">📄</span>'
            f'<span class="msg-file-name">{html.escape(fname)}</span>'
            f'</a>'
        )
    elif m.media_url:
        url = html.escape(m.media_url)
        media_html = f'<a class="msg-file" href="{url}" target="_blank" rel="noopener">📎 {html.escape(tipo)}</a>'

    if not media_html and not contenido_html:
        contenido_html = f'<span class="msg-empty">[{html.escape(m.tipo or "vacío")}]</span>'

    bubble_inner = media_html + contenido_html

    # Check de envío (solo outbound). Doble check si está leído (no rastreamos lectura, así que un solo check gris).
    check_svg = ""
    if side == "out":
        check_svg = (
            '<svg class="msg-check" viewBox="0 0 18 18" width="14" height="14" aria-hidden="true">'
            '<path d="M5 9.5 L8 12.5 L13 6.5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>'
            '<path d="M2 9.5 L5 12.5 L10 6.5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round" opacity=".55"/>'
            '</svg>'
        )

    hora_txt = _fmt_hora(m.timestamp)
    autor_html = f'<span class="msg-author-inline">{html.escape(autor_txt)}</span>' if autor_txt else ""

    avatar_html = f'<div class="{avatar_cls}">{avatar_text}</div>'
    msg_extra_cls = " msg-nota" if es_nota else ""
    bubble_extra_cls = " bubble-nota" if es_nota else ""

    return f"""
        <div class="msg msg-{side}{msg_extra_cls}" data-msg-id="{m.id}">
          {avatar_html}
          <div class="msg-content">
            {autor_html}
            <div class="msg-bubble{bubble_extra_cls}">{bubble_inner}</div>
            <div class="msg-meta"><span>{hora_txt}</span>{check_svg}
              <button type="button" class="msg-del-btn" data-msg-id="{m.id}" title="Eliminar mensaje (borra de WhatsApp si aún es posible)" aria-label="Eliminar">
                <svg width="11" height="11" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
              </button>
            </div>
          </div>
        </div>
        """


# ─── Lista de chats ────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def lista_chats(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    items, total = await _construir_lista_items(session)
    html_resp = (_LISTA_TEMPLATE
                 .replace("__SHELL_STYLES__", SHELL_STYLES)
                 .replace("__EXTRA_STYLES__", _CHATS_EXTRA_STYLES)
                 .replace("__PILL_STYLES__", PILL_STYLES)
                 .replace("__ICON_SPRITE__", ICON_SPRITE)
                 .replace("__SIDEBAR__", sidebar_html(active="chats"))
                 .replace("__THEME_JS__", THEME_TOGGLE_JS)
                 .replace("{{total}}", str(total))
                 .replace("{{items}}", items))
    return HTMLResponse(html_resp)


@router.get("/list.json", response_class=HTMLResponse)
async def lista_chats_json(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Devuelve solo los chat-items (HTML) para refresco en vivo de la lista."""
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")
    items, _ = await _construir_lista_items(session)
    return HTMLResponse(
        items,
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


# ─── Servir archivos subidos desde el admin (outbound humano con media) ───


@router.get("/local/{filename}")
async def servir_local(filename: str, request: Request):
    """Sirve un blob guardado en data/admin_uploads/ (uso interno del admin)."""
    if not _check_auth(request):
        raise HTTPException(401)
    import mimetypes
    from pathlib import Path as _P
    from fastapi.responses import FileResponse
    base = (_settings.prompts_path.parent / "admin_uploads").resolve()
    candidato = (base / filename).resolve()
    if not str(candidato).startswith(str(base)):
        raise HTTPException(400, "Ruta no permitida")
    if not candidato.exists() or not candidato.is_file():
        raise HTTPException(404)
    mime, _ = mimetypes.guess_type(candidato.name)
    return FileResponse(candidato, media_type=mime or "application/octet-stream")


# ─── Proxy de media (whapi voice/audio no expone link público) ─────────────


@router.get("/_media/{conv_id}")
async def proxy_media(
    conv_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Stream del binario de media desde whapi.

    Whapi solo entrega el contenido autenticado con Bearer token. Resolvemos
    `media_id` desde `conversacion.metadata.media_id`; si no está, lo sacamos
    consultando `GET /messages/<whapi_message_id>` y lo cacheamos en metadata.
    """
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    conv = (await session.execute(
        select(Conversacion).where(Conversacion.id == conv_id)
    )).scalar_one_or_none()
    if not conv:
        raise HTTPException(404, "Conversación no encontrada")

    meta = dict(conv.metadata_ or {})

    # 1) Si hay blob local guardado (caso outbound subido desde admin) → servirlo directo.
    local_filename = meta.get("local_path")
    if local_filename:
        import mimetypes
        from fastapi.responses import FileResponse
        base_dir = (_settings.prompts_path.parent / "admin_uploads").resolve()
        candidato = (base_dir / local_filename).resolve()
        if str(candidato).startswith(str(base_dir)) and candidato.exists():
            mime, _ = mimetypes.guess_type(candidato.name)
            return FileResponse(candidato, media_type=mime or "application/octet-stream")

    media_id: str | None = meta.get("media_id")
    media_mime: str | None = meta.get("media_mime")

    ident = _identidad_principal()
    token = ident.token or _settings.whapi_token
    base = _settings.whapi_base_url.rstrip("/")

    # Resolver media_id si no está cacheado.
    if not media_id and conv.whapi_message_id:
        try:
            async with httpx.AsyncClient(timeout=15) as cli:
                r = await cli.get(
                    f"{base}/messages/{conv.whapi_message_id}",
                    headers={"Authorization": f"Bearer {token}"},
                )
            if r.status_code == 200:
                data = r.json() or {}
                for key in ("voice", "audio", "image", "video", "document", "sticker"):
                    block = data.get(key)
                    if isinstance(block, dict) and block.get("id"):
                        media_id = block["id"]
                        media_mime = block.get("mime_type") or media_mime
                        break
                if media_id:
                    meta["media_id"] = media_id
                    if media_mime:
                        meta["media_mime"] = media_mime
                    await session.execute(
                        update(Conversacion).where(Conversacion.id == conv_id).values(metadata_=meta)
                    )
                    await session.commit()
        except Exception as e:
            log.warning("proxy_media.resolve_fail", conv_id=conv_id, error=str(e))

    if not media_id:
        raise HTTPException(404, "Sin media_id")

    # Descargar binario de whapi.
    try:
        async with httpx.AsyncClient(timeout=30) as cli:
            r = await cli.get(
                f"{base}/media/{media_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
    except Exception as e:
        log.warning("proxy_media.fetch_fail", conv_id=conv_id, media_id=media_id, error=str(e))
        raise HTTPException(502, "Error descargando media")

    if r.status_code != 200:
        raise HTTPException(r.status_code, f"whapi {r.status_code}")

    content_type = r.headers.get("content-type") or media_mime or "application/octet-stream"
    return Response(
        content=r.content,
        media_type=content_type,
        headers={"Cache-Control": "private, max-age=3600"},
    )


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
        .order_by(Conversacion.timestamp, Conversacion.id)
    )).scalars().all()

    nombre = cliente.nombre or "(sin nombre)"
    cliente_initial = (nombre[:1] or cliente.numero_whatsapp[-1:] or "?").upper()

    burbujas: list[str] = []
    fecha_actual = ""
    for m in mensajes:
        fecha_msg = m.timestamp.strftime("%Y-%m-%d") if m.timestamp else ""
        if fecha_msg != fecha_actual:
            try:
                from zoneinfo import ZoneInfo
                _label = m.timestamp.astimezone(ZoneInfo("America/Bogota")).strftime("%d %b %Y")
            except Exception:
                _label = fecha_msg
            burbujas.append(f'<div class="date-sep-wrap"><span class="date-sep">{html.escape(_label)}</span></div>')
            fecha_actual = fecha_msg
        burbujas.append(_burbuja_html(m, cliente_initial=cliente_initial))

    ultimo_id = mensajes[-1].id if mensajes else 0
    total = len(mensajes)
    bloqueado_chip = '<span class="cp-tag danger">BLOQUEADO</span>' if cliente.bloqueado else ''
    bloqueado_chip_header = '<span class="cp-tag danger" style="margin-left:6px;font-size:10px;">BLOQUEADO</span>' if cliente.bloqueado else ''

    # Chip de etiqueta para el panel derecho
    etiqueta = (cliente.etiqueta or "").lower()
    etiqueta_label = {
        "cliente": "Cliente",
        "prospecto": "Prospecto",
        "equipo": "Equipo DTGP",
        "personal": "Personal",
    }.get(etiqueta, "")
    if etiqueta_label:
        etiqueta_chip = f'<span class="cp-tag {etiqueta}">{html.escape(etiqueta_label)}</span>'
    else:
        etiqueta_chip = '<span class="cp-tag" style="background:#fef3c7;color:#92400e;border-color:#fde68a;">Sin clasificar</span>'

    # ¿Bot pausado para este cliente? (intervención humana activa)
    from sqlalchemy import text as sa_text
    pausa_row = (await session.execute(sa_text(
        "SELECT pausado_hasta, razon FROM intervencion_humana "
        "WHERE cliente_id = :cid AND pausado_hasta > now()"
    ), {"cid": cliente_id})).first()

    # Íconos SVG inline
    _ico_pause = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="6" y="4" width="4" height="16"/><rect x="14" y="4" width="4" height="16"/></svg>'
    _ico_power = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12 2v10"/><path d="M18.4 6.6a9 9 0 1 1-12.8 0"/></svg>'
    _ico_play = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="5 3 19 12 5 21 5 3"/></svg>'

    header_pause_chip = ""
    pause_sub_chip = ""
    if pausa_row:
        from datetime import datetime, timezone
        ph = pausa_row[0]
        if ph.tzinfo is None:
            ph = ph.replace(tzinfo=timezone.utc)
        delta_segundos = (ph - datetime.now(timezone.utc)).total_seconds()
        es_indefinida = delta_segundos > 365 * 24 * 3600
        if es_indefinida:
            chip_text = "PAUSADA"
            sub_text = "Pausa indefinida"
            title_attr = "Pausada indefinidamente. Click en ▶ para reactivar."
        else:
            mins = max(0, int(delta_segundos / 60))
            chip_text = "PAUSADA"
            sub_text = f"Restan {mins} min"
            title_attr = f"Pausada por {mins} min. Click en ▶ para reactivar antes."
        # Chip mínimo al lado del nombre, detalle de tiempo en el subtítulo
        header_pause_chip = f'<span class="h-pause-chip" title="{title_attr}">{chip_text}</span>'
        pause_sub_chip = f' · <span class="pause-sub">⏸ {html.escape(sub_text)}</span>'

    _ico_retry = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>'
    # El botón ⟲ "Bot toma el relevo" se muestra SIEMPRE — esté pausado o no.
    # El endpoint reintentar-respuesta ya quita la pausa automáticamente al
    # ejecutarse. Si la etiqueta es equipo/cliente_wl, devuelve un BORRADOR
    # que se pega en el composer para editar antes de enviar.
    retry_btn = (
        f'<button type="button" id="btn-reintentar" class="h-btn" '
        f'title="Bot toma el relevo: lee el chat completo, quita la pausa si la hay y le responde al cliente">{_ico_retry}</button>'
    )
    if pausa_row:
        # Pausado: ⟲ + ▶ (reactivar a secas, sin disparar respuesta)
        header_pause_actions = retry_btn + f"""
          <form method="POST" action="/admin/actions/cliente/{cliente_id}/reactivar-laura" style="margin:0;">
            <button type="submit" class="h-btn success" title="Solo reactivar Dairo (sin responder al último mensaje)">{_ico_play}</button>
          </form>"""
    else:
        # No pausado: ⟲ + ⏸ + ⏻
        header_pause_actions = retry_btn + f"""
          <form method="POST" action="/admin/actions/cliente/{cliente_id}/pausar-laura" style="margin:0;">
            <button type="submit" class="h-btn" title="Pausar Dairo 1 h">{_ico_pause}</button>
          </form>
          <form method="POST" action="/admin/actions/cliente/{cliente_id}/pausar-indefinido" style="margin:0;"
                onsubmit="return confirm('¿Pausar Dairo INDEFINIDAMENTE en este chat? Solo se reactiva manualmente.');">
            <button type="submit" class="h-btn danger" title="Pausar Dairo definitivamente (hasta reactivar manualmente)">{_ico_power}</button>
          </form>"""
    # Ya no usamos el banner gigante; se mantiene la variable vacía por compatibilidad.
    pausa_banner = ""

    flash = ""
    if request.query_params.get("msg") == "sent_ok":
        flash = '<div class="flash">Mensaje enviado. Dairo queda pausada 1 hora para que tú manejes la conversación.</div>'
    elif request.query_params.get("msg") == "reactivado":
        flash = '<div class="flash">Dairo reactivada. No había mensaje pendiente — responderá al próximo del cliente.</div>'
    elif request.query_params.get("msg") == "reactivando":
        flash = '<div class="flash">Dairo reactivada. Procesando el último mensaje del cliente — la respuesta llega en segundos.</div>'
    elif request.query_params.get("msg") == "marcado_interno":
        flash = '<div class="flash">Número marcado como interno. El bot ya no le responderá. Pausa de 24h aplicada para cancelar respuestas pendientes.</div>'
    elif request.query_params.get("msg") == "pausado":
        flash = '<div class="flash">Dairo pausada 1h en este chat. No responderá hasta que la reactives o pase la hora.</div>'
    elif request.query_params.get("msg") == "pausado_indef":
        flash = '<div class="flash">Dairo pausada INDEFINIDAMENTE en este chat. Solo se reactiva cuando lo hagas manualmente.</div>'

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
        "{{bloqueado_chip}}": bloqueado_chip_header,
        "{{bloqueado_chip_panel}}": bloqueado_chip,
        "{{etiqueta_chip}}": etiqueta_chip,
        "{{etiqueta_value}}": html.escape((cliente.etiqueta or "").lower()),
        "{{avatar_initial}}": html.escape(cliente_initial),
        "{{header_pause_actions}}": header_pause_actions,
        "{{header_pause_chip}}": header_pause_chip,
        "{{pause_sub_chip}}": pause_sub_chip,
        "{{burbujas}}": "".join(burbujas) or '<p style="text-align:center;color:var(--c-text-3);margin:40px 0;">Sin mensajes todavía.</p>',
        "{{flash}}": flash,
        "{{ultimo_id}}": str(ultimo_id),
    }
    for k, v in repls.items():
        html_resp = html_resp.replace(k, v)
    return HTMLResponse(html_resp)


@router.post("/cliente/{cliente_id}/send")
async def enviar_mensaje_manual(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Envía un mensaje + archivos opcionales desde el admin.

    Acepta multipart/form-data con:
      - mensaje: str (opcional)
      - files: list[UploadFile] (opcional, imágenes y documentos)

    Si el header Accept incluye application/json (o X-Requested-With=fetch),
    responde JSON (para uso AJAX). Si no, redirect al chat (compatibilidad).
    Pausa el bot 1 h tras enviar.
    """
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    # Parsear el form multipart manualmente — FastAPI no acepta UploadFile como
    # lista opcional con type hint simple sin complicarse.
    form = await request.form()
    texto = (form.get("mensaje") or "").strip() if isinstance(form.get("mensaje"), str) else ""
    modo = (form.get("modo") or "chat").strip() if isinstance(form.get("modo"), str) else "chat"
    archivos: list = []
    for k in form.keys():
        if k != "files":
            continue
        for v in form.getlist(k):
            # UploadFile-compatibles: tienen .filename y .read()
            if hasattr(v, "filename") and hasattr(v, "read") and v.filename:
                archivos.append(v)

    if not texto and not archivos:
        es_ajax = "application/json" in (request.headers.get("accept") or "") \
                  or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")
        if es_ajax:
            return {"ok": False, "error": "vacío"}
        return RedirectResponse(f"/admin/chats/{cliente_id}", status_code=303)

    es_ajax_global = "application/json" in (request.headers.get("accept") or "") \
                     or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")

    # Modo NOTA INTERNA: no toca whapi, solo guarda en BD como anotación visible
    # en el admin. No pausa el bot ni notifica al cliente.
    if modo == "nota":
        if not texto:
            if es_ajax_global:
                return {"ok": False, "error": "nota vacía"}
            return RedirectResponse(f"/admin/chats/{cliente_id}", status_code=303)
        conv = await guardar_conversacion(
            session, cliente_id=cliente_id, direccion="humano",
            tipo="texto", contenido=texto,
            metadata={"via": "admin_chats", "nota_interna": True,
                      "autor": request.session.get("admin_user", "admin")},
        )
        await session.commit()
        log.info("admin.chats.nota_interna", cliente_id=cliente_id, chars=len(texto))
        if es_ajax_global:
            return {"ok": True, "conv_ids": [conv.id] if conv and getattr(conv, "id", None) else [], "errores": [], "nota": True}
        return RedirectResponse(f"/admin/chats/{cliente_id}?msg=nota_ok", status_code=303)

    from app.whapi.client import enviar_imagen_bytes, enviar_documento_bytes, enviar_video_bytes
    from pathlib import Path as _Path
    import secrets as _secrets
    from datetime import datetime as _dt
    nuevos_conv_ids: list[int] = []
    errores: list[str] = []

    _uploads_dir = _settings.prompts_path.parent / "admin_uploads"
    _uploads_dir.mkdir(parents=True, exist_ok=True)

    def _detect_tipo(mime: str) -> tuple[str, str]:
        """Devuelve (tipo_conv, kind): kind in {'image','video','document'}."""
        if mime.startswith("image/"):
            return ("imagen", "image")
        if mime.startswith("video/"):
            return ("video", "video")
        return ("documento", "document")

    try:
        # 1) Archivos (cada uno como mensaje aparte; caption = texto solo en el 1º si es image/video)
        for idx, archivo in enumerate(archivos):
            data = await archivo.read()
            if not data:
                continue
            mime = (archivo.content_type or "").lower()
            fname = archivo.filename or "archivo"
            tipo_conv, kind = _detect_tipo(mime)
            caption = texto if (idx == 0 and texto and kind in ("image", "video")) else None
            try:
                if kind == "image":
                    await enviar_imagen_bytes(
                        cliente.numero_whatsapp, data, mime=mime, caption=caption, filename=fname,
                    )
                elif kind == "video":
                    await enviar_video_bytes(
                        cliente.numero_whatsapp, data,
                        mime=(mime or "video/mp4"), caption=caption, filename=fname,
                    )
                else:
                    await enviar_documento_bytes(
                        cliente.numero_whatsapp, data,
                        mime=(mime or "application/octet-stream"), filename=fname, caption=caption,
                    )
            except Exception as e:
                log.error("admin.chats.enviar_media_fail", cliente_id=cliente_id, error=str(e))
                errores.append(f"{fname}: {e}")
                continue

            # Guardar blob local para poder mostrarlo en el chat del admin
            # (whapi no expone link directo a los media outbound que nosotros enviamos)
            ts = _dt.utcnow().strftime("%Y%m%d_%H%M%S")
            safe_name = "".join(c for c in fname if c.isalnum() or c in "._-")[:60] or "archivo"
            local_filename = f"{ts}_{_secrets.token_hex(4)}_{safe_name}"
            local_path = _uploads_dir / local_filename
            try:
                local_path.write_bytes(data)
            except Exception as e:
                log.warning("admin.chats.local_write_fail", error=str(e))
                local_filename = None  # no rompemos el flujo

            meta_conv = {
                "via": "admin_chats",
                "filename": fname,
                "mime": mime,
                "size": len(data),
            }
            if local_filename:
                meta_conv["local_path"] = local_filename
            conv = await guardar_conversacion(
                session, cliente_id=cliente_id, direccion="humano",
                tipo=tipo_conv, contenido=caption,
                media_url=(f"/admin/chats/local/{local_filename}" if local_filename else None),
                metadata=meta_conv,
            )
            if conv and getattr(conv, "id", None):
                nuevos_conv_ids.append(conv.id)

        # 2) Texto suelto: solo si no hubo imagen con caption (para no duplicar) o si hay solo texto.
        envio_texto_aparte = bool(texto) and not (archivos and any(
            (a.content_type or "").startswith("image/") for a in archivos
        ))
        if envio_texto_aparte:
            try:
                await enviar_texto(cliente.numero_whatsapp, texto)
            except Exception as e:
                log.error("admin.chats.enviar_fail", cliente_id=cliente_id, error=str(e))
                errores.append(f"texto: {e}")
            else:
                conv = await guardar_conversacion(
                    session, cliente_id=cliente_id, direccion="humano",
                    tipo="texto", contenido=texto,
                    metadata={"via": "admin_chats"},
                )
                if conv and getattr(conv, "id", None):
                    nuevos_conv_ids.append(conv.id)

        # Pausar bot 1h tras enviar (si efectivamente se envió algo)
        if nuevos_conv_ids:
            await pausar_bot(session, cliente_id, horas=1, razon="enviado desde admin/chats")
        await session.commit()
    except Exception as e:
        log.error("admin.chats.send_unexpected", cliente_id=cliente_id, error=str(e))
        raise HTTPException(502, f"Falló envío: {e}")

    log.info(
        "admin.chats.enviado",
        cliente_id=cliente_id, numero=cliente.numero_whatsapp,
        chars=len(texto), archivos=len(archivos), errores=len(errores),
    )

    es_ajax = "application/json" in (request.headers.get("accept") or "") \
              or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")
    if es_ajax:
        return {
            "ok": not errores,
            "conv_ids": nuevos_conv_ids,
            "errores": errores,
        }
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=sent_ok", status_code=303)


@router.post("/mensaje/{conv_id}/eliminar")
async def eliminar_mensaje(
    conv_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Elimina un mensaje del chat: borra de BD y, si tiene whapi_message_id,
    intenta borrarlo de WhatsApp también (whapi DELETE /messages/<id>)."""
    if not _check_auth(request):
        raise HTTPException(401)
    conv = (await session.execute(
        select(Conversacion).where(Conversacion.id == conv_id)
    )).scalar_one_or_none()
    if not conv:
        return {"ok": False, "error": "no existe"}

    whapi_id = conv.whapi_message_id
    borrado_wa = False
    error_wa = None
    if whapi_id:
        try:
            from app.whapi.client import eliminar_mensaje as _del_whapi
            await _del_whapi(whapi_id)
            borrado_wa = True
        except Exception as e:
            error_wa = str(e)[:150]
            log.warning("admin.chats.del_whapi_fail", conv_id=conv_id, whapi_id=whapi_id, error=error_wa)

    await session.execute(sa_text(
        "DELETE FROM conversaciones WHERE id = :id"
    ), {"id": conv_id})
    await session.commit()

    autor = request.session.get("admin_user", "admin")
    log.warning(
        "admin.chats.mensaje_eliminado",
        conv_id=conv_id, whapi_id=whapi_id, borrado_wa=borrado_wa, autor=autor,
    )
    return {"ok": True, "borrado_wa": borrado_wa, "error_wa": error_wa}


@router.get("/{cliente_id}/messages.json", response_class=HTMLResponse)
async def mensajes_nuevos(
    cliente_id: int,
    request: Request,
    after_id: int = 0,
    session: AsyncSession = Depends(get_session),
):
    """Devuelve el HTML de los mensajes con id > after_id (para append en vivo).
    Header X-Last-Id con el id más alto. Sin recargar la página."""
    if not _check_auth(request):
        raise HTTPException(401, "No autenticado")
    nuevos = (await session.execute(
        select(Conversacion)
        .where(Conversacion.cliente_id == cliente_id, Conversacion.id > after_id)
        .order_by(Conversacion.timestamp, Conversacion.id)
    )).scalars().all()
    cli = (await session.execute(
        select(Cliente.nombre, Cliente.numero_whatsapp).where(Cliente.id == cliente_id)
    )).first()
    if cli:
        _ini_base = cli[0] or cli[1] or "?"
        cliente_initial = (_ini_base[:1] or "?").upper()
    else:
        cliente_initial = "?"
    cuerpo = "".join(_burbuja_html(m, cliente_initial=cliente_initial) for m in nuevos)
    last_id = nuevos[-1].id if nuevos else after_id
    return HTMLResponse(cuerpo, headers={"X-Last-Id": str(last_id)})


# ────────────────────────────────────────────────────────────────────────────
# Templates HTML inline
# ────────────────────────────────────────────────────────────────────────────


_CHATS_EXTRA_STYLES = """
<style>
  /* ============================================================
     CHAT UI — Layout 100vh, scroll interno solo en .thread
     Inspirado en /claude-ajustes-css.md con ajustes:
     - .composer ~200px alto, textarea ~80px
     - .main sin padding cuando contiene .chat-shell
     - Íconos de pausa en el header (no banner)
     - Upload drag/paste/click + previews
     ============================================================ */

  /* Paleta local — flat & accent morado */
  :root {
    --c-purple: #6366F1;
    --c-purple-hover: #4F46E5;
    --c-purple-soft: #E0E7FF;
    --c-purple-softer: #EEF2FF;
    --c-text: #0F172A;
    --c-text-2: #475569;
    --c-text-3: #94A3B8;
    --c-border: #E5E7EB;
    --c-border-soft: #F1F5F9;
    --c-card: #FFFFFF;
    --c-thread-bg: #F8FAFC;
    --c-bubble-in: #FFFFFF;
    --c-bubble-out: var(--c-purple);
    --c-bubble-out-fg: #FFFFFF;
    --c-success: #10B981;
    --c-danger: #EF4444;
    --c-warn: #F59E0B;
    --shadow-sm: 0 1px 2px rgba(15,23,42,.04);
    --shadow-md: 0 2px 8px rgba(15,23,42,.06);
  }
  [data-theme="dark"] {
    --c-text: #e2e8f0;
    --c-text-2: #94a3b8;
    --c-text-3: #64748b;
    --c-border: #1e293b;
    --c-border-soft: #1e293b;
    --c-card: #0f172a;
    --c-thread-bg: #0b1220;
    --c-bubble-in: #1e293b;
    --c-purple-soft: #312e81;
    --c-purple-softer: #1e1b4b;
  }

  .page-title { font-size: 22px; font-weight: 700; margin: 0 0 4px; color: var(--c-text); letter-spacing: -0.01em; }
  .page-subtitle { color: var(--c-text-2); font-size: 13px; margin-bottom: 20px; }

  /* ─── Lista de chats ──────────────────────────────────────────────────── */
  .chat-list { background: var(--c-card); border: 1px solid var(--c-border); border-radius: 14px; overflow: hidden; box-shadow: 0 1px 2px rgba(15,23,42,.04); }
  .chat-item {
    display: flex; align-items: center; gap: 14px; padding: 14px 18px;
    border-bottom: 1px solid var(--c-border-soft); text-decoration: none; color: inherit;
    transition: background .12s;
  }
  .chat-item:hover { background: var(--c-purple-softer); }
  .chat-item:last-child { border-bottom: none; }
  .avatar {
    width: 42px; height: 42px; border-radius: 50%;
    background: var(--c-purple); color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-weight: 600; font-size: 16px; flex-shrink: 0;
  }
  .chat-body { flex: 1; min-width: 0; }
  .chat-top { display: flex; justify-content: space-between; align-items: baseline; gap: 8px; }
  .chat-name { font-weight: 600; font-size: 14px; color: var(--c-text); overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
  .chat-time { font-size: 11px; color: var(--c-text-3); flex-shrink: 0; }
  .chat-bottom { display: flex; justify-content: space-between; align-items: center; margin-top: 4px; gap: 8px; }
  .chat-preview { font-size: 13px; color: var(--c-text-2); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; flex: 1; }
  .chat-count {
    background: var(--c-purple); color: #fff;
    font-size: 10px; font-weight: 700; padding: 2px 8px; border-radius: 999px;
    min-width: 18px; text-align: center;
  }
  .chat-meta { font-size: 11px; color: var(--c-text-3); margin-top: 3px; }

  /* Animación cuando un chat recibe mensaje nuevo (detección por polling) */
  @keyframes chatNuevoMsg {
    0%   { background: color-mix(in srgb, var(--c-purple) 22%, transparent); }
    100% { background: transparent; }
  }
  .chat-item.chat-just-updated {
    animation: chatNuevoMsg 2.4s ease-out;
  }
  .chat-item.chat-just-updated::before {
    content: '';
    position: absolute; left: 0; top: 0; bottom: 0;
    width: 3px; background: var(--c-purple);
    animation: chatNuevoMsg 2.4s ease-out;
  }
  .chat-item { position: relative; }  /* para que el ::before se posicione bien */

  /* ─── Layout del HILO: 100dvh, scroll solo en .thread ─────────────────── */
  /* 100dvh tiene en cuenta la barra de URL dinámica del navegador móvil
     (con fallback a 100vh para navegadores viejos). */
  .app:has(.chat-shell) { height: 100vh; height: 100dvh; overflow: hidden; }
  .main:has(.chat-shell) {
    flex: 1;
    height: 100vh; height: 100dvh;
    max-height: 100vh; max-height: 100dvh;
    overflow: hidden; padding: 0;
    display: flex; flex-direction: column;
  }
  .chat-shell {
    height: 100vh; height: 100dvh;
    max-height: 100vh; max-height: 100dvh;
    overflow: hidden;
    display: grid; grid-template-columns: 1fr 320px;
    grid-template-rows: 1fr;
    transition: grid-template-columns .2s ease;
  }
  .chat-shell.panel-collapsed { grid-template-columns: 1fr 0px; }
  .chat-shell.panel-collapsed .contact-panel { display: none; }
  @media (max-width: 1100px) {
    .chat-shell { grid-template-columns: 1fr !important; }
    .contact-panel { display: none !important; }
    /* En mobile/tablet el botón "toggle panel" no tiene sentido — el panel no existe. */
    #panel-toggle { display: none !important; }
  }

  .chat-area {
    height: 100%; max-height: 100%;
    min-height: 0;
    overflow: hidden;
    display: flex; flex-direction: column;
    background: var(--c-card);
  }

  /* Header del chat */
  .chat-header {
    flex-shrink: 0;
    display: flex; align-items: center; gap: 12px;
    padding: 12px 20px;
    background: #fff;
    border-bottom: 1px solid #e8e8f0;
    box-shadow: 0 2px 8px rgba(0,0,0,.04);
    z-index: 10;
  }
  [data-theme="dark"] .chat-header { background: var(--c-card); border-bottom-color: var(--c-border); }
  .chat-header .back-link {
    padding: 6px 8px; border-radius: 8px; color: #666;
    display: flex; align-items: center;
    transition: background .15s;
  }
  .chat-header .back-link:hover { background: #f0f0fa; color: var(--c-purple); }
  .chat-header .h-avatar {
    width: 40px; height: 40px; border-radius: 50%;
    background: var(--c-purple);
    color: #fff; font-weight: 700; font-size: 14px;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .chat-header .h-info { flex: 1; min-width: 0; overflow: hidden; }
  .chat-header .h-name {
    font-size: 15px; font-weight: 700; color: var(--c-text);
    display: flex; align-items: center; gap: 6px;
    min-width: 0;
  }
  .chat-header .h-name-txt {
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
    min-width: 0;
  }
  .chat-header .h-sub {
    font-size: 12px; color: #888; margin-top: 2px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .chat-header .h-sub .pause-sub { color: var(--c-danger); font-weight: 600; }

  /* Íconos de pausa en el header */
  .h-actions { display: flex; gap: 6px; flex-shrink: 0; align-items: center; }
  .h-btn {
    width: 36px; height: 36px; border-radius: 10px;
    display: inline-flex; align-items: center; justify-content: center;
    background: transparent; color: var(--c-text-2);
    border: 1px solid var(--c-border); cursor: pointer;
    transition: all .15s; text-decoration: none;
  }
  .h-btn:hover { background: var(--c-purple-softer); color: var(--c-purple); border-color: var(--c-purple); }
  .h-btn.danger { color: var(--c-danger); border-color: color-mix(in srgb, var(--c-danger) 30%, transparent); }
  .h-btn.danger:hover { background: color-mix(in srgb, var(--c-danger) 10%, transparent); border-color: var(--c-danger); color: var(--c-danger); }
  .h-btn.success { color: #fff; background: var(--c-success); border-color: var(--c-success); }
  .h-btn.success:hover { background: #15803d; border-color: #15803d; }
  .h-pause-chip {
    font-size: 10px; font-weight: 700; padding: 2px 7px;
    border-radius: 999px; background: color-mix(in srgb, var(--c-danger) 12%, transparent);
    color: var(--c-danger);
    border: 1px solid color-mix(in srgb, var(--c-danger) 30%, transparent);
    text-transform: uppercase; letter-spacing: 0.04em;
    margin-left: 4px;
  }
  .h-collapse-btn {
    width: 36px; height: 36px; border-radius: 10px;
    background: transparent; color: var(--c-text-2);
    border: 1px solid var(--c-border); cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
  }
  .h-collapse-btn:hover { background: var(--c-purple-softer); color: var(--c-purple); border-color: var(--c-purple); }
  .h-collapse-btn .icon-collapse { transition: transform .2s; }
  .chat-shell.panel-collapsed .h-collapse-btn .icon-collapse { transform: rotate(180deg); }

  /* Nota interna (mensajes que no van por WhatsApp) — plano */
  .msg-out .msg-bubble.bubble-nota,
  .msg-nota .msg-bubble {
    background: #FEF3C7 !important;
    color: #78350F !important;
    border: 1px dashed #F59E0B !important;
    box-shadow: none !important;
  }
  .msg-nota .msg-avatar { background: #F59E0B !important; }
  .msg-nota .msg-author-inline { color: #d97706 !important; }
  .nota-badge {
    display: inline-block; font-size: 9px; font-weight: 700;
    padding: 1px 6px; border-radius: 4px;
    background: #fef3c7; color: #92400e;
    margin-left: 6px; text-transform: uppercase; letter-spacing: 0.04em;
  }

  /* Composer modo nota interna — plano */
  .composer.modo-nota {
    background: #FFFBEB !important;
    border-top-color: #F59E0B !important;
  }
  [data-theme="dark"] .composer.modo-nota { background: #422006 !important; }
  .composer.modo-nota textarea {
    border-color: #F59E0B !important;
    background: #FFFBEB;
  }
  [data-theme="dark"] .composer.modo-nota textarea { background: #422006; color: #FDE68A; }
  .composer.modo-nota .send-btn {
    background: #F59E0B !important;
  }
  .composer.modo-nota .send-btn:hover { background: #D97706 !important; }
  .composer-tab[data-mode] { cursor: pointer; }
  .composer-tab[data-mode]:hover { background: var(--c-purple-softer); color: var(--c-purple); }
  .composer-tab.active[data-mode="nota"] {
    background: #fef3c7; color: #92400e;
    border-color: #fde68a;
  }

  /* Thread — flex:1 + min-height:0 → scroll funciona dentro del flex child */
  .thread {
    flex: 1 1 auto; min-height: 0;
    overflow-y: auto; overflow-x: hidden;
    padding: 16px 20px;
    background: #f7f7fa;
    scroll-behavior: smooth;
    display: flex; flex-direction: column;
  }
  [data-theme="dark"] .thread { background: var(--c-thread-bg); }
  .thread::-webkit-scrollbar { width: 6px; }
  .thread::-webkit-scrollbar-track { background: transparent; }
  .thread::-webkit-scrollbar-thumb { background: #d0d0e0; border-radius: 3px; }
  .thread::-webkit-scrollbar-thumb:hover { background: #a0a0c0; }
  [data-theme="dark"] .thread::-webkit-scrollbar-thumb { background: #334155; }

  /* Separadores de fecha — pill con líneas decorativas */
  .date-sep-wrap {
    display: flex; align-items: center;
    margin: 18px 0 12px; gap: 0;
  }
  .date-sep-wrap::before, .date-sep-wrap::after {
    content: ''; flex: 1; height: 1px;
    background: linear-gradient(90deg, transparent, #e0e0ea, transparent);
  }
  [data-theme="dark"] .date-sep-wrap::before,
  [data-theme="dark"] .date-sep-wrap::after {
    background: linear-gradient(90deg, transparent, #1e293b, transparent);
  }
  .date-sep {
    font-size: 12px; font-weight: 600; color: #999;
    padding: 3px 12px; background: #ededf5;
    border-radius: 10px; margin: 0 8px;
    white-space: nowrap;
  }
  [data-theme="dark"] .date-sep { background: #1e293b; color: #94a3b8; }

  /* Mensajes */
  .msg { display: flex; align-items: flex-end; gap: 8px; margin-bottom: 4px; max-width: 72%; }
  .msg-in + .msg-out, .msg-out + .msg-in { margin-top: 12px; }
  .msg-in { justify-content: flex-start; align-self: flex-start; }
  .msg-out { justify-content: flex-end; align-self: flex-end; flex-direction: row-reverse; }

  .msg-content { display: flex; flex-direction: column; max-width: 420px; min-width: 0; }
  .msg-in  .msg-content { align-items: flex-start; }
  .msg-out .msg-content { align-items: flex-end; }

  /* Avatares planos — colores sólidos, sin sombras ni gradientes */
  .msg-avatar {
    width: 30px; height: 30px; border-radius: 50%;
    color: #fff; font-size: 12px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
    flex-shrink: 0;
  }
  .msg-avatar-in    { background: var(--c-purple); }
  .msg-avatar-bot   { background: #F97316; font-size: 11px; }
  .msg-avatar-human { background: #16A34A; font-size: 11px; }
  .msg-avatar-admin { background: #2563EB; font-size: 11px; }

  /* Burbujas — esquinas asimétricas estilo WhatsApp */
  .msg-bubble {
    padding: 8px 14px; max-width: 420px;
    font-size: 14px; line-height: 1.5;
    word-break: break-word; white-space: pre-wrap;
  }
  .msg-in .msg-bubble {
    background: #fff; border: 1px solid #E5E7EB;
    border-radius: 18px 18px 18px 4px;
    box-shadow: 0 1px 2px rgba(15,23,42,.04);
    color: #0F172A;
  }
  [data-theme="dark"] .msg-in .msg-bubble {
    background: var(--c-bubble-in); color: var(--c-text);
    border-color: var(--c-border);
  }
  /* Burbujas outbound planas — color sólido según quién envía */
  .msg-out .msg-bubble {
    border-radius: 18px 18px 4px 18px;
    background: var(--c-purple);
    color: #fff;
  }
  .msg-out:has(.msg-avatar-bot) .msg-bubble    { background: #F97316; }
  .msg-out:has(.msg-avatar-human) .msg-bubble  { background: #16A34A; }
  .msg-out:has(.msg-avatar-admin) .msg-bubble  { background: #2563EB; }

  .msg-author-inline {
    font-size: 11px; font-weight: 600;
    color: #f97316;
    margin-bottom: 2px; padding: 0 4px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .msg-out:has(.msg-avatar-human) .msg-author-inline { color: #16a34a; }
  .msg-out:has(.msg-avatar-admin) .msg-author-inline { color: #2563eb; }

  .msg-meta {
    font-size: 11px; opacity: .55; margin-top: 2px; padding: 0 4px;
    display: flex; align-items: center; gap: 4px;
  }
  .msg-out .msg-meta { color: var(--c-text-2); }
  .msg-check { color: #5b4fd4; opacity: .85; flex-shrink: 0; }
  .msg-del-btn {
    background: transparent; border: none; cursor: pointer;
    color: var(--c-danger); padding: 2px 4px; border-radius: 4px;
    opacity: .35; transition: opacity .15s, background .12s;
    display: inline-flex; align-items: center; margin-left: 4px;
  }
  .msg:hover .msg-del-btn { opacity: .7; }
  .msg-del-btn:hover { opacity: 1 !important; background: color-mix(in srgb, var(--c-danger) 18%, transparent); }

  .msg-empty {
    background: #f0f0fa; color: #9090b0;
    font-size: 12px; font-style: italic;
    padding: 4px 10px; border-radius: 12px;
    border: 1px dashed #d8d8ee;
  }
  .msg-media {
    max-width: 280px; max-height: 360px; display: block;
    border-radius: 10px; cursor: pointer; background: #fff;
  }
  .msg-sticker { width: 120px; height: 120px; display: block; }
  .msg-file {
    display: inline-flex; align-items: center; gap: 8px;
    color: inherit; text-decoration: none;
    padding: 8px 12px; background: rgba(0,0,0,.06); border-radius: 10px;
    border: 1px solid rgba(0,0,0,.08);
  }
  .msg-out .msg-file { background: rgba(255,255,255,.18); border-color: rgba(255,255,255,.25); }
  .msg-file-ico { font-size: 18px; }
  .msg-file-name { font-size: 13px; font-weight: 500; }

  /* Audio player custom — usa currentColor para adaptarse al color del bubble */
  .audio-player {
    display: flex; align-items: center; gap: 10px;
    min-width: 240px; max-width: 320px;
    padding: 4px 6px;
  }
  .audio-player .ap-play {
    width: 34px; height: 34px; border-radius: 50%;
    border: none; cursor: pointer; flex-shrink: 0;
    display: inline-flex; align-items: center; justify-content: center;
    transition: filter .12s;
  }
  /* Inbound (burbuja blanca) → botón morado lleno */
  .msg-in .audio-player .ap-play {
    background: var(--c-purple); color: #fff;
  }
  /* Outbound (burbuja con color, sea morado/naranja/verde) → botón blanco translúcido */
  .msg-out .audio-player .ap-play {
    background: rgba(255,255,255,.28); color: #fff;
    box-shadow: inset 0 0 0 1px rgba(255,255,255,.35);
  }
  .audio-player .ap-play:hover { filter: brightness(1.12); }
  .audio-player .ap-wave {
    flex: 1; height: 28px; position: relative;
    border-radius: 4px; overflow: hidden;
    cursor: pointer;
  }
  /* Track (línea base) y barra de progreso usan currentColor con opacity baja/alta */
  .audio-player .ap-wave::before {
    content: ''; position: absolute; inset: 0;
    background-image: linear-gradient(90deg, currentColor 1px, transparent 1px);
    background-size: 4px 100%; background-position: 0 50%;
    background-repeat: repeat-x;
    opacity: .35;
  }
  .msg-out .audio-player .ap-wave::before { opacity: .5; }
  .audio-player .ap-progress {
    position: absolute; left: 0; top: 0; bottom: 0; width: 0;
    background: currentColor; opacity: .9;
    transition: width .1s linear;
  }
  .audio-player .ap-time {
    font-size: 11px; font-variant-numeric: tabular-nums;
    opacity: .85; flex-shrink: 0;
  }

  /* ─── Pause banner cuando está activo (info breve, controles en header) ─ */
  .pause-banner {
    flex-shrink: 0;
    display: flex; align-items: center; justify-content: space-between;
    gap: 10px; margin: 8px 16px;
    padding: 8px 16px; border-radius: 10px; font-size: 13px;
    border: 1px solid #fecaca; background: #fff8f8; color: var(--c-danger);
  }
  [data-theme="dark"] .pause-banner { background: rgba(239,68,68,.08); border-color: rgba(239,68,68,.3); }
  .pause-banner strong { color: inherit; }

  /* ─── Composer — más alto, con upload zone y previews ─────────────────── */
  .composer {
    flex-shrink: 0;
    padding: 16px 20px 20px;
    padding-bottom: calc(20px + env(safe-area-inset-bottom));  /* iPhone notch */
    background: #fff;
    border-top: 1px solid #e8e8f0;
    box-shadow: 0 -2px 8px rgba(0,0,0,.04);
  }
  [data-theme="dark"] .composer { background: var(--c-card); border-top-color: var(--c-border); }
  .composer-tabs {
    display: flex; gap: 4px; margin-bottom: 10px; align-items: center;
  }
  .composer-tab {
    padding: 5px 12px; border-radius: 8px;
    font-size: 12px; font-weight: 500;
    background: transparent; color: var(--c-text-2);
    border: 1px solid transparent; cursor: default;
  }
  .composer-tab.active {
    background: var(--c-purple-softer); color: var(--c-purple);
    border-color: color-mix(in srgb, var(--c-purple) 20%, transparent);
  }
  .composer-attach {
    margin-left: auto;
    width: 32px; height: 32px; border-radius: 8px;
    background: transparent; color: var(--c-text-2);
    border: 1px solid var(--c-border); cursor: pointer;
    display: inline-flex; align-items: center; justify-content: center;
  }
  .composer-attach:hover { background: var(--c-purple-softer); color: var(--c-purple); border-color: var(--c-purple); }

  /* Previews de archivos a enviar */
  .composer-attachments {
    display: flex; flex-wrap: wrap; gap: 8px;
    margin-bottom: 8px;
  }
  .composer-attachments:empty { display: none; }
  .attach-preview {
    position: relative;
    width: 80px; height: 80px; border-radius: 10px;
    overflow: hidden;
    border: 1px solid var(--c-border);
    background: var(--c-purple-softer);
    display: flex; align-items: center; justify-content: center;
    color: var(--c-purple); font-size: 11px;
  }
  .attach-preview img { width: 100%; height: 100%; object-fit: cover; }
  .attach-preview .attach-remove {
    position: absolute; top: 3px; right: 3px;
    width: 20px; height: 20px; border-radius: 50%;
    background: rgba(0,0,0,.7); color: #fff; border: none;
    font-size: 12px; line-height: 1; cursor: pointer;
    display: flex; align-items: center; justify-content: center;
  }
  .attach-preview .attach-name {
    position: absolute; bottom: 0; left: 0; right: 0;
    background: rgba(0,0,0,.6); color: #fff;
    font-size: 10px; padding: 2px 4px;
    white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }

  .composer-row { display: flex; gap: 10px; align-items: flex-end; }
  .composer textarea {
    flex: 1; min-height: 80px; max-height: 200px; resize: vertical;
    border: 1px solid var(--c-border); border-radius: 12px;
    padding: 12px 14px; font: inherit; font-size: 14px;
    background: var(--c-card); color: var(--c-text);
    box-sizing: border-box; outline: none;
    transition: border-color .15s, box-shadow .15s;
  }
  .composer textarea:focus {
    border-color: var(--c-purple);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--c-purple) 15%, transparent);
  }
  .composer.drag-over textarea {
    border-color: var(--c-purple);
    background: var(--c-purple-softer);
  }
  .send-btn {
    background: var(--c-purple); color: #fff;
    border: none; padding: 0 20px; height: 44px; border-radius: 10px;
    font-size: 14px; font-weight: 600; cursor: pointer;
    display: inline-flex; align-items: center; gap: 6px;
    transition: background .15s, transform .05s;
  }
  .send-btn:hover { background: var(--c-purple-hover); }
  .send-btn:active { transform: scale(.97); }
  .send-btn:disabled { opacity: .6; cursor: not-allowed; }
  .composer-hint { font-size: 11px; color: var(--c-text-3); margin-top: 8px; }
  .composer-hint kbd {
    background: var(--c-border-soft); padding: 1px 5px; border-radius: 4px;
    font-family: ui-monospace, monospace; font-size: 10px;
    border: 1px solid var(--c-border);
  }

  /* ─── Mobile compact: aprovechar la pantalla ──────────────────────────── */
  @media (max-width: 768px) {
    .chat-header { padding: 8px 12px; gap: 8px; }
    .chat-header .h-avatar { width: 34px; height: 34px; font-size: 12px; }
    .chat-header .h-name { font-size: 14px; }
    .chat-header .h-sub { font-size: 11px; }
    .h-actions { gap: 4px; }
    .h-btn { width: 32px; height: 32px; }
    .thread { padding: 10px 10px; }
    .msg { max-width: 88%; }
    .msg-content, .msg-bubble { max-width: 100%; }
    .composer { padding: 10px 12px 12px;
                padding-bottom: calc(12px + env(safe-area-inset-bottom)); }
    .composer-tabs { margin-bottom: 6px; }
    .composer textarea { min-height: 56px; padding: 10px 12px; font-size: 15px; }
    .composer .send-btn { padding: 0 14px; height: 40px; font-size: 13px; }
    .composer-hint { display: none; }  /* atajos de teclado no aplican en mobile */
  }

  /* ─── Panel derecho de contacto (gradient header) ─────────────────────── */
  .contact-panel {
    height: 100%; overflow-y: auto;
    border-left: 1px solid #e8e8f0;
    background: #fafafa;
    padding: 0;
    display: flex; flex-direction: column;
  }
  [data-theme="dark"] .contact-panel { background: var(--c-thread-bg); border-left-color: var(--c-border); }
  .contact-panel::-webkit-scrollbar { width: 4px; }
  .contact-panel::-webkit-scrollbar-thumb { background: #e0e0ea; border-radius: 2px; }

  /* Header del panel — card plana, sin gradiente ni círculos decorativos */
  .cp-head {
    background: var(--c-card);
    padding: 28px 20px 22px;
    text-align: center;
    border-bottom: 1px solid var(--c-border-soft);
    display: flex; flex-direction: column; gap: 8px;
    align-items: center;
  }
  .cp-avatar {
    width: 64px; height: 64px; border-radius: 50%;
    background: var(--c-purple); color: #fff;
    font-size: 22px; font-weight: 700;
    display: flex; align-items: center; justify-content: center;
  }
  .cp-name { font-size: 16px; font-weight: 700; color: var(--c-text); }
  .cp-num { font-size: 13px; color: var(--c-text-2); }
  .cp-tags {
    display: flex; justify-content: center; gap: 6px; flex-wrap: wrap;
  }
  .cp-tag {
    background: #EEF2FF; color: var(--c-purple);
    font-size: 10px; font-weight: 700; padding: 3px 10px;
    border-radius: 999px;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  [data-theme="dark"] .cp-tag { background: var(--c-purple-softer); }
  .cp-tag.danger { background: #FEE2E2; color: #B91C1C; }
  [data-theme="dark"] .cp-tag.danger { background: #7F1D1D; color: #FCA5A5; }

  /* Cards del panel (Etiqueta, Detalles, etc.) */
  .cp-card {
    padding: 14px 18px; background: #fff;
    margin: 12px; border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0,0,0,.06);
    border: 1px solid #f0f0f8;
  }
  [data-theme="dark"] .cp-card { background: var(--c-card); border-color: var(--c-border); }

  /* Nombre editable inline */
  .cp-name { cursor: text; padding: 2px 8px; border-radius: 6px; }
  .cp-name:hover { background: rgba(255,255,255,.15); }
  .cp-name.editing {
    background: rgba(255,255,255,.95);
    color: #1a1a2e;
    outline: 2px solid rgba(255,255,255,.6);
  }
  .cp-name[contenteditable="true"]:focus { outline: 2px solid rgba(255,255,255,.85); }

  /* Picker de etiquetas */
  .etiqueta-picker { display: flex; flex-wrap: wrap; gap: 6px; }
  .et-chip {
    font-size: 11px; font-weight: 600;
    padding: 5px 11px; border-radius: 999px;
    background: var(--c-card); color: var(--c-text-2);
    border: 1px solid var(--c-border);
    cursor: pointer; transition: all .12s;
  }
  .et-chip:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .et-chip.active[data-value="cliente"]   { background:#D1FAE5; color:#065F46; border-color:#A7F3D0; }
  .et-chip.active[data-value="prospecto"] { background:#FEF3C7; color:#92400E; border-color:#FDE68A; }
  .et-chip.active[data-value="equipo"]    { background:#DBEAFE; color:#1E40AF; border-color:#BFDBFE; }
  .et-chip.active[data-value="personal"]  { background:#F3F4F6; color:#374151; border-color:#D1D5DB; }
  .et-chip.active[data-value=""]          { background:#FEF2F2; color:#991B1B; border-color:#FECACA; }
  .et-chip.saving { opacity: .6; cursor: wait; }

  /* Lightbox modal de imágenes */
  .lightbox {
    position: fixed; inset: 0; z-index: 10000;
    display: none; align-items: center; justify-content: center;
    background: rgba(0,0,0,.85);
    backdrop-filter: blur(4px);
  }
  .lightbox.open { display: flex; }
  .lightbox img {
    max-width: 92vw; max-height: 88vh;
    border-radius: 8px;
    box-shadow: 0 20px 60px rgba(0,0,0,.5);
    user-select: none;
    opacity: 0; transition: opacity .15s;
  }
  .lightbox img.loaded { opacity: 1; }
  /* Spinner mientras carga */
  .lightbox-spinner {
    width: 48px; height: 48px;
    border: 3px solid rgba(255,255,255,.2);
    border-top-color: #fff;
    border-radius: 50%;
    animation: lb-spin .8s linear infinite;
    display: none;
    position: absolute;
  }
  .lightbox.loading .lightbox-spinner { display: block; }
  @keyframes lb-spin { to { transform: rotate(360deg); } }
  .lightbox-btn {
    position: absolute; background: rgba(255,255,255,.15); color: #fff;
    border: 1px solid rgba(255,255,255,.25); border-radius: 50%;
    width: 44px; height: 44px;
    display: inline-flex; align-items: center; justify-content: center;
    cursor: pointer; transition: background .12s;
  }
  .lightbox-btn:hover { background: rgba(255,255,255,.28); }
  .lightbox-close { top: 18px; right: 18px; }
  .lightbox-prev { left: 18px; top: 50%; transform: translateY(-50%); }
  .lightbox-next { right: 18px; top: 50%; transform: translateY(-50%); }
  .lightbox-info {
    position: absolute; bottom: 18px; left: 50%; transform: translateX(-50%);
    color: rgba(255,255,255,.75); font-size: 12px;
    background: rgba(0,0,0,.4); padding: 6px 12px; border-radius: 8px;
  }
  .msg-media { cursor: zoom-in; }

  /* Menú contextual de la lista de chats */
  .chat-ctx-menu {
    position: fixed; z-index: 9999;
    background: var(--c-card); color: var(--c-text);
    border: 1px solid var(--c-border); border-radius: 10px;
    box-shadow: 0 4px 12px rgba(15,23,42,.08);
    padding: 6px; min-width: 200px;
    display: none;
  }
  .chat-ctx-menu.open { display: block; }
  .chat-ctx-menu .ctx-title {
    font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em;
    color: var(--c-text-3); padding: 6px 10px 2px;
  }
  .chat-ctx-menu .ctx-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px; border-radius: 6px;
    font-size: 13px; cursor: pointer;
    color: var(--c-text); user-select: none;
  }
  .chat-ctx-menu .ctx-item:hover { background: var(--c-purple-softer); }
  .chat-ctx-menu .ctx-item.current { background: var(--c-purple); color: #fff; }
  .chat-ctx-menu .ctx-dot {
    width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0;
  }
  .chat-ctx-menu .ctx-divider {
    height: 1px; background: var(--c-border-soft); margin: 4px 0;
  }
  .chat-ctx-menu .ctx-title { display: flex; align-items: center; justify-content: space-between; }
  .chat-ctx-menu .ctx-link {
    font-size: 10px; color: var(--c-purple); text-decoration: none;
    text-transform: none; letter-spacing: 0; font-weight: 500;
  }
  .chat-ctx-menu .ctx-link:hover { text-decoration: underline; }
  .chat-ctx-menu .ctx-loading {
    padding: 6px 12px; font-size: 12px; color: var(--c-text-3);
  }
  .chat-ctx-menu .ctx-empty {
    padding: 6px 12px; font-size: 11px; color: var(--c-text-3); font-style: italic;
  }
  .chat-ctx-menu #ctx-tags-list {
    max-height: 200px; overflow-y: auto; padding: 2px 0;
  }
  .chat-ctx-menu .ctx-tag-item {
    display: flex; align-items: center; gap: 10px;
    padding: 6px 12px; border-radius: 6px;
    font-size: 12px; cursor: pointer; user-select: none;
    color: var(--c-text);
  }
  .chat-ctx-menu .ctx-tag-item:hover { background: var(--c-purple-softer); }
  .chat-ctx-menu .ctx-tag-check {
    width: 16px; height: 16px; border-radius: 4px;
    border: 1.5px solid var(--c-border);
    display: inline-flex; align-items: center; justify-content: center;
    flex-shrink: 0; font-size: 11px; color: #fff;
  }
  .chat-ctx-menu .ctx-tag-item.on .ctx-tag-check::after { content: '✓'; }
  .chat-ctx-menu .ctx-tag-swatch {
    width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
  }
  .cp-section-title {
    font-size: 11px; font-weight: 700;
    text-transform: uppercase; letter-spacing: 1px;
    color: #999; margin-bottom: 12px;
    display: flex; align-items: center; justify-content: space-between;
  }
  .cp-section-link {
    font-size: 10px; font-weight: 600; color: var(--c-purple);
    text-decoration: none; text-transform: none; letter-spacing: 0;
  }
  .cp-section-link:hover { text-decoration: underline; }

  /* Picker de tags en el panel del chat */
  .tags-picker { display: flex; flex-wrap: wrap; gap: 6px; }
  .tags-loading { font-size: 12px; color: var(--c-text-3); padding: 4px 0; }
  .tag-pick {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 4px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
    border: 1px solid var(--c-border);
    background: transparent; color: var(--c-text-2);
    cursor: pointer; transition: all .12s;
    opacity: 0.55;
  }
  .tag-pick:hover { opacity: 1; transform: translateY(-1px); }
  .tag-pick.on {
    opacity: 1;
    color: #fff !important;
    border-color: transparent;
    box-shadow: 0 1px 3px rgba(0,0,0,.12);
  }
  .tag-pick.on::after { content: '✓'; font-size: 10px; margin-left: 2px; }
  .tags-empty { font-size: 11px; color: var(--c-text-3); padding: 4px 0; font-style: italic; }

  /* Chips de tags en la lista de chats */
  .chat-tags { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .chat-tag-chip {
    font-size: 9px; font-weight: 700; padding: 1px 6px;
    border-radius: 4px; color: #fff;
    text-transform: uppercase; letter-spacing: 0.3px;
    line-height: 1.4;
  }
  .cp-fields { display: flex; flex-direction: column; gap: 10px; }
  .cp-field { display: flex; flex-direction: column; gap: 2px; }
  .cp-field label {
    font-size: 11px; font-weight: 600; color: #9090a0;
    text-transform: uppercase; letter-spacing: 0.5px;
  }
  .cp-field .val { font-size: 13px; color: var(--c-text); font-weight: 500; }

  .cp-actions {
    padding: 0 12px 16px;
    display: flex; flex-direction: column; gap: 8px;
  }
  .cp-btn {
    display: block; width: 100%; padding: 10px 16px;
    border-radius: 10px; font-size: 13px; font-weight: 600;
    text-align: center; cursor: pointer;
    transition: all .15s ease;
    text-decoration: none;
    background: #f5f5fc; color: #5b4fd4;
    border: 1px solid #e8e8f8;
  }
  .cp-btn:hover { background: #ebe8fc; border-color: #c0b8f8; }
  .cp-btn.danger { background: #fff5f5; color: #e02020; border-color: #ffe0e0; }
  .cp-btn.danger:hover { background: #ffe0e0; }
  [data-theme="dark"] .cp-btn { background: var(--c-card); color: var(--c-purple); border-color: var(--c-border); }
  [data-theme="dark"] .cp-btn:hover { background: var(--c-purple-softer); }

  /* Toast (notificaciones AJAX) */
  .toast-stack {
    position: fixed; bottom: 16px; right: 16px;
    display: flex; flex-direction: column; gap: 8px;
    z-index: 9999;
  }
  .toast {
    padding: 10px 16px; border-radius: 10px;
    font-size: 13px; color: #fff;
    background: var(--c-success);
    box-shadow: 0 4px 12px rgba(15,23,42,.1);
    animation: toast-in .25s ease;
    transition: opacity .3s, transform .3s;
    min-width: 180px;
  }
  .toast.error { background: var(--c-danger); }
  @keyframes toast-in { from { transform: translateY(20px); opacity: 0; } to { transform: translateY(0); opacity: 1; } }

  .flash {
    flex-shrink: 0;
    margin: 8px 16px; padding: 10px 14px;
    border-radius: 10px; font-size: 13px;
    background: color-mix(in srgb, var(--c-success) 10%, transparent);
    color: var(--c-success);
    border: 1px solid color-mix(in srgb, var(--c-success) 30%, transparent);
  }
</style>
"""

_LISTA_TEMPLATE = """<!doctype html>
<html lang="es" data-theme="light"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Chats — Dairo</title>
__SHELL_STYLES__
__EXTRA_STYLES__
__PILL_STYLES__
<style>
  .quick-filters { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 10px; }
  .tag-filters { display: flex; gap: 6px; flex-wrap: wrap; margin-bottom: 14px; }
  .tag-filter-chip {
    padding: 3px 10px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
    border: 1px solid var(--c-border); color: var(--c-text-2);
    background: transparent; cursor: pointer; opacity: 0.6;
    transition: all .12s;
  }
  .tag-filter-chip:hover { opacity: 1; }
  .tag-filter-chip.active {
    opacity: 1; color: #fff !important; border-color: transparent;
  }
  .quick-chip {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 12px; border-radius: 999px;
    background: var(--c-card); border: 1px solid var(--c-border);
    color: var(--c-text-2); font-size: 12px; font-weight: 500;
    cursor: pointer; transition: all .12s;
  }
  .quick-chip:hover { border-color: var(--c-purple); color: var(--c-purple); }
  .quick-chip.active { background: var(--c-purple); color: #fff; border-color: var(--c-purple); }
  .chat-name.nombre-vacio { font-style: italic; color: var(--c-text-3); }
  .chat-item .prev-ico { margin-right: 4px; }
  .chat-item .chat-meta { display:flex; align-items:center; gap:8px; }
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
      <input type="text" id="chat-filter" placeholder="Buscar por nombre o número (ej: Carlos, 31550, +573...)" autofocus autocomplete="off"/>
      <span class="filter-count" id="filter-count"></span>
    </div>
    <div class="quick-filters">
      <button type="button" class="quick-chip active" data-quick="todos">Todos</button>
      <button type="button" class="quick-chip" data-quick="cliente">Clientes</button>
      <button type="button" class="quick-chip" data-quick="prospecto">Prospectos</button>
      <button type="button" class="quick-chip" data-quick="equipo">Equipo</button>
      <button type="button" class="quick-chip" data-quick="personal">Personal</button>
      <button type="button" class="quick-chip" data-quick="grupo">Grupos</button>
      <button type="button" class="quick-chip" data-quick="sin">Sin clasificar</button>
    </div>
    <div class="tag-filters" id="tag-filters">
      <!-- chips de tags se inyectan via JS -->
    </div>
    <div class="chat-list" id="chat-list">
      {{items}}
    </div>
    <div class="empty-state" id="empty-state" style="display:none;">No hay coincidencias.</div>
  </main>
</div>

<!-- Menú contextual (click derecho en un chat) -->
<div class="chat-ctx-menu" id="chat-ctx" role="menu" aria-hidden="true">
  <div class="ctx-title" id="ctx-target">—</div>
  <div class="ctx-divider"></div>
  <div class="ctx-title">Etiquetar como</div>
  <div class="ctx-item" data-et="cliente"><span class="ctx-dot" style="background:#10B981"></span>Cliente</div>
  <div class="ctx-item" data-et="prospecto"><span class="ctx-dot" style="background:#F59E0B"></span>Prospecto</div>
  <div class="ctx-item" data-et="equipo"><span class="ctx-dot" style="background:#3B82F6"></span>Equipo</div>
  <div class="ctx-item" data-et="personal"><span class="ctx-dot" style="background:#6B7280"></span>Personal</div>
  <div class="ctx-item" data-et=""><span class="ctx-dot" style="background:#EF4444"></span>Sin clasificar</div>
  <div class="ctx-divider"></div>
  <div class="ctx-title">Tags <a href="/admin/etiquetas" class="ctx-link">gestionar</a></div>
  <div id="ctx-tags-list">
    <div class="ctx-loading">Cargando…</div>
  </div>
  <div class="ctx-divider"></div>
  <div class="ctx-item" data-action="abrir">Abrir chat →</div>
</div>

<div class="toast-stack" id="toast-stack-list"></div>
__THEME_JS__
<script>
  // Buscador client-side + refresco en vivo de la lista (sin recargar la página).
  (function(){
    var input = document.getElementById('chat-filter');
    var listEl = document.getElementById('chat-list');
    var count = document.getElementById('filter-count');
    var empty = document.getElementById('empty-state');
    var chips = document.querySelectorAll('.quick-chip');
    var quickFiltro = localStorage.getItem('chats_quick') || 'todos';
    chips.forEach(function(c){ c.classList.toggle('active', c.dataset.quick === quickFiltro); });

    // Filtro de tags (multi-select). Se guarda como CSV en localStorage.
    var tagFiltersEl = document.getElementById('tag-filters');
    var tagFiltrados = new Set((localStorage.getItem('chats_tag_filter') || '')
                                  .split(',').filter(Boolean).map(Number));

    function filtrar() {
      var items = Array.prototype.slice.call(listEl.querySelectorAll('.chat-item'));
      var q = (input.value || '').trim().toLowerCase();
      var visibles = 0;
      var hayTagFiltro = tagFiltrados.size > 0;
      items.forEach(function(it){
        var hay = it.getAttribute('data-search') || '';
        var matchTexto = !q || hay.indexOf(q) !== -1;
        var estado = it.getAttribute('data-estado') || 'ninguna';
        var matchQuick = (
          quickFiltro === 'todos' ||
          (quickFiltro === 'sin' && (estado === 'ninguna' || estado === '')) ||
          (estado === quickFiltro)
        );
        var matchTag = true;
        if (hayTagFiltro) {
          var raw = it.getAttribute('data-tags') || '';
          var ids = raw ? raw.split(',').map(Number) : [];
          // intersección: el chat debe tener AL MENOS UNO de los tags filtrados
          matchTag = ids.some(function(id){ return tagFiltrados.has(id); });
        }
        var match = matchTexto && matchQuick && matchTag;
        it.classList.toggle('hidden', !match);
        if (match) visibles++;
      });
      count.textContent = q ? (visibles + ' de ' + items.length) : '';
      empty.style.display = (visibles === 0 && (q || quickFiltro !== 'todos' || hayTagFiltro)) ? 'block' : 'none';
    }
    input.addEventListener('input', filtrar);
    chips.forEach(function(c){
      c.addEventListener('click', function(){
        quickFiltro = c.dataset.quick;
        localStorage.setItem('chats_quick', quickFiltro);
        chips.forEach(function(x){ x.classList.toggle('active', x === c); });
        filtrar();
      });
    });

    // Cargar chips de tags para filtrar
    (async function cargarTagFilters(){
      try {
        var r = await fetch('/admin/etiquetas/list.json', {headers:{'Accept':'application/json'}});
        if (!r.ok) return;
        var data = await r.json();
        var tags = data.tags || [];
        if (tags.length === 0) { tagFiltersEl.style.display = 'none'; return; }
        tagFiltersEl.innerHTML = tags.map(function(t){
          var on = tagFiltrados.has(t.id);
          var bg = on ? t.color : 'transparent';
          var bc = on ? t.color : '';
          return '<button type="button" class="tag-filter-chip' + (on ? ' active' : '') + '"' +
                 ' data-tag="' + t.id + '"' +
                 ' style="background:' + bg + (bc ? ';border-color:' + bc : '') + ';">' +
                 (t.nombre.replace(/</g,'&lt;')) + '</button>';
        }).join('');
        tagFiltersEl.addEventListener('click', function(e){
          var btn = e.target.closest('.tag-filter-chip');
          if (!btn) return;
          var id = Number(btn.dataset.tag);
          if (tagFiltrados.has(id)) tagFiltrados.delete(id);
          else tagFiltrados.add(id);
          localStorage.setItem('chats_tag_filter', Array.from(tagFiltrados).join(','));
          // Actualizar visual del chip
          var t = tags.find(function(x){ return x.id === id; });
          var nowOn = tagFiltrados.has(id);
          btn.classList.toggle('active', nowOn);
          btn.style.background = nowOn ? t.color : 'transparent';
          btn.style.borderColor = nowOn ? t.color : '';
          filtrar();
        });
      } catch(e) {}
    })();

    filtrar();

    // ── Polling en tiempo real con diff inteligente ──────────────────────
    // - 2.5s si la pestaña está visible · 15s si está oculta
    // - Diff por hash: si el HTML servido no cambió, no toca el DOM (sin flicker)
    // - Detecta chats con mensaje nuevo (preview o orden cambió) y los anima
    // - Badge en <title> con # de chats con cambios mientras la pestaña no se mira
    var tituloOriginal = document.title;
    var pendientesEnTitulo = 0;
    var ultimoHash = null;
    function snapshotChats(){
      // Mapa cliente_id -> {preview, posicion}
      var m = {};
      Array.prototype.slice.call(listEl.querySelectorAll('.chat-item')).forEach(function(it, idx){
        var cid = it.getAttribute('data-cliente') || '';
        var prev = (it.querySelector('.chat-preview') || {}).textContent || '';
        m[cid] = {preview: prev.trim(), pos: idx};
      });
      return m;
    }
    function hashRapido(str){
      // djb2 — barato, suficiente para detectar cambios
      var h = 5381;
      for (var i = 0; i < str.length; i++) h = ((h << 5) + h) + str.charCodeAt(i);
      return h;
    }
    async function refrescar(){
      if (document.activeElement === input) return; // no interrumpir la búsqueda
      try {
        var r = await fetch('/admin/chats/list.json?_=' + Date.now(),
                            {headers:{'Accept':'text/html'}, cache:'no-store'});
        if (!r.ok) { console.warn('[chats-poll] HTTP', r.status); return; }
        var nuevoHTML = await r.text();
        var h = hashRapido(nuevoHTML);
        if (h === ultimoHash) return;   // nada cambió → no tocar DOM (sin flicker)

        // Capturar estado anterior ANTES de reemplazar el DOM
        var antes = snapshotChats();
        var scrollTop = listEl.scrollTop;
        listEl.innerHTML = nuevoHTML;
        listEl.scrollTop = scrollTop;
        ultimoHash = h;

        // Detectar chats con cambios y animarlos
        var cambios = 0;
        Array.prototype.slice.call(listEl.querySelectorAll('.chat-item')).forEach(function(it, idx){
          var cid = it.getAttribute('data-cliente') || '';
          var prevAhora = ((it.querySelector('.chat-preview') || {}).textContent || '').trim();
          var antesItem = antes[cid];
          var esNuevoChat = !antesItem;
          var cambioMsg = antesItem && antesItem.preview !== prevAhora;
          var subio = antesItem && antesItem.pos > idx && idx === 0;
          if (esNuevoChat || cambioMsg || subio) {
            cambios++;
            it.classList.add('chat-just-updated');
            // Quitar la animación después de 2.4s
            setTimeout(function(){ it.classList.remove('chat-just-updated'); }, 2400);
          }
        });

        // Re-aplicar filtro
        filtrar();

        // Badge en pestaña si no estamos mirando
        if (cambios > 0 && document.visibilityState !== 'visible') {
          pendientesEnTitulo += cambios;
          document.title = '(' + pendientesEnTitulo + ') ' + tituloOriginal;
        }
        console.debug('[chats-poll] actualizado, cambios=' + cambios);
      } catch(e) { console.warn('[chats-poll] error:', e); }
    }
    // Limpiar badge cuando vuelva a estar visible + refrescar al volver
    document.addEventListener('visibilitychange', function(){
      if (document.visibilityState === 'visible') {
        pendientesEnTitulo = 0;
        document.title = tituloOriginal;
        refrescar();
      }
      reprogramar();
    });
    // Intervalo adaptativo: 2.5s mirando, 15s oculta
    var intervalo = null;
    function reprogramar(){
      if (intervalo) clearInterval(intervalo);
      var ms = document.visibilityState === 'visible' ? 2500 : 15000;
      intervalo = setInterval(refrescar, ms);
      console.debug('[chats-poll] intervalo cada ' + ms + 'ms');
    }
    // Primer refresco INMEDIATO (no esperar 2.5s) y arranque del intervalo
    refrescar();
    reprogramar();

    // ── Menú contextual ──────────────────────────────────────────────
    var menu = document.getElementById('chat-ctx');
    var ctxTarget = null;
    function toastL(msg, isErr){
      var stack = document.getElementById('toast-stack-list');
      if (!stack) return;
      var el = document.createElement('div');
      el.className = 'toast' + (isErr ? ' error' : '');
      el.textContent = msg;
      stack.appendChild(el);
      setTimeout(function(){ el.style.opacity='0'; el.style.transform='translateY(20px)'; }, 2400);
      setTimeout(function(){ stack.removeChild(el); }, 2700);
    }
    async function cargarTagsEnMenu(cid){
      var cont = document.getElementById('ctx-tags-list');
      cont.innerHTML = '<div class="ctx-loading">Cargando…</div>';
      try {
        var r = await fetch('/admin/actions/cliente/' + cid + '/tags',
                            {headers: {'Accept':'application/json'}, cache: 'no-store'});
        if (!r.ok) { cont.innerHTML = '<div class="ctx-empty">Error al cargar.</div>'; return; }
        var data = await r.json();
        var tags = (data && data.tags) || [];
        if (tags.length === 0) {
          cont.innerHTML = '<div class="ctx-empty">No hay etiquetas creadas.</div>';
          return;
        }
        cont.innerHTML = tags.map(function(t){
          var onCls = t.asignado ? ' on' : '';
          var bg = t.asignado ? t.color : 'transparent';
          return '<div class="ctx-tag-item' + onCls + '" data-tag="' + t.id + '" data-color="' + t.color + '">' +
                 '  <span class="ctx-tag-check" style="background:' + bg + ';border-color:' + (t.asignado ? t.color : 'var(--c-border)') + ';"></span>' +
                 '  <span class="ctx-tag-swatch" style="background:' + t.color + ';"></span>' +
                 '  <span>' + (t.nombre.replace(/</g,'&lt;')) + '</span>' +
                 '</div>';
        }).join('');
      } catch(e) {
        cont.innerHTML = '<div class="ctx-empty">Error de red.</div>';
      }
    }
    function abrirMenu(x, y, item){
      ctxTarget = item;
      document.getElementById('ctx-target').textContent = item.dataset.nombre || 'Contacto';
      // Marca etiqueta actual
      var actual = item.dataset.estado || 'ninguna';
      menu.querySelectorAll('.ctx-item[data-et]').forEach(function(li){
        var v = li.dataset.et;
        li.classList.toggle('current', (v === '' && (actual === 'ninguna' || actual === '')) || v === actual);
      });
      menu.style.left = Math.min(x, window.innerWidth - 240) + 'px';
      menu.style.top = Math.min(y, window.innerHeight - 480) + 'px';
      menu.classList.add('open');
      menu.setAttribute('aria-hidden', 'false');
      // Cargar tags asíncrono (no bloquea apertura del menú)
      cargarTagsEnMenu(item.dataset.cliente);
    }
    function cerrarMenu(){
      menu.classList.remove('open');
      menu.setAttribute('aria-hidden', 'true');
      ctxTarget = null;
    }
    listEl.addEventListener('contextmenu', function(e){
      var item = e.target.closest('.chat-item');
      if (!item) return;
      e.preventDefault();
      abrirMenu(e.clientX, e.clientY, item);
    });
    document.addEventListener('click', function(e){
      if (!menu.contains(e.target)) cerrarMenu();
    });
    document.addEventListener('keydown', function(e){
      if (e.key === 'Escape') cerrarMenu();
    });
    menu.addEventListener('click', async function(e){
      // 1) Click en un tag custom → toggle (el menú PERMANECE abierto para
      //    permitir aplicar varios tags seguidos)
      var tagItem = e.target.closest('.ctx-tag-item');
      if (tagItem && ctxTarget) {
        var tagId = tagItem.dataset.tag;
        var target = ctxTarget;
        var cid = target.dataset.cliente;
        // Visual optimista
        tagItem.style.opacity = '0.4';
        try {
          var rt = await fetch('/admin/actions/cliente/' + cid + '/tag/' + tagId + '/toggle', {
            method: 'POST',
            headers: {'Accept':'application/json','X-Requested-With':'fetch'},
          });
          var dt = await rt.json().catch(function(){ return {ok:false}; });
          if (rt.ok && dt.ok) {
            var nowOn = (dt.accion === 'agregado');
            tagItem.classList.toggle('on', nowOn);
            var color = tagItem.dataset.color;
            var check = tagItem.querySelector('.ctx-tag-check');
            check.style.background = nowOn ? color : 'transparent';
            check.style.borderColor = nowOn ? color : 'var(--c-border)';
            // Actualizar data-tags del chat-item para que el filtro funcione
            var raw = (target.getAttribute('data-tags') || '').split(',').filter(Boolean);
            if (nowOn && raw.indexOf(tagId) === -1) raw.push(tagId);
            if (!nowOn) raw = raw.filter(function(x){ return x !== tagId; });
            target.setAttribute('data-tags', raw.join(','));
            // Refrescar chips visibles en la tarjeta (próximo poll lo hará)
            toastL(nowOn ? 'Tag aplicado' : 'Tag quitado');
            filtrar();
          } else {
            toastL('Error con la etiqueta', true);
          }
        } catch(err) { toastL('Error de red', true); }
        finally { tagItem.style.opacity = ''; }
        return;
      }

      var item = e.target.closest('.ctx-item');
      if (!item || !ctxTarget) return;
      if (item.dataset.action === 'abrir') {
        var href2 = ctxTarget.getAttribute('href');
        cerrarMenu();
        if (href2) window.location.href = href2;
        return;
      }
      // Etiqueta primaria
      if (item.hasAttribute('data-et')) {
        var et = item.dataset.et;
        var cid2 = ctxTarget.dataset.cliente;
        var target2 = ctxTarget;
        cerrarMenu();
        try {
          var fd = new FormData(); fd.append('etiqueta', et);
          var r = await fetch('/admin/actions/cliente/' + cid2 + '/etiqueta', {
            method: 'POST', body: fd,
            headers: {'Accept':'application/json','X-Requested-With':'fetch'},
          });
          var data = await r.json().catch(function(){ return {ok:false}; });
          if (r.ok && data.ok) {
            target2.setAttribute('data-estado', et || 'ninguna');
            toastL('Etiquetado: ' + (et || 'sin clasificar'));
            filtrar();
          } else {
            toastL('Error al etiquetar', true);
          }
        } catch(err) { toastL('Error de red', true); }
      }
    });
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
    <div class="chat-shell">
      <section class="chat-area">
        <div class="chat-header">
          <a href="/admin/chats" class="back-link" aria-label="Volver a chats">
            <svg width="14" height="14"><use href="#i-back"/></svg>
          </a>
          <div class="h-avatar">{{avatar_initial}}</div>
          <div class="h-info">
            <div class="h-name"><span class="h-name-txt" title="Para renombrar usa el panel derecho">{{nombre}}</span>{{bloqueado_chip}}{{header_pause_chip}}</div>
            <div class="h-sub">{{numero}} · {{total}} mensajes{{pause_sub_chip}}</div>
          </div>
          <div class="h-actions">{{header_pause_actions}}
            <button type="button" id="panel-toggle" class="h-collapse-btn" title="Mostrar/ocultar panel de contacto" aria-label="Toggle panel contacto">
              <svg class="icon-collapse" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
            </button>
          </div>
        </div>
        {{flash}}
        <div class="thread" id="thread">
          {{burbujas}}
        </div>
        <div class="composer" id="composer">
          <div class="composer-tabs">
            <button type="button" class="composer-tab active" data-mode="chat">Chat</button>
            <button type="button" class="composer-tab" data-mode="nota" title="Nota visible solo en el admin — no se envía por WhatsApp">Nota interna</button>
            <button type="button" id="attach-btn" class="composer-attach" title="Adjuntar imagen / documento" aria-label="Adjuntar">
              <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l8.57-8.57A4 4 0 1 1 17.93 8.84l-8.59 8.57a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
            </button>
          </div>
          <div id="attach-list" class="composer-attachments"></div>
          <form method="POST" action="/admin/chats/cliente/{{cliente_id}}/send" id="composer-form" enctype="multipart/form-data">
            <input type="file" id="file-input" name="files" multiple accept="image/*,video/*,application/pdf,.doc,.docx,.xls,.xlsx,.txt,.csv" hidden />
            <div class="composer-row">
              <textarea name="mensaje" placeholder="Escribe a {{nombre}}… (puedes pegar imágenes con Ctrl+V, o arrastrar archivos aquí)" rows="3" id="composer-text"></textarea>
              <button type="submit" class="send-btn" id="send-btn">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4 20-7Z"/></svg>
                Enviar
              </button>
            </div>
            <div class="composer-hint">
              <kbd>Enter</kbd> envía · <kbd>Shift+Enter</kbd> nueva línea · <kbd>Ctrl+V</kbd> pega imagen · Dairo queda pausada 1 h tras enviar.
            </div>
          </form>
        </div>
      </section>

      <aside class="contact-panel">
        <div class="cp-head">
          <div class="cp-avatar">{{avatar_initial}}</div>
          <div class="cp-name" id="cp-name" data-original="{{nombre}}" title="Click para editar el nombre">{{nombre}}</div>
          <div class="cp-num">{{numero}}</div>
          <div class="cp-tags" id="cp-tags-display">{{etiqueta_chip}}{{bloqueado_chip_panel}}</div>
        </div>
        <div class="cp-card">
          <div class="cp-section-title">Etiqueta</div>
          <div class="etiqueta-picker" data-cliente="{{cliente_id}}" data-actual="{{etiqueta_value}}">
            <button type="button" class="et-chip" data-value="cliente">Cliente</button>
            <button type="button" class="et-chip" data-value="prospecto">Prospecto</button>
            <button type="button" class="et-chip" data-value="equipo">Equipo</button>
            <button type="button" class="et-chip" data-value="personal">Personal</button>
            <button type="button" class="et-chip" data-value="">Sin clasificar</button>
          </div>
        </div>
        <div class="cp-card">
          <div class="cp-section-title">
            Tags de seguimiento
            <a href="/admin/etiquetas" class="cp-section-link" title="Gestionar etiquetas">Gestionar →</a>
          </div>
          <div class="tags-picker" id="tags-picker" data-cliente="{{cliente_id}}">
            <div class="tags-loading">Cargando…</div>
          </div>
        </div>
        <div class="cp-card">
          <div class="cp-section-title">Detalles</div>
          <div class="cp-fields">
            <div class="cp-field"><label>Ciudad</label><span class="val">{{ciudad}}</span></div>
            <div class="cp-field"><label>Barrio</label><span class="val">{{barrio}}</span></div>
            <div class="cp-field"><label>Último contacto</label><span class="val">{{ultimo_contacto}}</span></div>
          </div>
        </div>
        <div class="cp-actions">
          <a href="/admin/cliente/details/{{cliente_id}}" class="cp-btn">Editar datos</a>
          <form method="POST" action="/admin/actions/cliente/{{cliente_id}}/marcar-interno"
                style="margin:0;display:contents;"
                onsubmit="return confirm('Marcar {{numero}} como número interno (bodega/asesora/sistema)? El bot dejará de responderle.');">
            <button type="submit" class="cp-btn">Marcar como interno</button>
          </form>
          <a href="/admin/actions/cliente/{{cliente_id}}/reset-form" class="cp-btn danger">Resetear conversación</a>
          <a href="/admin/actions/cliente/{{cliente_id}}/nuke-form" class="cp-btn danger">Eliminar cliente</a>
        </div>
      </aside>
    </div>
  </main>
</div>
<div class="toast-stack" id="toast-stack"></div>

<!-- Lightbox modal -->
<div class="lightbox" id="lightbox" role="dialog" aria-modal="true">
  <button type="button" class="lightbox-btn lightbox-close" aria-label="Cerrar">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 6 6 18"/><path d="m6 6 12 12"/></svg>
  </button>
  <button type="button" class="lightbox-btn lightbox-prev" aria-label="Anterior">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
  </button>
  <button type="button" class="lightbox-btn lightbox-next" aria-label="Siguiente">
    <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 18 15 12 9 6"/></svg>
  </button>
  <div class="lightbox-spinner" aria-hidden="true"></div>
  <img id="lightbox-img" alt="vista previa"/>
  <div class="lightbox-info" id="lightbox-info"></div>
</div>

__THEME_JS__
<script>
(function(){
  var t = document.getElementById('thread');
  function scrollFondo(){ if (t) t.scrollTop = t.scrollHeight; }
  // Scroll inicial + reintentos cuando cargan imágenes/audios (el scrollHeight
  // crece a medida que cargan, y si solo hacemos scroll una vez quedamos arriba).
  scrollFondo();
  setTimeout(scrollFondo, 50);
  setTimeout(scrollFondo, 250);
  setTimeout(scrollFondo, 700);
  setTimeout(scrollFondo, 1500);
  // Cuando cualquier <img> termina de cargar dentro del thread → re-scroll si estabamos cerca del fondo
  if (t) {
    t.querySelectorAll('img').forEach(function(img){
      img.addEventListener('load', function(){
        var nearBottom = (t.scrollHeight - t.scrollTop - t.clientHeight) < 300;
        if (nearBottom) scrollFondo();
      });
    });
  }
  var lastId = {{ultimo_id}};
  var cid = {{cliente_id}};

  function toast(msg, isError){
    var stack = document.getElementById('toast-stack');
    if (!stack) return;
    var el = document.createElement('div');
    el.className = 'toast' + (isError ? ' error' : '');
    el.textContent = msg;
    stack.appendChild(el);
    setTimeout(function(){ el.style.opacity = '0'; el.style.transform = 'translateY(20px)'; }, 2400);
    setTimeout(function(){ stack.removeChild(el); }, 2700);
  }

  // Polling de mensajes nuevos cada 4 s
  async function poll(){
    try {
      var r = await fetch('/admin/chats/' + cid + '/messages.json?after_id=' + lastId, {headers:{'Accept':'text/html'}});
      if (!r.ok) return;
      var html = (await r.text()).trim();
      var hdr = r.headers.get('X-Last-Id');
      if (html && t) {
        var nearBottom = (t.scrollHeight - t.scrollTop - t.clientHeight) < 160;
        t.insertAdjacentHTML('beforeend', html);
        if (nearBottom) t.scrollTop = t.scrollHeight;
      }
      if (hdr) { var n = parseInt(hdr, 10); if (!isNaN(n)) lastId = n; }
    } catch(e) {}
  }
  setInterval(poll, 4000);

  // ── Composer: AJAX send + textarea + upload ─────────────────────────
  var ta = document.getElementById('composer-text');
  var form = document.getElementById('composer-form');
  var sendBtn = document.getElementById('send-btn');
  var fileInput = document.getElementById('file-input');
  var attachBtn = document.getElementById('attach-btn');
  var attachList = document.getElementById('attach-list');
  var composer = document.getElementById('composer');
  if (!ta || !form) return;

  // Lista de archivos pendientes (FileList no es mutable, mantenemos array)
  var pendingFiles = [];

  function renderAttachments(){
    attachList.innerHTML = '';
    pendingFiles.forEach(function(f, i){
      var div = document.createElement('div');
      div.className = 'attach-preview';
      if (f.type && f.type.startsWith('image/')) {
        var img = document.createElement('img');
        img.src = URL.createObjectURL(f);
        div.appendChild(img);
      } else {
        div.textContent = '📄';
      }
      var name = document.createElement('span');
      name.className = 'attach-name';
      name.textContent = f.name || ('archivo ' + (i+1));
      div.appendChild(name);
      var rm = document.createElement('button');
      rm.type = 'button';
      rm.className = 'attach-remove';
      rm.textContent = '×';
      rm.onclick = function(){ pendingFiles.splice(i,1); renderAttachments(); };
      div.appendChild(rm);
      attachList.appendChild(div);
    });
  }

  function addFiles(files){
    if (!files) return;
    for (var i = 0; i < files.length; i++) {
      var f = files[i];
      if (!f) continue;
      // Limit a 16MB cada uno (whapi)
      if (f.size > 16 * 1024 * 1024) { toast('Archivo muy grande: ' + (f.name||'') + ' (max 16MB)', true); continue; }
      pendingFiles.push(f);
    }
    renderAttachments();
  }

  attachBtn.addEventListener('click', function(){ fileInput.click(); });
  fileInput.addEventListener('change', function(e){
    addFiles(e.target.files);
    fileInput.value = '';
  });

  // Drag & drop sobre todo el composer
  ['dragenter','dragover'].forEach(function(ev){
    composer.addEventListener(ev, function(e){
      e.preventDefault(); e.stopPropagation();
      composer.classList.add('drag-over');
    });
  });
  ['dragleave','drop'].forEach(function(ev){
    composer.addEventListener(ev, function(e){
      e.preventDefault(); e.stopPropagation();
      composer.classList.remove('drag-over');
    });
  });
  composer.addEventListener('drop', function(e){
    if (e.dataTransfer && e.dataTransfer.files) addFiles(e.dataTransfer.files);
  });

  // Paste imagen desde portapapeles
  ta.addEventListener('paste', function(e){
    if (!e.clipboardData) return;
    var items = e.clipboardData.items || [];
    var added = [];
    for (var i = 0; i < items.length; i++) {
      var it = items[i];
      if (it.kind === 'file') {
        var f = it.getAsFile();
        if (f) added.push(f);
      }
    }
    if (added.length) { addFiles(added); }
  });

  // Submit AJAX (sin reload)
  function setSending(s){
    sendBtn.disabled = s;
    sendBtn.style.opacity = s ? '.6' : '';
    sendBtn.textContent = s ? 'Enviando…' : '';
    if (!s) {
      sendBtn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M22 2 11 13"/><path d="m22 2-7 20-4-9-9-4 20-7Z"/></svg> Enviar';
    }
  }

  async function submitAjax(){
    var texto = ta.value.trim();
    if (!texto && pendingFiles.length === 0) return;
    setSending(true);
    var fd = new FormData();
    if (texto) fd.append('mensaje', texto);
    pendingFiles.forEach(function(f){ fd.append('files', f, f.name); });
    try {
      var r = await fetch(form.action, {
        method: 'POST',
        body: fd,
        headers: { 'Accept': 'application/json', 'X-Requested-With': 'fetch' },
      });
      var data = await r.json().catch(function(){ return {ok:false, error:'respuesta inválida'}; });
      if (r.ok && data.ok) {
        ta.value = '';
        pendingFiles = [];
        renderAttachments();
        ta.style.height = 'auto';
        // Trae los mensajes nuevos enseguida en vez de esperar al poll
        await poll();
        if (t) t.scrollTop = t.scrollHeight;
        toast('Enviado');
      } else {
        var err = (data && (data.error || (data.errores||[]).join(', '))) || ('HTTP ' + r.status);
        toast('Error: ' + err, true);
      }
    } catch(e) {
      toast('Error de red: ' + e.message, true);
    } finally {
      setSending(false);
    }
  }

  form.addEventListener('submit', function(e){
    e.preventDefault();
    submitAjax();
  });
  ta.addEventListener('keydown', function(e){
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); submitAjax(); }
  });

  // ── Tabs Chat / Nota interna ────────────────────────────────────────
  var modoActual = 'chat';
  var tabs = document.querySelectorAll('.composer-tab[data-mode]');
  tabs.forEach(function(tab){
    tab.addEventListener('click', function(){
      modoActual = tab.dataset.mode;
      tabs.forEach(function(t){ t.classList.toggle('active', t === tab); });
      composer.classList.toggle('modo-nota', modoActual === 'nota');
      ta.placeholder = modoActual === 'nota'
        ? 'Escribe una nota interna sobre este chat (solo visible para el equipo)...'
        : 'Escribe a {{nombre}}... (Ctrl+V pega imágenes, arrastra archivos)';
    });
  });

  // Modifico submitAjax para incluir el modo: agrego un hidden input dinámico
  var modoInput = document.createElement('input');
  modoInput.type = 'hidden'; modoInput.name = 'modo'; modoInput.value = 'chat';
  form.appendChild(modoInput);
  var _origSubmit = submitAjax;
  submitAjax = async function(){
    modoInput.value = modoActual;
    await _origSubmit();
  };

  // ── Panel colapsable ────────────────────────────────────────────────
  var shell = document.querySelector('.chat-shell');
  var panelBtn = document.getElementById('panel-toggle');
  if (shell && panelBtn) {
    if (localStorage.getItem('chat_panel_collapsed') === '1') {
      shell.classList.add('panel-collapsed');
    }
    panelBtn.addEventListener('click', function(){
      shell.classList.toggle('panel-collapsed');
      localStorage.setItem('chat_panel_collapsed',
        shell.classList.contains('panel-collapsed') ? '1' : '0');
    });
  }

  // ── Audio player custom ─────────────────────────────────────────────
  function fmtTime(s){
    if (!isFinite(s)) return '0:00';
    var m = Math.floor(s/60); var ss = Math.floor(s%60);
    return m + ':' + (ss < 10 ? '0' : '') + ss;
  }
  function decorarAudio(el){
    if (!el || el.dataset.bound) return;
    el.dataset.bound = '1';
    var src = el.dataset.src;
    var audio = new Audio();
    audio.preload = 'metadata';
    audio.src = src;
    var btn = el.querySelector('.ap-play');
    var prog = el.querySelector('.ap-progress');
    var time = el.querySelector('.ap-time');
    var iconPlay = el.querySelector('.ap-ico-play');
    var iconPause = el.querySelector('.ap-ico-pause');
    var wave = el.querySelector('.ap-wave');

    audio.addEventListener('loadedmetadata', function(){ time.textContent = fmtTime(audio.duration); });
    audio.addEventListener('timeupdate', function(){
      var pct = audio.duration ? (audio.currentTime / audio.duration * 100) : 0;
      prog.style.width = pct + '%';
      time.textContent = fmtTime(audio.duration - audio.currentTime);
    });
    audio.addEventListener('ended', function(){
      iconPlay.style.display = ''; iconPause.style.display = 'none';
      prog.style.width = '0%';
      time.textContent = fmtTime(audio.duration);
    });
    btn.addEventListener('click', function(){
      if (audio.paused) {
        // Pausar cualquier otro audio que esté sonando
        document.querySelectorAll('audio').forEach(function(a){ if (!a.paused) a.pause(); });
        audio.play();
        iconPlay.style.display = 'none'; iconPause.style.display = '';
      } else {
        audio.pause();
        iconPlay.style.display = ''; iconPause.style.display = 'none';
      }
    });
    wave.addEventListener('click', function(e){
      var rect = wave.getBoundingClientRect();
      var ratio = (e.clientX - rect.left) / rect.width;
      if (audio.duration) audio.currentTime = audio.duration * ratio;
    });
  }
  function decorarTodosAudios(){
    document.querySelectorAll('.audio-player').forEach(decorarAudio);
  }
  decorarTodosAudios();
  // Re-decorar después de cada poll() — observamos cambios en el thread
  var mo = new MutationObserver(function(){ decorarTodosAudios(); });
  if (t) mo.observe(t, { childList: true, subtree: true });

  // ── Etiqueta picker (panel derecho) ─────────────────────────────────
  var picker = document.querySelector('.etiqueta-picker');
  if (picker) {
    var cid = picker.dataset.cliente;
    var actual = picker.dataset.actual || '';
    function marcarActiva(val){
      picker.querySelectorAll('.et-chip').forEach(function(c){
        c.classList.toggle('active', c.dataset.value === val);
      });
    }
    marcarActiva(actual);
    picker.addEventListener('click', async function(e){
      var btn = e.target.closest('.et-chip');
      if (!btn) return;
      var nuevo = btn.dataset.value;
      btn.classList.add('saving');
      try {
        var fd = new FormData(); fd.append('etiqueta', nuevo);
        var r = await fetch('/admin/actions/cliente/' + cid + '/etiqueta', {
          method: 'POST', body: fd,
          headers: {'Accept':'application/json', 'X-Requested-With':'fetch'},
        });
        var data = await r.json().catch(function(){ return {ok:false}; });
        if (r.ok && data.ok) {
          marcarActiva(nuevo);
          toast('Etiqueta actualizada');
        } else {
          toast('Error: ' + (data.error || r.status), true);
        }
      } catch(err) { toast('Error de red', true); }
      finally { btn.classList.remove('saving'); }
    });
  }

  // ── Tags de seguimiento (panel derecho) ─────────────────────────────
  var tagsPicker = document.getElementById('tags-picker');
  if (tagsPicker) {
    var tagsCid = tagsPicker.dataset.cliente;
    async function cargarTags(){
      try {
        var r = await fetch('/admin/actions/cliente/' + tagsCid + '/tags', {
          headers: {'Accept':'application/json'}, cache: 'no-store',
        });
        if (!r.ok) { tagsPicker.innerHTML = '<div class="tags-empty">No se pudieron cargar.</div>'; return; }
        var data = await r.json();
        var tags = data.tags || [];
        if (tags.length === 0) {
          tagsPicker.innerHTML = '<div class="tags-empty">No hay etiquetas creadas. <a href="/admin/etiquetas">Crea la primera →</a></div>';
          return;
        }
        tagsPicker.innerHTML = tags.map(function(t){
          var on = t.asignado ? ' on' : '';
          var bg = t.asignado ? t.color : 'transparent';
          var desc = t.descripcion ? (' title="' + t.descripcion.replace(/"/g,'&quot;') + '"') : '';
          return '<button type="button" class="tag-pick' + on + '"' + desc +
                 ' data-tag="' + t.id + '"' +
                 ' style="background:' + bg + ';' + (t.asignado ? 'border-color:' + t.color : '') + ';">' +
                 (t.nombre.replace(/</g,'&lt;')) + '</button>';
        }).join('');
      } catch(e) {
        tagsPicker.innerHTML = '<div class="tags-empty">Error de red.</div>';
      }
    }
    tagsPicker.addEventListener('click', async function(e){
      var btn = e.target.closest('.tag-pick');
      if (!btn) return;
      var tagId = btn.dataset.tag;
      btn.style.opacity = '0.4';
      try {
        var r = await fetch('/admin/actions/cliente/' + tagsCid + '/tag/' + tagId + '/toggle', {
          method: 'POST',
          headers: {'Accept':'application/json','X-Requested-With':'fetch'},
        });
        var data = await r.json().catch(function(){ return {ok:false}; });
        if (r.ok && data.ok) {
          await cargarTags();
          toast(data.accion === 'agregado' ? 'Etiqueta agregada' : 'Etiqueta quitada');
        } else {
          toast('Error al guardar', true);
          btn.style.opacity = '';
        }
      } catch(err) {
        toast('Error de red', true);
        btn.style.opacity = '';
      }
    });
    cargarTags();
  }

  // ── Nombre editable inline (en panel derecho) ───────────────────────
  var nameEl = document.getElementById('cp-name');
  if (nameEl) {
    var cidName = (picker && picker.dataset.cliente) || {{cliente_id}};
    nameEl.addEventListener('click', function(){
      if (nameEl.dataset.editing) return;
      nameEl.dataset.editing = '1';
      nameEl.contentEditable = 'true';
      nameEl.classList.add('editing');
      // Selecciona el texto
      var range = document.createRange(); range.selectNodeContents(nameEl);
      var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      nameEl.focus();
    });
    async function guardarNombre(){
      if (!nameEl.dataset.editing) return;
      nameEl.dataset.editing = '';
      nameEl.contentEditable = 'false';
      nameEl.classList.remove('editing');
      var nuevo = nameEl.textContent.trim();
      var original = nameEl.dataset.original || '';
      if (nuevo === original) return;
      try {
        var fd = new FormData(); fd.append('nombre', nuevo);
        var r = await fetch('/admin/actions/cliente/' + cidName + '/rename', {
          method: 'POST', body: fd,
          headers: {'Accept':'application/json','X-Requested-With':'fetch'},
        });
        var data = await r.json().catch(function(){ return {ok:false}; });
        if (r.ok && data.ok) {
          nameEl.dataset.original = nuevo;
          // Actualiza también el header y el avatar inicial si aplica
          var hName = document.querySelector('.chat-header .h-name');
          if (hName) {
            // mantener trailing chips (BLOQ, PAUSADA) — solo cambiamos el primer text node
            var txt = nuevo || '(sin nombre)';
            for (var i=0; i<hName.childNodes.length; i++) {
              if (hName.childNodes[i].nodeType === 3) { hName.childNodes[i].nodeValue = txt + ' '; break; }
            }
          }
          toast('Nombre actualizado');
        } else {
          nameEl.textContent = original; // revert
          toast('Error al renombrar', true);
        }
      } catch(err) {
        nameEl.textContent = original;
        toast('Error de red', true);
      }
    }
    nameEl.addEventListener('blur', guardarNombre);
    nameEl.addEventListener('keydown', function(e){
      if (e.key === 'Enter') { e.preventDefault(); nameEl.blur(); }
      if (e.key === 'Escape') {
        e.preventDefault();
        nameEl.textContent = nameEl.dataset.original || '';
        nameEl.dataset.editing = '';
        nameEl.contentEditable = 'false';
        nameEl.classList.remove('editing');
      }
    });
  }

  // ── Eliminar mensaje (botón × en cada burbuja al hover) ─────────────
  document.addEventListener('click', async function(e){
    var btn = e.target.closest('.msg-del-btn');
    if (!btn) return;
    e.preventDefault();
    e.stopPropagation();
    var msgId = btn.dataset.msgId;
    var msgEl = btn.closest('.msg');
    var preview = (msgEl.querySelector('.msg-bubble') || {}).textContent || '';
    if (!confirm('¿Eliminar este mensaje? Se intenta borrar también de WhatsApp.\\n\\n"' + preview.slice(0,100) + '"')) return;
    btn.disabled = true;
    try {
      var r = await fetch('/admin/chats/mensaje/' + msgId + '/eliminar', {
        method: 'POST',
        headers: {'Accept':'application/json','X-Requested-With':'fetch'},
      });
      var d = await r.json();
      if (r.ok && d.ok) {
        msgEl.style.opacity = '0';
        setTimeout(function(){ msgEl.remove(); }, 300);
        toast(d.borrado_wa ? 'Eliminado (también de WhatsApp)' : 'Eliminado de BD. WhatsApp: ' + (d.error_wa || 'no se pudo'), !d.borrado_wa);
      } else {
        toast('Error al eliminar', true);
        btn.disabled = false;
      }
    } catch(err) {
      toast('Error de red', true);
      btn.disabled = false;
    }
  });

  // ── Botón "Reintentar respuesta" ────────────────────────────────────
  // - Prospecto / sin clasificar: el bot redacta Y envía solo (background).
  // - Equipo / cliente WL: el bot redacta un BORRADOR y lo pega en el
  //   composer para que el operador lo lea/edite/envíe.
  var btnRetry = document.getElementById('btn-reintentar');
  if (btnRetry) {
    btnRetry.addEventListener('click', async function(){
      btnRetry.disabled = true;
      var orig = btnRetry.innerHTML;
      btnRetry.innerHTML = '...';
      try {
        var r = await fetch('/admin/actions/cliente/' + cid + '/reintentar-respuesta', {
          method: 'POST',
          headers: {'Accept':'application/json','X-Requested-With':'fetch'},
        });
        var data = await r.json().catch(function(){ return {ok:false}; });
        if (r.ok && data.ok) {
          if (typeof data.borrador === 'string') {
            // Modo borrador (equipo/cliente_wl): pegar en composer
            var taEl = document.getElementById('composer-text');
            if (taEl) {
              taEl.value = data.borrador;
              taEl.focus();
              try {
                taEl.setSelectionRange(taEl.value.length, taEl.value.length);
                taEl.style.height = 'auto';
                taEl.style.height = Math.min(taEl.scrollHeight, 360) + 'px';
              } catch(_) {}
            }
            toast(data.borrador
              ? 'Borrador listo en el composer — léelo y dale Enviar si te sirve'
              : 'El bot no pudo generar un borrador. Escríbelo tú', !data.borrador);
          } else {
            toast('Reintentando — la respuesta llegará en segundos');
          }
        } else {
          toast('Error: ' + (data.error || r.status), true);
        }
      } catch(err) { toast('Error de red', true); }
      finally {
        btnRetry.disabled = false;
        btnRetry.innerHTML = orig;
      }
    });
  }

  // ── Lightbox de imágenes ────────────────────────────────────────────
  var lb = document.getElementById('lightbox');
  var lbImg = document.getElementById('lightbox-img');
  var lbInfo = document.getElementById('lightbox-info');
  var lbImages = []; var lbIdx = 0;
  function recolectarImagenes(){
    lbImages = Array.prototype.slice.call(document.querySelectorAll('.thread .msg-media'))
      .filter(function(el){ return el.tagName === 'IMG'; });
  }
  function mostrarLightbox(i){
    recolectarImagenes();
    if (!lbImages.length) return;
    lbIdx = (i + lbImages.length) % lbImages.length;
    lb.classList.add('open', 'loading');
    lbImg.classList.remove('loaded');
    lbImg.src = lbImages[lbIdx].src;
    lbInfo.textContent = (lbIdx + 1) + ' / ' + lbImages.length;
    document.body.style.overflow = 'hidden';
  }
  lbImg.addEventListener('load', function(){
    lb.classList.remove('loading');
    lbImg.classList.add('loaded');
  });
  lbImg.addEventListener('error', function(){
    lb.classList.remove('loading');
    lbInfo.textContent = 'No se pudo cargar la imagen';
  });
  function cerrarLightbox(){
    lb.classList.remove('open');
    document.body.style.overflow = '';
    lbImg.src = '';
  }
  document.addEventListener('click', function(e){
    var img = e.target.closest('.thread .msg-media');
    if (img && img.tagName === 'IMG') {
      e.preventDefault(); e.stopPropagation();
      recolectarImagenes();
      var idx = lbImages.indexOf(img);
      mostrarLightbox(idx >= 0 ? idx : 0);
    }
  });
  lb.querySelector('.lightbox-close').addEventListener('click', cerrarLightbox);
  lb.querySelector('.lightbox-prev').addEventListener('click', function(){ mostrarLightbox(lbIdx - 1); });
  lb.querySelector('.lightbox-next').addEventListener('click', function(){ mostrarLightbox(lbIdx + 1); });
  lb.addEventListener('click', function(e){ if (e.target === lb) cerrarLightbox(); });
  document.addEventListener('keydown', function(e){
    if (!lb.classList.contains('open')) return;
    if (e.key === 'Escape') cerrarLightbox();
    if (e.key === 'ArrowLeft') mostrarLightbox(lbIdx - 1);
    if (e.key === 'ArrowRight') mostrarLightbox(lbIdx + 1);
  });
})();
</script>
</body></html>"""
