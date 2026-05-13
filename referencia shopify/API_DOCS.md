# API Chancletas - Shopify

Base URL: `http://localhost:3002`

## Autenticación

Todas las rutas `/api/*` requieren el header:
```
x-api-key: TU_API_KEY_AQUI
```

## Endpoints

### Órdenes

#### `GET /api/orders`
Lista de órdenes con filtros opcionales.

| Parámetro | Tipo | Descripción |
|---|---|---|
| `status` | string | `any`, `open`, `closed`, `cancelled` (default: `any`) |
| `limit` | number | Máximo de resultados (default: 50, max: 250) |
| `created_at_min` | string | Fecha mínima ISO 8601 (ej: `2026-01-01T00:00:00-05:00`) |
| `created_at_max` | string | Fecha máxima ISO 8601 |
| `financial_status` | string | `paid`, `pending`, `refunded`, `partially_paid` |

Ejemplo: `GET /api/orders?status=open&limit=10&created_at_min=2026-03-01`

#### `GET /api/orders/summary`
Resumen de ventas con top 10 productos más vendidos.

| Parámetro | Tipo | Descripción |
|---|---|---|
| `created_at_min` | string | Fecha mínima para filtrar |
| `created_at_max` | string | Fecha máxima para filtrar |
| `financial_status` | string | Filtrar por estado de pago |

Respuesta:
```json
{
  "summary": {
    "total_orders": 250,
    "total_revenue": "39784100.00",
    "average_order_value": "159136.40",
    "currency": "COP",
    "paid_orders": 235,
    "pending_orders": 7,
    "refunded_orders": 0
  },
  "top_products": [
    { "name": "Producto ejemplo", "quantity": 48, "revenue": 6780000 }
  ]
}
```

#### `GET /api/orders/:id`
Detalle completo de una orden por su ID.

---

### Productos

#### `GET /api/products`
Lista de productos.

| Parámetro | Tipo | Descripción |
|---|---|---|
| `limit` | number | Máximo de resultados (default: 50) |
| `collection_id` | string | Filtrar por colección |
| `product_type` | string | Filtrar por tipo de producto |
| `status` | string | `active`, `archived`, `draft` |

#### `GET /api/products/:id`
Detalle completo de un producto por su ID (incluye variantes, precios, imágenes).

---

### Inventario

#### `GET /api/inventory`
Niveles de inventario por ubicación y producto.

---

### Health Check

#### `GET /health`
No requiere autenticación. Responde `{ "status": "ok" }`.

## Moneda

Todos los valores monetarios están en **COP** (Pesos Colombianos).

## Iniciar el servidor

```bash
npm install
npm start
```

El servidor corre en el puerto 3002 (configurable con `PORT` en `.env`).
