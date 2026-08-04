import secrets

from fastapi import Header, HTTPException, status

from app.core.config import settings


def _require_configured_key(
    provided_key: str | None,
    configured_key: str,
    *,
    not_configured_detail: str,
) -> None:
    if not configured_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=not_configured_detail,
        )
    if not provided_key or not secrets.compare_digest(provided_key, configured_key):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid integration key.",
        )


def require_integration_key(
    x_integration_key: str | None = Header(default=None, alias="X-Integration-Key"),
) -> None:
    _require_configured_key(
        x_integration_key,
        settings.INTEGRATION_API_KEY,
        not_configured_detail="Integration API is not configured.",
    )


def require_retrieval_key(
    x_integration_key: str | None = Header(default=None, alias="X-Integration-Key"),
) -> None:
    _require_configured_key(
        x_integration_key,
        settings.RETRIEVAL_API_KEY,
        not_configured_detail="Retrieval API is not configured.",
    )
