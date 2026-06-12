"""Dry-run de la acción seguimiento_prospectos: solo lista candidatos, no envía."""
import asyncio, sys
sys.path.insert(0, '/home/ubuntu/maria')
from app.db.session import async_session_factory
from app.automatizaciones.acciones import ejecutar_accion
from app.config import get_settings

# OJO: necesitamos forzar feature ON solo para el dry_run, no en BD/env
s = get_settings()
print(f"feature_seguimiento_auto actual: {s.feature_seguimiento_auto}")

# Temporal: parchear setting en memoria para que el dry-run funcione
import app.automatizaciones.acciones as acc
acc.settings.feature_seguimiento_auto = True

async def main():
    async with async_session_factory() as session:
        res = await ejecutar_accion("seguimiento_prospectos", session, {
            "dias_min": 1,
            "dias_max": 14,   # ampliado para ver más leads
            "max_envios": 50,
            "dry_run": True,
        })
        import json
        print(json.dumps(res, indent=2, ensure_ascii=False, default=str)[:5000])

asyncio.run(main())
