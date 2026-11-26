"""Structured blind-label evidence and versioned human review."""

import hashlib
import json
from collections import Counter, defaultdict

from sqlalchemy import func, or_
from sqlalchemy.orm import Session, load_only, selectinload

from app.models.blind_labeling import (
    POOL_STATE_ACTIVE,
    BlindLabelAnnotation,
    BlindLabelArbitration,
    BlindLabelAssignment,
    BlindLabelLearningReview,
    BlindLabelWorkOrder,
)
from app.models.integration import RetrievalQualityEvent
from app.models.knowledge import Knowledge, KnowledgeStatus
from app.schemas.blind_labeling import REPLAY_ISSUE_TYPES, BlindLabelLearningReviewInput
from app.services.blind_labeling import (
    BLIND_LABEL_NOT_REFERABLE_REASON_CODES,
    BLIND_LABEL_REFERABLE_REASON_CODES,
    BLIND_LABEL_TASK_REASON_CODES,
    consensus_for_work_order,
)


class LearningReviewError(ValueError):
    def __init__(self, status_code: int, message: str):
        super().__init__(message)
        self.status_code = status_code


def _digest(data: dict) -> str:
    payload = json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


_KNOWLEDGE_EVIDENCE_FIELDS = (
    Knowledge.title, Knowledge.subtitles, Knowledge.content, Knowledge.category_id, Knowledge.knowledge_origin,
    Knowledge.business_type, Knowledge.applicable_scenes, Knowledge.applicable_categories,
    Knowledge.applicable_brands, Knowledge.applicable_models, Knowledge.related_standard_items,
)


def _knowledge_version(knowledge: Knowledge | None) -> str | None:
    if knowledge is None or knowledge.status != KnowledgeStatus.PUBLISHED:
        return None
    evidence = {field.key: getattr(knowledge, field.key) for field in _KNOWLEDGE_EVIDENCE_FIELDS}
    evidence["updated_at"] = knowledge.updated_at.isoformat() if knowledge.updated_at else None
    return _digest(evidence)


def _knowledge_state(snapshot: dict, knowledge: Knowledge | None) -> str:
    if knowledge is None or knowledge.status != KnowledgeStatus.PUBLISHED:
        return "not_published"
    if "title" not in snapshot or "content" not in snapshot:
        return "snapshot_unverifiable"
    if snapshot["title"] != knowledge.title or snapshot["content"] != knowledge.content:
        return "snapshot_differs"
    return "snapshot_matches"


def _suggested_track(candidates: list[dict], task_reasons: Counter, completed: int) -> str:
    positives = [row for row in candidates if row["verdict"] == "referable"]
    revision_reasons = {"content_incorrect", "content_outdated", "needs_conditions", "needs_merge"}
    if any(
        revision_reasons.intersection(row["reason_counts"])
        for row in candidates
    ):
        return "knowledge_review"
    if len(positives) == 1:
        return (
            "ranking_review"
            if positives[0]["knowledge_state"] == "snapshot_matches"
            else "knowledge_review"
        )
    if completed and all(row["verdict"] == "not_referable" for row in candidates):
        if task_reasons["knowledge_missing"] == completed:
            return "knowledge_gap_review"
        if task_reasons["knowledge_exists_not_recalled"] == completed:
            return "recall_review"
    return "evidence_review"


def _preview_item(order, consensus, snapshots, knowledge_by_id):
    assignment_rows = [row for row in order.assignments if row.status == "completed"]
    allowed_reasons = BLIND_LABEL_REFERABLE_REASON_CODES | BLIND_LABEL_NOT_REFERABLE_REASON_CODES
    reason_counts = defaultdict(Counter)
    for assignment in assignment_rows:
        for annotation in assignment.annotations:
            if annotation.reason_code in allowed_reasons:
                reason_counts[annotation.candidate_ref][annotation.reason_code] += 1
    task_reasons = Counter(
        row.task_reason_code for row in assignment_rows
        if row.task_reason_code in BLIND_LABEL_TASK_REASON_CODES
    )
    items = []
    arbitrations = {row.candidate_ref: row for row in order.arbitrations}
    for result in consensus["candidate_results"]:
        ref = result["candidate_ref"]
        snapshot = snapshots[ref]
        raw_id = snapshot.get("knowledge_id")
        knowledge_id = raw_id if isinstance(raw_id, str) and raw_id else None
        knowledge = knowledge_by_id.get(knowledge_id)
        verdict = result["arbitrated_verdict"] or result["consensus_verdict"]
        verdict_source = None
        if verdict:
            verdict_source = "arbitration" if result["arbitrated"] else result["status"]
        items.append({
            "candidate_ref": ref,
            "rank": result["rank"],
            "knowledge_id": knowledge_id,
            "verdict": verdict,
            "verdict_source": verdict_source,
            "knowledge_state": _knowledge_state(snapshot, knowledge),
            "knowledge_version": _knowledge_version(knowledge),
            "arbitration_version": (
                arbitrations[ref].updated_at.isoformat() if result["arbitrated"] else None
            ),
            "knowledge_updated_at": (
                knowledge.updated_at.isoformat() if knowledge and knowledge.updated_at else None
            ),
            "referable_count": result["referable_count"],
            "not_referable_count": result["not_referable_count"],
            "reason_counts": dict(reason_counts[ref]),
        })
    if not items or (consensus["status"] != "majority" and not any(item["verdict"] for item in items)):
        return None
    positive_without_candidate = (
        consensus["status"] == "majority"
        and consensus["verdict"] == "referable"
        and not any(item["verdict"] == "referable" for item in items)
    )
    evidence = {
        "work_order_id": order.id,
        "category_id": order.category_id,
        "has_source_query": bool((order.query_text or "").strip()),
        "consensus_status": consensus["status"],
        "completed_annotators": len(assignment_rows),
        "required_annotators": consensus["required_annotators"],
        "task_reason_counts": dict(task_reasons),
        "candidates": items,
        "suggested_track": (
            "evidence_review" if positive_without_candidate
            else _suggested_track(items, task_reasons, len(assignment_rows))
        ),
    }
    if consensus["status"] == "majority":
        # 只有新增的多数表决样本扩展证据结构，保持原有全员一致与历史仲裁的证据版本兼容。
        evidence.update(
            verdict=consensus["verdict"],
            referable_count=consensus["referable_count"],
            not_referable_count=consensus["not_referable_count"],
        )
    return {
        **evidence,
        "evidence_version": _digest({
            **evidence,
            "source_event_id": order.source_event_id,
            "source_query": order.query_text,
        }),
    }


def _source_orders(db: Session):
    return db.query(BlindLabelWorkOrder).options(
        load_only(
            BlindLabelWorkOrder.id, BlindLabelWorkOrder.category_id,
            BlindLabelWorkOrder.assignment_limit, BlindLabelWorkOrder.candidate_snapshot,
            BlindLabelWorkOrder.source_event_id, BlindLabelWorkOrder.query_text,
        ),
        selectinload(BlindLabelWorkOrder.assignments).load_only(
            BlindLabelAssignment.id, BlindLabelAssignment.status,
            BlindLabelAssignment.completed_at, BlindLabelAssignment.task_reason_code,
        ).selectinload(BlindLabelAssignment.annotations).load_only(
            BlindLabelAnnotation.candidate_ref, BlindLabelAnnotation.verdict,
            BlindLabelAnnotation.reason_code,
        ),
        selectinload(BlindLabelWorkOrder.arbitrations).load_only(
            BlindLabelArbitration.candidate_ref, BlindLabelArbitration.verdict, BlindLabelArbitration.updated_at,
            # The shared consensus calculation also accesses these fields.
            BlindLabelArbitration.arbitrated_by, BlindLabelArbitration.reason,
        ),
    ).filter(BlindLabelWorkOrder.pool_state == POOL_STATE_ACTIVE)


def _latest_reviews(db: Session, work_order_ids: list[str]) -> dict:
    if not work_order_ids:
        return {}
    review = BlindLabelLearningReview
    latest_ids = (
        db.query(func.max(review.id)).filter(review.work_order_id.in_(work_order_ids)).group_by(review.work_order_id)
    )
    return {item.work_order_id: item for item in db.query(review).options(
        load_only(review.work_order_id, review.evidence_version, review.issue_type,
                  review.knowledge_id, review.knowledge_version, review.created_at)
    ).filter(review.id.in_(latest_ids)).all()}


def _evidence_items(db, orders, *, extra_knowledge_ids=(), lock=False):
    resolved = []
    knowledge_ids = {item for item in extra_knowledge_ids if item}
    for order in orders:
        consensus = consensus_for_work_order(db, order, order.assignments, order.arbitrations)
        if consensus["status"] not in {"unanimous", "majority", "arbitrated"}:
            continue
        snapshots = {
            str(item.get("candidate_ref") or ""): item
            for item in order.candidate_snapshot or [] if isinstance(item, dict)
        }
        resolved.append((order, consensus, snapshots))
        knowledge_ids.update(
            item["knowledge_id"] for item in snapshots.values()
            if isinstance(item.get("knowledge_id"), str) and item["knowledge_id"]
        )
    knowledge_by_id = {}
    if knowledge_ids:
        query = db.query(Knowledge).options(
            load_only(Knowledge.id, Knowledge.status, Knowledge.updated_at, *_KNOWLEDGE_EVIDENCE_FIELDS)
        ).filter(Knowledge.id.in_(knowledge_ids))
        if lock:
            query = query.order_by(Knowledge.id).with_for_update(read=True)
        knowledge_by_id = {item.id: item for item in query.all()}
    items = [
        item for order, consensus, snapshots in resolved
        if (item := _preview_item(order, consensus, snapshots, knowledge_by_id)) is not None
    ]
    return items, knowledge_by_id


def _review_status(evidence, review, knowledge_by_id):
    if review is None:
        return {"review_status": "pending_review", "replay_eligible": False, "review": None}
    is_stale = review.evidence_version != evidence["evidence_version"]
    if review.knowledge_id:
        current_version = _knowledge_version(knowledge_by_id.get(review.knowledge_id))
        is_stale = is_stale or not current_version or current_version != review.knowledge_version
    state = "stale" if is_stale else "excluded" if review.issue_type == "exclude" else "confirmed"
    return {
        "review_status": state,
        "replay_eligible": (
            state == "confirmed" and evidence["has_source_query"] and review.issue_type in REPLAY_ISSUE_TYPES
        ),
        "review": {
            "id": review.id, "issue_type": review.issue_type, "knowledge_id": review.knowledge_id,
            "created_at": review.created_at.isoformat(),
        },
    }


def preview_learning_samples(
    db: Session,
    *,
    cursor: str | None = None,
    scan_limit: int = 50,
    category_id: str | None = None,
) -> dict:
    """Scan a bounded keyset page. No writes, model calls or raw text in the output."""
    if not 1 <= scan_limit <= 100:
        raise ValueError("scan_limit must be between 1 and 100")
    query = _source_orders(db).filter(or_(
        BlindLabelWorkOrder.assignments.any(BlindLabelAssignment.status == "completed"),
        BlindLabelWorkOrder.arbitrations.any(),
    ))
    if cursor:
        query = query.filter(BlindLabelWorkOrder.id > cursor)
    if category_id:
        query = query.filter(BlindLabelWorkOrder.category_id == category_id)
    batch = query.order_by(BlindLabelWorkOrder.id).limit(scan_limit + 1).all()
    orders = batch[:scan_limit]
    reviews = _latest_reviews(db, [order.id for order in orders])
    items, knowledge = _evidence_items(
        db, orders, extra_knowledge_ids=[review.knowledge_id for review in reviews.values()],
    )
    return {
        "schema_version": 2,
        "scanned": len(orders),
        "next_cursor": orders[-1].id if len(batch) > scan_limit else None,
        "items": [
            {**item, **_review_status(item, reviews.get(item["work_order_id"]), knowledge)} for item in items
        ],
    }


def _review_context(db: Session, work_order_id: str, *, lock=False, selected_knowledge_id=None):
    query = _source_orders(db).filter(BlindLabelWorkOrder.id == work_order_id)
    order = (query.with_for_update() if lock else query).one_or_none()
    if order is None:
        raise LearningReviewError(409, "工单不存在或已退池，不能用于回流复核。")
    previous = (
        db.query(BlindLabelLearningReview).filter_by(work_order_id=work_order_id)
        .order_by(BlindLabelLearningReview.id.desc()).first()
    )
    items, knowledge = _evidence_items(
        db, [order], lock=lock,
        extra_knowledge_ids=(selected_knowledge_id, previous.knowledge_id if previous else None),
    )
    if not items:
        raise LearningReviewError(409, "工单尚未形成可复核结论，请重新扫描。")
    return items[0], previous, knowledge


def _review_detail(evidence, review, knowledge):
    state = _review_status(evidence, review, knowledge)
    if review:
        state["review"]["expected_answer"] = review.expected_answer
    return {"sample": evidence, **state}


def load_learning_review(db: Session, work_order_id: str) -> dict:
    return _review_detail(*_review_context(db, work_order_id))


def _original_retrieval_ids(db: Session, work_order_id: str) -> set[str]:
    row = (
        db.query(RetrievalQualityEvent.candidate_snapshot)
        .join(BlindLabelWorkOrder, BlindLabelWorkOrder.source_event_id == RetrievalQualityEvent.id)
        .filter(BlindLabelWorkOrder.id == work_order_id)
        .one_or_none()
    )
    snapshot = row[0] if row else None
    if not isinstance(snapshot, list):
        return set()
    return {
        candidate["knowledge_id"] for candidate in snapshot
        if isinstance(candidate, dict) and isinstance(candidate.get("knowledge_id"), str)
    }


def save_learning_review(db: Session, work_order_id: str, body: BlindLabelLearningReviewInput, reviewer_id: str) -> dict:
    evidence, previous, knowledge = _review_context(
        db, work_order_id, lock=True, selected_knowledge_id=body.knowledge_id,
    )
    if body.evidence_version != evidence["evidence_version"] or body.previous_review_id != (previous.id if previous else None):
        raise LearningReviewError(409, "证据或复核结果已变化，请重新打开后再确认。")
    if body.issue_type in REPLAY_ISSUE_TYPES and not evidence["has_source_query"]:
        raise LearningReviewError(422, "原工单缺少可回放的问题，不能确认为回放样本。")
    version = _knowledge_version(knowledge.get(body.knowledge_id))
    if body.knowledge_id and not version:
        raise LearningReviewError(422, "指定的正确知识不存在或尚未发布。")
    if body.issue_type in {"ranking", "retrieval_missed"}:
        in_frozen = body.knowledge_id in {item["knowledge_id"] for item in evidence["candidates"]}
        was_retrieved = in_frozen or body.knowledge_id in _original_retrieval_ids(db, work_order_id)
        if body.issue_type == "ranking" and not was_retrieved:
            raise LearningReviewError(422, "排序问题的正确知识必须出现在盲标候选或工单的原检索候选中。")
        if body.issue_type == "retrieval_missed" and was_retrieved:
            raise LearningReviewError(422, "正确知识已出现在原检索候选中，不能确认为漏召回。")
    review = BlindLabelLearningReview(
        work_order_id=work_order_id, evidence_version=evidence["evidence_version"],
        evidence_snapshot=evidence, issue_type=body.issue_type,
        knowledge_id=body.knowledge_id, knowledge_version=version, expected_answer=body.expected_answer,
        reviewer_id=reviewer_id,
    )
    db.add(review)
    db.flush()
    return _review_detail(evidence, review, knowledge)
