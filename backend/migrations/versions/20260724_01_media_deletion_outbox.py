"""add media deletion outbox and unique media filename

Revision ID: 20260724_01
Revises: 20260723_01
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op


revision = "20260724_01"
down_revision = "20260723_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_knowledge_media_filename",
        "knowledge_media",
        ["filename"],
    )
    op.create_table(
        "media_deletion_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
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
        sa.Column(
            "last_error",
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_media_deletion_tasks_storage_backend",
        "media_deletion_tasks",
        ["storage_backend"],
        unique=False,
    )
    op.create_index(
        "ix_media_deletion_tasks_next_attempt_at",
        "media_deletion_tasks",
        ["next_attempt_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_deletion_tasks_next_attempt_at",
        table_name="media_deletion_tasks",
    )
    op.drop_index(
        "ix_media_deletion_tasks_storage_backend",
        table_name="media_deletion_tasks",
    )
    op.drop_table("media_deletion_tasks")
    op.drop_constraint(
        "uq_knowledge_media_filename",
        "knowledge_media",
        type_="unique",
    )
