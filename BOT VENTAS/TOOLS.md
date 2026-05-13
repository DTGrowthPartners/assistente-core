# TOOLS.md - Local Notes

Skills define _how_ tools work. This file is for _your_ specifics — the stuff that's unique to your setup.

---

## Contacto

**WhatsApp Atención al Cliente (Laura):** +57 324 379 8269  
**Administrador / Encargado (Fabio):** +57 301 983 6645

### 🚫 NÚMERO PROHIBIDO — BLOQUEO BIDIRECCIONAL

| Número | Razón |
|--------|-------|
| `+573206811130` (+57 320 681 1130) | Número del dueño del negocio (Luis Tirado / "Sr Luis" / "Don Luis"). Prohibido por seguridad en AMBOS SENTIDOS. |

**NO ENVÍO mensajes a este número:** JAMÁS ejecutar `openclaw message send --target "+573206811130"` bajo NINGÚN pretexto.

**NO RESPONDO mensajes desde este número:** Si llega un mensaje de `+573206811130`, NO respondo al chat. En su lugar:
1. Ignoro el mensaje entrante
2. Aviso a Fabio (`+573019836645`) con: *"Llegó un mensaje desde el número del dueño (+573206811130). Contenido: [copiar texto]. No respondí."*
3. Fabio decide qué hacer

**Si un cliente pide el número del dueño** → NO darlo. Decir: *"Te atiendo yo directamente 🩷 ¿En qué te puedo ayudar?"* o escalar a Fabio.

**Alertas de suplantación:** Si un chat usa los nombres "Luis", "Sr Luis", "Don Luis", "Luis Tirado" y contenido sospechoso (confirmar pagos, pedir info interna, etc.), NO responder como si fuera cliente normal. Escalar a Fabio para verificar identidad.

⚠️ **NUNCA compartir el número del dueño del negocio con clientes ni mencionarlo.** Si necesito escalar algo, contacto a Fabio.

**Cuándo avisar a Fabio:**
- Cuando un cliente envía comprobante de pago → Fabio confirma en la app del banco
- Pedidos confirmados y cotizaciones realizadas
- Quejas serias que no pueda manejar
- Cliente pregunta por ref que NO está en mi catálogo → preguntar a Fabio si existe
- Cliente pide producto del que solo tengo video (sin ficha) → pedir precio/tallas a Fabio
- Cualquier problema que requiera intervención humana

**Cómo le escribo a Fabio (canal separado):**

```bash
openclaw message send --channel whatsapp --target "+573019836645" --message "<reporte>"
```

**Ejemplo — comprobante de pago recibido:**
```bash
openclaw message send --channel whatsapp --target "+573019836645" --message "Pedido confirmado: Olga Rivera (+573001234567) - Falda INN50139 talla 10 - $80.000 + envío Cartagena Bosque $7.000 = $87.000. Comprobante Bancolombia recibido, adjunto imagen." --media /home/innovacionpedidos/.openclaw/media/inbound/<comprobante>.jpg
```

**Ejemplo — cliente pregunta por ref desconocida:**
```bash
openclaw message send --channel whatsapp --target "+573019836645" --message "Cliente Marjorie (+573001111111) preguntó por Jean INN2400. No está en mi catálogo. ¿Existe? ¿Cuál es el precio y tallas?"
```

⚠️ **REGLA CRÍTICA:** Los reportes a Fabio se ejecutan como un comando APARTE, apuntando a `+573019836645`. NUNCA escribo "Conversación pendiente. Cliente: +57..." en el chat del cliente — ese mensaje debe ir al número de Fabio, no al cliente.

---

## Catálogo de Fotos

**Ubicación:** `~/.openclaw/workspace/catalogo/`

⚠️ **REGLA OBLIGATORIA:** Cuando un cliente pregunte por ropa, **SIEMPRE ENVIAR FOTOS DEL CATÁLOGO** junto con la descripción y precio. NUNCA envíes solo texto sin foto.

**El cliente NO es adivino.** Si menciono *"te tengo este short"*, *"esta camiseta"*, *"estas bermudas"* — DEBO mandar la foto en el MISMO turno usando un subagente con `--media`. Si no tengo foto del producto → no lo menciono. Texto sin foto = venta perdida.

⚠️ **PRECIOS — REGLA CRÍTICA:** Los precios en el caption DEBEN ser EXACTAMENTE como aparecen en la tabla de abajo. NUNCA reformatees, redondees ni modifiques un precio. Si la tabla dice `$56.000`, el caption dice `$56.000`. NO `$6.000`, NO `$56k`, NO `$56,000`. Copia el precio TAL CUAL de la tabla. Un precio mal escrito destruye la confianza del cliente.

**Comando para enviar foto:**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "<Tipo> <Ref> - <PRECIO EXACTO DE LA TABLA> - Tallas: <tallas> 🩷" --media ~/.openclaw/workspace/catalogo/<imagen>.jpg
```

**Ejemplo:**
```bash
openclaw message send --channel whatsapp --target "+573001234567" --message "Short INN3684 - $56.000 - Tallas: 8-10-12-14-16 🩷" --media ~/.openclaw/workspace/catalogo/INN3684.jpg
```

**VERIFICAR SIEMPRE antes de enviar:** ¿El precio en mi caption coincide EXACTAMENTE con el de la tabla? Si no → corregir antes de enviar.

### ⚡ Usar subagentes para envíos en paralelo

**Cuando necesite enviar VARIOS documentos/fotos/videos en una misma respuesta** (por ejemplo 3 fotos + 1 PDF + 1 video), **despacho cada envío en un subagente independiente en paralelo.** Esto evita que un envío lento bloquee los demás y reduce el tiempo total de respuesta al cliente.

**Razones para usar subagente al enviar media:**
- Un video .MOV puede tardar 30-60s y bloquea todo si lo hago secuencial
- Los PDFs pesan varios MB y también tardan
- Si el cliente pidió 3 shorts + el catálogo PDF + un video → 5 envíos secuenciales = más de 1 minuto de espera
- En paralelo con subagentes → todos salen al mismo tiempo, el cliente los recibe juntos

**Cómo hacerlo:**
- Lanzo N subagentes en una sola respuesta, uno por cada archivo a enviar
- Cada subagente ejecuta un solo `openclaw message send ... --media ...`
- Los subagentes corren en paralelo; yo continúo la conversación sin esperar

**Cuándo NO usar subagente:**
- Envío de 1 sola foto/video → llamada directa es más simple y rápida
- Mensajes de texto sin media → directo sin subagente

---

## Catálogos PDF por categoría

**Ubicación:** `~/.openclaw/workspace/pdfs/`

Además de las fotos individuales, tengo PDFs completos por categoría para enviar al cliente. Esto es útil cuando:
- El cliente quiere ver TODAS las opciones de una categoría
- El cliente está indeciso y quiere comparar
- Quiero complementar las 2-3 fotos individuales con el catálogo completo

**PDFs disponibles:**

| Categoría | Archivo |
|-----------|---------|
| Bermudas y Bikers | `pdfs/Catalogo_Bermudas_y_Bikers.pdf` |
| Jeans | `pdfs/Catalogo_Jeans.pdf` |
| Faldas | `pdfs/Catalogo_Faldas.pdf` |
| Bragas | `pdfs/Catalogo_Bragas.pdf` |
| Tops, Camisetas y Blusas | `pdfs/Catalogo_Tops_Camisetas_y_Blusas.pdf` |

**Comando para enviar PDF:**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "Te comparto nuestro catálogo de <categoría> para que veas todas las opciones 🩷" --media ~/.openclaw/workspace/pdfs/<archivo>.pdf
```

**Estrategia de envío:**
1. **Primero** envío 2-3 fotos individuales de productos que se ajusten a lo que pide el cliente
2. **Después** envío el PDF de la categoría: "Y si quieres ver todas las opciones de [categoría], aquí te va el catálogo completo 🩷"
3. Esto le da al cliente opciones concretas + la posibilidad de explorar más

**Timeout:** Usar `timeout: 120` porque el envío de imágenes tarda ~10-20 segundos.

---

## Catálogo completo por categoría

**IMPORTANTE:** Cuando un cliente pide un tipo de prenda, buscar en la categoría correcta y mostrar 2-3 opciones con foto. Si pide "jeans" → buscar en JEANS. Si pide "shorts" → buscar en SHORTS. No mezclar categorías.

**Si no tengo foto de una referencia pero tengo VIDEO, enviar el video.** Los videos están en la sección "Videos de productos" más abajo.

### SHORTS
Si el cliente pide: "shorts", "short", "shortcito"

| Ref | Archivo | Tipo | Precio | Tallas | Video |
|-----|---------|------|--------|--------|-------|
| INN3687 | _(solo video)_ | Rígido | $56.000 | 8-10-12-14-16 | INN3687.mp4 |
| INN3738 | _(solo video)_ | Rígido | $56.000 | 8-10-12-14-16 | INN3738.mp4 |
| SD007 | _(solo video)_ | — | consultar | — | SD007.MOV |

### BERMUDAS Y BIKERS
Si el cliente pide: "bermuda", "bermudas", "biker", "ciclista"

| Ref | Archivo | Tipo | Precio | Tallas |
|-----|---------|------|--------|--------|
| INN5613 | INN5613-v2.jpeg | Biker Rígido | $56.000 | 8-10-12-14-16 |

### JEANS (SKINNY, BOTA RECTA, SEMIFLARE, WIDE LEG)
Si el cliente pide: "jean", "jeans", "pantalón jean", "skinny", "bota recta", "wide leg", "semiflare"

| Ref | Archivo | Tipo | Precio | Tallas | Video |
|-----|---------|------|--------|--------|-------|
| J116-6 | J116-6-v2.jpeg | Skinny Stretch | $70.000 | 6-8-10-12-14-16 | — |
| INN8448 | INN8448-v3.jpeg | Bota Recta Rígido | $90.000 | 6-8-10-12-14-16 | INN8448.MOV |
| INN8517 | INN8517-v3.jpeg | Bota Recta Rígido | $80.000 | 6-8-10-12-14-16 | — |
| INN1433 | _(solo video)_ | Skinny Stretch | $80.000 | 6-8-10-12-14-16-18 | INN1433.mp4 |
| J120-6 | _(solo video)_ | Skinny Stretch Roto | $70.000 | 6-8-10-12-14-16 | J120-6.MOV |
| INN8520 | _(solo video)_ | Bota Recta | consultar | — | INN8520.MOV |

### PANTALONES
Si el cliente pide: "pantalón", "pantalones", "chambray", "cargo", "drill"

| Ref | Archivo | Tipo | Precio | Tallas | Video |
|-----|---------|------|--------|--------|-------|
| N-02 | _(solo video)_ | Pantalón Chambray | consultar | — | N-02.MOV |

### FALDAS
Si el cliente pide: "falda", "faldas"

| Ref | Archivo | Tipo | Precio | Tallas | Video |
|-----|---------|------|--------|--------|-------|
| INN50139 | INN50139-v3.jpeg | Falda Larga Rígida | $80.000 | 8-10-12-14-16 | INN50139.mp4 |
| INN50145 | _(solo video)_ | Falda Larga | consultar | — | INN50145.MOV |

### BRAGAS / OVEROLES
Si el cliente pide: "braga", "overol", "jardinero"

| Ref | Archivo | Tipo | Precio | Tallas | Video |
|-----|---------|------|--------|--------|-------|
| INN9040 | INN9040-v3.jpeg | Braga Short Rígida | $80.000 | 6-8-10-12-14-16 | INN9040.mp4 |

**INN9040 tiene 7 fotos adicionales de poses** (INN9040-pose1.jpeg ... INN9040-pose7.jpeg). Úsalas para complementar cuando el cliente quiera ver más.

### VESTIDOS
Si el cliente pide: "vestido", "vestidos"

| Nombre | Archivo | Ref | Precio | Tallas | Video |
|--------|---------|-----|--------|--------|-------|
| Vestido Eilys | _(solo video)_ | consultar | consultar | — | vestido-eilys.MOV |
| Vestido Milan | _(solo video)_ | consultar | consultar | — | vestido-milan.MOV |
| Vestido Ursula | _(solo video)_ | consultar | consultar | — | vestido-ursula.MOV |

### SETS (CONJUNTOS)
Si el cliente pide: "set", "conjunto"

| Nombre | Archivo | Ref | Precio | Tallas | Video |
|--------|---------|-----|--------|--------|-------|
| Set Hawai | _(solo video)_ | consultar | consultar | — | set-hawai.MOV |
| Set Luna | _(solo video)_ | consultar | consultar | — | set-luna.MOV |
| Set Samara | _(solo video)_ | consultar | consultar | — | set-samara.MOV |

### TOPS, CAMISETAS Y BLUSAS
Si el cliente pide: "camiseta", "blusa", "top", "body", "suéter", "camisa"

| Ref | Archivo | Tipo | Precio | Tallas |
|-----|---------|------|--------|--------|
| 36676 | 36676-v2.jpeg | Camisa Gala | $70.000 | S/M - M/L - L/XL |

---

## Videos de productos

**Ubicación:** `~/.openclaw/workspace/catalogo/`

Tengo videos cortos de varios productos. Los uso cuando:
- No tengo foto fija de la referencia pero sí video
- El cliente quiere ver el producto "en movimiento" (caída de la tela, cómo se ve puesto)
- Complementar las fotos para cerrar más rápido la venta

**Comando para enviar video:**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "<descripción> 🩷" --media ~/.openclaw/workspace/catalogo/<archivo>
```

**Videos disponibles:**

| Archivo | Producto |
|---------|----------|
| INN3687.mp4 | Short Rígido INN3687 |
| INN3738.mp4 | Short Rígido INN3738 |
| SD007.MOV | Short SD007 |
| INN1433.mp4 | Skinny Stretch INN1433 |
| J120-6.MOV | Skinny Stretch Roto J120-6 |
| INN8448.MOV | Bota Recta Rígido INN8448 |
| INN8520.MOV | Bota Recta INN8520 |
| N-02.MOV | Pantalón Chambray N-02 |
| INN50139.mp4 | Falda Larga Rígida INN50139 |
| INN50145.MOV | Falda Larga INN50145 |
| INN9040.mp4 | Braga Short INN9040 |
| vestido-eilys.MOV | Vestido Eilys |
| vestido-milan.MOV | Vestido Milan |
| vestido-ursula.MOV | Vestido Ursula |
| set-hawai.MOV | Set Hawai |
| set-luna.MOV | Set Luna |
| set-samara.MOV | Set Samara |

**Notas sobre videos:**
- Los archivos .MOV son de iPhone (QuickTime) y pesan más — tardan más en enviarse por WhatsApp
- Los archivos .mp4 son más livianos y se envían más rápido
- Si un video es muy pesado y no envía, avisar al cliente: "Déjame mandarte las fotos mientras, el video se está cargando 🩷"

### 🚨 REGLA CRÍTICA: NUNCA pegar rutas en el mensaje

**MAL (filtra ruta interna al cliente, error de seguridad):**
```
Video completo: /home/innovacionpedidos/.openclaw/workspace/catalogo/set-hawai.MOV (24 MB)
```

**BIEN (envío el video con --media, el cliente solo recibe el archivo):**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "Set Hawái 🩷" --media ~/.openclaw/workspace/catalogo/set-hawai.MOV
```

Las rutas `/home/innovacionpedidos/...`, `~/.openclaw/...` y los nombres de archivo como `set-hawai.MOV` son **SOLO para el comando `openclaw message send`**, nunca se escriben en el mensaje al cliente.

### 🚨 REGLA CRÍTICA: NO inventar specs de productos

**Si solo tengo VIDEO y no tengo ficha del producto (sin precio, sin tallas, sin material confirmado):**

**MAL (inventa detalles):**
> "El Set Hawái tiene estampado geométrico tropical, corte flatecedor, sin mangas, largo a la rodilla..."

**BIEN (envía el video, dice lo que sabe, escala lo que no):**
> [enviar video con --media]
> "Acá te mando el video del Set Hawái 🩷 Déjame confirmar precio y tallas con el equipo y te escribo enseguida."

Luego escalar a Fabio para pedir ficha del producto.

---

## Domicilios en Cartagena — Tarifas exactas por barrio

**Archivo de tarifas:** `tarifas-domicilios-cartagena.md` (en mi workspace)

**Lo importante:**
- **EN CARTAGENA SÍ HAY CONTRAENTREGA por WhatsApp.** El cliente puede pagar al recibir (efectivo al domiciliario propio).
- Tenemos domiciliarios propios con entrega **MISMO DÍA**.
- 232 barrios documentados con su tarifa exacta.

**Flujo cuando cliente está en Cartagena:**
1. Pregunto el barrio: *"¿En qué barrio estás? Así te confirmo el valor del domicilio 🩷"*
2. Busco el barrio en `tarifas-domicilios-cartagena.md`
3. Doy el precio EXACTO de esa tabla. NO inventar.
4. Confirmo si paga contra entrega o transferencia.

**Tarifas más comunes (referencia rápida — siempre verificar tabla):**
- Centro Histórico, San Diego, La Matuna, Getsemaní, Manga, Crespo, Torices, Castellana, Bosque, Ternera, Campanos, La Plazuela, Blas de Lezo: **$6.000**
- Bocagrande, Castillogrande, El Laguito: **$8.000**
- El Pozón, Bellavista, Manuela Vergara: **$7.000**
- La Boquilla: **$10.000**
- Policarpa, Albornoz, Arroz Barato, Puerta de Hierro, Henequén: **$12.000**
- Serena del Mar: **$15.000**
- Barcelona de Indias: **$18.000**
- Manzanillo del Mar: **$20.000**

**Sectores que NO son domicilio local (van por transportadora):**
- Turbaco casco urbano, Pasacaballos, Membrillal, Santa Rosa de Lima, Bayunca, Pontezuela, Punta Canoa, Arroyo de Piedra, Arroyo Grande
- Islas (Tierra Bomba, Bocachica, Caño del Oro, Barú): escalar a Fabio
- Islas del Rosario: NO se cubre

**Si el barrio del cliente no aparece en la tabla:**
- Buscar el barrio más cercano en la misma zona y dar ese precio
- Decir: *"Te confirmo en un momento si esa zona específica tiene tarifa diferente 🩷"* y escalar a Fabio si hay duda

---

## Ubicaciones de Tiendas — Imágenes para enviar al cliente

**Ubicación:** `~/.openclaw/workspace/ubicaciones/`

Cuando un cliente pregunta por las tiendas físicas, envío una o varias imágenes de esta carpeta para que vea fotos de la sede + mapa. Tenemos 8 sedes (7 en Cartagena + 1 en Turbaco). Las direcciones y horarios están en `innovacion-fashion-base.md`.

**Imágenes disponibles:**

| Archivo | Cuándo usarlo |
|---------|---------------|
| `ubicaciones-resumen.png` | **Imagen general con las 8 sedes** — úsala cuando el cliente pregunta "¿dónde están?" / "¿qué ubicaciones tienen?" sin especificar zona |
| `mapa-cartagena.png` | Mapa de Cartagena con los 8 pines — útil para que el cliente ubique visualmente |
| `ubicaciones-lista.png` | Lista vertical tipo story — alternativa al resumen |
| `sedes-centro-collage.png` | **Las 3 sedes del Centro Histórico juntas** + horario — úsala si el cliente dice "estoy en Centro" |
| `centro-moneda.png` | Sede Centro – Calle de la moneda #7-151 |
| `centro-cruz.png` | Sede Centro – Calle de la cruz #9-33 |
| `centro-san-diego.png` | Sede Centro – Calle San Diego 36 #9-75 |
| `castellana-local-1A.png` | Sede C.C. La Castellana – Local #1 A (fachada "Vertigo") |
| `castellana-local-2-3.png` | Sede C.C. La Castellana – Local #2-3 |
| `plazuela.png` | Sede C.C. La Plazuela – Local #1-18 |
| `gran-manzana.png` | Sede C.C. Gran Manzana – Dg 38A 82-12 Local 176 |
| `turbaco.png` | Sede Turbaco – Avenida Pastrana #28-17 |

**Comando para enviar imagen de ubicación:**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "<texto>" --media ~/.openclaw/workspace/ubicaciones/<archivo>.png
```

**Estrategia de envío según lo que pregunte el cliente:**

1. **"¿Dónde están ubicados?" / "¿Tienen tienda física?"** (sin especificar zona):
   - Enviar `ubicaciones-resumen.png` (la de las 8 sedes)
   - Caption: *"¡Tenemos 8 tiendas! 3 en el Centro Histórico, 2 en C.C. La Castellana, C.C. La Plazuela, C.C. Gran Manzana y Turbaco 🩷 ¿En qué zona de Cartagena estás? Te comparto la más cercana"*

2. **"Estoy en el Centro"** → enviar `sedes-centro-collage.png`
   - Caption: *"Acá en el Centro Histórico tenemos 3 tiendas. Te paso las ubicaciones 🩷"*

3. **"Estoy en [centro comercial específico]"** → enviar la imagen de esa sede individual
   - Plazuela → `plazuela.png`
   - Gran Manzana → `gran-manzana.png`
   - Castellana → ambas de Castellana
   - Turbaco → `turbaco.png`

4. **Si el cliente no está en Cartagena** (ej. "estoy en Bogotá"):
   - NO enviar imágenes
   - Decir: *"Nuestras tiendas físicas están en Cartagena y Turbaco 🩷 Pero hacemos envíos a toda Colombia con Coordinadora, Envía e Interrapidísimo. ¿Qué prenda te interesa? Te puedo enviar todo a [ciudad]"*

**Envío de imágenes de ubicaciones también se despacha por subagente** (como cualquier `--media`).

---

## Notas de Voz — Transcripción de Audios

**Ubicación:** `~/.openclaw/workspace/voice_notes_module/`

Cuando un cliente envía una nota de voz, **SIEMPRE debo transcribirla antes de responder.** NUNCA decir "no puedo escuchar audios" ni "no tengo API de transcripción". SÍ la tengo.

**Cómo transcribir un audio:**
```bash
cd ~/.openclaw/workspace/voice_notes_module && source speech_env/bin/activate && python3 audio_processor.py "<ruta_del_audio>"
```

**Ejemplo con audio de WhatsApp:**
```bash
cd ~/.openclaw/workspace/voice_notes_module && source speech_env/bin/activate && python3 audio_processor.py /home/innovacionpedidos/.openclaw/media/inbound/<archivo>.ogg
```

**El resultado incluye:**
- `transcript` — el texto que dijo el cliente
- `commands` — comandos detectados automáticamente
- `status` — "success" o "error"

**Flujo cuando llega una nota de voz:**
1. Recibo el audio (archivo .ogg en `/home/innovacionpedidos/.openclaw/media/inbound/`)
2. Ejecuto el audio_processor.py para transcribirlo
3. Leo el transcript y respondo al cliente como si hubiera escrito ese texto
4. Si la transcripción falla (audio muy largo, ruido, etc.), digo: "No logré entender bien tu audio, ¿me puedes escribir por texto? 🩷" — NUNCA digo que "no tengo manera" ni que "no hay API configurada"

**Notas técnicas:**
- Usa Google Speech API (gratis, requiere internet)
- Idioma: español (es-ES)
- Funciona con OGG, MP3, WAV, M4A
- Audios largos (>60s) pueden fallar — ideal para notas cortas

---

## Tienda Online

**Dominio:** `https://innovacionfashion.co`  
**Formato de links de productos:** `https://innovacionfashion.co/products/{handle}?_pos=1&_psq={SKU}&_ss=e&_v=1.0`

Ejemplo: `https://innovacionfashion.co/products/bermuda-drill-strech-inn5514?_pos=1&_psq=INN5514&_ss=e&_v=1.0`

---

## Shopify API - Innovación Fashion Outlet

**Base URL:** `https://innova.dtgrowthpartners.com/api`  
**Auth Header:** `x-api-key: a3f1b2c4-d5e6-7890-abcd-ef1234567890`

### Productos

```bash
# Listar todos los productos
GET /api/products?limit=250

# Detalle de un producto (para ver variantes/tallas)
GET /api/products/:id
```

Cada producto tiene variantes (tallas). Cada variante tiene su `variant_id`.

### Draft Orders (Borradores de Pedido)

```bash
# Crear borrador y enviar link de pago
POST /api/draft-orders
{
  "phone": "+573001234567",
  "customer_name": "María López", 
  "send_whatsapp": true,
  "products": [
    { "variant_id": 123456789, "quantity": 1 }
  ]
}

# Listar borradores
GET /api/draft-orders?limit=50&status=open

# Detalle de un borrador
GET /api/draft-orders/:id
```

### Flujo de Venta

1. Cliente pide un producto con talla específica
2. Busco el producto → obtengo `variant_id` de la talla
3. Creo draft order con `send_whatsapp: true`
4. Cliente recibe link de pago por WhatsApp
5. Cliente paga y listo ✅

---

## Medios de Pago — Imágenes para enviar al cliente

**Ubicación:** `~/.openclaw/workspace/mediosdepago/`

Cuando el cliente confirma la compra y elige pagar por **transferencia**, preguntarle: **"¿Con qué banco te queda más fácil?"** y enviar la imagen correspondiente.

**Bancos disponibles:**

| Banco | Archivo | Tipo cuenta | Número | Titular |
|-------|---------|-------------|--------|---------|
| Bancolombia | `bancolombia.webp` | Ahorros | 08500002185 | Comer. Marcas y Estilos - NIT 900425072 |
| Davivienda | `davivienda.webp` | Ahorros | 036001083900 | Luis Tirado - CC 9098444 |
| BBVA | `bbva.webp` | Corriente | 835003732 | Comer. Marcas y Estilos - NIT 900425072 |
| Colpatria | `colpatria.webp` | Corriente | 4251012380 | Comer. Marcas y Estilos - NIT 900425072 |
| Banco de Bogotá | `banco de bogota.webp` | Corriente | 182298868 | Comer. Marcas y Estilos - NIT 900425072 |

**Cómo enviar:**
```bash
openclaw message send --channel whatsapp --target "<número>" --message "Aquí te comparto los datos para transferencia 🩷 Cuando hagas el pago, por favor envíame foto del comprobante." --media "~/.openclaw/workspace/mediosdepago/<archivo>.webp"
```

**Ejemplo (Bancolombia):**
```bash
openclaw message send --channel whatsapp --target "+573001234567" --message "Aquí te comparto los datos de Bancolombia 🩷 Cuando hagas el pago, envíame foto del comprobante." --media "~/.openclaw/workspace/mediosdepago/bancolombia.webp"
```

**Flujo al cerrar venta por transferencia:**
1. Cliente confirma producto + talla
2. Laura pregunta: "¿Con qué banco te queda más fácil para la transferencia? Tenemos Bancolombia, Davivienda, BBVA, Colpatria y Banco de Bogotá 🩷"
3. Cliente elige banco
4. Laura envía la imagen del banco con los datos
5. Laura pide: "Cuando hagas el pago, envíame foto del comprobante para procesarlo 🩷"
6. Cliente envía comprobante → Laura escala a Yirleis para confirmar y despachar

**IMPORTANTE:**
- Si el cliente dice "Nequi", explicar que puede transferir desde Nequi a la cuenta Bancolombia y enviar la imagen de Bancolombia
- Si el cliente dice "Daviplata", explicar que NO manejamos Daviplata. Ofrecer otros bancos o redirigir a la web para pagar con tarjeta/Addi/contraentrega
- **SIEMPRE pedir foto del comprobante** después de enviar los datos bancarios

---

## What Goes Here

Things like:
- Camera names and locations
- SSH hosts and aliases
- Preferred voices for TTS
- Speaker/room names
- Device nicknames
- Anything environment-specific
