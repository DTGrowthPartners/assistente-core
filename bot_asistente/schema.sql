-- ============================================================================
-- Bot Laura — Schema PostgreSQL
-- ============================================================================
-- Ejecutar como superuser:
--   sudo -u postgres psql -d laurabot -f schema.sql
-- ============================================================================

-- Extensiones necesarias
CREATE EXTENSION IF NOT EXISTS pg_trgm;       -- búsqueda fuzzy de barrios
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";   -- para IDs adicionales si hace falta

-- ============================================================================
-- CLIENTES
-- ============================================================================
CREATE TABLE IF NOT EXISTS clientes (
    id              SERIAL PRIMARY KEY,
    numero_whatsapp VARCHAR(20) UNIQUE NOT NULL,
    nombre          VARCHAR(255),
    ciudad          VARCHAR(100),
    barrio          VARCHAR(150),
    primer_contacto TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ultimo_contacto TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    es_mayorista    BOOLEAN NOT NULL DEFAULT FALSE,
    bloqueado       BOOLEAN NOT NULL DEFAULT FALSE,
    razon_bloqueo   TEXT,
    metadata        JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_clientes_whatsapp ON clientes(numero_whatsapp);

COMMENT ON TABLE clientes IS 'Clientes que han escrito alguna vez por WhatsApp';
COMMENT ON COLUMN clientes.bloqueado IS 'TRUE para +573206811130 (dueño) y otros números prohibidos';

-- ============================================================================
-- CONVERSACIONES (historial completo, inbound + outbound)
-- ============================================================================
CREATE TABLE IF NOT EXISTS conversaciones (
    id                 BIGSERIAL PRIMARY KEY,
    cliente_id         INT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    whapi_message_id   VARCHAR(100),
    direccion          VARCHAR(10) NOT NULL CHECK (direccion IN ('inbound','outbound','humano')),
    tipo               VARCHAR(20) NOT NULL CHECK (tipo IN ('texto','imagen','audio','video','pdf','sticker','documento','ubicacion','contacto','desconocido')),
    contenido          TEXT,
    media_url          TEXT,
    media_path_local   TEXT,
    intent             VARCHAR(50),
    tokens_input       INT,
    tokens_output      INT,
    cache_read_tokens  INT,
    cache_create_tokens INT,
    costo_usd          NUMERIC(10, 6),
    modelo             VARCHAR(50),
    timestamp          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    metadata           JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_conv_cliente_ts ON conversaciones(cliente_id, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_conv_intent ON conversaciones(intent) WHERE intent IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conv_whapi_msg ON conversaciones(whapi_message_id) WHERE whapi_message_id IS NOT NULL;

COMMENT ON COLUMN conversaciones.direccion IS 'inbound=cliente, outbound=Laura, humano=asesora humana desde la misma línea';

-- ============================================================================
-- DEDUPE WEBHOOKS (whapi reintenta hasta 3 veces)
-- ============================================================================
CREATE TABLE IF NOT EXISTS webhooks_procesados (
    message_id   VARCHAR(100) PRIMARY KEY,
    procesado_en TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_webhooks_ts ON webhooks_procesados(procesado_en);

-- Función para limpiar webhooks viejos (llamar con cron cada hora)
CREATE OR REPLACE FUNCTION limpiar_webhooks_antiguos() RETURNS INT AS $$
DECLARE eliminados INT;
BEGIN
    DELETE FROM webhooks_procesados WHERE procesado_en < NOW() - INTERVAL '24 hours';
    GET DIAGNOSTICS eliminados = ROW_COUNT;
    RETURN eliminados;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- INTERVENCIÓN HUMANA (pausar bot cuando asesora interviene)
-- ============================================================================
CREATE TABLE IF NOT EXISTS intervencion_humana (
    cliente_id      INT PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,
    pausado_hasta   TIMESTAMPTZ NOT NULL,
    razon           TEXT,
    activado_en     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    activado_por    VARCHAR(50)
);
CREATE INDEX IF NOT EXISTS idx_intervencion_hasta ON intervencion_humana(pausado_hasta);

-- ============================================================================
-- SESIONES (contexto comprimido de cada conversación activa)
-- ============================================================================
CREATE TABLE IF NOT EXISTS sesiones (
    cliente_id            INT PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,
    estado                VARCHAR(50) NOT NULL DEFAULT 'inicial',
    producto_actual_ref   VARCHAR(50),
    productos_mostrados   JSONB NOT NULL DEFAULT '[]'::jsonb,
    talla_interes         VARCHAR(20),
    color_interes         VARCHAR(50),
    metodo_pago_elegido   VARCHAR(50),
    banco_elegido         VARCHAR(50),
    barrio                VARCHAR(150),
    direccion_envio       TEXT,
    notas_internas        TEXT,
    ultima_interaccion    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    proximo_seguimiento   TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_sesiones_seguimiento ON sesiones(proximo_seguimiento)
    WHERE proximo_seguimiento IS NOT NULL;

COMMENT ON COLUMN sesiones.estado IS 'inicial|saludo|explorando|mostrando_opciones|preguntando_talla|preguntando_color|preguntando_barrio|esperando_datos|esperando_pago|esperando_comprobante|comprobante_recibido|cerrada_venta|sin_venta';

-- ============================================================================
-- PEDIDOS
-- ============================================================================
CREATE TABLE IF NOT EXISTS pedidos (
    id                          SERIAL PRIMARY KEY,
    cliente_id                  INT NOT NULL REFERENCES clientes(id),
    shopify_draft_order_id      BIGINT,
    shopify_draft_invoice_url   TEXT,
    items                       JSONB NOT NULL,
    subtotal                    NUMERIC(10,2) NOT NULL,
    domicilio                   NUMERIC(10,2) NOT NULL DEFAULT 0,
    total                       NUMERIC(10,2) NOT NULL,
    estado                      VARCHAR(30) NOT NULL DEFAULT 'cotizacion'
                                CHECK (estado IN ('cotizacion','datos_completos','esperando_pago','comprobante_recibido','confirmado','despachado','entregado','cancelado')),
    direccion_envio             TEXT,
    ciudad                      VARCHAR(100),
    barrio                      VARCHAR(150),
    metodo_pago                 VARCHAR(50),
    banco                       VARCHAR(50),
    comprobante_url             TEXT,
    notas                       TEXT,
    notificado_a_fabio_en       TIMESTAMPTZ,
    confirmado_por_fabio_en     TIMESTAMPTZ,
    created_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at                  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_pedidos_cliente ON pedidos(cliente_id);
CREATE INDEX IF NOT EXISTS idx_pedidos_estado ON pedidos(estado);
CREATE INDEX IF NOT EXISTS idx_pedidos_shopify ON pedidos(shopify_draft_order_id) WHERE shopify_draft_order_id IS NOT NULL;

-- ============================================================================
-- TARIFAS DE DOMICILIO (Cartagena, 232 barrios)
-- ============================================================================
CREATE TABLE IF NOT EXISTS tarifas_domicilio (
    id                     SERIAL PRIMARY KEY,
    barrio                 VARCHAR(200) NOT NULL,
    barrio_normalizado     VARCHAR(200) NOT NULL,
    zona                   VARCHAR(80),
    precio                 NUMERIC(10,2),
    tipo                   VARCHAR(20) NOT NULL CHECK (tipo IN ('domicilio_local','transportadora','no_cubre','evaluar')),
    notas                  TEXT,
    UNIQUE(barrio_normalizado)
);
CREATE INDEX IF NOT EXISTS idx_tarifas_barrio_trgm ON tarifas_domicilio
    USING gin(barrio_normalizado gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_tarifas_zona ON tarifas_domicilio(zona);

COMMENT ON TABLE tarifas_domicilio IS 'Tarifas exactas de domicilio por barrio en Cartagena, fuente: tarifas-domicilios-cartagena.md';

-- ============================================================================
-- CACHE DE PRODUCTOS (doble fuente: Shopify API + HTML catalogo-de-whatsapp)
-- ============================================================================
CREATE TABLE IF NOT EXISTS productos_cache (
    ref                  VARCHAR(50) PRIMARY KEY,
    origen               VARCHAR(20) NOT NULL DEFAULT 'shopify'
                         CHECK (origen IN ('shopify','html_catalogo','manual','openclaw_legacy')),
    -- 'shopify'        → viene de la API Shopify, tiene variant_id, se pueden crear draft orders
    -- 'html_catalogo'  → viene del HTML público, NO tiene variant_id pero IGUAL se vende
    --                    (Laura siempre dice "sí tenemos disponibilidad", no hay inventario real)
    -- 'manual'         → cargado a mano para emergencias
    -- 'openclaw_legacy'→ ref que existía en TOOLS.md del setup viejo
    shopify_product_id   BIGINT,
    shopify_handle       VARCHAR(255),
    fuente_url           TEXT,                       -- de qué URL vino (para auditoría)
    nombre               VARCHAR(255),
    descripcion          TEXT,
    categoria            VARCHAR(50),
    precio_detal         NUMERIC(10,2),
    precio_mayor         NUMERIC(10,2),
    tallas               JSONB NOT NULL DEFAULT '[]'::jsonb,
    colores              JSONB NOT NULL DEFAULT '[]'::jsonb,
    variants             JSONB NOT NULL DEFAULT '[]'::jsonb,
    -- variants para origen='shopify': [{variant_id, talla, color, precio, disponible}]
    -- variants para origen='html_catalogo': [] (no aplica, no hay variant_id)
    imagen_url           TEXT,
    imagen_url_extras    JSONB NOT NULL DEFAULT '[]'::jsonb,
    foto_local           VARCHAR(255),
    video_local          VARCHAR(255),
    asumir_disponible    BOOLEAN NOT NULL DEFAULT TRUE,
    -- regla del negocio: SIEMPRE asumir disponibilidad (no hay inventario en tiempo real)
    activo               BOOLEAN NOT NULL DEFAULT TRUE,
    sincronizado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    notas                TEXT
);
CREATE INDEX IF NOT EXISTS idx_productos_cat ON productos_cache(categoria) WHERE activo;
CREATE INDEX IF NOT EXISTS idx_productos_origen ON productos_cache(origen) WHERE activo;
CREATE INDEX IF NOT EXISTS idx_productos_shopify ON productos_cache(shopify_product_id) WHERE shopify_product_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_productos_nombre_trgm ON productos_cache USING gin(nombre gin_trgm_ops);

COMMENT ON COLUMN productos_cache.origen IS 'shopify=API con variant_id (draft orders posibles); html_catalogo=HTML público (SIN variant_id, pero igual se vende — Laura asume disponibilidad)';
COMMENT ON COLUMN productos_cache.asumir_disponible IS 'Sin inventario en tiempo real. TRUE = Laura dice "sí tenemos" cuando preguntan.';

-- ============================================================================
-- ALERTAS A FABIO (cola de escalaciones)
-- ============================================================================
CREATE TABLE IF NOT EXISTS alertas_fabio (
    id            SERIAL PRIMARY KEY,
    cliente_id    INT REFERENCES clientes(id) ON DELETE SET NULL,
    tipo          VARCHAR(50) NOT NULL
                  CHECK (tipo IN ('comprobante_pago','ref_desconocida','queja','mensaje_dueno','pedido_confirmado','duda_mayorista','error_sistema','otro')),
    mensaje       TEXT NOT NULL,
    media_url     TEXT,
    whapi_message_id VARCHAR(100),
    enviado_a_fabio_en TIMESTAMPTZ,
    resuelto      BOOLEAN NOT NULL DEFAULT FALSE,
    resuelto_en   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_alertas_resuelto ON alertas_fabio(resuelto, created_at);

-- ============================================================================
-- LOG DE COSTOS (rollup diario para no escanear conversaciones)
-- ============================================================================
CREATE TABLE IF NOT EXISTS costos_diarios (
    fecha               DATE PRIMARY KEY,
    total_mensajes      INT NOT NULL DEFAULT 0,
    total_clientes      INT NOT NULL DEFAULT 0,
    total_tokens_input  BIGINT NOT NULL DEFAULT 0,
    total_tokens_output BIGINT NOT NULL DEFAULT 0,
    total_cache_read    BIGINT NOT NULL DEFAULT 0,
    total_costo_usd     NUMERIC(12, 6) NOT NULL DEFAULT 0,
    pedidos_creados     INT NOT NULL DEFAULT 0,
    actualizado_en      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- ============================================================================
-- TRIGGERS
-- ============================================================================
CREATE OR REPLACE FUNCTION trigger_set_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_pedidos_updated ON pedidos;
CREATE TRIGGER trg_pedidos_updated
    BEFORE UPDATE ON pedidos
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- Trigger para mantener ultimo_contacto del cliente actualizado
CREATE OR REPLACE FUNCTION trigger_actualizar_ultimo_contacto()
RETURNS TRIGGER AS $$
BEGIN
    UPDATE clientes SET ultimo_contacto = NEW.timestamp WHERE id = NEW.cliente_id;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_conv_ultimo_contacto ON conversaciones;
CREATE TRIGGER trg_conv_ultimo_contacto
    AFTER INSERT ON conversaciones
    FOR EACH ROW EXECUTE FUNCTION trigger_actualizar_ultimo_contacto();

-- ============================================================================
-- SEMILLAS — números bloqueados
-- ============================================================================
INSERT INTO clientes (numero_whatsapp, nombre, bloqueado, razon_bloqueo)
VALUES
    ('+573206811130', 'Luis Tirado (DUEÑO - PROHIBIDO)', TRUE,
     'Número del dueño del negocio. Bloqueo bidireccional. Si llega un mensaje, alertar a Fabio sin responder.')
ON CONFLICT (numero_whatsapp) DO UPDATE
    SET bloqueado = TRUE,
        razon_bloqueo = EXCLUDED.razon_bloqueo;

-- ============================================================================
-- VISTAS ÚTILES
-- ============================================================================

-- Vista: últimos 10 mensajes por cliente (para construir contexto rápido)
CREATE OR REPLACE VIEW vista_ultimos_mensajes AS
SELECT
    c.cliente_id,
    c.direccion,
    c.tipo,
    c.contenido,
    c.timestamp,
    c.intent,
    ROW_NUMBER() OVER (PARTITION BY c.cliente_id ORDER BY c.timestamp DESC) AS posicion_desc
FROM conversaciones c;

-- Vista: estado del funnel
CREATE OR REPLACE VIEW vista_funnel AS
SELECT
    estado,
    COUNT(*) AS cantidad,
    SUM(total) AS total_acumulado
FROM pedidos
WHERE created_at >= NOW() - INTERVAL '7 days'
GROUP BY estado
ORDER BY estado;

-- ============================================================================
-- VERIFICACIÓN FINAL
-- ============================================================================
DO $$
DECLARE
    n_tablas INT;
BEGIN
    SELECT COUNT(*) INTO n_tablas
    FROM information_schema.tables
    WHERE table_schema = 'public'
      AND table_name IN (
        'clientes', 'conversaciones', 'webhooks_procesados',
        'intervencion_humana', 'sesiones', 'pedidos',
        'tarifas_domicilio', 'productos_cache', 'alertas_fabio',
        'costos_diarios'
      );
    IF n_tablas <> 10 THEN
        RAISE EXCEPTION 'Se esperaban 10 tablas, se crearon %', n_tablas;
    END IF;
    RAISE NOTICE '✅ Schema creado correctamente — % tablas', n_tablas;
END $$;
