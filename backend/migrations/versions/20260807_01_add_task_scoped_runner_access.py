"""add task-scoped runner access credentials

Revision ID: 20260807_01
Revises: 20260806_01
Create Date: 2026-08-07
"""

import sqlalchemy as sa
from alembic import op

revision = "20260807_01"
down_revision = "20260806_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "embedding_training_jobs",
        sa.Column("runner_access_token_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "embedding_training_jobs",
        sa.Column("runner_access_issued_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "embedding_training_jobs",
        sa.Column("runner_access_expires_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "embedding_training_jobs",
        sa.Column("runner_access_last_used_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "embedding_training_jobs",
        sa.Column("runner_access_issued_by", sa.String(length=128), nullable=True),
    )
    op.create_index(
        "ix_embedding_training_jobs_runner_access_expires_at",
        "embedding_training_jobs",
        ["runner_access_expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_embedding_training_jobs_runner_access_expires_at",
        table_name="embedding_training_jobs",
    )
    for column in (
        "runner_access_issued_by",
        "runner_access_last_used_at",
        "runner_access_expires_at",
        "runner_access_issued_at",
        "runner_access_token_hash",
    ):
        op.drop_column("embedding_training_jobs", column)
