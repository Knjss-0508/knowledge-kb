# GPU 嵌入节点部署（模型在本机、后端与数据库在远端）

本文档描述一种**分离式部署形态**：GPU 机器只运行嵌入模型，应用后端、数据库和网站入口
全部保留在原服务器上。适用于「希望用本地显卡加速嵌入，但不愿迁移数据库和线上入口」的场景。

---

## 1. 架构

```
GPU 节点（本地电脑，RTX 4060）           应用服务器（原服务器）
┌────────────────────────┐              ┌──────────────────────────────────┐
│ embedding-qwen         │              │ 0.0.0.0:18080                    │
│ GPU / float16 / 1024维 │◄─ SSH 反向 ──│      ▲                           │
│                        │   隧道        │      │                           │
│ embedding-tunnel       │  (TCP 22)    │ kb-backend                       │
│  （本文件定义的容器）   │              │ EMBEDDING_BASE_URL=              │
└────────────────────────┘              │   http://host.docker.internal:18080/v1
                                        │      │                           │
                                        │ 宝塔 PostgreSQL（同机，1ms）      │
                                        │      │                           │
                                        │ nginx → 域名 + 证书（不变）       │
                                        └──────────────────────────────────┘
```

关键点：

- **数据库不动**，后端与数据库同机，单次查询约 **1ms**（对比跨公网访问的数百毫秒）。
- **网站不动**，域名、证书、nginx 配置全部保持原样。
- **GPU 节点不跑数据库**，只跑嵌入容器 + 隧道容器。
- 嵌入调用由后端经隧道转发到 GPU 节点，**无需重新向量化**（前提是模型与维度一致）。

---

## 2. 为什么用 SSH 反向隧道，而不是 VPN

在真实链路上实测（中国境内 GPU 节点 ↔ 云服务器）：

| 路径 | TCP 握手延迟 |
|---|---|
| 直连公网 `:22` | **32 ms** |
| 直连公网 `:443` | **32 ms** |
| 经 Tailscale DERP 中继 | **351 ms** |

直连失败的根因（实测确认）：

1. GPU 节点位于**对称型 NAT** 之后（Tailscale `netcheck` 报告 `MappingVariesByDestIP: true`）。
2. 云服务器在 EIP NAT 之后，且**安全组阻断全部入站 UDP**
   （从 GPU 节点公网 IP 向 `:41641` / `:443` / `:41642` 发送 UDP 探测包，
   服务器侧 `tcpdump` 入站捕获为 **0 包**）。

两侧都无法被对方主动连入，因此 Tailscale/WireGuard 这类以 UDP 为主的方案只能长期走中继。
**由 GPU 节点主动向应用服务器建立 SSH 连接**（TCP 22 通常已开放）可同时绕开这两个限制，
既不需要改动云安全组，也不需要开放任何入站端口。

---

## 3. 前提条件

| 项目 | 要求 |
|---|---|
| 模型 | 两侧 `EMBEDDING_MODEL` 与 `EMBEDDING_DIMENSIONS` **必须一致** |
| 应用服务器 sshd | `/etc/ssh/sshd_config` 设置 `GatewayPorts clientspecified` 后 reload |
| SSH 密钥 | GPU 节点生成密钥对，公钥写入应用服务器 `~/.ssh/authorized_keys` |
| 应用服务器端口 | `EMBEDDING_TUNNEL_REMOTE_PORT`（默认 18080）未被占用 |
| GPU 节点 | Docker 可用；GPU 场景需 `docker-compose.embedding-gpu.yml` |

### 为什么必须设置 `GatewayPorts clientspecified`

默认 `GatewayPorts no` 时，`ssh -R` 只能把转发端口绑定到**回环地址**，
应用服务器上的后端容器无法通过 `host.docker.internal` 访问它。
设为 `clientspecified` 后允许客户端指定绑定地址。

> 该选项允许任何已认证的 SSH 客户端绑定非回环地址。
> 生产环境建议配合 `Match User` 限制到专用账号，并仅向该账号分发密钥。

### 修改 sshd 的注意事项

`sshd_config` 采用**首个匹配生效**规则，且 `Include /etc/ssh/sshd_config.d/*.conf`
位于文件前部。因此：

- 追加设置时要确保文件**以换行结尾**，否则新行会与最后一行粘连成无效指令
  （`sshd -t` 可能因该行被识别为「已废弃选项」而**不报错**，导致配置静默失效）。
- 追加后必须用 `sshd -T | grep -i gatewayports` 确认生效值为 `clientspecified`，
  不能只看 `sshd -t` 是否通过。

---

## 4. 部署步骤

### 4.1 应用服务器

```bash
# 1) 开启 GatewayPorts 并确认生效
cp /etc/ssh/sshd_config /root/sshd_config.bak-$(date +%Y%m%d-%H%M%S)
[ -n "$(tail -c 1 /etc/ssh/sshd_config)" ] && echo >> /etc/ssh/sshd_config
printf '\nGatewayPorts clientspecified\n' >> /etc/ssh/sshd_config
sshd -t && systemctl reload sshd
sshd -T | grep -i gatewayports        # 必须输出 clientspecified

# 2) 安装 GPU 节点的公钥（内容由 GPU 节点提供）
mkdir -p ~/.ssh && chmod 700 ~/.ssh
echo '<GPU 节点的公钥>' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys

# 3) 切换后端嵌入地址（端口需与 EMBEDDING_TUNNEL_REMOTE_PORT 一致）
cp /opt/knowledge-kb/.env /root/env.bak-$(date +%Y%m%d-%H%M%S)
sed -i 's|^EMBEDDING_BASE_URL=.*|EMBEDDING_BASE_URL=http://host.docker.internal:18080/v1|' \
  /opt/knowledge-kb/.env

# 4) 重建后端（compose 组合以实际部署为准）
cd /opt/knowledge-kb && docker compose up -d backend

# 5) 确认后端仍健康（/ready 内部会探测嵌入服务）
curl -s http://127.0.0.1:8000/ready
```

> 切换前请先确认隧道已经建立（见 4.3），否则后端会因嵌入不可用而启动失败。

### 4.2 GPU 节点

```powershell
# 1) 生成专用密钥对（若尚未生成）
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\id_ed25519_kb_tunnel" -N '""' -C "kb-embedding-tunnel"
# 把 .pub 内容交给应用服务器写入 authorized_keys

# 2) 在 .env 中填写隧道配置
#    EMBEDDING_TUNNEL_SSH_HOST=<应用服务器地址>
#    EMBEDDING_TUNNEL_KEY_PATH=<私钥绝对路径>
#    其余保持默认

# 3) 启动嵌入服务与隧道
docker compose -f docker-compose.yml `
  -f docker-compose.embedding-gpu.yml `
  -f docker-compose.embedding-tunnel.yml `
  up -d embedding-qwen embedding-tunnel
```

### 4.3 验证

```bash
# 应用服务器侧：隧道端口应处于监听状态
ss -tlnp | grep 18080

# 应用服务器侧：经隧道访问嵌入服务
curl -s http://127.0.0.1:18080/health

# 应用服务器侧：后端容器内的真实调用路径
docker exec kb-backend python -c "
import json, urllib.request
body = json.dumps({'model':'Qwen/Qwen3-Embedding-0.6B','input':['隧道验证']}).encode()
req = urllib.request.Request('http://host.docker.internal:18080/v1/embeddings',
                             data=body, headers={'Content-Type':'application/json'})
with urllib.request.urlopen(req, timeout=30) as r:
    print('维度 =', len(json.load(r)['data'][0]['embedding']))
"
```

**决定性验证**：停掉应用服务器本机的嵌入容器后，嵌入调用仍应正常返回。
若仍正常，说明确实走的是 GPU 节点，而不是本机服务。

```bash
docker stop kb-embedding-qwen
docker exec kb-backend python -c "
from app.services.embedding import embed_texts
print('维度 =', len(embed_texts(['GPU 节点验证'])[0]))
"
```

### 4.4 释放应用服务器资源

确认切换成功后，可停用应用服务器上的 CPU 嵌入容器：

```bash
docker stop kb-embedding-qwen
```

参考收益（实测）：CPU 嵌入容器占用 **123.58% CPU / 3.17 GiB 内存**，
停用后应用服务器可用内存由 4.7 GiB 提升到 7.7 GiB。

> 注意：该容器由 compose 文件定义，后续执行 `docker compose up` 时可能被重新创建。

---

## 5. 回滚

```bash
# 应用服务器
sed -i 's|^EMBEDDING_BASE_URL=.*|EMBEDDING_BASE_URL=http://embedding-qwen:80/v1|' /opt/knowledge-kb/.env
docker start kb-embedding-qwen
cd /opt/knowledge-kb && docker compose up -d backend
curl -s http://127.0.0.1:8000/ready
```

恢复时间约 1 分钟。原 `.env` 备份位于切换前创建的 `env.bak-*`。

---

## 6. 容灾与运维

### 隧道自愈

`embedding-tunnel` 采用**双重保障**：

- 容器内 `while true` 循环：SSH 断开后 5 秒重连；
- `restart: unless-stopped`：容器退出后由 Docker 重启。

实测结果：

| 场景 | 恢复情况 |
|---|---|
| 杀掉容器内 ssh 进程 | 内置循环 5 秒后重连 |
| 整容器重启 | 25 秒内自动恢复，后端 `/ready` 正常 |

### 看门狗（自动恢复）

`scripts/watchdog-gpu-embedding.ps1` 负责在引擎或容器掉线后自动恢复：

1. `docker info` 不通 → 执行 `docker desktop start` 并等待引擎就绪（默认最多 300 秒）；
2. 检查 `kb-embedding-qwen` 与 `kb-embedding-tunnel` 是否 `running`；
3. 有不健康项时执行 `docker compose up -d embedding-qwen embedding-tunnel`；
4. 结果写入 `logs/gpu-embedding-watchdog.log`（自动截断，最多 2000 行）。

注册为计划任务：

```powershell
$scriptPath = "<仓库绝对路径>\scripts\watchdog-gpu-embedding.ps1"
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$scriptPath`""
$trigLogon  = New-ScheduledTaskTrigger -AtLogOn
$trigRepeat = New-ScheduledTaskTrigger -Once -At (Get-Date) `
  -RepetitionInterval (New-TimeSpan -Minutes 5)
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries `
  -DontStopIfGoingOnBatteries -StartWhenAvailable -MultipleInstances IgnoreNew `
  -ExecutionTimeLimit (New-TimeSpan -Hours 1)
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
  -LogonType Interactive -RunLevel Highest

Register-ScheduledTask -TaskName "KB-GPU-Embedding-Watchdog" `
  -Action $action -Trigger $trigLogon, $trigRepeat `
  -Settings $settings -Principal $principal -Force
```

实测：

| 场景 | 结果 |
|---|---|
| 两个容器都正常 | 1.3 秒内退出，退出码 0 |
| 手动停掉隧道容器 | 17 秒内自动恢复并确认 running |

### 必须注意的运维风险：无人登录时无法恢复

**GPU 节点重启后若无人登录，Docker Desktop 不会启动。** 看门狗的计划任务
以 `LogonType Interactive` 注册（不保存密码），因此**在没有交互式会话时也无法运行**。

此时的表现：

- 网站浏览、数据库查询、媒体访问**仍然正常**；
- **知识导入、向量化，以及依赖查询词嵌入的召回会失败**（查询向量缓存在进程内，
  重复的历史查询可能仍命中缓存，容易掩盖问题）。

缓解措施（按可靠性排序）：

1. **为 GPU 节点启用 Windows 自动登录**——最直接有效，重启后自动进入会话，
   Docker Desktop 与看门狗随之启动。
2. 评估把 Docker Desktop 改为系统级启动（需自行验证该版本是否支持无会话启动）。
3. 至少部署**外部告警**：在应用服务器上定时检查隧道端口，异常时通知值班人员。

### 日常检查

```bash
# 应用服务器
ss -tlnp | grep 18080                 # 隧道端口
curl -s http://127.0.0.1:8000/ready   # 后端 + 嵌入链路
docker logs kb-backend --since 10m | grep -iE 'error|timeout'
```

```powershell
# GPU 节点
docker ps --filter name=kb-embedding-tunnel --format '{{.Status}}'
docker logs kb-embedding-tunnel --tail 20
Get-Content logs\gpu-embedding-watchdog.log -Tail 20
schtasks /query /tn KB-GPU-Embedding-Watchdog /v /fo LIST
nvidia-smi --query-gpu=memory.used,utilization.gpu --format=csv
```

---

## 7. 安全说明

- 私钥只存放在 GPU 节点，**不得提交到 Git**；`.env` 已在 `.gitignore` 中。
- 隧道是加密的，且**不需要向公网开放任何入站端口**；数据库端口自始至终没有暴露。
- 应用服务器 sshd 的 `GatewayPorts clientspecified` 建议用 `Match User` 限制到专用账号。
- 若曾为其它方案（如 VPN）临时放行过数据库来源 IP，切换完成后应移除对应
  `pg_hba.conf` 规则以减少攻击面。

---

## 8. 与整体部署文档的关系

| 文档 | 适用形态 |
|---|---|
| `docs/deploy.md` | 单机部署（前后端、模型、数据库同机） |
| 本文档 | 分离部署（模型在本机，后端与数据库在远端） |
| `docs/server-deployment-boundary.md` | 服务器部署边界约束 |

选择依据：如果**不希望迁移数据库和线上入口**，用本文档的形态；
如果需要把后端也迁到 GPU 节点，则数据库访问会跨公网，务必先解决链路延迟问题
（本文档第 2 节的实测数据可直接参考）。
