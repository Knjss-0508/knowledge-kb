# PostgreSQL 性能审计（2026-09-23）

本文记录一次**基于真实测量**的性能排查过程、发现和修复。
所有数字都来自线上实测，测量方法附在文末，可复现。

---

## 一、结论速览

| # | 发现 | 影响 | 处理 |
|---|---|---|---|
| 1 | `log_statement = all` 长期开启 | 日志 **26 GB**，单日 2.5~3.4 GB，约 11 行/秒 | ✅ 已改为 `none` |
| 2 | worker 每轮询周期执行两条**无索引的 JSON 表达式查询** | 每 2.5 秒消耗 **约 430 ms** 数据库时间（约 15%~20% 算力） | ✅ 已加表达式索引，降到 **0.12 ms** |
| 3 | 嵌入调用有约 **70 ms** 固定传输开销 | 每次召回固定成本 | ⚠️ 隧道 + SSH 开销，基本不可压缩 |
| 4 | 原文档「查询向量嵌入占总耗时约 84%」**是错的** | 会误导优化方向 | ✅ 已在本文更正 |

**最重要的一条**：真正的性能大头不在向量检索，也不在嵌入，而在**后台 worker 的重复查询**和**日志刷屏**。
向量检索本身只要约 28 ms。

---

## 二、发现 1：日志刷屏（26 GB）

```
log_statement = all          ← postgresql.conf 第 887 行
日志目录占用: 26 GB
单个日志文件: 2.5 ~ 3.4 GB（log_rotation_size = 10MB 形同虚设）
增长速率: 约 11 行/秒
```

一天 95 万行日志里，绝大多数是三类噪音：

| 次数（20 万行样本） | 内容 | 来源 |
|---|---|---|
| 35,123 | `SELECT 1` | SQLAlchemy 连接池健康检查 |
| 26,707 / 25,672 | `BEGIN` / `ROLLBACK` | 空事务（ORM 查询前的开启与回滚） |
| 15,386 | `LIMIT 1 FOR UPDATE SKIP LOCKED` | 任务队列轮询 |
| 7,703 / 7,683 | `knowledge_import_tasks` / `knowledge_vector_tasks` 轮询 | 后台 worker |

### 处理

```sql
ALTER SYSTEM SET log_statement = 'none';                    -- 不再记录每一条语句
ALTER SYSTEM SET log_min_duration_statement = '1s';         -- 只保留慢查询
SELECT pg_reload_conf();                                    -- SIGHUP 生效，零停机
```

`log_statement` 和 `log_min_duration_statement` 都是 **SIGHUP 参数**，
`pg_reload_conf()` 即可生效，**不需要重启数据库**。

实测效果：**11 行/秒 → 0 行/秒**。

> ⚠️ `postgresql.conf` 里仍写着 `log_statement = all`。`ALTER SYSTEM` 的优先级更高，
> 所以当前生效值是 `none`；但如果有人清理 `postgresql.auto.conf`，
> 这个配置会「复活」。建议同时改掉 `postgresql.conf`。

### 遗留项

日志目录仍有 **26 GB** 历史日志（`/www/server/pgsql/logs`）。
磁盘 180 GB 用了 118 GB，剩 63 GB，暂不影响运行。
清理或压缩需要维护者确认（历史日志可能仍有排查价值）。

---

## 三、发现 2：worker 的无索引 JSON 查询（真正的性能大头）

### 怎么发现的

临时把 `log_min_duration_statement` 设为 `0`（记录所有语句及其耗时），
发一次真实检索请求，然后读日志。一次请求期间数据库执行了 **79 条语句、合计 526 ms**：

| 耗时 | 语句 |
|---|---|
| **220.714 ms** | `knowledge_items` 查询（`IS NULL`） |
| **209.114 ms** | `knowledge_items` 查询（`= 值`） |
| 29.289 ms | 真正的向量检索 |
| 28.020 ms | 真正的向量检索 |
| 17.157 ms | 真正的向量检索 |

**关键点：那两条 220ms 的查询不是这次请求发出的** —— 它们跑在另一个 PID 上，
是**后台 worker** 每 2.5 秒执行一次的轮询。

### 这两条查询

```sql
-- 查询 A：找缺失该 key 的行
SELECT ... FROM knowledge_items
 WHERE status = 'PUBLISHED'
   AND knowledge_origin = 'model_configuration'
   AND CAST((source_fields ->> '_model_configuration_normalized_category_model_key')
            AS VARCHAR) IS NULL;

-- 查询 B：按该 key 精确查找
SELECT ... FROM knowledge_items
 WHERE status = 'PUBLISHED'
   AND knowledge_origin = 'model_configuration'
   AND CAST((source_fields ->> '_model_configuration_normalized_category_model_key')
            AS VARCHAR) = '["手机","iphone 14 pro max"]'
 LIMIT 2;
```

`source_fields` 的类型是 **`json`（不是 `jsonb`）**，且**没有任何针对该表达式的索引**，
所以两条都退化成对 `knowledge_items` 的顺序扫描。

### 数据分布（说明查询 A 纯属浪费）

```
总行数           18,630
model_configuration 已发布   17,694
该 key 为空            0      ← 查询 A 恒返回 0 行，却每次全表扫 18,630 行
该 key 非空       17,694
key 不同取值数    17,694      ← 查询 B 高度选择性
```

### 修复

```sql
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_knowledge_items_mc_normalized_category_model_key
  ON knowledge_items (CAST((source_fields ->> '_model_configuration_normalized_category_model_key') AS VARCHAR))
  WHERE knowledge_origin = 'model_configuration' AND status = 'PUBLISHED';
```

用 `CONCURRENTLY` 建，**不阻塞写入**。索引约 **1 MB**（全表 44 MB，因为是部分索引）。

### 效果

| 查询 | 加索引前 | 加索引后 | 提升 |
|---|---|---|---|
| A（`IS NULL`） | **215.745 ms** | **0.041 ms** | **5,262 倍** |
| B（`= 值`） | **166.571 ms** | **0.076 ms** | **2,191 倍** |

`Buffers` 从 `shared hit=6950` 降到 `shared hit=3` —— 从扫 6950 个缓冲页变成读 3 个。

验证索引确实被 worker 使用：30 秒内 `idx_scan` 增加 **12 次**（约每 2.5 秒一次，正是轮询周期）。

### 持久化

线上是用 `CREATE INDEX CONCURRENTLY` 直接建的，**重新部署会丢**。
已补 Alembic 迁移 `20260923_01_add_model_configuration_json_lookup_index.py`
（`IF NOT EXISTS`，在线上是空操作，在新环境才真正创建）。

### 还可以更进一步

查询 A 在当前数据下**恒返回 0 行**，也就是每次轮询都在为「什么都找不到」付一次扫描。
索引已经把代价降到 0.041 ms，但如果要彻底省掉这次查询，需要改 worker 的取数逻辑
（例如记录「该 key 已全部回填完成」的标志位），属于代码改动，不在本次范围内。

---

## 四、发现 3：嵌入调用的固定开销

### 更正一个错误结论

`docs/postgres-recall-tuning.md` 里写「查询向量嵌入占总耗时约 84%」。
**这个结论是错的**，会把人引向错误的优化方向。

真实端到端耗时（热缓存，即查询向量缓存已命中、不需要真嵌入）：

```
冷缓存  559 / 585 / 828 ms
热缓存  437 / 454 / 469 ms
```

热缓存下嵌入几乎为 0，请求仍需约 454 ms —— 所以嵌入**不是**大头。

### 嵌入本身的分解

后端侧测量（`embed_texts`）：

```
embed_texts(1条) = 95 ms
embed_texts(5条) = 123 ms   ->  每条仅 25 ms
复用连接单条: 94 / 92 / 89 ms
```

GPU 节点侧（TEI 容器日志）：

```
total_time=27.294ms  (tokenization 4.1ms, inference 22.5ms)
total_time=18.258ms  (inference 16.5ms)
total_time=16.032ms  (inference 14.1ms)
```

**推理只要 15~36 ms，后端却测到 89~95 ms —— 差约 70 ms 是纯传输开销**
（后端容器 → 宿主 → 旧服务器 sshd → SSH 反向隧道 → GPU 节点 → TEI）。

这部分是家庭宽带 RTT + SSH 加密的开销，**基本不可压缩**。
能优化的只有「少调用」—— 而查询向量缓存（`_QUERY_EMBEDDING_CACHE`，LRU 512 条）
已经在做了。

### 已存在但容易被忽略的缓存

`backend/app/services/knowledge_dedup.py` 里已有查询向量缓存：

```python
_QUERY_EMBEDDING_CACHE_MAXSIZE = 512
_QUERY_EMBEDDING_CACHE: OrderedDict[...]
```

所以「提高查询向量缓存命中率」这个待办项，**缓存机制本身已经有了**，
需要的是分析真实查询的重复分布，而不是从头实现缓存。

---

## 五、测量方法（可复现）

### 抓一次请求执行的所有 SQL 及耗时

```sql
-- 零停机：SIGHUP 生效
ALTER SYSTEM SET log_min_duration_statement = 0;
SELECT pg_reload_conf();
```

然后发一次真实请求，再从 `/www/server/pgsql/logs/` 里读：

```bash
tail -n +$((BEFORE+1)) postgresql-$(date +%F).log > /tmp/slice.txt
grep -E "duration: [0-9.]+ ms" /tmp/slice.txt \
  | sed -E 's/^[0-9-]+ ([0-9:]+).*duration: ([0-9.]+) ms.*/\2|\1/' \
  | sort -t'|' -k1 -rn | head -20
# 合计
grep -oE "duration: [0-9.]+ ms" /tmp/slice.txt | awk '{s+=$2} END {print s" ms / "NR" 条"}'
```

**记得还原**：

```sql
ALTER SYSTEM RESET log_min_duration_statement;
SELECT pg_reload_conf();
```

### 发真实检索请求

接口需要三个头部和两个必填字段：

```python
# POST /api/v1/integration/standard-search
headers = {
    "X-Integration-Key": os.environ["RETRIEVAL_API_KEY"],   # 注意不是 INTEGRATION_API_KEY
    "X-Conversation-Id": "900000000000000001",               # 必须匹配 ^[0-9]{1,64}$
    "X-Request-Id": "900000000000000001",
}
payload = {
    "conversation_id": "900000000000000001",
    "request_id": "900000000000000001",
    "query": "年假有多少天",
    "normalized_question": "年假有多少天",                     # 必填，空值会被校验拒绝
    "knowledge_origin": "headquarters_standard",
    "business_type": "self_operated",                        # 只能是 self_operated / aggregated
}
```

### 区分「谁发出的查询」

日志行里的 PID 是关键：`[80570]` 与 `[105874]` 是不同连接。
把请求期间所有语句按 PID 分组，就能分辨哪些来自用户请求、哪些来自后台 worker。

---

## 六、排查中走过的弯路（记录以免重复）

| 弯路 | 原因 | 教训 |
|---|---|---|
| 认为「统计信息过期导致计划变差」 | 实测估算 18,341 vs 实际 18,630，只差 1.6% | 先测量再下结论 |
| 用子查询做向量操作数测 EXPLAIN | 非常量操作数用不了 HNSW 索引，测出的是顺序扫描 | 见 `postgres-recall-tuning.md` 的三个坑 |
| 用 `PGOPTIONS` 覆盖 `ef_search` | 会被 `ALTER DATABASE` 库级设置压过去 | 同上 |
| 认为「嵌入占 84%」 | 未区分冷/热缓存，且未测传输开销 | 分层测量，别只看总分 |
| 以为 220ms 的查询来自用户请求 | 没看 PID | 日志里 PID 是最便宜的区分手段 |

---

## 七、后续可选项

| 项 | 收益 | 代价 |
|---|---|---|
| 清理/压缩 26 GB 历史日志 | 释放磁盘 | 需维护者确认历史日志是否还有价值 |
| 同时改掉 `postgresql.conf` 的 `log_statement = all` | 防止配置「复活」 | 需改配置文件并 reload |
| 安装 `pg_stat_statements` | 可**持续**按总耗时排序查询，不必每次临时开日志 | **需重启数据库**（实测中断约 1.4 秒） |
| 改 worker 取数逻辑，省掉恒返回 0 行的查询 A | 再省 0.041 ms/周期 | 代码改动 |
| 排查 `feedback-status` 接口的 404 | 有客户端在调已删除的接口 | 需确认调用方 |

> `pg_stat_statements` 的扩展文件在 `/www/server/pgsql/share/extension/` 下齐全，
> 只差 `shared_preload_libraries` + 重启。它能让「哪条查询最耗数据库」变成一个随时可查的问题，
> 建议安排一次低峰期重启装上。
