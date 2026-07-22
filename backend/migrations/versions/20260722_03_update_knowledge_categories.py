"""Update the default knowledge categories.

Revision ID: 20260722_03
Revises: 20260722_02
Create Date: 2026-07-22
"""

from alembic import op


revision = "20260722_03"
down_revision = "20260722_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        INSERT INTO categories (
            id,
            name,
            parent_id,
            level,
            sort_order,
            created_at
        )
        VALUES
            ('cat-qc-standard', '质检标准', NULL, 1, 10, NOW()),
            ('cat-qc-process', '操作流程', NULL, 1, 20, NOW()),
            ('cat-case-analysis', '案例解析', NULL, 1, 30, NOW()),
            ('cat-extra-knowledge', '课外常识', NULL, 1, 40, NOW())
        ON CONFLICT (id) DO UPDATE
        SET name = EXCLUDED.name,
            parent_id = EXCLUDED.parent_id,
            level = EXCLUDED.level,
            sort_order = EXCLUDED.sort_order
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM categories
        WHERE id IN ('cat-case-analysis', 'cat-extra-knowledge')
          AND NOT EXISTS (
              SELECT 1
              FROM knowledge_items
              WHERE knowledge_items.category_id = categories.id
          )
        """
    )
    op.execute(
        """
        UPDATE categories
        SET name = '质检流程',
            parent_id = NULL,
            level = 1,
            sort_order = 20
        WHERE id = 'cat-qc-process'
        """
    )
