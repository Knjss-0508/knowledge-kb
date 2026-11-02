# 内存与资源配置治理（2026-11-02）

背景：WMS 项目方于 2026-11-02 出具《knowledge-kb 资源治理建议》，指出本机
（`81.71.6.245`，4 核 / 15.4 GB）当天发生 3 次整机硬复位，本项目容器合计
6.2 GiB 且无任何上限。核实后按「先治本、再兜底」的思路执行了三步。

## 1. 核实结论

文档主要数据准确，但有一条会直接导致操作事故的判断错误：

| 文档说法 | 核实结果 |
| --- | --- |
| 今天 3 次硬复位（16:25 / 17:49 / 18:04） | ✅ `last -x` 确认 |
| 容器无内存上限 | ✅ `HostConfig.Memory=0` |
| 稳态 6.2 GiB / 峰值 7.7 GiB | ✅ 3.52 + 2.69 GiB |
| Committed_AS 超 CommitLimit | ✅ 19.0 GB vs 8.97 GB |
| 31 个同尺寸镜像 | ⚠️ 实际 53 个 |
| **用 compose overlay 加 `mem_limit`** | ❌ **会换掉线上镜像** |

**为什么 compose 方案不可用**：`kb-backend` 由 `run-kb-backend.sh` 用
`docker run` 启动，`com.docker.compose.project` 标签为空，不受 compose 管理；
而 `compose.pin-image.yml` 钉的是 `frontend-stale-error-20261001`，比线上
`candidate-pause-10ed87b` 落后 11 个版本。照文档执行 `up -d` 会把后端回退到
10-01 的旧代码。

## 2. 定位 3.5 GiB 的真实来源

原本怀疑是业务层泄漏，逐项排除后确认是 **glibc 分配器碎片**：

| 检查 | 结果 |
| --- | --- |
| Python 对象数 | 4,824 个，无 > 100 KB 的大对象 |
| 清空曼哈顿缓存后的 RSS | 3,567.1 → 3,567.1 MB（完全不变） |
| 内存形态 | 81 个匿名段，其中 14 个是 64 MB 整段 |
| 线程数 | 19，`MALLOC_ARENA_MAX` 未设置 |

结论：19 个线程各自申请 arena，请求进出后内存不归还系统。不是泄漏，但会持续吃内存。

## 3. 执行的三步

### 3.1 关闭服务器上空转的 CPU embedding

服务器后端 `EMBEDDING_BASE_URL=http://host.docker.internal:18080/v1`，指向本机
GPU 隧道。服务器自带的 `kb-embedding-qwen` 全部日志中：

```text
GET /health          214,179 次
POST /v1/embeddings        3 次   ← 仅早期联调
```

13 天只回健康检查、0 次真实嵌入，却常驻 2.69 GiB。

处理：`docker stop kb-embedding-qwen` + `--restart=no`，**容器与镜像保留**
（`docker start kb-embedding-qwen` 即可回滚冷启动）。

> 注意：关闭后「GPU 隧道全挂」时没有常驻 fallback。回滚方式记录在本文末尾。

### 3.2 限制 glibc arena 并加内存上限

`scripts/run-kb-backend.sh`（本次首次纳入仓库，此前只存在于服务器，
是配置漂移的根源）新增：

```bash
--memory=4g --memory-swap=4g     # 超限只杀本容器
-e MALLOC_ARENA_MAX=2            # 收敛分配器碎片
--log-driver json-file \
  --log-opt max-size=50m --log-opt max-file=5   # 日志轮转
```

三项参数都可用环境变量覆盖（`KB_MEMORY_LIMIT` / `MALLOC_ARENA_MAX`）。

### 3.3 扩容 swap 兜底

新增 `/swapfile-kb-4g`（4 GB），保留原有 `/www/swap`（1 GB），并写入
`/etc/fstab` 保证重启后挂载。

## 4. 效果（实测）

### 4.1 内存

| 项目 | 治理前 | 治理后 |
| --- | --- | --- |
| `kb-backend` 稳态 | 3.52 GiB | **0.55 GiB**（220 次检索后） |
| `kb-backend` 峰值 | 3.64 GiB | **0.76 GiB**（cgroup peak） |
| `kb-embedding-qwen`（服务器） | 2.69 GiB | **0**（已停） |
| 本项目合计 | 6.2 GiB | **≈ 0.56 GiB** |
| 整机 `MemAvailable` | 2.6 GB | **8.3 GB** |
| swap | 1 GB | 5 GB |

### 4.2 压测

| 轮次 | 请求数 | 成功 | 内存 |
| --- | --- | --- | --- |
| 第 1 轮 | 60 | 60/60 | 530.6 MiB |
| 第 2 轮 | 160 | 160/160 | 570.6 MiB |

内存不随请求量线性上涨，说明碎片已被收敛。

### 4.3 功能

| 检查 | 结果 |
| --- | --- |
| `/health`、`/ready` | 200 |
| 公网 `/app` | 200，0.30 s |
| 经隧道调用 GPU 嵌入 | 1024 维，51 ms |
| 挂载（uploads / data / nlogs） | 完整 |
| 端口绑定 | `127.0.0.1:8000` |
| 日志 `Traceback` / OOM / QueuePool | 0 |

停机时间：约 16 秒（21:31:54 → 21:32:10）。

## 5. 顺带修好的问题：每日清理任务已静默失败 3 天

`scripts/retention/run-prune.sh` 硬编码 `knowledge-kb-backend:latest`。该标签
在 10-31 清理镜像时被删，之后 docker 转而从远端拉同名镜像，被镜像代理白名单拒绝：

```text
Unable to find image 'knowledge-kb-backend:latest' locally
🚫 这镜像不在白名单
```

结果 `retrieval_quality_events` 涨到 1,296,870 行 / 2.4 GB（日增约 4 万行）。

修复：改为跟随 `kb-backend` 正在运行的镜像，并加入「镜像不存在即中止」保护，
避免再次误拉远端镜像。dry-run 已验证通过。

> dry-run 显示「超过保留期的纯检索事件：0 行」——说明表大不是保留策略问题，
> 而是写入量大（`standard-search` 每次调用都会记录），90 天保留期暂时够用。

## 6. 回滚

| 动作 | 回滚方式 |
| --- | --- |
| 停止 CPU embedding | `docker start kb-embedding-qwen`（冷启动 1–2 分钟） |
| 后端内存参数 | `cp /opt/knowledge-kb-runtime/backups/memory-governance-20261102/run-kb-backend.sh.before` 后重新执行 |
| swap | `swapoff /swapfile-kb-4g`，并删除 `/etc/fstab` 对应行 |

治理前快照：`/opt/knowledge-kb-runtime/backups/memory-governance-20261102/`。

## 7. 仍未解决的问题

1. **整机超卖 2.2 倍**：`Committed_AS` 19 GB vs `CommitLimit` 8.97 GB，是全机
   6 个项目共担的结果。本次把本项目从「最大不可控块」变成「可控块」，但
   60% 的超卖仍在。
2. **三次硬复位无法归因**：`oom_kill=0`、无 panic、无 vmcore、
   `memory.pressure` 为 0。本次不做「不再重启」的承诺。
3. **镜像仍 53 个 / 9.6 GB**：可回收约 3.16 GB，属零停机清理项，尚未执行。
4. **本机是办公 PC**：数据库仍在服务器，本机跨网访问数据库 3.6–12 秒，
   后端无法迁移；本项目长期增长应迁独立云主机，而不是迁到办公 PC。

## 8. 镜像清理（2026-11-02 完成）

### 8.1 执行结果

`knowledge-kb-backend` 从 **53 个降到 16 个**，Docker 镜像总量 9.62 GB → 8.39 GB。

### 8.2 关于「可回收 3.16 GB」的说明

文档与 `docker system df` 显示的 3.16 GB **不能按字面理解**：这些镜像共享
同一个 483 MB 基础层，每个 tag 的真实独占只有 12–13 MB。删 35 个镜像实际只回收
约 0.9 GB，而不是 3.1 GB。

剩下的 2.575 GB「可回收」主要是**其他项目**的镜像（voc-workbench 三件套、
appeal-exemption-system、mysql、postgres），不属于本项目，不得清理。

### 8.3 顺带修掉的隐患：pin-image 钉着旧版本

`/opt/knowledge-kb-runtime/compose.pin-image.yml` 原本钉的是
`frontend-stale-error-20261001`，比线上 `candidate-pause-10ed87b` **落后 11 个版本**。
任何一次 compose 启动都会把后端回退到 10-01 的代码。

已改为钉住当前线上镜像，并备份原文件为 `compose.pin-image.yml.bak-20261102`。

### 8.4 新增工具：`scripts/prune-kb-images.sh`

保留规则（四条并集）：

1. 当前运行镜像；
2. 所有容器（含已停止）引用的镜像；
3. 最近 3 个构建；
4. 回滚锚点清单 `/opt/knowledge-kb-runtime/kb-rollback-anchors.txt`；
5. compose pin-image 钉住的镜像。

默认只打印清单，`--apply` 才删除。**不使用 `docker image prune -a`**——本机
6+ 项目共用 Docker，全局 prune 会误删其他项目的镜像。

回滚锚点清单只保留当前本地确实存在的 tag，避免清单失真：
本次发现 6 个历史文档记录的锚点（`log-verdict-retention-20261010`、
`pool-state-e3b361f` 等）在清理前就已不存在，已从清单移除并在此记录。
