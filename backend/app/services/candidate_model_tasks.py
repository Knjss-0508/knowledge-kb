from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import func, or_, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import SessionLocal
from app.models.integration import CandidateModelAutomation, CandidateModelTask, IntegrationIngestion
from app.models.user import User
from app.schemas.integration import CandidateReviewBatchModelAnnotate


logger = logging.getLogger(__name__)
ACTIVE_STATUSES = ("queued", "running")
SERVICE_ERROR_CODES = {"NETWORK_ERROR", "TIMEOUT", "HTTP_429"}
PERMANENT_SERVICE_ERRORS = {"CONFIG_MISSING", "HTTP_401", "HTTP_403"}


def _record_service_outcome(config: CandidateModelAutomation, outcome: dict | None) -> None:
    if not outcome:
        return
    entries = outcome.get("results", [])
    errors = [
        entry for entry in entries
        if entry.get("error_code") in SERVICE_ERROR_CODES | PERMANENT_SERVICE_ERRORS
        or str(entry.get("error_code") or "").startswith("HTTP_5")
    ]
    if errors:
        config.consecutive_network_failures = (config.consecutive_network_failures or 0) + 1
        if (config.consecutive_network_failures >= 2
                or errors[0].get("error_code") in PERMANENT_SERVICE_ERRORS):
            config.paused_at = datetime.utcnow()
            config.pause_reason = (
                f"模型服务不可用（{errors[0]['error_code']}），已暂停全部候选模型任务。"
                "请确认网关连接恢复后点击“恢复处理”；未处理候选保持原进度。"
            )
    elif any(entry.get("status") != "skipped" for entry in entries):
        config.consecutive_network_failures = 0


def automation_settings(db: Session, *, lock: bool = False) -> CandidateModelAutomation:
    query = db.query(CandidateModelAutomation).filter_by(id="default")
    return (query.with_for_update() if lock else query).one()


def task_snapshot(task: CandidateModelTask, *, include_results: bool = True) -> dict:
    results = list(task.results or [])
    counters = {
        key: sum(int(row.get(key) or 0) for row in results)
        for key in ("updated", "failed", "revisions_applied", "reannotated",
                    "submitted", "reused", "submission_failed")
    }
    return {
        "id": task.id, "status": task.status, "source": task.source,
        "total": len(task.ingestion_ids), "completed": len(results),
        **counters,
        "error_message": task.error_message,
        "created_at": task.created_at, "updated_at": task.updated_at,
        "results": [entry for row in results for entry in row.get("results", [])] if include_results else [],
    }


def enqueue_task(
    db: Session, ids: list[str], user_id: str, *, source: str = "manual",
) -> CandidateModelTask | None:
    automation_settings(db, lock=True)
    active = db.query(CandidateModelTask).filter(
        CandidateModelTask.status.in_(ACTIVE_STATUSES)
    ).all()
    busy = {value for job in active for value in job.ingestion_ids}
    selected = [value for value in dict.fromkeys(ids) if value not in busy]
    if not selected:
        return None
    task = CandidateModelTask(
        id="cmt-" + uuid.uuid4().hex, status="queued", source=source,
        requested_by=user_id, ingestion_ids=selected, results=[], error_message="",
    )
    db.add(task)
    db.flush()
    return task


def schedule_automatic_candidates(db: Session) -> CandidateModelTask | None:
    from app.routes.auth import has_permission

    config = automation_settings(db, lock=True)
    if not config.enabled or config.paused_at:
        return None
    user = db.get(User, config.requested_by)
    if not user or not user.is_active or not has_permission(user, "knowledge:submit"):
        config.enabled = False
        return None
    if db.query(CandidateModelTask.id).filter(
        CandidateModelTask.status.in_(ACTIVE_STATUSES)
    ).first():
        return None
    metadata = IntegrationIngestion.review_metadata
    processed_version = metadata["automatic_model_workflow"]["draft_version"].as_integer()
    draft_version = func.coalesce(metadata["draft_version"].as_integer(), 1)
    rows = db.query(IntegrationIngestion).filter(
        IntegrationIngestion.review_status.in_([
            "pending", "ready", "pending_model_annotation", "revision_required",
        ]),
        IntegrationIngestion.source_system != "excel",
        IntegrationIngestion.knowledge_id.is_(None),
        IntegrationIngestion.submitted_at.is_(None),
        IntegrationIngestion.reviewed_at.is_(None),
        or_(processed_version.is_(None), processed_version != draft_version),
    ).order_by(IntegrationIngestion.created_at, IntegrationIngestion.id).limit(100).all()
    if not rows:
        return None
    return enqueue_task(db, [row.id for row in rows], user.id, source="automatic")


def _mark_model_workflow_attempt(item: IntegrationIngestion, task_id: str | None) -> None:
    from app.services.candidate_review import current_draft_version

    item.review_metadata = {
        **dict(item.review_metadata or {}),
        "automatic_model_workflow": {
            "task_id": task_id,
            "draft_version": current_draft_version(item.review_metadata or {}),
        },
    }


def _model_workflow_attempted_current_draft(item: IntegrationIngestion) -> bool:
    from app.services.candidate_review import current_draft_version

    recorded = (item.review_metadata or {}).get("automatic_model_workflow") or {}
    try:
        return int(recorded.get("draft_version")) == current_draft_version(item.review_metadata or {})
    except (TypeError, ValueError):
        return False


@contextmanager
def candidate_model_execution_slot(engine: Engine) -> Iterator[bool]:
    """Share the candidate execution lock across worker and legacy HTTP callers."""
    if engine.dialect.name != "postgresql":
        yield True
        return
    with engine.connect() as connection:
        locked = bool(connection.execute(text("SELECT pg_try_advisory_lock(735216, 20261027)")).scalar())
        connection.commit()
        if not locked:
            yield False
            return
        try:
            yield True
        finally:
            connection.execute(text("SELECT pg_advisory_unlock(735216, 20261027)"))
            connection.commit()


def process_next_candidate_model_task(session_factory: sessionmaker = SessionLocal) -> bool:
    # A session-level PostgreSQL lock survives business commits. Expired leases
    # alone must never let a second process overtake a slow but living model call.
    with session_factory() as guard:
        engine = guard.get_bind()
    with candidate_model_execution_slot(engine) as locked:
        if not locked:
            return False
        return _process_next_candidate_model_task(session_factory)


def _process_next_candidate_model_task(session_factory: sessionmaker) -> bool:
    """One candidate per transaction checkpoint; a lease resumes interrupted tasks."""
    from app.routes.auth import has_permission
    from app.routes.integration import _candidate_is_locked, model_annotate_candidate_reviews
    from app.services.candidate_review import normalize_human_review

    now = datetime.utcnow()
    with session_factory() as db:
        config = automation_settings(db, lock=True)
        if config.paused_at:
            return False
        # Serialize model calls across application workers to keep service load bounded.
        if db.query(CandidateModelTask.id).filter(
            CandidateModelTask.status == "running",
            CandidateModelTask.lease_expires_at > now,
        ).first():
            return False
        task = db.query(CandidateModelTask).filter(
            CandidateModelTask.status.in_(ACTIVE_STATUSES)
        ).order_by(CandidateModelTask.created_at, CandidateModelTask.id).with_for_update().first()
        if not task:
            task = schedule_automatic_candidates(db)
        if not task:
            db.commit()
            return False
        if task.source == "automatic" and not config.enabled:
            task.status = "cancelled"
            task.lease_token = None
            task.lease_expires_at = None
            db.commit()
            return True
        token = uuid.uuid4().hex
        task.status = "running"
        task.lease_token = token
        task.lease_expires_at = now + timedelta(minutes=30)
        task_id, user_id, source = task.id, task.requested_by, task.source
        index = len(task.results or [])
        ingestion_id = task.ingestion_ids[index] if index < len(task.ingestion_ids) else None
        interrupted = bool(task.current_ingestion_id)
        task.current_ingestion_id = ingestion_id
        db.commit()

    with session_factory() as db:
        try:
            user = db.get(User, user_id)
            if not user or not user.is_active or not has_permission(user, "knowledge:submit"):
                raise PermissionError("任务发起人的提交权限已失效。")
            item = None
            skip = False
            if ingestion_id is None:
                outcome = None
            else:
                item = db.get(IntegrationIngestion, ingestion_id)
                human = normalize_human_review((item.candidate_payload or {}).get("human_review")) if item else {}
                skip = item and (
                    _candidate_is_locked(item) or (
                        source == "automatic" and (
                            item.reviewed_at or human.get("knowledge_value") in {"worthy", "unworthy"}
                            or _model_workflow_attempted_current_draft(item)
                        )
                    )
                )
                if skip:
                    outcome = {"results": [{
                        "ingestion_id": ingestion_id, "status": "skipped",
                        "review_status": item.review_status,
                        "error_message": "已送审、已有人工结论或当前草稿已处理，保留当前结果。",
                    }]}
                elif interrupted:
                    outcome = {"failed": 1, "results": [{
                        "ingestion_id": ingestion_id, "status": "failed",
                        "error_code": "MODEL_WORKFLOW_INTERRUPTED",
                        "error_message": "上次执行中断，已保留保存的草稿和初标；请检查当前内容后重试，避免重复采用修订。",
                    }]}
                else:
                    response = model_annotate_candidate_reviews(
                        CandidateReviewBatchModelAnnotate(
                            ingestion_ids=[ingestion_id],
                            apply_model_revisions=True, submit_ready=True,
                        ), db, user,
                    )
                    outcome = response.model_dump(mode="json")
                if item and not skip:
                    # A failed manual task must not be silently retried by the auto scan.
                    _mark_model_workflow_attempt(item, task_id)
            task = db.query(CandidateModelTask).filter_by(id=task_id, lease_token=token).with_for_update().one()
            if outcome is not None:
                task.results = [*list(task.results or []), outcome]
            _record_service_outcome(automation_settings(db, lock=True), outcome)
            task.status = "completed" if len(task.results) >= len(task.ingestion_ids) else "queued"
            task.lease_token = None
            task.lease_expires_at = None
            task.current_ingestion_id = None
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.exception("Candidate model task %s failed at %s", task_id, ingestion_id)
            task = db.query(CandidateModelTask).filter_by(id=task_id, lease_token=token).with_for_update().one_or_none()
            if task:
                if ingestion_id:
                    item = db.get(IntegrationIngestion, ingestion_id)
                    if item:
                        _mark_model_workflow_attempt(item, task_id)
                task.error_message = (
                    str(exc) if isinstance(exc, PermissionError)
                    else "后台处理异常，已保留此前进度；请查看失败明细并重试。"
                )[:512]
                task.results = [*list(task.results or []), {
                    "failed": 1, "results": [{
                        "ingestion_id": ingestion_id, "status": "failed",
                        "error_code": "BACKGROUND_MODEL_FAILED",
                        "error_message": task.error_message,
                    }],
                }]
                task.status = "failed" if isinstance(exc, PermissionError) else (
                    "completed" if len(task.results) >= len(task.ingestion_ids) else "queued"
                )
                task.lease_token = None
                task.lease_expires_at = None
                task.current_ingestion_id = None
                db.commit()
    return True


async def run_candidate_model_worker(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            processed = await asyncio.to_thread(process_next_candidate_model_task)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Candidate model worker iteration failed.")
            processed = False
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.05 if processed else 10)
        except TimeoutError:
            pass
