"""
Acciones admin custom — operaciones que SQLAdmin no cubre con CRUD básico.

Endpoints:
    POST /admin/actions/cliente/{id}/reset
        Borra TODAS las conversaciones, sesión, pausa de un cliente,
        pero mantiene el registro del cliente. Útil para "empezar limpio"
        sin perder el número.

    POST /admin/actions/cliente/{id}/nuke
        BORRADO TOTAL: cliente + pedidos + alertas + conversaciones + sesión.
        Útil cuando SQLAdmin se traba con FK (pedidos.cliente_id_fkey).

    POST /admin/actions/cliente/{id}/bloquear
        Marca al cliente como bloqueado=True (el bot no le responde).

    POST /admin/actions/cliente/{id}/desbloquear
        Marca como bloqueado=False.

    POST /admin/actions/equipo/recargar-cache
        Invalida el cache en memoria del directorio del equipo
        (útil tras editar miembros desde el admin).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, select, text as sa_text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    AlertaFabio,
    Cliente,
    Conversacion,
    IntervencionHumana,
    Pedido,
    Sesion,
    WebhookProcesado,
)
from app.db.session import get_session
from app.equipo.directorio import invalidar_cache
from app.logging_setup import log

router = APIRouter(prefix="/admin/actions", tags=["admin-actions"])


def _check_auth(request: Request) -> bool:
    return "admin_token" in request.session


@router.post("/cliente/{cliente_id}/reset")
async def reset_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Borra todas las conversaciones, sesión y pausa de un cliente."""
    if not _check_auth(request):
        raise HTTPException(401)

    # Cliente sigue existiendo
    cliente = (await session.execute(
        Cliente.__table__.select().where(Cliente.id == cliente_id)
    )).first()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    # Borrar conversaciones del cliente
    n_conv = await session.execute(
        delete(Conversacion).where(Conversacion.cliente_id == cliente_id)
    )

    # Borrar sesión
    await session.execute(
        delete(Sesion).where(Sesion.cliente_id == cliente_id)
    )

    # Borrar pausa por humano
    await session.execute(
        delete(IntervencionHumana).where(IntervencionHumana.cliente_id == cliente_id)
    )

    await session.commit()

    log.info("admin.cliente.reset", cliente_id=cliente_id, conversaciones_borradas=n_conv.rowcount)

    # Redirige de vuelta al detalle del cliente con mensaje flash en query
    return RedirectResponse(
        f"/admin/cliente/details/{cliente_id}?msg=reset_ok",
        status_code=303,
    )


@router.post("/cliente/{cliente_id}/bloquear")
async def bloquear_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    await session.execute(
        update(Cliente).where(Cliente.id == cliente_id).values(bloqueado=True)
    )
    await session.commit()
    log.info("admin.cliente.bloqueado", cliente_id=cliente_id)
    return RedirectResponse(f"/admin/cliente/details/{cliente_id}", status_code=303)


@router.post("/cliente/{cliente_id}/pausar-laura")
async def pausar_laura_manual(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Pausa Laura 1h para este cliente sin enviar mensaje. Toggle manual
    desde la vista de chat: el admin decide que NO quiere que el bot
    responda a este cliente por un rato (tomó el chat o no quiere bot)."""
    if not _check_auth(request):
        raise HTTPException(401)
    from app.db.repos import pausar_bot
    await pausar_bot(
        session,
        cliente_id=cliente_id,
        horas=1,
        razon="admin pausó manualmente desde /admin/chats",
    )
    await session.commit()
    log.info("admin.cliente.pausar_laura_manual", cliente_id=cliente_id)
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=pausado", status_code=303)


@router.post("/cliente/{cliente_id}/pausar-indefinido")
async def pausar_indefinido(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Pausa Dairo INDEFINIDAMENTE en este chat (hasta reactivación manual).

    Útil para casos donde el admin quiere apagar el bot en un chat hasta nuevo
    aviso (ej: tu propio número, contactos sensibles, manejo full-humano).
    """
    if not _check_auth(request):
        raise HTTPException(401)
    # 100 años de pausa = en la práctica, indefinida.
    hasta = datetime.now(timezone.utc) + timedelta(days=365 * 100)
    await session.execute(sa_text("""
        INSERT INTO intervencion_humana (cliente_id, pausado_hasta, razon)
        VALUES (:c, :h, :r)
        ON CONFLICT (cliente_id) DO UPDATE
        SET pausado_hasta = EXCLUDED.pausado_hasta, razon = EXCLUDED.razon
    """), {
        "c": cliente_id,
        "h": hasta,
        "r": "admin pausó indefinidamente desde /admin/chats",
    })
    await session.commit()
    log.warning("admin.cliente.pausar_indefinido", cliente_id=cliente_id)
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=pausado_indef", status_code=303)


@router.post("/cliente/{cliente_id}/reactivar-laura")
async def reactivar_laura(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Quita la pausa y, si hay un inbound sin responder, dispara el flow.

    El operador esperaría que "reanudar" implique también que el bot retome la
    conversación con el último mensaje del cliente. Si el último mensaje del
    chat es OUTBOUND (ya respondimos), solo quita la pausa.
    """
    if not _check_auth(request):
        raise HTTPException(401)

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        raise HTTPException(404)

    # 1) Quitar pausa
    await session.execute(sa_text(
        "DELETE FROM intervencion_humana WHERE cliente_id = :cid"
    ), {"cid": cliente_id})

    # 2) ¿Hay un inbound posterior al último outbound/humano? — si no, solo reactivar
    ultimo = (await session.execute(sa_text("""
        SELECT id, direccion, contenido, tipo, media_url, whapi_message_id, timestamp
        FROM conversaciones
        WHERE cliente_id = :cid
        ORDER BY id DESC LIMIT 1
    """), {"cid": cliente_id})).first()

    if not ultimo or ultimo.direccion != "inbound" or cliente.etiqueta == "personal":
        await session.commit()
        log.info("admin.cliente.reactivar_laura", cliente_id=cliente_id, accion="solo_pausa")
        if _es_ajax(request):
            return {"ok": True, "msg": "Reactivado (no hay mensaje pendiente)"}
        return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reactivado", status_code=303)

    await session.commit()

    # 3) Hay inbound pendiente → disparar el flow correspondiente
    from app.whapi.parser import MensajeWhapi
    msg = MensajeWhapi(
        id=ultimo.whapi_message_id or f"replay_{ultimo.id}",
        from_number=cliente.numero_whatsapp,
        to_number=None,
        direccion="inbound",
        is_from_bot=False,
        is_from_human=False,
        tipo=ultimo.tipo or "texto",
        texto=ultimo.contenido,
        media_url=ultimo.media_url,
        media_mime=None,
        caption=None,
        timestamp=int(ultimo.timestamp.timestamp()) if ultimo.timestamp else 0,
        chat_id="",
        raw={"replay": True, "from_conv_id": ultimo.id, "trigger": "reactivar-laura"},
        from_name=cliente.nombre,
    )

    import asyncio
    from app.equipo.directorio import es_miembro_equipo, whitelist_cliente
    from app.identidades import principal as _identidad_principal

    miembro = es_miembro_equipo(cliente.numero_whatsapp)
    cliente_wl = whitelist_cliente(cliente.numero_whatsapp)
    ident = _identidad_principal()
    autor = request.session.get("admin_user", "admin")

    if miembro or cliente_wl:
        from app.flows.equipo import procesar_mensaje_equipo
        miembro_obj = miembro or cliente_wl
        async def _run():
            from app.db.session import async_session_factory
            async with async_session_factory() as s:
                try:
                    await procesar_mensaje_equipo(
                        session=s, miembro=miembro_obj, msg=msg, identidad=ident,
                    )
                    await s.commit()
                except Exception:
                    await s.rollback()
                    log.exception("admin.reactivar.equipo_fail", cliente_id=cliente_id)
        asyncio.create_task(_run())
    else:
        from app.flows.conversation import procesar_mensaje_inbound
        async def _run():
            from app.db.session import async_session_factory
            async with async_session_factory() as s:
                try:
                    await procesar_mensaje_inbound(
                        session=s, cliente_id=cliente_id,
                        cliente_numero=cliente.numero_whatsapp,
                        msg=msg, identidad=ident,
                    )
                    await s.commit()
                except Exception:
                    await s.rollback()
                    log.exception("admin.reactivar.prospecto_fail", cliente_id=cliente_id)
        asyncio.create_task(_run())

    log.warning("admin.cliente.reactivar_laura", cliente_id=cliente_id, accion="reactivar_y_procesar", autor=autor)
    if _es_ajax(request):
        return {"ok": True, "msg": "Reactivado — el bot ya está procesando el último mensaje"}
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reactivando", status_code=303)


@router.post("/cliente/{cliente_id}/marcar-interno")
async def marcar_cliente_interno(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Marca el número del cliente como interno (bodega/asesora/sistema).

    - INSERT/UPSERT en numeros_internos con activo=true
    - Invalida cache del directorio (efecto <1s)
    - Pausa el cliente 24h (cancela respuestas pendientes del humanizer)
    """
    if not _check_auth(request):
        raise HTTPException(401)
    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        return RedirectResponse("/admin/chats?msg=cliente_no_existe", status_code=303)

    nombre_default = cliente.nombre or "Número interno"
    await session.execute(sa_text("""
        INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo)
        VALUES (:n, :nom, :raz, true)
        ON CONFLICT (numero_whatsapp) DO UPDATE
        SET nombre = COALESCE(EXCLUDED.nombre, numeros_internos.nombre),
            razon  = COALESCE(EXCLUDED.razon, numeros_internos.razon),
            activo = true
    """), {
        "n": cliente.numero_whatsapp,
        "nom": nombre_default,
        "raz": "Marcado interno desde /admin/chats",
    })

    # Pausar 24h para cancelar respuestas pendientes en humanizer
    hasta = datetime.now(timezone.utc) + timedelta(hours=24)
    await session.execute(sa_text("""
        INSERT INTO intervencion_humana (cliente_id, pausado_hasta, razon)
        VALUES (:c, :h, :r)
        ON CONFLICT (cliente_id) DO UPDATE
        SET pausado_hasta = EXCLUDED.pausado_hasta, razon = EXCLUDED.razon
    """), {"c": cliente_id, "h": hasta, "r": "marcado interno desde admin"})

    await session.commit()

    # Invalidar cache del directorio
    try:
        from app.equipo.directorio import invalidar_cache
        invalidar_cache()
    except Exception:
        pass

    log.info(
        "admin.cliente.marcado_interno",
        cliente_id=cliente_id,
        numero=cliente.numero_whatsapp,
    )
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=marcado_interno", status_code=303)


@router.post("/cliente/{cliente_id}/desbloquear")
async def desbloquear_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        raise HTTPException(401)
    await session.execute(
        update(Cliente).where(Cliente.id == cliente_id).values(bloqueado=False)
    )
    await session.commit()
    log.info("admin.cliente.desbloqueado", cliente_id=cliente_id)
    return RedirectResponse(f"/admin/cliente/details/{cliente_id}", status_code=303)


def _es_ajax(request: Request) -> bool:
    return (
        "application/json" in (request.headers.get("accept") or "")
        or request.headers.get("x-requested-with") in ("fetch", "XMLHttpRequest")
    )


@router.post("/cliente/{cliente_id}/etiqueta")
async def cambiar_etiqueta(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Cambia la etiqueta del cliente (cliente|prospecto|equipo|personal|null).

    Acepta form `etiqueta`. Si llega vacío o "ninguna" → NULL (sin clasificar).
    Devuelve JSON si AJAX, redirect si no.
    """
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    raw = (form.get("etiqueta") or "").strip().lower()
    valor: str | None
    if raw in ("", "ninguna", "null", "none", "sin_clasificar"):
        valor = None
    elif raw in ("cliente", "prospecto", "equipo", "personal"):
        valor = raw
    else:
        if _es_ajax(request):
            return {"ok": False, "error": "etiqueta inválida"}
        raise HTTPException(400, "etiqueta inválida")

    autor = request.session.get("admin_user", "admin")
    await session.execute(
        update(Cliente)
        .where(Cliente.id == cliente_id)
        .values(
            etiqueta=valor,
            etiqueta_actualizada_en=datetime.now(timezone.utc),
            etiqueta_actualizada_por=f"admin:{autor}",
        )
    )
    await session.commit()
    log.info("admin.cliente.etiqueta", cliente_id=cliente_id, etiqueta=valor, autor=autor)

    # Si la etiqueta nueva habilita respuesta del bot (cliente/prospecto/equipo)
    # Y el último mensaje del chat es un INBOUND sin respuesta posterior
    # (típico: fue silenciado por política estricta cuando estaba sin clasificar),
    # disparar el flow correspondiente para que el bot retome la conversación.
    disparado = False
    if valor in ("cliente", "prospecto", "equipo"):
        cliente = (await session.execute(
            select(Cliente).where(Cliente.id == cliente_id)
        )).scalar_one_or_none()
        ultimo = (await session.execute(sa_text("""
            SELECT id, direccion, contenido, tipo, media_url, whapi_message_id, timestamp
            FROM conversaciones
            WHERE cliente_id = :cid
            ORDER BY id DESC LIMIT 1
        """), {"cid": cliente_id})).first()

        if cliente and ultimo and ultimo.direccion == "inbound":
            from app.whapi.parser import MensajeWhapi
            msg = MensajeWhapi(
                id=ultimo.whapi_message_id or f"replay_{ultimo.id}",
                from_number=cliente.numero_whatsapp,
                to_number=None,
                direccion="inbound",
                is_from_bot=False,
                is_from_human=False,
                tipo=ultimo.tipo or "texto",
                texto=ultimo.contenido,
                media_url=ultimo.media_url,
                media_mime=None,
                caption=None,
                timestamp=int(ultimo.timestamp.timestamp()) if ultimo.timestamp else 0,
                chat_id="",
                raw={"replay": True, "from_conv_id": ultimo.id, "trigger": "etiquetado_manual"},
                from_name=cliente.nombre,
            )

            import asyncio as _asyncio
            from app.equipo.directorio import es_miembro_equipo, whitelist_cliente
            from app.identidades import principal as _identidad_principal
            ident = _identidad_principal()
            miembro = es_miembro_equipo(cliente.numero_whatsapp)
            cliente_wl = whitelist_cliente(cliente.numero_whatsapp)

            if valor == "equipo" or miembro or cliente_wl:
                from app.flows.equipo import procesar_mensaje_equipo
                miembro_obj = miembro or cliente_wl
                if miembro_obj is not None:
                    async def _run():
                        from app.db.session import async_session_factory
                        async with async_session_factory() as s2:
                            try:
                                await procesar_mensaje_equipo(
                                    session=s2, miembro=miembro_obj, msg=msg, identidad=ident,
                                )
                                await s2.commit()
                            except Exception:
                                await s2.rollback()
                                log.exception("admin.etiqueta.dispatch_equipo_fail", cliente_id=cliente_id)
                    _asyncio.create_task(_run())
                    disparado = True
            else:  # cliente o prospecto → flow prospecto
                from app.flows.conversation import procesar_mensaje_inbound
                async def _run():
                    from app.db.session import async_session_factory
                    async with async_session_factory() as s2:
                        try:
                            await procesar_mensaje_inbound(
                                session=s2, cliente_id=cliente_id,
                                cliente_numero=cliente.numero_whatsapp,
                                msg=msg, identidad=ident,
                            )
                            await s2.commit()
                        except Exception:
                            await s2.rollback()
                            log.exception("admin.etiqueta.dispatch_prospecto_fail", cliente_id=cliente_id)
                _asyncio.create_task(_run())
                disparado = True

            if disparado:
                log.warning("admin.etiqueta.dispatch_flow", cliente_id=cliente_id,
                            etiqueta=valor, autor=autor)

    if _es_ajax(request):
        return {
            "ok": True,
            "etiqueta": valor or "sin_clasificar",
            "dispatched": disparado,
        }
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=etiqueta_ok", status_code=303)


@router.post("/cliente/{cliente_id}/reintentar-respuesta")
async def reintentar_respuesta(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Fuerza al bot a procesar el último mensaje inbound del cliente que quedó
    sin respuesta (Claude se quedó vacío después de tool, o se cayó el flow).

    Decide el flow según la etiqueta del cliente:
      - prospecto / NULL  → flow conversation.procesar_mensaje_inbound
      - equipo / cliente WL → flow equipo.procesar_mensaje_equipo
      - personal          → rechaza (es silencio explícito)
    """
    if not _check_auth(request):
        raise HTTPException(401)

    cliente = (await session.execute(
        select(Cliente).where(Cliente.id == cliente_id)
    )).scalar_one_or_none()
    if not cliente:
        if _es_ajax(request):
            return {"ok": False, "error": "cliente no encontrado"}
        raise HTTPException(404)

    if cliente.etiqueta == "personal":
        if _es_ajax(request):
            return {"ok": False, "error": "contacto marcado como personal — silencio explícito"}
        raise HTTPException(400, "contacto es personal")

    # 1) Si hay pausa individual activa → quitarla (el botón "Bot toma el
    #    relevo" implica explícitamente que el bot vuelva a atender).
    await session.execute(sa_text(
        "DELETE FROM intervencion_humana WHERE cliente_id = :cid"
    ), {"cid": cliente_id})

    # 2) Tomar el ÚLTIMO mensaje inbound del cliente (incluso si después hubo
    #    respuesta humana del admin). El flow carga todo el historial (48h)
    #    así que el bot ve también lo que tú respondiste manualmente.
    ultimo_inbound = (await session.execute(sa_text("""
        SELECT id, contenido, tipo, media_url, whapi_message_id, timestamp
        FROM conversaciones
        WHERE cliente_id = :cid AND direccion = 'inbound'
        ORDER BY id DESC LIMIT 1
    """), {"cid": cliente_id})).first()
    if not ultimo_inbound:
        await session.commit()  # al menos quedó sin pausa
        if _es_ajax(request):
            return {"ok": True, "msg": "Pausa quitada. Aún no hay mensajes del cliente para que el bot retome."}
        return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reactivado", status_code=303)
    await session.commit()

    # Construir MensajeWhapi mock con la última conversación inbound
    from app.whapi.parser import MensajeWhapi
    inb_id, inb_contenido, inb_tipo, inb_media, inb_whapi_id, inb_ts = ultimo_inbound
    msg = MensajeWhapi(
        id=inb_whapi_id or f"replay_{inb_id}",
        from_number=cliente.numero_whatsapp,
        to_number=None,
        direccion="inbound",
        is_from_bot=False,
        is_from_human=False,
        tipo=inb_tipo or "texto",
        texto=inb_contenido,
        media_url=inb_media,
        media_mime=None,
        caption=None,
        timestamp=int(inb_ts.timestamp()) if inb_ts else 0,
        chat_id="",
        raw={"replay": True, "from_conv_id": inb_id},
        from_name=cliente.nombre,
    )

    # Disparar el flow apropiado en background
    import asyncio
    from app.equipo.directorio import es_miembro_equipo, whitelist_cliente
    from app.identidades import principal as _identidad_principal

    miembro = es_miembro_equipo(cliente.numero_whatsapp)
    cliente_wl = whitelist_cliente(cliente.numero_whatsapp)
    ident = _identidad_principal()

    autor = request.session.get("admin_user", "admin")

    if miembro or cliente_wl:
        # Para EQUIPO o CLIENTE WL → modo "borrador": el bot genera el texto
        # SÍNCRONAMENTE pero NO lo envía. Se devuelve para que el JS lo pegue
        # en el composer y el operador lo lea/edite antes de enviar.
        from app.flows.equipo import procesar_mensaje_equipo
        miembro_obj = miembro or cliente_wl
        flow_label = "equipo" if miembro else "cliente_wl"
        try:
            borrador = await procesar_mensaje_equipo(
                session=session, miembro=miembro_obj, msg=msg, identidad=ident,
                solo_generar=True,
            )
            await session.commit()
        except Exception as e:
            await session.rollback()
            log.exception("admin.reintentar.borrador_fail", cliente_id=cliente_id)
            if _es_ajax(request):
                return {"ok": False, "error": str(e)[:200]}
            raise HTTPException(500, "no se pudo generar borrador")
        log.warning("admin.reintentar_respuesta",
                    cliente_id=cliente_id, flow=flow_label, modo="borrador", autor=autor)
        if _es_ajax(request):
            return {
                "ok": True,
                "borrador": borrador or "",
                "flow": flow_label,
                "msg": "borrador generado — léelo en el composer y dale Enviar si te sirve",
            }
        return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reintentando", status_code=303)

    # Prospecto o sin clasificar → comportamiento original (background + envío auto)
    from app.flows.conversation import procesar_mensaje_inbound
    async def _run():
        from app.db.session import async_session_factory
        async with async_session_factory() as s:
            try:
                await procesar_mensaje_inbound(
                    session=s, cliente_id=cliente_id,
                    cliente_numero=cliente.numero_whatsapp,
                    msg=msg, identidad=ident,
                )
                await s.commit()
            except Exception:
                await s.rollback()
                log.exception("admin.reintentar.prospecto_fail", cliente_id=cliente_id)
    asyncio.create_task(_run())
    log.warning("admin.reintentar_respuesta", cliente_id=cliente_id, flow="prospecto", autor=autor)

    if _es_ajax(request):
        return {"ok": True, "msg": "reintentando — la respuesta llegará en segundos"}
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reintentando", status_code=303)


@router.post("/cliente/{cliente_id}/rename")
async def renombrar_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Cambia el nombre visible del cliente. Acepta form `nombre` (vacío permitido)."""
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    nuevo = (form.get("nombre") or "").strip()
    nuevo_val: str | None = nuevo if nuevo else None
    await session.execute(
        update(Cliente).where(Cliente.id == cliente_id).values(nombre=nuevo_val)
    )
    await session.commit()
    log.info("admin.cliente.rename", cliente_id=cliente_id, nuevo_nombre=nuevo_val)
    if _es_ajax(request):
        return {"ok": True, "nombre": nuevo_val or ""}
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=nombre_ok", status_code=303)


# ─── Tags de seguimiento (M2M cliente↔tag) ─────────────────────────────────


@router.post("/cliente/{cliente_id}/tag/{tag_id}/toggle")
async def toggle_tag_cliente(
    cliente_id: int,
    tag_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Si el cliente ya tiene el tag → lo quita. Si no lo tiene → lo añade.

    Devuelve JSON {ok, accion: 'agregado'|'quitado'} para usar desde el panel
    del chat con un solo click.
    """
    if not _check_auth(request):
        raise HTTPException(401)

    existe = (await session.execute(sa_text(
        "SELECT 1 FROM cliente_tags WHERE cliente_id = :c AND tag_id = :t"
    ), {"c": cliente_id, "t": tag_id})).first()

    autor = request.session.get("admin_user", "admin")
    if existe:
        await session.execute(sa_text(
            "DELETE FROM cliente_tags WHERE cliente_id = :c AND tag_id = :t"
        ), {"c": cliente_id, "t": tag_id})
        accion = "quitado"
    else:
        await session.execute(sa_text("""
            INSERT INTO cliente_tags (cliente_id, tag_id, added_by)
            VALUES (:c, :t, :a)
            ON CONFLICT DO NOTHING
        """), {"c": cliente_id, "t": tag_id, "a": autor})
        accion = "agregado"
    await session.commit()
    log.info("admin.cliente.tag.toggle",
             cliente_id=cliente_id, tag_id=tag_id, accion=accion, autor=autor)
    return {"ok": True, "accion": accion}


@router.get("/cliente/{cliente_id}/tags")
async def listar_tags_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Lista TODOS los tags, marcando cuáles tiene este cliente."""
    if not _check_auth(request):
        raise HTTPException(401)
    rows = (await session.execute(sa_text("""
        SELECT t.id, t.nombre, t.color, t.descripcion, t.orden,
               (ct.cliente_id IS NOT NULL) AS asignado
          FROM tags t
          LEFT JOIN cliente_tags ct
            ON ct.tag_id = t.id AND ct.cliente_id = :cid
         ORDER BY t.orden ASC, t.nombre ASC
    """), {"cid": cliente_id})).all()
    return {
        "tags": [
            {"id": r.id, "nombre": r.nombre, "color": r.color,
             "descripcion": r.descripcion, "orden": r.orden,
             "asignado": bool(r.asignado)}
            for r in rows
        ]
    }


@router.post("/bot/modo")
async def cambiar_bot_modo(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Cambia el modo global del bot: todos / solo_prospectos / off.

    - todos:            responde a equipo + prospectos + clientes WL (default)
    - solo_prospectos:  responde solo a equipo + prospectos. Clientes WL silenciados.
    - off:              solo equipo. Resto silenciado.
    """
    if not _check_auth(request):
        raise HTTPException(401)
    form = await request.form()
    modo = (form.get("modo") or "").strip().lower()
    if modo not in ("todos", "solo_prospectos", "off"):
        if _es_ajax(request):
            return {"ok": False, "error": "modo inválido"}
        raise HTTPException(400, "modo inválido")
    activo = modo != "off"
    autor = request.session.get("admin_user", "admin")
    await session.execute(sa_text("""
        UPDATE bot_estado SET
          activo = :a,
          modo = :m,
          pausado_por = CASE WHEN :a THEN NULL ELSE :u END,
          pausado_en  = CASE WHEN :a THEN NULL ELSE now() END,
          razon       = CASE WHEN :a THEN NULL ELSE :r END,
          actualizado_en = now()
        WHERE id = 1
    """), {"a": activo, "m": modo, "u": autor, "r": f"Modo {modo} desde dashboard"})
    await session.commit()
    try:
        from app.main import invalidar_bot_estado_cache
        invalidar_bot_estado_cache()
    except Exception:
        pass
    log.warning("admin.bot.modo", modo=modo, autor=autor)
    if _es_ajax(request):
        return {"ok": True, "modo": modo, "activo": activo}
    return RedirectResponse("/admin/dashboard?msg=bot_modo", status_code=303)


@router.post("/bot/toggle")
async def toggle_bot(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Invierte el flag bot_estado.activo. Si está activo lo pausa, y viceversa."""
    if not _check_auth(request):
        raise HTTPException(401)
    row = (await session.execute(sa_text(
        "SELECT activo FROM bot_estado WHERE id=1"
    ))).first()
    estaba_activo = bool(row[0]) if row else True
    nuevo_estado = not estaba_activo
    if nuevo_estado:
        await session.execute(sa_text(
            "UPDATE bot_estado SET activo=true, pausado_por=null, "
            "pausado_en=null, razon=null, actualizado_en=now() WHERE id=1"
        ))
    else:
        await session.execute(sa_text(
            "UPDATE bot_estado SET activo=false, pausado_por='dashboard', "
            "pausado_en=now(), razon='Pausado desde dashboard web', "
            "actualizado_en=now() WHERE id=1"
        ))
    await session.commit()
    # Forzar que la próxima lectura del estado vaya a la DB (no quede en cache).
    # Sin esto, había hasta 2-5s de ventana donde el bot seguía respondiendo
    # como "activo" después del toggle.
    try:
        from app.main import invalidar_bot_estado_cache
        invalidar_bot_estado_cache()
    except Exception:
        pass
    log.warning("admin.bot.toggle", nuevo_estado="activo" if nuevo_estado else "pausado")
    # Notificar al panel admin externo si está configurado
    try:
        import asyncio as _aio
        from app.panel_admin_webhook import emitir_evento as _emit
        _aio.create_task(_emit("bot.estado_cambiado", {
            "activo": nuevo_estado, "por": "dashboard",
            "razon": "Toggle desde dashboard web",
        }))
    except Exception:
        pass
    return RedirectResponse("/admin?msg=bot_toggle", status_code=303)


@router.post("/equipo/recargar-cache")
async def recargar_cache_equipo(request: Request):
    if not _check_auth(request):
        raise HTTPException(401)
    invalidar_cache()
    log.info("admin.equipo.cache_invalidado")
    return RedirectResponse("/admin/equipo-miembro/list?msg=cache_ok", status_code=303)


@router.get("/cliente/{cliente_id}/reset-form", response_class=HTMLResponse)
async def reset_form(cliente_id: int, request: Request):
    """Pequeña página con un botón de confirmación para hacer el reset."""
    if not _check_auth(request):
        raise HTTPException(401)
    return HTMLResponse(f"""
<!doctype html><html><head><meta charset="utf-8"><title>Reset cliente {cliente_id}</title>
<style>
  body {{ font-family: Inter, system-ui, sans-serif; background:#f4f6f9; padding:40px; color:#111827; }}
  .card {{ background:#fff; max-width:520px; margin:60px auto; padding:32px; border-radius:14px; border:1px solid #e5e7eb; }}
  h2 {{ margin:0 0 12px 0; font-size:20px; }}
  p {{ color:#6b7280; line-height:1.6; }}
  .btn {{ display:inline-block; padding:10px 18px; border-radius:8px; font-weight:600; text-decoration:none; font-size:13px; border:none; cursor:pointer; }}
  .btn-danger {{ background:#dc2626; color:#fff; }}
  .btn-secondary {{ background:#fff; border:1.5px solid #e5e7eb; color:#374151; margin-right:8px; }}
</style></head>
<body>
<div class="card">
  <h2>¿Resetear conversación del cliente #{cliente_id}?</h2>
  <p>Esto borra <strong>todas las conversaciones</strong>, la sesión activa y cualquier pausa por humano.
  El cliente se mantiene (su número, nombre, dirección).</p>
  <p>El bot tratará al cliente como uno nuevo cuando vuelva a escribir.</p>
  <form method="POST" action="/admin/actions/cliente/{cliente_id}/reset">
    <a href="/admin/cliente/details/{cliente_id}" class="btn btn-secondary">Cancelar</a>
    <button type="submit" class="btn btn-danger">Sí, resetear conversación</button>
  </form>
</div>
</body></html>""")


@router.post("/cliente/{cliente_id}/nuke")
async def nuke_cliente(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Borrado TOTAL: cliente + pedidos + alertas + conversaciones + sesión.

    Sortea la FK pedidos.cliente_id_fkey (sin ON DELETE CASCADE) borrando
    explícitamente las tablas dependientes antes del cliente.
    """
    if not _check_auth(request):
        raise HTTPException(401)

    cliente = (await session.execute(
        Cliente.__table__.select().where(Cliente.id == cliente_id)
    )).first()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    numero = cliente.numero_whatsapp

    # Orden importa: alertas (SET NULL) + pedidos (sin cascade) → cliente
    await session.execute(delete(AlertaFabio).where(AlertaFabio.cliente_id == cliente_id))
    n_ped = await session.execute(delete(Pedido).where(Pedido.cliente_id == cliente_id))
    # conversaciones, sesión, intervencion_humana caen por CASCADE al borrar cliente
    await session.execute(delete(Cliente).where(Cliente.id == cliente_id))
    await session.commit()

    log.warning(
        "admin.cliente.nuke",
        cliente_id=cliente_id,
        numero=numero,
        pedidos_borrados=n_ped.rowcount,
    )
    return RedirectResponse("/admin/cliente/list?msg=nuke_ok", status_code=303)


@router.get("/cliente/{cliente_id}/nuke-form", response_class=HTMLResponse)
async def nuke_form(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Confirmación destructiva — muestra qué se va a borrar."""
    if not _check_auth(request):
        raise HTTPException(401)

    cliente = (await session.execute(
        Cliente.__table__.select().where(Cliente.id == cliente_id)
    )).first()
    if not cliente:
        raise HTTPException(404, "Cliente no encontrado")

    from sqlalchemy import func, select as sa_select
    n_conv = (await session.execute(
        sa_select(func.count()).select_from(Conversacion).where(Conversacion.cliente_id == cliente_id)
    )).scalar_one()
    n_alertas = (await session.execute(
        sa_select(func.count()).select_from(AlertaFabio).where(AlertaFabio.cliente_id == cliente_id)
    )).scalar_one()

    numero = cliente.numero_whatsapp
    nombre = cliente.nombre or "(sin nombre)"

    return HTMLResponse(f"""
<!doctype html><html><head><meta charset="utf-8"><title>Eliminar cliente {cliente_id}</title>
<style>
  body {{ font-family: Inter, system-ui, sans-serif; background:#f4f6f9; padding:40px; color:#111827; }}
  .card {{ background:#fff; max-width:560px; margin:60px auto; padding:32px; border-radius:14px; border:1px solid #e5e7eb; }}
  h2 {{ margin:0 0 12px 0; font-size:20px; color:#b91c1c; }}
  p {{ color:#6b7280; line-height:1.6; }}
  ul {{ background:#fef2f2; border:1px solid #fecaca; border-radius:8px; padding:14px 22px; color:#7f1d1d; }}
  li {{ margin:4px 0; }}
  .btn {{ display:inline-block; padding:10px 18px; border-radius:8px; font-weight:600; text-decoration:none; font-size:13px; border:none; cursor:pointer; }}
  .btn-danger {{ background:#dc2626; color:#fff; }}
  .btn-secondary {{ background:#fff; border:1.5px solid #e5e7eb; color:#374151; margin-right:8px; }}
  strong {{ color:#111827; }}
</style></head>
<body>
<div class="card">
  <h2>⚠ Eliminar cliente #{cliente_id} completamente</h2>
  <p>Cliente: <strong>{nombre}</strong> · <strong>{numero}</strong></p>
  <p>Esta acción borra <strong>todo</strong> el rastro de este cliente:</p>
  <ul>
    <li><strong>{n_conv}</strong> mensaje(s) de conversación</li>
    <li><strong>{n_alertas}</strong> alerta(s) / pendiente(s)</li>
    <li>Sesión activa + cualquier pausa por humano</li>
    <li>El registro del contacto</li>
  </ul>
  <p>Al primer mensaje nuevo, el bot lo tratará como cliente nuevo desde cero.
  <strong>Esta acción es irreversible.</strong></p>
  <form method="POST" action="/admin/actions/cliente/{cliente_id}/nuke">
    <a href="/admin/cliente/details/{cliente_id}" class="btn btn-secondary">Cancelar</a>
    <button type="submit" class="btn btn-danger">Sí, eliminar cliente completo</button>
  </form>
</div>
</body></html>""")
