"""Store title and content vectors separately for duplicate detection.

Revision ID: 20260712_05
Revises: 20260712_04
Create Date: 2026-07-12
"""

from alembic import op


revision = "20260712_05"
down_revision = "20260712_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_embeddings "
        "ADD COLUMN IF NOT EXISTS title_embedding_vector vector(1024)"
    )
    op.execute(
        "ALTER TABLE knowledge_embeddings "
        "ADD COLUMN IF NOT EXISTS content_embedding_vector vector(1024)"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE knowledge_embeddings "
        "DROP COLUMN IF EXISTS content_embedding_vector"
    )
    op.execute(
        "ALTER TABLE knowledge_embeddings "
        "DROP COLUMN IF EXISTS title_embedding_vector"
    )
