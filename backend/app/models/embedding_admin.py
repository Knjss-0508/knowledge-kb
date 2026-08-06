from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    Column,
    DateTime,
    Float,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)

from app.core.database import Base


class EmbeddingRuntimeConfig(Base):
    """Versioned runtime configuration for retrieval and duplicate detection."""

    __tablename__ = "embedding_runtime_configs"

    id = Column(String(64), primary_key=True)
    version = Column(Integer, nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False, default="draft", index=True)
    config = Column(JSON, nullable=False, default=dict)
    evaluation_metrics = Column(JSON, nullable=False, default=dict)
    change_reason = Column(Text, nullable=False, default="")
    created_by = Column(String(128), nullable=False)
    activated_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    activated_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_embedding_runtime_config_status",
        ),
    )


class EmbeddingTrainingSample(Base):
    """Human-reviewed sample used by offline embedding evaluation or training."""

    __tablename__ = "embedding_training_samples"

    id = Column(String(64), primary_key=True)
    task_type = Column(String(32), nullable=False, index=True)
    query_text = Column(Text, nullable=False)
    positive_text = Column(Text, nullable=False, default="")
    negative_texts = Column(JSON, nullable=False, default=list)
    source_type = Column(String(32), nullable=False, default="manual", index=True)
    source_id = Column(String(128), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="candidate", index=True)
    reason = Column(Text, nullable=False, default="")
    sample_metadata = Column(JSON, nullable=False, default=dict)
    created_by = Column(String(128), nullable=False)
    reviewed_by = Column(String(128), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    reviewed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "task_type IN ('retrieval', 'deduplication')",
            name="ck_embedding_training_sample_task_type",
        ),
        CheckConstraint(
            "status IN ('candidate', 'verified', 'excluded')",
            name="ck_embedding_training_sample_status",
        ),
        UniqueConstraint(
            "source_type",
            "source_id",
            name="uq_embedding_training_sample_source",
        ),
    )


class EmbeddingTrainingJob(Base):
    """Persisted offline training job claimed by a local GPU runner."""

    __tablename__ = "embedding_training_jobs"

    id = Column(String(64), primary_key=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    stage = Column(String(64), nullable=False, default="queued")
    progress = Column(Float, nullable=False, default=0.0)
    base_model = Column(String(256), nullable=False)
    candidate_model_name = Column(String(256), nullable=False, unique=True, index=True)
    train_type = Column(String(32), nullable=False, default="lora")
    training_config = Column(JSON, nullable=False, default=dict)
    dataset_hash = Column(String(64), nullable=False, index=True)
    dataset_payload = Column(JSON, nullable=False, default=list)
    sample_count = Column(Integer, nullable=False, default=0)
    train_count = Column(Integer, nullable=False, default=0)
    validation_count = Column(Integer, nullable=False, default=0)
    test_count = Column(Integer, nullable=False, default=0)
    metrics = Column(JSON, nullable=False, default=dict)
    log_tail = Column(Text, nullable=False, default="")
    artifact_uri = Column(String(1024), nullable=False, default="")
    artifact_sha256 = Column(String(64), nullable=False, default="")
    runner_id = Column(String(128), nullable=True, index=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    requested_by = Column(String(128), nullable=False)
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ("
            "'queued', 'claimed', 'running', 'evaluating', 'completed', "
            "'failed', 'cancelled'"
            ")",
            name="ck_embedding_training_job_status",
        ),
        CheckConstraint(
            "train_type IN ('lora', 'full')",
            name="ck_embedding_training_job_train_type",
        ),
    )


class EmbeddingModelVersion(Base):
    """Candidate and deployed embedding model registry."""

    __tablename__ = "embedding_model_versions"

    id = Column(String(64), primary_key=True)
    model_name = Column(String(256), nullable=False, unique=True, index=True)
    base_model = Column(String(256), nullable=False)
    training_job_id = Column(String(64), nullable=False, unique=True, index=True)
    status = Column(String(32), nullable=False, default="candidate", index=True)
    dimension = Column(Integer, nullable=False, default=1024)
    metrics = Column(JSON, nullable=False, default=dict)
    artifact_uri = Column(String(1024), nullable=False, default="")
    artifact_sha256 = Column(String(64), nullable=False, default="")
    release_notes = Column(Text, nullable=False, default="")
    approved_by = Column(String(128), nullable=True)
    approved_at = Column(DateTime, nullable=True)
    deployed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected', 'deployed', 'retired')",
            name="ck_embedding_model_version_status",
        ),
    )


class EmbeddingTrainingRunner(Base):
    """Last-known health and GPU capability of an outbound local runner."""

    __tablename__ = "embedding_training_runners"

    id = Column(String(128), primary_key=True)
    name = Column(String(128), nullable=False)
    hostname = Column(String(256), nullable=False, default="")
    status = Column(String(32), nullable=False, default="online", index=True)
    gpu_name = Column(String(256), nullable=False, default="")
    gpu_memory_mb = Column(Integer, nullable=False, default=0)
    gpu_free_memory_mb = Column(Integer, nullable=False, default=0)
    cuda_version = Column(String(64), nullable=False, default="")
    runner_version = Column(String(64), nullable=False, default="")
    current_job_id = Column(String(64), nullable=True, index=True)
    runner_metadata = Column(JSON, nullable=False, default=dict)
    last_seen_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(
        DateTime,
        nullable=False,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('online', 'busy', 'offline', 'error')",
            name="ck_embedding_training_runner_status",
        ),
    )
