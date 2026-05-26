# GUIA — Bot Asistente (Laura)

Documentación interna del sistema construido para Innovación Fashion Outlet. Pensada para ser la base al clonar este mismo bot a otro vertical (SDR, estética, clínica, etc.). Léela completa antes de empezar uno nuevo.

> Última actualización: 2026-05-26 · Versión funcional: producción Innovación

---

## 1. Qué hace este sistema

Un bot de WhatsApp con inteligencia artificial (Claude Sonnet 4.6) que:

- Atiende clientes 24/7 con tono humano (sin emojis, frases cortas, cálida)
- Consulta catálogo (Shopify + scraping HTML), envía fotos de producto, cotiza envíos
- Toma pedidos (manuales y vía draft order Shopify), persiste todo en Postgres
- Escala al equipo (Stiven, Fabio, Roxana) por WhatsApp cuando detecta queja, comprobante, ref desconocida
- Permite al admin "hablarle" desde su propio WhatsApp para dar instrucciones (responder al cliente, confirmar pedido, marcar interno, etc.)
- Tiene panel web admin (chats, pedidos, alertas, estados, tareas programadas, configuración)
- Publica estados de WhatsApp automático (cron 2/día) y manual
- Maneja pausas (bot global, por cliente, automática por queja)
- Imita comportamiento humano (typing indicator, delay 60-180s, rate limit 30 msg/h)

**Lo que NO hace:**
- No cobra automáticamente (esperando pasarela; hoy verifica comprobantes vía admin)
- No tiene buffer de mensajes en ráfaga (cada mensaje del cliente entra serializado al lock)
- No agenda citas/reuniones (eso es del próximo vertical)
- No tiene multi-tenant (cada cliente = 1 deploy)

---

## 2. Stack técnico

| Pieza | Tech | Notas |
|---|---|---|
| Backend API | FastAPI + uvicorn | Async, async_session_factory de SQLAlchemy 2.0 |
| BD | Postgres 15 | asyncpg + psycopg2 (sync para algunas tareas) |
| ORM | SQLAlchemy 2.0 | `app/db/models.py` |
| LLM | Claude Sonnet 4.6 + Haiku 4.5 | Anthropic Python SDK |
| Provider LLM | Dario (proxy local) | Fallback automático a API directa si falla |
| WhatsApp | whapi.cloud | 1 número por cliente, token único, webhooks HTTPS |
| Catálogo público | Scraping HTML + products.json Shopify | Cron 12h |
| Draft orders | API intermedia → Shopify Admin API | Token vence cada 12h, se auto-renueva |
| Admin panel | SQLAdmin + vistas custom HTML | CSS shadcn-inspired |
| Auth admin | Cookie session + SessionMiddleware | Usuario/pass en .env |
| Cron / scheduling | systemd user timers + scheduler interno async | Stories systemd, tareas programadas BD-driven |
| Logs | structlog | Salida a `/home/asistente/logs/asistente.log` |
| Deploy | systemd user service en VPS Ubuntu 24.04 | No containerizado |
| Source control | git + GitHub | `santequera1/asistente1`, branch `main` |

**Servidor:**
- VPS Ubuntu 24.04, usuario `asistente` (no root)
- Sin Docker — todo corre como user systemd
- Nginx delante para HTTPS (no documentado aquí, ya configurado)
- Dominio: `asistente.dtgrowthpartners.com`

---

## 3. Estructura de directorios

```
bot_asistente/
├── app/
│   ├── main.py                    # FastAPI app, lifespan, webhook routing
│   ├── config.py                  # Settings (env vars, defaults)
│   ├── logging_setup.py
│   │
│   ├── claude/                    # ⭐ Cerebro del bot
│   │   ├── client.py              # Wrapper Anthropic + tool use loop
│   │   ├── anthropic_client.py    # Wrapper Dario→fallback API directa
│   │   ├── intent.py              # Clasificador Haiku (saludo, queja, etc.)
│   │   ├── prompts.py             # ⭐ SYSTEM_PROMPT_CLIENTE + SYSTEM_PROMPT_EQUIPO
│   │   ├── tools.py               # ⭐ Tools cliente-facing (buscar, escalar, pedido)
│   │   └── tools_equipo.py        # ⭐ Tools admin-facing (responder, marcar, etc.)
│   │
│   ├── flows/
│   │   ├── conversation.py        # Flujo principal cliente
│   │   └── equipo.py              # Flujo cuando admin escribe al bot
│   │
│   ├── whapi/
│   │   ├── client.py              # enviar_texto, enviar_imagen, etc.
│   │   └── parser.py              # MensajeWhapi + parsing del webhook
│   │
│   ├── shopify/
│   │   └── client.py              # fetch_productos_publicos, crear_draft_order
│   │
│   ├── db/
│   │   ├── models.py              # Todas las tablas
│   │   ├── repos.py               # Helpers de acceso (get_or_create_cliente, etc.)
│   │   └── session.py             # async_session_factory + engine
│   │
│   ├── equipo/
│   │   └── directorio.py          # Caché 30s de admins + internos
│   │
│   ├── utils/
│   │   ├── humanizer.py           # Delay 60-180s + typing indicator
│   │   └── horarios.py            # Ventana horaria de atención
│   │
│   ├── validators/
│   │   └── output_rules.py        # Validador post-Claude (emojis, precios, etc.)
│   │
│   ├── automatizaciones/          # ⭐ Tareas programadas (cron BD-driven)
│   │   ├── scheduler.py           # Loop async cada 60s
│   │   └── acciones.py            # Acciones predefinidas (reporte, alertas, etc.)
│   │
│   └── admin/                     # UI web admin
│       ├── _shell.py              # Sidebar + SHELL_STYLES compartidos
│       ├── dashboard.py           # Vista principal
│       ├── chats.py               # Lista chats + hilo conversación
│       ├── stories.py             # Estados WhatsApp
│       ├── automatizaciones.py    # CRUD tareas programadas
│       ├── actions.py             # POST endpoints (pausar, marcar interno, etc.)
│       ├── views.py               # Vistas SQLAdmin auto-generadas
│       └── static/                # custom.css, fonts
│
├── scripts/                       # Scripts standalone (cron systemd o manuales)
│   ├── seed_catalogo.py
│   ├── seed_catalogo_html.py      # ⭐ Scraper página WhatsApp con 5 estrategias
│   ├── seed_catalogo_publico.py   # Sync desde products.json Shopify
│   ├── seed_tarifas.py            # Bulk insert tarifas desde Excel
│   ├── publicar_story_diario.py   # Disparo de stories desde cron systemd
│   └── import_clientes_contactos.py  # Bulk import contactos (vCard/CSV/XLSX)
│
├── data/                          # Datos seed (CSV, JSON, imágenes bancos)
├── alembic/                       # Migraciones (poco usadas, casi todo con SQL directo)
├── .env                           # Secretos (NO commitear)
└── requirements.txt
```

**Las piezas marcadas con ⭐ son las que más editas por cliente nuevo.**

---

## 4. Flujos principales

### 4.1 Webhook de cliente → respuesta

```
Cliente escribe en WhatsApp
   ↓
whapi.cloud POSTea a https://asistente.dtgrowthpartners.com/webhook
   ↓
main.py: webhook()
   ├── 1. Dedupe por message_id (tabla webhook_procesados)
   ├── 2. Parse payload con whapi/parser.py → MensajeWhapi
   └── 3. Para cada mensaje, routing en orden:
       │
       ├─ is_from_bot → ignorar (eco propio)
       ├─ chat_id @g.us → ignorar (mensaje de grupo)
       ├─ es_miembro_equipo → _procesar_equipo_async
       ├─ es_numero_interno → ignorar
       ├─ from_number == dueno_phone_blocked → alerta dedupada
       ├─ bot_global pausado → solo persistir
       ├─ cliente bloqueado o pausa humana → solo persistir
       └─ default → _procesar_async (cliente normal)
            ↓
       lock_por_cliente
            ↓
       procesar_mensaje_inbound (flows/conversation.py)
            ├── Cargar historial (30 msgs / 48h max)
            ├── Clasificar intent (Haiku, ~$0.0001)
            ├── Si imagen: descargar bytes para visión
            ├── construir_contexto_cliente() → datos cliente + pedido + alertas
            ├── conversar() → CLAUDE TOOL USE LOOP (hasta 5 rondas)
            ├── _validar_y_reescribir_si_necesario()
            ├── Humanizar (typing + delay 60-180s)
            ├── enviar_texto vía whapi
            └── Persistir outbound
            ↓
       commit
            ↓
       _drain_outbox(outbox) → mensajes a admins (Fabio/Stiven) salen DESPUÉS del commit
```

**Decisiones clave:**

- **Lock por cliente_id**: serializa mensajes del mismo cliente. Sin esto, 3 mensajes en ráfaga generan 3 turns concurrentes que se pisan en BD.
- **Outbox pattern**: las notificaciones al equipo NO salen dentro de la transacción. Se acumulan en `ctx["outbox"]` y se drenan tras commit. Si la transacción hace rollback, no quedan alertas huérfanas a Fabio.
- **Humanizer**: delay 60-180s + typing indicator. Anti-detección Meta. Rate limit global 30 msg/h.

### 4.2 Tool use loop de Claude

```python
messages = [historial..., último_mensaje]
for ronda in range(5):
    resp = await claude.messages.create(
        system=[prompt_base_cacheado, contexto_dinamico_no_cacheado],
        tools=TOOL_DEFINITIONS,
        messages=messages,
    )
    if resp.stop_reason != "tool_use":
        return resp.text       # ya respondió → enviar al cliente
    # Hay tools que ejecutar
    messages.append({"role": "assistant", "content": tool_uses})
    tool_results = []
    for tool in resp.tool_uses:
        result = await ejecutar_tool(tool.name, tool.input, ctx)
        tool_results.append({"tool_use_id": tool.id, "content": result})
    messages.append({"role": "user", "content": tool_results})
```

**Prompt caching:** system_prompt cacheado con `cache_control: ephemeral`. Cache_read típico 38000+ tokens → costo ~10x menor por turn.

**Tools cliente** (`app/claude/tools.py`):
- `buscar_productos(categoria, talla, color, texto_libre, max_resultados)`
- `enviar_imagen_producto(ref, incluir_caption)` — manda foto al cliente
- `enviar_imagen_banco(banco)` — manda imagen + datos cuenta
- `cotizar_envio(barrio)` — busca en tarifas_domicilio
- `tomar_pedido_manual(items, nombre, cedula, email, ciudad, barrio, direccion, metodo_pago, subtotal, domicilio, total)`
- `crear_draft_order(items, nombre_cliente)` — link pago Shopify
- `escalar_a_equipo(tipo, mensaje, area, media_url)` — alerta a admins
- `programar_seguimiento(horas, razon)` — recordatorio interno

**Tools equipo** (`app/claude/tools_equipo.py`):
- `responder_a_cliente(numero, mensaje, pausar_chat=False)` — envía mensaje al cliente desde el admin
- `enviar_foto_producto_a_cliente(numero, ref, caption, pausar_chat=False)`
- `actualizar_pedido(pedido_id, estado, notas)` — notifica al grupo si pasa a confirmado
- `marcar_alerta_resuelta(alerta_id)`
- `crear_pedido_manual(numero, items, total, ...)` — registra venta cerrada conversacionalmente
- `marcar_numero_interno(numero, nombre, razon)` — agrega a internos + pausa 24h
- `consultar_chat_cliente(numero o nombre_parcial, max_mensajes)`
- `consultar_chats_sin_responder(max_resultados, horas_max)`
- `consultar_pedidos`, `consultar_alertas_abiertas`, `consultar_cliente`, `consultar_producto`, `consultar_equipo`
- `pausar_bot_global`, `reanudar_bot_global`, `consultar_estado_bot`

### 4.3 Flujo del admin

```
Admin escribe a número del bot
   ↓
es_miembro_equipo() → True
   ↓
_procesar_equipo_async → flows/equipo.py
   ├── Persistir inbound (cliente_proxy con nombre "[ADMIN] X")
   ├── Si imagen: descargar para Claude visión
   ├── Cargar historial últimos 12 turns admin↔bot
   ├── Construir contexto (alertas + pedidos recientes)
   ├── claude.messages.create(SYSTEM_PROMPT_EQUIPO, tools_equipo, ...)
   ├── Tool use loop (5 rondas máx)
   ├── Enviar respuesta (sin humanización, inmediato)
   └── Persistir outbound
```

### 4.4 Scheduler de automatizaciones

```python
# app/automatizaciones/scheduler.py
async def _loop():
    while running:
        rows = SELECT FROM tareas_programadas WHERE activo AND proxima_ejecucion <= now()
        for tarea in rows:
            await ejecutar_accion(tarea.accion, session, tarea.parametros)
            UPDATE tarea SET ultima_ejecucion=now(),
                              proxima_ejecucion=croniter(cron, now()),
                              ultimo_resultado=...
        await asyncio.sleep(60)
```

Acciones en `app/automatizaciones/acciones.py`: registry `ACCIONES_DISPONIBLES` con handler + descripción + parámetros esperados. Hoy: `reporte_ventas`, `recordatorio_alertas`, `reengagement`, `mensaje_custom`.

---

## 5. Modelo de datos (tablas principales)

| Tabla | Propósito |
|---|---|
| `clientes` | Cliente que escribe. PK id, único por numero_whatsapp. Incluye nombre, ciudad, barrio, cedula, email, bloqueado, ultimo_contacto, metadata JSONB |
| `conversaciones` | Cada mensaje (inbound/outbound/humano). Incluye direccion, tipo, contenido, media_url, intent, tokens, modelo, metadata JSONB, whapi_message_id |
| `pedidos` | items JSONB, subtotal, domicilio, total, estado, direccion, ciudad, barrio, metodo_pago, cedula, email, comprobante_url, shopify_draft_order_id, notificado_grupo_en |
| `alertas_fabio` | Alertas escaladas al equipo. tipo (CHECK constraint), mensaje, resuelto, cliente_id |
| `intervencion_humana` | Pausas por cliente. cliente_id (único), pausado_hasta, razon |
| `bot_estado` | Singleton id=1. activo bool, pausado_por, razon (pausa global) |
| `equipo_miembros` | Admins activos (Stiven, Fabio, Roxana). areas JSONB, es_fallback, horarios |
| `numeros_internos` | Números que el bot ignora (bodegas, asesoras, sistemas) |
| `productos_cache` | Catálogo cacheado. ref (PK), nombre, precio_detal, tallas, colores, variants JSONB, imagen_url, origen (shopify/html_catalogo), activo |
| `tarifas_domicilio` | Barrio → precio. zona, tipo, precio |
| `sesiones` | Estado por cliente: productos_mostrados, ultima_interaccion, metodo_pago_elegido, estado_pedido |
| `webhook_procesados` | Dedupe por message_id |
| `story_publicado` | Histórico de estados WhatsApp publicados |
| `tareas_programadas` | Cron jobs editables (sistema de automatizaciones) |

**Foreign keys principales:** `conversaciones.cliente_id → clientes.id`, `pedidos.cliente_id → clientes.id`, `alertas_fabio.cliente_id → clientes.id`.

**Migraciones:** Alembic está configurado pero la mayoría de los `ALTER TABLE` se han aplicado vía SQL directo en producción. Documentar siempre en el código fuente lo que la BD tiene.

---

## 6. Lo reusable (el "core" — NO tocar al clonar)

Estas piezas son agnósticas del vertical. Funcionan igual para tienda, estética o psicólogos:

| Carpeta/archivo | Por qué reusable |
|---|---|
| `app/main.py` (webhook routing, lifespan, middlewares) | Mismo flujo para cualquier bot |
| `app/whapi/` (cliente + parser) | API whapi es la misma |
| `app/claude/anthropic_client.py` (wrapper Dario fallback) | Misma estrategia para todos |
| `app/claude/client.py` (tool use loop) | Loop genérico, recibe tools como parámetro |
| `app/claude/intent.py` (clasificador) | Labels reusables; quizás algunos labels cambien |
| `app/db/session.py`, `app/db/repos.py` (get_or_create_cliente, etc.) | Helpers de BD genéricos |
| `app/equipo/directorio.py` (cache 30s admins/internos) | Mismo concepto siempre |
| `app/utils/humanizer.py` | Anti-detección Meta es universal |
| `app/utils/horarios.py` | Ventana horaria configurable por env |
| `app/automatizaciones/` (scheduler + acciones base) | Sistema completo de cron BD-driven |
| `app/admin/_shell.py` (sidebar, theme, layout) | UI reusable |
| `app/admin/dashboard.py` (cards genéricas) | Ajustar métricas por vertical, layout reusable |
| `app/admin/chats.py` (lista + hilo) | Funcional para cualquier bot WhatsApp |
| `app/admin/actions.py` (pausar, marcar interno, etc.) | Acciones admin genéricas |
| `app/admin/automatizaciones.py` | CRUD tareas reusable |
| `app/admin/stories.py` | Stories de WhatsApp reusable |
| `app/db/models.py` (tablas base: cliente, conversacion, equipo, alerta, sesion, etc.) | Reusables al 100% |

---

## 7. Lo específico de Innovación (lo que SÍ debes adaptar al clonar)

Estas piezas tienen lógica/datos del negocio retail. Para un vertical nuevo, las **reemplazas o reescribes**:

| Pieza | Hoy tiene | Para vertical nuevo |
|---|---|---|
| `app/claude/prompts.py` SYSTEM_PROMPT_CLIENTE | Identidad Laura, productos, pagos, escalación retail | Reescribir completo: objetivo del bot, tono, reglas del negocio |
| `app/claude/prompts.py` SYSTEM_PROMPT_EQUIPO | Tools del bot equipo retail | Ajustar según tools del nuevo vertical |
| `app/claude/tools.py` (8 tools cliente) | buscar_productos, enviar_foto, cotizar_envio, draft_order, pedido_manual, escalar | Reemplazar por tools del vertical (agendar_cita, cualificar_lead, etc.) |
| `app/claude/tools_equipo.py` (varias tools admin) | Algunas son genéricas (responder_a_cliente, marcar_interno, consultar_chat). Otras retail (crear_pedido_manual) | Mantener las genéricas, ajustar/agregar las del vertical |
| `app/db/models.py` (Pedido, ProductoCache, TarifaDomicilio) | Tablas retail | Reemplazar con modelos del vertical (Cita, Servicio, Profesional, Lead, etc.) |
| `app/admin/views.py` (SQLAdmin auto-generated) | Vistas para tablas retail | Regenerar con tablas del vertical |
| `app/shopify/` | Integración Shopify | Eliminar si no aplica; reemplazar con integración propia (Google Calendar, Calendly, etc.) |
| `scripts/seed_catalogo*.py` | Scrapers de catálogo retail | Eliminar o reemplazar con scripts de seed del vertical |
| `app/validators/output_rules.py` (R_PRECIO_RECONOCIDO) | Validación de precios catálogo | Mantener R_NO_EMOJIS si aplica; agregar validators del vertical |
| `app/automatizaciones/acciones.py` | reporte_ventas asume ingresos por pedidos | Agregar acciones específicas (reporte_citas_semana, recordatorio_sesion_24h, etc.) |
| `.env` | Datos de Innovación | Cambiar WHAPI_TOKEN, número, dominio, etc. |

---

## 8. Setup desde cero (clonar para vertical nuevo)

### 8.1 Pre-requisitos
- VPS Ubuntu 24.04+ con SSH habilitado
- Dominio apuntando al VPS (DNS A record)
- Cuenta whapi.cloud con número conectado + token
- Cuenta Anthropic con API key (para fallback)
- (Opcional) cuenta Max activa con Dario
- Cuenta Postgres (puede ser en mismo VPS)
- Cuenta de email SMTP si quieres notificaciones por email (opcional)

### 8.2 Setup del VPS

```bash
# Como root
adduser asistente   # crear usuario
usermod -aG sudo asistente   # opcional, si necesita sudo
loginctl enable-linger asistente   # para que systemd user persista sin login

# Como asistente
sudo apt install python3.12 python3.12-venv postgresql nodejs npm nginx
python3.12 -m venv ~/.venv
source ~/.venv/bin/activate
pip install -r requirements.txt
```

### 8.3 Clonar el repo

```bash
git clone https://github.com/santequera1/asistente1.git /home/asistente/app
cd /home/asistente/app
```

### 8.4 BD

```bash
sudo -u postgres createuser -P asistente_user
sudo -u postgres createdb -O asistente_user asistente_db
psql -U asistente_user -d asistente_db -h 127.0.0.1 -f schema.sql
```

O usar Alembic si está actualizado.

### 8.5 .env

```ini
BOT_ENV=production
BOT_PORT=8003

DATABASE_URL=postgresql+asyncpg://asistente_user:PASS@127.0.0.1:5432/asistente_db
DATABASE_URL_SYNC=postgresql://asistente_user:PASS@127.0.0.1:5432/asistente_db

ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_PROVIDER=fallback  # o "dario" o "direct"
DARIO_BASE_URL=http://127.0.0.1:3456

WHAPI_TOKEN=...
WHAPI_NUMERO_BOT=+57...   # número del bot
WHAPI_WEBHOOK_URL=https://tudominio.com/webhook
WHAPI_WEBHOOK_SECRET=...   # generar random

ADMIN_USER=admin
ADMIN_PASSWORD=...
ADMIN_SESSION_SECRET=...   # generar random largo

GRUPO_PEDIDOS_CONFIRMADOS_ID=120363...@g.us   # opcional

DUENO_PHONE_BLOCKED=+57...   # opcional, para bloquear dueño

# Shopify (si aplica al vertical)
SHOPIFY_API_BASE_URL=...
SHOPIFY_API_KEY=...
CATALOGO_PUBLICO_URL=...
CATALOGO_HTML_URL=...
```

### 8.6 Dario (opcional, solo si quieres usar Max)

```bash
npm config set prefix ~/.npm-global
echo 'export PATH=$HOME/.npm-global/bin:$PATH' >> ~/.bashrc
npm install -g @askalf/dario
dario login --manual --no-proxy   # seguir flujo OAuth
# Después crear systemd unit publicar abajo
```

### 8.7 Systemd unit del bot

`~/.config/systemd/user/asistente.service`:
```ini
[Unit]
Description=Bot Asistente
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/home/asistente/app
EnvironmentFile=/home/asistente/app/.env
ExecStart=/home/asistente/.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8003 --log-level info
Restart=on-failure
RestartSec=5
TimeoutStopSec=240   # graceful shutdown para humanizer
KillSignal=SIGTERM

[Install]
WantedBy=default.target
```

Habilitar:
```bash
systemctl --user daemon-reload
systemctl --user enable --now asistente.service
```

### 8.8 Systemd unit de Dario

`~/.config/systemd/user/dario.service`:
```ini
[Unit]
Description=Dario proxy
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=%h/.npm-global/bin:/usr/bin:/bin
Environment=DARIO_OVERAGE_BEHAVIOR=halt
ExecStart=%h/.npm-global/bin/dario proxy --host=127.0.0.1 --port=3456 --log-file=%h/.dario/proxy.log
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

### 8.9 Crons de catálogo y stories

Ver `~/.config/systemd/user/*.timer`. Hay 4-5 timers que sincronizan catálogo, publican stories, hacen logrotate y health checks. Copiar de `deployments/innovacion-fashion/systemd/` (carpeta a crear cuando estandaricemos).

### 8.10 Nginx (proxy HTTPS)

`/etc/nginx/sites-available/asistente`:
```nginx
server {
    listen 443 ssl http2;
    server_name tudominio.com;
    ssl_certificate /etc/letsencrypt/live/tudominio.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/tudominio.com/privkey.pem;

    client_max_body_size 10M;

    location / {
        proxy_pass http://127.0.0.1:8003;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto https;
        proxy_read_timeout 120s;
    }
}
```

### 8.11 Configurar webhook en whapi

Desde https://panel.whapi.cloud, en la configuración del canal:
- Webhook URL: `https://tudominio.com/webhook`
- Secret: el mismo de `WHAPI_WEBHOOK_SECRET`
- Eventos: `messages`, `statuses`

---

## 9. Cómo crear un nuevo bot (vertical nuevo) — checklist práctico

### Paso 1: definir el bot
- Objetivo principal (vender, agendar, cualificar, etc.)
- Persona del cliente
- Tono y estilo
- Reglas duras del negocio (pagos, horarios, escalación)
- Lista de tools que necesitará

### Paso 2: clonar y configurar
1. Fork del repo o nuevo VPS, mismos pasos del §8
2. Cambiar `.env`: WHAPI_TOKEN, número, ADMIN_USER, dominio
3. Crear BD nueva
4. Importar contactos si aplica (`scripts/import_clientes_contactos.py`)

### Paso 3: adaptar el prompt
1. Editar `app/claude/prompts.py` — reescribir `SYSTEM_PROMPT_CLIENTE` y `SYSTEM_PROMPT_EQUIPO`
2. Decidir si mantienes `R_NO_EMOJIS` u otros validadores

### Paso 4: reemplazar tools del vertical
1. Editar `app/claude/tools.py` — quitar tools retail, agregar tools del vertical
2. Editar `app/claude/tools_equipo.py` — mantener las admin genéricas, quitar específicas retail
3. Definir cada tool con `input_schema` JSON + handler async

### Paso 5: adaptar modelos
1. Editar `app/db/models.py` — quitar `Pedido`, `ProductoCache`, `TarifaDomicilio` si no aplican; agregar modelos del vertical
2. Migración SQL para crear las tablas nuevas
3. Regenerar vistas SQLAdmin en `app/admin/views.py`

### Paso 6: ajustar admin
1. Editar `app/admin/dashboard.py` — métricas del vertical (ej: citas agendadas, leads cualificados)
2. Mantener `app/admin/chats.py`, `actions.py`, `automatizaciones.py`, `stories.py` (genéricas)
3. Actualizar sidebar (`_shell.py`) con items del vertical

### Paso 7: agregar acciones de automatizaciones específicas
1. Editar `app/automatizaciones/acciones.py` — agregar handlers nuevos (ej: `recordatorio_cita_24h`, `seguimiento_lead_3d`)
2. Registrar en `ACCIONES_DISPONIBLES`

### Paso 8: testing
1. Probar webhook con curl simulado
2. Probar tool use loop con sandbox (mensajes propios al bot)
3. Verificar flujos críticos del vertical
4. Probar pausas, marcar interno, escalación

### Paso 9: deploy y monitoreo
1. systemd start
2. Verificar logs: `tail -f /home/asistente/logs/asistente.log`
3. Crear tareas programadas iniciales desde `/admin/automatizaciones`

---

## 10. Operación cotidiana

### Comandos comunes

```bash
# Logs en vivo
tail -f /home/asistente/logs/asistente.log

# Reiniciar bot
systemctl --user restart asistente

# Estado
systemctl --user status asistente
systemctl --user status dario
systemctl --user list-timers

# Dario uso
dario doctor --usage
dario usage

# BD queries útiles
psql -U asistente_user -d asistente_db -h 127.0.0.1

# Backup BD
pg_dump -U asistente_user -h 127.0.0.1 asistente_db | gzip > backup_$(date +%Y%m%d).sql.gz

# Forzar sync catálogo
systemctl --user start sync-catalogo-shopify.service
systemctl --user start sync-catalogo-html.service

# Forzar publicar story
systemctl --user start publicar-story.service
```

### Queries SQL útiles

```sql
-- Estado del bot
SELECT activo, pausado_por, razon FROM bot_estado WHERE id=1;

-- Activar/pausar bot
UPDATE bot_estado SET activo=true, pausado_por=NULL, razon=NULL WHERE id=1;

-- Top 10 clientes con más mensajes hoy
SELECT c.numero_whatsapp, c.nombre, COUNT(*) AS msgs
FROM conversaciones conv JOIN clientes c ON c.id=conv.cliente_id
WHERE conv.timestamp > CURRENT_DATE
GROUP BY c.id ORDER BY msgs DESC LIMIT 10;

-- Alertas abiertas por tipo
SELECT tipo, COUNT(*) FROM alertas_fabio
WHERE resuelto=false GROUP BY tipo;

-- Chats sin contestar (último msg fue inbound)
WITH ult AS (
  SELECT DISTINCT ON (cliente_id) cliente_id, direccion, contenido, timestamp
  FROM conversaciones ORDER BY cliente_id, timestamp DESC
)
SELECT c.numero_whatsapp, c.nombre, u.timestamp, u.contenido
FROM ult u JOIN clientes c ON c.id=u.cliente_id
WHERE u.direccion='inbound' AND c.bloqueado=false
ORDER BY u.timestamp ASC LIMIT 20;

-- Pausas activas
SELECT i.cliente_id, c.numero_whatsapp, c.nombre, i.pausado_hasta, i.razon
FROM intervencion_humana i JOIN clientes c ON c.id=i.cliente_id
WHERE i.pausado_hasta > NOW();

-- Despausar todos los clientes (cuidado)
DELETE FROM intervencion_humana WHERE pausado_hasta > NOW();
```

---

## 11. Decisiones de diseño importantes (justificadas)

### 11.1 Por qué async + uvicorn (no Django/sync)
- Webhooks llegan en bursts, asyncio escala mejor con I/O bound
- Tool use loop hace múltiples llamadas LLM + BD + whapi por mensaje, todo async

### 11.2 Por qué Postgres directo (no SQLite ni Mongo)
- Necesitamos transacciones reales (outbox pattern)
- JSONB para items, parametros, metadata
- Queries complejas con joins (chats sin responder, top clientes, etc.)

### 11.3 Por qué lock por cliente_id (no global)
- Permite procesar mensajes de N clientes en paralelo
- Pero serializa los del MISMO cliente (evita race en historial)

### 11.4 Por qué outbox pattern
- Si la transacción falla (rollback), no quedan alertas a Fabio huérfanas
- Los mensajes salen DESPUÉS del commit, garantizando consistencia

### 11.5 Por qué humanización (60-180s delay)
- WhatsApp Business detecta bots por respuesta instantánea + sin typing
- Caso real: cuenta de Sandra fue banneada por contestar en <2s
- Trade-off: cliente espera más, pero la cuenta no se banea

### 11.6 Por qué Dario + fallback API directa
- Anthropic tuvo problemas de facturación (créditos misteriosos)
- Max plan da costo fijo
- Dario rutea por Max sin tocar el código
- Si Max se agota o Dario falla, wrapper cae a API directa transparente

### 11.7 Por qué dedupe de alertas en 6h/12h
- Cliente puede repreguntar lo mismo 5 veces → 5 alertas a Fabio = spam
- 6h para abiertas: el equipo ya sabe, espera respuesta
- 12h para resueltas: si el admin ya respondió, el bot debe usar el historial, no re-escalar

### 11.8 Por qué auto-pausa en queja/comprobante
- Si cliente envía comprobante de pago, el bot NO debe seguir vendiendo encima
- Si queja: el bot puede empeorar la situación, mejor que humano la maneje
- Auto-pausa 2-4h da espacio al admin

### 11.9 Por qué cache 30s del directorio
- Cada mensaje consulta `es_miembro_equipo`, `es_numero_interno`
- Sin cache: query a BD por cada mensaje
- 30s es buen balance: cambios desde admin tardan máx 30s en propagar

### 11.10 Por qué config en BD (próximo paso) en vez de YAML
- Otros admins pueden editar sin SSH ni redeploy
- Versionado en BD (auditoría)
- Multi-vertical: misma codebase, distinta config por cliente

---

## 12. Deuda técnica y pendientes conocidos

### Pendientes operativos
- **Buffer de mensajes en ráfaga** (<20s): cliente que manda 5 msgs en 10s genera 5 turns del bot. Mejor agruparlos.
- **Editor de prompts desde admin**: hoy todo en código. Necesario para revender el bot.
- **Excel de tarifas pendiente** de actualizar con barrios faltantes.
- **Scraper HTML** funcionando con estrategia E para Innovación, pero frágil si la página cambia.

### Deuda técnica
- **Migraciones Alembic**: muchos cambios se aplicaron con SQL directo. Hay drift entre `models.py` y BD real (aunque manejable).
- **Tests**: no hay suite de tests. Confiamos en producción + logs.
- **Multi-tenancy**: no implementado. Cada cliente = 1 deploy. OK para 5-10 clientes, problemático con 50+.
- **Vault para secretos**: `.env` plain text en el VPS. Aceptable hoy, no escalable.
- **Backups automáticos**: no configurados. Hacer cron diario de `pg_dump`.

### Próximos pasos sugeridos (en orden)
1. **Refactor core → verticales**: separar lo reusable de lo específico Innovación
2. **Editor de prompts/config en admin**: tabla `bot_configuracion` editable
3. **Bot SDR (vertical captacion_b2b)**: validar el approach con un 2do bot real
4. **Tests críticos**: webhook routing, tool use loop, validators
5. **Multi-tenancy**: solo si llegamos a 10+ clientes y el modelo deploy-per-tenant duele

---

## 13. Contactos y accesos

| Recurso | URL/Info |
|---|---|
| Repo | https://github.com/santequera1/asistente1 |
| Admin Innovación | https://asistente.dtgrowthpartners.com/admin |
| VPS | asistente.dtgrowthpartners.com (usuario `asistente`) |
| Anthropic Console | https://console.anthropic.com (cuenta dev@dtgrowthpartners.com) |
| whapi panel | https://panel.whapi.cloud (canal Laura) |
| Dario docs | https://github.com/askalf/dario |
| Admins activos | Stiven Antequera (+573026444564), Fabio (+573019836645), Roxana Redes (+573022568586) |
| Grupo notificaciones | "Agentes IA Innova x DTGP" (`120363425539154194@g.us`) |

---

## Apéndice A: variables de entorno (referencia)

```
# Bot
BOT_ENV=production|development
BOT_PORT=8003

# BD
DATABASE_URL=postgresql+asyncpg://user:pass@host:port/db
DATABASE_URL_SYNC=postgresql://user:pass@host:port/db
DB_POOL_SIZE=10
DB_MAX_OVERFLOW=10

# Claude
ANTHROPIC_API_KEY=sk-ant-...
CLAUDE_MODEL_PRINCIPAL=claude-sonnet-4-6
CLAUDE_MODEL_INTENT=claude-haiku-4-5-20251001
CLAUDE_MAX_TOKENS_OUTPUT=1024
CLAUDE_MAX_CONVERSACIONES_POR_HORA=300

# Dario
CLAUDE_PROVIDER=fallback|dario|direct
DARIO_BASE_URL=http://127.0.0.1:3456
DARIO_API_KEY=dario

# Whapi
WHAPI_BASE_URL=https://gate.whapi.cloud
WHAPI_TOKEN=...
WHAPI_NUMERO_BOT=+57...
WHAPI_WEBHOOK_URL=https://tudominio.com/webhook
WHAPI_WEBHOOK_SECRET=...

# Admin
ADMIN_USER=...
ADMIN_PASSWORD=...
ADMIN_SESSION_SECRET=... (random largo)

# Shopify (opcional según vertical)
SHOPIFY_API_BASE_URL=...
SHOPIFY_API_KEY=...
CATALOGO_PUBLICO_URL=https://.../products.json
CATALOGO_HTML_URL=https://.../pages/catalogo
CATALOGO_HTML_SYNC_INTERVAL_HORAS=12

# Teléfonos
DUENO_PHONE_BLOCKED=+57...

# Grupos
GRUPO_PEDIDOS_CONFIRMADOS_ID=120363...@g.us

# Humanización
FEATURE_HUMANIZACION=true
HUMANIZATION_DELAY_MIN_S=60
HUMANIZATION_DELAY_MAX_S=180
HUMANIZATION_TYPING_INDICATOR=true

# Feature flags
FEATURE_HUMAN_TAKEOVER=true
```

---

## Apéndice B: glosario

- **Outbox**: lista de mensajes pendientes de enviar que se acumulan en `ctx["outbox"]` y se drenan tras commit de la transacción principal. Garantiza consistencia.
- **Humanizer**: módulo que simula comportamiento humano (typing + delay 60-180s) para evitar detección Meta.
- **Dario**: proxy local que rutea llamadas Anthropic a través de tu suscripción Max en vez de pay-per-token.
- **Tool use loop**: ciclo donde Claude decide → ejecutamos → devolvemos resultado → Claude decide otra vez. Máximo 5 rondas.
- **Pushname**: nombre que el cliente puso en su perfil de WhatsApp (whapi lo manda como `from_name`).
- **chat_id @g.us**: identificador de un grupo de WhatsApp (vs `@s.whatsapp.net` para 1:1).
- **Dedupe alertas**: lógica que evita escalar lo mismo dos veces (ventanas 6h abiertas / 12h resueltas).
- **Intervención humana**: cuando una asesora escribe desde su WhatsApp → bot se pausa para ese cliente 1h.
- **Kill switch global**: `bot_estado.activo=false` → bot ignora todos los clientes hasta reactivar.
- **Cliente proxy**: cuando un admin escribe al bot, se crea un "cliente" con nombre `[ADMIN] X` para persistir la conversación admin↔bot en `conversaciones`.
- **Bot equipo**: el flujo distinto que se activa cuando quien escribe es un `equipo_miembro` (admin), con prompt y tools propios.
- **Action handler**: función async en `automatizaciones/acciones.py` que ejecuta una tarea programada (reporte, recordatorio, etc.).

---

**Esta guía es viva.** Cada vez que cambies arquitectura, agregues feature core, o aprendas algo nuevo de producción, actualízala. Idealmente: si llega un dev nuevo, debe poder leer GUIA.md + el código y entender el sistema sin preguntar.
