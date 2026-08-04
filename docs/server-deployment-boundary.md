# 生产服务器部署边界与安全更新说明

> 最后只读核验：2026-07-27
> 目的：明确本项目在共享服务器上的部署范围。后续任何运维操作只能作用于本文件列出的项目资源，不能影响服务器上的其他网站、服务、数据或容器。

## 1. 已确认的项目范围

服务器上的本项目根目录：

```text
/www/wwwroot/knowledge-kb
```

只允许在上述目录及其子目录中读取、更新和部署。本项目的代码、Compose 配置、后端上传文件和运行日志均应从该目录进入后再操作。

不得因为目录名称相似、端口相同或容器命令方便而操作其他项目目录。

## 2. 已确认的运行服务

Docker 面板于 2026-07-27 显示以下知识库专属容器：

| 容器 | 当前状态 | 用途 | 操作边界 |
|---|---|---|---|
| `kb-backend` | 运行中 | FastAPI 后端与前端页面 | 仅允许通过项目 Compose 命令重建或重启 |
| `kb-postgres` | 运行中 | PostgreSQL + pgvector 主数据 | 不直接删除容器、卷或数据目录 |
| `kb-redis` | 运行中 | Redis 基础设施服务 | 不作为知识主数据备份来源 |
| `kb-embedding-qwen` | 运行中 | Qwen3 Embedding 服务 | 不替换模型或清理模型缓存，除非变更已评审 |
| `kb-migrate` | 已停止 | 数据库迁移初始化任务 | `Exited (0)` / 已停止是迁移完成后的正常状态，不应手动启动来“修复”服务 |

后端当前仅绑定本机回环地址：

```text
127.0.0.1:8000 -> 8000/tcp
```

公网访问必须经项目专属 Nginx/宝塔站点反向代理进入。不得把 `8000`、PostgreSQL 或 Redis 直接暴露到公网。

## 3. 项目专属更新配置

服务器已完成首次部署，后续发布均按“更新现有服务”处理，不得默认重新部署整套环境。

当前服务器只使用项目目录中的以下 Compose 文件组合：

```text
docker-compose.yml
docker-compose.local.yml
docker-compose.embedding-cpu.yml
```

当前服务器按本地 PostgreSQL、本地媒体目录和 CPU Embedding 的方案运行。普通后端或前端代码更新时，前端会随 `kb-backend` 镜像一起更新，只允许构建和替换后端：

```bash
cd /www/wwwroot/knowledge-kb
git fetch origin
git pull --ff-only origin master

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  build backend

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  up -d --no-deps backend
```

`--no-deps` 是服务器日常更新的必要保护，确保不会重建或重启 PostgreSQL、Redis、Qwen Embedding 和迁移容器。

首次启用答疑插件的服务器检索时，只在项目 `.env` 中新增一个与
`INTEGRATION_API_KEY` 不同、至少 24 位的 `RETRIEVAL_API_KEY`，然后仍按上面的
`build backend` 和 `up -d --no-deps backend` 增量替换后端。不要为增加密钥
重新执行全量部署脚本，也不要重建 PostgreSQL、Redis 或 Embedding。更新后应
分别验证：检索专用密钥能访问 `standard-search` 和
`retrieval-events:batch`，但不能访问 `taxonomy`；上游密钥的访问范围正好相反。

如果本次代码包含新的 Alembic 迁移，应只额外构建并运行迁移服务，再更新后端：

```bash
docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  build migrate backend

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  run --rm migrate

docker compose -p knowledge-kb \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  -f docker-compose.embedding-cpu.yml \
  up -d --no-deps backend
```

只有首次安装，或经过评审的数据库、Redis、Embedding、Compose 拓扑变更，才允许使用 `scripts/deploy.sh`。日常代码更新禁止执行全项目 `up -d --build`、`--remove-orphans` 或重新构建 Embedding。

更新前必须先检查：

```bash
cd /www/wwwroot/knowledge-kb
git status --short --branch
docker inspect kb-backend kb-postgres kb-redis kb-embedding-qwen \
  --format '{{.Name}} {{.State.Status}} {{.State.StartedAt}} {{.Image}}'
```

如果服务器工作区存在未提交修改、未知文件或当前分支不是预期分支，必须停止更新并先确认原因；不得用强制拉取、重置分支或覆盖文件的方式继续。

更新完成后再次执行同一条 `docker inspect`。普通后端更新只允许 `kb-backend` 的启动时间或镜像发生变化；PostgreSQL、Redis 和 Embedding 必须保持不变。

## 4. 每次更新后的最低验收

增量更新完成后，至少执行：

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/ready
```

并确认：

1. `kb-backend`、`kb-postgres`、`kb-redis`、`kb-embedding-qwen` 处于运行状态；
2. 没有数据库迁移时，`kb-migrate` 保持原有 `Exited (0)` 状态；有迁移时，本次迁移任务退出码必须为 0；
3. 登录页、管理后台和知识库关键接口可用；
4. 媒体上传与读取路径未改变；
5. 更新后的 Git 提交号与目标发布提交一致。

不能只凭 Docker 容器显示“运行中”就宣布部署完成。

## 5. 明确禁止的操作

以下命令或行为默认禁止，除非经过明确确认并已有可用备份：

```bash
docker compose down -v
docker system prune
docker volume prune
docker container prune
git reset --hard
git clean -fd
```

同样禁止：

- 删除或编辑其他项目的 Docker 容器、镜像、存储卷、数据库、Nginx 配置或网站目录；
- 打开、复制、提交或打印 `.env` 中的密码、密钥和连接串；
- 直接删除 `backend/uploads` 中的媒体文件；
- 用泛化的 Docker 清理命令处理本项目问题；
- 未经确认切换到 GPU 运行时、替换 Embedding 模型或改变 `EMBEDDING_DIMENSIONS=1024`；
- 将服务器管理后台地址、登录凭据、Cookie、数据库备份或集成密钥写入仓库。

## 6. 数据保护与故障处理

知识主数据位于 PostgreSQL，媒体本体位于项目的 `backend/uploads`。备份与恢复必须同时处理数据库和媒体文件。

发生异常时，优先按以下顺序排查，避免扩大影响：

1. 查看项目专属容器状态和日志；
2. 检查 `/health` 与 `/ready`；
3. 检查项目目录的 Git 状态和部署脚本输出；
4. 仅在确认故障归属本项目后，再处理对应容器或配置；
5. 涉及数据库恢复、媒体删除、Docker 卷或 Nginx 变更时，先备份并取得确认。

更完整的服务器部署、备份、恢复和 Nginx 运维说明见 `docs/deployment-operations-guide.md` 对应的文档分支；该文档尚未合并到 `master` 时，不应假定它已随主线代码发布。
