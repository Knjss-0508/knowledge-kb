# 生产上线记录：候选品类归属、自动模型标注送审与大批量后台任务（#239、#240，2026-10-27）

本文记录 2026-10-27 把 `master` 的 `82f48c6`（PR #239 合并提交 `ead76bd` + 回归修复 PR #240）
上线到 `https://knowledgekb.powerzhuan.cn` 的过程与取证。时间均为服务器 `date` 回显的 CST（UTC+8）。

本次上线起点是 `aggregate-category-aa9ae61d`（源码 `e882122`），即此前线上运行的版本。

## 1. 改动范围

### 1.1 PR #239（`ead76bd`）

- **品类归属**：十二个上门回收品类之外、有明确产品证据的候选统一归入聚合回收；曼哈顿缓存
  不再用上游 `bizType` 划分自营/聚合，改为按品类白名单归属。
- **持久化后台任务**：候选价值复核的批量模型标注改为后台任务，创建请求返回 HTTP 202，
  支持 20/50/100 条逐条处理；新增默认关闭的自动扫描开关、进度、逐条结果、失败重试与历史分页。
- **数据库**：新增迁移 `20261027_01_candidate_tasks`，父版本 `20261013_01_hf_persist`，
  新增 `candidate_model_tasks`、`candidate_model_automation` 两张表（自动开关默认关闭）。
- **Answer Hub**：来源预处理与业务层级映射同步上述品类规则。

### 1.2 PR #240（`82f48c6`，本次代码审视后补的回归修复）

PR #239 的 `candidate_business_type()` 在候选既没有适用品类、也没有
`answer_hub_replay_input` 产品证据时，会把历史候选从 `self_operated` 改判为 `aggregated`。
线上曼哈顿缓存只有 12 个自营品类，`aggregated` 分组的适用品类为空，这类候选送审后
无法匹配任何适用范围，等于检索不可用，同时也会改变查重分组。

修复为最小改动：没有明确产品证据时沿用历史默认值 `self_operated`；十二品类之外的
明确产品仍归入 `aggregated`。

| 文件 | 改动 |
| --- | --- |
| `backend/app/services/candidate_business.py` | 无产品证据时返回 `knowledge.get("business_type") or "self_operated"` |
| `backend/tests/test_candidate_business_rule.py` | 新增 `test_no_product_evidence_keeps_legacy_self_operated_default` |

## 2. 上线前只读基线（18:50 +08）

快照落盘 `/opt/knowledge-kb-runtime/backups/pr239-deploy-20261027/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:aggregate-category-aa9ae61d`（`sha256:1a586d13…`） |
| 容器启动时间 | 2026-10-27T08:48:53Z |
| 环境变量 md5（84 个键） | `89cb3c17c7a12154c6aa1fa6c38b6578` |
| 运行规格 md5 | `57386af22df887126935025b39ec3ff8` |
| 挂载 | `uploads`、`knowledge-kb_manhattan_cache:/app/data`、`/www/wwwlogs:/app/nlogs:ro` |
| `alembic current` | `20261013_01_hf_persist (head)` |
| 容器内 `/frontend/index.html` sha256 | `62e3da315cdf070c96edd9f9d952db6114d88f23421749865f328475a7dfcfa3`（812,774 B） |
| 数据库全量备份 | `knowledge_base-before-pr239.dump`（451,164,143 B，`pg_dump -Fc`，PostgreSQL 18） |
| 其他容器 | `kb-redis`、`kb-embedding-qwen` 均 `Up 2 weeks`，本次未重建 |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:aggregate-category-aa9ae61d` |

数据库备份在生产服务器本机执行：远端 Tailscale 链路导出 2.7 GB 库时只有约 5 KB/s，
全量备份不可行；服务器本机 39 秒完成。

## 3. 构建

```bash
git archive --format=tar.gz -o master-82f48c6.tar.gz 82f48c6
# 3,249,681 B，sha256 d430f8b3fb9e4bfe1a4da0308f68c81671d3daeb0f1fde83d74b2b520d05ed5a
```

上传解包至 `/opt/knowledge-kb-deploy/82f48c6`，用与仓库 `backend/Dockerfile` 等价的
Dockerfile 构建。构建必须使用 `--network host`：桥接网络内 DNS 解析失败，pip 无法访问
腾讯镜像（与既往 `231-build-pagination.sh` 等脚本一致）。

镜像 `knowledge-kb-backend:candidate-auto-82f48c6`（`sha256:17f268e3…`，119,690,302 B）。
镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `candidate_business.py` 无证据默认值 | `self_operated` | ✅ |
| `services/candidate_model_tasks.py` | 存在 | ✅ |
| `migrations/versions/20261027_01_candidate_tasks.py` | 存在 | ✅ |
| 前端含 `candidate-model-panel` | 是 | ✅ |
| 前端残留旧的 `candidate-reviews:batch-model-annotate` | 否 | ✅ 已移除 |
| `alembic heads` | `['20261027_01_candidate_tasks']` | ✅ |

## 4. 迁移与切换

本次含结构迁移，按项目规范采用「先构建、再短维护窗口迁移、立即启动」：

```bash
# 18:59:12 停止旧后端
docker stop -t 30 kb-backend

# 18:59:18 执行迁移
docker run --rm --name kb-migrate-pr239 --network knowledge-kb_default \
  --env-file /opt/knowledge-kb/.env \
  knowledge-kb-backend:candidate-auto-82f48c6 \
  python -m app.scripts.initialize_database
# Database schema is ready: revision=20261027_01_candidate_tasks, vector=0.8.6, dimensions=1024
# Active administrator already exists; bootstrap skipped.

# 18:59:30 以线上既有参数启动新后端
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:candidate-auto-82f48c6
```

切换前后逐项比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578` | 同左 | ✅ |
| 运行规格 md5 | `57386af22df887126935025b39ec3ff8` | 同左 | ✅ |
| 挂载集合 | uploads / manhattan_cache / nlogs(ro) | 同左 | ✅ |
| `alembic current` | `20261013_01_hf_persist` | `20261027_01_candidate_tasks (head)` | ✅ |
| 新增表 | 无 | `candidate_model_tasks`、`candidate_model_automation` | ✅ |
| 自动开关默认值 | — | `enabled=false` | ✅ 默认关闭 |
| 初始任务数 | — | `0` | ✅ 未自动触发 |

## 5. 上线后端到端验收

| 检查 | 结果 |
| --- | --- |
| `GET /health`（容器内） | 200 |
| `GET /ready`（容器内） | 200 |
| 线上 `GET /app` | 200，819,825 B |
| 线上页面 sha256 | `ce30d3a697c47610796693bdddc8142e866527ec6ef58212c5b78dab6cc6fc1b` = 容器内 `/frontend/index.html` ✅ |
| 无鉴权 `GET /api/v1/integration/candidate-model-automation` | 401 |
| 无鉴权 `POST /api/v1/integration/candidate-model-tasks` | 401 |
| 启动日志 `Traceback` / `Exception` / `Critical` | 0 |
| worker 异常日志 | 0 |
| `kb-backend` 内存占用 | 约 473 MiB / 15.1 GiB |
| `kb-redis`、`kb-embedding-qwen` 启动时间 | 未变化（`Up 2 weeks`） |

业务类型归判（容器内调用真实代码）：

| 输入 | 修复前 | 上线后 |
| --- | --- | --- |
| 仅 `category_id`，无产品证据 | `aggregated` | `self_operated` ✅ |
| 空 knowledge | `aggregated` | `self_operated` ✅ |
| `applicable_categories=["手机"]` | `self_operated` | `self_operated` ✅ |
| `applicable_categories=["无人机"]` | `aggregated` | `aggregated` ✅ |
| `business_type=aggregated` + 手机 | `aggregated` | `aggregated` ✅ |

## 6. Answer Hub

本机运行时 `E:\answer-hub-runtime\_scheduler.ps1` 的 `$AH` 直接指向仓库
`prototypes/answer-hub`，`venv` 的 `answer_hub` 也解析到该目录，因此仓库切到
`82f48c6` 后，分类改动已对运行时生效，无需二次部署。

- `pytest prototypes/answer-hub/tests/test_business_taxonomy.py test_product_taxonomy.py`：23 passed。
- 未改动 `E:\answer-hub-runtime` 下的调度脚本与计划任务配置。

## 7. 已知风险与后续

- 迁移窗口内后端停止约 33 秒（18:59:12–18:59:45），期间线上页面与 API 返回 502。
- 自动模型标注开关默认关闭，本次未开启；开启后会对存量待处理候选持续创建任务，需评估模型
  网关与 DeepSeek 配额后再决定。
- 生产 PostgreSQL 多进程 advisory lock、真实 DeepSeek 调用与网关超时未在本次验收中压测；
  首次开启自动处理时建议先小批量手工任务验证。
- 远端 Tailscale 链路带宽低（导出约 5 KB/s），后续备份应在生产服务器本机执行。
- 生产仓库 `/opt/knowledge-kb` 仍工作在 `server-preserve-20260914` 且有未提交改动，本次
  未触碰；发布使用独立的 `git archive` 导出目录与镜像标签。
