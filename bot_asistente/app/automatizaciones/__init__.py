"""Sistema de automatizaciones (tareas recurrentes editables desde el admin).

- scheduler.py: loop async que cada 60s ejecuta tareas con `proxima_ejecucion <= now()`
- acciones.py: registry de acciones predefinidas (reportes, recordatorios, etc.)
"""

from app.automatizaciones.acciones import ACCIONES_DISPONIBLES, ejecutar_accion
from app.automatizaciones.scheduler import iniciar_scheduler, detener_scheduler

__all__ = [
    "ACCIONES_DISPONIBLES",
    "ejecutar_accion",
    "iniciar_scheduler",
    "detener_scheduler",
]
