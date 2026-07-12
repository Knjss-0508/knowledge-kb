import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings


def require_integration_key(
    x_integration_key: str | None = Header(default=None, alias="X-Integration-Key"),
) -> None:
    if not settings.INTEGRATION_API_KEY:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Integration API is not configured.",
        )
    if not x_integration_key or not secrets.compare_digest(
        x_integration_key, settings.INTEGRATION_API_KEY
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid integration key.",
        )
