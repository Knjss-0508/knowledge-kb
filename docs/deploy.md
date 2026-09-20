# 答疑中台知识库部署说明

## 推荐生产架构

- 应用和 Qwen3：部署在业务服务器；
- 知识、分类、标签、向量、账号和审计记录：云 PostgreSQL 16 + pgvector；
- 图片和视频：S3 兼容对象存储，或保留在旧服务器本地目录并通过私网媒体网关访问；
- Redis：应用服务器内部使用，不保存知识主数据。

这里的“云模式”表示通过 `DATABASE_URL` 连接外部 PostgreSQL，也包括数据库继续运行在旧服务器的迁移场景。云模式不会启动本地 PostgreSQL，也不会在新电脑挂载本地媒体目录。后续新增知识由后端直接写入远程数据库，不需要重复上传 SQL。选择远程媒体模式时，浏览器仍只访问新电脑后端；媒体字节由新电脑后端经私网从旧服务器读取或写入。

## 一、准备云资源

创建一个空 PostgreSQL 数据库并确认：

- 版本为 PostgreSQL 16 或兼容版本；
- 允许 `CREATE EXTENSION vector`，或由云平台提前把 pgvector 安装在 `public` schema；
- 仅允许应用服务器访问数据库端口；
- 使用 S3 模式时，已创建私有 S3 bucket，并给应用账号授予对象上传、读取、删除权限；
- 使用旧服务器本地媒体模式时，已建立 Tailscale、WireGuard 或等价 VPN 私网，并已在旧服务器提供仅限私网访问的媒体网关。

`public` schema 是当前版本的硬性条件：SQL、Alembic 和向量索引均使用
`public.vector`。如果云平台把 pgvector 固定安装在其他 schema，必须先让
云平台支持安装或迁移到 `public`，否则不要继续导入。

## 二、导入空白完整 SQL

仓库提供两份内容一致的空白完整库，均包含全部表、约束、索引、pgvector
结构和四个基础分类，不包含本机测试数据或默认用户：

- `database/knowledge-kb-schema.sql`：用于 `psql`；
- `database/knowledge-kb-schema-console.sql`：用于云平台网页 SQL 控制台，
  不含 `\restrict`、`COPY`、`\.` 等 `psql` 元命令，并自带事务。

```bash
psql "$DATABASE_URL" \
  --single-transaction \
  --set ON_ERROR_STOP=1 \
  --file database/knowledge-kb-schema.sql
```

导入命令强制在单一事务中执行，任一步失败都会整体回滚。只能导入到空数据库，导入前不得覆盖已有生产库。

只有网页 SQL 控制台时，一次性执行
`database/knowledge-kb-schema-console.sql`，并确保平台在首个错误时停止。

## 三、配置云部署

```bash
cp .env.cloud.example .env
```

至少替换：

```dotenv
DEPLOY_DATABASE_MODE=cloud
DATABASE_URL=postgresql://用户:密码@云数据库地址:5432/knowledge_base?sslmode=require

INITIAL_ADMIN_USERNAME=knowledge-admin
INITIAL_ADMIN_PASSWORD=至少12位的强密码
ALLOW_INSECURE_DEFAULT_ADMIN=false
INTEGRATION_API_KEY=至少24位的随机服务密钥
RETRIEVAL_API_KEY=另一个至少24位的随机服务密钥

# 二选一：S3 媒体存储
MEDIA_STORAGE_BACKEND=s3
S3_BUCKET=实际bucket名称
S3_ENDPOINT_URL=
S3_REGION=实际地域
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
```

如果媒体继续保留在旧服务器的 `backend/uploads`，改用以下远程媒体配置，不填写真实地址或密钥到仓库：

```dotenv
# 新电脑：运行应用、Redis 与 GPU Embedding
MEDIA_STORAGE_BACKEND=remote
REMOTE_MEDIA_BASE_URL=https://旧服务器私网媒体网关地址
REMOTE_MEDIA_API_KEY=至少24位的随机共享密钥
REMOTE_MEDIA_PATH_PREFIX=/internal/media
REMOTE_MEDIA_TIMEOUT_SECONDS=60
REMOTE_MEDIA_CONNECT_TIMEOUT_SECONDS=10
REMOTE_MEDIA_READ_TIMEOUT_SECONDS=300
REMOTE_MEDIA_VERIFY_TLS=true

# 旧服务器：继续使用本地媒体目录，并设置与新电脑完全相同的共享密钥
MEDIA_STORAGE_BACKEND=local
REMOTE_MEDIA_API_KEY=与新电脑相同的至少24位随机共享密钥
# 旧服务器只作媒体网关时：关闭导入、向量、媒体清理 worker，并限制 HTTP 路由
BACKGROUND_WORKERS_ENABLED=false
MEDIA_GATEWAY_ONLY=true
```

`REMOTE_MEDIA_BASE_URL` 必须是旧服务器的 Tailscale、WireGuard 或其他 VPN 私网地址；不得填写公网媒体地址，也不得把 `/internal/media`、PostgreSQL `5432` 或旧服务器上传目录开放给公网。共享密钥只供两台后端互认，不是浏览器 Cookie、企业微信 Token 或前端配置，不能写入 Git、日志、截图或接口返回。

旧服务器网关需要仅允许私网来源访问 `/internal/media/*`，并校验同一个 `X-Internal-Media-Key`。来源限制不是应用代码自动完成的，必须在 Tailscale/WireGuard、防火墙或反向代理层落实；应用层只负责路径和共享密钥校验。设置 `MEDIA_GATEWAY_ONLY=true` 后，进程会强制停止全部后台 worker，只放行 `/internal/media/*`、`/health` 和 `/ready`；`/ready` 只检查共享数据库，不要求本机存在 Embedding 服务。新电脑会把浏览器请求的 Range 条件转发到网关，因此视频预览、拖动和分段加载仍由浏览器访问新电脑后端完成，浏览器不会直接拿到旧服务器地址或共享密钥。

两个服务密钥必须不同：`INTEGRATION_API_KEY` 供自动入库、字典和查重等上游
接口使用；`RETRIEVAL_API_KEY` 只供答疑插件检索已发布知识并回传召回质量。
插件包中不得包含权限更大的上游密钥。

连接 URL 中的用户名或密码若包含 `@`、`/`、`:`、`#`、`%` 等保留字符，
必须先做 URL 百分号编码，例如 `@` 编码为 `%40`、`#` 编码为 `%23`。

AWS 等支持实例角色的环境可以不写固定 S3 访问密钥；其他 S3 兼容服务填写 endpoint 和访问凭据。不要提交 `.env`。
固定 S3 凭据必须同时填写 Access Key 和 Secret Key；使用临时凭据时还需填写 Session Token。远程媒体模式不需要 S3 凭据，但必须配置私网网关地址和共享密钥。

## 四、启动

Linux：

```bash
bash scripts/deploy.sh --database-mode cloud --runtime auto
```

Windows：

```powershell
.\scripts\deploy.ps1 -DatabaseMode cloud -Runtime auto
```

新电脑有独显时可显式使用 GPU：

```powershell
.\scripts\deploy.ps1 -DatabaseMode cloud -Runtime gpu
```

远程媒体迁移的新电脑只使用 `docker-compose.yml` 与对应的 `docker-compose.embedding-gpu.yml`（或 CPU 覆盖文件）。**不要**加载 `docker-compose.local.yml`；该文件会启动本地 PostgreSQL 并切换到本地上传目录，导致数据库或媒体写入分裂。上述部署脚本在 `-DatabaseMode cloud` 时会自动排除该文件。

部署成功前会真实验证：

- 云数据库连接及迁移版本；
- pgvector 1024 维字段和 HNSW 索引；
- Qwen3 的真实 1024 维向量；
- 前述服务就绪后，仅执行一次所选媒体存储的上传、读取、删除探针；
- 后端就绪状态。

部署整体超时时，脚本会停止仍在运行的数据库初始化容器，但不会删除数据库、
Redis、模型缓存或其他数据卷。

`.env.cloud.example` 默认只监听服务器本机 `127.0.0.1:8000`。推荐通过 Nginx、Caddy 或安全隧道反向代理后访问：

```text
http://127.0.0.1:8000/
http://127.0.0.1:8000/app
```

确实需要直接通过公网 IP 访问时，将 `HOST_BIND_IP` 改为 `0.0.0.0`，并同时配置防火墙、HTTPS 和访问控制。

登录账号使用 `.env` 中设置的初始管理员，不存在生产默认密码。

首次部署成功后可删除 `INITIAL_ADMIN_USERNAME` 和 `INITIAL_ADMIN_PASSWORD`；后续只要数据库中仍有启用的超级管理员即可正常更新。修改已有管理员必须显式设置 `INITIAL_ADMIN_FORCE_RESET=true`。

## 远程媒体切换顺序

仅在“新电脑后端 + 旧服务器数据库与本地媒体目录”场景执行以下步骤。切换期间不要让两个完整业务后端同时对同一个 PostgreSQL 提供写服务；旧服务器应只保留受限媒体网关，或先隔离其业务入口和后台 worker。

1. 建立并验证新旧电脑之间的 Tailscale、WireGuard 或等价私网连接；数据库和媒体网关都只允许私网访问。
2. 在旧服务器保留 `MEDIA_STORAGE_BACKEND=local`、原 `backend/uploads` 目录和媒体网关，并设置与新电脑相同的 `REMOTE_MEDIA_API_KEY`。清理阶段暂时保持 `BACKGROUND_WORKERS_ENABLED=true`、`MEDIA_GATEWAY_ONLY=false`，让原后端继续运行本地媒体清理 worker。进入切换窗口前，在 `/opt/knowledge-kb` 使用旧服务器当前已验证的 Compose 文件组合执行 `docker compose ... config`，核对实际数据库服务、`DATABASE_URL`/`POSTGRES_HOST`、`UPLOAD_DIR` 和媒体卷仍指向原数据库与原 `backend/uploads`；不要把连接密码或共享密钥打印到日志。
3. 先阻断旧服务器完整业务入口的新写入，但不要先关闭旧 backend worker。保持旧 worker 运行，清空所有 `storage_backend='local'` 的临时上传和删除队列，直到下列两个计数都为 0：

   ```sql
   SELECT count(*) AS local_deletion_tasks
   FROM media_deletion_tasks
   WHERE storage_backend = 'local';

   SELECT count(*) AS local_staging_rows
   FROM media_upload_staging
   WHERE storage_backend = 'local';
   ```

   这两个计数是切换硬闸门，任一不为 0 都不得进入网关模式。远程 worker 只处理
   `storage_backend='remote'`，不会替旧服务器删除遗留的 `local` 文件；不要手工删除
   队列表记录，也不要把旧 `local` 任务改成 `remote`。对未完成的编辑草稿应先保存或放弃，
   失败任务先排查旧目录权限和文件状态。

   在旧服务器项目容器内可执行同样的只读预检；必须使用旧服务器当前已验证的完整
   Compose 文件组合，不要自行追加 `docker-compose.local.yml`：

   ```bash
   docker compose ... exec -T backend python -m app.scripts.check_media_cutover
   ```

   预检返回非 0 时不得继续切换。
4. 复核两个计数仍为 0 后，停止旧服务器对外的完整业务入口，将旧服务器切为
   `BACKGROUND_WORKERS_ENABLED=false`、`MEDIA_GATEWAY_ONLY=true`；确认 `/health`、`/ready`
   和带共享密钥的 `/internal/media/*` 仍可用。
5. 在新电脑写入 `DATABASE_URL`、远程媒体配置和 GPU 配置后，以
   `-DatabaseMode cloud -Runtime gpu` 启动；不要追加 `docker-compose.local.yml`。
   新电脑后端容器启动后，必须在容器内做一次 VPN 媒体网关连通性检查（不能只在宿主机检查）：

   ```bash
   docker compose -p knowledge-kb \
     -f docker-compose.yml \
     -f docker-compose.embedding-gpu.yml \
     exec -T backend python -c 'import os,socket; from urllib.parse import urlsplit; u=urlsplit(os.environ["REMOTE_MEDIA_BASE_URL"]); s=socket.create_connection((u.hostname,u.port or (443 if u.scheme=="https" else 80)),5); s.close(); print("media gateway reachable")'
   ```

   该命令只验证容器到私网地址的路由；部署脚本的媒体 smoke probe 还必须实际验证共享密钥、上传、读取和删除。
6. 将用户访问入口切换到新电脑后端，并验证图片预览、视频 Range 拖动、普通上传、临时上传后保存、删除媒体和 `/ready`。切换后再次查询旧库中的两个 `local` 计数，确认没有竞态写入；旧服务器媒体目录保留作为数据源，不迁移或删除。
7. 验收完成后，旧服务器仅保留私网媒体网关和数据库所需服务；不得把旧完整业务后端作为长期第二写入节点。

## 本地开发模式

```bash
cp .env.example .env
bash scripts/deploy.sh --database-mode local --runtime auto
```

本地模式会追加 `docker-compose.local.yml`，启动 `kb-postgres`，并把媒体挂载到 `backend/uploads`。仅本地开发保留旧默认管理员兼容逻辑，生产环境必须关闭。

## 日常更新

服务器已经完成首次部署后，日常发布属于增量更新，不再重复执行整套部署脚本。普通前端、后端代码更新只更新 `backend`：

迁移 `20260805_01` 会为旧知识补充“知识来源”。非空旧库必须先在 `.env`
中明确设置 `KNOWLEDGE_ORIGIN_BACKFILL=headquarters_standard` 或
`KNOWLEDGE_ORIGIN_BACKFILL=business_accumulation`；迁移会自动检测旧数据，
未配置时停止，避免静默归错来源。空库无需设置。

```bash
cd /opt/knowledge-kb
git status --short --branch
git fetch origin
git pull --ff-only origin master

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  -f /opt/knowledge-kb-runtime/docker-compose.server.yml \
  build backend

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  -f /opt/knowledge-kb-runtime/docker-compose.server.yml \
  up -d --no-deps backend
```

普通更新禁止执行全项目 `up -d --build`、`--remove-orphans` 或重新构建 Embedding。只有本次改动确实包含 Alembic 迁移时，才额外构建并运行 `migrate`；只有经过评审的数据库、Redis、Embedding 或 Compose 拓扑变更，才允许使用完整部署脚本。

共享生产服务器的准确目录、Compose 文件组合、更新前检查和更新后验收，以 `docs/server-deployment-boundary.md` 为准。

## 常用运维

云模式查看状态：

```bash
docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.embedding-cpu.yml ps
```

GPU 服务器把最后一个文件改为 `docker-compose.embedding-gpu.yml`。

查看日志：

```bash
docker logs -f kb-backend
docker logs kb-migrate
```

不要执行带 `-v` 的 `docker compose down`，否则可能删除 Redis、本地开发数据库或模型缓存卷。

## 生产备份

数据库：

```bash
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges \
  --file "knowledge-kb-$(date +%Y%m%d-%H%M%S).dump"
```

S3 模式同时启用对象存储的版本控制、生命周期策略或跨区域备份。远程媒体模式同时备份旧服务器的 `backend/uploads` 和数据库；只有数据库备份而没有对应媒体目录或 S3 备份时，图片和视频无法完整恢复。

备份可能包含知识内容、账号哈希和审计数据，不得提交到 Git。
