# 答疑中台知识库部署说明

## 推荐服务器

- 系统：Ubuntu 22.04 LTS 或 Ubuntu 24.04 LTS
- 配置：2 核 4GB 起步，4 核 8GB 更稳
- 硬盘：80GB 推荐
- 安全组：先放行 22 和 8000，后续上域名再放行 80 和 443

## 首次部署

服务器安装 Docker 和 Docker Compose 后，把项目上传到服务器，例如：

```bash
cd /opt
git clone <你的仓库地址> knowledge-kb
cd knowledge-kb
cp .env.example .env
```

检查 `.env`，然后启动：

```bash
bash scripts/deploy.sh
```

等价命令：

```bash
docker compose -p knowledge-kb up -d --build
```

访问：

```text
http://服务器公网IP:8000/
http://服务器公网IP:8000/app
```

默认管理员：

```text
用户名：Weichizhuo
密码：123456
```

## 日常更新

本地开发完成后提交并推送：

```bash
git add .
git commit -m "更新功能"
git push
```

服务器同步并重启：

```bash
cd /opt/knowledge-kb
git pull
bash scripts/deploy.sh
```

如果不用 Git，可以重新上传项目文件覆盖服务器项目目录，然后执行：

```bash
docker compose -p knowledge-kb up -d --build
```

## 离线镜像部署

如果服务器拉 Docker Hub 或 Python 基础镜像很慢，可以在本地先构建好后端镜像，再上传到服务器。

本地构建：

```bash
docker build -t knowledge-kb-backend:latest -f backend/Dockerfile .
```

导出镜像：

```bash
docker save -o knowledge-kb-backend.tar knowledge-kb-backend:latest
```

把项目代码和 `knowledge-kb-backend.tar` 上传到服务器后，在服务器导入镜像：

```bash
docker load -i knowledge-kb-backend.tar
```

然后启动依赖和后端：

```bash
docker compose -p knowledge-kb up -d
```

如果服务器完全不能访问镜像仓库，还需要提前在本地导出依赖镜像：

```bash
docker pull postgres:16-alpine
docker pull redis:7-alpine
docker pull elasticsearch:8.15.0
docker save -o knowledge-kb-deps.tar postgres:16-alpine redis:7-alpine elasticsearch:8.15.0
```

服务器导入：

```bash
docker load -i knowledge-kb-deps.tar
docker load -i knowledge-kb-backend.tar
docker compose -p knowledge-kb up -d
```

## 常用运维命令

查看容器：

```bash
docker compose -p knowledge-kb ps
```

查看后端日志：

```bash
docker logs -f kb-backend
```

重启后端：

```bash
docker restart kb-backend
```

停止服务：

```bash
docker compose -p knowledge-kb down
```

不要随便执行：

```bash
docker compose -p knowledge-kb down -v
```

`-v` 会删除数据库、Redis、Elasticsearch 的数据卷。

## 数据位置

PostgreSQL、Redis、Elasticsearch 数据保存在 Docker volume 中。更新代码、重新构建后端镜像、重启容器都不会删除数据。

上传文件保存在 `backend/uploads`，当前 Compose 会挂载为宿主机目录，便于备份。

## 备份建议

定期备份数据库：

```bash
docker exec kb-postgres pg_dump -U knowledge_admin knowledge_base > backup.sql
```

恢复数据库前先确认目标库可以覆盖。
