# 生产上线记录：高频问题同步解析缺陷修复（3c078f6，2026-10-19）

本文记录 2026-10-19 把 `codex/fix-hf-source-level-overflow-20261019` 分支的
`3c078f6` 上线到 `https://knowledgekb.powerzhuan.cn` 的过程与取证。时间为线上
`date -u` 回显的 UTC（+08:00 另注）。

承接 `docs/production-deploy-20261019-high-frequency-dashboard.md`（#211 上线记录）。

## 1. 问题现象

高频问题看板「查询」正常，但「同步数据」在含异常工单的日期范围会失败。用户选
`2026-10-18` 单日时提示「数据源已连接：获取 0 条…」。排查发现两个独立原因：

1. **上游当日确实无数据**：逐日探测 `qa.powerzhuan.cn/api/records`，`10-17`～`10-19`
   均返回 0 条（最近有数据的是 `10-16`，1303 条）。空日期返回 HTTP 200 + 空列表，
   所以「获取 0 条」是正确行为，不是故障。
2. **真实缺陷**：把日期换到有数据的范围后，同步仍会 500 ——

   ```
   psycopg2.errors.StringDataRightTruncation:
   value too long for type character varying(128)
   [SQL: INSERT INTO high_frequency_source_records (... level1 ...)]
   ```

## 2. 根因

上游部分工单的 `ai_result` 是「【字段】+ 正文」的文本报告，而 **`【判定依据】` 的正文会
引用字段名**，例如：

```
【一级分类】
基本信息问题

【判定依据】
平台标准依据：输入规则中，【产品类型】仅能从手机/平板/电脑等选项选择；
【一级分类】中“基本信息问题”包含“开机情况”“不回收类型”等二级分类。…
```

`parse_ai_result` 按正则扫描**所有** `【字段】` 标记，并把每个标记当作字段起点，后者覆盖前者。
于是 `【判定依据】` 里那句引用把 `一级分类` 的值替换成了其后整段散文（397 字），
写入 `high_frequency_source_records.level1`（`VARCHAR(128)`）时越界，整批同步回滚。

真实数据量化（2026-10-15~16，6303 条）：

| 项 | 越界前 |
| --- | --- |
| `level1` 最大长度 | **397**（限 128） |
| 越界记录数 | **1**（`level1`） |
| `level2` 最大长度 | 25（限 128） |
| `category` 最大长度 | 7（限 128） |

只有 1/6303 条越界，但足以让整批同步失败。

## 3. 修复

### 3.1 解析器：首次出现优先

`backend/app/services/high_frequency.py` 的 `parse_ai_result`：报告的字段按固定模板顺序
**只出现一次**，因此后续同名标记一律视为正文引用，不再覆盖已解析的真实值。

```python
if not field or field in result:
    # 之后的同名标记是正文引用（判定依据常引用「【产品类型】/【一级分类】」），
    # 覆盖真实值会把整段散文写进有界列，令整批同步失败。
    continue
```

### 3.2 写入边界：按列宽钳制

`backend/app/services/high_frequency_pipeline.py` 新增 `_column_limit` / `_bounded`，
按模型声明的列宽钳制写入值，把「单条畸形记录拖垮整批」改成「单条安全降级」：

- `high_frequency_source_records`：`level1`、`level2`、`category`、`analyzed_at`、`record_id`
- `high_frequency_standard_questions`：`normalized_question`、`standard_question`、`business_theme`
- `high_frequency_atomic_questions`：`normalized_question`

完整原文仍保留在 `raw_payload` 供审计。`record_id` 是唯一同步键，异常超长时**改用内容哈希**
而不是截断，避免把两个不同工单并成一条。

### 3.3 回归测试

- `test_field_citations_inside_judgment_basis_never_overwrite_real_values`：字段引用不覆盖真实值。
- `test_overlong_bounded_fields_are_clamped_instead_of_failing_the_batch`：超长有界字段被钳制，
  且同批其余记录正常入库。

本地测试：`test_high_frequency*.py`、`test_second_part_client.py`、`test_migration_revisions.py`
共 **56 passed**。（`test_explicit_numbered_issues_are_split_before_frequency_counting`
在修复前即失败，属 #211 遗留，与本次改动无关，已单独记录。）

## 4. 上线前真实数据 A/B

用线上镜像里的解析器与修复版解析器，对 2026-10-15~16 的 **6303 条真实数据**逐条对比
（修复版通过 `PYTHONPATH`/独立模块名加载，不改动运行中镜像）：

| 解析器 | `level1` 最大长度 | 越界条数 |
| --- | --- | --- |
| 线上旧版 | 397 | 1 |
| 修复版 | 8 | **0** |

**仅 1 条记录的 `(category, level1, level2)` 变化**：`level1` 由 397 字整段文字恢复为
`基本信息问题`；其余 **6302 条逐字节一致**，无回归。

## 5. 构建（14:29:18+08 → 14:29:20+08）

```bash
git archive --format=tar.gz -o fix-3c078f6.tar.gz 3c078f6   # 3,053,417 B
# sha256 2df2d09ae8f8ca4fbd857bc3b0cb814f87d8c6b8e518ef3a89669370f60c7b45
```

镜像 `knowledge-kb-backend:hf-sync-fix-3c078f6`（`ec6ec02028c6`，496MB），镜像内校验：

| 校验项 | 实测 |
| --- | --- |
| 解析器「首次出现优先」守卫 | ✅ 1 |
| `_bounded` 边界助手 | ✅ 1 |
| `/frontend/index.html` md5 | ✅ `184051a05225911e8b2e224369df127b`（与 #211 一致，前端未变） |
| alembic heads | ✅ `['20261013_01_hf_persist']` |
| `cmd` / `workdir` | ✅ 与线上一致 |

## 6. 切换（06:29:42Z / 14:29:42+08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:hf-sync-fix-3c078f6
# 新容器 aae09653fcd1521112f971e000380efb31732cd518e7c9389387bae2e66c67e5
```

**本次无数据库迁移**，`alembic current` 保持 `20261013_01_hf_persist (head)`。

切换前后：环境变量 md5 完全一致（`89cb3c17c7a12154c6aa1fa6c38b6578`，含上一步新增的
4 个 `SECOND_PART_*` 键）；运行规格 md5 完全一致；容器内前端 md5 未变；解析器守卫由 0 → 1。

> 说明：`{{json .Mounts}}` 的 md5 在同一容器上连续取值会不稳定（实测 3 次得到
> `b9c5…`/`d10c…`/`b9c5…`），属 inspect 输出顺序问题。逐项列出确认 3 个挂载齐全
> （`uploads`、`manhattan_cache:/app/data`、`/www/wwwlogs:/app/nlogs:ro`），与长期运行的
> 参考容器一致。

## 7. 上线后真实端到端验收

### 7.1 真实同步（此前必然失败的日期）

对 `2026-10-16`（含那条溢出记录）执行真实同步：

| 项 | 结果 |
| --- | --- |
| `fetched` | 1303 |
| `inserted` | 1303 |
| `queued` | 1303 |
| 耗时 | 约 6.0s |
| `level1`/`level2`/`record_id` 最大长度 | 8 / 25 / 19，**越界 0 条** |
| 此前溢出记录 `2111040070843633687` | `level1='基本信息问题'`、`level2='不回收类型'`、`category='笔记本'` ✅ |

### 7.2 后台归类

1303 个任务全部被后台 worker 领取处理：`completed` 1302、`ready` 1298、`review` 4。
期间出现 1 条 `failed`（`DeepSeek 问题拆分返回不完整，预期 1 条。`），属模型偶发返回不完整，
与本次缺陷无关。

### 7.3 失败记录可自动恢复

对同一日期范围再次同步：`unchanged 1303`、**`requeued 1`**、`queued 1` —— 只重排了那条失败记录。
10 秒后 worker 处理完成：`ready` 1298 → **1299**，`failed` **归零**。

### 7.4 看板渲染

| 项 | 结果 |
| --- | --- |
| `total_records` | 1303 |
| `ready_record_count` | 1299 |
| `high_frequency_count` | 109 |
| 高频排行行数 | 109 |
| `worker_state` | `completed` |

排行示例：`笔记本 | 硬盘是否品牌、原装或第三方 | 132 次`、
`笔记本 | 内存是否品牌、原装或第三方 | 116 次`、
`手机 | 本机显示正品部件但工具读出电池异常如何判定 | 55 次`。

### 7.5 运行期

`kb-backend` Up，镜像 `hf-sync-fix-3c078f6`；Traceback **0**、`500` **0**；
其余容器（`kb-redis`、`kb-embedding-qwen`、`appeal-exemption-*`、`voc-workbench-*`）状态未受影响。

## 8. 回滚

```bash
# 回到本次修复前的 #211 镜像（无迁移，立即生效）
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:hf-dashboard-26486fd
```

本次无数据库迁移，回滚只换镜像。已入库的 1303 条来源、原子问题与标准问题不受影响。

## 9. 已知边界与遗留

1. **上游数据会有空洞日**：`10-17`～`10-19` 无数据。看板对空范围返回 0 条是预期行为；
   建议同步前先用「检查数据源」确认当日有数据。
2. **宽日期范围会超时**：客户端按 5000 条上限二分窗口并取回全部数据，上游对大范围响应较慢，
   实测 `2026-09-28~10-19` 会读超时并报「第二部分接口暂时不可用」。建议按周/按天同步。
3. **`review` 状态记录**：4 条因模型无法可靠拆分而被自动排除在排行外（看板会提示条数），
   属设计内行为。
4. **`test_explicit_numbered_issues_are_split_before_frequency_counting` 为既有失败**：
   修复前即失败（`build_dashboard` 用的是 `_deterministic_classification` 的既有词汇表，
   与测试期望的「品牌、原装或第三方」标准标题不一致），与本次改动无关，本次未改动它。
