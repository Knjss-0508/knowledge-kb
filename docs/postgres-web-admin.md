# PostgreSQL 网页查询界面（Adminer）

> 适用：宝塔面板服务器上的 `knowledge_base`
> 脚本：`scripts/build-php-pgsql-extension.sh`、`scripts/deploy-adminer.sh`

## 一、为什么需要它

宝塔的 **PostgreSQL 管理器插件只做运维**：安装卸载、启停、改 `postgresql.conf`、
改 `pg_hba.conf`、备份恢复、改密码、扩展管理。**没有表浏览，也没有 SQL 窗口。**

phpMyAdmin 是 **MySQL 专用**，读不了 PostgreSQL。这是宝塔的产品缺口，不是配置问题。

面板「数据库」页里能看到 `knowledge_base`，但那是靠
`/www/server/panel/plugin/pgsql_manager_dbuser_info.json` 里的一行登记，
只提供备份 / 改密 / 删除入口，**点不进去看数据**。

## 二、方案

```
你的电脑（Tailscale 客户端）
      │  WireGuard 隧道（自带加密）
      ▼
http://<tailnet-ip>:8090          ← nginx 只绑这个地址，公网扫不到
      │
      ├─ 第一层：HTTP Basic 认证
      ├─ 第二层：Adminer 登录表单
      ▼
PHP-FPM ──pgsql 扩展──▶ PostgreSQL @ 127.0.0.1:5432
```

**为什么不挂公网域名**：需要加 DNS 记录 + 证书，且必须配 IP 白名单，
等于给生产库开一个公网入口。走 Tailscale 则零暴露面，且传输已加密。

## 三、执行顺序

```bash
# 1. 先编译 PHP 的 PostgreSQL 驱动（硬前提）
sudo bash scripts/build-php-pgsql-extension.sh

# 2. 再部署 Adminer
sudo bash scripts/deploy-adminer.sh

# 3. 随时做安全检查
sudo bash scripts/deploy-adminer.sh --verify

# 4. 卸载站点
sudo bash scripts/deploy-adminer.sh --uninstall
```

两个脚本都是幂等的，可以重复执行。

## 四、四个必须知道的坑

### 坑 1：宝塔的 PHP 默认没有 PostgreSQL 驱动

```
pgsql:     ❌ 缺失
pdo_pgsql: ❌ 缺失
（只有 pdo_mysql、pdo_sqlite）
```

任何 PHP 版 PostgreSQL 工具都跑不起来。而且宝塔 PHP 的
`Scan for additional .ini files in: (none)` —— **没有 ini 扫描目录**，
扩展只能写进 `php.ini`。

必须从源码编译。**关键点：`--with-*` 要传「目录」而不是 `pg_config` 全路径。**

```bash
# PHP 7.4 的 ext/*/config.m4 里是：
#   AC_PATH_PROG(PGCONFIG, pg_config, no, $PHP_PGSQL/bin:$PATH)
# 传 pg_config 全路径会让它去找 <路径>/bin/pg_config，必然报
#   configure: error: Cannot find libpq-fe.h
./configure --with-php-config=/www/server/php/74/bin/php-config \
            --with-pdo-pgsql=/www/server/pgsql        # 目录，不是 .../bin/pg_config
```

还要注意：**Adminer 源码里 `pgsql` 出现 42 次、`pdo_pgsql` 只出现 1 次** ——
`pgsql` 才是它真正用的驱动。两个都要编。

写入时要**同时写 `php.ini`（FPM 用）和 `php-cli.ini`（CLI 用）**，
否则会出现「命令行能用、网页不能用」的假象。

### 坑 2：凭据文件不能放在 `/www/server/panel/vhost/` 下

```
[crit] open() ".../adminer-tailnet.htpasswd" failed (13: Permission denied)
```

`/www/server/panel/vhost` 和它的 `nginx` 子目录权限是 **`drw-------` (600)**，
nginx worker 以 `www` 身份运行，**无法穿透**。

最阴的地方：**`nginx -t` 是用 root 跑的，检查时不报错** ——
只有真实请求才暴露，表现为「先 401，紧接着 500」。

正确做法：放到 `/etc/adminer-tailnet/htpasswd`（目录 755、文件 640 `root:www`）。

### 坑 3：Adminer 6.1.0 在 PHP 7.4 下登录必失败

症状极具误导性：

1. 登录 POST 返回 **302**（看起来成功了）
2. 跟跳转返回 **403 Forbidden**
3. 然后**静默退回登录页，不报任何错误**

排查时逐一排除过的方向（**都不是原因**）：

| 排查方向 | 排除依据 |
|---|---|
| PHP 扩展缺失 | 探针证实 FPM 侧 `pgsql=YES`、`pg_connect` 成功读到 18,630 条 |
| 密码含特殊字符 | 密码是纯字母数字 16 位，无引号/反斜杠/空格 |
| 会话丢失 | 探针证实会话保持（counter 1→2，cookie 稳定） |
| CSRF token 缺失 | 带上 token 仍然失败 |
| 暴力破解锁定 | 删掉 `/tmp/adminer-invalid` 后仍然失败 |
| `sslmode=` 空值 | 显式传 `ssl[mode]=prefer` 仍然失败 |

**解决：换成 Adminer 4.8.1**（明确支持 PHP 5.3~7.4），一次通过。

> **6.1.0 的 PHP 7.4 语法检查是通过的。**
> 所以 `php -l` 只能验证语法，**不能替代运行时验证**。

### 坑 4：备份文件留在网站根目录会被当 PHP 执行

`index.php.v6-backup` 这类文件放在网站根目录，**请求它就会被 PHP 解析执行**。
备份要移到网站根目录之外（例如 `/root/adminer-backup/`）。
`deploy-adminer.sh --verify` 会检查这一点。

## 五、登录信息

**第一层（HTTP Basic）**

- 用户名：由 `BASIC_USER` 决定，默认 `admin`
- 密码：脚本随机生成，写入 `/root/.adminer-tailnet-password`（权限 600）

**第二层（Adminer 登录表单）**

| 字段 | 值 |
|---|---|
| System | `PostgreSQL` |
| Server | `127.0.0.1` |
| Username | `.env` 的 `DATABASE_URL` 里内嵌的用户 |
| Password | `.env` 的 `DATABASE_URL` 里内嵌的密码 |
| Database | `knowledge_base` |

> ⚠️ **真实 `.env` 里没有 `POSTGRES_PASSWORD` 这个独立变量** —— 密码是内嵌在
> `DATABASE_URL` 里的（形如 `postgresql://user:PASSWORD@host:5432/db`）。
> `.env.example` 里的 `POSTGRES_PASSWORD=knowledge_pass_2026` 是**示例占位值**，
> 用它登录会 `FATAL: password authentication failed`。

> 补充：因为 `pg_hba.conf` 第一条是 `host all all 127.0.0.1/32 trust`
> （**首条匹配优先**），Adminer 从本机连库时密码**实际不被校验**，
> 随便填也能进。但仍然照填 —— 不要依赖这个特性。

## 六、安全边界（部署后应逐项确认）

| 检查项 | 期望 |
|---|---|
| nginx 监听范围 | 只有 `<tailnet-ip>:8090`，**不是** `0.0.0.0` |
| 公网 `<公网IP>:8090` | TCP **不通** |
| 公网 `<公网IP>:5432` | TCP **不通**（靠云安全组） |
| 无认证访问 | HTTP **401** |
| 防火墙规则 | 只放行 `100.64.0.0/10` → 8090 |
| 网站根目录 | 只有 `index.php` |

> ⚠️ 服务器 nftables 里有一条**无条件**的 `tcp dport 5432 accept`。
> 目前靠云安全组兜着，属于潜在风险 —— 若安全组放宽，数据库会直接暴露。
> 建议收窄为只放行 Tailscale 网段。

## 七、日常维护

**不要用宝塔面板修改 PHP 设置** —— 面板可能重写 `php.ini`，会丢掉
`extension=pgsql.so` / `extension=pdo_pgsql.so` 两行。改完务必检查：

```bash
/www/server/php/74/bin/php -m | grep -i pgsql
```

**升级 Adminer 前先在测试环境验证登录** —— 见坑 3。

**连续登录失败会被静默锁定**（不报错，只退回登录页）：

```bash
rm -f /tmp/adminer-invalid     # 解锁
```

**相关文件**

| 路径 | 说明 |
|---|---|
| `/www/wwwroot/adminer-tailnet/index.php` | Adminer 主程序 |
| `/www/server/panel/vhost/nginx/adminer-tailnet.conf` | nginx 站点配置 |
| `/etc/adminer-tailnet/htpasswd` | Basic 认证凭据（640 root:www） |
| `/root/.adminer-tailnet-password` | Basic 认证明文密码（600） |
| `/www/wwwlogs/adminer-tailnet.log` / `.error.log` | 访问与错误日志 |

## 八、备选方案

| 方案 | 评价 |
|---|---|
| **桌面客户端（DBeaver / pgAdmin）** | **日常开发首选** —— 走 Tailscale 直连 `<tailnet-ip>:5432`，功能远强于 Adminer（ER 图、数据比对、批量导出）。Adminer 只是补上「浏览器里随手看一眼」的场景 |
| 换 MySQL | **不可行** —— pgvector 无处可去：4 个 `vector(1024)` 列、2 个 HNSW 索引、3 处 `cosine_distance()` 调用、28 个 Alembic 迁移。MySQL 无 HNSW 索引、无可走索引的 `<=>` 余弦运算符，召回会从 2.6ms 的索引检索退化成 28ms 全表扫描，数据量一涨线性恶化 |
| phpPgAdmin | 已停更，PHP 7.4 不兼容 |
| 公网子域名部署 | 需要 DNS + 证书 + IP 白名单，等于给生产库开公网入口 |

## 九、相关文档

- `docs/postgres-recall-tuning.md` —— 召回性能调优（库级 HNSW 参数）
- `docs/server-deployment-boundary.md` —— 服务器部署边界
