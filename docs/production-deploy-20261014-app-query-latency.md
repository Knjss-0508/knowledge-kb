# 生产上线与回滚记录：/app 首屏与 5 个慢接口优化（2026-10-14）

本文记录 2026-10-14 把 `/app` 首屏与 5 个慢接口的优化（#194）上线到
`https://knowledgekb.powerzhuan.cn` 的完整过程：首次切换、上线后发现的领单 500 回归、
回滚、修复（#197）与最终重新切换。所有时间均为 UTC（宿主回显为 UTC，`date -Is` 为 +08:00）。

线上现状（截至本次最终切换）：

| 项 | 值 |
| --- | --- |
| 线上镜像 | `knowledge-kb-backend:blind-label-fix-876da12` |
| 镜像 ID | `sha256:567d2829671a0107d58437e4f3028f2c351ad9493788e90e3de1729f972f1f12` |
| 源码提交 | `master` 的 `876da12`（= #194 + #195 + #196 + #197） |
| 容器启动时间 | 2026-10-14T09:05:36.537163975Z |
| 回滚锚点 | `knowledge-kb-backend:log-verdict-retention-20261010` |
| 回滚命令 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:log-verdict-retention-20261010` |
| 数据库迁移 | 无（`alembic_version` 全程为 `20261012_01_blind_label_target`，已是 head） |

## 1. 上线内容

| PR | 提交 | 内容 |
| --- | --- | --- |
| #194 | `b2a734a` | 候选看板 / 置信度训练 / 盲标 / 召回分析等 5 个慢接口提速；检索分析与反馈记录改为流式取数并修正分页顺序（取行强制 `ORDER BY`，分组重开直接报错）；manhattan 缓存与 HTML 路由支持 `ETag` / `304`；前端首屏不再无条件并发触发秒级接口；`.gitattributes` 统一行尾为 LF |
| #195 | `b23bfee` | 忽略仓库根的本机运行产物目录（`.dsh-*`、`outputs/`） |
| #196 | `711682c` | 删除仓库根遗留的一次性盲标验证脚本 |
| #197 | `876da12` | 修复盲标领单 500（#194 回归，见第 4、5 节） |

#194 涉及 10 个文件、+1641 / -386：
`.gitattributes`、`backend/app/main.py`、`backend/app/routes/confidence_training.py`、
`backend/app/routes/integration.py`、`backend/app/routes/manhattan.py`、
`backend/app/services/blind_labeling.py`、`backend/app/services/retrieval_quality.py`、
`backend/tests/test_retrieval_quality_feedback.py`、`docs/server-deployment-boundary.md`、
`frontend/index.html`。无 Alembic 迁移。

## 2. 首次切换（08:23:26Z）

用主机规范脚本切换，镜像 `knowledge-kb-backend:app-query-latency-b2a734a`
（`sha256:ea2dcd19e21e9320bc406b76f6c319b2115915a7734954eee85b7b2a388b7ff4`）：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:app-query-latency-b2a734a
```

切换前后一致性（`docker inspect` 逐项比对）：

- `.env` 派生的容器环境变量 md5 前后同为 `582ccc90ebd2e3b556b88fda70862a1a`（74 个键）；
- 挂载集合相同（顺序不同），`cmd` / `restart` / 网络 / 端口绑定不变：
  `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`、
  `unless-stopped`、`knowledge-kb_default`、`127.0.0.1:8000:8000`。

部署与 `master` 的字节级一致（`docker exec sha256sum`）：

| 文件 | sha256 |
| --- | --- |
| `app/services/blind_labeling.py` | `6981f1d10407b836b9bcd6ef75cf741f51528265045d48885e69bf6e1688cc19` |
| `app/routes/integration.py` | `8dc6ccbbec68dbc0cbff2013f0d419af2315d30cd4137c87517384ee1fe62ffd` |
| `app/routes/confidence_training.py` | `0d9e4fcd…` |
| `app/routes/manhattan.py` | `044706b5…` |
| `app/services/retrieval_quality.py` | `745280754f318d52ae5e39763507b8756cd454ed62518cd7ee4d86154518abc4` |
| `app/main.py` | `107a64e04a18080621e73201f3ee4423e7f52eba0e9f86227e7a70385f7a6f0c` |
| `/frontend/index.html` | `f866f2169db523172d272e7c5cef527f384dcb4c5dd0487694ce3727b29cde3e` |

切后验证：

- `/health` 200；`https://knowledgekb.powerzhuan.cn/app` 200，HTML 750,041 B（sha256 与新镜像一致）；
- HTTP 条件请求生效：`/app`、`/`、`/login` 均返回 `etag` + `last-modified` +
  `cache-control: no-cache, must-revalidate, max-age=0`，带 `If-None-Match` 与
  `If-Modified-Since` 均返回 **304**；
- 接口 A/B（新旧实现同库对跑，共 33 例）：**33/33 identical，drift=0**。
  候选看板 11 例（`limit=100` 4.675s → 3.143s、`offset=100` 4.615s → 3.198s、
  `limit=500` 4.431s → 3.386s、`review_status=pending` 4.540s → 3.196s、
  `priority_only` 4.749s → 3.046s）；置信度训练 22 例（`overview` 3.790s/2 条 SQL →
  0.501s/2 条、`items` 无筛选 `limit=200` 3.820s → 0.049s、`limit=500` 4.432s → 0.071s、
  `band=B0` 4.082s → 0.396s、`band=B3` 3.743s → 0.424s、全筛选 `limit=10` 3.955s → 0.438s、
  补零参数 `limit=5` 3.844s → 0.441s）；
- 盲标总览 A/B 10 例全部 `identical=True legacy_stable=True`：无参 `8248.8ms/15733` 条 SQL →
  `952.2ms/26` 条；`status=disputed` `6394.8ms/12103` → `884.6ms/26`；唯一变慢是
  `keyword` 命中极少时 `251.8ms/2` → `806.5ms/26`（旧实现在关键词只命中 2 条时只发 2 条 SQL）；
- 召回分析取数上界 A/B（SQL `EXCEPT` 口径）：7 天窗口 `47710 = 47710`、全窗口 `123107 = 123107`，
  `a_only=0 b_only=0`；对照口径（每请求只取一条代表事件）会丢一半来源池事件，未采用；
- payload 多重集 A/B：旧上界 + 分组 与 新分页取数 双双得到 **23,895** 个 payload，
  `left_only=0 right_only=0 identical=True`；去掉 `ORDER BY` 时新实现按设计直接报错
  （`retrieval analytics event rows must be ordered by (conversation_id, request_id); request
  2110229418344449032/qa-web:2110229418344449032:mv7mu6pe:221 reappeared after its payload was built`）。

## 3. 事故：盲标领单 100% 500（08:27:31Z 起）

首次切换后，应用日志出现 24 行 `Traceback` 与 12 行 500 响应，全部是
`POST /api/v1/blind-labeling/my-batch%3Aclaim?target_count=50`，时间戳为
08:27:31、08:27:34、08:27:40、08:27:46、08:27:50、08:27:53、08:28:02、08:29:00、
08:30:18、08:30:20、08:35:43、08:39:12（均晚于 08:23:26Z 的切换时刻）。堆栈末端：

```text
routes/blind_labeling.py:206 claim_my_batch -> routes/blind_labeling.py:149 _claim_response
services/blind_labeling.py:632 claim_batch -> services/blind_labeling.py:547 _choose_assignments
services/blind_labeling.py:1244 work_order_status -> services/blind_labeling.py:1011 consensus_for_work_order
services/blind_labeling.py:999  _completed_assignments -> services/blind_labeling.py:1000 <lambda>
AttributeError: completed_at
```

根因：#194 为消除盲标总览的 N+1 查询，给 `work_order_status()` 增加了
`loaded_assignments` 参数；未预加载时它构造的是**单列投影**
`db.query(BlindLabelAssignment.status)`，却无条件把这份投影当作完整 ORM 分配行
传给 `consensus_for_work_order()`，`_completed_assignments()` 于是按
`item.completed_at` 排序而抛错（`KeyError: 'completed_at'` →
`AttributeError: completed_at`）。只有当候选工单至少有一条 `status='completed'` 的分配时
才触发，因此既有单元测试（分配状态只有 `assigned`）没有覆盖这条分支，上线前的 A/B 也
只覆盖读接口、没有覆盖领单这条写路径。

## 4. 回滚（08:56:44Z）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:log-verdict-retention-20261010
```

回滚后：容器环境 md5 仍为 `582ccc90ebd2e3b556b88fda70862a1a`、挂载集合不变；
容器内只读复现（对 5 张已完成分配最多的工单 + 3 张无已完成分配的工单直接调用
`work_order_status()`）得到 `REPRO SUMMARY: ok=8 failed=0`；标注提交恢复 `201 Created`；
`/health` 200、`/app` 200（ttfb 0.048s）；日志不再出现 claim 500。

## 5. 修复（PR #197 / `876da12`）

`backend/app/services/blind_labeling.py`：只有调用方确实预加载了完整 ORM 行
（盲标总览列表路径）时才把 `rows` 透传下去，其它调用方传 `None`，由
`consensus_for_work_order()` 自己查询完整分配行——即恢复 #194 之前的行为。

`backend/tests/test_blind_labeling.py`：新增回归测试
`test_claim_path_tolerates_a_work_order_that_already_has_a_completed_assignment`，
同时断言 `work_order_status()` 不再抛错（返回 `partial`）与真实领单路径能领到该工单。

验证（都在生产容器内运行，运行时与线上一致）：

| 运行 | 结果 |
| --- | --- |
| 修复前基线（master 的 service `sha256 6981f1d1…` + 新测试） | `1 failed, 48 passed`，失败点即 `AttributeError: completed_at`（`sqlalchemy/engine/result.py:201`） |
| 修复后（本修复 service `sha256 18fca5b5…` + 新测试） | `49 passed` |
| 静态检查 | `python -m py_compile` 通过 |

## 6. 最终切换（09:05:36Z）

镜像按 `master` `876da12` 重新构建为 `knowledge-kb-backend:blind-label-fix-876da12`
（`sha256:567d2829671a…`，496 MB），切换方式与首次相同。切换前后：

- 环境变量 md5 同为 `582ccc90ebd2e3b556b88fda70862a1a`；挂载 md5 同为
  `88e3d19a958535e437d47c2456cfcd3b`；`cmd` / `entrypoint=null` / `restart=unless-stopped` /
  端口 `127.0.0.1:8000:8000` / 网络 `knowledge-kb_default` /
  `--add-host=host.docker.internal:host-gateway` 全部不变；
  挂载为 `/app/uploads`、`/app/data:z`、`/app/nlogs:ro`；
- 线上 6 个文件 sha256 与本机 `master` 逐项相同：`blind_labeling.py`
  `18fca5b546c2e23f78073eb1088fe4b2303f09ca3b8a7cdf5e7e979cd3caded7`、
  `routes/blind_labeling.py` `b5b719375f1998a6cb919ba5d3b499cb4ea137e7f8718d9218ee4baa2425a91a`、
  `routes/integration.py` `8dc6ccbb…`、`services/retrieval_quality.py` `74528075…`、
  `main.py` `107a64e0…`、`/frontend/index.html` `f866f216…`；
- 领单路径与 #194 前镜像（回滚锚点）的输出**逐字相同**（`diff` 无差异，
  `REPRO SUMMARY: ok=8 failed=0`）——修复后该路径的判定结果与优化前一致；
- 上线后真实用户领单成功：`POST /api/v1/blind-labeling/my-batch%3Aclaim?target_count=50`
  返回 `200 OK`（修复前同一请求为 500）；近 15 分钟 `Traceback` 0、500 计数 0；
- `/health` 200（0.002s）、`https://knowledgekb.powerzhuan.cn/app` 200（750,041 B）；
- HTTP 条件请求：`/app`、`/`、`/login` 三路由 `If-None-Match` 与 `If-Modified-Since`
  均返回 304（`HEAD` 请求返回 405，属预期，浏览器走 `GET`）；
- 用新镜像作为运行时再跑一次 `tests/test_blind_labeling.py`：`49 passed`。

## 7. 经验与后续

1. **只读接口 A/B 通过 ≠ 可以上线**：本次 33 例接口 A/B、盲标总览 A/B、召回与 payload
   等价性全部通过，但领单这条**写路径**没有等价性用例，回归就是这样漏出去的。涉及
   共享服务函数的改动，验收清单里必须包含调用该函数的写路径（领单 / 提交 / 仲裁）。
2. **生产 `kb-backend` 不是 Compose 管理的容器**：`docker compose ... up -d --no-deps backend`
   会因容器名冲突回退创建 `xxxxxxxx_kb-backend` 新容器（不接管流量，但会留下垃圾容器和
   被重写的重建参数）。日常只替换后端镜像必须使用
   `bash /opt/knowledge-kb-runtime/run-kb-backend.sh <镜像标签>`，详见
   `docs/server-deployment-boundary.md`。
3. **切换前后必须固定比对项**：环境变量 md5、挂载集合、`cmd` / `entrypoint` / 重启策略 /
   端口 / 网络 / `--add-host`，以及镜像内关键文件的 sha256（与 `master` 对齐）。本次事故与
   回滚都靠这些比对项快速确认「只有后端镜像变了」。
4. **脚本投递方式**：从 Windows 侧执行远端脚本时，`ssh host "sh -s"` 或 base64 都会破坏行尾
   （表现为 `NEW=` 空值、`$'\r': command not found`、`docker: invalid reference format`）。
   可靠做法是本地写出 LF 文件 → `scp` → `ssh "sed -i 's/\r$//' <file>; sh <file>"`。
   抽 Git 对象里的旧版文件也必须用 `cmd /c "git show <rev>:<path> > <file>"`，PowerShell
   管道重定向会改行尾并让 sha256 对不上。
5. 留给运维的清理项：`/opt/knowledge-kb-deploy/pr194-ab/`（A/B 脚本与报告）、
   `/opt/knowledge-kb-deploy/fix-claim-500*/`、`/opt/knowledge-kb-runtime/.codex-switch-2026101*/`
   （切换前后比对记录，建议保留至下次上线），以及 `/tmp` 下本轮的一次性脚本与日志。
   历史镜像（`app-query-latency-b2a734a`、`log-verdict-retention-20261010`）暂时保留，
   前者是本轮优化版、后者是回滚锚点。
