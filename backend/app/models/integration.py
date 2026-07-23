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


class RetrievalQualityEvent(Base):
    __tablename__ = "retrieval_quality_events"

    id = Column(String(64), primary_key=True)
    idempotency_key = Column(String(128), nullable=False, unique=True, index=True)
    source_system = Column(String(64), nullable=False, index=True)
    conversation_id = Column(String(128), nullable=True, index=True)
    query_text = Column(String(1000), nullable=False, index=True)
    candidate_count = Column(Integer, nullable=False, default=0)
    top_knowledge_id = Column(String(64), nullable=True, index=True)
    top_rerank_score = Column(Float, nullable=True)
    score_threshold = Column(Float, nullable=False)
    selected = Column(Boolean, nullable=False, default=False)
    outcome = Column(String(32), nullable=False, index=True)
    event_metadata = Column(JSON, default=dict)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
