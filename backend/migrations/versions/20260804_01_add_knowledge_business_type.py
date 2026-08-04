"""add business type to knowledge items

Revision ID: 20260804_01
Revises: 20260728_05
Create Date: 2026-08-04
"""

import sqlalchemy as sa
from alembic import op


revision = "20260804_01"
down_revision = "20260728_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column("business_type", sa.String(length=32), nullable=True),
    )
    op.execute(
        "UPDATE knowledge_items "
        "SET business_type = 'self_operated' "
        "WHERE business_type IS NULL"
    )
    op.alter_column(
        "knowledge_items",
        "business_type",
        existing_type=sa.String(length=32),
        nullable=False,
    )
    op.create_check_constraint(
        "ck_knowledge_items_business_type",
        "knowledge_items",
        "business_type IN ('self_operated', 'aggregated')",
    )
    op.create_index(
        "ix_knowledge_items_business_type",
        "knowledge_items",
        ["business_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_items_business_type",
        table_name="knowledge_items",
    )
    op.drop_constraint(
        "ck_knowledge_items_business_type",
        "knowledge_items",
        type_="check",
    )
    op.drop_column("knowledge_items", "business_type")
