"""Allow the full pending-model-annotation ingestion status."""

import sqlalchemy as sa
from alembic import op


revision = "20261016_01_candidate_status"
down_revision = "20261012_02_evaluation_samples"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "integration_ingestions", "status",
        existing_type=sa.String(length=32), type_=sa.String(length=64),
        existing_nullable=False,
    )


def downgrade() -> None:
    op.alter_column(
        "integration_ingestions", "status",
        existing_type=sa.String(length=64), type_=sa.String(length=32),
        existing_nullable=False,
    )
