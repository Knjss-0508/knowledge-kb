# 生产上线记录：修复「全部」后月份不可选 + 自定义日期区间（#230，2026-10-23）

本文记录 2026-10-23 把 `master` 的 `d83dbd4`（PR #230）上线到
`https://knowledgekb.powerzhuan.cn` 的过程与取证。时间均为线上 `date` 回显的 CST（UTC+8）。

本次上线的起点是 `e48cb01`（#228），即本次之前线上正在运行的版本。
承接 `docs/production-deploy-20261023-blind-label-ranking-retired-month.md`。

## 1. 问题

### 1.1 点过「全部」后月份不可选（可复现）

月份输入框的 `:disabled` 绑定了 `ranking.allTime`：

```html
:disabled="blindLabeling.ranking.loading || blindLabeling.ranking.allTime"
```

点「全部」把 `allTime` 置为 `true`，输入框随即变灰；而把 `allTime` 改回 `false`
的唯一入口就是那个已被禁用的输入框 —— 形成死锁，只能刷新页面。

### 1.2 无法按「某日到某日」筛选

原设计只有「整月」与「全部」两种粒度。

## 2. 改动

用互斥的 `mode`（`month` / `range` / `all`）替代 `allTime` 布尔值。

| 文件 | 改动 |
| --- | --- |
| `frontend/index.html` | `ranking` 状态由 `allTime` 改为 `mode` + `dateFrom`/`dateTo`；工具栏新增「自定义日期」按钮与两个 `type="date"` 输入；新增 `toggleBlindLabelRankingRange`、`applyBlindLabelRankingDates`；`blindLabelRankingRange` 按 mode 分派；切换模式保留已选值；当前模式高亮 |
| `backend/tests/test_blind_labeling_frontend.py` | 新增契约用例：断言月份框不再被 `allTime` 禁用、三种 mode 切换、切「全部」不清空已选值、两个 date 输入与起止联动、单日回显 |

关键行为：

- 三个模式只决定「用哪组日期」，**切换时不禁用任何控件**；
- 点「全部」后**保留** `month` / `dateFrom` / `dateTo`，切回无需重选；
- 起止日期单向越界自动纠正（结束早于开始时把结束对齐到开始）；
- 单日区间在回显里显示为单日期；
- 首次进入「自定义日期」时用当前月首尾作为起点。

## 3. 上线前只读基线（17:17 +08）

快照落盘 `/opt/knowledge-kb-runtime/backups/blind-label-range-picker-20261023/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:ranking-retired-month-e48cb01` |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578` |
| 容器内 `/frontend/index.html` | md5 `7a4af4f3a319e6bf2c0f9ebafaac229f`，806,174 B |
| `alembic current` | `20261013_01_hf_persist (head)` |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:ranking-retired-month-e48cb01` |

## 4. 构建

```bash
git archive --format=tar.gz -o master-d83dbd4.tar.gz d83dbd4
# 3,211,772 B，sha256 1e5b1c34f2dfb7173b9d99fae486e31d7ab5fbf7b70667ecc716a6947c8ec435
```

上传解包到 `/opt/knowledge-kb-deploy/d83dbd4`（服务端 `sha256sum` 与本地一致），构建：

```bash
cd /opt/knowledge-kb-deploy/d83dbd4
SAFE_DOCKER_BYPASS=1 /usr/bin/docker build --cgroup-parent=qa-build.slice \
  -f backend/Dockerfile -t knowledge-kb-backend:ranking-range-picker-d83dbd4 .
```

镜像 `knowledge-kb-backend:ranking-range-picker-d83dbd4`（`06e492d2543c`），镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | 与源码 `b16e769fc5cfc521bab3b298430ec0ed` 相同 | ✅ 相同 |
| 文件大小 | 809,204 B | ✅ 相同 |
| `ranking.loading \|\| ...allTime` 禁用标记 | 0 | ✅ 0 |
| `allTime` 残留 | 0 | ✅ 0 |
| `dateFrom` 输入 | 1 | ✅ 1 |
| `toggleBlindLabelRankingRange` | ≥1 | ✅ 2 |
| `applyBlindLabelRankingDates` | ≥1 | ✅ 2 |
| `ranking.mode =` 赋值（month/range/all） | 4 处 | ✅ 4 |

## 5. 切换（17:17:10 +08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:ranking-range-picker-d83dbd4
```

**本次无数据库迁移**，`alembic current` 保持 `20261013_01_hf_persist (head)`。

切换前后逐项比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `89cb3c17…` | `89cb3c17…` | ✅ 一致 |
| 运行规格 md5 | `bfc47241b08ee82c928caf44a7e1506d` | 同 | ✅ 一致 |
| 挂载 | 3 项（顺序不同，集合一致） | 3 项 | ✅ 一致 |
| 前端 md5 | `7a4af4f3…`（806,174 B） | `b16e769f…`（809,204 B） | ✅ 已更新 |
| `alembic current` | `20261013_01_hf_persist` | 同 | ✅ 未变 |

## 6. 上线后验收

| 检查 | 结果 |
| --- | --- |
| 容器 | `Up`，`RestartCount=0`，`OOMKilled=false` |
| 线上 `/app` | 200，809,204 B，md5 `b16e769fc5cfc521bab3b298430ec0ed`（与源码一致） |
| `/health` / `/ready` | 200 / 200 |
| 旧死锁标记 `ranking.loading \|\| ...allTime` | **0** |
| `toggleBlindLabelRankingRange` | 2 |
| `type="date" ... dateFrom` | 1 |
| `ranking.mode = 'all'` | 1 |
| 近 8 分钟 Traceback / CRITICAL | 0 |
| 近 8 分钟 5xx | 0 |

## 7. 已知风险与后续

- 切换存在**约数秒 502 窗口**（重建容器所致，既有发布流程固有行为）。
- 前端为单文件 `index.html`，客户端旧缓存需 `Ctrl+F5`。
- 本次只改 kb-backend 镜像；`prototypes/answer-hub` 未部署，服务未重启。

## 8. 回滚

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:ranking-retired-month-e48cb01
```

无迁移，回滚只需换镜像，无需回滚数据库。
