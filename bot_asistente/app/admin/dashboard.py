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
    Cliente,
    Conversacion,
    Pedido,
    ProductoCache,
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

    # Ventas: contamos TODOS los pedidos excepto cancelados (incluye
    # 'datos_completos' que es el estado típico de contraentregas o pedidos
    # recién tomados pero aún no pagados).
    estados_venta = ("datos_completos", "esperando_pago", "comprobante_recibido",
                     "confirmado", "despachado", "entregado")

    ventas_hoy = (await session.execute(
        select(func.coalesce(func.sum(Pedido.total), 0), func.count())
        .where(Pedido.estado.in_(estados_venta))
        .where(Pedido.created_at >= hoy)
    )).one()
    ventas_7d = (await session.execute(
        select(func.coalesce(func.sum(Pedido.total), 0), func.count())
        .where(Pedido.estado.in_(estados_venta))
        .where(Pedido.created_at >= hace_7d)
    )).one()
    ventas_30d = (await session.execute(
        select(func.coalesce(func.sum(Pedido.total), 0), func.count())
        .where(Pedido.estado.in_(estados_venta))
        .where(Pedido.created_at >= hace_30d)
    )).one()

    # Pedidos por estado (últimos 7d)
    pedidos_por_estado = dict((await session.execute(
        select(Pedido.estado, func.count())
        .where(Pedido.created_at >= hace_7d)
        .group_by(Pedido.estado)
    )).all())

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

    # Clientes activos (escribieron al menos 1 vez en últimos 7d)
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

    # Top productos por menciones (no por ventas, ya que pedidos están vacíos al inicio)
    top_productos = []
    rows = (await session.execute(
        select(Pedido.items)
        .where(Pedido.created_at >= hace_30d)
    )).scalars().all()
    contador: dict[str, int] = {}
    for items in rows:
        for it in (items or []):
            ref = it.get("ref")
            if ref:
                contador[ref] = contador.get(ref, 0) + int(it.get("cantidad", 1))
    top_productos = sorted(contador.items(), key=lambda kv: -kv[1])[:10]

    # Alertas pendientes
    alertas_pendientes = (await session.execute(
        select(func.count()).select_from(AlertaFabio).where(AlertaFabio.resuelto.is_(False))
    )).scalar_one()

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

    # Pedidos recientes (top 8) para tabla principal
    pedidos_recientes_rows = (await session.execute(
        select(Pedido).order_by(Pedido.id.desc()).limit(8)
    )).scalars().all()
    pedidos_recientes = [
        {
            "id": p.id,
            "cliente_id": p.cliente_id,
            "total_cop": float(p.total or 0),
            "estado": p.estado,
            "metodo_pago": p.metodo_pago,
            "barrio": p.barrio,
            "creado": p.created_at.isoformat() if p.created_at else None,
        }
        for p in pedidos_recientes_rows
    ]

    # Serie diaria últimos 7 días: conversaciones y ventas (para gráfico)
    serie_7d: list[dict] = []
    for i in range(6, -1, -1):
        dia = (ahora - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        dia_siguiente = dia + timedelta(days=1)
        n_conv = (await session.execute(
            select(func.count()).select_from(Conversacion)
            .where(and_(Conversacion.timestamp >= dia, Conversacion.timestamp < dia_siguiente))
        )).scalar_one()
        ventas_dia = (await session.execute(
            select(func.coalesce(func.sum(Pedido.total), 0))
            .where(Pedido.estado.in_(estados_venta))
            .where(and_(Pedido.created_at >= dia, Pedido.created_at < dia_siguiente))
        )).scalar_one()
        serie_7d.append({
            "fecha": dia.strftime("%Y-%m-%d"),
            "label": dia.strftime("%a %d"),
            "conversaciones": int(n_conv),
            "ventas_cop": float(ventas_dia or 0),
        })

    # Catálogo
    productos_total = (await session.execute(
        select(func.count()).select_from(ProductoCache).where(ProductoCache.activo.is_(True))
    )).scalar_one()

    return {
        "hora_consulta": ahora.isoformat(),
        "ventas": {
            "hoy": {"total_cop": float(ventas_hoy[0]), "cantidad": int(ventas_hoy[1])},
            "7d": {"total_cop": float(ventas_7d[0]), "cantidad": int(ventas_7d[1])},
            "30d": {"total_cop": float(ventas_30d[0]), "cantidad": int(ventas_30d[1])},
        },
        "pedidos_por_estado_7d": {k: int(v) for k, v in pedidos_por_estado.items()},
        "conversaciones": {
            "total_hoy": int(conv_hoy),
            "inbound_hoy": int(inbound_hoy),
            "clientes_activos_7d": int(clientes_activos),
        },
        "claude": {
            "costo_usd_hoy": float(costo_hoy),
            "costo_usd_30d": float(costo_30d),
            "cache_hit_rate_pct": round(cache_hit_rate, 1),
        },
        "top_productos_30d": [{"ref": r, "vendidos": n} for r, n in top_productos],
        "alertas_pendientes": int(alertas_pendientes),
        "alertas_recientes": alertas_recientes,
        "pedidos_recientes": pedidos_recientes,
        "serie_7d": serie_7d,
        "catalogo": {"productos_activos": int(productos_total)},
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
    return HTMLResponse(_TEMPLATE_DASHBOARD)


_TEMPLATE_DASHBOARD = r"""<!doctype html>
<html lang="es" data-theme="light">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dashboard — Laura</title>
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
  body {
    background: var(--bg-canvas);
    color: var(--text-primary);
    font-family: 'Inter', system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  /* ===== Layout ===== */
  .app { display: grid; grid-template-columns: 240px 1fr; min-height: 100vh; }
  .sidebar {
    background: var(--bg-sidebar);
    border-right: 1px solid var(--border);
    padding: 20px 14px;
    display: flex; flex-direction: column;
    position: sticky; top: 0; height: 100vh;
  }
  .main { padding: 28px 32px; }

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
  <!-- ============ SIDEBAR ============ -->
  <aside class="sidebar">
    <div class="brand">
      <div class="brand-logo">L</div>
      <div class="brand-name">Laura · Innovación</div>
    </div>

    <button class="new-btn" onclick="location.href='/admin/chats'">+ Ver chats</button>

    <nav class="nav-group">
      <a class="nav-item active" href="/admin/dashboard"><svg class="ico" width="16" height="16"><use href="#i-dashboard"/></svg> Dashboard</a>
      <a class="nav-item" href="/admin/chats"><svg class="ico" width="16" height="16"><use href="#i-messages"/></svg> Chats</a>
      <a class="nav-item" href="/admin/cliente/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> Clientes</a>
      <a class="nav-item" href="/admin/pedido/list"><svg class="ico" width="16" height="16"><use href="#i-shop"/></svg> Pedidos</a>
      <a class="nav-item" href="/admin/alerta-fabio/list"><svg class="ico" width="16" height="16"><use href="#i-alert"/></svg> Alertas</a>
    </nav>

    <div class="nav-group">
      <div class="nav-group-label">Equipo</div>
      <a class="nav-item" href="/admin/equipo-miembro/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> Administradores</a>
      <a class="nav-item" href="/admin/numero-interno/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> Números internos</a>
    </div>

    <div class="nav-group">
      <div class="nav-group-label">Catálogo</div>
      <a class="nav-item" href="/admin/producto-cache/list"><svg class="ico" width="16" height="16"><use href="#i-shop"/></svg> Productos</a>
      <a class="nav-item" href="/admin/tarifa-domicilio/list"><svg class="ico" width="16" height="16"><use href="#i-money"/></svg> Tarifas envío</a>
    </div>

    <div class="nav-bottom">
      <a class="nav-item" href="/admin"><svg class="ico" width="16" height="16"><use href="#i-settings"/></svg> Volver al admin</a>
      <button class="nav-item" id="theme-toggle" style="background:transparent;border:none;width:100%;text-align:left;cursor:pointer;font:inherit;">
        <svg class="ico" width="16" height="16"><use href="#i-theme"/></svg> <span id="theme-label">Modo oscuro</span>
      </button>
    </div>
  </aside>

  <!-- ============ MAIN ============ -->
  <main class="main">

    <!-- Top bar -->
    <div class="topbar">
      <label class="search">
        <svg class="ico-search" width="14" height="14"><use href="#i-search"/></svg>
        <input type="text" placeholder="Buscar pedido, cliente, ref…" />
        <span class="kbd">/</span>
      </label>
      <div class="top-actions">
        <button class="btn-ghost" onclick="location.href='/admin/pedido/list'">Ver pedidos</button>
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
          <div class="chip blue"><svg class="ico" width="16" height="16"><use href="#i-shop"/></svg></div>
          <div class="card-title">Pedidos recientes</div>
          <div style="margin-left:auto;color:var(--text-tertiary);cursor:pointer;font-size:16px;line-height:1;">⋮</div>
        </div>
        <table>
          <thead>
            <tr><th>Cliente</th><th>Barrio</th><th>Pago</th><th style="text-align:right;">Total</th><th>Estado</th></tr>
          </thead>
          <tbody id="pedidos-body"></tbody>
        </table>
      </div>

      <div class="card">
        <div class="card-header">
          <div class="chip orange"><svg class="ico" width="16" height="16"><use href="#i-alert"/></svg></div>
          <div class="card-title">Acciones pendientes</div>
          <div style="margin-left:auto;color:var(--text-tertiary);cursor:pointer;font-size:16px;line-height:1;">⋮</div>
        </div>
        <ul class="action-list" id="actions-list"></ul>
      </div>
    </div>

    <!-- Chart card -->
    <div class="card chart-card">
      <div class="card-header">
        <div class="chip green"><svg class="ico" width="16" height="16"><use href="#i-spark"/></svg></div>
        <div class="card-title">Conversaciones y ventas — últimos 7 días</div>
        <div style="margin-left:auto;display:flex;align-items:center;gap:14px;font-size:12px;color:var(--text-secondary);">
          <span><span class="legend-dot" style="background:#7C3AED"></span>Conversaciones</span>
          <span><span class="legend-dot" style="background:#16A34A"></span>Ventas (COP)</span>
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
        { label: 'Ventas (COP)', data: d.serie_7d.map(s => s.ventas_cop),
          backgroundColor: 'rgba(22,163,74,0.7)', borderRadius: 6, yAxisID: 'y1' },
      ],
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        x: { grid: { display: false }, ticks: { color: textColor, font: { family: 'Inter' } } },
        y: { position: 'left', grid: { color: gridColor, borderDash: [3,3] }, ticks: { color: textColor } },
        y1:{ position: 'right', grid: { display: false }, ticks: { color: textColor, callback: v => cop(v) } },
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
    ? 'Laura responde a los clientes automáticamente.'
    : 'Laura no está respondiendo. ' + (est.razon || '') + (est.pausado_por ? ' · pausado por ' + est.pausado_por : '');
  const btn = document.getElementById('bot-banner-btn');
  btn.textContent = est.activo ? 'Pausar bot' : 'Reactivar bot';
  if (!est.activo) { btn.style.background = 'var(--accent-positive)'; btn.style.color = '#fff'; }

  // ----- KPIs (5-7 cards) -----
  const k = document.getElementById('kpis');
  const cards = [
    { chip: 'purple', icon: '#i-money',    t: 'Ventas hoy',         v: cop(d.ventas.hoy.total_cop),   sub: d.ventas.hoy.cantidad + ' pedidos' },
    { chip: 'blue',   icon: '#i-shop',     t: 'Ventas 7 días',      v: cop(d.ventas['7d'].total_cop), sub: d.ventas['7d'].cantidad + ' pedidos' },
    { chip: 'green',  icon: '#i-spark',    t: 'Ventas 30 días',     v: cop(d.ventas['30d'].total_cop),sub: d.ventas['30d'].cantidad + ' pedidos' },
    { chip: 'pink',   icon: '#i-messages', t: 'Conversaciones hoy', v: d.conversaciones.total_hoy,    sub: d.conversaciones.inbound_hoy + ' inbound' },
    { chip: 'orange', icon: '#i-users',    t: 'Clientes activos 7d',v: d.conversaciones.clientes_activos_7d, sub: 'únicos' },
    { chip: 'blue',   icon: '#i-bot',      t: 'Costo Claude hoy',   v: fmtUSD(d.claude.costo_usd_hoy),sub: '30d: ' + fmtUSD(d.claude.costo_usd_30d) },
    { chip: 'green',  icon: '#i-spark',    t: 'Cache hit',          v: d.claude.cache_hit_rate_pct + '%', sub: 'ahorro de tokens' },
    { chip: 'orange', icon: '#i-alert',    t: 'Alertas pendientes', v: d.alertas_pendientes,          sub: 'sin resolver' },
  ];
  k.innerHTML = cards.map(c => `
    <div class="kpi">
      <div class="kpi-top">
        <div class="chip ${c.chip}"><svg width="16" height="16"><use href="${c.icon}"/></svg></div>
        <div class="label">${c.t}</div>
        <div class="menu">⋮</div>
      </div>
      <div class="kpi-value">${c.v}</div>
      <div class="kpi-foot">
        <span class="vs">${c.sub}</span>
      </div>
    </div>
  `).join('');

  // ----- Pedidos recientes -----
  const tbody = document.getElementById('pedidos-body');
  if (!d.pedidos_recientes || d.pedidos_recientes.length === 0) {
    tbody.innerHTML = '<tr><td colspan="5" class="empty">Aún no hay pedidos registrados.</td></tr>';
  } else {
    tbody.innerHTML = d.pedidos_recientes.map(p => `
      <tr onclick="location.href='/admin/pedido/details/${p.id}'" style="cursor:pointer;">
        <td>
          <div class="row-avatar">
            <span class="avatar-circle">#${p.id}</span>
            <div>
              <div class="cell-main">Pedido #${p.id}</div>
              <div class="cell-sub">${p.creado ? new Date(p.creado).toLocaleDateString('es-CO') : ''}</div>
            </div>
          </div>
        </td>
        <td>${p.barrio || '<span style="color:var(--text-tertiary)">—</span>'}</td>
        <td>${(p.metodo_pago || '—').replace(/_/g, ' ')}</td>
        <td style="text-align:right;font-weight:600;">${cop(p.total_cop)}</td>
        <td><span class="badge-state ${p.estado || ''}">${p.estado || '—'}</span></td>
      </tr>
    `).join('');
  }

  // ----- Acciones pendientes -----
  const list = document.getElementById('actions-list');
  if (!d.alertas_recientes || d.alertas_recientes.length === 0) {
    list.innerHTML = '<li class="empty">Sin alertas pendientes 🎉</li>';
  } else {
    list.innerHTML = d.alertas_recientes.map(a => `
      <li class="action-item">
        <div class="action-icon"><svg width="14" height="14"><use href="#i-alert"/></svg></div>
        <div class="action-text">
          <div class="action-title">${(a.tipo || '').replace(/_/g, ' ')}</div>
          <div class="action-sub">${(a.preview || '').replace(/</g,'&lt;')}</div>
        </div>
        <button class="action-cta" onclick="location.href='/admin/alerta-fabio/details/${a.id}'">→</button>
      </li>
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
