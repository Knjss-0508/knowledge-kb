"""persist manual knowledge vectorization tasks for background processing

Revision ID: 20260828_01
Revises: 20260825_01
Create Date: 2026-08-28
"""

import sqlalchemy as sa
from alembic import op


revision = "20260828_01"
down_revision = "20260825_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create the durable queue used by manual knowledge writes.

    The API commits the knowledge row and this queue row first.  A background
    worker then owns embedding/deduplication work, so the queue must survive
    request disconnects and backend restarts.
    """

    op.create_table(
        "knowledge_vector_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column(
            "knowledge_id",
            sa.String(length=64),
            sa.ForeignKey("knowledge_items.id"),
            nullable=False,
        ),
        sa.Column(
            "task_type",
            sa.String(length=32),
            nullable=False,
            server_default="manual_vectorization",
        ),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "status",
            sa.String(length=32),
            nullable=False,
            server_default="queued",
        ),
        sa.Column(
            "attempt_count",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "next_attempt_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column(
            "error_message",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "task_type IN ('manual_vectorization')",
            name="ck_knowledge_vector_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'superseded')",
            name="ck_knowledge_vector_task_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    for index_name, columns in (
        (
            "ix_knowledge_vector_tasks_knowledge_id",
            ["knowledge_id"],
        ),
        (
            "ix_knowledge_vector_tasks_content_hash",
            ["content_hash"],
        ),
        (
            "ix_knowledge_vector_tasks_status",
            ["status"],
        ),
        (
            "ix_knowledge_vector_tasks_next_attempt_at",
            ["next_attempt_at"],
        ),
        (
            "ix_knowledge_vector_tasks_lease_expires_at",
            ["lease_expires_at"],
        ),
        (
            "ix_knowledge_vector_tasks_created_at",
            ["created_at"],
        ),
    ):
        op.create_index(index_name, "knowledge_vector_tasks", columns)


def downgrade() -> None:
    for index_name in (
        "ix_knowledge_vector_tasks_created_at",
        "ix_knowledge_vector_tasks_lease_expires_at",
        "ix_knowledge_vector_tasks_next_attempt_at",
        "ix_knowledge_vector_tasks_status",
        "ix_knowledge_vector_tasks_content_hash",
        "ix_knowledge_vector_tasks_knowledge_id",
    ):
        op.drop_index(index_name, table_name="knowledge_vector_tasks")
    op.drop_table("knowledge_vector_tasks")
