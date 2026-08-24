from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class EmbeddingRuntimeConfigValues(BaseModel):
    dedup_block_threshold: float = Field(0.96, ge=0, le=1)
    dedup_review_threshold: float = Field(0.88, ge=0, le=1)
    dedup_max_candidates: int = Field(10, ge=1, le=100)
    dedup_min_semantic_content_chars: int = Field(8, ge=1, le=1000)
    dedup_min_containment_content_chars: int = Field(12, ge=1, le=1000)
    search_chunk_size: int = Field(800, ge=100, le=8000)
    search_chunk_overlap: int = Field(120, ge=0, le=4000)
    retrieval_score_threshold: float = Field(0.42, ge=0, le=1)
    retrieval_headquarters_standard_top_k: int = Field(5, ge=1, le=10)
    retrieval_business_accumulation_top_k: int = Field(5, ge=1, le=10)
    retrieval_default_top_k: int = Field(10, ge=1, le=100)
    training_min_verified_samples: int = Field(20, ge=1, le=1000000)
    training_trigger_new_samples: int = Field(100, ge=1, le=1000000)
    training_schedule_days: int = Field(7, ge=1, le=365)
    minimum_recall_at_10: float = Field(0.8, ge=0, le=1)
    maximum_false_block_rate: float = Field(0.01, ge=0, le=1)

    @model_validator(mode="after")
    def validate_thresholds(self):
        if self.dedup_review_threshold >= self.dedup_block_threshold:
            raise ValueError("查重复核阈值必须低于查重阻断阈值")
        if self.search_chunk_overlap >= self.search_chunk_size:
            raise ValueError("分块重叠长度必须小于分块长度")
        return self


class EmbeddingRuntimeConfigCreate(BaseModel):
    config: EmbeddingRuntimeConfigValues
    change_reason: str = Field(..., min_length=1, max_length=1000)
    evaluation_metrics: dict[str, Any] = Field(default_factory=dict)
    activate: bool = False


class EmbeddingLabSearchRequest(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    query: str = Field(..., min_length=1, max_length=1000)
    top_k: int = Field(10, ge=1, le=50)
    applicable_category_id: str = Field(..., max_length=128)
    applicable_brand_id: str | None = Field(None, max_length=128)
    applicable_model_id: str | None = Field(None, max_length=128)

    @model_validator(mode="after")
    def validate_scope_filters(self):
        for field_name in (
            "applicable_category_id",
            "applicable_brand_id",
            "applicable_model_id",
        ):
            raw_value: str | None = getattr(self, field_name)
            setattr(
                self,
                field_name,
                raw_value.strip() if raw_value and raw_value.strip() else None,
            )
        if not self.applicable_category_id:
            raise ValueError("必须选择适用品类")
        if self.applicable_brand_id and not self.applicable_category_id:
            raise ValueError("选择品牌前必须先选择适用品类")
        if self.applicable_model_id and not self.applicable_brand_id:
            raise ValueError("选择机型前必须先选择品牌")
        return self


class EmbeddingTrainingSampleCreate(BaseModel):
    task_type: Literal["retrieval", "deduplication"]
    query_text: str = Field(..., min_length=1, max_length=20000)
    positive_text: str = Field("", max_length=50000)
    negative_texts: list[str] = Field(default_factory=list, max_length=50)
    source_type: str = Field("manual", min_length=1, max_length=32)
    source_id: str | None = Field(None, max_length=128)
    status: Literal["candidate", "verified", "excluded"] = "candidate"
    reason: str = Field("", max_length=2000)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_verified_sample(self):
        self.negative_texts = [
            value.strip()
            for value in self.negative_texts
            if isinstance(value, str) and value.strip()
        ]
        if self.status == "verified" and not self.positive_text.strip():
            raise ValueError("已确认样本必须填写正确知识内容")
        return self


class EmbeddingTrainingSampleUpdate(BaseModel):
    task_type: Literal["retrieval", "deduplication"] | None = None
    query_text: str | None = Field(None, min_length=1, max_length=20000)
    positive_text: str | None = Field(None, max_length=50000)
    negative_texts: list[str] | None = Field(None, max_length=50)
    status: Literal["candidate", "verified", "excluded"] | None = None
    reason: str | None = Field(None, max_length=2000)
    metadata: dict[str, Any] | None = None


class EmbeddingTrainingJobCreate(BaseModel):
    train_type: Literal["lora", "full"] = "lora"
    candidate_model_name: str | None = Field(None, max_length=256)
    training_config: dict[str, Any] = Field(default_factory=dict)
    include_task_types: list[Literal["retrieval", "deduplication"]] = Field(
        default_factory=lambda: ["retrieval", "deduplication"],
        min_length=1,
    )


class EmbeddingModelDecision(BaseModel):
    action: Literal["approve", "reject"]
    release_notes: str = Field("", max_length=4000)


class EmbeddingRunnerHeartbeat(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=128)
    name: str = Field(..., min_length=1, max_length=128)
    hostname: str = Field("", max_length=256)
    status: Literal["online", "busy", "error"] = "online"
    gpu_name: str = Field("", max_length=256)
    gpu_memory_mb: int = Field(0, ge=0)
    gpu_free_memory_mb: int = Field(0, ge=0)
    cuda_version: str = Field("", max_length=64)
    runner_version: str = Field("", max_length=64)
    current_job_id: str | None = Field(None, max_length=64)
    metadata: dict[str, Any] = Field(default_factory=dict)


class EmbeddingRunnerClaim(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=128)


class EmbeddingRunnerProgress(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=128)
    status: Literal["claimed", "running", "evaluating"] = "running"
    stage: str = Field(..., min_length=1, max_length=64)
    progress: float = Field(..., ge=0, le=100)
    log_tail: str = Field("", max_length=20000)
    lease_seconds: int = Field(180, ge=30, le=1800)


class EmbeddingRunnerComplete(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=128)
    metrics: dict[str, Any] = Field(default_factory=dict)
    artifact_uri: str = Field(..., min_length=1, max_length=1024)
    artifact_sha256: str = Field("", max_length=64)
    dimension: int = Field(1024, ge=1, le=65536)
    log_tail: str = Field("", max_length=20000)


class EmbeddingRunnerFailure(BaseModel):
    runner_id: str = Field(..., min_length=1, max_length=128)
    error_message: str = Field(..., min_length=1, max_length=10000)
    log_tail: str = Field("", max_length=20000)
    retryable: bool = False


class RetrievalQualityReview(BaseModel):
    review_status: Literal["confirmed", "excluded"]
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
    reason: str = Field("", max_length=2000)
    training_eligible: bool = False

    @model_validator(mode="after")
    def validate_training_label(self):
        if self.training_eligible and self.review_status != "confirmed":
            raise ValueError("只有已确认事件才能进入训练样本")
        if self.training_eligible and not self.expected_knowledge_id:
            raise ValueError("进入训练样本前必须指定正确知识")
        return self
