# 生产上线记录：业务层级优先级整合与候选人自动化修复（2026-11-03）

本次把 PR #259（`48cdff6`，第二部分业务层级优先并按 12 品类单向纠正）整合到最新
`master`（`8dc6af0`）后上线，同时修复两处在整合后才暴露的阻断问题，并处理了
2026-11-01 自动化批次的中断。时间为服务器 `date` 回显的 CST。

上线起点：`knowledge-kb-backend:candidate-pause-10ed87b`（源码 `10ed87b`），
数据库迁移 `20261028_01_candidate_pause`。

## 1. 代码审视结论

### 1.1 PR #259 本身

- 规则方向正确：第二部分已有「回收业务层级」时以源值为准；只有「自营 + 非上门
  12 类」这一种矛盾才纠正为聚合回收。聚合回收的品类库本来就包含手机等上门品类，
  不能按品类改回自营。
- 前端、候选复核、草稿提交、发布审核四处使用同一套规则，并补了 Manhattan
  类目 ID → 名称的还原，避免只看 ID 无法判断 12 品类归属。
- 发布审核遇到需要纠正业务类型时返回 409 `BUSINESS_TYPE_CORRECTED` 并重新入队
  查重/向量，不会用旧业务类型的查重结果直接发布。

### 1.2 整合时发现并修复的三处问题

| # | 问题 | 后果 | 修复 |
| --- | --- | --- | --- |
| 1 | `if "business_type" in body.model_fields_set` 即记为「人工选择」 | 前端保存任意字段都会带上当前 `business_type`，历史值被冻结成 `business_type_source=human`，之后源端层级回放与品类纠正全部失效 | 改为只有值真正变化时才标记人工结论 |
| 2 | `20261103_01_drop_idx` 删除 15 个 `retrieval_quality_events` 索引，但 `initialize_database.validate_schema` 仍强制要求其中 2 个 | 迁移成功后 migrate 容器报「索引缺失」并以非 0 退出，后端无法启动 | 从强校验清单移除这 2 个索引，并加回归测试 |
| 3 | Answer Hub 侧 `知识草稿状态=blocked`（模型输出质量校验失败、正文为空）不在 `DRAFT_INCOMPLETE_STATUSES` | 单条脏数据被本地判为失败 → 整批 `failed=1` → 自动化退出码 1 → 日期游标不前进 | 把 `blocked` 归入「草稿未就绪」，送候选价值复核由人工补齐 |

第 3 项正是 2026-11-01 批次中断的根因。

## 2. 存量影响评估（上线前，只读）

用新规则对生产 26,029 条 `knowledge_items` 做离线推导，其中已发布且有适用品类的
25,950 条：

| 现状 | 新规则推导 | 条数 |
| --- | --- | --- |
| self_operated | self_operated（不变） | 25,942 |
| self_operated | aggregated（应为聚合） | 5 |
| self_operated | 无品类证据，保留原值 | 3 |

即存量只有 5 条会在下次保存/审核时被纠正，不会批量改判。

## 3. 构建与校验

```bash
git archive --format=tar.gz -o integration-840f2ff.tar.gz 840f2ff
# 3,296,925 B
# sha256 5f5d6b6b7f6e7007eda099f78ecfff596455ff56ffd047cce883539f0a11e55f
```

镜像 `knowledge-kb-backend:integration-840f2ff`（`3ea2dd3a04f6`，498 MB）。

| 校验项 | 结果 |
| --- | --- |
| 镜像内 4 个关键文件 md5 与源码一致 | ✅（integration.py / initialize_database.py / candidate_business.py / candidate_model_tasks.py） |
| 镜像内保留 `business_type_source` 写保护逻辑 | ✅ 1 处 |
| 镜像内已移除失效索引强校验 | ✅ 0 处 |
| `alembic heads` | `['20261103_01_drop_idx']` 单头 ✅ |
| 镜像 CMD / workdir 与线上一致 | ✅ |

影子启动（`BACKGROUND_WORKERS_ENABLED=false`、只读挂载、`127.0.0.1:18000`）：

| 检查 | 结果 |
| --- | --- |
| `/ready`、`/health`、`/app` | 200 |
| 未鉴权 `candidate-model-automation` / `candidate-reviews` | 401 |
| 启动日志 Traceback / QueuePool | 0 |

## 4. 备份

`/opt/knowledge-kb-runtime/backups/integration-20261103/`

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:candidate-pause-10ed87b` |
| 环境变量 md5 | `5d5a6bba4d55a00f7b8da61f068ecbd7` |
| 挂载 md5 | `b9c504303a5b0d6f11ce8525819a7288` |
| 端口/Cmd/Entrypoint 规格 md5 | `7a2118458cfc4878af928de6d30ed853` |
| 内存上限 | 4 GiB / swap 4 GiB |
| 数据库全量备份 | `knowledge_base.before.dump`（783 MB，custom format） |
| 上线前行数 | ingestions 34,684 / knowledge_items 26,029 / tasks 284 / retrieval events 1,320,122 |

## 5. 迁移

本次迁移只把 `alembic_version` 推进到 `20261103_01_drop_idx`：15 个索引在生产上
**早已手工删除**（上线前实测该表只剩 5 个索引），`DROP INDEX IF EXISTS` 全部为空操作，
逐条无锁竞争，未影响在读请求。

```text
15:26:30  alembic upgrade head（旧后端继续服务）
15:26:31  20261103_01_drop_idx (head)
15:26:31  Database schema is ready: revision=20261103_01_drop_idx, vector=0.8.6, dimensions=1024
```

## 6. 切换

```text
15:27:26  停止旧后端
15:27:26  启动 integration-840f2ff
```

切换前后比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `5d5a6bba…` | `5d5a6bba…` | ✅ |
| 挂载 md5 | `b9c50430…` | `b9c50430…` | ✅ |
| 规格 md5 | `7a211845…` | `7a211845…` | ✅ |
| 内存 / MALLOC_ARENA_MAX | 4 GiB / 2 | 同左 | ✅ |
| 其他容器 | redis / appeal / voc | 未重建 | ✅ |

## 7. 上线后验收

| 检查 | 结果 |
| --- | --- |
| 公网 `/app` | 200，0.29 s |
| 公网 `/health` | 200 |
| 页面 sha256（公网 vs 容器内） | 一致 `da65220e…`（835,226 B） |
| 页面含业务类型辅助函数 | 5 处命中 |
| 未鉴权候选列表 / 自动标注 | 401 |
| `alembic current` | `20261103_01_drop_idx (head)` |
| 日志 Traceback / QueuePool / worker 异常 | 0 |
| 内存 | 567 MiB / 4 GiB（13.9%） |

业务类型规则实测（容器内只读调用）：

| 场景 | 期望 | 实际 |
| --- | --- | --- |
| 自营 + 手机 | self_operated | ✅ |
| 自营 + 智能手环（非 12 类） | aggregated | ✅ |
| 显式聚合 + 手机 | aggregated | ✅ |
| 源端层级=聚合回收 | aggregated | ✅ |
| 源端层级=自营回收 + 智能手环 | aggregated | ✅ |
| 人工显式选择聚合回收 | aggregated（不被源端改回） | ✅ |
| 无人机等非上门品类 | aggregated | ✅ |
| 占位品类「待确认」 | 保留历史值 | ✅ |

## 8. 自动化中断处理

### 8.1 中断现象

`job-20261101-020040-fe473a2f`（沉淀 2026-10-21）在「同步答疑中台候选价值复核」
阶段失败 1 条：`TOP-0B9BD55A27`「手写笔连接还原是否需要解绑账号」正文为空、
`知识草稿状态=blocked`，被本地判成 `LOCAL_VALIDATION_ERROR 缺少知识内容。`。

队列文件被移入 `failed`，调度脚本第二轮起检测到 `failed` 有文件即跳过，游标停在
2026-10-22 不再前进（10-31 之后的 11-02、11-03 两次都只是 defer）。

### 8.2 处置

1. 用修复后的代码做预校验：980 条全部可构造送审载荷，本地拒绝 0 条。
2. `retry-cz-sync --run-id 20261101-020041-18cf367d`（只重跑 CZ 同步，不重跑模型、
   不重聚类）。

结果：

| 项 | 重试前 | 重试后 |
| --- | --- | --- |
| 运行状态 | failed | **review_pending** |
| CZ 同步 | queued 850 / reused 126 / failed 1 | queued 1 / reused 976 / **failed 0** |
| 队列文件 | `failed/` | `completed/` |
| 失败队列文件数 | 1 | **0** |

3. 清除计划里的失败标记（`automation_schedule resume`），`cursor_date` 与
   `last_successful_*` 一律不动，因此没有跳过任何日期。

`-DryRun` 复核：窗口回到 `2026-10-22 ~ 2026-10-22`，pending/processing/failed
三个桶均为 0，安全闸通过。

## 9. 回滚

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:candidate-pause-10ed87b
```

本次迁移不新增/修改任何表结构，旧镜像可直接运行；如需回退版本号：
`alembic downgrade 20261028_01_candidate_pause`（会重建 15 个低基数索引，
不建议在业务高峰执行）。数据回退使用 `knowledge_base.before.dump`。

自动化侧如需回退队列状态，可用
`E:\answer-hub-runtime\data\automation-plan.json.bak-20261103`。

## 10. 已知风险

1. **沉淀价值判断仍需人工**：`blocked` 候选现在会进入候选价值复核并显示
   `draft_incomplete`，需要人工补齐正文或判定不沉淀；不会自动发布。
2. **源端层级依赖 Answer Hub**：若上游不给「回收业务层级」，只能按品类推断，
   缺少可识别品类时保留历史值（本机存量只有 3 条属于这种情况）。
3. **自动模型标注开关仍为关闭**（`enabled=false`，未暂停）。本次不改变该开关。
4. **计划游标停在 2026-10-22**，即 10-22 之后的数据尚未沉淀；夜间任务恢复后会
   按天顺延，历史积压仍需若干天跑完。
