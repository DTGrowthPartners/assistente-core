-- ============================================================================
-- Migración 006 — Memoria evolutiva + recordatorios (pilares openclaw)
-- ============================================================================
-- `memorias`: cosas que María APRENDE (reglas, hechos, preferencias). Se cargan
-- al system prompt cada turn según el scope (general o por contacto).
-- `recordatorios`: cosas pendientes con fecha (promesas, follow-ups, seguimientos).
-- El heartbeat los lee y decide si actuar.
-- ============================================================================

CREATE TABLE IF NOT EXISTS memorias (
    id           SERIAL PRIMARY KEY,
    scope        VARCHAR(20) NOT NULL CHECK (scope IN ('general','contacto','equipo')),
    contacto_id  INT REFERENCES clientes(id) ON DELETE CASCADE,
    titulo       VARCHAR(180) NOT NULL,
    contenido    TEXT NOT NULL,
    tipo         VARCHAR(30) NOT NULL DEFAULT 'regla'
                 CHECK (tipo IN ('regla','hecho','preferencia','aprendizaje','recordatorio_persistente')),
    activa       BOOLEAN NOT NULL DEFAULT TRUE,
    creado_por   VARCHAR(60),
    tags         JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    -- Si scope='contacto' debe traer contacto_id; si 'general' no.
    CONSTRAINT memorias_scope_contacto CHECK (
        (scope = 'contacto' AND contacto_id IS NOT NULL)
        OR (scope IN ('general','equipo') AND contacto_id IS NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_memorias_activas ON memorias(scope, contacto_id) WHERE activa;
CREATE INDEX IF NOT EXISTS idx_memorias_updated ON memorias(updated_at DESC);

DROP TRIGGER IF EXISTS trg_memorias_updated ON memorias;
CREATE TRIGGER trg_memorias_updated BEFORE UPDATE ON memorias
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

COMMENT ON TABLE memorias IS 'Cosas que María aprende y recuerda entre conversaciones. scope=general (todas), contacto (solo ese), equipo (el equipo DTGP).';

-- ── Recordatorios (promesas, follow-ups, seguimientos) ─────────────────────
CREATE TABLE IF NOT EXISTS recordatorios (
    id              SERIAL PRIMARY KEY,
    contacto_id     INT REFERENCES clientes(id) ON DELETE CASCADE,
    accion          TEXT NOT NULL,                          -- qué hacer
    motivo          TEXT,                                    -- por qué / contexto
    vence_en        TIMESTAMPTZ NOT NULL,
    estado          VARCHAR(20) NOT NULL DEFAULT 'pendiente'
                    CHECK (estado IN ('pendiente','atendido','descartado')),
    origen          VARCHAR(30) NOT NULL DEFAULT 'manual'
                    CHECK (origen IN ('manual','promesa_detectada','seguimiento_auto','equipo')),
    creado_por      VARCHAR(60),
    atendido_en     TIMESTAMPTZ,
    atendido_notas  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_recordatorios_pendientes
    ON recordatorios(vence_en, estado) WHERE estado='pendiente';
CREATE INDEX IF NOT EXISTS idx_recordatorios_contacto
    ON recordatorios(contacto_id);

DROP TRIGGER IF EXISTS trg_recordatorios_updated ON recordatorios;
CREATE TRIGGER trg_recordatorios_updated BEFORE UPDATE ON recordatorios
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

COMMENT ON TABLE recordatorios IS 'Pendientes con fecha. El heartbeat los lee y decide si actuar (responder follow-up, escalar, etc.).';

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 006: memorias + recordatorios';
END $$;
