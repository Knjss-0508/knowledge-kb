"""Add pgvector columns, migrate JSON vectors and create ANN indexes.

Revision ID: 20260712_02
Revises: 20260712_01
Create Date: 2026-07-12
"""

from alembic import op

revision = "20260712_02"
down_revision = "20260712_01"
branch_labels = None
depends_on = None

FROZEN_EMBEDDING_DIMENSIONS = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute(
        "ALTER TABLE knowledge_embeddings ADD COLUMN IF NOT EXISTS "
        f"embedding_vector vector({FROZEN_EMBEDDING_DIMENSIONS})"
    )
    op.execute(
        "ALTER TABLE knowledge_search_embeddings ADD COLUMN IF NOT EXISTS "
        f"embedding_vector vector({FROZEN_EMBEDDING_DIMENSIONS})"
    )
    op.execute(
        "UPDATE knowledge_embeddings "
        "SET embedding_vector = embedding::text::vector "
        "WHERE embedding_vector IS NULL AND embedding IS NOT NULL"
    )
    op.execute(
        "UPDATE knowledge_search_embeddings "
        "SET embedding_vector = embedding::text::vector "
        "WHERE embedding_vector IS NULL AND embedding IS NOT NULL"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_embeddings_vector_hnsw "
        "ON knowledge_embeddings USING hnsw (embedding_vector vector_cosine_ops)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_knowledge_search_embeddings_vector_hnsw "
        "ON knowledge_search_embeddings USING hnsw (embedding_vector vector_cosine_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_knowledge_search_embeddings_vector_hnsw")
    op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_vector_hnsw")
    op.execute("ALTER TABLE knowledge_search_embeddings DROP COLUMN IF EXISTS embedding_vector")
    op.execute("ALTER TABLE knowledge_embeddings DROP COLUMN IF EXISTS embedding_vector")
