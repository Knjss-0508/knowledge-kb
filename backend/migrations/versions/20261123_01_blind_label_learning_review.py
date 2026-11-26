"""Persist human-reviewed blind-label replay samples."""

import sqlalchemy as sa
from alembic import op


revision = "20261123_01_blind_review"
down_revision = "20261103_01_drop_idx"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blind_label_learning_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("work_order_id", sa.String(64), sa.ForeignKey("blind_label_work_orders.id"), nullable=False),
        sa.Column("evidence_version", sa.String(64), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("issue_type", sa.String(24), nullable=False),
        sa.Column("knowledge_id", sa.String(64), nullable=True),
        sa.Column("knowledge_version", sa.String(64), nullable=True),
        sa.Column("expected_answer", sa.Text(), nullable=False),
        sa.Column("reviewer_id", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "ix_blind_label_learning_reviews_order_rev",
        "blind_label_learning_reviews",
        ["work_order_id", "id"],
    )


def downgrade() -> None:
    op.drop_index("ix_blind_label_learning_reviews_order_rev", table_name="blind_label_learning_reviews")
    op.drop_table("blind_label_learning_reviews")
