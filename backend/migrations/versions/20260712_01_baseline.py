"""Create the baseline schema and seed data.

Revision ID: 20260712_01
Revises:
Create Date: 2026-07-12
"""

from alembic import op

from app.core.database import Base
from app.models import integration  # noqa: F401
from app.models import knowledge  # noqa: F401
from app.models import user  # noqa: F401
from app.routes.auth import hash_password


revision = "20260712_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql("CREATE EXTENSION IF NOT EXISTS vector")
    Base.metadata.create_all(bind=bind)

    for sql in (
        "UPDATE knowledge_items SET updated_by = created_by WHERE updated_by IS NULL",
        "ALTER TABLE knowledge_items ALTER COLUMN category_id SET NOT NULL",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-standard', '质检标准', NULL, 1, 10, NOW()) ON CONFLICT (id) DO NOTHING",
        "INSERT INTO categories (id, name, parent_id, level, sort_order, created_at) VALUES ('cat-qc-process', '质检流程', NULL, 1, 20, NOW()) ON CONFLICT (id) DO NOTHING",
    ):
        bind.exec_driver_sql(sql)

    admin = bind.exec_driver_sql(
        "SELECT id FROM users WHERE username = 'Weichizhuo'"
    ).first()
    if not admin:
        bind.exec_driver_sql(
            "INSERT INTO users (id, username, password_hash, role, is_active, created_at, updated_at) VALUES (%s, %s, %s, %s, %s, NOW(), NOW())",
            ("super-admin", "Weichizhuo", hash_password("123456"), "super_admin", True),
        )
    bind.exec_driver_sql("UPDATE users SET role = 'visitor' WHERE role = 'user'")


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
