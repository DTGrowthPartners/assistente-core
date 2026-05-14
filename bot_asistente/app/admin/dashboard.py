"""Endpoint /admin/dashboard — métricas clave del bot."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

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

    # Ventas (pedidos en estado confirmado, despachado, entregado)
    estados_venta = ("confirmado", "despachado", "entregado")

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
        "catalogo": {"productos_activos": int(productos_total)},
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


_TEMPLATE_DASHBOARD = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Dashboard — Asistente</title>
<style>
  :root { --bg:#0f1115; --card:#1a1d24; --text:#e7e9ec; --muted:#8a8f99; --accent:#5cc8ff; }
  * { box-sizing: border-box; }
  body { background:var(--bg); color:var(--text); font-family:-apple-system,Segoe UI,Roboto,sans-serif; margin:0; padding:24px; }
  h1 { font-size:24px; margin:0 0 4px 0; }
  .subtitle { color:var(--muted); font-size:13px; margin-bottom:24px; }
  .grid { display:grid; grid-template-columns:repeat(auto-fit, minmax(220px, 1fr)); gap:16px; }
  .card { background:var(--card); padding:18px; border-radius:10px; }
  .card h3 { margin:0 0 8px 0; color:var(--muted); font-size:13px; font-weight:500; text-transform:uppercase; letter-spacing:0.5px; }
  .big { font-size:28px; font-weight:600; color:var(--accent); }
  .sub { font-size:13px; color:var(--muted); margin-top:4px; }
  .nav { margin-bottom:24px; }
  .nav a { color:var(--accent); text-decoration:none; margin-right:16px; }
  table { width:100%; border-collapse:collapse; }
  th, td { padding:8px 12px; text-align:left; border-bottom:1px solid #2a2e36; font-size:14px; }
  th { color:var(--muted); font-weight:500; }
  .section { margin-top:32px; }
</style>
</head>
<body>
<div class="nav">
  <a href="/admin">← Volver al admin</a>
</div>
<h1>Dashboard</h1>
<div class="subtitle" id="hora">Cargando…</div>

<div class="grid" id="kpis"></div>

<div class="section">
  <h2 style="font-size:18px; color:var(--muted);">Top productos (30 días)</h2>
  <div class="card"><table id="top"><thead><tr><th>Ref</th><th>Vendidos</th></tr></thead><tbody></tbody></table></div>
</div>

<script>
const fmt = n => new Intl.NumberFormat('es-CO').format(n);
const fmtUSD = n => '$' + n.toFixed(4);
const cop = n => '$' + fmt(Math.round(n));

fetch('/admin/dashboard.json').then(r => r.json()).then(d => {
  document.getElementById('hora').textContent = 'Consultado: ' + new Date(d.hora_consulta).toLocaleString('es-CO');
  const k = document.getElementById('kpis');
  const cards = [
    { t: 'Ventas hoy', v: cop(d.ventas.hoy.total_cop), s: d.ventas.hoy.cantidad + ' pedidos' },
    { t: 'Ventas 7 días', v: cop(d.ventas['7d'].total_cop), s: d.ventas['7d'].cantidad + ' pedidos' },
    { t: 'Ventas 30 días', v: cop(d.ventas['30d'].total_cop), s: d.ventas['30d'].cantidad + ' pedidos' },
    { t: 'Conversaciones hoy', v: d.conversaciones.total_hoy, s: d.conversaciones.inbound_hoy + ' inbound' },
    { t: 'Clientes activos 7d', v: d.conversaciones.clientes_activos_7d, s: 'únicos' },
    { t: 'Costo Claude hoy', v: fmtUSD(d.claude.costo_usd_hoy), s: '30d: ' + fmtUSD(d.claude.costo_usd_30d) },
    { t: 'Cache hit hoy', v: d.claude.cache_hit_rate_pct + '%', s: 'ahorro de tokens' },
    { t: 'Alertas pendientes', v: d.alertas_pendientes, s: 'sin resolver' },
    { t: 'Productos activos', v: d.catalogo.productos_activos, s: 'en catálogo' },
  ];
  k.innerHTML = cards.map(c => `<div class="card"><h3>${c.t}</h3><div class="big">${c.v}</div><div class="sub">${c.s}</div></div>`).join('');

  const tbody = document.querySelector('#top tbody');
  if (d.top_productos_30d.length === 0) {
    tbody.innerHTML = '<tr><td colspan="2" style="color:var(--muted)">Sin pedidos confirmados aún</td></tr>';
  } else {
    tbody.innerHTML = d.top_productos_30d.map(p => `<tr><td>${p.ref}</td><td>${p.vendidos}</td></tr>`).join('');
  }
}).catch(e => {
  document.body.innerHTML += '<p style="color:#f55">Error: ' + e + '</p>';
});
</script>
</body>
</html>"""
