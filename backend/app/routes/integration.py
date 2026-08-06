import logging
import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.core.config import settings
from app.core.integration_auth import require_integration_key, require_retrieval_key
from app.models.integration import IntegrationIngestion, RetrievalQualityEvent
from app.models.knowledge import Category, Knowledge, KnowledgeStatus, TagDimension
from app.models.user import User
from app.routes.auth import get_current_user, require_permission
from app.routes.knowledge import _generate_knowledge_id, _normalize_content
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.knowledge_dedup import (
    DedupDecision,
    _content_to_text,
    check_duplicate,
    ensure_search_embeddings,
    save_embedding,
    search_embeddings,
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
    IntegrationStandardSearchCandidate,
    IntegrationStandardSearchRequest,
    IntegrationStandardSearchResponse,
    IntegrationTaxonomyResponse,
    RetrievalQualityEventBatch,
    RetrievalQualityEventBatchResponse,
    RetrievalQualityEventResult,
)
from app.schemas.knowledge import (
    BusinessTypeOption,
    CategoryResponse,
    KnowledgeOriginOption,
    TagDimensionResponse,
    TagValueResponse,
)
from app.services.candidate_review import (
    evaluate_review_status,
    normalize_human_review,
)


router = APIRouter(prefix="/integration", tags=["自动化接入"])
logger = logging.getLogger(__name__)

TAXONOMY_VERSION = "automation-v5"
STANDARD_SEARCH_MAX_RESULTS = 5
STANDARD_SEARCH_KNOWLEDGE_ORIGINS = (
    "headquarters_standard",
    "business_accumulation",
)


def _to_dedup_response(decision: DedupDecision) -> IntegrationDedupResponse:
    return IntegrationDedupResponse(
        action=decision.action,
        embedding_model=settings.EMBEDDING_MODEL,
        content_hash=decision.content_hash,
        block_threshold=decision.block_threshold,
        review_threshold=decision.review_threshold,
        matches=[
            IntegrationDedupMatch(
                knowledge_id=match.knowledge_id,
                title=match.title,
                status=match.status,
                knowledge_origin=match.knowledge_origin,
                business_type=match.business_type,
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


def _candidate_payload_with_taxonomy_defaults(
    candidate_payload: dict | None,
) -> tuple[dict, dict]:
    """Normalize candidates created before the taxonomy fields became required."""

    payload = dict(candidate_payload or {})
    knowledge = dict(payload.get("knowledge") or {})
    knowledge.setdefault("knowledge_origin", "business_accumulation")
    knowledge.setdefault("business_type", "self_operated")
    payload["knowledge"] = knowledge
    return payload, knowledge


def _candidate_review_item(item: IntegrationIngestion) -> CandidateReviewListItem:
    payload, knowledge = _candidate_payload_with_taxonomy_defaults(
        item.candidate_payload
    )
    selection = dict(payload.get("selection") or item.selection_metadata or {})
    review_metadata = dict(item.review_metadata or {})
    model_review = dict(payload.get("model_review") or review_metadata.get("model_review") or {})
    human_review = normalize_human_review(
        payload.get("human_review") or review_metadata.get("human_review") or {}
    )
    deduplication = None
    raw_deduplication = review_metadata.get("deduplication")
    if isinstance(raw_deduplication, dict):
        try:
            deduplication = IntegrationDedupResponse.model_validate(raw_deduplication)
        except ValueError:
            logger.warning(
                "Ignoring malformed deduplication metadata for candidate review %s",
                item.id,
            )
    confirmation = dict(review_metadata.get("deduplication_confirmation") or {})
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
        knowledge_origin=str(knowledge["knowledge_origin"]),
        business_type=str(knowledge["business_type"]),
        category_id=str(knowledge.get("category_id") or ""),
        applicable_scenes=list(knowledge.get("scene_tags") or []),
        applicable_categories=list(knowledge.get("applicable_categories") or []),
        applicable_brands=list(knowledge.get("applicable_brands") or []),
        applicable_models=list(knowledge.get("applicable_models") or []),
        related_standard_items=list(knowledge.get("related_standard_items") or []),
        recommended_reply=knowledge.get("recommended_reply"),
        evidence_excerpt=knowledge.get("evidence_excerpt"),
        selection=selection,
        model_review=model_review,
        human_review=human_review,
        priority_review=bool(model_review.get("priority_review")),
        deduplication=deduplication,
        deduplication_confirmed=(
            _deduplication_confirmation_matches_response(
                confirmation,
                deduplication,
            )
            if deduplication
            else False
        ),
        deduplication_only=bool(review_metadata.get("deduplication_only")),
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


def _deduplication_match_keys(matches) -> list[str]:
    return sorted(
        f"{match.knowledge_id}:{match.match_type}"
        for match in matches
    )


def _deduplication_confirmation_matches(
    confirmation: dict[str, Any] | None,
    decision: DedupDecision,
) -> bool:
    if decision.action != "review_duplicate":
        return True
    confirmation = dict(confirmation or {})
    return (
        confirmation.get("content_hash") == decision.content_hash
        and confirmation.get("match_keys") == _deduplication_match_keys(decision.matches)
    )


def _deduplication_confirmation_matches_response(
    confirmation: dict[str, Any] | None,
    decision: IntegrationDedupResponse,
) -> bool:
    confirmation = dict(confirmation or {})
    return (
        decision.action == "review_duplicate"
        and confirmation.get("content_hash") == decision.content_hash
        and confirmation.get("match_keys") == _deduplication_match_keys(decision.matches)
    )


def _deduplication_confirmation(
    decision: IntegrationDedupResponse,
    username: str,
) -> dict[str, Any]:
    return {
        "content_hash": decision.content_hash,
        "match_keys": _deduplication_match_keys(decision.matches),
        "confirmed_by": username,
        "confirmed_at": datetime.utcnow().isoformat(),
    }


def _deduplication_review_message(deduplication: IntegrationDedupResponse) -> str:
    top_match = deduplication.matches[0] if deduplication.matches else None
    if top_match and top_match.match_type == "title_exact":
        message = "标题完全相同但正文不同，需人工核对后确认提交。"
    else:
        message = "检测到疑似重复知识，需人工核对后确认提交。"
    if top_match:
        message += f" 命中 {top_match.knowledge_id}《{top_match.title}》。"
    return message


def _queue_duplicate_candidate(
    db: Session,
    candidate: IntegrationCandidate,
    deduplication: IntegrationDedupResponse,
) -> IntegrationIngestion:
    payload, selection, review_metadata, review_status = _candidate_queue_state(candidate)
    review_metadata["deduplication"] = deduplication.model_dump(mode="json")
    return IntegrationIngestion(
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
        error_code="DUPLICATE_REVIEW_REQUIRED",
        error_message=_deduplication_review_message(deduplication),
    )


_RETRIEVAL_TECHNICAL_FAILURES = {"timeout", "error", "invalid_response"}


def _metadata_value(candidate, key, default=None):
    metadata = candidate.metadata if isinstance(candidate.metadata, dict) else {}
    return metadata.get(key, default)


def _retrieval_candidate_snapshot(candidate) -> list[dict]:
    if candidate.candidates:
        return [
            {
                "knowledge_id": item.knowledge_id,
                "rank": item.rank,
                "title": item.title,
                "embedding_score": item.embedding_score,
                "rerank_score": item.rerank_score,
                "final_score": item.final_score,
                "selected": item.selected,
            }
            for item in sorted(candidate.candidates, key=lambda item: item.rank)
        ]

    candidate_ids = _metadata_value(candidate, "candidate_ids", [])
    if not isinstance(candidate_ids, list):
        candidate_ids = []
    selected_id = candidate.selected_knowledge_id or _metadata_value(
        candidate,
        "selected_knowledge_id",
    )
    selected_rank = candidate.selected_candidate_rank or _metadata_value(
        candidate,
        "selected_candidate_rank",
    )
    snapshot = []
    for index, knowledge_id in enumerate(candidate_ids[: candidate.candidate_count], start=1):
        normalized_id = str(knowledge_id or "").strip()
        if not normalized_id:
            continue
        snapshot.append(
            {
                "knowledge_id": normalized_id[:64],
                "rank": index,
                "title": "",
                "embedding_score": None,
                "rerank_score": candidate.top_rerank_score if index == 1 else None,
                "final_score": candidate.top_rerank_score if index == 1 else None,
                "selected": (
                    normalized_id == selected_id
                    or (selected_rank is not None and index == int(selected_rank))
                ),
            }
        )
    if not snapshot and candidate.top_knowledge_id:
        snapshot.append(
            {
                "knowledge_id": candidate.top_knowledge_id,
                "rank": 1,
                "title": "",
                "embedding_score": None,
                "rerank_score": candidate.top_rerank_score,
                "final_score": candidate.top_rerank_score,
                "selected": bool(candidate.selected),
            }
        )
    return snapshot


def _retrieval_feedback_dimensions(candidate) -> dict:
    selected_knowledge_id = (
        candidate.selected_knowledge_id
        or _metadata_value(candidate, "selected_knowledge_id")
        or (candidate.top_knowledge_id if candidate.selected else None)
    )
    selected_candidate_rank = (
        candidate.selected_candidate_rank
        or _metadata_value(candidate, "selected_candidate_rank")
        or (1 if candidate.selected else None)
    )

    if candidate.request_status in _RETRIEVAL_TECHNICAL_FAILURES:
        threshold_status = "not_applicable"
        selection_status = "not_evaluated"
        outcome = "technical_failure"
    elif candidate.candidate_count == 0:
        threshold_status = "not_applicable"
        selection_status = "not_evaluated"
        outcome = "no_candidates"
    else:
        threshold_status = (
            "below"
            if candidate.top_rerank_score < candidate.score_threshold
            else "passed"
        )
        if selected_knowledge_id:
            selection_status = (
                "top_selected"
                if (
                    selected_knowledge_id == candidate.top_knowledge_id
                    or selected_candidate_rank == 1
                )
                else "alternative_selected"
            )
        else:
            selection_status = "none_selected"

        if threshold_status == "below":
            outcome = "low_score"
        elif selection_status == "top_selected":
            outcome = "accepted"
        elif selection_status == "alternative_selected":
            outcome = "accepted_alternative"
        else:
            outcome = "not_selected"

    return {
        "selected_knowledge_id": selected_knowledge_id,
        "selected_candidate_rank": selected_candidate_rank,
        "threshold_status": threshold_status,
        "selection_status": selection_status,
        "outcome": outcome,
    }


def _resolve_retrieval_outcome(candidate) -> str:
    return _retrieval_feedback_dimensions(candidate)["outcome"]


def _standard_search_strings(values, *, limit: int = 100) -> list[str]:
    result: list[str] = []
    for value in values or []:
        normalized = str(value).strip()
        if normalized and normalized not in result:
            result.append(normalized)
        if len(result) >= limit:
            break
    return result


def _to_standard_search_candidate(
    item: Knowledge,
    score: float,
) -> IntegrationStandardSearchCandidate:
    applicable_categories = _standard_search_strings(item.applicable_categories)
    keywords = _standard_search_strings(
        [*(item.subtitles or []), *(item.applicable_scenes or [])]
    )
    category = getattr(item, "category", None)
    normalized_score = max(0.0, min(1.0, float(score)))
    return IntegrationStandardSearchCandidate(
        id=item.id,
        title=item.title,
        text=_content_to_text(item.content),
        score=normalized_score,
        final_score=normalized_score,
        status="published",
        knowledge_origin=getattr(
            item,
            "knowledge_origin",
            "business_accumulation",
        ),
        business_type=item.business_type,
        category_id=item.category_id,
        level1_label=str(getattr(category, "name", "") or ""),
        product_type=applicable_categories[0] if applicable_categories else "",
        models=_standard_search_strings(item.applicable_models),
        keywords=keywords,
        source_ref=f"knowledge-kb://knowledge/{item.id}",
    )


@router.post(
    "/standard-search",
    response_model=IntegrationStandardSearchResponse,
    summary="为答疑智能推荐助手检索已发布知识",
)
def search_standard_provider_knowledge(
    body: IntegrationStandardSearchRequest,
    db: Session = Depends(get_db),
    _: None = Depends(require_retrieval_key),
):
    top_k_per_origin = min(body.limit, STANDARD_SEARCH_MAX_RESULTS)
    inferred_business_type = body.business_type
    if not inferred_business_type:
        inferred_business_type = (
            "aggregated"
            if any(
                hint.strip() == "聚合回收"
                for hint in (
                    body.product_type,
                    body.order_info.category,
                )
            )
            else "self_operated"
        )
    try:
        ranked_by_origin = {
            knowledge_origin: search_embeddings(
                db,
                query=body.normalized_question,
                business_type=inferred_business_type,
                knowledge_origin=knowledge_origin,
                top_k=top_k_per_origin,
            )
            for knowledge_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS
        }
    except EmbeddingServiceUnavailable as exc:
        logger.warning("Embedding unavailable during standard provider search: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Embedding 服务不可用，无法完成语义检索",
        ) from exc

    published_ranked: list[tuple[Knowledge, float]] = []
    for knowledge_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS:
        published_ranked.extend(
            [
                (item, score)
                for item, score in ranked_by_origin[knowledge_origin]
                if item.status == KnowledgeStatus.PUBLISHED
            ][:top_k_per_origin]
        )
    candidates = [
        _to_standard_search_candidate(item, score)
        for item, score in published_ranked
    ]
    return IntegrationStandardSearchResponse(
        provider="knowledge-kb",
        status="success" if candidates else "no_match",
        retrieval_mode="semantic_pgvector",
        knowledge_version=settings.VERSION,
        candidates=candidates,
    )


@router.post(
    "/retrieval-events:batch",
    response_model=RetrievalQualityEventBatchResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
def submit_retrieval_quality_events(
    body: RetrievalQualityEventBatch,
    db: Session = Depends(get_db),
    _: None = Depends(require_retrieval_key),
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

        dimensions = _retrieval_feedback_dimensions(candidate)
        outcome = dimensions["outcome"]
        candidate_snapshot = _retrieval_candidate_snapshot(candidate)
        latency_ms = _metadata_value(candidate, "latency_ms")
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
            schema_version=max(
                candidate.schema_version,
                2 if candidate.candidates else 1,
            ),
            request_status=candidate.request_status,
            threshold_status=dimensions["threshold_status"],
            selection_status=dimensions["selection_status"],
            selected_knowledge_id=dimensions["selected_knowledge_id"],
            selected_candidate_rank=dimensions["selected_candidate_rank"],
            expected_knowledge_id=candidate.expected_knowledge_id,
            feedback_type=candidate.feedback_type,
            failure_reason=candidate.failure_reason,
            candidate_snapshot=candidate_snapshot,
            embedding_model=candidate.embedding_model,
            reranker_model=candidate.reranker_model,
            prompt_version=candidate.prompt_version,
            retrieval_latency_ms=candidate.retrieval_latency_ms,
            rerank_latency_ms=candidate.rerank_latency_ms,
            total_latency_ms=(
                candidate.total_latency_ms
                if candidate.total_latency_ms is not None
                else (
                    max(0.0, float(latency_ms))
                    if latency_ms is not None
                    else None
                )
            ),
            training_eligible=False,
            review_status="unreviewed",
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
    summary = {
        "total": 0,
        "accepted": 0,
        "accepted_alternative": 0,
        "low_score": 0,
        "no_candidates": 0,
        "not_selected": 0,
        "technical_failure": 0,
        "successful_requests": 0,
        "candidate_queries": 0,
        "threshold_passed": 0,
        "threshold_below": 0,
        "top_selected": 0,
        "alternative_selected": 0,
        "none_selected": 0,
        "reviewed": 0,
        "training_eligible": 0,
    }
    for outcome, total in (
        db.query(RetrievalQualityEvent.outcome, func.count(RetrievalQualityEvent.id))
        .group_by(RetrievalQualityEvent.outcome)
        .all()
    ):
        summary[outcome] = total

    request_counts = dict(
        db.query(
            RetrievalQualityEvent.request_status,
            func.count(RetrievalQualityEvent.id),
        )
        .group_by(RetrievalQualityEvent.request_status)
        .all()
    )
    threshold_counts = dict(
        db.query(
            RetrievalQualityEvent.threshold_status,
            func.count(RetrievalQualityEvent.id),
        )
        .group_by(RetrievalQualityEvent.threshold_status)
        .all()
    )
    selection_counts = dict(
        db.query(
            RetrievalQualityEvent.selection_status,
            func.count(RetrievalQualityEvent.id),
        )
        .group_by(RetrievalQualityEvent.selection_status)
        .all()
    )
    summary["successful_requests"] = sum(
        int(request_counts.get(key, 0))
        for key in ("success", "no_match", "fallback")
    )
    summary["candidate_queries"] = sum(
        int(threshold_counts.get(key, 0))
        for key in ("passed", "below")
    )
    summary["threshold_passed"] = int(threshold_counts.get("passed", 0))
    summary["threshold_below"] = int(threshold_counts.get("below", 0))
    summary["top_selected"] = int(selection_counts.get("top_selected", 0))
    summary["alternative_selected"] = int(
        selection_counts.get("alternative_selected", 0)
    )
    summary["none_selected"] = int(selection_counts.get("none_selected", 0))
    summary["reviewed"] = (
        db.query(func.count(RetrievalQualityEvent.id))
        .filter(RetrievalQualityEvent.review_status == "confirmed")
        .scalar()
        or 0
    )
    summary["training_eligible"] = (
        db.query(func.count(RetrievalQualityEvent.id))
        .filter(RetrievalQualityEvent.training_eligible.is_(True))
        .scalar()
        or 0
    )

    risks = (
        db.query(RetrievalQualityEvent)
        .filter(
            or_(
                RetrievalQualityEvent.outcome.notin_(["accepted", "accepted_alternative"]),
                RetrievalQualityEvent.review_status == "unreviewed",
            )
        )
        .order_by(RetrievalQualityEvent.created_at.desc())
        .limit(50)
        .all()
    )
    summary["total"] = sum(
        int(summary.get(key, 0))
        for key in (
            "accepted",
            "accepted_alternative",
            "low_score",
            "no_candidates",
            "not_selected",
            "technical_failure",
        )
    )

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    rates = {
        "candidate_coverage_rate": rate(
            summary["candidate_queries"],
            summary["successful_requests"],
        ),
        "threshold_pass_rate": rate(
            summary["threshold_passed"],
            summary["candidate_queries"],
        ),
        "any_selection_rate": rate(
            summary["top_selected"] + summary["alternative_selected"],
            summary["candidate_queries"],
        ),
        "top1_selection_rate": rate(
            summary["top_selected"],
            summary["candidate_queries"],
        ),
        "alternative_selection_rate": rate(
            summary["alternative_selected"],
            summary["candidate_queries"],
        ),
        "no_selection_rate": rate(
            summary["none_selected"],
            summary["candidate_queries"],
        ),
        "review_coverage_rate": rate(summary["reviewed"], summary["total"]),
    }

    latencies = sorted(
        float(value)
        for (value,) in (
            db.query(RetrievalQualityEvent.total_latency_ms)
            .filter(RetrievalQualityEvent.total_latency_ms.is_not(None))
            .order_by(RetrievalQualityEvent.created_at.desc())
            .limit(5000)
            .all()
        )
    )

    def percentile(values: list[float], percentile_value: float) -> float | None:
        if not values:
            return None
        index = min(len(values) - 1, max(0, round((len(values) - 1) * percentile_value)))
        return round(values[index], 2)

    latency = {
        "count": len(latencies),
        "average_ms": round(sum(latencies) / len(latencies), 2) if latencies else None,
        "p50_ms": percentile(latencies, 0.5),
        "p95_ms": percentile(latencies, 0.95),
    }
    return {
        "summary": summary,
        "rates": rates,
        "latency": latency,
        "definitions": {
            "candidate_coverage_rate": "成功请求中至少返回一条候选知识的比例",
            "threshold_pass_rate": "有候选请求中最高分达到当次阈值的比例",
            "top1_selection_rate": "有候选请求中最终采用第一名的比例",
            "alternative_selection_rate": "有候选请求中最终采用第二名及以后候选的比例",
            "no_selection_rate": "有候选请求中最终没有采用任何候选的比例",
            "review_coverage_rate": "已由人工明确原因和正确目标的事件比例",
        },
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
                "schema_version": event.schema_version,
                "request_status": event.request_status,
                "threshold_status": event.threshold_status,
                "selection_status": event.selection_status,
                "selected_knowledge_id": event.selected_knowledge_id,
                "selected_candidate_rank": event.selected_candidate_rank,
                "expected_knowledge_id": event.expected_knowledge_id,
                "feedback_type": event.feedback_type,
                "failure_reason": event.failure_reason,
                "candidates": event.candidate_snapshot or [],
                "embedding_model": event.embedding_model,
                "reranker_model": event.reranker_model,
                "prompt_version": event.prompt_version,
                "retrieval_latency_ms": event.retrieval_latency_ms,
                "rerank_latency_ms": event.rerank_latency_ms,
                "total_latency_ms": event.total_latency_ms,
                "training_eligible": event.training_eligible,
                "review_status": event.review_status,
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
        knowledge_origins=[
            KnowledgeOriginOption(
                value="headquarters_standard",
                label="总部标准",
            ),
            KnowledgeOriginOption(
                value="business_accumulation",
                label="业务沉淀",
            ),
        ],
        business_types=[
            BusinessTypeOption(value="self_operated", label="自营回收"),
            BusinessTypeOption(value="aggregated", label="聚合回收"),
        ],
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
            knowledge_origin=body.knowledge.knowledge_origin,
            business_type=body.knowledge.business_type,
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
    accepted = review_required = rejected = reused = 0

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
                knowledge_origin=candidate.knowledge.knowledge_origin,
                business_type=candidate.knowledge.business_type,
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
        if decision.action == "review_duplicate":
            ingestion = _queue_duplicate_candidate(db, candidate, deduplication)
            db.add(ingestion)
            review_required += 1
            results.append(
                IntegrationCandidateResult(
                    event_id=candidate.event_id,
                    idempotency_key=candidate.idempotency_key,
                    status="review_required",
                    ingestion_id=ingestion.id,
                    error_code="DUPLICATE_REVIEW_REQUIRED",
                    error_message=_deduplication_review_message(deduplication),
                    deduplication=deduplication,
                )
            )
            continue

        knowledge = Knowledge(
            id=_generate_knowledge_id(db),
            title=candidate.knowledge.title,
            subtitles=candidate.knowledge.subtitles,
            content=_normalize_content(candidate.knowledge.content),
            knowledge_origin=candidate.knowledge.knowledge_origin,
            business_type=candidate.knowledge.business_type,
            category_id=candidate.knowledge.category_id,
            status=KnowledgeStatus.REVIEW,
            source="automation",
            source_session_id=candidate.source.conversation_id,
            quality_score=candidate.selection.confidence,
            applicable_scenes=candidate.knowledge.scene_tags,
            applicable_categories=candidate.knowledge.applicable_categories,
            applicable_brands=candidate.knowledge.applicable_brands,
            applicable_models=candidate.knowledge.applicable_models,
            related_standard_items=candidate.knowledge.related_standard_items,
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
            status="review_submitted",
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
        review_required=review_required,
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
    deduplication_required: bool = Query(False),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: Session = Depends(get_db),
    _: User = Depends(require_permission("knowledge:submit")),
):
    rows = (
        db.query(IntegrationIngestion)
        .filter(
            IntegrationIngestion.review_status.isnot(None),
            IntegrationIngestion.source_system != "excel",
        )
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
        "deduplication_required": sum(
            bool(
                item.deduplication
                and item.deduplication.action == "review_duplicate"
                and not item.deduplication_confirmed
            )
            for item in all_items
        ),
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
    if deduplication_required:
        filtered = [
            item
            for item in filtered
            if (
                item.deduplication
                and item.deduplication.action == "review_duplicate"
                and not item.deduplication_confirmed
            )
        ]

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
            IntegrationIngestion.source_system != "excel",
        )
        .first()
    )
    if not item:
        raise HTTPException(status_code=404, detail="Candidate review item not found.")
    if item.review_status == "submitted":
        raise HTTPException(status_code=409, detail="Submitted candidate cannot be edited.")

    payload, knowledge = _candidate_payload_with_taxonomy_defaults(
        item.candidate_payload
    )
    updates = body.model_dump(exclude_unset=True)
    confirm_dedup_review = updates.pop("confirm_dedup_review", None)
    deduplication_sensitive_changed = False
    for field, payload_key in (
        ("title", "title"),
        ("subtitles", "subtitles"),
        ("content", "content"),
        ("knowledge_origin", "knowledge_origin"),
        ("business_type", "business_type"),
        ("category_id", "category_id"),
        ("applicable_scenes", "scene_tags"),
        ("applicable_categories", "applicable_categories"),
        ("applicable_brands", "applicable_brands"),
        ("applicable_models", "applicable_models"),
        ("related_standard_items", "related_standard_items"),
        ("recommended_reply", "recommended_reply"),
    ):
        if field in updates:
            value = updates.pop(field)
            if knowledge.get(payload_key) != value:
                deduplication_sensitive_changed = True
            knowledge[payload_key] = value
    payload["knowledge"] = knowledge

    review_metadata = dict(item.review_metadata or {})
    if deduplication_sensitive_changed or confirm_dedup_review is False:
        review_metadata.pop("deduplication_confirmation", None)
    elif confirm_dedup_review is True:
        raw_deduplication = review_metadata.get("deduplication")
        try:
            deduplication = IntegrationDedupResponse.model_validate(raw_deduplication)
        except ValueError as exc:
            raise HTTPException(
                status_code=409,
                detail="当前候选尚未生成疑似重复命中，不能确认查重结果。",
            ) from exc
        if deduplication.action != "review_duplicate":
            raise HTTPException(
                status_code=409,
                detail="当前候选不存在需人工确认的疑似重复命中。",
            )
        review_metadata["deduplication_confirmation"] = _deduplication_confirmation(
            deduplication,
            current_user.username,
        )
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
            .filter(
                IntegrationIngestion.id == ingestion_id,
                IntegrationIngestion.source_system != "excel",
            )
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
            normalized_payload, _ = _candidate_payload_with_taxonomy_defaults(
                item.candidate_payload
            )
            candidate = IntegrationCandidate.model_validate(normalized_payload)
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
                knowledge_origin=candidate.knowledge.knowledge_origin,
                business_type=candidate.knowledge.business_type,
            )
            deduplication = _to_dedup_response(decision)
            if decision.action == "block_duplicate":
                raise ValueError("DUPLICATE_BLOCKED")
            if not _deduplication_confirmation_matches(
                dict((item.review_metadata or {}).get("deduplication_confirmation") or {}),
                decision,
            ):
                review_metadata = dict(item.review_metadata or {})
                review_metadata["deduplication"] = deduplication.model_dump(mode="json")
                review_metadata.pop("deduplication_confirmation", None)
                item.review_metadata = review_metadata
                item.review_status = "ready"
                item.status = "candidate_ready"
                item.error_code = "DUPLICATE_REVIEW_REQUIRED"
                item.error_message = _deduplication_review_message(deduplication)
                db.commit()
                failed += 1
                results.append(
                    CandidateReviewSubmitResult(
                        ingestion_id=item.id,
                        status="failed",
                        error_code="DUPLICATE_REVIEW_REQUIRED",
                        error_message=item.error_message,
                    )
                )
                continue

            deduplication_metadata = deduplication.model_dump(mode="json")
            deduplication_metadata["candidate_review"] = {
                "ingestion_id": item.id,
                "model_review": dict((item.review_metadata or {}).get("model_review") or {}),
                "human_review": dict((item.review_metadata or {}).get("human_review") or {}),
                "deduplication_confirmation": dict(
                    (item.review_metadata or {}).get("deduplication_confirmation") or {}
                ),
            }
            knowledge = Knowledge(
                id=_generate_knowledge_id(db),
                title=candidate.knowledge.title,
                subtitles=candidate.knowledge.subtitles,
                content=content,
                knowledge_origin=candidate.knowledge.knowledge_origin,
                business_type=candidate.knowledge.business_type,
                category_id=candidate.knowledge.category_id,
                status=KnowledgeStatus.REVIEW,
                source="automation",
                source_session_id=candidate.source.conversation_id,
                quality_score=candidate.selection.confidence,
                applicable_scenes=candidate.knowledge.scene_tags,
                applicable_categories=candidate.knowledge.applicable_categories,
                applicable_brands=candidate.knowledge.applicable_brands,
                applicable_models=candidate.knowledge.applicable_models,
                related_standard_items=candidate.knowledge.related_standard_items,
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
