# 生产上线记录：知识沉淀高频问题看板（#211）+ 主题转写与候选复核修复（#210）

本文记录 2026-10-19 把 `master` 的 `26486fd`（PR #211「增加知识沉淀高频问题看板」）与
其父提交 `75df712`（PR #210「修复主题知识转写与候选复核评测闭环」）上线到
`https://knowledgekb.powerzhuan.cn` 的过程与取证。时间均为线上 `date -u` 回显的 UTC
（+08:00 另注）。

承接 `docs/production-deploy-20261016-blind-label-mine-pagination-serverside.md`（#208 上线记录）。
本次上线的起点是 `bc17a6f`（#208），即本次之前线上正在运行的版本。

## 1. 本次改动范围

| 提交 | 内容 | 是否影响线上容器 |
| --- | --- | --- |
| `75df712` #210 | 后端候选复核 / 模型标注 / 评测样本 / Prompt 修订隔离；前端候选复核；`prototypes/answer-hub` 转写与证据链 | 后端镜像 + 前端；Answer Hub 见第 8 节 |
| `26486fd` #211 | 新增「知识沉淀高频问题看板」：新数据源客户端、归类流水线、后台 worker、新路由与新前端页 | 后端镜像 + 前端 |

对比 `bc17a6f`：99 个文件、+23032 / −1202 行，其中 `backend` 46 个、`prototypes` 49 个、
`frontend/index.html` 1 个，另加 `.env.example`、`docker-compose.yml`、`docs` 各 1 个。

新引入的环境变量（`.env.example` 与 `docker-compose.yml` 已声明）：

```dotenv
SECOND_PART_API_URL=            # 第二部分只读数据源（看板的工单来源）
SECOND_PART_API_TOKEN=
SECOND_PART_API_TIMEOUT_SECONDS=30
SECOND_PART_API_QUERY_LIMIT=10000
HIGH_FREQUENCY_CLASSIFICATION_ENABLED=true
HIGH_FREQUENCY_WORKER_CONCURRENCY=4
```

## 2. 数据库迁移

本次含 4 个迁移，全部是**新增对象 / 加宽列**，对旧后端向后兼容，因此采用
「先迁移、后切镜像」，迁移期间旧后端继续服务，无需停机窗口：

| 迁移 | 内容 |
| --- | --- |
| `20261012_02_evaluation_samples` | 新增 `evaluation_samples` 等表（评测样本冻结） |
| `20261016_01_candidate_status` | `integration_ingestions.status` 由 32 加宽到 64 |
| `20261018_01_merge_review_heads` | 汇合盲标与候选两条迁移分支（空迁移，不改数据） |
| `20261013_01_hf_persist` | 新增 4 张 `high_frequency_*` 表（高频问题看板持久化） |

最终迁移头为 `20261013_01_hf_persist`（`20261018_01_merge_review_heads` 是汇合节点，
其子节点才是头）。注意：仓库中 `20261013_01_hf_persist` 的 `down_revision` 指向
`20261018_01_merge_review_heads`，因此该"13 号"迁移实际排在汇合节点之后，这是本次的预期结构。

## 3. 上线前只读基线（03:07:11Z / 11:07:11+08）

快照落盘 `/opt/knowledge-kb-runtime/.dsh-deploy-26486fd/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:mine-paging-serverside-bc17a6f`（`sha256:3d501d3333cc…`） |
| 容器启动时间 | 2026-10-16T09:38:51.746679508Z |
| 环境变量 md5（74 个键） | `582ccc90ebd2e3b556b88fda70862a1a` |
| 挂载集合 md5 | `b9c504303a5b0d6f11ce8525819a7288` |
| 运行规格 md5 | `7a2118458cfc4878af928de6d30ed853` |
| `alembic current` | `20261014_01_blind_pool_state` |
| 容器内 `/frontend/index.html` | sha256 `eec5d4c3…`（754,058 B） |
| 新功能标记命中数 | `high_frequency` 0、前端 `highFrequency` 0（确认 #211 不在线上） |
| 健康 | `/health` 200、`/ready` 200、线上 `/app` 200（754,058 B） |
| 近 6 小时错误 | Traceback 4、500 0（4 条为既有历史日志） |
| 数据库 | PostgreSQL 18.0，库大小 1793 MB；`high_frequency%` / `evaluation_samples%` 表 0 张；`integration_ingestions.status` 长度 32 |
| 磁盘 | `/dev/vda1` 180G，已用 134G，可用 47G（75%） |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:mine-paging-serverside-bc17a6f` |

`/opt/knowledge-kb/.env` 中本次新功能所需键全部缺失（`SECOND_PART_*`、`HIGH_FREQUENCY_*`、
`GROUP_LLM_*`），`DEEPSEEK_BASE_URL` / `DEEPSEEK_API_KEY` / `ANSWER_HUB_BASE_URL` /
`ANSWER_HUB_API_KEY` 已存在。

## 4. 构建（11:14:41+08 → 11:14:43+08）

```bash
git archive --format=tar.gz -o master-26486fd.tar.gz 26486fd   # 3,051,444 B
# sha256 0f6c668086657d6d70e25a796ab4a752ac572ce237899637dfe2d673aa858429
```

上传线上解包到 `/opt/knowledge-kb-deploy/26486fd`，用与仓库 `backend/Dockerfile` 等价的
Dockerfile 构建（pip 走腾讯镜像、`SAFE_BUILD_SLICE=qabuild.slice` 限流）：

镜像 `knowledge-kb-backend:hf-dashboard-26486fd`（`3406caa6f6a3`，496MB），镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | 与源码 `184051a05225911e8b2e224369df127b` 相同 | ✅ 相同 |
| `/frontend/index.html` 大小 | 792,768 B | ✅ 相同 |
| `app/main.py` 中 `high_frequency` 引用 | ≥1 | ✅ 6 |
| `services/high_frequency_pipeline.py` 存在 | 1 | ✅ 1 |
| `services/second_part_client.py` 含 `SECOND_PART_API_TOKEN` | ≥1 | ✅ 5 |
| 迁移文件数 / 头 | 44 / `['20261013_01_hf_persist']` | ✅ 一致 |
| `cmd` / `entrypoint` / `workdir` | 与线上一致 | ✅ `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]` / `null` / `/app` |

## 5. 迁移前备份与迁移（11:15:33Z+08 → 11:16Z+08）

备份目录 `/opt/knowledge-kb-runtime/backups/hf-dashboard-20261019/`：

| 文件 | 说明 |
| --- | --- |
| `knowledge_base.before.dump`（255M） | 全库 `pg_dump -Fc` 自定义格式备份 |
| `migration_scope.before.sql`（242M） | 迁移相关表（含 `alembic_version`）的 SQL 备份 |
| `image.before.txt` / `env.before.md5` / `mounts.before.md5` / `spec.before.md5` | 现场参数指纹 |
| `alembic.before.txt` / `alembic.after.txt` | 迁移前后版本 |

迁移前行数：`integration_ingestions` 19341、`knowledge_items` 19543、
`blind_label_work_orders` 5941、`blind_label_assignments` 7442。

以**新镜像**通过 `knowledge-kb_default` 网络与 `.env` 连接生产库执行
`python -m alembic upgrade head`。

迁移后校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| 5 张新表 | 全部存在 | ✅ `high_frequency_source_records` / `_atomic_questions` / `_standard_questions` / `_processing_tasks` / `evaluation_samples` |
| `alembic_version` | `20261013_01_hf_persist` | ✅ |
| `integration_ingestions.status` 长度 | 64 | ✅ 64 |
| 行数 | 不变 | ✅ `integration_ingestions` 19341、`knowledge_items` 19543 |
| 旧后端 | 仍健康 | ✅ `kb-backend` Up、`/health` 200 |

## 6. 切换（03:16:51Z / 11:16:51+08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh \
  knowledge-kb-backend:hf-dashboard-26486fd
# 新容器 78bbe83c4acca7ff99df5f6bc30c279413c8475f3596a0be154b111fdcb5ed09
```

切换前后逐项比对，**只有镜像、容器 id、启动时间变化**：

| 项 | 切换前 | 切换后 |
| --- | --- | --- |
| 环境变量 md5（74 键） | `582ccc90ebd2e3b556b88fda70862a1a` | ✅ 相同 |
| 挂载集合 md5 | `b9c504303a5b0d6f11ce8525819a7288` | ✅ 相同 |
| 运行规格 md5 | `7a2118458cfc4878af928de6d30ed853` | ✅ 相同 |
| 容器内 `/frontend/index.html` | — | sha256 `6394ffea…`（792,768 B） |
| `main.py` `high_frequency` 引用 / 迁移文件 | 0 / 无 | ✅ 6 / `20261018_01` 存在 |
| `alembic current` | — | ✅ `20261013_01_hf_persist (head)` |

## 7. 上线后验收

### 7.1 功能与探活

| 项 | 结果 |
| --- | --- |
| 线上 `/app` | 200（792,768 B，md5 `184051a05225911e8b2e224369df127b`，与源码一致） |
| `/health` / `/ready` | 200 / 200 |
| 新页面标记（容器内与线上直取一致） | `highFrequency` 45、`hf-rank-table` 2 |
| 新路由注册 | `/api/v1/high-frequency` 4 条、`/api/v1/confidence-training` 16 条 |
| 无 key 调 `/api/v1/high-frequency` | 401（鉴权正常） |

### 7.2 新看板对生产库的只读端到端调用

在容器内以只读事务直接调用 `build_persisted_dashboard`（空表状态）：

| 项 | 结果 |
| --- | --- |
| 返回键 | `business_theme_options` / `categories` / `level1_options` / `level2_options` / `meta` / `pending_cases` / `rows` / `summary` / `trend` |
| `meta` | `worker_state=completed`、`classification_pending=false`、`possibly_truncated=false` |
| 空表统计 | 各计数为 0，`rows` 0，无异常 |
| 数据源未配置时的降级 | 返回 503 与中文提示「第二部分接口地址尚未配置，请设置 SECOND_PART_API_URL 后再查询。」，未抛未捕获异常 |
| `#210` 路由导入 | `confidence_training` / `integration` / `evaluation_samples` 导入正常 |
| 后台 worker | `HIGH_FREQUENCY_WORKER_CONCURRENCY=4`、`HIGH_FREQUENCY_CLASSIFICATION_ENABLED=True`，4 个空闲 worker 已启动 |

### 7.3 运行期观察（03:17Z 起，约 11 分钟）

- `kb-backend` Up，镜像 `hf-dashboard-26486fd`，`RestartCount=0`、`OOMKilled=false`；
- Traceback **0**、`500` **0**、`502`/`503` **0**；
- 状态码统计：200 × 676、401 × 2（401 为无 key 探活，符合预期）；
- 业务流量正常：`POST /api/v1/integration/standard-search 200`；
- 内存 482 MiB / 15.12 GiB，CPU 0.48%；
- 其余容器（`kb-redis`、`kb-embedding-qwen`、`appeal-exemption-*`、`voc-workbench-*`）状态未受影响；
- 磁盘仍为已用 134G / 可用 47G。

## 8. 本次未部署的部分：Answer Hub（重要）

`75df712`（#210）同时修改了 `prototypes/answer-hub`。经只读核验：

- 生产机上 Answer Hub 是**独立服务**：systemd `answer-hub-api.service`（`/opt/knowledge-kb/prototypes/answer-hub`
  下的独立 venv，监听 `0.0.0.0:8780`，当前 `active`），另有 `answer-hub-queue.timer`（当前 `disabled`）；
- 本次只是替换了 **kb-backend 镜像**，**没有**更新 `/opt/knowledge-kb` 里 Answer Hub 的源码，
  也**没有**重启 `answer-hub-api.service`；
- 逐文件 blob 比对（以 `26486fd` 为基线）：Answer Hub 的 `workflow.py`、`ai_result.py`、`audit.py`、
  `automation_api.py`、`automation_queue.py`、`cz_integration.py`、`draft_quality.py`、`knowledge_value.py`、
  `web.py` 等仍是 `bc17a6f` 时代的内容，**不含** #210 新增的 `question_standardization.py`、
  `answer_evidence.py`、`draft_prompt_replay.py`，且 `/api/v1/draft-prompt-replays` 在生产
  Answer Hub 上不存在（`grep` 计数 0）；
- 生产 Answer Hub 另有 `automation_control.py`、`automation_schedule.py`、`cli.py`、`mimo.py`、
  `second_part_pull.py` 属于**线上独有 / 领先**版本（blob 不在任何仓库 ref 中），说明该服务的源码
  在服务器上另行维护，**不能用仓库文件直接覆盖**。

因此本次部署范围明确限定为 **知识库（kb-backend + 前端 + 数据库迁移）**。`#210` 中
「置信度训练 → 草稿 Prompt 回放」这条路径需要 Answer Hub 提供 `/api/v1/draft-prompt-replays`，
在 Answer Hub 另行发布前**不可用**（调用会得到 `ANSWER_HUB_DRAFT_REPLAY_UNAVAILABLE` 并提示
Answer Hub 草稿 Prompt 回放不可用，属可预期降级，不会 5xx）。Answer Hub 的发布需按
`prototypes/answer-hub/docs/answer-hub-本机迁移运行手册.md` 单独排期与执行。

## 9. 已知缺口：看板数据源凭据未配置

新的高频问题看板依赖 `SECOND_PART_API_URL` / `SECOND_PART_API_TOKEN`，生产 `.env` 中尚未配置，
因此：

- 「同步数据」「检查数据源」会返回 503 与中文提示，不会报 5xx；
- 看板可正常打开，但当前筛选范围内无数据；
- 服务器上 Answer Hub 已有可用的第二部分凭据（其 `.env` 中 `SECOND_PART_API_TOKEN` 长度 43，
  数据源同为 `https://qa.powerzhuan.cn/api/records`），可由运维按需复制到
  `/opt/knowledge-kb/.env` 后重启 `kb-backend`（取值不写入仓库、日志或聊天记录）。

`HIGH_FREQUENCY_CLASSIFICATION_ENABLED` 默认 `true`，复用已有 `DEEPSEEK_BASE_URL` /
`DEEPSEEK_API_KEY`（或「模型连接配置」页保存的连接配置）做多意图归类；未配置时自动降级为
纯原子问题统计，不影响页面可用性。

## 10. 回滚

```bash
# 回到 #208 版本（含数据兼容，可立即回滚）
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:mine-paging-serverside-bc17a6f
# 再往前一版（#206）
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:my-annotations-pagination-ff6f528
```

本次迁移**均为新增表 / 加宽列**，旧镜像看不到新表也能正常运行，因此回滚只需换镜像，**无需回滚数据库**。
如需回滚数据库（非必要），可用
`/opt/knowledge-kb-runtime/backups/hf-dashboard-20261019/knowledge_base.before.dump` 恢复。
已写入的高频问题数据与评测样本在回滚后仍保留在库中，不会被旧镜像破坏。

## 11. 验证方式与边界

- 采用与仓库既有上线流程一致的做法：`git archive` 干净导出 → 线上等价 Dockerfile 构建 →
  镜像内指纹比对 → 以新镜像跑迁移 → 主机规范脚本 `run-kb-backend.sh` 切换 → 只读端到端调用验真；
- 本次未在生产机跑完整后端测试套件；上线判据是**镜像内指纹一致 + 迁移结构校验 + 线上只读端到端调用 + 运行期错误扫描**；
- `/opt/knowledge-kb/frontend/index.html`（宿主机副本）本就与容器内文件不同步，本次未改动宿主机副本；
- 生产机 `/opt/knowledge-kb` 工作区长期存在未提交改动（本次核验为 58 个 answer-hub 文件等），
  属既有的「server-preserve」状态，本次**未做任何改动**，也未执行任何 `reset` / `clean`。
