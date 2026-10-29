# 故障后续处理：隧道重连与候选列表慢查询（2026-10-29）

承接 `docs/incident-20261029-search-pool-exhaustion.md`。本轮处理该故障记录里
列出的两项遗留问题。

## 1. 隧道托管

### 1.1 实测到的两个缺陷

**缺陷 A：内层重连循环从未执行**

`docker-compose.embedding-tunnel.yml` 的隧道命令以 `set -e` 开头。`ssh` 断开必然
返回非 0，脚本随即退出，`while true` 重连循环形同虚设：

| 证据 | 值 |
| --- | --- |
| 日志中 `连接结束（退出码 …），5 秒后重连` | **0 次** |
| 容器 `RestartCount` | **2500** |
| 每次重启额外动作 | `apk add openssh-client` |

结果只能靠 Docker `restart: unless-stopped` 兜底，重启间隔十几秒，日志被刷满。

**缺陷 B：常驻会话与容器争抢同一端口**

`AnswerHub-Tunnel` 计划任务以 `ssh -N -R 0.0.0.0:18780:…` 常驻，另有容器内的一次性
会话也尝试绑定，生产端 sshd 因此反复报：

```text
error: bind [0.0.0.0]:18780: Address already in use
error: bind [0.0.0.0]:19000: Address already in use
```

10:51 前后隧道重建时，后端同期出现 2203 条 `Embedding unavailable`，是整站挂起的触发源。

**缺陷 C：巡检探测进程挂死并累积**

`AnswerHub-Watch` 每 10 分钟运行 `E:\answer-hub-runtime\_watch.ps1`。其中探测用的
`ssh` 缺少心跳与超时，连接被中间设备静默丢弃后永不退出；任务被计划程序强杀后
这些 `ssh` 成为孤儿：

| 项 | 值 |
| --- | --- |
| 累积孤儿探测进程 | **94 个** |
| 最早进程 | 2026-10-19 17:42 |
| 父进程 | 全部已退出 |

### 1.2 处理

| 对象 | 处理 | 备份 |
| --- | --- | --- |
| `docker-compose.embedding-tunnel.yml` | `set -e` → `set -u`；连接存活不足 60 秒才指数退避（5→10→…→60 秒），存活够久重置为 5 秒（PR #245） | Git 历史 |
| `E:\answer-hub-runtime\_watch.ps1`（非仓库文件） | 探测 ssh 增加 `ServerAliveInterval=15`、`ServerAliveCountMax=2`，并把 stdin 置空 | `_watch.ps1.bak-poolfix-20261029` |
| 94 个孤儿探测进程 | 按「父进程已退出」精确筛选后结束 | 保留 2 个正常隧道进程 |

### 1.3 验证

- 重建隧道容器后 `restarts=0`，90 秒内未再重启（此前每十几秒一次）。
- 生产端 `0.0.0.0:18080` 正常监听；后端 `embed_texts` 返回 1024 维、115 ms。
- 巡检脚本手工执行 4.6 秒完成，输出 `local=200 tunnel=200 e2e=200`，
  执行前后残留探测进程均为 **0**。
- 后续两次定时巡检（12:02、12:12）均 `tunnel=200 e2e=200`，无新增孤儿进程。

## 2. 候选列表慢查询

### 2.1 根因

`GET /api/v1/integration/candidate-reviews` 每次请求都会解析全部 29,881 行
`candidate_payload` / `review_metadata`（约 538 MB JSON）。瓶颈是 PostgreSQL 对
`json`（非 `jsonb`）列的逐行解析与传输，不是索引。

生产库实测：

| 查询 | 耗时 |
| --- | --- |
| 只取 `id, review_status` | 0.05 s |
| 只取 `candidate_payload->'model_review'` | 2.77 s |
| 再加 `knowledge` | 3.9 s |
| 现网三字段组合 | 6.3–7.3 s |
| 6 并发同一请求 | 每个 20 s |

访问日志显示绝大多数请求自带 `review_status=pending`，但原实现先全量加载再过滤。

### 2.2 处理

PR #244 把 `review_status` 下推到 SQL，再对其余行做窄投影：

- 状态下推只作用于普通列，不改任何 JSON 派生逻辑；
- 不带状态时保持原全量行为，摘要仍覆盖所有状态；
- 用 `**kwargs` 透传，兼容只接受 `keyword` 的旧实现与测试替身。

### 2.3 验证

生产库只读等价性：`pending` 下推结果与「全量取回后过滤」的 id 集合、状态、
更新时间、`event_id` 完全一致（10,740 行，`same_ids=True`、`same_fields=True`）。

| 场景 | 改前 | 改后 |
| --- | --- | --- |
| `review_status=pending` | 6.3–7.3 s | **1.65 s**（端到端含构页 2.07 s） |
| `review_status=revision_required` | 全量扫描 | **0.26 s** |
| 无状态（全部） | 6.3–7.3 s | 6.72 s（未变） |

完整 `backend/tests`：1052 passed、4 failed；这 4 项在改动前同样失败。

## 3. 部署取证

| 项 | 值 |
| --- | --- |
| 源码 | `26d952d`（候选列表）+ `cb7601a`（隧道配置） |
| 后端镜像 | `knowledge-kb-backend:candidate-pushdown-26d952d`（`sha256:3fb9ceff…`） |
| 隧道容器 | 按新 Compose 配置 `--force-recreate`，`restarts=0` |
| 数据库迁移 | 无，保持 `20261027_01_candidate_tasks` |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578`（切换前后一致） |
| 运行规格 md5 | `57386af22df887126935025b39ec3ff8`（切换前后一致） |
| 切换后 | `/health` 12 ms、`/ready` 36 ms、公网 `/app` 200；日志 0 条 `QueuePool`/`Traceback` |
| 未重建 | `kb-redis`、`kb-embedding-qwen` |

回滚锚点：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:search-pool-fix-1a16017
```

## 4. 仍需处理

1. **端口残留会话**：生产端 `18780`/`19000` 常驻会话与容器会话仍可能争抢同一端口。
   本次只让失败退避，未清理 sshd 侧旧会话（避免影响其他隧道）。建议给每个隧道
   分配独立端口，或统一由 systemd 托管单一会话。
2. **无状态请求仍慢**：首次进入候选页的摘要仍要全量解析约 6.7 秒。下一步应把摘要
   改为 SQL 聚合，或把 `candidate_payload` / `review_metadata` 迁移为 `jsonb`
   并加表达式索引。
3. **生产镜像漂移**：`manhattan.py` 的报错文案改动仍不在 Git 里，本次部署继续
   手工保留（sha256 `f9327056…`）。建议尽快提交回仓库。
4. **巡检脚本不在仓库**：`_watch.ps1` 只存在于本机运行时目录，建议纳入仓库版本管理。
