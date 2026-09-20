# 自适应部署

部署脚本会同时选择数据库模式和 Qwen3 运行模式。

> `scripts/deploy.sh` 和 `scripts/deploy.ps1` 用于首次安装或经过评审的基础设施、运行时、Compose 拓扑变更。已经部署完成的服务器进行日常代码发布时，不得再次全量运行部署脚本；应按 `docs/server-deployment-boundary.md` 只更新实际发生变化的服务。普通前端、后端代码更新只构建并替换 `backend`，必须使用 `up -d --no-deps backend`，不得重建 PostgreSQL、Redis 或 Embedding。

## 数据库模式

```bash
./scripts/deploy.sh --database-mode auto --runtime auto
```

Windows：

```powershell
.\scripts\deploy.ps1 -DatabaseMode auto -Runtime auto
```

数据库模式：

- `auto`：配置了 `DATABASE_URL` 时选择云数据库，否则选择本地 PostgreSQL；
- `cloud`：强制使用云 PostgreSQL，缺少任一必要条件会直接失败；
- `local`：加载 `docker-compose.local.yml`，启动本地 PostgreSQL 和本地上传目录。

当应用迁移到另一台带独显的电脑、而 PostgreSQL 和媒体仍留在旧服务器时，使用
`cloud` 数据库模式并将媒体后端设为 `remote`。新电脑只加载基础 Compose 和 GPU
覆盖文件；**不要加载 `docker-compose.local.yml`**，否则会启动本地 PostgreSQL
并把媒体写入新电脑的本地目录。

生产服务器应明确设置：

```dotenv
DEPLOY_DATABASE_MODE=cloud
```

这里的“云模式”表示通过 `DATABASE_URL` 连接外部 PostgreSQL，也包括数据库继续运行在旧服务器的迁移场景。云模式不会加载本地 PostgreSQL 服务，也不会在连接失败后回退本地。部署前会检查：

- `DATABASE_URL` 已配置；
- 媒体存储为 `s3`（并配置 bucket）或 `remote`（并配置私网媒体网关及共享密钥）；
- `s3` 模式下固定 S3 Access Key 与 Secret Key 成对配置；
- `INTEGRATION_API_KEY` 已替换为至少 24 位的非占位密钥；
- `RETRIEVAL_API_KEY` 已替换为另一个至少 24 位的非占位密钥；
- 首次部署的初始管理员使用至少 12 位密码；
- 已禁止固定默认弱管理员；
- 向量维度保持 1024；
- Compose 最终服务列表中不存在 `postgres`。

数据库初始化阶段会自动执行 Alembic、验证 `public` schema 中的 `vector`
扩展、四个 `vector(1024)` 字段、关键约束与索引和当前迁移版本。pgvector
位于其他 schema 的托管数据库不兼容当前版本。

云模板默认绑定 `127.0.0.1:8000`，应通过同机 Nginx、Caddy 或安全隧道访问。
只有确实需要直接暴露公网端口时才设置 `HOST_BIND_IP=0.0.0.0`，并同步配置
防火墙、HTTPS 和访问控制。

## 远程媒体模式

远程媒体模式的链路是：

```text
浏览器 → 新电脑后端 → Tailscale/WireGuard/VPN 私网 → 旧服务器媒体网关 → backend/uploads
                              └──────────────→ 旧服务器 PostgreSQL
```

新电脑配置：

```dotenv
MEDIA_STORAGE_BACKEND=remote
REMOTE_MEDIA_BASE_URL=https://replace-with-private-media-gateway
REMOTE_MEDIA_API_KEY=replace-with-a-random-secret-at-least-24-chars
REMOTE_MEDIA_PATH_PREFIX=/internal/media
REMOTE_MEDIA_TIMEOUT_SECONDS=60
REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS=10
REMOTE_MEDIA_READ_TIMEOUT_SECONDS=300
REMOTE_MEDIA_VERIFY_TLS=true
```

旧服务器媒体网关配置：

```dotenv
MEDIA_STORAGE_BACKEND=local
REMOTE_MEDIA_API_KEY=与新电脑完全相同的随机共享密钥
BACKGROUND_WORKERS_ENABLED=false
MEDIA_GATEWAY_ONLY=true
```

`REMOTE_MEDIA_BASE_URL` 只能指向 Tailscale、WireGuard 或等价 VPN 私网地址。
旧网关只允许私网来源访问 `/internal/media/*`，并校验相同的
`X-Internal-Media-Key`。来源限制不是应用代码自动完成的，必须在
Tailscale/WireGuard、防火墙或反向代理层落实；应用层只负责路径和共享密钥校验。
共享密钥不得进入浏览器、前端代码、Git、日志或截图；不要把旧服务器的
PostgreSQL `5432`、上传目录或媒体网关公开到公网。

`MEDIA_GATEWAY_ONLY=true` 会强制关闭所有后台 worker，只放行
`/internal/media/*`、`/health` 和 `/ready`；网关模式的 `/ready` 只检查共享数据库，
不依赖本机 Embedding 服务。新电脑普通应用节点应使用
`BACKGROUND_WORKERS_ENABLED=true`、`MEDIA_GATEWAY_ONLY=false`。

旧服务器进入网关模式时，不要套用新电脑的 cloud/local 部署脚本，也不要自行
追加或删除 `docker-compose.local.yml`。必须沿用旧服务器当前已经验证的 Compose
文件组合和运行时覆盖文件，按 `docs/server-deployment-boundary.md` 的
backend-only 更新流程重建并重启 backend；先用 `docker compose ... config` 核对
`DATABASE_URL`/`POSTGRES_HOST`、`UPLOAD_DIR` 和媒体卷仍指向原数据库与原
`backend/uploads`，再切换 `MEDIA_GATEWAY_ONLY=true`。旧服务器没有独立的
运行时组合时，先停在这里，不要用本地示例覆盖生产配置。

新电脑的远程媒体部署不得追加 `docker-compose.local.yml`：

```powershell
.\scripts\deploy.ps1 -DatabaseMode cloud -Runtime gpu
```

切换前先保持旧服务器 `BACKGROUND_WORKERS_ENABLED=true`、`MEDIA_GATEWAY_ONLY=false`，
阻断旧完整业务入口的新写入，让本地媒体清理 worker 完成已有任务，并盘点：

```sql
SELECT count(*) AS local_deletion_tasks
FROM media_deletion_tasks
WHERE storage_backend = 'local';

SELECT count(*) AS local_staging_rows
FROM media_upload_staging
WHERE storage_backend = 'local';
```

两个查询必须都为 0，才允许切换；远程 worker 不会处理遗留的 `local` 队列。
不要手工删除这些队列记录，也不要直接把 `local` 任务改成 `remote`。确认旧
本地文件处理完成后，再停止旧服务器对外的完整业务入口，将其切换为
`BACKGROUND_WORKERS_ENABLED=false`、`MEDIA_GATEWAY_ONLY=true`，然后启动新电脑。

也可以在旧服务器项目容器内执行只读预检：

```bash
docker compose ... exec -T backend python -m app.scripts.check_media_cutover
```

预检返回非 0 时不得继续切换；`...` 必须替换为旧服务器当前已验证的 Compose
文件组合，不能改用本地示例或追加 `docker-compose.local.yml`。
切换期间不要让旧、新两个完整业务后端长期同时写同一个 PostgreSQL；旧服务器应
只保留受限媒体网关和数据库服务。

## Qwen3 运行模式

部署脚本始终使用 `Qwen/Qwen3-Embedding-0.6B` 和 1024 维向量，只选择 GPU 或 CPU，不会替换模型。

`auto` 会先验证 Docker 能否启动配置的 GPU 镜像。验证通过时使用 `docker-compose.embedding-gpu.yml`，否则使用 `docker-compose.embedding-cpu.yml`。

```bash
./scripts/deploy.sh --runtime gpu
./scripts/deploy.sh --runtime cpu
```

GPU 显式模式预检失败会直接终止；自动模式才允许切换到 CPU。

CPU Compose会把模型容器的80端口映射到宿主机
`127.0.0.1:8080`，供同机Answer Hub使用；容器间调用仍使用
`http://embedding-qwen:80/v1`。该映射只允许绑定回环地址，不能改成
`0.0.0.0`，也不能通过公网反向代理开放。

## 完成条件

脚本只在以下真实检查全部通过后报告成功：

1. 数据库迁移容器退出码为 0；
2. 数据库、pgvector、迁移版本和向量索引正确；
3. 后端 `/ready` 可用；
4. Qwen3 返回一个真实的 1024 维向量；
5. 仅在前四项通过后，按所选媒体后端执行一次上传、读取和删除探针；远程模式
   的探针实际写入旧服务器媒体目录。

媒体探针失败会立即终止，不会循环重复写入。部署超时会停止仍在运行的
数据库初始化容器，但不会删除任何数据卷。任何步骤失败都会输出迁移、
Embedding 和后端日志。Docker Desktop/WSL 的 GPU 配置默认预加载
`/usr/lib/x86_64-linux-gnu/libcuda.so.1`，其他宿主机可通过
`TEI_GPU_LD_PRELOAD` 调整。
