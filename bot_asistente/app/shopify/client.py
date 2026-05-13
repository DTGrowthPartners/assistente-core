"""
Cliente Shopify — dos fuentes:
  1. https://innovacionfashion.co/products.json (público, ✅ funciona)
  2. https://innova.dtgrowthpartners.com/api    (intermedia, draft orders — token caducado)

Lectura de productos: usamos products.json (más fresco, sin token).
Creación de draft orders: usa la API intermedia (cuando el admin renueve el token).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()


class ShopifyError(Exception):
    pass


# ─── LECTURA PÚBLICA (productos) ───────────────────────────────────────────


async def fetch_productos_publicos(limit: int = 250, page: int = 1) -> list[dict[str, Any]]:
    """Lista productos vía endpoint público products.json (sin token)."""
    url = settings.catalogo_publico_url
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.get(url, params={"limit": limit, "page": page})
        r.raise_for_status()
        return (r.json() or {}).get("products", [])


async def fetch_producto_por_handle(handle: str) -> dict[str, Any] | None:
    """Trae un producto específico por handle desde el endpoint público."""
    url = f"https://innovacionfashion.co/products/{handle}.json"
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(url)
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return (r.json() or {}).get("product")


# ─── API INTERMEDIA (draft orders) ─────────────────────────────────────────


async def crear_draft_order(
    cliente_numero: str,
    nombre_cliente: str,
    items: list[dict[str, Any]],
    enviar_link_whatsapp: bool = True,
) -> dict[str, Any]:
    """
    Crea un draft order en Shopify vía API intermedia.

    items: [{"variant_id": int, "quantity": int}]

    Devuelve el draft order creado, con `invoice_url` para que el cliente pague.
    Lanza ShopifyError si la API responde error (típicamente token caducado).
    """
    url = f"{settings.shopify_api_base_url}/draft-orders"
    payload = {
        "phone": cliente_numero,
        "customer_name": nombre_cliente,
        "send_whatsapp": enviar_link_whatsapp,
        "products": items,
    }
    headers = {"x-api-key": settings.shopify_api_key}

    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(url, json=payload, headers=headers)
        if r.status_code >= 400:
            log.error(
                "shopify.draft_order.fail",
                status=r.status_code,
                body=r.text[:400],
            )
            raise ShopifyError(
                f"Draft order falló: HTTP {r.status_code}. "
                f"Esto suele significar que el SHOPIFY_ACCESS_TOKEN del servidor "
                f"de la API intermedia está caducado. Mensaje: {r.text[:200]}"
            )
        return r.json()


# ─── HELPERS ───────────────────────────────────────────────────────────────


def precio_min_variantes(variants: list[dict]) -> Decimal | None:
    """Devuelve el precio mínimo entre las variantes (precio detal típico)."""
    precios = []
    for v in variants or []:
        try:
            precios.append(Decimal(str(v.get("price", 0))))
        except Exception:
            continue
    return min(precios) if precios else None
