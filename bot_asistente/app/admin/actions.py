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

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, text as sa_text, update
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


@router.post("/cliente/{cliente_id}/reactivar-laura")
async def reactivar_laura(
    cliente_id: int,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Quita la pausa de intervención humana para este cliente.

    Útil cuando el operador estaba atendiendo manualmente y decide que
    Laura retome la conversación sin esperar a que expire la pausa de 1h.
    """
    if not _check_auth(request):
        raise HTTPException(401)
    await session.execute(sa_text(
        "DELETE FROM intervencion_humana WHERE cliente_id = :cid"
    ), {"cid": cliente_id})
    await session.commit()
    log.info("admin.cliente.reactivar_laura", cliente_id=cliente_id)
    return RedirectResponse(f"/admin/chats/{cliente_id}?msg=reactivado", status_code=303)


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
    log.warning("admin.bot.toggle", nuevo_estado="activo" if nuevo_estado else "pausado")
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
    n_pedidos = (await session.execute(
        sa_select(func.count()).select_from(Pedido).where(Pedido.cliente_id == cliente_id)
    )).scalar_one()
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
    <li><strong>{n_pedidos}</strong> pedido(s) en la tabla pedidos</li>
    <li><strong>{n_conv}</strong> mensaje(s) de conversación</li>
    <li><strong>{n_alertas}</strong> alerta(s) a Fabio</li>
    <li>Sesión activa + cualquier pausa por humano</li>
    <li>El registro del cliente</li>
  </ul>
  <p>Al primer mensaje nuevo, el bot lo tratará como cliente nuevo desde cero.
  <strong>Esta acción es irreversible.</strong></p>
  <form method="POST" action="/admin/actions/cliente/{cliente_id}/nuke">
    <a href="/admin/cliente/details/{cliente_id}" class="btn btn-secondary">Cancelar</a>
    <button type="submit" class="btn btn-danger">Sí, eliminar cliente completo</button>
  </form>
</div>
</body></html>""")
