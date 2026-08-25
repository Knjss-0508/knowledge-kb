"""make model configuration source record id trace-only

Revision ID: 20260825_01
Revises: 20260820_01
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op


revision = "20260825_01"
down_revision = "20260820_01"
branch_labels = None
depends_on = None


_MODEL_CONFIGURATION_FILTER = (
    "knowledge_origin = 'model_configuration' "
    "AND source_record_id IS NOT NULL"
)


def upgrade() -> None:
    op.drop_index(
        "uq_knowledge_items_model_configuration_source_record_id",
        table_name="knowledge_items",
    )


def downgrade() -> None:
    bind = op.get_bind()
    duplicated_source_id_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM ("
            "SELECT source_record_id FROM knowledge_items "
            "WHERE knowledge_origin = 'model_configuration' "
            "AND source_record_id IS NOT NULL "
            "GROUP BY source_record_id HAVING COUNT(*) > 1"
            ") AS duplicated_source_ids"
        )
    ).scalar_one()
    if duplicated_source_id_count:
        raise RuntimeError(
            "Cannot restore the model configuration source record ID "
            "unique index while duplicated trace IDs exist."
        )
    op.create_index(
        "uq_knowledge_items_model_configuration_source_record_id",
        "knowledge_items",
        ["source_record_id"],
        unique=True,
        postgresql_where=sa.text(_MODEL_CONFIGURATION_FILTER),
    )
