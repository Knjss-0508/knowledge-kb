"""add durable confidence training settings

Revision ID: 20260928_01
Revises: 20260825_01
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_01"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "confidence_training_settings",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )


def downgrade() -> None:
    op.drop_table("confidence_training_settings")
