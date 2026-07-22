import enum
from datetime import datetime

from sqlalchemy import (
    Column, String, Text, Enum, Float, Integer, DateTime,
    ForeignKey, JSON, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector

from app.core.database import Base
from app.core.config import settings


class KnowledgeStatus(str, enum.Enum):
    DRAFT = "draft"
    REVIEW = "review"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class Knowledge(Base):
    __tablename__ = "knowledge_items"

    id = Column(String(64), primary_key=True)
    title = Column(String(256), nullable=False, index=True)
    subtitles = Column(JSON, default=list, nullable=True)
    content = Column(JSON, nullable=False, default=dict)
    # content blocks 支持三种类型:
    # {"type":"text", "value":"文本内容"}
    # {"type":"image", "media_id":"media-xxx", "alt":"图片描述", "caption":"图片说明文字"}
    # {"type":"video", "media_id":"media-xxx", "alt":"视频描述", "caption":"视频说明文字", "duration":"03:20"}
    category_id = Column(String(64), ForeignKey("categories.id"), nullable=False, index=True)
    status = Column(Enum(KnowledgeStatus), default=KnowledgeStatus.DRAFT, index=True)
    source = Column(String(32), default="manual")
    source_session_id = Column(String(128), nullable=True)
    quality_score = Column(Float, default=0.0)
    applicable_scenes = Column(JSON, default=list)
    applicable_categories = Column(JSON, default=list)
    applicable_brands = Column(JSON, default=list)
    applicable_models = Column(JSON, default=list)
    deduplication_metadata = Column(JSON, default=dict)
    created_by = Column(String(128), nullable=False)
    updated_by = Column(String(128), nullable=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    created_at = Column(DateTime, default=datetime.utcnow)

    category = relationship("Category", back_populates="knowledge_items")
    tags = relationship("KnowledgeTag", back_populates="knowledge_item", cascade="all, delete-orphan")
    usage_stats = relationship("UsageStat", back_populates="knowledge_item", uselist=False, cascade="all, delete-orphan")
    media = relationship("KnowledgeMedia", back_populates="knowledge_item", cascade="all, delete-orphan")
    change_logs = relationship("KnowledgeChangeLog", back_populates="knowledge_item", cascade="all, delete-orphan")
    embeddings = relationship("KnowledgeEmbedding", back_populates="knowledge_item", cascade="all, delete-orphan")
    search_embeddings = relationship("KnowledgeSearchEmbedding", back_populates="knowledge_item", cascade="all, delete-orphan")
    deduplication_feedbacks = relationship(
        "KnowledgeDeduplicationFeedback",
        back_populates="knowledge_item",
        foreign_keys="KnowledgeDeduplicationFeedback.knowledge_id",
        cascade="all, delete-orphan",
    )


class KnowledgeChangeLog(Base):
    __tablename__ = "knowledge_change_logs"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    changed_by = Column(String(128), nullable=False)
    changed_fields = Column(JSON, default=list, nullable=False)
    before_data = Column(JSON, default=dict, nullable=False)
    after_data = Column(JSON, default=dict, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    knowledge_item = relationship("Knowledge", back_populates="change_logs")


class KnowledgeEmbedding(Base):
    __tablename__ = "knowledge_embeddings"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    embedding_model = Column(String(256), nullable=False, index=True)
    embedding_dimension = Column(Integer, nullable=False)
    content_hash = Column(String(64), nullable=False, index=True)
    embedding = Column(JSON, nullable=False)
    embedding_vector = Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=True)
    title_embedding_vector = Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=True)
    content_embedding_vector = Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    knowledge_item = relationship("Knowledge", back_populates="embeddings")

    __table_args__ = (
        UniqueConstraint("knowledge_id", "embedding_model", name="uq_knowledge_embedding_model"),
    )


class KnowledgeSearchEmbedding(Base):
    """检索专用向量：副标题问法和正文分块与查重向量分开保存。"""

    __tablename__ = "knowledge_search_embeddings"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    embedding_model = Column(String(256), nullable=False, index=True)
    embedding_kind = Column(String(32), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False, default=0)
    content_hash = Column(String(64), nullable=False, index=True)
    source_text = Column(Text, nullable=False)
    embedding_dimension = Column(Integer, nullable=False)
    embedding = Column(JSON, nullable=False)
    embedding_vector = Column(Vector(settings.EMBEDDING_DIMENSIONS), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    knowledge_item = relationship("Knowledge", back_populates="search_embeddings")

    __table_args__ = (
        UniqueConstraint(
            "knowledge_id",
            "embedding_model",
            "embedding_kind",
            "chunk_index",
            name="uq_knowledge_search_embedding",
        ),
    )


class KnowledgeDeduplicationFeedback(Base):
    __tablename__ = "knowledge_deduplication_feedback"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    matched_knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    verdict = Column(String(32), nullable=False, default="different")
    reason = Column(Text, nullable=False)
    submitted_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    knowledge_item = relationship(
        "Knowledge",
        back_populates="deduplication_feedbacks",
        foreign_keys=[knowledge_id],
    )

    __table_args__ = (
        UniqueConstraint(
            "knowledge_id",
            "matched_knowledge_id",
            "submitted_by",
            name="uq_dedup_feedback_submitter",
        ),
    )


class KnowledgeMedia(Base):
    """知识条目关联的媒体文件(图片/视频)"""
    __tablename__ = "knowledge_media"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    media_type = Column(String(16), nullable=False)  # image / video
    filename = Column(String(256), nullable=False)
    original_name = Column(String(256), nullable=False)
    file_path = Column(String(512), nullable=False)
    file_size = Column(Integer, default=0)
    mime_type = Column(String(128), default="image/png")
    alt = Column(String(256), default="")
    caption = Column(Text, default="")  # 媒体描述/说明文字
    duration = Column(String(32), default="")  # 视频时长，如 "03:20"
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    knowledge_item = relationship("Knowledge", back_populates="media")


class Category(Base):
    __tablename__ = "categories"

    id = Column(String(64), primary_key=True)
    name = Column(String(128), nullable=False)
    parent_id = Column(String(64), ForeignKey("categories.id"), nullable=True, index=True)
    level = Column(Integer, default=1)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    children = relationship("Category", backref="parent", remote_side="Category.id")
    knowledge_items = relationship("Knowledge", back_populates="category")

    __table_args__ = (
        UniqueConstraint("name", "parent_id", name="uq_category_name_parent"),
    )


class TagDimension(Base):
    __tablename__ = "tag_dimensions"

    id = Column(String(64), primary_key=True)
    name = Column(String(64), nullable=False, unique=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    values = relationship("TagValue", back_populates="dimension", cascade="all, delete-orphan")


class TagValue(Base):
    __tablename__ = "tag_values"

    id = Column(String(64), primary_key=True)
    dimension_id = Column(String(64), ForeignKey("tag_dimensions.id"), nullable=False, index=True)
    value = Column(String(128), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    dimension = relationship("TagDimension", back_populates="values")

    __table_args__ = (
        UniqueConstraint("dimension_id", "value", name="uq_tag_value_per_dim"),
    )


class KnowledgeTag(Base):
    __tablename__ = "knowledge_tags"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, index=True)
    tag_value_id = Column(String(64), ForeignKey("tag_values.id"), nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    knowledge_item = relationship("Knowledge", back_populates="tags")
    tag_value = relationship("TagValue")

    __table_args__ = (
        UniqueConstraint("knowledge_id", "tag_value_id", name="uq_knowledge_tag"),
    )


class UsageStat(Base):
    __tablename__ = "usage_stats"

    id = Column(String(64), primary_key=True)
    knowledge_id = Column(String(64), ForeignKey("knowledge_items.id"), nullable=False, unique=True)
    recommend_count = Column(Integer, default=0)
    click_count = Column(Integer, default=0)
    feedback_score = Column(Float, default=0.0)
    last_used_at = Column(DateTime, nullable=True)

    knowledge_item = relationship("Knowledge", back_populates="usage_stats")
