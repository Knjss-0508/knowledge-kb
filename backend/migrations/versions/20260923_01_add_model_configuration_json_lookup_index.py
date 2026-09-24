"""Add expression index for model configuration normalized category/model key lookup.

Revision ID: 20260923_01
Revises: 20260828_01
Create Date: 2026-09-23

背景
----
后台 worker 每个轮询周期（约 2.5 秒）都会对 knowledge_items 执行两条查询，
过滤条件是对 json 字段做表达式取值：

    WHERE status = 'PUBLISHED'
      AND knowledge_origin = 'model_configuration'
      AND CAST((source_fields ->> '_model_configuration_normalized_category_model_key')
               AS VARCHAR) IS NULL            -- 查找缺失该 key 的行
    WHERE status = 'PUBLISHED'
      AND knowledge_origin = 'model_configuration'
      AND CAST((source_fields ->> '_model_configuration_normalized_category_model_key')
               AS VARCHAR) = '<value>'
      LIMIT 2

source_fields 是 json（不是 jsonb），且没有任何针对该表达式的索引，
因此两条查询都退化为对 knowledge_items 的顺序扫描。

线上实测（knowledge_items 共 18630 行，其中 model_configuration 已发布 17694 行）：

    IS NULL 查询    215.745 ms   ->  0.041 ms   （约 5262 倍）
    = 值查询        166.571 ms   ->  0.076 ms   （约 2191 倍）

按 2.5 秒一个轮询周期计算，这两条查询原本持续占用数据库约 15%~20% 的算力。

注意：IS NULL 那条查询在当前数据下恒返回 0 行（17694 行的该 key 全部非空），
也就是说它每次都在为「什么都找不到」付出一次全表扫描。索引消除了这个代价；
若后续要彻底省掉这次查询，需要改 worker 的取数逻辑，不在本迁移范围内。

索引设计
--------
- 表达式索引：source_fields ->> '<key>' 的 json 取值函数是 IMMUTABLE，可建索引。
- 部分索引（WHERE knowledge_origin / status）：只覆盖该查询实际关心的行，
  索引体积仅约 1 MB（全表 44 MB）。
- 用 IF NOT EXISTS 保证幂等：线上已用 CREATE INDEX CONCURRENTLY 建过一次，
  本迁移在线上是空操作，在新环境才会真正创建。
"""

from alembic import op


revision = "20260923_01"
down_revision = "20260828_01"
branch_labels = None
depends_on = None


INDEX_NAME = "ix_knowledge_items_mc_normalized_category_model_key"

EXPRESSION = (
    "CAST((source_fields ->> '_model_configuration_normalized_category_model_key') "
    "AS VARCHAR)"
)


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {INDEX_NAME} "
        f"ON knowledge_items ({EXPRESSION}) "
        "WHERE knowledge_origin = 'model_configuration' AND status = 'PUBLISHED'"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {INDEX_NAME}")
