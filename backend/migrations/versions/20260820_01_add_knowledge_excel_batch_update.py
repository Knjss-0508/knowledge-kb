"""add knowledge Excel batch update task type

Revision ID: 20260820_01
Revises: 20260818_02
Create Date: 2026-08-20
"""

import sqlalchemy as sa
from alembic import op


revision = "20260820_01"
down_revision = "20260818_02"
branch_labels = None
depends_on = None


_IMPORT_TYPE_CONSTRAINT = "ck_knowledge_import_task_import_type"


def upgrade() -> None:
    op.drop_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        type_="check",
    )
    op.create_check_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        "import_type IN "
        "('knowledge', 'knowledge_update', 'model_configuration')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    knowledge_update_count = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM knowledge_import_tasks "
            "WHERE import_type = 'knowledge_update'"
        )
    ).scalar_one()
    if knowledge_update_count:
        raise RuntimeError(
            "Cannot downgrade while knowledge_update import tasks exist."
        )
    op.drop_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        type_="check",
    )
    op.create_check_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        "import_type IN ('knowledge', 'model_configuration')",
    )
