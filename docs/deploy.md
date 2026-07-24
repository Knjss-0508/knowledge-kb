# 答疑中台知识库部署说明

## 推荐生产架构

- 应用和 Qwen3：部署在业务服务器；
- 知识、分类、标签、向量、账号和审计记录：云 PostgreSQL 16 + pgvector；
- 图片和视频：S3 兼容对象存储；
- Redis：应用服务器内部使用，不保存知识主数据。

云模式不会启动本地 PostgreSQL，也不会挂载本地媒体目录。后续新增知识由后端直接写入云数据库，不需要重复上传 SQL。

## 一、准备云资源

创建一个空 PostgreSQL 数据库并确认：

- 版本为 PostgreSQL 16 或兼容版本；
- 允许 `CREATE EXTENSION vector`，或由云平台提前把 pgvector 安装在 `public` schema；
- 仅允许应用服务器访问数据库端口；
- 已创建私有 S3 bucket，并给应用账号授予对象上传、读取、删除权限。

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

MEDIA_STORAGE_BACKEND=s3
S3_BUCKET=实际bucket名称
S3_ENDPOINT_URL=
S3_REGION=实际地域
S3_ACCESS_KEY_ID=
S3_SECRET_ACCESS_KEY=
```

连接 URL 中的用户名或密码若包含 `@`、`/`、`:`、`#`、`%` 等保留字符，
必须先做 URL 百分号编码，例如 `@` 编码为 `%40`、`#` 编码为 `%23`。

AWS 等支持实例角色的环境可以不写固定访问密钥；其他 S3 兼容服务填写 endpoint 和访问凭据。不要提交 `.env`。
固定 S3 凭据必须同时填写 Access Key 和 Secret Key；使用临时凭据时还需填写 Session Token。

## 四、启动

Linux：

```bash
bash scripts/deploy.sh --database-mode cloud --runtime auto
```

Windows：

```powershell
.\scripts\deploy.ps1 -DatabaseMode cloud -Runtime auto
```

部署成功前会真实验证：

- 云数据库连接及迁移版本；
- pgvector 1024 维字段和 HNSW 索引；
- Qwen3 的真实 1024 维向量；
- 前述服务就绪后，仅执行一次对象存储上传、读取、删除探针；
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

## 本地开发模式

```bash
cp .env.example .env
bash scripts/deploy.sh --database-mode local --runtime auto
```

本地模式会追加 `docker-compose.local.yml`，启动 `kb-postgres`，并把媒体挂载到 `backend/uploads`。仅本地开发保留旧默认管理员兼容逻辑，生产环境必须关闭。

## 日常更新

```bash
cd /opt/knowledge-kb
git pull
bash scripts/deploy.sh --database-mode cloud --runtime auto
```

部署脚本使用 `--remove-orphans`。从本地模式切换为云模式时会停止旧 `kb-postgres` 容器，但不会删除原 `pg_data` 卷。

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

同时启用对象存储的版本控制、生命周期策略或跨区域备份。只有数据库备份而没有 S3 备份时，图片和视频无法完整恢复。

备份可能包含知识内容、账号哈希和审计数据，不得提交到 Git。
