"""Persist candidate model tasks and the automatic processing switch."""

from alembic import op
import sqlalchemy as sa


revision = "20261027_01_candidate_tasks"
down_revision = "20261013_01_hf_persist"
branch_labels = None
depends_on = None


def upgrade() -> None:
    automation = op.create_table(
        "candidate_model_automation",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("requested_by", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
    )
    op.bulk_insert(automation, [{"id": "default", "enabled": False}])
    op.create_table(
        "candidate_model_tasks",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("source", sa.String(16), nullable=False),
        sa.Column("requested_by", sa.String(64), nullable=False),
        sa.Column("ingestion_ids", sa.JSON(), nullable=False),
        sa.Column("results", sa.JSON(), nullable=False),
        sa.Column("lease_token", sa.String(64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("current_ingestion_id", sa.String(64), nullable=True),
        sa.Column("error_message", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_candidate_model_tasks_status", "candidate_model_tasks", ["status"])
    op.create_index("ix_candidate_model_tasks_created_at", "candidate_model_tasks", ["created_at"])


def downgrade() -> None:
    op.drop_table("candidate_model_tasks")
    op.drop_table("candidate_model_automation")
