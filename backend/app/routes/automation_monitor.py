from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings
from app.models.user import User
from app.routes.auth import require_permission
from app.services.automation_monitor import (
    AutomationMonitorError,
    AutomationMonitorGateway,
    build_monitor_overview,
    monitor_error_message,
    safe_record_id,
)


router = APIRouter(prefix="/automation-monitor", tags=["自动化运行监管"])


def _gateway() -> AutomationMonitorGateway:
    return AutomationMonitorGateway(
        settings.ANSWER_HUB_BASE_URL,
        settings.ANSWER_HUB_API_KEY,
        settings.ANSWER_HUB_TIMEOUT_SECONDS,
    )


def _raise_gateway_error(exc: AutomationMonitorError) -> None:
    raise HTTPException(exc.status_code, monitor_error_message(exc.kind)) from exc


@router.get("/overview")
def get_overview(
    limit: int = Query(100, ge=1, le=100),
    _: User = Depends(require_permission("knowledge:submit")),
) -> dict[str, Any]:
    return build_monitor_overview(_gateway(), limit=limit)


@router.patch("/control")
def update_control(
    body: dict[str, Any],
    _: User = Depends(require_permission("account:manage")),
) -> dict[str, Any]:
    if not isinstance(body.get("enabled"), bool):
        raise HTTPException(400, "enabled 必须是布尔值。")
    try:
        return _gateway().request("PATCH", "/api/v1/automation/control", {"enabled": body["enabled"]})
    except AutomationMonitorError as exc:
        _raise_gateway_error(exc)

@router.post("/runs")
def run_once(_: User = Depends(require_permission("account:manage"))) -> dict[str, Any]:
    try:
        return _gateway().request("POST", "/api/v1/automation/runs")
    except AutomationMonitorError as exc:
        _raise_gateway_error(exc)


@router.post("/retry-failed")
def retry_failed(_: User = Depends(require_permission("account:manage"))) -> dict[str, Any]:
    try:
        return _gateway().request("POST", "/api/v1/automation/retry-failed")
    except AutomationMonitorError as exc:
        _raise_gateway_error(exc)


@router.get("/logs/latest")
def latest_logs(
    lines: int = Query(120, ge=1, le=200),
    _: User = Depends(require_permission("knowledge:submit")),
) -> dict[str, Any]:
    try:
        return _gateway().request("GET", f"/api/v1/automation/logs/latest?lines={lines}")
    except AutomationMonitorError as exc:
        _raise_gateway_error(exc)


@router.patch("/jobs/{record_id}/feedback")
def save_feedback(
    record_id: str,
    body: dict[str, Any],
    current_user: User = Depends(require_permission("knowledge:submit")),
) -> dict[str, Any]:
    allowed = {"status", "owner", "cause_type", "note"}
    payload = {key: value for key, value in body.items() if key in allowed}
    payload["actor"] = current_user.username
    try:
        return _gateway().request(
            "PATCH", f"/api/v1/automation/jobs/{safe_record_id(record_id)}/feedback", payload
        )
    except AutomationMonitorError as exc:
        _raise_gateway_error(exc)
