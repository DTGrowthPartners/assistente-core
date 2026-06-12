# API externa del bot Dairo

> Integración para plataformas terceras (panel admin de bots, monitores Meta,
> CRMs externos). Permite **enviar mensajes**, **consultar estado**,
> **activar/desactivar el bot remotamente** y **recibir webhooks** cuando
> ocurren eventos relevantes.

**Base URL producción**: `https://david.dtgrowthpartners.com`
**Auth**: header `X-API-Key: <tu-key>` en todos los endpoints (excepto `/health`).
**Encoding**: siempre `Content-Type: application/json; charset=utf-8`.

---

## Tabla de contenidos

1. [Autenticación](#1-autenticación)
2. [Endpoints](#2-endpoints)
   - [GET /health](#21-get-health)
   - [POST /enviar](#22-post-enviar)
   - [GET /estado](#23-get-estado)
   - [POST /estado](#24-post-estado)
   - [GET /stats](#25-get-stats)
3. [Webhooks salientes (bot → tu plataforma)](#3-webhooks-salientes)
4. [Códigos de error](#4-códigos-de-error)
5. [Ejemplos de integración](#5-ejemplos-de-integración)

---

## 1. Autenticación

Todos los endpoints (excepto `/health`) requieren:

```
X-API-Key: <API_EXTERNO_KEY>
```

La key se genera en el servidor del bot con `openssl rand -hex 32` y se
guarda en `.env`. Pídesela al admin del bot (Stiven).

Respuesta si falta o es inválida:
```json
HTTP 401
{ "detail": "unauthorized" }
```

Respuesta si la key no está configurada en el bot:
```json
HTTP 503
{ "detail": "API_EXTERNO_KEY no configurada" }
```

---

## 2. Endpoints

### 2.1 GET /health

Healthcheck público (sin auth). Útil para uptime checks.

```bash
curl https://david.dtgrowthpartners.com/api/externo/health
```

Respuesta:
```json
{
  "ok": true,
  "service": "bot-dairo",
  "env": "production",
  "ts": "2026-06-10T18:00:00-05:00"
}
```

---

### 2.2 POST /enviar

Envía un mensaje WhatsApp a un número o grupo vía whapi.

**Body**:

| Campo | Tipo | Requerido | Descripción |
|---|---|---|---|
| `destino` | string | ✅ | Número (`+573001234567`), grupo (`120363xxx@g.us`), o JID whapi (`573001234567@s.whatsapp.net`) |
| `mensaje` | string | ✅ | Texto del mensaje (≤ 4000 chars) |
| `origen` | string | — | Identificador del sistema emisor (solo logs). Default `"externo"` |

**Ejemplo**:
```bash
curl -X POST https://david.dtgrowthpartners.com/api/externo/enviar \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-API-Key: <KEY>" \
  -d '{
    "destino": "120363422490459440@g.us",
    "mensaje": "⚠️ Cuenta Meta venció — renovar antes de hoy",
    "origen": "panel-admin"
  }'
```

Respuesta:
```json
{ "ok": true, "whapi_id": "Psot...wmcBq", "destino": "120363422490459440@g.us" }
```

---

### 2.3 GET /estado

Devuelve el estado actual del bot.

```bash
curl -H "X-API-Key: <KEY>" \
  https://david.dtgrowthpartners.com/api/externo/estado
```

Respuesta:
```json
{
  "ok": true,
  "ts": "2026-06-10T18:00:00-05:00",
  "estado": {
    "activo": true,
    "modo": "todos",
    "pausado_por": null,
    "pausado_en": null,
    "razon": null,
    "actualizado_en": "2026-06-10T15:30:00+00:00"
  },
  "trafico_24h": {
    "inbound": 142,
    "outbound": 137
  }
}
```

**Campos clave**:
- `activo`: `true` si el bot responde mensajes
- `modo`: `"todos"` | `"solo_prospectos"` | `"off"`
  - `todos` — responde a equipo + prospectos + clientes WL
  - `solo_prospectos` — solo prospectos; clientes silenciados
  - `off` — solo equipo; resto silenciado
- `pausado_por` / `razon` — quién y por qué pausó (si está pausado)

---

### 2.4 POST /estado

Cambia el estado del bot. Hay tres formas de usarlo:

**A) Toggle simple (on/off)**:
```json
{ "activo": false, "razon": "Mantenimiento del servidor", "por": "panel-admin" }
```

**B) Cambiar modo** (implica activo según el modo):
```json
{ "modo": "solo_prospectos", "razon": "Pausa atención a clientes WL" }
```

**C) Apagar todo**:
```json
{ "modo": "off", "razon": "Incidente — investigando" }
```

| Campo | Tipo | Notas |
|---|---|---|
| `activo` | bool | true = activar / false = pausar |
| `modo` | string | `"todos"` \| `"solo_prospectos"` \| `"off"` |
| `razon` | string | Por qué (queda registrado, max 240 chars) |
| `por` | string | Quién hizo el cambio (max 80 chars) |

Reglas:
- `modo=off` ⇒ `activo=false` automáticamente.
- `modo=todos` ⇒ `activo=true` automáticamente.
- `modo=solo_prospectos` respeta `activo` si lo mandás.
- Si solo mandás `activo`, el `modo` actual se mantiene.

**Ejemplo**:
```bash
curl -X POST https://david.dtgrowthpartners.com/api/externo/estado \
  -H "Content-Type: application/json; charset=utf-8" \
  -H "X-API-Key: <KEY>" \
  -d '{ "activo": false, "razon": "Mantenimiento", "por": "panel-admin" }'
```

Respuesta:
```json
{
  "ok": true,
  "estado": {
    "activo": false,
    "modo": "todos",
    "pausado_por": "panel-admin",
    "pausado_en": "2026-06-10T23:00:00+00:00",
    "razon": "Mantenimiento",
    "actualizado_en": "2026-06-10T23:00:00+00:00"
  }
}
```

> ⚡ **El cambio surte efecto inmediato.** El cache interno se invalida en cuanto
> recibe el POST, no hay ventana de 5s.

---

### 2.5 GET /stats

Stats agregadas para mostrar en el panel admin.

```bash
curl -H "X-API-Key: <KEY>" \
  https://david.dtgrowthpartners.com/api/externo/stats
```

Respuesta (resumida):
```json
{
  "ok": true,
  "ts": "2026-06-10T18:00:00-05:00",
  "tz": "America/Bogota",
  "estado": { "activo": true, "modo": "todos", ... },
  "conversaciones": {
    "inbound_hoy":  142,
    "outbound_hoy": 137,
    "inbound_mes":  3210,
    "outbound_mes": 3104,
    "clientes_unicos_hoy": 38,
    "clientes_unicos_mes": 412
  },
  "prospectos": {
    "nuevos":      12,
    "calificando": 7,
    "agendados":   3,
    "no_fit":      18,
    "clientes":    24,
    "total":       64
  },
  "citas": {
    "activas":     5,
    "completadas": 32,
    "canceladas":  4,
    "hoy":         2,
    "mes":         18,
    "total":       72
  },
  "claude_hoy": {
    "tokens_input":  45120,
    "tokens_output": 12380,
    "cache_read":    320000,
    "cache_write":   8500
  },
  "alertas_abiertas": 2
}
```

Todas las cuentas son acumulados desde la medianoche de Bogotá hasta `ts`.

---

## 3. Webhooks salientes

El bot puede notificar a tu plataforma cuando ocurren eventos. Para activar:

**1) Configurar en el `.env` del bot**:
```
PANEL_ADMIN_WEBHOOK_URL=https://tu-panel.com/api/bots/dairo/events
PANEL_ADMIN_WEBHOOK_SECRET=<opcional, openssl rand -hex 32>
```

**2) Tu plataforma recibe POSTs**:
```
POST https://tu-panel.com/api/bots/dairo/events
Content-Type: application/json; charset=utf-8
X-Bot-Source: dairo-bot
X-Bot-Signature: <hmac-sha256(body, secret)>   ← solo si configuraste secret
```

**Body**:
```json
{
  "event": "bot.estado_cambiado",
  "ts": "2026-06-10T18:00:00-05:00",
  "bot": "dairo-bot",
  "data": { ... }
}
```

### Eventos emitidos

#### `bot.estado_cambiado`
Cuando alguien (dashboard interno, API externa, tool del bot) cambia el
estado on/off o el modo.

```json
"data": {
  "activo": true,
  "modo": "todos",
  "por": "panel-admin",
  "razon": "Reactivado tras mantenimiento"
}
```

#### `bot.cita_agendada`
Cuando el bot completa una cita en Cal.com con un prospecto.

```json
"data": {
  "cliente_id": 4523,
  "cliente_numero": "+573001234567",
  "nombre": "Carlos Pérez",
  "email": "carlos@empresa.co",
  "negocio": "Empresa XYZ",
  "fecha_inicio": "2026-06-15T10:00:00-05:00",
  "calcom_uid": "abc123"
}
```

#### `bot.alerta_abierta`
Cuando se registra una alerta crítica (fallo de Claude, problema de
integración, prospecto en riesgo, etc.).

```json
"data": {
  "alerta_id": 87,
  "tipo": "claude_api_fail",
  "mensaje": "Falló Claude API atendiendo a +573...",
  "cliente_id": 4523
}
```

### Verificar la firma HMAC

Si configuraste `PANEL_ADMIN_WEBHOOK_SECRET`, todos los webhooks vienen con
`X-Bot-Signature`. Tu plataforma debe verificarla:

```python
import hmac, hashlib
def verificar(body_bytes, signature_header, secret):
    expected = hmac.new(secret.encode(), body_bytes, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature_header)
```

```js
import crypto from 'node:crypto';
function verificar(bodyRaw, signatureHeader, secret) {
  const expected = crypto.createHmac('sha256', secret).update(bodyRaw).digest('hex');
  return crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(signatureHeader));
}
```

### Garantías de los webhooks

- **Async, no-bloqueante**: el bot no espera tu respuesta para seguir operando.
- **Best-effort**: si tu URL falla, se loggea pero NO se reintenta. Si tu
  plataforma estuvo caída, esos eventos se pierden. Si necesitas ack/retry,
  combinalo con polling de `GET /stats` cada N minutos.
- **Sin orden estricto**: los eventos llegan ASAP, pero pueden llegar fuera
  de orden si tu endpoint tiene latencia variable.
- **Sin dedup**: tu plataforma debe ser idempotente. Cada evento incluye
  identificadores únicos (alerta_id, calcom_uid) que podés usar como clave.

---

## 4. Códigos de error

| Status | Significado | Qué hacer |
|---|---|---|
| 200 | OK | ✅ |
| 400 | Body malformado o campo inválido | Revisar el JSON / campos requeridos |
| 401 | API key faltante o inválida | Confirmar `X-API-Key` |
| 500 | Error interno del bot | Reintentar con backoff; si persiste, avisar a Stiven |
| 502 | whapi rechazó el mensaje (solo en `/enviar`) | Token whapi caído / número bloqueado |
| 503 | El bot no terminó de configurarse (sin API key en .env) | Contactar admin del bot |

---

## 5. Ejemplos de integración

### Python — pausar el bot desde tu panel
```python
import requests

requests.post(
    "https://david.dtgrowthpartners.com/api/externo/estado",
    headers={
        "Content-Type": "application/json; charset=utf-8",
        "X-API-Key": API_KEY,
    },
    json={"activo": False, "razon": "Mantenimiento programado", "por": "panel-admin"},
).raise_for_status()
```

### Node.js — pollear stats cada 60s
```js
const STATS_URL = "https://david.dtgrowthpartners.com/api/externo/stats";

setInterval(async () => {
  const res = await fetch(STATS_URL, { headers: { "X-API-Key": API_KEY } });
  if (!res.ok) return console.error("stats failed", res.status);
  const data = await res.json();
  actualizarDashboard(data);
}, 60_000);
```

### Recibir webhooks (Express)
```js
import express from "express";
import crypto from "node:crypto";
const app = express();

app.post("/api/bots/dairo/events", express.raw({ type: "*/*" }), (req, res) => {
  // Validar firma (opcional)
  const sig = req.header("X-Bot-Signature");
  if (sig) {
    const expected = crypto.createHmac("sha256", WEBHOOK_SECRET)
      .update(req.body).digest("hex");
    if (!crypto.timingSafeEqual(Buffer.from(expected), Buffer.from(sig))) {
      return res.status(401).end();
    }
  }
  const event = JSON.parse(req.body);
  switch (event.event) {
    case "bot.estado_cambiado": registrarCambio(event.data); break;
    case "bot.cita_agendada":   guardarCita(event.data); break;
    case "bot.alerta_abierta":  abrirIncidente(event.data); break;
  }
  res.json({ ok: true });
});
```

### curl — toggle rápido del bot
```bash
# Pausar
curl -X POST https://david.dtgrowthpartners.com/api/externo/estado \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json; charset=utf-8" \
  -d '{"activo":false,"razon":"test","por":"manual"}'

# Reactivar
curl -X POST https://david.dtgrowthpartners.com/api/externo/estado \
  -H "X-API-Key: $KEY" -H "Content-Type: application/json; charset=utf-8" \
  -d '{"activo":true,"por":"manual"}'
```

---

## Resumen

- Para **pausar/activar** desde tu panel: `POST /estado`
- Para **ver stats** en vivo: `GET /stats` (pollear cada 30-60s)
- Para **enviar mensajes**: `POST /enviar`
- Para **reaccionar a eventos en tiempo real**: configurar `PANEL_ADMIN_WEBHOOK_URL` y recibir webhooks salientes
- Para **uptime**: `GET /health` (sin auth)

Cualquier duda o feature nueva, contactar a Stiven (dev@dtgrowthpartners.com).
