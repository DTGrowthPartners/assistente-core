"""
seed_tarifas.py — Carga las tarifas de domicilio desde tarifas-domicilios-cartagena.md a Postgres.

Uso:
    python scripts/seed_tarifas.py [--archivo /ruta/a/tarifas-domicilios-cartagena.md]

Idempotente: usa ON CONFLICT (barrio_normalizado) DO UPDATE.

Salida esperada: ~232 barrios cargados (231 según conteo manual del MD, puede variar).
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import unicodedata
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Iterator

import psycopg2  # type: ignore
from psycopg2.extras import execute_values  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Parser del Markdown
# ─────────────────────────────────────────────────────────────────────────────

# Captura tablas tipo:  | Barrio | $6.000 |  |
ROW_RE = re.compile(
    r"^\|\s*\*{0,2}([^|*]+?)\*{0,2}\s*\|\s*\*{0,2}([^|*]+?)\*{0,2}\s*(?:\|.*)?\|?\s*$"
)
ZONE_RE = re.compile(r"^##\s+ZONA\s+\d+\s+—\s+(.+?)(?:\s+\(.*\))?$", re.IGNORECASE)
HEADER_ROW_RE = re.compile(r"^\|\s*(barrio|sector)\s*\|", re.IGNORECASE)
SEPARATOR_ROW_RE = re.compile(r"^\|[\s\-:|]+\|$")
PRICE_RE = re.compile(r"\$\s?([\d.,]+)")


@dataclass
class Tarifa:
    barrio: str
    barrio_normalizado: str
    zona: str
    precio: Decimal | None
    tipo: str  # 'domicilio_local' | 'transportadora' | 'no_cubre' | 'evaluar'
    notas: str | None = None


def normalizar(texto: str) -> str:
    """Lowercase + sin tildes + sin caracteres especiales para búsqueda fuzzy."""
    nfkd = unicodedata.normalize("NFKD", texto)
    sin_tildes = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", sin_tildes.lower().strip())


def clasificar_celda_precio(celda_raw: str) -> tuple[Decimal | None, str, str | None]:
    """
    Toma el valor crudo de la columna precio y devuelve (precio, tipo, notas).
    """
    celda = celda_raw.strip().strip("*").strip()
    celda_lower = celda.lower().strip("_ ")

    # Caso 1: precio numérico — domicilio local
    match = PRICE_RE.search(celda)
    if match:
        # Quita puntos como separador de miles, coma decimal a punto
        num_str = match.group(1).replace(".", "").replace(",", ".")
        try:
            return Decimal(num_str), "domicilio_local", None
        except Exception:
            pass

    # Caso 2: "envío" → va por transportadora
    if "envío" in celda_lower or "envio" in celda_lower or "transportadora" in celda_lower:
        return None, "transportadora", celda

    # Caso 3: islas / evaluar / escalar
    if "evaluar" in celda_lower or "escalar" in celda_lower or "lancha" in celda_lower:
        return None, "evaluar", celda

    # Caso 4: no se cubre
    if "no se cubre" in celda_lower or "no cubre" in celda_lower:
        return None, "no_cubre", celda

    # Default: tratar como evaluar
    return None, "evaluar", celda or None


def parsear_archivo(ruta: Path) -> Iterator[Tarifa]:
    """Itera sobre las filas válidas del MD y produce Tarifa por cada barrio."""
    zona_actual = "Desconocida"
    contenido = ruta.read_text(encoding="utf-8")

    for linea in contenido.splitlines():
        linea = linea.rstrip()

        # Detectar cambio de zona
        m_zona = ZONE_RE.match(linea)
        if m_zona:
            zona_actual = m_zona.group(1).strip()
            continue

        # Saltar encabezados y separadores de tabla
        if HEADER_ROW_RE.match(linea) or SEPARATOR_ROW_RE.match(linea):
            continue

        # Fila de datos
        m = ROW_RE.match(linea)
        if not m:
            continue

        barrio_raw, precio_raw = m.group(1), m.group(2)
        barrio = barrio_raw.strip()

        # Algunos casos especiales
        if not barrio or barrio.lower() in ("barrio", "sector", "zona"):
            continue

        precio, tipo, notas = clasificar_celda_precio(precio_raw)
        norm = normalizar(barrio)
        if not norm:
            continue

        yield Tarifa(
            barrio=barrio,
            barrio_normalizado=norm,
            zona=zona_actual,
            precio=precio,
            tipo=tipo,
            notas=notas,
        )


# ─────────────────────────────────────────────────────────────────────────────
# Carga a Postgres
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO tarifas_domicilio (barrio, barrio_normalizado, zona, precio, tipo, notas)
VALUES %s
ON CONFLICT (barrio_normalizado) DO UPDATE
SET barrio = EXCLUDED.barrio,
    zona = EXCLUDED.zona,
    precio = EXCLUDED.precio,
    tipo = EXCLUDED.tipo,
    notas = EXCLUDED.notas;
"""


def cargar(tarifas: list[Tarifa], dsn: str) -> int:
    """Inserta/actualiza las tarifas en Postgres. Retorna número de filas afectadas."""
    values = [
        (t.barrio, t.barrio_normalizado, t.zona, t.precio, t.tipo, t.notas)
        for t in tarifas
    ]

    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, values, page_size=200)
            conn.commit()
            return len(values)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Carga tarifas de domicilio a Postgres")
    parser.add_argument(
        "--archivo",
        default="BOT VENTAS/tarifas-domicilios-cartagena.md",
        help="Ruta al archivo Markdown con las tarifas",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL_SYNC", "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"),
        help="DSN de Postgres (sync, sin asyncpg)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Solo imprime, no escribe en DB")
    args = parser.parse_args()

    ruta = Path(args.archivo)
    if not ruta.exists():
        print(f"❌ No existe: {ruta}", file=sys.stderr)
        return 1

    print(f"📄 Parseando {ruta} …")
    tarifas = list(parsear_archivo(ruta))
    print(f"   → {len(tarifas)} barrios encontrados")

    # Resumen por tipo
    por_tipo: dict[str, int] = {}
    for t in tarifas:
        por_tipo[t.tipo] = por_tipo.get(t.tipo, 0) + 1
    print(f"   → Por tipo: {por_tipo}")

    if args.dry_run:
        print("\n🧪 Dry-run, no se escribe nada. Primeros 5:")
        for t in tarifas[:5]:
            print(f"   {t}")
        return 0

    print(f"📤 Cargando a Postgres ({args.dsn.split('@')[-1]}) …")
    n = cargar(tarifas, args.dsn)
    print(f"✅ Cargados {n} barrios")
    return 0


if __name__ == "__main__":
    sys.exit(main())
