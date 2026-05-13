# Bot de Tienda Online - Plan de Implementación

**Objetivo:** Crear un bot WhatsApp inteligente que mantenga contexto de conversación, responda preguntas sobre productos (tallas, colores, precios) y guíe al cliente hacia la compra.

---

## 1. Arquitectura General

```
Cliente (WhatsApp)
       ↓
    whapi (envía/recibe mensajes)
       ↓
Bot Principal (Python/FastAPI)
       ↓
    ├─ Parser HTML (sincroniza catálogo)
    ├─ PostgreSQL (histórico, contexto)
    └─ Claude API (genera respuestas inteligentes)
```

---

## 2. Schema PostgreSQL

Crea estas tablas en tu base de datos:

```sql
-- Tabla de clientes
CREATE TABLE clientes (
    id SERIAL PRIMARY KEY,
    numero_whatsapp VARCHAR(20) UNIQUE NOT NULL,
    nombre VARCHAR(255),
    fecha_creacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    ultimo_contacto TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    notas TEXT
);

-- Catálogo de productos
CREATE TABLE catalogo (
    id SERIAL PRIMARY KEY,
    referencia VARCHAR(50) UNIQUE NOT NULL,
    nombre VARCHAR(255) NOT NULL,
    descripcion TEXT,
    tallas JSON NOT NULL,  -- ["6", "8", "10", "12"] o ["S", "M", "L", "XL"]
    colores JSON NOT NULL, -- ["Negro", "Azul", "Rojo"]
    precio_detal DECIMAL(10, 2) NOT NULL,
    precio_mayorista DECIMAL(10, 2),
    imagen_url VARCHAR(500),
    fecha_actualizacion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    activo BOOLEAN DEFAULT TRUE
);

-- Histórico de conversaciones
CREATE TABLE conversaciones (
    id SERIAL PRIMARY KEY,
    cliente_id INT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    producto_id INT REFERENCES catalogo(id),
    mensaje_usuario TEXT NOT NULL,
    respuesta_bot TEXT NOT NULL,
    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    contexto_usado JSON  -- Qué info pasamos a Claude
);

-- Sesiones activas (para mantener contexto del cliente)
CREATE TABLE sesiones_activas (
    id SERIAL PRIMARY KEY,
    cliente_id INT NOT NULL UNIQUE REFERENCES clientes(id) ON DELETE CASCADE,
    producto_id INT REFERENCES catalogo(id),
    estado VARCHAR(50),  -- "explorando", "preguntando_talla", "preguntando_color", "listo_comprar"
    ultima_interaccion TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    contexto_comprimido JSON,  -- {"producto": "...", "explorado": [...], "interes": "..."}
    datos_carrito JSON  -- {"cantidad": 1, "talla": "M", "color": "Negro"}
);

-- Índices para búsquedas rápidas
CREATE INDEX idx_cliente_whatsapp ON clientes(numero_whatsapp);
CREATE INDEX idx_conversacion_cliente ON conversaciones(cliente_id);
CREATE INDEX idx_sesion_cliente ON sesiones_activas(cliente_id);
CREATE INDEX idx_catalogo_referencia ON catalogo(referencia);
```

---

## 3. Datos de Entrada

**Fuente:** https://innovacionfashion.co/pages/catalogo-de-whatsapp

**Estructura del catálogo:**
- Nombre + REF (código único)
- Tallas (varían por producto: "8,10,12" o "S,M,L,XL")
- Colores (múltiples opciones)
- Precio detal + mayorista
- Imagen

**Asunciones:**
- El precio es IGUAL para todas las tallas/colores
- Si está en el HTML = está disponible (no hay stock por talla)
- El bot responde con PRECIO DETAL (información general, sin mayorista)

---

## 4. Estructura del Proyecto

```
proyecto/
├── bot_tienda.py           # Bot principal (recibe/envía mensajes)
├── parser_catalogo.py      # Extrae HTML → PostgreSQL
├── claude_context.py       # Construye contexto para Claude
├── requirements.txt        # Dependencias
├── .env                    # Variables de entorno
└── README.md              # Instrucciones
```

---

## 5. Variables de Entorno (.env)

```env
# Database
DATABASE_URL=postgresql://usuario:password@localhost:5432/tienda_db

# WhatsApp (whapi)
WHAPI_TOKEN=tu_token_whapi
WHAPI_WEBHOOK_URL=https://tudominio.com/webhook

# Claude API
CLAUDE_API_KEY=tu_api_key
CLAUDE_MODEL=claude-3-5-sonnet-20241022

# Bot
BOT_PORT=8000
BOT_HOST=0.0.0.0
```

---

## 6. Flujo de Conversación

### Usuario llega (primer mensaje):
```
Usuario: "Hola, vi la falda del anuncio y quiero saber qué tallas tienen"

Bot:
1. Recibe mensaje de whapi
2. Busca cliente en DB por número → si no existe, crea registro
3. Busca "falda" en catálogo
4. Carga contexto:
   - Producto: "Falda Negra (INN3756)"
   - Tallas: [6, 8, 10, 12, 14, 16]
   - Colores: [Negro, Rojo, Azul]
   - Precio: $56.000
5. Pasa a Claude:
   "El cliente pregunta por tallas de la Falda Negra.
    Disponibles: 6, 8, 10, 12, 14, 16.
    Precio: $56.000. Responde amablemente."
6. Claude responde (ej):
   "¡Hola! 😊 La Falda Negra está disponible en tallas:
    6, 8, 10, 12, 14, 16.
    Precio: $56.000. ¿Cuál talla usas? 👗"
7. Guarda en DB:
   - conversaciones: mensaje_usuario + respuesta_bot
   - sesiones_activas: producto="Falda Negra", estado="preguntando_talla"
```

### Usuario pregunta por talla específica:
```
Usuario: "¿Y en talla 10?"

Bot:
1. Recibe mensaje
2. Carga sesión activa → ya sabe que habla de "Falda Negra"
3. Carga últimos 3 mensajes de conversaciones
4. Pasa a Claude:
   "Cliente pregunta por talla 10. 
    Contexto: Estamos discutiendo Falda Negra (INN3756).
    Talla 10 SÍ está disponible.
    Responde sobre disponibilidad y próximos pasos."
5. Claude responde:
   "¡Perfecto! La talla 10 está disponible en Falda Negra.
    ¿En qué color la prefieres? (Negro, Rojo, Azul) 👇"
6. Actualiza sesión_activa: estado="preguntando_color"
```

---

## 7. Pasos de Implementación

### PASO 1: Setup Base de Datos
- [ ] Crear base de datos PostgreSQL
- [ ] Ejecutar SQL del schema (sección 2)
- [ ] Verificar tablas creadas

**Comando:**
```bash
psql -U postgres -d tienda_db -f schema.sql
```

---

### PASO 2: Setup Proyecto Python
- [ ] Crear carpeta del proyecto
- [ ] Crear venv: `python -m venv venv`
- [ ] Activar: `source venv/bin/activate`
- [ ] Crear `.env` con variables (sección 5)
- [ ] Instalar dependencias: `pip install -r requirements.txt`

**requirements.txt:**
```
fastapi==0.104.1
uvicorn==0.24.0
python-dotenv==1.0.0
psycopg2-binary==2.9.9
anthropic==0.25.0
requests==2.31.0
beautifulsoup4==4.12.2
lxml==4.9.3
```

---

### PASO 3: Parser HTML → PostgreSQL
**Archivo: `parser_catalogo.py`**

Función:
- Descarga HTML de https://innovacionfashion.co/pages/catalogo-de-whatsapp
- Extrae: referencia, nombre, tallas, colores, precios, imagen
- Inserta/actualiza en tabla `catalogo`
- Se ejecuta cada X horas (cron job)

**Lógica:**
1. Parsea HTML con BeautifulSoup
2. Extrae cada producto (probablemente divs con clase "producto")
3. Limpia datos (tallas como JSON, colores como JSON)
4. Upsert a PostgreSQL (INSERT OR UPDATE)
5. Log de cambios

---

### PASO 4: Claude Context Helper
**Archivo: `claude_context.py`**

Función:
- Toma un número de cliente y su último mensaje
- Carga histórico de sesión
- Construye "contexto comprimido" para Claude
- Retorna prompt listo para enviar a Claude API

**Entrada:**
```python
def construir_contexto_claude(cliente_numero, mensaje_usuario):
    # Carga cliente
    # Carga sesión activa
    # Carga últimos 3 mensajes
    # Carga producto actual
    # Retorna prompt completo
```

**Salida:**
```python
{
    "prompt": "El cliente pregunta por... Contexto: ...",
    "producto_info": {...},
    "sesion": {...},
    "historico": [...]
}
```

---

### PASO 5: Bot Principal
**Archivo: `bot_tienda.py`**

Estructura:
```python
from fastapi import FastAPI, Request
from anthropic import Anthropic
import os

app = FastAPI()
client_claude = Anthropic()

@app.post("/webhook")
async def webhook_whapi(request: Request):
    """
    Recibe mensajes de whapi
    1. Valida token
    2. Extrae cliente_numero y mensaje
    3. Carga/crea cliente en DB
    4. Construye contexto con claude_context.py
    5. Llamada a Claude API
    6. Guarda en conversaciones
    7. Envía respuesta a whapi
    8. Actualiza sesión_activa
    """
    pass

@app.get("/health")
async def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
```

---

### PASO 6: Integración whapi
- [ ] Registrarse en whapi.com
- [ ] Obtener TOKEN
- [ ] Configurar webhook URL en whapi
- [ ] Esperar mensajes en `@app.post("/webhook")`

---

### PASO 7: Testing
- [ ] Enviar mensaje de prueba a bot
- [ ] Verificar en conversaciones que se guardó
- [ ] Verificar respuesta de Claude
- [ ] Probar cambios de contexto (talla → color)

---

## 8. Info de Empresa (Sistema de Prompts)

**Información a proporcionar a Claude:**

```
Eres un asistente de ventas de Innovación Fashion Outlet.

INSTRUCCIONES:
- Sé amable, profesional y usa emojis moderadamente
- Responde preguntas sobre tallas, colores, precios
- Cuando el cliente esté listo, sugiere "Pedir ahora" o enviar link de compra
- Si pregunta por stock: "Sí, disponible" (asume que si está en catálogo, hay)
- Si pregunta por envíos/devoluciones/tiempos: Responde genérico o pasa a humano

POLÍTICA:
- Entrega en 2-3 días hábiles a nivel nacional
- Devoluciones hasta 7 días
- Precio mostrado es DETAL (no mayorista)
- Aceptamos transferencia, PSE, tarjeta crédito
```

---

## 9. Checklist de Implementación

### Semana 1:
- [ ] Base de datos creada y schema ejecutado
- [ ] Proyecto Python con venv
- [ ] Parser HTML extrayendo catálogo
- [ ] Primera sincronización en DB (verificar 100+ productos)

### Semana 2:
- [ ] Claude Context Helper funcionando
- [ ] Bot recibiendo mensajes de whapi (webhook)
- [ ] Integración con Claude API
- [ ] Histórico guardado en conversaciones

### Semana 3:
- [ ] Testing con clientes reales
- [ ] Mejoras de prompts
- [ ] Manejo de edge cases (producto no encontrado, cliente confundido)

---

## 10. Comandos Útiles

**Iniciar bot:**
```bash
source venv/bin/activate
python bot_tienda.py
```

**Sincronizar catálogo manually:**
```bash
python parser_catalogo.py
```

**Ver logs:**
```bash
tail -f bot.log
```

**Consultar DB:**
```bash
psql -U postgres -d tienda_db
SELECT COUNT(*) FROM catalogo;
SELECT * FROM conversaciones ORDER BY timestamp DESC LIMIT 10;
```

---

## 11. Próximos Pasos

1. ✅ Leer este documento
2. ⭕ Hacer PASO 1: Setup PostgreSQL
3. ⭕ Hacer PASO 2: Setup Python
4. ⭕ Hacer PASO 3: Parser HTML
5. ⭕ Hacer PASO 4: Claude Context
6. ⭕ Hacer PASO 5: Bot Principal
7. ⭕ Hacer PASO 6: whapi integration
8. ⭕ Testing

---

## Notas

- **No uses OpenClaw:** Arquitectura simple, sin overhead
- **Postgres es tu amigo:** Histórico completo, fácil analizar después
- **Claude API:** Contexto estructurado = respuestas coherentes
- **whapi:** Recibe/envía mensajes, mantén token seguro

