"""add related standard items to knowledge

Revision ID: 20260728_01
Revises: 20260724_02
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260728_01"
down_revision = "20260724_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column(
            "related_standard_items",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.alter_column(
        "knowledge_items",
        "related_standard_items",
        server_default=None,
    )


def downgrade() -> None:
    op.drop_column("knowledge_items", "related_standard_items")
