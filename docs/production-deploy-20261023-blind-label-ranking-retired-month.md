# 生产上线记录：排行完成工单数与「我的已标注」对齐 + 月份选择器（#228，2026-10-23）

本文记录 2026-10-23 把 `master` 的 `e48cb01`（PR #228）上线到
`https://knowledgekb.powerzhuan.cn` 的过程与取证。时间均为线上 `date` 回显的 CST（UTC+8）。

本次上线的起点是 `493b3a6`（#226），即本次之前线上正在运行的版本。
承接 `docs/production-deploy-20261023-blind-label-count-wording.md`。

## 1. 问题

### 1.1 完成工单数 278 vs 我的已标注 300

标注员陈勇江在「标注排行」的完成工单数是 **278**，「我的已标注」是 **300**。

生产库只读核对（按用户名聚合，2026-10-23 16:40 前后）：

| 指标 | 值 |
| --- | --- |
| `completed`（全部） | **300** |
| 其中位于已退池工单 | **22** |
| `released`（已回收） | 124 |
| 派发总数 | 424 |

按月份分布：

| 月份 | 派发 | 完成 | 其中退池完成 | 回收 |
| --- | --- | --- | --- | --- |
| 2026-09 | 64 | 0 | 0 | 64 |
| 2026-10 | 360 | 300 | 22 | 60 |

根因：排行接口默认 `include_retired=False`（排除已退池工单），而「我的已标注」
不做退池过滤。`300 − 22 = 278`，两个页面因此对同一概念给出两个数字。

### 1.2 时间筛选不够用

排行只有 全部 / 近30天 / 近7天 / 当日 四档，无法精确核对某个自然月。

## 2. 改动

| 文件 | 改动 |
| --- | --- |
| `backend/app/services/blind_labeling.py` | `annotation_ranking()` 的 `include_retired` 默认值 `False → True`；docstring 写明与「我的已标注」同口径及 22 单差异来源 |
| `backend/app/routes/blind_labeling.py` | `GET /blind-labeling/ranking` 的 Query 默认值与描述同步 |
| `frontend/index.html` | 时间筛选改为 `type="month"` 月份选择器（默认当月）+「全部」开关 + 区间回显；`ranking` 状态由 `period` 改为 `month`/`allTime`；新增 `blindLabelRankingCurrentMonth`、`blindLabelRankingRangeLabel`、`applyBlindLabelRankingMonth`、`setBlindLabelRankingAll`；「完成工单数」悬停说明改为「与我的已标注同一口径」 |
| `backend/tests/test_blind_labeling.py` | 用例改为断言「默认包含退池、显式 `include_retired=False` 才排除」 |
| `backend/tests/test_blind_labeling_frontend.py` | 新增月份选择器契约用例（默认当月、自然月末计算、「全部」开关） |

行为变更：

- 排行的「完成工单数」会**变大**（纳入退池工单上的完成记录），这是本次目标；
- 显式调用 `include_retired=false` 仍可排除退池，接口保持向后兼容；
- 排序主键仍是标注条数，口径未变；
- 月份区间取**完整自然月**（首日 → 月末最后一天），后端 `end_date` 按含当天处理。

## 3. 上线前只读基线（16:57 +08）

快照落盘 `/opt/knowledge-kb-runtime/backups/blind-label-retired-month-20261023/`。

| 项 | 值 |
| --- | --- |
| 切换前镜像 | `knowledge-kb-backend:blind-label-unit-493b3a6` |
| 环境变量 md5 | `89cb3c17c7a12154c6aa1fa6c38b6578` |
| 容器内 `/frontend/index.html` | md5 `d935757989750d9d30038d502a460155`，804,878 B |
| `alembic current` | `20261013_01_hf_persist (head)` |
| 回滚锚点 | `bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:blind-label-unit-493b3a6` |

## 4. 构建

```bash
git archive --format=tar.gz -o master-e48cb01.tar.gz e48cb01
# 3,209,039 B，sha256 8a44dd854f6f008df89f9790831f77a38cebca23b587ad8ed7a467f5a0795510
```

上传解包到 `/opt/knowledge-kb-deploy/e48cb01`（服务端 `sha256sum` 与本地一致），构建：

```bash
cd /opt/knowledge-kb-deploy/e48cb01
SAFE_DOCKER_BYPASS=1 /usr/bin/docker build --cgroup-parent=qa-build.slice \
  -f backend/Dockerfile -t knowledge-kb-backend:ranking-retired-month-e48cb01 .
```

镜像 `knowledge-kb-backend:ranking-retired-month-e48cb01`（`d76adb8f79f1`），镜像内校验：

| 校验项 | 期望 | 实测 |
| --- | --- | --- |
| `/frontend/index.html` md5 | 与源码 `7a4af4f3a319e6bf2c0f9ebafaac229f` 相同 | ✅ 相同 |
| 文件大小 | 806,174 B | ✅ 相同 |
| 月份选择器标记 | 1 | ✅ 1 |
| `blindLabelRankingCurrentMonth` | ≥1 | ✅ 4 |
| 旧提示「本表默认不计已退池工单」 | 0 | ✅ 0 |
| `include_retired: bool = True` | 1 | ✅ 1 |
| `alembic heads` | `['20261013_01_hf_persist']` | ✅ 相同 |

## 5. 切换（16:59:06 +08）

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:ranking-retired-month-e48cb01
```

**本次无数据库迁移**，`alembic current` 保持 `20261013_01_hf_persist (head)`。

切换前后逐项比对：

| 项 | 切换前 | 切换后 | 判定 |
| --- | --- | --- | --- |
| 环境变量 md5 | `89cb3c17…` | `89cb3c17…` | ✅ 一致 |
| 运行规格 md5 | `bfc47241b08ee82c928caf44a7e1506d` | 同 | ✅ 一致 |
| 挂载 | 3 项 | 3 项 | ✅ 一致 |
| 前端 md5 | `d9357579…`（804,878 B） | `7a4af4f3…`（806,174 B） | ✅ 已更新 |
| `alembic current` | `20261013_01_hf_persist` | 同 | ✅ 未变 |

## 6. 上线后验收

### 6.1 生产库口径复核（容器内只读调用）

| 调用 | 陈勇江 completed | assigned | released |
| --- | --- | --- | --- |
| 全部（含退池，**新默认**） | **300** | 300 | 124 |
| 2026-10（含退池） | **300** | 300 | 105 |
| 全部（排除退池，旧口径） | 278 | 278 | 124 |

**结论：与「我的已标注」的 300 完全一致**；旧口径仍可通过
`include_retired=false` 取得，未被移除。

### 6.2 线上检查

| 检查 | 结果 |
| --- | --- |
| 容器 | `Up`，`RestartCount=0`，`OOMKilled=false` |
| 线上 `/app` | 200，806,174 B，md5 `7a4af4f3a319e6bf2c0f9ebafaac229f`（与源码一致） |
| `/health` / `/ready` | 200 / 200 |
| 月份选择器标记 | 1 |
| 旧函数 `blindLabelRankingPeriods` | **0**（已移除） |
| 旧提示「本表默认不计已退池工单」 | **0**（已替换） |
| 新提示「与我的已标注同一口径」 | 1 |
| 近 8 分钟 Traceback / CRITICAL | 0 |
| 近 8 分钟 5xx | 0 |

## 7. 已知风险与后续

- 切换存在**约数秒 502 窗口**（重建容器所致，既有发布流程固有行为）。
- 排行「完成工单数」变大属预期；若后续业务上想区分「是否含退池」，
  可在页面上加开关，接口已支持。
- 本次只改 kb-backend 镜像；`prototypes/answer-hub` 未部署，服务未重启。
- 前端为单文件 `index.html`，客户端旧缓存需 `Ctrl+F5`。

## 8. 回滚

```bash
bash /opt/knowledge-kb-runtime/run-kb-backend.sh knowledge-kb-backend:blind-label-unit-493b3a6
```

无迁移，回滚只需换镜像，无需回滚数据库。
