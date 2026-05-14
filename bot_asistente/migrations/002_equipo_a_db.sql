-- =============================================================================
-- Migración 002 — Mover equipo de YAML a Postgres
-- =============================================================================
-- Ejecutar:
--   PGPASSWORD='...' psql -h localhost -U asistente_user -d asistente_db \
--     -f migrations/002_equipo_a_db.sql
-- =============================================================================

-- Tabla de miembros del equipo (superiores que reciben escalaciones)
CREATE TABLE IF NOT EXISTS equipo_miembros (
    id              SERIAL PRIMARY KEY,
    nombre          VARCHAR(100) NOT NULL,
    numero_whatsapp VARCHAR(20) UNIQUE NOT NULL,
    rol             VARCHAR(50),
    -- Áreas de responsabilidad — array JSON de strings:
    --   ["pagos", "pedidos", "telas_calidad", "mayorista",
    --    "envios_nacionales", "tienda_fisica", "quejas", "tecnico", "otro"]
    areas           JSONB NOT NULL DEFAULT '[]'::jsonb,
    es_fallback     BOOLEAN NOT NULL DEFAULT FALSE,
    -- Horario opcional, formato libre tipo "08:00-22:00":
    horario_lunes_sabado VARCHAR(30),
    horario_domingo      VARCHAR(30),
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    notas           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_equipo_activo ON equipo_miembros(activo) WHERE activo;
CREATE INDEX IF NOT EXISTS idx_equipo_fallback ON equipo_miembros(es_fallback) WHERE es_fallback;

COMMENT ON TABLE equipo_miembros IS 'Personas del equipo interno que reciben escalaciones del bot. Editable desde /admin.';
COMMENT ON COLUMN equipo_miembros.es_fallback IS 'TRUE = recibe lo que ningún otro miembro cubre. Hoy: Fabio.';

-- Tabla de números internos (asesoras humanas + otros que el bot debe ignorar)
CREATE TABLE IF NOT EXISTS numeros_internos (
    id              SERIAL PRIMARY KEY,
    numero_whatsapp VARCHAR(20) UNIQUE NOT NULL,
    nombre          VARCHAR(100),
    razon           TEXT,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_numeros_internos_activo ON numeros_internos(activo) WHERE activo;

COMMENT ON TABLE numeros_internos IS 'Números del equipo que el bot ignora silenciosamente. Editable desde /admin.';

-- Triggers para updated_at
DROP TRIGGER IF EXISTS trg_equipo_updated ON equipo_miembros;
CREATE TRIGGER trg_equipo_updated
    BEFORE UPDATE ON equipo_miembros
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- =============================================================================
-- Seeds iniciales — mismos datos que data/equipo.yaml
-- =============================================================================

INSERT INTO equipo_miembros (
    nombre, numero_whatsapp, rol, areas, es_fallback,
    horario_lunes_sabado, horario_domingo, activo, notas
)
VALUES (
    'Fabio (test = Stiven)',
    '+573026444564',
    'administrador',
    '["pagos","pedidos","mayorista","otro"]'::jsonb,
    TRUE,
    '08:00-22:00', '10:00-18:00',
    TRUE,
    'Confirma pagos en la app del banco. Coordina despachos. TEST MODE: cambiar numero_whatsapp a +573019836645 cuando salga a producción.'
)
ON CONFLICT (numero_whatsapp) DO NOTHING;

INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo) VALUES
    ('+573243798269', 'Línea producción (asesoras)', 'Línea compartida con asesoras humanas — el bot no debe responder cuando ellas escriben', TRUE),
    ('+573206811130', 'Luis Tirado (dueño)', 'Bloqueo bidireccional por seguridad', TRUE)
ON CONFLICT (numero_whatsapp) DO NOTHING;

-- =============================================================================
-- Verificación
-- =============================================================================
DO $$
DECLARE
    n_miembros INT;
    n_internos INT;
BEGIN
    SELECT COUNT(*) INTO n_miembros FROM equipo_miembros WHERE activo;
    SELECT COUNT(*) INTO n_internos FROM numeros_internos WHERE activo;
    RAISE NOTICE '✅ Migración 002 OK: % miembros equipo, % números internos', n_miembros, n_internos;
END $$;
