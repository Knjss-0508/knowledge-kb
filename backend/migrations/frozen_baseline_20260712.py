"""Frozen schema used only by the 20260712 baseline migration.

Do not import current application models here. New schema changes must be
implemented as new Alembic revisions so fresh databases replay the same
historical sequence as existing deployments.
"""

import enum

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.orm import DeclarativeBase


FROZEN_EMBEDDING_DIMENSIONS = 1024


class FrozenBase(DeclarativeBase):
    pass


class FrozenKnowledgeLayer(str, enum.Enum):
    L1 = "L1"
    L2 = "L2"
    L3 = "L3"


class FrozenKnowledgeStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class FrozenUser(FrozenBase):
    __tablename__ = "users"

    id = sa.Column(sa.String(64), primary_key=True)
    username = sa.Column(sa.String(64), unique=True, nullable=False, index=True)
    password_hash = sa.Column(sa.String(256), nullable=False)
    role = sa.Column(sa.String(32), nullable=False)
    is_active = sa.Column(sa.Boolean, nullable=False)
    created_at = sa.Column(sa.DateTime)
    updated_at = sa.Column(sa.DateTime)


class FrozenUserSession(FrozenBase):
    __tablename__ = "user_sessions"

    token_hash = sa.Column(sa.String(64), primary_key=True)
    user_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    expires_at = sa.Column(sa.DateTime, nullable=False, index=True)
    created_at = sa.Column(sa.DateTime, nullable=False)


class FrozenCategory(FrozenBase):
    __tablename__ = "categories"

    id = sa.Column(sa.String(64), primary_key=True)
    name = sa.Column(sa.String(128), nullable=False)
    parent_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("categories.id"),
        nullable=True,
        index=True,
    )
    level = sa.Column(sa.Integer)
    sort_order = sa.Column(sa.Integer)
    created_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint("name", "parent_id", name="uq_category_name_parent"),
    )


class FrozenKnowledge(FrozenBase):
    __tablename__ = "knowledge_items"

    id = sa.Column(sa.String(64), primary_key=True)
    title = sa.Column(sa.String(256), nullable=False, index=True)
    subtitles = sa.Column(sa.JSON, nullable=True)
    content = sa.Column(sa.JSON, nullable=False)
    layer = sa.Column(
        sa.Enum(FrozenKnowledgeLayer, name="knowledgelayer"),
        nullable=False,
        index=True,
    )
    category_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("categories.id"),
        nullable=False,
        index=True,
    )
    status = sa.Column(
        sa.Enum(FrozenKnowledgeStatus, name="knowledgestatus"),
        index=True,
    )
    source = sa.Column(sa.String(32))
    source_session_id = sa.Column(sa.String(128), nullable=True)
    quality_score = sa.Column(sa.Float)
    applicable_scenes = sa.Column(sa.JSON)
    applicable_business_types = sa.Column(sa.JSON)
    applicable_categories = sa.Column(sa.JSON)
    applicable_brands = sa.Column(sa.JSON)
    applicable_models = sa.Column(sa.JSON)
    deduplication_metadata = sa.Column(sa.JSON)
    is_model_personal = sa.Column(sa.String(16))
    created_by = sa.Column(sa.String(128), nullable=False)
    updated_by = sa.Column(sa.String(128), nullable=True)
    updated_at = sa.Column(sa.DateTime)
    created_at = sa.Column(sa.DateTime)


class FrozenKnowledgeChangeLog(FrozenBase):
    __tablename__ = "knowledge_change_logs"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    changed_by = sa.Column(sa.String(128), nullable=False)
    changed_fields = sa.Column(sa.JSON, nullable=False)
    before_data = sa.Column(sa.JSON, nullable=False)
    after_data = sa.Column(sa.JSON, nullable=False)
    created_at = sa.Column(sa.DateTime, nullable=False, index=True)


class FrozenKnowledgeEmbedding(FrozenBase):
    __tablename__ = "knowledge_embeddings"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    embedding_model = sa.Column(sa.String(256), nullable=False, index=True)
    embedding_dimension = sa.Column(sa.Integer, nullable=False)
    content_hash = sa.Column(sa.String(64), nullable=False, index=True)
    embedding = sa.Column(sa.JSON, nullable=False)
    embedding_vector = sa.Column(Vector(FROZEN_EMBEDDING_DIMENSIONS), nullable=True)
    title_embedding_vector = sa.Column(
        Vector(FROZEN_EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    content_embedding_vector = sa.Column(
        Vector(FROZEN_EMBEDDING_DIMENSIONS),
        nullable=True,
    )
    created_at = sa.Column(sa.DateTime)
    updated_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint(
            "knowledge_id",
            "embedding_model",
            name="uq_knowledge_embedding_model",
        ),
    )


class FrozenKnowledgeSearchEmbedding(FrozenBase):
    __tablename__ = "knowledge_search_embeddings"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    embedding_model = sa.Column(sa.String(256), nullable=False, index=True)
    embedding_kind = sa.Column(sa.String(32), nullable=False, index=True)
    chunk_index = sa.Column(sa.Integer, nullable=False)
    content_hash = sa.Column(sa.String(64), nullable=False, index=True)
    source_text = sa.Column(sa.Text, nullable=False)
    embedding_dimension = sa.Column(sa.Integer, nullable=False)
    embedding = sa.Column(sa.JSON, nullable=False)
    embedding_vector = sa.Column(Vector(FROZEN_EMBEDDING_DIMENSIONS), nullable=True)
    created_at = sa.Column(sa.DateTime)
    updated_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint(
            "knowledge_id",
            "embedding_model",
            "embedding_kind",
            "chunk_index",
            name="uq_knowledge_search_embedding",
        ),
    )


class FrozenKnowledgeDeduplicationFeedback(FrozenBase):
    __tablename__ = "knowledge_deduplication_feedback"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    matched_knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    verdict = sa.Column(sa.String(32), nullable=False)
    reason = sa.Column(sa.Text, nullable=False)
    submitted_by = sa.Column(sa.String(128), nullable=False)
    created_at = sa.Column(sa.DateTime)
    updated_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint(
            "knowledge_id",
            "matched_knowledge_id",
            "submitted_by",
            name="uq_dedup_feedback_submitter",
        ),
    )


class FrozenKnowledgeMedia(FrozenBase):
    __tablename__ = "knowledge_media"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    media_type = sa.Column(sa.String(16), nullable=False)
    filename = sa.Column(sa.String(256), nullable=False)
    original_name = sa.Column(sa.String(256), nullable=False)
    file_path = sa.Column(sa.String(512), nullable=False)
    file_size = sa.Column(sa.Integer)
    mime_type = sa.Column(sa.String(128))
    alt = sa.Column(sa.String(256))
    caption = sa.Column(sa.Text)
    duration = sa.Column(sa.String(32))
    sort_order = sa.Column(sa.Integer)
    created_at = sa.Column(sa.DateTime)


class FrozenTagDimension(FrozenBase):
    __tablename__ = "tag_dimensions"

    id = sa.Column(sa.String(64), primary_key=True)
    name = sa.Column(sa.String(64), nullable=False, unique=True)
    created_at = sa.Column(sa.DateTime)


class FrozenTagValue(FrozenBase):
    __tablename__ = "tag_values"

    id = sa.Column(sa.String(64), primary_key=True)
    dimension_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("tag_dimensions.id"),
        nullable=False,
        index=True,
    )
    value = sa.Column(sa.String(128), nullable=False)
    created_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint(
            "dimension_id",
            "value",
            name="uq_tag_value_per_dim",
        ),
    )


class FrozenKnowledgeTag(FrozenBase):
    __tablename__ = "knowledge_tags"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        index=True,
    )
    tag_value_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("tag_values.id"),
        nullable=False,
        index=True,
    )
    created_at = sa.Column(sa.DateTime)

    __table_args__ = (
        sa.UniqueConstraint(
            "knowledge_id",
            "tag_value_id",
            name="uq_knowledge_tag",
        ),
    )


class FrozenUsageStat(FrozenBase):
    __tablename__ = "usage_stats"

    id = sa.Column(sa.String(64), primary_key=True)
    knowledge_id = sa.Column(
        sa.String(64),
        sa.ForeignKey("knowledge_items.id"),
        nullable=False,
        unique=True,
    )
    recommend_count = sa.Column(sa.Integer)
    click_count = sa.Column(sa.Integer)
    feedback_score = sa.Column(sa.Float)
    last_used_at = sa.Column(sa.DateTime, nullable=True)


class FrozenIntegrationIngestion(FrozenBase):
    __tablename__ = "integration_ingestions"

    id = sa.Column(sa.String(64), primary_key=True)
    event_id = sa.Column(sa.String(128), nullable=False, index=True)
    idempotency_key = sa.Column(
        sa.String(128),
        nullable=False,
        unique=True,
        index=True,
    )
    source_system = sa.Column(sa.String(64), nullable=False, index=True)
    source_conversation_id = sa.Column(
        sa.String(128),
        nullable=False,
        index=True,
    )
    source_conversation_url = sa.Column(sa.String(1024), nullable=True)
    source_message_ids = sa.Column(sa.JSON)
    redaction_status = sa.Column(sa.String(32), nullable=False)
    processing_metadata = sa.Column(sa.JSON)
    selection_metadata = sa.Column(sa.JSON)
    status = sa.Column(sa.String(32), nullable=False, index=True)
    knowledge_id = sa.Column(sa.String(64), nullable=True, index=True)
    error_code = sa.Column(sa.String(64), nullable=True)
    error_message = sa.Column(sa.String(512), nullable=True)
    created_at = sa.Column(sa.DateTime)
    updated_at = sa.Column(sa.DateTime)


class FrozenRetrievalQualityEvent(FrozenBase):
    __tablename__ = "retrieval_quality_events"

    id = sa.Column(sa.String(64), primary_key=True)
    idempotency_key = sa.Column(
        sa.String(128),
        nullable=False,
        unique=True,
        index=True,
    )
    source_system = sa.Column(sa.String(64), nullable=False, index=True)
    conversation_id = sa.Column(sa.String(128), nullable=True, index=True)
    query_text = sa.Column(sa.String(1000), nullable=False, index=True)
    candidate_count = sa.Column(sa.Integer, nullable=False)
    top_knowledge_id = sa.Column(sa.String(64), nullable=True, index=True)
    top_rerank_score = sa.Column(sa.Float, nullable=True)
    score_threshold = sa.Column(sa.Float, nullable=False)
    selected = sa.Column(sa.Boolean, nullable=False)
    outcome = sa.Column(sa.String(32), nullable=False, index=True)
    event_metadata = sa.Column(sa.JSON)
    created_at = sa.Column(sa.DateTime, index=True)
