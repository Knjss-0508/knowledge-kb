"""Editable internal model connection settings for the confidence pipeline.

The DeepSeek-flash route used to be configurable only through ``DEEPSEEK_*``
environment variables, which forces an .env edit plus a container restart.
This module keeps an optional override record in the existing
``confidence_training_settings`` table so an operator can change the address,
model name, API key and timeout from the confidence-training page and have the
next model call use it immediately.

Safety rules:
* the override record uses its own primary key, so the training switches can
  never overwrite the model route and vice versa;
* every field falls back to the environment variable when the override is
  empty, so a half-filled form can never point the pipeline at nothing;
* the API key is never returned to a client or written to a log — only a
  masked form and a boolean "configured" flag leave this module.
"""

from __future__ import annotations

import threading
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.integration import ConfidenceTrainingSettingsRecord

MODEL_CONNECTION_RECORD_ID = "confidence-model-connection"
DEFAULT_MODEL_TIMEOUT_SECONDS = 60.0
MIN_MODEL_TIMEOUT_SECONDS = 1.0
MAX_MODEL_TIMEOUT_SECONDS = 600.0
MAX_BASE_URL_LENGTH = 500
MAX_MODEL_NAME_LENGTH = 128
MAX_API_KEY_LENGTH = 500
MASK_HEAD = 4
MASK_TAIL = 4


class ModelConnectionError(ValueError):
    """A rejected model-connection configuration change."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalize_base_url(value: Any) -> str:
    return _text(value).rstrip("/")


def _normalize_timeout(value: Any) -> float | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        timeout = float(value)
    except (TypeError, ValueError) as exc:
        raise ModelConnectionError(
            "MODEL_TIMEOUT_INVALID",
            "超时时间必须是 1-600 之间的秒数。",
        ) from exc
    if not MIN_MODEL_TIMEOUT_SECONDS <= timeout <= MAX_MODEL_TIMEOUT_SECONDS:
        raise ModelConnectionError(
            "MODEL_TIMEOUT_OUT_OF_RANGE",
            f"超时时间必须在 {MIN_MODEL_TIMEOUT_SECONDS:.0f}-{MAX_MODEL_TIMEOUT_SECONDS:.0f} 秒之间。",
        )
    return timeout


def mask_api_key(api_key: Any) -> str:
    """Return a display-only form of the key. Never returns the full key."""
    text = _text(api_key)
    if not text:
        return ""
    if len(text) <= MASK_HEAD + MASK_TAIL:
        return "•" * len(text)
    return f"{text[:MASK_HEAD]}{'•' * 6}{text[-MASK_TAIL:]}"


def environment_model_config() -> dict[str, Any]:
    """The route defined by the deployment environment variables."""
    base_url = _normalize_base_url(settings.DEEPSEEK_BASE_URL) or _normalize_base_url(
        settings.GROUP_LLM_BASE_URL
    )
    api_key = _text(settings.DEEPSEEK_API_KEY) or _text(settings.GROUP_LLM_API_KEY)
    model = _text(settings.DEEPSEEK_MODEL) or "deepseek-flash"
    try:
        timeout = _normalize_timeout(settings.DEEPSEEK_TIMEOUT_SECONDS)
    except ModelConnectionError:
        timeout = None
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "timeout_seconds": timeout or DEFAULT_MODEL_TIMEOUT_SECONDS,
    }


def merge_model_config(
    overrides: dict[str, Any] | None,
    base: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Layer stored/request overrides on top of a base configuration."""
    resolved = dict(base or environment_model_config())
    values = overrides or {}
    for key in ("base_url", "api_key", "model"):
        value = _text(values.get(key))
        if not value:
            continue
        resolved[key] = _normalize_base_url(value) if key == "base_url" else value
    timeout = _normalize_timeout(values.get("timeout_seconds"))
    if timeout is not None:
        resolved["timeout_seconds"] = timeout
    return resolved


def _stored_overrides(db: Session) -> dict[str, Any]:
    """Read the override record, tolerating a pre-migration or down database."""
    try:
        record = (
            db.query(ConfidenceTrainingSettingsRecord)
            .filter(ConfidenceTrainingSettingsRecord.id == MODEL_CONNECTION_RECORD_ID)
            .first()
        )
    except SQLAlchemyError:
        db.rollback()
        return {}
    return dict(record.settings or {}) if record else {}


# Model calls happen outside any request scope (background workers, shadow
# reruns), so the effective override is cached in-process.  The cache is filled
# at startup and refreshed by every successful PATCH; a process that never
# loaded it falls back to the environment variables.
_CACHE_LOCK = threading.Lock()
_CACHED_OVERRIDES: dict[str, Any] | None = None


def cache_model_config(overrides: dict[str, Any] | None) -> None:
    global _CACHED_OVERRIDES
    with _CACHE_LOCK:
        _CACHED_OVERRIDES = dict(overrides) if overrides else None


def cached_model_config() -> dict[str, Any] | None:
    with _CACHE_LOCK:
        return dict(_CACHED_OVERRIDES) if _CACHED_OVERRIDES else None


def resolve_model_config() -> dict[str, Any]:
    """Effective route for model callers that have no database session."""
    return merge_model_config(cached_model_config(), environment_model_config())


def refresh_model_config_cache(session_factory=None) -> dict[str, Any]:
    """Reload the cached override from the database (startup / after a save)."""
    if session_factory is None:
        from app.core.database import SessionLocal

        session_factory = SessionLocal
    db = session_factory()
    try:
        overrides = _stored_overrides(db)
    finally:
        db.close()
    cache_model_config(overrides)
    return resolve_model_config()


def model_config_snapshot(db: Session | None = None) -> dict[str, Any]:
    """Client-safe view of the effective model route (API key masked)."""
    stored: dict[str, Any] = {}
    updated_by = ""
    updated_at: datetime | None = None
    if db is not None:
        try:
            record = (
                db.query(ConfidenceTrainingSettingsRecord)
                .filter(ConfidenceTrainingSettingsRecord.id == MODEL_CONNECTION_RECORD_ID)
                .first()
            )
        except SQLAlchemyError:
            db.rollback()
            record = None
        if record is not None:
            stored = dict(record.settings or {})
            updated_by = record.updated_by or ""
            updated_at = record.updated_at
    else:
        stored = cached_model_config() or {}
    env = environment_model_config()
    effective = merge_model_config(stored, env)
    stored_key = _text(stored.get("api_key"))
    if stored_key:
        api_key_source = "database"
    elif env["api_key"]:
        api_key_source = "environment"
    else:
        api_key_source = "missing"
    return {
        "configured": bool(effective["base_url"] and effective["api_key"]),
        "source": "database" if stored else "environment",
        "base_url": effective["base_url"],
        "model": effective["model"],
        "timeout_seconds": effective["timeout_seconds"],
        "api_key_configured": bool(effective["api_key"]),
        "api_key_masked": mask_api_key(effective["api_key"]),
        "api_key_source": api_key_source,
        "overrides": {
            "base_url": bool(_normalize_base_url(stored.get("base_url"))),
            "model": bool(_text(stored.get("model"))),
            "timeout_seconds": stored.get("timeout_seconds") not in (None, ""),
            "api_key": bool(stored_key),
        },
        "environment": {
            "base_url": env["base_url"],
            "model": env["model"],
            "timeout_seconds": env["timeout_seconds"],
            "api_key_configured": bool(env["api_key"]),
        },
        "prompt_version": _text(settings.DEEPSEEK_PROMPT_VERSION),
        "updated_by": updated_by,
        "updated_at": updated_at.isoformat() if isinstance(updated_at, datetime) else None,
    }


def update_model_config(
    db: Session,
    values: dict[str, Any],
    *,
    updated_by: str,
) -> dict[str, Any]:
    """Merge a client request into the override record and apply it at once."""
    record = (
        db.query(ConfidenceTrainingSettingsRecord)
        .filter(ConfidenceTrainingSettingsRecord.id == MODEL_CONNECTION_RECORD_ID)
        .first()
    )
    stored = dict(record.settings or {}) if record is not None else {}
    if values.get("reset_to_environment"):
        stored = {}
    else:
        for key in ("base_url", "model"):
            if key not in values or values[key] is None:
                continue
            text = _normalize_base_url(values[key]) if key == "base_url" else _text(values[key])
            if not text:
                stored.pop(key, None)
                continue
            if key == "base_url":
                if len(text) > MAX_BASE_URL_LENGTH:
                    raise ModelConnectionError("MODEL_BASE_URL_TOO_LONG", "模型地址过长。")
                if not text.startswith(("http://", "https://")):
                    raise ModelConnectionError(
                        "MODEL_BASE_URL_INVALID",
                        "模型地址必须以 http:// 或 https:// 开头。",
                    )
            elif len(text) > MAX_MODEL_NAME_LENGTH:
                raise ModelConnectionError("MODEL_NAME_TOO_LONG", "模型名称过长。")
            stored[key] = text
        if values.get("timeout_seconds") is not None:
            timeout = _normalize_timeout(values["timeout_seconds"])
            if timeout is None:
                stored.pop("timeout_seconds", None)
            else:
                stored["timeout_seconds"] = timeout
        if values.get("clear_api_key"):
            stored.pop("api_key", None)
        else:
            api_key = _text(values.get("api_key"))
            if api_key:
                if len(api_key) > MAX_API_KEY_LENGTH:
                    raise ModelConnectionError("MODEL_API_KEY_TOO_LONG", "API Key 过长。")
                stored["api_key"] = api_key
    if record is None:
        record = ConfidenceTrainingSettingsRecord(
            id=MODEL_CONNECTION_RECORD_ID,
            settings=stored,
            updated_by=updated_by,
        )
        db.add(record)
    else:
        record.settings = stored
        record.updated_by = updated_by
    db.commit()
    db.refresh(record)
    cache_model_config(stored)
    return model_config_snapshot(db)


def model_config_for_test(
    db: Session | None,
    values: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Effective route for a connection test, optionally with unsaved values.

    An empty inline field falls back to the stored/environment value, so an
    operator can test a new address without re-typing the saved API key.
    """
    base = merge_model_config(
        _stored_overrides(db) if db is not None else cached_model_config(),
        environment_model_config(),
    )
    return merge_model_config(values, base)
