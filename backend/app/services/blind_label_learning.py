"""Read-only, structured evidence preview for the blind-label learning loop."""

import hashlib
import json
from collections import Counter, defaultdict

from sqlalchemy import or_
from sqlalchemy.orm import Session, load_only, selectinload

from app.models.blind_labeling import (
    POOL_STATE_ACTIVE,
    BlindLabelAnnotation,
    BlindLabelArbitration,
    BlindLabelAssignment,
    BlindLabelWorkOrder,
)
from app.models.knowledge import Knowledge, KnowledgeStatus
from app.services.blind_labeling import (
    BLIND_LABEL_NOT_REFERABLE_REASON_CODES,
    BLIND_LABEL_REFERABLE_REASON_CODES,
    BLIND_LABEL_TASK_REASON_CODES,
    consensus_for_work_order,
)


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
    for result in consensus["candidate_results"]:
        ref = result["candidate_ref"]
        snapshot = snapshots[ref]
        raw_id = snapshot.get("knowledge_id")
        knowledge_id = raw_id if isinstance(raw_id, str) and raw_id else None
        knowledge = knowledge_by_id.get(knowledge_id)
        verdict = result["arbitrated_verdict"] or result["consensus_verdict"]
        verdict_source = None
        if verdict:
            verdict_source = "arbitration" if result["arbitrated"] else "unanimous"
        items.append({
            "candidate_ref": ref,
            "rank": result["rank"],
            "knowledge_id": knowledge_id,
            "verdict": verdict,
            "verdict_source": verdict_source,
            "knowledge_state": _knowledge_state(snapshot, knowledge),
            "knowledge_updated_at": (
                knowledge.updated_at.isoformat() if knowledge and knowledge.updated_at else None
            ),
            "referable_count": result["referable_count"],
            "not_referable_count": result["not_referable_count"],
            "reason_counts": dict(reason_counts[ref]),
        })
    if not items or not any(item["verdict"] for item in items):
        return None
    evidence = {
        "work_order_id": order.id,
        "category_id": order.category_id,
        "consensus_status": consensus["status"],
        "completed_annotators": len(assignment_rows),
        "required_annotators": consensus["required_annotators"],
        "task_reason_counts": dict(task_reasons),
        "candidates": items,
        "suggested_track": _suggested_track(items, task_reasons, len(assignment_rows)),
    }
    payload = json.dumps(evidence, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return {
        **evidence,
        "evidence_version": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "review_status": "pending_review",
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
    query = db.query(BlindLabelWorkOrder).options(
        load_only(
            BlindLabelWorkOrder.id, BlindLabelWorkOrder.category_id,
            BlindLabelWorkOrder.assignment_limit, BlindLabelWorkOrder.candidate_snapshot,
        ),
        selectinload(BlindLabelWorkOrder.assignments).load_only(
            BlindLabelAssignment.id, BlindLabelAssignment.status,
            BlindLabelAssignment.completed_at, BlindLabelAssignment.task_reason_code,
        ).selectinload(BlindLabelAssignment.annotations).load_only(
            BlindLabelAnnotation.candidate_ref, BlindLabelAnnotation.verdict,
            BlindLabelAnnotation.reason_code,
        ),
        selectinload(BlindLabelWorkOrder.arbitrations).load_only(
            BlindLabelArbitration.candidate_ref, BlindLabelArbitration.verdict,
            # The shared consensus calculation also accesses these fields.
            BlindLabelArbitration.arbitrated_by, BlindLabelArbitration.reason,
        ),
    ).filter(
        BlindLabelWorkOrder.pool_state == POOL_STATE_ACTIVE,
        or_(
            BlindLabelWorkOrder.assignments.any(BlindLabelAssignment.status == "completed"),
            BlindLabelWorkOrder.arbitrations.any(),
        ),
    )
    if cursor:
        query = query.filter(BlindLabelWorkOrder.id > cursor)
    if category_id:
        query = query.filter(BlindLabelWorkOrder.category_id == category_id)
    batch = query.order_by(BlindLabelWorkOrder.id).limit(scan_limit + 1).all()
    orders = batch[:scan_limit]
    resolved = []
    knowledge_ids = set()
    for order in orders:
        consensus = consensus_for_work_order(db, order, order.assignments, order.arbitrations)
        if consensus["status"] not in {"unanimous", "arbitrated"}:
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
        knowledge_by_id = {
            item.id: item for item in db.query(Knowledge).options(
                load_only(Knowledge.id, Knowledge.title, Knowledge.content, Knowledge.status, Knowledge.updated_at)
            ).filter(Knowledge.id.in_(knowledge_ids)).all()
        }
    items = [
        item for order, consensus, snapshots in resolved
        if (item := _preview_item(order, consensus, snapshots, knowledge_by_id)) is not None
    ]
    return {
        "schema_version": 1,
        "scanned": len(orders),
        "next_cursor": orders[-1].id if len(batch) > scan_limit else None,
        "items": items,
    }
