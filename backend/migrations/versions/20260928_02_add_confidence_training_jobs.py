"""add manual confidence training jobs

Revision ID: 20260928_02
Revises: 20260928_01
Create Date: 2026-09-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_02"
down_revision = "20260928_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "confidence_training_jobs",
        sa.Column("id", sa.String(length=64), primary_key=True),
        sa.Column("status", sa.String(length=48), nullable=False),
        sa.Column("stage", sa.String(length=128), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("evaluation_scope", sa.String(length=32), nullable=False),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column("dataset_payload", sa.JSON(), nullable=False),
        sa.Column("sample_count", sa.Integer(), nullable=False),
        sa.Column("train_count", sa.Integer(), nullable=False),
        sa.Column("validation_count", sa.Integer(), nullable=False),
        sa.Column("test_count", sa.Integer(), nullable=False),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.String(length=2000), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_confidence_training_jobs_status", "confidence_training_jobs", ["status"])
    op.create_index("ix_confidence_training_jobs_dataset_hash", "confidence_training_jobs", ["dataset_hash"])
    op.create_index("ix_confidence_training_jobs_created_at", "confidence_training_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_confidence_training_jobs_created_at", table_name="confidence_training_jobs")
    op.drop_index("ix_confidence_training_jobs_dataset_hash", table_name="confidence_training_jobs")
    op.drop_index("ix_confidence_training_jobs_status", table_name="confidence_training_jobs")
    op.drop_table("confidence_training_jobs")
