"""ADMIN_API_KEY check for control and approval endpoints (X-API-Key header)."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header

from app.config import Settings, get_settings
from app.core.errors import UnauthorizedError


def require_admin(
    x_api_key: str | None = Header(default=None, description="Required when ADMIN_API_KEY is set."),
    settings: Settings = Depends(get_settings),
) -> None:
    """No key configured (development only; production refuses to start without one) -> open."""
    expected = settings.admin_api_key
    if expected is None:
        return
    # Constant-time comparison; never log either value.
    if x_api_key is None or not hmac.compare_digest(x_api_key.encode(), expected.get_secret_value().encode()):
        raise UnauthorizedError("Missing or invalid X-API-Key header")
