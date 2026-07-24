# 云数据库 SQL

数据库目录提供两份内容一致的空白完整库：

- `knowledge-kb-schema.sql`：用于 `psql` 命令行导入；
- `knowledge-kb-schema-console.sql`：不含 `\restrict`、`COPY`、`\.` 等
  `psql` 元命令，用于云平台网页 SQL 控制台。

两份文件都包含：

- 全部业务表、约束、序列和索引；
- `vector(1024)` 字段及 HNSW 索引；
- Alembic 当前版本标记；
- 四个基础知识分类；
- 不包含本机测试知识、用户、登录会话或任何令牌。

## 导入

先在云平台创建空 PostgreSQL 数据库，并确认 `vector` 扩展已安装在
`public` schema（SQL、Alembic 和向量索引均使用 `public.vector`）。
如果云平台将扩展固定安装在其他 schema，当前版本不兼容，不能继续导入。

```bash
psql "$DATABASE_URL" \
  --single-transaction \
  --set ON_ERROR_STOP=1 \
  --file database/knowledge-kb-schema.sql
```

导入命令强制使用单一事务；任何建表、索引或约束失败都会整体回滚，不会留下半套库。

云平台只提供网页 SQL 控制台时，打开
`database/knowledge-kb-schema-console.sql`，一次性执行整份文件。该文件自带
`BEGIN` 和 `COMMIT`，执行工具必须支持整段脚本，且应在首个错误时停止。

`DATABASE_URL` 中的用户名或密码如果包含 `@`、`/`、`:`、`#`、`%` 等
保留字符，必须先做 URL 百分号编码，例如 `@` 编码为 `%40`、`#` 编码为
`%23`。不要把未编码的密码直接拼入连接 URL。

SQL 只需要在首次建立空库时导入一次。之后后端通过 `DATABASE_URL` 直接读写云数据库，不需要每次导出、上传 SQL。

## 管理员

SQL 文件不包含固定管理员或默认密码。首次部署时必须在 `.env` 设置：

```dotenv
INITIAL_ADMIN_USERNAME=knowledge-admin
INITIAL_ADMIN_PASSWORD=至少12位的强密码
ALLOW_INSECURE_DEFAULT_ADMIN=false
```

数据库初始化容器会在结构验证完成后创建首个超级管理员。

首次创建成功后可以从 `.env` 删除这两个值；后续部署会检测已有的启用超级管理员。只有显式设置 `INITIAL_ADMIN_FORCE_RESET=true` 时，才会修改已有同名账号。

## 媒体文件

图片和视频本体不放入 PostgreSQL。云模式使用 S3 兼容对象存储，SQL 中仅保存对象 key、文件名和描述信息。备份时需要同时备份：

1. PostgreSQL；
2. S3 bucket 或对应对象存储。

## 后续备份

```bash
pg_dump "$DATABASE_URL" --format=custom --no-owner --no-privileges \
  --file "knowledge-kb-$(date +%Y%m%d-%H%M%S).dump"
```

备份文件可能包含知识内容、账号哈希和审计数据，不得提交到 Git。
