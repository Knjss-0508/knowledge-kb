from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String, UniqueConstraint, event
from sqlalchemy.dialects.postgresql import JSONB

from app.core.database import Base


# PostgreSQL 用 jsonb（按键取值不必反复解析文本）；SQLite 没有 jsonb，
# 单元测试与本地隔离库回落到普通 json。
_JsonDocument = JSON().with_variant(JSONB(), "postgresql")


class IntegrationIngestion(Base):
    __tablename__ = "integration_ingestions"

    id = Column(String(64), primary_key=True)
    event_id = Column(String(128), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    source_system = Column(String(64), nullable=False, index=True)
    source_conversation_id = Column(String(128), nullable=False, index=True)
    source_conversation_url = Column(String(1024), nullable=True)
    source_message_ids = Column(JSON, default=list)
    redaction_status = Column(String(32), nullable=False, default="redacted")
    processing_metadata = Column(JSON, default=dict)
    # 这三列在候选列表里被反复按键取值，生产库用 jsonb 存二进制结构，
    # 避免每个 `->` 表达式都重新解析整份文本（实测 7.3s -> 1.5s）。
    # SQLite 没有 jsonb，测试环境回落到普通 json。
    selection_metadata = Column(_JsonDocument, default=dict)
    candidate_payload = Column(_JsonDocument, default=dict)
    review_metadata = Column(_JsonDocument, default=dict)
    review_status = Column(String(32), nullable=True, index=True)
    reviewed_by = Column(String(128), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    status = Column(String(64), nullable=False, index=True)
    knowledge_id = Column(String(64), nullable=True, index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class CandidateModelAutomation(Base):
    __tablename__ = "candidate_model_automation"

    id = Column(String(32), primary_key=True)
    enabled = Column(Boolean, nullable=False, default=False)
    requested_by = Column(String(64), nullable=True)
    consecutive_network_failures = Column(Integer, nullable=False, default=0)
    paused_at = Column(DateTime, nullable=True)
    pause_reason = Column(String(512), nullable=False, default="")
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class CandidateModelTask(Base):
    __tablename__ = "candidate_model_tasks"

    id = Column(String(64), primary_key=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    source = Column(String(16), nullable=False, default="manual")
    requested_by = Column(String(64), nullable=False)
    ingestion_ids = Column(JSON, nullable=False, default=list)
    results = Column(JSON, nullable=False, default=list)
    lease_token = Column(String(64), nullable=True)
    lease_expires_at = Column(DateTime, nullable=True)
    current_ingestion_id = Column(String(64), nullable=True)
    error_message = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConfidenceTrainingSettingsRecord(Base):
    """One durable, local-only configuration record for confidence evaluation."""

    __tablename__ = "confidence_training_settings"

    id = Column(String(64), primary_key=True)
    settings = Column(JSON, nullable=False, default=dict)
    updated_by = Column(String(128), nullable=False, default="system")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class ConfidenceTrainingJob(Base):
    """A manually created local rehearsal task for confidence-model training."""

    __tablename__ = "confidence_training_jobs"

    id = Column(String(64), primary_key=True)
    status = Column(String(48), nullable=False, default="waiting_for_internal_trainer", index=True)
    stage = Column(String(128), nullable=False, default="训练输入已冻结，等待内部训练器")
    model_name = Column(String(256), nullable=False, default="deepseek-flash")
    evaluation_scope = Column(String(32), nullable=False, default="shadow_only")
    dataset_hash = Column(String(64), nullable=False, index=True)
    dataset_payload = Column(JSON, nullable=False, default=list)
    sample_count = Column(Integer, nullable=False, default=0)
    train_count = Column(Integer, nullable=False, default=0)
    validation_count = Column(Integer, nullable=False, default=0)
    test_count = Column(Integer, nullable=False, default=0)
    requested_by = Column(String(128), nullable=False)
    error_message = Column(String(2000), nullable=False, default="")
    analysis_result = Column(JSON, nullable=False, default=dict)
    candidate_prompt = Column(String(20000), nullable=False, default="")
    resolved_model_version = Column(String(256), nullable=False, default="")
    shadow_evaluation = Column(JSON, nullable=False, default=dict)
    # Human disposition for any sample made worse by a candidate prompt.  This
    # is deliberately separate from the candidate-review human truth.
    regression_reviews = Column(JSON, nullable=False, default=dict)
    # Candidate prompts are immutable revisions; a newer candidate must never
    # silently replace the one that was evaluated before it.
    prompt_versions = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)


class EvaluationSample(Base):
    """Append-only source snapshot for reproducible model-quality evaluation."""

    __tablename__ = "evaluation_samples"

    id = Column(String(64), primary_key=True)
    snapshot_id = Column(String(64), nullable=False, index=True)
    candidate_id = Column(String(64), nullable=False, index=True)
    source_batch = Column(String(256), nullable=False, default="")
    topic_group = Column(String(512), nullable=False, index=True)
    draft_version = Column(String(128), nullable=False)
    draft_snapshot = Column(JSON, nullable=False, default=dict)
    published_knowledge_snapshot = Column(JSON, nullable=False, default=dict)
    draft_revision_history = Column(JSON, nullable=False, default=list)
    model_output = Column(JSON, nullable=False, default=dict)
    model_version = Column(String(256), nullable=False, default="")
    prompt_version = Column(String(128), nullable=False, default="")
    human_truth = Column(JSON, nullable=False, default=dict)
    reviewed_at = Column(DateTime, nullable=True)
    dataset_split = Column(String(16), nullable=False, index=True)
    sample_hash = Column(String(64), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    __table_args__ = (
        UniqueConstraint(
            "snapshot_id",
            "candidate_id",
            name="uq_evaluation_samples_snapshot_candidate",
        ),
    )


@event.listens_for(EvaluationSample, "before_update")
def _reject_evaluation_sample_update(_mapper, _connection, _target) -> None:
    raise ValueError("evaluation_sample records are immutable")


@event.listens_for(EvaluationSample, "before_delete")
def _reject_evaluation_sample_delete(_mapper, _connection, _target) -> None:
    raise ValueError("evaluation_sample records are immutable")


class RetrievalQualityEvent(Base):
    __tablename__ = "retrieval_quality_events"

    id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    source_system = Column(String(64), nullable=False, index=True)
    conversation_id = Column(String(128), nullable=True, index=True)
    question_form_id = Column(String(64), nullable=True, index=True)
    conversation_id_kind = Column(String(32), nullable=True)
    work_order_verified = Column(Boolean, nullable=True)
    request_id = Column(String(80), nullable=True, index=True)
    source_kind = Column(
        String(16),
        nullable=False,
        default="combined",
        index=True,
    )
    query_text = Column(String(1000), nullable=False, index=True)
    candidate_count = Column(Integer, nullable=False, default=0)
    top_knowledge_id = Column(String(64), nullable=True, index=True)
    top_rerank_score = Column(Float, nullable=True)
    score_threshold = Column(Float, nullable=False)
    selected = Column(Boolean, nullable=False, default=False)
    outcome = Column(String(32), nullable=False, index=True)
    schema_version = Column(Integer, nullable=False, default=1)
    request_status = Column(String(32), nullable=False, default="success", index=True)
    threshold_status = Column(String(32), nullable=False, default="not_applicable", index=True)
    selection_status = Column(String(32), nullable=False, default="not_evaluated", index=True)
    selected_knowledge_id = Column(String(64), nullable=True, index=True)
    selected_candidate_rank = Column(Integer, nullable=True)
    expected_knowledge_id = Column(String(64), nullable=True, index=True)
    feedback_type = Column(String(32), nullable=False, default="none", index=True)
    failure_reason = Column(String(64), nullable=False, default="", index=True)
    candidate_snapshot = Column(JSON, default=list)
    embedding_model = Column(String(256), nullable=False, default="")
    reranker_model = Column(String(256), nullable=False, default="")
    prompt_version = Column(String(128), nullable=False, default="")
    retrieval_latency_ms = Column(Float, nullable=True)
    rerank_latency_ms = Column(Float, nullable=True)
    total_latency_ms = Column(Float, nullable=True)
    training_eligible = Column(Boolean, nullable=False, default=False, index=True)
    review_status = Column(String(32), nullable=False, default="unreviewed", index=True)
    reviewed_by = Column(String(128), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    event_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
