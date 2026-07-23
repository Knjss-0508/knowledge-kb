"""add candidate review queue fields

Revision ID: 20260723_01
Revises: 20260722_04
Create Date: 2026-07-23
"""

from alembic import op
import sqlalchemy as sa


revision = "20260723_01"
down_revision = "20260722_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "integration_ingestions",
        sa.Column("candidate_payload", sa.JSON(), nullable=True),
    )
    op.add_column(
        "integration_ingestions",
        sa.Column("review_metadata", sa.JSON(), nullable=True),
    )
    op.add_column(
        "integration_ingestions",
        sa.Column("review_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "integration_ingestions",
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "integration_ingestions",
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "integration_ingestions",
        sa.Column("submitted_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_integration_ingestions_review_status",
        "integration_ingestions",
        ["review_status"],
        unique=False,
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
