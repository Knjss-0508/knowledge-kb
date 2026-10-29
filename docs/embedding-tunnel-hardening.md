# 嵌入隧道加固：端口占用与会话回收（2026-10-29）

## 1. 三个端口，三种持有者

生产服务器上同时存在三条反向隧道：

| 端口 | 用途 | 持有方式 |
| --- | --- | --- |
| `18080` | 嵌入服务（Qwen/GPU） | 本机 `kb-embedding-tunnel` 容器 |
| `18780` | Answer Hub API | Windows 计划任务 `AnswerHub-Tunnel` 常驻 ssh |
| `19000` | 模型网关 | Windows 计划任务 `ModelGateway-Tunnel` 常驻 ssh |

`18780` / `19000` 的常驻会话存活超过 1.5 小时属正常；容器侧的 `18080` 会话
每次容器重建都会重新绑定。

## 2. 为什么会 `Address already in use`

远端 sshd 当前配置：

```text
clientaliveinterval 0     ← 服务端从不主动探测
clientalivecountmax 3
tcpkeepalive yes
gatewayports clientspecified
```

`ClientAliveInterval=0` 意味着**服务端永远不会主动回收失联会话**。当 GPU 节点
侧的 ssh 进程被强制结束、或中间网络静默断开时：

1. 客户端进程已经没了；
2. 服务端会话仍处于半开状态，继续持有 `0.0.0.0:18080`；
3. 新隧道绑定同一端口时报 `bind: Address already in use`；
4. `ExitOnForwardFailure=yes` 让新连接立刻退出，容器只能反复重启碰运气。

2026-10-29 10:51 的故障就是这样发生的：容器 `RestartCount` 达到 2500，
后端同期出现 2203 条 `Embedding unavailable`。

## 3. 已做的改动

### 3.1 客户端：重连循环真正生效（PR #245）

隧道脚本原本以 `set -e` 开头，`ssh` 断开返回非 0 会直接终止脚本，
`while true` 重连循环从未执行（日志里「连接结束」出现 0 次）。改为 `set -u`
并加指数退避（5→60 秒，连接存活 60 秒以上则重置）。

### 3.2 客户端：探测脚本不再产生挂死进程

`E:\answer-hub-runtime\_watch.ps1` 的探测 ssh 增加
`ServerAliveInterval=15`、`ServerAliveCountMax=2`，并把 stdin 置空。
此前每 10 分钟跑一次、连接静默断开后永不退出，累计 94 个孤儿进程（最早
2026-10-19）。

### 3.3 服务端：按端口清理残留会话

新增 `scripts/cleanup-stale-tunnel-sessions.sh`：

```bash
# 默认只诊断，不改动任何进程
./scripts/cleanup-stale-tunnel-sessions.sh

# 清理「已丢失转发端口」的 sshd 子进程（需人工确认后再执行）
./scripts/cleanup-stale-tunnel-sessions.sh --force

# 只处理一个端口
./scripts/cleanup-stale-tunnel-sessions.sh --port 18080
```

**不要用「该端口有无 ESTABLISHED 连接」判断会话死活**——空闲的健康隧道本来
就没有连接，按这个条件会把正常的 `18780` / `19000` 误判成残留（2026-10-29
实测踩到）。脚本因此默认只输出诊断信息；`--force` 也只清理「已经不在任何
转发端口监听列表里」的 sshd 子进程，并显式跳过 `:22` 主监听进程。

## 3.4 服务端：主动回收失联会话（已执行）

新增 `/etc/ssh/sshd_config.d/99-kb-tunnel-keepalive.conf`：

```text
ClientAliveInterval 30
ClientAliveCountMax 3
```

效果：90 秒内没有响应的会话会被服务端主动断开并释放端口，从根上消除残留占用，
不再依赖容器反复重启碰运气。

执行顺序（实际采用）：

1. 写入配置后用 `sshd -t` 校验（`SSHD_CONFIG_OK`）；
2. `systemctl reload sshd`（reload 而非 restart，不影响已有会话）；
3. 用新连接验证可登录（`NEW_CONNECTION_OK`）；
4. 验证三条隧道：`18080`/`18780`/`19000` 各一个监听，`18780` 返回 200，
   `19000` 返回 404（正常），后端 `embed_texts` 返回 1024 维、55 ms。

## 5. 验收清单

| 检查 | 期望 |
| --- | --- |
| `docker inspect kb-embedding-tunnel --format '{{.RestartCount}}'` | 不再增长 |
| `ss -lntp \| grep -E ':18080\|:18780\|:19000'` | 三个端口各一个监听 |
| 后端 `embed_texts(["x"])` | 返回 1024 维，耗时 < 200 ms |
| `E:\answer-hub-runtime\observation.log` 最新行 | `tunnel=200 e2e=200` |
| Windows `ssh.exe` 中匹配 `8780/health` 的进程数 | 0 |
