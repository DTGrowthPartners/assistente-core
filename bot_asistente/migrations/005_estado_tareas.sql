-- ============================================================================
-- Migración 005 — bot_estado + tareas_programadas
-- ============================================================================
-- Estas tablas existían en el deploy viejo de Innovación pero se habían creado
-- con SQL directo (no estaban en schema.sql ni en 002). Las definimos aquí para
-- que un deploy nuevo de María quede completo. Aplicar DESPUÉS de schema.sql
-- (necesita la función trigger_set_updated_at) y ANTES de 004 (que seedea tareas).
-- ============================================================================

CREATE TABLE IF NOT EXISTS bot_estado (
    id             INT PRIMARY KEY DEFAULT 1,
    activo         BOOLEAN NOT NULL DEFAULT TRUE,
    pausado_por    VARCHAR(80),
    pausado_en     TIMESTAMPTZ,
    razon          TEXT,
    actualizado_en TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT bot_estado_singleton CHECK (id = 1)
);
INSERT INTO bot_estado (id, activo) VALUES (1, TRUE) ON CONFLICT (id) DO NOTHING;

CREATE TABLE IF NOT EXISTS tareas_programadas (
    id                SERIAL PRIMARY KEY,
    nombre            VARCHAR(120) NOT NULL,
    cron              VARCHAR(60) NOT NULL,
    zona_horaria      VARCHAR(60) NOT NULL DEFAULT 'America/Bogota',
    accion            VARCHAR(60) NOT NULL,
    parametros        JSONB NOT NULL DEFAULT '{}'::jsonb,
    activo            BOOLEAN NOT NULL DEFAULT TRUE,
    ultima_ejecucion  TIMESTAMPTZ,
    proxima_ejecucion TIMESTAMPTZ,
    ultimo_resultado  JSONB,
    creado_por        VARCHAR(60),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_tareas_prox ON tareas_programadas(proxima_ejecucion) WHERE activo;

DROP TRIGGER IF EXISTS trg_tareas_updated ON tareas_programadas;
CREATE TRIGGER trg_tareas_updated BEFORE UPDATE ON tareas_programadas
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 005: bot_estado + tareas_programadas';
END $$;
