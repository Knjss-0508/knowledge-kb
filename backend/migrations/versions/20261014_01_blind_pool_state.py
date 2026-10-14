"""盲标工单池状态：退出派发而不删除任何标注记录

Revision ID: 20261014_01_blind_pool_state
Revises: 20261012_01_blind_label_target
Create Date: 2026-10-14

背景（2026-10-14 线上事故 2110212947606246403）：助手会把「当前页面号码」
同时填进 ``conversationId`` 与 ``workOrderId``，会话号因此进入盲标池（当天池内
3535 条里 285 条经访问日志判定为会话号）。建单门禁只能拦住「以后新建」，已经在
池里的会话号工单仍会被派发和标注；而 ``BlindLabelWorkOrder.assignments`` 是
``cascade="all, delete-orphan"``，物理删除会连带删掉已完成的标注（当时 100 条
工单、782 条标注），属于不可接受的数据损失。因此改为持久化的「退池」：

* ``pool_state``：``active``（默认）/ ``retired``，退池后不再被派发、不再计入池内统计；
* ``retired_at`` / ``retire_reason``：退池时间与原因（当前取值 ``session_number``）。

历史行由 ``server_default='active'`` 回填，所以对已有数据加非空列是安全的；
标注与派发记录（含 released 的行）全部保留，管理员总览仍可查。

注意：``alembic_version.version_num`` 是 ``varchar(32)``，revision id 必须不超过
32 个字符（本 id 为 28 字符），该约束由 ``backend/tests/test_migration_revisions.py``
兜住。
"""

import sqlalchemy as sa
from alembic import op


revision = "20261014_01_blind_pool_state"
down_revision = "20261012_01_blind_label_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blind_label_work_orders",
        sa.Column(
            "pool_state",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
    )
    op.add_column(
        "blind_label_work_orders",
        sa.Column("retired_at", sa.DateTime(), nullable=True),
    )
    op.add_column(
        "blind_label_work_orders",
        sa.Column("retire_reason", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_blind_label_work_orders_pool_state",
        "blind_label_work_orders",
        ["pool_state"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_blind_label_work_orders_pool_state",
        table_name="blind_label_work_orders",
    )
    op.drop_column("blind_label_work_orders", "retire_reason")
    op.drop_column("blind_label_work_orders", "retired_at")
    op.drop_column("blind_label_work_orders", "pool_state")
