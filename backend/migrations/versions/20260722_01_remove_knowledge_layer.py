"""Remove the standalone L1/L2/L3 knowledge layer.

Revision ID: 20260722_01
Revises: 20260712_05
Create Date: 2026-07-22
"""

from alembic import op


revision = "20260722_01"
down_revision = "20260712_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The baseline migration creates tables from the current ORM metadata, so
    # IF EXISTS keeps both fresh installs and existing deployments upgradeable.
    op.execute(
        """
        UPDATE knowledge_items
        SET deduplication_metadata = jsonb_set(
            deduplication_metadata::jsonb,
            '{matches}',
            COALESCE(
                (
                    SELECT jsonb_agg(match_item - 'layer')
                    FROM jsonb_array_elements(
                        deduplication_metadata::jsonb -> 'matches'
                    ) AS matches(match_item)
                ),
                '[]'::jsonb
            )
        )::json
        WHERE deduplication_metadata IS NOT NULL
          AND jsonb_typeof(deduplication_metadata::jsonb -> 'matches') = 'array'
        """
    )
    op.execute(
        """
        UPDATE knowledge_change_logs
        SET changed_fields = COALESCE(
                (
                    SELECT jsonb_agg(field_name)
                    FROM jsonb_array_elements_text(
                        changed_fields::jsonb
                    ) AS fields(field_name)
                    WHERE field_name <> 'layer'
                ),
                '[]'::jsonb
            )::json,
            before_data = (before_data::jsonb - 'layer')::json,
            after_data = (after_data::jsonb - 'layer')::json
        """
    )
    op.execute("DROP INDEX IF EXISTS ix_knowledge_items_layer")
    op.execute("ALTER TABLE knowledge_items DROP COLUMN IF EXISTS layer")
    op.execute("DROP TYPE IF EXISTS knowledgelayer")


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN "
        "CREATE TYPE knowledgelayer AS ENUM ('L1', 'L2', 'L3'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; "
        "END $$"
    )
    op.execute(
        "ALTER TABLE knowledge_items "
        "ADD COLUMN IF NOT EXISTS layer knowledgelayer"
    )
    op.execute("UPDATE knowledge_items SET layer = 'L2' WHERE layer IS NULL")
    op.execute("ALTER TABLE knowledge_items ALTER COLUMN layer SET NOT NULL")
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_items_layer "
        "ON knowledge_items (layer)"
    )
