-- ============================================================================
-- Migración 004 — Seed de tareas programadas (rutina diaria de María)
-- ============================================================================
-- Reproduce el "heartbeat"/cron de openclaw como tareas_programadas (cron en BD,
-- editables desde /admin/automatizaciones).
--
-- ⚠️ Se insertan con activo=FALSE a propósito: NO disparan hasta que estén las
-- credenciales (DTOS_API_KEY, METASUITE) y se revisen los destinos. Activar
-- cada una desde el admin cuando todo esté verificado.
-- ============================================================================

INSERT INTO tareas_programadas (nombre, cron, zona_horaria, accion, parametros, activo, creado_por)
VALUES
    ('Reporte CEO (8 AM L-V)', '0 8 * * 1-5', 'America/Bogota', 'reporte_ceo',
     '{"destino_id": "+573007189383"}'::jsonb, false, 'seed'),

    ('Motivacional equipo (9 AM L-V)', '0 9 * * 1-5', 'America/Bogota', 'motivacional_equipo',
     '{"destinos": ["+573007189383", "+573116123189", "+573005033093"]}'::jsonb, false, 'seed'),

    ('Seguimiento tareas (9:10 L-V)', '10 9 * * 1-5', 'America/Bogota', 'seguimiento_tareas',
     '{"usuarios": ["Edgardo", "Jhonathan"], "telefonos": {"Edgardo": "+573116123189", "Jhonathan": "+573005033093"}}'::jsonb, false, 'seed'),

    ('Reflexión semanal (Vie 4 PM)', '0 16 * * 5', 'America/Bogota', 'reflexion_semanal',
     '{"destino_id": "+573007189383"}'::jsonb, false, 'seed'),

    ('Alerta financiera (cada 4h)', '0 */4 * * *', 'America/Bogota', 'alerta_financiera',
     '{"destino_id": "+573007189383", "piso": 5000000, "techo": 10000000}'::jsonb, false, 'seed'),

    ('Clientes en riesgo (Lun/Jue 11 AM)', '0 11 * * 1,4', 'America/Bogota', 'clientes_en_riesgo',
     '{"destino_id": "+573007189383"}'::jsonb, false, 'seed'),

    -- Reportes Meta Ads a clientes (7 AM L-V). Ajustar destino/account por cliente.
    ('Reporte Meta — Equilibrio (7 AM)', '0 7 * * 1-5', 'America/Bogota', 'reporte_meta_cliente',
     '{"account_id": "act_1604918750004319", "destino_id": "+573007399331", "nombre_cliente": "Equilibrio Clinic", "date_preset": "yesterday"}'::jsonb, false, 'seed'),

    ('Reporte Meta — Tennis (7 AM)', '0 7 * * 1-5', 'America/Bogota', 'reporte_meta_cliente',
     '{"account_id": "act_660842485358224", "destino_id": "+573243019151", "nombre_cliente": "Tennis Cartagena", "date_preset": "yesterday"}'::jsonb, false, 'seed'),

    ('Reporte Meta — ACBFIT (7 AM)', '0 7 * * 1-5', 'America/Bogota', 'reporte_meta_cliente',
     '{"account_id": "act_1214099615878120", "destino_id": "+573008125144", "nombre_cliente": "ACBFIT", "date_preset": "yesterday"}'::jsonb, false, 'seed'),

    ('Recordatorio pendientes (cada 6h)', '0 */6 * * *', 'America/Bogota', 'recordatorio_pendientes',
     '{"destino_id": "+573007189383", "horas_min": 4, "max_alertas": 5}'::jsonb, false, 'seed')
ON CONFLICT DO NOTHING;

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 004 (María): tareas programadas seed (activo=false — activar desde /admin tras credenciales)';
END $$;
