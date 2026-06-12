"""Registry de identidades del bot (multi-canal whapi).

Identidad PRINCIPAL = canal principal de whapi (`WHAPI_TOKEN` + `WHAPI_NUMERO_BOT`).
Su nombre/persona/política se configuran vía `IDENTIDAD_PRINCIPAL_*` en `.env`.
Hoy: Dairo Traslaviña (CEO), persona Dairo, política ESTRICTA.

Identidad SECUNDARIA (opcional) = `WHAPI_TOKEN_DAIRO` + `WHAPI_NUMERO_DAIRO`. Se
usa si existe un segundo canal whapi en paralelo. Si vacía, queda inactiva y
`/webhook/secundaria` devuelve "ignorada".
"""

from __future__ import annotations

from dataclasses import dataclass

from app.config import get_settings


@dataclass(frozen=True)
class Identidad:
    key: str                          # "principal" | "secundaria"
    nombre: str                       # display name (ej. "Dairo Traslaviña")
    numero: str                       # +57... (el bot)
    token: str                        # whapi token de este canal
    persona_prompt_file: str | None   # data/prompts/<file> ; None = usa IDENTIDAD por defecto
    politica_estricta: bool           # True → desconocidos sin etiqueta = silencio
    webhook_path: str                 # "/webhook" o "/webhook/<key>"
    activa: bool                      # depende de tener token + número configurados


def _construir() -> dict[str, Identidad]:
    s = get_settings()
    principal = Identidad(
        key="principal",
        nombre=s.identidad_principal_nombre,
        numero=s.whapi_numero_bot,
        token=s.whapi_token,
        persona_prompt_file=s.identidad_principal_persona_file or None,
        politica_estricta=bool(s.identidad_principal_estricta),
        webhook_path="/webhook",
        activa=bool(s.whapi_token and s.whapi_numero_bot),
    )
    secundaria = Identidad(
        key="secundaria",
        nombre="Secundaria",
        numero=s.whapi_numero_dairo,   # nombre histórico de la var
        token=s.whapi_token_dairo,
        persona_prompt_file=None,
        politica_estricta=False,
        webhook_path="/webhook/secundaria",
        activa=bool(s.whapi_token_dairo and s.whapi_numero_dairo),
    )
    return {"principal": principal, "secundaria": secundaria}


def todas() -> dict[str, Identidad]:
    return _construir()


def por_key(key: str) -> Identidad | None:
    return _construir().get(key)


def por_path(path: str) -> Identidad | None:
    for i in _construir().values():
        if i.webhook_path == path:
            return i
    return None


def principal() -> Identidad:
    """La identidad por defecto del canal principal (/webhook)."""
    return _construir()["principal"]


def dairo() -> Identidad:
    """Identidad principal (alias semántico — el bot habla como Dairo)."""
    return principal()
