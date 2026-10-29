# 三项遗留问题处理记录（2026-10-29）

承接 `docs/incident-20261029-search-pool-exhaustion.md` 与
`docs/followup-20261029-tunnel-and-candidate-list.md`，本轮完成剩余三项。

## 1. 生产镜像漂移：manhattan 错误提示回填

线上 `manhattan.py` 有一组按异常类型给出的中文提示，只存在于手工构建的镜像里，
仓库仍是英文通用提示，每次部署都要手工 `docker cp` 保留。

**处理**（PR #247）：

- 把这组文案写回仓库：`ConnectTimeout` / `ReadTimeout` / `ConnectError` /
  `NetworkError` / 其他，各自给出可操作的中文原因；
- 新增 `test_manhattan_session_error_copy.py`，锁定 5 个分支都返回 502 与对应文案。

**结果**：本次部署直接用 `git archive` 构建，`grep` 确认仓库内已含文案
（`manhattan_in_repo=yes`），不再需要手工保留。

## 2. 候选列表根治：jsonb + 精简投影 + 单层查询

### 2.1 分阶段改动

| PR | 改动 | 效果 |
| --- | --- | --- |
| #244 | 状态过滤下推 SQL | pending 6.3s → 1.65s |
| #247 | 摘要改为全量 SQL 计数（同时修复 #244 引入的摘要随筛选缩小的回归） | 摘要 8.0s → 17ms |
| #248 | 三个 JSON 列迁移为 jsonb | 取三字段 7.35s → 1.54s |
| #249 | 只投影子文档，不再传整列 | 整列 6.3s → 2.3s |
| #250 | 去掉为 CAST 内联而保留的冗余子查询 | 全量 5.1s → 4.17s |

### 2.2 关键发现

1. **`json` 是文本存储**：每个 `->` 表达式都会重新解析整份文档，这是 6–7 秒的根因。
   改为 `jsonb` 后同一查询降到 1.54 秒。
2. **三层子查询在 jsonb 下成为纯开销**：它们原本用于阻止 `cast(jsonb)` 被内联，
   CAST 消失后只剩「各自物化 3 万行」。合并为单层后继续下降。
3. **摘要不能随筛选缩小**：PR #244 下推后 `summary.total` 从 29,881 变成 10,740，
   与页面语义不符。改为按 `review_status` 分组计数（普通列，17ms）。
4. **优先级不能下推 SQL**：实测 SQL 近似统计 1,684 条，实际 20,875 条，
   偏差过大，因此保留 Python 派生。

### 2.3 上线后实测

| 场景 | 处理前 | 处理后 |
| --- | --- | --- |
| `pending` 列表（默认页） | 6.3–7.3 s | **1.53 s** |
| `revision_required` | 全量扫描 | 0.26 s |
| 无状态全量列表 | 6.3–7.3 s | **4.17 s** |
| 摘要状态计数 | 8.0 s | 17 ms |

无状态请求只占线上 677 / 4,851（14%），最慢路径已从 6.3 秒降到 4.17 秒。

### 2.4 部署

- 迁移 `20261029_01_candidate_jsonb`（父版本 `20261027_01_candidate_tasks`）；
- 迁移前备份：`kb-before-jsonb.dump`（535,945,657 B）；
- 停后端 → 迁移（21 秒）→ 启动新后端（12:39:48–12:40:36，约 48 秒维护窗口）；
- 环境变量 md5（`89cb3c17…`）与运行规格 md5（`57386af2…`）切换前后一致。

## 3. 隧道端口治理

### 3.1 根因

生产 sshd `ClientAliveInterval=0`，服务端从不探测。客户端被强杀或网络静默断开时，
半开会话一直占着 `0.0.0.0:18080 / 18780 / 19000`，新隧道报
`Address already in use`，只能靠容器反复重启（实测 2,500 次）。

### 3.2 处理（PR #251）

新增 `/etc/ssh/sshd_config.d/99-kb-tunnel-keepalive.conf`：

```text
ClientAliveInterval 30
ClientAliveCountMax 3
```

90 秒内无响应的会话由服务端回收并释放端口。执行顺序：`sshd -t` 校验 →
`systemctl reload sshd`（非 restart）→ 新连接验证 → 三条隧道验证。

### 3.3 诊断脚本的边界

新增 `scripts/cleanup-stale-tunnel-sessions.sh`，但**默认只诊断、不杀进程**。

原因：反向转发端口空闲时本来就没有 ESTABLISHED 连接，按「有无连接」判断死活会把
健康的 `18780` / `19000` 误判为残留（本次 dry-run 实测踩到，立即修正为诊断模式）。
`--force` 只清理「已不在任何转发端口监听列表里」的 sshd 子进程，并跳过 `:22` 主进程。

### 3.4 验证

| 检查 | 结果 |
| --- | --- |
| `sshd -T \| grep clientaliveinterval` | `30` |
| 新 SSH 连接 | `NEW_CONNECTION_OK` |
| 三个端口监听 | 各 1 个（共 3） |
| `18780` / `19000` | 200 / 404（正常） |
| 后端 `embed_texts` | 1024 维、55 ms |
| 隧道容器 `RestartCount` | **0**（此前 2,500） |
| Windows 孤儿探测进程 | **0**（此前 94） |

## 4. 最终状态

```text
backend   knowledge-kb-backend:candidate-single-a033900  running
health    200 (2 ms)
ready     200 (48 ms)
public    /app 200 (0.30 s)
tunnel    18080 / 18780 / 19000 各 1 个监听
日志      Traceback / QueuePool = 0
redis / embedding-qwen  未重建
```

回滚锚点：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:candidate-pushdown-26d952d
```

数据库回滚使用 `/opt/knowledge-kb-runtime/backups/pr248-jsonb-20261029/kb-before-jsonb.dump`；
迁移本身也提供 `downgrade`（jsonb → json）。

## 5. 仍可继续优化

1. **无状态列表 4.17 秒**：仍需把全部候选的子文档读进 Python 才能算优先级与去重指标。
   彻底解决需要把这些指标固化成列或物化视图。
2. **巡检脚本不在仓库**：`E:\answer-hub-runtime\_watch.ps1` 只存在于本机运行时目录，
   建议纳入版本管理。
3. **端口规划**：三个隧道共用同一 sshd 账户，建议后续给每条隧道分配独立端口段或
   独立 systemd 单元，进一步隔离故障域。
