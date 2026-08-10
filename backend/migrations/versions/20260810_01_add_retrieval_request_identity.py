"""add retrieval request identity and source pool

Revision ID: 20260810_01
Revises: 20260807_01
Create Date: 2026-08-10
"""

import sqlalchemy as sa
from alembic import op


revision = "20260810_01"
down_revision = "20260807_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 历史反馈可能没有请求身份，因此数据库列保持可空；新 API 契约会
    # 强制所有新写入同时携带合法的 conversation_id 和 request_id。
    # 历史事件统一标记为 combined，新插件事件按 reply/standard 分池。
    op.add_column(
        "retrieval_quality_events",
        sa.Column("request_id", sa.String(length=80), nullable=True),
    )
    op.create_index(
        "ix_retrieval_quality_events_request_id",
        "retrieval_quality_events",
        ["request_id"],
        unique=False,
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column(
            "source_kind",
            sa.String(length=16),
            nullable=False,
            server_default="combined",
        ),
    )
    op.create_index(
        "ix_retrieval_quality_events_source_kind",
        "retrieval_quality_events",
        ["source_kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_retrieval_quality_events_source_kind",
        table_name="retrieval_quality_events",
    )
    op.drop_column("retrieval_quality_events", "source_kind")
    op.drop_index(
        "ix_retrieval_quality_events_request_id",
        table_name="retrieval_quality_events",
    )
    op.drop_column("retrieval_quality_events", "request_id")
