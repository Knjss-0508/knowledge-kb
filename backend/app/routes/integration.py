import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
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
    CandidateReviewBatchSubmit,
    CandidateReviewBatchSubmitResponse,
    CandidateReviewListItem,
    CandidateReviewListResponse,
    CandidateReviewSubmitResult,
    CandidateReviewUpdate,
    IntegrationCandidate,
    IntegrationCandidateBatch,
    IntegrationCandidateBatchResponse,
    IntegrationCandidateQueueBatchResponse,
    IntegrationCandidateQueueResult,
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
from app.services.candidate_review import (
    evaluate_review_status,
    normalize_human_review,
)


router = APIRouter(prefix="/integration", tags=["自动化接入"])
logger = logging.getLogger(__name__)

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


def _candidate_review_item(item: IntegrationIngestion) -> CandidateReviewListItem:
    payload = dict(item.candidate_payload or {})
    knowledge = dict(payload.get("knowledge") or {})
    selection = dict(payload.get("selection") or item.selection_metadata or {})
    review_metadata = dict(item.review_metadata or {})
    model_review = dict(payload.get("model_review") or review_metadata.get("model_review") or {})
    human_review = normalize_human_review(
        payload.get("human_review") or review_metadata.get("human_review") or {}
    )
    return CandidateReviewListItem(
        id=item.id,
        event_id=item.event_id,
        source_system=item.source_system,
        source_conversation_id=item.source_conversation_id,
        source_conversation_url=item.source_conversation_url,
        review_status=item.review_status or "pending",
        status=item.status,
        title=str(knowledge.get("title") or ""),
        subtitles=list(knowledge.get("subtitles") or []),
        content=knowledge.get("content") or {"blocks": []},
        category_id=str(knowledge.get("category_id") or ""),
        applicable_scenes=list(knowledge.get("scene_tags") or []),
        applicable_categories=list(knowledge.get("applicable_categories") or []),
        applicable_brands=list(knowledge.get("applicable_brands") or []),
        applicable_models=list(knowledge.get("applicable_models") or []),
        recommended_reply=knowledge.get("recommended_reply"),
        evidence_excerpt=knowledge.get("evidence_excerpt"),
        selection=selection,
        model_review=model_review,
        human_review=human_review,
        priority_review=bool(model_review.get("priority_review")),
        knowledge_id=item.knowledge_id,
        error_code=item.error_code,
        error_message=item.error_message,
        reviewed_by=item.reviewed_by,
        reviewed_at=item.reviewed_at,
        submitted_at=item.submitted_at,
        created_at=item.created_at,
        updated_at=item.updated_at,
    )


def _candidate_content(knowledge_payload: dict[str, Any]) -> Any:
    content = _normalize_content(knowledge_payload.get("content"))
    recommended_reply = str(knowledge_payload.get("recommended_reply") or "").strip()
    if recommended_reply and isinstance(content, dict):
        content = dict(content)
        content["recommended_reply"] = recommended_reply
    return content


def _candidate_queue_state(
    candidate: IntegrationCandidate,
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], str]:
    selection = candidate.selection.model_dump(mode="json")
    model_review = (
        candidate.model_review.model_dump(mode="json")
        if candidate.model_review
        else {}
    )
    human_review = normalize_human_review(
        candidate.human_review.model_dump(mode="json")
        if candidate.human_review
        else {}
    )
    review_status, eligible, reason = evaluate_review_status(selection, human_review)
    selection["eligible"] = eligible
    selection["review_reason"] = reason

    payload = candidate.model_dump(mode="json")
    payload["selection"] = selection
    payload["human_review"] = human_review
    review_metadata = {
        "model_review": model_review,
        "human_review": human_review,
    }
    return payload, selection, review_metadata, review_status


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


@router.post(
    "/knowledge-review-candidates:batch",
    response_model=IntegrationCandidateQueueBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def queue_knowledge_review_candidates(
    body: IntegrationCandidateBatch,
    db: Session = Depends(get_db),
    _: None = Depends(require_integration_key),
):
    results: list[IntegrationCandidateQueueResult] = []
    queued = ready = rejected = reused = 0

    for candidate in body.items:
        payload, selection, review_metadata, review_status = _candidate_queue_state(
            candidate
        )
        existing = (
            db.query(IntegrationIngestion)
            .filter(IntegrationIngestion.idempotency_key == candidate.idempotency_key)
            .first()
        )
        if existing:
            if existing.knowledge_id and existing.review_status is None:
                existing.candidate_payload = payload
                existing.review_metadata = review_metadata
                existing.review_status = "submitted"
                existing.submitted_at = existing.submitted_at or existing.created_at
            elif existing.reviewed_at is None:
                existing.event_id = candidate.event_id
                existing.source_system = candidate.source.system
                existing.source_conversation_id = candidate.source.conversation_id
                existing.source_conversation_url = candidate.source.conversation_url
                existing.source_message_ids = candidate.source.message_ids
                existing.redaction_status = candidate.source.redaction_status
                existing.processing_metadata = candidate.processing.model_dump(mode="json")
                existing.selection_metadata = selection
                existing.candidate_payload = payload
                existing.review_metadata = review_metadata
                existing.review_status = review_status
                existing.status = f"candidate_{review_status}"
                existing.error_code = None
                existing.error_message = None
            reused += 1
            results.append(
                IntegrationCandidateQueueResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="reused",
                    ingestion_id=existing.id,
                    review_status=(
                        existing.review_status
                        or ("submitted" if existing.knowledge_id else "pending")
                    ),
                )
            )
            continue

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
            selection_metadata=selection,
            candidate_payload=payload,
            review_metadata=review_metadata,
            review_status=review_status,
            status=f"candidate_{review_status}",
        )
        db.add(ingestion)
        if review_status == "ready":
            ready += 1
            result_status = "ready"
        elif review_status == "rejected":
            rejected += 1
            result_status = "rejected"
        else:
            queued += 1
            result_status = "queued"
        results.append(
            IntegrationCandidateQueueResult(
                event_id=candidate.event_id,
                idempotency_key=candidate.idempotency_key,
                status=result_status,
                ingestion_id=ingestion.id,
                review_status=review_status,
            )
        )

    db.commit()
    return IntegrationCandidateQueueBatchResponse(
        queued=queued,
        ready=ready,
        rejected=rejected,
        reused=reused,
        results=results,
    )


@router.get(
    "/candidate-reviews",
    response_model=CandidateReviewListResponse,
)
def list_candidate_reviews(
    keyword: str = Query("", max_length=200),
    review_status: str = Query(""),
    priority_only: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    rows = (
        db.query(IntegrationIngestion)
        .filter(IntegrationIngestion.review_status.isnot(None))
        .order_by(IntegrationIngestion.created_at.desc())
        .all()
    )
    all_items = [_candidate_review_item(row) for row in rows]
    summary = {
        "total": len(all_items),
        "pending": sum(item.review_status == "pending" for item in all_items),
        "ready": sum(item.review_status == "ready" for item in all_items),
        "rejected": sum(item.review_status == "rejected" for item in all_items),
        "submitted": sum(item.review_status == "submitted" for item in all_items),
        "failed": sum(item.review_status == "failed" for item in all_items),
        "priority": sum(item.priority_review for item in all_items),
    }

    normalized_keyword = keyword.strip().lower()
    filtered = all_items
    if normalized_keyword:
        filtered = [
            item
            for item in filtered
            if normalized_keyword
            in " ".join(
                (
                    item.title,
                    item.event_id,
                    item.source_conversation_id,
                    item.evidence_excerpt or "",
                )
            ).lower()
        ]
    if review_status:
        filtered = [item for item in filtered if item.review_status == review_status]
    if priority_only:
        filtered = [item for item in filtered if item.priority_review]

    return CandidateReviewListResponse(
        total=len(filtered),
        summary=summary,
        items=filtered[offset : offset + limit],
    )


@router.patch(
    "/candidate-reviews/{ingestion_id}",
    response_model=CandidateReviewListItem,
)
def update_candidate_review(
    ingestion_id: str,
    body: CandidateReviewUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    item = (
        db.query(IntegrationIngestion)
        .filter(
            IntegrationIngestion.id == ingestion_id,
            IntegrationIngestion.review_status.isnot(None),
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Candidate review item not found.")
    if item.review_status == "submitted":
        raise HTTPException(status_code=409, detail="Submitted candidate cannot be edited.")

    payload = dict(item.candidate_payload or {})
    knowledge = dict(payload.get("knowledge") or {})
    updates = body.model_dump(exclude_unset=True)
    for field, payload_key in (
        ("title", "title"),
        ("subtitles", "subtitles"),
        ("content", "content"),
        ("category_id", "category_id"),
        ("applicable_scenes", "scene_tags"),
        ("applicable_categories", "applicable_categories"),
        ("applicable_brands", "applicable_brands"),
        ("applicable_models", "applicable_models"),
        ("recommended_reply", "recommended_reply"),
    ):
        if field in updates:
            knowledge[payload_key] = updates.pop(field)
    payload["knowledge"] = knowledge

    review_metadata = dict(item.review_metadata or {})
    human_review = dict(
        payload.get("human_review")
        or review_metadata.get("human_review")
        or {}
    )
    human_review.update(updates)
    human_review["reviewer"] = current_user.username
    human_review["reviewed_at"] = datetime.utcnow().isoformat()
    human_review = normalize_human_review(human_review)

    selection = dict(payload.get("selection") or item.selection_metadata or {})
    review_status, eligible, reason = evaluate_review_status(selection, human_review)
    selection["eligible"] = eligible
    selection["review_reason"] = reason
    payload["selection"] = selection
    payload["human_review"] = human_review

    item.candidate_payload = payload
    item.selection_metadata = selection
    item.review_metadata = {
        **review_metadata,
        "model_review": dict(payload.get("model_review") or review_metadata.get("model_review") or {}),
        "human_review": human_review,
    }
    item.review_status = review_status
    item.status = f"candidate_{review_status}"
    item.reviewed_by = current_user.username
    item.reviewed_at = datetime.utcnow()
    item.error_code = None
    item.error_message = None
    db.commit()
    db.refresh(item)
    return _candidate_review_item(item)


@router.post(
    "/candidate-reviews:batch-submit",
    response_model=CandidateReviewBatchSubmitResponse,
)
def submit_candidate_reviews(
    body: CandidateReviewBatchSubmit,
    db: Session = Depends(get_db),
    current_user: User = Depends(require_permission("knowledge:submit")),
):
    submitted = failed = reused = 0
    results: list[CandidateReviewSubmitResult] = []

    for ingestion_id in body.ingestion_ids:
        item = (
            db.query(IntegrationIngestion)
            .filter(IntegrationIngestion.id == ingestion_id)
            .first()
        )
        if not item:
            failed += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=ingestion_id,
                    status="failed",
                    error_code="CANDIDATE_NOT_FOUND",
                    error_message="Candidate review item not found.",
                )
            )
            continue
        if item.knowledge_id:
            reused += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=item.id,
                    status="reused",
                    knowledge_id=item.knowledge_id,
                )
            )
            continue
        if item.review_status != "ready":
            failed += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=item.id,
                    status="failed",
                    error_code="REVIEW_NOT_READY",
                    error_message="Candidate has not passed the knowledge value review gate.",
                )
            )
            continue

        try:
            candidate = IntegrationCandidate.model_validate(item.candidate_payload or {})
            category = (
                db.query(Category)
                .filter(Category.id == candidate.knowledge.category_id)
                .first()
            )
            if not category:
                raise ValueError("CATEGORY_NOT_FOUND")
            content = _candidate_content(candidate.knowledge.model_dump(mode="json"))
            decision = check_duplicate(
                db,
                title=candidate.knowledge.title,
                subtitles=candidate.knowledge.subtitles,
                content=content,
                scene_tags=candidate.knowledge.scene_tags,
            )
            deduplication = _to_dedup_response(decision)
            if decision.action == "block_duplicate":
                raise ValueError("DUPLICATE_BLOCKED")

            deduplication_metadata = deduplication.model_dump(mode="json")
            deduplication_metadata["candidate_review"] = {
                "ingestion_id": item.id,
                "model_review": dict((item.review_metadata or {}).get("model_review") or {}),
                "human_review": dict((item.review_metadata or {}).get("human_review") or {}),
            }
            knowledge = Knowledge(
                id=_generate_knowledge_id(db),
                title=candidate.knowledge.title,
                subtitles=candidate.knowledge.subtitles,
                content=content,
                category_id=candidate.knowledge.category_id,
                status=KnowledgeStatus.REVIEW,
                source="automation",
                source_session_id=candidate.source.conversation_id,
                quality_score=candidate.selection.confidence,
                applicable_scenes=candidate.knowledge.scene_tags,
                applicable_categories=candidate.knowledge.applicable_categories,
                applicable_brands=candidate.knowledge.applicable_brands,
                applicable_models=candidate.knowledge.applicable_models,
                deduplication_metadata=deduplication_metadata,
                created_by=current_user.username,
                updated_by=current_user.username,
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
            item.knowledge_id = knowledge.id
            item.review_status = "submitted"
            item.status = (
                "review_duplicate"
                if decision.action == "review_duplicate"
                else "review_submitted"
            )
            item.submitted_at = datetime.utcnow()
            item.error_code = None
            item.error_message = None
            db.commit()
            submitted += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=item.id,
                    status="submitted",
                    knowledge_id=knowledge.id,
                )
            )
        except EmbeddingServiceUnavailable as exc:
            db.rollback()
            item = db.query(IntegrationIngestion).filter(IntegrationIngestion.id == ingestion_id).first()
            if item:
                item.review_status = "failed"
                item.status = "candidate_failed"
                item.error_code = "DEDUP_UNAVAILABLE"
                item.error_message = str(exc)[:512]
                db.commit()
            failed += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=ingestion_id,
                    status="failed",
                    error_code="DEDUP_UNAVAILABLE",
                    error_message=str(exc),
                )
            )
        except ValueError as exc:
            db.rollback()
            raw_error = str(exc)
            error_code = (
                raw_error
                if raw_error in {"CATEGORY_NOT_FOUND", "DUPLICATE_BLOCKED"}
                else "CANDIDATE_PAYLOAD_INVALID"
            )
            error_message = {
                "CATEGORY_NOT_FOUND": "category_id does not exist in the current taxonomy.",
                "DUPLICATE_BLOCKED": "Candidate matches an existing knowledge item and was not ingested.",
            }.get(error_code, raw_error)
            item = db.query(IntegrationIngestion).filter(IntegrationIngestion.id == ingestion_id).first()
            if item:
                item.review_status = "failed"
                item.status = "candidate_failed"
                item.error_code = error_code
                item.error_message = error_message[:512]
                db.commit()
            failed += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=ingestion_id,
                    status="failed",
                    error_code=error_code,
                    error_message=error_message,
                )
            )
        except Exception:
            db.rollback()
            logger.exception(
                "Unexpected error while submitting candidate review %s",
                ingestion_id,
            )
            error_code = "CANDIDATE_SUBMIT_FAILED"
            error_message = "Unexpected error while creating knowledge from candidate."
            item = db.query(IntegrationIngestion).filter(IntegrationIngestion.id == ingestion_id).first()
            if item:
                item.review_status = "failed"
                item.status = "candidate_failed"
                item.error_code = error_code
                item.error_message = error_message
                db.commit()
            failed += 1
            results.append(
                CandidateReviewSubmitResult(
                    ingestion_id=ingestion_id,
                    status="failed",
                    error_code=error_code,
                    error_message=error_message,
                )
            )

    return CandidateReviewBatchSubmitResponse(
        submitted=submitted,
        failed=failed,
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
