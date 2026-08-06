"""add embedding model console, training jobs, and richer retrieval feedback

Revision ID: 20260806_01
Revises: 20260805_02
Create Date: 2026-08-06
"""

import sqlalchemy as sa
from alembic import op


revision = "20260806_01"
down_revision = "20260805_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "retrieval_quality_events",
        sa.Column("schema_version", sa.Integer(), nullable=False, server_default=sa.text("1")),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("request_status", sa.String(length=32), nullable=False, server_default="success"),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column(
            "threshold_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_applicable",
        ),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column(
            "selection_status",
            sa.String(length=32),
            nullable=False,
            server_default="not_evaluated",
        ),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("selected_knowledge_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("selected_candidate_rank", sa.Integer(), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("expected_knowledge_id", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("feedback_type", sa.String(length=32), nullable=False, server_default="none"),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("failure_reason", sa.String(length=64), nullable=False, server_default=""),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column(
            "candidate_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("embedding_model", sa.String(length=256), nullable=False, server_default=""),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("reranker_model", sa.String(length=256), nullable=False, server_default=""),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("prompt_version", sa.String(length=128), nullable=False, server_default=""),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("retrieval_latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("rerank_latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("total_latency_ms", sa.Float(), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("training_eligible", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column(
            "review_status",
            sa.String(length=32),
            nullable=False,
            server_default="unreviewed",
        ),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "retrieval_quality_events",
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
    )

    op.execute(
        """
        UPDATE retrieval_quality_events
        SET
            threshold_status = CASE
                WHEN candidate_count = 0 OR top_rerank_score IS NULL
                    THEN 'not_applicable'
                WHEN top_rerank_score < score_threshold THEN 'below'
                ELSE 'passed'
            END,
            selection_status = CASE
                WHEN candidate_count = 0 THEN 'not_evaluated'
                WHEN selected THEN 'top_selected'
                ELSE 'none_selected'
            END,
            selected_knowledge_id = CASE WHEN selected THEN top_knowledge_id ELSE NULL END
        """
    )

    for column in (
        "request_status",
        "threshold_status",
        "selection_status",
        "selected_knowledge_id",
        "expected_knowledge_id",
        "feedback_type",
        "failure_reason",
        "training_eligible",
        "review_status",
    ):
        op.create_index(
            f"ix_retrieval_quality_events_{column}",
            "retrieval_quality_events",
            [column],
            unique=False,
        )

    op.create_table(
        "embedding_runtime_configs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="draft"),
        sa.Column(
            "config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column(
            "evaluation_metrics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("change_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("activated_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("activated_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ('draft', 'active', 'archived')",
            name="ck_embedding_runtime_config_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("version"),
    )
    op.create_index(
        "ix_embedding_runtime_configs_version",
        "embedding_runtime_configs",
        ["version"],
        unique=True,
    )
    op.create_index(
        "ix_embedding_runtime_configs_status",
        "embedding_runtime_configs",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_embedding_runtime_configs_created_at",
        "embedding_runtime_configs",
        ["created_at"],
        unique=False,
    )

    op.create_table(
        "embedding_training_samples",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("task_type", sa.String(length=32), nullable=False),
        sa.Column("query_text", sa.Text(), nullable=False),
        sa.Column("positive_text", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "negative_texts",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("source_type", sa.String(length=32), nullable=False, server_default="manual"),
        sa.Column("source_id", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="candidate"),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column(
            "sample_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("reviewed_by", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "task_type IN ('retrieval', 'deduplication')",
            name="ck_embedding_training_sample_task_type",
        ),
        sa.CheckConstraint(
            "status IN ('candidate', 'verified', 'excluded')",
            name="ck_embedding_training_sample_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_type",
            "source_id",
            name="uq_embedding_training_sample_source",
        ),
    )
    for column in ("task_type", "source_type", "source_id", "status", "created_at"):
        op.create_index(
            f"ix_embedding_training_samples_{column}",
            "embedding_training_samples",
            [column],
            unique=False,
        )

    op.create_table(
        "embedding_training_jobs",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(length=64), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("base_model", sa.String(length=256), nullable=False),
        sa.Column("candidate_model_name", sa.String(length=256), nullable=False),
        sa.Column("train_type", sa.String(length=32), nullable=False, server_default="lora"),
        sa.Column(
            "training_config",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("dataset_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "dataset_payload",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'::json"),
        ),
        sa.Column("sample_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("train_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("validation_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("test_count", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "metrics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("log_tail", sa.Text(), nullable=False, server_default=""),
        sa.Column("artifact_uri", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("runner_id", sa.String(length=128), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("requested_by", sa.String(length=128), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("completed_at", sa.DateTime(), nullable=True),
        sa.CheckConstraint(
            "status IN ("
            "'queued', 'claimed', 'running', 'evaluating', 'completed', "
            "'failed', 'cancelled'"
            ")",
            name="ck_embedding_training_job_status",
        ),
        sa.CheckConstraint(
            "train_type IN ('lora', 'full')",
            name="ck_embedding_training_job_train_type",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("candidate_model_name"),
    )
    for column in (
        "status",
        "candidate_model_name",
        "dataset_hash",
        "runner_id",
        "lease_expires_at",
        "created_at",
    ):
        op.create_index(
            f"ix_embedding_training_jobs_{column}",
            "embedding_training_jobs",
            [column],
            unique=column == "candidate_model_name",
        )

    op.create_table(
        "embedding_model_versions",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("model_name", sa.String(length=256), nullable=False),
        sa.Column("base_model", sa.String(length=256), nullable=False),
        sa.Column("training_job_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="candidate"),
        sa.Column("dimension", sa.Integer(), nullable=False, server_default=sa.text("1024")),
        sa.Column(
            "metrics",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("artifact_uri", sa.String(length=1024), nullable=False, server_default=""),
        sa.Column("artifact_sha256", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("release_notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("approved_by", sa.String(length=128), nullable=True),
        sa.Column("approved_at", sa.DateTime(), nullable=True),
        sa.Column("deployed_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('candidate', 'approved', 'rejected', 'deployed', 'retired')",
            name="ck_embedding_model_version_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("model_name"),
        sa.UniqueConstraint("training_job_id"),
    )
    for column in ("model_name", "training_job_id", "status", "created_at"):
        op.create_index(
            f"ix_embedding_model_versions_{column}",
            "embedding_model_versions",
            [column],
            unique=column in {"model_name", "training_job_id"},
        )

    op.create_table(
        "embedding_training_runners",
        sa.Column("id", sa.String(length=128), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("hostname", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="online"),
        sa.Column("gpu_name", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("gpu_memory_mb", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("gpu_free_memory_mb", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("cuda_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("runner_version", sa.String(length=64), nullable=False, server_default=""),
        sa.Column("current_job_id", sa.String(length=64), nullable=True),
        sa.Column(
            "runner_metadata",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'::json"),
        ),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "status IN ('online', 'busy', 'offline', 'error')",
            name="ck_embedding_training_runner_status",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in ("status", "current_job_id", "last_seen_at"):
        op.create_index(
            f"ix_embedding_training_runners_{column}",
            "embedding_training_runners",
            [column],
            unique=False,
        )


def downgrade() -> None:
    for table, columns in (
        (
            "embedding_training_runners",
            ("last_seen_at", "current_job_id", "status"),
        ),
        (
            "embedding_model_versions",
            ("created_at", "status", "training_job_id", "model_name"),
        ),
        (
            "embedding_training_jobs",
            (
                "created_at",
                "lease_expires_at",
                "runner_id",
                "dataset_hash",
                "candidate_model_name",
                "status",
            ),
        ),
        (
            "embedding_training_samples",
            ("created_at", "status", "source_id", "source_type", "task_type"),
        ),
        (
            "embedding_runtime_configs",
            ("created_at", "status", "version"),
        ),
    ):
        for column in columns:
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)

    for column in (
        "review_status",
        "training_eligible",
        "failure_reason",
        "feedback_type",
        "expected_knowledge_id",
        "selected_knowledge_id",
        "selection_status",
        "threshold_status",
        "request_status",
    ):
        op.drop_index(
            f"ix_retrieval_quality_events_{column}",
            table_name="retrieval_quality_events",
        )

    for column in (
        "reviewed_at",
        "reviewed_by",
        "review_status",
        "training_eligible",
        "total_latency_ms",
        "rerank_latency_ms",
        "retrieval_latency_ms",
        "prompt_version",
        "reranker_model",
        "embedding_model",
        "candidate_snapshot",
        "failure_reason",
        "feedback_type",
        "expected_knowledge_id",
        "selected_candidate_rank",
        "selected_knowledge_id",
        "selection_status",
        "threshold_status",
        "request_status",
        "schema_version",
    ):
        op.drop_column("retrieval_quality_events", column)
