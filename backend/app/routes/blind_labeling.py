"""Conversation-level blind retrieval labeling APIs."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models.blind_labeling import (
    BlindLabelArbitration,
    BlindLabelAssignment,
    BlindLabelBatch,
    BlindLabelWorkOrder,
)
from app.models.user import User
from app.routes.auth import get_current_user, has_permission, require_permission
from app.schemas.blind_labeling import (
    BlindLabelArbitrationInput,
    BlindLabelArbitrationRequest,
    BlindLabelClaimRequest,
    BlindLabelReleaseRequest,
    BlindLabelSubmitRequest,
)
from app.services.blind_labeling import (
    BLIND_LABEL_BATCH_SIZE,
    assignment_summary,
    batch_summary,
    claim_batch,
    current_batch,
    mark_assignment_started,
    overview,
    private_work_order_detail,
    public_assignment_detail,
    release_assignment,
    submit_assignment,
)

router = APIRouter(prefix="/blind-labeling", tags=["召回盲标"])


def _admin_can_view(user: User) -> bool:
    return has_permission(user, "retrieval:label_overview")


def require_blind_annotator():
    """Allow support roles to label, but never auto-assign batches to admins."""

    def checker(user: User = Depends(get_current_user)) -> User:
        if getattr(user, "role", None) == "super_admin":
            raise HTTPException(403, "管理员请使用召回盲标总览，不参与个人批次标注")
        if not has_permission(user, "retrieval:blind_label"):
            raise HTTPException(403, "Permission denied.")
        return user

    return checker


def require_blind_label_release_access():
    """仅允许仲裁管理员在紧急情况下回收未完成的盲标任务。"""

    def checker(user: User = Depends(get_current_user)) -> User:
        if not has_permission(user, "retrieval:label_arbitrate"):
            raise HTTPException(403, "Permission denied.")
        return user

    return checker


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _resolve_date_range(
    start_at: datetime | None,
    end_at: datetime | None,
    start_date: date | None,
    end_date: date | None,
) -> tuple[datetime | None, datetime | None]:
    start_at = _normalize_datetime(start_at)
    end_at = _normalize_datetime(end_at)
    if start_at is None and start_date is not None:
        start_at = datetime.combine(start_date, time.min)
    if end_at is None and end_date is not None:
        # Treat the selected end date as inclusive by using the next midnight
        # as an exclusive upper bound.
        end_at = datetime.combine(end_date + timedelta(days=1), time.min)
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HTTPException(status_code=422, detail="开始时间必须早于结束时间")
    return start_at, end_at


def _get_assignment(db: Session, assignment_id: str) -> BlindLabelAssignment:
    assignment = (
        db.query(BlindLabelAssignment)
        .filter(BlindLabelAssignment.id == assignment_id)
        .first()
    )
    if not assignment:
        raise HTTPException(status_code=404, detail="盲标任务不存在")
    return assignment


def _get_work_order(db: Session, identifier: str) -> BlindLabelWorkOrder | None:
    """Resolve either the internal work-order ID or the source conversation ID."""

    identifier = str(identifier or "").strip()
    if not identifier:
        return None
    return (
        db.query(BlindLabelWorkOrder)
        .filter(
            (BlindLabelWorkOrder.id == identifier)
            | (BlindLabelWorkOrder.conversation_id == identifier)
        )
        .first()
    )


def _claim_response(db: Session, user_id: str):
    try:
        batch, assignments = claim_batch(db, user_id)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return {
        "batch": batch_summary(db, batch),
        "items": [assignment_summary(db, item) for item in assignments],
        "assignments": [assignment_summary(db, item) for item in assignments],
        "total": len(assignments),
    }


def _require_own_or_admin(current_user: User, assignment: BlindLabelAssignment) -> None:
    if assignment.user_id != current_user.id and not _admin_can_view(current_user):
        raise HTTPException(status_code=403, detail="只能访问自己被分配的盲标任务")


@router.get("/my-batch")
def get_my_batch(
    target_count: int = Query(
        BLIND_LABEL_BATCH_SIZE,
        ge=BLIND_LABEL_BATCH_SIZE,
        le=BLIND_LABEL_BATCH_SIZE,
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_annotator()),
):
    """Read the current batch without silently claiming the next one.

    A completed batch is returned as context with no active items.  The client
    must call the explicit ``POST /my-batch:claim`` command when the annotator
    chooses to receive another 50-item batch.
    """

    batch, assignments = current_batch(db, current_user.id)
    return {
        "batch": batch_summary(db, batch) if batch else None,
        "items": [assignment_summary(db, item) for item in assignments],
        "assignments": [assignment_summary(db, item) for item in assignments],
        "total": len(assignments),
    }


@router.post("/my-batch:claim")
def claim_my_batch(
    body: BlindLabelClaimRequest | None = None,
    target_count: int | None = Query(
        None,
        ge=BLIND_LABEL_BATCH_SIZE,
        le=BLIND_LABEL_BATCH_SIZE,
    ),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_annotator()),
):
    """POST compatibility form for clients that model claiming as a command."""

    return _claim_response(db, current_user.id)


@router.get("/my-assignments")
def list_my_assignments(
    batch_id: str | None = Query(None),
    assignment_status: str | None = Query(None, alias="status"),
    include_released: bool = Query(False),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_annotator()),
):
    query = db.query(BlindLabelAssignment).filter(
        BlindLabelAssignment.user_id == current_user.id
    )
    if batch_id:
        query = query.filter(BlindLabelAssignment.batch_id == batch_id)
    if assignment_status:
        query = query.filter(BlindLabelAssignment.status == assignment_status.strip())
    elif not include_released:
        query = query.filter(BlindLabelAssignment.status != "released")
    assignments = query.order_by(
        BlindLabelAssignment.assigned_at.desc(), BlindLabelAssignment.id.desc()
    ).all()
    total = len(assignments)
    start = (page - 1) * page_size
    batch = (
        db.query(BlindLabelBatch)
        .filter(BlindLabelBatch.user_id == current_user.id)
        .order_by(BlindLabelBatch.created_at.desc(), BlindLabelBatch.id.desc())
        .first()
    )
    return {
        "batch": batch_summary(db, batch) if batch else None,
        "items": [assignment_summary(db, item) for item in assignments[start : start + page_size]],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/assignments/{assignment_id}")
def get_assignment_detail(
    assignment_id: str,
    conversation_id: str | None = Query(None, alias="conversationId"),
    work_order_id: str | None = Query(None, alias="workOrderId"),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("retrieval:blind_label")),
):
    assignment = (
        db.query(BlindLabelAssignment)
        .filter(BlindLabelAssignment.id == assignment_id)
        .first()
    )
    if assignment is None:
        resolved_work_order = _get_work_order(
            db,
            work_order_id or conversation_id or assignment_id,
        )
        if resolved_work_order is not None:
            assignment = (
                db.query(BlindLabelAssignment)
                .filter(BlindLabelAssignment.work_order_id == resolved_work_order.id)
                .filter(
                    BlindLabelAssignment.user_id == current_user.id
                    if not _admin_can_view(current_user)
                    else True
                )
                .order_by(BlindLabelAssignment.assigned_at.desc())
                .first()
            )
            if assignment is None and _admin_can_view(current_user):
                return private_work_order_detail(db, resolved_work_order)
    if assignment is None:
        raise HTTPException(status_code=404, detail="盲标任务不存在")
    _require_own_or_admin(current_user, assignment)
    if assignment.user_id == current_user.id:
        try:
            assignment = mark_assignment_started(db, assignment)
            db.commit()
        except ValueError as exc:
            db.rollback()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
    if _admin_can_view(current_user):
        return private_work_order_detail(db, assignment.work_order)
    return public_assignment_detail(db, assignment)


@router.post("/assignments/{assignment_id}/submit", status_code=status.HTTP_201_CREATED)
def submit_assignment_labels(
    assignment_id: str,
    body: BlindLabelSubmitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_annotator()),
):
    assignment = _get_assignment(db, assignment_id)
    if assignment.user_id != current_user.id:
        raise HTTPException(status_code=403, detail="只能提交自己的盲标任务")
    try:
        annotations = submit_assignment(
            db,
            assignment,
            [item.model_dump(by_alias=False) for item in body.annotations],
            note=body.note,
            task_reason_code=body.task_reason_code,
        )
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception:
        db.rollback()
        raise
    db.refresh(assignment)
    return {
        "status": "recorded",
        "assignment_id": assignment.id,
        "batch": batch_summary(db, assignment.batch),
        "annotation_count": len(annotations),
    }


@router.post("/submit", status_code=status.HTTP_201_CREATED)
def submit_assignment_labels_compat(
    body: BlindLabelSubmitRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_annotator()),
):
    if not body.assignment_id:
        raise HTTPException(status_code=422, detail="assignmentId is required")
    return submit_assignment_labels(body.assignment_id, body, db, current_user)


@router.post("/assignments/{assignment_id}/release")
def release_my_assignment(
    assignment_id: str,
    body: BlindLabelReleaseRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_label_release_access()),
):
    if not has_permission(current_user, "retrieval:label_arbitrate"):
        raise HTTPException(status_code=403, detail="仅管理员可应急回收未完成盲标任务")
    assignment = _get_assignment(db, assignment_id)
    try:
        release_assignment(db, assignment, body.reason if body else "")
        db.commit()
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"status": "released", "assignment_id": assignment.id}


@router.post("/release")
def release_assignment_compat(
    assignment_id: str | None = Query(None, alias="assignmentId"),
    body: BlindLabelReleaseRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_blind_label_release_access()),
):
    assignment_id = assignment_id or (body.assignment_id if body else None)
    if not assignment_id:
        raise HTTPException(status_code=422, detail="assignmentId is required")
    return release_my_assignment(assignment_id, body, db, current_user)


@router.get("/overview")
def get_blind_label_overview(
    start_at: datetime | None = Query(None),
    end_at: datetime | None = Query(None),
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    date_from: date | None = Query(None, alias="date_from"),
    date_to: date | None = Query(None, alias="date_to"),
    status_filter: str | None = Query(None, alias="status"),
    dimension: str | None = Query(None),
    verdict: str | None = Query(None),
    user_id: str | None = Query(None),
    annotator_id: str | None = Query(None, alias="annotator_id"),
    batch_id: str | None = Query(None),
    conversation_id: str | None = Query(None),
    work_order_id: str | None = Query(None, alias="work_order_id"),
    category: str | None = Query(None),
    keyword: str | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("retrieval:label_overview")),
):
    start_at, end_at = _resolve_date_range(
        start_at,
        end_at,
        start_date or date_from,
        end_date or date_to,
    )
    return overview(
        db,
        start_at=start_at,
        end_at=end_at,
        status_filter=status_filter,
        verdict=dimension or verdict,
        dimension=dimension or verdict,
        user_id=user_id or annotator_id,
        batch_id=batch_id,
        conversation_id=conversation_id or work_order_id,
        category=category,
        keyword=keyword,
        page=page,
        page_size=page_size,
    )


@router.get("/work-orders/{work_order_id}")
def get_work_order_detail(
    work_order_id: str,
    conversation_id: str | None = Query(None, alias="conversationId"),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("retrieval:label_overview")),
):
    work_order = _get_work_order(db, conversation_id or work_order_id)
    if not work_order:
        raise HTTPException(status_code=404, detail="盲标工单不存在")
    return private_work_order_detail(db, work_order)


@router.post("/work-orders/{work_order_id}/arbitrate")
def arbitrate_work_order(
    work_order_id: str,
    body: BlindLabelArbitrationRequest | None = None,
    candidate_ref: str | None = Query(None, alias="candidateRef"),
    verdict: str | None = Query(None),
    reason: str = Query(""),
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("retrieval:label_arbitrate")),
):
    """Persist administrator arbitration separately from retrieval telemetry."""

    work_order = _get_work_order(db, work_order_id)
    if not work_order:
        raise HTTPException(status_code=404, detail="盲标工单不存在")

    decisions = list(body.decisions) if body and body.decisions else []
    if not decisions and body and body.candidate_ref and body.verdict:
        decisions = [
            BlindLabelArbitrationInput(
                candidateRef=body.candidate_ref,
                verdict=body.verdict,
                reason=body.reason,
            )
        ]
    if not decisions and candidate_ref and verdict:
        try:
            decisions = [
                BlindLabelArbitrationInput(
                    candidateRef=candidate_ref,
                    verdict=verdict,
                    reason=reason,
                )
            ]
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
    if not decisions:
        raise HTTPException(status_code=422, detail="至少提供一个候选项的仲裁结论")
    valid_refs = {
        str(item.get("candidate_ref") or "")
        for item in (work_order.candidate_snapshot or [])
        if isinstance(item, dict)
    }
    seen_refs: set[str] = set()
    recorded: list[dict[str, str]] = []
    for decision in decisions:
        ref = str(decision.candidate_ref).strip()
        if ref not in valid_refs:
            raise HTTPException(status_code=422, detail="仲裁候选项不属于当前工单")
        if ref in seen_refs:
            raise HTTPException(status_code=422, detail="同一候选项不能重复仲裁")
        seen_refs.add(ref)
        existing = (
            db.query(BlindLabelArbitration)
            .filter(
                BlindLabelArbitration.work_order_id == work_order.id,
                BlindLabelArbitration.candidate_ref == ref,
            )
            .with_for_update()
            .first()
        )
        if existing is None:
            existing = BlindLabelArbitration(
                id=f"arb-{uuid.uuid4().hex[:24]}",
                work_order_id=work_order.id,
                candidate_ref=ref,
            )
            db.add(existing)
        existing.verdict = decision.verdict
        existing.reason = decision.reason.strip()
        existing.arbitrated_by = current_user.username
        existing.updated_at = datetime.utcnow()
        recorded.append(
            {
                "candidate_ref": ref,
                "verdict": decision.verdict,
            }
        )
    db.commit()
    return {
        "status": "accepted",
        "work_order_id": work_order.id,
        "decisions": recorded,
        "arbitrated_by": current_user.username,
        "persisted": True,
    }
