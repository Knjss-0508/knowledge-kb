# 生产上线记录：盲标「我的已标注」SQL 侧分页（#208，2026-10-16）

本文记录 2026-10-16 把 `master` 的 `bc17a6f`（PR #208「盲标「我的已标注」改为 SQL 侧分页，
默认每页 50 条」）上线到 `https://knowledgekb.powerzhuan.cn` 的过程与取证。
时间均为线上 `date -u` 回显的 UTC（+08:00 另注）。

承接 `docs/production-deploy-20261016-blind-label-mine-pagination.md`（#206 上线记录）。
#206 解决了「截断在 200 条 / 条数不对」，但实现是**全量加载 + 内存切片**；
本次把分页与筛选真正下推到 SQL。

## 1. 问题与修复

#206 的实现（`ff6f528`）虽然接收 `page` / `page_size`，但：

```python
assignments = query.order_by(...).all()          # 把该用户全部派发读进内存
if start_at or end_at:                            # 时间筛选在 Python 里做
    assignments = [i for i in assignments if _matches(i, ...)]
total = len(assignments)                          # 总数靠内存长度
items = assignments[start : start + page_size]    # 内存切片
```

且 `assignment_summary()` 对每条派发单独 `COUNT` 一次标注数（N+1）。
于是 SQL 语句数 = `2 × 每页条数 + 1`，随每页条数线性增长；时间筛选前还要先全量加载。

`bc17a6f` 的修复（无数据库迁移、无接口契约变化）：

| 文件 | 改动 |
| --- | --- |
| `backend/app/services/blind_labeling.py` | 新增 `assignment_activity_range_condition()`（活动区间判定下推 SQL：`or_` + `exists` 子查询，含**标注提交时间**）；新增 `annotation_counts_for()`（一次 `IN` + `GROUP BY` 批量取标注数）；`assignment_summary()` 增加可选 `annotation_count` 入参 |
| `backend/app/routes/blind_labeling.py` | 总数用 `query.count()`；当前页用 `selectinload(work_order)` + `offset/limit` |
| `frontend/index.html` | 「我的已标注」默认每页 200 → **50** |
| 测试 | 新增 3 个后端回归用例 + 前端契约断言 |

## 2. 上线前只读基线（09:36:10Z / 17:36:10+08）

快照落盘 `/opt/knowledge-kb-runtime/.dsh-deploy-bc17a6f/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:my-annotations-pagination-ff6f528`（`sha256:640bbfa1157a…`） |
| 容器启动时间 | 2026-10-16T08:42:02.083692172Z |
| 环境变量 md5（74 个键） | `582ccc90ebd2e3b556b88fda70862a1a` |
| 挂载集合 md5 | `88e3d19a958535e437d47c2456cfcd3b` |
| 运行规格 md5（cmd/entrypoint/restart/ports/network） | `4e8b4c3dec4e65c9e2496c6fe67b3a22` |
| `alembic current` | `20261014_01_blind_pool_state (head)` |
| 容器内 `/frontend/index.html` | md5 `e46bdb7edf01ba50cd9bfbcbd086f519`（754,061 B） |
| 容器内 `annotation_counts_for` 命中数 | **0**（确认新实现不在线上） |
| 健康 | `/health` 200、`/ready` 200、线上 `/app` 200（754,061 B） |
| 近 30 分钟错误 | Traceback 0、500 0 |
| 磁盘 | `/dev/vda1` 180G，已用 129G，可用 52G（72%） |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:my-annotations-pagination-ff6f528` |

## 3. 构建（17:37:04+08 完成）

```bash
git archive --format=tar.gz -o master-bc17a6f.tar.gz bc17a6f   # 2,853,986 B
# sha256 2feac380920e900011ef23b90405120b5e3a5eb59ac9dd432319a096cf9920f7
```

上传线上解包到 `/opt/knowledge-kb-deploy/bc17a6f`，用与仓库 `backend/Dockerfile` 等价的
Dockerfile 构建（pip 走腾讯镜像、`SAFE_BUILD_SLICE=qabuild.slice` 限流）：

镜像 `knowledge-kb-backend:mine-paging-serverside-bc17a6f`（`3d501d3333cc`，496MB），
镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | `91581659ef79ff922bdd4edd112b1177` | ✅ 相同 |
| `/frontend/index.html` 大小 | 754,058 B | ✅ 相同 |
| 默认每页 50 标记 | 1 | ✅ 1 |
| 服务层 `annotation_counts_for` | 1 | ✅ 1 |
| 路由 `.limit(page_size)`（分页下推） | ≥1 | ✅ 1 |
| `query.order_by(None).count()`（SQL 计数） | 1 | ✅ 1 |
| `cmd` / `entrypoint` / `workdir` | 与线上一致 | ✅ `["uvicorn","app.main:app","--host","0.0.0.0","--port","8000"]` / `null` / `/app` |

## 4. 数据库迁移

**本次无迁移**，`alembic current` 切换前后均为 `20261014_01_blind_pool_state (head)`。

## 5. 切换（09:38:51Z / 17:38:51+08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh \
  knowledge-kb-backend:mine-paging-serverside-bc17a6f
# 新容器 93cd88e8d7bb…
```

切换前后逐项比对，**只有镜像、容器 id、启动时间变化**：

| 项 | 切换前 | 切换后 |
| --- | --- | --- |
| 环境变量 md5（74 键） | `582ccc90ebd2e3b556b88fda70862a1a` | ✅ 相同 |
| 挂载集合 md5 | `88e3d19a958535e437d47c2456cfcd3b` | ✅ 相同 |
| 运行规格 md5 | `4e8b4c3dec4e65c9e2496c6fe67b3a22` | ✅ 相同 |
| 容器内 `/frontend/index.html` | — | `91581659ef79ff922bdd4edd112b1177`（754,058 B） |
| 路由 `.limit(page_size)` / 服务 `annotation_counts_for` | 0 / 0 | 1 / 1 |
| `alembic current` | head | ✅ 不变 |

## 6. 上线后验收

### 6.1 线上路由连生产库（只读事务）

| 用户 | 请求 | 结果 |
| --- | --- | --- |
| 陈勇江（251 条） | `page=1 size=50` | 0.024s / **6 条语句** / 50 条 / total=251 / 6 页 |
| 陈勇江 | `page=2 size=50` | 0.010s / 6 条语句 / 50 条 |
| 陈勇江 | `page=6 size=50` | 0.004s / 6 条语句 / 1 条（末页） |
| 陈勇江 | `page=1 size=200` | 0.026s / 6 条语句 / 200 条 / 2 页 |
| 邓志成（553 条） | `page=1 size=50` | 0.010s / 6 条语句 / 50 条 / total=553 / 12 页 |
| 邓志成 | `page=1 size=200` | 0.024s / 6 条语句 / 200 条 / 3 页 |

分页不重不漏：陈勇江按 50 条/页翻 1..7 页共取回 251 条、去重后仍 251 条；
邓志成取回 350 条、去重后 350 条（7 页均为满页）。

### 6.2 优化前后对照（同一只读事务、同一份生产数据）

| 用户 | 旧实现 | 新实现 |
| --- | --- | --- |
| 陈勇江 `size=200` 无筛选 | 0.242s / **401** 条语句 | 0.033s / **6** 条语句 |
| 陈勇江 `size=200` + 10-14~10-16 | 0.253s / **452** 条语句 | 0.022s / **6** 条语句 |
| 邓志成 `size=50` | 0.063s / **101** 条语句 | 0.016s / **6** 条语句 |
| 陈勇江 越界页 `page=999` | — | 0.003s / 4 条语句 / 空页 |

语句数从 `2 × 每页条数 + 1` 降为**恒定 6 条**。新旧结果逐条等价：`total` 相同、
首页 id 顺序完全一致、每条 `annotation_count` 完全一致。

### 6.3 页面与探活

- 线上 `/app` 200（754,058 B，md5 `91581659…`）；
- `/health` 200（0.042s）、`/ready` 200（0.071s）；
- 无 key 调 `/api/v1/blind-labeling/my-assignments` → 401（鉴权正常）。

### 6.4 运行期观察（09:39Z 起）

- `kb-backend` Up，镜像 `mine-paging-serverside-bc17a6f`；
- Traceback **0**、`500 Internal Server Error` **0**；
- 30 分钟状态码直方图：200 × 302、404 × 17、401 × 1，**无 5xx**；
- 那 17 条 404 全部是 `POST /__wbpay/...` 外部扫描探测：仓库与镜像内都不存在该路由
  （`/app/app/main.py` 无 `wbpay`），与本次改动无关；
- 业务日志正常：`GET /api/v1/blind-labeling/my-assignments?...page_size=200... 200 OK`
  （旧标签页仍在用旧前端，后端兼容）、`POST /api/v1/integration/standard-search 200`；
- 其余容器（`kb-redis`、`kb-embedding-qwen`、`appeal-exemption-*`、`voc-workbench-*`）状态未受影响；
- 磁盘仍为已用 129G / 可用 52G。

## 7. 回滚

```bash
# 回到 #206 版本
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:my-annotations-pagination-ff6f528
# 再往前一版（#199 盲标池状态）
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:pool-state-e3b361f
```

本次无迁移，回滚只换镜像；已提交的标注数据与派发状态不受影响。

## 8. 已知风险与遗留

- **默认每页 200 → 50**：答疑员看到的页数会变多（陈勇江 251 条：2 页 → 6 页）。
  这是本次的预期改动；分页条仍可选 50/100/200/500。已打开旧标签页的用户需要刷新
  （前端为整页下发），否则继续用旧 JS 请求 `page_size=200`（后端已兼容，不报错）。
- **时间筛选语义未变**，仍是「分配/开始/完成/回收 + 标注提交时间任一命中」；
  下推后的 SQL 用 `exists` 子查询表达标注提交时间，已用等价性用例守住。
- 线上 `/opt/knowledge-kb/frontend/index.html`（宿主机副本）仍是旧版本，线上页面实际由
  容器内文件提供，两者本就不同步（见 `docs/production-repo-divergence-20261010.md`），
  本次未改动宿主机副本。
- 本次未跑完整后端套件（本地环境有 3 failed / 23 errors 属无关模块的依赖问题）；
  上线验证依靠镜像内指纹比对 + 线上路由对真实库的只读端到端调用。
