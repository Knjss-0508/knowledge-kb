# 知识库部署与运维说明

本文说明 `knowledge-kb` 在腾讯云服务器上的实际部署结构、组成程序、数据位置、启动方式、数据库查看方法、更新方式和常见故障排查。

文档中的命令默认在 Debian/Ubuntu 服务器上执行。生产环境执行命令前，请确认当前目录和项目名正确。

## 1. 当前部署概览

当前服务器部署目录：

```text
/www/wwwroot/knowledge-kb
```

整体结构：

```text
公网访问
  │
  ├─ http://服务器IP:8888/
  │      │
  │      └─ 宝塔 Nginx 反向代理
  │             │
  │             └─ http://127.0.0.1:8000
  │                    │
  │                    └─ knowledge-kb backend
  │                           ├─ PostgreSQL + pgvector
  │                           ├─ Redis
  │                           └─ Qwen3 Embedding CPU 服务
  │
  └─ 图片和视频
         └─ /www/wwwroot/knowledge-kb/backend/uploads
```

当前没有配置域名和 HTTPS，公网入口是 HTTP。

已验证的访问地址：

```text
登录页：http://111.230.109.227:8888/
管理后台：http://111.230.109.227:8888/app
API 文档：http://111.230.109.227:8888/docs
```

`8801` 已在服务器 Nginx 和 UFW 中配置，但如果腾讯云安全组没有放行 TCP `8801`，公网无法访问。`8888` 目前作为可用入口保留。

## 2. 服务器环境

已部署服务器的主要环境：

| 项目 | 配置 |
|---|---|
| 操作系统 | Debian 12 |
| 架构 | x86_64 |
| CPU | 2 核 AMD EPYC，支持 AVX2 |
| 内存 | 约 8 GiB |
| 磁盘 | 约 80 GiB |
| Docker | Docker Engine |
| Compose | Docker Compose Plugin |
| 反向代理 | 宝塔管理的 Nginx |
| 数据库模式 | 本地 Docker PostgreSQL |
| Embedding 模式 | Qwen3 CPU |
| 媒体存储 | 本地目录 |

Qwen3 Embedding CPU 服务约占用 3 GiB 内存。服务器上如果同时运行宝塔、MySQL、Node.js 或远程开发服务，内存余量会比较有限。

## 3. 项目目录说明

服务器项目根目录：

```text
/www/wwwroot/knowledge-kb
```

重要目录和文件：

| 路径 | 作用 |
|---|---|
| `backend/` | FastAPI 后端代码、数据库迁移和后端测试 |
| `backend/app/main.py` | FastAPI 应用入口 |
| `backend/app/routes/` | API 路由 |
| `backend/app/models/` | SQLAlchemy 数据模型 |
| `backend/app/services/` | 查重、Embedding、Excel、媒体存储等服务 |
| `backend/migrations/` | Alembic 数据库迁移 |
| `backend/uploads/` | 本地模式下的图片和视频文件 |
| `frontend/` | Vue 3 管理页面和前端依赖 |
| `embedding-cpu/` | Qwen3 CPU Embedding 服务 |
| `database/` | 空白数据库 SQL 和数据库说明 |
| `scripts/deploy.sh` | Linux 部署脚本 |
| `scripts/deploy.ps1` | Windows 部署脚本 |
| `docker-compose.yml` | Redis、后端和数据库初始化服务 |
| `docker-compose.local.yml` | 本地 PostgreSQL 和本地媒体存储覆盖配置 |
| `docker-compose.embedding-cpu.yml` | CPU Embedding 服务覆盖配置 |
| `docker-compose.embedding-gpu.yml` | GPU Embedding 服务覆盖配置 |
| `.env` | 服务器运行配置，包含密码和密钥，不得提交 Git |

### 3.1 后端是什么

后端是 Python FastAPI 服务，负责：

- 登录、用户和权限
- 知识条目增删改查
- 草稿、审核、发布和废弃
- 图片、视频上传
- Excel 批量导入
- 分类和标签
- 语义查重
- pgvector 语义检索
- 上游候选知识接入
- 下游召回质量事件

后端容器名：

```text
kb-backend
```

容器内部监听 `8000`，服务器只绑定到：

```text
127.0.0.1:8000
```

外部访问由宝塔 Nginx 转发。

### 3.2 前端是什么

前端是 `frontend/index.html` 提供的 Vue 3 单页管理后台。

它没有独立的 Node 构建流程，后端启动后通过以下路径直接返回：

```text
/
/login
/app
```

登录页和管理后台通过 `/api/v1` 调用后端 API。

### 3.3 PostgreSQL 是什么

PostgreSQL 是主数据库，保存：

- 知识标题和正文
- 知识状态
- 分类、标签
- 用户和登录会话
- 查重向量
- 语义检索向量
- 媒体文件元数据
- 变更日志
- 候选审核记录
- 召回质量事件

容器名：

```text
kb-postgres
```

使用的镜像包含 `pgvector`，用于向量查重和语义检索。

### 3.4 Redis 是什么

Redis 是缓存和基础设施服务，目前不是知识主数据存储位置。

容器名：

```text
kb-redis
```

不要把 PostgreSQL 数据迁移或备份任务当成 Redis 数据备份。

### 3.5 Qwen3 Embedding 是什么

Embedding 服务把中文标题、正文和用户查询转换为 1024 维向量，用于：

- 判断知识是否重复
- 按语义检索已发布知识

容器名：

```text
kb-embedding-qwen
```

当前使用：

```text
模型：Qwen/Qwen3-Embedding-0.6B
维度：1024
运行模式：CPU
```

模型第一次启动时会下载并缓存，之后从 Docker Volume 加载。

## 4. Docker 服务和数据卷

当前 Compose 项目名：

```text
knowledge-kb
```

查看容器：

```bash
cd /www/wwwroot/knowledge-kb

docker compose \
  -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  ps -a
```

当前主要容器：

| 容器 | 作用 |
|---|---|
| `kb-backend` | FastAPI 后端和前端静态页面 |
| `kb-postgres` | PostgreSQL + pgvector |
| `kb-redis` | Redis |
| `kb-embedding-qwen` | Qwen3 CPU 向量服务 |
| `kb-migrate` | 数据库初始化和迁移，成功后退出 |

`kb-migrate` 正常状态是 `Exited (0)`，它不是常驻服务。

查看 Docker Volume：

```bash
docker volume ls | grep knowledge-kb
```

主要 Volume：

| Volume | 作用 |
|---|---|
| `knowledge-kb_pg_data` | PostgreSQL 数据 |
| `knowledge-kb_redis_data` | Redis 数据 |
| `knowledge-kb_embedding_cpu_model_cache` | Qwen 模型缓存 |

查看 Volume 的实际路径：

```bash
docker volume inspect knowledge-kb_pg_data
docker volume inspect knowledge-kb_redis_data
docker volume inspect knowledge-kb_embedding_cpu_model_cache
```

不要直接编辑 Docker Volume 内部文件。数据库数据应通过 PostgreSQL 命令或备份工具操作。

## 5. 查看 `knowledge_base` 数据库

### 5.1 进入数据库命令行

先查看 `.env` 中的数据库用户名和数据库名：

```bash
cd /www/wwwroot/knowledge-kb
grep -E '^(POSTGRES_USER|POSTGRES_DB)=' .env
```

不要用 `cat .env` 把完整密钥显示到终端或日志中。

默认数据库连接命令：

```bash
docker exec -it kb-postgres \
  psql -U knowledge_admin -d knowledge_base
```

如果 `.env` 中修改过用户名或数据库名，请替换命令中的值。

### 5.2 常用 psql 命令

进入 `psql` 后：

```sql
-- 查看所有表
\dt

-- 查看一张表的结构
\d+ knowledge_items

-- 查看当前数据库
\conninfo

-- 退出
\q
```

### 5.3 查看知识数量

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT status, COUNT(*) FROM knowledge_items GROUP BY status ORDER BY status;"
```

知识状态通常包括：

```text
draft
review
published
deprecated
```

### 5.4 查看知识列表

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT id, title, status, category_id, created_by, created_at
   FROM knowledge_items
   ORDER BY created_at DESC
   LIMIT 20;"
```

### 5.5 查看已发布知识

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT id, title, category_id, updated_at
   FROM knowledge_items
   WHERE status = 'published'
   ORDER BY updated_at DESC;"
```

### 5.6 查看分类

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT id, name, parent_id, level, sort_order
   FROM categories
   ORDER BY level, sort_order;"
```

### 5.7 查看用户

只查看账号基本信息，不要查询或复制密码哈希：

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT id, username, role, is_active, created_at
   FROM users
   ORDER BY created_at;"
```

### 5.8 查看向量和媒体记录数量

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT
      (SELECT COUNT(*) FROM knowledge_embeddings) AS dedup_embeddings,
      (SELECT COUNT(*) FROM knowledge_search_embeddings) AS search_embeddings,
      (SELECT COUNT(*) FROM knowledge_media) AS media_records;"
```

### 5.9 查看迁移版本和 pgvector

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT version_num FROM alembic_version;
   SELECT extname, extversion
   FROM pg_extension
   WHERE extname = 'vector';"
```

## 6. 查看媒体文件

本地媒体文件目录：

```text
/www/wwwroot/knowledge-kb/backend/uploads
```

查看媒体目录大小：

```bash
du -sh /www/wwwroot/knowledge-kb/backend/uploads
```

查看最近文件：

```bash
find /www/wwwroot/knowledge-kb/backend/uploads \
  -maxdepth 1 -type f \
  -printf '%TY-%Tm-%Td %TH:%TM %s %p\n' \
  | sort -r \
  | head -30
```

数据库中的 `knowledge_media` 只保存媒体元数据和文件名，媒体本体保存在上述目录。

删除媒体时，后端会先处理数据库记录和媒体删除任务；不要直接手动删除文件，否则数据库可能还保留对应记录。

## 7. 查看日志和健康状态

### 7.1 查看服务状态

```bash
cd /www/wwwroot/knowledge-kb

docker compose \
  -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  ps
```

### 7.2 查看后端日志

```bash
docker logs -f --tail 200 kb-backend
```

### 7.3 查看数据库日志

```bash
docker logs -f --tail 200 kb-postgres
```

### 7.4 查看 Embedding 日志

```bash
docker logs -f --tail 200 kb-embedding-qwen
```

### 7.5 查看 Redis 日志

```bash
docker logs -f --tail 200 kb-redis
```

### 7.6 健康检查

从服务器本机访问后端：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

通过宝塔 Nginx 访问：

```bash
curl http://127.0.0.1:8888/health
curl http://127.0.0.1:8888/
curl http://127.0.0.1:8888/app
```

正常结果：

```json
{"status":"ok","service":"答疑中台知识库","version":"0.1.0"}
```

## 8. 首次部署过程

### 8.1 准备代码

如果服务器可以访问代码仓库：

```bash
mkdir -p /www/wwwroot
cd /www/wwwroot
git clone <仓库地址> knowledge-kb
cd knowledge-kb
```

如果服务器无法访问 GitHub，可以在本机打包后上传：

```powershell
tar -czf knowledge-kb.tar.gz `
  --exclude='.git' `
  --exclude='.env' `
  --exclude='__pycache__' `
  -C D:\knowledge-kb .

scp knowledge-kb.tar.gz root@服务器IP:/tmp/
```

服务器上解压：

```bash
mkdir -p /www/wwwroot/knowledge-kb
tar -xzf /tmp/knowledge-kb.tar.gz \
  -C /www/wwwroot/knowledge-kb
```

### 8.2 创建配置文件

```bash
cd /www/wwwroot/knowledge-kb
cp .env.example .env
chmod 600 .env
nano .env
```

本地一体化部署至少需要配置：

```dotenv
DEPLOY_DATABASE_MODE=local
DEPLOY_RUNTIME=cpu

POSTGRES_PASSWORD=强数据库密码

INITIAL_ADMIN_USERNAME=knowledge-admin
INITIAL_ADMIN_PASSWORD=至少12位的强密码
INITIAL_ADMIN_FORCE_RESET=false
ALLOW_INSECURE_DEFAULT_ADMIN=false

INTEGRATION_API_KEY=至少24位的随机密钥

HOST_BIND_IP=127.0.0.1
BACKEND_PORT=8000

MEDIA_STORAGE_BACKEND=local
EMBEDDING_DIMENSIONS=1024
```

服务器网络受限时，还需要：

```dotenv
HF_ENDPOINT=https://hf-mirror.com
PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple
PYTORCH_INDEX_URL=https://download.pytorch.org/whl/cpu
REDIS_IMAGE=docker.m.daocloud.io/library/redis:7-alpine
PGVECTOR_IMAGE=docker.m.daocloud.io/pgvector/pgvector:pg16
```

密码和密钥不要写入 Git、Markdown、截图或日志。

这些镜像配置已经由 Compose 的 build args 和环境变量接入，不需要再手工修改
Dockerfile 或 Compose 文件。网络正常的服务器可以保留 `.env.example` 中的默认地址；
受限网络服务器再替换为可访问的镜像地址。

### 8.3 启动部署

```bash
cd /www/wwwroot/knowledge-kb
bash scripts/deploy.sh --database-mode local --runtime cpu
```

部署脚本会：

1. 检查 Compose 配置；
2. 拉取或构建 Docker 镜像；
3. 创建 PostgreSQL、Redis、Embedding 数据卷；
4. 启动 PostgreSQL；
5. 执行数据库迁移；
6. 创建首个管理员；
7. 启动 Qwen3 Embedding；
8. 检查 1024 维向量；
9. 启动后端；
10. 检查媒体存储上传、读取和删除。

## 9. 更新代码

### 9.1 使用 Git 更新

```bash
cd /www/wwwroot/knowledge-kb
git fetch origin
git pull --ff-only origin master
bash scripts/deploy.sh --database-mode local --runtime cpu
```

如果服务器不能访问 GitHub，使用本机重新打包上传，先确认服务器上没有未备份的本地修改。

### 9.2 更新时不会删除的数据

正常执行以下命令不会删除数据库和媒体：

```bash
docker compose up -d --build
bash scripts/deploy.sh --database-mode local --runtime cpu
```

绝对不要在没有备份的情况下执行：

```bash
docker compose down -v
```

`-v` 可能删除 PostgreSQL、Redis 和模型缓存数据卷。

## 10. 数据备份

### 10.1 备份 PostgreSQL

```bash
mkdir -p /www/backups/knowledge-kb

docker exec kb-postgres \
  pg_dump -U knowledge_admin -d knowledge_base \
  --format=custom \
  --no-owner \
  --no-privileges \
  > /www/backups/knowledge-kb/knowledge-base-$(date +%Y%m%d-%H%M%S).dump
```

### 10.2 备份媒体文件

```bash
tar -czf \
  /www/backups/knowledge-kb/uploads-$(date +%Y%m%d-%H%M%S).tar.gz \
  -C /www/wwwroot/knowledge-kb/backend uploads
```

### 10.3 查看备份大小

```bash
du -sh /www/backups/knowledge-kb
ls -lh /www/backups/knowledge-kb
```

数据库备份和媒体备份必须同时保存。只有数据库没有媒体文件，图片和视频无法恢复。

## 11. 数据恢复

恢复前先停止后端，避免恢复过程中继续写入：

```bash
docker stop kb-backend
```

恢复 PostgreSQL：

```bash
docker cp knowledge-base.dump kb-postgres:/tmp/knowledge-base.dump

docker exec kb-postgres \
  pg_restore -U knowledge_admin -d knowledge_base \
  --clean \
  --if-exists \
  --no-owner \
  --no-privileges \
  /tmp/knowledge-base.dump
```

恢复媒体：

```bash
tar -xzf uploads-backup.tar.gz \
  -C /www/wwwroot/knowledge-kb/backend
```

启动后端：

```bash
docker start kb-backend
curl http://127.0.0.1:8000/ready
```

恢复操作会覆盖同名数据库对象，执行前必须确认备份文件和目标数据库。

## 12. 宝塔 Nginx 反向代理

当前服务器使用的配置文件：

```text
/www/server/panel/vhost/nginx/knowledge-kb-ip-8888.conf
```

主要代理关系：

```text
0.0.0.0:8888
    ↓
127.0.0.1:8000
```

查看 Nginx 配置是否正确：

```bash
nginx -t
```

重新加载 Nginx：

```bash
systemctl reload nginx
```

不要直接修改宝塔主配置文件。新增站点或端口时，优先通过宝塔面板操作，或在 `/www/server/panel/vhost/nginx/` 增加独立配置文件。

仓库中的可复制模板：

```text
deploy/baota/knowledge-kb-ip-port.conf.example
```

使用模板时，将其中的 `PORT` 替换为实际端口，并同时确认：

1. 腾讯云安全组允许该 TCP 端口；
2. 服务器 UFW 或其他防火墙允许该端口；
3. 端口没有被其他程序占用；
4. `nginx -t` 检查通过后再执行 `systemctl reload nginx`。

## 13. API 接口入口

公网 API 根路径：

```text
http://111.230.109.227:8888/api/v1
```

Swagger 文档：

```text
http://111.230.109.227:8888/docs
```

常用接口：

| 方法 | 路径 | 作用 |
|---|---|---|
| `POST` | `/api/v1/auth/login` | 登录 |
| `GET` | `/api/v1/auth/me` | 当前用户 |
| `GET` | `/api/v1/knowledge` | 知识列表 |
| `POST` | `/api/v1/knowledge` | 创建知识 |
| `GET` | `/api/v1/knowledge/{id}` | 知识详情 |
| `PATCH` | `/api/v1/knowledge/{id}` | 更新知识 |
| `POST` | `/api/v1/knowledge/{id}/approve` | 审核发布 |
| `POST` | `/api/v1/knowledge/search` | 语义检索 |
| `GET` | `/api/v1/categories` | 分类列表 |
| `GET` | `/api/v1/tags/dimensions` | 标签维度 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/ready` | 数据库和 Embedding 就绪检查 |

登录接口返回 Bearer Token。需要登录的接口携带：

```http
Authorization: Bearer <token>
```

上游自动化接口还需要：

```http
X-Integration-Key: <INTEGRATION_API_KEY>
```

## 14. 常见故障

### 后端容器反复重启

```bash
docker logs --tail 200 kb-backend
docker logs --tail 200 kb-migrate
```

重点检查：

- `.env` 数据库配置；
- PostgreSQL 是否健康；
- 数据库迁移是否成功；
- Embedding 是否健康；
- `EMBEDDING_DIMENSIONS` 是否为 `1024`。

### Embedding 一直不健康

```bash
docker logs --tail 200 kb-embedding-qwen
docker stats --no-stream
```

重点检查：

- 是否能够访问模型镜像；
- 模型缓存 Volume 是否有空间；
- 内存是否不足；
- CPU 是否长期满载。

### 登录页能打开，但登录失败

检查管理员是否存在：

```bash
docker exec kb-postgres \
  psql -U knowledge_admin -d knowledge_base -c \
  "SELECT username, role, is_active FROM users;"
```

如果只是忘记密码，应通过管理员密码重置流程处理，不要直接修改数据库密码哈希。

### 公网访问超时

依次检查：

```bash
ss -lntp | grep -E ':8000|:8888|:8801'
ufw status
nginx -t
curl http://127.0.0.1:8888/health
```

如果服务器本机正常、外部超时，通常是腾讯云安全组没有开放对应端口。

### 磁盘空间不足

```bash
df -h
docker system df
du -sh /www/wwwroot/knowledge-kb/backend/uploads
du -sh /var/lib/docker
```

不要直接删除 PostgreSQL Volume。清理 Docker 构建缓存前，先确认没有正在使用的镜像：

```bash
docker builder prune
```

## 15. 安全要求

1. `.env` 必须保持 `600` 权限。
2. 不要提交 `.env`、数据库备份、Cookie、管理员密码或集成密钥。
3. PostgreSQL 和 Redis 不应直接暴露公网。
4. 后端 `8000` 应只监听 `127.0.0.1`，由 Nginx 对外代理。
5. 当前公网入口是 HTTP，正式环境应配置 HTTPS。
6. SSH 和宝塔面板端口应限制来源 IP。
7. 服务器 root 密码曾经通过临时方式使用后，应及时更换并改用 SSH 密钥。
8. 数据库备份和媒体备份应保存到服务器之外的位置。
9. 生产环境不要使用默认管理员密码。

## 16. 当前部署版本检查

服务器上查看代码提交：

```bash
cd /www/wwwroot/knowledge-kb
git rev-parse --short HEAD
git status --short --branch
```

查看正在运行的镜像：

```bash
docker images --format 'table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}'
```

查看 Compose 配置是否包含预期服务：

```bash
docker compose \
  -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  config --services
```

