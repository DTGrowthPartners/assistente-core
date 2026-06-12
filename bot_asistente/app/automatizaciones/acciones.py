"""Acciones de proactividad de Dairo (ex-heartbeat de openclaw), disparadas por
el scheduler (tareas_programadas, cron en BD).

Cada acción es async, recibe (session, params) y devuelve dict con resultado.
Para agregar una: definir `async def accion_X(...)` y registrarla en
ACCIONES_DISPONIBLES con su schema de parámetros.

Las acciones data-driven usan las APIs DT-OS / MetaSuite (se reusan). Las "soft"
(motivacional, reflexión, sugerencias) componen el texto con Claude.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text as sa_text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.integrations import dtos, metasuite
from app.logging_setup import log
from app.whapi.client import enviar_texto

settings = get_settings()


# ─── HELPERS ───────────────────────────────────────────────────────────────


async def _enviar_a_destino(destino_tipo: str, destino_id: str, mensaje: str) -> dict:
    """destino_tipo: 'numero' (contacto) o 'grupo' (chat_id @g.us)."""
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
    out = plantilla
    for k, v in ctx.items():
        out = out.replace("{{" + k + "}}", str(v))
    return out


async def _redactar(system: str, instruccion: str, max_tokens: int = 400) -> str:
    """Compone un mensaje breve con Claude (para motivacional / sugerencias / reflexión).

    Si Claude falla, devuelve "" y el caller decide un fallback.
    """
    try:
        from app.claude.anthropic_client import get_anthropic_client
        client = get_anthropic_client()
        resp = await client.messages.create(
            model=settings.claude_model_principal,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": instruccion}],
        )
        return "\n".join(b.text for b in resp.content if getattr(b, "type", None) == "text").strip()
    except Exception as e:
        log.warning("automatizacion.redactar_fail", error=str(e))
        return ""


def _resumen_finanzas(data: Any) -> str:
    """Extrae un resumen legible del payload de DT-OS /finances (defensivo)."""
    if not isinstance(data, dict):
        return "Sin datos financieros."
    partes: list[str] = []
    disp = data.get("disponible") or data.get("available")
    if isinstance(disp, dict):
        total = disp.get("total")
        if total is not None:
            partes.append(f"Disponible total: {total}")
        for banco in ("Bancolombia", "Nequi", "Daviplata"):
            if banco in disp:
                partes.append(f"  {banco}: {disp[banco]}")
    for k in ("ingresos", "gastos", "ingresosReales", "gastosReales"):
        if k in data:
            partes.append(f"{k}: {data[k]}")
    return "\n".join(partes) if partes else "Datos financieros recibidos (revisar detalle en DT-OS)."


# ═══════════════════════════════════════════════════════════════════════════
# ACCIONES — proactividad del equipo
# ═══════════════════════════════════════════════════════════════════════════


async def accion_reporte_ceo(session: AsyncSession, params: dict) -> dict:
    """Reporte CEO diario a Dairo (8 AM): finanzas + estado de tareas + sugerencias.

    params: destino_id (default settings.dairo_phone), mes?
    """
    destino_id = params.get("destino_id") or settings.dairo_phone
    fin = await dtos.finanzas(mes=params.get("mes"))
    tareas = await dtos.tareas_todas()

    bloque_fin = _resumen_finanzas(fin.get("data")) if fin.get("ok") else "No pude leer finanzas (DT-OS)."
    bloque_tareas = ""
    if tareas.get("ok") and isinstance(tareas.get("data"), dict):
        resumen = tareas["data"].get("resumen") or tareas["data"].get("summary")
        bloque_tareas = str(resumen)[:600] if resumen else "Tareas consultadas (ver detalle en DT-OS)."
    else:
        bloque_tareas = "No pude leer tareas (DT-OS)."

    sugerencias = await _redactar(
        system=(
            "Eres Dairo, operativa de DTGP. Con base en el estado financiero y de tareas que te paso, "
            "da EXACTAMENTE 3 sugerencias accionables y breves para el CEO (Dairo), en español, sin relleno."
        ),
        instruccion=f"FINANZAS:\n{bloque_fin}\n\nTAREAS:\n{bloque_tareas}",
        max_tokens=300,
    ) or "1. Revisar disponible.\n2. Priorizar tareas atrasadas.\n3. Seguimiento a clientes en riesgo."

    from zoneinfo import ZoneInfo as _ZI
    hoy = datetime.now(_ZI(settings.tz)).strftime("%Y-%m-%d")
    mensaje = (
        f"Reporte CEO — {hoy}\n\n"
        f"FINANZAS\n{bloque_fin}\n\n"
        f"EQUIPO / TAREAS\n{bloque_tareas}\n\n"
        f"SUGERENCIAS\n{sugerencias}"
    )
    res = await _enviar_a_destino("numero", destino_id, mensaje)
    return {"ok": res.get("ok"), "preview": mensaje[:200], "destino": res.get("destino"), "error": res.get("error")}


async def accion_motivacional_equipo(session: AsyncSession, params: dict) -> dict:
    """Mensaje motivacional al equipo (9 AM). params: destinos (lista de números) o destino_id."""
    destinos = params.get("destinos") or [d for d in [params.get("destino_id"), settings.dairo_phone] if d]
    texto = await _redactar(
        system="Eres Dairo, parte del equipo de DTGP. Escribe un mensaje motivacional corto (2-3 frases), cálido y genuino, para arrancar el día. Sin clichés vacíos.",
        instruccion="Genera el mensaje motivacional de hoy para el equipo.",
        max_tokens=180,
    ) or "Arrancamos un nuevo día. Enfoquémonos en lo que mueve la aguja. Vamos con todo."
    enviados, fallos = 0, 0
    for d in destinos:
        r = await _enviar_a_destino("numero", d, texto)
        enviados += 1 if r.get("ok") else 0
        fallos += 0 if r.get("ok") else 1
    return {"ok": fallos == 0, "enviados": enviados, "fallos": fallos, "preview": texto[:160]}


async def accion_seguimiento_tareas(session: AsyncSession, params: dict) -> dict:
    """Seguimiento de tareas atrasadas a miembros específicos (9:10 AM).

    params: usuarios (lista de nombres DT-OS) y un mapa usuario→numero en `telefonos`.
    """
    usuarios = params.get("usuarios") or ["Edgardo", "Jhonathan"]
    telefonos = params.get("telefonos") or {
        "Edgardo": settings.edgardo_phone,
        "Jhonathan": settings.jhonathan_phone,
    }
    enviados = 0
    detalle = []
    for usuario in usuarios:
        r = await dtos.tareas(usuario=usuario, estado="TODO")
        numero = telefonos.get(usuario)
        if not numero:
            continue
        data = r.get("data") if r.get("ok") else None
        n = len(data) if isinstance(data, list) else (data.get("total") if isinstance(data, dict) else "?")
        msg = f"Buenos días {usuario}. Tienes tareas pendientes (TODO): {n}. ¿Vamos avanzando alguna hoy?"
        env = await _enviar_a_destino("numero", numero, msg)
        enviados += 1 if env.get("ok") else 0
        detalle.append({"usuario": usuario, "pendientes": n, "enviado": env.get("ok")})
    return {"ok": True, "enviados": enviados, "detalle": detalle}


async def accion_reflexion_semanal(session: AsyncSession, params: dict) -> dict:
    """Reflexión semanal (Vie 4 PM): finanzas de la semana + foco para la próxima."""
    destino_id = params.get("destino_id") or settings.dairo_phone
    fin = await dtos.finanzas()
    bloque = _resumen_finanzas(fin.get("data")) if fin.get("ok") else "Sin datos financieros."
    texto = await _redactar(
        system="Eres Dairo, operativa de DTGP. Escribe una reflexión semanal breve para el CEO: 2 logros, 1 alerta, y el foco para la próxima semana. Español, directa, sin relleno.",
        instruccion=f"Datos financieros de referencia:\n{bloque}",
        max_tokens=350,
    ) or f"Reflexión semanal.\n{bloque}\nFoco: priorizar cobros y clientes en riesgo."
    res = await _enviar_a_destino("numero", destino_id, "Reflexión semanal\n\n" + texto)
    return {"ok": res.get("ok"), "preview": texto[:200], "error": res.get("error")}


async def accion_alerta_financiera(session: AsyncSession, params: dict) -> dict:
    """Cada 4h: alerta si el disponible está por debajo de un piso o por encima de un techo.

    params: destino_id, piso (default 5_000_000), techo (default 10_000_000).
    """
    destino_id = params.get("destino_id") or settings.dairo_phone
    piso = float(params.get("piso", 5_000_000))
    techo = float(params.get("techo", 10_000_000))
    fin = await dtos.finanzas()
    if not fin.get("ok"):
        return {"ok": False, "error": "no pude leer finanzas", "skip": True}
    data = fin.get("data") or {}
    disp = data.get("disponible") or {}
    total = None
    if isinstance(disp, dict):
        total = disp.get("total")
    try:
        total_num = float(str(total).replace("$", "").replace(".", "").replace(",", "")) if total is not None else None
    except Exception:
        total_num = None
    if total_num is None:
        return {"ok": True, "skip": True, "razon": "sin total disponible parseable"}
    if total_num < piso:
        msg = f"⚠️ Alerta financiera: disponible bajo (${total_num:,.0f}). Revisar flujo de caja."
    elif total_num >= techo:
        msg = f"Disponible alto (${total_num:,.0f}). Buen momento para inversiones/pagos pendientes."
    else:
        return {"ok": True, "skip": True, "razon": f"disponible en rango ({total_num:,.0f})"}
    res = await _enviar_a_destino("numero", destino_id, msg + "\n(Saldos de Sheets; validar antes de mover plata.)")
    return {"ok": res.get("ok"), "disponible": total_num, "error": res.get("error")}


async def accion_clientes_en_riesgo(session: AsyncSession, params: dict) -> dict:
    """Lun/Jue 11 AM: clientes sin actividad o con cobros vencidos → aviso al equipo.

    Usa DT-OS /finances?tipo=receivable (cuentas por cobrar) como señal.
    """
    destino_id = params.get("destino_id") or settings.dairo_phone
    rec = await dtos.finanzas(tipo="receivable")
    if not rec.get("ok"):
        return {"ok": False, "error": "no pude leer cuentas por cobrar", "skip": True}
    data = rec.get("data")
    resumen = str(data)[:600] if data else "Sin cuentas por cobrar."
    msg = f"Clientes en riesgo / cobros pendientes:\n{resumen}"
    res = await _enviar_a_destino("numero", destino_id, msg)
    return {"ok": res.get("ok"), "error": res.get("error")}


async def accion_reporte_meta_cliente(session: AsyncSession, params: dict) -> dict:
    """Reporte de Meta Ads a un cliente (7 AM). params: account_id, destino_id, date_preset, nombre_cliente."""
    account_id = params.get("account_id")
    destino_id = params.get("destino_id")
    if not account_id or not destino_id:
        return {"ok": False, "error": "faltan account_id o destino_id"}
    preset = params.get("date_preset", "yesterday")
    res = await metasuite.campañas(account_id, date_preset=preset)
    if not res.get("ok"):
        return {"ok": False, "error": res.get("error")}
    nombre = params.get("nombre_cliente", "")
    texto = await _redactar(
        system=(
            "Eres Dairo, de DTGP. Resume estas métricas de Meta Ads en un mensaje CORTO y claro para el "
            "cliente (no técnico): inversión, resultados (mensajes/leads), y una nota positiva. Español, sin jerga."
        ),
        instruccion=f"Cliente: {nombre}\nMétricas ({preset}):\n{str(res.get('data'))[:1500]}",
        max_tokens=350,
    )
    if not texto:
        return {"ok": False, "error": "no se pudo redactar el reporte"}
    env = await _enviar_a_destino("numero", destino_id, texto)
    return {"ok": env.get("ok"), "account_id": account_id, "preview": texto[:200], "error": env.get("error")}


async def accion_recordatorio_pendientes(session: AsyncSession, params: dict) -> dict:
    """Recordatorio de alertas/pendientes abiertas >Nh al equipo. (genérica, reusada)"""
    destino_tipo = params.get("destino_tipo", "numero")
    destino_id = params.get("destino_id") or settings.dairo_phone
    horas_min = int(params.get("horas_min", 2))
    max_alertas = int(params.get("max_alertas", 5))
    rows = (await session.execute(sa_text(
        """SELECT a.id, a.tipo, LEFT(a.mensaje, 120), a.created_at,
                  c.numero_whatsapp, COALESCE(c.nombre, '-')
           FROM alertas_fabio a LEFT JOIN clientes c ON c.id = a.cliente_id
           WHERE a.resuelto = false
             AND a.created_at <= NOW() - (:h || ' hours')::interval
           ORDER BY a.created_at ASC LIMIT :lim"""
    ), {"h": str(horas_min), "lim": max_alertas})).fetchall()
    if not rows:
        return {"ok": True, "skip": True, "razon": f"sin pendientes >{horas_min}h"}
    lineas = []
    for r in rows:
        edad = int((datetime.now(timezone.utc) - r[3].replace(tzinfo=timezone.utc)).total_seconds() / 3600) if r[3] else 0
        lineas.append(f"#{r[0]} {r[1]} · {r[5]} {r[4]} ({edad}h)\n  {r[2]}")
    mensaje = f"Pendientes abiertos (>{horas_min}h)\n\n" + "\n\n".join(lineas)
    res = await _enviar_a_destino(destino_tipo, destino_id, mensaje)
    return {"ok": res.get("ok"), "pendientes": len(rows), "error": res.get("error")}


async def accion_mensaje_custom(session: AsyncSession, params: dict) -> dict:
    """Mensaje arbitrario (texto fijo o con query SQL). params: destino_tipo, destino_id, mensaje, query_sql?"""
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
            r = (await session.execute(sa_text(query_sql))).fetchall()
            ctx["resultado"] = (str(r[0][0]) if len(r) == 1 and len(r[0]) == 1
                                else "\n".join(" | ".join(str(c) for c in row) for row in r[:20]))
        except Exception as e:
            return {"ok": False, "error": f"query falló: {str(e)[:200]}"}
    mensaje = _render_plantilla(plantilla, ctx)
    res = await _enviar_a_destino(destino_tipo, destino_id, mensaje)
    return {"ok": res.get("ok"), "preview": mensaje[:200], "error": res.get("error")}


# ═══════════════════════════════════════════════════════════════════════════
# MONITOREOS (dependen de fuentes externas — pendientes de endpoint/credenciales)
# ═══════════════════════════════════════════════════════════════════════════


async def accion_monitor_email(session: AsyncSession, params: dict) -> dict:
    """Monitor de correo (ej. Equilibrio cada 5 min Lun-Sáb 1-3 PM).

    PENDIENTE: requiere credenciales IMAP en .env (host/usuario/password) y la
    lógica de parsing del workspace-maria/equilibrio/email_monitor.py. Stub
    estructurado: no falla, solo reporta que falta configuración.
    """
    if not params.get("imap_host"):
        return {"ok": True, "skip": True, "razon": "monitor_email pendiente: faltan credenciales IMAP en params/.env"}
    # TODO: portar workspace-maria/equilibrio/email_monitor.py (IMAP poll + notify).
    return {"ok": True, "skip": True, "razon": "monitor_email: lógica IMAP pendiente de portar"}


async def accion_monitor_bancolombia(session: AsyncSession, params: dict) -> dict:
    """Monitor de movimientos Bancolombia (cada 3 min).

    PENDIENTE: openclaw lo hacía con una fuente externa (parsing de notificaciones).
    No hay endpoint documentado en DT-OS para esto. Stub: requiere definir la
    fuente (endpoint o email de notificaciones del banco) y credenciales.
    """
    return {"ok": True, "skip": True, "razon": "monitor_bancolombia pendiente: definir fuente/endpoint + credenciales"}


async def accion_sync_chats_whapi(session: AsyncSession, params: dict) -> dict:
    """Importa de whapi los mensajes recientes de chats individuales que NO
    están en BD. Cubre los casos en que el webhook falló (restart, timeout)
    o whapi nunca nos entregó el evento.

    params:
      max_chats: int (default 30) — top N chats más recientes a chequear.
      max_msgs_por_chat: int (default 15) — cuántos mensajes traer por chat.
      horas_max: int (default 24) — solo importa mensajes más nuevos que esto.
    """
    import httpx
    from datetime import datetime, timedelta, timezone
    from app.config import get_settings as _gs
    from app.whapi.client import _headers
    from app.whapi.parser import parsear_mensaje, normalizar_numero
    from app.db.repos import get_or_create_cliente, guardar_conversacion
    settings_local = _gs()

    max_chats = int(params.get("max_chats") or 30)
    max_msgs = int(params.get("max_msgs_por_chat") or 15)
    horas_max = int(params.get("horas_max") or 24)
    desde_ts = int((datetime.now(timezone.utc) - timedelta(hours=horas_max)).timestamp())

    base = settings_local.whapi_base_url.rstrip("/")

    # 1) lista de chats recientes
    try:
        async with httpx.AsyncClient(timeout=20) as c:
            r = await c.get(f"{base}/chats?count={max_chats}", headers=_headers())
        if r.status_code >= 400:
            return {"ok": False, "error": f"chats list HTTP {r.status_code}"}
        chats = r.json().get("chats", [])
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}

    importados = 0
    revisados = 0
    saltados_no_individual = 0
    for ch in chats:
        cid = ch.get("id") or ""
        if not cid.endswith("@s.whatsapp.net"):
            saltados_no_individual += 1
            continue
        revisados += 1

        # 2) últimos mensajes del chat
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                r = await c.get(
                    f"{base}/messages/list/{cid}?count={max_msgs}",
                    headers=_headers(),
                )
            if r.status_code >= 400:
                continue
            msgs = r.json().get("messages", [])
        except Exception:
            continue

        for raw in msgs:
            ts = raw.get("timestamp") or 0
            if ts < desde_ts:
                continue
            msg_id = raw.get("id")
            if not msg_id:
                continue
            # Skip outbounds del bot API (los enviamos nosotros, ya están en BD
            # aunque sin whapi_message_id) — re-importarlos duplica el chat.
            if raw.get("from_me") and (raw.get("source") or "").lower() == "api":
                continue
            # ¿Ya está en BD por whapi_message_id?
            existe = (await session.execute(sa_text(
                "SELECT 1 FROM conversaciones WHERE whapi_message_id = :id LIMIT 1"
            ), {"id": msg_id})).first()
            if existe:
                continue

            # Parsear con el parser estándar
            mensaje = parsear_mensaje(raw)
            if not mensaje or not (mensaje.texto or mensaje.media_url or mensaje.tipo == "audio"):
                continue
            # Solo nos interesan mensajes con contenido real
            if mensaje.tipo == "desconocido" and not (mensaje.texto or "").strip() and not mensaje.media_url:
                continue

            # Resolver / crear cliente
            try:
                cliente = await get_or_create_cliente(
                    session, mensaje.from_number, nombre=mensaje.from_name,
                )
                direccion = "humano" if mensaje.is_from_human else (
                    "outbound" if mensaje.is_from_bot else "inbound"
                )
                # Anti-duplicado por contenido+ts: si ya hay una conv del mismo
                # cliente en el mismo minuto con el mismo contenido inicial,
                # asumir que es duplicado (caso outbound bot sin whapi_id).
                if mensaje.texto:
                    dup = (await session.execute(sa_text("""
                        SELECT 1 FROM conversaciones
                        WHERE cliente_id = :cid AND direccion = :dir
                          AND ABS(EXTRACT(EPOCH FROM (timestamp - to_timestamp(:t)))) < 90
                          AND LEFT(COALESCE(contenido,''), 60) = LEFT(:c, 60)
                        LIMIT 1
                    """), {"cid": cliente.id, "dir": direccion, "t": ts, "c": mensaje.texto})).first()
                    if dup:
                        continue
                await guardar_conversacion(
                    session, cliente_id=cliente.id, direccion=direccion,
                    tipo=mensaje.tipo, contenido=mensaje.texto,
                    whapi_message_id=mensaje.id, media_url=mensaje.media_url,
                    metadata={"importado_via_sync": True, "raw_ts": ts},
                )
                # Forzar timestamp original
                await session.execute(sa_text(
                    "UPDATE conversaciones SET timestamp = to_timestamp(:t) WHERE whapi_message_id = :id"
                ), {"t": ts, "id": msg_id})
                await session.commit()
                importados += 1
            except Exception as e:
                log.warning("sync_chats.import_fail", msg_id=msg_id, error=str(e)[:100])
                await session.rollback()
                continue

    log.warning(
        "acciones.sync_chats_done",
        chats_revisados=revisados, importados=importados,
        saltados_no_individual=saltados_no_individual,
    )
    return {
        "ok": True, "chats_revisados": revisados,
        "mensajes_importados": importados,
        "saltados_no_individual": saltados_no_individual,
    }


async def accion_enviar_pendientes_apertura(session: AsyncSession, params: dict) -> dict:
    """Envía los mensajes que el bot generó FUERA DE HORARIO y dejó en BD
    con `metadata.no_enviado=true, razon=fuera_de_horario`.

    Pensado para correr a las 8 am L-V — recoge todo lo que llegó en la
    madrugada y lo dispara con delay humanizado entre cada envío.

    params:
      max_envios: int (default 50) — cap por seguridad.
      delay_min_s / delay_max_s: int (default 8/22) — entre envíos.
    """
    import asyncio
    import random
    from app.whapi.client import enviar_texto
    from sqlalchemy import text as sa_text

    max_envios = int(params.get("max_envios") or 50)
    dmin = float(params.get("delay_min_s") or 8.0)
    dmax = float(params.get("delay_max_s") or 22.0)

    rows = (await session.execute(sa_text("""
        SELECT c.id, c.contenido, cl.numero_whatsapp, cl.nombre
        FROM conversaciones c
        JOIN clientes cl ON cl.id = c.cliente_id
        WHERE c.direccion = 'outbound'
          AND c.metadata->>'no_enviado' = 'true'
          AND c.metadata->>'razon' IN ('fuera_de_horario', 'rate_limit')
          AND c.contenido IS NOT NULL
          AND trim(c.contenido) != ''
        ORDER BY c.timestamp ASC
        LIMIT :lim
    """), {"lim": max_envios})).fetchall()

    if not rows:
        log.info("acciones.pendientes_apertura.vacio")
        return {"ok": True, "enviados": 0, "razon": "sin pendientes"}

    enviados = 0
    fallos: list[str] = []
    for r in rows:
        conv_id, texto, numero, nombre = r[0], r[1], r[2], r[3]
        try:
            await enviar_texto(numero, texto)
            enviados += 1
            # Marcar como enviado en BD
            await session.execute(sa_text("""
                UPDATE conversaciones
                SET metadata = (metadata - 'no_enviado' - 'razon' - 'programado_para')
                               || jsonb_build_object('enviado_retroactivo', true, 'enviado_en', now()::text)
                WHERE id = :id
            """), {"id": conv_id})
            await session.commit()
            log.info("acciones.pendiente_enviado", conv_id=conv_id, numero=numero, chars=len(texto or ""))
        except Exception as e:
            fallos.append(f"{conv_id}: {str(e)[:80]}")
            log.warning("acciones.pendiente_fail", conv_id=conv_id, error=str(e))
        # delay entre envíos para no spammear
        await asyncio.sleep(random.uniform(dmin, dmax))

    log.warning(
        "acciones.pendientes_apertura.done",
        total=len(rows), enviados=enviados, fallos=len(fallos),
    )
    return {"ok": True, "total": len(rows), "enviados": enviados, "fallos": fallos[:10]}


async def accion_seguimiento_prospectos(session: AsyncSession, params: dict) -> dict:
    """Seguimiento automático a prospectos que no han agendado.

    Lógica:
      1. Encuentra prospectos (etiqueta='prospecto') sin cita activa, sin pausa,
         sin tag 'Cerrado / ganado' ni 'Perdido' ni 'No fit', cuya última inbound
         está entre `dias_min` y `dias_max` días atrás, y a quienes el bot AÚN
         no les envió seguimiento en este intento (no tienen tag temporal).
      2. Para cada uno, llama a Claude con identidad Dairo + servicios +
         **historial real** + instrucción de retomar la conversación con
         naturalidad. El bot decide qué decir (no plantilla).
      3. Envía vía whapi con delay humanizado entre cada uno.
      4. Aplica tag 'Seguimiento enviado' para no repetir hoy.

    Si `feature_seguimiento_auto=False` en settings → no-op, solo loguea.

    params:
      dias_min:    int (default 1)  — cliente sin contestar al menos N días.
      dias_max:    int (default 7)  — no más de N días (leads viejos no).
      max_envios:  int (default 30) — cap por corrida para no quemar el número.
      delay_min_s: int (default 35) — segundos entre envíos.
      delay_max_s: int (default 90) — segundos entre envíos.
      dry_run:     bool (default False) — si True, no envía, solo lista candidatos.
    """
    import asyncio
    import random
    from sqlalchemy import text as _sa

    if not settings.feature_seguimiento_auto:
        log.info("acciones.seguimiento_prospectos.feature_off")
        return {"ok": True, "skipped": True, "razon": "feature_seguimiento_auto=false"}

    dias_min = int(params.get("dias_min") or 1)
    dias_max = int(params.get("dias_max") or 7)
    max_envios = int(params.get("max_envios") or 30)
    dmin = float(params.get("delay_min_s") or 35.0)
    dmax = float(params.get("delay_max_s") or 90.0)
    dry_run = bool(params.get("dry_run"))
    # Si True, NO descarta a quienes tienen el tag 'Seguimiento enviado' de los
    # últimos 24h. Útil para ejecuciones puntuales que limpian backlog.
    ignorar_cooldown = bool(params.get("ignorar_cooldown"))

    # Tags que NO queremos volver a contactar (lead muerto/cerrado/desencaje).
    TAGS_EXCLUSION = ("Cerrado / ganado", "Perdido", "No fit")
    # Tag temporal que marcamos cuando enviamos seguimiento (anti-doble-envío).
    TAG_HECHO = "Seguimiento enviado"

    candidatos = (await session.execute(_sa("""
        SELECT
          c.id, c.numero_whatsapp, c.nombre, c.etiqueta,
          (SELECT MAX(timestamp) FROM conversaciones
             WHERE cliente_id = c.id AND direccion = 'inbound') AS ult_inbound,
          (SELECT MAX(timestamp) FROM conversaciones
             WHERE cliente_id = c.id) AS ult_msg
        FROM clientes c
        WHERE c.etiqueta = 'prospecto'
          AND c.bloqueado = FALSE
          AND NOT EXISTS (
              SELECT 1 FROM citas
              WHERE cliente_id = c.id
                AND estado IN ('agendada','reprogramada','completada')
          )
          AND NOT EXISTS (
              SELECT 1 FROM intervencion_humana
              WHERE cliente_id = c.id AND pausado_hasta > now()
          )
          AND NOT EXISTS (
              SELECT 1 FROM cliente_tags ct
              JOIN tags t ON t.id = ct.tag_id
              WHERE ct.cliente_id = c.id AND t.nombre = ANY(:excl)
          )
          AND (
              SELECT MAX(timestamp) FROM conversaciones
              WHERE cliente_id = c.id
          ) BETWEEN now() - make_interval(days => :dmax)
                AND now() - make_interval(days => :dmin)
        ORDER BY (
            SELECT MAX(timestamp) FROM conversaciones WHERE cliente_id = c.id
        ) ASC
        LIMIT :lim
    """), {
        "excl": list(TAGS_EXCLUSION),
        "dmin": dias_min,
        "dmax": dias_max,
        "lim": max_envios * 3,    # margen: algunos van a filtrarse por tag_hecho
    })).fetchall()

    if not candidatos:
        log.info("acciones.seguimiento_prospectos.sin_candidatos",
                 dias_min=dias_min, dias_max=dias_max)
        return {"ok": True, "candidatos": 0, "enviados": 0}

    # Filtrar los que ya tienen tag de "seguimiento enviado" reciente (24h).
    # Si `ignorar_cooldown=true`, salteamos el filtro (útil para limpiar backlog).
    finales = []
    for c in candidatos:
        if not ignorar_cooldown:
            ya = (await session.execute(_sa("""
                SELECT 1 FROM cliente_tags ct
                JOIN tags t ON t.id = ct.tag_id
                WHERE ct.cliente_id = :cid AND t.nombre = :tag
                  AND ct.added_at > now() - interval '24 hours'
            """), {"cid": c.id, "tag": TAG_HECHO})).first()
            if ya:
                continue
        finales.append(c)
        if len(finales) >= max_envios:
            break

    log.warning("acciones.seguimiento_prospectos.candidatos",
                total=len(candidatos), a_enviar=len(finales), dry_run=dry_run)

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "candidatos": [
                {"id": c.id, "numero": c.numero_whatsapp, "nombre": c.nombre,
                 "ult_msg": str(c.ult_msg)[:19] if c.ult_msg else None}
                for c in finales
            ],
        }

    # Asegurar que el tag 'Seguimiento enviado' existe en BD
    await session.execute(_sa("""
        INSERT INTO tags (nombre, color, descripcion, orden, created_by)
        VALUES (:n, '#A78BFA', 'Marcado por accion_seguimiento_prospectos. Evita doble envío en 24h.', 200, 'sistema')
        ON CONFLICT (nombre) DO NOTHING
    """), {"n": TAG_HECHO})
    await session.commit()

    enviados = 0
    fallos: list[dict] = []
    from app.identidades import principal as _identidad_principal
    ident = _identidad_principal()

    for c in finales:
        try:
            texto = await _generar_mensaje_seguimiento(
                session=session, cliente_id=c.id, cliente_numero=c.numero_whatsapp,
                cliente_nombre=c.nombre, identidad=ident,
            )
            if not texto:
                fallos.append({"cliente_id": c.id, "razon": "claude_vacio"})
                continue
            await enviar_texto(c.numero_whatsapp, texto)
            # Persistir como outbound con metadata específica
            await session.execute(_sa("""
                INSERT INTO conversaciones
                  (cliente_id, direccion, tipo, contenido, intent, modelo, metadata, timestamp)
                VALUES
                  (:cid, 'outbound', 'texto', :txt, 'seguimiento_auto',
                   :modelo, CAST(:meta AS jsonb), now())
            """), {
                "cid": c.id,
                "txt": texto,
                "modelo": settings.claude_model_principal,
                "meta": '{"via":"seguimiento_auto"}',
            })
            # Aplicar tag para no repetir
            await session.execute(_sa("""
                INSERT INTO cliente_tags (cliente_id, tag_id, added_by)
                SELECT :cid, t.id, 'sistema:seguimiento'
                  FROM tags t WHERE t.nombre = :tag
                ON CONFLICT DO NOTHING
            """), {"cid": c.id, "tag": TAG_HECHO})
            await session.commit()
            enviados += 1
            log.info("acciones.seguimiento.enviado",
                     cliente_id=c.id, numero=c.numero_whatsapp, chars=len(texto))
        except Exception as e:
            fallos.append({"cliente_id": c.id, "error": str(e)[:120]})
            log.warning("acciones.seguimiento.fail", cliente_id=c.id, error=str(e))
        # Delay humanizado entre envíos
        await asyncio.sleep(random.uniform(dmin, dmax))

    log.warning("acciones.seguimiento_prospectos.done",
                candidatos=len(finales), enviados=enviados, fallos=len(fallos))
    return {
        "ok": True,
        "candidatos": len(finales),
        "enviados": enviados,
        "fallos": fallos[:10],
    }


# ════════════════════════════════════════════════════════════════════════════
# SEGUIMIENTO POST-CITA — el bot escribe 4-6h después de la reunión a
# prospectos cuya cita quedó marcada como "completada" en /admin.
# Mensaje cálido: ¿quedó alguna duda?, ¿avanzamos con la propuesta?
# ════════════════════════════════════════════════════════════════════════════


async def accion_seguimiento_post_cita(session: AsyncSession, params: dict) -> dict:
    """Seguimiento post-reunión.

    Dispara para citas en una de estas dos situaciones:
      A) `estado='completada'` (marcada manual desde el admin) y fecha entre
         `horas_min` y `horas_max` atrás.
      B) `estado IN ('agendada','reprogramada')` cuya fecha ya pasó hace al
         menos `horas_min_asumida` (default 24h) — asumimos que ocurrió.
         Caso típico: nadie marcó como completada pero la reunión fue.

    En AMBOS casos, además se verifica que el cliente NO haya escrito DESPUÉS
    de la cita (sino ya está retomada la conversación, no hay nada que hacer).

    params:
      horas_min:           int (def 4)  — mínimo desde la reunión (caso A).
      horas_max:           int (def 8)  — máximo desde la reunión (caso A).
      horas_min_asumida:   int (def 24) — caso B: fecha pasó hace al menos.
      horas_max_asumida:   int (def 168) — caso B: no mirar más allá de 7d.
      max_envios:          int (def 20)
      delay_min_s:         int (def 35)
      delay_max_s:         int (def 90)
      dry_run:             bool
    """
    import asyncio
    import random
    from sqlalchemy import text as _sa

    if not settings.feature_seguimiento_auto:
        log.info("acciones.seguimiento_post_cita.feature_off")
        return {"ok": True, "skipped": True, "razon": "feature_seguimiento_auto=false"}

    horas_min = int(params.get("horas_min") or 4)
    horas_max = int(params.get("horas_max") or 8)
    horas_min_asumida = int(params.get("horas_min_asumida") or 24)
    horas_max_asumida = int(params.get("horas_max_asumida") or 168)
    max_envios = int(params.get("max_envios") or 20)
    dmin = float(params.get("delay_min_s") or 35.0)
    dmax = float(params.get("delay_max_s") or 90.0)
    dry_run = bool(params.get("dry_run"))

    TAGS_EXCLUSION = ("Cerrado / ganado", "Perdido", "No fit", "Post-cita enviado")
    TAG_HECHO = "Post-cita enviado"

    candidatos = (await session.execute(_sa("""
        SELECT
          c.id          AS cliente_id,
          c.numero_whatsapp,
          c.nombre,
          ci.id         AS cita_id,
          ci.fecha_inicio,
          ci.estado     AS cita_estado,
          ci.negocio
        FROM citas ci
        JOIN clientes c ON c.id = ci.cliente_id
        WHERE c.bloqueado = FALSE
          AND ci.estado IN ('completada', 'agendada', 'reprogramada')
          AND (
                -- A) marcada como completada en ventana corta
                (ci.estado = 'completada'
                 AND ci.fecha_inicio BETWEEN now() - make_interval(hours => :hmax)
                                          AND now() - make_interval(hours => :hmin))
                OR
                -- B) sin marcar, pero fecha ya pasó hace ≥ asumida
                (ci.estado IN ('agendada','reprogramada')
                 AND ci.fecha_inicio BETWEEN now() - make_interval(hours => :hmax_asum)
                                          AND now() - make_interval(hours => :hmin_asum))
              )
          AND NOT EXISTS (
              SELECT 1 FROM intervencion_humana
              WHERE cliente_id = c.id AND pausado_hasta > now()
          )
          AND NOT EXISTS (
              SELECT 1 FROM cliente_tags ct
              JOIN tags t ON t.id = ct.tag_id
              WHERE ct.cliente_id = c.id AND t.nombre = ANY(:excl)
          )
          -- No molestar a quien YA respondió después de la cita
          AND NOT EXISTS (
              SELECT 1 FROM conversaciones cv
              WHERE cv.cliente_id = c.id
                AND cv.direccion = 'inbound'
                AND cv.timestamp > ci.fecha_inicio + interval '15 minutes'
          )
        ORDER BY ci.fecha_inicio ASC
        LIMIT :lim
    """), {
        "excl": list(TAGS_EXCLUSION),
        "hmin": horas_min,
        "hmax": horas_max,
        "hmin_asum": horas_min_asumida,
        "hmax_asum": horas_max_asumida,
        "lim": max_envios,
    })).fetchall()

    if not candidatos:
        log.info("acciones.seguimiento_post_cita.sin_candidatos",
                 horas_min=horas_min, horas_max=horas_max)
        return {"ok": True, "candidatos": 0, "enviados": 0}

    log.warning("acciones.seguimiento_post_cita.candidatos",
                total=len(candidatos), dry_run=dry_run)

    if dry_run:
        return {
            "ok": True, "dry_run": True,
            "candidatos": [
                {"cliente_id": c.cliente_id, "numero": c.numero_whatsapp,
                 "nombre": c.nombre, "cita": str(c.fecha_inicio)[:19],
                 "cita_estado": c.cita_estado}
                for c in candidatos
            ],
        }

    # Asegurar que el tag existe
    await session.execute(_sa("""
        INSERT INTO tags (nombre, color, descripcion, orden, created_by)
        VALUES (:n, '#34D399', 'Marcado por accion_seguimiento_post_cita. '
                                'El bot ya hizo seguimiento de esta reunión.',
                201, 'sistema')
        ON CONFLICT (nombre) DO NOTHING
    """), {"n": TAG_HECHO})
    await session.commit()

    enviados = 0
    fallos: list[dict] = []
    from app.identidades import principal as _identidad_principal
    ident = _identidad_principal()

    for c in candidatos:
        try:
            texto = await _generar_mensaje_post_cita(
                session=session,
                cliente_id=c.cliente_id,
                cliente_nombre=c.nombre,
                negocio=c.negocio,
                identidad=ident,
            )
            if not texto:
                fallos.append({"cliente_id": c.cliente_id, "razon": "claude_vacio_o_skip"})
                continue
            await enviar_texto(c.numero_whatsapp, texto)
            await session.execute(_sa("""
                INSERT INTO conversaciones
                  (cliente_id, direccion, tipo, contenido, intent, modelo, metadata, timestamp)
                VALUES
                  (:cid, 'outbound', 'texto', :txt, 'post_cita_auto',
                   :modelo, CAST(:meta AS jsonb), now())
            """), {
                "cid": c.cliente_id,
                "txt": texto,
                "modelo": settings.claude_model_principal,
                "meta": '{"via":"post_cita_auto","cita_id":' + str(c.cita_id) + '}',
            })
            await session.execute(_sa("""
                INSERT INTO cliente_tags (cliente_id, tag_id, added_by)
                SELECT :cid, t.id, 'sistema:post_cita'
                  FROM tags t WHERE t.nombre = :tag
                ON CONFLICT DO NOTHING
            """), {"cid": c.cliente_id, "tag": TAG_HECHO})
            await session.commit()
            enviados += 1
            log.info("acciones.post_cita.enviado",
                     cliente_id=c.cliente_id, numero=c.numero_whatsapp,
                     chars=len(texto))
        except Exception as e:
            fallos.append({"cliente_id": c.cliente_id, "error": str(e)[:120]})
            log.warning("acciones.post_cita.fail",
                        cliente_id=c.cliente_id, error=str(e))
        await asyncio.sleep(random.uniform(dmin, dmax))

    log.warning("acciones.seguimiento_post_cita.done",
                candidatos=len(candidatos), enviados=enviados, fallos=len(fallos))
    return {
        "ok": True,
        "candidatos": len(candidatos),
        "enviados": enviados,
        "fallos": fallos[:10],
    }


async def _generar_mensaje_post_cita(
    *,
    session: AsyncSession,
    cliente_id: int,
    cliente_nombre: str | None,
    negocio: str | None,
    identidad,
) -> str:
    """Genera mensaje de seguimiento post-reunión.

    Lee historial real + instrucción de tono cálido: preguntar si quedó con
    dudas e invitar a avanzar con la propuesta. Si Claude detecta que NO
    tiene sentido escribir, devuelve "SKIP".
    """
    from sqlalchemy import text as _sa
    from app.claude.anthropic_client import get_anthropic_client
    from app.claude.prompts import construir_system_prompt

    rows = (await session.execute(_sa("""
        SELECT direccion, contenido, timestamp
          FROM conversaciones
         WHERE cliente_id = :c
           AND contenido IS NOT NULL
           AND trim(contenido) != ''
           AND timestamp > now() - interval '21 days'
         ORDER BY timestamp ASC
         LIMIT 40
    """), {"c": cliente_id})).fetchall()
    if not rows:
        return ""

    historial = []
    for r in rows:
        role = "assistant" if r.direccion in ("outbound", "humano") else "user"
        historial.append({"role": role, "content": r.contenido})

    system = construir_system_prompt(persona_file=identidad.persona_prompt_file)
    nombre_str = f" ({cliente_nombre})" if cliente_nombre else ""
    negocio_str = f" Su negocio es {negocio}." if negocio else ""
    instruccion = (
        "## SEGUIMIENTO POST-REUNIÓN — instrucción para este turno\n\n"
        f"Hace unas horas tuviste una reunión por videollamada con este "
        f"prospecto{nombre_str}.{negocio_str} La reunión YA terminó y la "
        "marcaron como completada en el sistema. Tu trabajo ahora es "
        "escribirle UN mensaje breve para hacer un seguimiento cálido.\n\n"
        "**Genera UN solo mensaje corto (2-4 líneas máximo):**\n"
        "- Tono cercano, agradecido por el tiempo de la reunión.\n"
        "- **Pregunta si quedó con alguna duda** sobre lo que conversaron.\n"
        "- **Invita a avanzar** sin presionar — algo tipo \"¿cómo lo viste?\" "
        "o \"¿quedamos en avanzar?\".\n"
        "- NO uses frases robóticas tipo \"hacer seguimiento\", \"checkear\" "
        "o \"darle un toque\".\n"
        "- NO repitas argumentos de venta que ya diste en la reunión.\n"
        "- Si recuerdas algo específico que se conversó (un punto, una duda, "
        "un próximo paso acordado), conéctalo. Eso hace el mensaje genuino.\n"
        "- Termina con UNA pregunta corta.\n\n"
        "Si la conversación cerró de forma negativa (el cliente dijo "
        "claramente que no, o quedó molesto), responde con la palabra exacta "
        "`SKIP` y nada más."
    )
    system = list(system) + [{"type": "text", "text": instruccion}]

    cli = get_anthropic_client()
    try:
        resp = await cli.messages.create(
            model=settings.claude_model_principal,
            max_tokens=300,
            system=system,
            messages=historial + [
                {"role": "user", "content": "[Seguimiento post-cita: genera el mensaje ahora]"},
            ],
        )
    except Exception as e:
        log.warning("post_cita.claude_fail", cliente_id=cliente_id, error=str(e))
        return ""

    chunks = []
    for b in (resp.content or []):
        if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip():
            chunks.append(b.text)
    texto = "\n".join(t.strip() for t in chunks if t.strip()).strip()
    if not texto or texto.upper() == "SKIP":
        log.info("post_cita.skip", cliente_id=cliente_id, motivo=texto[:30] or "vacio")
        return ""
    return texto


async def _generar_mensaje_seguimiento(
    *,
    session: AsyncSession,
    cliente_id: int,
    cliente_numero: str,
    cliente_nombre: str | None,
    identidad,
) -> str:
    """Llama a Claude con identidad + historial real del cliente + instrucción
    de retomar la conversación con naturalidad. Sin tools (forzar texto).
    """
    from sqlalchemy import text as _sa
    from app.claude.anthropic_client import get_anthropic_client
    from app.claude.prompts import construir_system_prompt

    # Cargar últimos 30 mensajes / 14 días
    rows = (await session.execute(_sa("""
        SELECT direccion, contenido, timestamp
          FROM conversaciones
         WHERE cliente_id = :c
           AND contenido IS NOT NULL
           AND trim(contenido) != ''
           AND timestamp > now() - interval '14 days'
         ORDER BY timestamp ASC
         LIMIT 30
    """), {"c": cliente_id})).fetchall()
    if not rows:
        return ""

    historial = []
    for r in rows:
        role = "assistant" if r.direccion in ("outbound", "humano") else "user"
        historial.append({"role": role, "content": r.contenido})

    # System prompt + bloque de instrucción específica de seguimiento
    system = construir_system_prompt(persona_file=identidad.persona_prompt_file)
    instruccion_seguimiento = (
        "## SEGUIMIENTO AUTOMÁTICO — instrucción para este turno\n\n"
        f"Estás retomando una conversación con un prospecto"
        f"{f' ({cliente_nombre})' if cliente_nombre else ''} que **no respondió "
        "desde hace más de 24 horas**. El historial completo está en los mensajes "
        "previos.\n\n"
        "**Genera UN solo mensaje breve** (1-3 líneas, máximo) para retomar la "
        "conversación:\n"
        "- Conecta con lo último que conversaron (no escribas '¡hola!' como si "
        "fuera la primera vez — el cliente ya te conoce).\n"
        "- Si quedó pendiente algo concreto (envío de info, agendar, decisión), "
        "retoma eso de forma natural.\n"
        "- Si no había nada pendiente claro, pregunta cómo va o si pudo pensarlo.\n"
        "- Tono cercano, NO insistente. Cero presión.\n"
        "- NO uses frases tipo 'solo quería hacer seguimiento' o 'recordarte que' — "
        "suena a venta. Habla como si te acabaras de acordar.\n"
        "- Termina con UNA pregunta corta para invitar a responder.\n\n"
        "Si crees que NO tiene sentido escribirle (la conversación cerró bien, "
        "el cliente dijo claramente que no, etc.), responde con la palabra "
        "exacta `SKIP` y nada más — eso le indica al sistema que no envíe nada."
    )
    system = list(system) + [{"type": "text", "text": instruccion_seguimiento}]

    # Llamada SIN tools → forzar respuesta de texto
    cli = get_anthropic_client()
    try:
        resp = await cli.messages.create(
            model=settings.claude_model_principal,
            max_tokens=300,
            system=system,
            messages=historial + [
                {"role": "user", "content": "[Seguimiento automático: genera el mensaje ahora]"},
            ],
        )
    except Exception as e:
        log.warning("seguimiento.claude_fail", cliente_id=cliente_id, error=str(e))
        return ""

    chunks = []
    for b in (resp.content or []):
        if getattr(b, "type", None) == "text" and getattr(b, "text", "").strip():
            chunks.append(b.text)
    texto = "\n".join(t.strip() for t in chunks if t.strip()).strip()
    if not texto or texto.upper() == "SKIP":
        log.info("seguimiento.skip", cliente_id=cliente_id, motivo=texto[:30] or "vacio")
        return ""
    return texto


# ─── REGISTRY ────────────────────────────────────────────────────────────────


from app.automatizaciones.heartbeat import accion_heartbeat as _accion_heartbeat


ACCIONES_DISPONIBLES: dict[str, dict[str, Any]] = {
    "heartbeat": {
        "handler": _accion_heartbeat,
        "descripcion": "Dairo decide UNA acción útil (pilar openclaw). Conservadora por defecto.",
        "parametros": {"respetar_horario": "bool (default true)"},
    },
    "reporte_ceo": {
        "handler": accion_reporte_ceo,
        "descripcion": "Reporte CEO diario a Dairo (finanzas + tareas + 3 sugerencias)",
        "parametros": {"destino_id": "+57... (default Dairo)", "mes": "opcional"},
    },
    "motivacional_equipo": {
        "handler": accion_motivacional_equipo,
        "descripcion": "Mensaje motivacional al equipo",
        "parametros": {"destinos": "lista de +57... o destino_id"},
    },
    "seguimiento_tareas": {
        "handler": accion_seguimiento_tareas,
        "descripcion": "Seguimiento de tareas TODO a miembros (Edgardo/Jhonathan)",
        "parametros": {"usuarios": "lista nombres DT-OS", "telefonos": "mapa usuario→+57..."},
    },
    "reflexion_semanal": {
        "handler": accion_reflexion_semanal,
        "descripcion": "Reflexión semanal al CEO (viernes)",
        "parametros": {"destino_id": "+57... (default Dairo)"},
    },
    "alerta_financiera": {
        "handler": accion_alerta_financiera,
        "descripcion": "Alerta si disponible < piso o ≥ techo",
        "parametros": {"destino_id": "+57...", "piso": "int", "techo": "int"},
    },
    "clientes_en_riesgo": {
        "handler": accion_clientes_en_riesgo,
        "descripcion": "Aviso de clientes en riesgo / cobros vencidos",
        "parametros": {"destino_id": "+57..."},
    },
    "reporte_meta_cliente": {
        "handler": accion_reporte_meta_cliente,
        "descripcion": "Reporte de Meta Ads a un cliente",
        "parametros": {"account_id": "act_...", "destino_id": "+57...", "date_preset": "yesterday|...", "nombre_cliente": "str"},
    },
    "recordatorio_pendientes": {
        "handler": accion_recordatorio_pendientes,
        "descripcion": "Recordatorio de pendientes/alertas abiertas >Nh",
        "parametros": {"destino_id": "+57...", "horas_min": "int", "max_alertas": "int"},
    },
    "mensaje_custom": {
        "handler": accion_mensaje_custom,
        "descripcion": "Mensaje arbitrario, opcional con query SQL ({{resultado}})",
        "parametros": {"destino_tipo": "numero|grupo", "destino_id": "...", "mensaje": "texto con {{fecha}} {{hora}} {{resultado}}", "query_sql": "SELECT opcional"},
    },
    "monitor_email": {
        "handler": accion_monitor_email,
        "descripcion": "Monitor de correo (Equilibrio). PENDIENTE credenciales IMAP.",
        "parametros": {"imap_host": "...", "imap_user": "...", "imap_pass": "...", "destino_id": "+57..."},
    },
    "monitor_bancolombia": {
        "handler": accion_monitor_bancolombia,
        "descripcion": "Monitor movimientos Bancolombia. PENDIENTE definir fuente.",
        "parametros": {},
    },
    "enviar_pendientes_apertura": {
        "handler": accion_enviar_pendientes_apertura,
        "descripcion": "Envía mensajes que el bot generó fuera de horario y dejó pendientes (madrugada → 8 am).",
        "parametros": {"max_envios": "int (default 50)", "delay_min_s": "int", "delay_max_s": "int"},
    },
    "sync_chats_whapi": {
        "handler": accion_sync_chats_whapi,
        "descripcion": "Importa de whapi mensajes recientes que el webhook no entregó (restart, hipo).",
        "parametros": {"max_chats": "int (def 30)", "max_msgs_por_chat": "int (def 15)", "horas_max": "int (def 24)"},
    },
    "seguimiento_prospectos": {
        "handler": accion_seguimiento_prospectos,
        "descripcion": (
            "Seguimiento automático a prospectos sin cita que no han contestado "
            "en N días. Claude lee el historial y genera mensaje contextual. "
            "Requiere feature_seguimiento_auto=true."
        ),
        "parametros": {
            "dias_min": "int (def 1) — días mínimos sin contestar",
            "dias_max": "int (def 7) — no contactar leads más viejos",
            "max_envios": "int (def 30) — cap por corrida",
            "delay_min_s": "int (def 35)",
            "delay_max_s": "int (def 90)",
            "ignorar_cooldown": "bool — saltar el cooldown de 24h del tag (para limpiar backlog)",
            "dry_run": "bool — si true, solo lista candidatos sin enviar",
        },
    },
    "seguimiento_post_cita": {
        "handler": accion_seguimiento_post_cita,
        "descripcion": (
            "Seguimiento post-reunión. Dispara cuando: (A) la cita está marcada "
            "'completada' y pasó 4-8 h, o (B) la cita sigue 'agendada' pero su "
            "fecha ya pasó hace al menos 24 h (asumimos que ocurrió). Excluye a "
            "quienes ya escribieron después de la cita. Tono cálido: ¿quedó "
            "alguna duda? ¿cómo lo viste? Requiere feature_seguimiento_auto=true."
        ),
        "parametros": {
            "horas_min": "int (def 4) — caso A: mínimo desde la reunión",
            "horas_max": "int (def 8) — caso A: máximo (margen del cron)",
            "horas_min_asumida": "int (def 24) — caso B: horas desde la fecha",
            "horas_max_asumida": "int (def 168) — caso B: tope superior (7 días)",
            "max_envios": "int (def 20)",
            "delay_min_s": "int (def 35)",
            "delay_max_s": "int (def 90)",
            "dry_run": "bool — si true, solo lista candidatos sin enviar",
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
