# 线上故障与修复记录：检索占用连接池导致整站挂起（2026-10-29）

## 1. 现象

2026-10-29 上午，`https://knowledgekb.powerzhuan.cn/app` 打开后一直转圈：

- 10:00 起 Nginx 访问日志出现 502/499，`/app`、`/` 与 `/api/v1/auth/me` 均为 `upstream timed out`；
- 服务器本机 `curl http://127.0.0.1:8000/health` 与 `/app` 均 10 秒超时；
- 容器状态是 `running`、重启次数 0，CPU 0.18%，内存约 3.9 GB；
- 10:55 重启容器后立即恢复（`/health` 2 ms、`/app` 6 ms）。

## 2. 根因

检索接口在等待外部 embedding 服务时，一直占着数据库连接。

调用链：`search_standard_provider_knowledge` → `search_embeddings()` → 查适用范围 → `_query_embedding()`（同步等待外部服务）。

| 环节 | 证据 |
| --- | --- |
| 连接池很小 | `pool_size=5`、`max_overflow=10`，共 15 个连接 |
| 外部调用很慢 | `EMBEDDING_TIMEOUT_SECONDS=30`；embedding 走 `host.docker.internal:18080` 反向 SSH 隧道 |
| 隧道当时在抖动 | 10:51 前后隧道重建，`sshd` 反复报 `bind [0.0.0.0]:18780 / 19000: Address already in use` |
| 后端确认不可用 | 日志 2203 条 `Embedding unavailable`、5079 条超时 |
| 连接池被打满 | 首个 `QueuePool limit of size 5 overflow 10 reached` 出现在 10:01（UTC 02:01），当天累计 4673 条 |
| 整站连带失败 | 连接耗尽后 `/health` 也要排队 30 秒，Nginx 判上游超时，返回 502/499 |

关键点：`/health` 本身不查库，但它在 Starlette 的同步线程池里执行，而线程池已被大量“排队等连接”的请求占满，所以健康检查也一起挂住。

复现（修复前生产容器内，3 个检索请求）：`engine.pool.checkedout()` 在 embedding 等待期间峰值为 3，直接证明连接被长期占用。

## 3. 修复

PR #242（`1a16017`）在 `backend/app/services/knowledge_dedup.py` 增加
`_release_db_connection_before_external_call()`：

- 适用范围查询已经结束，后续查询会重新取连接，检索结果与过滤条件不变；
- 缓存命中不等待外部服务，保持原行为，不付重复取连接的开销；
- 会话存在未提交写入（`db.new/dirty/deleted` 非空）时不关闭，避免回滚业务改动。

## 4. 验证

单元与回归：

- 新增 3 项测试：外部调用前释放连接、有未提交写入时不释放、缓存命中时不释放；
- `test_applicability_scope.py`、`test_knowledge_dedup.py`、`test_deduplication_workflows.py`、`test_embedding.py` 全部通过；
- 完整 `backend/tests`：1049 passed、4 failed；这 4 项在改动前的 `master`（`5d4f4bc`）上同样失败，与本次无关。

生产验证（修复镜像 `knowledge-kb-backend:search-pool-fix-1a16017`）：

| 场景 | 修复前 | 修复后 |
| --- | --- | --- |
| 3 个检索在 embedding 期间 | 占用 3 个连接 | 入口处降到 0，等待期间保持 0 |
| 30 个检索同时卡在 embedding | 连接池被占满 | `checkedout=0` |
| 同一时刻 `/health` | 30 秒超时 | 200，4 ms |
| 故障后连接池 | 需重启恢复 | `checkedout=0`，无泄漏 |

## 5. 部署

- 源码：`1a16017` 的 `git archive` 导出，构建时保留线上未被 Git 跟踪的
  `manhattan.py` 报错文案改动（sha256 `f9327056…`，构建后校验一致）。
- 镜像：`knowledge-kb-backend:search-pool-fix-1a16017`（`sha256:e9f38463…`）。
- 本次无数据库迁移，`alembic` 保持 `20261027_01_candidate_tasks`。
- 用 `run-kb-backend.sh` 切换；切换前后环境变量 md5（`89cb3c17…`）与运行规格 md5（`57386af2…`）完全一致。
- `kb-redis`、`kb-embedding-qwen` 未重建。

切换后：`/health` 200（2 ms）、`/app` 200（7 ms）、公网 `/app` 200，日志 0 条
`QueuePool`/`Traceback`。

回滚锚点：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:manhattan-error-copy-b9a38c2-nspgze
```

## 6. 遗留问题与建议

1. **隧道重连策略（最高优先级）**：`18080` 反向隧道抖动时，`sshd` 会因
   `Address already in use` 反复绑定失败。建议给隧道加重连退避、旧连接清理和
   端口占用检测，或改用 `autossh`/systemd 单元托管。
2. **检索与向量化解耦**：本次只解决“等待期间不占连接”。如果希望隧道故障时
   检索快速失败而不是等 30 秒，可把 `EMBEDDING_TIMEOUT_SECONDS` 降到 5–8 秒，
   并给 `/health` 与 `/ready` 用独立执行器，避免被业务线程池拖住。
3. **候选列表慢查询**：`/api/v1/integration/candidate-reviews` 每次都会全量解析
   29,881 行 JSON（约 538 MB），实测单请求 6.3 秒，6 并发时每个 20 秒。本次未改，
   但它同样会消耗连接池，建议改为 SQL 侧聚合 + 分页。
4. **生产镜像漂移**：线上 `manhattan.py` 有一处未被 Git 跟踪的文案改动。建议
   把它提交回仓库，避免每次部署都要手工保留。
