from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from app.schemas.knowledge import CategoryResponse, TagDimensionResponse


class IntegrationSource(BaseModel):
    system: str = Field(..., min_length=1, max_length=64, description="上游系统标识")
    conversation_id: str = Field(..., min_length=1, max_length=128, description="上游会话ID")
    conversation_url: str | None = Field(None, max_length=1024, description="原会话受控访问链接")
    message_ids: list[str] = Field(default=[], description="用于生成知识的消息ID列表")
    redaction_status: Literal["redacted", "not_required"] = Field(
        "redacted", description="会话是否已完成脱敏"
    )


class IntegrationProcessing(BaseModel):
    summary_version: str = Field(..., min_length=1, max_length=64, description="会话浓缩版本")
    label_model: str = Field(..., min_length=1, max_length=128, description="自动标注模型或规则版本")
    skill_name: str = Field(..., min_length=1, max_length=128, description="知识改写 Skill 名称")
    skill_version: str = Field(..., min_length=1, max_length=64, description="知识改写 Skill 版本")
    prompt_version: str | None = Field(None, max_length=64, description="改写提示词版本")
    model_name: str | None = Field(None, max_length=128, description="执行改写的模型")


class IntegrationSelection(BaseModel):
    eligible: bool = Field(..., description="是否通过上游入库筛选")
    confidence: float = Field(..., ge=0, le=1, description="自动化综合置信度")
    duplicate_fingerprint: str | None = Field(
        None, max_length=128, description="上游去重指纹"
    )
    reasons: list[str] = Field(default=[], description="筛选或质量判断依据")


class IntegrationKnowledgePayload(BaseModel):
    title: str = Field(..., min_length=1, max_length=256, description="知识标题")
    subtitles: list[str] = Field(default=[], description="副标题列表")
    content: Any = Field(..., description="改写后的知识内容，支持富文本 blocks 结构")
    category_id: str = Field(..., min_length=1, max_length=64, description="知识库分类ID")
    layer: str = Field(..., pattern=r"^L[1-3]$", description="L1/L2/L3 知识层级")
    scene_tags: list[str] = Field(default=[], description="场景标签")
    applicable_business_types: list[Any] = Field(default=[], description="适用业务")
    applicable_categories: list[Any] = Field(default=[], description="适用类目")
    applicable_brands: list[Any] = Field(default=[], description="适用品牌")
    applicable_models: list[Any] = Field(default=[], description="适用机型")
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
    event_id: str = Field(..., min_length=1, max_length=128, description="上游事件ID")
    idempotency_key: str = Field(
        ..., min_length=1, max_length=128, description="幂等键，同一业务事件必须稳定不变"
    )
    source: IntegrationSource
    processing: IntegrationProcessing
    selection: IntegrationSelection
    knowledge: IntegrationKnowledgePayload


class IntegrationCandidateBatch(BaseModel):
    items: list[IntegrationCandidate] = Field(
        ..., min_length=1, max_length=100, description="候选知识列表"
    )


class IntegrationDedupMatch(BaseModel):
    knowledge_id: str
    title: str
    status: Literal["review", "published"]
    category_id: str
    layer: Literal["L1", "L2", "L3"]
    match_type: Literal["exact", "semantic", "content_containment"]
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
    status: Literal["review_submitted", "rejected", "reused"]
    ingestion_id: str | None = None
    knowledge_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    deduplication: IntegrationDedupResponse | None = None


class IntegrationCandidateBatchResponse(BaseModel):
    accepted: int
    rejected: int
    reused: int
    results: list[IntegrationCandidateResult]


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


class IntegrationLayerDefinition(BaseModel):
    value: Literal["L1", "L2", "L3"]
    label: str
    description: str


class IntegrationTaxonomyResponse(BaseModel):
    version: str
    layers: list[IntegrationLayerDefinition]
    categories: list[CategoryResponse]
    tag_dimensions: list[TagDimensionResponse]


class RetrievalQualityEventPayload(BaseModel):
    idempotency_key: str = Field(..., min_length=1, max_length=128)
    source_system: str = Field(..., min_length=1, max_length=64)
    query: str = Field(..., min_length=1, max_length=1000)
    conversation_id: str | None = Field(None, max_length=128)
    candidate_count: int = Field(..., ge=0)
    top_knowledge_id: str | None = Field(None, max_length=64)
    top_rerank_score: float | None = Field(None, ge=0, le=1)
    score_threshold: float = Field(..., ge=0, le=1)
    selected: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("top_rerank_score")
    @classmethod
    def score_requires_candidate(cls, value: float | None, info) -> float | None:
        if info.data.get("candidate_count", 0) > 0 and value is None:
            raise ValueError("top_rerank_score is required when candidate_count is greater than 0")
        return value


class RetrievalQualityEventBatch(BaseModel):
    items: list[RetrievalQualityEventPayload] = Field(..., min_length=1, max_length=100)


class RetrievalQualityEventResult(BaseModel):
    idempotency_key: str
    status: Literal["recorded", "reused"]
    outcome: Literal["accepted", "low_score", "no_candidates", "not_selected"]
    event_id: str


class RetrievalQualityEventBatchResponse(BaseModel):
    recorded: int
    reused: int
    results: list[RetrievalQualityEventResult]
