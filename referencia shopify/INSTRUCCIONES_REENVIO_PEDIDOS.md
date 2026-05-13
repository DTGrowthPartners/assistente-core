# Instrucciones: Reenvío escalonado de WhatsApp a pedidos pendientes

## ⚠️ Importante
- **NO usar la funcionalidad de carritos abandonados.** El cron de carritos abandonados está deshabilitado por ahora. Ignora cualquier endpoint relacionado (`/api/abandoned-carts/*`) y no programes mensajes para checkouts abandonados.
- Esta tarea aplica **únicamente para pedidos reales (orders)** que quedaron sin notificación por la caída del bot.

## Contexto
Cuando el bot de WhatsApp estuvo caído, varios pedidos de Shopify llegaron pero no recibieron el mensaje de confirmación automático. Esos pedidos quedan marcados con la nota:

> "❌ No se pudo enviar WhatsApp al cliente {nombre} ({telefono}), requiere contacto manual"

Tu tarea es **reenviar el mensaje de confirmación a esos pedidos**, pero de forma escalonada para evitar que Meta detecte patrones de bot y nos bloquee la cuenta.

---

## Reglas obligatorias

### 1. Intervalos aleatorios entre envíos
- **NO** envíes los mensajes seguidos.
- Entre cada envío debe haber un intervalo **aleatorio entre 5 y 8 minutos**.
- Calcula un valor diferente para cada pedido (no uses siempre 5 ni siempre 8).
- Ejemplo de cálculo: `intervalo = 5 + random(0..3) minutos`

### 2. Orden aleatorio
- **NO** envíes los pedidos en orden secuencial (#2920, #2921, #2922...).
- Mezcla la lista antes de empezar para que no haya un patrón cronológico.

### 3. Horario humano
- Solo envía mensajes entre las **8:00 AM y 8:00 PM hora Colombia (America/Bogota)**.
- Si el siguiente envío caería fuera de ese rango, espera hasta el día siguiente a las 8:00 AM.

### 4. Validación previa
Antes de cada reenvío:
- Verifica que el pedido **no haya sido confirmado manualmente** ya (revisa la nota del pedido por si una asesora lo contactó).
- Si el pedido ya tiene una nota tipo "✅ confirmado" o "✅ contactado manualmente", **sáltalo**.

---

## Cómo programar cada envío (CronCreate)

Para cada pedido pendiente, debes crear un cron único usando la herramienta `CronCreate` con un schedule que se ejecute **una sola vez** en el momento calculado.

### Endpoint a llamar
```
POST https://innova.dtgrowthpartners.com/api/orders/{ORDER_ID}/resend-whatsapp
Header: x-api-key: a3f1b2c4-d5e6-7890-abcd-ef1234567890
```

### Flujo recomendado
1. **Lista los pedidos pendientes** usando `GET /api/orders?financial_status=pending&limit=50` o el filtro que aplique.
2. **Filtra** los que tengan la nota de "requiere contacto manual" y NO tengan confirmación posterior.
3. **Mezcla** la lista aleatoriamente.
4. **Calcula** la hora de envío de cada uno acumulando intervalos aleatorios desde ahora:
   - Pedido 1: ahora + 0 min
   - Pedido 2: hora_anterior + (5–8 min aleatorio)
   - Pedido 3: hora_anterior + (5–8 min aleatorio)
   - ...
5. **Verifica horario humano** (8 AM – 8 PM Bogotá). Si una hora se sale, recálcala para el día siguiente 8 AM + offset.
6. **Crea un trigger** con `CronCreate` para cada pedido que ejecute la llamada al endpoint.
7. **Reporta** al usuario la lista completa con: order_number, cliente, hora programada de envío.

### Ejemplo de prompt para cada cron
```
Llama POST https://innova.dtgrowthpartners.com/api/orders/{ORDER_ID}/resend-whatsapp
con header x-api-key: a3f1b2c4-d5e6-7890-abcd-ef1234567890.
Si la respuesta es success:true, reporta "✅ Enviado a {cliente}".
Si falla, reporta el error y NO reintentes automáticamente.
```

---

## Reglas de seguridad

- **NO** envíes más de **15 mensajes por hora** en total (combinando todos los crons).
- **NO** envíes el mismo mensaje dos veces al mismo cliente. El endpoint deja una nota de "REENVIADO" en el pedido — verifica que no esté antes de programar.
- Si Meta empieza a rechazar mensajes (errores tipo "blocked" o "rate limit"), **detén inmediatamente todos los crons pendientes** con `CronDelete` y avisa al usuario.

---

## Después de programar

Cuando termines de crear todos los crons, devuelve un resumen así:

```
Programados {N} reenvíos:

🕐 14:32 - Pedido #2918 - María López
🕑 14:39 - Pedido #2924 - Andrea Gómez
🕒 14:45 - Pedido #2920 - Carolina Ruiz
...

Próximo envío en X minutos. Total estimado: ~{HH:MM}
```
