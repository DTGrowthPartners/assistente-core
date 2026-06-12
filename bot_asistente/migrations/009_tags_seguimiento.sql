-- ============================================================================
-- Migración 009 — Tags de seguimiento (multi-etiqueta por cliente)
-- ============================================================================
-- La columna `clientes.etiqueta` (cliente/prospecto/equipo/personal) sigue
-- controlando el routing del bot. Esta migración suma una segunda capa de
-- "tags" libres para que el equipo haga seguimiento: 'propuesta enviada',
-- 'falta agendar', 'reunión hecha', etc.
--
-- Modelo: many-to-many entre clientes y tags.
-- ============================================================================

CREATE TABLE IF NOT EXISTS tags (
    id SERIAL PRIMARY KEY,
    nombre VARCHAR(50) NOT NULL UNIQUE,
    color VARCHAR(7) NOT NULL DEFAULT '#7C3AED',  -- hex como #RRGGBB
    descripcion TEXT,
    orden INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by VARCHAR(60)
);

CREATE INDEX IF NOT EXISTS idx_tags_orden ON tags(orden, nombre);

CREATE TABLE IF NOT EXISTS cliente_tags (
    cliente_id INT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    tag_id INT NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    added_by VARCHAR(60),
    PRIMARY KEY (cliente_id, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_cliente_tags_tag ON cliente_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_cliente_tags_cliente ON cliente_tags(cliente_id);

-- Seed inicial con tags útiles para el embudo de ventas DTGP.
INSERT INTO tags (nombre, color, descripcion, orden, created_by) VALUES
    ('Falta agendar',     '#F59E0B', 'Mostró interés pero aún no fija una cita',     10, 'seed_009'),
    ('Cita agendada',     '#3B82F6', 'Tiene reunión programada en Cal.com',          20, 'seed_009'),
    ('Reunión hecha',     '#10B981', 'Ya tuvimos la reunión de diagnóstico',         30, 'seed_009'),
    ('Propuesta enviada', '#8B5CF6', 'Se le envió cotización o propuesta formal',    40, 'seed_009'),
    ('Cerrado / ganado',  '#059669', 'Convertido a cliente activo',                  50, 'seed_009'),
    ('Perdido',           '#6B7280', 'No avanzó · no responde · no interesado',      60, 'seed_009'),
    ('No fit',            '#9CA3AF', 'Negocio fuera de nuestro foco / no calza',     70, 'seed_009'),
    ('Seguir en X días',  '#EAB308', 'Marcado para retomar después',                 80, 'seed_009')
ON CONFLICT (nombre) DO NOTHING;

DO $$
DECLARE n INT;
BEGIN
    SELECT COUNT(*) INTO n FROM tags;
    RAISE NOTICE 'Migración 009 lista. Tags totales: %', n;
END $$;
