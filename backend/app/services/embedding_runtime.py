from __future__ import annotations

from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.embedding_admin import EmbeddingRuntimeConfig


def default_embedding_runtime_config() -> dict[str, Any]:
    return {
        "dedup_block_threshold": settings.DEDUP_BLOCK_THRESHOLD,
        "dedup_review_threshold": settings.DEDUP_REVIEW_THRESHOLD,
        "dedup_max_candidates": settings.DEDUP_MAX_CANDIDATES,
        "dedup_min_semantic_content_chars": settings.DEDUP_MIN_SEMANTIC_CONTENT_CHARS,
        "dedup_min_containment_content_chars": settings.DEDUP_MIN_CONTAINMENT_CONTENT_CHARS,
        "search_chunk_size": settings.SEARCH_CHUNK_SIZE,
        "search_chunk_overlap": settings.SEARCH_CHUNK_OVERLAP,
        "retrieval_score_threshold": 0.42,
        "retrieval_headquarters_standard_top_k": 5,
        "retrieval_business_accumulation_top_k": 5,
        "retrieval_default_top_k": 10,
        "training_min_verified_samples": 20,
        "training_trigger_new_samples": 100,
        "training_schedule_days": 7,
        "minimum_recall_at_10": 0.8,
        "maximum_false_block_rate": 0.01,
    }


def get_active_runtime_record(db: Session) -> EmbeddingRuntimeConfig | None:
    try:
        return (
            db.query(EmbeddingRuntimeConfig)
            .filter(EmbeddingRuntimeConfig.status == "active")
            .order_by(
                EmbeddingRuntimeConfig.activated_at.desc(),
                EmbeddingRuntimeConfig.version.desc(),
            )
            .first()
        )
    except SQLAlchemyError:
        # Unit tests and rolling deployments can briefly run against the
        # pre-console schema. Existing environment defaults remain safe.
        db.rollback()
        return None


def get_active_runtime_values(db: Session) -> dict[str, Any]:
    values = default_embedding_runtime_config()
    record = get_active_runtime_record(db)
    if record and isinstance(record.config, dict):
        values.update(record.config)
    return values
