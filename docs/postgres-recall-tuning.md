# PostgreSQL 召回性能调优（库级 HNSW 参数）

> 适用：`knowledge_base`（PostgreSQL 18.0 + pgvector 0.8.6）
> 脚本：`scripts/apply-postgres-recall-tuning.sh`

## 一、问题

知识召回（`POST /api/v1/knowledge/search`）在向量检索阶段拿到的候选太少，
导致最终返回的知识条目数远低于 `top_k`，召回质量被白白浪费。

原因在 `backend/app/services/knowledge_dedup.py` 的向量检索语句：

```sql
ORDER BY embedding_vector <=> :query_vector
LIMIT max(top_k * 12, 50)          -- 例如 top_k=10 -> LIMIT 120
```

**这个 LIMIT 作用在「向量行」上，而不是「知识条目」上。** 而
`knowledge_search_embeddings` 表里一个知识条目平均有 **4.67 个向量分片**
（4,271 行 / 914 个知识条目），所以 120 行去重后可能只剩 20 多个条目。

再加上 pgvector 的 HNSW 索引默认 `hnsw.ef_search = 40`，索引本身只吐 40 个候选，
去重后只剩约 20 个知识条目 —— 这就是瓶颈。

## 二、实测数据

| 配置 | 返回向量行 | 去重后知识条目 | 执行时间 |
|---|---|---|---|
| `ef_search=40`（默认） | 41 | **20** | 1.8 ms |
| `ef_search=200` | 120 | **54** | 2.7 ms |
| `ef_search=200` + `iterative_scan=relaxed_order` | 120 | 54 | 2.7 ms |
| 顺序扫描（对照） | 120 | 54 | **28.0 ms** |

结论：

1. `ef_search` 从 40 提到 200，**去重后候选从 20 个涨到 54 个（2.7 倍）**，代价只有 0.9 ms。
2. **HNSW 索引必须保留** —— 顺序扫描是 28 ms，比索引慢约 10 倍。这组数据也是
   「不要为了别的目的把 PostgreSQL 换成 MySQL」的直接依据：MySQL 没有 HNSW 索引，
   也没有可走索引的 `<=>` 余弦运算符，换过去召回会直接退化成全表扫描。

为什么选 `strict_order` 而不是 `relaxed_order`：

- `relaxed_order` 允许返回**乱序**结果，会破坏 `ORDER BY distance LIMIT` 的语义 ——
  也就是说「最近的 N 条」不再真的是最近的 N 条，这对召回是不可接受的。
- 两者在本场景下候选数和耗时相同（都是 120 / 54 / 2.7 ms），所以选语义正确的那个。

## 三、执行

```bash
# 应用（需 root，脚本内部会切到 postgres 超级用户）
sudo bash scripts/apply-postgres-recall-tuning.sh

# 只查看当前值，不做修改
sudo bash scripts/apply-postgres-recall-tuning.sh --verify

# 回滚
sudo bash scripts/apply-postgres-recall-tuning.sh --rollback
```

**为什么必须超级用户**：`hnsw.ef_search` / `hnsw.iterative_scan` 是 **pgvector 的扩展级 GUC**，
`ALTER DATABASE ... SET` 设置扩展 GUC 需要超级用户权限。应用用户 `knowledge_admin`
（`rolsuper=false`、`rolcreatedb=false`）执行会报：

```
ERROR:  permission denied to set parameter "hnsw.ef_search"
```

脚本内部通过 `sudo -u postgres` 提权，只修改 `knowledge_base` 这一个库，
**不影响同实例的其他数据库**。

## 四、生效范围（重要）

`ALTER DATABASE ... SET` **只对新建立的会话生效**。应用后端用的是连接池，
已经存在的连接仍持有旧值。

所以应用之后必须重启后端：

```bash
docker restart kb-backend
```

## 五、验证

```bash
# 库级持久设置
sudo -u postgres /www/server/pgsql/bin/psql -d knowledge_base -Atc "
  SELECT d.datname || ' -> ' || s.setconfig::text
    FROM pg_db_role_setting s
    JOIN pg_database d ON d.oid = s.setdatabase
   WHERE s.setrole = 0"

# 期望输出
#   knowledge_base -> {hnsw.ef_search=200,hnsw.iterative_scan=strict_order}
```

> 注意：PostgreSQL 18 已移除 `pg_database.datconfig` 列，必须查 `pg_db_role_setting`。

新会话读值：

```bash
sudo -u postgres /www/server/pgsql/bin/psql -d knowledge_base -Atc "SHOW hnsw.ef_search"
```

## 六、相关文件

| 文件 | 说明 |
|---|---|
| `backend/app/services/knowledge_dedup.py` | 向量检索语句与 `LIMIT max(top_k*12, 50)` |
| `backend/migrations/versions/20260712_02_vector_indexes.py` | HNSW 索引的创建（`vector_cosine_ops`） |
| `scripts/apply-postgres-recall-tuning.sh` | 本调优的应用/验证/回滚脚本 |

## 七、后续可选项

| 项 | 收益 | 代价 |
|---|---|---|
| `shared_buffers` 128MB → 2GB | 消除冷启动尖峰（首次召回实测 503 ms，因 HNSW 索引被挤出缓冲区） | **需重启 PostgreSQL**，约 5~10 秒中断 |
| 向量检索改用「先按知识条目聚合再 LIMIT」 | 从根上解决「LIMIT 作用在向量行」的问题 | 需要改 SQL 与索引，改动面较大 |
| 提高查询向量缓存命中率 | 查询向量嵌入占总耗时约 84% | 需要分析真实查询分布 |
