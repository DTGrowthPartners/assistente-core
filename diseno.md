# 🎨 Rediseño UI — CRUD "Números Internos"

## Resumen del sistema de diseño

Inspirado en la referencia visual provista (Tabler/clean admin UI), el objetivo fue
transformar una tabla CRUD básica en una interfaz moderna, limpia y funcional sin
modificar el backend — solo con CSS y JavaScript en tiempo real.

---

## 🎨 Tokens de diseño

### Colores

| Token              | Valor      | Uso                                  |
|--------------------|------------|--------------------------------------|
| `--color-bg`       | `#f4f6f9`  | Fondo general de la página           |
| `--color-surface`  | `#ffffff`  | Tarjetas, tabla, inputs              |
| `--color-border`   | `#e5e7eb`  | Bordes de cards, tabla, inputs       |
| `--color-text-primary` | `#111827` | Títulos, nombres, valores clave   |
| `--color-text-secondary` | `#374151` | Texto de tabla general           |
| `--color-text-muted` | `#6b7280` | Razón, metadatos secundarios       |
| `--color-text-disabled` | `#9ca3af` | "Showing X of Y items", labels   |
| `--color-brand`    | `#111827`  | Botón primario (+ New), pag activa   |
| `--color-link`     | `#1d4ed8`  | Números de WhatsApp, links           |
| `--color-success`  | `#065f46`  | Badge "Activo" (texto)               |
| `--color-success-bg` | `#d1fae5` | Badge "Activo" (fondo)             |
| `--color-warning`  | `#d97706`  | Botón editar (ícono)                 |
| `--color-danger`   | `#dc2626`  | Botón eliminar (ícono)               |

### Tipografía

```css
font-family: 'Inter', sans-serif;

/* Título de página */
font-size: 22px; font-weight: 700; color: #111827; letter-spacing: -0.3px;

/* Encabezados de columna */
font-size: 11px; font-weight: 600; color: #9ca3af;
text-transform: uppercase; letter-spacing: 0.6px;

/* Cuerpo de tabla */
font-size: 13.5px; font-weight: 400; color: #374151;

/* Número WhatsApp */
font-size: 13px; font-weight: 600; color: #1d4ed8;

/* Nombre */
font-size: 13.5px; font-weight: 500; color: #111827;

/* Razón */
font-size: 12.5px; font-weight: 400; color: #6b7280;
```

### Espaciado y radios

| Elemento           | Border-radius | Padding              |
|--------------------|---------------|----------------------|
| Cards de métricas  | `12px`        | `16px 20px`          |
| Tabla principal    | `14px`        | —                    |
| Filas de tabla     | —             | `14px 16px`          |
| Botón primario     | `8px`         | `8px 16px`           |
| Botón secundario   | `8px`         | `7px 14px`           |
| Botones de acción  | `6px`         | `4px 8px`            |
| Badges de estado   | `999px`       | `4px 10px`           |
| Input de búsqueda  | `8px`         | `8px 14px`           |
| Paginación         | `7px`         | `5px 11px`           |

---

## 🧩 Componentes

### 1. Barra de métricas (Stats Bar)

Tres tarjetas horizontales encima de la tabla con KPIs del listado.

```html
<div style="display:flex; gap:16px; margin-bottom:16px;">

  <!-- Tarjeta: Total registros -->
  <div class="stat-card">
    <div class="stat-icon" style="background:#eff6ff;">
      <!-- Icono teléfono SVG, stroke #1d4ed8 -->
    </div>
    <div>
      <div class="stat-label">TOTAL REGISTROS</div>
      <div class="stat-value">17</div>
    </div>
  </div>

  <!-- Tarjeta: Activos -->
  <div class="stat-card">
    <div class="stat-icon" style="background:#d1fae5;">
      <!-- Icono check SVG, stroke #065f46 -->
    </div>
    <div>
      <div class="stat-label">ACTIVOS</div>
      <div class="stat-value" style="color:#065f46;">17</div>
    </div>
  </div>

  <!-- Tarjeta: Importados vCard -->
  <div class="stat-card">
    <div class="stat-icon" style="background:#fef3c7;">
      <!-- Icono alerta SVG, stroke #92400e -->
    </div>
    <div>
      <div class="stat-label">IMPORTADOS VCARD</div>
      <div class="stat-value" style="color:#92400e;">15</div>
    </div>
  </div>

</div>
```

```css
.stat-card {
  background: #ffffff;
  border: 1px solid #e5e7eb;
  border-radius: 12px;
  padding: 16px 20px;
  flex: 1;
  display: flex;
  align-items: center;
  gap: 12px;
}

.stat-icon {
  width: 40px;
  height: 40px;
  border-radius: 10px;
  display: flex;
  align-items: center;
  justify-content: center;
}

.stat-label {
  font-size: 11px;
  font-weight: 500;
  color: #6b7280;
  text-transform: uppercase;
  letter-spacing: 0.5px;
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
  color: #111827;
  line-height: 1.2;
}
```

---

### 2. Header del card (título + acciones)

```html
<div class="card-header">
  <h3 class="card-title">Números internos</h3>
  <div class="ms-auto d-flex gap-2">
    <button class="btn-export">Export ▾</button>
    <a href="/new" class="btn-primary">+ New Número interno</a>
  </div>
</div>
```

```css
.card-header {
  background: #ffffff;
  border-bottom: 1.5px solid #f3f4f6;
  padding: 16px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.card-title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}

.btn-export {
  background: #ffffff;
  border: 1.5px solid #e5e7eb;
  color: #374151;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  padding: 7px 14px;
}

.btn-primary {
  background: #111827;
  border: none;
  border-radius: 8px;
  color: #ffffff;
  font-size: 13px;
  font-weight: 600;
  padding: 8px 16px;
  text-decoration: none;
}
```

---

### 3. Barra de filtros / búsqueda

```html
<div class="filters-bar">
  <div class="dropdown">
    <button class="btn-actions">Actions ▾</button>
  </div>
  <div class="search-group ms-auto">
    <input type="search" placeholder="Buscar por número..." />
    <button class="btn-primary">Buscar</button>
  </div>
</div>
```

```css
.filters-bar {
  display: flex;
  align-items: center;
  padding: 12px 20px;
  gap: 12px;
}

input[type="search"] {
  border: 1.5px solid #e5e7eb;
  border-radius: 8px;
  padding: 8px 14px;
  font-size: 13px;
  min-width: 220px;
  background: #ffffff;
  transition: border-color 0.2s;
}

input[type="search"]:focus {
  border-color: #1d4ed8;
  box-shadow: 0 0 0 3px rgba(29, 78, 216, 0.08);
  outline: none;
}
```

---

### 4. Tabla de datos

```css
/* Encabezado */
table thead th {
  background: #f9fafb;
  color: #9ca3af;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.6px;
  text-transform: uppercase;
  border-bottom: 1px solid #e5e7eb;
  padding: 12px 16px;
}

/* Filas */
table tbody tr {
  border-bottom: 1px solid #f3f4f6;
  transition: background 0.15s ease;
}

table tbody tr:hover {
  background: #f9fafb;
}

table tbody td {
  padding: 14px 16px;
  vertical-align: middle;
  border: none;
  border-bottom: 1px solid #f3f4f6;
}

/* Columna WhatsApp */
td.col-whatsapp {
  font-weight: 600;
  color: #1d4ed8;
  font-size: 13px;
  font-family: 'Inter', monospace;
}

/* Columna Nombre */
td.col-nombre {
  font-weight: 500;
  color: #111827;
  font-size: 13.5px;
}

/* Columna Razón */
td.col-razon {
  color: #6b7280;
  font-size: 12.5px;
  overflow: hidden;
  text-overflow: ellipsis;
}
```

---

### 5. Botones de acción (Ver / Editar / Eliminar)

Cada acción tiene su propio color semántico con fondo suave.

```html
<td class="col-acciones">
  <a href="/show" class="btn-action btn-view">👁</a>
  <a href="/edit" class="btn-action btn-edit">✏️</a>
  <a href="/delete" class="btn-action btn-delete">🗑</a>
</td>
```

```css
.btn-action {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  text-decoration: none;
  margin: 0 2px;
  transition: background 0.15s;
}

/* Ver */
.btn-view  { background: #eff6ff; color: #1d4ed8; }
.btn-view:hover { background: #dbeafe; }

/* Editar */
.btn-edit  { background: #fffbeb; color: #d97706; }
.btn-edit:hover { background: #fef3c7; }

/* Eliminar */
.btn-delete { background: #fef2f2; color: #dc2626; }
.btn-delete:hover { background: #fee2e2; }
```

---

### 6. Badge de estado

```html
<!-- Activo -->
<span class="badge badge-active">
  <span class="badge-dot"></span>
  Activo
</span>

<!-- Inactivo -->
<span class="badge badge-inactive">
  <span class="badge-dot"></span>
  Inactivo
</span>
```

```css
.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  font-size: 11.5px;
  font-weight: 600;
  padding: 4px 10px;
  border-radius: 999px;
  letter-spacing: 0.1px;
}

.badge-dot {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  display: inline-block;
}

.badge-active  { background: #d1fae5; color: #065f46; }
.badge-active .badge-dot { background: #10b981; }

.badge-inactive { background: #f3f4f6; color: #9ca3af; }
.badge-inactive .badge-dot { background: #d1d5db; }
```

---

### 7. Footer con paginación

```html
<div class="card-footer">
  <p class="showing-info">Showing 1 to 10 of 17 items</p>
  <div class="pagination-controls">
    <a class="page-link disabled">‹ prev</a>
    <a class="page-link active">1</a>
    <a class="page-link">2</a>
    <a class="page-link">next ›</a>
    <span>Show</span>
    <select class="per-page-select">
      <option>10 / Page</option>
      <option>25 / Page</option>
    </select>
  </div>
</div>
```

```css
.card-footer {
  background: #fafafa;
  border-top: 1px solid #f3f4f6;
  padding: 14px 20px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}

.showing-info {
  font-size: 12.5px;
  color: #9ca3af;
  margin: 0;
}

.page-link {
  border-radius: 7px;
  margin: 0 2px;
  border: 1.5px solid #e5e7eb;
  background: #ffffff;
  color: #374151;
  font-size: 13px;
  font-weight: 500;
  padding: 5px 11px;
  text-decoration: none;
  display: inline-flex;
  align-items: center;
  transition: all 0.15s;
}

.page-link.active {
  background: #111827;
  border-color: #111827;
  color: #ffffff;
  font-weight: 600;
}

.page-link.disabled {
  color: #d1d5db;
  pointer-events: none;
}

.per-page-select {
  border-radius: 7px;
  border: 1.5px solid #e5e7eb;
  font-size: 12.5px;
  color: #374151;
  padding: 5px 10px;
  background: #ffffff;
}
```

---

### 8. Sidebar de navegación

```css
.navbar-vertical {
  background: #1a1f2e;
  width: 240px;
  min-height: 100vh;
}

/* Secciones (Configuración, Operación...) */
.nav-category {
  font-size: 10px;
  font-weight: 600;
  color: rgba(255, 255, 255, 0.35);
  text-transform: uppercase;
  letter-spacing: 0.8px;
  padding: 12px 16px 4px;
}

/* Links de navegación */
.nav-link {
  font-size: 13px;
  font-weight: 400;
  color: rgba(255, 255, 255, 0.65);
  padding: 8px 14px;
  border-radius: 6px;
  margin: 1px 6px;
  display: flex;
  align-items: center;
  gap: 8px;
  transition: all 0.15s;
}

/* Link activo */
.nav-link.active {
  color: #ffffff;
  font-weight: 600;
  background: rgba(255, 255, 255, 0.1);
}

/* Logout */
.btn-logout {
  background: rgba(255, 255, 255, 0.1);
  color: #ffffff;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  padding: 8px 16px;
  margin: 8px;
}
```

---

## 📐 Layout general