from datetime import datetime

from sqlalchemy import Boolean, Column, DateTime, Float, Integer, JSON, String

from app.core.database import Base


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
    selection_metadata = Column(JSON, default=dict)
    candidate_payload = Column(JSON, default=dict)
    review_metadata = Column(JSON, default=dict)
    review_status = Column(String(32), nullable=True, index=True)
    reviewed_by = Column(String(128), nullable=True)
    reviewed_at = Column(DateTime, nullable=True)
    submitted_at = Column(DateTime, nullable=True)
    status = Column(String(32), nullable=False, index=True)
    knowledge_id = Column(String(64), nullable=True, index=True)
    error_code = Column(String(64), nullable=True)
    error_message = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


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


class RetrievalQualityEvent(Base):
    __tablename__ = "retrieval_quality_events"

    id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    source_system = Column(String(64), nullable=False, index=True)
    conversation_id = Column(String(128), nullable=True, index=True)
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
