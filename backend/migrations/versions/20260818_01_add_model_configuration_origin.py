"""add managed model configuration knowledge origin

Revision ID: 20260818_01
Revises: 20260810_01
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_01"
down_revision = "20260810_01"
branch_labels = None
depends_on = None


_MODEL_CONFIGURATION_FILTER = (
    "knowledge_origin = 'model_configuration' "
    "AND source_record_id IS NOT NULL"
)
_MODEL_CONFIGURATION_KEY_FILTER = (
    "knowledge_origin = 'model_configuration' "
    "AND source_knowledge_key IS NOT NULL"
)


def upgrade() -> None:
    op.drop_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        "knowledge_origin IN "
        "('headquarters_standard', 'business_accumulation', "
        "'model_configuration')",
    )
    op.create_index(
        "uq_knowledge_items_model_configuration_source_record_id",
        "knowledge_items",
        ["source_record_id"],
        unique=True,
        postgresql_where=sa.text(_MODEL_CONFIGURATION_FILTER),
    )
    op.create_index(
        "uq_knowledge_items_model_configuration_source_knowledge_key",
        "knowledge_items",
        ["source_knowledge_key"],
        unique=True,
        postgresql_where=sa.text(_MODEL_CONFIGURATION_KEY_FILTER),
    )


def downgrade() -> None:
    bind = op.get_bind()
    model_configuration_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_items "
            "WHERE knowledge_origin = 'model_configuration'"
        )
    ).scalar_one()
    if model_configuration_count:
        raise RuntimeError(
            "Cannot downgrade while model_configuration knowledge exists."
        )
    op.drop_index(
        "uq_knowledge_items_model_configuration_source_knowledge_key",
        table_name="knowledge_items",
    )
    op.drop_index(
        "uq_knowledge_items_model_configuration_source_record_id",
        table_name="knowledge_items",
    )
    op.drop_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        type_="check",
    )
    op.create_check_constraint(
        "ck_knowledge_items_knowledge_origin",
        "knowledge_items",
        "knowledge_origin IN "
        "('headquarters_standard', 'business_accumulation')",
    )
