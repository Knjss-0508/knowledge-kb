#!/bin/bash
# 应用 PostgreSQL 召回相关的库级参数调优。
#
# 背景：pgvector 的 hnsw.ef_search / hnsw.iterative_scan 是【扩展级 GUC】，
# 用 ALTER DATABASE ... SET 设置它们需要【超级用户】权限，普通应用用户
# （knowledge_admin）会报 permission denied。因此本脚本必须以 postgres
# 超级用户身份执行。
#
# 用法：
#   sudo bash scripts/apply-postgres-recall-tuning.sh            # 应用并验证
#   sudo bash scripts/apply-postgres-recall-tuning.sh --verify   # 只验证
#   sudo bash scripts/apply-postgres-recall-tuning.sh --rollback # 恢复默认
#
# 环境变量（一般不用改）：
#   PG_BIN            默认 /www/server/pgsql/bin
#   DB_NAME           默认 knowledge_base
#   EF_SEARCH         默认 200
#   ITERATIVE_SCAN    默认 strict_order
set -u

PG_BIN="${PG_BIN:-/www/server/pgsql/bin}"
DB_NAME="${DB_NAME:-knowledge_base}"
EF_SEARCH="${EF_SEARCH:-200}"
ITERATIVE_SCAN="${ITERATIVE_SCAN:-strict_order}"

MODE="${1:-apply}"

log()  { printf '  %s\n' "$*"; }
fail() { printf '  [错误] %s\n' "$*" >&2; exit 1; }

# 以 postgres 超级用户执行 SQL。ALTER DATABASE 设置扩展 GUC 必须超级用户。
psql_super() {
  sudo -u postgres "$PG_BIN/psql" -d "$DB_NAME" -Atc "$1"
}

# 只读查询，用应用用户或超级用户都行
psql_ro() {
  sudo -u postgres "$PG_BIN/psql" -d "$DB_NAME" -Atc "$1"
}

echo "=== 0) 前置检查 ==="
[ -x "$PG_BIN/psql" ] || fail "找不到 $PG_BIN/psql"
log "psql: $PG_BIN/psql"

PGVER=$(psql_ro "SHOW server_version" 2>/dev/null | tr -d ' ')
[ -n "$PGVER" ] || fail "无法连接数据库 $DB_NAME"
log "PostgreSQL 版本: $PGVER"

# pgvector 是否已装（没装的话这两个 GUC 根本不存在）
VECVER=$(psql_ro "SELECT extversion FROM pg_extension WHERE extname='vector'" 2>/dev/null)
[ -n "$VECVER" ] || fail "数据库 $DB_NAME 未安装 pgvector 扩展，无需也无法设置 hnsw.* 参数"
log "pgvector 版本: $VECVER"

echo
echo "=== 1) 当前值（注意：ALTER DATABASE 只影响【新会话】） ==="
psql_ro "SELECT 'hnsw.ef_search = ' || current_setting('hnsw.ef_search')" 2>&1 | sed 's/^/  /'
psql_ro "SELECT 'hnsw.iterative_scan = ' || current_setting('hnsw.iterative_scan')" 2>&1 | sed 's/^/  /'
echo "  --- 库级持久设置（pg_db_role_setting，PG18 起 pg_database.datconfig 已移除） ---"
psql_ro "SELECT '  ' || coalesce(d.setconfig::text, '(未设置)')
           FROM pg_db_role_setting s
           JOIN pg_database d ON d.oid = s.setdatabase
          WHERE d.datname = '$DB_NAME' AND s.setrole = 0" 2>&1

case "$MODE" in
  --verify|verify)
    echo
    echo "=== 验证完成（未做任何修改） ==="
    exit 0
    ;;
  --rollback|rollback)
    echo
    echo "=== 2) 回滚：清除库级设置，恢复 pgvector 默认 ==="
    psql_super "ALTER DATABASE \"$DB_NAME\" RESET hnsw.ef_search"    || fail "重置 hnsw.ef_search 失败"
    psql_super "ALTER DATABASE \"$DB_NAME\" RESET hnsw.iterative_scan" || fail "重置 hnsw.iterative_scan 失败"
    log "已重置"
    echo
    echo "  回滚后的库级设置："
    psql_ro "SELECT '  ' || coalesce(d.setconfig::text, '(未设置)')
               FROM pg_db_role_setting s
               JOIN pg_database d ON d.oid = s.setdatabase
              WHERE d.datname = '$DB_NAME' AND s.setrole = 0" 2>&1
    echo
    echo "  ⚠️ 已存在的连接仍持有旧值，需重启应用才会全部生效："
    echo "     docker restart kb-backend"
    exit 0
    ;;
esac

echo
echo "=== 2) 应用设置 ==="
psql_super "ALTER DATABASE \"$DB_NAME\" SET hnsw.ef_search = $EF_SEARCH" \
  || fail "设置 hnsw.ef_search 失败（是否以 root/postgres 身份运行？）"
log "hnsw.ef_search = $EF_SEARCH"
psql_super "ALTER DATABASE \"$DB_NAME\" SET hnsw.iterative_scan = $ITERATIVE_SCAN" \
  || fail "设置 hnsw.iterative_scan 失败"
log "hnsw.iterative_scan = $ITERATIVE_SCAN"

echo
echo "=== 3) 验证（新会话应读到新值） ==="
NEW_EF=$(psql_ro "SHOW hnsw.ef_search" 2>/dev/null | tr -d ' ')
NEW_IS=$(psql_ro "SHOW hnsw.iterative_scan" 2>/dev/null | tr -d ' ')
log "新会话 hnsw.ef_search = $NEW_EF"
log "新会话 hnsw.iterative_scan = $NEW_IS"

[ "$NEW_EF" = "$EF_SEARCH" ] || fail "ef_search 未生效（读到 $NEW_EF，期望 $EF_SEARCH）"
[ "$NEW_IS" = "$ITERATIVE_SCAN" ] || fail "iterative_scan 未生效（读到 $NEW_IS，期望 $ITERATIVE_SCAN）"

echo
echo "=== 4) 候选池大小对比（这是调优的直接目的） ==="
echo "  说明：向量检索的 LIMIT 作用在【向量行】上，而一个知识条目平均有 4~5 个"
echo "        向量分片。ef_search 过小会导致去重后的知识条目数远少于 top_k。"
for ef in 40 "$EF_SEARCH"; do
  n=$(sudo -u postgres env PGOPTIONS="-c hnsw.ef_search=$ef" \
      "$PG_BIN/psql" -d "$DB_NAME" -Atc "
        WITH q AS (SELECT embedding_vector FROM knowledge_search_embeddings
                    WHERE embedding_vector IS NOT NULL LIMIT 1)
        SELECT count(*) || ' 行 / ' || count(DISTINCT knowledge_id) || ' 个知识条目'
          FROM (SELECT kse.knowledge_id
                  FROM knowledge_search_embeddings kse, q
                 WHERE kse.embedding_vector IS NOT NULL
                 ORDER BY kse.embedding_vector <=> q.embedding_vector
                 LIMIT 120) t" 2>/dev/null)
  log "ef_search=$ef -> $n"
done

echo
echo "=== 完成 ==="
echo "  ⚠️ 已存在的数据库连接仍持有旧值。要让应用全部生效："
echo "     docker restart kb-backend"
echo
echo "  回滚："
echo "     sudo bash $0 --rollback"
