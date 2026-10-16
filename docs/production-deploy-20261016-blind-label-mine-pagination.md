# 生产上线记录：召回盲标「我的已标注」服务端分页（#206，2026-10-16）

本文记录 2026-10-16 把 `master` 的 `ff6f528`（PR #206「召回盲标「我的已标注」按服务端分页，
不再截断在 200 条」）上线到 `https://knowledgekb.powerzhuan.cn` 的完整过程与取证。
时间为线上 `date -u` 回显的 UTC（+08:00 另注）。

## 1. 问题与修复

线上答疑员反馈「我的已标注」条数一直是 **200**，但实际已完成标注已超过 200 条。

根因（旧代码，`master` 的 `b92b9e0`）：

```js
// frontend/index.html：「我的已标注」固定请求第一页 200 条
params.set('page', '1');
params.set('page_size', '200');
...
// 又把「当前页长度」当成总数显示
self.blindLabeling.pagination.total = self.blindLabeling.assignments.length;
```

于是历史标注超过 200 条时，既翻不到第 2 页，条数也恒等于 200。

`ff6f528` 的修复（仅两个功能文件，无数据库迁移）：

| 文件 | 改动 |
| --- | --- |
| `backend/app/routes/blind_labeling.py` | `my-assignments` 响应补充 `total_pages`（+2 行） |
| `frontend/index.html` | 按真实页码/每页条数取数、条数取服务端 `total`、新增分页条（上一页/下一页、第 X / Y 页、每页 50/100/200/500）、切换页签与时间范围回到第一页、页码越界回落末页（+67 / −6） |

## 2. 上线前只读基线（08:33:49Z / 16:33:49+08）

快照落盘 `/opt/knowledge-kb-runtime/.dsh-deploy-ff6f528/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:pool-state-e3b361f`（`sha256:33bfc8bc6144…`） |
| 容器启动时间 | 2026-10-14T14:48:48.656323455Z（已运行约 42 小时） |
| 环境变量 md5（74 个键） | `582ccc90ebd2e3b556b88fda70862a1a` |
| 挂载集合 md5 | `88e3d19a958535e437d47c2456cfcd3b` |
| 运行规格 | `cmd=["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]`、`entrypoint=null`、`restart=unless-stopped`、端口 `127.0.0.1:8000:8000`、网络 `knowledge-kb_default`（172.18.0.2） |
| `alembic current` | `20261014_01_blind_pool_state (head)` |
| 容器内 `/frontend/index.html` | md5 `7b020e3cd90c60388462b239b278e88a`，750,041 B（= 旧版） |
| 容器内 `total_pages` 命中数 | **0**（确认修复不在线上） |
| 健康 | `/health` 200、`/ready` 200、线上 `/app` 200（750,041 B） |
| 近 30 分钟错误 | Traceback 0、500 0 |
| 磁盘 | `/dev/vda1` 180G，已用 128G，可用 53G（72%） |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:pool-state-e3b361f` |

线上 `/app` 实测（HTTPS 抓取 750,041 B）与容器内文件 md5 相同，且**不含** `blindLabelListTotal`，
证明问题出在「修复未部署」，而非「修复无效」。

## 3. 构建（16:36:16+08 开始，16:38:28+08 完成）

沿用线上既有做法：用 `git archive` 的**干净导出**构建，不碰 `/opt/knowledge-kb` 工作区
（该目录有大量未提交改动，见 `docs/production-repo-divergence-20261010.md`）。

```bash
git archive --format=tar.gz -o master-ff6f528.tar.gz ff6f528   # 2,851,596 B
# sha256 07444530621f615a23f617b621fffd3e2a08f958493ea06e138ea327c0f3a3d7
scp -q master-ff6f528.tar.gz root@<prod>:/tmp/master-ff6f528.tar.gz
```

线上解包到 `/opt/knowledge-kb-deploy/ff6f528`，用与仓库 `backend/Dockerfile` 等价的
`Dockerfile`（仅 pip 走腾讯镜像）构建：

```bash
export SAFE_BUILD_SLICE=qabuild.slice          # IO 限流，防整机冻结
docker build --network host -f "$STAGE/Dockerfile" \
  -t knowledge-kb-backend:my-annotations-pagination-ff6f528 "$SRC"
```

首次构建因基础镜像仓库瞬时 **502 Bad Gateway**（`docker.m.daocloud.io` HEAD 失败）中止；
直接 `docker pull` 重试三次成功后重新构建，未改动任何 daemon 配置。

镜像 `knowledge-kb-backend:my-annotations-pagination-ff6f528`
（`640bbfa1157a`，496MB），镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | `e46bdb7edf01ba50cd9bfbcbd086f519` | ✅ 相同 |
| `/frontend/index.html` 大小 | 754,061 B | ✅ 相同 |
| `blindLabelListTotal` 行数 | 2 | ✅ 2 |
| `/app/app/routes/blind_labeling.py` 的 `total_pages` 行数 | 1 | ✅ 1 |
| 旧的固定 `page_size=200` 请求 | 0 | ✅ 0 |
| `cmd` / `entrypoint` / `workdir` | 与线上一致 | ✅ `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]` / `null` / `/app` |

## 4. 数据库迁移

**本次无迁移。** `git diff e3b361f ff6f528 -- backend/migrations` 为空，切换前后
`alembic current` 均为 `20261014_01_blind_pool_state (head)`，因此不存在先迁移后切换的窗口。

## 5. 切换（08:42:02Z / 16:42:02+08）

用主机规范脚本切换（生产 `kb-backend` **不是** Compose 容器，不能用
`docker compose up -d --no-deps backend`，会因容器名冲突产生垃圾容器）：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh \
  knowledge-kb-backend:my-annotations-pagination-ff6f528
# 新容器 487af322dfa0…
```

切换前后 `docker inspect` 逐项比对，**只有镜像、容器 id、启动时间与网络 EndpointID/MacAddress 变化**：

| 项 | 切换前 | 切换后 |
| --- | --- | --- |
| 镜像 | `pool-state-e3b361f` | `my-annotations-pagination-ff6f528` |
| 环境变量 md5（74 键） | `582ccc90ebd2e3b556b88fda70862a1a` | ✅ 相同 |
| 挂载集合 md5 | `88e3d19a958535e437d47c2456cfcd3b` | ✅ 相同 |
| `cmd` / `entrypoint` / `restart` / 端口绑定 | 见第 2 节 | ✅ 逐字符相同 |
| 网络 | `knowledge-kb_default` | ✅ 同一网络（EndpointID/Mac 每次运行必然变化） |
| 容器内 `/frontend/index.html` | — | md5 `e46bdb7edf01ba50cd9bfbcbd086f519`，754,061 B |
| 容器内 `total_pages` 行数 | 0 | 1 |
| `alembic current` | `20261014_01_blind_pool_state (head)` | ✅ 不变 |

> 说明：第 2 节记录的运行规格 md5 含 `NetworkSettings.Networks`，其中 `EndpointID`/`MacAddress`
> 由 Docker 在每次 `docker run` 时重新分配，因此该段 md5 必然变化，不代表运行参数变化；
> 上表按字段逐项比对确认参数未变。

## 6. 上线后验收

### 6.1 真实库端到端分页（只读调用路由函数）

以受影响答疑员（`陈勇江`，`user_id=ae29c473…`）在生产库上直接调用
`list_my_assignments(status='completed')`：

| 请求 | items | total | total_pages | page |
| --- | --- | --- | --- | --- |
| `page=1, page_size=200` | 200 | **251** | **2** | 1 |
| `page=2, page_size=200` | 51 | 251 | 2 | 2 |
| `page=1, page_size=50` | 50 | 251 | 6 | 1 |

第 1 页与第 2 页的 assignment id **交集为 0**（200 + 51 = 251，无重复无遗漏）。
该用户在库中的 `completed` 派发为 251 条，与接口返回的 `total` 一致。

### 6.2 页面与探活

- 线上 `/app` 200，返回 754,061 B，md5 `e46bdb7e…`；页面内 `blindLabelListTotal` 命中 2 处；
- `/health` 200（0.039s）、`/ready` 200（0.069s）；
- 无 key 调 `/api/v1/blind-labeling/my-assignments` 与 `/my-batch` 均 **401**（鉴权正常）。

### 6.3 运行期观察（08:44Z 起）

- `kb-backend` Up，镜像 `my-annotations-pagination-ff6f528`；
- 容器启动至今 Traceback **0**、`500 Internal Server Error` **0**；
- 业务日志正常：`GET /api/v1/blind-labeling/my-assignments 200`、
  `POST /api/v1/blind-labeling/assignments/<id>/submit 201`、
  `POST /api/v1/integration/standard-search 200` 等持续出现；
- 其余容器（`kb-redis`、`kb-embedding-qwen`、`appeal-exemption-*`、`voc-workbench-*`）状态未受影响；
- 磁盘仍为已用 128G / 可用 53G。

## 7. 回滚

回滚锚点镜像仍在本地：

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:pool-state-e3b361f
```

本次无迁移，回滚只换镜像即可，不涉及数据回退；已提交的标注数据与派发状态不受影响。

## 8. 已知风险与遗留

- **答疑员需要刷新页面**：前端文件是整页下发的单页应用，已打开的旧标签页仍用旧 JS；
  提示用户刷新（或按 Ctrl+F5）后即可看到「共 251 条 / 第 1 / 2 页」。未改动静态资源缓存策略。
- **条数口径**：`total` 统计的是该用户的 `completed` **派发**条数（一个工单一次派发算 1 条），
  不是标注明细行数（该用户标注明细为 619 行 / 251 个工单）。
- 线上 `/opt/knowledge-kb/frontend/index.html`（宿主机副本，750,307 B，2026-10-10）仍是旧版本；
  线上页面实际由容器内 `/frontend/index.html` 提供，两者本就不同步
  （见 `docs/production-repo-divergence-20261010.md`），本次未改动宿主机副本。
- 本次未跑测试套件（生产机无 pytest、本机环境缺依赖）；验证方式是镜像内文件指纹比对
  ＋真实库只读端到端调用。
