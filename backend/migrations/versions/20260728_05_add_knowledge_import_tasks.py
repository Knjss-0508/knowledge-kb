"""persist Excel knowledge import tasks for background processing

Revision ID: 20260728_05
Revises: 20260728_04
Create Date: 2026-07-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260728_05"
down_revision = "20260728_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "knowledge_import_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("original_filename", sa.String(length=256), nullable=False),
        sa.Column("file_size", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("file_content", sa.LargeBinary(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("total_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("processed_rows", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("imported", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("review_required", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("pending_review", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("deprecated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("results", sa.JSON(), nullable=False, server_default=sa.text("'[]'::json")),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'completed_with_errors', 'failed')",
            name="ck_knowledge_import_task_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_knowledge_import_tasks_created_by",
        "knowledge_import_tasks",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_import_tasks_file_sha256",
        "knowledge_import_tasks",
        ["file_sha256"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_import_tasks_status",
        "knowledge_import_tasks",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_import_tasks_next_attempt_at",
        "knowledge_import_tasks",
        ["next_attempt_at"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_import_tasks_lease_expires_at",
        "knowledge_import_tasks",
        ["lease_expires_at"],
        unique=False,
    )
    op.create_index(
        "ix_knowledge_import_tasks_created_at",
        "knowledge_import_tasks",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_import_tasks_created_at", table_name="knowledge_import_tasks")
    op.drop_index("ix_knowledge_import_tasks_lease_expires_at", table_name="knowledge_import_tasks")
    op.drop_index("ix_knowledge_import_tasks_next_attempt_at", table_name="knowledge_import_tasks")
    op.drop_index("ix_knowledge_import_tasks_status", table_name="knowledge_import_tasks")
    op.drop_index("ix_knowledge_import_tasks_file_sha256", table_name="knowledge_import_tasks")
    op.drop_index("ix_knowledge_import_tasks_created_by", table_name="knowledge_import_tasks")
    op.drop_table("knowledge_import_tasks")
