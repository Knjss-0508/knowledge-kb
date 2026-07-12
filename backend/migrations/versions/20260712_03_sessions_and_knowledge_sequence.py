"""Persist user sessions and allocate knowledge display IDs atomically.

Revision ID: 20260712_03
Revises: 20260712_02
Create Date: 2026-07-12
"""

from alembic import op


revision = "20260712_03"
down_revision = "20260712_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS user_sessions (
            token_hash VARCHAR(64) PRIMARY KEY,
            user_id VARCHAR(64) NOT NULL REFERENCES users(id),
            expires_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP NOT NULL
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_sessions_user_id ON user_sessions (user_id)"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_user_sessions_expires_at ON user_sessions (expires_at)"
    )

    op.execute("CREATE SEQUENCE IF NOT EXISTS knowledge_item_number_seq START WITH 1")
    op.execute(
        """
        SELECT setval(
            'knowledge_item_number_seq',
            COALESCE(
                (
                    SELECT MAX(
                        (ASCII(SUBSTRING(id FROM 1 FOR 1)) - ASCII('A')) * 99999
                        + CAST(SUBSTRING(id FROM 3) AS INTEGER)
                    )
                    FROM knowledge_items
                    WHERE id ~ '^[A-Z]-[0-9]{5}$'
                ),
                0
            ) + 1,
            false
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP SEQUENCE IF EXISTS knowledge_item_number_seq")
    op.drop_index("ix_user_sessions_expires_at", table_name="user_sessions")
    op.drop_index("ix_user_sessions_user_id", table_name="user_sessions")
    op.drop_table("user_sessions")
