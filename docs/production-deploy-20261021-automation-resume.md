# 生产上线记录：运行监管「队列未清干净只算跳过」与恢复自动顺延入口（#217，2026-10-21）

本文记录 2026-10-21 把 `master` 的 `229cd2a`（PR #217「队列未清干净只算『跳过』，
并补上恢复自动顺延入口」）上线到 `https://knowledgekb.powerzhuan.cn` 的过程与取证。
时间均为线上 `date -u` 回显的 UTC（+08:00 另注）。

## 1. 问题与修复

页面长期挂着「最近失败：队列存在未解决的 failed 文件。；失败时日期游标不会移动。」，
且夜间调度自 2026-10-11 起一条数据都没拉。根因是两个缺陷叠加成的死锁：

1. **把「跳过」记成了「失败」**：定时脚本发现 `pending`/`processing`/`failed` 桶里还有
   未清理的文件时走的是 `automation_schedule failure`。这不是失败，是设计好的
   「本轮跳过、本次不拉取」。
2. **一旦暂停就再也起不来**：连续三晚走 `failure` 后 `consecutive_failure_count` 达到 3，
   `auto_advance_paused` 被置为 `true`。此后 `prepare_window()` 每次都直接抛错退出，
   脚本连队列检查都走不到，游标永远停在原地；`last_failure_reason` 也再没人清得掉。

`229cd2a` 的修复（无数据库迁移、无接口契约破坏性变更）：

| 文件 | 改动 |
| --- | --- |
| `prototypes/answer-hub/src/answer_hub/automation_schedule.py` | 新增 `record_deferral()`（跳过只写 `last_deferral_*`，不动失败计数、不暂停顺延）、`resume_plan()`（只清记录与暂停标记，`cursor_date` 不动）；CLI `action` 扩展为 `prepare/commit/failure/defer/resume` |
| `prototypes/answer-hub/scripts/run_scheduled_queue.sh` | 队列阻塞分支改调 `defer`；真失败（拉取连续 3 次失败）仍走 `failure` |
| `prototypes/answer-hub/scripts/local-migration/nightly-scheduler.ps1` | 同上（保留 UTF-8 BOM） |
| `prototypes/answer-hub/src/answer_hub/automation_api.py` | 新增 `POST /api/v1/automation/resume`；`control.plan` 暴露 `last_deferral_date`/`last_deferral_reason` |
| `backend/app/routes/automation_monitor.py` | 新增 `POST /automation-monitor/resume`（`account:manage`） |
| `frontend/index.html` | 暂停时显示「清除失败记录并恢复顺延」；「本次未执行（主动跳过）」用中性灰蓝单独渲染 |
| 测试 | `automation_schedule` 4 个、`automation_api_schedule_control` 3 个、`scheduled_queue_script` 1 个、`automation_monitor_frontend` 3 个 |

## 2. 拓扑取证（重要）

「自动化流程监管」页读的是**本机运行的 Answer Hub**，不是服务器本地那份：

```text
浏览器 → knowledgekb.powerzhuan.cn → 生产 kb-backend
        → ANSWER_HUB_API_BASE_URL=http://172.18.0.1:18780
        → 生产 sshd 反向隧道(0.0.0.0:18780) → 本机 100.72.97.89:8780（Answer Hub）
```

因此：

- 生产页与本机页展示的是**同一份** `E:\answer-hub-runtime\data\automation-plan.json`；
- 真正把它锁死的是**本机**夜间调度器（计划任务 `AnswerHub-Pull-Scheduler` →
  `E:\answer-hub-runtime\_scheduler.ps1`），不是仓库里的脚本副本；
- 服务器本地 `/opt/knowledge-kb/prototypes/answer-hub/data/automation-plan.json`
  （`server-preserve-20260914` 工作树）**不是**监管页数据源，本次未改动；
  其 `answer-hub-queue.timer` 为 `disabled`，不会自行运行。

## 3. 本机运行时修复（非仓库文件）

| 对象 | 处理 | 备份 |
| --- | --- | --- |
| `E:\answer-hub-runtime\_scheduler.ps1` | 队列阻塞分支 `failure` → `defer`（根治点；保留 BOM，语法校验通过，`-DryRun` 冒烟通过） | `_scheduler.ps1.bak-defer-20261021-123546` |
| `E:\answer-hub-runtime\data\automation-plan.json` | 执行 `resume`：清失败记录与暂停标记，**游标保持 `2026-10-08` 不动** | `automation-plan.json.bak-resume-20261021-121213` |
| 本机 `kb-backend` 容器 | 切到 `knowledge-kb-backend:local-automation-resume-229cd2a`（保持原网络/端口/挂载/`--env-file` 规格） | 旧镜像仍在本地 |
| `.dsh-local/pr-api.ps1`（被 `.gitignore` 忽略的本机工具） | 修 `"#$Number:"` 变量插值语法并补 UTF-8 BOM，Windows PowerShell 5.1 实测可用 | `pr-api.ps1.bak-20261021` |

修复后计划状态：`cursor_date=2026-10-08`、`auto_advance_paused=false`、
`consecutive_failure_count=0`、`last_failure_reason=""`（本机页与生产页的告警条同时消失）。

## 4. 上线前只读基线（12:41 前，+08）

快照落盘 `/opt/knowledge-kb-runtime/backups/automation-resume-20261021/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:hf-sync-fix-3c078f6`（`sha256:ec6ec02028c6…`） |
| 容器启动时间 | 2026-10-19T06:29:42.994190874Z |
| 环境变量 md5（78 个键） | `89cb3c17c7a12154c6aa1fa6c38b6578` |
| 运行规格 md5（cmd/entrypoint/workdir/ports/network） | `7a2118458cfc4878af928de6d30ed853` |
| 挂载集合 md5 | `60a40c7bd1ab5b758767f21168de2cda` |
| `alembic current` | `20261013_01_hf_persist (head)` |
| 容器内 `/frontend/index.html` sha256 | `6394ffeaec3cdc69…` |
| 容器内 `routes/automation_monitor.py` sha256 | `d9cc831bc669cc01…` |
| 容器内 `services/automation_monitor.py` sha256 | `fd6254b70a867474…` |
| 健康 | `/health` 200 |
| 容器内 `def resume` 命中数 | **0**（确认新接口不在线上） |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:hf-sync-fix-3c078f6` |

## 5. 构建（12:41:40+08 完成）

```bash
git archive --format=tar.gz -o master-229cd2a.tar.gz 229cd2a   # 3,181,705 B
# sha256 a532edd4c12692cb7df44afffc3aa5a251481d7bc08abc49ac72d563fc542c6a
```

上传解包到 `/opt/knowledge-kb-deploy/229cd2a`，用与仓库 `backend/Dockerfile` 等价的
Dockerfile 构建（pip 走腾讯镜像、`SAFE_BUILD_SLICE=qabuild.slice` 限流）。

镜像 `knowledge-kb-backend:automation-resume-229cd2a`（`77728137b03a`，496MB），镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | `035a708b9b11a9cdd70cc3f8e1d8c2da` | ✅ 相同 |
| `/app/app/routes/automation_monitor.py` `def resume` | ≥1 | ✅ 1 |
| alembic heads | `['20261013_01_hf_persist']` | ✅ 相同 |

## 6. 切换（12:41:59.665967003Z / 12:41:59+08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:automation-resume-229cd2a
# 新容器 039bfd2b700a02b6d70bae2d1ec9b1cc758621d3e1e2248518fae8e4d643c201
```

**本次无数据库迁移**，`alembic current` 保持 `20261013_01_hf_persist (head)`。

切换前后逐项比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578` | `89cb3c17c7a12154c6aa1fa6c38b6578` | ✅ 一致 |
| 运行规格 md5 | `7a2118458cfc4878af928de6d30ed853` | `7a2118458cfc4878af928de6d30ed853` | ✅ 一致 |
| 挂载集合 md5 | `60a40c7bd1ab5b758767f21168de2cda` | `b9c504303a5b0d6f11ce8525819a7288` | ⚠️ inspect 输出顺序不稳定（已知）；逐项确认仍是同样的 3 个挂载：`uploads`、`manhattan_cache:/app/data`、`/www/wwwlogs:/app/nlogs:ro` |
| `services/automation_monitor.py` sha256 | `fd6254b70a867474…` | `fd6254b70a867474…` | ✅ 未变（证明只改了本次涉及的文件） |
| `/frontend` sha256 | `6394ffea…` | `a5f6dd8a…` | ✅ 已更新 |
| `routes/automation_monitor.py` sha256 | `d9cc831b…` | `a13b4f27…` | ✅ 已更新 |
| 容器内 `def resume` | 0 | 1 | ✅ 新接口生效 |

## 7. 上线后端到端验收

| 检查 | 结果 |
| --- | --- |
| 本机 `GET /health` / `/ready` / `/app` | 200 / 200 / 200 |
| 无 Key `GET /api/v1/automation-monitor/overview` | **401**（鉴权生效） |
| 无 Key `POST /api/v1/automation-monitor/resume` | **401**（鉴权生效） |
| 线上 `GET /app` | 200，794,479 B |
| 线上 `/app`（CR 归一）md5 | `035a708b9b11a9cdd70cc3f8e1d8c2da` = 本地 `master` `229cd2a` 的同一文件 ✅ |
| 线上页面含新提示/按钮标记 | ✅（`automation-plan-note`、`resumeAutomationMonitor`） |
| 启动日志 | 无 Traceback / Exception / Critical |
| 其他容器 | `kb-embedding-qwen`、`kb-redis` 等未被重建（`Up 8 days`） |

## 8. 已知风险与后续

- 切换过程中存在**约数秒的 502 窗口**（重建容器所致，属既有发布流程的固有行为）。
- 生产服务器本地那份 `automation-plan.json` 仍保留历史 `last_failure_reason`
  （`consecutive_failure_count=2`，未暂停）。它**不是**监管页数据源，本次未改动；
  如后续要清理，应连同其 `answer-hub-queue.timer` 的定位一并评估。
- 队列里堆积的失败文件仍需人工处理（页面「重试失败任务」或
  `run_automation_queue.ps1 -RetryFailed`）；`defer` 只记录跳过原因，不代替清理。
- 本机 `_scheduler.ps1` 是**非仓库副本**，已单独修正；若日后重新同步仓库脚本，
  需确认队列阻塞分支仍为 `defer`。
