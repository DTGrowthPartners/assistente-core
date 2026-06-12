"""Helper centralizado para notificar al equipo DTGP vía grupo de WhatsApp.

Cuando ocurre algo que necesita atención humana (alerta, mensaje sin
clasificar, escalación, etc.), en lugar de mandar mensajes individuales
a Dairo/Stiven, los publicamos en el grupo `EQUIPO DTGP` para que el
primero que esté disponible atienda.

Configurable vía `settings.equipo_dtgp_group_id`. Si está vacío, cae al
fallback de mandar a `superiores_para()` individuales.
"""

from __future__ import annotations

from app.config import get_settings
from app.logging_setup import log
from app.whapi.client import enviar_botones, enviar_texto

settings = get_settings()


async def notificar_equipo(texto: str) -> bool:
    """Envía un mensaje al grupo del equipo DTGP.

    Devuelve True si se envió correctamente. False si falló o no hay grupo
    configurado (en cuyo caso el caller puede caer al fallback).

    Es seguro llamarlo desde cualquier flujo — atrapa errores y los loguea
    sin propagar (no queremos que un fallo de notif rompa el flujo principal).
    """
    if not settings.equipo_dtgp_group_id:
        log.warning("notif_equipo.sin_grupo_configurado")
        return False
    try:
        await enviar_texto(settings.equipo_dtgp_group_id, texto)
        return True
    except Exception as e:
        log.exception("notif_equipo.fail", error=str(e))
        return False


async def notificar_equipo_con_botones(
    body: str,
    botones: list[tuple[str, str]],
    *,
    header: str | None = None,
    footer: str | None = None,
) -> dict | None:
    """Envía una alerta al grupo con botones quick-reply (1-3 botones).

    Devuelve la respuesta de whapi (incluye message.id para guardar metadata
    de la alerta), o None si falla / no hay grupo configurado.
    """
    if not settings.equipo_dtgp_group_id:
        log.warning("notif_equipo.sin_grupo_configurado")
        return None
    try:
        return await enviar_botones(
            settings.equipo_dtgp_group_id,
            body=body, botones=botones, header=header, footer=footer,
        )
    except Exception as e:
        log.exception("notif_equipo_botones.fail", error=str(e))
        return None


async def notificar_equipo_con_fallback(texto: str) -> int:
    """Notifica al grupo. Si falla o no hay grupo, cae a notificar a los
    superiores individuales. Devuelve el número de destinos notificados.
    """
    if await notificar_equipo(texto):
        return 1
    # Fallback individuos
    from app.equipo.directorio import superiores_para
    enviados = 0
    for sup in superiores_para("clientes"):
        try:
            await enviar_texto(sup.numero_whatsapp, texto)
            enviados += 1
        except Exception as e:
            log.warning("notif_equipo.fallback_fail", numero=sup.numero_whatsapp, error=str(e))
    return enviados
