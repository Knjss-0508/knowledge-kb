"""Remove unused business type and model personalization fields.

Revision ID: 20260722_02
Revises: 20260722_01
Create Date: 2026-07-22
"""

from alembic import op


revision = "20260722_02"
down_revision = "20260722_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE knowledge_change_logs
        SET changed_fields = COALESCE(
                (
                    SELECT jsonb_agg(field_name)
                    FROM jsonb_array_elements_text(
                        changed_fields::jsonb
                    ) AS fields(field_name)
                    WHERE field_name NOT IN (
                        'applicable_business_types',
                        'is_model_personal'
                    )
                ),
                '[]'::jsonb
            )::json,
            before_data = (
                before_data::jsonb
                - 'applicable_business_types'
                - 'is_model_personal'
            )::json,
            after_data = (
                after_data::jsonb
                - 'applicable_business_types'
                - 'is_model_personal'
            )::json
        """
    )
    op.execute(
        "ALTER TABLE knowledge_items "
        "DROP COLUMN IF EXISTS applicable_business_types"
    )
    op.execute(
        "ALTER TABLE knowledge_items "
        "DROP COLUMN IF EXISTS is_model_personal"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_items "
        "ADD COLUMN IF NOT EXISTS applicable_business_types JSON DEFAULT '[]'::json"
    )
    op.execute(
        "ALTER TABLE knowledge_items "
        "ADD COLUMN IF NOT EXISTS is_model_personal VARCHAR(16) DEFAULT 'false'"
    )
