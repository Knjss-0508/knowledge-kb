from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class HighFrequencySourceRecord(Base):
    """A durable, current version of one upstream work order."""

    __tablename__ = "high_frequency_source_records"

    id = Column(String(64), primary_key=True)
    record_id = Column(String(256), nullable=False, unique=True, index=True)
    source_hash = Column(String(64), nullable=False, index=True)
    analyzed_at = Column(String(64), nullable=False, default="")
    analyzed_on = Column(Date, nullable=True, index=True)
    category = Column(String(128), nullable=False, default="", index=True)
    level1 = Column(String(128), nullable=False, default="", index=True)
    level2 = Column(String(128), nullable=False, default="", index=True)
    chat_content = Column(Text, nullable=False, default="")
    core_problem = Column(Text, nullable=False, default="")
    judgment = Column(Text, nullable=False, default="")
    basis = Column(Text, nullable=False, default="")
    reference_reply = Column(Text, nullable=False, default="")
    raw_payload = Column(JSON, nullable=False, default=dict)
    source_possibly_truncated = Column(Boolean, nullable=False, default=False)
    source_request_count = Column(Integer, nullable=False, default=1)
    processing_status = Column(String(32), nullable=False, default="queued", index=True)
    model_name = Column(String(256), nullable=False, default="")
    prompt_version = Column(String(128), nullable=False, default="")
    rule_version = Column(String(128), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    processed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    atomic_questions = relationship(
        "HighFrequencyAtomicQuestion",
        back_populates="source_record",
        cascade="all, delete-orphan",
    )
    processing_tasks = relationship(
        "HighFrequencyProcessingTask",
        back_populates="source_record",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "processing_status IN ('queued', 'running', 'ready', 'review', 'failed')",
            name="ck_hf_source_record_status",
        ),
    )


class HighFrequencyStandardQuestion(Base):
    """A versioned standard-question dictionary scoped to one product category."""

    __tablename__ = "high_frequency_standard_questions"

    id = Column(String(64), primary_key=True)
    category = Column(String(128), nullable=False, index=True)
    normalized_question = Column(String(512), nullable=False)
    standard_question = Column(String(512), nullable=False)
    business_theme = Column(String(256), nullable=False, default="")
    model_name = Column(String(256), nullable=False, default="")
    prompt_version = Column(String(128), nullable=False, default="")
    rule_version = Column(String(128), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    atomic_questions = relationship(
        "HighFrequencyAtomicQuestion",
        back_populates="standard_question",
    )

    __table_args__ = (
        UniqueConstraint(
            "category",
            "normalized_question",
            "prompt_version",
            name="uq_hf_standard_question_version",
        ),
    )


class HighFrequencyAtomicQuestion(Base):
    """An auditable atomic question extracted from one source record."""

    __tablename__ = "high_frequency_atomic_questions"

    id = Column(String(64), primary_key=True)
    source_record_id = Column(
        String(64),
        ForeignKey("high_frequency_source_records.id"),
        nullable=False,
        index=True,
    )
    atomic_index = Column(Integer, nullable=False)
    atomic_question = Column(Text, nullable=False)
    normalized_question = Column(String(512), nullable=False, index=True)
    split_status = Column(String(64), nullable=False, default="单问题")
    processing_status = Column(String(32), nullable=False, default="queued", index=True)
    standard_question_id = Column(
        String(64),
        ForeignKey("high_frequency_standard_questions.id"),
        nullable=True,
        index=True,
    )
    mapping_source = Column(String(32), nullable=False, default="")
    model_name = Column(String(256), nullable=False, default="")
    prompt_version = Column(String(128), nullable=False, default="")
    rule_version = Column(String(128), nullable=False, default="")
    error_message = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    source_record = relationship("HighFrequencySourceRecord", back_populates="atomic_questions")
    standard_question = relationship("HighFrequencyStandardQuestion", back_populates="atomic_questions")

    __table_args__ = (
        UniqueConstraint(
            "source_record_id",
            "normalized_question",
            name="uq_hf_atomic_question_source_normalized",
        ),
        CheckConstraint(
            "processing_status IN ('queued', 'ready', 'review', 'failed')",
            name="ck_hf_atomic_question_status",
        ),
    )


class HighFrequencyProcessingTask(Base):
    """A lease-based durable task for a new or changed source record."""

    __tablename__ = "high_frequency_processing_tasks"

    id = Column(String(64), primary_key=True)
    source_record_id = Column(
        String(64),
        ForeignKey("high_frequency_source_records.id"),
        nullable=False,
        index=True,
    )
    source_hash = Column(String(64), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="queued", index=True)
    attempt_count = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    lease_expires_at = Column(DateTime, nullable=True, index=True)
    error_message = Column(Text, nullable=False, default="")
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    source_record = relationship("HighFrequencySourceRecord", back_populates="processing_tasks")

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'superseded')",
            name="ck_hf_processing_task_status",
        ),
        Index(
            "ix_hf_processing_task_ready",
            "status",
            "next_attempt_at",
            "lease_expires_at",
        ),
    )
