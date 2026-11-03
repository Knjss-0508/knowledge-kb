"""Drop retrieval event indexes that no query plan ever used.

背景（2026-11-03）：``retrieval_quality_events`` 有 20 个索引，占 723 MB。
在 130 万行的真实数据上核对 ``pg_stat_user_indexes.idx_scan``（数据库自创建
以来未重置统计），其中 15 个索引 **scan 次数为 0**，且都属于低基数列——
PostgreSQL 在这些列上直接顺序扫描更便宜，索引只会拖慢写入、占磁盘。

删除后该表从 2466 MB 降到 2111 MB（表本身不会缩小，省下的是索引空间）。

保留的 5 个：
    idempotency_key  唯一约束，写入去重要用
    created_at       分析页时间窗聚合（Index Only Scan）
    question_form_id 按工单号反查
    review_status    复核队列
    (pkey)           主键
"""

from alembic import op


revision = "20261103_01_drop_idx"
down_revision = "20261028_01_candidate_pause"
branch_labels = None
depends_on = None


# 顺序按线上实际存在、且 scan=0 的索引。
_UNUSED_INDEXES = (
    "ix_retrieval_quality_events_threshold_status",
    "ix_retrieval_quality_events_conversation_id",
    "ix_retrieval_quality_events_source_system",
    "ix_retrieval_quality_events_top_knowledge_id",
    "ix_retrieval_quality_events_expected_knowledge_id",
    "ix_retrieval_quality_events_failure_reason",
    "ix_retrieval_quality_events_feedback_type",
    "ix_retrieval_quality_events_training_eligible",
    "ix_retrieval_quality_events_outcome",
    "ix_retrieval_quality_events_query_text",
    "ix_retrieval_quality_events_request_id",
    "ix_retrieval_quality_events_request_status",
    "ix_retrieval_quality_events_selected_knowledge_id",
    "ix_retrieval_quality_events_selection_status",
    "ix_retrieval_quality_events_source_kind",
)


def upgrade() -> None:
    for name in _UNUSED_INDEXES:
        # 线上已手工删除，这里用 IF EXISTS 保持幂等。
        op.execute(f'DROP INDEX IF EXISTS "{name}"')


def downgrade() -> None:
    # 回退时按模型原定义重建（仅恢复结构，索引本身没有查询价值）。
    for name in _UNUSED_INDEXES:
        column = name.removeprefix("ix_retrieval_quality_events_")
        op.execute(
            f'CREATE INDEX IF NOT EXISTS "{name}" '
            f'ON retrieval_quality_events ("{column}")'
        )
