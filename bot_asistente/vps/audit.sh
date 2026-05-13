#!/bin/bash
# audit.sh — Inventario del VPS antes de instalar nada.
# Correr como usuario `asistente`:
#   bash audit.sh
#
# Devuelve un reporte legible + un JSON al final para automatización.

set -u
ok()    { printf "  ✅ %s\n" "$*"; }
fail()  { printf "  ❌ %s\n" "$*"; }
warn()  { printf "  ⚠️  %s\n" "$*"; }
section(){ printf "\n\033[1;36m== %s ==\033[0m\n" "$*"; }

REPORT_JSON=$(mktemp)
echo "{" > "$REPORT_JSON"

# ─── Identidad ──────────────────────────────────────────────────────────────
section "Identidad"
USER_ACTUAL=$(whoami)
HOSTNAME=$(hostname)
OS_NAME=$(. /etc/os-release && echo "$PRETTY_NAME")
KERNEL=$(uname -r)
echo "  Usuario:  $USER_ACTUAL"
echo "  Host:     $HOSTNAME"
echo "  OS:       $OS_NAME"
echo "  Kernel:   $KERNEL"
echo "  UID/GID:  $(id -u)/$(id -g)"

cat >> "$REPORT_JSON" <<EOF
  "user": "$USER_ACTUAL",
  "hostname": "$HOSTNAME",
  "os": "$OS_NAME",
EOF

# ─── Python ─────────────────────────────────────────────────────────────────
section "Python"
PY_VERSIONS=()
for py in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$py" &>/dev/null; then
    V=$("$py" --version 2>&1)
    PY_VERSIONS+=("$py:$V")
    ok "$py → $V"
  fi
done
if [ ${#PY_VERSIONS[@]} -eq 0 ]; then
  fail "Ningún Python encontrado"
fi

PY_BEST=""
for needed in python3.13 python3.12 python3.11; do
  if command -v "$needed" &>/dev/null; then
    PY_BEST="$needed"
    break
  fi
done
if [ -n "$PY_BEST" ]; then
  ok "Python a usar para el venv: $PY_BEST"
else
  warn "Falta Python 3.11+. Necesario para FastAPI moderno."
fi

# venv module
if command -v "$PY_BEST" &>/dev/null && "$PY_BEST" -c "import venv" 2>/dev/null; then
  ok "Módulo venv disponible"
else
  fail "Falta python3-venv (apt install ${PY_BEST}-venv)"
fi

echo "  \"python\": \"$PY_BEST\"," >> "$REPORT_JSON"

# ─── Postgres ───────────────────────────────────────────────────────────────
section "PostgreSQL"
if command -v psql &>/dev/null; then
  PG_VERSION=$(psql --version)
  ok "$PG_VERSION"
  # Probar conexión (sin password aún)
  if pg_isready &>/dev/null; then
    ok "Servidor Postgres respondiendo"
  else
    warn "Postgres instalado pero pg_isready falla — ¿servicio detenido?"
  fi
  echo "  \"postgres_client\": \"$PG_VERSION\"," >> "$REPORT_JSON"
else
  fail "psql no encontrado (apt install postgresql-client)"
  echo "  \"postgres_client\": null," >> "$REPORT_JSON"
fi

# Verificar si hay servidor postgres corriendo
if ss -tln 2>/dev/null | grep -q ":5432"; then
  ok "Puerto 5432 escuchando (servidor Postgres local)"
  echo "  \"postgres_local\": true," >> "$REPORT_JSON"
else
  warn "Nadie escucha en 5432 — ¿Postgres no está en este VPS o usa puerto distinto?"
  echo "  \"postgres_local\": false," >> "$REPORT_JSON"
fi

# ─── Nginx ──────────────────────────────────────────────────────────────────
section "Nginx"
if command -v nginx &>/dev/null; then
  ok "$(nginx -v 2>&1)"
  # Verificar que nuestro subdominio esté configurado
  CONFIG_FILE=""
  for f in /etc/nginx/sites-enabled/* /etc/nginx/conf.d/*.conf; do
    [ -f "$f" ] && grep -l "asistente.dtgrowthpartners.com" "$f" 2>/dev/null && CONFIG_FILE="$f"
  done
  if [ -n "$CONFIG_FILE" ]; then
    ok "Subdominio asistente.dtgrowthpartners.com configurado en $CONFIG_FILE"
  else
    warn "No encontré config nginx para asistente.dtgrowthpartners.com"
  fi
else
  fail "nginx no encontrado"
fi

# ─── Conectividad y subdominio ──────────────────────────────────────────────
section "DNS y HTTPS"
SUBDOMINIO_IP=$(getent hosts asistente.dtgrowthpartners.com | awk '{print $1}')
if [ -n "$SUBDOMINIO_IP" ]; then
  ok "asistente.dtgrowthpartners.com → $SUBDOMINIO_IP"
else
  warn "DNS no resuelve aún"
fi

HTTPS_STATUS=$(curl -s -o /dev/null -w "%{http_code}" https://asistente.dtgrowthpartners.com/ --max-time 10 2>/dev/null || echo "timeout")
echo "  HTTPS asistente.dtgrowthpartners.com → $HTTPS_STATUS (espera 502 hasta levantar el bot)"

# ─── Espacio en disco ───────────────────────────────────────────────────────
section "Disco"
df -h / /home 2>/dev/null | awk 'NR==1 || /\//{print "  " $0}'
HOME_AVAIL=$(df -BG /home 2>/dev/null | awk 'NR==2 {gsub("G","",$4); print $4}')
if [ -n "$HOME_AVAIL" ] && [ "$HOME_AVAIL" -lt 2 ]; then
  warn "Menos de 2GB libres en /home"
else
  ok "${HOME_AVAIL}G libres en /home"
fi

# ─── Memoria ────────────────────────────────────────────────────────────────
section "Memoria"
free -h | awk 'NR==1 || /Mem|Swap/{print "  " $0}'

# ─── Linger / systemd-user ──────────────────────────────────────────────────
section "Systemd-user"
if loginctl show-user "$USER_ACTUAL" -p Linger 2>/dev/null | grep -q "Linger=yes"; then
  ok "Linger activado para $USER_ACTUAL — systemd-user persistirá sin sesión"
else
  warn "Linger NO activado. Sin sudo no lo puedes activar tú. Pídele al admin: loginctl enable-linger $USER_ACTUAL"
fi

systemctl --user status 2>/dev/null | head -1 && ok "systemctl --user funciona" || warn "systemctl --user no disponible"

# ─── Otras herramientas ─────────────────────────────────────────────────────
section "Herramientas adicionales"
for tool in git curl jq ffmpeg openssl; do
  if command -v "$tool" &>/dev/null; then
    ok "$tool"
  else
    warn "$tool no instalado"
  fi
done

# ─── Estructura de directorios ──────────────────────────────────────────────
section "Estructura en \$HOME"
ls -la "$HOME" 2>/dev/null | head -20

# ─── Catálogo openclaw (si existe en este VPS) ──────────────────────────────
section "Datos del bot anterior (openclaw)"
for ruta in \
  "/home/innovacionpedidos/.openclaw/workspace/catalogo" \
  "/home/innovacionpedidos/.openclaw/workspace/pdfs" \
  "/home/innovacionpedidos/.openclaw/workspace/mediosdepago" \
  "/home/innovacionpedidos/.openclaw/workspace/ubicaciones" \
  "/home/innovacionpedidos/.openclaw/workspace/voice_notes_module"; do
  if [ -d "$ruta" ]; then
    SIZE=$(du -sh "$ruta" 2>/dev/null | awk '{print $1}')
    COUNT=$(ls "$ruta" 2>/dev/null | wc -l)
    ok "$ruta ($COUNT archivos, $SIZE)"
  elif [ -r "$ruta" ]; then
    ok "$ruta (existe, no listable)"
  else
    warn "$ruta no accesible (puede existir pero sin permisos)"
  fi
done

# ─── JSON final ─────────────────────────────────────────────────────────────
echo "  \"final\": \"ok\"" >> "$REPORT_JSON"
echo "}" >> "$REPORT_JSON"

section "Reporte JSON guardado"
echo "  $REPORT_JSON"
cat "$REPORT_JSON"

echo ""
echo "════════════════════════════════════════════════════════════════"
echo "  Si todo se ve bien:"
echo "    1. Pásale este reporte a tu colega Stiven/Claude"
echo "    2. Si faltan paquetes del SO → bootstrap_root.sh (necesita sudo)"
echo "    3. Si todo está → bootstrap_user.sh"
echo "════════════════════════════════════════════════════════════════"
