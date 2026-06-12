"""Cliente MetaSuite (dashboard de Meta Ads de DTGP).

Base: settings.metasuite_base_url (https://metasuite.dtgrowthpartners.com/api)
Algunos deploys configuran el token en backend (no requiere header); si tu
deploy lo pide, se manda como Bearer. Se REUSA tal cual.

Date presets: maximum (default), today, yesterday, last_7d, last_14d, last_30d,
this_month, last_month.
"""

from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.logging_setup import log

settings = get_settings()

_TIMEOUT = 25


def _headers() -> dict[str, str]:
    h = {"Content-Type": "application/json"}
    if settings.metasuite_token:
        h["Authorization"] = f"Bearer {settings.metasuite_token}"
    return h


async def _get(path: str, params: dict | None = None) -> dict[str, Any]:
    url = f"{settings.metasuite_base_url.rstrip('/')}/{path.lstrip('/')}"
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as c:
            r = await c.get(url, headers=_headers(), params=params)
        if r.status_code >= 400:
            log.warning("metasuite.http_error", path=path, status=r.status_code, body=r.text[:300])
            return {"ok": False, "error": f"MetaSuite {path} → {r.status_code}"}
        return {"ok": True, "data": r.json()}
    except Exception as e:
        log.exception("metasuite.fail", path=path, error=str(e))
        return {"ok": False, "error": str(e)[:200]}


async def health() -> dict[str, Any]:
    return await _get("/health")


async def ad_accounts() -> dict[str, Any]:
    return await _get("/ad-accounts")


async def campañas(account_id: str, date_preset: str = "last_30d") -> dict[str, Any]:
    return await _get(f"/campaigns/{account_id}", params={"date_preset": date_preset})


async def dashboard(resumen: bool = False) -> dict[str, Any]:
    return await _get("/dashboard/summary" if resumen else "/dashboard")
