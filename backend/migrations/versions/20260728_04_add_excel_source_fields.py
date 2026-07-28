"""preserve original excel source fields for export

Revision ID: 20260728_04
Revises: 20260728_03
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260728_04"
down_revision = "20260728_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "knowledge_items",
        sa.Column(
            "source_fields",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
    )
    op.alter_column("knowledge_items", "source_fields", server_default=None)


def downgrade() -> None:
    op.drop_column("knowledge_items", "source_fields")
