# 自适应部署

部署脚本会同时选择数据库模式和 Qwen3 运行模式。

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

生产服务器应明确设置：

```dotenv
DEPLOY_DATABASE_MODE=cloud
```

云模式不会加载本地 PostgreSQL 服务，也不会在云连接失败后回退本地。部署前会检查：

- `DATABASE_URL` 已配置；
- 媒体存储为 `s3` 且 bucket 已配置；
- 固定 S3 Access Key 与 Secret Key 成对配置；
- `INTEGRATION_API_KEY` 已替换为至少 24 位的非占位密钥；
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

## Qwen3 运行模式

部署脚本始终使用 `Qwen/Qwen3-Embedding-0.6B` 和 1024 维向量，只选择 GPU 或 CPU，不会替换模型。

`auto` 会先验证 Docker 能否启动配置的 GPU 镜像。验证通过时使用 `docker-compose.embedding-gpu.yml`，否则使用 `docker-compose.embedding-cpu.yml`。

```bash
./scripts/deploy.sh --runtime gpu
./scripts/deploy.sh --runtime cpu
```

GPU 显式模式预检失败会直接终止；自动模式才允许切换到 CPU。

## 完成条件

脚本只在以下真实检查全部通过后报告成功：

1. 数据库迁移容器退出码为 0；
2. 数据库、pgvector、迁移版本和向量索引正确；
3. 后端 `/ready` 可用；
4. Qwen3 返回一个真实的 1024 维向量；
5. 仅在前四项通过后，媒体存储执行一次上传、读取和删除探针。

媒体探针失败会立即终止，不会循环重复写入。部署超时会停止仍在运行的
数据库初始化容器，但不会删除任何数据卷。任何步骤失败都会输出迁移、
Embedding 和后端日志。Docker Desktop/WSL 的 GPU 配置默认预加载
`/usr/lib/x86_64-linux-gnu/libcuda.so.1`，其他宿主机可通过
`TEI_GPU_LD_PRELOAD` 调整。
