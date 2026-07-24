"""add persistent media upload staging

Revision ID: 20260724_02
Revises: 20260724_01
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from alembic import op


revision = "20260724_02"
down_revision = "20260724_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "media_upload_staging",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("username", sa.String(length=128), nullable=False),
        sa.Column("storage_backend", sa.String(length=16), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
        sa.Column("filename", sa.String(length=256), nullable=False),
        sa.Column(
            "status",
            sa.String(length=16),
            nullable=False,
            server_default="uploading",
        ),
        sa.Column("media_type", sa.String(length=16), nullable=False),
        sa.Column("original_name", sa.String(length=256), nullable=False),
        sa.Column(
            "file_size",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "mime_type",
            sa.String(length=128),
            nullable=False,
            server_default="image/png",
        ),
        sa.Column(
            "alt",
            sa.String(length=256),
            nullable=False,
            server_default="",
        ),
        sa.Column(
            "caption",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint(
            "status IN ('uploading', 'ready')",
            name="ck_media_upload_staging_status",
        ),
        sa.UniqueConstraint(
            "filename",
            name="uq_media_upload_staging_filename",
        ),
    )
    op.create_index(
        "ix_media_upload_staging_username",
        "media_upload_staging",
        ["username"],
        unique=False,
    )
    op.create_index(
        "ix_media_upload_staging_expires_at",
        "media_upload_staging",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_media_upload_staging_expires_at",
        table_name="media_upload_staging",
    )
    op.drop_index(
        "ix_media_upload_staging_username",
        table_name="media_upload_staging",
    )
    op.drop_table("media_upload_staging")
