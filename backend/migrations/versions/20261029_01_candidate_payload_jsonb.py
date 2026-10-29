"""Store candidate JSON columns as jsonb so list queries stop re-parsing text.

候选列表每次请求都要在这些列上做多个 ``->`` 取值。``json`` 是文本存储，
PostgreSQL 对每个取值表达式都会重新解析整份文档：3 万行、约 538 MB 的实测
里，取三个字段要 6.3–7.3 秒。换成 ``jsonb`` 后同一查询降到约 1.5 秒。

重写整表在生产库实测约 10 秒，因此部署时需要和后端切换一起放进短维护窗口。
"""

from alembic import op


revision = "20261029_01_candidate_jsonb"
down_revision = "20261027_01_candidate_tasks"
branch_labels = None
depends_on = None


_COLUMNS = ("candidate_payload", "review_metadata", "selection_metadata")


def _alter_type(source: str, target: str) -> None:
    for column in _COLUMNS:
        op.execute(
            f"ALTER TABLE integration_ingestions "
            f"ALTER COLUMN {column} TYPE {target} USING {column}::{target}"
        )


def upgrade() -> None:
    _alter_type("json", "jsonb")


def downgrade() -> None:
    _alter_type("jsonb", "json")
