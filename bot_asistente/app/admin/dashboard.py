"""Endpoint /admin/dashboard — métricas clave del bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.admin._shell import ICON_SPRITE, SHELL_STYLES, THEME_TOGGLE_JS, sidebar_html
from app.db.models import (
    AlertaFabio,
    Cita,
    Cliente,
    Conversacion,
    Prospecto,
)
from app.db.session import get_session

router = APIRouter(prefix="/admin", tags=["admin"])


def _check_auth(request: Request) -> bool:
    """Misma sesión que SQLAdmin."""
    return "admin_token" in request.session


@router.get("/dashboard.json")
async def dashboard_json(
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    if not _check_auth(request):
        return JSONResponse({"error": "unauthorized"}, status_code=401)

    ahora = datetime.now(timezone.utc)
    hoy = ahora.replace(hour=0, minute=0, second=0, microsecond=0)
    hace_7d = ahora - timedelta(days=7)
    hace_30d = ahora - timedelta(days=30)

    # Prospectos (nuevos por periodo)
    prospectos_hoy = (await session.execute(
        select(func.count()).select_from(Prospecto).where(Prospecto.created_at >= hoy)
    )).scalar_one()
    prospectos_7d = (await session.execute(
        select(func.count()).select_from(Prospecto).where(Prospecto.created_at >= hace_7d)
    )).scalar_one()

    # Prospectos por estado del funnel (últimos 30d)
    prospectos_por_estado = dict((await session.execute(
        select(Prospecto.estado, func.count())
        .where(Prospecto.created_at >= hace_30d)
        .group_by(Prospecto.estado)
    )).all())

    # Citas agendadas por periodo (por fecha de la cita)
    citas_hoy = (await session.execute(
        select(func.count()).select_from(Cita)
        .where(and_(Cita.fecha_inicio >= hoy, Cita.fecha_inicio < hoy + timedelta(days=1)))
        .where(Cita.estado.in_(["agendada", "reprogramada"]))
    )).scalar_one()
    citas_7d = (await session.execute(
        select(func.count()).select_from(Cita)
        .where(Cita.created_at >= hace_7d)
    )).scalar_one()
    citas_30d = (await session.execute(
        select(func.count()).select_from(Cita)
        .where(Cita.created_at >= hace_30d)
    )).scalar_one()

    # Conversaciones
    conv_hoy = (await session.execute(
        select(func.count())
        .select_from(Conversacion)
        .where(Conversacion.timestamp >= hoy)
    )).scalar_one()

    inbound_hoy = (await session.execute(
        select(func.count())
        .select_from(Conversacion)
        .where(and_(Conversacion.timestamp >= hoy, Conversacion.direccion == "inbound"))
    )).scalar_one()

    # Mensajes enviados hoy (outbound del bot + humano admin)
    outbound_hoy = (await session.execute(
        select(func.count())
        .select_from(Conversacion)
        .where(and_(
            Conversacion.timestamp >= hoy,
            Conversacion.direccion.in_(["outbound", "humano"]),
        ))
    )).scalar_one()

    # Chats activos hoy (clientes distintos que escribieron al menos 1 inbound HOY)
    chats_activos_hoy = (await session.execute(
        select(func.count(func.distinct(Conversacion.cliente_id)))
        .where(and_(Conversacion.timestamp >= hoy, Conversacion.direccion == "inbound"))
    )).scalar_one()

    # Clientes activos (escribieron al menos 1 vez en últimos 7d) — para histórico
    clientes_activos = (await session.execute(
        select(func.count(func.distinct(Conversacion.cliente_id)))
        .where(Conversacion.timestamp >= hace_7d)
    )).scalar_one()

    # Costo Claude
    costo_hoy = (await session.execute(
        select(func.coalesce(func.sum(Conversacion.costo_usd), 0))
        .where(Conversacion.timestamp >= hoy)
    )).scalar_one()
    costo_30d = (await session.execute(
        select(func.coalesce(func.sum(Conversacion.costo_usd), 0))
        .where(Conversacion.timestamp >= hace_30d)
    )).scalar_one()

    # Tokens cacheados (cuánto cache ahorró)
    cache_hoy = (await session.execute(
        select(
            func.coalesce(func.sum(Conversacion.tokens_input), 0),
            func.coalesce(func.sum(Conversacion.cache_read_tokens), 0),
        ).where(Conversacion.timestamp >= hoy)
    )).one()
    cache_hit_rate = 0.0
    if cache_hoy[0] + cache_hoy[1]:
        cache_hit_rate = float(cache_hoy[1]) / float(cache_hoy[0] + cache_hoy[1]) * 100

    # Alertas pendientes
    alertas_pendientes = (await session.execute(
        select(func.count()).select_from(AlertaFabio).where(AlertaFabio.resuelto.is_(False))
    )).scalar_one()

    # Prospectos sin cita activa (warm leads para seguimiento)
    from sqlalchemy import text as _sa
    sin_agendar = (await session.execute(_sa("""
        SELECT COUNT(*)
          FROM clientes c
         WHERE c.etiqueta = 'prospecto'
           AND c.bloqueado = FALSE
           AND NOT EXISTS (
               SELECT 1 FROM citas
                WHERE cliente_id = c.id
                  AND estado IN ('agendada','reprogramada','completada')
           )
           AND NOT EXISTS (
               SELECT 1 FROM cliente_tags ct
               JOIN tags t ON t.id = ct.tag_id
               WHERE ct.cliente_id = c.id
                 AND t.nombre IN ('Cerrado / ganado','Perdido','No fit')
           )
           AND (
               SELECT MAX(timestamp) FROM conversaciones
                WHERE cliente_id = c.id
           ) > now() - interval '14 days'
    """))).scalar_one() or 0

    # Conversaciones sin responder: último mensaje del cliente fue inbound
    # hace > 1h, no hay outbound posterior, no está pausado.
    sin_responder = (await session.execute(_sa("""
        SELECT COUNT(DISTINCT c.id)
          FROM clientes c
          JOIN conversaciones cv ON cv.cliente_id = c.id
         WHERE cv.id = (
                 SELECT MAX(id) FROM conversaciones WHERE cliente_id = c.id
               )
           AND cv.direccion = 'inbound'
           AND cv.timestamp < now() - interval '1 hour'
           AND cv.timestamp > now() - interval '7 days'
           AND c.etiqueta IS DISTINCT FROM 'personal'
           AND c.bloqueado = FALSE
           AND NOT EXISTS (
               SELECT 1 FROM intervencion_humana
                WHERE cliente_id = c.id AND pausado_hasta > now()
           )
    """))).scalar_one() or 0

    # Seguimientos enviados hoy (cron seguimiento_prospectos)
    seguimientos_hoy = (await session.execute(_sa("""
        SELECT COUNT(*) FROM conversaciones
         WHERE timestamp >= :hoy
           AND direccion = 'outbound'
           AND (metadata->>'via' = 'seguimiento_auto' OR intent = 'seguimiento_auto')
    """), {"hoy": hoy})).scalar_one() or 0

    # Alertas recientes (top 5 sin resolver) para panel "Acciones pendientes"
    alertas_recientes_rows = (await session.execute(
        select(AlertaFabio).where(AlertaFabio.resuelto.is_(False))
        .order_by(AlertaFabio.created_at.desc()).limit(5)
    )).scalars().all()
    alertas_recientes = [
        {
            "id": a.id,
            "tipo": a.tipo,
            "preview": (a.mensaje or "")[:80],
            "creada": a.created_at.isoformat() if a.created_at else None,
        }
        for a in alertas_recientes_rows
    ]

    # Citas próximas (top 8) para tabla principal
    citas_rows = (await session.execute(
        select(Cita, Cliente)
        .join(Cliente, Cliente.id == Cita.cliente_id)
        .where(Cita.estado.in_(["agendada", "reprogramada"]))
        .where(Cita.fecha_inicio >= ahora - timedelta(hours=12))
        .order_by(Cita.fecha_inicio.asc()).limit(8)
    )).all()
    citas_proximas = [
        {
            "id": c.id,
            "nombre": c.nombre or (cl.nombre if cl else None) or "—",
            "negocio": c.negocio or "—",
            "fecha": c.fecha_inicio.isoformat() if c.fecha_inicio else None,
            "estado": c.estado,
        }
        for c, cl in citas_rows
    ]

    # Serie diaria últimos 7 días: conversaciones + prospectos nuevos
    serie_7d: list[dict] = []
    for i in range(6, -1, -1):
        dia = (ahora - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        dia_siguiente = dia + timedelta(days=1)
        n_conv = (await session.execute(
            select(func.count()).select_from(Conversacion)
            .where(and_(Conversacion.timestamp >= dia, Conversacion.timestamp < dia_siguiente))
        )).scalar_one()
        n_pros = (await session.execute(
            select(func.count()).select_from(Prospecto)
            .where(and_(Prospecto.created_at >= dia, Prospecto.created_at < dia_siguiente))
        )).scalar_one()
        serie_7d.append({
            "fecha": dia.strftime("%Y-%m-%d"),
            "label": dia.strftime("%a %d"),
            "conversaciones": int(n_conv),
            "prospectos": int(n_pros),
        })

    return {
        "hora_consulta": ahora.isoformat(),
        "prospectos": {
            "hoy": int(prospectos_hoy),
            "7d": int(prospectos_7d),
            "por_estado_30d": {k: int(v) for k, v in prospectos_por_estado.items()},
        },
        "citas": {"hoy": int(citas_hoy), "7d": int(citas_7d), "30d": int(citas_30d)},
        "conversaciones": {
            "total_hoy": int(conv_hoy),
            "inbound_hoy": int(inbound_hoy),
            "outbound_hoy": int(outbound_hoy),
            "chats_activos_hoy": int(chats_activos_hoy),
            "clientes_activos_7d": int(clientes_activos),
        },
        "claude": {
            "costo_usd_hoy": float(costo_hoy),
            "costo_usd_30d": float(costo_30d),
            "cache_hit_rate_pct": round(cache_hit_rate, 1),
        },
        "alertas_pendientes": int(alertas_pendientes),
        "sin_agendar": int(sin_agendar),
        "sin_responder": int(sin_responder),
        "seguimientos_hoy": int(seguimientos_hoy),
        "alertas_recientes": alertas_recientes,
        "citas_proximas": citas_proximas,
        "serie_7d": serie_7d,
        "bot_estado": await _bot_estado(session),
    }


async def _bot_estado(session: AsyncSession) -> dict:
    from sqlalchemy import text as sa_text
    row = (await session.execute(sa_text(
        "SELECT activo, pausado_por, pausado_en, razon FROM bot_estado WHERE id=1"
    ))).first()
    if not row:
        return {"activo": True, "pausado_por": None, "pausado_en": None, "razon": None}
    return {
        "activo": bool(row[0]),
        "pausado_por": row[1],
        "pausado_en": row[2].isoformat() if row[2] else None,
        "razon": row[3],
    }


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_html(request: Request):
    """Página HTML mínima que llama al JSON y renderiza con JS."""
    if not _check_auth(request):
        return HTMLResponse(
            '<p>No autenticado. <a href="/admin/login">Login</a></p>',
            status_code=401,
        )
    html = _TEMPLATE_DASHBOARD.replace("__SIDEBAR__", sidebar_html(active="dashboard"))
    return HTMLResponse(html)


_TEMPLATE_DASHBOARD = r"""<!doctype html>
<html lang="es" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard — Dairo</title>
<script>
/* Theme init temprano: aplica data-theme antes de que el CSS renderice
   para evitar flash claro→oscuro y bg mezclado. */
(function(){
  try {
    var saved = localStorage.getItem('theme');
    document.documentElement.setAttribute('data-theme', saved === 'dark' ? 'dark' : 'light');
  } catch(e) {}
})();
</script>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<style>
  /* ===== Variables: light mode (default) ===== */
  :root {
    --bg-canvas: #F7F7F8;
    --bg-card: #FFFFFF;
    --bg-sidebar: #FAFAFA;
    --bg-soft: #F4F4F5;
    --border: #EAEAEC;
    --border-subtle: #F0F0F1;
    --text-primary: #1A1A1A;
    --text-secondary: #6B7280;
    --text-tertiary: #9CA3AF;
    --accent-positive: #16A34A;
    --accent-positive-bg: rgba(22,163,74,.10);
    --accent-negative: #DC2626;
    --accent-negative-bg: rgba(220,38,38,.10);
    --shadow-card: 0 1px 2px rgba(0,0,0,0.04);
    --btn-primary-bg: #1A1A1A;
    --btn-primary-text: #FFFFFF;

    --chip-purple: #7C3AED;   --chip-purple-bg: rgba(124,58,237,.12);
    --chip-blue:   #2563EB;   --chip-blue-bg:   rgba(37,99,235,.12);
    --chip-orange: #EA580C;   --chip-orange-bg: rgba(234,88,12,.12);
    --chip-pink:   #DB2777;   --chip-pink-bg:   rgba(219,39,119,.12);
    --chip-green:  #16A34A;   --chip-green-bg:  rgba(22,163,74,.12);
  }
  /* ===== Variables: dark mode ===== */
  [data-theme="dark"] {
    --bg-canvas: #09090B;
    --bg-card: #18181B;
    --bg-sidebar: #0F0F10;
    --bg-soft: #1F1F23;
    --border: #27272A;
    --border-subtle: #1F1F23;
    --text-primary: #FAFAFA;
    --text-secondary: #A1A1AA;
    --text-tertiary: #71717A;
    --accent-positive: #22C55E;
    --accent-positive-bg: rgba(34,197,94,.15);
    --accent-negative: #EF4444;
    --accent-negative-bg: rgba(239,68,68,.15);
    --shadow-card: 0 1px 2px rgba(0,0,0,0.3);
    --btn-primary-bg: #FAFAFA;
    --btn-primary-text: #0A0A0A;
  }

  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  html { background: var(--bg-canvas); }
  body {
    background: var(--bg-canvas) !important;
    color: var(--text-primary);
    font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  /* ===== Layout ===== */
  .app {
    display: grid; grid-template-columns: 240px 1fr; min-height: 100vh;
    background: var(--bg-canvas) !important;
  }
  .sidebar {
    background: var(--bg-sidebar) !important;
    border-right: 1px solid var(--border);
    padding: 20px 14px;
    display: flex; flex-direction: column;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
  }
  .main { background: var(--bg-canvas) !important; padding: 28px 32px; }

  /* ===== Sidebar ===== */
  .brand { display: flex; align-items: center; gap: 10px; padding: 4px 10px 16px; }
  .brand-logo {
    width: 32px; height: 32px; border-radius: 8px;
    background: var(--chip-orange-bg); color: var(--chip-orange);
    display: grid; place-items: center; font-weight: 700; font-size: 14px;
  }
  .brand-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .brand-menu { margin-left: auto; color: var(--text-tertiary); cursor: pointer; }

  .new-btn {
    display: block;
    width: 100%; padding: 9px 12px; margin: 4px 0 16px;
    background: var(--bg-card); border: 1px solid var(--border);
    color: var(--text-primary) !important; border-radius: 8px;
    font: inherit; font-weight: 500; font-size: 13px;
    cursor: pointer; text-align: left; text-decoration: none;
    box-shadow: var(--shadow-card);
  }
  .new-btn:hover { background: var(--bg-soft); }

  .nav-group { margin-bottom: 16px; }
  .nav-group-label {
    font-size: 11px; color: var(--text-tertiary);
    text-transform: uppercase; letter-spacing: .5px;
    padding: 6px 12px; font-weight: 500;
  }
  .nav-item {
    display: flex; align-items: center; gap: 10px;
    padding: 8px 12px; border-radius: 8px;
    color: var(--text-secondary); text-decoration: none;
    font-size: 13.5px; font-weight: 500;
    margin: 1px 0;
  }
  .nav-item:hover { background: var(--bg-soft); color: var(--text-primary); }
  .nav-item.active { background: var(--bg-soft); color: var(--text-primary); }
  .nav-item .ico { width: 16px; height: 16px; flex-shrink: 0; }
  .nav-bottom { margin-top: auto; padding-top: 16px; border-top: 1px solid var(--border-subtle); }

  /* ===== Top bar ===== */
  .topbar { display: flex; align-items: center; gap: 16px; margin-bottom: 20px; }
  .search {
    flex: 1; max-width: 480px; position: relative;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 8px; padding: 8px 12px 8px 36px;
    display: flex; align-items: center; gap: 8px;
  }
  .search input {
    flex: 1; background: transparent; border: none; outline: none;
    color: var(--text-primary); font: inherit; font-size: 13px;
  }
  .search input::placeholder { color: var(--text-tertiary); }
  .search .ico-search { position: absolute; left: 12px; color: var(--text-tertiary); }
  .search .kbd {
    background: var(--bg-soft); border: 1px solid var(--border);
    border-radius: 4px; padding: 2px 6px; font-size: 11px;
    color: var(--text-tertiary);
  }
  .top-actions { margin-left: auto; display: flex; align-items: center; gap: 8px; }

  /* ===== Title ===== */
  .page-title { font-size: 22px; font-weight: 600; margin: 0 0 4px; }
  .page-subtitle { color: var(--text-secondary); font-size: 13px; margin-bottom: 24px; }

  /* ===== Filtros ===== */
  .filter-row { display: flex; align-items: center; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }
  .seg {
    display: inline-flex; background: var(--bg-soft); padding: 3px;
    border-radius: 8px; border: 1px solid var(--border);
  }
  .seg button {
    padding: 5px 12px; border: none; background: transparent;
    color: var(--text-secondary); font: inherit; font-size: 12.5px; font-weight: 500;
    border-radius: 6px; cursor: pointer;
  }
  .seg button.active { background: var(--bg-card); color: var(--text-primary); box-shadow: var(--shadow-card); }
  .pill {
    padding: 6px 12px; background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 8px; font-size: 12.5px; color: var(--text-primary);
    display: inline-flex; align-items: center; gap: 6px;
    cursor: pointer;
  }
  .btn-primary {
    padding: 7px 14px; background: var(--btn-primary-bg); color: var(--btn-primary-text);
    border: none; border-radius: 8px; font: inherit; font-size: 13px; font-weight: 500;
    cursor: pointer;
  }
  .btn-primary:hover { opacity: .9; }
  .btn-ghost {
    padding: 6px 12px; background: transparent; border: 1px solid var(--border);
    border-radius: 8px; color: var(--text-primary); font: inherit; font-size: 12.5px;
    cursor: pointer;
  }

  /* ===== Toggle switch (iOS style) ===== */
  .switch-row { display: inline-flex; align-items: center; gap: 8px; }
  .switch { position: relative; width: 36px; height: 20px; }
  .switch input { display: none; }
  .switch span {
    position: absolute; inset: 0; background: var(--bg-soft);
    border: 1px solid var(--border); border-radius: 999px; cursor: pointer;
    transition: background .2s;
  }
  .switch span::before {
    content: ''; position: absolute; left: 2px; top: 2px;
    width: 14px; height: 14px; border-radius: 50%; background: var(--text-primary);
    transition: transform .2s;
  }
  .switch input:checked + span { background: var(--accent-positive); border-color: var(--accent-positive); }
  .switch input:checked + span::before { transform: translateX(16px); background: #fff; }

  /* ===== Cards ===== */
  .card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: var(--shadow-card);
  }
  .card-header { padding: 14px 18px; display: flex; align-items: center; gap: 10px; border-bottom: 1px solid var(--border-subtle); }
  .card-title { font-size: 14px; font-weight: 600; }
  .card-body { padding: 18px; }

  /* Chip de ícono */
  .chip {
    width: 32px; height: 32px; border-radius: 8px;
    display: grid; place-items: center; flex-shrink: 0;
  }
  .chip.purple { background: var(--chip-purple-bg); color: var(--chip-purple); }
  .chip.blue   { background: var(--chip-blue-bg);   color: var(--chip-blue); }
  .chip.orange { background: var(--chip-orange-bg); color: var(--chip-orange); }
  .chip.pink   { background: var(--chip-pink-bg);   color: var(--chip-pink); }
  .chip.green  { background: var(--chip-green-bg);  color: var(--chip-green); }

  /* ===== KPI grid ===== */
  .kpi-grid {
    display: grid; gap: 16px; margin-bottom: 24px;
    grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  }
  .kpi {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 16px 18px; box-shadow: var(--shadow-card);
  }
  a.kpi-link {
    text-decoration: none; color: inherit; display: block;
    transition: transform .12s, border-color .12s, box-shadow .12s;
  }
  a.kpi-link:hover {
    transform: translateY(-2px);
    border-color: var(--chip-purple);
    box-shadow: 0 6px 16px rgba(124,58,237,.15);
  }
  .kpi-top { display: flex; align-items: center; gap: 10px; }
  .kpi-top .label { font-size: 13px; color: var(--text-secondary); flex: 1; }
  .kpi-top .menu  { color: var(--text-tertiary); cursor: pointer; font-size: 16px; line-height: 1; }
  .kpi-value { font-size: 30px; font-weight: 700; margin: 12px 0 8px; letter-spacing: -.5px; color: var(--text-primary); }
  .card-title { color: var(--text-primary); }
  .page-title { color: var(--text-primary); }
  .kpi-foot  { display: flex; align-items: center; justify-content: space-between; font-size: 12px; }
  .kpi-foot .vs { color: var(--text-tertiary); }
  .delta { display: inline-flex; align-items: center; gap: 3px; font-weight: 600; }
  .delta.up   { color: var(--accent-positive); }
  .delta.down { color: var(--accent-negative); }

  /* ===== Bot status banner ===== */
  .bot-banner {
    display: flex; align-items: center; justify-content: space-between; gap: 16px;
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; padding: 14px 18px; margin-bottom: 20px;
    box-shadow: var(--shadow-card);
  }
  .bot-status-dot {
    display: inline-block; width: 8px; height: 8px; border-radius: 50%;
    margin-right: 8px;
  }
  .bot-status-dot.active   { background: var(--accent-positive); }
  .bot-status-dot.inactive { background: var(--accent-negative); }

  /* ===== Two-column row ===== */
  .row-2col { display: grid; grid-template-columns: 2fr 1fr; gap: 16px; margin-bottom: 24px; }
  @media (max-width: 1024px) { .row-2col { grid-template-columns: 1fr; } }

  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left; font-size: 11.5px; font-weight: 500; color: var(--text-tertiary);
    padding: 10px 18px; text-transform: uppercase; letter-spacing: .5px;
    border-bottom: 1px solid var(--border-subtle);
  }
  tbody td { padding: 14px 18px; border-bottom: 1px solid var(--border-subtle); font-size: 13.5px; }
  tbody tr:last-child td { border-bottom: none; }
  .row-avatar {
    display: inline-flex; align-items: center; gap: 10px;
  }
  .avatar-circle {
    width: 28px; height: 28px; border-radius: 50%;
    background: var(--bg-soft); color: var(--text-secondary);
    display: grid; place-items: center; font-size: 12px; font-weight: 600;
  }
  .cell-main { font-weight: 500; color: var(--text-primary); }
  .cell-sub  { font-size: 11.5px; color: var(--text-tertiary); margin-top: 2px; }
  .badge-state {
    display: inline-block; padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 500;
    background: var(--bg-soft); color: var(--text-secondary);
    border: 1px solid var(--border);
  }
  .badge-state.confirmado { background: var(--accent-positive-bg); color: var(--accent-positive); border-color: transparent; }

  /* ===== Action list (panel derecho) ===== */
  .action-list { list-style: none; margin: 0; padding: 0; }
  .action-item {
    display: flex; align-items: center; gap: 12px;
    padding: 14px 18px; border-bottom: 1px solid var(--border-subtle);
  }
  .action-item:last-child { border-bottom: none; }
  .action-icon {
    width: 28px; height: 28px; border-radius: 6px;
    background: var(--bg-soft); color: var(--text-secondary);
    display: grid; place-items: center; flex-shrink: 0;
  }
  .action-text { flex: 1; min-width: 0; }
  .action-title { font-size: 13px; font-weight: 500; color: var(--text-primary); }
  .action-sub { font-size: 11.5px; color: var(--text-tertiary); margin-top: 2px; }
  .action-cta {
    background: transparent; border: 1px solid var(--border);
    border-radius: 6px; padding: 5px 10px; font: inherit; font-size: 12px;
    color: var(--text-primary); cursor: pointer; flex-shrink: 0;
  }
  .action-cta:hover { background: var(--bg-soft); }

  /* ===== Chart ===== */
  .chart-card { padding: 0; }
  .chart-canvas-wrap { padding: 18px; height: 320px; position: relative; }
  .legend-dot { display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 6px; }

  .empty { padding: 32px 18px; text-align: center; color: var(--text-tertiary); font-size: 13px; }

  /* Iconos SVG inline (16x16 stroke 1.75) */
  .ico { display: inline-block; vertical-align: middle; }
</style>
</head>
<body>

<svg width="0" height="0" style="position:absolute">
  <defs>
    <symbol id="i-dashboard" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="9"/><rect x="14" y="3" width="7" height="5"/><rect x="14" y="12" width="7" height="9"/><rect x="3" y="16" width="7" height="5"/></symbol>
    <symbol id="i-messages" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 11.5a8.38 8.38 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.38 8.38 0 0 1-3.8-.9L3 21l1.9-5.7a8.38 8.38 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.38 8.38 0 0 1 3.8-.9h.5a8.48 8.48 0 0 1 8 8v.5z"/></symbol>
    <symbol id="i-users" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><path d="M23 21v-2a4 4 0 0 0-3-3.87"/><path d="M16 3.13a4 4 0 0 1 0 7.75"/></symbol>
    <symbol id="i-shop" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M6 2L3 6v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2V6l-3-4z"/><line x1="3" y1="6" x2="21" y2="6"/><path d="M16 10a4 4 0 0 1-8 0"/></symbol>
    <symbol id="i-alert" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></symbol>
    <symbol id="i-settings" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/></symbol>
    <symbol id="i-search" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></symbol>
    <symbol id="i-plus" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></symbol>
    <symbol id="i-up" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="7" y1="17" x2="17" y2="7"/><polyline points="7 7 17 7 17 17"/></symbol>
    <symbol id="i-down" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="17" y1="7" x2="7" y2="17"/><polyline points="17 17 7 17 7 7"/></symbol>
    <symbol id="i-cal" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></symbol>
    <symbol id="i-bot" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="7" width="18" height="13" rx="2"/><circle cx="9" cy="13" r="1"/><circle cx="15" cy="13" r="1"/><path d="M12 7V3"/></symbol>
    <symbol id="i-theme" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></symbol>
    <symbol id="i-arrow" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="5" y1="12" x2="19" y2="12"/><polyline points="12 5 19 12 12 19"/></symbol>
    <symbol id="i-money" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="12" y1="1" x2="12" y2="23"/><path d="M17 5H9.5a3.5 3.5 0 0 0 0 7h5a3.5 3.5 0 0 1 0 7H6"/></symbol>
    <symbol id="i-spark" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 17 9 11 13 15 21 7"/></symbol>
  </defs>
</svg>

<div class="app">
  <!-- ============ SIDEBAR (placeholder sustituido con sidebar_html) ============ -->
  __SIDEBAR__

  <!-- ============ MAIN ============ -->
  <main class="main">

    <!-- Top bar -->
    <div class="topbar">
      <label class="search">
        <svg class="ico-search" width="14" height="14"><use href="#i-search"/></svg>
        <input type="text" placeholder="Buscar contacto, prospecto, cita…" />
        <span class="kbd">/</span>
      </label>
      <div class="top-actions">
        <button class="btn-ghost" onclick="location.href='/admin/cita/list'">Ver citas</button>
        <button class="btn-primary" onclick="window.print()">Exportar</button>
      </div>
    </div>

    <h1 class="page-title">Esto es lo que está pasando</h1>
    <p class="page-subtitle" id="hora">Cargando…</p>

    <!-- Bot status banner -->
    <div class="bot-banner" id="bot-banner">
      <div style="display:flex;align-items:center;gap:12px;">
        <div class="chip purple"><svg class="ico" width="16" height="16"><use href="#i-bot"/></svg></div>
        <div>
          <div style="font-weight:600;font-size:13.5px;" id="bot-banner-title">Cargando…</div>
          <div style="font-size:12px;color:var(--text-tertiary);" id="bot-banner-sub"></div>
        </div>
      </div>
      <form method="POST" action="/admin/actions/bot/toggle">
        <button type="submit" class="btn-primary" id="bot-banner-btn">…</button>
      </form>
    </div>

    <!-- KPI grid -->
    <div class="kpi-grid" id="kpis"></div>

    <!-- Row: tabla pedidos + acciones -->
    <div class="row-2col">
      <div class="card">
        <div class="card-header">
          <div class="chip blue"><svg class="ico" width="16" height="16"><use href="#i-cal"/></svg></div>
          <div class="card-title">Citas próximas</div>
          <div style="margin-left:auto;color:var(--text-tertiary);cursor:pointer;font-size:16px;line-height:1;">⋮</div>
        </div>
        <table>
          <thead>
            <tr><th>Contacto</th><th>Fecha</th><th>Estado</th><th style="text-align:right;"></th></tr>
          </thead>
          <tbody id="pedidos-body"></tbody>
        </table>
      </div>

    </div>

    <!-- Chart card -->
    <div class="card chart-card">
      <div class="card-header">
        <div class="chip green"><svg class="ico" width="16" height="16"><use href="#i-spark"/></svg></div>
        <div class="card-title">Conversaciones y prospectos — últimos 7 días</div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:14px;font-size:12px;color:var(--text-secondary);">
          <span><span class="legend-dot" style="background:#7C3AED"></span>Conversaciones</span>
          <span><span class="legend-dot" style="background:#16A34A"></span>Prospectos</span>
        </div>
      </div>
      <div class="chart-canvas-wrap"><canvas id="chart-7d"></canvas></div>
    </div>

  </main>
</div>

<script>
// ============ Theme toggle ============
// Default = light. Solo aplica dark si el user lo eligió explícitamente.
// No respetamos prefers-color-scheme automáticamente para evitar mezclas
// extrañas cuando algunos elementos no heredan las vars correctamente.
(function(){
  const saved = localStorage.getItem('theme');
  document.documentElement.setAttribute('data-theme', saved === 'dark' ? 'dark' : 'light');
  document.getElementById('theme-toggle').addEventListener('click', () => {
    const cur = document.documentElement.getAttribute('data-theme') || 'light';
    const nxt = cur === 'dark' ? 'light' : 'dark';
    document.documentElement.setAttribute('data-theme', nxt);
    localStorage.setItem('theme', nxt);
    document.getElementById('theme-label').textContent = nxt === 'dark' ? 'Modo claro' : 'Modo oscuro';
    if (window._chart7d) renderChart(window._chart7dData);
  });
  document.getElementById('theme-label').textContent =
    document.documentElement.getAttribute('data-theme') === 'dark' ? 'Modo claro' : 'Modo oscuro';
})();

// ============ Formatters ============
const fmt = n => new Intl.NumberFormat('es-CO').format(n);
const fmtUSD = n => '$' + Number(n).toFixed(4);
const cop = n => '$' + fmt(Math.round(n));
const initials = s => (s||'?').split(/\s+/).map(p=>p[0]).slice(0,2).join('').toUpperCase();

function deltaHtml(pct, positiveIsGood = true) {
  const up = pct >= 0;
  const cls = (up === positiveIsGood) ? 'up' : 'down';
  const ico = up ? '#i-up' : '#i-down';
  return `<span class="delta ${cls}"><svg width="11" height="11"><use href="${ico}"/></svg>${Math.abs(pct).toFixed(1)}%</span>`;
}

// ============ Chart ============
function renderChart(d) {
  const isDark = document.documentElement.getAttribute('data-theme') === 'dark';
  const gridColor = isDark ? '#27272A' : '#F0F0F1';
  const textColor = isDark ? '#A1A1AA' : '#6B7280';

  if (window._chart7d) window._chart7d.destroy();
  const ctx = document.getElementById('chart-7d').getContext('2d');
  window._chart7d = new Chart(ctx, {
    type: 'bar',
    data: {
      labels: d.serie_7d.map(s => s.label),
      datasets: [
        { label: 'Conversaciones', data: d.serie_7d.map(s => s.conversaciones),
          backgroundColor: 'rgba(124,58,237,0.7)', borderRadius: 6, yAxisID: 'y' },
        { label: 'Prospectos', data: d.serie_7d.map(s => s.prospectos),
          backgroundColor: 'rgba(22,163,74,0.7)', borderRadius: 6, yAxisID: 'y1' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor, font: { family: 'Inter' } } },
        y: { position: 'left', grid: { color: gridColor, borderDash: [3,3] }, ticks: { color: textColor } },
        y1:{ position: 'right', grid: { display: false }, ticks: { color: textColor, precision: 0 } },
      },
    },
  });
}

// ============ Fetch + render ============
fetch('/admin/dashboard.json').then(r => r.json()).then(d => {
  document.getElementById('hora').textContent = 'Consultado: ' + new Date(d.hora_consulta).toLocaleString('es-CO');

  // ----- Bot banner -----
  const est = d.bot_estado || { activo: true };
  const banner = document.getElementById('bot-banner');
  const dot = est.activo
    ? '<span class="bot-status-dot active"></span>Bot ACTIVO'
    : '<span class="bot-status-dot inactive"></span>Bot PAUSADO';
  document.getElementById('bot-banner-title').innerHTML = dot;
  document.getElementById('bot-banner-sub').textContent = est.activo
    ? 'Dairo responde automáticamente.'
    : 'Dairo no está respondiendo. ' + (est.razon || '') + (est.pausado_por ? ' · pausado por ' + est.pausado_por : '');
  const btn = document.getElementById('bot-banner-btn');
  btn.textContent = est.activo ? 'Pausar bot' : 'Reactivar bot';
  if (!est.activo) { btn.style.background = 'var(--accent-positive)'; btn.style.color = '#fff'; }

  // ----- KPIs -----
  const k = document.getElementById('kpis');
  const costoStr = '$' + (d.claude.costo_usd_hoy || 0).toFixed(2);
  const cards = [
    { chip: 'purple', icon: '#i-users',    t: 'Prospectos hoy',     v: d.prospectos.hoy,                    sub: d.prospectos['7d'] + ' en 7 días', href: '/admin/prospecto/list' },
    { chip: 'green',  icon: '#i-cal',      t: 'Citas hoy',          v: d.citas.hoy,                         sub: d.citas['7d'] + ' agendadas (7d)', href: '/admin/cita/list' },
    { chip: 'blue',   icon: '#i-cal',      t: 'Citas 30 días',      v: d.citas['30d'],                      sub: 'agendadas',                       href: '/admin/cita/list' },
    { chip: 'pink',   icon: '#i-messages', t: 'Chats activos hoy',  v: d.conversaciones.chats_activos_hoy,  sub: 'contactos únicos',                href: '/admin/chats' },
    { chip: 'blue',   icon: '#i-bot',      t: 'Mensajes enviados',  v: d.conversaciones.outbound_hoy,       sub: 'hoy' },
    { chip: 'purple', icon: '#i-messages', t: 'Recibidos hoy',      v: d.conversaciones.inbound_hoy,        sub: 'del cliente' },
    { chip: 'orange', icon: '#i-alert',    t: 'Sin responder',      v: d.sin_responder,                     sub: '> 1h sin respuesta',              href: '/admin/chats' },
    { chip: 'green',  icon: '#i-users',    t: 'Sin agendar',        v: d.sin_agendar,                       sub: 'warm leads',                      href: '/admin/seguimiento' },
    { chip: 'pink',   icon: '#i-bot',      t: 'Seguimientos hoy',   v: d.seguimientos_hoy,                  sub: 'auto-enviados' },
    { chip: 'blue',   icon: '#i-spark',    t: 'Costo Claude hoy',   v: costoStr,                            sub: 'USD' },
    { chip: 'orange', icon: '#i-alert',    t: 'Pendientes',         v: d.alertas_pendientes,                sub: 'sin resolver',                    href: '/admin/alerta-fabio/list' },
  ];
  k.innerHTML = cards.map(c => {
    const inner = `
      <div class="kpi-top">
        <div class="chip ${c.chip}"><svg width="16" height="16"><use href="${c.icon}"/></svg></div>
        <div class="label">${c.t}</div>
        <div class="menu">⋮</div>
      </div>
      <div class="kpi-value">${c.v}</div>
      <div class="kpi-foot">
        <span class="vs">${c.sub}</span>
      </div>`;
    return c.href
      ? `<a class="kpi kpi-link" href="${c.href}">${inner}</a>`
      : `<div class="kpi">${inner}</div>`;
  }).join('');

  // ----- Citas próximas -----
  const tbody = document.getElementById('pedidos-body');
  if (!d.citas_proximas || d.citas_proximas.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty">No hay citas próximas.</td></tr>';
  } else {
    tbody.innerHTML = d.citas_proximas.map(c => `
      <tr onclick="location.href='/admin/cita/details/${c.id}'" style="cursor:pointer;">
        <td>
          <div class="row-avatar">
            <span class="avatar-circle">${initials(c.nombre)}</span>
            <div>
              <div class="cell-main">${(c.nombre || '—').replace(/</g,'&lt;')}</div>
              <div class="cell-sub">${(c.negocio || '').replace(/</g,'&lt;')}</div>
            </div>
          </div>
        </td>
        <td>${c.fecha ? new Date(c.fecha).toLocaleString('es-CO', {weekday:'short', day:'2-digit', month:'short', hour:'2-digit', minute:'2-digit'}) : '—'}</td>
        <td><span class="badge-state ${c.estado || ''}">${c.estado || '—'}</span></td>
        <td style="text-align:right;">→</td>
      </tr>
    `).join('');
  }

  // ----- Chart -----
  window._chart7dData = d;
  renderChart(d);

}).catch(e => {
  console.error(e);
  document.body.insertAdjacentHTML('beforeend',
    '<div style="position:fixed;bottom:20px;right:20px;background:var(--accent-negative);color:#fff;padding:12px 16px;border-radius:8px;">Error: ' + e + '</div>');
});
</script>
</body>
</html>"""
