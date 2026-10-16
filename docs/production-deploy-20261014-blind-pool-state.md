# 生产上线与回滚记录：盲标池会话号工单退池（#199）上线（2026-10-14）

本文记录 2026-10-14 把 `master` 的 `e3b361f`（#199「盲标池会话号工单」建单 / 派发 /
退池三段修复 + #200 文档）上线到 `https://knowledgekb.powerzhuan.cn` 的完整过程。
所有时间均为 UTC（容器与宿主 `docker` 回显为 UTC，`docker images` 的 `CreatedAt` 为
+08:00，正文中如需对比会同时标注）。

线上现状（截至本次切换）：

| 项 | 值 |
| --- | --- |
| 线上镜像 | `knowledge-kb-backend:pool-state-e3b361f` |
| 镜像 ID | `sha256:33bfc8bc614442b1bd1ef122644d76ca551f21b3139cc9156d84696f36f5b9ea` |
| 镜像构建时间 | 2026-10-14 22:43:33 +0800（= 14:43:33Z），496MB |
| 源码提交 | `master` 的 `e3b361f`（= #198 + #199 + #200） |
| 容器启动时间 | 2026-10-14T14:48:48.656323455Z |
| 回滚锚点（上一版） | `knowledge-kb-backend:blind-label-fix-876da12`（`sha256:567d2829671a…`） |
| 回滚命令 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:blind-label-fix-876da12` |
| 更早的锚点 | `knowledge-kb-backend:log-verdict-retention-20261010`（`sha256:151986fde427…`） |
| 数据库迁移 | `20261012_01_blind_label_target` → `20261014_01_blind_pool_state` |

## 1. 上线内容

待部署区间 `876da12..e3b361f`：

| PR | 提交 | 内容 |
| --- | --- | --- |
| #198 | `f2981f8` | 记录 2026-10-14 `/app` 首屏与慢接口优化的上线/回滚过程（docs） |
| #199 | `99fe345` | 盲标池会话号工单三段修复：建单门禁按访问日志实时判定、派发前复核、worker 每轮自动退池（含新迁移、运维脚本、3 个测试、2 篇 docs） |
| #200 | `e3b361f` | 记录答疑工作台聊天记录发送者姓名解析口径（docs） |

`876da12..e3b361f` 共 13 个文件、+1421 / −5。本次实际进入镜像的业务代码：

- `backend/app/services/blind_labeling.py`（+231）：`session_number_snapshot()` TTL 缓存快照、
  `_session_retire_candidates()`、`retire_session_work_orders()`；
- `backend/app/services/work_order_log_verdicts.py`（+149）：`verdict_map()`、`_tail_bytes()`、
  `recent_signals()`（只读日志尾部）、`recent_verdict_for()`、`_retire_limit()`、
  `retire_session_work_orders_task()`（并入判定 worker 循环，`await asyncio.to_thread(...)`）；
- `backend/app/models/blind_labeling.py`（+19）：`pool_state` / `retired_at` / `retire_reason`；
- `backend/app/routes/blind_labeling.py`（+6）、`backend/app/core/config.py`；
- `backend/migrations/versions/20261014_01_blind_pool_state.py`（新，67 行）；
- `backend/scripts/retire_session_work_orders.py`（新，160 行，运维审计入口）；
- 测试：`test_blind_labeling.py`（+312）、`test_work_order_log_verdicts.py`（+97）、`test_config.py`（+22）。

配置默认值变化（均为带默认值的新键，**不需要改 `.env`**）：

| 键 | 旧 → 新 | 含义 |
| --- | --- | --- |
| `WORK_ORDER_LOG_POLL_SECONDS` | 300.0 → **60.0** | 判定 worker 轮询周期缩短到 1 分钟 |
| `WORK_ORDER_LOG_TAIL_BYTES` | 新增 **8388608**（8 MiB） | 每轮只读访问日志尾部，0 关闭实时判定 |
| `WORK_ORDER_LOG_RETIRE_LIMIT` | 新增 **1000** | 每轮最多自动退池工单数，0 关闭 |

迁移 `20261014_01_blind_pool_state` 是**纯增量、向后兼容**的：只对
`blind_label_work_orders` 做 `ADD COLUMN pool_state String(16) NOT NULL DEFAULT 'active'`、
`retired_at DateTime NULL`、`retire_reason String(64) NULL` 和
`create_index ix_blind_label_work_orders_pool_state`，旧镜像不读这些列也能继续跑，
所以本次采用「先迁移、后切镜像」，两段之间没有停服风险窗口。

## 2. 上线前准备（只读）

1. **基线快照**（仅读，落盘 `/opt/knowledge-kb-runtime/.dsh-deploy-e3b361f/`）：

   | 项 | 值 |
   | --- | --- |
   | 切换前镜像 | `knowledge-kb-backend:blind-label-fix-876da12`（`sha256:567d2829671a…`） |
   | 容器环境变量 md5（74 个键） | `582ccc90ebd2e3b556b88fda70862a1a` |
   | 挂载集合 md5 | `88e3d19a958535e437d47c2456cfcd3b` |
   | 运行规格 md5 | `affcf75a3d3e95edd658e30c86e93c88` |
   | `cmd` / `entrypoint` | `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]` / `null` |
   | 重启策略 / 端口 / 网络 | `unless-stopped` / `127.0.0.1:8000:8000` / `knowledge-kb_default`（172.18.0.2） |
   | `alembic current` | `20261012_01_blind_label_target (head)` |
   | 健康 | `/health` 200、`/ready` 200、`https://…/app` 200（750041 B）、近 30 分钟 Traceback 0 / 500 0 |
   | 磁盘 | `/dev/vda1` 180G，已用 124G，可用 57G（69%） |

   注意：`kb-backend` 镜像**没有 HEALTHCHECK**（`docker inspect` 无 `.State.Health`），
   验收只能看 HTTP 探活和日志，不能看容器健康状态（本次踩到过，见第 7 节）。

2. **干净导出**：线上 `/opt/knowledge-kb` 工作区有 142+ 条未提交改动和 607 个部署暂存文件
   （见 `docs/production-repo-divergence-20261010.md`），不能拿来构建。用
   `git archive --format=tar.gz -o master-e3b361f.tar.gz e3b361f`（2,834,492 B，
   md5 `14a420b1c9973220f32240ffdc23097c`）导出，上传后解到
   `/opt/knowledge-kb-deploy/e3b361f`。

3. **构建**：stage Dockerfile 放在 `/opt/knowledge-kb-runtime/.kb-deploy-e3b361f/Dockerfile`
   （与仓库一致，仅 pip 源改 mirrors.tencent.com），`export SAFE_BUILD_SLICE=qabuild.slice`
   后 `docker build --network host -t knowledge-kb-backend:pool-state-e3b361f`，
   耗时约 4 秒（pip 层命中缓存），产物 `sha256:33bfc8bc6144…`。

4. **镜像内文件与本地导出逐项一致**（`docker exec sha256sum` = 工作区 export 的同名文件）：

   | 文件 | sha256 |
   | --- | --- |
   | `app/services/blind_labeling.py` | `769290513d5a0ec8fc9cce2ac77a81d6cdc3f7736bc28ca3ce51bba85d43b0b8` |
   | `app/services/work_order_log_verdicts.py` | `8f6edc8ccabe25ec7bfa89e66126b415837724ee9f9092f2f137489e2a1566fc` |
   | `app/core/config.py` | `5a50929dbd5687ef1eec07af07aa7e6c0e935e0a9853e55dee7989397d6ab061` |
   | `app/models/blind_labeling.py` | `50951981956901973b41c18ae77a9dfb06816a8492364c270ec4441209d6f2e6` |
   | `app/routes/blind_labeling.py` | `6922def1ca7f8ffef3252078ba4ecd359bcaeeb381feb649a96775e3decb8812` |
   | `migrations/versions/20261014_01_blind_pool_state.py` | `842f923d240b55aec63bd2a10892a2c5be12196a28cc065036743dd37d1117fa` |
   | `/frontend/index.html` | `f866f2169db523172d272e7c5cef527f384dcb4c5dd0487694ce3727b29cde3e`（与切换前一致，前端未变） |

5. **测试**：用新镜像对本次区间相关测试跑过（`test_blind_labeling.py`、
   `test_work_order_log_verdicts.py`、`test_config.py`、`test_migration_revisions.py` 等），
   全部通过；新测试中 `test_claim_path_tolerates_a_work_order_that_already_has_a_completed_assignment`
   正是 #197 那次领单 500 的回归守卫。

## 3. 数据库迁移（切换前）

迁移不依赖 compose（生产 `kb-backend` 不是 Compose 管理的容器），沿用
「`docker inspect` 导出环境 + 同网络一次性容器」的方式：

```bash
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' > /tmp/kb-deploy-env
docker run --rm --network knowledge-kb_default --env-file /tmp/kb-deploy-env \
  --entrypoint python knowledge-kb-backend:pool-state-e3b361f -m alembic upgrade head
```

迁移退出码 0；`alembic current` 变为 `20261014_01_blind_pool_state`。
迁移本身没有删除/重命名任何列，旧镜像在新 schema 上仍可正常读写。

## 4. 切换（14:48:48Z）

用主机规范脚本切换（**不能**用 `docker compose up -d --no-deps backend`：生产
`kb-backend` 不是 Compose 容器，会因容器名冲突生成垃圾容器）：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:pool-state-e3b361f
# 新容器 a31693abc74259412c266d614bbeb936eb4de20cc175ec91258a395fe15717d7
```

切换前后 `docker inspect` 逐项比对，**只有镜像与启动时间变化**：

- 环境变量：73 个键完全一致（脚本内部 md5 `1caf40bac9428c37bc2ab8f96225fa96` 前后相同）；
- 挂载集合一致：`/opt/knowledge-kb/backend/uploads → /app/uploads`（rw）、
  `/www/wwwlogs → /app/nlogs`（**ro**，工单号门禁的数据源，漏挂会让门禁静默失效）、
  `knowledge-kb_manhattan_cache → /app/data`（rw）；
- 运行规格：`cmd`、`entrypoint`、`WorkingDir=/app`、`User`、端口绑定、网络
  `knowledge-kb_default`、`restart=unless-stopped`、`ExtraHosts=host.docker.internal:host-gateway`
  全部相同（规格 md5 `f1720955bf2f5207391ce9b7e99b0be5`）。

## 5. 上线后验收

### 5.1 退池实际生效（业务副作用，符合预期）

worker 在切换后第一个轮询周期内就完成了存量退池（容器 14:48:48Z 启动，验收脚本 14:52Z 查询时已生效）：

| 指标 | 切换后实测 | 变化 |
| --- | --- | --- |
| `pool_state='retired'` | **290**（全部 `retire_reason='session_number'`） | 切换前为 0；切换前 dry-run 判定「可退池工单 290 条，其中在途派发 16 条」 |
| `pool_state='active'` | **3337** | 3627 − 290 |
| 派发 `completed` | **3865** | **不变**：已完成的标注劳动不回收 |
| 派发 `released` | **1012** | 其中 16 条来自本次释放在途派发（996 + 16） |
| 派发 `assigned` | **189** | 与 `in_progress`（6）合计 195，正好等于切换前 211 − 16 |
| 派发 `in_progress` | **6** | 同上 |
| `blind_label_annotations` | **9299** | **不变**：不删除任何标注 |
| 退池工单上保留的已完成派发 | **299** | |
| 退池工单上保留的标注条数 | **796** | 涉及 100 张已被真实标注的工单 |

`backend/scripts/retire_session_work_orders.py`（`--apply` 才写库）再次 dry-run 复核幂等：

```
已判定为「只当过会话号」的号码：117383 个
本轮可退池工单：0 条，其中在途派发：0 条
当前已退池工单：290 条，保留下来的已完成派发：299 条
dry-run：没有写入任何数据。加 --apply 才会真正退池。
```

### 5.2 建单/派发门禁双向验证（只读）

用生产数据直接调用判定函数（不写库）：

- 已退池工单样本 5/5 仍判定为会话号（`is_session_number(...) == True`）；
- 在池工单样本 5/5 判定为 `False`；
- **池内仍是会话号的工单数 = 0**（无残留，退池清干净）。

### 5.3 worker 存活与在跑

- `/app/data/work_order_log_verdicts.json`（0600）mtime 持续刷新：14:54:56Z（观测时刻 14:55:25Z），
  文件内记录 200000 条判定结论；
- 会话号快照数量在观测窗口内递增：117383 → 117385 → 117389 → 117518，说明 worker 每轮都在读
  `/app/nlogs/zzdy.powerzhuan.cn.log` 的尾部（`WORK_ORDER_LOG_TAIL_BYTES=8 MiB`）；
- 切换后日志中**没有**「读取访问日志失败」类告警，说明只读挂载与路径正确。

### 5.4 接口与探活

| 检查 | 结果 |
| --- | --- |
| `http://127.0.0.1:8000/health`、`/ready`、`/app` | 200 / 200 / 200 |
| `https://knowledgekb.powerzhuan.cn/health` | 200 |
| `GET /api/v1/blind-labeling/my-batch`（无 Cookie） | 401 |
| `GET /api/v1/blind-labeling/overview`（无 Cookie） | 401 |
| `POST /api/v1/blind-labeling/my-batch:claim?target_count=50`（无 Cookie） | 422（客户端不能自定批次大小，符合设计） |
| 切换后 30 分钟日志 | Traceback 0、500 0；4xx 全部是本次验收探活自身产生的请求 |

观察窗口（14:48Z–14:58Z）内没有外部用户请求落到盲标接口，只有本机 `172.18.0.1` 的探活，
因此**真实标注员的领单/提交路径需要在业务时段人工确认一次**（下一节）。

## 6. 已知影响与人工确认项

- 本次上线会自动退池 290 张会话号工单（2026-10-14 事故当天统计的池内存量是 285 张，
  多出的 5 张是其后新进池/补建的工单），并把 16 条在途派发置为 `released`；
  **已完成的 299 条派发、796 条标注全部保留**，
  标注员此前的劳动不会被删除，也不会重复派发这些工单。
- 若某标注员的工作台在切换瞬间正好打开着被退池工单的详情页，页面上的提交会失败并提示工单已退池
  （这是预期行为，`pool_state='retired'` 不再派发）。
- 待人工确认：业务时段登录标注员账号，确认「领单 → 提交 → 仲裁」全链路正常，并确认工作台不再出现
  会话号工单。

## 7. 过程中遇到的问题与处置

1. **`docker inspect` 探活字段不存在**：切换脚本第一步用了
   `{{.State.Health.Status}}`，但 `kb-backend` 镜像没有 HEALTHCHECK，`docker inspect`
   直接报错，加上 `set -eu`，脚本在**没做任何改动前**就退出了（安全失败）。改为
  `Config.Image` + `State.StartedAt` 并用 HTTP 探活替代健康状态。
2. **`retire_session_work_orders.py` 单独执行报错**（已修，见第 9 节）：直接
   `python scripts/retire_session_work_orders.py` 会抛

   ```
   sqlalchemy.exc.InvalidRequestError: When initializing mapper
   Mapper[BlindLabelBatch(blind_label_batches)], expression 'User' failed to locate
   a name ('User').
   ```

   原因是脚本只导入 `app.models.blind_labeling`，而 `BlindLabelBatch.user` 的 relationship
   目标是 `User`（`app/models/user.py`）——应用进程由 `app.main` 预导入全部模型，只有独立执行
   才踩到。本次用「先 `import app.main` 再切 `--apply`/dry-run」的临时包装跑通干跑与复核，
   真正的修复走独立分支。
3. **Windows 侧投递脚本必须保 LF**：`ssh host "sh -s"` 或 base64 都会破坏行尾
   （`$'\r': command not found`）。本次统一：本地写 LF → `scp` → `ssh "sed -i 's/\r$//' <file>; sh <file>"`。

## 8. 回滚

回滚后端镜像（迁移是纯增量的，不需要降级数据库）：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:blind-label-fix-876da12
```

回滚后的行为差异：不再有自动退池（`pool_state` 列保留但旧代码不读），已退池的 290 张工单
仍是 `retired` 状态、不会重新进池（数据状态不回退，也不需要回退）；日志实时判定 worker 恢复为
300 秒周期。若确需降级 schema（一般不需要）：

```bash
docker run --rm --network knowledge-kb_default --env-file /tmp/kb-deploy-env \
  --entrypoint python knowledge-kb-backend:pool-state-e3b361f -m alembic downgrade 20261012_01_blind_label_target
```

## 9. 遗留事项

- 退池脚本「可独立执行」的修复已提分支 `fix/retire-script-standalone-mapper`
  （提交 `a31d9e4`：显式导入 `User` + 新增
  `backend/tests/test_retire_session_work_orders_script.py` 回归测试），待评审合并；
  该分支尚未进入线上镜像，线上脚本仍是旧版（本次靠包装脚本跑通）。
- 业务时段确认标注员全链路（见第 6 节）。
