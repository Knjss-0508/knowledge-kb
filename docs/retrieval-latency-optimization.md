# 检索接口延迟优化：找出并消除应用层热路径

**日期**：2026-09-23
**接口**：`POST /api/v1/integration/standard-search`
**结论**：一次检索从 **450.9 ms 降到 160.1 ms**（快 2.8 倍），且**检索结果逐字节不变**。

---

## 一、结论速览

| 项目 | 优化前 | 优化后 | 变化 |
|---|---|---|---|
| 检索接口延迟（中位） | 450.9 ms | **160.1 ms** | **−290.8 ms（−64.5%）** |
| 其中 `resolve_applicability_scope` | 约 440 ms | 约 150 ms | 主要来源 |
| 现有测试套件 | — | 15 passed | 无回归 |
| 检索结果 | — | **逐字节一致** | 无行为变化 |

**根因**：`backend/app/services/applicability.py` 的 `scope_keys()` 在热路径上
**反复归一化模块常量**，一次检索触发 **31 万次** `normalize_scope_key` 调用、
**171 万次**生成器迭代。

---

## 二、怎么找到的（测量过程）

这一段记录**试错过程**，因为前面几个假设都是错的，值得留档。

### 第 1 步：排除数据库

按 PID 汇总一次请求的全部 SQL：**130 条语句、合计 85.8 ms**。
其中检索连接只占 71.2 ms。→ **数据库不是瓶颈**。

### 第 2 步：排除 N+1 与连接往返

实测每条语句的往返+DBAPI+ORM 开销只有 **0.14 ~ 0.18 ms**，
130 条语句合计约 **23 ms**。→ **N+1 不是那 370 ms**。

### 第 3 步：排除嵌入

热缓存下 `_query_embedding` 命中缓存返回耗时 **0.0 ms**（未命中约 110 ms，
其中嵌入隧道往返 78~85 ms，batcher 窗口仅 `QUERY_EMBEDDING_BATCH_WAIT_MS=20`）。
→ **嵌入不是瓶颈**。

### 第 4 步：排除 ORM 列加载

`db.query(Knowledge)` 取全列（含 `content`，`json` 类型，平均 805 字符/行）
最多 120 行：

| 形态 | 耗时 |
|---|---|
| 全列 | 15.9 ms |
| `defer(content)` | 9.1 ms |
| 真实检索形态（全列 + joinedload + distance） | 31.9 ms |
| 真实检索形态（`defer content`） | 19.5 ms |

→ 只省 12.4 ms，**不是那 370 ms**。

### 第 5 步：用 PostgreSQL 日志的**语句间空隙**定位

日志带毫秒时间戳，请求跨度 384 ms 而 SQL 合计只有 4.6 ms。
空隙出现在 `SELECT embedding_runtime_configs` 之后（249 ms）等位置 ——
说明时间花在**两条 SQL 之间的应用代码**里。

### 第 6 步：进程内直接剖析路由函数（决定性）

`TestClient` 把应用跑在另一个线程，cProfile 只能看到 `_thread.lock.acquire`。
改为**直接同步调用路由函数**（它是普通 `def`），得到：

```
1        0.077s  cumtime 1.826s   applicability.py:423(resolve_applicability_scope)
125287   0.275s  cumtime 1.727s   applicability.py:95(scope_keys)
313234   0.373s  cumtime 1.257s   applicability.py:80(normalize_scope_key)
1711823  0.389s                    applicability.py:83(<genexpr>)
1483542  0.109s                    str.isalnum
```

剖析器放大约 4 倍，折算后正是实测的约 440 ms。

---

## 三、根因

`applicability.py` 里 `_CATEGORY_ALIAS_GROUPS` 是**模块常量**（2 组、共 4 个别名），
但 `scope_keys()` 每次调用都把它重新归一化一遍：

```python
if kind == "category" and keys:
    for aliases in _CATEGORY_ALIAS_GROUPS:
        normalized_aliases = {normalize_scope_key(alias) for alias in aliases}  # ← 每次都算
        if keys.intersection(normalized_aliases):
            keys.update(normalized_aliases)
```

而 `resolve_applicability_scope()` 会**按车型逐个调用** `scope_keys()`：

```python
for model in group["models"]:
    model_category_ids = scope_keys([...], "category")   # ← 每次都触发上面的重算
    model_brand_ids    = scope_keys([...], "brand")
    if not scope_keys(model, "model").isdisjoint(requested_models):
```

车型数量乘以每次 3 个 `scope_keys` 调用，于是同一个常量被反复计算几十万次。

---

## 四、修复

两处改动，都在 `backend/app/services/applicability.py`：

### 1. 别名组在导入时预计算一次

```python
_NORMALIZED_CATEGORY_ALIAS_GROUPS: tuple[frozenset[str], ...] = tuple(
    frozenset(normalize_scope_key(alias) for alias in aliases)
    for aliases in _CATEGORY_ALIAS_GROUPS
)
```

`_CATEGORY_ALIAS_GROUPS` 是常量、全仓库只在热循环这一处使用、运行时从不被改写，
因此预计算是安全的。

### 2. `normalize_scope_key` 加记忆化

它只依赖入参字符串（`unicodedata.normalize` + `casefold` + `_EMPTY_SCOPE_KEYS` 常量），
是**纯函数**，结果永不变化，无需失效：

```python
@lru_cache(maxsize=_NORMALIZE_SCOPE_KEY_CACHE_MAXSIZE)   # 131072
def _normalize_scope_key_cached(raw_value: str) -> str: ...

def normalize_scope_key(value: Any) -> str:
    raw_value = "" if value is None else str(value)
    return _normalize_scope_key_cached(raw_value)
```

缓存在 `str(value)` 之后进行，所以键一定是可哈希的（入参可能是 dict）。

---

## 五、验证方式（线上实测，可复现）

在服务器上把新文件临时换入容器、**用新进程**（不重启服务、不影响线上流量）测量，
测完立即还原：

```
优化前（容器内原文件 md5 4e3b55b34e2ef3525d946acbecca45d6）:
  7 次: 最快 436.7  中位 450.9  最慢 485.4 ms

优化后（换入新文件）:
  7 次: 最快 158.9  中位 160.1  最慢 178.2 ms
```

### 正确性

1. **现有测试套件**：`tests/test_applicability_scope.py` + `tests/test_business_types.py`
   → **15 passed**（容器内临时安装 pytest 运行）。

2. **`scope_keys` 单元级对照**：9 个用例（含 dict、None、空列表、数字、中文别名）
   新旧实现返回值**完全一致**。

3. **端到端响应指纹**（最强的一条）：5 个检索用例，覆盖
   `headquarters_standard` / `business_accumulation` 与 `self_operated` / `aggregated`，
   其中一个返回 5 个候选、5385 字节。优化前后对响应体做 `sha256`：

```
   ✅ 逐字节完全一致：优化不改变任何检索结果
```

### 测量脚本要点

- 直接同步调用 `search_standard_provider_knowledge(...)`，绕开 TestClient 的线程边界，
  cProfile 才能看到服务端真实耗时
- 换文件用 `docker cp`，**容器内原文件先备份**，测完还原并核对 md5
- 因为运行中的进程已导入旧模块，换文件**不影响线上流量**；测量走新起的 Python 进程

---

## 六、走过的弯路（记录以免重复）

| 弯路 | 原因 | 教训 |
|---|---|---|
| 以为瓶颈是数据库 | 只看总耗时没按 PID 拆分 | 日志里的 PID 是最便宜的区分手段 |
| 以为瓶颈是 N+1 | 没量化**单条语句**的往返开销（实测仅 0.14 ms） | 先量单价，再算总价 |
| 以为瓶颈是嵌入 | 热缓存命中实际是 0.0 ms | 缓存命中与未命中要分开测 |
| 以为瓶颈是 ORM 大字段 | `defer(content)` 只省 12.4 ms | 猜测的收益要先量 |
| 用 `TestClient` + cProfile | 应用在另一个线程，只看到锁等待 | 同步路由要**直接调用** |
| 看 `/proc/1/stat` 判断 CPU | uvicorn 的 worker 是 PID 1 的**子进程** | 进程统计要确认目标进程 |
| 用相对路径 `[System.IO.File]::ReadAllText` | .NET 用**进程工作目录**，不是 PowerShell 的 `Set-Location`，读到的是另一个工作树里未修改的文件 | 脚本里一律用绝对路径 |

---

## 七、上线记录（2026-09-23）

修复已通过 PR #100 合并进 `master` 并**部署到线上**。

### 部署方式

只部署这一个修复，**不牵动同事尚未部署的提交**：服务器仓库
`/opt/knowledge-kb` 停在 `server-preserve-20260914`（`fd163ca3`），
只把修复后的 `backend/app/services/applicability.py` 写入该仓库再重建镜像。

写入前核对过：该文件在服务器上的 md5（`4e3b55b34e2ef3525d946acbecca45d6`）
与 `origin/master~1` 的版本**完全一致**，确认只替换了本次改的那一处。

### 部署步骤与实测

```
1) 为当前镜像打回滚标签
   knowledge-kb-backend:before-appl-fix-20260923-124654 -> 86e646678111

2) 写入修复后的文件（md5 f2cae75fae399d7a099978afb127a800），python3 -m py_compile 通过

3) 后台重建镜像
   Image knowledge-kb-backend Built        （pip 层命中缓存，仅源码层重跑，约 1 分钟）

4) 校验新镜像内容
   applicability.py md5 = f2cae75fae399d7a099978afb127a800  ✅
   embedding.py md5 与服务器一致（此前热修被保留）           ✅

5) 重建容器
   up -d 耗时 4 秒，就绪探针第 2 次通过，总中断约 6 秒
```

### 上线后实测

```
20 次热查询: 最快 167  中位 171  P90 181  最慢 246 ms
全部 HTTP 200，候选数与部署前一致（无回归）

（部署前中位 450 ~ 570 ms）
```

其它确认项：

| 项 | 结果 |
|---|---|
| 容器状态 | `kb-backend \| Up (healthy)`，重启 0 次 |
| 公网首页 | HTTP 200 |
| uploads 文件 | 10 个，与数据库 `knowledge_media` 10 条一致 |
| `knowledge_items` | 18,630 条 |
| 数据库日志速率 | 0 行/秒 |
| 回滚方式 | `docker tag knowledge-kb-backend:before-appl-fix-20260923-124654 knowledge-kb-backend:latest` 后重建容器 |

### 部署后服务器仓库的状态

`/opt/knowledge-kb` 现在有**两个已跟踪文件的改动**，都是**已部署进镜像的源码**，
不是遗留的临时改动：

- `backend/app/services/embedding.py`（更早的嵌入客户端复用修复，已烘焙进镜像）
- `backend/app/services/applicability.py`（本次修复）

后续若要把服务器仓库整体升到最新 `master`，需要单独安排一次部署
（那会一并上线同事的 5 个提交：`media_storage.py` / `media.py` / `main.py` /
`config.py` / `docker-compose.yml`），属于另一个决策。

---

## 八、第二轮：车型位置索引（163 ms → 49 ms）

第一轮把 450 ms 降到 163 ms 后重新测量，发现瓶颈**转移**了。

### 测量（同一线程直接调用路由）

```
models 数量            : 31,321        ← 缓存的车型总数
resolve_applicability_scope 单次: 121.57 ms   ← 占 163 ms 总时间的 74%
scope_keys 调用         : 125,287 次
_read_manhattan_cache   : 0.005 ms     ← 可忽略，不是瓶颈
cursor.execute          : 仅 4 次 / 33 ms  ← 数据库已不是瓶颈

cProfile tottime 排名:
  scope_keys           125,287 次  0.218s   ← 第一
  resolve_..._scope          1 次  0.065s
  normalize_scope_key  187,950 次  0.061s
```

`125,287 ≈ 31,321 车型 × 4`，与「每个车型调用 `scope_keys` 三次」完全吻合。

### 根因

```python
for model in group["models"]:            # 3.1 万个车型，每个请求都全量遍历
    model_category_ids = scope_keys([model.get("categoryId"), model.get("category_id")], "category")
    ...
    model_brand_ids = scope_keys([model.get("brandId"), model.get("brand_id")], "brand")
    ...
    if not scope_keys(model, "model").isdisjoint(requested_models):
        matching_models.append(model)
```

第一轮给 `normalize_scope_key` 加了 `lru_cache`，把单次归一化变便宜了，但**遍历本身没省**：
3.1 万次循环、12.5 万次 `scope_keys`、每次都新建 set。

### 修复

预先算好「归一化车型键 → 车型在列表中的位置」：

```python
index: dict[str, list[int]] = {}
for position, model in enumerate(models):
    if not isinstance(model, dict):
        continue
    for model_key in scope_keys(model, "model"):
        index.setdefault(model_key, []).append(position)
```

查询时只对命中的车型做类目/品牌校验：

```python
matched_positions = set()
for model_key in requested_models:
    positions = position_index.get(model_key)
    if positions:
        matched_positions.update(positions)
for position in sorted(matched_positions):   # sorted 保持与原实现相同的顺序
    ...
```

**等价性论证**：索引给出的候选集恰好是原实现中通过第三个判断
（`not scope_keys(model, "model").isdisjoint(requested_models)`）的那些车型 ——
因为该判断成立当且仅当存在某个键同时属于 `scope_keys(model, "model")` 与
`requested_models`，而这正是该车型在索引中出现在某个被请求键下的条件。
`requested_models` 为空时结果必然为空，整个循环可跳过。

**失效判定用对象身份**：`manhattan.py` 的 `_read_cache()` 已按
`(路径, st_mtime_ns, st_size)` 记忆，缓存文件未变时返回**同一个 dict 对象**，
文件变更时返回新对象。因此索引用 `is` 比较即可，不需要猜 `updated_at`。
同时持有 `cache` 的强引用，使 `is` 判断在条目存活期间始终可靠
（`dict` 不支持弱引用，不能用 `WeakKeyDictionary`）。

### 验证

1. **681 个用例对拍**：把改动前的实现从 git 取出、与改动后的实现放在同一进程里，
   覆盖全空 / 类目 ID / 类目名 / 品牌 ID / 品牌名 / 车型 ID / 车型名 / 跨类目组合 /
   品牌前缀 / 通用键 / 无意义值 / 多值 / 字典 / 嵌套列表 / 空元组 / 空列表 / 空字符串
   → **681 / 681 结果完全一致**。

2. **测试套件**：新增 `tests/test_model_position_index.py`（含与暴力遍历参考实现对拍、
   索引重建、非 dict 元素、空车型列表、无车型时跳过索引）+ 原有
   `test_applicability_scope.py` + `test_business_types.py` → **19 passed**。

3. **端到端（容器内换入新文件，同一线程直接调用路由）**：

```
旧版本: 7 次  最快 157.3  中位 163.2  最慢 181.1 ms
新版本: 7 次  最快  44.5  中位  49.1  最慢  51.3 ms      → 快 3.3 倍
cProfile 函数调用: 1,306,791 → 22,630                    → 减少 98.3%
```

### 索引构建代价

| 项 | 数值 |
|---|---|
| 冷启动首次构建 | 约 88 ~ 197 ms（`lru_cache` 预热后稳定在约 90 ms） |
| **进程刚启动后的首次构建** | **约 250 ms**（`lru_cache` 全冷，实测 249.8 ms） |
| 索引键数 | 62,475 |
| 热取 | 0.0002 ~ 0.0018 ms |
| 重建时机 | 仅当缓存文件变更（`_read_cache` 返回新对象）时 |

这是**一次性**开销，摊销到之后的所有请求上。最坏情况是「容器刚重启 + 缓存文件
也刚变更」，此时首个请求会付约 250 ms；之后的请求都是 50 ms。

### 上线记录

已通过 PR #103 合并并部署（部署方式与第一轮相同：只写入这一个文件再重建镜像，
写入前校验本地文件非空且 md5 匹配）。

```
回滚标签: knowledge-kb-backend:before-model-index-20260923-141752 -> 3a502dcac1e1
新镜像:   4f288e3affa4
构建耗时: 20 秒        重建中断: 5 秒
新镜像内 applicability.py md5 = 3fbf15e40f84813030006dbfac6ab06b  ✅
新镜像内 embedding.py md5 与服务器一致（更早的热修保留）          ✅
```

上线后实测：

```
20 次热查询: 最快 47  中位 50  P90 57  最慢 58 ms
全部 HTTP 200，候选数与部署前一致
容器 kb-backend Up (healthy)，重启 0 次
公网首页 HTTP 200；knowledge_items 18,630 条；
uploads 10 个文件与 knowledge_media 10 条一致；数据库日志 0 行/秒
```

### 两轮累计

| 阶段 | 检索延迟 |
|---|---|
| 优化前 | 450 ~ 570 ms |
| 第一轮后（`lru_cache` + 预计算别名组） | 163 ms |
| 第二轮后（车型位置索引） | **49 ms** |

---

## 九、第三轮：先取 id 再按 id 取全行（50 ms → 40 ms）

第二轮之后剩下的时间几乎全在数据库里：单次检索只有 **3 条 SQL**，合计 32 ms，
其中 `cursor.execute` 占大头。把每条语句单独计时后看到了这个：

```
[1] 27.70 ms   rowcount=60   ← knowledge_items 检索（第一个 knowledge_origin）
[2]  3.99 ms   rowcount=60   ← 同一条 SQL，第二个 knowledge_origin
[3]  0.30 ms   rowcount=1    ← embedding_runtime_configs
非数据库时间: 13.0 ms
```

同一条 SQL 跑两次是设计使然（每个 `knowledge_origin` 各搜一次），但两次差了 7 倍，
说明第一条本身有问题。

### 怎么找到的

`EXPLAIN (ANALYZE, BUFFERS)` 显示第一条查询**完全没有走向量索引**：

```
Limit (actual time=21.556..21.567 rows=60)
  Buffers: shared hit=14367          ← 14,367 次共享缓冲命中
  -> Sort (top-N heapsort, 223kB)
       -> Hash Join (actual time=1.550..18.856 rows=4131)
            -> Seq Scan on knowledge_search_embeddings (rows=4271)
            -> Hash -> Index Scan using ix_knowledge_items_knowledge_origin (rows=841)
  width=1771                          ← 单行 1771 字节
```

三个关键事实：

1. **`LIMIT 60` 在 `Sort` 之后才生效**。排序阶段要把 **4,131 行宽行**（含 `content`
   等 JSON 列，单行约 1771 字节）全部物化，之后才丢掉 4,071 行。
2. **候选行里绝大部分是重复的**。同一条知识在 `knowledge_search_embeddings` 里有多条
   嵌入，所以 60 行候选只对应 **17 个**（`business_accumulation` 下 42 个）不同知识条目。
   也就是说取回的宽行里有七成是白取的。
3. **HNSW 索引在生产里从未被使用**（`idx_scan` 只有 124，全是测量时产生的）。
   即使把向量内联成字面量（`bindparam(..., literal_execute=True)`）逼规划器考虑它，
   仍然是 `Seq Scan + Sort` —— 因为该表只有 4,271 行，全表扫描确实比索引扫描便宜。
   **结论：HNSW 不是这里的优化方向，已排除。**

### 修复

把一次查询拆成两步（`backend/app/services/knowledge_dedup.py` 的 `search_embeddings`）：

1. 第一步只用**完全相同**的过滤条件、排序与 `LIMIT` 取 `(id, distance)`，
   每个 id 取最小距离（等价于原实现 `max(1 - distance)` 的最大分数）；
2. 第二步按 id 取完整行并 `joinedload` 类目，只取真正命中的条目。

这样排序阶段物化的是窄行（只有 id 和距离），宽行只取真正要用的那十几个。

### 验证方式

| 项 | 结果 |
|---|---|
| 响应等价 | 8 个用例（两种业务类型 × 三种知识来源）响应 `sha256` 与改动前**逐个一致** |
| 测试套件 | 容器内换入新文件跑完整套件，失败集合与基线**完全相同**（18 个既有失败，**0 个新增**）；相关三个测试文件 19 passed |
| 端到端（24 次） | 中位 **45.5 → 35.5 ms**，P90 **62.6 → 40.5 ms** |
| 上线后线上 | 24 次中位 40 ms、P90 50 ms，全部 HTTP 200，公网 200 |

> `tests/test_embedding_training_runner.py` 在收集阶段就报
> `FileNotFoundError: '/training-runner/runner.py'`，是镜像里缺文件的既有问题，
> 与本次改动无关。

### 上线记录（2026-09-23）

| 项 | 值 |
|---|---|
| PR | #105（Squash and merge） |
| master | `29060ae` |
| 回滚标签 | `knowledge-kb-backend:before-narrow-fetch-20260923-144635`（= `4f288e3affa4`） |
| 新镜像 | `knowledge-kb-backend:latest` = `c7694028f37e` |
| 构建 / 重建耗时 | 10 秒 / 4 秒 |
| 上线后容器内 md5 | `9fe18e9773b1b7c230ecbe3f1b7e989b` |
| 热修保留 | `embedding.py` = `28e324a20386b721806a7db339460b36`、`applicability.py` = `3fbf15e40f84813030006dbfac6ab06b` |
| 回滚方式 | `docker tag knowledge-kb-backend:before-narrow-fetch-20260923-144635 knowledge-kb-backend:latest` 后重建后端容器 |

### 已知风险

- 第二步是按 id 单独取行，若某条目在两次查询之间被删除，该条会从结果中消失
  （原实现是同一条快照查询，不存在该窗口）。窗口极短，且只会少给一条候选。
- 第一步返回的 60 行仍可能含重复 id，第二步按 id 去重后条目数与原来一致。

---

## 十、后续可选项

| 项 | 收益 | 代价 |
|---|---|---|
| ~~`resolve_applicability_scope` 按 category/brand 建索引，避免全量遍历车型~~ **已完成** | 163 → 49 ms | — |
| ~~检索查询只取窄列、按 id 再取全行~~ **已完成** | 50 → 40 ms | — |
| 给该函数加按请求参数的结果缓存 | 重复查询参数时可跳过（现在只剩 0.2 ms，收益很小） | 需处理缓存失效 |
| 确认 `knowledge_search_embeddings` 是否该按 `embedding_kind` 过滤 | 候选集可能大幅缩小（841 条知识对应 4,271 条嵌入） | **会改变检索结果，需产品确认** |
| 减少每次检索的连接数 | 减少往返 | 需确认连接池配置 |
| 把索引构建移到缓存刷新时预热，避免变更后首个请求付 90 ms | 消除一次性毛刺 | 需改缓存刷新路径 |
| 两个 `knowledge_origin` 各查一次数据库 | 合并后省一次往返 | 两次的过滤条件不同，合并需改 SQL 语义 |

> 现在单次检索约 40 ms，其中数据库约 25 ms、非数据库约 13 ms。
> 应用层继续优化的空间已经很小，剩下的方向是缩小候选集（需要产品确认）
> 或减少嵌入表的行数。


---

## 附：本次优化周期的完整改动清单

同一天围绕「让答疑中台更快」做的一整轮工作，全部走独立分支 + PR 合并，
每项都有真实测量依据并在服务器上实测验证：

| PR | 内容 | 关键数据 |
|---|---|---|
| #98 | 为 `model_configuration` 的 JSON 表达式查询补索引；修正「嵌入占 84%」的错误结论 | worker 轮询查询 215.745 → 0.041 ms；该 worker 进程整体 430 → 4.2 ms |
| #99 | 补充审计文档：按 PID 隔离后的真实耗时分解 | 请求 455 ms 中数据库只占 85.8 ms |
| #100 | 消除适用范围解析热路径上的重复归一化 | 检索 450.9 → 160.1 ms |
| #101 | 把 `.env.example` 的 blob 规范化为 LF | 消除每次 checkout 的永久「已修改」状态 |
| #102 | 记录第一轮的上线过程，并标注 `postgresql.conf` 已修 | — |
| #103 | 车型位置索引，不再全量遍历 31,321 个车型 | 检索 163.2 → 49.1 ms |
| #105 | **本次**：检索先取 id 再按 id 取全行，不再物化 4,131 行宽行 | 检索 45.5 → 35.5 ms（中位） |

另外两项服务器配置改动（不在代码仓库内）：

| 改动 | 关键数据 |
|---|---|
| `ALTER SYSTEM SET log_statement = 'none'` + reload | 日志 11 行/秒 → 0 行/秒；历史日志累计 26 GB |
| `postgresql.conf` 第 887 行 `log_statement = all` → `'none'` | 防止 `auto.conf` 被清空后日志再次刷屏，零停机 |
| 排除 HNSW 索引方向 | 该表仅 4,271 行，规划器始终选 `Seq Scan + Sort`；`idx_scan` 仅 124（全为测量产生） |
