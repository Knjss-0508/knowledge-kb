"""Add conversation-level blind retrieval labeling workflow.

Revision ID: 20260924_01
Revises: 20260923_01
"""

import sqlalchemy as sa
from alembic import op


revision = "20260924_01"
down_revision = "20260923_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "blind_label_batches",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="blind_label_batches_pkey"),
        sa.CheckConstraint(
            "status IN ('active', 'completed')",
            name="ck_blind_label_batch_status",
        ),
        sa.CheckConstraint(
            "target_count > 0 AND target_count <= 500",
            name="ck_blind_label_batch_target_count",
        ),
    )
    op.create_index("ix_blind_label_batches_user_id", "blind_label_batches", ["user_id"])
    op.create_index("ix_blind_label_batches_status", "blind_label_batches", ["status"])
    op.create_index("ix_blind_label_batches_created_at", "blind_label_batches", ["created_at"])
    op.create_index(
        "ix_blind_label_batches_user_status",
        "blind_label_batches",
        ["user_id", "status"],
    )
    op.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS uq_blind_label_active_batch_per_user "
        "ON blind_label_batches (user_id) WHERE status = 'active'"
    )

    op.create_table(
        "blind_label_work_orders",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("conversation_id", sa.String(length=128), nullable=False),
        sa.Column("source_event_id", sa.String(length=64), nullable=True),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("category_id", sa.String(length=64), nullable=True),
        sa.Column("category_name", sa.String(length=128), nullable=True),
        sa.Column("candidate_snapshot", sa.JSON(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["source_event_id"], ["retrieval_quality_events.id"]),
        sa.PrimaryKeyConstraint("id", name="blind_label_work_orders_pkey"),
        sa.UniqueConstraint("conversation_id", name="uq_blind_label_work_order_conversation"),
    )
    op.create_index("ix_blind_label_work_orders_conversation_id", "blind_label_work_orders", ["conversation_id"])
    op.create_index("ix_blind_label_work_orders_source_event_id", "blind_label_work_orders", ["source_event_id"])
    op.create_index("ix_blind_label_work_orders_category_id", "blind_label_work_orders", ["category_id"])
    op.create_index("ix_blind_label_work_orders_source_created_at", "blind_label_work_orders", ["source_created_at"])
    op.create_index("ix_blind_label_work_orders_created_at", "blind_label_work_orders", ["created_at"])

    op.create_table(
        "blind_label_assignments",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("batch_id", sa.String(length=64), nullable=False),
        sa.Column("work_order_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("assigned_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("released_at", sa.DateTime(), nullable=True),
        sa.Column("release_reason", sa.String(length=512), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["blind_label_batches.id"]),
        sa.ForeignKeyConstraint(["work_order_id"], ["blind_label_work_orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="blind_label_assignments_pkey"),
        sa.UniqueConstraint(
            "work_order_id",
            "user_id",
            name="uq_blind_label_assignment_work_order_user",
        ),
        sa.CheckConstraint(
            "status IN ('assigned', 'in_progress', 'completed', 'released')",
            name="ck_blind_label_assignment_status",
        ),
    )
    op.create_index("ix_blind_label_assignments_batch_id", "blind_label_assignments", ["batch_id"])
    op.create_index("ix_blind_label_assignments_work_order_id", "blind_label_assignments", ["work_order_id"])
    op.create_index("ix_blind_label_assignments_user_id", "blind_label_assignments", ["user_id"])
    op.create_index("ix_blind_label_assignments_status", "blind_label_assignments", ["status"])
    op.create_index("ix_blind_label_assignments_assigned_at", "blind_label_assignments", ["assigned_at"])
    op.create_index("ix_blind_label_assignments_completed_at", "blind_label_assignments", ["completed_at"])
    op.create_index(
        "ix_blind_label_assignments_work_order_status",
        "blind_label_assignments",
        ["work_order_id", "status"],
    )
    op.create_index(
        "ix_blind_label_assignments_user_status",
        "blind_label_assignments",
        ["user_id", "status"],
    )

    op.create_table(
        "blind_label_annotations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("assignment_id", sa.String(length=64), nullable=False),
        sa.Column("work_order_id", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_ref", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["blind_label_assignments.id"]),
        sa.ForeignKeyConstraint(["work_order_id"], ["blind_label_work_orders.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id", name="blind_label_annotations_pkey"),
        sa.UniqueConstraint(
            "assignment_id",
            "candidate_ref",
            name="uq_blind_label_annotation_assignment_candidate",
        ),
        sa.CheckConstraint(
            "verdict IN ('referable', 'not_referable')",
            name="ck_blind_label_annotation_verdict",
        ),
    )
    op.create_index("ix_blind_label_annotations_assignment_id", "blind_label_annotations", ["assignment_id"])
    op.create_index("ix_blind_label_annotations_work_order_id", "blind_label_annotations", ["work_order_id"])
    op.create_index("ix_blind_label_annotations_user_id", "blind_label_annotations", ["user_id"])
    op.create_index("ix_blind_label_annotations_created_at", "blind_label_annotations", ["created_at"])
    op.create_index(
        "ix_blind_label_annotations_work_order_candidate",
        "blind_label_annotations",
        ["work_order_id", "candidate_ref"],
    )

    op.create_table(
        "blind_label_arbitrations",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("work_order_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_ref", sa.String(length=64), nullable=False),
        sa.Column("verdict", sa.String(length=24), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("arbitrated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["work_order_id"], ["blind_label_work_orders.id"]),
        sa.PrimaryKeyConstraint("id", name="blind_label_arbitrations_pkey"),
        sa.UniqueConstraint(
            "work_order_id",
            "candidate_ref",
            name="uq_blind_label_arbitration_work_order_candidate",
        ),
        sa.CheckConstraint(
            "verdict IN ('referable', 'not_referable')",
            name="ck_blind_label_arbitration_verdict",
        ),
    )
    op.create_index("ix_blind_label_arbitrations_work_order_id", "blind_label_arbitrations", ["work_order_id"])


def downgrade() -> None:
    op.drop_index("ix_blind_label_arbitrations_work_order_id", table_name="blind_label_arbitrations")
    op.drop_table("blind_label_arbitrations")
    op.drop_index("ix_blind_label_annotations_work_order_candidate", table_name="blind_label_annotations")
    op.drop_index("ix_blind_label_annotations_created_at", table_name="blind_label_annotations")
    op.drop_index("ix_blind_label_annotations_user_id", table_name="blind_label_annotations")
    op.drop_index("ix_blind_label_annotations_work_order_id", table_name="blind_label_annotations")
    op.drop_index("ix_blind_label_annotations_assignment_id", table_name="blind_label_annotations")
    op.drop_table("blind_label_annotations")
    op.drop_index("ix_blind_label_assignments_user_status", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_work_order_status", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_completed_at", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_assigned_at", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_status", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_user_id", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_work_order_id", table_name="blind_label_assignments")
    op.drop_index("ix_blind_label_assignments_batch_id", table_name="blind_label_assignments")
    op.drop_table("blind_label_assignments")
    op.drop_index("ix_blind_label_work_orders_created_at", table_name="blind_label_work_orders")
    op.drop_index("ix_blind_label_work_orders_source_created_at", table_name="blind_label_work_orders")
    op.drop_index("ix_blind_label_work_orders_category_id", table_name="blind_label_work_orders")
    op.drop_index("ix_blind_label_work_orders_source_event_id", table_name="blind_label_work_orders")
    op.drop_index("ix_blind_label_work_orders_conversation_id", table_name="blind_label_work_orders")
    op.drop_table("blind_label_work_orders")
    op.execute("DROP INDEX IF EXISTS uq_blind_label_active_batch_per_user")
    op.drop_index("ix_blind_label_batches_user_status", table_name="blind_label_batches")
    op.drop_index("ix_blind_label_batches_created_at", table_name="blind_label_batches")
    op.drop_index("ix_blind_label_batches_status", table_name="blind_label_batches")
    op.drop_index("ix_blind_label_batches_user_id", table_name="blind_label_batches")
    op.drop_table("blind_label_batches")
