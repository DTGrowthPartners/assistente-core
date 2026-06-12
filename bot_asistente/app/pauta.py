"""Detección de mensajes "canned" (predeterminados) de pauta Meta.

Estos textos los manda el usuario al **iniciar la conversación desde un anuncio**
o **al pulsar uno de los botones FAQ** del perfil de WhatsApp Business. Para
todos ellos Meta tiene una auto-respuesta configurada — el bot **NO debe
responder**, solo etiquetar al contacto como `prospecto` y esperar a que el
lead escriba algo REAL (entonces sí entra el flujo de prospecto).
"""

from __future__ import annotations

import re
import unicodedata


# Patrones tal como llegan (después se normalizan). Edita acá si Meta o
# WhatsApp Business cambia los textos.
PATRONES_PAUTA: list[str] = [
    # ─── CTA del anuncio (Meta CTWA) ──────────────────────────────────────
    "Hola, estoy interesado en su publicidad",
    "Hola estoy interesado en su publicidad",
    "Hola, quiero más información",
    "Hola quiero mas informacion",
    "Estoy interesado en hacer publicidad para mi negocio",
    # ─── Botones FAQ del perfil de WhatsApp Business ──────────────────────
    # Auditoría
    "Quiero mi auditoría gratis",
    "Quiero mi auditoria gratis",
    "Quiero mi auditoría gratis de publicidad",
    "Quiero mi auditoria gratis de publicidad",
    # Ya pauta pero sin resultados / siente que no rinde
    "Ya pauto pero siento que no rinde la inversión",
    "Ya pauto pero siento que no rinde la inversion",
    "Ya pauto pero no veo resultados",
    "Ya invierto en publicidad pero no veo resultados",
    # Tiene presupuesto y quiere escalar
    "Tengo presupuesto y quiero escalar",
    "Tengo presupuesto y quiero escalar anuncios",
    # Empezar a pautar
    "Quiero empezar a pautar pero no sé cómo",
    "Quiero empezar a pautar pero no se como",
    # Otros servicios
    "Necesito web, agentes IA o automatización",
    "Necesito web agentes IA o automatizacion",
    "Necesito web, agentes ia o automatizacion",
    # Capacitación
    "Me interesa una capacitación",
    "Me interesa una capacitacion",
    # ─── Variantes legacy (mantenemos por si quedaron botones viejos) ─────
    "¿Qué servicios ofrecen y cuánto cuesta?",
    "Qué servicios ofrecen y cuánto cuesta",
    "Que servicios ofrecen y cuanto cuesta",
]


def _normalizar(texto: str) -> str:
    """Quita acentos, emojis, signos y baja a minúsculas. Conserva letras/dígitos/espacios."""
    if not texto:
        return ""
    s = unicodedata.normalize("NFKD", texto)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = re.sub(r"[^a-z0-9\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


# Set normalizado para matching rápido.
_CANNED_NORM: set[str] = {_normalizar(p) for p in PATRONES_PAUTA}


def es_canned_pauta(texto: str | None) -> bool:
    """True si el texto contiene uno de los mensajes predeterminados de pauta.

    Antes hacía match EXACTO contra el set normalizado. Ahora hace match por
    SUBSTRING para capturar mensajes donde el cliente prepende/agrega texto
    ('Hola, vi el perfil y quiero mi auditoría gratis 🚀' → matchea la frase
    'quiero mi auditoria gratis' adentro).
    """
    if not texto:
        return False
    norm = _normalizar(texto)
    if not norm:
        return False
    if norm in _CANNED_NORM:
        return True  # match exacto (rápido)
    # Substring: si alguno de los patrones canónicos está dentro del texto.
    # Solo aplica a patrones de ≥3 palabras para evitar falsos positivos.
    for patron in _CANNED_NORM:
        if len(patron) >= 12 and patron in norm:
            return True
    return False


# Saludos cortos típicos. El que escribe un saludo genérico a este número de
# Dairo probablemente viene de la pauta (es un número público publicitado).
SALUDOS_SIMPLES: set[str] = {
    "hola", "holaa", "holaaa", "ola", "buenas", "buenos dias", "buen dia",
    "buenas tardes", "buenas noches", "hi", "hello", "hey",
    "hola buenas", "hola buen dia", "hola buenas tardes", "hola buenas noches",
    "saludos", "que tal", "como estas", "como estan",
    "info", "informacion", "mas informacion", "quiero mas informacion",
    "interesado", "interesada", "me interesa",
}


def es_saludo_simple(texto: str | None) -> bool:
    """True si el texto es un saludo corto sin más contexto.

    Util para detectar que alguien escribió 'hola' a un número público
    (típicamente desde el botón 'Enviar mensaje' del perfil de WhatsApp
    Business, sin pasar por el CTWA que dispara el referral).
    """
    if not texto:
        return False
    norm = _normalizar(texto)
    if not norm:
        return False
    # Texto corto + matchea un saludo conocido
    if len(norm) <= 40 and norm in SALUDOS_SIMPLES:
        return True
    # Empieza con saludo conocido + es muy corto (≤6 palabras total)
    palabras = norm.split()
    if len(palabras) <= 6:
        primeras = " ".join(palabras[:2])
        if primeras in SALUDOS_SIMPLES or palabras[0] in SALUDOS_SIMPLES:
            return True
    return False
