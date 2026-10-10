"""record upstream conversation identity and work-order verification

Revision ID: 20261010_01_conversation_identity
Revises: 20261008_01_question_form_id
Create Date: 2026-10-10
"""

import sqlalchemy as sa
from alembic import op


revision = "20261010_01_conversation_identity"
down_revision = "20261008_01_question_form_id"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 助手（插件 0.5.8）会把「当前页面号码」同时填进 conversationId 与
    # workOrderId，因此落在 question_form_id 上的号码未必是工单号。
    # conversation_id_kind 原样保留助手自报的身份类型（workorder /
    # conversation，取不到为空），work_order_verified 记录我们向上游
    # queryQuestionFormDetail 校验的结论（True 存在 / False 查不到 /
    # NULL 未校验或上游不可用）。历史数据两列都保持可空。
    op.add_column(
        "retrieval_quality_events",
        sa.Column("conversation_id_kind", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("work_order_verified", sa.Boolean(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("retrieval_quality_events", "work_order_verified")
    op.drop_column("retrieval_quality_events", "conversation_id_kind")
