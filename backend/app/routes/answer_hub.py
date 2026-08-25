from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models.integration import IntegrationIngestion
from app.models.user import User
from app.routes.auth import require_permission
from app.schemas.answer_hub import AnswerHubControlUpdate
from app.services.answer_hub_operations import (
    AnswerHubGateway,
    AnswerHubGatewayError,
    answer_hub_error_message,
    build_answer_hub_overview,
)


router = APIRouter(prefix="/answer-hub", tags=["Answer Hub 自动化"])


def _gateway() -> AnswerHubGateway:
    return AnswerHubGateway(
        base_url=settings.ANSWER_HUB_BASE_URL,
        api_key=settings.ANSWER_HUB_API_KEY,
        timeout_seconds=settings.ANSWER_HUB_TIMEOUT_SECONDS,
    )


def _waiting_review_count(db: Session) -> int:
    return int(
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.review_status == "pending")
        .count()
    )


def _upstream_error(exc: AnswerHubGatewayError) -> HTTPException:
    return HTTPException(
        status_code=exc.status_code,
        detail=answer_hub_error_message(exc.kind),
    )


@router.get("/overview")
def get_answer_hub_overview(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
) -> dict[str, Any]:
    return build_answer_hub_overview(_gateway(), waiting_review=_waiting_review_count(db))


@router.patch("/control")
def update_answer_hub_control(
    payload: AnswerHubControlUpdate,
    user: User = Depends(require_permission("account:manage")),
) -> dict[str, Any]:
    patch = payload.model_dump(exclude_none=True)
    if not patch:
        raise HTTPException(status_code=400, detail="请至少修改一个控制项。")
    try:
        return _gateway().request("PATCH", "/api/v1/automation/control", patch)
    except AnswerHubGatewayError as exc:
        raise _upstream_error(exc) from exc


@router.post("/runs")
def start_answer_hub_run(
    user: User = Depends(require_permission("account:manage")),
) -> dict[str, Any]:
    try:
        return _gateway().request(
            "POST",
            "/api/v1/automation/runs",
            {"actor": user.username},
        )
    except AnswerHubGatewayError as exc:
        raise _upstream_error(exc) from exc


@router.post("/jobs/{job_id}/retry")
def retry_answer_hub_job(
    job_id: str,
    user: User = Depends(require_permission("account:manage")),
) -> dict[str, Any]:
    safe_job_id = quote(job_id, safe="")
    try:
        return _gateway().request(
            "POST",
            f"/api/v1/automation/jobs/{safe_job_id}/retry",
            {"actor": user.username},
        )
    except AnswerHubGatewayError as exc:
        raise _upstream_error(exc) from exc
