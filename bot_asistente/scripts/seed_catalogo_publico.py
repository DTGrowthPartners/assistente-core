"""
seed_catalogo_publico.py — Sincroniza catálogo desde el endpoint público products.json de Shopify.

Endpoint: https://innovacionfashion.co/products.json?limit=250&page=N

Por qué este script reemplaza al seed_catalogo.py (API intermedia):
    - El endpoint products.json es público (sin API key) y devuelve el catálogo
      oficial directamente de Shopify, sin depender de la API intermedia
      ni del SHOPIFY_ACCESS_TOKEN (que actualmente está caducado).
    - Datos limpios con variant_id, imágenes CDN, tallas, precios.
    - Útil para crear draft orders después (cuando el token se renueve, podemos
      usar la API intermedia solo para POST /draft-orders y leer productos de aquí).

Uso:
    python scripts/seed_catalogo_publico.py [--dry-run] [--save-raw archivo.json]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx  # type: ignore
import psycopg2  # type: ignore
from psycopg2.extras import execute_values, Json  # type: ignore


URL_BASE = "https://innovacionfashion.co/products.json"
USER_AGENT = "AsistenteBot/1.0"
DEFAULT_DSN = "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"


# ─────────────────────────────────────────────────────────────────────────────
# Modelo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Producto:
    ref: str
    shopify_product_id: int
    shopify_handle: str
    nombre: str
    descripcion: str | None
    categoria: str | None
    precio_detal: Decimal | None
    precio_mayor: Decimal | None
    tallas: list[str] = field(default_factory=list)
    colores: list[str] = field(default_factory=list)
    variants: list[dict[str, Any]] = field(default_factory=list)
    imagen_url: str | None = None
    imagen_url_extras: list[str] = field(default_factory=list)
    activo: bool = True
    fuente_url: str = ""


# ─────────────────────────────────────────────────────────────────────────────
# Categoría
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIA_RULES = [
    # Orden importa: las reglas más específicas primero.
    # camisetas ANTES de camisas (para evitar que "camisa" matchee "camiseta").
    ("camisetas", r"\bcamiseta|\bcrop top|\bt-shirt"),
    ("camisas", r"\bcamisa(?!ta)"),   # "camisa" pero NO "camiseta"
    ("blusas", r"\bblusa"),
    ("body", r"\bbody\b"),
    ("sueteres", r"\bsu[eé]ter|\bsweater"),
    ("shorts", r"\bshort"),
    ("bermudas", r"\bbermuda|\bbiker|\bciclista"),
    ("jeans", r"\bjean\b|\bskinny|\bbota recta|\bwide leg|\bsemiflare|\bculotte"),
    ("pantalones", r"\bpantal[oó]n|\bchambray|\bcargo|\bdrill"),
    ("faldas", r"\bfalda"),
    ("bragas", r"\bbraga|\boverol|\bjardiner"),
    ("vestidos", r"\bvestido"),
    ("sets", r"\bset\b|\bconjunto"),
    ("tops", r"\btop\b"),
    ("chalecos", r"\bchaleco"),
]


def detectar_categoria(p: dict) -> str | None:
    """
    Detecta categoría con cascada en orden de confianza:
    1. title (más confiable, las palabras describen lo que es)
    2. product_type (de Shopify Admin, suele estar bien)
    3. tags (ruidoso — ej. tag "amour" matchea con cosas raras)
    """
    nombre = p.get("title", "").lower()
    for cat, patron in CATEGORIA_RULES:
        if re.search(patron, nombre):
            return cat

    tipo = (p.get("product_type") or "").lower()
    if tipo:
        for cat, patron in CATEGORIA_RULES:
            if re.search(patron, tipo):
                return cat

    # Tags como último recurso (ruidoso)
    tags = " ".join((t or "").lower() for t in (p.get("tags") or []))
    if tags:
        for cat, patron in CATEGORIA_RULES:
            if re.search(patron, tags):
                return cat
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Extracción de referencia
# ─────────────────────────────────────────────────────────────────────────────

RE_REF = re.compile(
    r"\b("
    r"INN\d{3,6}"
    r"|J\d+-?\d*"
    r"|SD\d+"
    r"|N-?\d+"
    r")\b",
    re.IGNORECASE,
)


def extraer_ref(p: dict) -> str | None:
    """Extrae la REF del título. Ej: 'Jean Culotte ... -INN20076' → 'INN20076'."""
    title = p.get("title", "")
    m = RE_REF.search(title)
    if m:
        return m.group(1).upper()
    # Fallback: handle
    handle = p.get("handle", "")
    m = RE_REF.search(handle)
    if m:
        return m.group(1).upper()
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador
# ─────────────────────────────────────────────────────────────────────────────

def precio_to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


def shopify_to_producto(p: dict) -> Producto | None:
    """De la respuesta de products.json a Producto."""
    nombre = p.get("title")
    if not nombre:
        return None

    ref = extraer_ref(p)
    if not ref:
        # Fallback: usar handle como ref
        ref = (p.get("handle") or str(p.get("id") or "")).upper()
        if not ref:
            return None

    variants_raw = p.get("variants") or []

    # Precios desde variantes
    precios = [precio_to_decimal(v.get("price")) for v in variants_raw]
    precios = [x for x in precios if x is not None]
    precio_detal = min(precios) if precios else None

    precio_mayor = None
    for v in variants_raw:
        cap = precio_to_decimal(v.get("compare_at_price"))
        if cap is not None:
            if precio_mayor is None or cap < precio_mayor:
                precio_mayor = cap

    # Tallas / colores desde options
    tallas: list[str] = []
    colores: list[str] = []
    for opt in p.get("options") or []:
        name = (opt.get("name") or "").strip().lower()
        valores = opt.get("values") or []
        if "talla" in name or "size" in name:
            tallas = [str(v).strip() for v in valores]
        elif "color" in name:
            colores = [str(v).strip() for v in valores]

    # Variants compactos
    variants_compact = [
        {
            "variant_id": v.get("id"),
            "talla": v.get("option1"),
            "color": v.get("option2"),
            "precio": str(precio_to_decimal(v.get("price")) or ""),
            "disponible": v.get("available", True),
            "sku": v.get("sku"),
        }
        for v in variants_raw
    ]

    # Imágenes
    imagen_url = None
    extras: list[str] = []
    images = p.get("images") or []
    if images:
        urls = []
        for img in images:
            if isinstance(img, dict):
                urls.append(img.get("src"))
            elif isinstance(img, str):
                urls.append(img)
        urls = [u for u in urls if u]
        if urls:
            imagen_url = urls[0]
            extras = urls[1:6]  # max 5 extras

    return Producto(
        ref=ref,
        shopify_product_id=p.get("id"),
        shopify_handle=p.get("handle"),
        nombre=nombre,
        descripcion=p.get("body_html"),
        categoria=detectar_categoria(p),
        precio_detal=precio_detal,
        precio_mayor=precio_mayor,
        tallas=tallas,
        colores=colores,
        variants=variants_compact,
        imagen_url=imagen_url,
        imagen_url_extras=extras,
        activo=True,
        fuente_url=f"https://innovacionfashion.co/products/{p.get('handle')}",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fetch paginado
# ─────────────────────────────────────────────────────────────────────────────

def fetch_todos(limit_por_pagina: int = 250, max_paginas: int = 20) -> list[dict]:
    """Trae todos los productos paginando hasta agotar."""
    todos = []
    with httpx.Client(timeout=30, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        for page in range(1, max_paginas + 1):
            params = {"limit": limit_por_pagina, "page": page}
            r = c.get(URL_BASE, params=params)
            r.raise_for_status()
            data = r.json()
            productos = data.get("products", [])
            if not productos:
                break
            todos.extend(productos)
            print(f"   página {page}: +{len(productos)} (total {len(todos)})")
            if len(productos) < limit_por_pagina:
                break
            time.sleep(0.5)  # cortesía con el servidor
    return todos


# ─────────────────────────────────────────────────────────────────────────────
# Upsert
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO productos_cache (
    ref, origen, fuente_url, shopify_product_id, shopify_handle,
    nombre, descripcion, categoria,
    precio_detal, precio_mayor, tallas, colores, variants,
    imagen_url, imagen_url_extras, asumir_disponible, activo
) VALUES %s
ON CONFLICT (ref) DO UPDATE SET
    origen = EXCLUDED.origen,
    fuente_url = EXCLUDED.fuente_url,
    shopify_product_id = EXCLUDED.shopify_product_id,
    shopify_handle = EXCLUDED.shopify_handle,
    nombre = EXCLUDED.nombre,
    descripcion = EXCLUDED.descripcion,
    categoria = COALESCE(EXCLUDED.categoria, productos_cache.categoria),
    precio_detal = EXCLUDED.precio_detal,
    precio_mayor = EXCLUDED.precio_mayor,
    tallas = EXCLUDED.tallas,
    colores = EXCLUDED.colores,
    variants = EXCLUDED.variants,
    imagen_url = COALESCE(EXCLUDED.imagen_url, productos_cache.imagen_url),
    imagen_url_extras = EXCLUDED.imagen_url_extras,
    asumir_disponible = TRUE,
    activo = EXCLUDED.activo,
    sincronizado_en = NOW();
"""


def upsert(productos: list[Producto], dsn: str) -> int:
    if not productos:
        return 0
    rows = [
        (
            p.ref, "shopify", p.fuente_url,
            p.shopify_product_id, p.shopify_handle,
            p.nombre, p.descripcion, p.categoria,
            p.precio_detal, p.precio_mayor,
            Json(p.tallas), Json(p.colores), Json(p.variants),
            p.imagen_url, Json(p.imagen_url_extras),
            True,  # asumir_disponible
            p.activo,
        )
        for p in productos
    ]
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=100)
            conn.commit()
    return len(rows)


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza catálogo público Shopify → Postgres")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL_SYNC", DEFAULT_DSN))
    parser.add_argument("--save-raw", help="Guarda la respuesta cruda como JSON")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--max-paginas", type=int, default=20)
    args = parser.parse_args()

    print(f"🌐 Fetching {URL_BASE} (paginado) …")
    try:
        raw = fetch_todos(max_paginas=args.max_paginas)
    except Exception as e:
        print(f"❌ {e}", file=sys.stderr)
        return 2
    print(f"✅ {len(raw)} productos totales recibidos")

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, default=str)
        print(f"   raw guardado en {args.save_raw}")

    productos: list[Producto] = []
    sin_ref = 0
    for p in raw:
        prod = shopify_to_producto(p)
        if prod:
            productos.append(prod)
        else:
            sin_ref += 1

    print(f"   {len(productos)} adaptados, {sin_ref} sin ref válida")

    por_cat: dict[str, int] = {}
    sin_precio = 0
    sin_imagen = 0
    for p in productos:
        k = p.categoria or "(sin categoría)"
        por_cat[k] = por_cat.get(k, 0) + 1
        if not p.precio_detal:
            sin_precio += 1
        if not p.imagen_url:
            sin_imagen += 1
    print(f"   por categoría: {por_cat}")
    if sin_precio:
        print(f"   ⚠️  {sin_precio} sin precio")
    if sin_imagen:
        print(f"   ⚠️  {sin_imagen} sin imagen")

    if args.dry_run:
        print("\n🧪 Dry-run. Primeros 5:")
        for p in productos[:5]:
            print(f"   {p.ref}: {p.nombre[:50]} | {p.precio_detal} | tallas={p.tallas}")
        return 0

    print(f"📤 Upsert a Postgres …")
    n = upsert(productos, args.dsn)
    print(f"✅ {n} productos cargados (origen='shopify')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
