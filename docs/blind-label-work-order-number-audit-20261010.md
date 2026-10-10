# 盲标工单号核查报告（2026-10-10）

> 起因：盲标弹窗里的工单号 `2108536379561477121` 在「答疑工作台 2.0 / 工单管理」按工单号查询返回「暂无数据」。
> 范围：**只读核查**，未修改任何数据库记录、未改动线上代码。数据快照时间 2026-10-10 14:38（服务器时间）。

## 一、结论摘要

| 项目 | 结果 |
| --- | --- |
| 核查对象 | `blind_label_work_orders` 全部 **1,292** 条（`question_form_id` 100% 等于 `conversation_id`） |
| 真工单号（上游工单详情有数据） | **1,076 条（83.3%）** |
| 疑似会话号（只有聊天记录、没有工单详情） | **216 条（16.7%）** |
| 其中：上游明确答复「查不到」 | **57 条**（工单详情接口返回 123/124 字节表头） |
| 其中：本次日志里未见工单详情调用 | 159 条（仅有聊天记录 >300 字节的旁证） |
| 同时具备两类数据的号码 | **0 条**（工单号/会话号两个 ID 空间互斥，与 10-08 的经验一致） |
| 受影响数据量 | 216 条里有 **63 条**已被指派过：**194 条指派 / 344 条人工标注**（其中已提交 133 条、待标注 43 条、进行中 2 条、已回收 16 条） |

一句话：**盲标池里混进了 16.7% 的「会话号」**。这些号码在上游是「聊天会话」而不是「工单」，
用户按工单号去工单管理里查自然查不到。

## 二、单个号码的完整证据链（`2108536379561477121`）

该号对应盲标工单 `wo-1a586c09714745a29397ac46`（源事件 `rqe-ffb53849b1d2` / `rqe-c989389e8f69`，
`source_created_at` 2026-10-09 12:40:25，物化时间 2026-10-10 01:04:32，快照 3 条候选 A-18670/A-18654/A-18827）。

zzdy 反代 nginx 访问日志（`/www/wwwlogs/zzdy.powerzhuan.cn.log`）里的原始记录：

```
# 工单详情接口：4 次，全部返回 123/124 字节（上游「查不到」的表头）
... [10/Oct/2026:11:02:04 +0800] "GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=2108536379561477121&sceneType=2 HTTP/1.1" 200 124 ...
... [10/Oct/2026:13:57:07 +0800] "GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=2108536379561477121&sceneType=2 HTTP/1.1" 200 123 ...
... [10/Oct/2026:13:57:14 +0800] "GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=2108536379561477121&sceneType=2 HTTP/1.1" 200 124 ...
... [10/Oct/2026:13:57:31 +0800] "GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=2108536379561477121&sceneType=2 HTTP/1.1" 200 124 ...

# 聊天记录接口：87 次，有数据（827/853 字节），且 10-09 20:34 那几次来自助手客户端（Electron/39.8.10）
... [09/Oct/2026:20:34:03 +0800] "GET /nmhtapi/im/history?conversationId=2108536379561477121 HTTP/1.1" 200 827 ... Electron/39.8.10 ...
... [09/Oct/2026:20:34:09 +0800] "GET /nmhtapi/im/history?conversationId=2108536379561477121 HTTP/1.1" 200 827 ... Electron/39.8.10 ...
```

对照真工单号的同一位置（`2108802993527718918`，盲标工单 `wo-79a2bdcbde85468b8793da40`）：

```
... [10/Oct/2026:14:13:46 +0800] "GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=2108802993527718918&sceneType=1 HTTP/1.1" 200 656 ...   ← 有数据
（同号 im/history 无记录）
```

## 三、判定方法（可复现）

1. 取「工单详情接口」响应体大小作为判据：`GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>&sceneType=1|2`
   — 有数据 534–1128 字节，**查不到只有 123/124 字节**（见 `docs/qa-web-assistant-work-order-id-spec.md:40`）。
   聊天接口 `GET /nmhtapi/im/history?conversationId=<号>` 只有存在会话才返回内容（>300 字节）。
2. 判据在真实日志中复核（取 10-08 已确认的真工单号）：
   `2104401146108973610` → 工单详情 639 字节 / 聊天 68 字节；`2104404253270344801` → 650 字节 / 68 字节。
3. 全量扫描 zzdy 反代日志（无需 Cookie）：`/www/wwwlogs/zzdy.powerzhuan.cn.log`（9.5 GB，扫 32,523,205 行、命中 19,834 行、45 秒）
   + `/root/nmht-paths-20261008.txt`（3.8 GB，扫 12,526,483 行、命中 1,558 行、23 秒），阈值 300 字节。
4. 判定口径：
   - 工单详情最大响应 > 300 字节 → **真工单号**；
   - 工单详情 123/124 字节 或 从未调用，且聊天最大响应 > 300 字节 → **疑似会话号**；
   - 两者都无 → 无日志记录（本次 0 条）。

扫描脚本（已入库）：`scripts/audit/dump_blind_label_pool_ids.py`（容器内导出号码 + 指派/标注计数）、`scripts/audit/scan_nmht_access_log_for_work_orders.py`（宿主单遍扫日志出判定）。

## 四、全量结果分布

- 按创建日：`2026-10-08` 9 条、`2026-10-09` 103 条、`2026-10-10` 104 条（09-28 ~ 09-30 那批 151 条全部是真工单号，即 10-08 清理保留下来的那批）。
- 按分类：`cat-qc-standard` 164、`cat-case-analysis` 35、`cat-extra-knowledge` 13、`cat-qc-process` 4。
- 疑似号码的聊天最大响应体 456–3339 字节（均为真实会话）。

## 五、根因

1. **助手上报**：插件 0.5.8 用「当前页面号码」当请求身份（`conversationId` 与 `workOrderId` 同值，`conversationIdKind` 本应标出类型，见
   `docs/qa-web-assistant-work-order-id-spec.md:1.3`）。当助手页面处于会话上下文时，它把**会话号**填进了 `workOrderId`。
2. **知识库原样落库**：`backend/app/routes/integration.py:1196`、`:1833` 直接把 payload 写进 `retrieval_quality_events.question_form_id`，
   只做格式归一（`backend/app/schemas/integration.py:26-39`：纯数字 ≤64 位，脏值置空），**不做存在性校验**；`conversationIdKind` 后端完全没有接收（grep 全仓只有 `workOrderId` 别名）。
3. **池子按「有工单号就建单」物化**：`backend/app/services/blind_labeling.py:259-347 ensure_work_orders()` 只要求
   `question_form_id IS NOT NULL` + `source_kind IN ('reply','combined')` + `request_status IN ('success','fallback')` + 快照非空，
   于是假的工单号也会生成盲标工单、被指派给标注员。

结果：全库 43,546 条带 `question_form_id` 的事件 **100% 等于同一条记录的 `conversation_id`**，从数据上无法事后区分身份类型。

## 六、建议（按优先级）

> 处置进展（2026-10-10 晚）：第 1、2 条**已实现**（分支见 PR，实现说明见
> `docs/qa-web-assistant-work-order-id-spec.md` 第 2.7 节）：新增
> `backend/app/services/work_order_verification.py` + 迁移
> `backend/migrations/versions/20261010_01_work_order_identity.py`
> （`revision = 20261010_01_work_order_identity`；
> `retrieval_quality_events.conversation_id_kind` / `work_order_verified`），
> `ensure_work_orders()` 建单前复核上游工单详情。第 3 条（存量清理）与第 4 条（助手侧）
> **仍未做**，且第 1 条在线上默认休眠（需要曼哈顿 Cookie 才生效）。
>
> 上线（2026-10-10）：生产已应用迁移 `20261010_01_work_order_identity`，后端镜像
> `knowledge-kb-backend:work-order-verification-20261010b` 已切换（回滚 tag
> `rollback-before-181-20261010`）；上线后只读演练确认门禁在无 Cookie 时休眠、
> 不发上游请求（`WorkOrderVerifier.enabled = False`、`stats.checks = 0`）。
>
> Cookie 持久化（2026-10-10 补充）：门禁只差 Cookie 即可生效，而运行时 Cookie 原先
> 只存在于进程内存（容器重启即丢）。现在界面「知识工作区 → 输入 `mht` → 更新曼哈顿数据
> → 粘贴后台 Cookie → 验证并保存」会把 Cookie 以 `0600` 写入
> `/app/data/manhattan_cookie.json`（数据卷 `knowledge-kb_manhattan_cache`），
> 读取优先级为运行时 → 持久化文件 → `NMHT_COOKIE`；`POST`/`DELETE /manhattan/session`
> 收紧为需要 `account:manage`。粘贴一次后门禁即在后续重启与部署中持续生效。

1. **止血（代码）**：`ensure_work_orders` 建单前增加「工单详情查得到」的门禁——可用已落库的 `conversationIdKind`（需先让后端接收并入库，成本最低）
   或调用上游 `queryQuestionFormDetail` 校验（需要 Cookie，成本高）。
2. **身份标记**：后端接收并持久化 `conversationIdKind`（`workorder` / `conversation`），落库到 `retrieval_quality_events`，
   并在 `/model-connection` 同级的治理界面暴露统计，便于日常巡检。
3. **存量清理**：216 条疑似工单（63 条已有标注数据）。10-08 那次清理的口径与本报告一致（590 → 真 167 / 假 423，先导出 JSON 备份再删除，
   连带 475 条指派、319 条标注）。**建议先备份 + 只清理「已验证查不到」的 57 条，129 条仅有旁证的先观察**（或用 Cookie 逐号复核后再动）。
4. **助手侧**：按 `docs/qa-web-assistant-work-order-id-spec.md` 第 3 节补齐 `orderInfo.workOrderId` 为空时不得用会话号顶替。

## 七、产物

- 疑似清单（216 行，Excel 可直接打开）：`docs/blind-label-suspect-work-orders-20261010.csv`
- 可复现脚本（入库，只读）：
  - `scripts/audit/dump_blind_label_pool_ids.py`（在 kb-backend 容器内导出全池号码 + 指派/标注计数 → `/tmp/pool-ids.json`）
  - `scripts/audit/scan_nmht_access_log_for_work_orders.py`（在宿主机单遍扫 nginx 日志 → `/tmp/pool-verdict.json`、`/tmp/pool-verdict.tsv`）
- 原始判定数据：服务器 `/tmp/pool-verdict.json`（923 KB）、`/tmp/pool-verdict.tsv`（391 KB）、`/tmp/susp-impact.json`；
  本机快照（未入库）`outputs/blind-label-work-order-audit-20261010/pool-verdict.{json,tsv}`

> 说明：核查期间盲标池仍在增长（同一小时里从 1,158 涨到 1,292，标注从 3,528 涨到 3,548），
> 本报告数字是 2026-10-10 14:38 的快照。
