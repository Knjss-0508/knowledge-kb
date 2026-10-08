# 线上生产环境隐患体检报告（2026-10-08）

> 体检方式：**只读检查**（不改配置、不重启服务、不动数据）。
> 体检对象：`81.71.6.245`（knowledgekb.powerzhuan.cn）上的 CZ 知识库本体及同机其它项目。
> 结论：**站点本身健康**（容器全部 healthy、后端近 24 小时 0 错误、外网访问正常），但存在 1 个高危、3 个中危隐患，详见下文。

---

## 一、结论摘要（先看这张表）

| 编号 | 隐患 | 等级 | 影响 | 建议动作 | 预计耗时 |
|---|---|---|---|---|---|
| R1 | **知识库数据库从未纳入自动备份**（唯一一份是 9/7 手动备份） | 🔴 高 | 数据库损坏只能回到 9/7，丢一个月业务数据 | 在宝塔面板为 `knowledge_base` 建每日备份任务并做一次恢复演练 | 30 分钟 |
| R2 | 数据库本机"免密即超级用户"（`trust`） | 🟠 中高 | 任何人只要能上机器，就直通数据库最高权限 | 改为密码认证（需先摸清宝塔备份脚本连库方式） | 1 小时 |
| R3 | SSH 允许 root 密码登录 + 无 fail2ban | 🟠 中高 | 已有 148,232 次爆破记录，暴力破解成功即拿到 root | 关闭密码登录、启用密钥、安装 fail2ban | 40 分钟 |
| R4 | MySQL 3306 对公网开放 | 🟡 中 | 攻击面暴露在互联网 | 腾讯云安全组关闭 3306 入站 | 15 分钟 |
| R5 | 日志无切割，单文件 8.3GB；系统邮件已满 | 🟡 中 | 继续增长会吃满磁盘（当前剩 47G） | 配置日志切割 + 清空邮件文件 | 30 分钟 |
| R6 | 线上代码与 git 不一致（140 项改动、HEAD 停在 9/17） | 🟡 中 | 出问题无法追溯、回滚只能靠镜像 | 建立版本基线流程（不急于一次做完） | 半天 |
| R7 | 证书续期链路未确认 | 🟡 中 | 知识库证书 11/16 到期；另个项目 10/25 到期 | 提前一周手工验证续期 | 20 分钟 |
| R8 | 内存余量偏紧 | 🟢 低 | 可用 4.3G / 15G，embedding 占 3.2G | 继续观察，暂不动 | — |
| R9 | 宝塔"项目守护进程"任务来源不明 | 🟢 低 | 与 10-08 容器被删事故时间接近，但未证实 | 已决定不追查；保留本报告线索备查 | — |

**已确认没问题的部分**（不必重复排查）：

- 三个容器 `restart=unless-stopped` + healthy，机器重启会自动拉起（`/etc/rc.d/rc3.d/` 有 S55nginx、S55pgsql、S56redis、S64mysqld，`docker.service` enabled）。
- 后端近 24 小时错误计数 `0`；Docker 日志未膨胀（最大的 `*-json.log` 仅 164K）。
- 保留策略任务 `30 3 * * *` 正常执行（10-08 03:30 输出"无需清理"）。
- docker 的 iptables 链完整（含 `DOCKER-FORWARD` / `DOCKER-BRIDGE`）；每 5 分钟的网络自愈脚本已修复并验证。
- 嵌入服务（`kb-embedding-qwen`）已恢复，`POST /v1/embeddings` 返回 200、维度 1024。

---

## 二、逐项详情与修复步骤

### 🔴 R1 知识库数据库没有新备份（最高优先）

**证据（已定位到根因）**

- 备份目录 `/www/backup/database/pgsql/knowledge_base/` 内**只有一个文件**：
  `knowledge_base_2026-09-07_10-31-13_pgsql_data.sql.gz`（62,784,908 字节，**9 月 7 日**）。
- 宝塔备份台账 `/www/server/panel/data/db/backup.db`（表 `backup`）里 knowledge_base **只有 1 条记录**：

  ```
  id=40  name=knowledge_base_2026-09-07_10-31-13_pgsql_data.sql.gz
         size=62784908  addtime=2026-09-07 10:31:24  ps=手动备份  cron_id=0
  ```

  `ps=手动备份`、`cron_id=0` 说明：**这份备份是 9 月 7 日人工点出来的，从来没有被纳入任何定时任务**。也就是说，从建库到现在，知识库一直处在"没有自动备份"的状态。
- 另有一个名字带空格的目录 `/www/backup/database/pgsql/knowledge base/`（9 月 9 日 11:08 创建，**空的**）——同一天面板日志有 `pgsqlModel.py:InputSql 执行权限修复SQL文件` 的调试记录，属于一次带空格库名的误操作残留，可以清掉。
- 宝塔每日 04:00 的任务 `panel/script/backup.py database ALL 7` 仍在运行（最近 2026-10-08 04:00:48 显示 Successful），但它**只管已配置的备份对象**，knowledge_base 从未被加进去，所以一直没产出。
- 好消息：面板的数据库台账 `/www/server/panel/data/db/database.db`（表 `databases`）**已经登记了这个库**：

  ```
  id=3  name=knowledge_base  username=knowledge_admin  type=PgSql  ps=知识库中台  addtime=2026-09-05 11:58:10
  ```

  所以**在面板里可以直接为它建备份任务**，不用手工写脚本。
- 数据库现状：`public` schema 共 **34 张表**、**1050 MB**；实例内共 4 个库（`postgres` 7686 kB、`template1`、`template0`、`knowledge_base` 1050 MB）；角色只有 `postgres`（超级用户）和 `knowledge_admin`（普通用户）。

**为什么要紧**

备份是最后一道防线。现在这道防线停在 9 月 7 日，中间一个月的业务数据（录入、标注、置信度训练结果等）**没有任何副本**。

**修复方案（二选一，推荐 A）**

**方案 A：用宝塔面板加一个 PostgreSQL 备份任务（推荐，界面操作，库已在台账里）**

1. 登录宝塔面板 → 左侧「计划任务」→ 添加任务。
2. 任务类型：**备份数据库**；数据库类型：**PostgreSQL**；数据库：`knowledge_base`；备份保留：`7` 份；执行周期：**每天 05:00**（避开 01:30、04:00 已有任务）。
3. 保存后点「执行」试跑一次，然后确认目录里出现当天的新文件，并且台账里多了一条 `ps` 不是"手动备份"的记录：

```bash
ls -lh /www/backup/database/pgsql/knowledge_base/
```

**方案 B：用脚本 + crontab（面板里选不到 PostgreSQL 类型时使用）**

新建 `/opt/knowledge-kb/scripts/backup-pg-knowledge-base.sh`：

```bash
#!/usr/bin/env bash
# 每日备份 knowledge_base 库，保留 7 天
set -euo pipefail
DEST=/www/backup/database/pgsql/knowledge_base
PG_DUMP=/www/server/pgsql/bin/pg_dump      # 本机唯一存在的 pg_dump 路径
mkdir -p "$DEST"
STAMP=$(date +%F_%H-%M-%S)
OUT="$DEST/knowledge_base_${STAMP}_pgsql_data.sql.gz"
"$PG_DUMP" -h 127.0.0.1 -U postgres -d knowledge_base --no-owner | gzip -6 > "$OUT"
gzip -t "$OUT"                              # 能完整解压才算成功
echo "[$(date '+%F %T')] backup ok: $OUT ($(stat -c%s "$OUT") bytes)"
find "$DEST" -name 'knowledge_base_*.sql.gz' -mtime +7 -delete
```

> ⚠️ **与 R2 的联动**：上面的 `-U postgres` 之所以能免密连上，靠的是 `pg_hba.conf` 里的 `host all all 127.0.0.1/32 trust`。**如果你之后按 R2 去掉了 `trust`，这个脚本会立刻失败**。所以两件事要一起做：先在 `/www/server/pgsql/.pgpass` 写入密码（权限 600，内容形如 `127.0.0.1:5432:knowledge_base:postgres:<密码>`），脚本里用 `export PGPASSWORD` 或 `--no-password` + `.pgpass`，**再**改 `pg_hba.conf`。**不要把密码写进脚本或 crontab**（crontab 全员可读）。

加入 crontab：

```cron
0 5 * * * /bin/bash /opt/knowledge-kb/scripts/backup-pg-knowledge-base.sh >> /var/log/kb-pg-backup.log 2>&1
```

**验收（必做：恢复演练，否则备份等于没有）**

```bash
# 1) 备份文件能完整解压
gzip -t /www/backup/database/pgsql/knowledge_base/knowledge_base_*.sql.gz && echo OK

# 2) 恢复到临时库，比对表数量（不碰生产库）
createdb -h 127.0.0.1 -U postgres kb_restore_test
gunzip -c /www/backup/database/pgsql/knowledge_base/knowledge_base_*.sql.gz | psql -h 127.0.0.1 -U postgres -d kb_restore_test -q
psql -h 127.0.0.1 -U postgres -d kb_restore_test -c "select count(*) from information_schema.tables where table_schema='public';"
# 期望：34

# 3) 清理临时库
dropdb -h 127.0.0.1 -U postgres kb_restore_test
```

**回滚**：删除新增的宝塔任务或 crontab 行即可，备份文件本身不影响线上。

---

### 🟠 R2 数据库本机免密（`pg_hba.conf` 里的 `trust`）

**证据**（`/www/server/pgsql/data/pg_hba.conf` 生效规则）

```
local   all             all                                     trust
host    all             all             127.0.0.1/32            trust
host    all             all             ::1/128                 trust
local   replication     all                                     trust
host    replication     all             127.0.0.1/32            trust
host    replication     all             ::1/128                 trust
host    knowledge_base   knowledge_admin    127.0.0.1/32    md5
host    knowledge_base    knowledge_admin    172.17.0.0/16    scram-sha-256
host    knowledge_base    knowledge_admin    172.18.0.0/16    scram-sha-256
host    knowledge_base    knowledge_admin    100.72.97.89/32    scram-sha-256
```

`trust` = **不校验密码，直接放行**，而且作用范围是 `all`（所有库、所有用户），也就是**本机任意用户都能以超级用户身份进库**。好消息是**没有 `0.0.0.0/0` 规则**，外网连不进来（实测 5432 从外网也不通）。

**为什么是中高危**

它本身不对外暴露，但和 R3 组合起来就是"SSH 一旦被攻破 → 数据库毫无第二道门"。属于典型的"纵深防御缺失"。

**修复步骤（务必先摸清依赖，再改）**

1. 先备份并排查谁会本机免密连库：

```bash
cp -a /www/server/pgsql/data/pg_hba.conf /www/server/pgsql/data/pg_hba.conf.bak-$(date +%Y%m%d)
# 宝塔备份脚本、任何本机脚本、psql 定时任务都要确认它们用什么身份连库
grep -rn "psql\|pg_dump" /www/server/panel/script/ /opt/knowledge-kb/scripts/ 2>/dev/null | head -20
```

2. 给这些脚本配置好密码（`.pgpass`，权限 600），**再**把两条 `trust` 改成：

```
local   all   all                        scram-sha-256
host    all   all        127.0.0.1/32    scram-sha-256
host    all   all        ::1/128         scram-sha-256
```

> ⚠️ **特别注意 `local` 那一行**：宝塔面板自己管理 PostgreSQL 时很可能就是走本地 socket 免密（本机没有单独的 `.pgpass` 就是这个原因）。如果一次改得太狠，面板的「数据库」「备份」页面可能立刻报错。
> **稳妥做法（分两步）**：先只改 `host ... 127.0.0.1/32 trust`（只影响走 TCP 的脚本），确认面板功能正常、备份正常；再把 `local all all trust` 收窄为 `local all postgres trust`（只给 postgres 用户保留本机 socket 免密），而不是直接全改密码。这样既堵住"任意本机用户免密即超级用户"，又不打断面板。

3. 热加载（不重启库、不断连接）：

```bash
/www/server/pgsql/bin/pg_ctl reload -D /www/server/pgsql/data
```

4. 验收：应用侧功能正常（`curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8000/health` → 200），宝塔备份任务下一次执行仍 Successful。

**回滚**：`cp -a /www/server/pgsql/data/pg_hba.conf.bak-<日期> /www/server/pgsql/data/pg_hba.conf && pg_ctl reload`。

---

### 🟠 R3 SSH 加固

**证据**

- `/etc/ssh/sshd_config:40 PermitRootLogin yes`、`:45 PubkeyAuthentication yes`
- `/etc/ssh/sshd_config.d/50-cloud-init.conf:1 PasswordAuthentication yes`
- `/var/log/secure` 中 `Failed password` 累计 **148,232 次**；**未安装 fail2ban**。

**修复前置条件（必须先确认，否则会把所有人锁在门外）**

- [ ] 确认你和同事**都有可用的 SSH 私钥**，并且已经验证能免密登录；
- [ ] 确认宝塔面板能正常打开（改错了还能从面板的"终端"进去救）；
- [ ] 记录腾讯云控制台的 VNC/救援登录入口。

**步骤**

1. 先加白名单（避免误封自己）：

```bash
# 把公司出口 IP 加入白名单（示例，按实际情况填）
iptables -I INPUT -s 218.17.233.0/24 -p tcp --dport 22 -j ACCEPT
```

2. 安装并启用 fail2ban：

```bash
dnf install -y fail2ban || yum install -y fail2ban
systemctl enable --now fail2ban
fail2ban-client status sshd
```

3. 关闭密码登录、限制 root 只能用密钥：

```
# /etc/ssh/sshd_config.d/99-hardening.conf
PermitRootLogin prohibit-password
PasswordAuthentication no
```

```bash
sshd -t && systemctl reload sshd
```

4. **验收（关键）**：windows 侧**新开一个终端**测试密钥登录成功，再关掉当前会话；如果新会话连不上，立即从宝塔面板终端把 `PasswordAuthentication yes` 改回来并 reload。

**回滚**：删除 `/etc/ssh/sshd_config.d/99-hardening.conf` → `systemctl reload sshd`；或从宝塔面板终端操作。

---

### 🟡 R4 MySQL 3306 对外开放

**证据**：从外网实测 `81.71.6.245:3306` **可连通**；同机 PG 5432、宝塔面板 888、后端 8000 均被安全组挡住（这是好事）。MySQL 版本 5.7.44，监听 `*:3306`，`root@localhost` 用密码认证（实测空密码被拒）。

**修复（推荐在云侧做，最安全）**

1. 登录腾讯云控制台 → 该实例的**安全组** → 入站规则 → 找到放通 3306 的规则 → **删除**（或把来源改成公司出口 IP/32）。
2. 同时到宝塔面板 → 安全 → 确认 3306 没有在面板层面额外放通。

**替代方案（机器侧）**：`/etc/my.cnf` 加 `bind-address = 127.0.0.1` 后重启 MySQL。

> ⚠️ 重启 MySQL 会短暂中断使用 MySQL 的其它项目（本机还有 `voc-workbench-mysql`、`appeal-exemption-system-postgres` 等）。改之前先确认**没有外部程序依赖远程连 MySQL**（问一下同事或看连接日志）。优先用安全组方案，零中断。

---

### 🟡 R5 磁盘与日志

**证据**

- `/` 180G，已用 134G（74%），**剩 47G**；inode 仅 2%。
- `/www/wwwlogs` 占 **15G**，其中 `zzdy.powerzhuan.cn.log` **8.3GB**、岗位知识萃取舱 503MB、MHT-WMS 304MB、knowledgekb 190MB。
- `/var/log` 3.0G；`/opt/docker` 5.0G（镜像 9.535GB、其中 3.344GB 可回收；构建缓存 5.737GB、仅 178MB 可回收）。
- **`/etc/logrotate.d/` 里没有 `wwwlogs` 和 `kb-*` 的轮转配置**。
- `/var/mail/root` 已达 **51,199,695 字节（写满）**，postfix 一直退信刷屏：`cannot update mailbox /var/mail/root for user root. error writing message: File too large`。

**修复步骤**

1. 日志切割（任选）：宝塔面板 → 网站 → 设置 → **日志切割**（对每个站点开启，保留 7 天）；或手工建 `/etc/logrotate.d/wwwlogs`：

```
/www/wwwlogs/*.log {
    daily
    rotate 7
    missingok
    notifempty
    compress
    delaycompress
    copytruncate
}
```

2. 立即腾空间（zzdy 属于**另一个项目**，删除前请确认该项目不再需要历史日志）：

```bash
du -sh /www/wwwlogs/*.log | sort -rh | head
: > /www/wwwlogs/zzdy.powerzhuan.cn.log     # 清空而不删除文件（nginx 无需重启）
```

3. 清空系统邮件并加轮转：

```bash
: > /var/mail/root
# /etc/logrotate.d/mail 里确保有 /var/mail/root 的规则；确认后 postfix 退信刷屏即停止
```

4. 磁盘回收（可选，**不要用 `docker system prune -a`**，会删掉回滚镜像）：

```bash
docker image prune          # 只清 dangling（无标签）镜像，安全
docker builder prune --keep-storage 2GB   # 构建缓存限量，需要时再加
```

**验收**：`df -h /` 剩余空间上升；`tail -20 /var/log/maillog` 不再刷 `File too large`。

---

### 🟡 R6 线上代码与 git 不一致

**证据**：`/opt/knowledge-kb` 的 HEAD 是 `fd163ca3`（2026-09-17，分支 `server-preserve-20260914`），`git status --porcelain` 有 **140 项**改动。也就是说**线上跑的代码不是 git 里那一份**，无法追溯与审查。

**建议（不急，但要有路线）**

1. **短期止血**：每次部署都在 `/opt/knowledge-kb-runtime/` 下留 `.codex-deploy-<日期>-<用途>/` 目录，记录改了哪些文件、镜像 tag、前后 sha256（现有习惯已如此，继续保持）。
2. **中期**：把线上实际在跑的关键文件（后端 `backend/app/**`、`frontend/index.html`）导出成一份快照，与本地仓库 `master` 做 diff，把差异逐项"要么提交、要么回滚"，分几次 PR 收敛。
3. **长期**：服务器不能直连 GitHub（`fatal: unable to access ... Failure when receiving data from the peer`），所以仍走"本地改 → 传文件 → 服务器重建镜像"的流程；每次都用镜像 tag 标记来源 commit（例如 `knowledge-kb-backend:master-0fa63ba-20261007`）。

---

### 🟡 R7 证书续期链路

**证据（各站点证书到期时间）**

| 站点 | 到期 |
|---|---|
| 岗位知识萃取舱 | **2026-10-25**（最近） |
| MHT-WMS / mhdwms | 2026-11-15 |
| **knowledgekb.powerzhuan.cn** | **2026-11-16** |
| vocone | 2026-11-22 |
| appeal-api / upload-api | 2026-11-23 |
| zzdy | 2026-11-25 |
| zntj | 2026-12-07 |
| paixiu | 2026-12-20 |

- 知识库证书文件：`/www/server/panel/vhost/letsencrypt/knowledgekb.powerzhuan.cn/fullchain.pem`（宝塔 letsencrypt 目录）。
- acme.sh 目录里**只有** appeal-api / upload-api / vocone / zntj / zzdy 五个 `_ecc`，**没有 knowledgekb**；宝塔的续期任务 `acme_v2.py --renew_v2=1` 每天 15:25 在跑。

**待确认**：knowledgekb 的证书由谁续期（宝塔面板的 SSL 续期 vs acme.sh）。**建议在 11/09 前手工验证一次**：

```bash
# 手工触发一次续期（宝塔计划任务里点"执行"亦可），然后确认到期日被推进
openssl x509 -enddate -noout -in /www/server/panel/vhost/letsencrypt/knowledgekb.powerzhuan.cn/fullchain.pem
```

「岗位知识萃取舱」10/25 到期，请该项目负责人提前确认续期。

---

### 🟢 R8 内存余量

- 宿主：15Gi 总 / 10Gi 已用 / **4.3Gi 可用**，swap 1Gi 未使用，load 0.43。
- 占用：`kb-embedding-qwen` **3.234GiB**、`kb-backend` 1.167GiB、`voc-workbench-mysql` 438.8MiB、`appeal-api` 182.9MiB、`appeal-pg` 33MiB、`kb-redis` 8.2MiB。
- 判断：目前够用，但**再加服务或提高 embedding 并发就会紧张**。建议在宝塔面板开启内存告警（面板日志显示 10-08 09:49 已有人添加过"首页内存告警"）。

---

### 🟢 R9 宝塔"项目守护进程"任务（线索备查，不追查）

- `/www/server/panel/logs/task.log` 每 2 分钟一次「执行任务: 项目守护进程」，另有 09:50:59「检查502任务」。
- 与 10-08 09:50 的容器被删事故时间接近，但**面板并未托管 knowledge-kb 这个 compose 项目**，机制**未证实**。
- 同时间段唯一异常记录：宝塔面板 `log.db` 显示 `09:48:21 用户登录成功，帐号 08a63833，登录IP 218.17.233.156`。
- 结论：已按用户指示停止追查，保留为线索。**防御性建议**：关键容器保持 `restart=unless-stopped`（已满足）；如需更强保障，可加一个"每 10 分钟检查容器是否存活、缺失则告警"的巡检脚本。

---

## 三、建议执行顺序与时间窗

**避开这些已有任务的时间点**：01:30（宝塔路径备份）、03:30（保留策略清理）、04:00（宝塔数据库备份）、15:25（证书续期）、`*/2` 与 `*/5` 的巡检脚本。

| 顺序 | 动作 | 建议时间 | 中断风险 |
|---|---|---|---|
| 1 | R1 补数据库备份 + 恢复演练 | 任意（不重启服务） | 无 |
| 2 | R5 清空 `/var/mail/root`、清 zzdy 日志、配日志切割 | 任意 | 无 |
| 3 | R7 确认知识库证书续期链路 | 11/09 前 | 无 |
| 4 | R4 安全组关闭 3306 | 与同事确认后 | 无（安全组生效实时） |
| 5 | R2 pg_hba 改密码认证 | 需先调研依赖 | 低（reload 不断连接） |
| 6 | R3 SSH 加固 + fail2ban | 需先确认密钥可用 | 中（做错会锁门，务必两人在场） |
| 7 | R6 代码与 git 收敛 | 排期 | 低 |

---

## 四、应急手册（站点又打不开时照这个做）

**第 1 步：判断是"整站不通"还是"只有后端 502"**

```bash
systemctl is-active nginx docker
docker ps --format '{{.Names}}\t{{.Status}}'
curl -s -o /dev/null -w 'health=%{http_code}\n' http://127.0.0.1:8000/health
curl -s -o /dev/null -w 'app=%{http_code}\n' https://knowledgekb.powerzhuan.cn/app
tail -30 /www/wwwlogs/knowledgekb.powerzhuan.cn.log
```

**第 2 步：容器不见了 → 按权威清单恢复**

```bash
# 权威 compose 文件清单（顺序不能改，共 17 个）
cat /opt/knowledge-kb-runtime/scripts/kb-compose-args.txt

# 命名卷若也被删，先补建（嵌入模型缓存为空时容器会因离线模式起不来）
docker volume create knowledge-kb_embedding_cpu_model_cache

cd /opt/knowledge-kb
docker compose $(cat /opt/knowledge-kb-runtime/scripts/kb-compose-args.txt) up -d --no-deps redis backend embedding-qwen
```

> ⚠️ 教训：不要把某个 compose 文件从清单里去掉"图省事"，否则会报 `service "embedding-qwen" has neither an image nor a build context specified: invalid compose project`。
> ⚠️ 不要运行 `scripts/deploy.ps1`（它带 `--remove-orphans`，会误杀 `kb-embedding-tunnel`）。

**第 3 步：502 但容器是健康的 → 检查宿主到 8000 的通路**

```bash
iptables -t nat -S OUTPUT | grep -n DOCKER
# 正确形态必须带 "! -d 127.0.0.0/8"，缺了它会导致 127.0.0.1:8000 被 DNAT 走网桥被内核丢弃 → 502
# 若发现缺少排除项的错误规则：
iptables -t nat -D OUTPUT -m addrtype --dst-type LOCAL -j DOCKER
/opt/knowledge-kb-runtime/scripts/kb-network-guard.sh
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8000/health
```

**第 4 步：回滚后端镜像**

```bash
# 上一个版本的镜像（仍然保留）
docker images | grep knowledge-kb-backend
# 需要回滚时：把 /opt/knowledge-kb-runtime/.codex-deploy-XXXXXXXX-*/compose.*.yml 里的 image 值改回旧 tag，
# 再执行第 2 步的 up -d --no-deps backend
```

**常用备份目录**（出问题时可从这些目录找回改动前的原文件）

- `/opt/knowledge-kb/.codex-deploy-20261007-adoption-gate/`（含 `confidence_training.py.before`、`container-before.json`、`iptables-before.txt`）
- `/opt/knowledge-kb/.codex-deploy-20261007-timeout/`、`/opt/knowledge-kb/.codex-deploy-20261006-pr173/`
- `/opt/knowledge-kb-runtime/scripts/kb-network-guard.sh.bak-20261008-1010`

---

## 五、附录：本次体检的关键数字

- 系统：OpenCloudOS 9.6，kernel 6.6.119，uptime 8 天 22 小时。
- Docker：29.7.2，`data-root=/opt/docker`，`live-restore=true`，镜像源 `docker.m.daocloud.io` / `docker.1ms.run`，**未配置 log-opts 轮转**。
- 磁盘：`/` 180G / 已用 134G（74%）/ 剩 47G；`/www/wwwlogs` 15G；`/var/log` 3.0G；`/opt/docker` 5.0G。
- 定时任务：腾讯 stargate `*/5`、宝塔 4 项（01:30 / 04:00 / 15:25 / 16:25）、保留策略 `30 3 * * *`、嵌入隧道检查 `*/2`、网络自愈 `*/5`。
- 外网端口实测：22 开放、**3306 开放**、888 关闭、5432 关闭、8000 关闭。
- 数据库：`knowledge_base` 34 表 / 1050 MB；实例内 4 个库；角色 `postgres`（超级用户）、`knowledge_admin`；`pg_dump` 路径 `/www/server/pgsql/bin/pg_dump`；宿主 PG 5432 监听 `0.0.0.0`（受 pg_hba 限制）、MySQL 5.7.44 监听 `*:3306`、宿主 redis 仅 `127.0.0.1:6379`。
- 备份现状：知识库**唯一一份备份是 2026-09-07 手动备份**（台账 `ps=手动备份`、`cron_id=0`，从未进入定时任务）；`/www/backup/database/pgsql/` 下另有一个 9/9 建的空目录 `knowledge base`（带空格，误操作残留，可删）；其它 `/www/backup/knowledge-kb/*` 为 9/4–9/8 的代码备份。

---

*本报告为只读体检结论，未对线上做任何配置变更。*
