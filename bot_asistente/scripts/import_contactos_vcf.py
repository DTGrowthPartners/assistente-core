"""
import_contactos_vcf.py — Importa contactos .vcf a la tabla `numeros_internos`.

Parsea archivos vCard (.vcf) exportados desde WhatsApp Business y los carga
como números internos del equipo (el bot los ignora cuando escriben).

Uso:
    python scripts/import_contactos_vcf.py --carpeta "BOT VENTAS/contactos"
    python scripts/import_contactos_vcf.py --carpeta /ruta --dry-run
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

import psycopg2  # type: ignore
from psycopg2.extras import execute_values  # type: ignore

RE_TEL = re.compile(r"TEL[^:]*:(.+)")
RE_FN = re.compile(r"^FN:(.+)$")
RE_WAID = re.compile(r"waid=(\d+)")
RE_BIZ_NAME = re.compile(r"^X-WA-BIZ-NAME:(.+)$")


def parsear_vcf(contenido: str) -> dict | None:
    """Extrae nombre y número E.164 de una vCard."""
    nombre = None
    biz_name = None
    numero_e164 = None

    for linea in contenido.splitlines():
        linea = linea.strip()
        if m := RE_FN.match(linea):
            nombre = m.group(1).strip()
        elif m := RE_BIZ_NAME.match(linea):
            biz_name = m.group(1).strip()
        elif linea.upper().startswith("TEL"):
            # Preferir waid (siempre normalizado)
            waid_m = RE_WAID.search(linea)
            if waid_m:
                num = waid_m.group(1)
                numero_e164 = f"+{num}"
            else:
                # Caer al valor después de ":"
                if ":" in linea:
                    valor = linea.split(":", 1)[1].strip()
                    digits = re.sub(r"[^\d+]", "", valor)
                    if digits.startswith("+"):
                        numero_e164 = digits
                    elif digits.startswith("57") and len(digits) >= 10:
                        numero_e164 = f"+{digits}"
                    elif len(digits) == 10:
                        numero_e164 = f"+57{digits}"

    if not numero_e164:
        return None

    return {
        "numero_whatsapp": numero_e164,
        "nombre": nombre or biz_name or "(sin nombre)",
        "razon": "Contacto interno del equipo (importado desde vCard)",
    }


def iterar_vcfs(carpeta: Path):
    for path in sorted(carpeta.iterdir()):
        if path.suffix.lower() != ".vcf":
            continue
        try:
            yield path, path.read_text(encoding="utf-8", errors="replace")
        except Exception as e:
            print(f"  ⚠️  no se pudo leer {path.name}: {e}", file=sys.stderr)


UPSERT_SQL = """
INSERT INTO numeros_internos (numero_whatsapp, nombre, razon, activo)
VALUES %s
ON CONFLICT (numero_whatsapp) DO UPDATE
SET nombre = COALESCE(numeros_internos.nombre, EXCLUDED.nombre),
    razon = COALESCE(numeros_internos.razon, EXCLUDED.razon),
    activo = TRUE;
"""


def cargar(contactos: list[dict], dsn: str) -> int:
    if not contactos:
        return 0
    rows = [(c["numero_whatsapp"], c["nombre"], c["razon"], True) for c in contactos]
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=100)
            conn.commit()
    return len(rows)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--carpeta", required=True, help="Carpeta con archivos .vcf")
    parser.add_argument(
        "--dsn",
        default=os.environ.get(
            "DATABASE_URL_SYNC",
            "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db",
        ),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    carpeta = Path(args.carpeta)
    if not carpeta.is_dir():
        print(f"❌ {carpeta} no existe", file=sys.stderr)
        return 1

    print(f"📂 Parseando {carpeta} …")
    contactos: list[dict] = []
    seen_numeros: set[str] = set()
    for path, contenido in iterar_vcfs(carpeta):
        info = parsear_vcf(contenido)
        if not info:
            print(f"  ⚠️  {path.name}: no se encontró número")
            continue
        if info["numero_whatsapp"] in seen_numeros:
            print(f"  ↪︎ {path.name}: duplicado ({info['numero_whatsapp']}), saltando")
            continue
        seen_numeros.add(info["numero_whatsapp"])
        contactos.append(info)
        print(f"  ✅ {info['numero_whatsapp']:18s} {info['nombre']}")

    print(f"\n📊 {len(contactos)} contactos únicos encontrados")

    if args.dry_run:
        print("🧪 Dry-run, no se escribe en DB")
        return 0

    n = cargar(contactos, args.dsn)
    print(f"📤 {n} contactos cargados/actualizados en numeros_internos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
