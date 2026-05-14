"""
seed_catalogo_whatsapp.py — Carga productos del catálogo de WhatsApp (HTML).

Fuente: https://innovacionfashion.co/pages/catalogo-de-whatsapp

Por qué este script y no `/products.json`:
- /products.json devuelve solo los 74 productos PUBLICADOS oficialmente.
- El HTML del catálogo de WhatsApp incluye productos adicionales (drafts,
  productos con stock interno) que el equipo vende manualmente.
- Cada `<div class="cwa-card" data-ref="..." data-name="..." ...>` trae
  todos los datos en atributos `data-*`. Parser trivial.

Regla del negocio:
- Productos cargados con `origen='html_catalogo'` se asumen SIEMPRE
  disponibles (no hay inventario en tiempo real). El bot dice "sí tenemos".
- No tienen `variant_id` de Shopify, así que NO se pueden crear draft orders.
  Los pedidos van por `tomar_pedido_manual` + escalación al equipo.

Idempotente: upsert por ref. Si un producto ya está como `origen='shopify'`
en la DB, se PRESERVA ese origen (no se sobreescribe con html_catalogo).

Uso:
    python scripts/seed_catalogo_whatsapp.py [--dry-run] [--from-file ruta.html]
"""

from __future__ import annotations

import argparse
import gzip
import os
import re
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg2  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from psycopg2.extras import execute_values, Json  # type: ignore


URL_CATALOGO = "https://innovacionfashion.co/pages/catalogo-de-whatsapp"
DEFAULT_DSN = "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ────────────────────────────────────────────────────────────────────────────
# Modelo
# ────────────────────────────────────────────────────────────────────────────


@dataclass
class ProductoCard:
    ref: str
    nombre: str
    categoria: str | None = None
    tag: str | None = None
    precio_detal: Decimal | None = None
    precio_mayor: Decimal | None = None
    tallas: list[str] = field(default_factory=list)
    colores: list[str] = field(default_factory=list)
    imagen_url: str | None = None
    imagen_extras: list[str] = field(default_factory=list)
    fuente_url: str = URL_CATALOGO


# ────────────────────────────────────────────────────────────────────────────
# Fetch del HTML (con --compressed que es el truco)
# ────────────────────────────────────────────────────────────────────────────


def fetch_html(url: str = URL_CATALOGO) -> str:
    """
    Descarga el HTML del catálogo. Usa curl --compressed porque urllib en
    Python no maneja brotli y la página viene con Content-Encoding: br.
    """
    try:
        result = subprocess.run(
            [
                "curl", "-sL", "--compressed", "--max-time", "30",
                "-A", USER_AGENT,
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9",
                "-H", "Accept-Language: es-CO,es;q=0.9",
                url,
            ],
            capture_output=True, check=True, timeout=60,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except subprocess.CalledProcessError as e:
        print(f"❌ curl falló: {e}", file=sys.stderr)
        return ""
    except FileNotFoundError:
        # Fallback: urllib (puede no manejar brotli)
        print("⚠️  curl no disponible, usando urllib", file=sys.stderr)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9",
            "Accept-Language": "es-CO,es;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        })
        resp = urllib.request.urlopen(req, timeout=30)
        body = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip":
            body = gzip.decompress(body)
        return body.decode("utf-8", errors="replace")


# ────────────────────────────────────────────────────────────────────────────
# Parser
# ────────────────────────────────────────────────────────────────────────────


CATEGORIA_RULES = [
    ("camisetas", r"\bcamiseta|\bcrop top|\bt-shirt"),
    ("camisas", r"\bcamisa(?!ta)"),
    ("blusas", r"\bblusa"),
    ("body", r"\bbody\b"),
    ("sueteres", r"\bsu[eé]ter|\bsweater"),
    ("shorts", r"\bshort"),
    ("bermudas", r"\bbermuda|\bbiker|\bciclista"),
    ("jeans", r"\bjean|\bskinny|\bbota recta|\bwide leg|\bsemiflare|\bculotte"),
    ("pantalones", r"\bpantal[oó]n|\bchambray|\bcargo|\bdrill"),
    ("faldas", r"\bfalda"),
    ("bragas", r"\bbraga|\boverol|\bjardiner"),
    ("vestidos", r"\bvestido"),
    ("sets", r"\bset\b|\bconjunto"),
    ("tops", r"\btop\b"),
    ("chalecos", r"\bchaleco"),
]


def detectar_categoria(nombre: str, tag: str | None = None) -> str | None:
    texto = (nombre + " " + (tag or "")).lower()
    for cat, patron in CATEGORIA_RULES:
        if re.search(patron, texto):
            return cat
    # Mapeo por tags conocidos del HTML
    mapa_tags = {
        "skinnys": "jeans",
        "shorts": "shorts",
        "bermudas": "bermudas",
        "faldas": "faldas",
        "bragas": "bragas",
    }
    if tag and tag.lower() in mapa_tags:
        return mapa_tags[tag.lower()]
    return None


def parsear_precio(raw: str | None) -> Decimal | None:
    """'$70.000' → Decimal('70000')."""
    if not raw:
        return None
    m = re.search(r"\$\s*([\d.,]+)", raw)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", "")
    try:
        return Decimal(num)
    except Exception:
        return None


def parsear_tallas(raw: str | None) -> list[str]:
    """'6-8-10-12-14-16-18' → ['6','8','10','12','14','16','18'] o 'S/M-L/XL'."""
    if not raw:
        return []
    raw = raw.strip()
    if not raw:
        return []
    # Si vienen separadas por guión o coma o slash
    partes = re.split(r"[-,/|]", raw)
    out: list[str] = []
    for p in partes:
        p = p.strip()
        if p and p not in out:
            out.append(p)
    return out


def parsear_colores(raw: str | None) -> list[str]:
    if not raw:
        return []
    partes = re.split(r"[,/]", raw)
    return [p.strip() for p in partes if p.strip()]


def normalizar_url_imagen(url: str | None) -> str | None:
    if not url:
        return None
    url = url.strip()
    if url.startswith("//"):
        return "https:" + url
    return url


def parsear_cards(html: str) -> list[ProductoCard]:
    """Extrae todos los `<div class="cwa-card" data-*>`."""
    soup = BeautifulSoup(html, "lxml")
    cards: list[ProductoCard] = []
    for div in soup.select("div.cwa-card"):
        ref = (div.get("data-ref") or "").strip()
        nombre = (div.get("data-name") or "").strip()
        if not ref or not nombre:
            continue

        tag = (div.get("data-tag") or "").strip() or None
        prod = ProductoCard(
            ref=ref,
            nombre=nombre,
            tag=tag,
            categoria=detectar_categoria(nombre, tag),
            precio_detal=parsear_precio(div.get("data-precio")),
            precio_mayor=parsear_precio(div.get("data-mayor")),
            tallas=parsear_tallas(div.get("data-talla")),
            colores=parsear_colores(div.get("data-color")),
            imagen_url=normalizar_url_imagen(div.get("data-img")),
        )

        imgs_raw = div.get("data-imgs") or ""
        extras: list[str] = []
        for u in re.split(r"[,\s]+", imgs_raw):
            u = normalizar_url_imagen(u.strip())
            if u and u != prod.imagen_url:
                extras.append(u)
        prod.imagen_extras = extras[:5]

        cards.append(prod)

    # Dedupe por ref (puede haber duplicados visuales en el HTML)
    vistos: dict[str, ProductoCard] = {}
    for c in cards:
        if c.ref not in vistos:
            vistos[c.ref] = c
    return list(vistos.values())


# ────────────────────────────────────────────────────────────────────────────
# Upsert a Postgres
# ────────────────────────────────────────────────────────────────────────────


UPSERT_SQL = """
INSERT INTO productos_cache (
    ref, origen, fuente_url, nombre, categoria,
    precio_detal, precio_mayor, tallas, colores, variants,
    imagen_url, imagen_url_extras, asumir_disponible, activo
) VALUES %s
ON CONFLICT (ref) DO UPDATE SET
    -- PRESERVAR origen='shopify' si ya existe (más completo, tiene variant_id)
    origen = CASE
        WHEN productos_cache.origen = 'shopify' THEN productos_cache.origen
        ELSE EXCLUDED.origen
    END,
    fuente_url = COALESCE(productos_cache.fuente_url, EXCLUDED.fuente_url),
    nombre = COALESCE(productos_cache.nombre, EXCLUDED.nombre),
    categoria = COALESCE(productos_cache.categoria, EXCLUDED.categoria),
    -- Solo actualizar precio si el actual está vacío
    precio_detal = COALESCE(productos_cache.precio_detal, EXCLUDED.precio_detal),
    precio_mayor = COALESCE(productos_cache.precio_mayor, EXCLUDED.precio_mayor),
    -- Tallas/colores: usar las del HTML si productos_cache las tiene vacías
    tallas = CASE
        WHEN jsonb_array_length(productos_cache.tallas) > 0 THEN productos_cache.tallas
        ELSE EXCLUDED.tallas
    END,
    colores = CASE
        WHEN jsonb_array_length(productos_cache.colores) > 0 THEN productos_cache.colores
        ELSE EXCLUDED.colores
    END,
    imagen_url = COALESCE(productos_cache.imagen_url, EXCLUDED.imagen_url),
    asumir_disponible = TRUE,
    sincronizado_en = NOW();
"""


def cargar(productos: list[ProductoCard], dsn: str) -> int:
    if not productos:
        return 0
    rows = [
        (
            p.ref,
            "html_catalogo",
            p.fuente_url,
            p.nombre,
            p.categoria,
            p.precio_detal,
            p.precio_mayor,
            Json(p.tallas),
            Json(p.colores),
            Json([]),
            p.imagen_url,
            Json(p.imagen_extras),
            True,
            True,
        )
        for p in productos
    ]
    with psycopg2.connect(dsn) as conn:
        with conn.cursor() as cur:
            execute_values(cur, UPSERT_SQL, rows, page_size=100)
            conn.commit()
    return len(rows)


# ────────────────────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────────────────────


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--from-file", help="HTML local (skip fetch)")
    parser.add_argument("--save-html", help="Guardar HTML descargado a este path")
    parser.add_argument("--dsn", default=os.environ.get("DATABASE_URL_SYNC", DEFAULT_DSN))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.from_file:
        html = Path(args.from_file).read_text(encoding="utf-8")
        print(f"📂 {args.from_file}: {len(html):,} bytes")
    else:
        print(f"🌐 GET {URL_CATALOGO}")
        html = fetch_html()
        if not html:
            print("❌ HTML vacío", file=sys.stderr)
            return 1
        print(f"   {len(html):,} bytes")
        if args.save_html:
            Path(args.save_html).write_text(html, encoding="utf-8")
            print(f"   guardado en {args.save_html}")

    productos = parsear_cards(html)
    print(f"🎯 {len(productos)} productos parseados de {html.count('cwa-card')} cwa-card en HTML")

    if not productos:
        print("⚠️  Sin productos. Revisar el HTML.", file=sys.stderr)
        return 2

    # Stats
    por_cat: dict[str, int] = {}
    con_precio = 0
    con_imagen = 0
    for p in productos:
        k = p.categoria or "(sin)"
        por_cat[k] = por_cat.get(k, 0) + 1
        if p.precio_detal:
            con_precio += 1
        if p.imagen_url:
            con_imagen += 1
    print(f"   por categoría: {por_cat}")
    print(f"   con precio: {con_precio}/{len(productos)}")
    print(f"   con imagen: {con_imagen}/{len(productos)}")

    if args.dry_run:
        print("\n🧪 Dry-run. Primeros 5:")
        for p in productos[:5]:
            print(f"   {p.ref} | {p.nombre} | {p.categoria} | "
                  f"${p.precio_detal}/${p.precio_mayor} | tallas={p.tallas}")
        return 0

    n = cargar(productos, args.dsn)
    print(f"✅ {n} productos upserteados (origen='html_catalogo' o preservado si ya era 'shopify')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
