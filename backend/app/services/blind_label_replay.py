"""Offline semantic-retrieval baseline for explicitly selected blind-label reviews."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.routes import integration
from app.schemas.integration import IntegrationStandardSearchOrderInfo, IntegrationStandardSearchRequest
from app.services.blind_label_learning import LearningReviewError, load_learning_review
from app.services.embedding import EmbeddingServiceUnavailable


class ReplayOrderScope(IntegrationStandardSearchOrderInfo):
    model_config = ConfigDict(extra="forbid", protected_namespaces=())


class ReplayRequest(BaseModel):
    # Only the fields consumed by semantic retrieval. No original identities or transcript.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True, protected_namespaces=())

    normalized_question: str = Field(alias="normalizedQuestion", min_length=1, max_length=1000)
    business_type: Literal["self_operated", "aggregated"] = Field(alias="businessType")
    product_type: str = Field("", alias="productType", max_length=500)
    category_id: str = Field("", alias="categoryId", max_length=128)
    brand: str = Field("", max_length=500)
    brand_id: str = Field("", alias="brandId", max_length=128)
    model: str = Field("", max_length=500)
    model_id: str = Field("", alias="modelId", max_length=128)
    order_info: ReplayOrderScope = Field(default_factory=ReplayOrderScope, alias="orderInfo")
    limit: int = Field(5, ge=1, le=20)
    candidate_pool_limit: int | None = Field(None, alias="candidatePoolLimit", ge=1, le=20)

    def to_search_request(self) -> IntegrationStandardSearchRequest:
        return IntegrationStandardSearchRequest.model_validate({
            **self.model_dump(by_alias=True),
            # Required only by the shared data contract. No HTTP request or telemetry.
            "conversationId": "0", "requestId": "offline-replay",
        })


class ReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    work_order_id: str = Field(min_length=1, max_length=64)
    review_id: int = Field(gt=0)
    request: ReplayRequest


class ReplayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cases: list[ReplayCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def unique_orders(self):
        if len({case.work_order_id for case in self.cases}) != len(self.cases):
            raise ValueError("同一工单不能重复计入基线")
        return self


def _target_rank(groups, knowledge_id: str):
    for origin, ranked in groups.items():
        for rank, (item, score) in enumerate(ranked, start=1):
            if item.id == knowledge_id:
                return origin, rank, float(score)
    return None, None, None


def run_retrieval_replay(db: Session, manifest: ReplayManifest) -> dict:
    """Compute per-origin ranks before and after the configured threshold."""
    config = integration.get_active_runtime_values(db)
    rows = []
    for case in manifest.cases:
        row = {"work_order_id": case.work_order_id, "review_id": case.review_id}
        rows.append(row)
        try:
            detail = load_learning_review(db, case.work_order_id)
        except LearningReviewError as exc:
            if exc.status_code != 409:
                raise
            row["status"] = "review_changed_or_ineligible"
            continue
        review = detail.get("review") or {}
        if not detail["replay_eligible"] or review.get("id") != case.review_id:
            row["status"] = "review_changed_or_ineligible"
            continue
        try:
            ranked, returned, threshold = integration.rank_standard_semantic_request(
                db, case.request.to_search_request(), runtime_config=config,
            )
        except EmbeddingServiceUnavailable:
            row["status"] = "embedding_unavailable"
            continue

        target = review["knowledge_id"]
        origin, rank, score = _target_rank(ranked, target)
        _, returned_rank, _ = _target_rank(returned, target)
        if returned_rank is not None:
            status = "returned"
        elif rank is None:
            status = "not_in_search_window"
        elif score < threshold:
            status = "filtered_by_threshold"
        else:
            status = "not_returned"
        row.update(
            issue_type=review["issue_type"], knowledge_id=target, origin=origin,
            search_rank=rank, returned_rank=returned_rank, score=score,
            threshold=threshold, status=status,
        )

    evaluated = [row for row in rows if "returned_rank" in row]
    total = len(evaluated)
    top3_hits = sum(row["returned_rank"] is not None and row["returned_rank"] <= 3 for row in evaluated)
    return {
        "schema_version": 1,
        "scope": "semantic_provider_retrieval_only",
        "rank_scope": "within_knowledge_origin",
        "summary": {
            "requested": len(rows), "evaluated": total, "not_evaluated": len(rows) - total,
            "search_window_hits": sum(row["search_rank"] is not None for row in evaluated),
            "returned_hits": sum(row["returned_rank"] is not None for row in evaluated),
            "top3_hits": top3_hits,
            "top3_hit_rate": top3_hits / total if total else None,
        },
        "cases": rows,
    }
