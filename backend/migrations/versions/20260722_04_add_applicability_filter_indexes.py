"""Add indexes for applicability filters.

Revision ID: 20260722_04
Revises: 20260722_03
Create Date: 2026-07-22
"""

from alembic import op


revision = "20260722_04"
down_revision = "20260722_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_knowledge_items_applicable_categories_gin "
        "ON knowledge_items USING gin ((applicable_categories::jsonb))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_knowledge_items_applicable_brands_gin "
        "ON knowledge_items USING gin ((applicable_brands::jsonb))"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS "
        "ix_knowledge_items_applicable_models_gin "
        "ON knowledge_items USING gin ((applicable_models::jsonb))"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_applicable_models_gin")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_applicable_brands_gin")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_applicable_categories_gin")
