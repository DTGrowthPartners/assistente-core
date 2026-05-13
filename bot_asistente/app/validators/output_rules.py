"""
Validador de respuestas antes de enviarlas al cliente.

Verifica que el output de Claude cumpla las reglas inquebrantables (las que
NO podemos confiar 100% al system prompt). Algunas son críticas de negocio,
otras de seguridad.

Una respuesta que falle alguna regla:
  - Se loggea con warning
  - Se le devuelve a Claude para que reescriba (con la lista de issues)
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass
class ValidationIssue:
    rule: str            # ID de la regla violada
    severity: str        # 'critico' | 'alto' | 'medio'
    descripcion: str     # qué se detectó
    sugerencia: str      # cómo arreglarlo


# ────────────────────────────────────────────────────────────────────────────
# REGLAS
# ────────────────────────────────────────────────────────────────────────────

# Nombres prohibidos al cliente (revelan estructura interna)
NOMBRES_INTERNOS_PROHIBIDOS = ["Fabio", "Yirleis", "Luis Tirado", "Don Luis", "Sr Luis"]

# Número del dueño — bidireccional
NUMERO_BLOQUEADO_PATRONES = [
    r"\+?57\s*320\s*681\s*1130",
    r"\+?57\s*320\s*?681\s*?1130",
    r"\b573206811130\b",
    r"\b3206811130\b",
]

# Frases de notas internas que no deben ir al chat del cliente
NOTAS_INTERNAS_PROHIBIDAS = [
    r"conversaci[oó]n\s+(cerrada|pendiente|completada)",
    r"cliente\s*\(",
    r"seguimiento\s+necesario",
    r"Cliente\s+est[áa]\s+lista\s+para",
    r"\(continuar mañana",
    r"pendiente de respuesta",
]

# Archivos internos
ARCHIVOS_INTERNOS = [
    r"\bIDENTITY\.md\b", r"\bSOUL\.md\b", r"\bGUIA-VENTAS\.md\b",
    r"\bTOOLS\.md\b", r"\binnovacion-fashion-base\.md\b",
    r"\btarifas-domicilios-cartagena\.md\b", r"\.openclaw\b",
    r"/home/innovacion", r"/opt/openclaw",
]

# Patrones que sugieren precio inventado (mencionar precio sin que venga de tool)
# NOTA: Esto es heurístico. La regla principal es que tool `buscar_productos`
# alimente los precios. Aquí solo flag-eamos formatos sospechosos.
PRECIO_FORMATO_SOSPECHOSO = [
    r"\$\d{1,3}k\b",        # "$56k" en lugar de "$56.000"
    r"\$\d+,\d{3}",         # "$56,000" formato gringo
]


def validar(texto: str, *, contexto: dict | None = None) -> list[ValidationIssue]:
    """
    Valida una respuesta. Devuelve lista de issues (vacía = OK).

    contexto opcional:
        {"productos_mencionados": [...refs...], "precios_validos": {ref: precio}}
    """
    issues: list[ValidationIssue] = []
    if not texto:
        return issues

    # 1. Nombres internos prohibidos
    for nombre in NOMBRES_INTERNOS_PROHIBIDOS:
        if re.search(rf"\b{re.escape(nombre)}\b", texto, re.IGNORECASE):
            issues.append(ValidationIssue(
                rule="R19_NOMBRES_INTERNOS",
                severity="critico",
                descripcion=f"La respuesta menciona '{nombre}' al cliente.",
                sugerencia=f"Reemplazar '{nombre}' por 'el equipo' o 'una asesora'.",
            ))

    # 2. Número del dueño
    for patron in NUMERO_BLOQUEADO_PATRONES:
        if re.search(patron, texto):
            issues.append(ValidationIssue(
                rule="R10_NUMERO_BLOQUEADO",
                severity="critico",
                descripcion="La respuesta contiene el número del dueño (+573206811130).",
                sugerencia="Quitar ese número. NUNCA compartir números internos.",
            ))
            break

    # 3. Notas internas escritas al cliente
    for patron in NOTAS_INTERNAS_PROHIBIDAS:
        if re.search(patron, texto, re.IGNORECASE):
            issues.append(ValidationIssue(
                rule="R15_NOTAS_INTERNAS",
                severity="critico",
                descripcion=f"La respuesta parece nota interna de reporte: '{patron}'.",
                sugerencia="Reescribir solo el mensaje comercial al cliente, sin reportes.",
            ))
            break

    # 4. Archivos internos / paths
    for patron in ARCHIVOS_INTERNOS:
        if re.search(patron, texto, re.IGNORECASE):
            issues.append(ValidationIssue(
                rule="R18_ARCHIVOS_INTERNOS",
                severity="alto",
                descripcion=f"La respuesta filtra info interna: {patron}",
                sugerencia="Reescribir sin mencionar archivos/paths del sistema.",
            ))

    # 5. Formato de precio sospechoso (heurístico)
    for patron in PRECIO_FORMATO_SOSPECHOSO:
        if re.search(patron, texto, re.IGNORECASE):
            issues.append(ValidationIssue(
                rule="R4_PRECIO_FORMATO",
                severity="medio",
                descripcion=f"Formato de precio no estándar detectado: {patron}",
                sugerencia="Usa formato $XX.XXX con punto como separador de miles.",
            ))

    # 6. Precio inventado (si pasamos contexto con precios válidos)
    if contexto:
        precios_validos = set(map(str, (contexto.get("precios_validos") or {}).values()))
        if precios_validos:
            precios_en_texto = re.findall(r"\$([\d.,]+)", texto)
            for p in precios_en_texto:
                normalizado = p.replace(".", "").replace(",", "")
                if normalizado and normalizado not in precios_validos:
                    # Es solo warning porque puede ser una suma legítima (total con envío)
                    issues.append(ValidationIssue(
                        rule="R4_PRECIO_NO_RECONOCIDO",
                        severity="medio",
                        descripcion=f"Precio ${p} no coincide con ningún producto del catálogo.",
                        sugerencia=(
                            "Verifica que el precio venga de tool `buscar_productos` o "
                            "sea una suma legítima (subtotal + domicilio)."
                        ),
                    ))

    # 7. Bot menciona ser Laura (no debería pasar pero defensivo)
    if re.search(r"\bsoy\s+laura\b", texto, re.IGNORECASE):
        issues.append(ValidationIssue(
            rule="R_IDENTIDAD",
            severity="critico",
            descripcion="El bot se identifica como 'Laura' (incorrecto, somos 'asistente virtual').",
            sugerencia="Reemplazar por 'Soy el asistente virtual de Innovación Fashion'.",
        ))

    # 8. Emojis — el negocio decidió no usar ninguno
    if _contiene_emoji(texto):
        issues.append(ValidationIssue(
            rule="R_NO_EMOJIS",
            severity="alto",
            descripcion="La respuesta contiene emojis. Política: cero emojis en mensajes al cliente.",
            sugerencia="Quitar todos los emojis. La calidez se transmite con palabras.",
        ))

    return issues


# Rango de emojis Unicode (cubre la mayoría usados en WhatsApp)
_EMOJI_PATTERN = re.compile(
    "["
    "\U0001F300-\U0001F9FF"  # Misc Symbols and Pictographs, Supplemental Symbols
    "\U0001F600-\U0001F64F"  # Emoticons
    "\U0001F680-\U0001F6FF"  # Transport and Map
    "\U0001F700-\U0001F77F"
    "\U0001F780-\U0001F7FF"
    "\U0001F800-\U0001F8FF"
    "\U0001F900-\U0001F9FF"
    "\U0001FA00-\U0001FA6F"
    "\U0001FA70-\U0001FAFF"
    "\U00002600-\U000027BF"  # Misc Symbols + Dingbats (incluye 😊 no, ese está arriba)
    "\U0001F1E0-\U0001F1FF"  # Banderas
    "]",
    flags=re.UNICODE,
)


def _contiene_emoji(texto: str) -> bool:
    return bool(_EMOJI_PATTERN.search(texto))


def stripear_emojis(texto: str) -> str:
    """Quita emojis y limpia espacios sobrantes. Más barato que pedir reescritura."""
    sin_emoji = _EMOJI_PATTERN.sub("", texto)
    # Limpia espacios dobles y espacios antes de signos de puntuación
    sin_emoji = re.sub(r" {2,}", " ", sin_emoji)
    sin_emoji = re.sub(r" ([,.!?])", r"\1", sin_emoji)
    # Limpia líneas vacías excesivas (más de 2 saltos seguidos)
    sin_emoji = re.sub(r"\n{3,}", "\n\n", sin_emoji)
    return sin_emoji.strip()


def construir_feedback_para_regenerar(issues: list[ValidationIssue]) -> str:
    """Mensaje que se le devuelve a Claude para que reescriba la respuesta."""
    if not issues:
        return ""
    lineas = [
        "Tu respuesta anterior tenía estos problemas y NO se envió al cliente:",
        "",
    ]
    for i, issue in enumerate(issues, 1):
        lineas.append(f"{i}. [{issue.severity.upper()}] {issue.descripcion}")
        lineas.append(f"   → {issue.sugerencia}")
    lineas.append("")
    lineas.append("Por favor reescribe la respuesta corrigiendo TODOS estos puntos.")
    return "\n".join(lineas)
