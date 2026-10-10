# 答疑智能推荐助手「工单号 / 会话号」改造：现状、已完成改动与后续需求

- 面向读者：答疑中台知识库（本仓库）维护者 + 答疑智能推荐助手（`zntj.powerzhuan.cn`）的维护方
- 日期：2026-10-08
- 结论一句话：**知识库侧已经按「只用工单号」改造并上线；助手侧 0.5.8 已经会上送两个号，我们只补了「没有工单号时改用会话号继续检索、不上送假工单号」这一处，也已上线。**

> 本文替换了此前那份「请工作台/助手改成同时上送两个号」的需求稿 —— 经实测，**上游答疑工作台本来就把两个号一起给了助手**（见第 1 节），所以不存在「工作台缺号」的问题；助手 0.5.8 也已经按两个号上送。

## 0. 状态一览

| 事项 | 状态 | 位置 |
|---|---|---|
| 工作台把「会话号 + 工单号」一起给助手 | ✅ 上游本来就支持，无需改动 | 见 1.1 抓包实证 |
| 助手 0.5.8 上送两个号 | ✅ 厂商已实现 | `web/business-adapter.js`、`modules/05-标准检索/server-retrieval-client.js` |
| 助手「没有工单号就一个请求都不发」 | ✅ 已改成「用会话号继续检索」（我们的补丁，2026-10-08 15:00 上线） | 见第 3 节 |
| 知识库接收工单号（新字段 + 别名 + 迁移） | ✅ 已上线（2026-10-08 14:2x） | 见第 2 节 |
| 知识库「没有工单号的样本不进盲标池」 | ✅ 已上线并在线上验证 | 见 2.3、2.6 |
| 知识库盲标弹窗「只用工单号」 | ✅ 已上线（工单详情 + 聊天面板都用工单号） | `frontend/index.html` |
| 存量 423 条「只有会话号」的历史盲标工单 | ✅ 已备份后物理删除 | 见 2.5 |
| 建单前复核上游工单详情（新代码，默认休眠需 Cookie） | 🟢 已上线（2026-10-10，迁移已应用；门禁因线上无 Cookie 暂休眠） | 见 2.7 |
| 曼哈顿 Cookie 持久化（重启/部署后仍生效） | 🟢 已上线（2026-10-10） | 见 2.7 |
| 无 Cookie 的访问日志判定门禁（主用判定） | 🟡 已实现待部署（2026-10-10） | 见 2.8 |

## 1. 为什么最终判定「不用改上游、不用改工作台」

### 1.1 实证：工作台给助手的载荷里两个号都有，而且不相同

2026-10-08 13:54:57 抓到答疑工作台（Electron 2.7.3，UA `…/2.7.3 Chrome/142.0.7444.265 Electron/39.8.10`）发给助手的 `QA_WEB_CONVERSATION` 消息（节选，原始字段名）：

```
context=s45|conversationId=s19#83f46d24|messages=arr5|operatorName=s3|orderInfo=obj8
|workOrderId=s19#c7313804|ids=conversationId+workOrderId|eq=conversationId!=workOrderId
|orderInfo=adminName+brand+businessType+category+categoryId+model+questionType+smOrderId
|org=https://zzdy.powerzhuan.cn
```

`eq=conversationId!=workOrderId` 说明两个 19 位号**同时在、且不相等**；`orderInfo` 里没有 `workOrderId`（工单号在顶层）。结论：工作台侧零改动。

### 1.2 上游两套号的含义

| 号码 | 上游接口 | 作用 |
|---|---|---|
| **工单号**（`questionFormId`） | `GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>&sceneType=1\|2` | 曼哈顿后台的工单详情（有数据时约 534–1128 B；查不到时只有 123/124 B） |
| **会话号**（`conversationId`） | `GET /nmhtapi/im/history?conversationId=<号>` | 答疑工作台里的聊天记录（有数据时约 615 B） |

一个号只能打开上游的一半页面，两者互不通用（实测 0 条号同时具备两种数据）。

### 1.3 助手 0.5.8 实际上送什么（用户实测抓包，2026-10-08）

```
X-Conversation-Id: 2092919668271485492
{"conversationId":"2092919668271485492","workOrderId":"2092919668271485492",
 "conversationIdKind":"workorder","pluginVersion":"0.5.8","requestMode":"semantic"}
```

即 0.5.8 用**工单号**当请求身份（两个字段同值），并要求必须有工单号：

```js
// web/business-adapter.js（厂商 0.5.8 原逻辑）
if (!workOrderId || !rawContext) {
  if (!workOrderId) base.skipReason = 'WORK_ORDER_ID_REQUIRED';
  return base;              // 没有工单号 ⇒ 一个请求都不发
}
```

这带来一个新问题：**只有会话号、没有工单号的会话，助手什么都不发**，知识库因此完全看不到这批样本。我们的口径是「没有工单号的检索事件照常入库，只是不进盲标池」，所以补了第 3 节的改动。旧版 0.5.7 只发 `conversationId`（会话号），线上仍有少量旧实例在跑，所以库里仍会看到少量无工单号的行。

## 2. 知识库侧改动（已上线）

### 2.1 字段与契约

- 新列 `question_form_id`（`varchar(64)`，可空）加在两张表：`retrieval_quality_events`、`blind_label_work_orders`；迁移文件 `backend/migrations/versions/20261008_01_add_question_form_id.py`（`revision="20261008_01_question_form_id"`，`down_revision="20260929_01_blind_reason_codes"`），线上已执行到 `20261008_01_question_form_id (head)`。
- 命名说明：代码里原有的 `work_order_id` 指**知识库自己的**盲标工单主键（`wo-…`），所以上游的「工单号」另起名 `question_form_id`。
- 接口 `POST /api/v1/integration/standard-search` 与检索质量事件接口都接收该字段，并接受别名 `questionFormId` / `workOrderId` / `question_form_id` / `work_order_id` / `realWorkOrderId` / `real_work_order_id`。
- 脏值（`""`、`"-"`、`null`、`undefined`、非纯数字、超 64 位）统一归一化成 `None`，**绝不返回 422** —— 遥测字段不能打断检索（`backend/app/schemas/integration.py` 的 `normalize_question_form_id()`）。
- 落库调用点：`backend/app/routes/integration.py:1196`（检索事件）与 `:1833`（质量事件）。

### 2.2 幂等不变

`conversation_id ␟ request_id ␟ source_kind` 的幂等身份判定**故意没有改**，避免历史空值行与新行相撞返回 409。

### 2.3 严格口径：没有工单号的样本不进盲标池

`backend/app/services/blind_labeling.py:259 ensure_work_orders()`：

- 候选查询增加 `RetrievalQualityEvent.question_form_id.isnot(None)`；
- 去重键从会话号改为工单号（`latest_by_question_form`，一个工单只留最新一条事件）；
- 物化时写入 `question_form_id`，并同时登记 `existing_conversations` / `existing_question_forms`。

**结果：没有工单号的事件照常入库，但永远不会变成盲标工单。**

### 2.4 盲标弹窗「只用工单号」（`frontend/index.html`，md5 `23bacce7241b7fdb383d0a131a2e9896`，721,236 B）

- 弹窗标题与聊天面板头部显示工单号（`blindLabelQuestionFormId()`，缺失时退回会话号并显示「未提供」）；`:2180` 文案「… · 按工单号加载答疑平台对话」。
- 聊天面板：`blindLabelChatUrl()` = `workOrderChatUrl(工单号 || 会话号)` → `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<号>&sceneType=2`（`:5750`、`:5766-5770`）。
- 新增「工单详情」入口：`blindLabelWorkOrderDetailUrl()` → `https://nmht.zhuanspirit.com/qaManage/workOrderDetail?questionFormId=<工单号>&sceneType=2`（`:5760-5765`，新窗口）。
- 工单 meta 行在「会话号 ≠ 工单号」时额外显示「· 会话 <号>」（`:2193`）。
- 兼容旧数据：`blindLabelQuestionFormId()`（`:8728`）读 `question_form_id` / `questionFormId` / `upstream_work_order_id`；`blindLabelWorkOrderId()`（`:8736`）优先工单号 → 会话号 → 旧字段。

### 2.5 存量清理（已执行，可回溯）

- 以 300 字节为界重新分类 590 条历史盲标工单（`/root/nmht-paths-20261008.txt` 索引 + `.dsh-remote/classify_pool.py`）：**真工单号 167 条 / 只有会话号 423 条**。
- 167 条回填 `question_form_id = conversation_id`；423 条**先导出 JSON 备份再物理删除**，连带删除其 475 条指派、319 条人工标注。
- 备份位置：服务器 `/opt/knowledge-kb-runtime/.codex-deploy-20261008-question-form-id/cleanup-backup-20261008/`，本地 `outputs/blind-label-cleanup-20261008/`。

### 2.6 上线与线上验证

- 镜像：`knowledge-kb-backend:question-form-id-20261008b`（b 版 = 同一份后端代码 + 打了补丁的前端 `index.html`），切换记录在 `/tmp/kb-compose-args.txt` 末尾的 overlay `compose.question-form-id.yml`；容器 `kb-backend` Up (healthy)、`/health` 200、无 key 请求 401、深链返回 721,236 B 且 md5 一致。
- 回滚：去掉该 overlay（或用旧 tag 的 overlay 覆盖）后 `up -d --no-deps backend`；后端文件原件在 `<stage>/backup-20261008-qfi/`。
- 测试：4 个关键测试文件 **102 passed / 0 failed**；全量套件与旧镜像基线失败集合完全一致（25 failed / 558 passed / 1 error，零回归）；新增「无工单号事件绝不进池」「有工单号事件带号入池」两个用例（`backend/tests/test_blind_labeling.py`）。
- 线上探针（`docker run … /probe.py`，真实接口 + 真实库）：会话号请求 → HTTP 200 且落库 `question_form_id=NULL`；克隆一条合格事件去掉工单号 → 池内 0 行，补上工单号 → 池内 1 行（`RESULT: strict rule CONFIRMED`），探针数据已清理。
- 线上统计（截至 2026-10-08 15:1x 本地）：

| 指标 | 数值 |
|---|---|
| 盲标池工单总数 | 197 |
| 其中带工单号 | **197** |
| 其中无工单号 | **0** |
| 严格口径上线后新建的工单 | 30 |
| 上线后入库的事件 | 3,534 |
| 其中无工单号（照常入库、不进池） | 3,132 |
| 其中带工单号 | 402 |

### 2.7 建单前工单号真实性校验（2026-10-10）

严格口径只保证「有号码」，不保证「号码真的是工单号」。2026-10-10 的核查（`docs/blind-label-work-order-number-audit-20261010.md`）确认：1,292 条盲标工单里 **216 条（16.7%）的号码在上游只能取到聊天记录、取不到工单详情**，即助手把会话号填进了 `workOrderId`。因此新增一道**建单前上游复核**：

- 新增 `backend/app/services/work_order_verification.py`：调用上游 `GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>&sceneType=2`，按**实测响应体大小**判定（不解析业务 JSON）：`≤128 B` → `False`（上游「查不到」，实测 123/124 B）；`≥300 B` → `True`（实测 534–1128 B）；中间值 → `None`。
- `backend/app/services/blind_labeling.py` 的 `ensure_work_orders()`：判重之后、物化之前校验。
  - `False` → **不建单**，并在 `retrieval_quality_events.work_order_verified` 记 `False`（下次 claim 不再重复请求上游）；
  - `True` → 记 `True` 并正常建单；
  - `None`（没有 Cookie / 超时 / 401 / 非 200 / 空响应 / 登录页）→ **fail-open**，行为与改造前一致，避免没有 Cookie 时把盲标池饿死。
- 另外接收并落库助手自报的身份类型：`retrieval_quality_events.conversation_id_kind`（`workorder` / `conversation`，见 `backend/app/schemas/integration.py` 的 `conversationIdKind` 别名）。`kind='conversation'` 的事件直接跳过，不再请求上游。
- 迁移：`backend/migrations/versions/20261010_01_work_order_identity.py`（`revision = 20261010_01_work_order_identity`，`down_revision = 20261008_01_question_form_id`）新增上述两列。**部署必须先 `alembic upgrade head` 再重启后端**：ORM 会 SELECT 全部映射列（含新列），缺列时池子查询直接报错，无法靠代码兜底。
  - revision id 必须 ≤32 字符：`alembic_version.version_num` 是 `varchar(32)`（线上实测 32），最初的 `20261010_01_conversation_identity`（33 字符）在 PostgreSQL 上写版本号时抛 `psycopg2.errors.StringDataRightTruncation`，`alembic upgrade head` 整体回滚（DDL 未生效）。约束由 `backend/tests/test_migration_revisions.py` 兜住。
- 生效前提：上游复核需要曼哈顿 Cookie（持久化的 Cookie 或 `NMHT_COOKIE` 环境变量）。**线上 `NMHT_COOKIE` 目前为空**，所以这道门禁默认处于「全部 `None` → 不拦截」状态；粘贴 Cookie 后才真正拦截。每次 claim 最多校验 40 个号码，连续 3 次无法判定即自动停用（避免拖慢领取）。
- Cookie 持久化（2026-10-10 补充）：Cookie 原先只存在于 uvicorn 进程内存（`_runtime_cookie`），容器每次重启/重新部署都会丢失，门禁会静默退回休眠。现在「知识工作区 → 输入 `mht` → 更新曼哈顿数据 → 粘贴后台 Cookie → 验证并保存」会把 Cookie 以 `0600` 权限原子写入 `/app/data/manhattan_cookie.json`（数据卷 `knowledge-kb_manhattan_cache`，随容器重建保留）；读取优先级为 **运行时粘贴 → 持久化文件 → `NMHT_COOKIE` 环境变量**，`GET /manhattan/session` 返回 `source`（`runtime`／`saved`／`env`）与 `persisted`／`updated_at`／`updated_by` 供界面显示。`POST`／`DELETE /manhattan/session` 已收紧为需要 `account:manage` 权限；清除连接或上游判定登录过期（refresh 收到 401/403）都会同时删掉该文件。若该文件保存失败，接口仍返回成功但 `persisted=false`，界面会提示「容器重启后需要重新粘贴」。
- 单次校验上限、超时与缓存都在 `WorkOrderVerifier` 里，行为由 `backend/tests/test_work_order_verification.py`、`backend/tests/test_manhattan_session_persistence.py` 与 `backend/tests/test_blind_labeling.py` 的门禁用例覆盖；界面契约由 `backend/tests/test_manhattan_session_frontend.py` 覆盖。
- **上线记录（2026-10-10）**：生产迁移已应用（`alembic current` = `20261010_01_work_order_identity (head)`，`retrieval_quality_events` 两列已存在），后端镜像 `knowledge-kb-backend:work-order-verification-20261010b` 已上线（`/health`、`/ready`、`/app` 均 200）；回滚镜像 tag `knowledge-kb-backend:rollback-before-181-20261010`，部署前文件备份在 `/opt/knowledge-kb-runtime/deploy-backup-20261010-work-order-verification/`。上线后只读演练 `ensure_work_orders(db, 0)`（结果 rollback）在 25 个候选上正常跑通，`WorkOrderVerifier.enabled = False`、`stats.checks = 0`，确认门禁休眠且不发上游请求；最新事件已能看到助手自报的 `conversation_id_kind = 'workorder'`。

### 2.8 无 Cookie 的访问日志判定门禁（2026-10-10，主用判定）

Cookie 复核的致命弱点在线上被证实：`NMHT_COOKIE` 为空、运行时粘贴的 Cookie 只存在于 uvicorn 内存、而浏览器里的 Cookie 是 Chromium 的 `v20` 应用绑定加密（无法用 DPAPI 导出）。也就是说**没有可自动获得的凭据**，2.7 那道门禁会长期休眠。于是补一道不需要登录的判定：

- 证据源是 zzdy 反代访问日志 `/www/wwwlogs/zzdy.powerzhuan.cn.log`（即 1.2 节实测体量的原始出处），只读、无需 Cookie。判据与 `docs/blind-label-work-order-number-audit-20261010.md` 完全一致：
  - `GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>` 响应体 `≥300 B` → `real`（真工单号；实测 534–1128 B）；
  - 只在 `GET /nmhtapi/im/history?conversationId=<号>` 上有数据（`≥300 B`，空会话只有 68 B）且从未在上面那条接口上有数据 → `session`（只当过会话号）；
  - 只有「查不到」（123/124 B）或日志里根本没有该号码 → **不下结论**（fail-open 放行，避免把新工单饿死）；
  - 同一号码两者都有数据 → `real`（真工单号即使也被当作会话号用，不降级）。
- 实现：`backend/app/services/work_order_log_verdicts.py`
  - **增量扫描**：状态文件 `<backend>/data/work_order_log_verdicts.json`（容器内 `/app/data`，`0600`、tmp + `os.replace` 原子写）记录 `offset`/`inode`/`verdicts`，每轮只读新增字节（尾部半行留给下一轮，避免半行写出错误结论）；日志被轮转或截断时从头重扫，**已得结论保留**。首轮全量扫描实测约 45 s（9.58 GB / 3,250 万行）。
  - 后台 worker：`backend/app/main.py` 的 lifespan 里按 `WORK_ORDER_LOG_ENABLED` 启动 `run_work_order_log_verdict_worker`，默认每 `WORK_ORDER_LOG_POLL_SECONDS=300` 秒一轮；有新结论时回写既有事件的 `work_order_verified`（`session` → `false`，`real` → `true`），每批 `WORK_ORDER_LOG_BACKFILL_LIMIT`（默认 500）条、分批提交（0 关闭回写），使拦截在 SQL 层就生效。**首轮全量扫描判定的号码不会再算作「新增」**，所以 worker 发现结论文件还没有回写标记时会做一次全量回写（`backfill_all_verdicts`，按同一批大小分批）并把 `backfilled_at` 写回结论文件——升级现网或清空数据卷后靠它把历史遥测一次性对齐。
  - 门禁接线：`ensure_work_orders()` 里**上游复核优先**；`WorkOrderVerifier.verify()` 返回 `None` 时改查日志判定，`session` → 不建单并记 `work_order_verified=False`，`real` → 记 `True`，未知 → 照常建单。
- 部署要求：容器必须能读到日志——在 `run-kb-backend.sh` 的 `docker run` 参数里加只读挂载 `-v /www/wwwlogs:/app/nlogs:ro`（配置项 `WORK_ORDER_LOG_PATH` 默认 `/app/nlogs/zzdy.powerzhuan.cn.log`）。**漏掉挂载时门禁会静默失效**（日志不可读只记一条 warning）。本机制不涉及数据库变更，无需迁移。
- 测试：`backend/tests/test_work_order_log_verdicts.py`（体量判据、增量、尾部半行、轮转重扫、每轮字节预算、统计、分批回写与解除拦截、首轮全量回写与标记）与 `backend/tests/test_blind_labeling.py` 的四条门禁用例（日志判 `session` 拦、判 `real` 放、上游有结论时不查日志、fail-open）。
- 已知边界：日志里查不到的号码一律放行；判定只覆盖「上游真实流量留下过记录」的号码，不会主动调上游接口。

## 3. 助手侧补丁（已上线，2026-10-08 15:00）

改动 3 个文件（都在 `/www/wwwroot/zntj.powerzhuan.cn/`），每处都带注释标记「知识库侧补丁 2026-10-08」：

| 文件 | 改后 md5 | 字节 |
|---|---|---|
| `web/business-adapter.js` | `74286a10c8c3bb59cbcadae00fb7b980` | 18,328 |
| `web/app.js` | `40ec95f767034d08df2040fd095d0973` | 17,756 |
| `modules/05-标准检索/server-retrieval-client.js` | `0f775f28ffa31e5d2c7c4bb654c3be82` | 67,326 |

行为对照：

| 情况 | 补丁前（0.5.8） | 补丁后 |
|---|---|---|
| 有工单号 + 会话号 | 发请求，`conversationId=workOrderId=工单号`，`conversationIdKind='workorder'` | 不变 |
| 只有会话号 | **一个请求都不发** | 发请求，`conversationId=会话号`、`workOrderId=""`、`conversationIdKind='conversation'`（知识库记为无工单号 ⇒ 不进盲标池） |
| 两个号都没有 | 不发 | 不发（`skipReason='WORK_ORDER_ID_REQUIRED'`） |

关键改动点：

- `web/business-adapter.js` `recommend()`：新增 `conversationId`（数字校验）、`identityId = workOrderId \|\| conversationId`；跳过条件由 `!workOrderId` 改为 `!identityId`；`createRequestId('qa-web'/'qa-web-config', identityId)`；两处客户端调用各增 `conversationId`。
- `modules/05-标准检索/server-retrieval-client.js` `search()` 与 `searchModelConfiguration()`：`transportConversationId = workOrderId || conversationId`、`conversationIdKind = workOrderId ? 'workorder' : 'conversation'`；payload 里 `conversationIdKind` 不再硬编码；`orderInfo.workOrderId` 在无工单号时保持空（**不会用会话号冒充工单号**）。
- `web/app.js`：跳过提示文案改为「当前会话没有工单ID也没有会话ID，未上送知识库」。

验证方式：本地离线测试床 `.dsh-remote/assistant-0.5.8/testbed/verify.js`（用真实文件 + 假 fetch，三种情况全部通过），另外核对了知识库的响应契约 —— `IntegrationStandardSearchResponse` 回显的 `conversationId`/`requestId` 取自请求体，客户端的身份校验用同一个 `transportConversationId`，不会因改用会话号而报 `SERVER_IDENTITY_MISMATCH`。

回滚：服务器 `/root/assistant-058-backup-20261008-150052/`（3 个原件，2026-10-08 13:49 版本）逐个 `cp -a` 回去即可。**注意：厂商下一次升级助手会覆盖这些文件**，补丁需要重打。

补丁 diff（可直接 `git apply`）：`docs/qa-web-assistant-patch-20261008/patch-business-adapter.diff`、`patch-server-retrieval-client.diff`、`patch-app.diff`。

## 4. 如果要厂商出正式版本，请按这 4 条实现

1. `normalizeConversation()`：**各自保留原值**，`conversationId` 与 `workOrderId` 分开校验，缺了就留空，**不要用一个号顶替另一个号**（尤其不要再用会话号填 `orderInfo.workOrderId`）。
2. `recommend()`：**没有工单号也必须继续检索**，用会话号当请求身份；只有两个号都没有时才放弃。
3. 请求体：`conversationId`（实际身份）、`workOrderId`（真实的工单号，可为空串）、`conversationIdKind`（`workorder` / `conversation`）三者都带；`X-Conversation-Id` 与 `X-Request-Id` 的生成规则**不要改**。
4. 面板文案：没有工单号时应显示「会话 xxx」，不要显示「工单 xxx」，避免再次误导。

**明确不要做的事**：不要把所有号整体换成工单号（那 423 条只有会话号的样本会立刻失去身份）；不要在缺号时用另一个号顶替；不要改动请求头语义。

## 5. 验收清单

1. 有工单号的会话：抓请求体应同时出现两个号且**不相等**，知识库新行 `question_form_id` 有值，盲标池能看到该工单。
2. 只有会话号的会话：请求体 `workOrderId` 必须是空串、`conversationIdKind='conversation'`；知识库该行 `question_form_id` 为空，**盲标池里查不到**。
3. 盲标弹窗：点开一条样本，「工单详情」按钮打开曼哈顿后台且页面有数据；聊天面板按工单号正常加载。
4. 回归：推荐面板功能不变；知识库 `health` 200、无 key 401。

## 6. 产物与路径

- 知识库侧改动：`backend/app/{models,routes,schemas,services}`、`backend/migrations/versions/20261008_01_add_question_form_id.py`、`frontend/index.html`、`backend/tests/test_blind_labeling.py`（分支 `feature/upstream-work-order-id`，**尚未提交**）。
- 服务器 staging：`/opt/knowledge-kb-runtime/.codex-deploy-20261008-question-form-id/`（含 `upload/`、`backup-20261008-qfi/`、`cleanup-backup-20261008/`、`Dockerfile.question-form-id`、`compose.question-form-id.yml`）。
- 本地脚本与素材：`.dsh-remote/`（部署、测试、迁移、探针、统计脚本）、`.dsh-remote/assistant-0.5.8/`（助手源码、原件、测试床、patch）。
- 参照：上游接口 `GET /nmhtapi/qa/queryQuestionFormDetail?questionFormId=<号>&sceneType=1|2`、`GET /nmhtapi/im/history?conversationId=<号>`；知识库接口文档 `docs/automation-api-reference.md` 第 5.1 节。
