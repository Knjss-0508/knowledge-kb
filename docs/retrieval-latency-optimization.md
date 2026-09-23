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

## 八、后续可选项

| 项 | 收益 | 代价 |
|---|---|---|
| `resolve_applicability_scope` 按 category/brand 建索引，避免全量遍历车型 | 剩下约 150 ms 中的一部分 | 算法改动，需谨慎验证 |
| 给该函数加按请求参数的结果缓存 | 重复查询参数时可跳过 | 需处理缓存失效 |
| 检索查询改用 `load_only` 只取需要的列 | 约 12 ms | 需确认调用方不用 `content` |
| 减少每次检索的连接数 | 减少往返 | 需确认连接池配置 |

> 上面几项都**尚未实施**，需要单独排期。

---

## 附：本次优化周期的完整改动清单

同一天围绕「让答疑中台更快」做的一整轮工作，全部走独立分支 + PR 合并，
每项都有真实测量依据并在服务器上实测验证：

| PR | 内容 | 关键数据 |
|---|---|---|
| #98 | 为 `model_configuration` 的 JSON 表达式查询补索引；修正「嵌入占 84%」的错误结论 | worker 轮询查询 215.745 → 0.041 ms；该 worker 进程整体 430 → 4.2 ms |
| #99 | 补充审计文档：按 PID 隔离后的真实耗时分解 | 请求 455 ms 中数据库只占 85.8 ms |
| #100 | **本次**：消除适用范围解析热路径上的重复归一化 | 检索 450.9 → 160.1 ms |
| #101 | 把 `.env.example` 的 blob 规范化为 LF | 消除每次 checkout 的永久「已修改」状态 |

另外两项服务器配置改动（不在代码仓库内）：

| 改动 | 关键数据 |
|---|---|
| `ALTER SYSTEM SET log_statement = 'none'` + reload | 日志 11 行/秒 → 0 行/秒；历史日志累计 26 GB |
| `postgresql.conf` 第 887 行 `log_statement = all` → `'none'` | 防止 `auto.conf` 被清空后日志再次刷屏，零停机 |
