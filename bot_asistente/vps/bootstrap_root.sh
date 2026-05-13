#!/bin/bash
# bootstrap_root.sh — Setup que requiere sudo en el VPS.
# Correr UNA vez como root o con sudo:
#   sudo bash bootstrap_root.sh
#
# Crea:
#   - Paquetes del SO (python3.11+, postgres si falta, etc.)
#   - DB `asistente` y rol `asistente` en Postgres
#   - Carpetas /home/asistente/{app,data,logs}
#
# NO toca nginx (subdominio ya configurado por el admin del VPS).

set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Este script requiere sudo/root."
  exit 1
fi

ASISTENTE_USER="asistente"
ASISTENTE_DB="asistente"
ASISTENTE_HOME="/home/$ASISTENTE_USER"

# ────────────────────────────────────────────────────────────────────────────
# 1. Paquetes del SO
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [1/4] Paquetes del SO ════════"
apt-get update -qq

# Detectar versión de Ubuntu/Debian para escoger versión de Python
PY_PAQUETE="python3.11"
if apt-cache show python3.12 &>/dev/null; then
  PY_PAQUETE="python3.12"
fi
if apt-cache show python3.13 &>/dev/null; then
  PY_PAQUETE="python3.13"
fi

apt-get install -y \
  "$PY_PAQUETE" "${PY_PAQUETE}-venv" "${PY_PAQUETE}-dev" \
  build-essential libpq-dev \
  postgresql-client \
  git curl jq openssl ffmpeg \
  >/dev/null

echo "✅ Paquetes instalados (Python: $PY_PAQUETE)"

# ────────────────────────────────────────────────────────────────────────────
# 2. Postgres — DB y rol asistente
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [2/4] Postgres ════════"

if ! command -v psql &>/dev/null; then
  echo "❌ psql no disponible. ¿Postgres no está instalado en este VPS?"
  echo "   Si el Postgres está en otro servidor, edita DATABASE_URL en .env"
  exit 2
fi

# Generar password fuerte para el usuario asistente de Postgres
ASISTENTE_DB_PASS=$(openssl rand -hex 24)

# Crear rol si no existe
sudo -u postgres psql -tAc "SELECT 1 FROM pg_roles WHERE rolname='$ASISTENTE_USER'" | grep -q 1 || \
  sudo -u postgres psql -c "CREATE ROLE $ASISTENTE_USER WITH LOGIN PASSWORD '$ASISTENTE_DB_PASS';"

# Actualizar password (idempotente)
sudo -u postgres psql -c "ALTER ROLE $ASISTENTE_USER WITH PASSWORD '$ASISTENTE_DB_PASS';"

# Crear DB si no existe
sudo -u postgres psql -tAc "SELECT 1 FROM pg_database WHERE datname='$ASISTENTE_DB'" | grep -q 1 || \
  sudo -u postgres createdb -O "$ASISTENTE_USER" "$ASISTENTE_DB"

# Habilitar extensiones que necesita el schema
sudo -u postgres psql -d "$ASISTENTE_DB" -c "CREATE EXTENSION IF NOT EXISTS pg_trgm;"
sudo -u postgres psql -d "$ASISTENTE_DB" -c "CREATE EXTENSION IF NOT EXISTS \"uuid-ossp\";"

echo "✅ DB $ASISTENTE_DB y rol $ASISTENTE_USER listos"

# Guardar el password en archivo readable solo por el usuario asistente
echo "$ASISTENTE_DB_PASS" > "$ASISTENTE_HOME/.pg_password"
chown "$ASISTENTE_USER:$ASISTENTE_USER" "$ASISTENTE_HOME/.pg_password"
chmod 600 "$ASISTENTE_HOME/.pg_password"
echo "🔑 Password guardado en $ASISTENTE_HOME/.pg_password (chmod 600)"

# ────────────────────────────────────────────────────────────────────────────
# 3. Estructura de directorios
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [3/4] Directorios ════════"

for d in app data data/catalogo data/pdfs data/mediosdepago data/ubicaciones data/prompts data/voice_notes_module logs; do
  mkdir -p "$ASISTENTE_HOME/$d"
done
chown -R "$ASISTENTE_USER:$ASISTENTE_USER" "$ASISTENTE_HOME"
echo "✅ Estructura creada en $ASISTENTE_HOME"

# ────────────────────────────────────────────────────────────────────────────
# 4. Verificar linger (debería estar ya según notas_claudevps.md)
# ────────────────────────────────────────────────────────────────────────────
echo "════════ [4/4] Linger systemd-user ════════"

if loginctl show-user "$ASISTENTE_USER" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
  echo "✅ Linger ya activo para $ASISTENTE_USER"
else
  loginctl enable-linger "$ASISTENTE_USER"
  echo "✅ Linger activado para $ASISTENTE_USER"
fi

# ────────────────────────────────────────────────────────────────────────────
# Resumen
# ────────────────────────────────────────────────────────────────────────────
cat <<EOF

════════════════════════════════════════════════════════════════
✅ Bootstrap root COMPLETO

Conexión Postgres para el bot (también guardado en ~asistente/.pg_password):

  DATABASE_URL=postgresql+asyncpg://asistente:${ASISTENTE_DB_PASS}@localhost:5432/asistente
  DATABASE_URL_SYNC=postgresql://asistente:${ASISTENTE_DB_PASS}@localhost:5432/asistente

Siguiente paso (como usuario asistente):
  su - asistente
  bash bootstrap_user.sh
════════════════════════════════════════════════════════════════
EOF
