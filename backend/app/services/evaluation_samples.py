from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime
from enum import Enum
from typing import Any

from sqlalchemy.orm import Session

from app.models.integration import EvaluationSample, IntegrationIngestion
from app.models.knowledge import Knowledge, KnowledgeStatus
from app.services.candidate_review import current_draft_version
from app.services.confidence_training import confidence_band, item_payload


def stable_topic_split(topic_group: str) -> str:
    """Assign an entire topic group to a deterministic 60/20/20 split."""
    digest = hashlib.sha256(
        f"confidence-topic-split-v1:{topic_group.strip().casefold()}".encode("utf-8")
    ).hexdigest()
    bucket = int(digest[:8], 16) % 10
    if bucket < 6:
        return "train"
    if bucket < 8:
        return "validation"
    return "test"


def _stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _value(value: Any) -> str:
    if isinstance(value, Enum):
        return str(value.value)
    return str(value or "")


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _topic_group(row: IntegrationIngestion, knowledge: dict[str, Any]) -> str:
    selection = dict(row.selection_metadata or {})
    processing = dict(row.processing_metadata or {})
    payload = dict(row.candidate_payload or {})
    candidates = (
        selection.get("topic_group_id"),
        selection.get("topic_group"),
        selection.get("topic_id"),
        selection.get("cluster_id"),
        selection.get("cluster_key"),
        processing.get("topic_group_id"),
        processing.get("topic_id"),
        payload.get("topic_group_id"),
        payload.get("topic_id"),
        knowledge.get("topic_group_id"),
        knowledge.get("topic_id"),
        knowledge.get("source_topic_key"),
    )
    for candidate in candidates:
        normalized = str(candidate or "").strip()
        if normalized:
            return normalized[:512]
    normalized_title = re.sub(
        r"[^\w]+",
        "",
        str(knowledge.get("title") or "").casefold(),
    )
    if normalized_title:
        return "title:" + normalized_title[:506]
    # A conversation is a conservative fallback when no topic identifier or
    # usable title was captured.
    return str(row.source_conversation_id or row.event_id or row.id).strip()[:512]


def _source_batch(row: IntegrationIngestion) -> str:
    processing = dict(row.processing_metadata or {})
    selection = dict(row.selection_metadata or {})
    payload = dict(row.candidate_payload or {})
    for value in (
        processing.get("source_batch"),
        processing.get("batch_id"),
        selection.get("source_batch"),
        selection.get("batch_id"),
        payload.get("source_batch"),
    ):
        normalized = str(value or "").strip()
        if normalized:
            return normalized[:256]
    return str(row.event_id or "").strip()[:256]


def _published_knowledge_snapshot(
    row: IntegrationIngestion,
    published_knowledge_by_id: dict[str, Knowledge],
) -> dict[str, Any]:
    if not row.knowledge_id:
        return {}
    knowledge = published_knowledge_by_id.get(str(row.knowledge_id))
    if not knowledge:
        return {}
    published_content = deepcopy(knowledge.content)
    published_reply = (
        str(published_content.get("recommended_reply") or "")
        if isinstance(published_content, dict)
        else ""
    )
    if isinstance(published_content, dict):
        published_content.pop("recommended_reply", None)
    return {
        "knowledge_id": knowledge.id,
        "title": str(knowledge.title or ""),
        "content": published_content,
        "recommended_reply": published_reply,
        "published_at": _parse_datetime(knowledge.updated_at).isoformat()
        if _parse_datetime(knowledge.updated_at)
        else "",
    }


def _snapshot_values(
    row: IntegrationIngestion,
    published_knowledge_by_id: dict[str, Knowledge],
) -> dict[str, Any] | None:
    source = dict(row.candidate_payload or {})
    knowledge = dict(source.get("knowledge") or {})
    source_metadata = dict(row.review_metadata or {})
    human = dict(source.get("human_review") or source_metadata.get("human_review") or {})
    model = dict(source.get("model_review") or source_metadata.get("model_review") or {})
    current = item_payload(row)
    if current.get("truth_status") != "confirmed":
        return None

    draft_snapshot = {
        "title": str(knowledge.get("title") or ""),
        "content": knowledge.get("content"),
        "recommended_reply": str(knowledge.get("recommended_reply") or ""),
        "evidence_excerpt": str(
            knowledge.get("evidence_excerpt")
            or dict(row.selection_metadata or {}).get("evidence_excerpt")
            or ""
        ),
        "answer_hub_replay_input": dict(
            (row.selection_metadata or {}).get("answer_hub_replay_input")
            or (source.get("selection") or {}).get("answer_hub_replay_input")
            or {}
        ),
    }
    history = list(source_metadata.get("draft_revision_history") or [])
    draft_hash = hashlib.sha256(_stable_json(draft_snapshot).encode("utf-8")).hexdigest()[:12]
    draft_version = f"draft-{len(history)}-{draft_hash}"
    topic_group = _topic_group(row, knowledge)
    reviewed_at = _parse_datetime(
        row.reviewed_at or human.get("reviewed_at") or human.get("updated_at")
    )
    human_truth = {
        "knowledge_value": current.get("human_label"),
        "draft_disposition": current.get("human_draft_disposition"),
        "draft_version": current_draft_version(source_metadata),
        "draft_version_source": (
            "human_review_draft_version"
            if "human_review_draft_version" in source_metadata
            else "legacy_v1"
        ),
        "reviewed_by": str(
            row.reviewed_by or human.get("reviewer") or human.get("reviewed_by") or ""
        ),
        "reviewed_at": reviewed_at.isoformat() if reviewed_at else "",
    }
    published_snapshot = _published_knowledge_snapshot(
        row,
        published_knowledge_by_id,
    )
    return {
        "candidate_id": row.id,
        "source_batch": _source_batch(row),
        "topic_group": topic_group,
        "draft_version": draft_version,
        "draft_snapshot": draft_snapshot,
        "published_knowledge_snapshot": published_snapshot,
        "draft_revision_history": history,
        "model_output": model,
        "model_version": str(
            model.get("resolved_model_version")
            or model.get("model_version")
            or model.get("model_name")
            or model.get("provider")
            or ""
        ),
        "prompt_version": str(model.get("prompt_version") or ""),
        "human_truth": human_truth,
        "reviewed_at": reviewed_at,
        "dataset_split": stable_topic_split(topic_group),
    }


def _latest_snapshot_id(db: Session) -> str | None:
    latest = (
        db.query(EvaluationSample.snapshot_id)
        .order_by(EvaluationSample.created_at.desc(), EvaluationSample.id.desc())
        .first()
    )
    return str(latest[0]) if latest else None


def freeze_evaluation_snapshot(
    db: Session,
    source_rows: list[IntegrationIngestion] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Append a complete immutable snapshot when the confirmed source set changes."""
    if source_rows is None:
        source_rows = (
            db.query(IntegrationIngestion)
            .filter(
                IntegrationIngestion.review_status.isnot(None),
                IntegrationIngestion.source_system != "excel",
            )
            .order_by(IntegrationIngestion.id.asc())
            .all()
        )
    knowledge_ids = {
        str(row.knowledge_id)
        for row in source_rows
        if row.knowledge_id
    }
    published_knowledge_by_id = {
        str(knowledge.id): knowledge
        for knowledge in (
            db.query(Knowledge)
            .filter(
                Knowledge.id.in_(knowledge_ids),
                Knowledge.status == KnowledgeStatus.PUBLISHED,
            )
            .all()
            if knowledge_ids
            else []
        )
    }
    values = [
        value
        for row in source_rows
        if (
            value := _snapshot_values(
                row,
                published_knowledge_by_id,
            )
        )
        is not None
    ]
    values.sort(key=lambda item: str(item["candidate_id"]))
    if not values:
        latest_id = _latest_snapshot_id(db)
        return {
            "snapshot_id": latest_id,
            "sample_count": 0,
            "created": False,
            "confirmed_source_count": 0,
        }

    snapshot_hash = hashlib.sha256(_stable_json(values).encode("utf-8")).hexdigest()
    snapshot_id = "evs-" + snapshot_hash[:32]
    existing = (
        db.query(EvaluationSample.id)
        .filter(EvaluationSample.snapshot_id == snapshot_id)
        .first()
    )
    if existing:
        return {
            "snapshot_id": snapshot_id,
            "sample_count": len(values),
            "created": False,
            "confirmed_source_count": len(values),
        }

    created_at = now or datetime.utcnow()
    for value in values:
        sample_hash = hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()
        sample_id = "evs-" + hashlib.sha256(
            f"{snapshot_id}:{value['candidate_id']}".encode("utf-8")
        ).hexdigest()[:28]
        db.add(EvaluationSample(
            id=sample_id,
            snapshot_id=snapshot_id,
            candidate_id=value["candidate_id"],
            source_batch=value["source_batch"],
            topic_group=value["topic_group"],
            draft_version=value["draft_version"],
            draft_snapshot=value["draft_snapshot"],
            published_knowledge_snapshot=value["published_knowledge_snapshot"],
            draft_revision_history=value["draft_revision_history"],
            model_output=value["model_output"],
            model_version=value["model_version"],
            prompt_version=value["prompt_version"],
            human_truth=value["human_truth"],
            reviewed_at=value["reviewed_at"],
            dataset_split=value["dataset_split"],
            sample_hash=sample_hash,
            created_at=created_at,
        ))
    db.commit()
    return {
        "snapshot_id": snapshot_id,
        "sample_count": len(values),
        "created": True,
        "confirmed_source_count": len(values),
    }


def latest_evaluation_samples(
    db: Session,
    *,
    snapshot_id: str | None = None,
    limit: int | None = None,
) -> list[EvaluationSample]:
    selected_snapshot_id = snapshot_id or _latest_snapshot_id(db)
    if not selected_snapshot_id:
        return []
    query = (
        db.query(EvaluationSample)
        .filter(EvaluationSample.snapshot_id == selected_snapshot_id)
        .order_by(EvaluationSample.candidate_id.asc())
    )
    if limit is not None:
        query = query.limit(limit)
    return query.all()


def evaluation_sample_payload(sample: EvaluationSample) -> dict[str, Any]:
    model = dict(sample.model_output or {})
    truth = dict(sample.human_truth or {})
    human_label = str(truth.get("knowledge_value") or "")
    model_label = str(model.get("knowledge_value") or model.get("decision") or "")
    model_draft = {
        "submit_for_human_review": "approved",
        "return_for_revision": "revision_required",
        "hold_for_evidence": "hold_for_evidence",
    }.get(str(model.get("suggested_action") or ""))
    if not model_draft:
        model_draft = {
            "approved": "approved",
            "revision_required": "revision_required",
            "hold_for_evidence": "hold_for_evidence",
        }.get(str(model.get("draft_quality") or ""))
    human_draft = str(truth.get("draft_disposition") or "")
    same_value = bool(model_label and human_label and model_label == human_label)
    same_draft = human_label == "unworthy" or model_draft == human_draft
    strict_correct = same_value and same_draft
    return {
        "id": sample.candidate_id,
        "candidate_id": sample.candidate_id,
        "evaluation_sample_id": sample.id,
        "snapshot_id": sample.snapshot_id,
        "source_batch": sample.source_batch,
        "event_id": sample.source_batch,
        "topic_group": sample.topic_group,
        "draft_version": sample.draft_version,
        "title": str((sample.draft_snapshot or {}).get("title") or ""),
        "model_label": model_label or None,
        "model_confidence": model.get("confidence"),
        "confidence_band": confidence_band(model.get("confidence")),
        "model_version": sample.model_version,
        "prompt_version": sample.prompt_version,
        "model_output": model,
        "human_truth": truth,
        "human_label": human_label or None,
        "human_draft_disposition": human_draft or None,
        "model_draft_disposition": model_draft,
        "truth_status": "confirmed",
        "model_correctness": "strict_correct" if strict_correct else "wrong",
        "strict_correct": strict_correct,
        "acceptable_correct": same_value,
        "error_type": [],
        "error_severity": None,
        "review_note": "",
        "correction_status": "not_started",
        "correction_round": 0,
        "corrected_model_review": None,
        "training_candidate_status": "not_recommended",
        "dataset_split": sample.dataset_split,
        "review_status": "frozen",
        "reviewed_at": sample.reviewed_at.isoformat() if sample.reviewed_at else None,
    }
