"""add immutable confidence evaluation samples

Revision ID: 20261012_02_evaluation_samples
Revises: 20261012_01_blind_label_target
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op


revision = "20261012_02_evaluation_samples"
down_revision = "20261012_01_blind_label_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "evaluation_samples",
        sa.Column("id", sa.String(length=64), nullable=False),
        sa.Column("snapshot_id", sa.String(length=64), nullable=False),
        sa.Column("candidate_id", sa.String(length=64), nullable=False),
        sa.Column("source_batch", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("topic_group", sa.String(length=512), nullable=False),
        sa.Column("draft_version", sa.String(length=128), nullable=False),
        sa.Column("draft_snapshot", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column(
            "published_knowledge_snapshot",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column(
            "draft_revision_history",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
        sa.Column("model_output", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("model_version", sa.String(length=256), nullable=False, server_default=""),
        sa.Column("prompt_version", sa.String(length=128), nullable=False, server_default=""),
        sa.Column("human_truth", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
        sa.Column("reviewed_at", sa.DateTime(), nullable=True),
        sa.Column("dataset_split", sa.String(length=16), nullable=False),
        sa.Column("sample_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "snapshot_id",
            "candidate_id",
            name="uq_evaluation_samples_snapshot_candidate",
        ),
    )
    op.create_index(
        "ix_evaluation_samples_snapshot_id",
        "evaluation_samples",
        ["snapshot_id"],
    )
    op.create_index(
        "ix_evaluation_samples_candidate_id",
        "evaluation_samples",
        ["candidate_id"],
    )
    op.create_index(
        "ix_evaluation_samples_topic_group",
        "evaluation_samples",
        ["topic_group"],
    )
    op.create_index(
        "ix_evaluation_samples_dataset_split",
        "evaluation_samples",
        ["dataset_split"],
    )
    op.create_index(
        "ix_evaluation_samples_created_at",
        "evaluation_samples",
        ["created_at"],
    )

    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            CREATE FUNCTION reject_evaluation_sample_mutation()
            RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'evaluation_sample records are immutable';
            END;
            $$ LANGUAGE plpgsql
            """
        )
        op.execute(
            """
            CREATE TRIGGER evaluation_samples_immutable
            BEFORE UPDATE OR DELETE ON evaluation_samples
            FOR EACH ROW EXECUTE FUNCTION reject_evaluation_sample_mutation()
            """
        )
    elif dialect == "sqlite":
        op.execute(
            """
            CREATE TRIGGER evaluation_samples_immutable_update
            BEFORE UPDATE ON evaluation_samples
            BEGIN
                SELECT RAISE(ABORT, 'evaluation_sample records are immutable');
            END
            """
        )
        op.execute(
            """
            CREATE TRIGGER evaluation_samples_immutable_delete
            BEFORE DELETE ON evaluation_samples
            BEGIN
                SELECT RAISE(ABORT, 'evaluation_sample records are immutable');
            END
            """
        )


def downgrade() -> None:
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS evaluation_samples_immutable ON evaluation_samples")
        op.execute("DROP FUNCTION IF EXISTS reject_evaluation_sample_mutation()")
    elif dialect == "sqlite":
        op.execute("DROP TRIGGER IF EXISTS evaluation_samples_immutable_update")
        op.execute("DROP TRIGGER IF EXISTS evaluation_samples_immutable_delete")
    op.drop_index("ix_evaluation_samples_created_at", table_name="evaluation_samples")
    op.drop_index("ix_evaluation_samples_dataset_split", table_name="evaluation_samples")
    op.drop_index("ix_evaluation_samples_topic_group", table_name="evaluation_samples")
    op.drop_index("ix_evaluation_samples_candidate_id", table_name="evaluation_samples")
    op.drop_index("ix_evaluation_samples_snapshot_id", table_name="evaluation_samples")
    op.drop_table("evaluation_samples")
