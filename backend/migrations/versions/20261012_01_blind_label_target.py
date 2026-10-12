"""固定盲标工单的标注人数口径

Revision ID: 20261012_01_blind_label_target
Revises: 20261010_01_work_order_identity
Create Date: 2026-10-12

已有工单按旧版三人规则固化；迁移后新建工单默认五人。这样人数调整后，
历史已完成或已仲裁的工单不会重新打开。
"""

import sqlalchemy as sa
from alembic import op


revision = "20261012_01_blind_label_target"
down_revision = "20261010_01_work_order_identity"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 先用默认值 3 回填所有历史工单，保留它们创建时的规则口径。
    op.add_column(
        "blind_label_work_orders",
        sa.Column(
            "assignment_limit",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("3"),
        ),
    )
    # 后续原生写入使用新默认值；ORM 物化新工单时也会显式写入 5。
    op.alter_column(
        "blind_label_work_orders",
        "assignment_limit",
        server_default=sa.text("5"),
    )


def downgrade() -> None:
    op.drop_column("blind_label_work_orders", "assignment_limit")
