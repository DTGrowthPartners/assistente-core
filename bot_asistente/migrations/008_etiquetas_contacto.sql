-- ============================================================================
-- Migración 008 — Etiquetas de contacto (cliente / prospecto / equipo / personal)
-- ============================================================================
-- Sirve sobre todo para el canal de Dairo (donde el número es real y el bot
-- SOLO debe responder a cliente/prospecto/equipo, jamás a contactos personales).
-- También útil en cualquier canal para silenciar números explícitamente.
-- ============================================================================

ALTER TABLE clientes ADD COLUMN IF NOT EXISTS etiqueta VARCHAR(20)
    CHECK (etiqueta IN ('cliente','prospecto','equipo','personal'));
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS etiqueta_actualizada_en TIMESTAMPTZ;
ALTER TABLE clientes ADD COLUMN IF NOT EXISTS etiqueta_actualizada_por VARCHAR(60);

CREATE INDEX IF NOT EXISTS idx_clientes_etiqueta ON clientes(etiqueta) WHERE etiqueta IS NOT NULL;

-- Backfill desde la whitelist: si un número ya está como equipo o cliente en
-- contactos_whitelist y tiene fila en clientes, propaga la etiqueta.
UPDATE clientes c
   SET etiqueta = w.rol,
       etiqueta_actualizada_en = now(),
       etiqueta_actualizada_por = 'backfill_008'
  FROM contactos_whitelist w
 WHERE c.numero_whatsapp = w.numero_whatsapp
   AND w.activo = true
   AND c.etiqueta IS NULL
   AND w.rol IN ('equipo','cliente');

DO $$
DECLARE n_eq INT; n_cl INT;
BEGIN
    SELECT COUNT(*) INTO n_eq FROM clientes WHERE etiqueta='equipo';
    SELECT COUNT(*) INTO n_cl FROM clientes WHERE etiqueta='cliente';
    RAISE NOTICE '✅ Migración 008: etiqueta agregada. Backfill: % equipo, % cliente.', n_eq, n_cl;
END $$;
