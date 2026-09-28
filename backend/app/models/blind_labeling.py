"""Blind retrieval-labeling persistence models.

The blind-labeling workflow intentionally lives beside (rather than inside)
``RetrievalQualityEvent``.  A retrieval event is an immutable telemetry record
and may still be reviewed by the legacy single-review flow; blind labels are
independent, multi-annotator evidence that must never overwrite that record.
"""

from datetime import datetime

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import relationship

from app.core.database import Base


class BlindLabelBatch(Base):
    """A user's 50-item blind-labeling batch."""

    __tablename__ = "blind_label_batches"

    id = Column(String(64), primary_key=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    target_count = Column(Integer, nullable=False, default=50)
    status = Column(String(16), nullable=False, default="active", index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    completed_at = Column(DateTime, nullable=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    user = relationship("User")
    assignments = relationship(
        "BlindLabelAssignment",
        back_populates="batch",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_blind_label_batch_status",
        ),
        CheckConstraint(
            "target_count > 0 AND target_count <= 500",
            name="ck_blind_label_batch_target_count",
        ),
        Index("ix_blind_label_batches_user_status", "user_id", "status"),
        Index(
            "uq_blind_label_active_batch_per_user",
            "user_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )


class BlindLabelWorkOrder(Base):
    """Immutable conversation-level work item and frozen business TOP-3.

    ``candidate_snapshot`` contains private fields (knowledge IDs and scores)
    needed by administrators for audit.  Route serializers strip those fields
    from ordinary annotator responses.
    """

    __tablename__ = "blind_label_work_orders"

    id = Column(String(64), primary_key=True)
    conversation_id = Column(String(128), nullable=False, unique=True, index=True)
    source_event_id = Column(String(64), ForeignKey("retrieval_quality_events.id"), nullable=True, index=True)
    query_text = Column(Text, nullable=False, default="")
    category_id = Column(String(64), nullable=True, index=True)
    category_name = Column(String(128), nullable=True)
    candidate_snapshot = Column(JSON, nullable=False, default=list)
    source_created_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    source_event = relationship("RetrievalQualityEvent")
    assignments = relationship(
        "BlindLabelAssignment",
        back_populates="work_order",
        cascade="all, delete-orphan",
    )
    arbitrations = relationship(
        "BlindLabelArbitration",
        back_populates="work_order",
        cascade="all, delete-orphan",
    )

    __table_args__ = ()


class BlindLabelAssignment(Base):
    """One user's reservation/submission for a work order.

    A row is retained when released.  The unique work-order/user constraint
    therefore prevents a released item from being assigned back to the same
    user, while ``status='released'`` keeps that slot available to another
    annotator.
    """

    __tablename__ = "blind_label_assignments"

    id = Column(String(64), primary_key=True)
    batch_id = Column(String(64), ForeignKey("blind_label_batches.id"), nullable=False, index=True)
    work_order_id = Column(String(64), ForeignKey("blind_label_work_orders.id"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    status = Column(String(16), nullable=False, default="assigned", index=True)
    assigned_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    started_at = Column(DateTime, nullable=True)
    completed_at = Column(DateTime, nullable=True, index=True)
    released_at = Column(DateTime, nullable=True)
    release_reason = Column(String(512), nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    batch = relationship("BlindLabelBatch", back_populates="assignments")
    work_order = relationship("BlindLabelWorkOrder", back_populates="assignments")
    user = relationship("User")
    annotations = relationship(
        "BlindLabelAnnotation",
        back_populates="assignment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        UniqueConstraint(
            "work_order_id",
            "user_id",
            name="uq_blind_label_assignment_work_order_user",
        ),
        CheckConstraint(
            "status IN ('assigned', 'in_progress', 'completed', 'released')",
            name="ck_blind_label_assignment_status",
        ),
        Index(
            "ix_blind_label_assignments_work_order_status",
            "work_order_id",
            "status",
        ),
        Index(
            "ix_blind_label_assignments_user_status",
            "user_id",
            "status",
        ),
    )


class BlindLabelAnnotation(Base):
    """One annotator's independent verdict for one frozen candidate."""

    __tablename__ = "blind_label_annotations"

    id = Column(String(64), primary_key=True)
    assignment_id = Column(String(64), ForeignKey("blind_label_assignments.id"), nullable=False, index=True)
    work_order_id = Column(String(64), ForeignKey("blind_label_work_orders.id"), nullable=False, index=True)
    user_id = Column(String(64), ForeignKey("users.id"), nullable=False, index=True)
    candidate_ref = Column(String(64), nullable=False)
    verdict = Column(String(24), nullable=False)
    reason = Column(Text, nullable=False, default="")
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)

    assignment = relationship("BlindLabelAssignment", back_populates="annotations")
    work_order = relationship("BlindLabelWorkOrder")
    user = relationship("User")

    __table_args__ = (
        UniqueConstraint(
            "assignment_id",
            "candidate_ref",
            name="uq_blind_label_annotation_assignment_candidate",
        ),
        CheckConstraint(
            "verdict IN ('referable', 'not_referable')",
            name="ck_blind_label_annotation_verdict",
        ),
        Index(
            "ix_blind_label_annotations_work_order_candidate",
            "work_order_id",
            "candidate_ref",
        ),
    )


class BlindLabelArbitration(Base):
    """Administrator's resolution for a disputed candidate."""

    __tablename__ = "blind_label_arbitrations"

    id = Column(String(64), primary_key=True)
    work_order_id = Column(String(64), ForeignKey("blind_label_work_orders.id"), nullable=False, index=True)
    candidate_ref = Column(String(64), nullable=False)
    verdict = Column(String(24), nullable=False)
    reason = Column(Text, nullable=False, default="")
    arbitrated_by = Column(String(128), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow, onupdate=datetime.utcnow)

    work_order = relationship("BlindLabelWorkOrder", back_populates="arbitrations")

    __table_args__ = (
        UniqueConstraint(
            "work_order_id",
            "candidate_ref",
            name="uq_blind_label_arbitration_work_order_candidate",
        ),
        CheckConstraint(
            "verdict IN ('referable', 'not_referable')",
            name="ck_blind_label_arbitration_verdict",
        ),
    )
