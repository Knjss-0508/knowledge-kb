"""add model configuration Excel import task metadata

Revision ID: 20260818_02
Revises: 20260818_01
Create Date: 2026-08-18
"""

import sqlalchemy as sa
from alembic import op


revision = "20260818_02"
down_revision = "20260818_01"
branch_labels = None
depends_on = None


_IMPORT_TYPE_CONSTRAINT = "ck_knowledge_import_task_import_type"


def upgrade() -> None:
    op.add_column(
        "knowledge_import_tasks",
        sa.Column(
            "import_type",
            sa.String(length=32),
            nullable=False,
            server_default="knowledge",
        ),
    )
    for column_name in ("created", "updated", "unchanged"):
        op.add_column(
            "knowledge_import_tasks",
            sa.Column(
                column_name,
                sa.Integer(),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )
    op.create_check_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        "import_type IN ('knowledge', 'model_configuration')",
    )
    op.create_index(
        "ix_knowledge_import_tasks_import_type",
        "knowledge_import_tasks",
        ["import_type"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_knowledge_import_tasks_import_type",
        table_name="knowledge_import_tasks",
    )
    op.drop_constraint(
        _IMPORT_TYPE_CONSTRAINT,
        "knowledge_import_tasks",
        type_="check",
    )
    for column_name in ("unchanged", "updated", "created", "import_type"):
        op.drop_column("knowledge_import_tasks", column_name)
