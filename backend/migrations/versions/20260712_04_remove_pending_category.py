"""Remove the unused pending-classification category.

Revision ID: 20260712_04
Revises: 20260712_03
Create Date: 2026-07-12
"""

from alembic import op


revision = "20260712_04"
down_revision = "20260712_03"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "DELETE FROM categories "
        "WHERE id = 'cat-pending-classification' "
        "AND NOT EXISTS ("
        "SELECT 1 FROM knowledge_items "
        "WHERE knowledge_items.category_id = categories.id"
        ")"
    )


def downgrade() -> None:
    op.execute(
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) "
        "VALUES ('cat-pending-classification', '待整理', NULL, 1, 0, NOW()) "
        "ON CONFLICT (id) DO NOTHING"
    )
