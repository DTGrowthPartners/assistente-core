"""
Acciones admin custom — operaciones que SQLAdmin no cubre con CRUD básico.

Endpoints:
    POST /admin/actions/cliente/{id}/reset
        Borra TODAS las conversaciones, sesión, pausa de un cliente,
        pero mantiene el registro del cliente. Útil para "empezar limpio"
        sin perder el número.

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
from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import (
    Cliente,
    Conversacion,
    IntervencionHumana,
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
