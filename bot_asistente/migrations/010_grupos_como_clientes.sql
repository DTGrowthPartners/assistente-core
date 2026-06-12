-- ============================================================================
-- Migración 010 — Grupos de WhatsApp como "clientes" virtuales
-- ============================================================================
-- Para que los grupos aparezcan como chats en /admin/chats con todo el
-- historial agrupado, los persistimos con clientes.numero_whatsapp = chat_id
-- (formato '1203...@g.us', ~25 chars) y etiqueta = 'grupo'.
--
-- Cambios:
--  1) Ampliar `clientes.numero_whatsapp` de 20 → 50 chars.
--  2) Añadir 'grupo' al CHECK de `clientes.etiqueta`.
--  3) Lo mismo para `conversaciones.whapi_message_id` no aplica (otro campo).
-- ============================================================================

ALTER TABLE clientes ALTER COLUMN numero_whatsapp TYPE VARCHAR(50);

ALTER TABLE clientes DROP CONSTRAINT IF EXISTS clientes_etiqueta_check;
ALTER TABLE clientes ADD CONSTRAINT clientes_etiqueta_check
    CHECK (etiqueta IN ('cliente','prospecto','equipo','personal','grupo'));

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 010 lista. Grupos pueden persistirse como clientes con etiqueta=grupo.';
END $$;
