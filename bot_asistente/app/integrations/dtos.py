"""Cliente de la API DT-OS (backend operativo de DTGP).

Base: settings.dtos_base_url  (https://os.dtgrowthpartners.com/api/webhook/bot)
Auth: header `x-api-key: settings.dtos_api_key`  (ROTAR la key vieja)

Se REUSA tal cual — el bot solo la consume. Endpoints documentados en el
workspace openclaw (TOOLS.md / MEMORY.md). Todas las funciones devuelven dicts
{"ok": bool, "data"/"error": ...} y nunca lanzan al caller.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

_TIMEOUT = 25


def _headers() -> dict[str, str]:
    return {"x-api-key": settings.dtos_api_key, "Content-Type": "application/json"}


def _configurado() -> bool:
    return bool(settings.dtos_api_key and settings.dtos_base_url)


async def _request(method: str, path: str, *, params: dict | None = None, json: dict | None = None) -> dict[str, Any]:
    if not _configurado():
        return {"ok": False, "error": "DT-OS no configurado (falta dtos_api_key)."}
    url = f"{settings.dtos_base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.request(method, url, headers=_headers(), params=params, json=json)
        if r.status_code >= 400:
            log.warning("dtos.http_error", method=method, path=path, status=r.status_code, body=r.text[:300])
            return {"ok": False, "error": f"DT-OS {method} {path} → {r.status_code}", "status": r.status_code}
        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}
        return {"ok": True, "data": data}
    except Exception as e:
        log.exception("dtos.request.fail", method=method, path=path, error=str(e))
        return {"ok": False, "error": str(e)[:200]}


# ── Lectura ──────────────────────────────────────────────────────────────────

async def finanzas(mes: str | None = None, tipo: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if mes:
        params["mes"] = mes
    if tipo:
        params["tipo"] = tipo  # receivable | payable
    return await _request("GET", "/finances", params=params or None)


async def transacciones() -> dict[str, Any]:
    return await _request("GET", "/sheets/transacciones")


async def terceros(buscar: str | None = None, tipo: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if buscar:
        params["buscar"] = buscar
    if tipo:
        params["tipo"] = tipo
    return await _request("GET", "/terceros", params=params or None)


async def clientes(search: str | None = None) -> dict[str, Any]:
    params = {"search": search} if search else None
    return await _request("GET", "/clients", params=params)


async def tareas(usuario: str | None = None, estado: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if usuario:
        params["usuario"] = usuario
    if estado:
        params["estado"] = estado
    return await _request("GET", "/tasks", params=params or None)


async def tareas_todas() -> dict[str, Any]:
    return await _request("GET", "/tasks/all")


async def crm(deals: bool = False, filtros: dict | None = None) -> dict[str, Any]:
    if deals:
        return await _request("GET", "/crm/deals", params=filtros or None)
    return await _request("GET", "/crm")


async def briefs(proyecto: str | None = None, search: str | None = None) -> dict[str, Any]:
    params: dict[str, str] = {}
    if proyecto:
        params["proyecto"] = proyecto
    if search:
        params["search"] = search
    return await _request("GET", "/briefs", params=params or None)


async def brief_markdown(brief_id: str) -> dict[str, Any]:
    return await _request("GET", f"/briefs/{brief_id}/markdown")


async def invoices(status: str | None = None, cliente: str | None = None, limit: int | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if status:
        params["status"] = status
    if cliente:
        params["cliente"] = cliente
    if limit:
        params["limit"] = limit
    return await _request("GET", "/invoices", params=params or None)


# ── Escritura ────────────────────────────────────────────────────────────────

async def registrar_gasto(payload: dict) -> dict[str, Any]:
    """payload: fecha, importe, descripcion, categoria, cuenta, entidad, terceroId?"""
    return await _request("POST", "/sheets/gastos", json=payload)


async def registrar_ingreso(payload: dict) -> dict[str, Any]:
    """payload: fecha, importe, descripcion, categoria, cuenta, entidad, terceroId?"""
    return await _request("POST", "/sheets/ingresos", json=payload)


async def crear_tarea(payload: dict) -> dict[str, Any]:
    """payload: titulo (req), asignado?, proyecto?, prioridad?, fechaFin?, descripcion?, creador?"""
    return await _request("POST", "/tasks", json=payload)


async def actualizar_tarea(tarea_id: str, payload: dict) -> dict[str, Any]:
    """payload: estado (TODO/IN_PROGRESS/DONE), prioridad?, titulo?"""
    return await _request("PATCH", f"/tasks/{tarea_id}", json=payload)


async def crear_tercero(payload: dict) -> dict[str, Any]:
    return await _request("POST", "/terceros", json=payload)


async def actualizar_tercero(tercero_id: str, payload: dict) -> dict[str, Any]:
    return await _request("PATCH", f"/terceros/{tercero_id}", json=payload)


async def crear_deal(payload: dict) -> dict[str, Any]:
    """payload: nombre, empresa, telefono, valorEstimado, etapa, prioridad"""
    return await _request("POST", "/crm/deals", json=payload)


async def actualizar_deal(deal_id: str, payload: dict) -> dict[str, Any]:
    return await _request("PATCH", f"/crm/deals/{deal_id}", json=payload)


async def crear_cuenta_cobro(payload: dict) -> dict[str, Any]:
    """payload: nombre_cliente, identificacion, fecha, concepto, servicios[], observaciones?

    La nota legal obligatoria (art. 383 E.T.) la inyecta el caller en
    `observaciones` (regla de negocio). Devuelve el id de la cuenta creada.
    """
    return await _request("POST", "/invoices/generate", json=payload)


async def descargar_pdf_cuenta_cobro(invoice_id: str) -> dict[str, Any]:
    """Descarga el PDF binario de una cuenta de cobro creada en DT-OS.

    Devuelve {"ok": True, "data": bytes, "filename": "..."} si tuvo éxito.
    """
    if not _configurado():
        return {"ok": False, "error": "DT-OS no configurado."}
    url = f"{settings.dtos_base_url.rstrip('/')}/invoices/{invoice_id}/download"
    try:
        async with httpx.AsyncClient(timeout=60) as c:
            r = await c.get(url, headers={"x-api-key": settings.dtos_api_key})
        if r.status_code >= 400:
            log.warning("dtos.descargar_pdf.http_error",
                        invoice_id=invoice_id, status=r.status_code, body=r.text[:300])
            return {"ok": False, "error": f"DT-OS download → {r.status_code}"}
        # Inferir filename del header Content-Disposition si viene
        cd = r.headers.get("content-disposition", "")
        filename = f"cuenta_cobro_{invoice_id}.pdf"
        if "filename=" in cd:
            try:
                filename = cd.split("filename=", 1)[1].strip().strip('"').strip("'") or filename
            except Exception:
                pass
        return {"ok": True, "data": r.content, "filename": filename,
                "mime": r.headers.get("content-type", "application/pdf")}
    except Exception as e:
        log.exception("dtos.descargar_pdf.fail", invoice_id=invoice_id, error=str(e))
        return {"ok": False, "error": str(e)[:200]}
