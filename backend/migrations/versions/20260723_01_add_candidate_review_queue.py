"""add candidate review queue fields

Revision ID: 20260723_01
Revises: 20260722_04
Create Date: 2026-07-23
"""

from alembic import op


revision = "20260723_01"
down_revision = "20260722_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # IF NOT EXISTS keeps the revision safe for databases that received the
    # candidate columns before this migration was introduced.
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS candidate_payload JSON"
    )
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS review_metadata JSON"
    )
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS review_status VARCHAR(32)"
    )
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS reviewed_by VARCHAR(128)"
    )
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS reviewed_at TIMESTAMP WITHOUT TIME ZONE"
    )
    op.execute(
        "ALTER TABLE integration_ingestions "
        "ADD COLUMN IF NOT EXISTS submitted_at TIMESTAMP WITHOUT TIME ZONE"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_integration_ingestions_review_status "
        "ON integration_ingestions (review_status)"
    )


def downgrade() -> None:
    op.drop_index(
        "ix_integration_ingestions_review_status",
        table_name="integration_ingestions",
    )
    op.drop_column("integration_ingestions", "submitted_at")
    op.drop_column("integration_ingestions", "reviewed_at")
    op.drop_column("integration_ingestions", "reviewed_by")
    op.drop_column("integration_ingestions", "review_status")
    op.drop_column("integration_ingestions", "review_metadata")
    op.drop_column("integration_ingestions", "candidate_payload")
