#!/usr/bin/env python3
"""Bulk-import de contactos al canal de Dairo (CSV o vCard).

Por defecto los marca como `etiqueta='personal'` (el bot NUNCA responde a ellos
en el canal de Dairo). Es la opción SEGURA: importas TODO como personal y luego
re-etiquetas los pocos que sí son clientes/prospectos/equipo.

Uso:
    # CSV: columnas numero,nombre,etiqueta (etiqueta opcional, default 'personal')
    python scripts/import_contactos_dairo.py --archivo data/contactos_dairo.csv

    # vCard exportado del teléfono — todos quedan como 'personal'
    python scripts/import_contactos_dairo.py --archivo data/contactos_dairo.vcf

Flags:
    --default-etiqueta {personal,prospecto,cliente,equipo}   (default: personal)
    --dry-run                                                 (no escribe nada)
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

# Asegurar que el paquete `app` esté en path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import get_settings  # noqa: E402
from app.db.models import Cliente  # noqa: E402


VALID_ETIQUETAS = {"personal", "prospecto", "cliente", "equipo"}


def _normalize_numero(raw: str, strict: bool = False) -> str | None:
    """A '+57...' E.164. Devuelve None si parece basura.

    Con `strict=True` solo acepta **Colombia válida** (móvil `+573X...` 10 dígitos
    o fijo `+5760X...` 10 dígitos). Rechaza internacionales, truncados, ruido.
    """
    if not raw:
        return None
    s = re.sub(r"[^\d+]", "", raw)
    if not s:
        return None
    # Normalizar prefijo
    if s.startswith("+"):
        digits = s.lstrip("+")
    else:
        digits = s
        if len(digits) == 10 and (digits.startswith("3") or digits.startswith("60")):
            digits = "57" + digits
        elif len(digits) == 12 and digits.startswith("57"):
            pass
        elif not strict:
            # modo flexible: deja que sea
            return "+" + digits if digits else None
        else:
            return None

    if strict:
        # Solo Colombia
        if not digits.startswith("57"):
            return None
        rest = digits[2:]
        if len(rest) != 10:
            return None
        if not (rest.startswith("3") or rest.startswith("60")):
            return None
        return "+" + digits

    # Modo flexible (compatibilidad con la importación anterior)
    if len(digits) < 8 or len(digits) > 15:
        return None
    return "+" + digits


_MESES_RE = r"Enero|Febrero|Marzo|Abril|Mayo|Junio|Julio|Agosto|Septiembre|Octubre|Noviembre|Diciembre"
_RE_FECHA_A = re.compile(rf"^({_MESES_RE})\s*\d+$", re.IGNORECASE)
_RE_FECHA_B = re.compile(rf"^\d+\s*(de\s+)?({_MESES_RE})\b", re.IGNORECASE)
_RE_CLIENTE_N = re.compile(r"^cliente\s*\d+$", re.IGNORECASE)
_RE_CORTO = re.compile(r"^[a-zA-Z0-9]{1,3}$")
_RE_PURO_NUM = re.compile(r"^\d+$")
_RE_BACKSLASH = re.compile(r"^[\\,\s.]+$")


def _limpiar_nombre(raw: str | None) -> str | None:
    """Devuelve None si el nombre parece basura (códigos cortos, fechas, etc.)."""
    if not raw:
        return None
    s = raw.strip()
    # quitar escapes vCard típicos al inicio
    s = s.replace("\\,", ",").replace("\\;", ";").strip(" ,;.")
    if not s:
        return None
    if len(s) <= 2:
        return None
    if _RE_PURO_NUM.match(s):
        return None
    if _RE_BACKSLASH.match(s):
        return None
    if _RE_FECHA_A.match(s):
        return None
    if _RE_FECHA_B.match(s):
        return None
    if _RE_CLIENTE_N.match(s):
        return None
    if _RE_CORTO.match(s):
        return None
    return s[:100]


def _parse_csv(path: Path, strict: bool = False) -> list[tuple[str, str | None, str | None]]:
    out: list[tuple[str, str | None, str | None]] = []
    with path.open(encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            numero = _normalize_numero((row.get("numero") or row.get("phone") or "").strip(), strict=strict)
            if not numero:
                continue
            nombre_raw = (row.get("nombre") or row.get("name") or "").strip() or None
            nombre = _limpiar_nombre(nombre_raw) if strict else nombre_raw
            etiqueta = (row.get("etiqueta") or "").strip().lower() or None
            if etiqueta and etiqueta not in VALID_ETIQUETAS:
                print(f"⚠ etiqueta inválida '{etiqueta}' para {numero}, ignorada")
                etiqueta = None
            out.append((numero, nombre, etiqueta))
    return out


def _parse_vcard(path: Path, strict: bool = False) -> list[tuple[str, str | None, str | None]]:
    out: list[tuple[str, str | None, str | None]] = []
    text = path.read_text(encoding="utf-8", errors="replace")
    for bloque in re.split(r"BEGIN:VCARD", text, flags=re.IGNORECASE):
        if "END:VCARD" not in bloque.upper():
            continue
        nombre_raw = None
        m_fn = re.search(r"^FN[^:]*:(.+)$", bloque, flags=re.MULTILINE | re.IGNORECASE)
        if m_fn:
            nombre_raw = m_fn.group(1).strip()
        nombre = _limpiar_nombre(nombre_raw) if strict else nombre_raw
        for m_tel in re.finditer(
            r"^(?:[\w.]+\.)?TEL[^:]*:(.+)$",
            bloque, flags=re.MULTILINE | re.IGNORECASE,
        ):
            numero = _normalize_numero(m_tel.group(1).strip(), strict=strict)
            if numero:
                out.append((numero, nombre, None))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--archivo", required=True)
    ap.add_argument("--default-etiqueta", default="personal", choices=sorted(VALID_ETIQUETAS))
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--strict", action="store_true",
        help="Limpia agresivamente: solo Colombia válida, nombres-basura → null.",
    )
    args = ap.parse_args()

    path = Path(args.archivo)
    if not path.exists():
        print(f"✗ archivo no existe: {path}")
        sys.exit(1)

    if path.suffix.lower() == ".csv":
        contactos = _parse_csv(path, strict=args.strict)
    elif path.suffix.lower() in (".vcf", ".vcard"):
        contactos = _parse_vcard(path, strict=args.strict)
    else:
        print(f"✗ extensión no soportada: {path.suffix}. Usa .csv o .vcf")
        sys.exit(1)

    # Dedupe por numero (último gana)
    dedup: dict[str, tuple[str, str | None]] = {}
    for numero, nombre, etq in contactos:
        dedup[numero] = (nombre, etq)
    contactos = [(n, nm, e) for n, (nm, e) in dedup.items()]

    print(f"→ {len(contactos)} contactos únicos en {path.name}")
    if args.dry_run:
        for n, nm, e in contactos[:20]:
            print(f"  {n}  {nm}  → {e or args.default_etiqueta}")
        if len(contactos) > 20:
            print(f"  ... y {len(contactos)-20} más")
        return

    settings = get_settings()
    engine = create_engine(settings.database_url_sync, pool_size=2, max_overflow=2)
    creados = actualizados = saltados = 0
    with Session(engine) as s:
        for numero, nombre, etq in contactos:
            etiqueta = etq or args.default_etiqueta
            existing = s.execute(
                select(Cliente).where(Cliente.numero_whatsapp == numero)
            ).scalar_one_or_none()
            if existing:
                if existing.etiqueta is None or existing.etiqueta != etiqueta:
                    # No pisamos etiquetas explícitas distintas a default a menos que el CSV lo diga.
                    if etq:
                        existing.etiqueta = etiqueta
                        existing.etiqueta_actualizada_en = datetime.now(timezone.utc)
                        existing.etiqueta_actualizada_por = "import_contactos_dairo"
                        actualizados += 1
                    elif existing.etiqueta is None:
                        existing.etiqueta = etiqueta
                        existing.etiqueta_actualizada_en = datetime.now(timezone.utc)
                        existing.etiqueta_actualizada_por = "import_contactos_dairo"
                        actualizados += 1
                    else:
                        saltados += 1
                else:
                    saltados += 1
                if nombre and not (existing.nombre or "").strip():
                    existing.nombre = nombre[:100]
            else:
                c = Cliente(
                    numero_whatsapp=numero,
                    nombre=nombre[:100] if nombre else None,
                    etiqueta=etiqueta,
                    etiqueta_actualizada_en=datetime.now(timezone.utc),
                    etiqueta_actualizada_por="import_contactos_dairo",
                )
                s.add(c)
                creados += 1
        s.commit()

    print(f"✓ creados={creados}  actualizados={actualizados}  saltados={saltados}")


if __name__ == "__main__":
    main()
