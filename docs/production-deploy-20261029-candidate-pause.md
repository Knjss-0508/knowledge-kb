# 生产上线记录：候选自动标注服务故障保护与任务面板折叠（#254，2026-10-29）

本文记录 2026-10-29 把 `master` 的 `10ed87b`（PR #254）上线到
`https://knowledgekb.powerzhuan.cn` 的过程与取证。时间为服务器 `date` 回显的 CST。

上线起点是 `candidate-single-a033900`（源码 `a033900`）。

## 1. 改动范围

### 1.1 服务故障保护

- 新增 `SERVICE_ERROR_CODES`（`NETWORK_ERROR` / `TIMEOUT` / `HTTP_429`）与
  `PERMANENT_SERVICE_ERRORS`（`CONFIG_MISSING` / `HTTP_401` / `HTTP_403`）；
- `_record_service_outcome()` 统计连续服务故障：达到 2 次或遇到永久错误即暂停，
  成功处理后清零，`skipped` 不计入；
- 暂停后 `schedule_automatic_candidates()` 不再派发，手动创建任务返回 409；
- 新增 `POST /candidate-model-automation:resume` 恢复处理；
- 旧版批量模型标注接口也套用同一执行锁（`candidate_model_execution_slot`）。

### 1.2 任务面板折叠

- 列表接口支持 `task_page=0`（不返回分页数据）、`task_filter=all|failures`、
  `include_results`；
- 返回 `service_paused` / `pause_reason` / `paused_at` / `active_task` /
  `failed_task_total` / `history_total`；
- 前端把自动处理任务折叠为面板，突出「正在处理」与「失败」两类。

### 1.3 数据库

迁移 `20261028_01_candidate_pause`，父版本 `20261029_01_candidate_jsonb`
（即本机上一轮上线的 jsonb 迁移），`candidate_model_automation` 增加三列：
`consecutive_network_failures`、`paused_at`、`pause_reason`。

部署前已核对迁移链为单头，仓库的 `test_history_has_a_single_head` 通过。

## 2. 上线前基线（19:20 +08）

快照落盘 `/opt/knowledge-kb-runtime/backups/pr254-20261029/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:candidate-single-a033900` |
| `alembic current` | `20261029_01_candidate_jsonb (head)` |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578` |
| 新增列 | 不存在（`paused_cols=0`） |
| 自动处理开关 | `enabled=false`，未暂停 |
| 数据库备份 | `kb-before-pr254.dump`（532,657,266 B） |

## 3. 构建

```bash
git archive --format=tar.gz -o master-10ed87b.tar.gz 10ed87b
# 3,280,707 B，sha256 916e7b91ef470d6488e70c6995d04fff5912dcc057420a8627f1e827c6a53320
```

镜像 `knowledge-kb-backend:candidate-pause-10ed87b`（`sha256:c28be668…`）。镜像内校验：

| 校验项 | 结果 |
| --- | --- |
| 迁移文件存在 | ✅ |
| `consecutive_network_failures` 逻辑 | ✅ |
| `candidate-model-automation:resume` 路由 | ✅ |
| 前端含 `candidate-model-panel` | ✅ |
| `alembic heads` | `['20261028_01_candidate_pause']` ✅ |

## 4. 迁移与切换

```text
19:25:09 停止旧后端
19:25:11 执行迁移（仅加三列，2 秒）
19:25:13 Database schema is ready: revision=20261028_01_candidate_pause
19:25:37 启动新后端
```

维护窗口约 28 秒。

切换前后比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `89cb3c17…` | `89cb3c17…` | ✅ |
| 挂载 | uploads / data / nlogs | 同左 | ✅ |
| `alembic current` | `20261029_01_candidate_jsonb` | `20261028_01_candidate_pause` | ✅ |
| 新增三列 | 无 | 全部存在 | ✅ |
| 自动处理状态 | 关闭、未暂停 | `enabled=false, paused=None, failures=0` | ✅ |

## 5. 上线后验收

| 检查 | 结果 |
| --- | --- |
| `/health`（连续 3 次） | 200，1.6–2.3 ms |
| `/ready` | 200，50 ms |
| 公网 `/app` | 200，0.29–0.33 s |
| 页面文件名 sha256 | 与容器内 `/frontend/index.html` 一致（`21ddcec3…`） |
| 无鉴权 `GET …candidate-model-automation?task_page=0` | 401 |
| 无鉴权 `POST …candidate-model-automation:resume` | 401 |
| 前端折叠面板标记 | 命中 2 处 `candidate-model-panel` |
| 日志 `Traceback` / `QueuePool` | 0 |
| `kb-redis`、`kb-embedding-qwen` | 未重建（`Up 5 hours`） |

暂停保护逻辑实测（容器内直接调用 `_record_service_outcome`）：

| 场景 | 结果 |
| --- | --- |
| 第 1 次 `NETWORK_ERROR` | 计数 1，未暂停 |
| 第 2 次 `NETWORK_ERROR` | 计数 2，**暂停**，原因写入 `pause_reason` |
| 单次 `HTTP_401` | **立即暂停**（永久错误） |
| 成功处理后 | 计数清零 |
| `skipped` | 不改变计数 |

## 6. 回滚

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:candidate-single-a033900
```

新增列是 `ADD COLUMN`（带默认值），旧镜像不使用它们，回滚镜像无需降级数据库。
如需彻底回退表结构：`alembic downgrade 20261029_01_candidate_jsonb`，
或使用 `kb-before-pr254.dump`。

## 7. 注意事项

- **自动处理开关仍为关闭**。本次只上线保护逻辑，未开启自动处理；
  开启后一旦连续 2 次网络故障会自动暂停并给出原因，需要人工点「恢复处理」。
- 迁移顺带修好了上一轮部署留下的隐患：`20261028_01_candidate_pause` 的
  `down_revision` 正确指向本机先上线的 `20261029_01_candidate_jsonb`，
  没有出现分叉。
- 前端「恢复处理」按钮文案来自 `pause_reason` 动态渲染，静态 HTML 中只包含
  处理函数与面板骨架，属预期。
