"""Modo demo carnicería.

Permite que números específicos usen este mismo bot como una carnicería,
consultando un catálogo vivo desde Google Sheets publicado como CSV.
"""

from __future__ import annotations

import csv
import io
import time
import unicodedata
from difflib import SequenceMatcher
from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

_cache: dict[str, Any] = {"ts": 0.0, "items": []}


def normalizar_numero(numero: str | None) -> str:
    digitos = "".join(ch for ch in str(numero or "") if ch.isdigit())
    if len(digitos) == 10 and digitos.startswith("3"):
        return "+57" + digitos
    if digitos.startswith("57") and len(digitos) == 12:
        return "+" + digitos
    if str(numero or "").startswith("+"):
        return "+" + digitos
    return digitos


def es_numero_demo(numero: str | None) -> bool:
    settings = get_settings()
    permitidos = {
        normalizar_numero(n.strip())
        for n in (settings.carniceria_demo_numeros or "").split(",")
        if n.strip()
    }
    return normalizar_numero(numero) in permitidos


def _normalizar_texto(valor: Any) -> str:
    texto = unicodedata.normalize("NFKD", str(valor or "").lower())
    texto = "".join(ch for ch in texto if not unicodedata.combining(ch))
    return " ".join(texto.split())


def _campo(row: dict[str, str], *nombres: str) -> str:
    normalizados = {_normalizar_texto(k): v for k, v in row.items()}
    for nombre in nombres:
        if _normalizar_texto(nombre) in normalizados:
            return (normalizados[_normalizar_texto(nombre)] or "").strip()
    return ""


def _item_desde_row(row: dict[str, str]) -> dict[str, str]:
    precio_base = _campo(row, "precio", "valor", "precio por kg (ars)")
    precio_oferta = _campo(row, "precio oferta", "precio oferta (ars)", "precio promocion", "precio promoción")
    en_oferta = _campo(row, "en oferta", "oferta", "promo", "promocion", "promoción")
    descuento = _campo(row, "% descuento", "descuento")
    precio = precio_oferta or precio_base
    oferta = _campo(row, "oferta", "promo", "promocion", "promoción")
    if precio_oferta or _normalizar_texto(en_oferta) in {"si", "sí", "yes", "true", "1"}:
        partes = []
        if precio_oferta:
            partes.append(f"precio oferta {precio_oferta}")
        if precio_base and precio_base != precio_oferta:
            partes.append(f"precio regular {precio_base}")
        if descuento:
            partes.append(f"descuento {descuento}")
        oferta = ", ".join(partes) or en_oferta

    return {
        "corte": _campo(row, "corte", "producto", "nombre", "item"),
        "categoria": _campo(row, "categoria", "tipo"),
        "precio": precio,
        "unidad": _campo(row, "unidad", "presentacion", "medida") or ("kg" if precio_base or precio_oferta else ""),
        "disponible": _campo(row, "disponible", "stock", "estado", "disponibilidad"),
        "oferta": oferta,
        "descripcion": _campo(row, "descripcion", "descripción", "detalle", "notas"),
    }


async def cargar_menu(force: bool = False) -> dict[str, Any]:
    settings = get_settings()
    url = settings.carniceria_sheet_csv_url
    if not url:
        return {
            "ok": False,
            "error": "CARNICERIA_SHEET_CSV_URL no está configurada",
            "items": [],
        }

    ahora = time.time()
    ttl = max(int(settings.carniceria_sheet_cache_segundos or 180), 30)
    if not force and _cache["items"] and ahora - float(_cache["ts"]) < ttl:
        return {"ok": True, "items": _cache["items"], "cached": True}

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            res = await client.get(url)
        if res.status_code >= 400:
            return {"ok": False, "error": f"Google Sheets CSV HTTP {res.status_code}", "items": []}
        reader = csv.DictReader(io.StringIO(res.text))
        items = [
            item for item in (_item_desde_row(row) for row in reader)
            if item["corte"]
        ]
        _cache.update({"ts": ahora, "items": items})
        return {"ok": True, "items": items, "cached": False}
    except Exception as e:
        log.warning("carniceria.sheet_fail", error=str(e)[:200])
        return {"ok": False, "error": str(e)[:200], "items": _cache["items"]}


def _score(item: dict[str, str], consulta: str) -> float:
    q = _normalizar_texto(consulta)
    texto = _normalizar_texto(
        " ".join([
            item.get("corte", ""),
            item.get("categoria", ""),
            item.get("descripcion", ""),
            item.get("oferta", ""),
        ])
    )
    if not q:
        return 0.1
    if q in texto:
        return 1.0
    palabras = [p for p in q.split() if len(p) > 2]
    if palabras and all(p in texto for p in palabras):
        return 0.9
    if palabras and any(p in texto for p in palabras):
        return 0.65
    return SequenceMatcher(None, q, texto).ratio()


async def buscar_menu(consulta: str, limite: int = 6) -> dict[str, Any]:
    res = await cargar_menu()
    if not res.get("ok"):
        return res
    items = list(res.get("items") or [])
    scored = sorted(
        ((_score(item, consulta), item) for item in items),
        key=lambda pair: pair[0],
        reverse=True,
    )
    encontrados = [item for score, item in scored if score >= 0.45][: max(1, min(limite, 10))]
    if not encontrados and not consulta:
        encontrados = items[: max(1, min(limite, 10))]
    ofertas = [item for item in items if item.get("oferta")][:3]
    return {
        "ok": True,
        "consulta": consulta,
        "total_items": len(items),
        "resultados": encontrados,
        "ofertas": ofertas,
    }


PROMPT_CARNICERIA = """
Eres el vendedor de WhatsApp de una carnicería. Atiendes SOLO este demo.

PERSONALIDAD
- Cercano, vendedor y práctico. Buscas ayudar y cerrar venta.
- Mensajes cortos, naturales, tipo WhatsApp.
- No digas que eres Dairo, DTGP, agencia ni bot. Eres la carnicería.

OBJETIVO
1. Responder precios y disponibilidad de cortes de carne.
2. Recomendar cortes relacionados, combos y ofertas.
3. Empujar suavemente al pedido: cantidad, dirección, hora de entrega o recogida.
4. Si no está el corte exacto, ofrece alternativas parecidas.

CATÁLOGO Y PRECIOS
- Para precios, cortes, combos, disponibilidad u ofertas, usa SIEMPRE la tool
  `consultar_menu_carniceria`.
- No inventes precios. Si la hoja no está configurada o no encuentras el corte,
  dilo con naturalidad y ofrece revisar alternativas disponibles.
- Si el cliente pregunta por "carne para asar", "sancocho", "molida",
  "punta", "lomo", etc., consulta el menú y sugiere 2-3 opciones.

VENTA
- Después de dar precio, intenta avanzar: "¿Cuántas libras te separo?" o
  "¿Te lo mando para hoy?".
- Menciona ofertas si la tool las devuelve y son relevantes.
- No hagas diagnósticos de marketing ni hables de reuniones.
""".strip()
