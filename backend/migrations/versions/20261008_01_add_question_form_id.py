"""add upstream question form id to retrieval events and blind labeling

Revision ID: 20261008_01_question_form_id
Revises: 20260929_01_blind_reason_codes
Create Date: 2026-10-08
"""

import sqlalchemy as sa
from alembic import op


revision = "20261008_01_question_form_id"
down_revision = "20260929_01_blind_reason_codes"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 上游存在两种号码：会话号（聊天记录可查）与曼哈顿工单号
    # （questionFormId，工单详情可查）。历史数据只有会话号，因此新列
    # 保持可空；是否回填由数据清理脚本按账号类型单独处理。
    op.add_column(
        "retrieval_quality_events",
        sa.Column("question_form_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_retrieval_quality_events_question_form_id",
        "retrieval_quality_events",
        ["question_form_id"],
        unique=False,
    )
    op.add_column(
        "blind_label_work_orders",
        sa.Column("question_form_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_blind_label_work_orders_question_form_id",
        "blind_label_work_orders",
        ["question_form_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_blind_label_work_orders_question_form_id",
        table_name="blind_label_work_orders",
    )
    op.drop_column("blind_label_work_orders", "question_form_id")
    op.drop_index(
        "ix_retrieval_quality_events_question_form_id",
        table_name="retrieval_quality_events",
    )
    op.drop_column("retrieval_quality_events", "question_form_id")
