"""Business logic for conversation-level blind retrieval labeling.

This module deliberately does not call the legacy retrieval-review service.
Blind annotations are independent evidence and are only aggregated for the
blind-label overview/consensus endpoints.
"""

from __future__ import annotations

import copy
import math
import uuid
from collections import Counter, defaultdict
from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.blind_labeling import (
    BlindLabelAnnotation,
    BlindLabelArbitration,
    BlindLabelAssignment,
    BlindLabelBatch,
    BlindLabelWorkOrder,
)
from app.models.integration import RetrievalQualityEvent
from app.models.knowledge import Category, Knowledge

BLIND_LABEL_BATCH_SIZE = 50
BLIND_LABEL_MAX_ASSIGNMENTS = 3
BLIND_LABEL_MAX_CANDIDATES = 3
# 盲标只接收 TOP1 候选分数达到 0.60 的请求；分数统一使用 0~1 口径。
BLIND_LABEL_MIN_TOP1_SCORE = 0.60
BLIND_LABEL_VERDICTS = {"referable", "not_referable"}
BLIND_LABEL_ASSIGNMENT_STATUSES = {"assigned", "in_progress", "completed", "released"}
BLIND_LABEL_ACTIVE_STATUSES = {"assigned", "in_progress", "completed"}
BLIND_LABEL_AUTO_RELEASE_REASON = "系统超时自动回收"
# A verdict remains the only value used by consensus and arbitration.  These
# codes are structured diagnostic evidence for later retrieval/knowledge work.
BLIND_LABEL_REFERABLE_REASON_CODES = {
    "needs_conditions",
    "needs_merge",
    "other",
}
BLIND_LABEL_NOT_REFERABLE_REASON_CODES = {
    "topic_irrelevant",
    "content_incorrect",
    "content_outdated",
    "conditions_not_met",
    "other",
}
BLIND_LABEL_TASK_REASON_CODES = {
    "knowledge_exists_not_recalled",
    "knowledge_missing",
    "unable_to_judge",
}
_PRIVATE_CANDIDATE_KEYS = {
    "knowledge_id",
    "knowledge_origin",
    "source_kind",
    "selected",
    "embedding_score",
    "rerank_score",
    "final_score",
    "score_threshold",
    "top_knowledge_id",
    "selected_knowledge_id",
    "selected_candidate_rank",
    "candidate_ids",
}


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:24]}"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _safe_content(value: Any) -> Any:
    """Copy knowledge content while dropping internal identifiers/telemetry."""

    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).strip().lower()
            if normalized in _PRIVATE_CANDIDATE_KEYS:
                continue
            result[str(key)] = _safe_content(item)
        return result
    if isinstance(value, list):
        return [_safe_content(item) for item in value]
    return copy.deepcopy(value)


def _candidate_origins(event: RetrievalQualityEvent) -> list[str | None]:
    metadata = _as_dict(event.event_metadata)
    raw = metadata.get("candidate_origins")
    return raw if isinstance(raw, list) else []


def _numeric_score(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    score = float(value)
    if not math.isfinite(score) or not 0 <= score <= 1:
        return None
    return score


def _candidate_score(candidate: dict[str, Any]) -> float | None:
    for key in ("final_score", "rerank_score", "embedding_score"):
        score = _numeric_score(candidate.get(key))
        if score is not None:
            return score
    return None


def _top1_score(
    candidates: list[dict[str, Any]],
    fallback: Any = None,
) -> float | None:
    if candidates:
        score = _candidate_score(candidates[0])
        if score is not None:
            return score
    return _numeric_score(fallback)


def _top1_score_is_eligible(
    candidates: list[dict[str, Any]],
    fallback: Any = None,
) -> bool:
    score = _top1_score(candidates, fallback)
    return score is not None and score >= BLIND_LABEL_MIN_TOP1_SCORE


def _business_candidate_rows(event: RetrievalQualityEvent) -> list[dict[str, Any]]:
    """Return the event's business-accumulation candidates in rank order.

    ``reply`` events historically omitted per-candidate origins; in that case
    the event source itself is the business-accumulation pool.  Combined events
    are accepted only when an explicit candidate origin says business.
    """

    source_kind = str(event.source_kind or "").strip().lower()
    origins = _candidate_origins(event)
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    raw_snapshot = event.candidate_snapshot or []
    if not isinstance(raw_snapshot, list):
        return rows
    for index, raw in enumerate(raw_snapshot):
        if not isinstance(raw, dict):
            continue
        origin = str(raw.get("knowledge_origin") or "").strip()
        if not origin and index < len(origins):
            origin = str(origins[index] or "").strip()
        if origin != "business_accumulation":
            if not (source_kind == "reply" and not origin):
                continue
            origin = "business_accumulation"
        knowledge_id = str(raw.get("knowledge_id") or "").strip()
        if not knowledge_id or knowledge_id in seen_ids:
            continue
        seen_ids.add(knowledge_id)
        try:
            original_rank = int(raw.get("rank") or index + 1)
        except (TypeError, ValueError):
            original_rank = index + 1
        rows.append(
            {
                "knowledge_id": knowledge_id[:64],
                "rank": max(1, original_rank),
                "title": str(raw.get("title") or "")[:256],
                "embedding_score": raw.get("embedding_score"),
                "rerank_score": raw.get("rerank_score"),
                "final_score": raw.get("final_score"),
                "selected": bool(raw.get("selected")),
                "knowledge_origin": "business_accumulation",
            }
        )
        if len(rows) >= BLIND_LABEL_MAX_CANDIDATES:
            break
    rows.sort(key=lambda item: (int(item.get("rank") or 0), item["knowledge_id"]))
    # Re-number after filtering the headquarters pool out of a combined event.
    for rank, row in enumerate(rows, start=1):
        row["rank"] = rank
    return rows


def freeze_work_order_snapshot(db: Session, event: RetrievalQualityEvent) -> dict[str, Any] | None:
    """Build a private immutable snapshot from one business-pool event."""

    candidates = _business_candidate_rows(event)
    if not candidates:
        return None
    # 老事件可能只保存 event.top_rerank_score，没有逐候选 final_score；
    # 将该有效 TOP1 分数补回快照，确保后续分配阶段仍能执行同一门槛。
    if _candidate_score(candidates[0]) is None:
        # reply 事件本身就是业务沉淀池；combined 事件的 event 分数可能来自总部
        # 标准池，不能拿它冒充业务 TOP1 分数。
        fallback_score = (
            _numeric_score(event.top_rerank_score)
            if str(event.source_kind or "").strip().lower() == "reply"
            else None
        )
        if fallback_score is not None:
            candidates[0]["final_score"] = fallback_score
    knowledge_ids = [item["knowledge_id"] for item in candidates]
    knowledge_rows = {
        item.id: item
        for item in db.query(Knowledge).filter(Knowledge.id.in_(knowledge_ids)).all()
    }
    category_names: dict[str, str] = {}
    category_ids = {
        str(item.category_id)
        for item in knowledge_rows.values()
        if item.category_id
    }
    if category_ids:
        category_names = {
            category.id: category.name
            for category in db.query(Category).filter(Category.id.in_(category_ids)).all()
        }

    snapshot: list[dict[str, Any]] = []
    for item in candidates:
        knowledge = knowledge_rows.get(item["knowledge_id"])
        category_id = str(knowledge.category_id) if knowledge and knowledge.category_id else None
        category_name = category_names.get(category_id or "", "")
        snapshot.append(
            {
                "candidate_ref": _new_id("cand"),
                "knowledge_id": item["knowledge_id"],
                "rank": item["rank"],
                "title": str((knowledge.title if knowledge else item["title"]) or "")[:256],
                "content": copy.deepcopy(knowledge.content) if knowledge else {},
                "category_id": category_id,
                "category_name": category_name,
                "knowledge_origin": "business_accumulation",
                "embedding_score": item.get("embedding_score"),
                "rerank_score": item.get("rerank_score"),
                "final_score": item.get("final_score"),
                "selected": bool(item.get("selected")),
            }
        )
    return {
        "query": str(event.query_text or "")[:1000],
        "candidates": snapshot,
    }


def ensure_work_orders(db: Session, minimum_new: int = 0) -> int:
    """Materialize missing work orders from business-pool events.

    Events are materialized newest first.  A unique conversation constraint and
    nested transactions make concurrent claim requests safe even when both
    happen to inspect the same telemetry row.

    严格口径：只有带上游曼哈顿工单号（``question_form_id``）的事件才能进入
    盲标池。仅有会话号的样本会被跳过 —— 标注页要用工单号打开上游工单详情，
    没有工单号的样本无法核对。工单号是盲标工单的身份，同一工单的多次会话
    只保留最新一条。
    """

    existing_conversations = {
        str(value)
        for (value,) in db.query(BlindLabelWorkOrder.conversation_id).all()
        if value
    }
    existing_question_forms = {
        str(value)
        for (value,) in db.query(BlindLabelWorkOrder.question_form_id).all()
        if value
    }
    needed = max(0, int(minimum_new or 0))
    # A claim may need to skip events with missing/deleted knowledge.  Keep a
    # bounded oversampling factor so a large telemetry table is never loaded.
    limit = max(100, needed * 25)
    candidate_events = (
        db.query(RetrievalQualityEvent)
        .filter(
            RetrievalQualityEvent.conversation_id.isnot(None),
            RetrievalQualityEvent.question_form_id.isnot(None),
            RetrievalQualityEvent.candidate_snapshot.isnot(None),
            RetrievalQualityEvent.source_kind.in_(("reply", "combined")),
            RetrievalQualityEvent.request_status.in_(("success", "fallback")),
        )
        # Scan newest first so a conversation's frozen snapshot always comes
        # from its latest eligible retrieval event and newly created work orders
        # retain a deterministic source-time priority for dispatch.
        .order_by(RetrievalQualityEvent.created_at.desc(), RetrievalQualityEvent.id.desc())
        .limit(limit)
        .all()
    )
    latest_by_question_form: dict[str, RetrievalQualityEvent] = {}
    for event in candidate_events:
        question_form_id = str(event.question_form_id or "").strip()
        if question_form_id and question_form_id not in latest_by_question_form:
            latest_by_question_form[question_form_id] = event
    events = list(latest_by_question_form.values())
    created = 0
    seen_in_run: set[str] = set()
    for event in events:
        conversation_id = str(event.conversation_id or "").strip()
        question_form_id = str(event.question_form_id or "").strip()
        if (
            not conversation_id
            or not question_form_id
            or question_form_id in existing_question_forms
            or conversation_id in existing_conversations
            or question_form_id in seen_in_run
        ):
            continue
        snapshot = freeze_work_order_snapshot(db, event)
        if not snapshot or not snapshot.get("candidates"):
            continue
        candidates = snapshot["candidates"]
        if not _top1_score_is_eligible(candidates):
            continue
        first = candidates[0] if candidates else {}
        work_order = BlindLabelWorkOrder(
            id=_new_id("wo"),
            conversation_id=conversation_id[:128],
            question_form_id=question_form_id[:64],
            source_event_id=event.id,
            query_text=snapshot["query"],
            category_id=first.get("category_id"),
            category_name=first.get("category_name") or None,
            candidate_snapshot=candidates,
            source_created_at=event.created_at,
        )
        try:
            with db.begin_nested():
                db.add(work_order)
                db.flush()
        except IntegrityError:
            continue
        existing_conversations.add(conversation_id)
        existing_question_forms.add(question_form_id)
        seen_in_run.add(question_form_id)
        created += 1
        if needed and created >= needed:
            break
    return created


def _active_batch_rows(db: Session, batch: BlindLabelBatch) -> list[BlindLabelAssignment]:
    return (
        db.query(BlindLabelAssignment)
        .filter(
            BlindLabelAssignment.batch_id == batch.id,
            BlindLabelAssignment.status != "released",
        )
        .order_by(BlindLabelAssignment.assigned_at, BlindLabelAssignment.id)
        .all()
    )


def _batch_counts(db: Session, batch: BlindLabelBatch) -> dict[str, int]:
    rows = db.query(BlindLabelAssignment.status).filter(BlindLabelAssignment.batch_id == batch.id).all()
    statuses = [str(status) for (status,) in rows]
    active = [status for status in statuses if status != "released"]
    return {
        "assigned_count": len(active),
        "completed_count": statuses.count("completed"),
        "in_progress_count": sum(status in {"assigned", "in_progress"} for status in statuses),
        "released_count": statuses.count("released"),
    }


def _maybe_complete_batch(db: Session, batch: BlindLabelBatch) -> None:
    counts = _batch_counts(db, batch)
    if (
        batch.status == "active"
        and counts["assigned_count"] >= batch.target_count
        and counts["completed_count"] >= batch.target_count
    ):
        batch.status = "completed"
        batch.completed_at = datetime.utcnow()


def _work_order_capacity(db: Session, work_order_id: str) -> int:
    rows = (
        db.query(BlindLabelAssignment.user_id, BlindLabelAssignment.status)
        .filter(BlindLabelAssignment.work_order_id == work_order_id)
        .with_for_update()
        .all()
    )
    active_users = {
        str(user_id)
        for user_id, status in rows
        if str(status) in BLIND_LABEL_ACTIVE_STATUSES
    }
    return max(0, BLIND_LABEL_MAX_ASSIGNMENTS - len(active_users))


def _choose_assignments(
    db: Session,
    batch: BlindLabelBatch,
    user_id: str,
    needed: int,
) -> list[BlindLabelAssignment]:
    if needed <= 0:
        return []
    active_assignment_count = (
        db.query(func.count(BlindLabelAssignment.id))
        .filter(
            BlindLabelAssignment.work_order_id == BlindLabelWorkOrder.id,
            BlindLabelAssignment.status.in_(BLIND_LABEL_ACTIVE_STATUSES),
        )
        .correlate(BlindLabelWorkOrder)
        .scalar_subquery()
    )
    current_user_already_assigned = (
        db.query(BlindLabelAssignment.id)
        .filter(
            BlindLabelAssignment.work_order_id == BlindLabelWorkOrder.id,
            BlindLabelAssignment.user_id == user_id,
        )
        .exists()
    )
    generated_at = func.coalesce(
        BlindLabelWorkOrder.source_created_at,
        BlindLabelWorkOrder.created_at,
    )
    # First fill work orders already assigned to other annotators, favoring the
    # ones nearest the three-person cap.  Within the same assignment level,
    # dispatch the newest source work order first.  Lock each selected work
    # order again before counting active annotators: PostgreSQL honors the row
    # lock; SQLite simply serializes the test transaction.
    candidates = (
        db.query(BlindLabelWorkOrder)
        .filter(
            ~current_user_already_assigned,
            active_assignment_count < BLIND_LABEL_MAX_ASSIGNMENTS,
        )
        .order_by(
            active_assignment_count.desc(),
            generated_at.desc(),
            BlindLabelWorkOrder.id.desc(),
        )
        .limit(max(200, needed * 30))
        .all()
    )
    created: list[BlindLabelAssignment] = []
    selected_work_order_ids: set[str] = set()
    for candidate in candidates:
        if len(created) >= needed:
            break
        if candidate.id in selected_work_order_ids:
            continue
        locked = (
            db.query(BlindLabelWorkOrder)
            .filter(BlindLabelWorkOrder.id == candidate.id)
            .with_for_update()
            .first()
        )
        if not locked or _work_order_capacity(db, locked.id) <= 0:
            continue
        # 二次防线：历史上已物化但尚未分配的低分工单，也不能进入新批次。
        if not _top1_score_is_eligible(
            [item for item in (locked.candidate_snapshot or []) if isinstance(item, dict)]
        ):
            continue
        assignment = BlindLabelAssignment(
            id=_new_id("assign"),
            batch_id=batch.id,
            work_order_id=locked.id,
            user_id=user_id,
            status="assigned",
        )
        try:
            with db.begin_nested():
                db.add(assignment)
                db.flush()
        except IntegrityError:
            continue
        created.append(assignment)
        selected_work_order_ids.add(locked.id)
    return created


def claim_batch(
    db: Session,
    user_id: str,
    target_count: int = BLIND_LABEL_BATCH_SIZE,
) -> tuple[BlindLabelBatch, list[BlindLabelAssignment]]:
    """Return/reuse a user's fixed-size 50-item batch.

    ``target_count`` is retained only for compatibility with earlier callers;
    it is intentionally ignored so a browser request cannot reserve more or
    fewer work orders than the established blind-label batch size.
    """

    target_count = BLIND_LABEL_BATCH_SIZE
    batch = (
        db.query(BlindLabelBatch)
        .filter(
            BlindLabelBatch.user_id == user_id,
            BlindLabelBatch.status == "active",
        )
        .order_by(BlindLabelBatch.created_at.desc(), BlindLabelBatch.id.desc())
        .with_for_update()
        .first()
    )
    if batch is None:
        batch = BlindLabelBatch(
            id=_new_id("batch"),
            user_id=user_id,
            target_count=target_count,
            status="active",
        )
        try:
            with db.begin_nested():
                db.add(batch)
                db.flush()
        except IntegrityError:
            # A concurrent first visit may have won the partial unique index.
            # Re-read the committed active batch instead of creating a second
            # batch or leaking the integrity error to the browser.
            batch = (
                db.query(BlindLabelBatch)
                .filter(
                    BlindLabelBatch.user_id == user_id,
                    BlindLabelBatch.status == "active",
                )
                .order_by(BlindLabelBatch.created_at.desc(), BlindLabelBatch.id.desc())
                .with_for_update()
                .first()
            )
            if batch is None:
                raise
    else:
        # Normalize active batches created before batch size became fixed.
        if int(batch.target_count or 0) != BLIND_LABEL_BATCH_SIZE:
            batch.target_count = BLIND_LABEL_BATCH_SIZE

    rows = _active_batch_rows(db, batch)
    needed = max(0, target_count - len(rows))
    if needed:
        ensure_work_orders(db, needed)
        _choose_assignments(db, batch, user_id, needed)
    _maybe_complete_batch(db, batch)
    db.flush()
    return batch, _active_batch_rows(db, batch)


def current_batch(
    db: Session,
    user_id: str,
) -> tuple[BlindLabelBatch | None, list[BlindLabelAssignment]]:
    """Read a user's current batch without creating or assigning anything.

    The read endpoint must not silently start the next 50-item batch after a
    user finishes the previous one.  An explicit ``claim_batch`` call is the
    command that creates/refills a batch.  When no active batch exists, return
    the most recent completed batch as context and an empty work-item list;
    callers can then show a deliberate "领取下一批" action.
    """

    batch = (
        db.query(BlindLabelBatch)
        .filter(
            BlindLabelBatch.user_id == user_id,
            BlindLabelBatch.status == "active",
        )
        .order_by(BlindLabelBatch.created_at.desc(), BlindLabelBatch.id.desc())
        .first()
    )
    if batch is not None:
        return batch, _active_batch_rows(db, batch)
    batch = (
        db.query(BlindLabelBatch)
        .filter(BlindLabelBatch.user_id == user_id)
        .order_by(BlindLabelBatch.created_at.desc(), BlindLabelBatch.id.desc())
        .first()
    )
    return batch, []


def mark_assignment_started(
    db: Session,
    assignment: BlindLabelAssignment,
) -> BlindLabelAssignment:
    """在行锁下把待标任务切换为进行中，绝不复活已回收任务。"""

    locked = (
        db.query(BlindLabelAssignment)
        .populate_existing()
        .filter(BlindLabelAssignment.id == assignment.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise ValueError("盲标分配不存在")
    if locked.status == "released":
        if locked.release_reason == BLIND_LABEL_AUTO_RELEASE_REASON:
            raise ValueError("该盲标任务已超时自动回收，请刷新后领取新任务")
        raise ValueError("该盲标任务已释放")
    if locked.status == "assigned":
        locked.status = "in_progress"
        locked.started_at = datetime.utcnow()
        db.flush()
    return locked


def submit_assignment(
    db: Session,
    assignment: BlindLabelAssignment,
    annotations: Iterable[dict[str, Any]],
    *,
    note: str = "",
    task_reason_code: str = "",
) -> list[BlindLabelAnnotation]:
    """Validate and persist a complete one-shot annotation submission."""

    locked = (
        db.query(BlindLabelAssignment)
        .populate_existing()
        .filter(BlindLabelAssignment.id == assignment.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise ValueError("盲标分配不存在")
    if locked.status == "released":
        if locked.release_reason == BLIND_LABEL_AUTO_RELEASE_REASON:
            raise ValueError("该盲标任务已超时自动回收，请刷新后领取新任务")
        raise ValueError("该盲标任务已释放")
    if locked.status == "completed":
        raise ValueError("该盲标任务已经提交")
    candidates = list(locked.work_order.candidate_snapshot or [])
    expected_refs = {
        str(item.get("candidate_ref") or "")
        for item in candidates
        if isinstance(item, dict)
    }
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in annotations:
        candidate_ref = str(item.get("candidate_ref") or item.get("candidateRef") or "").strip()
        verdict = str(item.get("verdict") or "").strip()
        if verdict in {"可参考", "helpful"}:
            verdict = "referable"
        elif verdict in {"不可参考", "unhelpful"}:
            verdict = "not_referable"
        if candidate_ref not in expected_refs:
            raise ValueError("包含不属于当前盲标任务的候选项")
        if candidate_ref in seen:
            raise ValueError("同一候选项不能重复标注")
        if verdict not in BLIND_LABEL_VERDICTS:
            raise ValueError("标注结果必须为 referable 或 not_referable")
        reason_code = str(item.get("reason_code") or item.get("reasonCode") or "").strip()
        reason = str(item.get("reason") or "").strip()[:2000]
        if verdict == "referable":
            if reason_code and reason_code not in BLIND_LABEL_REFERABLE_REASON_CODES:
                raise ValueError("可参考原因不合法")
        else:
            if reason_code not in BLIND_LABEL_NOT_REFERABLE_REASON_CODES:
                raise ValueError("选择不可参考时必须选择原因")
        if reason_code == "other" and not reason:
            raise ValueError("选择其他原因时请填写补充说明")
        seen.add(candidate_ref)
        normalized.append(
            {
                "candidate_ref": candidate_ref,
                "verdict": verdict,
                "reason_code": reason_code,
                "reason": reason,
            }
        )
    task_reason_code = str(task_reason_code or "").strip()
    if task_reason_code and task_reason_code not in BLIND_LABEL_TASK_REASON_CODES:
        raise ValueError("本工单原因不合法")

    referable_items = [
        item for item in normalized if item["verdict"] == "referable"
    ]
    if len(referable_items) > 1:
        raise ValueError("一次标注只能选择一条可参考候选")

    if not referable_items:
        # The only valid submission without a referable candidate is the
        # explicit "全部不可参考" path.  It must carry a decision and reason
        # for every frozen candidate.  The assignment-level task reason is
        # retained as optional compatibility metadata and is not required.
        if seen != expected_refs:
            raise ValueError("三条候选均不可参考时，必须分别选择每条候选的原因")
    # A single referable candidate is sufficient to complete this work order.
    # The other candidates may be omitted by the new UI; when a client still
    # submits explicit not-referable decisions, their reason validation above
    # remains in force.
    created: list[BlindLabelAnnotation] = []
    for item in normalized:
        annotation = BlindLabelAnnotation(
            id=_new_id("label"),
            assignment_id=locked.id,
            work_order_id=locked.work_order_id,
            user_id=locked.user_id,
            candidate_ref=item["candidate_ref"],
            verdict=item["verdict"],
            reason_code=item["reason_code"],
            reason=item["reason"],
        )
        db.add(annotation)
        created.append(annotation)
    locked.status = "completed"
    locked.completed_at = datetime.utcnow()
    locked.note = str(note or "").strip()[:2000]
    locked.task_reason_code = task_reason_code
    locked.updated_at = datetime.utcnow()
    _maybe_complete_batch(db, locked.batch)
    db.flush()
    return created


def release_assignment(db: Session, assignment: BlindLabelAssignment, reason: str = "") -> None:
    locked = (
        db.query(BlindLabelAssignment)
        .populate_existing()
        .filter(BlindLabelAssignment.id == assignment.id)
        .with_for_update()
        .first()
    )
    if locked is None:
        raise ValueError("盲标分配不存在")
    if locked.status == "completed":
        raise ValueError("已提交的盲标任务不能释放")
    if locked.status == "released":
        return
    locked.status = "released"
    locked.released_at = datetime.utcnow()
    locked.release_reason = str(reason or "").strip()[:512]
    locked.updated_at = datetime.utcnow()
    db.flush()


def release_expired_assignments(
    db: Session,
    *,
    now: datetime | None = None,
    timeout_seconds: int | None = None,
    batch_size: int | None = None,
) -> int:
    """自动回收领取超时但尚未提交的盲标任务。

    ``assigned_at`` 是唯一可靠的租约起点：页面没有草稿和心跳，不能用
    ``updated_at`` 伪造活跃时间。行锁使后台回收与最终提交互斥；先获得锁的
    一方完成后，另一方会按最新状态跳过或返回已回收提示。
    """

    if not settings.BLIND_LABEL_AUTO_RELEASE_ENABLED:
        return 0
    effective_timeout = max(
        1,
        int(
            settings.BLIND_LABEL_ASSIGNMENT_TIMEOUT_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        ),
    )
    limit = max(
        1,
        int(
            settings.BLIND_LABEL_RELEASE_BATCH_SIZE
            if batch_size is None
            else batch_size
        ),
    )
    released_at = now or datetime.utcnow()
    cutoff = released_at - timedelta(seconds=effective_timeout)
    assignments = (
        db.query(BlindLabelAssignment)
        .filter(
            BlindLabelAssignment.status.in_(("assigned", "in_progress")),
            BlindLabelAssignment.assigned_at <= cutoff,
        )
        .order_by(BlindLabelAssignment.assigned_at, BlindLabelAssignment.id)
        .with_for_update(skip_locked=True)
        .limit(limit)
        .all()
    )
    for assignment in assignments:
        assignment.status = "released"
        assignment.released_at = released_at
        assignment.release_reason = BLIND_LABEL_AUTO_RELEASE_REASON
        assignment.updated_at = released_at
    db.flush()
    return len(assignments)


def batch_summary(db: Session, batch: BlindLabelBatch) -> dict[str, Any]:
    counts = _batch_counts(db, batch)
    target_count = int(batch.target_count or BLIND_LABEL_BATCH_SIZE)
    pending_count = max(0, target_count - counts["assigned_count"])
    return {
        "id": batch.id,
        "status": batch.status,
        "target_count": target_count,
        "total": target_count,
        "total_count": target_count,
        **counts,
        "assigned": counts["assigned_count"],
        "completed": counts["completed_count"],
        "pending": pending_count,
        "in_progress": counts["in_progress_count"],
        "started_at": batch.created_at,
        "created_at": batch.created_at,
        "completed_at": batch.completed_at,
    }


def _public_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidate_ref": str(snapshot.get("candidate_ref") or ""),
        "rank": int(snapshot.get("rank") or 0),
        "title": str(snapshot.get("title") or ""),
        "content": _safe_content(snapshot.get("content")),
        "category": str(snapshot.get("category_name") or ""),
    }


def _private_candidate(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        **_public_candidate(snapshot),
        "knowledge_id": snapshot.get("knowledge_id"),
        "category_id": snapshot.get("category_id"),
        "knowledge_origin": snapshot.get("knowledge_origin"),
        "embedding_score": snapshot.get("embedding_score"),
        "rerank_score": snapshot.get("rerank_score"),
        "final_score": snapshot.get("final_score"),
        "selected": bool(snapshot.get("selected")),
    }


def assignment_summary(db: Session, assignment: BlindLabelAssignment) -> dict[str, Any]:
    work_order = assignment.work_order
    snapshots = [item for item in (work_order.candidate_snapshot or []) if isinstance(item, dict)]
    annotation_count = db.query(func.count(BlindLabelAnnotation.id)).filter(
        BlindLabelAnnotation.assignment_id == assignment.id
    ).scalar() or 0
    return {
        "id": assignment.id,
        "assignment_id": assignment.id,
        "work_order_id": work_order.id,
        "batch_id": assignment.batch_id,
        "conversation_id": work_order.conversation_id,
        "question_form_id": work_order.question_form_id or "",
        "query": work_order.query_text,
        "category": work_order.category_name or "",
        "candidate_count": len(snapshots),
        "status": assignment.status,
        "assigned_at": assignment.assigned_at,
        "started_at": assignment.started_at,
        "completed_at": assignment.completed_at,
        "created_at": assignment.created_at,
        "updated_at": assignment.updated_at,
        "annotation_count": int(annotation_count),
        "annotations": int(annotation_count),
        "note": assignment.note or "",
        "task_reason_code": assignment.task_reason_code or "",
    }


def public_assignment_detail(db: Session, assignment: BlindLabelAssignment) -> dict[str, Any]:
    return {
        "assignment": assignment_summary(db, assignment),
        "note": assignment.note or "",
        "task_reason_code": assignment.task_reason_code or "",
        "candidates": [
            _public_candidate(item)
            for item in (assignment.work_order.candidate_snapshot or [])
            if isinstance(item, dict)
        ],
        "annotations": [
            {
                "candidateRef": item.candidate_ref,
                "verdict": item.verdict,
                "reasonCode": item.reason_code or "",
                "reason": item.reason,
            }
            for item in sorted(assignment.annotations, key=lambda value: value.candidate_ref)
        ],
    }


def _completed_assignments(db: Session, work_order_id: str) -> list[BlindLabelAssignment]:
    return (
        db.query(BlindLabelAssignment)
        .filter(
            BlindLabelAssignment.work_order_id == work_order_id,
            BlindLabelAssignment.status == "completed",
        )
        .order_by(BlindLabelAssignment.completed_at, BlindLabelAssignment.id)
        .all()
    )


def consensus_for_work_order(db: Session, work_order: BlindLabelWorkOrder) -> dict[str, Any]:
    assignments = _completed_assignments(db, work_order.id)
    candidates = [item for item in (work_order.candidate_snapshot or []) if isinstance(item, dict)]
    candidate_results: list[dict[str, Any]] = []
    arbitration_by_ref = {
        item.candidate_ref: item
        for item in db.query(BlindLabelArbitration)
        .filter(BlindLabelArbitration.work_order_id == work_order.id)
        .all()
    }
    # New submissions contain at most one referable candidate.  In that mode,
    # different selected candidates are a real disagreement even though the
    # non-selected candidates are intentionally omitted rather than recorded
    # as negative labels.  Keep the old per-candidate positive/negative split
    # behavior for legacy submissions that contain multiple referable labels.
    referable_refs_by_assignment = [
        {
            annotation.candidate_ref
            for annotation in assignment.annotations
            if annotation.verdict == "referable"
        }
        for assignment in assignments
    ]
    single_choice_mode = bool(assignments) and all(
        len(refs) <= 1 for refs in referable_refs_by_assignment
    )
    selected_refs = {
        next(iter(refs))
        for refs in referable_refs_by_assignment
        if len(refs) == 1
    }
    choice_disagreement = (
        single_choice_mode
        and len(assignments) >= BLIND_LABEL_MAX_ASSIGNMENTS
        and len(
            {
                next(iter(refs)) if refs else None
                for refs in referable_refs_by_assignment
            }
        )
        > 1
    )
    for candidate in candidates:
        ref = str(candidate.get("candidate_ref") or "")
        votes = [
            annotation.verdict
            for assignment in assignments
            for annotation in assignment.annotations
            if annotation.candidate_ref == ref
        ]
        counts = Counter(votes)
        preliminary = None
        if counts:
            preliminary = counts.most_common(1)[0][0]
        arbitration = arbitration_by_ref.get(ref)
        split = (
            counts.get("referable", 0) > 0
            and counts.get("not_referable", 0) > 0
        )
        choice_split = choice_disagreement and ref in selected_refs
        if arbitration is not None:
            candidate_status = "arbitrated"
        elif split or choice_split:
            candidate_status = "needs_arbitration"
        elif len(votes) >= BLIND_LABEL_MAX_ASSIGNMENTS:
            candidate_status = "unanimous"
        else:
            candidate_status = "pending"
        candidate_results.append(
            {
                "candidate_ref": ref,
                "rank": int(candidate.get("rank") or 0),
                "referable_count": int(counts.get("referable", 0)),
                "not_referable_count": int(counts.get("not_referable", 0)),
                "preliminary_verdict": preliminary,
                "status": candidate_status,
                "needs_arbitration": (split or choice_split) and arbitration is None,
                "arbitrated_verdict": arbitration.verdict if arbitration else None,
                "arbitrated_by": arbitration.arbitrated_by if arbitration else None,
                "arbitration_reason": arbitration.reason if arbitration else "",
                "arbitrated": arbitration is not None,
            }
        )
    completed_count = len(assignments)
    if completed_count < BLIND_LABEL_MAX_ASSIGNMENTS:
        overall_status = "pending"
        needs_arbitration = False
    else:
        split = any(
            result["referable_count"] > 0 and result["not_referable_count"] > 0
            for result in candidate_results
        )
        has_disagreement = split or choice_disagreement
        unresolved_split = any(
            result["needs_arbitration"] for result in candidate_results
        )
        overall_status = (
            "majority"
            if unresolved_split
            else ("arbitrated" if has_disagreement else "unanimous")
        )
        needs_arbitration = unresolved_split
    return {
        "status": overall_status,
        "completed_annotators": completed_count,
        "needs_arbitration": needs_arbitration,
        "candidate_results": candidate_results,
    }


def private_work_order_detail(db: Session, work_order: BlindLabelWorkOrder) -> dict[str, Any]:
    assignments = (
        db.query(BlindLabelAssignment)
        .filter(BlindLabelAssignment.work_order_id == work_order.id)
        .order_by(BlindLabelAssignment.assigned_at, BlindLabelAssignment.id)
        .all()
    )
    return {
        "id": work_order.id,
        "conversation_id": work_order.conversation_id,
        "question_form_id": work_order.question_form_id or "",
        "query": work_order.query_text,
        "category": work_order.category_name or "",
        "source_event_id": work_order.source_event_id,
        "source_created_at": work_order.source_created_at,
        "created_at": work_order.created_at,
        "candidates": [
            _private_candidate(item)
            for item in (work_order.candidate_snapshot or [])
            if isinstance(item, dict)
        ],
        "assignments": [
            {
                **assignment_summary(db, assignment),
                "user_id": assignment.user_id,
                "username": assignment.user.username if assignment.user else "",
                "annotations": [
                    {
                        "candidate_ref": annotation.candidate_ref,
                        "verdict": annotation.verdict,
                        "reason_code": annotation.reason_code or "",
                        "reason": annotation.reason,
                        "created_at": annotation.created_at,
                    }
                    for annotation in assignment.annotations
                ],
            }
            for assignment in assignments
        ],
        "consensus": consensus_for_work_order(db, work_order),
    }


def work_order_status(db: Session, work_order: BlindLabelWorkOrder) -> tuple[str, dict[str, Any]]:
    assignments = (
        db.query(BlindLabelAssignment.status)
        .filter(BlindLabelAssignment.work_order_id == work_order.id)
        .all()
    )
    statuses = [str(status) for (status,) in assignments]
    active = [status for status in statuses if status != "released"]
    consensus = consensus_for_work_order(db, work_order)
    if consensus["status"] == "majority":
        return "needs_arbitration", consensus
    if consensus["status"] in {"unanimous", "arbitrated"}:
        if consensus["status"] == "arbitrated":
            return "arbitrated", consensus
        return "completed", consensus
    if not active:
        return "pending", consensus
    if any(status == "completed" for status in active):
        return "partial", consensus
    return "in_progress", consensus


def _assignment_activity_times(assignment: BlindLabelAssignment) -> list[datetime]:
    """Return all timestamps that represent activity for overview filtering."""

    values: list[datetime] = []
    for value in (
        assignment.assigned_at,
        assignment.started_at,
        assignment.completed_at,
        assignment.released_at,
    ):
        if value is not None:
            values.append(value)
    values.extend(
        annotation.created_at
        for annotation in assignment.annotations
        if annotation.created_at is not None
    )
    return values


def _assignment_matches_activity_range(
    assignment: BlindLabelAssignment,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    values = _assignment_activity_times(assignment)
    if not values:
        return False
    return any(
        (start_at is None or value >= start_at)
        and (end_at is None or value < end_at)
        for value in values
    )


def overview(
    db: Session,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    status_filter: str | None = None,
    verdict: str | None = None,
    dimension: str | None = None,
    user_id: str | None = None,
    batch_id: str | None = None,
    conversation_id: str | None = None,
    category: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = db.query(BlindLabelWorkOrder).order_by(
        BlindLabelWorkOrder.created_at.desc(), BlindLabelWorkOrder.id.desc()
    )
    # The panel's time filter is based on assignment/annotation activity, not
    # the original retrieval event time.  Do the range check after loading
    # assignments so a recently labeled older work order is not hidden.
    if conversation_id:
        identifier = conversation_id.strip()
        query = query.filter(
            (BlindLabelWorkOrder.question_form_id == identifier)
            | (BlindLabelWorkOrder.conversation_id == identifier)
        )
    if category:
        query = query.filter(BlindLabelWorkOrder.category_name == category.strip())
    candidates = query.all()
    items: list[dict[str, Any]] = []
    user_stats: dict[str, dict[str, Any]] = defaultdict(
        lambda: {
            "user_id": "",
            "username": "",
            "assigned_count": 0,
            "completed_count": 0,
            "released_count": 0,
            "annotation_count": 0,
            "referable_count": 0,
            "not_referable_count": 0,
            "total_duration_seconds": 0.0,
            "duration_count": 0,
            "last_activity_at": None,
        }
    )
    for work_order in candidates:
        if keyword:
            keyword_value = keyword.strip().lower()
            if keyword_value and keyword_value not in (
                f"{work_order.question_form_id or ''} {work_order.conversation_id} "
                f"{work_order.query_text}"
            ).lower():
                continue
        assignments = (
            db.query(BlindLabelAssignment)
            .filter(BlindLabelAssignment.work_order_id == work_order.id)
            .order_by(BlindLabelAssignment.assigned_at, BlindLabelAssignment.id)
            .all()
        )
        if batch_id:
            assignments = [item for item in assignments if item.batch_id == batch_id]
        if user_id:
            assignments = [item for item in assignments if item.user_id == user_id]
        if start_at is not None or end_at is not None:
            assignments = [
                item
                for item in assignments
                if _assignment_matches_activity_range(item, start_at, end_at)
            ]
        dimension_filter = (dimension or verdict or "").strip().lower()
        dimension_status_filter = None
        if dimension_filter in {"disputed", "争议"}:
            dimension_status_filter = "disputed"
        elif dimension_filter in {"consensus", "共识"}:
            dimension_status_filter = "consensus"
        if dimension_filter in {"helpful", "可参考"}:
            dimension_filter = "referable"
        elif dimension_filter in {"unhelpful", "不可参考"}:
            dimension_filter = "not_referable"
        if dimension_filter in {"referable", "not_referable", "annotations"}:
            verdict_normalized = dimension_filter
            if verdict_normalized == "annotations":
                has_dimension = any(
                    assignment.annotations for assignment in assignments
                )
            else:
                has_dimension = any(
                    annotation.verdict == verdict_normalized
                    for assignment in assignments
                    for annotation in assignment.annotations
                )
            if not has_dimension:
                continue
        if not assignments:
            continue
        current_status, consensus = work_order_status(db, work_order)
        if dimension_status_filter == "disputed" and not consensus["needs_arbitration"]:
            continue
        if dimension_status_filter == "consensus" and current_status not in {"completed", "arbitrated"}:
            continue
        status_value = (status_filter or "").strip().lower()
        if status_value in {"disputed", "争议", "majority"}:
            status_matches = consensus["needs_arbitration"]
        elif status_value in {"consensus", "已完成", "completed"}:
            status_matches = current_status in {"completed", "arbitrated"}
        elif status_value:
            status_matches = current_status == status_value
        else:
            status_matches = True
        if not status_matches:
            continue
        active_assignments = [item for item in assignments if item.status != "released"]
        completed = [item for item in active_assignments if item.status == "completed"]
        last_activity = max(
            [
                value
                for value in (
                    *(item.completed_at for item in assignments),
                    *(item.released_at for item in assignments),
                    *(item.assigned_at for item in assignments),
                )
                if value is not None
            ],
            default=None,
        )
        items.append(
            {
                "id": work_order.id,
                "work_order_id": work_order.id,
                "conversation_id": work_order.conversation_id,
                "question_form_id": work_order.question_form_id or "",
                "query": work_order.query_text,
                "category": work_order.category_name or "",
                "candidate_count": len(work_order.candidate_snapshot or []),
                "assignment_count": len(active_assignments),
                "completed_assignments": len(completed),
                "released_assignments": len(assignments) - len(active_assignments),
                "status": current_status,
                "consensus_status": consensus["status"],
                "needs_arbitration": consensus["needs_arbitration"],
                "preliminary_verdict": (
                    consensus["candidate_results"][0]["preliminary_verdict"]
                    if consensus["candidate_results"]
                    else None
                ),
                "first_assigned_at": min(
                    (item.assigned_at for item in assignments if item.assigned_at),
                    default=None,
                ),
                "last_activity_at": last_activity,
                "last_annotated_at": max(
                    (item.completed_at for item in assignments if item.completed_at),
                    default=None,
                ),
                "created_at": work_order.created_at,
                "updated_at": last_activity or work_order.updated_at,
                "max_annotators": BLIND_LABEL_MAX_ASSIGNMENTS,
                "assignment_limit": BLIND_LABEL_MAX_ASSIGNMENTS,
                "dimension": (
                    consensus["candidate_results"][0]["preliminary_verdict"]
                    if consensus["candidate_results"]
                    else None
                ),
                "consensus": consensus,
                "assignments": [
                    {
                        "id": item.id,
                        "batch_id": item.batch_id,
                        "user_id": item.user_id,
                        "username": item.user.username if item.user else "",
                        "status": item.status,
                        "assigned_at": item.assigned_at,
                        "completed_at": item.completed_at,
                    }
                    for item in assignments
                ],
            }
        )
        for assignment in assignments:
            stat = user_stats[assignment.user_id]
            stat["user_id"] = assignment.user_id
            stat["username"] = assignment.user.username if assignment.user else ""
            if assignment.status == "released":
                stat["released_count"] += 1
            else:
                stat["assigned_count"] += 1
            if assignment.status == "completed":
                stat["completed_count"] += 1
            if assignment.completed_at:
                stat["last_activity_at"] = max(
                    value
                    for value in (stat["last_activity_at"], assignment.completed_at)
                    if value is not None
                )
            elif assignment.assigned_at:
                stat["last_activity_at"] = max(
                    value
                    for value in (stat["last_activity_at"], assignment.assigned_at)
                    if value is not None
                )
            for annotation in assignment.annotations:
                stat["annotation_count"] += 1
                if annotation.verdict == "referable":
                    stat["referable_count"] += 1
                else:
                    stat["not_referable_count"] += 1
            if assignment.completed_at and assignment.started_at:
                duration = (assignment.completed_at - assignment.started_at).total_seconds()
                if duration >= 0:
                    stat["total_duration_seconds"] += duration
                    stat["duration_count"] += 1
    items.sort(key=lambda item: (item["last_activity_at"] is not None, item["last_activity_at"], item["id"]), reverse=True)
    total = len(items)
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    total_pages = max(1, (total + page_size - 1) // page_size)
    page = min(page, total_pages)
    visible = items[(page - 1) * page_size : page * page_size]
    summary = {
        "work_orders": total,
        "assigned_work_orders": sum(item["assignment_count"] > 0 for item in items),
        "completed_work_orders": sum(item["status"] in {"completed", "arbitrated"} for item in items),
        "pending_work_orders": sum(item["status"] in {"pending", "in_progress", "partial", "needs_arbitration"} for item in items),
        "total_assignments": sum(item["assignment_count"] for item in items),
        "completed_assignments": sum(item["completed_assignments"] for item in items),
        "released_assignments": sum(item["released_assignments"] for item in items),
        "unanimous_count": sum(item["consensus_status"] == "unanimous" for item in items),
        "majority_count": sum(item["consensus_status"] == "majority" for item in items),
        "arbitrated_count": sum(item["consensus_status"] == "arbitrated" for item in items),
        "pending_consensus_count": sum(item["consensus_status"] == "pending" for item in items),
        "needs_arbitration_count": sum(item["needs_arbitration"] for item in items),
        "active_annotators": len({assignment["user_id"] for item in items for assignment in item["assignments"] if assignment["status"] != "released"}),
    }
    summary.update(
        {
            "total": summary["work_orders"],
            "completed": summary["completed_work_orders"],
            "pending": summary["pending_work_orders"],
            "in_progress": sum(item["status"] == "in_progress" for item in items),
            "partial": sum(item["status"] == "partial" for item in items),
            "disputed": summary["needs_arbitration_count"],
            "annotations": sum(stat["annotation_count"] for stat in user_stats.values()),
            # Pending and majority/disputed work orders are intentionally not
            # included in the denominator.  The rate measures agreement only
            # among resolved (unanimous or administrator-arbitrated) orders.
            "resolved_count": summary["unanimous_count"] + summary["arbitrated_count"],
            "consensus_rate": round(
                summary["unanimous_count"]
                / (summary["unanimous_count"] + summary["arbitrated_count"]),
                4,
            )
            if (summary["unanimous_count"] + summary["arbitrated_count"])
            else 0.0,
            "resolution_rate": round(
                (summary["unanimous_count"] + summary["arbitrated_count"])
                / summary["work_orders"],
                4,
            )
            if summary["work_orders"]
            else 0.0,
        }
    )
    people: list[dict[str, Any]] = []
    for stat in user_stats.values():
        assigned = int(stat["assigned_count"])
        completed_count = int(stat["completed_count"])
        people.append(
            {
                "user_id": stat["user_id"],
                "username": stat["username"],
                "assigned_count": assigned,
                "completed_count": completed_count,
                "released_count": int(stat["released_count"]),
                "completion_rate": round(completed_count / assigned, 4) if assigned else 0.0,
                "annotation_count": int(stat["annotation_count"]),
                "referable_count": int(stat["referable_count"]),
                "not_referable_count": int(stat["not_referable_count"]),
                "average_duration_seconds": (
                    round(stat["total_duration_seconds"] / stat["duration_count"], 2)
                    if stat["duration_count"]
                    else None
                ),
                "last_activity_at": stat["last_activity_at"],
                "total_assigned": assigned,
                "total_completed": completed_count,
                "total_annotations": int(stat["annotation_count"]),
                "helpful_count": int(stat["referable_count"]),
                "unhelpful_count": int(stat["not_referable_count"]),
                "assigned": assigned,
                "completed": completed_count,
                "annotations": int(stat["annotation_count"]),
                "helpful_rate": (
                    round(stat["referable_count"] / stat["annotation_count"], 4)
                    if stat["annotation_count"]
                    else 0.0
                ),
            }
        )
    people.sort(key=lambda item: (item["completed_count"], item["annotation_count"], item["username"]), reverse=True)
    return {
        "summary": summary,
        "items": visible,
        "work_orders": visible,
        "people": people,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }
