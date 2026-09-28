"""add confidence prompt shadow evaluation

Revision ID: 20260928_04
Revises: 20260928_03
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_04"
down_revision = "20260928_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "confidence_training_jobs",
        sa.Column("shadow_evaluation", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )


def downgrade() -> None:
    op.drop_column("confidence_training_jobs", "shadow_evaluation")
