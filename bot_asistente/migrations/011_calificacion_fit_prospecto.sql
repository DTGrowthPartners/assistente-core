-- ============================================================================
-- Migración 011 — Calificación de fit del prospecto
-- ============================================================================
-- Para evitar que María agende reuniones con prospectos sin presupuesto ni
-- estructura, agregamos 3 campos a `prospectos`:
--   - tipo_organizacion: empresa / emprendimiento_estructurado / persona_natural / desconocido
--   - es_empresa:        booleano simple para chequeos rápidos (TRUE si la
--                        respuesta cualifica para agendar)
--   - presupuesto_mensual_cop: presupuesto mensual aproximado (COP) que el
--                        prospecto puede destinar a marketing total (ads + fee).
--                        Mínimo para agendar: 2_000_000.
-- ============================================================================

ALTER TABLE prospectos
  ADD COLUMN IF NOT EXISTS tipo_organizacion VARCHAR(40),
  ADD COLUMN IF NOT EXISTS es_empresa BOOLEAN,
  ADD COLUMN IF NOT EXISTS presupuesto_mensual_cop BIGINT;

COMMENT ON COLUMN prospectos.tipo_organizacion IS
  'empresa | emprendimiento_estructurado | persona_natural | desconocido';
COMMENT ON COLUMN prospectos.es_empresa IS
  'TRUE si tiene estructura de negocio (empresa registrada o emprendimiento operativo).';
COMMENT ON COLUMN prospectos.presupuesto_mensual_cop IS
  'Presupuesto mensual total en COP (ads + fee DTGP). Umbral mínimo para agendar: 2_000_000.';

DO $$
BEGIN
    RAISE NOTICE 'Migración 011 lista. Campos nuevos en prospectos: tipo_organizacion, es_empresa, presupuesto_mensual_cop.';
END $$;
