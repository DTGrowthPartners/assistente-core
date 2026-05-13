"""
seed_catalogo.py — Sincroniza productos desde la API Shopify propia a Postgres.

API: https://innova.dtgrowthpartners.co/api
Auth: header x-api-key

Uso:
    python scripts/seed_catalogo.py [--dry-run]

Idempotente: upsert por ref (Shopify SKU).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx  # type: ignore
import psycopg2  # type: ignore
from psycopg2.extras import execute_values, Json  # type: ignore


# ─────────────────────────────────────────────────────────────────────────────
# Modelo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Producto:
    ref: str
    shopify_product_id: int | None
    shopify_handle: str | None
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
    foto_local: str | None = None
    video_local: str | None = None
    activo: bool = True


# ─────────────────────────────────────────────────────────────────────────────
# Clasificación de categoría
# ─────────────────────────────────────────────────────────────────────────────

CATEGORIA_RULES = [
    ("shorts", r"\bshort"),
    ("bermudas", r"\bbermuda|\bbiker|\bciclista"),
    ("jeans", r"\bjean"),
    ("pantalones", r"\bpantal[oó]n|\bchambray|\bcargo|\bdrill"),
    ("faldas", r"\bfalda"),
    ("bragas", r"\bbraga|\boverol|\bjardiner"),
    ("vestidos", r"\bvestido"),
    ("sets", r"\bset\b|\bconjunto"),
    ("tops", r"\btop\b"),
    ("camisetas", r"\bcamiseta|\bcamisa|\bbody|\bsu[eé]ter"),
    ("blusas", r"\bblusa"),
    ("chalecos", r"\bchaleco"),
]


def detectar_categoria(nombre: str, tags: list[str] | None = None) -> str | None:
    """Detecta categoría desde el nombre del producto o sus tags."""
    texto = nombre.lower()
    if tags:
        texto += " " + " ".join(t.lower() for t in tags)
    for cat, patron in CATEGORIA_RULES:
        if re.search(patron, texto):
            return cat
    return None


def extraer_ref_de_sku(sku: str | None, nombre: str | None) -> str | None:
    """Extrae REF tipo INN1234, J116-6, SD007 del SKU o nombre."""
    candidatos = []
    if sku:
        candidatos.append(sku)
    if nombre:
        candidatos.append(nombre)
    for c in candidatos:
        m = re.search(r"\b(INN\d+|J\d+-\d+|SD\d+|N-\d+|\d{4,})\b", c, re.IGNORECASE)
        if m:
            return m.group(1).upper()
    return None


def precio_to_decimal(v: Any) -> Decimal | None:
    if v is None or v == "":
        return None
    try:
        return Decimal(str(v))
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# Cliente Shopify API
# ─────────────────────────────────────────────────────────────────────────────

def fetch_productos_shopify(base_url: str, api_key: str, limit: int = 250) -> list[dict]:
    """Llama al endpoint /products y devuelve la lista cruda."""
    url = f"{base_url.rstrip('/')}/products"
    headers = {"x-api-key": api_key}
    with httpx.Client(timeout=60) as client:
        r = client.get(url, params={"limit": limit}, headers=headers)
        r.raise_for_status()
        data = r.json()
        # Asumimos respuesta {products: [...]} o lista directa
        if isinstance(data, dict) and "products" in data:
            return data["products"]
        if isinstance(data, list):
            return data
        raise RuntimeError(f"Formato inesperado: {type(data)}")


# ─────────────────────────────────────────────────────────────────────────────
# Adaptador: Shopify product → Producto
# ─────────────────────────────────────────────────────────────────────────────

def shopify_to_producto(p: dict) -> Producto | None:
    """
    Adapta la respuesta de Shopify (objeto producto) al modelo interno.
    Tolerante a esquemas: si tu API devuelve algo distinto, ajustar acá.
    """
    nombre = p.get("title") or p.get("name") or ""
    if not nombre:
        return None

    # SKU desde la primera variante
    variants_raw = p.get("variants") or []
    sku_principal = None
    if variants_raw:
        sku_principal = variants_raw[0].get("sku") or variants_raw[0].get("SKU")

    ref = extraer_ref_de_sku(sku_principal, nombre)
    if not ref:
        # Fallback: usar handle o product_id como ref
        ref = (p.get("handle") or str(p.get("id") or "")).upper()
        if not ref:
            return None

    # Precios — tomamos el menor como detal, opcional mayor de metafields
    precios_variantes = [
        precio_to_decimal(v.get("price")) for v in variants_raw
        if precio_to_decimal(v.get("price")) is not None
    ]
    precio_detal = min(precios_variantes) if precios_variantes else None
    precio_mayor = None
    for v in variants_raw:
        if v.get("compare_at_price"):
            cap = precio_to_decimal(v["compare_at_price"])
            if cap is not None and (precio_mayor is None or cap < precio_mayor):
                precio_mayor = cap

    # Tallas y colores desde opciones
    tallas: list[str] = []
    colores: list[str] = []
    for opt in p.get("options") or []:
        nombre_opt = (opt.get("name") or "").lower()
        valores = opt.get("values") or []
        if "talla" in nombre_opt or "size" in nombre_opt:
            tallas = [str(v) for v in valores]
        elif "color" in nombre_opt:
            colores = [str(v) for v in valores]

    # Variants compactos para draft orders
    variants_compact = [
        {
            "variant_id": v.get("id"),
            "talla": v.get("option1") or v.get("size"),
            "color": v.get("option2") or v.get("color"),
            "precio": str(precio_to_decimal(v.get("price")) or ""),
            "disponible": v.get("available", True),
        }
        for v in variants_raw
    ]

    # Imágenes
    imagenes = p.get("images") or []
    imagen_url = None
    extras: list[str] = []
    if imagenes:
        # Soporta lista de strings o lista de dicts
        urls = [
            (i.get("src") or i.get("url")) if isinstance(i, dict) else i
            for i in imagenes
        ]
        urls = [u for u in urls if u]
        if urls:
            imagen_url = urls[0]
            extras = urls[1:]

    return Producto(
        ref=ref,
        shopify_product_id=p.get("id"),
        shopify_handle=p.get("handle"),
        nombre=nombre,
        descripcion=p.get("body_html") or p.get("description"),
        categoria=detectar_categoria(nombre, p.get("tags")),
        precio_detal=precio_detal,
        precio_mayor=precio_mayor,
        tallas=tallas,
        colores=colores,
        variants=variants_compact,
        imagen_url=imagen_url,
        imagen_url_extras=extras,
        activo=p.get("status", "active") == "active",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Upsert a Postgres
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO productos_cache (
    ref, shopify_product_id, shopify_handle, nombre, descripcion, categoria,
    precio_detal, precio_mayor, tallas, colores, variants,
    imagen_url, imagen_url_extras, foto_local, video_local, activo
) VALUES %s
ON CONFLICT (ref) DO UPDATE SET
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
    activo = EXCLUDED.activo,
    sincronizado_en = NOW();
"""


def upsert_productos(productos: list[Producto], dsn: str) -> int:
    if not productos:
        return 0

    rows = [
        (
            p.ref, p.shopify_product_id, p.shopify_handle, p.nombre, p.descripcion, p.categoria,
            p.precio_detal, p.precio_mayor,
            Json(p.tallas), Json(p.colores), Json(p.variants),
            p.imagen_url, Json(p.imagen_url_extras),
            p.foto_local, p.video_local, p.activo,
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
    parser = argparse.ArgumentParser(description="Sincroniza catálogo Shopify → Postgres")
    parser.add_argument(
        "--shopify-url",
        default=os.environ.get("SHOPIFY_API_BASE_URL", "https://innova.dtgrowthpartners.com/api"),
    )
    parser.add_argument(
        "--shopify-key",
        default=os.environ.get("SHOPIFY_API_KEY", ""),
        help="x-api-key para autenticarse",
    )
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL_SYNC", "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--save-raw",
        default=None,
        help="Guarda la respuesta cruda de la API en este path JSON (para debugging)",
    )
    args = parser.parse_args()

    if not args.shopify_key:
        print("❌ Falta SHOPIFY_API_KEY (o --shopify-key)", file=sys.stderr)
        return 1

    print(f"🌐 Pidiendo productos a {args.shopify_url} …")
    try:
        raw = fetch_productos_shopify(args.shopify_url, args.shopify_key)
    except httpx.HTTPStatusError as e:
        print(f"❌ HTTP {e.response.status_code}: {e.response.text[:500]}", file=sys.stderr)
        return 2
    except Exception as e:
        print(f"❌ Error de red: {e}", file=sys.stderr)
        return 2

    print(f"   → {len(raw)} productos recibidos de Shopify")

    if args.save_raw:
        with open(args.save_raw, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2, default=str)
        print(f"   → respuesta cruda guardada en {args.save_raw}")

    productos: list[Producto] = []
    skipped = 0
    for p in raw:
        prod = shopify_to_producto(p)
        if prod:
            productos.append(prod)
        else:
            skipped += 1

    print(f"   → {len(productos)} adaptados, {skipped} omitidos (sin ref o nombre)")

    # Resumen por categoría
    por_cat: dict[str, int] = {}
    for p in productos:
        k = p.categoria or "(sin categoría)"
        por_cat[k] = por_cat.get(k, 0) + 1
    print(f"   → Por categoría: {por_cat}")

    if args.dry_run:
        print("\n🧪 Dry-run. Primeros 3:")
        for p in productos[:3]:
            print(f"   {p.ref}: {p.nombre} | {p.precio_detal} | tallas={p.tallas}")
        return 0

    print(f"📤 Upserting a Postgres …")
    n = upsert_productos(productos, args.dsn)
    print(f"✅ {n} productos sincronizados")
    return 0


if __name__ == "__main__":
    sys.exit(main())
