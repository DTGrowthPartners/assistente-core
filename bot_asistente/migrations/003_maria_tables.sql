-- ============================================================================
-- Migración 003 — Tablas del vertical María / DTGP
-- ============================================================================
-- Aditiva: crea las tablas nuevas sin tocar las del bot retail (que se
-- eliminan en una migración posterior cuando su código deje de usarse).
--   psql -U <user> -d <db> -f migrations/003_maria_tables.sql
-- ============================================================================

-- ── WHITELIST (equipo DTGP + clientes activos) ─────────────────────────────
CREATE TABLE IF NOT EXISTS contactos_whitelist (
    id              SERIAL PRIMARY KEY,
    numero_whatsapp VARCHAR(20) UNIQUE NOT NULL,
    rol             VARCHAR(20) NOT NULL CHECK (rol IN ('equipo','cliente')),
    nombre          VARCHAR(150),
    empresa         VARCHAR(150),
    email           VARCHAR(120),
    nit             VARCHAR(30),
    dtos_client_id  VARCHAR(60),
    meta_account_id VARCHAR(60),
    permisos        JSONB NOT NULL DEFAULT '{}'::jsonb,
    activo          BOOLEAN NOT NULL DEFAULT TRUE,
    notas           TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_whitelist_numero ON contactos_whitelist(numero_whatsapp);
CREATE INDEX IF NOT EXISTS idx_whitelist_rol ON contactos_whitelist(rol) WHERE activo;

COMMENT ON TABLE contactos_whitelist IS 'Whitelist maestra de María (ex WHITELIST.md). rol equipo|cliente → flujo operativo; ausente → flujo prospecto.';

-- ── PROSPECTOS (extensión 1:1 de clientes) ──────────────────────────────────
CREATE TABLE IF NOT EXISTS prospectos (
    cliente_id   INT PRIMARY KEY REFERENCES clientes(id) ON DELETE CASCADE,
    negocio      VARCHAR(255),
    sector       VARCHAR(100),
    ciudad       VARCHAR(100),
    necesidad    TEXT,
    ya_pauta     BOOLEAN,
    tiene_web    BOOLEAN,
    estado       VARCHAR(30) NOT NULL DEFAULT 'nuevo'
                 CHECK (estado IN ('nuevo','calificando','agendado','no_fit','cliente','descartado')),
    score        INT,
    dtos_deal_id VARCHAR(60),
    notas        TEXT,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_prospectos_estado ON prospectos(estado);

-- ── CITAS (agendadas vía Cal.com) ──────────────────────────────────────────
CREATE TABLE IF NOT EXISTS citas (
    id                SERIAL PRIMARY KEY,
    cliente_id        INT NOT NULL REFERENCES clientes(id) ON DELETE CASCADE,
    nombre            VARCHAR(150),
    email             VARCHAR(120),
    negocio           VARCHAR(255),
    fecha_inicio      TIMESTAMPTZ NOT NULL,
    fecha_fin         TIMESTAMPTZ,
    calcom_booking_id VARCHAR(60),
    calcom_uid        VARCHAR(80),
    estado            VARCHAR(20) NOT NULL DEFAULT 'agendada'
                      CHECK (estado IN ('agendada','reprogramada','cancelada','completada','no_asistio')),
    notas             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_citas_cliente ON citas(cliente_id);
CREATE INDEX IF NOT EXISTS idx_citas_fecha ON citas(fecha_inicio);
CREATE INDEX IF NOT EXISTS idx_citas_estado ON citas(estado);

-- ── TRIGGERS updated_at ─────────────────────────────────────────────────────
-- (reusa trigger_set_updated_at() creado en schema.sql)
DROP TRIGGER IF EXISTS trg_whitelist_updated ON contactos_whitelist;
CREATE TRIGGER trg_whitelist_updated BEFORE UPDATE ON contactos_whitelist
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
DROP TRIGGER IF EXISTS trg_prospectos_updated ON prospectos;
CREATE TRIGGER trg_prospectos_updated BEFORE UPDATE ON prospectos
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();
DROP TRIGGER IF EXISTS trg_citas_updated ON citas;
CREATE TRIGGER trg_citas_updated BEFORE UPDATE ON citas
    FOR EACH ROW EXECUTE FUNCTION trigger_set_updated_at();

-- ============================================================================
-- SEED — Equipo DTGP + clientes activos (de WHITELIST.md / MEMORY.md)
-- ============================================================================

-- Equipo interno (rol=equipo). Estos números también deben existir en
-- equipo_miembros para que el routing es_miembro_equipo() los reconozca.
INSERT INTO contactos_whitelist (numero_whatsapp, rol, nombre, empresa, notas)
VALUES
    ('+573007189383', 'equipo', 'Dairo Orozco', 'DTGP', 'CEO — prioridad máxima'),
    ('+573026444564', 'equipo', 'Stiven Antequera', 'DTGP', 'Dueño técnico'),
    ('+573116123189', 'equipo', 'Edgardo Saltarín', 'DTGP', NULL),
    ('+573005033093', 'equipo', 'Jhonathan', 'DTGP', 'Equipo (NO confundir con +573007856068 que es otro Jhonatan, amigo)')
ON CONFLICT (numero_whatsapp) DO UPDATE
    SET rol = EXCLUDED.rol, nombre = EXCLUDED.nombre, empresa = EXCLUDED.empresa;

-- Clientes activos con contacto WhatsApp (rol=cliente).
INSERT INTO contactos_whitelist (numero_whatsapp, rol, nombre, empresa, nit, meta_account_id, dtos_client_id)
VALUES
    ('+573008125144', 'cliente', 'Anita Balceiro', 'ACBFIT', '901725973', 'act_1214099615878120', NULL),
    ('+573007399331', 'cliente', 'Jenifer Coneo', 'Equilibrio Clinic', '9012506980', 'act_1604918750004319', NULL),
    ('+573107528529', 'cliente', 'Johana', 'Equilibrio Clinic', '9012506980', 'act_1604918750004319', NULL),
    ('+573243019151', 'cliente', 'Raiza', 'Tennis Cartagena', NULL, 'act_660842485358224', NULL),
    ('+573136303989', 'cliente', 'Willy', 'Importaciones Cartagena', NULL, NULL, 'cmk45eta2000orh96z6ul0agy'),
    ('+573006324023', 'cliente', 'Angelica', 'Importaciones Cartagena', NULL, NULL, 'cmk45eta2000orh96z6ul0agy'),
    ('+573182066879', 'cliente', 'Camilo Villalba', 'AutoExpress / Sanautos', '901794841-2', NULL, 'cmk45esqn000crh96iim01jq9')
ON CONFLICT (numero_whatsapp) DO UPDATE
    SET rol = EXCLUDED.rol, nombre = EXCLUDED.nombre, empresa = EXCLUDED.empresa;

-- Equipo DTGP en equipo_miembros (routing operativo). es_fallback=true → reciben
-- escalaciones que no matcheen un área específica.
INSERT INTO equipo_miembros (nombre, numero_whatsapp, rol, areas, es_fallback, activo)
VALUES
    ('Dairo Orozco', '+573007189383', 'CEO', '["finanzas","clientes","estrategia"]'::jsonb, TRUE, TRUE),
    ('Stiven Antequera', '+573026444564', 'Dueño técnico', '["sistema","desarrollo"]'::jsonb, TRUE, TRUE),
    ('Edgardo Saltarín', '+573116123189', 'Operaciones', '["tareas","clientes"]'::jsonb, FALSE, TRUE),
    ('Jhonathan', '+573005033093', 'Equipo', '["contabilidad"]'::jsonb, FALSE, TRUE)
ON CONFLICT (numero_whatsapp) DO UPDATE
    SET nombre = EXCLUDED.nombre, rol = EXCLUDED.rol, areas = EXCLUDED.areas, activo = TRUE;

-- ── Ampliar tipos válidos de alertas para el dominio María ──────────────────
ALTER TABLE alertas_fabio DROP CONSTRAINT IF EXISTS alertas_fabio_tipo_check;
ALTER TABLE alertas_fabio ADD CONSTRAINT alertas_fabio_tipo_check
    CHECK (tipo IN (
        'comprobante_pago','ref_desconocida','queja','mensaje_dueno',
        'pedido_confirmado','duda_mayorista','error_sistema','otro',
        'pide_humano','prospecto_caliente','fuera_de_alcance','agendamiento'
    ));

DO $$
BEGIN
    RAISE NOTICE '✅ Migración 003 (María): contactos_whitelist, prospectos, citas creadas + seed equipo/clientes';
END $$;
