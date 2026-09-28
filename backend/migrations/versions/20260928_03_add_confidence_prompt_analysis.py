"""add confidence prompt analysis fields

Revision ID: 20260928_03
Revises: 20260928_02
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_03"
down_revision = "20260928_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("confidence_training_jobs", sa.Column("analysis_result", sa.JSON(), nullable=False, server_default=sa.text("'{}'")))
    op.add_column("confidence_training_jobs", sa.Column("candidate_prompt", sa.String(length=20000), nullable=False, server_default=""))
    op.add_column("confidence_training_jobs", sa.Column("resolved_model_version", sa.String(length=256), nullable=False, server_default=""))


def downgrade() -> None:
    op.drop_column("confidence_training_jobs", "resolved_model_version")
    op.drop_column("confidence_training_jobs", "candidate_prompt")
    op.drop_column("confidence_training_jobs", "analysis_result")
