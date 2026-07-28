"""add optional source identifiers to knowledge

Revision ID: 20260728_03
Revises: 20260728_02
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260728_03"
down_revision = "20260728_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("source_topic_key", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("source_record_id", sa.String(length=256), nullable=True),
    )
    op.add_column(
        "knowledge_items",
        sa.Column("source_knowledge_key", sa.String(length=512), nullable=True),
    )
    op.create_index(
        "ix_knowledge_items_source_topic_key",
        "knowledge_items",
        ["source_topic_key"],
    )
    op.create_index(
        "ix_knowledge_items_source_record_id",
        "knowledge_items",
        ["source_record_id"],
    )
    op.create_index(
        "ix_knowledge_items_source_knowledge_key",
        "knowledge_items",
        ["source_knowledge_key"],
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_items_source_knowledge_key", table_name="knowledge_items")
    op.drop_index("ix_knowledge_items_source_record_id", table_name="knowledge_items")
    op.drop_index("ix_knowledge_items_source_topic_key", table_name="knowledge_items")
    op.drop_column("knowledge_items", "source_knowledge_key")
    op.drop_column("knowledge_items", "source_record_id")
    op.drop_column("knowledge_items", "source_topic_key")
