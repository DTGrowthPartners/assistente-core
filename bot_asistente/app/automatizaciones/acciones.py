"""Catálogo de acciones predefinidas que las tareas programadas pueden ejecutar.

Cada acción es async, recibe (session, parametros) y devuelve dict con resultado.
Las acciones envían mensajes vía whapi a destinos (admin, grupo, cliente).

Para agregar una acción nueva: definir handler `async def accion_X(...)` y
registrarla en `ACCIONES_DISPONIBLES` con su schema de parámetros.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging_setup import log
from app.whapi.client import enviar_texto


# ─── HELPERS ───────────────────────────────────────────────────────────────


def _fmt_cop(n: int | float | Decimal | None) -> str:
    if n is None:
        return "$0"
    return f"${int(n):,}".replace(",", ".")


async def _enviar_a_destino(destino_tipo: str, destino_id: str, mensaje: str) -> dict:
    """destino_tipo: 'numero' (cliente o admin) o 'grupo' (chat_id @g.us)."""
    destino = destino_id
    if destino_tipo == "grupo" and not destino.endswith("@g.us"):
        destino = destino + "@g.us"
    try:
        await enviar_texto(destino, mensaje)
        return {"ok": True, "destino": destino, "chars": len(mensaje)}
    except Exception as e:
        log.warning("automatizacion.envio_fail", destino=destino, error=str(e))
        return {"ok": False, "destino": destino, "error": str(e)[:200]}


def _render_plantilla(plantilla: str, ctx: dict[str, Any]) -> str:
    """Reemplaza placeholders {{var}} en el texto con valores del ctx."""
    out = plantilla
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


# ─── ACCIÓN: REPORTE VENTAS ────────────────────────────────────────────────


async def accion_reporte_ventas(session: AsyncSession, params: dict) -> dict:
    """
    Resumen de ventas del periodo configurado.

    params:
      destino_tipo: 'numero' | 'grupo'   (default 'grupo')
      destino_id:   str                  (número E.164 con + o chat_id de grupo)
      periodo:      'hoy' | '24h' | 'semana'    (default 'hoy')
      top_n:        int                  (default 3)
    """
    destino_tipo = params.get("destino_tipo", "grupo")
    destino_id = params.get("destino_id")
    periodo = params.get("periodo", "hoy")
    top_n = int(params.get("top_n", 3))

    if not destino_id:
        return {"ok": False, "error": "falta destino_id"}

    if periodo == "hoy":
        where_ts = "created_at >= CURRENT_DATE"
        label = "HOY"
    elif periodo == "semana":
        where_ts = "created_at >= NOW() - INTERVAL '7 days'"
        label = "ÚLTIMOS 7 DÍAS"
    else:  # 24h
        where_ts = "created_at >= NOW() - INTERVAL '24 hours'"
        label = "ÚLTIMAS 24H"

    estados_venta = "'datos_completos','esperando_pago','comprobante_recibido','confirmado','despachado','entregado'"
    row = (await session.execute(sa_text(
        f"""SELECT COUNT(*), COALESCE(SUM(total), 0)
            FROM pedidos
            WHERE {where_ts} AND estado IN ({estados_venta})"""
    ))).first()
    n_pedidos, total = (row[0], row[1]) if row else (0, 0)

    # Top productos del periodo
    rows_items = (await session.execute(sa_text(
        f"""SELECT items FROM pedidos
            WHERE {where_ts} AND estado IN ({estados_venta}) AND items IS NOT NULL"""
    ))).fetchall()
    contador: dict[str, int] = {}
    for (items,) in rows_items:
        for it in (items or []):
            ref = (it.get("ref") or it.get("nombre") or "").strip()
            if ref:
                contador[ref] = contador.get(ref, 0) + int(it.get("cantidad", 1))
    top = sorted(contador.items(), key=lambda kv: -kv[1])[:top_n]
    top_str = ", ".join(f"{ref} ({n})" for ref, n in top) if top else "—"

    mensaje = (
        f"📊 REPORTE VENTAS — {label}\n"
        f"Pedidos: {n_pedidos}\n"
        f"Total: {_fmt_cop(total)}\n"
        f"Top {top_n} refs: {top_str}"
    )
    res_envio = await _enviar_a_destino(destino_tipo, destino_id, mensaje)
    return {
        "ok": res_envio.get("ok", False),
        "preview": mensaje[:200],
        "pedidos": n_pedidos,
        "total": float(total or 0),
        "destino": res_envio.get("destino"),
        "error": res_envio.get("error"),
    }


# ─── ACCIÓN: RECORDATORIO ALERTAS ──────────────────────────────────────────


async def accion_recordatorio_alertas(session: AsyncSession, params: dict) -> dict:
    """
    Si hay alertas abiertas hace más de N horas, envía recordatorio a un admin.

    params:
      destino_tipo: 'numero' | 'grupo'    (default 'numero')
      destino_id:   str                   (número del admin)
      horas_min:    int                   (default 2 — solo alerta >=2h)
      max_alertas:  int                   (default 5)
    """
    destino_tipo = params.get("destino_tipo", "numero")
    destino_id = params.get("destino_id")
    horas_min = int(params.get("horas_min", 2))
    max_alertas = int(params.get("max_alertas", 5))

    if not destino_id:
        return {"ok": False, "error": "falta destino_id"}

    rows = (await session.execute(sa_text(
        """SELECT a.id, a.tipo, LEFT(a.mensaje, 120), a.created_at,
                  c.numero_whatsapp, COALESCE(c.nombre, '-')
           FROM alertas_fabio a
           LEFT JOIN clientes c ON c.id = a.cliente_id
           WHERE a.resuelto = false
             AND a.created_at <= NOW() - (:h || ' hours')::interval
             AND a.tipo NOT IN ('mensaje_dueno')
           ORDER BY a.created_at ASC
           LIMIT :lim"""
    ), {"h": str(horas_min), "lim": max_alertas})).fetchall()

    if not rows:
        return {"ok": True, "skip": True, "razon": f"sin alertas abiertas >{horas_min}h"}

    lineas = []
    for r in rows:
        edad_h = int((datetime.now(timezone.utc) - r[3].replace(tzinfo=timezone.utc)).total_seconds() / 3600) if r[3] else 0
        lineas.append(f"#{r[0]} {r[1]} · {r[5]} {r[4]} ({edad_h}h)\n  {r[2]}")

    mensaje = f"🔔 ALERTAS PENDIENTES (>{horas_min}h)\n\n" + "\n\n".join(lineas)
    if len(rows) >= max_alertas:
        mensaje += f"\n\n…hay más alertas, revisa /admin/alerta-fabio/list"

    res = await _enviar_a_destino(destino_tipo, destino_id, mensaje)
    return {"ok": res.get("ok"), "alertas": len(rows), "preview": mensaje[:200], "error": res.get("error")}


# ─── ACCIÓN: REENGAGEMENT CLIENTES INACTIVOS ──────────────────────────────


async def accion_reengagement(session: AsyncSession, params: dict) -> dict:
    """
    Identifica clientes con último contacto >N días que NO compraron, y les
    envía un mensaje plantilla configurable.

    params:
      dias_inactividad: int              (default 14)
      plantilla:        str              (texto, placeholder {{nombre}})
      max_envios:       int              (default 10 — para no spamear)
      excluir_bloqueados: bool           (default true)
    """
    dias = int(params.get("dias_inactividad", 14))
    plantilla = params.get("plantilla") or (
        "Hola {{nombre}}, hace un tiempo que no nos escribes. "
        "Estamos con catálogo nuevo y promos interesantes. "
        "¿Te muestro algo en particular?"
    )
    max_envios = int(params.get("max_envios", 10))
    excluir_bloq = bool(params.get("excluir_bloqueados", True))

    where_bloq = "AND c.bloqueado = false" if excluir_bloq else ""
    rows = (await session.execute(sa_text(
        f"""SELECT c.id, c.numero_whatsapp, COALESCE(c.nombre, 'cliente')
            FROM clientes c
            WHERE c.ultimo_contacto IS NOT NULL
              AND c.ultimo_contacto <= NOW() - (:d || ' days')::interval
              AND c.ultimo_contacto >  NOW() - (:d2 || ' days')::interval
              {where_bloq}
              AND NOT EXISTS (SELECT 1 FROM pedidos p WHERE p.cliente_id = c.id)
              AND NOT EXISTS (SELECT 1 FROM numeros_internos ni WHERE ni.numero_whatsapp = c.numero_whatsapp AND ni.activo)
              AND NOT EXISTS (SELECT 1 FROM equipo_miembros em WHERE em.numero_whatsapp = c.numero_whatsapp AND em.activo)
            ORDER BY c.ultimo_contacto ASC
            LIMIT :lim"""
    ), {"d": str(dias), "d2": str(dias + 30), "lim": max_envios})).fetchall()

    if not rows:
        return {"ok": True, "skip": True, "razon": "sin clientes inactivos en el rango"}

    enviados = 0
    fallos = 0
    for cid, numero, nombre in rows:
        msg = _render_plantilla(plantilla, {"nombre": nombre.split()[0]})
        try:
            await enviar_texto(numero, msg)
            # Registrar como humano para que el bot vea que ya hubo contacto
            # y no se confunda si el cliente responde después.
            await session.execute(sa_text(
                """INSERT INTO conversaciones (cliente_id, direccion, tipo, contenido, metadata)
                   VALUES (:c, 'outbound', 'texto', :m, :meta::jsonb)"""
            ), {"c": cid, "m": msg, "meta": '{"via":"automatizacion","accion":"reengagement"}'})
            enviados += 1
        except Exception as e:
            fallos += 1
            log.warning("automatizacion.reengagement.fail", cliente_id=cid, error=str(e))

    return {
        "ok": True,
        "enviados": enviados,
        "fallos": fallos,
        "candidatos": len(rows),
    }


# ─── ACCIÓN: MENSAJE CUSTOM ────────────────────────────────────────────────


async def accion_mensaje_custom(session: AsyncSession, params: dict) -> dict:
    """
    Envía un mensaje arbitrario (texto fijo o plantilla con placeholders SQL).

    params:
      destino_tipo: 'numero' | 'grupo'
      destino_id:   str
      mensaje:      str   — texto a enviar, soporta placeholders {{fecha}}, {{hora}}
      query_sql:    str?  — SQL opcional. Si está, se ejecuta y su resultado se
                            inyecta como {{resultado}}. Cuidado: no escapar entrada
                            del usuario. Solo SELECTs.
    """
    destino_tipo = params.get("destino_tipo", "numero")
    destino_id = params.get("destino_id")
    plantilla = params.get("mensaje") or ""
    query_sql = (params.get("query_sql") or "").strip()

    if not destino_id or not plantilla:
        return {"ok": False, "error": "falta destino_id o mensaje"}

    ctx = {
        "fecha": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "hora": datetime.now(timezone.utc).strftime("%H:%M"),
    }

    if query_sql:
        if not query_sql.lower().lstrip().startswith("select"):
            return {"ok": False, "error": "query_sql debe ser SELECT"}
        try:
            res = (await session.execute(sa_text(query_sql))).fetchall()
            # Formato simple: si una sola fila, una columna → valor escalar
            if len(res) == 1 and len(res[0]) == 1:
                ctx["resultado"] = str(res[0][0])
            else:
                ctx["resultado"] = "\n".join(" | ".join(str(c) for c in r) for r in res[:20])
        except Exception as e:
            return {"ok": False, "error": f"query falló: {str(e)[:200]}"}

    mensaje = _render_plantilla(plantilla, ctx)
    res_envio = await _enviar_a_destino(destino_tipo, destino_id, mensaje)
    return {
        "ok": res_envio.get("ok"),
        "preview": mensaje[:200],
        "destino": res_envio.get("destino"),
        "error": res_envio.get("error"),
    }


# ─── REGISTRY ──────────────────────────────────────────────────────────────


ACCIONES_DISPONIBLES: dict[str, dict[str, Any]] = {
    "reporte_ventas": {
        "handler": accion_reporte_ventas,
        "descripcion": "Resumen de ventas (hoy/24h/semana) al grupo o admin",
        "parametros": {
            "destino_tipo": "grupo | numero",
            "destino_id": "chat_id del grupo o +57XXX del admin",
            "periodo": "hoy | 24h | semana",
            "top_n": "int (default 3)",
        },
    },
    "recordatorio_alertas": {
        "handler": accion_recordatorio_alertas,
        "descripcion": "Recordatorio de alertas pendientes >Nh al admin",
        "parametros": {
            "destino_tipo": "numero | grupo",
            "destino_id": "+57XXX o chat_id grupo",
            "horas_min": "int (default 2)",
            "max_alertas": "int (default 5)",
        },
    },
    "reengagement": {
        "handler": accion_reengagement,
        "descripcion": "Mensaje a clientes inactivos >N días (que no compraron)",
        "parametros": {
            "dias_inactividad": "int (default 14)",
            "plantilla": "texto con {{nombre}} (placeholder)",
            "max_envios": "int (default 10)",
            "excluir_bloqueados": "bool (default true)",
        },
    },
    "mensaje_custom": {
        "handler": accion_mensaje_custom,
        "descripcion": "Mensaje arbitrario, opcional con query SQL ({{resultado}})",
        "parametros": {
            "destino_tipo": "numero | grupo",
            "destino_id": "+57XXX o chat_id grupo",
            "mensaje": "texto con placeholders {{fecha}} {{hora}} {{resultado}}",
            "query_sql": "SELECT opcional (cuidado: no aceptar del usuario)",
        },
    },
}


async def ejecutar_accion(nombre: str, session: AsyncSession, params: dict) -> dict:
    """Ejecuta una acción por nombre. Maneja errores y devuelve dict resultado."""
    accion = ACCIONES_DISPONIBLES.get(nombre)
    if not accion:
        return {"ok": False, "error": f"acción desconocida: {nombre}"}
    try:
        return await accion["handler"](session, params or {})
    except Exception as e:
        log.exception("automatizacion.accion.fail", accion=nombre, error=str(e))
        return {"ok": False, "error": str(e)[:300]}
