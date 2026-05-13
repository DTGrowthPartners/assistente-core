"""
seed_catalogo_html.py — Sincroniza el catálogo público de WhatsApp a Postgres.

Fuente: https://innovacionfashion.co/pages/catalogo-de-whatsapp

¿Por qué este script existe además de seed_catalogo.py?
    Innovación Fashion tiene DOS fuentes de productos:
      1. Shopify Admin API → productos con variant_id (permite draft orders)
      2. Página /pages/catalogo-de-whatsapp → ~70-80 productos que NO siempre
         están en Shopify pero igual se venden.

    Los productos de origen='html_catalogo' NO tienen variant_id, así que para
    estos NO se pueden crear draft orders automáticos. Laura debe procesar el
    pedido manual y escalar a Fabio para confirmar/despachar.

    Regla del negocio: SIEMPRE asumir disponibilidad (no hay inventario en
    tiempo real). Si el cliente pregunta "¿tienen tallas?" → Laura dice que sí
    y si después no hay, una asesora humana lo maneja.

Uso:
    python scripts/seed_catalogo_html.py              # fetch + parse + upsert
    python scripts/seed_catalogo_html.py --dry-run    # solo imprime
    python scripts/seed_catalogo_html.py --save-html /tmp/catalogo.html
    python scripts/seed_catalogo_html.py --from-file /tmp/catalogo.html  # offline

Estrategias de extracción (en cascada):
    A. JSON-LD embedded (`<script type="application/ld+json">`)
    B. Atributos data-* de Shopify (window.ShopifyAnalytics, etc.)
    C. Detección heurística de cards de producto con BeautifulSoup
    D. Regex sobre texto plano (último recurso, busca patrones de REF + precio)

La primera estrategia que encuentre >=5 productos gana.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import dataclass, field, asdict
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterable

import httpx  # type: ignore
import psycopg2  # type: ignore
from bs4 import BeautifulSoup  # type: ignore
from psycopg2.extras import execute_values, Json  # type: ignore


URL_CATALOGO = "https://innovacionfashion.co/pages/catalogo-de-whatsapp"
USER_AGENT = "Mozilla/5.0 (compatible; LauraBot/1.0; +https://innovacionfashion.co)"


# ─────────────────────────────────────────────────────────────────────────────
# Modelo
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class ProductoHTML:
    ref: str
    nombre: str
    categoria: str | None = None
    precio_detal: Decimal | None = None
    precio_mayor: Decimal | None = None
    tallas: list[str] = field(default_factory=list)
    colores: list[str] = field(default_factory=list)
    imagen_url: str | None = None
    imagen_url_extras: list[str] = field(default_factory=list)
    descripcion_raw: str | None = None
    fuente_url: str = URL_CATALOGO


# ─────────────────────────────────────────────────────────────────────────────
# Utilidades de parseo
# ─────────────────────────────────────────────────────────────────────────────

RE_PRECIO = re.compile(r"\$\s*([\d.,]+)")
RE_REF = re.compile(
    r"\b("
    r"INN\d{3,6}"           # INN3756, INN50139
    r"|J\d+-?\d*"           # J116-6, J120-6
    r"|SD\d+"               # SD007
    r"|N-?\d+"              # N-02, N-167
    r"|REF[\s:\-]*\d+"      # REF-0068, REF: 0099, REF 9122
    r"|\b\d{4,6}\b"         # 36676, 9122
    r")\b",
    re.IGNORECASE,
)
RE_TALLA_NUM = re.compile(r"\b\d{1,2}\b")
RE_TALLA_RANGO = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\b")
RE_TALLA_LETRA = re.compile(r"\b(S/M|L/XL|XS|S|M|L|XL|XXL)\b")
COLORES_COMUNES = {
    "negro", "blanco", "rojo", "azul", "azul claro", "azul cielo", "rosado",
    "rosa", "amarillo", "verde", "naranja", "lila", "morado", "vinotinto",
    "beige", "café", "marrón", "gris", "dorado", "plata", "mostaza",
    "turquesa", "fucsia", "coral",
}


def normalizar_ref(raw: str) -> str:
    """Limpia y normaliza una referencia."""
    s = raw.upper().strip()
    s = re.sub(r"REF[\s:\-]*", "", s)
    s = re.sub(r"\s+", "", s)
    return s


def parsear_precio(texto: str) -> Decimal | None:
    """'$56.000' o '56.000 COP' → Decimal('56000')"""
    m = RE_PRECIO.search(texto)
    if not m:
        return None
    num = m.group(1).replace(".", "").replace(",", ".")
    try:
        return Decimal(num)
    except Exception:
        return None


def extraer_tallas(texto: str) -> list[str]:
    """De texto tipo '6-18' o '8,10,12,14,16' o 'S/M, L/XL' a lista."""
    tallas: list[str] = []

    # Rangos numéricos: "6-18" → [6, 8, 10, 12, 14, 16, 18]
    for m in RE_TALLA_RANGO.finditer(texto):
        ini, fin = int(m.group(1)), int(m.group(2))
        if 4 <= ini <= 20 and 4 <= fin <= 20 and ini < fin:
            tallas.extend(str(t) for t in range(ini, fin + 1, 2))

    # Letras: S/M, L/XL
    for m in RE_TALLA_LETRA.finditer(texto):
        t = m.group(1).upper()
        if t not in tallas:
            tallas.append(t)

    # Si no encontramos rangos, buscamos números sueltos válidos
    if not any(c.isdigit() for c in "".join(tallas)):
        for m in RE_TALLA_NUM.finditer(texto):
            n = int(m.group())
            if 4 <= n <= 20 and str(n) not in tallas:
                tallas.append(str(n))

    return tallas


def extraer_colores(texto: str) -> list[str]:
    """Detecta colores comunes en el texto."""
    encontrados: list[str] = []
    texto_lower = texto.lower()
    # Ordenamos por largo descendente para que "azul claro" gane sobre "azul"
    for color in sorted(COLORES_COMUNES, key=lambda c: -len(c)):
        if color in texto_lower:
            # Evita duplicar "azul" si ya está "azul claro"
            if not any(c in encontrados for c in COLORES_COMUNES if color in c and c != color):
                encontrados.append(color.capitalize())
    return encontrados


def detectar_categoria(nombre: str) -> str | None:
    """Misma lógica que seed_catalogo.py."""
    n = nombre.lower()
    rules = [
        ("shorts", r"\bshort"),
        ("bermudas", r"\bbermuda|\bbiker|\bciclista"),
        ("jeans", r"\bjean|\bskinny|\bbota recta|\bwide leg|\bsemiflare"),
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
    for cat, patron in rules:
        if re.search(patron, n):
            return cat
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Estrategias de extracción
# ─────────────────────────────────────────────────────────────────────────────

def estrategia_a_json_ld(soup: BeautifulSoup) -> list[ProductoHTML]:
    """Busca <script type='application/ld+json'> con productos."""
    productos: list[ProductoHTML] = []
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue

        candidatos = data if isinstance(data, list) else [data]
        for d in candidatos:
            if not isinstance(d, dict):
                continue
            tipo = d.get("@type")
            if tipo == "Product":
                productos.append(_json_ld_a_producto(d))
            elif tipo == "ItemList":
                for item in d.get("itemListElement", []):
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        productos.append(_json_ld_a_producto(item))
    return [p for p in productos if p]


def _json_ld_a_producto(d: dict) -> ProductoHTML | None:
    nombre = d.get("name") or ""
    if not nombre:
        return None
    ref = ""
    sku = d.get("sku") or d.get("mpn") or ""
    if sku:
        m = RE_REF.search(sku)
        ref = normalizar_ref(m.group(1)) if m else normalizar_ref(sku)
    if not ref:
        m = RE_REF.search(nombre)
        if m:
            ref = normalizar_ref(m.group(1))
    if not ref:
        return None

    offer = d.get("offers") or {}
    if isinstance(offer, list) and offer:
        offer = offer[0]
    precio_detal = None
    try:
        precio_detal = Decimal(str(offer.get("price"))) if offer.get("price") else None
    except Exception:
        pass

    imagen = d.get("image")
    if isinstance(imagen, list):
        imagen_url = imagen[0] if imagen else None
        extras = imagen[1:] if len(imagen) > 1 else []
    else:
        imagen_url = imagen
        extras = []

    return ProductoHTML(
        ref=ref,
        nombre=nombre,
        categoria=detectar_categoria(nombre),
        precio_detal=precio_detal,
        imagen_url=imagen_url,
        imagen_url_extras=extras,
        descripcion_raw=d.get("description"),
    )


def estrategia_b_shopify_window(soup: BeautifulSoup, html_raw: str) -> list[ProductoHTML]:
    """Busca data inyectada por Shopify (window.ShopifyAnalytics, _ShopifyAnalytics, etc.)."""
    productos: list[ProductoHTML] = []

    # Patrón típico: window.meta = { products: [...] } o ShopifyAnalytics.meta
    for patron in [
        r"window\.ShopifyAnalytics\.meta\s*=\s*({.*?});",
        r"window\.meta\s*=\s*({.*?});",
        r"meta\s*=\s*({\"page\":.*?});",
    ]:
        m = re.search(patron, html_raw, re.DOTALL)
        if not m:
            continue
        try:
            data = json.loads(m.group(1))
            for p in data.get("products", []):
                ref_raw = p.get("variants", [{}])[0].get("sku") or ""
                ref_m = RE_REF.search(ref_raw)
                ref = normalizar_ref(ref_m.group(1)) if ref_m else normalizar_ref(ref_raw)
                if not ref:
                    continue
                productos.append(ProductoHTML(
                    ref=ref,
                    nombre=p.get("name", ""),
                    categoria=detectar_categoria(p.get("name", "")),
                    precio_detal=Decimal(str(p.get("price", 0))) / 100 if p.get("price") else None,
                ))
        except Exception:
            continue
    return productos


def estrategia_c_cards_html(soup: BeautifulSoup) -> list[ProductoHTML]:
    """Busca cards de producto vía heurística de selectores."""
    productos: list[ProductoHTML] = []

    # Posibles contenedores
    candidatos_selectores = [
        "div.producto", "div.producto-card", "article.producto",
        "div.product-card", "div.product", "li.product",
        "div[class*='producto']", "div[class*='product']",
    ]

    cards = []
    for sel in candidatos_selectores:
        cards = soup.select(sel)
        if len(cards) >= 5:
            break

    if not cards:
        return []

    for card in cards:
        texto = card.get_text(" ", strip=True)
        if not texto or len(texto) < 10:
            continue

        m_ref = RE_REF.search(texto)
        if not m_ref:
            continue
        ref = normalizar_ref(m_ref.group(1))

        # Nombre: primer h2/h3/h4 del card
        nombre_el = card.find(["h2", "h3", "h4", "h1"])
        nombre = nombre_el.get_text(strip=True) if nombre_el else texto[:80]

        # Imagen
        img = card.find("img")
        imagen_url = None
        if img:
            imagen_url = img.get("src") or img.get("data-src") or img.get("data-original")
            if imagen_url and imagen_url.startswith("//"):
                imagen_url = "https:" + imagen_url

        # Precios — primer y segundo "$N.NNN" en el texto
        precios = [parsear_precio(m.group()) for m in RE_PRECIO.finditer(texto)]
        precios = [p for p in precios if p is not None]
        precio_detal = precios[0] if precios else None
        precio_mayor = precios[1] if len(precios) >= 2 else None
        # Heurística: si hay dos, el mayor de los dos es el detal
        if precio_detal and precio_mayor and precio_mayor > precio_detal:
            precio_detal, precio_mayor = precio_mayor, precio_detal

        productos.append(ProductoHTML(
            ref=ref,
            nombre=nombre,
            categoria=detectar_categoria(nombre),
            precio_detal=precio_detal,
            precio_mayor=precio_mayor,
            tallas=extraer_tallas(texto),
            colores=extraer_colores(texto),
            imagen_url=imagen_url,
        ))
    return productos


def estrategia_d_texto_plano(soup: BeautifulSoup) -> list[ProductoHTML]:
    """
    Último recurso: extrae texto plano de toda la página y busca bloques
    cohesivos que contengan REF + precio cerca.

    Asume que cada producto está en un bloque consecutivo de texto:
       SHORT RÍGIDO
       INN3756
       Tallas: 8,10,12,14,16
       $56.000
       $47.600
    """
    texto_completo = soup.get_text("\n", strip=True)
    bloques = re.split(r"\n{2,}", texto_completo)

    productos: list[ProductoHTML] = []
    for bloque in bloques:
        if len(bloque) < 20 or len(bloque) > 1500:
            continue
        m_ref = RE_REF.search(bloque)
        if not m_ref:
            continue
        precios = [parsear_precio(m.group()) for m in RE_PRECIO.finditer(bloque)]
        precios = [p for p in precios if p is not None]
        if not precios:
            continue

        ref = normalizar_ref(m_ref.group(1))
        lineas = [l.strip() for l in bloque.splitlines() if l.strip()]
        # El nombre suele ser la primera línea no-ref no-precio
        nombre = ""
        for l in lineas:
            if RE_REF.search(l) or RE_PRECIO.search(l):
                continue
            if 3 <= len(l) <= 80:
                nombre = l
                break
        if not nombre:
            nombre = ref

        precio_detal = max(precios)
        precio_mayor = min(precios) if len(precios) >= 2 else None
        if precio_mayor == precio_detal:
            precio_mayor = None

        productos.append(ProductoHTML(
            ref=ref,
            nombre=nombre,
            categoria=detectar_categoria(nombre),
            precio_detal=precio_detal,
            precio_mayor=precio_mayor,
            tallas=extraer_tallas(bloque),
            colores=extraer_colores(bloque),
            descripcion_raw=bloque[:500],
        ))

    # Dedupe por ref (en caso de bloques que se repitan)
    vistos: set[str] = set()
    unicos: list[ProductoHTML] = []
    for p in productos:
        if p.ref in vistos:
            continue
        vistos.add(p.ref)
        unicos.append(p)
    return unicos


def extraer_productos(html: str) -> tuple[list[ProductoHTML], str]:
    """Aplica las 4 estrategias en orden y retorna la primera con >=5 productos."""
    soup = BeautifulSoup(html, "lxml")

    for nombre_estrategia, fn in [
        ("A: JSON-LD", lambda: estrategia_a_json_ld(soup)),
        ("B: Shopify window meta", lambda: estrategia_b_shopify_window(soup, html)),
        ("C: HTML cards heurística", lambda: estrategia_c_cards_html(soup)),
        ("D: Texto plano", lambda: estrategia_d_texto_plano(soup)),
    ]:
        try:
            productos = fn()
        except Exception as e:
            print(f"   ⚠️  {nombre_estrategia} falló: {e}", file=sys.stderr)
            continue
        if len(productos) >= 5:
            return productos, nombre_estrategia

    # Si ninguna pasó el umbral, devolvemos la mejor
    todas: list[tuple[list[ProductoHTML], str]] = []
    for nombre_estrategia, fn in [
        ("A: JSON-LD", lambda: estrategia_a_json_ld(soup)),
        ("B: Shopify window meta", lambda: estrategia_b_shopify_window(soup, html)),
        ("C: HTML cards heurística", lambda: estrategia_c_cards_html(soup)),
        ("D: Texto plano", lambda: estrategia_d_texto_plano(soup)),
    ]:
        try:
            todas.append((fn(), nombre_estrategia))
        except Exception:
            todas.append(([], nombre_estrategia))

    mejor = max(todas, key=lambda t: len(t[0]))
    return mejor


# ─────────────────────────────────────────────────────────────────────────────
# DB upsert
# ─────────────────────────────────────────────────────────────────────────────

UPSERT_SQL = """
INSERT INTO productos_cache (
    ref, origen, fuente_url, nombre, descripcion, categoria,
    precio_detal, precio_mayor, tallas, colores, variants,
    imagen_url, imagen_url_extras, asumir_disponible, activo
) VALUES %s
ON CONFLICT (ref) DO UPDATE SET
    -- NO sobrescribe variants/shopify_product_id si vienen de Shopify
    origen = CASE
        WHEN productos_cache.origen = 'shopify' THEN productos_cache.origen
        ELSE EXCLUDED.origen
    END,
    fuente_url = EXCLUDED.fuente_url,
    nombre = COALESCE(EXCLUDED.nombre, productos_cache.nombre),
    descripcion = COALESCE(EXCLUDED.descripcion, productos_cache.descripcion),
    categoria = COALESCE(EXCLUDED.categoria, productos_cache.categoria),
    precio_detal = COALESCE(productos_cache.precio_detal, EXCLUDED.precio_detal),
    precio_mayor = COALESCE(productos_cache.precio_mayor, EXCLUDED.precio_mayor),
    tallas = CASE
        WHEN jsonb_array_length(productos_cache.tallas) > 0 THEN productos_cache.tallas
        ELSE EXCLUDED.tallas
    END,
    colores = CASE
        WHEN jsonb_array_length(productos_cache.colores) > 0 THEN productos_cache.colores
        ELSE EXCLUDED.colores
    END,
    imagen_url = COALESCE(productos_cache.imagen_url, EXCLUDED.imagen_url),
    imagen_url_extras = EXCLUDED.imagen_url_extras,
    asumir_disponible = EXCLUDED.asumir_disponible,
    sincronizado_en = NOW();
"""


def upsert(productos: list[ProductoHTML], dsn: str) -> int:
    if not productos:
        return 0
    rows = [
        (
            p.ref,
            "html_catalogo",
            p.fuente_url,
            p.nombre,
            p.descripcion_raw,
            p.categoria,
            p.precio_detal,
            p.precio_mayor,
            Json(p.tallas),
            Json(p.colores),
            Json([]),  # variants vacío para html_catalogo
            p.imagen_url,
            Json(p.imagen_url_extras),
            True,      # asumir_disponible
            True,      # activo
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

def fetch_html(url: str) -> str:
    with httpx.Client(timeout=60, follow_redirects=True, headers={"User-Agent": USER_AGENT}) as c:
        r = c.get(url)
        r.raise_for_status()
        return r.text


def main() -> int:
    parser = argparse.ArgumentParser(description="Sincroniza catálogo HTML público → Postgres")
    parser.add_argument("--url", default=URL_CATALOGO)
    parser.add_argument("--from-file", help="Leer HTML de un archivo local en vez de fetch")
    parser.add_argument("--save-html", help="Guardar el HTML descargado a este path (para debug)")
    parser.add_argument("--save-json", help="Guardar productos extraídos como JSON")
    parser.add_argument(
        "--dsn",
        default=os.environ.get("DATABASE_URL_SYNC", "postgresql://asistente_user:Colombia1234.@127.0.0.1:5432/asistente_db"),
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.from_file:
        print(f"📂 Leyendo HTML de {args.from_file} …")
        html = Path(args.from_file).read_text(encoding="utf-8")
    else:
        print(f"🌐 Descargando {args.url} …")
        try:
            html = fetch_html(args.url)
        except Exception as e:
            print(f"❌ Error: {e}", file=sys.stderr)
            return 2
        print(f"   → {len(html):,} bytes")

    if args.save_html:
        Path(args.save_html).write_text(html, encoding="utf-8")
        print(f"   → HTML guardado en {args.save_html}")

    print("🔍 Extrayendo productos …")
    productos, estrategia = extraer_productos(html)
    print(f"   → estrategia ganadora: {estrategia}")
    print(f"   → {len(productos)} productos extraídos")

    # Resumen
    if productos:
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
        print(f"   → Por categoría: {por_cat}")
        if sin_precio:
            print(f"   ⚠️  {sin_precio} sin precio")
        if sin_imagen:
            print(f"   ⚠️  {sin_imagen} sin imagen")

    if args.save_json:
        out = [
            {**asdict(p), "precio_detal": str(p.precio_detal) if p.precio_detal else None,
             "precio_mayor": str(p.precio_mayor) if p.precio_mayor else None}
            for p in productos
        ]
        Path(args.save_json).write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"   → JSON guardado en {args.save_json}")

    if args.dry_run:
        print("\n🧪 Dry-run. Primeros 3:")
        for p in productos[:3]:
            print(f"   {p.ref}: {p.nombre} | {p.precio_detal} | tallas={p.tallas} | colores={p.colores}")
        return 0

    print(f"📤 Upserting a Postgres …")
    n = upsert(productos, args.dsn)
    print(f"✅ {n} productos sincronizados (origen='html_catalogo')")
    return 0


if __name__ == "__main__":
    sys.exit(main())
