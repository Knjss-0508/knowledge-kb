from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.core.config import settings
from app.models.user import User
from app.routes.auth import require_permission
from app.services.automation_monitor import (
    AutomationMonitorError,
    AutomationMonitorGateway,
    build_monitor_overview,
    monitor_error_detail,
    safe_record_id,
)


router = APIRouter(prefix="/automation-monitor", tags=["自动化运行监管"])


def _gateway() -> AutomationMonitorGateway:
    base_url = settings.ANSWER_HUB_API_BASE_URL or settings.ANSWER_HUB_BASE_URL
    timeout_seconds = (
        settings.ANSWER_HUB_API_TIMEOUT_SECONDS
        if settings.ANSWER_HUB_API_BASE_URL
        else settings.ANSWER_HUB_TIMEOUT_SECONDS
    )
    return AutomationMonitorGateway(
        base_url,
        settings.ANSWER_HUB_API_KEY,
        timeout_seconds,
    )


def _raise_gateway_error(exc: AutomationMonitorError) -> None:
    raise HTTPException(exc.status_code, monitor_error_detail(exc.kind, exc.detail)) from exc


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
    allowed = {
        "enabled", "schedule_enabled", "schedule_time", "timezone",
        "second_part_query_from_date", "second_part_query_to_date",
        "knowledge_settle_from_date", "knowledge_settle_to_date",
    }
    payload = {key: body[key] for key in allowed if key in body}
    if not payload:
        raise HTTPException(400, "请至少修改一个自动化控制项。")
    for key in ("enabled", "schedule_enabled"):
        if key in payload and not isinstance(payload[key], bool):
            raise HTTPException(400, f"{key} 必须是布尔值。")
    if "schedule_time" in payload:
        value = payload["schedule_time"]
        if not isinstance(value, str) or not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise HTTPException(400, "schedule_time 必须是 HH:MM 格式。")
    from_date = payload.get(
        "knowledge_settle_from_date",
        payload.get("second_part_query_from_date", ""),
    )
    to_date = payload.get(
        "knowledge_settle_to_date",
        payload.get("second_part_query_to_date", ""),
    )
    if bool(from_date) != bool(to_date):
        raise HTTPException(400, "第二部分采集开始日期和结束日期必须同时填写。")
    if from_date and (
        not isinstance(from_date, str)
        or not isinstance(to_date, str)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", from_date)
        or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", to_date)
        or from_date > to_date
    ):
        raise HTTPException(400, "第二部分采集日期范围无效，请使用 YYYY-MM-DD 且开始日期不晚于结束日期。")
    payload["knowledge_settle_from_date"] = from_date
    payload["knowledge_settle_to_date"] = to_date
    payload["second_part_query_from_date"] = from_date
    payload["second_part_query_to_date"] = to_date
    try:
        return _gateway().request("PATCH", "/api/v1/automation/control", payload)
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
