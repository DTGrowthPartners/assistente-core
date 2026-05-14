"""Autenticación simple usuario/password para el panel /admin."""

from __future__ import annotations

import secrets

from sqladmin.authentication import AuthenticationBackend
from starlette.requests import Request

from app.config import get_settings

settings = get_settings()


class AdminAuth(AuthenticationBackend):
    """Auth básica con un solo usuario/password definidos en .env."""

    async def login(self, request: Request) -> bool:
        form = await request.form()
        username = str(form.get("username", "")).strip()
        password = str(form.get("password", ""))

        # Comparación constant-time para evitar timing attacks
        user_ok = secrets.compare_digest(username, settings.admin_user)
        pass_ok = secrets.compare_digest(password, settings.admin_password)

        if user_ok and pass_ok:
            request.session.update({"admin_token": secrets.token_urlsafe(32)})
            return True
        return False

    async def logout(self, request: Request) -> bool:
        request.session.clear()
        return True

    async def authenticate(self, request: Request) -> bool:
        return "admin_token" in request.session
