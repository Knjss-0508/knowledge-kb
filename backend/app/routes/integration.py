import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.core.integration_auth import require_integration_key, require_retrieval_key
from app.models.integration import IntegrationIngestion, RetrievalQualityEvent
from app.models.knowledge import Category, Knowledge, KnowledgeStatus, TagDimension
from app.models.user import User
from app.routes.auth import get_current_user, require_permission
from app.routes.knowledge import _generate_knowledge_id, _normalize_content
from app.routes.manhattan import _read_cache as _read_manhattan_cache
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
    IntegrationModelConfigurationItem,
    IntegrationModelConfigurationResult,
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
from app.services.applicability import resolve_applicability_scope
from app.services.candidate_review import (
    evaluate_review_status,
    normalize_human_review,
)
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.embedding_runtime import get_active_runtime_values
from app.services.knowledge_dedup import (
    DedupDecision,
    _content_to_text,
    check_duplicate,
    ensure_search_embeddings,
    save_embedding,
    search_embeddings,
)
from app.services.model_configuration import (
    MODEL_CONFIGURATION_ORIGIN,
    ModelConfigurationAmbiguousError,
    ModelConfigurationMatch,
    find_exact_model_configuration,
)
from app.services.retrieval_quality import (
    latest_retrieval_quality_request_event_ids,
)

router = APIRouter(prefix="/integration", tags=["自动化接入"])
logger = logging.getLogger(__name__)

TAXONOMY_VERSION = "automation-v6"
STANDARD_SEARCH_DEFAULT_RESULTS = 5
STANDARD_SEARCH_MAX_RESULTS = 10
RETRIEVAL_NEAR_THRESHOLD_MARGIN = 0.05
STANDARD_SEARCH_KNOWLEDGE_ORIGINS = (
    "headquarters_standard",
    "business_accumulation",
)
STANDARD_SEARCH_TOP_K_CONFIG_KEYS = {
    "headquarters_standard": "retrieval_headquarters_standard_top_k",
    "business_accumulation": "retrieval_business_accumulation_top_k",
}


def _retrieval_score_threshold(runtime_config: dict[str, Any]) -> float:
    try:
        score_threshold = float(runtime_config["retrieval_score_threshold"])
    except (KeyError, TypeError, ValueError):
        score_threshold = 0.42
    return max(0.0, min(1.0, score_threshold))


def _active_retrieval_score_threshold(db: Session) -> float:
    return _retrieval_score_threshold(get_active_runtime_values(db))


def _standard_search_top_k_by_origin(
    runtime_config: dict[str, Any],
    *,
    request_limit: int,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for knowledge_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS:
        config_key = STANDARD_SEARCH_TOP_K_CONFIG_KEYS[knowledge_origin]
        try:
            configured_top_k = int(runtime_config[config_key])
        except (KeyError, TypeError, ValueError):
            configured_top_k = STANDARD_SEARCH_DEFAULT_RESULTS
        configured_top_k = max(
            1,
            min(configured_top_k, STANDARD_SEARCH_MAX_RESULTS),
        )
        result[knowledge_origin] = min(request_limit, configured_top_k)
    return result


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


def _ensure_candidate_origin_is_writable(knowledge: dict) -> None:
    if (
        str(knowledge.get("knowledge_origin") or "").strip()
        == MODEL_CONFIGURATION_ORIGIN
    ):
        raise ValueError("KNOWLEDGE_ORIGIN_MANAGED")


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


def _refresh_unreviewed_candidate(
    existing: IntegrationIngestion,
    candidate: IntegrationCandidate,
    *,
    payload: dict[str, Any],
    selection: dict[str, Any],
    review_metadata: dict[str, Any],
    review_status: str,
) -> None:
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


def _retrieval_candidate_origins(candidate) -> list[str | None]:
    candidate_origins = _metadata_value(candidate, "candidate_origins", [])
    if not isinstance(candidate_origins, list):
        candidate_origins = []
    return [
        value if value in STANDARD_SEARCH_KNOWLEDGE_ORIGINS else None
        for value in (
            str(candidate_origin or "").strip()
            for candidate_origin in candidate_origins
        )
    ]


def _retrieval_ranked_candidate_entries(candidate) -> list[tuple[Any, str | None]]:
    candidate_origins = _retrieval_candidate_origins(candidate)
    candidate_ids = _metadata_value(candidate, "candidate_ids", [])
    if not isinstance(candidate_ids, list):
        candidate_ids = []
    origin_by_id = {
        str(knowledge_id or "").strip(): candidate_origins[index]
        for index, knowledge_id in enumerate(candidate_ids)
        if (
            index < len(candidate_origins)
            and candidate_origins[index] is not None
            and str(knowledge_id or "").strip()
        )
    }
    return [
        (
            item,
            origin_by_id.get(item.knowledge_id)
            or (
                candidate_origins[source_index]
                if source_index < len(candidate_origins)
                else None
            ),
        )
        for source_index, item in sorted(
            enumerate(candidate.candidates),
            key=lambda pair: pair[1].rank,
        )
    ]


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
                "knowledge_origin": knowledge_origin,
            }
            for item, knowledge_origin in _retrieval_ranked_candidate_entries(candidate)
        ]

    candidate_origins = _retrieval_candidate_origins(candidate)
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
                "knowledge_origin": (
                    candidate_origins[index - 1]
                    if index - 1 < len(candidate_origins)
                    else None
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
                "knowledge_origin": (
                    candidate_origins[0] if candidate_origins else None
                ),
            }
        )
    return snapshot


def _retrieval_selected_pool_rank(
    candidate,
    selected_knowledge_id: str | None,
    reported_rank: int | None,
) -> int | None:
    selected_id = str(selected_knowledge_id or "").strip()
    if not selected_id:
        return reported_rank

    entries: list[tuple[str, str | None]] = []
    if candidate.candidates:
        entries = [
            (item.knowledge_id, knowledge_origin)
            for item, knowledge_origin in _retrieval_ranked_candidate_entries(candidate)
        ]
    else:
        candidate_ids = _metadata_value(candidate, "candidate_ids", [])
        if isinstance(candidate_ids, list):
            candidate_origins = _retrieval_candidate_origins(candidate)
            entries = [
                (
                    str(knowledge_id or "").strip(),
                    candidate_origins[index]
                    if index < len(candidate_origins)
                    else None,
                )
                for index, knowledge_id in enumerate(candidate_ids)
            ]

    selected_index = next(
        (
            index
            for index, (knowledge_id, _) in enumerate(entries)
            if knowledge_id == selected_id
        ),
        None,
    )
    if selected_index is None:
        return reported_rank
    selected_origin = entries[selected_index][1]
    if selected_origin not in STANDARD_SEARCH_KNOWLEDGE_ORIGINS:
        return reported_rank
    return sum(
        1
        for _, knowledge_origin in entries[: selected_index + 1]
        if knowledge_origin == selected_origin
    )


def _retrieval_review_candidate_snapshot(event: RetrievalQualityEvent) -> list[dict]:
    candidate_origins = (
        (event.event_metadata or {}).get("candidate_origins", [])
        if isinstance(event.event_metadata, dict)
        else []
    )
    if not isinstance(candidate_origins, list):
        candidate_origins = []

    snapshot: list[dict] = []
    for index, item in enumerate(event.candidate_snapshot or []):
        if not isinstance(item, dict):
            continue
        candidate = dict(item)
        origin = str(candidate.get("knowledge_origin") or "").strip()
        if (
            origin not in STANDARD_SEARCH_KNOWLEDGE_ORIGINS
            and index < len(candidate_origins)
        ):
            origin = str(candidate_origins[index] or "").strip()
        candidate["knowledge_origin"] = (
            origin if origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS else None
        )
        snapshot.append(candidate)
    return snapshot


def _retrieval_event_sort_key(
    event: RetrievalQualityEvent,
) -> tuple[bool, datetime | None, str]:
    return (
        event.created_at is not None,
        event.created_at,
        str(event.id or ""),
    )


def _retrieval_request_group_key(
    event: RetrievalQualityEvent,
) -> tuple[str, str, str]:
    conversation_id = str(event.conversation_id or "").strip()
    request_id = str(event.request_id or "").strip()
    if conversation_id and request_id:
        return ("request", conversation_id, request_id)
    return ("event", str(event.id or ""), "")


def _retrieval_request_event_groups(
    events: list[RetrievalQualityEvent],
) -> list[list[RetrievalQualityEvent]]:
    grouped: dict[tuple[str, str, str], list[RetrievalQualityEvent]] = {}
    for event in events:
        grouped.setdefault(_retrieval_request_group_key(event), []).append(event)
    return [
        sorted(group, key=_retrieval_event_sort_key, reverse=True)
        for group in grouped.values()
    ]


def _retrieval_candidate_score(candidate: dict) -> float | None:
    for key in ("final_score", "rerank_score", "embedding_score"):
        value = candidate.get(key)
        if isinstance(value, (int, float)):
            return float(value)
    return None


def _retrieval_request_candidate_snapshot(
    events: list[RetrievalQualityEvent],
) -> list[dict]:
    source_order = {
        "standard": 0,
        "reply": 1,
        "combined": 2,
    }
    merged: list[dict] = []
    seen_knowledge_ids: set[str] = set()
    ordered_events = sorted(
        events,
        key=_retrieval_event_sort_key,
        reverse=True,
    )
    ordered_events.sort(
        key=lambda event: source_order.get(
            str(event.source_kind or ""),
            3,
        )
    )
    for event in ordered_events:
        fallback_origin = {
            "standard": "headquarters_standard",
            "reply": "business_accumulation",
        }.get(str(event.source_kind or ""))
        candidates = sorted(
            _retrieval_review_candidate_snapshot(event),
            key=lambda candidate: (
                int(candidate.get("rank") or 0),
                str(candidate.get("knowledge_id") or ""),
            ),
        )
        for candidate in candidates:
            normalized = dict(candidate)
            if (
                normalized.get("knowledge_origin")
                not in STANDARD_SEARCH_KNOWLEDGE_ORIGINS
            ):
                normalized["knowledge_origin"] = fallback_origin
            knowledge_id = str(normalized.get("knowledge_id") or "").strip()
            if knowledge_id and knowledge_id in seen_knowledge_ids:
                continue
            if knowledge_id:
                seen_knowledge_ids.add(knowledge_id)
            merged.append(normalized)
    return merged


def _retrieval_request_payload(
    events: list[RetrievalQualityEvent],
) -> dict[str, Any]:
    ordered_events = sorted(
        events,
        key=_retrieval_event_sort_key,
        reverse=True,
    )
    representative = ordered_events[0]
    state_event = next(
        (
            event
            for event in ordered_events
            if (
                event.review_status != "unreviewed"
                or event.expected_knowledge_id
                or event.training_eligible
            )
        ),
        representative,
    )
    selection_event = next(
        (
            event
            for event in ordered_events
            if (
                event.selected_knowledge_id
                or event.selected
                or event.feedback_type != "none"
            )
        ),
        representative,
    )
    selected_knowledge_id = str(
        selection_event.selected_knowledge_id
        or (
            selection_event.top_knowledge_id
            if selection_event.selected
            else ""
        )
        or ""
    ).strip() or None
    candidates = _retrieval_request_candidate_snapshot(ordered_events)
    selected_index = next(
        (
            index
            for index, candidate in enumerate(candidates)
            if str(candidate.get("knowledge_id") or "").strip()
            == selected_knowledge_id
        ),
        None,
    )
    selected_candidate_rank = selection_event.selected_candidate_rank
    if selected_index is not None:
        selected_origin = candidates[selected_index].get("knowledge_origin")
        if selected_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS:
            selected_candidate_rank = sum(
                1
                for candidate in candidates[: selected_index + 1]
                if candidate.get("knowledge_origin") == selected_origin
            )
        else:
            selected_candidate_rank = selected_index + 1

    statuses = {
        str(event.request_status or "")
        for event in ordered_events
    }
    if candidates:
        if "success" in statuses:
            request_status = "success"
        elif "fallback" in statuses:
            request_status = "fallback"
        else:
            request_status = representative.request_status
    elif "no_match" in statuses:
        request_status = "no_match"
    elif "success" in statuses:
        request_status = "success"
    elif "fallback" in statuses:
        request_status = "fallback"
    else:
        request_status = representative.request_status

    score_threshold = float(representative.score_threshold or 0)
    scored_candidates = [
        (candidate, _retrieval_candidate_score(candidate))
        for candidate in candidates
    ]
    scored_candidates = [
        (candidate, score)
        for candidate, score in scored_candidates
        if score is not None
    ]
    top_candidate = (
        max(scored_candidates, key=lambda item: item[1])[0]
        if scored_candidates
        else (candidates[0] if candidates else None)
    )
    event_top_scores = [
        float(event.top_rerank_score)
        for event in ordered_events
        if event.top_rerank_score is not None
    ]
    candidate_top_score = (
        _retrieval_candidate_score(top_candidate)
        if top_candidate
        else None
    )
    top_rerank_score = max(
        [
            score
            for score in (candidate_top_score, *event_top_scores)
            if score is not None
        ],
        default=None,
    )
    if candidates:
        threshold_status = (
            "below"
            if (
                top_rerank_score is not None
                and top_rerank_score < score_threshold
            )
            else "passed"
        )
        if selected_knowledge_id:
            selection_status = (
                "top_selected"
                if selected_candidate_rank == 1
                else "alternative_selected"
            )
        else:
            selection_status = "none_selected"
    else:
        threshold_status = "not_applicable"
        selection_status = "not_evaluated"

    if (
        not candidates
        and statuses.intersection(_RETRIEVAL_TECHNICAL_FAILURES)
    ):
        outcome = "technical_failure"
    elif not candidates:
        outcome = "no_candidates"
    elif threshold_status == "below":
        outcome = "low_score"
    elif selection_status == "top_selected":
        outcome = "accepted"
    elif selection_status == "alternative_selected":
        outcome = "accepted_alternative"
    else:
        outcome = "not_selected"

    source_kinds = {
        str(event.source_kind or "")
        for event in ordered_events
        if str(event.source_kind or "")
    }
    source_kind = (
        next(iter(source_kinds))
        if len(source_kinds) == 1
        else "combined"
    )

    def latest_text(attribute: str) -> str:
        return next(
            (
                str(getattr(event, attribute) or "")
                for event in ordered_events
                if str(getattr(event, attribute) or "")
            ),
            "",
        )

    def maximum_number(attribute: str) -> float | None:
        values = [
            float(value)
            for event in ordered_events
            if (value := getattr(event, attribute)) is not None
        ]
        return max(values) if values else None

    return {
        "id": representative.id,
        "source_system": representative.source_system,
        "conversation_id": representative.conversation_id,
        "request_id": representative.request_id,
        "source_kind": source_kind,
        "query": representative.query_text,
        "candidate_count": len(candidates),
        "top_knowledge_id": (
            top_candidate.get("knowledge_id")
            if top_candidate
            else None
        ),
        "top_rerank_score": top_rerank_score,
        "score_threshold": score_threshold,
        "selected": selection_status == "top_selected",
        "outcome": outcome,
        "schema_version": max(
            int(event.schema_version or 1)
            for event in ordered_events
        ),
        "request_status": request_status,
        "threshold_status": threshold_status,
        "selection_status": selection_status,
        "selected_knowledge_id": selected_knowledge_id,
        "selected_candidate_rank": selected_candidate_rank,
        "expected_knowledge_id": state_event.expected_knowledge_id,
        "feedback_type": state_event.feedback_type,
        "failure_reason": state_event.failure_reason,
        "candidates": candidates,
        "embedding_model": latest_text("embedding_model"),
        "reranker_model": latest_text("reranker_model"),
        "prompt_version": latest_text("prompt_version"),
        "retrieval_latency_ms": maximum_number("retrieval_latency_ms"),
        "rerank_latency_ms": maximum_number("rerank_latency_ms"),
        "total_latency_ms": maximum_number("total_latency_ms"),
        "training_eligible": state_event.training_eligible,
        "review_status": state_event.review_status,
        "created_at": representative.created_at,
    }


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
    selected_pool_rank = _retrieval_selected_pool_rank(
        candidate,
        selected_knowledge_id,
        selected_candidate_rank,
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
                    or selected_pool_rank == 1
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


def _retrieval_source_kind(candidate) -> str:
    metadata = (
        candidate.metadata
        if isinstance(candidate.metadata, dict)
        else {}
    )
    source_kind = str(metadata.get("source_kind") or "").strip().lower()
    return (
        source_kind
        if source_kind in {"reply", "standard"}
        else "combined"
    )


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


def _model_configuration_source_field(
    item: Knowledge,
    field: str,
) -> str:
    source_fields = (
        item.source_fields if isinstance(item.source_fields, dict) else {}
    )
    return str(source_fields.get(field) or "").strip()


def _to_model_configuration_result(
    match: ModelConfigurationMatch | None,
) -> IntegrationModelConfigurationResult:
    if match is None:
        return IntegrationModelConfigurationResult(
            status="no_match",
            match_mode="none",
            item=None,
        )
    item = match.item
    return IntegrationModelConfigurationResult(
        status="success",
        match_mode=match.match_mode,
        item=IntegrationModelConfigurationItem(
            knowledge_id=item.id,
            category_id=_model_configuration_source_field(item, "品类ID"),
            category=_model_configuration_source_field(item, "品类"),
            brand_id=_model_configuration_source_field(item, "品牌ID"),
            brand=_model_configuration_source_field(item, "品牌"),
            model_id=_model_configuration_source_field(item, "型号ID"),
            model=_model_configuration_source_field(item, "型号"),
            title=item.title,
            content=_content_to_text(item.content),
            source_ref=f"knowledge-kb://knowledge/{item.id}",
        ),
    )


@router.post(
    "/standard-search",
    response_model=IntegrationStandardSearchResponse,
    summary="为答疑智能推荐助手检索已发布知识",
)
def search_standard_provider_knowledge(
    body: IntegrationStandardSearchRequest,
    x_conversation_id: str = Header(..., alias="X-Conversation-Id"),
    x_request_id: str = Header(..., alias="X-Request-Id"),
    db: Session = Depends(get_db),
    _: None = Depends(require_retrieval_key),
):
    if (
        x_conversation_id != body.conversation_id
        or x_request_id != body.request_id
    ):
        logger.warning(
            "Standard search identity mismatch: "
            "body_conversation_id=%s body_request_id=%s "
            "header_conversation_id=%s header_request_id=%s",
            body.conversation_id,
            body.request_id,
            x_conversation_id,
            x_request_id,
        )
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "conversationId": body.conversation_id,
                "requestId": body.request_id,
                "code": "REQUEST_IDENTITY_MISMATCH",
                "message": (
                    "请求 Header 与正文中的 conversationId/requestId "
                    "必须完全一致"
                ),
            },
        )
    runtime_config = get_active_runtime_values(db)
    top_k_by_origin = _standard_search_top_k_by_origin(
        runtime_config,
        request_limit=body.limit,
    )
    score_threshold = _retrieval_score_threshold(runtime_config)
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
        manhattan_cache = _read_manhattan_cache()
    except (OSError, ValueError) as exc:
        logger.warning("Unable to read Manhattan applicability cache: %s", exc)
        manhattan_cache = {}
    applicability_scope = resolve_applicability_scope(
        manhattan_cache,
        inferred_business_type,
        category_values=(
            body.category_id,
            body.product_type,
            body.order_info.category_id,
            body.order_info.category,
        ),
        brand_values=(
            body.brand_id,
            body.brand,
            body.order_info.brand_id,
            body.order_info.brand,
        ),
        model_values=(
            body.model_id,
            body.model,
            body.order_info.model_id,
            body.order_info.model,
        ),
    )
    try:
        model_configuration_match = find_exact_model_configuration(
            db,
            category_id=(
                body.category_id
                or body.order_info.category_id
            ),
            category_name=(
                body.order_info.category
                or body.product_type
            ),
            brand_id=(
                body.brand_id
                or body.order_info.brand_id
            ),
            brand_name=(
                body.brand
                or body.order_info.brand
            ),
            model_id=(
                body.model_id
                or body.order_info.model_id
            ),
            model_name=(
                body.model
                or body.order_info.model
            ),
        )
    except ModelConfigurationAmbiguousError as exc:
        logger.warning(
            "%s conversation_id=%s request_id=%s match_mode=%s "
            "category=%s model=%s match_count=%s knowledge_ids=%s",
            exc.code,
            body.conversation_id,
            body.request_id,
            exc.match_mode,
            exc.category_value,
            exc.model_value,
            exc.match_count,
            ",".join(exc.knowledge_ids),
        )
        model_configuration_match = None
    model_configuration = _to_model_configuration_result(
        model_configuration_match
    )
    try:
        ranked_by_origin = {
            knowledge_origin: search_embeddings(
                db,
                query=body.normalized_question,
                business_type=inferred_business_type,
                knowledge_origin=knowledge_origin,
                applicable_category_keys=applicability_scope["categories"],
                applicable_brand_keys=applicability_scope["brands"],
                applicable_model_keys=applicability_scope["models"],
                top_k=top_k_by_origin[knowledge_origin],
            )
            for knowledge_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS
        }
    except EmbeddingServiceUnavailable as exc:
        if model_configuration.status == "success":
            logger.warning(
                "Embedding unavailable but exact model configuration matched: "
                "conversation_id=%s request_id=%s error=%s",
                body.conversation_id,
                body.request_id,
                exc,
            )
            return IntegrationStandardSearchResponse(
                conversation_id=body.conversation_id,
                request_id=body.request_id,
                provider="knowledge-kb",
                status="success",
                retrieval_mode="exact_scope_fallback",
                knowledge_version=settings.VERSION,
                score_threshold=score_threshold,
                candidates=[],
                model_configuration=model_configuration,
            )
        logger.warning(
            "Embedding unavailable during standard provider search: "
            "conversation_id=%s request_id=%s error=%s",
            body.conversation_id,
            body.request_id,
            exc,
        )
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "conversationId": body.conversation_id,
                "requestId": body.request_id,
                "code": "EMBEDDING_SERVICE_UNAVAILABLE",
                "message": "Embedding 服务不可用，无法完成语义检索",
            },
        )

    published_ranked: list[tuple[Knowledge, float]] = []
    for knowledge_origin in STANDARD_SEARCH_KNOWLEDGE_ORIGINS:
        published_ranked.extend(
            [
                (item, score)
                for item, score in ranked_by_origin[knowledge_origin]
                if (
                    item.status == KnowledgeStatus.PUBLISHED
                    and float(score) >= score_threshold
                )
            ][:top_k_by_origin[knowledge_origin]]
        )
    candidates = [
        _to_standard_search_candidate(item, score)
        for item, score in published_ranked
    ]
    return IntegrationStandardSearchResponse(
        conversation_id=body.conversation_id,
        request_id=body.request_id,
        provider="knowledge-kb",
        status=(
            "success"
            if candidates or model_configuration.status == "success"
            else "no_match"
        ),
        retrieval_mode=(
            "hybrid_exact_scope_pgvector"
            if candidates and model_configuration.status == "success"
            else (
                "exact_scope"
                if model_configuration.status == "success"
                else "semantic_pgvector"
            )
        ),
        knowledge_version=settings.VERSION,
        score_threshold=score_threshold,
        candidates=candidates,
        model_configuration=model_configuration,
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
    seen_events: dict[str, RetrievalQualityEvent] = {}
    score_threshold = _active_retrieval_score_threshold(db)

    for candidate in body.items:
        candidate_source_kind = _retrieval_source_kind(candidate)
        existing = seen_events.get(candidate.idempotency_key)
        if existing is None:
            existing = (
                db.query(RetrievalQualityEvent)
                .filter(
                    RetrievalQualityEvent.idempotency_key
                    == candidate.idempotency_key
                )
                .first()
            )
        if existing:
            if (
                existing.conversation_id != candidate.conversation_id
                or existing.request_id != candidate.request_id
                or existing.source_kind != candidate_source_kind
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "IDEMPOTENCY_IDENTITY_CONFLICT",
                        "message": (
                            "同一 idempotency_key 不得关联不同的 "
                            "conversation_id/request_id/source_kind"
                        ),
                    },
                )
            reused += 1
            results.append(
                RetrievalQualityEventResult(
                    idempotency_key=existing.idempotency_key,
                    conversation_id=existing.conversation_id,
                    request_id=existing.request_id,
                    status="reused",
                    outcome=existing.outcome,
                    event_id=existing.id,
                )
            )
            continue

        evaluated_candidate = candidate.model_copy(
            update={"score_threshold": score_threshold}
        )
        dimensions = _retrieval_feedback_dimensions(evaluated_candidate)
        outcome = dimensions["outcome"]
        candidate_snapshot = _retrieval_candidate_snapshot(candidate)
        latency_ms = _metadata_value(candidate, "latency_ms")
        event = RetrievalQualityEvent(
            id=f"rqe-{uuid.uuid4().hex[:12]}",
            idempotency_key=candidate.idempotency_key,
            source_system=candidate.source_system,
            conversation_id=candidate.conversation_id,
            request_id=candidate.request_id,
            source_kind=candidate_source_kind,
            query_text=candidate.query,
            candidate_count=candidate.candidate_count,
            top_knowledge_id=candidate.top_knowledge_id,
            top_rerank_score=candidate.top_rerank_score,
            score_threshold=score_threshold,
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
        try:
            with db.begin_nested():
                db.add(event)
                db.flush()
        except IntegrityError:
            existing = (
                db.query(RetrievalQualityEvent)
                .filter(
                    RetrievalQualityEvent.idempotency_key
                    == candidate.idempotency_key
                )
                .first()
            )
            if existing is None:
                raise
            if (
                existing.conversation_id != candidate.conversation_id
                or existing.request_id != candidate.request_id
                or existing.source_kind != candidate_source_kind
            ):
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "code": "IDEMPOTENCY_IDENTITY_CONFLICT",
                        "message": (
                            "同一 idempotency_key 不得关联不同的 "
                            "conversation_id/request_id/source_kind"
                        ),
                    },
                )
            seen_events[candidate.idempotency_key] = existing
            reused += 1
            results.append(
                RetrievalQualityEventResult(
                    idempotency_key=existing.idempotency_key,
                    conversation_id=existing.conversation_id,
                    request_id=existing.request_id,
                    status="reused",
                    outcome=existing.outcome,
                    event_id=existing.id,
                )
            )
            continue
        seen_events[candidate.idempotency_key] = event
        recorded += 1
        results.append(
            RetrievalQualityEventResult(
                idempotency_key=event.idempotency_key,
                conversation_id=event.conversation_id,
                request_id=event.request_id,
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
    page: int = 1,
    page_size: int = 20,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
):
    page = max(1, int(page or 1))
    page_size = max(1, min(100, int(page_size or 20)))
    start_at = (
        start_at.astimezone(timezone.utc).replace(tzinfo=None)
        if start_at is not None and start_at.tzinfo is not None
        else start_at
    )
    end_at = (
        end_at.astimezone(timezone.utc).replace(tzinfo=None)
        if end_at is not None and end_at.tzinfo is not None
        else end_at
    )
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="开始时间必须早于结束时间",
        )
    summary = {
        "total": 0,
        "accepted": 0,
        "accepted_alternative": 0,
        "low_score": 0,
        "no_candidates": 0,
        "not_selected": 0,
        "technical_failure": 0,
        "successful_requests": 0,
        "candidate_requests": 0,
        "candidate_queries": 0,
        "near_threshold": 0,
        "clear_threshold": 0,
        "threshold_below": 0,
        "top_selected": 0,
        "alternative_selected": 0,
        "none_selected": 0,
        "reviewed": 0,
        "training_eligible": 0,
    }
    latest_request_events = (
        db.query(RetrievalQualityEvent)
        .filter(
            RetrievalQualityEvent.id.in_(
                latest_retrieval_quality_request_event_ids(
                    start_at=start_at,
                    end_at=end_at,
                )
            )
        )
        .all()
    )
    request_payloads = [
        _retrieval_request_payload(events)
        for events in _retrieval_request_event_groups(
            latest_request_events
        )
    ]
    request_payloads.sort(
        key=lambda item: (
            item["created_at"] is not None,
            item["created_at"],
            str(item["id"] or ""),
        ),
        reverse=True,
    )
    for item in request_payloads:
        outcome = str(item["outcome"] or "")
        if outcome in summary:
            summary[outcome] += 1
        if item["request_status"] in ("success", "no_match", "fallback"):
            summary["successful_requests"] += 1
        if (
            item["candidate_count"] > 0
            and item["request_status"] in ("success", "fallback")
        ):
            summary["candidate_requests"] += 1
            top_score = item["top_rerank_score"]
            if top_score is not None:
                score_margin = round(
                    float(top_score) - float(item["score_threshold"]),
                    10,
                )
                if score_margin < 0:
                    summary["threshold_below"] += 1
                elif score_margin < RETRIEVAL_NEAR_THRESHOLD_MARGIN:
                    summary["near_threshold"] += 1
                else:
                    summary["clear_threshold"] += 1
        if item["selection_status"] == "top_selected":
            summary["top_selected"] += 1
        elif item["selection_status"] == "alternative_selected":
            summary["alternative_selected"] += 1
        elif item["selection_status"] == "none_selected":
            summary["none_selected"] += 1
        if item["review_status"] == "confirmed":
            summary["reviewed"] += 1
        if item["training_eligible"]:
            summary["training_eligible"] += 1
    summary["candidate_queries"] = summary["candidate_requests"]
    summary["total"] = len(request_payloads)

    risk_items = [
        item
        for item in request_payloads
        if (
            item["outcome"] not in ("accepted", "accepted_alternative")
            or item["review_status"] == "unreviewed"
        )
    ]
    risk_total = len(risk_items)
    risk_total_pages = max(1, (risk_total + page_size - 1) // page_size)
    page = min(page, risk_total_pages)
    risks = risk_items[
        (page - 1) * page_size : page * page_size
    ]

    def rate(numerator: int, denominator: int) -> float:
        return round(numerator / denominator, 4) if denominator else 0.0

    rates = {
        "candidate_coverage_rate": rate(
            summary["candidate_queries"],
            summary["successful_requests"],
        ),
        "near_threshold_rate": rate(
            summary["near_threshold"],
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
        float(item["total_latency_ms"])
        for item in request_payloads[:5000]
        if item["total_latency_ms"] is not None
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
            "near_threshold_rate": (
                "状态为成功或回退且有候选的请求中，"
                "最高分达到当次阈值、但高出不足 0.05 的比例"
            ),
            "top1_selection_rate": "有候选请求中最终采用所属候选池第一名的比例",
            "alternative_selection_rate": "有候选请求中最终采用所属候选池第二至第五名的比例",
            "no_selection_rate": "有候选请求中最终没有采用任何候选的比例",
            "review_coverage_rate": "已由人工明确原因和正确目标的请求比例",
        },
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": risk_total,
            "total_pages": risk_total_pages,
        },
        "time_range": {
            "start_at": start_at,
            "end_at": end_at,
        },
        "risks": risks,
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
            KnowledgeOriginOption(
                value="model_configuration",
                label="机型配置信息",
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
    seen_ingestions: dict[str, IntegrationIngestion] = {}

    for candidate in body.items:
        payload, selection, review_metadata, review_status = _candidate_queue_state(
            candidate
        )
        existing = seen_ingestions.get(candidate.idempotency_key)
        if existing is None:
            existing = (
                db.query(IntegrationIngestion)
                .filter(IntegrationIngestion.idempotency_key == candidate.idempotency_key)
                .first()
            )
        if existing:
            _refresh_unreviewed_candidate(
                existing,
                candidate,
                payload=payload,
                selection=selection,
                review_metadata=review_metadata,
                review_status=review_status,
            )
            seen_ingestions[candidate.idempotency_key] = existing
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
        try:
            with db.begin_nested():
                db.add(ingestion)
                db.flush()
        except IntegrityError:
            existing = (
                db.query(IntegrationIngestion)
                .filter(IntegrationIngestion.idempotency_key == candidate.idempotency_key)
                .first()
            )
            if existing is None:
                raise
            _refresh_unreviewed_candidate(
                existing,
                candidate,
                payload=payload,
                selection=selection,
                review_metadata=review_metadata,
                review_status=review_status,
            )
            seen_ingestions[candidate.idempotency_key] = existing
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
        seen_ingestions[candidate.idempotency_key] = ingestion
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
    try:
        _ensure_candidate_origin_is_writable(knowledge)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="机型配置信息由飞书专用同步维护，历史候选不能人工修改。",
        ) from exc
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
            _ensure_candidate_origin_is_writable(
                normalized_payload["knowledge"]
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
                if raw_error
                in {
                    "CATEGORY_NOT_FOUND",
                    "DUPLICATE_BLOCKED",
                    "KNOWLEDGE_ORIGIN_MANAGED",
                }
                else "CANDIDATE_PAYLOAD_INVALID"
            )
            error_message = {
                "CATEGORY_NOT_FOUND": "category_id does not exist in the current taxonomy.",
                "DUPLICATE_BLOCKED": "Candidate matches an existing knowledge item and was not ingested.",
                "KNOWLEDGE_ORIGIN_MANAGED": (
                    "机型配置信息由飞书专用同步维护，历史候选不能提交入库。"
                ),
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
