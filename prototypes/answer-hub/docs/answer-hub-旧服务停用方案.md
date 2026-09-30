# Answer Hub 旧服务停用方案

> 状态：**待执行**（**生产切换已于 2026-09-30 15:14 完成并验证通过**，观察期自该时刻起算）。本方案在「切换成功且稳定运行」之后才启用。
> 适用范围：停用**旧服务器**上那份 Answer Hub（`answer-hub-api.service`）。
> 前置文档：[`answer-hub-本机迁移运行手册.md`](./answer-hub-本机迁移运行手册.md)（下称「运行手册」）。

## 零、这份文档解决什么问题

迁移完成后，服务器上会**同时存在两份 Answer Hub**：

```
服务器 /opt/knowledge-kb/prototypes/answer-hub/  →  answer-hub-api.service（旧，0.0.0.0:8780）
本机（新电脑）                                    →  AnswerHub-API-Local（新，100.72.97.89:8780）
                                                    ↑ 经 AnswerHub-Tunnel 暴露为服务器 :18780
```

生产的 `kb-backend` 切换后走**隧道**（`:18780` → 本机）。旧服务这时是「**已不承载流量、但完好可用的退路**」。

停用的目标只有一句话：**把旧服务从「运行中」变成「已停止」，同时确保它随时能被拉起来。**

⚠️ **本方案不含任何删除操作。** 停用 ≠ 退役，更 ≠ 清理。清理是另一件独立的事，见第四节。

### 术语与对象对照

| 对象 | 位置 | 本方案中的角色 |
|---|---|---|
| `answer-hub-api.service` | 服务器 systemd | **被停用对象**（当前 `active`，监听 `0.0.0.0:8780`） |
| `answer-hub-queue.service` | 服务器 systemd | ⚠️ **不碰**（当前 `failed`，属另一待决事项，见第六节） |
| `AnswerHub-API-Local` | 本机计划任务 | 切换后的生产 API，**停用期间必须持续健康** |
| `AnswerHub-Tunnel` | 本机计划任务 | 服务器 `0.0.0.0:18780` → 本机 `8780`，**停用期间必须持续健康** |
| `AnswerHub-Watch` | 本机计划任务 | 每 10 分钟写 `observation.log`，**本方案第 1 节的主要判据来源** |
| `Docker-Embedding-Watchdog` | 本机计划任务 | 每 5 分钟守护 GPU 容器，**不碰** |
| `answer-hub-api.service` 的 8780 端口 | 服务器 | ⚠️ **与隧道端口 18780 不是同一个端口，两者互不影响** |

### 执行前必看的三条铁律

1. **只 `systemctl stop`，不 `disable`，不删任何文件。**
2. **服务器 nginx 是宿主机宝塔 nginx（非容器、反代多站点）→ 不 reload、不 restart。**
3. **本机不只是 GPU 节点**，它承载着服务器生产既有的嵌入链路 → 停用全程不得动本机。

---

## 一、停用前的稳定观察门槛

> **原则**：旧服务是唯一能立刻接管的退路。**只有在新路径被证明稳定之后，才允许把退路从「运行中」降级为「已停止」。** 观察期不达标就往后推，没有例外。

### 1.1 观察起点与时长

| 项 | 要求 |
|---|---|
| 观察起点 | 运行手册第四节「切换与验证」**全部步骤完成、且人工确认「运行监管」页面正常**之后的时刻 |
| **最短连续观察时长** | **连续 7 天（168 小时）无中断** |
| 推荐时长 | 14 天（跨过一个完整工作周 + 一个周末低峰 + 至少一次夜间队列窗口） |
| 「连续」的定义 | 观察期内**不允许出现任何一次「未达标」事件**（见 1.3）。出现过就**从该事件恢复正常的时刻重新起算** |

> ⚠️ **不建议压缩到 7 天以下。** 夜间队列是低频路径（运行手册第六节、第十节），一周内最多只能观察到寥寥数次；观察期短于 7 天，等于**根本没覆盖到夜间路径**就宣布稳定。

### 1.2 观察期必须同时满足的四类证据

| # | 证据 | 判据 | 采集位置 |
|---|---|---|---|
| 1 | **隧道与 API 存活连续性** | 观察期内本机 `observation.log` **无缺口、无 `fail`/`down` 记录** | 本机 |
| 2 | **接口成功率** | `/health`、`/ready`、业务接口 **成功率 100%**（按分钟探测统计） | 服务器侧 + 容器内 |
| 3 | **容器内真实路径可用** | `kb-backend` 容器内经 Docker 网关访问 `:18780` 全程 200 | 服务器 |
| 4 | **真实业务使用痕迹** | 本机 `api-access.log` 持续出现来自 `kb-backend` 的**规律请求**，且用户侧无投诉 | 本机 + 人工确认 |

### 1.3 检查命令与期望输出

#### 1.3.1 本机 `observation.log`（判据 1）

```powershell
# 最近 200 行，人工扫一遍有没有 fail / down / error
Get-Content E:\answer-hub-runtime\observation.log -Tail 200
```

```powershell
# 观察期内一共出现过多少次异常（期望：0）
Select-String -Path E:\answer-hub-runtime\observation.log -Pattern 'fail|down|error|refused|timeout' |
  Measure-Object | Select-Object -ExpandProperty Count
```

```powershell
# 最近一次巡检时间（期望：距今 10 分钟以内）
Get-Item E:\answer-hub-runtime\observation.log | Select-Object LastWriteTime
```

| 期望 | 说明 |
|---|---|
| 异常计数 = **0** | `AnswerHub-Watch` 每 10 分钟写一行，正常行不含 fail/down/error |
| 日志 `LastWriteTime` 距今 **< 10 分钟** | 巡检任务本身在正常跑；超过 10 分钟说明**巡检自己也停了**，此时日志的「无异常」毫无意义 |
| 时间戳**连续无跳空** | 相邻两行间隔应约 10 分钟。出现数小时空档 = 本机曾经休眠/重启/任务停摆，那段时间**没有任何观测**，不能计入观察期 |

> ⚠️ **`observation.log` 只能证明「本机 API 在与否」，证明不了「服务器在调它」。** 判据 4（`api-access.log`）才是有流量经过的证据。两者必须同时看。

##### `observation.log` 字段含义（2026-09-30 生产切换后修订）

每行字段固定、顺序固定：

| 字段 | 实际探测目标 | 生产判据？ |
|---|---|---|
| `local=<code>(<ms>ms)` | 本机 Answer Hub 自检（`100.72.97.89:8780`） | 是（新路径的本机端点，但**单独看它证明不了「生产在用」**，见本节末尾 ⚠️） |
| `rollback=<code>` | **服务器本地旧服务**（`127.0.0.1:8780`） | ❌ **不是**。它红了只说明**退路**没了，生产可能完全正常 |
| `tunnel=<code>` | 服务器 `127.0.0.1:18780`（SSH 反向隧道回环）= 新生产路径入口 | 是 |
| `e2e=<code>` | `kb-backend` **容器内**真实调用本机 `/health` + `/api/v1/automation/control`（两个都 200 才记 `200`） | 是，**最接近「生产是否真的通」的字段** |
| `apiTask=` / `tunTask=` | 本机 `AnswerHub-API-Local` / `AnswerHub-Tunnel` 计划任务状态 | 是 |

> ⚠️ **历史行（2026-09-30 15:22 及之前）该位置字段名是 `prod=`，它探的同样是服务器本地旧服务 `:8780`** —— 只是当时被错误地当成了「生产」。读老行时请一律把 `prod=` 按 `rollback=` 理解；**不要**因为老行写着 `prod=200` 就以为生产走的是服务器本地。
>
> **改名原因**：旧服务停用后 `:8780` 必然不可达（见 4.1）。若继续把它当生产判据，观察期会**从停用那一刻起永久 FAIL，而生产其实完全正常**。这是最难排查的一类误判 —— 监控喊着「生产挂了」，实际挂的只是一条已经不该被使用的退路。
>
> **字段契约如何保证不错位**：`observation.log` 由 `_watch.ps1`（仓库内镜像：`scripts/local-migration/observation-watch.ps1`）写入，由 `_smoke.ps1`（仓库内镜像：`scripts/local-migration/smoke-test.ps1`）第【H】节读取校验。两者已同步为**双格式兼容**：含 `rollback=` 的新行按 `local=200 + rollback=200 + tunnel=200 + e2e=200` 校验，含 `prod=` 的历史行按旧字段名 `local=200 + prod=200 + tunnel=200` 校验。**因此历史行不会因改名被静默漏检，新行也不会因为读旧字段名而被误判为异常。**

```powershell
# 字段分布自检：期望「历史行 prod= 若干 + 新行 rollback=/e2e= 若干」，两者都不应为 0
Select-String -Path E:\answer-hub-runtime\observation.log -Pattern 'rollback=' | Measure-Object | Select-Object -ExpandProperty Count
Select-String -Path E:\answer-hub-runtime\observation.log -Pattern 'prod='     | Measure-Object | Select-Object -ExpandProperty Count
```

```powershell
# 新格式行的健康自检：期望无输出
Select-String -Path E:\answer-hub-runtime\observation.log -Pattern 'rollback=' |
  Where-Object { $_.Line -notmatch 'local=200' -or $_.Line -notmatch 'rollback=200' -or `
                 $_.Line -notmatch 'tunnel=200' -or $_.Line -notmatch 'e2e=200' }
```

#### 1.3.2 四类接口探测（判据 2、3）——在服务器上执行

```bash
# 一次性跑 100 轮，统计各接口成功率与最大延迟（只读，不改任何东西）
KEY=$(grep -E '^ANSWER_HUB_API_KEY=' /opt/knowledge-kb/.env | cut -d= -f2-)
OK=0; BAD=0; MAX=0
for i in $(seq 1 100); do
  CODE=$(curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 15 \
         -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:18780/api/v1/automation/control)
  H=$(echo "$CODE" | awk '{print $1}'); T=$(echo "$CODE" | awk '{print $2}')
  if [ "$H" = "200" ]; then OK=$((OK+1)); else BAD=$((BAD+1)); echo "非 200: $CODE"; fi
  MAX=$(echo "$MAX $T" | awk '{print ($2>$1)?$2:$1}')
  sleep 1
done
echo "成功 $OK / 失败 $BAD / 最大耗时 ${MAX}s"
```

| 探测目标 | 命令 | 期望 |
|---|---|---|
| 本机 API（自检） | `curl -s -o /dev/null -w '%{http_code}\n' http://100.72.97.89:8780/health` | `200` |
| 隧道回环（服务器本地） | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18780/health` | `200` |
| 业务接口（带鉴权） | 见上面循环 | 成功率 **100%**（100/100） |
| 容器内真实路径 | `docker exec kb-backend python -c "import urllib.request; print(urllib.request.urlopen('http://172.18.0.1:18780/health', timeout=15).status)"` | `200` |
| 后端生产就绪 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready` | `200`（⚠️ 不是 `/health`，见运行手册第七节） |

> ⚠️ **不要 `echo $KEY`。** 本仓库是 public，任何被打进日志或终端记录的输出都等于对外公开。上面第 3.3 类探测已经用了 `-H` 传递，不需要打印。

**延迟容忍**：隧道已实测约 **52ms**，业务接口单次耗时应稳定在**秒级以内**。若出现持续数百毫秒以上的劣化或偶发超时，先查隧道与本机负载，**不要带着劣化去停用旧服务**。

#### 1.3.3 真实流量痕迹（判据 4）

```powershell
# 本机收到的请求；期望持续滚动出现来自 kb-backend 的请求
Get-Content E:\answer-hub-runtime\api-access.log -Tail 50
```

> ⚠️ **日志有轮转/容量上限的风险未确认**（见第六节）。若 `api-access.log` 被轮转覆盖，则**无法用文件证明「7 天连续有流量」**。执行时请先确认该文件的保留策略；若保留期不足观察期，必须改用「每次巡检时人工记一笔」的方式补证，不能因为「文件里没看到旧记录」就判定没流量。

### 1.4 ⚠️ 必须暂缓停用的情况（任一命中即推迟）

| # | 情况 | 处置 |
|---|---|---|
| 1 | 观察期**不足 7 天**，或期间出现过一次未达标事件 | 推迟，重新起算 |
| 2 | `observation.log` 出现 **fail / down / error**，或时间戳存在**跳空** | 先查清原因并修复，**重新起算观察期** |
| 3 | 任一接口成功率 **< 100%**，或有非 200 响应 | 先定位（本机 API？隧道？网关？），修好后重新起算 |
| 4 | 容器内访问 `172.18.0.1:18780` **非 200** | ⚠️ 这是切换后的真实路径，它坏了等于生产已经在走降级路径，**立刻停手排查** |
| 5 | `curl http://127.0.0.1:8000/ready` **非 200** | 后端生产本身不健康，此时任何操作都会混淆归因 |
| 6 | 本机计划任务有任何一项非预期状态（见 1.5） | 先修任务；**API 或隧道一旦不健康，旧服务就是唯一退路，绝不能停** |
| 7 | 本机近期有重启 / 休眠 / 蓝屏记录 | 重启后自愈链路**从未验证**（运行手册第八节第 6 条），必须重新起算观察期 |
| 8 | 用户侧有任何未解释的「页面报错 / 数据不对」反馈 | 先查清。**旧数据库停在切换时刻不再更新**（见第六节），此时回滚也救不了数据，贸然停用会丢掉唯一的现场 |
| 9 | ⚠️ 服务器正处于 Codex 代理自动部署窗口内 | 见 1.6 |
| 10 | 夜间队列窗口内（见下方说明） | 改期 |

**关于夜间窗口**：服务器夜间拉取仍在跑（运行手册第十节待决事项），且 `answer-hub-queue.service` 当前 `failed`。**停用期间队列的失败状态会与本次操作混在一起，无法归因。** 建议在**队列处于「非运行时段」**执行停用，并在停用记录里写明当时队列状态。

### 1.5 本机四项计划任务的前置检查

```powershell
Get-ScheduledTask -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' |
  Select-Object TaskName,State,@{n='Last';e={$_.LastRunTime}}
```

```powershell
Get-ScheduledTaskInfo -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' |
  Select-Object TaskName,LastRunTime,LastTaskResult
```

| 任务 | 期望 State | 说明 |
|---|---|---|
| `AnswerHub-API-Local` | `Running` | 长期 `Running` 是正常的（每分钟周期触发 + `MultipleInstances=IgnoreNew`） |
| `AnswerHub-Tunnel` | `Running` | 同上 |
| `AnswerHub-Watch` | `Ready` | 执行完即退出，`LastTaskResult` 应为 `0` |
| `Docker-Embedding-Watchdog` | `Ready` | 同上 |

```powershell
Get-NetTCPConnection -LocalPort 8780 -State Listen |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

期望：至少一条 LISTENING（本机 API 在听 `100.72.97.89:8780`）。

> ⚠️ `LastTaskResult = 2147946720`（`0x800710E0`，ERROR_OPERATION_IN_PROGRESS）出现在显示 `Running` 的任务上是**正常值**，不是报错。详见运行手册第五节第 1 项。

### 1.6 ⚠️ 避开 Codex 代理自动部署窗口

这本手册**不重复展开**检查方法 —— **直接引用运行手册第五节第 9 项**：

- 检查命令：运行手册 **第五节 9.1**（`9.1.1` ~ `9.1.5`，全部只读）
- 判断标准：运行手册 **第五节 9.2**
- 门槛：运行手册 **第五节 9.3** —— 9.1.2 与 9.1.5 **无输出**，且最近一次部署活动距今 **≥ 20 分钟**（**≥ 45 分钟为佳**）
- 撞车处置：运行手册 **第五节 9.4**；盯梢：**第五节 9.5**
- 背景数据（11:09 / 11:25 / 11:51，约 20~40 分钟一轮）：运行手册 **第五节的 9.1 背景说明**与**第七节「切换可能撞上 Codex 代理的自动部署」**

**本方案为什么也要避开**：停用本身**不重建容器**，所以撞车后果轻于切换。但仍有两处真实交集：

1. 停用前后要做的一系列**只读检查**（`docker inspect`、`docker events`）会被部署重建**污染结论** —— 看到 `kb-backend` 刚重启，无法判断是「部署导致的」还是「生产在出问题」。
2. 一旦观察期证据里出现容器重建，**判据 3「容器内真实路径可用」的连续性就断了**，需要重新起算。

> ⚠️ **门槛必须在动手前 5 分钟内重跑一遍。** 这是本方案里**唯一会随时间失效**的检查（运行手册第五节第 9 项已说明原因）。

### 1.7 逐项打勾清单

> 动手前 5 分钟内逐项确认。

| # | 检查项 | 期望 | 通过 |
|---|---|---|---|
| 1 | 观察期连续时长 | ≥ 7 天（推荐 14 天） | ☐ |
| 2 | `observation.log` 异常计数 | 0 | ☐ |
| 3 | `observation.log` 时间戳连续 | 无跳空，最近写入 < 10 分钟 | ☐ |
| 4 | `/health`（本机 API） | 200 | ☐ |
| 5 | `/health`（隧道回环 `:18780`） | 200 | ☐ |
| 6 | 业务接口 100 轮成功率 | 100% | ☐ |
| 7 | 容器内 `172.18.0.1:18780/health` | 200 | ☐ |
| 8 | `kb-backend` `/ready` | 200 | ☐ |
| 9 | 本机四项计划任务 | 如 1.5 表 | ☐ |
| 10 | 本机 8780 在监听 | LISTENING | ☐ |
| 11 | `api-access.log` 有规律请求 | 有 | ☐ |
| 12 | ⚠️ Codex 部署窗口（运行手册 9.3 门槛） | 距今 ≥ 20 分钟无活动 | ☐ |
| 13 | 不在夜间队列运行时段 | 是 | ☐ |
| 14 | 用户侧无未解释反馈 | 是 | ☐ |
| 15 | 服务器 6 个生产容器全部 `Up` | 见第 2.4 节命令 | ☐ |
| 16 | **回滚路径已验证可执行**（第 3.5 节的演练） | 已演练通过 | ☐ |

> ⚠️ **第 16 项是硬门槛。** 没有验证过回滚就停用旧服务，等于把唯一的退路关掉但没确认门还能开。演练方法见第 3.5 节。

---

## 二、停用步骤

### 2.1 为什么是 `stop` 而不是 `disable`

| 操作 | 效果 | 本方案 |
|---|---|---|
| `systemctl stop` | 停止当前运行，**保留自启配置**。重启服务器后服务**会自动起来** | ✅ **采用** |
| `systemctl disable` | 停止 + **移除开机自启**。重启后不会起来 | ❌ 不采用 |
| `rm` 任何文件 | 不可逆 | ❌ **绝对禁止** |

**三个理由：**

1. **`stop` 的恢复成本最低。** 回滚时只需一条 `systemctl start`，无需回忆任何配置。`disable` 多了一个必须记住的「记得再 `enable`」步骤 —— 而这恰恰是故障当下最容易漏掉的。
2. ⚠️ **`disable` 会在「服务器重启」这个场景里让退路静默失效。** 停用后如果服务器自己重启过（系统维护、断电、宿主机迁移），`stop` 的服务会重新变成 `active`，退路自动接回；`disable` 的服务则仍然是停的，而**没有人会收到通知**。后一种状态下你以为「随时可以回滚」，实际回滚时才发现要先 `enable`，在故障压力下多一步就多一分出错余地。
3. **文件是回滚能力的物理载体。** 「停用不删除」的意义在于：代码目录、venv、`.env`、`answer_hub.db` 全部原地保留，所以回滚就是**打开一个开关**，不是一次「重新部署」。一旦删了文件，`systemctl start` 也会失败 —— 到那时回滚就退化成了「从零重建旧服务」，**而那件事从没做过**。

> **一句话**：`stop` 是**可逆的开关**；`disable` 和删除是**逐步把开关拆掉**。本方案只允许可逆动作。

### 2.2 停用前留档（先取证，再动手）

在**任何状态变更之前**记录基线。这些输出是后面「确认没有连带影响」的对照物。

```bash
# 2.2.1 停用前：服务状态与时戳
systemctl status answer-hub-api --no-pager | head -n 15
systemctl show answer-hub-api -p ActiveState -p SubState -p UnitFileState -p ExecMainStartTimestamp

# 2.2.2 停用前：端口占用（谁在听 8780 / 18780）
ss -ltnp | grep -E ':8780|:18780'

# 2.2.3 停用前：6 个生产容器状态（这是第 2.4 节复验的对照基线）
docker ps --format '{{.Names}}\t{{.Status}}' | sort

# 2.2.4 停用前：队列服务状态（另一个待决事项，只记录不处理）
systemctl is-active answer-hub-queue; systemctl is-failed answer-hub-queue

# 2.2.5 停用前：数据文件大小与修改时间（证明「停写时刻」）
ls -l --time-style=full-iso /opt/knowledge-kb/prototypes/answer-hub/answer_hub.db

# 2.2.6 停用前：后端的 Answer Hub 指向（确认确实已切成隧道）
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i answer_hub
```

| 期望 | 说明 |
|---|---|
| `ActiveState=active`、`SubState=running`、`UnitFileState=enabled` | 旧服务当前在跑且开机自启 |
| `:8780` 有 `answer-hub-api` 监听 | 旧服务真实占用 8780 |
| **`:18780` 由 SSH 隧道进程监听** | ⚠️ 与 8780 是**两个独立端口**，停 8780 **不会**释放 18780 |
| 6 个容器全部 `Up` | 且健康状态与运行手册记录一致 |
| `ANSWER_HUB_BASE_URL` / `ANSWER_HUB_API_BASE_URL` 指向 **`:18780`** | 指向 8780 则说明**切换未完成，禁止停用** |

⚠️ 把 2.2.5 的输出（`answer_hub.db` 的大小与 mtime）**抄进停用记录**。它标记了「旧库最后一次被写入」的时刻，是将来判断「这份数据是切换到哪天为止的快照」的唯一凭据。

### 2.3 执行停用

```bash
# 第一步：停用（唯一的状态变更操作）
sudo systemctl stop answer-hub-api
```

**第二步：确认停用成功**

```bash
systemctl is-active answer-hub-api
# 期望：inactive

systemctl is-enabled answer-hub-api
# 期望：enabled  ← ★ 必须是 enabled，这证明自启配置被保留了

ss -ltnp | grep ':8780'
# 期望：无输出（8780 已释放）

curl -s -o /dev/null -w '%{http_code}\n' --max-time 5 http://127.0.0.1:8780/health
# 期望：000（连接被拒绝，这是正确的）
```

| 检查 | ✅ 正确结果 | ⚠️ 异常信号 |
|---|---|---|
| `is-active` | `inactive` | 仍 `active` → 检查是否有 `Restart=` 或路径级守护把它拉起来了 |
| `is-enabled` | **`enabled`** | `disabled` → **误用了 `disable`，立刻 `systemctl enable answer-hub-api` 补回** |
| `ss -ltnp \| grep :8780` | 无输出 | 仍有监听 → 有别的进程占了 8780，查清是谁 |
| 服务单元文件仍存在 | `ls -l /etc/systemd/system/answer-hub-api.service` 有文件 | 文件不在 → 被删了，**立即停止后续步骤并上报** |

> ⚠️ **`curl http://127.0.0.1:8780/health` 返回 `000` 是「停用成功」的证据，不是故障。**
> ⚠️ **服务器侧对 8780 的监控告警（若有）从此刻起会持续报警 —— 这是预期行为**，应在停用记录里登记，并按第 4.1 节处理告警抑制。**不要把「8780 报警」当成回滚信号。**

### 2.4 停用后必须复验的七项

停用影响的是 8780。以下七项**都与 8780 无关，因此必须全部保持正常** —— 任何一项变化都说明停用动作产生了连带影响，**立即按第三节回滚**。

#### (1) 6 个生产容器 —— 绝不可是本次操作的受害者

```bash
docker ps --format '{{.Names}}\t{{.Status}}' | sort
```

| 容器 | 期望 |
|---|---|
| `kb-backend` | `Up ... (healthy)` |
| `kb-embedding-qwen` | `Up` |
| `kb-redis` | `Up` |
| `appeal-exemption-system-api-1` | `Up` |
| `appeal-exemption-system-postgres-1` | `Up` |
| `voc-workbench-mysql-1` | `Up` |

**与 2.2.3 的基线逐行比对**：容器名集合与状态都应完全一致。⚠️ 名单外若多出任何**停止/消失**的容器，立刻查。

```bash
# 交叉验证：容器从未被重建/停止（期望：无输出）
timeout 5 docker events --since 30m --filter 'event=stop' --filter 'event=die' \
  --filter 'event=destroy' --filter 'event=restart' || true
```

#### (2) 后端生产就绪 —— 用户可见能力

```bash
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:8000/ready
# 期望：200

curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 \
     -H "X-Answer-Hub-Key: $KEY" http://127.0.0.1:18780/api/v1/automation/control
# 期望：200
```

> ⚠️ 后端是 `/ready`，**不是 `/health`**（运行手册第七节：`/health` 返回 000 属正常）。

#### (3) 本机嵌入链路 —— 迁移前既有依赖，与本停用完全无关

⚠️ **这条链路是服务器生产的一部分，且 `EMBEDDING_BASE_URL` 指向本机 GPU（经服务器 `:18080` 隧道）。** 停用旧 Answer Hub **绝不能碰到它**。

```bash
# 服务器 :18080 隧道端口在听
ss -ltnp | grep ':18080'

# 容器内真实调用本机 GPU 嵌入（最权威的证据）
docker exec kb-backend python -c "
import os, urllib.request, json
u = os.environ.get('EMBEDDING_BASE_URL','').rstrip('/')
print('EMBEDDING_BASE_URL =', u)
" 2>/dev/null || echo "（若容器内无该变量名，改用 /ready 与检索功能间接验证）"
```

```bash
# 服务器本地退化容器仍在（万一本机不可用时的退路）
docker ps --format '{{.Names}}\t{{.Status}}' | grep kb-embedding || true
```

| 检查 | 期望 |
|---|---|
| `:18080` 在监听 | 有输出 |
| `EMBEDDING_BASE_URL` | 仍指向本机 GPU 隧道，**本次操作不得改变它** |
| 嵌入相关容器 | 状态与停用前一致 |
| 业务侧检索功能 | 实测一次检索/嵌入调用成功（人工做，见第 4.1 节） |

> ⚠️ **`EMBEDDING_BASE_URL` 与 `ANSWER_HUB_BASE_URL` 是两个毫不相干的变量。** 停用 Answer Hub 时**任何**针对 `EMBEDDING_BASE_URL` 的改动都是越界 —— 改坏它 = 直接打断服务器生产的嵌入检索。

#### (4) 隧道仍在工作

```bash
# 隧道端口仍在听，且仍然 200
ss -ltnp | grep ':18780'
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:18780/health
# 期望：200
```

```powershell
# 本机侧：隧道任务仍 Running
Get-ScheduledTask -TaskName 'AnswerHub-Tunnel','AnswerHub-API-Local' |
  Select-Object TaskName,State
```

#### (5) 公网站点正常 —— ⚠️ 只看不动

服务器 nginx 是**宿主机宝塔 nginx，反代多站点**。**绝不 reload、绝不 restart。**

```bash
# 多个站点各探一次（把域名换成实际站点；此处用占位）
for u in https://<站点A域名>/ https://<站点B域名>/ ; do
  printf '%s -> ' "$u"
  curl -s -o /dev/null -w '%{http_code}\n' --max-time 15 "$u"
done
# 期望：全部 2xx / 3xx
```

```bash
# 只读确认：nginx 进程与配置没有被本次操作触碰
ps -eo pid,etime,cmd | grep -E 'nginx' | grep -v grep
ls -l --time-style=full-iso /etc/nginx/nginx.conf
# 期望：进程运行时长 > 本次操作前的时间；配置文件 mtime 早于本次操作
```

> ⚠️ **停用 Answer Hub 完全不需要动 nginx。** 若你发现自己在改 nginx 配置或需要 reload —— **停下来，说明你走错了路**。宝塔 nginx 反代多站点，reload 操作会同时影响所有站点。

#### (6) 本机四项计划任务与自愈能力

```powershell
Get-ScheduledTask -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' |
  Select-Object TaskName,State,@{n='Last';e={$_.LastRunTime}}
```

期望与 1.5 表一致。

#### (7) 日志侧：请求仍在进来

```powershell
Get-Content E:\answer-hub-runtime\api-access.log -Tail 20 -Wait
```

期望：停用后**仍持续**出现来自 `kb-backend` 的请求。这证明流量确实走的是隧道 → 本机，而不是旧服务。

> ⚠️ 业务接口 200 + 容器内可达 + 日志有请求 —— **三者同时成立才叫「流量确实在新路径上」**。只看其中一项都可能被表面现象骗过（例如回滚演练时 SSH 转发把 18780 抢走，接口照样 200，但流量其实回到了服务器本地）。

### 2.5 停用失败的常见原因

| 现象 | 原因 | 处置 |
|---|---|---|
| `stop` 后 `is-active` 仍 `active` | 单元含 `Restart=always`，或另有守护在拉起 | 查 `systemctl show answer-hub-api -p Restart`；**不要连续反复 stop**，先把守护源找出来再决定 |
| 几分钟后服务自己变成 `active` | 存在外部健康检查/守护脚本在自动拉起 | 同上；⚠️ 找到守护后**只停这一个服务的目标**，不要顺手 disable 别的单元 |
| `stop` 报权限/策略错误 | 当前账号权限不足 | 换有权限的账号；**不要绕过**（例如直接 `kill -9`） |
| 8780 停了但业务接口挂 | 说明生产其实还在打 8780 | ⚠️ **立即 `systemctl start answer-hub-api` 恢复**，然后查 `.env` 到底指向哪 —— 这是「切换未真正生效」的信号 |

> ⚠️ **禁止用 `kill -9` 替代 `systemctl stop`。** 绕过 systemd 会留下不一致的状态记录，且会跳过优雅退出（可能损坏 `answer_hub.db`）。

---

## 三、回滚方案

> **回滚目标**：让 `kb-backend` 重新用上「服务器本地那份 Answer Hub」。
> **回滚越快越好**，所以下面给出**两条路径**：一条是标准路径，一条是应急路径。

### 3.1 先弄清一件事：回滚要不要改 `.env`？

这是最容易搞错的地方。**看你的故障属于哪一类：**

| 故障类型 | 症状 | 要不要改 `.env` |
|---|---|---|
| **A. 本机侧故障** | 本机 API 挂了 / 本机重启 / 本机网络断 / GPU 挂了 | ⚠️ **要改**。因为 `kb-backend` 现在指向 `:18780` → 隧道 → 本机，本机坏了这条路就没了 |
| **B. 本机没坏，只是不放心** | 想临时切回旧服务验证一下 | ⚠️ **要改**（或走 3.4 的临时转发） |
| **C. 只是想重新启用旧服务** | 例如服务器重启后你希望旧服务也活着 | **不改**。只需 `systemctl start answer-hub-api` |

> ⚠️ **关键认知：`systemctl start answer-hub-api` 单独一条并不能完成回滚。**
> 因为它只是让旧服务在服务器的 `127.0.0.1:8780` 上重新开始监听，而**生产的 `kb-backend` 此刻看的是 `:18780`**。
> 更危险的是：`:18780` 是**隧道端口**，指向本机。如果你只 start 了旧服务，流量**仍然去本机** —— 你以为回滚了，实际什么都没变。
> **`start` 旧服务只是回滚的必要条件，不是充分条件。**

> ⚠️ **另一个必须知道的陷阱**：`start` 之后旧服务的 `answer_hub.db` 是**切换到停用那一刻的冻结快照**。切换之后的全部新数据都在本机的库里。**回滚回旧服务 = 业务数据回到过去。** 这是数据层面的真实倒退，不是「无感切换」。

### 3.2 标准回滚路径（推荐）

**适用**：本机侧确认不可用，需要把生产切回服务器本地旧服务。
**原理**：还原 `.env` 两行 → 让 `kb-backend` 重新指向服务器本地 `:8780` → 重建容器 → 启动旧服务。

```bash
# ── 第 1 步：找到切换时的备份目录并确认里面有什么 ────────────────
ls -lt /opt/knowledge-kb-runtime/ | grep switch-backup
# 期望：能看到切换时生成的 switch-backup-<时间戳>/

BK=$(ls -dt /opt/knowledge-kb-runtime/switch-backup-* | head -n 1)
echo "使用备份目录: $BK"
ls -l "$BK"
# 期望 3 个文件：env.bak / urls.before.txt / kb-backend.before.json
cat "$BK/urls.before.txt"    # 回滚的权威副本：两行 URL 的切换前取值
```

↓

```bash
# ── 第 2 步：先恢复旧服务（让 8780 重新可用）───────────────────
sudo systemctl start answer-hub-api
systemctl is-active answer-hub-api          # 期望：active
curl -s -o /dev/null -w '%{http_code}\n' --max-time 8 http://127.0.0.1:8780/health
# 期望：200   ← ← ← 这一行不通，就不要再往下走，先修旧服务
```

↓

```bash
# ── 第 3 步：重新启用旧服务（因为前面是 stop 而非 disable，此步通常已满足）──
systemctl is-enabled answer-hub-api
# 期望：enabled（第 2.3 节保留了自启配置，所以这里应该已经是 enabled）
# 若显示 disabled：systemctl enable answer-hub-api
```

↓

```bash
# ── 第 4 步：备份当前 .env（回滚也要留证据）────────────────────
cp /opt/knowledge-kb/.env /opt/knowledge-kb-runtime/env.before-rollback-$(date +%Y%m%d-%H%M%S)
```

↓

```bash
# ── 第 5 步：把 .env 两行改回服务器本地 8780 ────────────────────
# ⚠️ 动手前先看一眼当前值，并用 grep 确认只改这两行
grep -nE '^(ANSWER_HUB_BASE_URL|ANSWER_HUB_API_BASE_URL)=' /opt/knowledge-kb/.env
# 期望（回滚前）：两行都指向 :18780

sed -i 's#^\(ANSWER_HUB_BASE_URL=\).*#\1http://host.docker.internal:8780#'      /opt/knowledge-kb/.env
sed -i 's#^\(ANSWER_HUB_API_BASE_URL=\).*#\1http://172.18.0.1:8780#'           /opt/knowledge-kb/.env

grep -nE '^(ANSWER_HUB_BASE_URL|ANSWER_HUB_API_BASE_URL)=' /opt/knowledge-kb/.env
# 期望（回滚后）：host.docker.internal:8780 / 172.18.0.1:8780
```

> ⚠️ **必须先 `grep` 看一眼再改，改完再 `grep` 一次。** 「改一半」是最坏的结果 —— 两个变量一个 8780 一个 18780，故障表现会变得极难解释。
> ⚠️ 或者直接用权威副本：`cp "$BK/env.bak" /opt/knowledge-kb/.env`。两种方式二选一，**不要混着做**。

↓

```bash
# ── 第 6 步：重建 kb-backend（⚠️ 必须钉死镜像 + --no-build）──────
docker inspect kb-backend --format '{{.Config.Image}}'
# ⚠️ 先读出来，确认带 tag；下面把它钉死

cd /opt/knowledge-kb
docker compose \
  -f docker-compose.yml \
  -f "$BK/compose.pin-image.yml" \
  up -d --force-recreate --no-deps --no-build backend
```

> ⚠️⚠️ **绝不要在这套 compose 上裸跑 `docker compose up`。** 运行手册第七节记载：`backend` 服务用的是 `build:` 而非 `image:`，裸跑 up 会**顺带把生产代码换成当前 checkout 的版本**，本仓库已因此出过事故（alembic 迁移版本缺失导致 `kb-backend` 起不来）。必须带 `--no-build` 并钉死镜像。

↓

```bash
# ── 第 7 步：等健康 + 验证 ──────────────────────────────────────
for i in $(seq 1 30); do
  S=$(docker inspect kb-backend --format '{{.State.Health.Status}}' 2>/dev/null)
  echo "$(date '+%H:%M:%S') health=$S"
  [ "$S" = "healthy" ] && break
  sleep 10
done

curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:8000/ready
# 期望：200

# 确认容器内 URL 已切回 8780
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i answer_hub
# 期望：两个变量都指向 8780

# 确认确实打到服务器本地旧服务（而非隧道）
ss -tnp | grep ':8780' | head
```

↓

```bash
# ── 第 8 步：人工验证业务 ──────────────────────────────────────
# 1) 打开「运行监管」页面，手工操作一遍（这是唯一能证明业务可用的证据）
# 2) 确认页面数据「停在了切换时刻」是预期现象（旧库不再更新），不是新故障
```

### 3.3 ⚠️ 验证回滚成功的三条独立证据

**只满足一条不算成功。** 三条同时成立才算回滚到位：

| # | 证据 | 命令 | 期望 |
|---|---|---|---|
| 1 | 容器内 URL 指向 8780 | `docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' \| grep -i answer_hub` | 两行都含 `8780` |
| 2 | 服务器本地旧服务在响应 | `curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/health` | `200` |
| 3 | **流量确实回到了本地** | 停掉本机 `AnswerHub-API-Local`（或在**隔离环境**中断隧道）后，业务接口**仍应 200** | 仍 `200` |

> ⚠️ **第 3 条是唯一能排除「假回滚」的证据。** 前两条都能在「流量其实还走隧道」的情况下同时成立（隧道还在、本机 API 还活着、旧服务你刚 start 起来了 —— 但没人用旧服务）。要确认流量真的换路了，必须**让新路径不可用再观察**。
> ⚠️ 第 3 条的验证**只能在低峰窗口、且有人值守时做**，因为它会短暂干扰生产。若不能做，就**在回滚记录里明确写下「第 3 条未验证」** —— 不要把未验证当成已验证。

### 3.4 应急快速回滚（不改 `.env`、不重建容器）

**适用**：改 `.env` + 重建容器的路径太慢、或当下不适合重建容器（例如正撞上 Codex 部署窗口）。
**原理**：⚠️ **这是本方案里唯一「不改配置就能换路」的技巧。**
`kb-backend` 指向的是 `:18780`。18780 是**服务器上的一个端口**，由 SSH 隧道占用 —— **谁占着它，流量就去谁那里。** 所以只要把 18780 的转发目标从「本机 8780」改成「服务器本地 8780」，生产就**在完全不改配置、不碰容器**的情况下回到了旧服务。

```bash
# ── 应急路径：三步 ────────────────────────────────────────────

# 1) 启动旧服务
sudo systemctl start answer-hub-api
systemctl is-active answer-hub-api

# 2) 让服务器本地 127.0.0.1:8780 顶上 18780 的位置
#    ⚠️ 前提：隧道进程（占着 18780）必须先退出，否则端口被占用会绑定失败
#       先确认是谁在占 18780
ss -ltnp | grep ':18780'
#       然后停止本机的 AnswerHub-Tunnel 计划任务（本机侧操作），等服务器端口释放
#       确认释放：ss -ltnp | grep ':18780'  → 无输出

# 3) 建立 SSH 本地转发：服务器 18780 → 服务器本地 8780
ssh -N -L 0.0.0.0:18780:127.0.0.1:8780 <SSH_USER>@127.0.0.1
#    ⚠️ 上面这条在服务器上对自身建立转发，等价于「让 18780 指向本地 8780」
```

```bash
# ── 验证 ─────────────────────────────────────────────────────
curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:18780/health
# 期望：200（现在 18780 后面是服务器本地旧服务）

curl -s -o /dev/null -w '%{http_code}\n' --max-time 10 http://127.0.0.1:8000/ready
# 期望：200
# 应用侧无需任何改动 —— 它看到的还是 18780
```

| 应急路径优点 | 应急路径代价 |
|---|---|
| ⚠️ **不改 `.env`、不重建容器** → 不触发镜像重建，不与 Codex 部署撞车 | 服务器上多了一个**手工** SSH 转发进程，**本机重启/网络抖动后不会自愈** |
| 秒级完成（前提是端口已释放） | 遗留状态：事后必须**手工**把它清掉并改回 `.env`，否则配置与现状不符，下次排查会被误导 |
| 可与 3.2 标准路径**先后配合**：先用它止血，再从容走标准路径 | ⚠️ 若 `<SSH_USER>@127.0.0.1` 的免密/密钥不可用，这条路当场就走不通 |

> ⚠️ **应急路径是「止血」不是「治愈」。** 用它恢复服务之后，**必须在同一个维护窗口内补齐 3.2 的标准回滚**（改 `.env` + 重建容器），并把手工转发进程清理掉。否则系统会长期处于「配置写 18780→本机，实际 18780→本地旧服务」的分裂状态 —— 下一个排查的人会被彻底带偏。

### 3.5 ⚠️ 停用前的回滚演练（第 1.7 节第 16 项硬门槛）

**没有演练过就不许停用。** 演练必须在**低峰窗口、有人值守**下进行，且演练本身就是一次**短暂的生产切换**，不是纯只读操作。

| 演练步骤 | 动作 | 通过标准 |
|---|---|---|
| 1 | 按 2.2 留档 | 拿到基线 |
| 2 | 按 2.3 `systemctl stop answer-hub-api` | `inactive` 且 `enabled` |
| 3 | 按 2.4 复验七项 | 全部正常 |
| 4 | 按 **3.4 应急路径**回滚 | 18780 `200`、`/ready` `200` |
| 5 | 按 **3.3 第 3 条**验证流量真的换路 | 停本机 API 后业务仍 200 |
| 6 | 清理应急转发进程，按 **3.2 标准路径**回滚 | 3.3 三条证据齐全 |
| 7 | 确认回到切换后状态（`.env` 指 `:18780`、本机 API 健康） | 与演练前基线一致 |
| 8 | 填写演练记录（时间、耗时、遇到的意外） | 有书面记录 |

> ⚠️ **演练记录要写清「哪条路径实际走通了、耗时多少」。** 第一次在真实故障压力下尝试回滚，和演练过一遍再回滚，是完全不同的两件事。
> ⚠️ **演练必须在切换完成且观察期达标之后做**，不能在切换前做 —— 切换前旧服务是生产，停它等于停生产。

### 3.6 回滚决策速查表

| 现象 | 判断 | 动作 |
|---|---|---|
| 本机 API 挂了，30~60 秒未自愈 | 自愈机制失效 | 先看 `AnswerHub-API-Local` 任务；超 5 分钟未恢复 → **3.2 或 3.4 回滚** |
| 本机重启了 / 断电 | 自愈链路未验证 | **直接回滚**，不要等它「自己起来」 |
| 隧道断了，重连不上 | 18780 不可达 | **回滚**；注意 3.4 需要先释放 18780 |
| 本机 GPU / 嵌入容器挂了 | 影响的是 `EMBEDDING_BASE_URL` 那条链路，与本停用无关 | ⚠️ **不是本方案的回滚场景**；修嵌入链路，**不要改 Answer Hub 配置** |
| 公网站点 502 | 可能是 nginx/CDN/链路，可能与本操作无关 | ⚠️ **先只读排查 nginx 与站点**，**不改 nginx、不 reload**；确认与本操作无关后再看 Answer Hub |
| 业务数据「变旧了」 | 本机库与旧库不同步 | ⚠️ **回滚也解决不了**（旧库停在切换时刻）。这是数据一致性议题，需业务决策，不要用「回滚」当应对 |
| 停用后 8780 监控开始报警 | 预期现象 | ⚠️ **不要据此回滚**；按 4.1 登记并抑制告警 |
| 什么都正常，只是有人很紧张 | — | 不需要回滚。回滚本身有数据倒退代价 |

---

## 四、停用后收尾（只记录，不删除）

### 4.1 记录停用时间与证据

**必须落成书面记录**（建议写进本仓库的运维记录或本项目 issue，⚠️ **不得包含任何密钥值**）：

| 记录项 | 内容 | 从哪里取 |
|---|---|---|
| 停用时刻 | 精确到分钟（含时区） | 执行时手记 |
| 停用前服务状态 | `ActiveState` / `SubState` / `UnitFileState` | 2.2.1 |
| 停用后服务状态 | `is-active` = `inactive`；**`is-enabled` = `enabled`** | 2.3 |
| ⭐ **`answer_hub.db` 的大小与 mtime** | **这是「旧库最后一次写入」的时刻** | 2.2.5 |
| 6 个容器停用前/后对照 | `docker ps` 两次输出 | 2.2.3 / 2.4(1) |
| 隧道端口状态 | `:18780` 仍 200 | 2.4(4) |
| 嵌入链路状态 | `:18080` 可达、`EMBEDDING_BASE_URL` **未被改动** | 2.4(3) |
| 公网站点状态 | 各站点 HTTP 码 | 2.4(5) |
| 观察期结论 | 连续多少天、异常计数、是否达标 | 第 1 节 |
| ⚠️ Codex 部署窗口检查结果 | 最近一次部署活动距今多少分钟 | 运行手册 9.1 ~ 9.3 |
| 队列服务状态 | `answer-hub-queue` 当时是 `failed` 还是别的 | 2.2.4 |
| 回滚演练结论 | 3.5 的演练是否通过、耗时 | 3.5 |
| 已知告警 | 8780 相关告警已从何时开始报警 | 人工登记 |

**另外两件必做的小事：**

```bash
# (a) 保存停用后的服务状态快照（只写日志，不改服务）
systemctl status answer-hub-api --no-pager > /opt/knowledge-kb-runtime/answer-hub-api-stopped-$(date +%Y%m%d-%H%M%S).log
ls -l /opt/knowledge-kb-runtime/answer-hub-api-stopped-*.log
```

```powershell
# (b) 本机侧留一份当时的任务与端口状态
Get-ScheduledTask -TaskName 'AnswerHub-API-Local','AnswerHub-Tunnel',
  'AnswerHub-Watch','Docker-Embedding-Watchdog' | Select-Object TaskName,State
Get-NetTCPConnection -LocalPort 8780 -State Listen | Select-Object LocalAddress,OwningProcess
```

**告警处理**：若存在针对服务器 `127.0.0.1:8780` 的监控/告警，停用后它会**持续报警**。处置方式按你的监控体系来（暂停该规则 / 改判据为 18780），但 ⚠️ **必须在记录里写明做了哪种处置** —— 否则将来没人知道「8780 一直红」是预期还是真故障。

> ⚠️ **停用后必须区分「两种红了」**（字段含义见 1.3.1）：
>
> | 现象 | 含义 | 处置 |
> |---|---|---|
> | `observation.log` 的 `rollback=` 变成 `ERR`；`_smoke.ps1` 的「回滚目标（服务器本地旧服务 :8780）」变 `[FAIL]` | **预期现象** —— 停用后 `:8780` 必然不可达，这两个判据只是用来盯住**退路**是否还在 | ❌ **不要据此回滚**。在停用记录里登记为「退路已按计划停用」 |
> | `tunnel=` 或 `e2e=` 非 `200`（即 `_smoke.ps1` 的「容器内 e2e 生产路径」变 `[FAIL]`） | ⚠️ **真故障** —— **新生产路径**断了 | ✅ **立即按第 3.4 节应急路径处置** |
>
> **一句话**：判断生产是否健康只看 `tunnel=` 与 `e2e=`；`prod=`（历史行）/ `rollback=` 字段与生产无关。

### 4.2 哪些保留不动（全部）

**停用后，以下全部原地保留，一项都不动：**

| 保留对象 | 位置 | 保留理由 |
|---|---|---|
| systemd 单元 | `answer-hub-api.service` | 回滚开关的本体 |
| **单元的自启配置** | `UnitFileState=enabled` | 见 2.1 理由 2：服务器重启后退路自动接回 |
| 代码目录 | `/opt/knowledge-kb/prototypes/answer-hub/` | 回滚的物理载体 |
| 虚拟环境 | 代码目录内 `.venv` | 重建一次 venv 的成本远高于保留 |
| 配置文件 | `prototypes/answer-hub/.env`（root:www 640） | ⚠️ **权限与所有者一律不动**，改了会导致回滚起不来 |
| ⭐ **数据库** | `answer_hub.db`（约 280 MB） | **切换到停用期间的全部历史数据快照**，只此一份 |
| 备份目录 | `/opt/knowledge-kb-runtime/switch-backup-*` | 回滚的权威副本 |
| 停用记录产物 | `answer-hub-api-stopped-*.log` | 证据 |

> ⚠️ **`answer_hub.db` 是「只此一份」的。** 本机当前的库**不是它的副本**，两者是**各自独立积累**的两份数据。删掉服务器这份 = 切换到停用期间的历史数据**永久丢失**，且**本机的数据补不回来**。

### 4.3 观察多久才考虑清理

| 观察期 | 期间要求 | 可以做 |
|---|---|---|
| **停用后 0 ~ 30 天** | 本机路径持续稳定；旧服务保持停止但**随时可启** | ✅ 只做记录与监控。<br>❌ **不做任何清理** |
| **30 ~ 90 天** | 上述继续成立，且**数据侧确认不需要回溯**（见下） | ✅ 可以**开始评估**清理，<br>❌ **仍不执行删除** |
| **≥ 90 天且业务方书面确认** | 见 4.4 全部前提满足 | 可以**另开变更单**评估清理 |

> ⚠️ **30 天是下限，不是建议值。** 理由：旧库是「切换到停用」期间的唯一数据快照；只有等到「本机库已被证明完整覆盖该时段业务、且业务方确认无需回溯」之后，旧库才会从「退路」变成「冗余」。这个判断**不是技术判断，是业务判断**。
> ⚠️ **本方案不给任何清理时间表的承诺。** 上面的天数只是「最早可以考虑」的参考线，**不构成清理授权**。

### 4.4 ⚠️ 清理的前提条件（**本方案不提供删除命令**）

> ⚠️ **本节只列条件，不给命令。** 有意为之：清理是**不可逆**操作，必须由**独立的变更单**承载，并由了解数据归属的人审批。把删除命令写进一份「停用方案」里，等于给执行人递了一把随时可能被顺手使用的刀。

**以下条件必须全部满足，缺一不可：**

| # | 前提 | 为什么 |
|---|---|---|
| 1 | 旧服务已停止**且**停用记录完整（4.1） | 无记录的清理无法追溯 |
| 2 | 本机路径稳定运行 **≥ 90 天** | 覆盖夜间队列等低频路径的多轮周期 |
| 3 | ⭐ **旧库 `answer_hub.db` 的数据归属已澄清**：确认其覆盖时段的数据已在本机库中完整可用，**或**业务方书面确认无需回溯 | 这是唯一真正阻塞清理的条件 |
| 4 | 业务方**书面**确认不再需要旧服务 | 技术不能替业务决定「数据不要了」 |
| 5 | 已确认**没有任何调用方**还在指向旧地址 | 见下面第 6 条的命令 |
| 6 | 已确认 `ANSWER_HUB_BASE_URL` / `ANSWER_HUB_API_BASE_URL` 长期稳定指向 `:18780`，且**不会被任何机制改回** `:8780` | ⚠️ 与前提 5 是不同的问题：一个是「有没有人在调」，一个是「有没有机制会把它改回去」 |
| 7 | 已确认**服务器 nginx 没有遗留指向 8780 的反代配置** | 清理后若有站点仍反代 8780，会变成 502 |
| 8 | 已确认没有**外部系统的定时任务/脚本**在夜间调用旧 API | 夜间失败最不容易被发现 |
| 9 | 回滚窗口已关闭，并且**有替代退路** | 清理后就没有退路了：「本机故障」将无处可退 |
| 10 | 有一份**独立的、经过审批的变更单** | 清理不是本方案的延伸动作 |
| 11 | 清理对象的数据**已归档备份到独立介质**（若前提 3 的结论是「需要保留但不再运行」） | 区分「停用服务」与「销毁数据」 |

**只读核查命令（供前提 5、6、7、8 使用）**：

```bash
# 前提 6：当前生产指向
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i answer_hub

# 前提 6：.env 中的值（只看变量名与端口，不要打印密钥）
grep -nE '^ANSWER_HUB_(BASE_URL|API_BASE_URL)=' /opt/knowledge-kb/.env

# 前提 7：nginx 配置里还有没有 8780 的引用（只读，不 reload）
grep -rn '8780' /etc/nginx/ /www/server/panel/vhost/nginx/ 2>/dev/null | head -n 20

# 前提 5 / 8：还有谁在连接 8780（⚠️ 停用后这里应当本来就很少）
ss -tnp | grep ':8780' | head -n 20
```

> ⚠️ 上面 `grep` 的输出**可能包含域名与路径信息**。本仓库是 public，**贴进文档或 issue 之前先脱敏**（服务器公网地址一律写 `<SERVER_HOST>`）。

---

## 五、⚠️ 绝对不能碰的清单

> **本节是全文档的风险红线。** 执行停用时只做第 2.3 节那一条 `systemctl stop`，其余一切保持原样。

### 5.1 服务器：绝不能影响的容器

| 容器 | 性质 |
|---|---|
| `kb-backend` | 生产后端本体 |
| `kb-embedding-qwen` | ⚠️ 服务器本地嵌入（CPU 嵌入，**万一本机不可用时的退路**） |
| `kb-redis` | 生产缓存 |
| `appeal-exemption-system-api-1` | 另一业务系统 |
| `appeal-exemption-system-postgres-1` | 另一业务系统的数据库 |
| `voc-workbench-mysql-1` | 另一业务系统的数据库 |

```bash
# 停用全程只允许用这条只读命令看它们
docker ps --format '{{.Names}}\t{{.Status}}' | sort
```

- ❌ 不要 `docker stop` / `restart` / `rm` 上面任何一个
- ❌ 不要删除任何**卷（volume）**
- ❌ 不要碰任何 `kb-video-demo-*` 容器或卷
- ❌ 不要碰任何 `voc-workbench-*` 容器或卷
- ❌ 不要在这套 compose 上裸跑 `docker compose up`（见 3.2 第 6 步的警告）

### 5.2 服务器：宿主机宝塔 nginx

| 项 | 规则 |
|---|---|
| nginx 进程 | 它是**宿主机宝塔 nginx，反代多站点**，不是容器 |
| 操作 | ❌ **绝不 `reload`**、❌ **绝不 `restart`**、❌ 不改配置、❌ 不改站点 |
| 原因 | 一个 reload 会影响**所有站点**，而停用 Answer Hub **根本不需要动 nginx** |
| 允许 | ✅ 只读：`ps -eo pid,etime,cmd \| grep nginx`、`curl` 探站点 |

> ⚠️ **如果你发现自己需要改 nginx 才能完成停用，那说明你的方案错了 —— 停下来，回到第 2.3 节。**

### 5.3 ⚠️ 本机：它不是「GPU 节点」，它已经在承载服务器生产

**这是最容易被误伤的区域。** 停用旧 Answer Hub 时，本机侧**一切都不许动**。

```
服务器 kb-backend 的 EMBEDDING_BASE_URL
  → 服务器 :18080 隧道
  → 本机 GPU 容器 kb-embedding-qwen
  ★ 这条依赖在【迁移之前】就存在 —— 与本停用无关，绝不能被本次操作影响
```

| 本机对象 | 规则 |
|---|---|
| `Docker-Embedding-Watchdog` 任务 | ❌ 不停止、不修改 |
| `kb-embedding-qwen` 容器 | ❌ 不停止、不重启 |
| 本机 GPU 服务 `127.0.0.1:8080` | ❌ 不动 |
| `AnswerHub-API-Local` 任务 | ❌ 不动（它是**切换后的生产**） |
| `AnswerHub-Tunnel` 任务 | ❌ 不动（它是**切换后的生产链路**；⚠️ 唯一例外见 3.4 应急回滚，且必须先取得授权） |
| `AnswerHub-Watch` 任务 | ❌ 不动（它是观察证据的唯一来源） |
| 服务器 `.env` 的 `EMBEDDING_BASE_URL` | ❌ **绝不修改**（改它 = 直接打断服务器生产的嵌入检索） |
| 本机重启 / 关机 / 休眠 | ❌ **停用窗口前后都不许**（重启本机会中断服务器生产的嵌入能力） |
| 本机 Docker Desktop | ❌ 不重启 |

> ⚠️ **停用旧 Answer Hub 的正确姿势是「只动服务器上的一条 systemctl stop」。** 如果这次操作里你动了本机的任何东西，说明范围已经失控。

### 5.4 其他

| 对象 | 规则 |
|---|---|
| `answer-hub-queue.service` | ⚠️ **不碰**。当前 `failed`，属另一待决事项（见第六节）。**不要**顺手 `reset-failed` / `restart` / `disable` |
| Codex 代理自动部署机制 | ❌ 不干扰、不删除 `.codex-deploy-*` 目录 |
| `/opt/knowledge-kb/.env`（除回滚时的两行 URL 外） | ❌ 不改其他任何行 |
| `answer_hub.db` | ❌ **不删除、不移动、不修改** |
| 代码目录与 `.venv` | ❌ 不删除、不 git 操作、不重装依赖 |
| systemd 单元的自启配置 | ❌ 不 `disable`（见 2.1） |

---

## 六、本方案未覆盖的风险

> **诚实声明**：本节列出**本方案没有验证过、也没有能力验证**的部分。列出它们不是免责，而是让执行人在动手前知道边界在哪。

| # | 未覆盖的风险 | 影响 | 目前状态 |
|---|---|---|---|
| 1 | ⚠️ **停用后本机成为单点** | 本机宕机 = Answer Hub 整体不可用 | **无 UPS、无冗余，未缓解** |
| 2 | ⚠️ **本机重启的 `Boot` 触发从未验证** | 重启后自愈能力是未知数 | 建任务后本机从未重启过 |
| 3 | ⚠️ **`answer-hub-queue.service` 的 `failed` 与本次停用的关系未厘清** | 归因混淆、夜间路径存在未知缺口 | 未厘清 |
| 4 | 旧库数据是否需要回迁未决策 | 「回滚」会造成业务数据倒退 | 需业务决策 |
| 5 | `api-access.log` 保留策略未确认 | 可能无法证明「长期连续有流量」 | 未确认 |
| 6 | 本机 8780 端口是否有第二份 Answer Hub 在跑 | 回滚/排查时可能查到错的进程 | 未排查 |
| 7 | 服务器是否有会自动拉起 `answer-hub-api` 的守护 | `stop` 可能被静默撤销 | 未排查 |
| 8 | Codex 自动部署是否会重建/影响旧服务 | 未知 | 未确认 |
| 9 | 7 天观察期与成功率 100% 均为**建议阈值**，无历史基线 | 阈值可能过松或过紧 | 无实测基线 |
| 10 | 回滚演练（3.5）本身未被实际执行过 | 演练可能暴露新问题 | 待执行 |

以下逐条展开。

### 1. ⚠️ 停用后本机成为单点（无 UPS、无冗余）

停用旧服务意味着**退路从「随时可用」变成「需要人工操作才能恢复」**。而本机是：

| 单点因素 | 说明 |
|---|---|
| **无 UPS** | 一次停电 = Answer Hub 不可用，**且嵌入链路也一起断**（见 5.3） |
| **无冗余** | 只有一台本机，没有第二台可接管 |
| **家用/办公网络** | 与机房网络相比，抖动、掉线、ISP 故障的概率更高 |
| **Windows 桌面系统** | 会自动更新、可能自动重启、可能休眠 |
| **依赖 GPU 容器 + Docker Desktop** | 任何一环出问题都会波及（运行手册第三节已记录自启链曾经不可靠） |

**关键认知**：⚠️ **停用旧服务并没有「增加」单点风险，它是把原本就存在的单点风险「正式化」了。** 切换那一刻本机就已经是单点；停用只是**移除了人工兜底**。所以这一条真正的含义是：**停用之后，本机的可用性就是 Answer Hub 的可用性上限。**

**建议但本方案未实施**：UPS、第二台备用机、本机自动重启后的完整演练。这些都超出「停用」的范围。

### 2. ⚠️ 本机重启的 `Boot` 触发从未验证

运行手册第三节、第八节第 6 条已记录：四个计划任务都配了 `Boot` 触发，但**建任务后本机从未重启过**（GPU 容器连续运行 21 小时可佐证）。**「本机重启后一切自动恢复」属于假设，不是已验证事实。**

**为什么这条在停用后变得更严重**：

| 停用前 | 停用后 |
|---|---|
| 本机重启后任务没起来 → 可以回滚到旧服务 | 本机重启后任务没起来 → **Answer Hub 直接不可用**，只能人工介入 |
| 退路是「运行中的旧服务」 | 退路是「需要有人记得去 start 的旧服务」 |

**⚠️ 三件不能做的事**：

1. ❌ **不要为了「验证 Boot 触发」在本机生产时段重启。**
2. ❌ **不要把这次重启当成「顺便验证一下」的机会** —— 重启本机会**同时中断服务器生产的嵌入能力**（`EMBEDDING_BASE_URL` 那条链路），影响范围**超出 Answer Hub**。
3. ❌ **不要在停用窗口前后安排本机维护。**

**若必须验证**：走独立的低峰窗口 + 人工授权，并且**先确认服务器的 `kb-embedding-qwen` 退化路径可用**。这件事应当**先于**停用完成 —— 一个「重启后能否自愈」都未知的本机，不适合成为唯一承载。

### 3. ⚠️ `answer-hub-queue.service` 的 `failed` 与本次停用的关系未厘清

**已知**：`answer-hub-queue.service`（夜间队列）当前处于 **`failed`** 状态，是**另一个待决事项**（运行手册第六节、第十节第 1 条：夜间队列被 `job-20260926-200330-a33725f4` 卡住，`cursor_date` 停在 `2026-09-13`，跳过 17 天）。

**未厘清的问题：**

| # | 未厘清 | 为什么重要 |
|---|---|---|
| 1 | `answer-hub-queue.service` 与 `answer-hub-api.service` 是否共享代码目录 / venv / 配置 / 数据库？ | 若共享，**停用 API 是否会连带影响队列的恢复**？反之，将来恢复队列是否会顺带把 API 拉起来？ |
| 2 | 队列的 `failed` 是否**部分由** API 侧原因导致（例如 API 重启过）？ | 若是，停用 API 可能让队列**再也起不来** |
| 3 | 停用 API 后，队列若被人工修复并启动，它会不会**也监听 8780**、与隧道/本机产生冲突？ | 端口冲突 → 可能让流量走到错误的服务上 |
| 4 | 运行手册第六节的「不能两台机器同时跑夜间拉取」是否与停用有关 | 双跑会产生**重复处理**，是数据正确性问题 |
| 5 | 停用后若队列被 `enable` 并运行，它写的是**旧库** → 旧库会不会被重新写入？ | ⚠️ 如果会，那么「旧库冻结在切换时刻」这个前提**不成立**，第 4.1 节的记录会失真 |

> ⚠️ **本方案明确不处理队列。** 但 **停用前应当先读一眼队列的状态与依赖**（`systemctl cat answer-hub-queue`、`systemctl status answer-hub-queue`，**只读**），把上面第 1、2 条的答案记进停用记录。**如果发现队列与 API 共享状态，暂停停用，先把这个关系搞清。**

### 4. 旧库数据是否需要回迁未决策

旧服务的 `answer_hub.db`（约 280 MB）在停用后**停止更新**，成为「切换到停用」期间的快照。本机库是**另一份独立积累的数据**。

**未决策**：

- 这份旧库是否需要与本机库**合并**？如果需要，合并规则是什么（去重依据？时间窗口？冲突取舍？）
- 如果不需要合并，它的**保留期限**是多久（合规/审计要求）？
- ⚠️ **这个决策有截止日期**：一旦按第 4.4 节清理，就永久失去选择权

> ⚠️ **「回滚」不能解决这个问题。** 回滚只是让业务重新读旧库，**并不会把本机的新数据带回去**。回滚 = 业务数据倒退到切换时刻。这是本方案一个重要的、容易被忽略的代价。

### 5. `api-access.log` 保留策略未确认

第 1.3.3 节依赖 `E:\answer-hub-runtime\api-access.log` 证明「观察期内持续有流量」。但该文件**是否轮转、保留多久、有无容量上限，均未确认**。

**风险**：若保留期短于观察期，则**无法用文件证明 7 天连续有流量**，第 1 节的判据 4 会失去数据来源。

**处置**：执行观察期检查时**先确认保留策略**。若不足，改用「每次巡检人工记一笔 + 记入记录表」的方式补证，⚠️ **不能因为「文件里没有旧记录」就判定「当时没流量」**。

### 6. 本机 8780 端口是否有第二份 Answer Hub 在跑

本方案的检查项都假设**本机 `100.72.97.89:8780` 后面就是 `AnswerHub-API-Local` 那一个进程**。若本机还跑着别的 Answer Hub 实例（例如旧的手工启动残留、或另一个端口的实例），判断会被污染。

**风险**：回滚/排查时查到**错的进程**，得出错误结论（运行手册第七节已记录过同类教训：「任何异常信号在下结论前必须用独立方式复验，并优先怀疑测量方法本身」）。

**处置**：停用前执行一次：

```powershell
Get-NetTCPConnection -LocalPort 8780 -State Listen | Select-Object LocalAddress,OwningProcess
Get-Process -Id (Get-NetTCPConnection -LocalPort 8780 -State Listen).OwningProcess |
  Select-Object Id,ProcessName,Path,StartTime
```

确认只有一个预期的进程，且它是计划任务拉起来的那个。

### 7. 服务器是否存在会自动拉起 `answer-hub-api` 的守护

第 2.5 节已列现象（`stop` 后被自动拉回 `active`），但**未排查服务器上是否真的存在这样的机制**。

**风险**：停用后若干分钟服务又变成 `active` —— 执行人可能误判为「停用没生效」，然后**反复 stop**，或错误地改用 `disable`（违反第 2.1 节）。

**处置**：停用后**至少观察 15 分钟**再宣布停用成功。若服务自己变回 `active`：

1. 查 `systemctl show answer-hub-api -p Restart -p RestartSec`
2. 查是否有 systemd timer、cron、或外部守护在拉它
3. ⚠️ **找到守护后只针对这一个服务处理，不要顺手 disable/stop 别的单元**

### 8. Codex 代理自动部署是否会重建/影响旧服务

已知 Codex 代理会**每 20~40 分钟重建 `kb-backend`**（实测 11:09 / 11:25 / 11:51）。**未知**：

- 它的部署脚本是否会**顺带操作 `answer-hub-api.service`**（例如一并重启旧服务）？
- 它是否会**修改 `/opt/knowledge-kb/prototypes/answer-hub/` 下的文件**（代码 / `.env`）？

**风险**：若它会启动/重建旧服务，则**本次停用可能被静默撤销**，而第 4.1 节记录的「停用时刻」失实。

**有利的事实**（来自运行手册第七节）：Codex 部署**也读 `/opt/knowledge-kb/.env`**，所以它不会把 `ANSWER_HUB_BASE_URL` 改回 `:8780`。⚠️ **但那是「配置不会被改回去」，与「服务不会被拉起来」是两件不同的事**，后者仍未确认。

**处置**：停用后**跨过一个部署周期**（≥ 45 分钟）再复验一次 `systemctl is-active answer-hub-api`，确认它没有被部署机制拉起来。

### 9. 观察期与成功率阈值的依据

第 1 节的「7 天」「100% 成功率」「相邻巡检间隔 10 分钟」都是**按运行手册的已知事实与业务节奏推导的建议值，没有历史基线数据支撑**。

| 阈值 | 依据 | 局限 |
|---|---|---|
| 7 天 | 覆盖一个完整工作周 + 至少一次夜间窗口 | 未实测「7 天足够」；夜间队列低频，7 天可能只碰到 7 次 |
| 成功率 100% | 内部链路（隧道 52ms）应当稳定 | ⚠️ **未测过基线**；若环境本来就有偶发波动，这条会导致观察期永远不达标 |
| 巡检间隔 10 分钟 | `AnswerHub-Watch` 的实际配置 | ⚠️ 10 分钟的采样粒度会**漏掉 30~60 秒级的自愈事件**（自愈成功时日志可能看不出异常） |

> ⚠️ **若发现阈值导致观察期无法达标，应先怀疑阈值本身，而不是放宽它。** 运行手册第七节明确写过这条教训：「任何异常信号在下结论前必须用独立方式复验，并优先怀疑测量方法本身」。

### 10. 回滚演练（3.5）尚未执行

第 3.5 节的演练是**本方案自己提出的硬门槛**（第 1.7 节第 16 项），但**尚未执行过**。这意味着：

- 3.2 标准回滚路径的**实际耗时未知**（容器重建 + 等 healthy，可能数分钟）
- 3.4 应急路径的**前提条件未验证**（SSH 免密是否可用、端口释放是否顺畅）
- 3.3 第 3 条「验证流量真的换路」的操作方式**未演练**

> ⚠️ **在演练通过之前，不要执行停用。** 这与运行手册第八节第 1 条「脚本的回滚路径从未被执行过」是同一类问题 —— **第一次在真实故障下执行回滚，和演练过再执行，风险完全不同。**

---

## 附录 A：停用操作速查（一页版）

```bash
# ═══ 停用前（全部只读）═══════════════════════════════════════
systemctl is-active answer-hub-api              # active
systemctl is-enabled answer-hub-api             # enabled
ss -ltnp | grep -E ':8780|:18780'
docker ps --format '{{.Names}}\t{{.Status}}' | sort      # 抄下基线
ls -l --time-style=full-iso /opt/knowledge-kb/prototypes/answer-hub/answer_hub.db   # ★ 抄下 mtime
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i answer_hub   # 必须指 :18780
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready                 # 200
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18780/health               # 200
# ⚠️ Codex 部署窗口：按运行手册第五节 9.1 ~ 9.3，距今 ≥ 20 分钟无活动

# ═══ 停用（唯一的状态变更）═══════════════════════════════════
sudo systemctl stop answer-hub-api

# ═══ 停用后确认（全部只读）═══════════════════════════════════
systemctl is-active answer-hub-api              # inactive
systemctl is-enabled answer-hub-api             # ★ enabled（必须是 enabled）
ls -l /etc/systemd/system/answer-hub-api.service # 文件仍在
ss -ltnp | grep ':8780'                         # 无输出

# ═══ 复验七项：都不许受影响 ══════════════════════════════════
docker ps --format '{{.Names}}\t{{.Status}}' | sort                       # (1) 与基线一致
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready      # (2) 200
ss -ltnp | grep ':18080'                                                  # (3) 嵌入链路仍在
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18780/health    # (4) 200
curl -s -o /dev/null -w '%{http_code}\n' https://<站点域名>/              # (5) 2xx/3xx（⚠️ 不动 nginx）
# (6) 本机四项计划任务状态正常（PowerShell）
# (7) 本机 api-access.log 仍持续有 kb-backend 请求

# ═══ 观察 15 分钟后再确认一次（防被守护静默拉起）═════════════
sleep 900; systemctl is-active answer-hub-api    # 仍期望 inactive
```

## 附录 B：回滚速查（一页版）

```bash
# ── 路径 1：标准回滚（改 .env + 重建容器）────────────────────
BK=$(ls -dt /opt/knowledge-kb-runtime/switch-backup-* | head -n 1)
cat "$BK/urls.before.txt"

sudo systemctl start answer-hub-api
systemctl is-active answer-hub-api                                # active
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8780/health   # ★ 200，否则先修旧服务

cp /opt/knowledge-kb/.env /opt/knowledge-kb-runtime/env.before-rollback-$(date +%Y%m%d-%H%M%S)
sed -i 's#^\(ANSWER_HUB_BASE_URL=\).*#\1http://host.docker.internal:8780#' /opt/knowledge-kb/.env
sed -i 's#^\(ANSWER_HUB_API_BASE_URL=\).*#\1http://172.18.0.1:8780#'      /opt/knowledge-kb/.env
grep -nE '^(ANSWER_HUB_BASE_URL|ANSWER_HUB_API_BASE_URL)=' /opt/knowledge-kb/.env   # ★ 两行都 8780

cd /opt/knowledge-kb
docker compose -f docker-compose.yml -f "$BK/compose.pin-image.yml" \
  up -d --force-recreate --no-deps --no-build backend
# ⚠️ 必须 --no-build + 钉死镜像，绝不裸跑 up

for i in $(seq 1 30); do
  S=$(docker inspect kb-backend --format '{{.State.Health.Status}}' 2>/dev/null)
  echo "$(date '+%H:%M:%S') health=$S"; [ "$S" = "healthy" ] && break; sleep 10
done
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/ready              # 200
docker inspect kb-backend --format '{{range .Config.Env}}{{println .}}{{end}}' | grep -i answer_hub  # 都含 8780

# ── 路径 2：应急（不改 .env、不重建容器，秒级止血）──────────
sudo systemctl start answer-hub-api
# 先让本机隧道退出并确认 18780 已释放
ss -ltnp | grep ':18780'        # 需先变成无输出
# 再把 18780 指向服务器本地 8780
ssh -N -L 0.0.0.0:18780:127.0.0.1:8780 <SSH_USER>@127.0.0.1
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:18780/health   # 200
# ⚠️ 止血后必须在同一窗口内补做路径 1，并清掉这个手工转发进程

# ── 回滚成功的三条独立证据（缺一不可）───────────────────────
# 1) 容器内两个变量都含 8780
# 2) 服务器本地 8780/health = 200
# 3) 新路径不可用时业务仍 200（真正证明流量换路了）
```

## 附录 C：关键事实速查

| 事实 | 值 |
|---|---|
| 被停用对象 | 服务器 systemd `answer-hub-api.service`（`active`，监听 `0.0.0.0:8780`） |
| ⚠️ 停用方式 | **`systemctl stop`；不 `disable`；不删文件** |
| 隧道端口 | 服务器 `0.0.0.0:18780` → 本机 `8780`，约 52ms |
| ⚠️ 端口关系 | **8780（旧服务）与 18780（隧道）是两个独立端口，停止 8780 不影响 18780** |
| 本机 API | `100.72.97.89:8780`，任务 `AnswerHub-API-Local`，自愈 30~60 秒 |
| ⚠️ **本机还承载的既有依赖** | 服务器 `kb-backend` 的 `EMBEDDING_BASE_URL` → 本机 GPU（经服务器 `:18080`），**迁移前就存在** |
| 旧代码 | `/opt/knowledge-kb/prototypes/answer-hub/`（venv：同名目录下 `.venv`） |
| 旧配置 | `prototypes/answer-hub/.env`（**root:www 640**） |
| 旧数据 | `answer_hub.db`（约 280 MB）——**切换前全部历史数据的唯一一份快照** |
| 队列服务 | `answer-hub-queue.service`：**`failed`**，另一待决事项，**不碰** |
| ⚠️ 服务器 nginx | **宿主机宝塔 nginx，反代多站点 → 不 reload、不 restart** |
| 6 个生产容器 | `kb-backend`、`kb-embedding-qwen`、`kb-redis`、`appeal-exemption-system-api-1`、`appeal-exemption-system-postgres-1`、`voc-workbench-mysql-1` |
| ⚠️ 禁止触碰 | 任何 `kb-video-demo-*`、任何 `voc-workbench-*` 容器与卷 |
| ⚠️ Codex 自动部署 | 约 20~40 分钟重建一次 `kb-backend`（实测 11:09 / 11:25 / 11:51）；检查方法见运行手册第五节第 9 项 |
| 观察门槛 | 连续 ≥ 7 天（推荐 14 天），异常计数 0，接口成功率 100% |
| 最短保留期 | 停用后 **≥ 30 天**不做任何清理；清理另开变更单 |

## 附录 D：与运行手册的对应关系

| 本方案 | 引用/依赖运行手册 |
|---|---|
| 1.6 避开 Codex 部署窗口 | **第五节第 9 项**（9.1 命令 / 9.2 标准 / 9.3 门槛 / 9.4 处置 / 9.5 盯梢） |
| 1.3.2 `/ready` 而非 `/health` | **第七节**「`kb-backend` 没有 `/health` 端点」 |
| 2.4(3) 本机不只是 GPU 节点 | **第三节**「⚠️ 本机不只是『GPU 节点』」；**第八节第 7 条** |
| 3.2 第 6 步 必须 `--no-build` + 钉镜像 | **第七节**「`docker compose up` 误重新构建镜像」 |
| 3.2 第 1 步 备份目录的三个文件 | **第五节第 7 项** |
| 4.1 告警登记 / 6.2 重启从未验证 | **第三节**「未验证的路径：本机重启」；**第八节第 6 条** |
| 6.3 队列的 failed | **第六节**「⚠️ 不能两台机器同时跑」；**第十节第 1、3 条** |
| 全局「下结论前先复验测量方法」 | **第七节**「临时探测工具本身也会出错」 |
| 全局 公网地址脱敏为 `<SERVER_HOST>` | **第七节**「公开仓库里服务器地址必须脱敏」 |
| 全局 PowerShell 中文脚本用 pwsh.exe | **第七节**「PowerShell 5.1 解析中文脚本」 |

---

> **执行前最后一句话**：本方案的全部内容可以浓缩成一句 —— **只做那一条 `systemctl stop`，别的一律不动；没演练过回滚，就不要停用。**
