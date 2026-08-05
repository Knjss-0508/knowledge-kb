"""add import cancellation and failed-row retry state

Revision ID: 20260805_02
Revises: 20260805_01
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op


revision = "20260805_02"
down_revision = "20260805_01"
branch_labels = None
depends_on = None


_STATUS_CONSTRAINT = "ck_knowledge_import_task_status"
_STATUS_VALUES = (
    "'queued', 'running', 'completed', "
    "'completed_with_errors', 'failed', 'cancelled'"
)
_LEGACY_STATUS_VALUES = (
    "'queued', 'running', 'completed', "
    "'completed_with_errors', 'failed'"
)


def upgrade() -> None:
    op.add_column(
        "knowledge_import_tasks",
        sa.Column(
            "retry_rows",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.drop_constraint(
        _STATUS_CONSTRAINT,
        "knowledge_import_tasks",
        type_="check",
    )
    op.create_check_constraint(
        _STATUS_CONSTRAINT,
        "knowledge_import_tasks",
        f"status IN ({_STATUS_VALUES})",
    )


def downgrade() -> None:
    op.execute(
        """
        UPDATE knowledge_import_tasks
        SET status = 'failed',
            error_message = CASE
                WHEN error_message = ''
                    THEN '任务在数据库回滚前已被取消。'
                ELSE error_message
            END
        WHERE status = 'cancelled'
        """
    )
    op.drop_constraint(
        _STATUS_CONSTRAINT,
        "knowledge_import_tasks",
        type_="check",
    )
    op.create_check_constraint(
        _STATUS_CONSTRAINT,
        "knowledge_import_tasks",
        f"status IN ({_LEGACY_STATUS_VALUES})",
    )
    op.drop_column("knowledge_import_tasks", "retry_rows")
