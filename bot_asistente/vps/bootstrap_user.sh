#!/bin/bash
# bootstrap_user.sh — Setup que corre como usuario `asistente` (sin sudo).
# Pre-requisito: bootstrap_root.sh ya ejecutado.
#
# Hace:
#   - venv Python con dependencias del bot
#   - .env real con DATABASE_URL usando el password guardado
#   - Aplica schema.sql a la DB
#   - Carga seeds (tarifas, catálogo Shopify, catálogo HTML)
#   - Copia data del bot openclaw anterior si existe
#   - Configura servicio systemd-user

set -euo pipefail

if [ "$(whoami)" != "asistente" ]; then
  echo "❌ Este script debe correrse como usuario 'asistente'"
  echo "   sudo su - asistente"
  exit 1
fi

ASISTENTE_HOME="/home/asistente"
APP_DIR="$ASISTENTE_HOME/app"
DATA_DIR="$ASISTENTE_HOME/data"
LOG_DIR="$ASISTENTE_HOME/logs"
VENV_DIR="$ASISTENTE_HOME/.venv"

cd "$ASISTENTE_HOME"

# ────────────────────────────────────────────────────────────────────────────
# 1. Sanity check
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [1/7] Sanity check ════════"

if [ ! -f "$ASISTENTE_HOME/.pg_password" ]; then
  echo "❌ No existe ~/.pg_password — ¿corriste bootstrap_root.sh?"
  exit 2
fi

PG_PASS=$(cat "$ASISTENTE_HOME/.pg_password")
echo "✅ Password Postgres recuperado"

# Detectar Python disponible
PY=""
for v in python3.13 python3.12 python3.11; do
  command -v "$v" &>/dev/null && PY="$v" && break
done
if [ -z "$PY" ]; then
  echo "❌ Python 3.11+ no encontrado"
  exit 3
fi
echo "✅ Python: $PY"

# Verificar que la DB responde
if ! PGPASSWORD="$PG_PASS" psql -h localhost -U asistente -d asistente -c "SELECT 1" &>/dev/null; then
  echo "❌ No puedo conectar a Postgres con las credenciales generadas"
  exit 4
fi
echo "✅ Postgres accesible"

# ────────────────────────────────────────────────────────────────────────────
# 2. venv + dependencias
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [2/7] venv ════════"

if [ ! -d "$VENV_DIR" ]; then
  "$PY" -m venv "$VENV_DIR"
  echo "✅ venv creado en $VENV_DIR"
else
  echo "✅ venv ya existía"
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"
pip install --upgrade pip -q

# Si pyproject existe en app/, usarlo. Si no, instalar manualmente.
if [ -f "$APP_DIR/pyproject.toml" ]; then
  pip install -e "$APP_DIR" -q
else
  pip install -q \
    "fastapi>=0.115" "uvicorn[standard]>=0.34" \
    "sqlalchemy[asyncio]>=2.0" "asyncpg>=0.30" "alembic>=1.13" \
    "psycopg2-binary>=2.9" \
    "anthropic>=0.40" \
    "httpx>=0.27" "pydantic-settings>=2.5" \
    "beautifulsoup4>=4.12" "lxml>=5.3" \
    "structlog>=24.4" "python-dotenv>=1.0"
fi
echo "✅ Dependencias instaladas"

# ────────────────────────────────────────────────────────────────────────────
# 3. .env  (si no existe, lo creamos con el password de Postgres)
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [3/7] .env ════════"

ENV_FILE="$APP_DIR/.env"
if [ -f "$ENV_FILE" ]; then
  echo "✅ .env ya existe ($ENV_FILE) — actualizando solo DATABASE_URL"
  sed -i \
    -e "s|^DATABASE_URL=.*|DATABASE_URL=postgresql+asyncpg://asistente:${PG_PASS}@localhost:5432/asistente|" \
    -e "s|^DATABASE_URL_SYNC=.*|DATABASE_URL_SYNC=postgresql://asistente:${PG_PASS}@localhost:5432/asistente|" \
    "$ENV_FILE"
else
  echo "⚠️  Falta $ENV_FILE — copia tu .env local del repo a esta ruta."
  echo "   Cuando lo hagas, las líneas DATABASE_URL* deben ser:"
  echo ""
  echo "   DATABASE_URL=postgresql+asyncpg://asistente:${PG_PASS}@localhost:5432/asistente"
  echo "   DATABASE_URL_SYNC=postgresql://asistente:${PG_PASS}@localhost:5432/asistente"
  echo ""
fi
chmod 600 "$ENV_FILE" 2>/dev/null || true

# ────────────────────────────────────────────────────────────────────────────
# 4. schema.sql
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [4/7] Schema Postgres ════════"

if [ -f "$APP_DIR/schema.sql" ]; then
  PGPASSWORD="$PG_PASS" psql -h localhost -U asistente -d asistente -f "$APP_DIR/schema.sql"
  echo "✅ Schema aplicado"
else
  echo "⚠️  $APP_DIR/schema.sql no existe — sube los archivos del repo primero"
fi

# ────────────────────────────────────────────────────────────────────────────
# 5. Copiar data del bot openclaw anterior (si está accesible)
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [5/7] Migrar data de openclaw ════════"

OPENCLAW_BASE="/home/innovacionpedidos/.openclaw/workspace"
if [ -d "$OPENCLAW_BASE" ] && [ -r "$OPENCLAW_BASE" ]; then
  for sub in catalogo pdfs mediosdepago ubicaciones voice_notes_module; do
    if [ -d "$OPENCLAW_BASE/$sub" ] && [ -r "$OPENCLAW_BASE/$sub" ]; then
      echo "  Copiando $sub …"
      cp -rn "$OPENCLAW_BASE/$sub/." "$DATA_DIR/$sub/" 2>/dev/null || \
        echo "    ⚠️  permisos insuficientes — pídele al admin que haga ACL"
    fi
  done
  echo "✅ Data migrada (la que se pudo)"
else
  echo "⚠️  $OPENCLAW_BASE no accesible. Si el bot anterior corre en otro usuario,"
  echo "    pídele al admin que haga: setfacl -R -m u:asistente:rX $OPENCLAW_BASE"
fi

# ────────────────────────────────────────────────────────────────────────────
# 6. Migraciones María (schema base + tablas DTGP + seed cron)
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [6/7] Migraciones ════════"

# Ajusta usuario/DB si tu deploy usa otros nombres (ver .env).
DB_USER="${DB_USER:-maria_user}"
DB_NAME="${DB_NAME:-maria_db}"

run_sql () {  # $1 = archivo .sql
  if [ -f "$1" ]; then
    echo "  → aplicando $(basename "$1")"
    PGPASSWORD="$PG_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -f "$1" \
      || echo "  ⚠️  falló $(basename "$1")"
  fi
}

run_sql "$APP_DIR/schema.sql"
run_sql "$APP_DIR/migrations/002_equipo_a_db.sql"
run_sql "$APP_DIR/migrations/005_estado_tareas.sql"
run_sql "$APP_DIR/migrations/003_maria_tables.sql"
run_sql "$APP_DIR/migrations/004_maria_tareas.sql"

# Verificación rápida
echo ""
echo "  Estado actual de la DB (María):"
PGPASSWORD="$PG_PASS" psql -h localhost -U "$DB_USER" -d "$DB_NAME" -c "
  SELECT 'contactos_whitelist' AS tabla, COUNT(*) AS filas FROM contactos_whitelist
  UNION ALL SELECT 'prospectos', COUNT(*) FROM prospectos
  UNION ALL SELECT 'citas', COUNT(*) FROM citas
  UNION ALL SELECT 'tareas_programadas', COUNT(*) FROM tareas_programadas
  UNION ALL SELECT 'clientes', COUNT(*) FROM clientes;
"

# ────────────────────────────────────────────────────────────────────────────
# 7. Servicio systemd-user (lo dejamos preparado, no se inicia todavía)
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [7/7] Servicio systemd-user ════════"

SYSTEMD_USER_DIR="$ASISTENTE_HOME/.config/systemd/user"
mkdir -p "$SYSTEMD_USER_DIR"

cat > "$SYSTEMD_USER_DIR/asistente.service" <<EOF
[Unit]
Description=Bot Asistente — Innovacion Fashion Outlet
After=network.target

[Service]
Type=exec
WorkingDirectory=$APP_DIR
EnvironmentFile=$APP_DIR/.env
ExecStart=$VENV_DIR/bin/uvicorn app.main:app --host 127.0.0.1 --port 8003
Restart=on-failure
RestartSec=5
StandardOutput=append:$LOG_DIR/asistente.log
StandardError=append:$LOG_DIR/asistente.err

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
echo "✅ Service file en $SYSTEMD_USER_DIR/asistente.service"
echo "   Para activarlo después:"
echo "     systemctl --user enable asistente.service"
echo "     systemctl --user start asistente.service"

# ────────────────────────────────────────────────────────────────────────────
# Resumen
# ────────────────────────────────────────────────────────────────────────────
cat <<EOF

════════════════════════════════════════════════════════════════
✅ Bootstrap user COMPLETO

Verificaciones:
  source $VENV_DIR/bin/activate
  python -c "from anthropic import Anthropic; print('anthropic OK')"
  python -c "import fastapi; print(fastapi.__version__)"

Próximo paso:
  - Si el código del bot ya está en $APP_DIR, levanta:
      systemctl --user start asistente.service
      tail -f $LOG_DIR/asistente.log
  - Si todavía no hay código (solo schema + scripts), eso lo subimos en
    el siguiente paso desde el repo local con scp/git.

Webhook URL para configurar en panel whapi:
  https://asistente.dtgrowthpartners.com/webhook
════════════════════════════════════════════════════════════════
EOF
