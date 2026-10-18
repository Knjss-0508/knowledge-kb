"""persist incremental high-frequency question processing

Revision ID: 20261013_01_hf_persist
Revises: 20261012_01_blind_label_target
Create Date: 2026-10-13
"""

import sqlalchemy as sa
from alembic import op


revision = "20261013_01_hf_persist"
down_revision = "20261018_01_merge_review_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "high_frequency_source_records",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("record_id", sa.String(length=256), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("analyzed_at", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("analyzed_on", sa.Date(), nullable=True),
        sa.Column("category", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("level1", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("level2", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("chat_content", sa.Text(), nullable=False, server_default=""),
        sa.Column("core_problem", sa.Text(), nullable=False, server_default=""),
        sa.Column("judgment", sa.Text(), nullable=False, server_default=""),
        sa.Column("basis", sa.Text(), nullable=False, server_default=""),
        sa.Column("reference_reply", sa.Text(), nullable=False, server_default=""),
        sa.Column("raw_payload", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("source_possibly_truncated", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("source_request_count", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("processing_status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("model_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("rule_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "processing_status IN ('queued', 'running', 'ready', 'review', 'failed')",
            name="ck_hf_source_record_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("record_id"),
    )
    op.create_index("ix_high_frequency_source_records_record_id", "high_frequency_source_records", ["record_id"])
    op.create_index("ix_high_frequency_source_records_source_hash", "high_frequency_source_records", ["source_hash"])
    op.create_index("ix_high_frequency_source_records_analyzed_on", "high_frequency_source_records", ["analyzed_on"])
    op.create_index("ix_high_frequency_source_records_category", "high_frequency_source_records", ["category"])
    op.create_index("ix_high_frequency_source_records_level1", "high_frequency_source_records", ["level1"])
    op.create_index("ix_high_frequency_source_records_level2", "high_frequency_source_records", ["level2"])
    op.create_index("ix_high_frequency_source_records_processing_status", "high_frequency_source_records", ["processing_status"])
    op.create_index("ix_high_frequency_source_records_created_at", "high_frequency_source_records", ["created_at"])

    op.create_table(
        "high_frequency_standard_questions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("category", sa.String(length=128), nullable=False),
        sa.Column("normalized_question", sa.String(length=512), nullable=False),
        sa.Column("standard_question", sa.String(length=512), nullable=False),
        sa.Column("business_theme", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("rule_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("category", "normalized_question", "prompt_version", name="uq_hf_standard_question_version"),
    )
    op.create_index("ix_high_frequency_standard_questions_category", "high_frequency_standard_questions", ["category"])
    op.create_index("ix_high_frequency_standard_questions_created_at", "high_frequency_standard_questions", ["created_at"])

    op.create_table(
        "high_frequency_atomic_questions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=False),
        sa.Column("atomic_index", sa.Integer(), nullable=False),
        sa.Column("atomic_question", sa.Text(), nullable=False),
        sa.Column("normalized_question", sa.String(length=512), nullable=False),
        sa.Column("split_status", sa.String(length=64), nullable=False, server_default="单问题"),
        sa.Column("processing_status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("standard_question_id", sa.String(length=64), nullable=True),
        sa.Column("mapping_source", sa.String(length=32), nullable=False, server_default=""),
        sa.Column("model_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("rule_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "processing_status IN ('queued', 'ready', 'review', 'failed')",
            name="ck_hf_atomic_question_status",
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["high_frequency_source_records.id"]),
        sa.ForeignKeyConstraint(["standard_question_id"], ["high_frequency_standard_questions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_record_id", "normalized_question", name="uq_hf_atomic_question_source_normalized"),
    )
    op.create_index("ix_high_frequency_atomic_questions_source_record_id", "high_frequency_atomic_questions", ["source_record_id"])
    op.create_index("ix_high_frequency_atomic_questions_normalized_question", "high_frequency_atomic_questions", ["normalized_question"])
    op.create_index("ix_high_frequency_atomic_questions_processing_status", "high_frequency_atomic_questions", ["processing_status"])
    op.create_index("ix_high_frequency_atomic_questions_standard_question_id", "high_frequency_atomic_questions", ["standard_question_id"])

    op.create_table(
        "high_frequency_processing_tasks",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("source_record_id", sa.String(length=64), nullable=False),
        sa.Column("source_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'completed', 'failed', 'superseded')",
            name="ck_hf_processing_task_status",
        ),
        sa.ForeignKeyConstraint(["source_record_id"], ["high_frequency_source_records.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_high_frequency_processing_tasks_source_record_id", "high_frequency_processing_tasks", ["source_record_id"])
    op.create_index("ix_high_frequency_processing_tasks_source_hash", "high_frequency_processing_tasks", ["source_hash"])
    op.create_index("ix_high_frequency_processing_tasks_status", "high_frequency_processing_tasks", ["status"])
    op.create_index("ix_high_frequency_processing_tasks_next_attempt_at", "high_frequency_processing_tasks", ["next_attempt_at"])
    op.create_index("ix_high_frequency_processing_tasks_lease_expires_at", "high_frequency_processing_tasks", ["lease_expires_at"])
    op.create_index("ix_high_frequency_processing_tasks_created_at", "high_frequency_processing_tasks", ["created_at"])
    op.create_index(
        "ix_hf_processing_task_ready",
        "high_frequency_processing_tasks",
        ["status", "next_attempt_at", "lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_table("high_frequency_processing_tasks")
    op.drop_table("high_frequency_atomic_questions")
    op.drop_table("high_frequency_standard_questions")
    op.drop_table("high_frequency_source_records")
