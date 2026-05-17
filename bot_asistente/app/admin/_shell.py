"""Shell compartido del admin nuevo (sidebar, styles base, theme toggle).

Se importa desde dashboard.py y chats.py para que el look sea consistente.
"""

from __future__ import annotations


# ── CSS variables + reset + componentes base ────────────────────────────────

SHELL_STYLES = r"""
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<!-- Theme init: ANTES del CSS para evitar flash y bg mezclados -->
<script>
(function(){
  try {
    var saved = localStorage.getItem('theme');
    document.documentElement.setAttribute('data-theme', saved === 'dark' ? 'dark' : 'light');
  } catch(e) {}
})();
</script>
<style>
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
    font-size: 14px; line-height: 1.5;
    -webkit-font-smoothing: antialiased;
  }

  /* Layout app */
  .app {
    display: grid; grid-template-columns: 240px 1fr; min-height: 100vh;
    background: var(--bg-canvas) !important;
  }
  .app.collapsed { grid-template-columns: 64px 1fr; }
  @media (max-width: 768px) {
    .app { grid-template-columns: 1fr; }
    .sidebar { position: fixed !important; left: 0; top: 0; bottom: 0; z-index: 90;
               width: 260px; transform: translateX(-100%); transition: transform .25s ease; }
    .sidebar.open { transform: translateX(0); box-shadow: 0 0 24px rgba(0,0,0,.2); }
    .main { padding: 16px 14px !important; }
  }
  .sidebar {
    background: var(--bg-sidebar) !important; border-right: 1px solid var(--border);
    padding: 20px 14px; display: flex; flex-direction: column;
    position: sticky; top: 0; height: 100vh; overflow-y: auto;
    transition: width .2s ease;
  }
  .app.collapsed .sidebar { padding: 20px 8px; }
  .app.collapsed .sidebar .brand-name,
  .app.collapsed .sidebar .nav-group-label,
  .app.collapsed .sidebar .nav-item span,
  .app.collapsed .sidebar #theme-label,
  .app.collapsed .sidebar .new-btn { display: none; }
  .app.collapsed .sidebar .nav-item { justify-content: center; padding: 10px; }
  .app.collapsed .sidebar .brand { justify-content: center; }
  .main {
    background: var(--bg-canvas) !important;
    padding: 28px 32px; min-width: 0;
  }

  /* Mobile hamburger + collapse desktop button */
  .mobile-bar {
    display: none; align-items: center; gap: 10px; padding: 10px 14px;
    background: var(--bg-card); border-bottom: 1px solid var(--border);
    position: sticky; top: 0; z-index: 80;
  }
  .mobile-bar .brand-mini { display: flex; align-items: center; gap: 8px; font-weight: 600; font-size: 14px; }
  .mobile-bar .hamburger {
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 8px;
    width: 36px; height: 36px; display: grid; place-items: center; cursor: pointer;
    color: var(--text-primary);
  }
  @media (max-width: 768px) { .mobile-bar { display: flex; } }
  .sidebar-backdrop {
    display: none; position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 85;
  }
  .sidebar-backdrop.show { display: block; }

  /* Collapse button (desktop) — esquina superior derecha del sidebar */
  .sidebar-collapse-btn {
    position: absolute; right: 8px; top: 16px; width: 22px; height: 22px;
    background: var(--bg-card); border: 1px solid var(--border); border-radius: 6px;
    display: grid; place-items: center; cursor: pointer; z-index: 5;
    color: var(--text-secondary); padding: 0;
  }
  .sidebar-collapse-btn:hover { color: var(--text-primary); background: var(--bg-soft); }
  .app.collapsed .sidebar-collapse-btn svg { transform: rotate(180deg); }
  @media (max-width: 768px) { .sidebar-collapse-btn { display: none; } }

  /* Sidebar */
  .brand { display: flex; align-items: center; gap: 10px; padding: 4px 10px 16px; }
  .brand-logo {
    width: 32px; height: 32px; border-radius: 8px;
    background: var(--chip-orange-bg); color: var(--chip-orange);
    display: grid; place-items: center; font-weight: 700; font-size: 14px;
  }
  .brand-name { font-weight: 600; font-size: 14px; color: var(--text-primary); }
  .new-btn {
    display: block; width: 100%; padding: 9px 12px; margin: 4px 0 16px;
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
    font-size: 13.5px; font-weight: 500; margin: 1px 0;
  }
  .nav-item:hover { background: var(--bg-soft); color: var(--text-primary); }
  .nav-item.active { background: var(--bg-soft); color: var(--text-primary); }
  .nav-item .ico { width: 16px; height: 16px; flex-shrink: 0; }
  .nav-bottom { margin-top: auto; padding-top: 16px; border-top: 1px solid var(--border-subtle); }

  /* Botones genéricos */
  .btn-primary {
    padding: 7px 14px; background: var(--btn-primary-bg); color: var(--btn-primary-text);
    border: none; border-radius: 8px; font: inherit; font-size: 13px; font-weight: 500;
    cursor: pointer;
  }
  .btn-primary:hover { opacity: .9; }
  .btn-ghost {
    padding: 6px 12px; background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 8px; color: var(--text-primary) !important; font: inherit; font-size: 12.5px;
    cursor: pointer; text-decoration: none; display: inline-block;
  }
  .btn-ghost:hover { background: var(--bg-soft); }

  /* Cards genéricas */
  .card {
    background: var(--bg-card); border: 1px solid var(--border);
    border-radius: 12px; box-shadow: var(--shadow-card);
  }
  .card-header {
    padding: 14px 18px; display: flex; align-items: center; gap: 10px;
    border-bottom: 1px solid var(--border-subtle);
  }
  .card-title { font-size: 14px; font-weight: 600; color: var(--text-primary); }

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

  /* Badges */
  .badge {
    display: inline-block; font-size: 10px; font-weight: 600;
    padding: 2px 8px; border-radius: 999px;
  }
  .badge-admin   { background: var(--chip-blue-bg); color: var(--chip-blue); margin-left: 6px; }
  .badge-blocked { background: var(--accent-negative-bg); color: var(--accent-negative); margin-left: 6px; }

  .ico { display: inline-block; vertical-align: middle; }
</style>
"""


# ── SVG sprite con íconos Lucide-style ──────────────────────────────────────

ICON_SPRITE = r"""
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
    <symbol id="i-back" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></symbol>
  </defs>
</svg>
"""


def sidebar_html(active: str = "dashboard") -> str:
    """Sidebar reutilizable. `active` marca el ítem actual (dashboard/chats/clientes/etc.)."""
    def cls(name: str) -> str:
        return "nav-item active" if active == name else "nav-item"

    return f"""
<aside class="sidebar">
  <button class="sidebar-collapse-btn" id="sidebar-collapse" title="Colapsar / Expandir">
    <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>
  </button>
  <div class="brand">
    <div class="brand-logo">L</div>
    <div class="brand-name">Laura · Innovación</div>
  </div>

  <a class="new-btn" href="/admin/chats">+ Ver chats</a>

  <nav class="nav-group">
    <a class="{cls('dashboard')}" href="/admin/dashboard"><svg class="ico" width="16" height="16"><use href="#i-dashboard"/></svg> <span>Dashboard</span></a>
    <a class="{cls('chats')}" href="/admin/chats"><svg class="ico" width="16" height="16"><use href="#i-messages"/></svg> <span>Chats</span></a>
    <a class="{cls('clientes')}" href="/admin/cliente/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> <span>Clientes</span></a>
    <a class="{cls('pedidos')}" href="/admin/pedido/list"><svg class="ico" width="16" height="16"><use href="#i-shop"/></svg> <span>Pedidos</span></a>
    <a class="{cls('alertas')}" href="/admin/alerta-fabio/list"><svg class="ico" width="16" height="16"><use href="#i-alert"/></svg> <span>Alertas</span></a>
  </nav>

  <div class="nav-group">
    <div class="nav-group-label">Equipo</div>
    <a class="{cls('equipo')}" href="/admin/equipo-miembro/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> <span>Administradores</span></a>
    <a class="{cls('internos')}" href="/admin/numero-interno/list"><svg class="ico" width="16" height="16"><use href="#i-users"/></svg> <span>Números internos</span></a>
  </div>

  <div class="nav-group">
    <div class="nav-group-label">Catálogo</div>
    <a class="{cls('productos')}" href="/admin/producto-cache/list"><svg class="ico" width="16" height="16"><use href="#i-shop"/></svg> <span>Productos</span></a>
    <a class="{cls('tarifas')}" href="/admin/tarifa-domicilio/list"><svg class="ico" width="16" height="16"><use href="#i-money"/></svg> <span>Tarifas envío</span></a>
  </div>

  <div class="nav-group">
    <div class="nav-group-label">Avanzado</div>
    <a class="{cls('conversaciones')}" href="/admin/conversacion/list"><svg class="ico" width="16" height="16"><use href="#i-messages"/></svg> <span>Conversaciones</span></a>
    <a class="{cls('sesiones')}" href="/admin/sesion/list"><svg class="ico" width="16" height="16"><use href="#i-cal"/></svg> <span>Sesiones</span></a>
    <a class="{cls('pausas')}" href="/admin/intervencion-humana/list"><svg class="ico" width="16" height="16"><use href="#i-bot"/></svg> <span>Pausas humano</span></a>
  </div>

  <div class="nav-bottom">
    <a class="nav-item" href="/admin"><svg class="ico" width="16" height="16"><use href="#i-settings"/></svg> <span>Volver al admin</span></a>
    <button class="nav-item" id="theme-toggle" style="background:transparent;border:none;width:100%;text-align:left;cursor:pointer;font:inherit;">
      <svg class="ico" width="16" height="16"><use href="#i-theme"/></svg> <span id="theme-label">Modo oscuro</span>
    </button>
  </div>
</aside>
"""


# JS del theme toggle + sidebar collapse desktop + hamburger móvil
THEME_TOGGLE_JS = r"""
<script>
(function(){
  // ── Auto-inyectar mobile-bar + backdrop si no existen ──
  if (!document.querySelector('.mobile-bar')) {
    var mb = document.createElement('div');
    mb.innerHTML = ''
      + '<div class="mobile-bar">'
      + '  <button class="hamburger" id="mobile-hamburger" aria-label="Menú">'
      + '    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>'
      + '  </button>'
      + '  <div class="brand-mini"><div class="brand-logo">L</div><span>Laura · Innovación</span></div>'
      + '</div>'
      + '<div class="sidebar-backdrop" id="sidebar-backdrop"></div>';
    while (mb.firstChild) document.body.insertBefore(mb.firstChild, document.body.firstChild);
  }

  // ── Auto-inyectar botón collapse si el sidebar no lo tiene ──
  var sidebars = document.querySelectorAll('.sidebar');
  sidebars.forEach(function(s){
    if (s.querySelector('.sidebar-collapse-btn')) return;
    var b = document.createElement('button');
    b.className = 'sidebar-collapse-btn';
    b.id = 'sidebar-collapse';
    b.title = 'Colapsar / Expandir';
    b.innerHTML = '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="15 18 9 12 15 6"/></svg>';
    s.style.position = s.style.position || 'sticky';
    s.appendChild(b);
    // mejor: ponerlo absolute dentro del sidebar (CSS ya define right:-12px;top:18px)
  });

  // ── Tema dark/light ──
  const saved = localStorage.getItem('theme');
  document.documentElement.setAttribute('data-theme', saved === 'dark' ? 'dark' : 'light');
  const btn = document.getElementById('theme-toggle');
  if (btn) {
    btn.addEventListener('click', () => {
      const cur = document.documentElement.getAttribute('data-theme') || 'light';
      const nxt = cur === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', nxt);
      localStorage.setItem('theme', nxt);
      const lbl = document.getElementById('theme-label');
      if (lbl) lbl.textContent = nxt === 'dark' ? 'Modo claro' : 'Modo oscuro';
      if (window._chart7d && window._chart7dData) renderChart(window._chart7dData);
    });
    const lbl = document.getElementById('theme-label');
    if (lbl) lbl.textContent =
      document.documentElement.getAttribute('data-theme') === 'dark' ? 'Modo claro' : 'Modo oscuro';
  }

  // ── Sidebar collapse (desktop) ──
  const app = document.querySelector('.app');
  if (app && localStorage.getItem('sidebar') === 'collapsed') app.classList.add('collapsed');
  const cBtn = document.getElementById('sidebar-collapse');
  if (cBtn && app) {
    cBtn.addEventListener('click', () => {
      app.classList.toggle('collapsed');
      localStorage.setItem('sidebar', app.classList.contains('collapsed') ? 'collapsed' : 'expanded');
    });
  }

  // ── Hamburger móvil ──
  const hb = document.getElementById('mobile-hamburger');
  const sb = document.querySelector('.sidebar');
  const bk = document.getElementById('sidebar-backdrop');
  function closeMobile() { if (sb) sb.classList.remove('open'); if (bk) bk.classList.remove('show'); }
  if (hb && sb) {
    hb.addEventListener('click', () => {
      sb.classList.toggle('open');
      if (bk) bk.classList.toggle('show', sb.classList.contains('open'));
    });
  }
  if (bk) bk.addEventListener('click', closeMobile);
  // Cerrar al navegar
  document.querySelectorAll('.sidebar a').forEach(a => a.addEventListener('click', () => {
    if (window.innerWidth <= 768) closeMobile();
  }));
})();
</script>
"""

# Barra superior móvil (hamburger + logo). Solo se muestra <768px vía CSS.
MOBILE_BAR_HTML = """
<div class="mobile-bar">
  <button class="hamburger" id="mobile-hamburger" aria-label="Menú">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="3" y1="6" x2="21" y2="6"/><line x1="3" y1="12" x2="21" y2="12"/><line x1="3" y1="18" x2="21" y2="18"/></svg>
  </button>
  <div class="brand-mini">
    <div class="brand-logo">L</div>
    <span>Laura · Innovación</span>
  </div>
</div>
<div class="sidebar-backdrop" id="sidebar-backdrop"></div>
"""
