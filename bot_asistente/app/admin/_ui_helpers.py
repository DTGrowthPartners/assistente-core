"""Helpers UI compartidos entre vistas del admin (chats, contactos, etc.).

Sistema de diseño DTGP — ver claude-ajustes-css.md para la guía completa.
"""

from __future__ import annotations

from datetime import datetime, timezone

AVATAR_COLORS = [
    ("#7C3AED", "#FFFFFF"),  # violeta
    ("#2563EB", "#FFFFFF"),  # azul
    ("#059669", "#FFFFFF"),  # verde
    ("#D97706", "#FFFFFF"),  # naranja
    ("#DC2626", "#FFFFFF"),  # rojo
    ("#0891B2", "#FFFFFF"),  # cyan
    ("#65A30D", "#FFFFFF"),  # lima
    ("#DB2777", "#FFFFFF"),  # rosa
]


def avatar_color(seed: str) -> tuple[str, str]:
    """Devuelve (bg, fg) determinístico según el nombre/número."""
    if not seed:
        return ("#9CA3AF", "#FFFFFF")
    idx = sum(ord(c) for c in seed) % len(AVATAR_COLORS)
    return AVATAR_COLORS[idx]


def get_initials(name: str | None, fallback: str = "?") -> str:
    """Saca las iniciales (1-2 letras) del nombre."""
    if not name or not name.strip():
        return fallback
    parts = [p for p in name.strip().split() if p and p[0].isalnum()]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    return name.strip()[:2].upper()


def format_relative_date(dt: datetime | None) -> str:
    """Fecha relativa estilo 'Hoy 10:54', 'Ayer 15:30', 'Hace 3 días', '15 nov 2025'."""
    if not dt:
        return "—"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/Bogota")
        dt_local = dt.astimezone(tz) if dt.tzinfo else dt.replace(tzinfo=timezone.utc).astimezone(tz)
        now_local = datetime.now(tz)
    except Exception:
        dt_local = dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        now_local = datetime.now(timezone.utc)

    diff = now_local - dt_local
    secs = diff.total_seconds()
    if secs < 60:
        return "Ahora mismo"
    if secs < 3600:
        return f"Hace {int(secs // 60)} min"

    same_day = dt_local.date() == now_local.date()
    if same_day:
        return f"Hoy {dt_local.strftime('%H:%M')}"
    yesterday = (now_local.date() - dt_local.date()).days == 1
    if yesterday:
        return f"Ayer {dt_local.strftime('%H:%M')}"
    days_ago = (now_local.date() - dt_local.date()).days
    if 0 < days_ago < 7:
        return f"Hace {days_ago} días"
    # Más de una semana — fecha corta en español
    meses = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"]
    mes = meses[dt_local.month - 1]
    if dt_local.year == now_local.year:
        return f"{dt_local.day} {mes}"
    return f"{dt_local.day} {mes} {dt_local.year}"


def format_phone(numero: str | None) -> str:
    """Formatea '+573003200547' como '+57 300 320 0547'. Si no encaja con Colombia, devuelve tal cual."""
    if not numero:
        return ""
    n = numero.strip()
    only = "".join(c for c in n if c.isdigit())
    if n.startswith("+57") and len(only) == 12:
        # +57 XXX XXX XXXX
        return f"+57 {only[2:5]} {only[5:8]} {only[8:12]}"
    if n.startswith("+1") and len(only) == 11:
        return f"+1 ({only[1:4]}) {only[4:7]}-{only[7:11]}"
    if n.startswith("+") and len(only) >= 10:
        # Genérico: agrupa cada 3-4
        cc_len = len(only) - 10
        return f"+{only[:cc_len]} {only[cc_len:cc_len+3]} {only[cc_len+3:cc_len+6]} {only[cc_len+6:]}"
    return n


# ─── CSS común (pills, avatares) — para inyectar en vistas que lo necesiten ─

PILL_STYLES = """
<style>
  /* Pills de etiqueta — sistema de diseño DTGP */
  .pill {
    display: inline-flex; align-items: center;
    padding: 2px 10px; border-radius: 9999px;
    font-size: 12px; font-weight: 600;
    white-space: nowrap; line-height: 1.6;
  }
  .pill--cliente    { background: #D1FAE5; color: #065F46; }
  .pill--prospecto  { background: #FEF3C7; color: #92400E; }
  .pill--equipo     { background: #DBEAFE; color: #1E40AF; }
  .pill--personal   { background: #F3F4F6; color: #374151; }
  .pill--grupo      { background: #EDE9FE; color: #5B21B6; }
  .pill--sin       { background: #FEF2F2; color: #991B1B; }
  .pill--sin_clasificar { background: #FEF2F2; color: #991B1B; }
  .pill--bloqueado  { background: #1F2937; color: #9CA3AF; }
  [data-theme="dark"] .pill--cliente { background: #064E3B; color: #6EE7B7; }
  [data-theme="dark"] .pill--prospecto { background: #78350F; color: #FCD34D; }
  [data-theme="dark"] .pill--equipo { background: #1E3A8A; color: #93C5FD; }
  [data-theme="dark"] .pill--personal { background: #334155; color: #CBD5E1; }
  [data-theme="dark"] .pill--grupo { background: #4C1D95; color: #C4B5FD; }
  [data-theme="dark"] .pill--sin_clasificar { background: #7F1D1D; color: #FCA5A5; }

  /* Estado del bot por chat */
  .pill--bot-active { background: #ECFDF5; color: #047857; }
  .pill--bot-paused { background: #FEF2F2; color: #B91C1C; }
</style>
"""


def pill_etiqueta(etiqueta: str | None, bloqueado: bool = False) -> str:
    """HTML del pill de etiqueta."""
    if bloqueado:
        return '<span class="pill pill--bloqueado">Bloqueado</span>'
    et = (etiqueta or "").lower()
    labels = {
        "cliente": "Cliente", "prospecto": "Prospecto",
        "equipo": "Equipo", "personal": "Personal",
        "grupo": "Grupo",
    }
    if et in labels:
        return f'<span class="pill pill--{et}">{labels[et]}</span>'
    return '<span class="pill pill--sin_clasificar">Sin clasificar</span>'
