"""Create the frozen baseline schema and seed non-sensitive reference data.

Revision ID: 20260712_01
Revises:
Create Date: 2026-07-12
"""

from alembic import op

from migrations.frozen_baseline_20260712 import FrozenBase


revision = "20260712_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    vector_schema = bind.exec_driver_sql(
        """
        SELECT namespace.nspname
        FROM pg_extension AS extension
        JOIN pg_namespace AS namespace
          ON namespace.oid = extension.extnamespace
        WHERE extension.extname = 'vector'
        """
    ).scalar_one()
    if vector_schema != "public":
        raise RuntimeError(
            "The vector extension must be installed in the public schema."
        )
    FrozenBase.metadata.create_all(bind=bind)

    for sql in (
        "UPDATE knowledge_items SET updated_by = created_by WHERE updated_by IS NULL",
        "ALTER TABLE knowledge_items ALTER COLUMN category_id SET NOT NULL",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-standard', '质检标准', NULL, 1, 10, NOW()) ON CONFLICT (id) DO NOTHING",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-process', '质检流程', NULL, 1, 20, NOW()) ON CONFLICT (id) DO NOTHING",
    ):
        bind.exec_driver_sql(sql)

    bind.exec_driver_sql("UPDATE users SET role = 'visitor' WHERE role = 'user'")


def downgrade() -> None:
    FrozenBase.metadata.drop_all(bind=op.get_bind())
