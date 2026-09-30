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

四个 Windows 计划任务（开机自启 + 周期自愈）：

| 任务名 | 作用 | 说明 |
|---|---|---|
| `AnswerHub-API-Local` | 本机 Answer Hub API | 监听 `100.72.97.89:8780` |
| `AnswerHub-Tunnel` | SSH 反向隧道 | 服务器 `0.0.0.0:18780` → 本机 `8780` |
| `AnswerHub-Watch` | 每 10 分钟巡检 | 写 `observation.log` |
| `Docker-Embedding-Watchdog` | Docker/GPU 容器守护 | 开机 + 登录 + 每 5 分钟检查 |

### ⚠️ 本机不只是「GPU 节点」—— 它已在承载服务器生产

排查出的完整依赖关系：

```
本机
├─ Docker: kb-embedding-qwen (Qwen3-Embedding-0.6B, 127.0.0.1:8080)
│   └─ kb-embedding-tunnel (alpine, while-loop 5 秒重连 + unless-stopped)
│        └─→ 服务器 :18080 → kb-backend 的 EMBEDDING_BASE_URL
│             ★ 服务器生产正在使用这条链路（迁移前就存在）
│
└─ 计划任务: Answer Hub API (100.72.97.89:8780)
    └─ AnswerHub-Tunnel → 服务器 :18780
         └─→ kb-backend 的 ANSWER_HUB_BASE_URL（切换后启用）
```

验证证据：

```bash
docker exec kb-backend python -c "..."   # 容器内真实调用本机 GPU：成功，1024 维，51ms
```

**降级退路**：服务器本地 `kb-embedding-qwen` 容器（CPU 嵌入）保留，万一本机不可用可切回。

### ⚠️ Docker Desktop 自启链曾经不可靠

```
Docker Desktop 配置 AutoStart = False                      ⚠️ 信号矛盾
注册表 HKCU\...\Run\Docker Desktop = E:\DockerDesktop\...  ✅
```

原有三层启动链（重启 → 自动登录 → Docker Desktop → 容器）中间一环不确定。故新增 `Docker-Embedding-Watchdog`：Docker 不在就启动，生产容器不在就 `docker start` 拉起。**这一环缺失时，本机重启会导致服务器 `kb-backend` 嵌入检索直接失败。**

### 未验证的路径：本机重启

`Boot` 触发从未被验证 —— 建任务后本机未重启过（GPU 容器连续运行 21 小时可佐证）。

**注意**：重启本机会**短暂中断服务器生产的嵌入能力**，必须选低峰时段并由人工确认，不可随意测试。

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

## 五、切换前检查清单

> **用途**：动手前 5 分钟内逐项打勾，确认「可以切」而不是「大概能切」。
> **原则**：能在窗口前发现的问题，绝不留到窗口中途才发现。
> 下表与 `scripts/local-migration/switch-answer-hub.sh` 的阶段 1 前置检查一一对应，但**人工先跑一遍**，避免脚本在中途 `fatal` 退出。

| # | 检查项 | 期望结果 | 失败则 |
|---|---|---|---|
| 1 | 四个计划任务状态 | API/隧道 `Running`，巡检/看门狗 `Ready`；`Ready` 的任务 `LastTaskResult=0` | 先修任务，不切 |
| 2 | 本机 API 进程在监听 | 8780 端口 LISTENING | 先修 API，不切 |
| 3 | 隧道 + 业务接口（服务器侧） | `/health` 200 且业务接口 200 | 先修隧道，不切 |
| 4 | 容器内可达隧道端口 | `172.18.0.1:18780` 返回 200 | 先查网关/隧道绑定，不切 |
| 5 | 服务器本地 `answer-hub-api` | `active`（回滚目标，**必须可用**） | **禁止切换** |
| 6 | 当前运行镜像 | 记录并核对，必须带 tag | 记录后由脚本再次防呆 |
| 7 | 备份目录已生成且 3 个文件在 | 切换脚本阶段 2 的产物 | 不切，先查磁盘 |
| 8 | `.env` 可写 | `-w` 为真 | 修权限后再切 |
| 9 | 执行时机 | 低峰窗口，且与同事的部署不重叠 | 改期 |
| 10 | 确认没有其他人在部署（Codex 代理自动部署） | 最近 20 分钟内**无**部署活动（首选 45 分钟以上），且此刻无 `docker compose`/构建进程 | **推迟本次切换**，不要抢跑 |

### 1. 四个计划任务都正常

```powershell
Get-ScheduledTask -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' |
  Select-Object TaskName,State,@{n='Last';e={($_.LastRunTime)}}
```

期望：

| 任务 | 期望 State | 说明 |
|---|---|---|
| `AnswerHub-API-Local` | `Running` | 每 1 分钟周期触发 + `MultipleInstances=IgnoreNew`，进程活着时新实例被忽略，因此**长期显示 Running 是正常的** |
| `AnswerHub-Tunnel` | `Running` | 同上 |
| `AnswerHub-Watch` | `Ready` | 巡检脚本执行完即退出 |
| `Docker-Embedding-Watchdog` | `Ready` | 同上 |

```powershell
Get-ScheduledTask -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' |
  Get-ScheduledTaskInfo | Select-Object TaskName,LastRunTime,LastTaskResult
```

> ⚠️ `LastTaskResult = 2147946720`（即 `0x800710E0` = ERROR_OPERATION_IN_PROGRESS）**是正常值**，含义是「上一实例还在跑，新实例被忽略」，不是报错。因此这个值通常只出现在显示 `Running` 的周期任务上；显示 `Ready` 的任务应当为 `0`。

### 2. 本机 API 在监听

```powershell
Get-NetTCPConnection -LocalPort 8780 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

期望：至少一条 LISTENING。API 计划任务监听 `100.72.97.89:8780`。

（这一项与下面的 HTTP 探测互补：端口在听但不响应，说明进程僵住了。）

### 3. 本机 API 从服务器侧可达（隧道回环 + 业务接口）

在**服务器**上执行：

```bash
# 3.1 服务器本地回环（先确认 API 自己活着）
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:8780/health
# 期望 200

# 3.2 经隧道回环（切换后 kb-backend 走的就是这条路）
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:18780/health
# 期望 200

# 3.3 业务接口（带鉴权，验证的不只是端口通、还包括业务路由和鉴权）
KEY=$(grep -E '^ANSWER_HUB_API_KEY=' /opt/knowledge-kb/.env | cut -d= -f2-)
curl -s -o /dev/null -w '%{http_code}\n' --max-time 20 \
     -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:18780/api/v1/automation/control
# 期望 200（401 说明鉴权头没带上；404 先怀疑路径漏了 /api/v1 前缀）
```

⚠️ 上面第 3.3 条**不要把 `$KEY` 打印出来**：本仓库是 public，任何输出都会进日志。

### 4. `kb-backend` 容器内能访问隧道端口

```bash
docker exec kb-backend python -c "
import urllib.request
r = urllib.request.urlopen('http://172.18.0.1:18780/health', timeout=15)
print('HTTP', r.status)
"
# 期望：HTTP 200
```

这一项验证的是**切换后的真实路径**：容器经 Docker 网关 `172.18.0.1` 访问宿主机上的隧道端口。前三项都绿而这一项红，说明问题在 Docker 网络侧（网关 IP 变了、隧道只绑定在 `127.0.0.1`、或被防火墙拦了）。

### 5. 服务器本地 `answer-hub-api` 是 active（回滚目标）

```bash
systemctl is-active answer-hub-api
# 期望：active

curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://127.0.0.1:8780/health
# 期望 200（这里指的是服务器本地那份 Answer Hub）
```

⚠️ **这是整个清单里唯一的硬门槛**：回滚目标不可用时**绝对不要切换**。切换后遇到问题想回滚，等于从一个坏状态退回另一个坏状态。

### 6. 当前运行镜像是什么（人工核对）

```bash
docker inspect kb-backend --format '{{.Config.Image}}'
# 例：kb-backend:20260930（必须带 tag）

docker inspect kb-backend --format '{{.Image}}'
# 镜像 ID，形如 sha256:....
```

把这两个值抄进切换记录。切换脚本会在启动时再读一次 `{{.Config.Image}}` 生成 `compose.pin-image.yml` 钉死它，并做防呆：**读不到镜像名、或缺 tag，直接拒绝执行**。人工核对的目的，是确认「脚本即将钉死的那个镜像」就是你以为的那一个 —— 若镜像名带 `:latest` 这类浮动 tag，说明这里曾被人手动重建过，需要先查清来源再决定是否切换。

### 7. 备份目录是否已生成、`.env` 是否可写

切换脚本的阶段 2 会把备份写到：

```bash
ls -l /opt/knowledge-kb-runtime/switch-backup-<时间戳>/
# 期望有 3 个文件：
#   env.bak                切换前的 .env 完整副本
#   urls.before.txt        切换前两行 URL 的快照（回滚时的权威副本）
#   kb-backend.before.json 切换前的容器完整配置
```

建议在**正式切换前**先跑一次 dry-run（它只做前置检查与备份、不改任何配置），用它的输出确认备份目录真的建得出来：

```bash
bash /opt/knowledge-kb-runtime/switch-answer-hub.sh --dry-run
```

`.env` 可写性：

```bash
test -w /opt/knowledge-kb/.env && echo "可写 OK" || echo "不可写 ❌ 停止切换"
```

### 8. 建议的执行时机

| 要求 | 原因 |
|---|---|
| 低峰时段（建议 22:00 — 次日 06:00） | 切换瞬间有短暂的 502：2026-09-30 真实切换实测「容器重建 → 应用可服务」约 20 秒（见第八节第 5 条） |
| 与同事的部署窗口错开 | 见第十节「待决事项」：该环境的部署由同事负责；**另需避开 Codex 代理的自动部署（见第 9 项）** |
| 避开夜间队列窗口 | 夜间拉取仍在服务器上跑，队列当前还卡着（见第十节「待决事项」） |
| 本机 GPU 容器稳定运行 | 服务器生产的嵌入链路经过本机，本机抖动会波及服务器 |

⚠️ 切换过程中**不要动本机**：重启本机会同时中断服务器生产的嵌入能力（原因见第八节第 7 条）。

### 9. 确认当前没有其他人在部署（Codex 代理自动部署）

> ⚠️ **这是本清单里唯一会随时间失效的检查。** 前面第 1~8 项的状态相对稳定，这一项每隔 20~40 分钟就可能翻转，**所以必须在动手前 5 分钟内重跑一遍**，而不能和前面的项目一起在半小时前查完。

**背景（实测数据）**：服务器上有一套由 Codex 代理自动部署留下的痕迹。同一天的实测时间线：

```
Codex 部署目录时间线:
  11:51:35  .codex-deploy-20260930-embedded-chat-2e1
  11:51:34  .codex-deploy-20260930-pre-embedded-chat-2e1
  11:24:44  .codex-deploy-20260930-layout-7bc
  11:24:43  .codex-deploy-20260930-pre-layout-7bc
  10:48:01  .codex-deploy-20260930-candidate-review-ready-fb5fb6f
  10:20:23  .codex-deploy-20260930-master-f851

镜像构建时间线:
  11:51:57  knowledge-kb-backend:master-2e1d1f00-20260930   ← 当前运行中
  11:25:58  knowledge-kb-backend:master-7bc28635-20260930
  11:09:29  knowledge-kb-backend:master-f851d59-20260930

⇒ 上午大致每 20~40 分钟就有一次自动部署
```

**为什么要查**：切换会重建 `kb-backend` 容器；Codex 代理部署时**同样会重建这个容器**。两边同时动手，等于**同时操作同一个容器与同一个 `/opt/knowledge-kb/.env`**，后果可能远重于已知的「约 20 秒 502」：`.env` 里两行 URL 被后写的一方覆盖成半新半旧、两边镜像互相顶掉导致脚本的镜像漂移检测误判并触发回滚，而**回滚路径本身从未验证过**（见第八节第 1 条），救不了场。

#### 9.1 检查命令（在服务器上执行，全部只读）

```bash
# 9.1.1 Codex 部署留痕：最近 10 条，按时间倒序
#        ⚠️ 目录名里的 20260930 只是命名习惯，判据必须看时间戳（%T+），不能看名字里的日期
find /opt -maxdepth 3 -name '.codex-deploy-*' -printf '%T+  %p\n' 2>/dev/null | sort -r | head -n 10

# 9.1.2 此刻有没有部署/构建进程在跑
ps -eo pid,etime,cmd | grep -E 'docker[ -]compose|docker build|codex|git (pull|checkout)' | grep -v grep

# 9.1.3 kb-backend 当前容器的启动时间（任何一方部署都会重建它）
docker inspect kb-backend --format '{{.State.StartedAt}}  status={{.State.Status}}'

# 9.1.4 最近构建出来的后端镜像时间
docker images --format '{{.Repository}}:{{.Tag}}  {{.CreatedAt}}' | grep '^knowledge-kb-backend' | head -n 5

# 9.1.5 最近 45 分钟内 kb-backend 有没有被创建/启动/停止过
#        事件是流式输出，5 秒后由 timeout 主动结束（退出码 124）属正常，不要当成报错
#        ⚠️ 必须带齐下面 6 个事件类型过滤，缺一个都会漏判部署信号
timeout 5 docker events --since 45m --filter 'container=kb-backend' \
  --filter 'event=create' --filter 'event=start' --filter 'event=restart' \
  --filter 'event=stop' --filter 'event=die' --filter 'event=destroy' || true
# 期望：无输出
```

> ⚠️ **未过滤的 `docker events` 输出里有大量健康检查噪声，必须忽略。** `kb-backend` 自带 healthcheck，**每 10 秒**执行一次 `python -c "...urlopen('http://127.0.0.1:8000/ready')..."`，每次调用产生 3 条 `exec_create` / `exec_start` / `exec_die` 事件。所以**不加 `--filter 'event=...'` 时，45 分钟窗口里的输出几乎全是这类噪声，永远不可能是 0 行** —— 照那样执行，这一项会被永远判成「⚠️ 危险，推迟切换」，把切换无限期卡住（或者执行人干脆绕过这一项，风险更大）。
> 只有**容器级事件** `create` / `start` / `restart` / `stop` / `die` / `destroy` 才是部署信号。上面命令已按事件类型过滤，**干净的窗口就该是 0 行**；若仍有输出，那才是真的有人在重建容器。
> 另一条独立证据是与 9.1.3 交叉验证：实测某次检查时 `StartedAt` 距今 73 分钟、`RestartCount=0`，而把窗口放大到 6 小时执行同一命令同样是 0 行 —— 三者一致，可证容器确实没被重建过。

> **关于路径**：实测记录只留下了 `.codex-deploy-*` 的目录名与时间，没有记全父目录，所以 9.1.1 用 `find /opt -maxdepth 3` 兜底搜索。若已确认实际父目录（例如 `/opt/knowledge-kb`），直接把 `find /opt` 换成该具体路径，输出更快也更准。若输出为空但确信服务器上有部署留痕，先怀疑路径写错或权限不足，而不是「没有部署」。
> 上面全部是只读命令，不会改动任何容器、镜像或配置。

#### 9.2 判断标准

| 检查 | ✅ 干净（可以切） | ⚠️ 危险（推迟） |
|---|---|---|
| 9.1.1 | 最新一条时间戳距今 **≥ 20 分钟**（≥45 分钟最佳） | 出现距今 < 20 分钟的 `.codex-deploy-*` |
| 9.1.2 | **无输出** | 出现 `docker compose` / `docker build` / `codex` 进程 |
| 9.1.3 | `StartedAt` 距今 **≥ 20 分钟**（且与 9.1.4 的最新镜像时间对得上） | 容器是几分钟前刚启动的 → 有人/有代理正在部署 |
| 9.1.4 | 最新镜像 `CreatedAt` 距今 ≥ 20 分钟，且 tag 数量少而稳定 | 几分钟内又冒出新 tag（说明正处在 20~40 分钟的连发节奏里） |
| 9.1.5 | **无输出**（只看容器级事件；未加事件类型过滤时的 `exec_*` 是健康检查噪声，不算） | 出现 `create` / `start` / `restart` / `stop` / `die` / `destroy` 事件 |

#### 9.3 「多久之内没有部署活动」的可执行门槛

> **门槛**：9.1.2 与 9.1.5 **无输出**（此刻没有任何部署在跑），**并且**最近一次部署活动 —— 「新 `.codex-deploy-*` 目录 / 新镜像 / 容器重建」三者取最新 —— 距今 **≥ 20 分钟**，才算窗口干净。
> 其中 9.1.5 的「无输出」指的是**带事件类型过滤后**的输出（容器级事件一个都没有）；未过滤时刷屏的 `exec_*` 是 `kb-backend` 每 10 秒一次的健康检查，属于噪声，不能据此判定「有人在部署」。

| 最近一次部署活动距今 | 结论 |
|---|---|
| **< 20 分钟** | ⚠️ **禁止切换**。实测是 20~40 分钟一轮的节奏，且个别间隔更短（镜像时间线 11:09:29 → 11:25:58 只有 16 分 29 秒），所以 20 分钟以内的「安静」分辨不出「下一轮马上就到」 |
| **20 ~ 45 分钟** | 可以切，但属于次选：必须**全程盯梢**（9.5），并准备随时中止 |
| **≥ 45 分钟** | ✅ 首选窗口。45 分钟 = 实测间隔上界约 40 分钟 + 5 分钟余量，通常意味着 Codex 这一轮已经收工 |

补充说明：

- 切换本身最坏约 7~8 分钟（见第八节第 8 条），上面的门槛已把这 8 分钟算进缓冲，不会「查到干净、切到一半就撞上」。
- ⚠️ 任何门槛都只能证明「**过去**安静」，不能保证「**未来**也安静」。真正的兜底是 9.5 的盯梢，而不是门槛本身。
- 上午那种每 20~40 分钟一次的节奏属于代理活跃期；**首选低峰时段（22:00 — 次日 06:00）执行**，撞车概率最低（与第 8 项「建议的执行时机」一致）。

#### 9.4 发现正在部署怎么办

| 发现的情况 | 处置 |
|---|---|
| 9.1.2 有 `docker compose` / 构建进程，或 9.1.1 出现 1 分钟内的新目录 | ⚠️ **放弃本次切换**（不是「歇 5 分钟再试」）。等它跑完、且 `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready` 连续 10 分钟返回 200，再**从本清单第 1 项重新走一遍** |
| 容器 `StartedAt` 只有几分钟 | 同上：先等，不抢 |
| 已经改了 `.env`、甚至已经发起 `--force-recreate`，这时才发现对方也在部署 | ⚠️ **立刻停手**：不要手工改配置、不要手工重启容器，让脚本走它自己的自动回滚；等两边都静止后再重跑清单。（第五节末的提醒同样适用：手工和脚本同时动手会把状态搅得更乱。） |
| 只是「刚部署完」，进程已退出、容器已稳定 | 至少再等 5~10 分钟，重复 9.1.5 确认容器不再被重建，然后再开始 |

> ⚠️ **不要「抢在对方前面几秒完成」**。两边都在重建 `kb-backend`、都在读写同一个 `.env`，抢跑最可能的结果是把两行 URL 写成「一半新一半旧」，那比一次约 20 秒的 502 难查得多。

#### 9.5 切换期间的盯梢（推荐）

正式切换时**另开一个终端**持续盯梢，一旦出现新留痕立刻停手：

```bash
# 每 15 秒打印一次当前时间与最近 3 条部署留痕；只读，Ctrl+C 结束
while true; do
  date '+%H:%M:%S'
  find /opt -maxdepth 3 -name '.codex-deploy-*' -printf '%T+  %p\n' 2>/dev/null | sort -r | head -n 3
  sleep 15
done
```

若盯梢期间发现新留痕，按 9.4 处置。

### 10. 执行命令与 dry-run 命令

```bash
# 第一步：dry-run（只做前置检查 + 生成备份目录，不改任何配置）
bash /opt/knowledge-kb-runtime/switch-answer-hub.sh --dry-run

# 第二步：确认 dry-run 全部 ✅ 后，正式切换
bash /opt/knowledge-kb-runtime/switch-answer-hub.sh
```

正式切换会依次输出：钉死镜像 → 阶段 1 前置检查 → 阶段 2 备份 → 阶段 3 改 `.env` 两行 → 阶段 4 `--force-recreate --no-deps --no-build` 重建 → 等 `healthy`（最多 300 秒，超时自动回滚）→ 阶段 5 验证（容器内 URL 生效 + `/ready` 200 + 公网入口）→ 阶段 6 提示观察本机请求日志 → 打印回滚所需信息。

同时开一个窗口盯本机收到的请求：

```powershell
Get-Content E:\answer-hub-runtime\api-access.log -Tail 20 -Wait
```

期望：出现来自 `kb-backend` 的规律请求（「运行监管」页面走的就是这条路）。

> **关于自动化与人工重复**：脚本自己会重跑上面第 1~7 项，任一项失败即中止。人工先跑一遍的价值在于「提前发现问题」，而不是「替代脚本」。**若切换中途失败，请信任脚本的自动回滚，不要手工抢着改配置** —— 手工与脚本同时动手会把状态搅得更乱。

## 六、夜间调度器

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

## 七、已知陷阱

| 陷阱 | 现象 | 应对 |
|---|---|---|
| `docker compose up` 误重新构建镜像 | `/opt/knowledge-kb/docker-compose.yml` 里 `backend` 服务用的是 `build:` 而非 `image:`，重建容器会**顺带把生产代码换成当前 checkout 的版本**。本仓库已因此出过事故（alembic 迁移版本缺失导致 `kb-backend` 起不来） | 切换脚本改为：运行时读当前镜像 → 生成 override 钉死 → 配合 `--no-build`；并加防呆（读不到镜像名或缺 tag 就拒绝执行）。**永远不要在这套 compose 上裸跑 `up`** |
| `kb-backend` 没有 `/health` 端点 | `curl http://127.0.0.1:8000/health` 返回 **HTTP 000**（连接层面就没有该路由），看起来像「生产挂了」 | 正确的端点是 `/ready`：就绪返回 `{"status":"ready"}`，依赖未就绪返回 503。判断生产是否恢复可服务**必须看 `/ready`**；容器自身 healthcheck 用的也是 `/ready` |
| 公开仓库里服务器地址必须脱敏 | 本仓库是 public，一旦把服务器公网地址写进脚本或文档就等于对外公开 | 服务器公网地址统一用 `<SERVER_HOST>` 占位；输出日志时不要把密钥值 `echo` 出来 |
| PowerShell 变量名不区分大小写 | `$rt`（reasoning token 数）静默覆盖了 `$RT`（路径变量）—— 两者是同一个变量 | 变量名不用大小写区分语义；命名时加限定词（如 `$RuntimeRoot`）。此坑曾导致冒烟测试出现两处假失败 |
| 临时探测工具本身也会出错 | 本会话出现三类自伤：探测路径漏写 `/api/v1` 前缀 → 误报 404；把当前时间估早 10 分钟 → 误判巡检停摆；用 `git show` 提取脚本内容做语法检查 → 内容损坏 → 误报语法错误 | **任何异常信号在下结论前必须用独立方式复验，并优先怀疑测量方法本身**，而不是先怀疑生产 |
| SSH 管道丢输出 | 管道里最后一条命令的结果丢失，显示成 `HTTP 000`，**误判为生产故障** | 验证关键状态时单独执行；或写入文件后再读 |
| 硬编码 `\r` | 传给远程的参数带 `\r`，产生名为 `automation-runs\r\n` 的怪异目录、或 `--output-dir` 失效导致「0 runs」 | 远程脚本统一 `-replace "\`r",""` |
| PowerShell 5.1 解析中文脚本 | `The string is missing the terminator` | 用 `pwsh.exe`（PowerShell 7）执行含中文的 `.ps1` |
| `Out-File -Encoding UTF8` 写 BOM | JSON POST body 被拒（HTTP 400） | 用 `[System.IO.File]::WriteAllText($p,$j,(New-Object System.Text.UTF8Encoding($false)))` |
| 系统代理漏 Tailscale 网段 | 本机访问 `100.x` 得到 `502 Bad Gateway`（看似有服务在应答，其实被 `127.0.0.1:7897` 代理拦截） | 代理绕过列表加 `100.*` / `100.64.0.0/10` |
| `ExitOnForwardFailure=yes` | 服务器端口被旧隧道占用时，新隧道直接退出（退出码 255）——**这是正确行为** | 重启隧道前先确认远端端口已释放 |
| 不要在别人的项目目录跑 compose | 会重建别的项目的生产容器（曾造成约 20 分钟中断） | 显式传全部 `-f`，绝不在其他项目目录裸跑 `docker compose` |
| ⚠️ 切换可能撞上 Codex 代理的自动部署 | 服务器上有 Codex 代理自动部署留下的痕迹，实测上午大致每 20~40 分钟就重建一次 `kb-backend`；切换重建的是**同一个容器**，撞车时两边同时读写同一个 `/opt/knowledge-kb/.env`，故障可能远比「约 20 秒 502」严重 | 切换前按第五节第 9 项检查部署活动，**最近 20 分钟内有过活动就推迟**；切换期间另开终端盯梢。详见本节下方「切换可能撞上 Codex 代理的自动部署」 |

### ⚠️ 切换可能撞上 Codex 代理的自动部署

**现象与实测数据**：服务器上有一套由 Codex 代理自动部署留下的痕迹。同一天的实测时间线：

```
Codex 部署目录时间线:
  11:51:35  .codex-deploy-20260930-embedded-chat-2e1
  11:51:34  .codex-deploy-20260930-pre-embedded-chat-2e1
  11:24:44  .codex-deploy-20260930-layout-7bc
  11:24:43  .codex-deploy-20260930-pre-layout-7bc
  10:48:01  .codex-deploy-20260930-candidate-review-ready-fb5fb6f
  10:20:23  .codex-deploy-20260930-master-f851

镜像构建时间线:
  11:51:57  knowledge-kb-backend:master-2e1d1f00-20260930   ← 当前运行中
  11:25:58  knowledge-kb-backend:master-7bc28635-20260930
  11:09:29  knowledge-kb-backend:master-f851d59-20260930

⇒ 上午大致每 20~40 分钟就有一次自动部署
```

两点解读：

- 每一轮部署会留下两个目录（`...-pre-xxx` 与 `...-xxx`），同一秒生成，是同一次部署的前后快照；同一份时间线里，相邻两轮最多只隔约 37 分钟（10:48:01 → 11:24:43），而镜像构建的最短间隔只有 16 分 29 秒（11:09:29 → 11:25:58）。
- ⚠️ 目录名里的日期（`20260930`）只是命名习惯，**判断「多久没部署了」必须看文件时间戳**，不能看名字。

**风险**：切换与 Codex 部署**都会重建 `kb-backend`**。两边同时动手，就是同时操作同一个容器与同一个 `.env`：

- `.env` 里两行 URL 被后写的一方覆盖成「一半新一半旧」
- 两边镜像互相顶掉，脚本的镜像漂移检测误判并触发回滚
- 而**回滚路径从未真正执行过**（见第八节第 1 条），不能拿它当兜底

**应对**：

1. 切换前执行第五节的第 9 项（命令与判断标准都在那里），**并在动手前 5 分钟内重跑一次**。
2. 门槛：最近 20 分钟内有过部署活动就推迟；**45 分钟以上无活动才是首选窗口**。
3. 切换期间另开一个终端盯梢（命令见第五节的第 9 项），出现新留痕立即停手。
4. 首选低峰时段（22:00 — 次日 06:00）执行 —— 上午那种 20~40 分钟一轮的连发属于代理活跃期，撞车概率明显更高。

**✅ 一个有利的事实**：Codex 部署时**也读 `/opt/knowledge-kb/.env`**。所以切换把 `ANSWER_HUB_BASE_URL` / `ANSWER_HUB_API_BASE_URL` 改成 `:18780` 之后，它们后续的部署会**自动带上这个值，不会被改回 `:8780`** —— 这一点已在较早的排查中确认。

也就是说，**要提防的只是「时间上撞车」，不是「配置被改回去」**。这两件事的处置完全不同：撞车靠推迟与盯梢解决，配置被改回去才需要防「别人覆盖 `.env`」的机制，而后者在这个环境里不存在。

## 八、尚未覆盖的风险

> **诚实声明**：本节记录本次准备工作中**没有验证过**的部分，供执行人决策。
> 列出它们不是为了免责，而是为了让执行人预先知道边界在哪里 —— 本手册前面已多处出现「已实现但未验证」的状态。

| # | 未覆盖的风险 | 影响 | 目前状态 |
|---|---|---|---|
| 1 | **脚本的回滚路径从未真正执行过** | 第一次执行时，回滚可能也不work | 只走通 dry-run 阶段 2 |
| 2 | `/ready` 是硬编码假设 | 假失败会误触发回滚 | 未经故障注入 |
| 3 | `/ready` 200 ≠ 端到端业务可用 | 可能「技术成功、业务不可用」 | 需人工确认 |
| 4 | 公网入口失败只告警、不回滚 | 公网故障时切换仍宣告成功 | 有意的取舍 |
| 5 | 切换有短暂的 502：「容器重建 → 应用可服务」实测约 20 秒（其中大部分是健康检查采样粒度，真实业务不可用窗口更接近容器启动本身，实测 1.3~2 秒） | 用户可见短暂失败 | 2026-09-30 真实切换已实测，需选低峰 |
| 6 | 本机重启（`Boot` 触发）从未验证 | 自愈能力在重启后是未知数 | 建任务后未重启过 |
| 7 | 重启本机会中断服务器生产的嵌入 | 影响服务器生产 | 迁移前就存在的依赖 |
| 8 | 镜像钉死文件在切换过程中无二次校验 | 理论上存在被篡改窗口 | 仅切换前读一次 |

以下逐条展开。

### 1. ⚠️ 脚本的回滚路径从未被执行过

切换脚本（`switch-answer-hub.sh`）含自动回滚。**2026-09-30 的真实切换已把正式流程的阶段 3~5 走通**（改 `.env` → 重建容器 → 等 `healthy` → `/ready` 200），但在那之前它只走到 dry-run 的阶段 2（前置检查 + 备份）；而**回滚路径至今仍然没有被执行过**。以下分支**仅经代码审查，未做故障注入验证**：

- `rollback()` 本身（还原 `.env` + 重建容器 + 等 `healthy`）
- 容器未在 300 秒内健康 → 自动回滚分支
- `/ready` 未在 120 秒内返回 200 → 自动回滚分支
- 镜像漂移检测（`Config.Image` 变了 / 同 tag 指向了新构建的 imageID）→ 自动回滚分支

**含义**：脚本第一次真正触发回滚时，**回滚路径也同样是「第一次执行」**。所以「反正有自动回滚」不能当作兜底保证。

**建议**：执行时选在绝对低峰 + 有人值守的窗口 —— 回滚路径**至今仍是「第一次执行」状态**；把「手工回滚」的两条命令单独抄在手边，不依赖脚本。

### 2. ⚠️ `/ready` 端点是硬编码假设

脚本里 `KB_BACKEND_URL=http://127.0.0.1:8000`、路径 `/ready` 都是写死的。

**风险场景**：将来 `kb-backend` 换了容器端口、或改了路由前缀，这个检查会一直返回 `000`/`404`，脚本会**误判为「后端起不来」并回滚**。

**关键认知**：这是**假失败，不是真故障**。排查时先确认 `/ready` 这个假设是否还成立，再去查容器。

**改进方向**：把探测 URL 做成参数，或直接以容器自身的 healthcheck 状态（`docker inspect --format '{{.State.Health.Status}}'`）作为判据。

### 3. `/ready` 返回 200 不等于端到端业务可用

`/ready` 200 只说明**后端自身与依赖就绪**，不代表「运行监管」页面真的能正常用。

端到端还需要：公网入口 2xx（本节第 4 条说明它不参与回滚）、本机 `api-access.log` 出现来自 `kb-backend` 的请求、页面手工操作正常。**后者是唯一能证明「业务可用」的证据，必须人工做。**

### 4. 公网入口检查失败时「只告警、不触发回滚」

这是**有意的取舍**，理由：公网可达性受宝塔 nginx / CDN / 运营商链路影响，可能因与本切换无关的原因短暂失败。如果让它触发回滚，就会出现「回滚一个本身完全正常的切换」。

**代价**：公网入口真的故障时，切换过程**仍会宣告成功**（脚本末尾照样打印「切换完成 ✅」）。执行人必须自己看阶段 5 的公网检查结果，⚠️ 不能只看结尾那一行。

### 5. 切换时的短暂 502：实测约 20 秒（2026-09-30 真实切换）

`--force-recreate` 会让容器重启，期间 nginx 反代会返回 502。

**2026-09-30 的真实切换实测时间线**（取自切换脚本自身的日志）：

```
15:14:05  阶段 3 改配置（改写 .env 两行）
15:14:05  阶段 4 容器 Recreate / Started
15:14:26  容器健康状态变为 healthy   ← 脚本每 10 秒采样一次：+10s 时仍是 starting，+20s 才 healthy
15:14:27  阶段 5  /ready 返回 200
⇒ 从容器重建到应用可服务，实测约 20 秒（此前按秒级估计的 5~10 秒偏乐观）
   口径说明：时间戳跨度 15:14:05 → 15:14:27 是 22 秒，但脚本每 10 秒才采样一次，
   真实就绪时刻落在两次采样之间、无法更精确，因此全文统一记为「约 20 秒」。
```

这 20 秒的构成 —— **大部分是观测粒度，不是真实的不可用时间**：

```
healthcheck：/ready，Interval 10s / Retries 10 / StartPeriod 0s
  ⇒ 脚本每 10 秒才采样一次，判定 healthy 至少要两次采样，单是这一步就占掉约 20 秒
容器启动本身（早前实测）：
  StartedAt → 最早应用日志 约 1.3 秒；import app.main 约 1.77 秒
⇒ 真实业务不可用窗口更接近启动时间（容器启动本身实测 1.3~2 秒；容器停止与端口重绑的零星开销未单独测量）；
  但脚本必须等到 healthy 才继续，所以「脚本判定可服务」的等待时间仍是约 20 秒。
```

**处置**：选低峰执行；若确认有用户正在使用，提前打招呼。

另需明确：这 20 秒属于**脚本的观测等待**，不是要去处理的故障，也不改变原有结论 ——

- **不需要动 nginx**：切换重建的是同一个 `kb-backend` 服务，对外发布的宿主机端口仍是 `127.0.0.1:8000`（宝塔 nginx 的反代目标没变）。**不改 nginx 配置、也不 reload**（宝塔 nginx 反代多站点，reload 会影响所有站点）。
- **端口由 Docker 自己重新绑定**：容器重建时 Docker 会释放并重新绑定同一个宿主机端口（docker-proxy / iptables 转发），端口号前后一致，因此 nginx 侧不需要任何配合动作，中间那几秒就是本文测到的 502 窗口。
- 因此**不要为了这 20 秒去加长脚本超时或改健康检查间隔**：真实业务不可用窗口只有容器启动那 1.3~2 秒的量级，把 `Interval` 调小只会增加容器负担，并不会让用户少失败。

### 6. ⚠️ 本机重启（`Boot` 触发）从未验证

四个计划任务都配了 `Boot` 触发，但**建任务后本机未重启过**（GPU 容器连续运行 21 小时可佐证）。因此「本机重启后一切自动恢复」这一条**属于假设，不是已验证事实**。

**影响**：本机重启后，若任务没有自动起来，那么：

- 服务器生产的**嵌入**能力会中断（见下一条）
- 切换后，Answer Hub 的调用链也会中断

### 7. ⚠️ 重启本机会短暂中断「服务器生产的嵌入能力」

这不是本次迁移引入的，而是**迁移前就存在**的依赖：

```
服务器 kb-backend 的 EMBEDDING_BASE_URL
  → 本机 GPU 容器 kb-embedding-qwen（经 :18080 隧道）
```

所以**重启本机 = 短暂影响服务器生产**。禁止在生产时段为了「验证 Boot 触发」而重启本机；必须走低峰窗口 + 人工确认。同理，不要在切换窗口前后安排本机维护。

### 8. 其他已知但未验证的细节

- **镜像钉死文件在切换过程中没有二次校验**：脚本在**切换前**只读一次 `Config.Image` 并生成钉死文件；切换过程中不会再次比对「钉死文件是否被人改过」。→ **首次执行时请先手工抄下镜像名与 imageID，与脚本输出逐一比对。**
- **切换的完整耗时未实测**：只能按各阶段超时上限推算最坏约 7~8 分钟（3010s 健康等待 + 120s `/ready` 等待 + 约 30s 公网轮询）。**2026-09-30 的真实切换实测了其中最关键的一段 —— 阶段 3 改配置 → 阶段 4 重建 → 阶段 5 `/ready` 200，约 20 秒（时间戳跨度 15:14:05 → 15:14:27 为 22 秒，受 10 秒采样粒度限制，统一记为约 20 秒，见第八节第 5 条）**；脚本从启动到退出的端到端总耗时仍无完整记录（含阶段 6 的 20 秒静置与人工确认）。
- **备份目录的清理策略未明确**：`/opt/knowledge-kb-runtime/switch-backup-<时间戳>/` 会随时间累积，长期可能占用磁盘，但删除时机需人工决定（回滚窗口结束前不能删）。
- **Windows 端「nightly-scheduler」计划的启用顺序未演练**：见第六节 —— 必须「先停服务器的 `answer-hub-queue.timer`，再启用本机调度任务」，且必须确认不会两台机器同时拉同一批数据（双跑会产生重复处理）。

## 九、验证方法

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

## 十、待决事项

1. **夜间队列阻塞**：`job-20260926-200330-a33725f4` 已成功处理 995 条，仅 6 条 CZ 同步失败，导致整个队列跳过 17 天（`cursor_date` 停在 `2026-09-13`）。处理方式涉及「是否向 CZ 提交 995 条候选」，需业务决策。
2. **切换窗口**：需与正在部署该环境的同事协调。
3. **服务器本地 Answer Hub 停用**：切换验证稳定后再做，且是「停用不删除」。
