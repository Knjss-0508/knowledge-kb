"""Persist candidate model service failure protection."""

from alembic import op
import sqlalchemy as sa


revision = "20261028_01_candidate_pause"
down_revision = "20261029_01_candidate_jsonb"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("candidate_model_automation", sa.Column(
        "consecutive_network_failures", sa.Integer(), nullable=False, server_default="0",
    ))
    op.add_column("candidate_model_automation", sa.Column("paused_at", sa.DateTime(), nullable=True))
    op.add_column("candidate_model_automation", sa.Column(
        "pause_reason", sa.String(512), nullable=False, server_default="",
    ))


def downgrade() -> None:
    op.drop_column("candidate_model_automation", "pause_reason")
    op.drop_column("candidate_model_automation", "paused_at")
    op.drop_column("candidate_model_automation", "consecutive_network_failures")
