"""Limpieza de menciones WhatsApp (LIDs) en texto entrante.

Cuando alguien hace `@Dairo Traslaviña` en un grupo, whapi entrega el texto con
el LID interno: `@243365899698272 \nhola, tienes acceso al modulo ...`.

Si pasamos ese texto crudo a Claude, el modelo se confunde — no sabe qué es ese
ID, alucina respuestas técnicas mencionándolo. Esta función reemplaza patrones
`@<digits-largos>` con un placeholder neutral antes de mostrárselo al modelo.

Si en el futuro se agrega un mapeo LID→nombre (ej. columna `lid_whatsapp` en
EquipoMiembro), pasarlo aquí en `mapeo` para sustituciones más precisas.
"""

from __future__ import annotations

import re

# LIDs de whapi suelen ser cadenas numéricas de 10+ dígitos (ej. 243365899698272).
# Restringimos el patrón a >=10 dígitos para no afectar menciones inocuas tipo
# "@123" o números de teléfono cortos.
_PATRON_LID = re.compile(r"@(\d{10,})")


def limpiar_menciones_lid(
    texto: str | None,
    mapeo: dict[str, str] | None = None,
) -> str:
    """Reemplaza `@<lid>` en el texto por `@<nombre>` (si está mapeado) o por
    `@miembro` como fallback neutro.

    No quita la mención — sigue indicando que ALGUIEN fue mencionado, solo que
    sin el ID opaco que confunde al modelo.

    >>> limpiar_menciones_lid("@243365899698272 hola")
    '@miembro hola'
    >>> limpiar_menciones_lid("@243365899698272 hola", {"243365899698272": "Dairo"})
    '@Dairo hola'
    """
    if not texto:
        return texto or ""
    mp = mapeo or {}

    def _sub(m: re.Match) -> str:
        lid = m.group(1)
        nombre = mp.get(lid)
        return f"@{nombre}" if nombre else "@miembro"

    return _PATRON_LID.sub(_sub, texto)
