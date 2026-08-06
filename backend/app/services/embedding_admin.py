from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models.embedding_admin import (
    EmbeddingRuntimeConfig,
    EmbeddingTrainingSample,
)
from app.models.integration import RetrievalQualityEvent
from app.models.knowledge import (
    Knowledge,
    KnowledgeDeduplicationFeedback,
)
from app.services.knowledge_dedup import _content_to_text, build_embedding_text


def next_runtime_config_version(db: Session) -> int:
    current = db.query(func.max(EmbeddingRuntimeConfig.version)).scalar()
    return int(current or 0) + 1


def activate_runtime_config(
    db: Session,
    record: EmbeddingRuntimeConfig,
    *,
    username: str,
) -> None:
    now = datetime.utcnow()
    (
        db.query(EmbeddingRuntimeConfig)
        .filter(
            EmbeddingRuntimeConfig.status == "active",
            EmbeddingRuntimeConfig.id != record.id,
        )
        .update(
            {
                EmbeddingRuntimeConfig.status: "archived",
            },
            synchronize_session=False,
        )
    )
    record.status = "active"
    record.activated_by = username
    record.activated_at = now


def knowledge_training_text(item: Knowledge) -> str:
    return build_embedding_text(
        item.title,
        item.subtitles,
        item.content,
        item.applicable_scenes,
    )


def _create_source_sample(
    db: Session,
    *,
    task_type: str,
    query_text: str,
    positive_text: str,
    negative_texts: list[str],
    source_type: str,
    source_id: str,
    reason: str,
    metadata: dict[str, Any],
    created_by: str,
) -> bool:
    if (
        db.query(EmbeddingTrainingSample)
        .filter(
            EmbeddingTrainingSample.source_type == source_type,
            EmbeddingTrainingSample.source_id == source_id,
        )
        .first()
    ):
        return False
    db.add(
        EmbeddingTrainingSample(
            id=f"ets-{uuid.uuid4().hex[:16]}",
            task_type=task_type,
            query_text=query_text.strip(),
            positive_text=positive_text.strip(),
            negative_texts=[
                value.strip()
                for value in negative_texts
                if isinstance(value, str) and value.strip()
            ],
            source_type=source_type,
            source_id=source_id,
            status="candidate",
            reason=reason.strip(),
            sample_metadata=metadata,
            created_by=created_by,
        )
    )
    db.flush()
    return True


def import_retrieval_samples(db: Session, *, created_by: str) -> int:
    imported = 0
    events = (
        db.query(RetrievalQualityEvent)
        .filter(
            RetrievalQualityEvent.review_status == "confirmed",
            RetrievalQualityEvent.training_eligible.is_(True),
            RetrievalQualityEvent.expected_knowledge_id.is_not(None),
        )
        .order_by(RetrievalQualityEvent.created_at)
        .all()
    )
    for event in events:
        expected = (
            db.query(Knowledge)
            .filter(Knowledge.id == event.expected_knowledge_id)
            .first()
        )
        if not expected:
            continue
        negatives: list[str] = []
        for candidate in event.candidate_snapshot or []:
            if not isinstance(candidate, dict):
                continue
            knowledge_id = str(candidate.get("knowledge_id") or "").strip()
            if not knowledge_id or knowledge_id == expected.id:
                continue
            item = db.query(Knowledge).filter(Knowledge.id == knowledge_id).first()
            if item:
                negatives.append(knowledge_training_text(item))
        if _create_source_sample(
            db,
            task_type="retrieval",
            query_text=event.query_text,
            positive_text=knowledge_training_text(expected),
            negative_texts=negatives,
            source_type="retrieval_event",
            source_id=event.id,
            reason=event.failure_reason or "人工确认的召回反馈",
            metadata={
                "expected_knowledge_id": expected.id,
                "selected_knowledge_id": event.selected_knowledge_id,
                "selection_status": event.selection_status,
                "threshold_status": event.threshold_status,
                "embedding_model": event.embedding_model,
            },
            created_by=created_by,
        ):
            imported += 1
    db.commit()
    return imported


def import_deduplication_samples(db: Session, *, created_by: str) -> int:
    imported = 0
    feedbacks = (
        db.query(KnowledgeDeduplicationFeedback)
        .filter(KnowledgeDeduplicationFeedback.verdict == "different")
        .order_by(KnowledgeDeduplicationFeedback.created_at)
        .all()
    )
    for feedback in feedbacks:
        item = db.query(Knowledge).filter(Knowledge.id == feedback.knowledge_id).first()
        matched = (
            db.query(Knowledge)
            .filter(Knowledge.id == feedback.matched_knowledge_id)
            .first()
        )
        if not item or not matched:
            continue
        query_text = item.title.strip()
        positive_text = _content_to_text(item.content)
        if not query_text or not positive_text:
            continue
        if _create_source_sample(
            db,
            task_type="deduplication",
            query_text=query_text,
            positive_text=positive_text,
            negative_texts=[knowledge_training_text(matched)],
            source_type="deduplication_feedback",
            source_id=feedback.id,
            reason=feedback.reason,
            metadata={
                "knowledge_id": item.id,
                "matched_knowledge_id": matched.id,
                "verdict": feedback.verdict,
            },
            created_by=created_by,
        ):
            imported += 1
    db.commit()
    return imported


def training_prompt(task_type: str) -> str:
    if task_type == "deduplication":
        return "判断待审核知识与已有知识是否表达同一业务规则，并优先区分适用范围或条件不同的内容"
    return "检索能够准确回答用户问题的已发布知识"


def build_training_dataset(
    samples: list[EmbeddingTrainingSample],
) -> tuple[list[dict[str, Any]], str, dict[str, int]]:
    dataset: list[dict[str, Any]] = []
    counts = {"train": 0, "validation": 0, "test": 0}
    for sample in samples:
        stable_key = sample.source_id or sample.id
        bucket = int(hashlib.sha256(stable_key.encode("utf-8")).hexdigest()[:8], 16) % 100
        split = "train" if bucket < 80 else ("validation" if bucket < 90 else "test")
        counts[split] += 1
        dataset.append(
            {
                "id": sample.id,
                "task_type": sample.task_type,
                "split": split,
                "messages": [{"role": "user", "content": sample.query_text}],
                "positive": [sample.positive_text],
                "negative": list(sample.negative_texts or []),
                "prompt": training_prompt(sample.task_type),
                "metadata": {
                    "source_type": sample.source_type,
                    "source_id": sample.source_id,
                    **(
                        sample.sample_metadata
                        if isinstance(sample.sample_metadata, dict)
                        else {}
                    ),
                },
            }
        )
    serialized = json.dumps(
        dataset,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return dataset, hashlib.sha256(serialized.encode("utf-8")).hexdigest(), counts
