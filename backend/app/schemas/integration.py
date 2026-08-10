from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.knowledge import (
    BusinessType,
    BusinessTypeOption,
    CategoryResponse,
    KnowledgeOrigin,
    KnowledgeOriginOption,
    TagDimensionResponse,
)


class IntegrationSource(BaseModel):
    system: str = Field(..., min_length=1, max_length=64, description="上游系统标识")
    conversation_id: str = Field(..., min_length=1, max_length=128, description="上游会话ID")
    conversation_url: str | None = Field(None, max_length=1024, description="原会话受控访问链接")
    message_ids: list[str] = Field(default=[], description="用于生成知识的消息ID列表")
    redaction_status: Literal["redacted", "not_required"] = Field(
        "redacted", description="会话是否已完成脱敏"
    )


class IntegrationProcessing(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    summary_version: str = Field(..., min_length=1, max_length=64, description="会话浓缩版本")
    label_model: str = Field(..., min_length=1, max_length=128, description="自动标注模型或规则版本")
    skill_name: str | None = Field(None, min_length=1, max_length=128, description="兼容旧调用的知识改写 Skill 名称")
    skill_version: str | None = Field(None, min_length=1, max_length=64, description="兼容旧调用的知识改写 Skill 版本")
    plugin_name: str | None = Field(None, min_length=1, max_length=128, description="知识改写插件名称")
    plugin_version: str | None = Field(None, min_length=1, max_length=64, description="知识改写插件版本")
    prompt_version: str | None = Field(None, max_length=64, description="改写提示词版本")
    model_name: str | None = Field(None, max_length=128, description="执行改写的模型")

    @model_validator(mode="after")
    def processing_extension_must_be_complete(self):
        if bool(self.plugin_name) != bool(self.plugin_version):
            raise ValueError("plugin_name and plugin_version must be provided together")
        if bool(self.skill_name) != bool(self.skill_version):
            raise ValueError("skill_name and skill_version must be provided together")
        if not self.plugin_name and not self.skill_name:
            raise ValueError("plugin_name/plugin_version are required")
        return self


class IntegrationSelection(BaseModel):
    eligible: bool = Field(..., description="是否通过上游入库筛选")
    confidence: float = Field(..., ge=0, le=1, description="自动化综合置信度")
    duplicate_fingerprint: str | None = Field(
        None, max_length=128, description="上游去重指纹"
    )
    reasons: list[str] = Field(default=[], description="筛选或质量判断依据")


class IntegrationModelReview(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    status: str | None = Field(None, max_length=64, description="模型初标状态")
    decision: str | None = Field(None, max_length=64, description="模型初标结论")
    knowledge_value: str | None = Field(None, max_length=64, description="模型判断的沉淀价值")
    reason: str | None = Field(None, max_length=4000, description="模型初标原因")
    error_type: str | None = Field(None, max_length=128, description="模型识别的错误类型")
    standard_consistency: str | None = Field(None, max_length=64)
    evidence_sufficiency: str | None = Field(None, max_length=64)
    content_consistency: str | None = Field(None, max_length=64)
    image_necessity: str | None = Field(None, max_length=64)
    title_quality: str | None = Field(None, max_length=64)
    confidence: float | None = Field(None, ge=0, le=1)
    priority_review: bool = False
    provider: str | None = Field(None, max_length=128)
    model_name: str | None = Field(None, max_length=128)
    prompt_version: str | None = Field(None, max_length=128)
    run_id: str | None = Field(None, max_length=128)


class IntegrationHumanReview(BaseModel):
    knowledge_value: str | None = Field(None, max_length=32)
    usability: str | None = Field(None, max_length=32)
    modification_notes: str | None = Field(None, max_length=4000)
    feedback: str | None = Field(None, max_length=4000)
    decision: str | None = Field(None, max_length=64)
    error_type: str | None = Field(None, max_length=128)
    training_eligible: str | None = Field(None, max_length=32)
    notes: str | None = Field(None, max_length=4000)
    reviewer: str | None = Field(None, max_length=128)
    reviewed_at: datetime | None = None


class IntegrationKnowledgePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=256, description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表")
    content: Any = Field(..., description="改写后的知识内容，支持富文本 blocks 结构")
    knowledge_origin: KnowledgeOrigin = Field(..., description="知识来源")
    business_type: BusinessType = Field(..., description="知识所属业务类型")
    category_id: str = Field(..., min_length=1, max_length=64, description="知识库分类ID")
    scene_tags: list[str] = Field(default=[], description="场景标签")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
    related_standard_items: list[str] = Field(default=[], description="关联标准项")
    recommended_reply: str | None = Field(None, max_length=4000, description="推荐回复")
    evidence_excerpt: str | None = Field(
        None, max_length=4000, description="已脱敏的关键证据摘要"
    )

    @field_validator("category_id")
    @classmethod
    def category_id_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("category_id must not be blank")
        return value


class IntegrationCandidate(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    event_id: str = Field(..., min_length=1, max_length=128, description="上游事件ID")
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128, description="幂等键，同一业务事件必须稳定不变"
    )
    source: IntegrationSource
    processing: IntegrationProcessing
    selection: IntegrationSelection
    knowledge: IntegrationKnowledgePayload
    model_review: IntegrationModelReview | None = None
    human_review: IntegrationHumanReview | None = None


class IntegrationCandidateBatch(BaseModel):
    items: list[IntegrationCandidate] = Field(
        ..., min_length=1, max_length=100, description="候选知识列表"
    )


class IntegrationDedupMatch(BaseModel):
    knowledge_id: str
    title: str
    status: Literal["review", "published"]
    knowledge_origin: KnowledgeOrigin
    business_type: BusinessType
    category_id: str
    match_type: Literal["exact", "title_exact", "semantic", "content_containment"]
    similarity: float = Field(..., ge=0, le=1)
    title_similarity: float | None = Field(None, ge=0, le=1)
    content_similarity: float | None = Field(None, ge=0, le=1)


class IntegrationDedupResponse(BaseModel):
    action: Literal["create", "review_duplicate", "block_duplicate"]
    embedding_model: str
    content_hash: str
    block_threshold: float
    review_threshold: float
    matches: list[IntegrationDedupMatch]


class IntegrationDedupCheckRequest(BaseModel):
    knowledge: IntegrationKnowledgePayload
    exclude_knowledge_id: str | None = Field(
        None,
        max_length=64,
        description="编辑已有知识时排除自身；自动化新建时不要传递",
    )


class IntegrationCandidateResult(BaseModel):
    event_id: str
    idempotency_key: str
    status: Literal["review_submitted", "review_required", "rejected", "reused"]
    ingestion_id: str | None = None
    knowledge_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    deduplication: IntegrationDedupResponse | None = None


class IntegrationCandidateBatchResponse(BaseModel):
    accepted: int
    review_required: int
    rejected: int
    reused: int
    results: list[IntegrationCandidateResult]


class IntegrationCandidateQueueResult(BaseModel):
    event_id: str
    idempotency_key: str
    status: Literal["queued", "ready", "rejected", "reused"]
    ingestion_id: str
    review_status: str


class IntegrationCandidateQueueBatchResponse(BaseModel):
    queued: int
    ready: int
    rejected: int
    reused: int
    results: list[IntegrationCandidateQueueResult]


class CandidateReviewUpdate(BaseModel):
    title: str | None = Field(None, min_length=1, max_length=256)
    subtitles: list[str] | None = None
    content: Any | None = None
    knowledge_origin: KnowledgeOrigin | None = None
    business_type: BusinessType | None = None
    category_id: str | None = Field(None, min_length=1, max_length=64)
    applicable_scenes: list[str] | None = None
    applicable_categories: list[Any] | None = None
    applicable_brands: list[Any] | None = None
    applicable_models: list[Any] | None = None
    related_standard_items: list[str] | None = None
    recommended_reply: str | None = Field(None, max_length=4000)
    knowledge_value: str | None = Field(None, max_length=32)
    usability: str | None = Field(None, max_length=32)
    modification_notes: str | None = Field(None, max_length=4000)
    feedback: str | None = Field(None, max_length=4000)
    decision: str | None = Field(None, max_length=64)
    error_type: str | None = Field(None, max_length=128)
    training_eligible: str | None = Field(None, max_length=32)
    notes: str | None = Field(None, max_length=4000)
    confirm_dedup_review: bool | None = Field(
        None,
        description="已对比当前疑似重复命中并确认内容确实不同",
    )

    @field_validator("knowledge_origin", "business_type")
    @classmethod
    def taxonomy_field_must_not_be_null(
        cls,
        value: KnowledgeOrigin | BusinessType | None,
    ) -> KnowledgeOrigin | BusinessType | None:
        if value is None:
            raise ValueError("taxonomy field must not be null")
        return value


class CandidateReviewListItem(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    id: str
    event_id: str
    source_system: str
    source_conversation_id: str
    source_conversation_url: str | None = None
    review_status: str
    status: str
    title: str
    subtitles: list[str] = Field(default_factory=list)
    content: Any
    knowledge_origin: KnowledgeOrigin
    business_type: BusinessType
    category_id: str
    applicable_scenes: list[str] = Field(default_factory=list)
    applicable_categories: list[Any] = Field(default_factory=list)
    applicable_brands: list[Any] = Field(default_factory=list)
    applicable_models: list[Any] = Field(default_factory=list)
    related_standard_items: list[str] = Field(default_factory=list)
    recommended_reply: str | None = None
    evidence_excerpt: str | None = None
    selection: dict[str, Any] = Field(default_factory=dict)
    model_review: dict[str, Any] = Field(default_factory=dict)
    human_review: dict[str, Any] = Field(default_factory=dict)
    priority_review: bool = False
    deduplication: IntegrationDedupResponse | None = None
    deduplication_confirmed: bool = False
    deduplication_only: bool = False
    knowledge_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    reviewed_by: str | None = None
    reviewed_at: datetime | None = None
    submitted_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class CandidateReviewListResponse(BaseModel):
    total: int
    summary: dict[str, int]
    items: list[CandidateReviewListItem]


class CandidateReviewBatchSubmit(BaseModel):
    ingestion_ids: list[str] = Field(..., min_length=1, max_length=100)


class CandidateReviewSubmitResult(BaseModel):
    ingestion_id: str
    status: Literal["submitted", "failed", "reused"]
    knowledge_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class CandidateReviewBatchSubmitResponse(BaseModel):
    submitted: int
    failed: int
    reused: int
    results: list[CandidateReviewSubmitResult]


class IntegrationIngestionResponse(BaseModel):
    id: str
    event_id: str
    idempotency_key: str
    source_system: str
    source_conversation_id: str
    status: str
    knowledge_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
    updated_at: datetime


class IntegrationTaxonomyResponse(BaseModel):
    version: str
    knowledge_origins: list[KnowledgeOriginOption]
    business_types: list[BusinessTypeOption]
    categories: list[CategoryResponse]
    tag_dimensions: list[TagDimensionResponse]


class IntegrationStandardSearchOrderInfo(BaseModel):
    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    category: str = Field("", max_length=500)
    category_id: str = Field("", alias="categoryId", max_length=128)
    brand: str = Field("", max_length=500)
    brand_id: str = Field("", alias="brandId", max_length=128)
    model: str = Field("", max_length=500)
    model_id: str = Field("", alias="modelId", max_length=128)

    @field_validator(
        "category",
        "category_id",
        "brand",
        "brand_id",
        "model",
        "model_id",
    )
    @classmethod
    def normalize_scope_value(cls, value: str) -> str:
        return value.strip()


class IntegrationStandardSearchRequest(BaseModel):
    """答疑智能推荐助手 external-standard-provider 的请求契约。"""

    model_config = ConfigDict(populate_by_name=True, protected_namespaces=())

    conversation_id: str = Field(
        ...,
        alias="conversationId",
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]{1,64}$",
        description="插件从当前页面读取的原始工单ID，服务器不得生成或改写。",
    )
    request_id: str = Field(
        ...,
        alias="requestId",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9._:-]{1,80}$",
        description="插件生成的单次请求标识，服务器必须原样回传。",
    )
    normalized_question: str = Field(
        ...,
        alias="normalizedQuestion",
        min_length=1,
        max_length=8000,
    )
    knowledge_origin: KnowledgeOrigin | None = Field(
        None,
        alias="knowledgeOrigin",
        description=(
            "兼容旧客户端的知识来源字段；标准检索当前固定同时检索"
            "总部标准和业务沉淀。"
        ),
    )
    business_type: BusinessType | None = Field(None, alias="businessType")
    product_type: str = Field("", alias="productType", max_length=500)
    category_id: str = Field("", alias="categoryId", max_length=128)
    brand: str = Field("", max_length=500)
    brand_id: str = Field("", alias="brandId", max_length=128)
    model: str = Field("", max_length=500)
    model_id: str = Field("", alias="modelId", max_length=128)
    order_info: IntegrationStandardSearchOrderInfo = Field(
        default_factory=IntegrationStandardSearchOrderInfo,
        alias="orderInfo",
    )
    part_terms: list[str] = Field(default_factory=list, alias="partTerms", max_length=100)
    phenomenon_terms: list[str] = Field(
        default_factory=list,
        alias="phenomenonTerms",
        max_length=100,
    )
    category_intent: list[str] = Field(
        default_factory=list,
        alias="categoryIntent",
        max_length=100,
    )
    limit: int = Field(
        5,
        ge=1,
        le=20,
        description="每个知识来源的候选上限，服务端最多取 5 条。",
    )

    @field_validator("normalized_question")
    @classmethod
    def normalized_question_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("normalizedQuestion must not be blank")
        return value

    @field_validator(
        "product_type",
        "category_id",
        "brand",
        "brand_id",
        "model",
        "model_id",
    )
    @classmethod
    def normalize_scope_value(cls, value: str) -> str:
        return value.strip()

    @field_validator("part_terms", "phenomenon_terms", "category_intent")
    @classmethod
    def normalize_context_terms(cls, values: list[str]) -> list[str]:
        normalized: list[str] = []
        for value in values:
            item = value.strip()
            if not item:
                continue
            if len(item) > 500:
                raise ValueError("context term must not exceed 500 characters")
            normalized.append(item)
        return normalized


class IntegrationStandardSearchCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: str
    title: str
    text: str
    score: float = Field(ge=0, le=1)
    final_score: float = Field(alias="finalScore", ge=0, le=1)
    status: Literal["published"] = "published"
    knowledge_origin: KnowledgeOrigin = Field(alias="knowledgeOrigin")
    business_type: BusinessType = Field(alias="businessType")
    category_id: str | None = Field(None, alias="categoryId")
    level1_label: str = Field("", alias="level1Label")
    product_type: str = Field("", alias="productType")
    models: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    source_ref: str = Field(alias="sourceRef")


class IntegrationStandardSearchResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    conversation_id: str = Field(
        alias="conversationId",
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]{1,64}$",
    )
    request_id: str = Field(
        alias="requestId",
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9._:-]{1,80}$",
    )
    provider: str
    status: Literal["success", "no_match"]
    retrieval_mode: str = Field(alias="retrievalMode")
    knowledge_version: str = Field(alias="knowledgeVersion")
    score_threshold: float = Field(alias="scoreThreshold", ge=0, le=1)
    candidates: list[IntegrationStandardSearchCandidate]


class RetrievalQualityCandidatePayload(BaseModel):
    knowledge_id: str = Field(..., min_length=1, max_length=64)
    rank: int = Field(..., ge=1, le=100)
    title: str = Field("", max_length=256)
    embedding_score: float | None = Field(None, ge=0, le=1)
    rerank_score: float | None = Field(None, ge=0, le=1)
    final_score: float | None = Field(None, ge=0, le=1)
    selected: bool = False


class RetrievalQualityEventPayload(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    source_system: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=1000)
    conversation_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[0-9]{1,64}$",
    )
    request_id: str = Field(
        ...,
        min_length=1,
        max_length=80,
        pattern=r"^[A-Za-z0-9._:-]{1,80}$",
    )
    schema_version: int = Field(1, ge=1, le=10)
    request_status: Literal[
        "success",
        "no_match",
        "timeout",
        "error",
        "invalid_response",
        "fallback",
    ] = "success"
    candidate_count: int = Field(..., ge=0)
    top_knowledge_id: str | None = Field(None, max_length=64)
    top_rerank_score: float | None = Field(None, ge=0, le=1)
    score_threshold: float = Field(..., ge=0, le=1)
    selected: bool = False
    selected_knowledge_id: str | None = Field(None, max_length=64)
    selected_candidate_rank: int | None = Field(None, ge=1, le=100)
    expected_knowledge_id: str | None = Field(None, max_length=64)
    feedback_type: Literal["none", "helpful", "unhelpful", "corrected"] = "none"
    failure_reason: Literal[
        "",
        "knowledge_missing",
        "wrong_ranking",
        "wrong_scope",
        "stale_content",
        "threshold_too_high",
        "threshold_too_low",
        "query_misunderstood",
        "candidate_irrelevant",
        "answer_not_used",
        "technical_failure",
        "user_correction",
        "user_unhelpful",
        "unknown",
    ] = ""
    candidates: list[RetrievalQualityCandidatePayload] = Field(
        default_factory=list,
        max_length=100,
    )
    embedding_model: str = Field("", max_length=256)
    reranker_model: str = Field("", max_length=256)
    prompt_version: str = Field("", max_length=128)
    retrieval_latency_ms: float | None = Field(None, ge=0)
    rerank_latency_ms: float | None = Field(None, ge=0)
    total_latency_ms: float | None = Field(None, ge=0)
    metadata: dict[str, Any] = Field(
        default_factory=dict,
        validate_default=True,
    )

    @field_validator("metadata")
    @classmethod
    def require_candidate_source_pool(cls, value: dict[str, Any]) -> dict[str, Any]:
        source_kind = value.get("source_kind")
        if not isinstance(source_kind, str):
            raise ValueError("metadata.source_kind must be reply or standard")
        normalized_source_kind = source_kind.strip().lower()
        if normalized_source_kind not in {"reply", "standard"}:
            raise ValueError("metadata.source_kind must be reply or standard")
        return {
            **value,
            "source_kind": normalized_source_kind,
        }

    @field_validator("top_rerank_score")
    @classmethod
    def score_requires_candidate(cls, value: float | None, info) -> float | None:
        if info.data.get("candidate_count", 0) > 0 and value is None:
            raise ValueError("top_rerank_score is required when candidate_count is greater than 0")
        return value

    @model_validator(mode="after")
    def validate_feedback_dimensions(self):
        if self.candidates:
            ranks = [item.rank for item in self.candidates]
            if len(ranks) != len(set(ranks)):
                raise ValueError("candidate ranks must be unique")
            if self.candidate_count != len(self.candidates):
                raise ValueError("candidate_count must match candidates length")
        if self.selected_candidate_rank is not None and not self.selected_knowledge_id:
            raise ValueError("selected_knowledge_id is required when selected_candidate_rank is set")
        return self


class RetrievalQualityEventBatch(BaseModel):
    items: list[RetrievalQualityEventPayload] = Field(..., min_length=1, max_length=100)


class RetrievalQualityEventResult(BaseModel):
    idempotency_key: str
    conversation_id: str
    request_id: str
    status: Literal["recorded", "reused"]
    outcome: Literal[
        "accepted",
        "accepted_alternative",
        "low_score",
        "no_candidates",
        "not_selected",
        "technical_failure",
    ]
    event_id: str


class RetrievalQualityEventBatchResponse(BaseModel):
    recorded: int
    reused: int
    results: list[RetrievalQualityEventResult]
