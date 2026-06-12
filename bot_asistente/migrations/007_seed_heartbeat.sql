-- ============================================================================
-- Migración 007 — Seed del cron del heartbeat (proactividad openclaw-style)
-- ============================================================================
-- INACTIVO por defecto. Activar desde /admin/automatizaciones cuando se quiera
-- que María empiece a actuar proactivamente.
-- ============================================================================

INSERT INTO tareas_programadas (nombre, cron, zona_horaria, accion, parametros, activo, creado_por)
VALUES
    ('Heartbeat (cada 30 min, horario diurno)', '*/30 8-22 * * *', 'America/Bogota',
     'heartbeat', '{"respetar_horario": true}'::jsonb, false, 'seed')
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 007: heartbeat seedeado (activo=false). Activar desde /admin/automatizaciones.';
END $$;
