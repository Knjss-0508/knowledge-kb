import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.core.integration_auth import require_integration_key
from app.models.integration import IntegrationIngestion, RetrievalQualityEvent
from app.models.knowledge import Category, Knowledge, KnowledgeStatus, TagDimension
from app.models.user import User
from app.routes.auth import get_current_user, require_permission
from app.routes.knowledge import _generate_knowledge_id, _normalize_content
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.knowledge_dedup import (
    DedupDecision,
    check_duplicate,
    ensure_search_embeddings,
    save_embedding,
)
from app.schemas.integration import (
    IntegrationCandidateBatch,
    IntegrationCandidateBatchResponse,
    IntegrationCandidateResult,
    IntegrationDedupCheckRequest,
    IntegrationDedupMatch,
    IntegrationDedupResponse,
    IntegrationIngestionResponse,
    IntegrationTaxonomyResponse,
    RetrievalQualityEventBatch,
    RetrievalQualityEventBatchResponse,
    RetrievalQualityEventResult,
)
from app.schemas.knowledge import CategoryResponse, TagDimensionResponse, TagValueResponse


router = APIRouter(prefix="/integration", tags=["自动化接入"])

TAXONOMY_VERSION = "automation-v3"


def _to_dedup_response(decision: DedupDecision) -> IntegrationDedupResponse:
    return IntegrationDedupResponse(
        action=decision.action,
        embedding_model=settings.EMBEDDING_MODEL,
        content_hash=decision.content_hash,
        block_threshold=settings.DEDUP_BLOCK_THRESHOLD,
        review_threshold=settings.DEDUP_REVIEW_THRESHOLD,
        matches=[
            IntegrationDedupMatch(
                knowledge_id=match.knowledge_id,
                title=match.title,
                status=match.status,
                category_id=match.category_id,
                match_type=match.match_type,
                similarity=match.similarity,
                title_similarity=match.title_similarity,
                content_similarity=match.content_similarity,
            )
            for match in decision.matches
        ],
    )


def _to_ingestion_response(item: IntegrationIngestion) -> IntegrationIngestionResponse:
    return IntegrationIngestionResponse(
        id=item.id,
        event_id=item.event_id,
        idempotency_key=item.idempotency_key,
        source_system=item.source_system,
        source_conversation_id=item.source_conversation_id,
        status=item.status,
        knowledge_id=item.knowledge_id,
        error_code=item.error_code,
        error_message=item.error_message,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _resolve_retrieval_outcome(candidate) -> str:
    if candidate.candidate_count == 0:
        return "no_candidates"
    if candidate.top_rerank_score < candidate.score_threshold:
        return "low_score"
    if not candidate.selected:
        return "not_selected"
    return "accepted"


@router.post(
    "/retrieval-events:batch",
    response_model=RetrievalQualityEventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_retrieval_quality_events(
    body: RetrievalQualityEventBatch,
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    results: list[RetrievalQualityEventResult] = []
    recorded = reused = 0

    for candidate in body.items:
        existing = (
            db.query(RetrievalQualityEvent)
            .filter(RetrievalQualityEvent.idempotency_key == candidate.idempotency_key)
            .first()
        )
        if existing:
            reused += 1
            results.append(
                RetrievalQualityEventResult(
                    idempotency_key=existing.idempotency_key,
                    status="reused",
                    outcome=existing.outcome,
                    event_id=existing.id,
                )
            )
            continue

        outcome = _resolve_retrieval_outcome(candidate)
        event = RetrievalQualityEvent(
            id=f"rqe-{uuid.uuid4().hex[:12]}",
            idempotency_key=candidate.idempotency_key,
            source_system=candidate.source_system,
            conversation_id=candidate.conversation_id,
            query_text=candidate.query,
            candidate_count=candidate.candidate_count,
            top_knowledge_id=candidate.top_knowledge_id,
            top_rerank_score=candidate.top_rerank_score,
            score_threshold=candidate.score_threshold,
            selected=candidate.selected,
            outcome=outcome,
            event_metadata=candidate.metadata,
        )
        db.add(event)
        recorded += 1
        results.append(
            RetrievalQualityEventResult(
                idempotency_key=event.idempotency_key,
                status="recorded",
                outcome=outcome,
                event_id=event.id,
            )
        )

    db.commit()
    return RetrievalQualityEventBatchResponse(
        recorded=recorded,
        reused=reused,
        results=results,
    )


@router.get("/retrieval-analytics")
def get_retrieval_analytics(
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:view")),
):
    summary = {"total": 0, "accepted": 0, "low_score": 0, "no_candidates": 0, "not_selected": 0}
    for outcome, total in (
        db.query(RetrievalQualityEvent.outcome, func.count(RetrievalQualityEvent.id))
        .group_by(RetrievalQualityEvent.outcome)
        .all()
    ):
        summary[outcome] = total

    risks = (
        db.query(RetrievalQualityEvent)
        .filter(RetrievalQualityEvent.outcome != "accepted")
        .order_by(RetrievalQualityEvent.created_at.desc())
        .limit(50)
        .all()
    )
    summary["total"] = sum(
        summary[key] for key in ("accepted", "low_score", "no_candidates", "not_selected")
    )
    return {
        "summary": summary,
        "risks": [
            {
                "id": event.id,
                "source_system": event.source_system,
                "query": event.query_text,
                "candidate_count": event.candidate_count,
                "top_knowledge_id": event.top_knowledge_id,
                "top_rerank_score": event.top_rerank_score,
                "score_threshold": event.score_threshold,
                "selected": event.selected,
                "outcome": event.outcome,
                "created_at": event.created_at,
            }
            for event in risks
        ],
    }


@router.get("/taxonomy", response_model=IntegrationTaxonomyResponse)
def get_taxonomy(
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    categories = db.query(Category).order_by(Category.level, Category.sort_order).all()
    dimensions = db.query(TagDimension).all()
    return IntegrationTaxonomyResponse(
        version=TAXONOMY_VERSION,
        categories=[CategoryResponse.model_validate(item) for item in categories],
        tag_dimensions=[
            TagDimensionResponse(
                id=dimension.id,
                name=dimension.name,
                values=[
                    TagValueResponse(
                        id=value.id,
                        dimension_id=value.dimension_id,
                        value=value.value,
                    )
                    for value in dimension.values
                ],
            )
            for dimension in dimensions
        ],
    )


@router.post(
    "/knowledge-dedup:check",
    response_model=IntegrationDedupResponse,
)
def check_knowledge_deduplication(
    body: IntegrationDedupCheckRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    """Optional upstream pre-check. Final deduplication is always repeated on submission."""
    try:
        decision = check_duplicate(
            db,
            title=body.knowledge.title,
            subtitles=body.knowledge.subtitles,
            content=_normalize_content(body.knowledge.content),
            scene_tags=body.knowledge.scene_tags,
            exclude_knowledge_id=body.exclude_knowledge_id,
        )
        db.commit()
    except EmbeddingServiceUnavailable as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Embedding service is unavailable; deduplication cannot be completed: {exc}",
        )
    except ValueError as exc:
        db.rollback()
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    return _to_dedup_response(decision)


@router.post(
    "/knowledge-candidates:batch",
    response_model=IntegrationCandidateBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_knowledge_candidates(
    body: IntegrationCandidateBatch,
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    results: list[IntegrationCandidateResult] = []
    accepted = rejected = reused = 0

    for candidate in body.items:
        existing = (
            db.query(IntegrationIngestion)
            .filter(IntegrationIngestion.idempotency_key == candidate.idempotency_key)
            .first()
        )
        if existing:
            reused += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="reused",
                    ingestion_id=existing.id,
                    knowledge_id=existing.knowledge_id,
                    error_code=existing.error_code,
                    error_message=existing.error_message,
                )
            )
            continue

        category = (
            db.query(Category)
            .filter(Category.id == candidate.knowledge.category_id)
            .first()
        )
        if not category:
            rejected += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="rejected",
                    error_code="CATEGORY_NOT_FOUND",
                    error_message="category_id does not exist in the current taxonomy.",
                )
            )
            continue

        if not candidate.selection.eligible:
            rejected += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="rejected",
                    error_code="CANDIDATE_NOT_ELIGIBLE",
                    error_message="Candidate was marked ineligible by the upstream selector.",
                )
            )
            continue

        try:
            decision = check_duplicate(
                db,
                title=candidate.knowledge.title,
                subtitles=candidate.knowledge.subtitles,
                content=_normalize_content(candidate.knowledge.content),
                scene_tags=candidate.knowledge.scene_tags,
            )
        except EmbeddingServiceUnavailable as exc:
            rejected += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="rejected",
                    error_code="DEDUP_UNAVAILABLE",
                    error_message=f"Embedding service is unavailable; candidate was not ingested: {exc}",
                )
            )
            continue
        except ValueError as exc:
            rejected += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="rejected",
                    error_code="DEDUP_INVALID_CONTENT",
                    error_message=str(exc),
                )
            )
            continue

        deduplication = _to_dedup_response(decision)
        if decision.action == "block_duplicate":
            rejected += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="rejected",
                    error_code="DUPLICATE_BLOCKED",
                    error_message="Candidate matches an existing knowledge item and was not ingested.",
                    deduplication=deduplication,
                )
            )
            continue

        knowledge = Knowledge(
            id=_generate_knowledge_id(db),
            title=candidate.knowledge.title,
            subtitles=candidate.knowledge.subtitles,
            content=_normalize_content(candidate.knowledge.content),
            category_id=candidate.knowledge.category_id,
            status=KnowledgeStatus.REVIEW,
            source="automation",
            source_session_id=candidate.source.conversation_id,
            quality_score=candidate.selection.confidence,
            applicable_scenes=candidate.knowledge.scene_tags,
            applicable_categories=candidate.knowledge.applicable_categories,
            applicable_brands=candidate.knowledge.applicable_brands,
            applicable_models=candidate.knowledge.applicable_models,
            deduplication_metadata=deduplication.model_dump(mode="json"),
            created_by=f"automation:{candidate.source.system}"[:128],
        )
        db.add(knowledge)
        db.flush()
        if decision.embedding:
            save_embedding(
                db,
                knowledge=knowledge,
                content_hash=decision.content_hash,
                embedding=decision.embedding,
                title_embedding=decision.title_embedding,
                content_embedding=decision.content_embedding,
            )
        ensure_search_embeddings(db, knowledge)

        ingestion = IntegrationIngestion(
            id=f"ing-{uuid.uuid4().hex[:12]}",
            event_id=candidate.event_id,
            idempotency_key=candidate.idempotency_key,
            source_system=candidate.source.system,
            source_conversation_id=candidate.source.conversation_id,
            source_conversation_url=candidate.source.conversation_url,
            source_message_ids=candidate.source.message_ids,
            redaction_status=candidate.source.redaction_status,
            processing_metadata=candidate.processing.model_dump(mode="json"),
            selection_metadata={
                **candidate.selection.model_dump(mode="json"),
                "evidence_excerpt": candidate.knowledge.evidence_excerpt,
                "deduplication": deduplication.model_dump(mode="json"),
            },
            status="review_duplicate" if decision.action == "review_duplicate" else "review_submitted",
            knowledge_id=knowledge.id,
        )
        db.add(ingestion)
        accepted += 1
        results.append(
            IntegrationCandidateResult(
                event_id=candidate.event_id,
                idempotency_key=candidate.idempotency_key,
                status="review_submitted",
                ingestion_id=ingestion.id,
                knowledge_id=knowledge.id,
                deduplication=deduplication,
            )
        )

    db.commit()
    return IntegrationCandidateBatchResponse(
        accepted=accepted,
        rejected=rejected,
        reused=reused,
        results=results,
    )


@router.get(
    "/ingestions/{ingestion_id}",
    response_model=IntegrationIngestionResponse,
)
def get_ingestion(
    ingestion_id: str,
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    item = db.query(IntegrationIngestion).filter(IntegrationIngestion.id == ingestion_id).first()
    if not item:
        raise HTTPException(status_code=404, detail="Ingestion not found.")
    return _to_ingestion_response(item)
