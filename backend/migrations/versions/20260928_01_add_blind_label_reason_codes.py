"""Add structured diagnostic reasons to blind labeling.

Revision ID: 20260928_01
Revises: 20260924_01
"""

import sqlalchemy as sa
from alembic import op


revision = "20260928_01"
down_revision = "20260924_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "blind_label_assignments",
        sa.Column("note", sa.Text(), nullable=False, server_default=sa.text("''")),
    )
    op.add_column(
        "blind_label_assignments",
        sa.Column(
            "task_reason_code",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.add_column(
        "blind_label_annotations",
        sa.Column(
            "reason_code",
            sa.String(length=64),
            nullable=False,
            server_default=sa.text("''"),
        ),
    )
    op.alter_column("blind_label_assignments", "note", server_default=None)
    op.alter_column("blind_label_assignments", "task_reason_code", server_default=None)
    op.alter_column("blind_label_annotations", "reason_code", server_default=None)


def downgrade() -> None:
    op.drop_column("blind_label_annotations", "reason_code")
    op.drop_column("blind_label_assignments", "task_reason_code")
    op.drop_column("blind_label_assignments", "note")
