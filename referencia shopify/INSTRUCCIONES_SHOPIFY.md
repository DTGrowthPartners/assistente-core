# Instrucciones para obtener credenciales de Shopify (Custom App)

## Prerequisitos
- Tener acceso al admin de la tienda Shopify
- Tener permisos de propietario o colaborador con acceso a desarrollo de apps

## Paso 1: Acceder al Admin
1. Ir a `https://admin.shopify.com`
2. Iniciar sesion con las credenciales de la tienda
3. Seleccionar la tienda si hay varias disponibles

## Paso 2: Habilitar desarrollo de apps custom
1. En el menu lateral, ir a **Settings** (icono de engranaje, abajo a la izquierda)
2. Click en **Apps and sales channels**
3. Click en **Develop apps** (boton en la parte superior de la pagina)
4. Si aparece un banner pidiendo habilitar el desarrollo de apps:
   - Click en **Allow custom app development**
   - Confirmar en el dialogo que aparece
5. Si ya esta habilitado, continuar al paso 3

## Paso 3: Crear la Custom App
1. Click en el boton **Create an app**
2. En el campo "App name" escribir: `Bot Chancletas`
3. Seleccionar el desarrollador (email del usuario actual)
4. Click en **Create app**

## Paso 4: Configurar permisos (API Scopes)
1. Dentro de la app recien creada, click en **Configure Admin API scopes**
2. Buscar y marcar los siguientes permisos:
   - `read_orders` (en la seccion Orders)
   - `read_products` (en la seccion Products)
   - `read_inventory` (en la seccion Inventory)
3. Click en **Save** para guardar los permisos

## Paso 5: Instalar la app
1. Click en la pestana **API credentials** (parte superior)
2. Click en el boton **Install app**
3. Confirmar la instalacion en el dialogo que aparece

## Paso 6: Copiar las credenciales
1. Despues de instalar, en la seccion **Admin API access token**:
   - Click en **Reveal token once** (IMPORTANTE: solo se muestra una vez)
   - Copiar el token (empieza con `shpat_`)
2. En la seccion superior de la misma pagina, copiar tambien:
   - **API key** (si se necesita en el futuro)
   - **API secret key** (si se necesita en el futuro)
3. El **Store URL** es el dominio de la tienda: `nombre-tienda.myshopify.com`
   - Se puede ver en la barra de direcciones o en Settings > Domains

## Paso 7: Guardar en el archivo .env
Crear o editar el archivo `.env` en la raiz del proyecto con estos valores:

```
SHOPIFY_STORE_URL=nombre-tienda.myshopify.com
SHOPIFY_ACCESS_TOKEN=shpat_xxxxxxxxxxxxxxxxxxxxxxxxxxxx
API_KEY=generar-una-clave-secreta-aleatoria
```

Nota: `API_KEY` no es de Shopify, es una clave que se define para proteger los endpoints de esta API. Puede ser cualquier string seguro (ejemplo: un UUID como `a3f1b2c4-d5e6-7890-abcd-ef1234567890`).

## Validacion
Para verificar que las credenciales funcionan, iniciar el servidor y hacer una peticion de prueba:

```bash
npm start
# En otra terminal:
curl http://localhost:3002/api/products -H "x-api-key: TU_API_KEY"
```

Si la respuesta contiene productos de la tienda, la configuracion es correcta.
