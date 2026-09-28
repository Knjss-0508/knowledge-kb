"""add regression reviews and immutable prompt versions

Revision ID: 20260928_05
Revises: 20260928_04
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_05"
down_revision = "20260928_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "confidence_training_jobs",
        sa.Column("regression_reviews", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
    )
    op.add_column(
        "confidence_training_jobs",
        sa.Column("prompt_versions", sa.JSON(), nullable=False, server_default=sa.text("'[]'")),
    )


def downgrade() -> None:
    op.drop_column("confidence_training_jobs", "prompt_versions")
    op.drop_column("confidence_training_jobs", "regression_reviews")
