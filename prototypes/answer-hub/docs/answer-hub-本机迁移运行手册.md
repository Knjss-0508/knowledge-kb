# Answer Hub 本机迁移运行手册

> 状态：迁移准备完成，等待切换窗口。本手册记录架构、切换步骤、回滚方法与已知陷阱。

## 一、为什么要迁移

Answer Hub 需要调用公司内网 DeepSeek 网关（`tokenhub.zhuanspirit.com`）。实测结论：

| 位置 | 内网网关连通性 | 说明 |
|---|---|---|
| 开发机（本机） | ✅ 可达 | TLS 正常，模型可调用 |
| 旧服务器 | ❌ 不可达 | 路由经 `10.1.0.1` 腾讯云 VPC 网关后黑洞 |

因此服务器上的 Answer Hub **无法直接使用内网模型**。把 Answer Hub 迁到本机，同时保留服务器上的后端、数据库、网站不变。

## 二、目标架构

```
┌─────────────────── 旧服务器（保留） ───────────────────┐
│  kb-backend  PostgreSQL  nginx  网站                   │
│  （「运行监管」页面通过隧道调用 Answer Hub）            │
└───────────────────────┬───────────────────────────────┘
                        │ SSH 反向隧道
                        │ 服务器 :18780 → 本机 8780
                        │ 实测 52ms（对比 Tailscale 1.01s）
┌───────────────────────┴───────────────────────────────┐
│  本机（新电脑，GPU 节点）                              │
│    Answer Hub API    127.0.0.1 / 100.72.97.89:8780     │
│    GPU 嵌入服务      127.0.0.1:8080（Qwen3-Embedding） │
│    内网 DeepSeek     直连可用                          │
└───────────────────────────────────────────────────────┘
```

### 为什么不用 Tailscale

实测数据传输依赖美国中继，不适合承载生产链路：

```
tailscale ping → via DERP(lax) 615ms / 324ms / 633ms / 1.281s → 超时
netcheck: MappingVariesByDestIP=true（硬 NAT，打洞不可能成功）
          PortMapping 为空（UPnP/NAT-PMP/PCP 全部不可用）
          Nearest DERP = San Francisco（国内节点不可达）
```

SSH 反向隧道复用已验证机制，延迟 52ms，快 19.5 倍。

## 三、本机服务组成

三个 Windows 计划任务（开机自启 + 每分钟周期自愈）：

| 任务名 | 作用 | 说明 |
|---|---|---|
| `AnswerHub-API-Local` | 本机 Answer Hub API | 监听 `100.72.97.89:8780` |
| `AnswerHub-Tunnel` | SSH 反向隧道 | 服务器 `0.0.0.0:18780` → 本机 `8780` |
| `AnswerHub-Watch` | 每 10 分钟巡检 | 写 `observation.log` |

### ⚠️ 自愈必须用「周期触发」，不能用「失败后重启」

初次配置使用 `RestartCount` / `RestartInterval`，**实测完全失效**：

```
kill -9 API    → 200 秒 / 20 次探测 → 从未恢复
kill -9 隧道   → 200 秒 / 20 次探测 → 从未恢复
任务状态=Ready  上次结果=4294967295 (0xFFFFFFFF)
```

改为「每 1 分钟周期触发 + `MultipleInstances=IgnoreNew`」后：

```
kill -9 API    → 30 秒自愈 ✅
kill -9 隧道   → 60 秒自愈 ✅
```

原理：进程活着时新实例被忽略，进程死了就真正启动。不依赖「任务失败」这个不可靠的判断。

## 四、切换步骤

改动点极小 —— 只改 `/opt/knowledge-kb/.env` 两行：

```diff
- ANSWER_HUB_BASE_URL=http://host.docker.internal:8780
+ ANSWER_HUB_BASE_URL=http://host.docker.internal:18780

- ANSWER_HUB_API_BASE_URL=http://172.18.0.1:8780
+ ANSWER_HUB_API_BASE_URL=http://172.18.0.1:18780
```

> 已确认：整个 compose 栈（10 个文件）中，**只有 `docker-compose.yml` 第 85-89 行**引用这些变量，其余 9 个文件不覆盖。变量值来自 `.env`。

### 前置检查（全部必须通过）

```bash
# 本机 API
curl -s -o /dev/null -w '%{http_code}' http://100.72.97.89:8780/health          # 期望 200

# 隧道（服务器侧）
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18780/health            # 期望 200

# 业务接口（带鉴权）
curl -s -o /dev/null -w '%{http_code}' -H "X-Answer-Hub-Key: $KEY" \
     http://127.0.0.1:18780/api/v1/automation/control                           # 期望 200

# 容器内可达（切换后的真实路径）
docker exec kb-backend python -c "import urllib.request; \
  print(urllib.request.urlopen('http://172.18.0.1:18780/health', timeout=15).status)"   # 期望 200

# 回滚目标仍健康
curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8780/health            # 期望 200
```

### 切换与验证

1. 备份 `.env` 与 `docker inspect kb-backend`
2. 修改上述两行
3. `docker compose ... up -d --force-recreate --no-deps backend`
4. 等待 `healthy`（最长 300 秒，超时自动回滚）
5. 确认容器内 URL 生效：`docker inspect kb-backend --format '{{range .Config.Env}}...'`
6. 检查本机 `api-access.log` 是否出现来自 `kb-backend` 的规律请求
7. 打开「运行监管」页面确认正常

### 回滚

```bash
cp <备份目录>/env.bak /opt/knowledge-kb/.env
docker compose ... up -d --force-recreate --no-deps backend
```

**切换后服务器本地 Answer Hub 不停**，保留作为随时可回滚的退路。确认稳定运行一段时间后，再执行 `systemctl stop answer-hub-api`（**停用不删除**）。

## 五、夜间调度器

服务器用 systemd（`answer-hub-queue.timer` + `run_scheduled_queue.sh`），本机需要 Windows 等价物。

### ⚠️ 红线陷阱

服务器的拉取配置 `config/second-part-pull.powerzhuan.local.json` 里写着：

```json
"workflow": { "sync_to_cz_review": true }
```

**这个 true 与 `.env` 里的 `SYNC_TO_CZ_REVIEW=false` 冲突。** 如果原样复制到本机，可能导致本机向 CZ 推送数据。因此本机版本：

1. 把 `workflow.sync_to_cz_review` 强制改为 `false`
2. 命令行**不传** `--sync-to-cz-review`
3. 运行前四重安全闸校验，任一为真即拒绝运行（退出码 9）

### ⚠️ 不能两台机器同时跑

服务器与本机若同时执行夜间拉取，会用同一个日期窗口从 `qa.powerzhuan.cn` 拉取同一批数据，**产生重复处理**。正确顺序：

```
1. 先停服务器的 answer-hub-queue.timer
2. 再启用本机的调度任务
```

因此本机调度器「已实现但暂不启用」。

## 六、已知陷阱

| 陷阱 | 现象 | 应对 |
|---|---|---|
| SSH 管道丢输出 | 管道里最后一条命令的结果丢失，显示成 `HTTP 000`，**误判为生产故障** | 验证关键状态时单独执行；或写入文件后再读 |
| 硬编码 `\r` | 传给远程的参数带 `\r`，产生名为 `automation-runs\r\n` 的怪异目录、或 `--output-dir` 失效导致「0 runs」 | 远程脚本统一 `-replace "\`r",""` |
| PowerShell 5.1 解析中文脚本 | `The string is missing the terminator` | 用 `pwsh.exe`（PowerShell 7）执行含中文的 `.ps1` |
| `Out-File -Encoding UTF8` 写 BOM | JSON POST body 被拒（HTTP 400） | 用 `[System.IO.File]::WriteAllText($p,$j,(New-Object System.Text.UTF8Encoding($false)))` |
| 系统代理漏 Tailscale 网段 | 本机访问 `100.x` 得到 `502 Bad Gateway`（看似有服务在应答，其实被 `127.0.0.1:7897` 代理拦截） | 代理绕过列表加 `100.*` / `100.64.0.0/10` |
| `ExitOnForwardFailure=yes` | 服务器端口被旧隧道占用时，新隧道直接退出（退出码 255）——**这是正确行为** | 重启隧道前先确认远端端口已释放 |
| 不要在别人的项目目录跑 compose | 会重建别的项目的生产容器（曾造成约 20 分钟中断） | 显式传全部 `-f`，绝不在其他项目目录裸跑 `docker compose` |

## 七、验证方法

### 数据对等性

迁移后必须证明本机与服务器返回**同样的业务数据**：

```bash
# jobs 接口（1.65MB 真实业务数据）
curl -s -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:8780/api/v1/automation/jobs?limit=100 > server.json
curl -s -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:18780/api/v1/automation/jobs?limit=100 > local.json
cmp server.json local.json && echo "字节完全一致"
```

实测结果：`jobs` 与 `health` **字节完全一致**；`control` 的差异全部可解释（本机无 systemd 调度器，故 `installed=false`）。

### 数据库

```sql
PRAGMA integrity_check;   -- 期望 ok
SELECT COUNT(*) FROM candidates;        -- 5229
SELECT COUNT(*) FROM topic_registry;    -- 487
SELECT COUNT(*) FROM model_runs;        -- 8166
SELECT COUNT(*) FROM ingestion_records; -- 2781
```

## 八、待决事项

1. **夜间队列阻塞**：`job-20260926-200330-a33725f4` 已成功处理 995 条，仅 6 条 CZ 同步失败，导致整个队列跳过 17 天（`cursor_date` 停在 `2026-09-13`）。处理方式涉及「是否向 CZ 提交 995 条候选」，需业务决策。
2. **切换窗口**：需与正在部署该环境的同事协调。
3. **服务器本地 Answer Hub 停用**：切换验证稳定后再做，且是「停用不删除」。
