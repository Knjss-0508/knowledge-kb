# Answer Hub Workflow

## 主题级工作流（当前）

当前按固定12品类配置处理手机、平板电脑、智能手表、耳机/耳麦、笔记本、游戏机、游戏卡带、单电/微单机身、单反机身、相机镜头、手写笔和学习机；单条高质量原子问题也可以形成待审核主题。`适用范围`只能输出这12个正式名称，品牌和机型使用独立字段：

聚类硬边界采用 `回收业务层级 -> 产品品类 -> 业务主题`。现有质检规则属于“自营回收”；
“聚合回收”已预留为并列业务层级，但产品品类和专属质检口径尚未配置，因此当前聚合
回收记录只做层级隔离并强制人工优先审核。自营回收与聚合回收绝对不能聚类到一起。

```text
第二部分服务器数据
→ CZ 接口幂等接收
→ 原子问题拆分
→ 纯大模型 1～N 聚类
→ 高置信聚类准入；低置信和风险聚类转人工复核
→ 与同业务层级、同品类历史主题增量归并并追加原工单和证据
→ 标注主题问题分类（质检标准、质检流程、案例解析、课外常识、不确定）和是否值得沉淀
→ 仅对值得沉淀主题进行知识转写
→ 模型初标转写内容质量
→ 同步答疑中台候选价值复核
→ 人工复核后批量送审至知识库管理
→ Qwen3查重拦截、终审与发布
```

- 具备清晰、可复用边界的单成员少见案例、特殊案例或标准咨询也可以进入 `topic_review_queue.xlsx`，但必须人工优先复核；不得为了减少单例强行合并。
- 单主题会话只保留1个原子问题；只有明确多主题会话才拆成2～3个；不确定会话最多保留1个暂定原子问题。
- 完整自动化在聚类后先执行准入门禁。只有`direct_mimo`成功、置信度不低于0.75、
  无原子/聚类风险标记且回收业务层级和产品品类一致的主题才进入知识分类与沉淀价值判断。
- 低置信、规则降级、调用失败、冲突拆分或模型要求复核的主题写入
  `pending_cluster_rows`，不会消耗后续分类、转写和初审模型额度。
- 正式聚类准入通过后启用持久化增量主题库。高置信历史匹配复用原主题ID并幂等追加
  原始工单ID、原子问题和事实证据；模糊历史匹配写入
  `pending_historical_topic_review`。不同回收业务层级、不同品类、同工单不同原子、
  明确适用范围或阈值冲突、以及必须按现象值拆分的质检规则不得自动合并。
- 未启用聚类准入的本地验证不会写入历史主题库。升级前普通`review_pending`候选不会
  自动成为可信历史主题；只回填已发布或明确通过高置信聚类准入的候选。
- 无聊天且无可用图片的记录进入 `evidence_gap_rows`；1～N 个原子问题均可形成待审核主题。
- 聚类后先执行主题问题分类与沉淀价值标注；不值得沉淀主题保留审计和价值复核记录，但不进行正式知识转写。
- 已有稳定知识覆盖且没有新增边界、例外或操作差异的基础常见问题不重复收集。
- Streamlit 工作台只用于各板块准确性验证；正式候选在答疑中台“候选价值复核”中编辑和复核。
- 只有模型或组员确认具备复用价值、值得沉淀的知识点才能进入批量送审；纯个案结论和无复用价值内容留在例外队列。
- 验证准确率和自动放行精确率达到配置门槛后，可绑定具体模型与 Prompt 版本，让模型自动标注替代第三部分逐条人工复标；低置信度和风险候选仍进入人工例外队列。
- 人工复核完成后，在答疑中台点击“批量送审至知识库管理”；成功项只进入知识库待审核队列。
- 旧调用方仍可使用`/api/v1/integration/second-part/records:batch`；当前推荐由
  Answer Hub 自动化完成处理后调用
  `/api/v1/integration/knowledge-review-candidates:batch`同步候选。
- 当前批量链路不读取、不检索、不主动生成质检标准关联；`关联标准项`字段始终保留，新候选默认为空，已有值不覆盖。
- Qwen3 Embedding 对批量导入执行重复拦截；自动化批量场景还必须具备标题或正文的有效文本重合证据，避免同品类、同回复模板的不同知识被整批误拦截。
- 服务端会统一检查知识ID、主标题、知识内容、推荐回复、知识分类、适用范围和关键词；已有标准关联或显式标准引用会保留并进入“标准关联搁置”，不通过当前无标准入口送审。
- 批量入库按候选逐条提交事务，单条向量或写库失败不会回滚后续成功项。

完整部署和联调步骤见 [CZ_INTEGRATION_RUNBOOK.md](CZ_INTEGRATION_RUNBOOK.md)。

运行指标、失败恢复、脱敏门禁、保留策略、备份恢复和端到端验收见
[OPERATIONS_RUNBOOK.md](OPERATIONS_RUNBOOK.md)。

## 实验性主题自动标注模型

项目提供一个不调用外部服务的本地实验模型，同时预测：

- `主题问题分类`：质检标准、质检流程、案例解析、课外常识或不确定。
- `是否值得沉淀`：值得沉淀或不值得沉淀。

使用人工复核工作簿训练时，模型读取`人工主题问题分类`、
`人工是否值得沉淀`和`是否进入训练集`。人工标签不足时，也可以显式允许
`topic_stage_predictions.json`中的 MiMo 初标作为伪标签，但这种模型强制进入
人工复核，不具备生产自动放行资格。伪标签训练默认剔除低于 0.72 置信度的结果，
以及除“不确定”外仍被 MiMo 标记为需要人工复核的结果。

# 安全默认：人工复核完成后使用人工标签训练
```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli train-topic-label-model `
  --source ".\outputs\topic-stage-label-current-20260804\最新版聚类结果_主题分类与沉淀价值_人工复核.xlsx" `
  --output-dir ".\outputs\topic-label-model-human-v1"
```

仅复现首个伪标签实验基线时，必须显式允许伪标签。当前数据中安全筛选后的样本无法
覆盖全部标签，因此首个实验还显式纳入了带上游风险标记的伪标签；该开关不得用于
生产模型：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli train-topic-label-model `
  --source ".\outputs\topic-stage-label-current-20260804\topic_stage_predictions.json" `
  --output-dir ".\outputs\topic-label-model-v1" `
  --allow-pseudo-labels `
  --allow-upstream-risk-pseudo-labels
```

批量标注主题 JSON：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli predict-topic-label-model `
  --model-dir ".\outputs\topic-label-model-v1" `
  --source ".\outputs\topic-stage-label-current-20260804\topic_stage_predictions.json" `
  --output ".\outputs\topic-label-model-v1\predictions.json"
```

输出包括：

- `model.npz`：只保存哈希特征权重，不保存原始主题文本或可读词表。
- `metadata.json`：模型版本、标签来源、人工复核门禁和训练指纹。
- `training_report.json`：分层留出评估、标签分布和不平衡风险提示。
- `predictions.json`：主题ID和模型标签，不复制原始主题文本；单条失败不影响后续主题。

该模型不会读取旧一级/二级分类或标准关联字段，也不会生成知识正文。目前不得接入
自动发布、自动送审或生产自动审核；应先完成人工标注，再使用人工真值重新训练和验收。

## 发布前一键验收

仅检查当前源码：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_release.ps1
```

完成全量测试、Compose检查、构建交付包并扫描敏感文件：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_release.ps1 `
  -BuildPackage -Version 20260722-preserve-standard-fields-v12
```

脚本会执行根项目测试、CZ后端测试、Python编译、前端JavaScript语法检查、Docker Compose配置检查、交付包清单和SHA256校验，并在解压后的交付包内再次运行两套测试。任一步失败均返回非零退出码。
也可以直接双击根目录的`发布前验收并打包.cmd`。

This repository implements the third-part knowledge ingestion workflow for the answer hub:

`第二部分数据 -> 数据预处理 -> 无标准案例改写 -> 人工复核 -> Qwen3查重拦截 -> CZ待审核 -> 发布`

## What it does

- Reads the Excel output from the second part.
- Uses complete conversations, historical replies and sanitized case images as primary evidence.
- Builds a topic evidence package before transcription. Each fact keeps its fact ID,
  source record ID, human-corrected core problem, human judgment conclusion,
  conversation excerpt, historical reply and matching case-image links.
- Selects representative boundary, judgment, image and complete-context facts instead
  of truncating every topic to the first five records.
- Aggregates one or more atomic questions into auditable topic candidates.
- Uses the MiMo OpenAI-compatible API; the recommended low-cost default is
  `mimo-v2.5` for text, images and videos.
- Loads the shared embedded terminology dictionary by default on every workflow run,
  including deployed automation and queue runs. The run manifest records its version
  and entry count; a missing or invalid dictionary stops the run.
- Falls back to the deterministic rule candidate when MiMo is not configured or fails; these rows are forced into human review.
- Rule fallback content is organized from source facts as background, judgment object,
  source verification basis, human conclusion and evidence boundary. Missing methods
  or boundaries are reported as gaps instead of being invented.
- Every substantive statement in the draft and recommended reply must be supported by
  the same source fact or a matched real standard. Unsupported thresholds, entities,
  actions, causes, scopes or judgments are written to `主题无来源内容` and forced into
  modification review; a model review cannot override this deterministic gate.
- Case images are selected only from representative facts in the same topic. The
  candidate workbook keeps their source-fact mapping, and CZ receives them as rich
  content image blocks with the fact trace retained in `evidence_excerpt`.
- Saves raw/preprocessed records, image download results, model input/output metadata, candidates, and review feedback in local SQLite.
- Lets cz annotate review decisions and corrections in the same workbook.
- Exports published knowledge rows and feedback events for retraining.

## Input files

### Source workbook
Expected columns from the second-part data:

- 序号
- 上传者
- 分析时间
- 工单ID
- 回收单号
- 聊天内容
- 图片链接
- 核心问题
- 判定结论
- 判定依据
- 产品类型
- 一级分类
- 二级分类
- 参考话术
- 历史实际回复（可选；缺失时兼容读取参考话术）

### Legacy standard-aware mode

旧的标准检索与引用代码仍保留用于历史文件兼容，但当前第二部分批量入口和页面默认不启用。

## Commands

Install in editable mode:

```bash
pip install -e .
```

### Configure the recommended low-cost MiMo model

Copy `.env.example` to `.env`, then fill the API key copied from the MiMo
console. Do not place the key in Excel, browser code, or source code.

```text
MIMO_API_KEY=你的Key
MIMO_API_KEYS=备用Key1,备用Key2,备用Key3
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5
MIMO_MEDIA_MODEL=mimo-v2.5
MIMO_TIMEOUT_SECONDS=60
MIMO_RESPONSE_READ_TIMEOUT_SECONDS=90
MIMO_RESPONSE_TIMEOUT_CIRCUIT_BREAKER_FAILURES=2
MIMO_THINKING_TYPE=disabled
MIMO_MAX_COMPLETION_TOKENS=2048
MIMO_CLUSTER_MEDIA_POLICY=on_demand
MIMO_CLUSTER_MEDIA_MIN_TEXT_CHARS=220
ANSWER_HUB_MIMO_MAX_WORKERS=4
ANSWER_HUB_DIRECT_CLUSTER_MAX_WORKERS=4
ANSWER_HUB_DIRECT_ATOMIC_BATCH_SIZE=4
ANSWER_HUB_DIRECT_ATOMIC_BATCH_MAX_CHARS=16000
ANSWER_HUB_DIRECT_MIMO_BATCH_SIZE=6
ANSWER_HUB_DIRECT_RECONCILE_MODEL_FLOOR=0.68
ANSWER_HUB_DIRECT_RECONCILE_LIMIT=24
ANSWER_HUB_CLUSTER_ADMISSION_MIN_CONFIDENCE=0.75
MIMO_INPUT_COST_PER_MILLION_TOKENS=0.14
MIMO_OUTPUT_COST_PER_MILLION_TOKENS=0.28
ANSWER_HUB_DB_PATH=data/phone_mvp.db
```

`MIMO_API_KEY` 是主 Key，`MIMO_API_KEYS` 是按顺序使用的备用 Key，可用逗号或
分号分隔。当前 Key 出现余额不足、额度耗尽或鉴权失败时会自动停用并切换；普通
接口限流仍按 `MIMO_MAX_RETRIES` 使用当前 Key 重试。日志只记录切换次数，不记录
Key 内容。成本字段单位为美元/百万Token，仅用于本地估算。

`mimo-v2.5-pro`可保留为离线对照或高难度人工重试模型，不再作为每次默认调用。
切换后必须重新验证聚类、图文解析、JSON成功率和人工修改率；自动审核在模型与
Prompt重新验收前保持关闭。

When any of the first three MiMo fields is absent, the pipeline remains usable
but marks all rows as priority human review and writes a rule-based fallback
candidate.

Create the raw audit workbook and the topic review workbook:

```bash
answer-hub ingest \
  --source "D:\\飞书\\共享数据汇总_2026-07-10~2026-07-10.xlsx" \
  --output-dir ".\\outputs"
```

Phone example:

```bash
answer-hub ingest \
  --source "D:\\飞书\\共享数据汇总_2026-07-10~2026-07-10.xlsx" \
  --product-type "手机" \
  --output-dir ".\\outputs\\phone"
```

Use `--rule-only` to validate preprocessing and case-only candidate generation without calling MiMo. Use `--audit-db .\\data\\phone_mvp.db` to override the local audit database path.

The command now writes:

- `review_queue.xlsx`: per-record audit and model trace, not the formal review entry.
- `topic_review_queue.xlsx`: topic-level candidate workbook for local review.
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the 12-field case-only contract.

Finalize locally reviewed topic candidates for submission to the cz knowledge website and optional training:

```bash
answer-hub finalize-topic \
  --review-file ".\\outputs\\phone\\topic_review_queue_reviewed.xlsx" \
  --output-dir ".\\outputs\\phone\\final"
```

This writes `candidate_knowledge_for_submission.xlsx`, `topic_feedback.jsonl`, and `topic_training_samples.jsonl`. The result is still `待审核`; the cz website owns formal approval and publication.

The legacy per-record finalize command remains available for old `review_queue.xlsx` files:

```bash
answer-hub finalize \
  --review-file ".\\outputs\\review_queue.xlsx" \
  --output-dir ".\\outputs"
```

Create a quality report from cz-reviewed rows:

```bash
answer-hub evaluate \
  --review-file ".\\outputs\\review_queue.xlsx" \
  --output-dir ".\\outputs"
```

The command writes `quality_report.json` with standard Top5 hit rate, model-to-reviewer
standard/category agreement, title modification rate, rejection rate, standard coverage
gaps, and priority-review rate.

Install test dependencies and run the suite:

```bash
pip install -e ".[dev]"
pytest -q
```

## Streamlit 主题知识准确性验证平台

安装一次前端依赖：

```cmd
python -m pip install streamlit openpyxl
```

启动本地工作台：

直接双击项目根目录中的：

```text
启动自动化看板.cmd
```

或者在 PowerShell 中执行：

```powershell
Set-Location "C:\Users\admin\Desktop\答疑中台知识库"
.\start_streamlit.ps1
```

启动后访问 `http://localhost:8501`。该平台不直接向知识库发布知识。页面当前包含以下工作区：

1. `运行监管`：查看生产接口、自动化队列和本地验证的持久化运行记录，识别疑似卡住阶段，并记录负责人、处理状态和反馈历史。
2. `自动化看板`：上传脱敏会话，验证输入清洗、语义标注、主题聚类、价值分类、选择性知识转写和内容质量初标；当前默认不读取标准目录。
3. `转人工分析`：执行百晓生与曼哈顿转人工数据分析。
4. `聚类验证`：对边界样本执行聚类判断并收集人工反馈。
5. `完整聚类标注`：完成聚类单元的人工标注与验收。
6. `生成主题候选`：手动运行原有主题候选流程，便于调参与单步验证。
7. `审核与反馈`：验证 `topic_review_queue.xlsx` 的模型结果，下载审核底稿、值得沉淀主题的12项候选和训练反馈样本。

没有聊天内容且没有可用现场图片的记录只进入 `evidence_gap_rows`，不会独立生成主题候选。若电脑无法访问 PyPI，请使用公司镜像或由管理员提供 `streamlit` 的离线 wheel 安装包。

### 自动化命令行入口

工作台和命令行共用同一套自动化编排。后续可由定时任务、上游服务或工作台 API 调用：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m answer_hub.cli automate `
  --source ".\data\共享数据.xlsx" `
  --output-dir ".\outputs\automation-runs"
```

只验证本地规则链路、不调用模型：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m answer_hub.cli automate `
  --source ".\data\共享数据.xlsx" `
  --output-dir ".\outputs\automation-runs" `
  --rule-only `
  --clustering-mode rule
```

使用 MiMo 的自动化入口会先做一次轻量预检。若 MiMo API Key、余额、额度或地址不可用，
流程会停止在“等待人工确认”，不自动生成规则兜底候选。人工确认仍要继续生成待审核结果时，
显式增加：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli automate `
  --source ".\data\共享数据.xlsx" `
  --output-dir ".\outputs\automation-runs" `
  --clustering-mode direct_mimo `
  --continue-on-mimo-unavailable
```

只验证聚类、不生成主题价值、知识转写、推荐回复和内容初审时，增加
`--cluster-only`。该模式只生成一个 `cluster_result.xlsx`，里面只有“聚类结果”工作表：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli automate `
  --source ".\data\质检答疑案例库 (4).xlsx" `
  --output-dir ".\outputs\cluster-only-20260802" `
  --clustering-mode direct_mimo `
  --cluster-only
```

`--cluster-only` 的 `direct_mimo` 默认使用纯文本聚类，不下载、解析或发送图片/视频，
并允许同品类记录进入批量原子提取。只有需要专门对比媒体影响时，才显式增加
`--cluster-media-policy on_demand`；完整自动化流程仍默认读取
`MIMO_CLUSTER_MEDIA_POLICY`，不受此验证策略影响。

`direct_mimo`默认并行执行原子提取和独立聚类批次。无需附媒体的同品类会话默认每
6条合并为一次原子提取请求，并受24000字符预算限制；批量输出异常时自动拆小直至
单条。聊天明确要求查看图中、截图、照片或视频细节时仍保留媒体输入，不进入文本
批量。二次归并先使用本地十品类质检口径，只有相似度达到配置门槛时才调用模型，
并把成功裁决写入断点缓存。不同产品品类始终由程序硬隔离，不能通过批量、提高并发、
降低门槛或复用缓存而跨品类合并。

每次运行会生成独立目录和 `automation_run.json`，持续记录阶段状态、细分子阶段、最近活动、指标、错误和产物路径。自动化只生成待审核知识；人工确认后可提交 CZ，但不会自动发布。

Run the local validation page:

```bash
set PYTHONPATH=src
python -m answer_hub.web
```

Open `http://127.0.0.1:8765`. The page accepts the second-part workbook, calls MiMo only on the local server, previews the candidate queue and downloads the review workbook. The API key is never exposed to the browser.

## 百晓生转人工分析

Streamlit 工作台新增“转人工分析”，支持曼哈顿与百晓生数据导入、工单关联、
周度分层抽样、召回与工具能力分析、低置信度人工复核和八张工作表的周报导出。
诊断标签统一写入“备注”，不单独增加标签列。

详细配置、接口勘探和命令行用法见 `TRANSFER_ANALYSIS.md`。

If PyPI is unavailable and Flask cannot be installed, run the bundled Codex Python instead. The web entrypoint automatically falls back to a standard-library local server:

```cmd
set PYTHONPATH=src
"C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m answer_hub.web
```

## Phone MVP processing rules

1. Clean source fields and normalize duplicate image links. Rows without usable
   conversation evidence and without usable images enter `evidence_gap_rows`;
   legacy core question, conclusion, basis and category remain weak reference
   fields and are not hard admission gates.
2. Filter to `产品类型=手机` only when the caller explicitly selects the phone
   compatibility flow.
3. Use the complete conversation, historical actual reply and sanitized case image as primary evidence.
4. Download at most four public JPEG/PNG/WebP images, each at most 5MB.
5. Send case evidence to MiMo. The response must not cite or invent quality standards.
6. If image download fails, MiMo fails, or the evidence is uncertain, keep a process-style candidate and set `是否重点复核=是`.
7. Aggregate evidence-qualified records by topic before review. Only `通过` / `修改后通过` outcomes are exported as 12-field candidates.
8. CZ uses Qwen3 Embedding for final duplicate interception before creating a `review` item.

## Output files

- `review_queue.xlsx`: source rows plus model labels and the legacy per-record review fields.
- `topic_review_queue.xlsx`: topic-level review queue, source mapping and evidence gaps.
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the 12-field case-only contract.
- `candidate_knowledge_for_submission.xlsx`: locally approved candidates ready to submit to the cz website.
- `topic_feedback.jsonl`: topic-level reviewer feedback.
- `topic_training_samples.jsonl`: approved reviewer-corrected examples selected for future training.
- `review_queue.xlsx` also includes a `preprocessed_queue` sheet so you can inspect the data cleaning stage before model labeling.
- `review_queue.xlsx` includes `excluded_rows` when `--product-type` is used, so other categories cannot enter the phone candidate queue by mistake.
- `published_knowledge.xlsx`: approved records ready for the knowledge base.
- `published_knowledge.jsonl`: machine-readable published records.
- `feedback_events.jsonl`: model-versus-human correction log.
- `data/phone_mvp.db`: local audit database. It contains the raw/preprocessed records and image metadata, retrieved standards plus sanitized model request/response, candidate JSON, final human result and feedback event. It never stores API keys or base64 image bodies.
- `summary.json`: counts and review statistics.

## Workflow state values

- `raw`
- `preprocessed`
- `model_labeled`
- `review_pending`
- `review_approved`
- `review_rejected`
- `published`
- `deprecated`

## 无人值守自动化队列

现有自动化看板适合人工上传并立即运行；无人值守模式使用四态文件队列：

```text
data/automation-queue/
├─ pending/      # 放入待处理的 .xlsx 或 .xlsm
├─ processing/   # 运行时自动认领
├─ completed/    # 流程完成，审核结果和驰卓提交结果已留档
├─ failed/       # 处理或驰卓接口失败，等待检查或重试
└─ logs/         # 每批运行摘要
```

### 第二部分主动推送 JSON（联调推荐）

如果第二部分能够主动调用 Answer Hub，而不是由 Answer Hub 定时拉取，可使用：

```text
POST /api/v1/automation/second-part/records:batch
```

该接口只接收已脱敏记录；接收成功后会生成标准 Excel 快照并进入同一持久化队列，
后续仍按“清洗 → 聚类 → 主题价值判断 → 选择性转写 → 内容初审 → CZ 候选价值复核”执行。
它不会直接创建知识、送审或发布。

请求样例见 `examples\answer_hub_second_part_push.example.json`。每次请求都必须提供稳定的
`idempotency_key`；相同数据重试会复用原任务，若同一键对应的数据内容不同则返回 `409`。
每条 `items` 记录均需包含 `redaction_status: "redacted"` 和 `record`，单次最多 100 条。

本机完整联调步骤如下（只在已配置本机 MiMo 和 CZ 后执行）：

```powershell
# 终端一：启动接收接口
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\start_automation_api.ps1
```

```powershell
# 终端二：把本机 API Key 通过环境变量提供；不要写入 JSON 样例或日志。
if (-not $env:ANSWER_HUB_API_KEY) {
  throw "请先在当前终端安全设置 ANSWER_HUB_API_KEY。"
}
$headers = @{ "X-Answer-Hub-Key" = $env:ANSWER_HUB_API_KEY }
$body = Get-Content -Raw -Encoding UTF8 `
  .\examples\answer_hub_second_part_push.example.json
$job = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8780/api/v1/automation/second-part/records:batch" `
  -Headers $headers `
  -ContentType "application/json; charset=utf-8" `
  -Body $body
$job

# 未安装计划任务时，手动处理刚刚入队的数据。
.\.venv\Scripts\python.exe -m answer_hub.cli automation-queue `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs

# 查看任务结果；CZ 同步成功时 summary.cz_candidate_sync.failed 应为 0。
Invoke-RestMethod `
  -Uri $job.status_url `
  -Headers $headers
```

当任务状态为 `completed` 且 `cz_candidate_sync.failed` 为 `0` 后，打开本机 CZ 的
“候选价值复核”查看候选。`AUTO_REVIEW_ENABLED=false` 时全部候选仍会保留为人工复核，
不会自动发布。

### 第二部分接口定时拉取

第二部分只提供 JSON 接口时，正式链路为：

```text
第二部分接口
-> second-part-pull 拉取新增记录并记录 cursor
-> 转成 Answer Hub 标准 Excel
-> data/automation-queue/pending
-> automation-queue
-> CZ 候选价值复核
```

#### PowerZhuan QA 接口联调

已提供针对以下查询接口的配置模板：

```text
GET https://qa.powerzhuan.cn/api/records
```

模板文件为 `config\second-part-pull.powerzhuan.example.json`，已配置：

- Bearer Token 从 `SECOND_PART_API_TOKEN` 环境变量读取；
- `from`、`to` 使用 `SECOND_PART_QUERY_FROM_DATE` 和 `SECOND_PART_QUERY_TO_DATE`；
- `limit=1000`；
- 响应记录路径为顶层 `records`；
- `工单ID`、`聊天内容`和`产品类型`为必填映射，字段不匹配时停止入队。

先复制成本机配置，不要在 JSON 文件中写 Token：

```powershell
Copy-Item `
  .\config\second-part-pull.powerzhuan.example.json `
  .\config\second-part-pull.powerzhuan.local.json
```

在本机 `.env` 中配置变量。以下范围用于 2026 年 8 月 10 日的单日联调：

```dotenv
SECOND_PART_PULL_PROFILE=config/second-part-pull.powerzhuan.local.json
SECOND_PART_PULL_STATE=data/second-part-pull/powerzhuan-state.json
SECOND_PART_PULL_MAX_PAGES=1
SECOND_PART_API_TOKEN=
SECOND_PART_QUERY_FROM_DATE=2026-08-10
SECOND_PART_QUERY_TO_DATE=2026-08-10
SECOND_PART_QUERY_WINDOW_DAYS=1
```

这是一次性联调写法。正式定时运行时，建议把 `SECOND_PART_QUERY_FROM_DATE` 和
`SECOND_PART_QUERY_TO_DATE` 留空：队列脚本会取昨天作为最后完整日期，并按
`SECOND_PART_QUERY_WINDOW_DAYS` 拉取连续日期范围。每天运行设置为 `1`；每 3 天运行设置为
`3`。若需要重跑某一天，再临时设置明确的 FROM/TO，处理完成后恢复为空。

Token 由使用者只填入本机 `.env`，不得粘贴到聊天、命令历史或提交到仓库。执行一次只读拉取：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli second-part-pull `
  --profile .\config\second-part-pull.powerzhuan.local.json `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs `
  --state-file data\second-part-pull\powerzhuan-state.json `
  --max-pages 1
```

如果返回“缺少必填字段”，说明 PowerZhuan 的 `records[0]` 字段名与模板别名不同；
此时应提供一条已脱敏的 `records[0]` JSON 样例调整映射，不得放宽门禁或让空记录入队。

拉取成功后，手动运行一次 Answer Hub 到候选价值复核的完整流程：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli automation-queue `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs `
  --clustering-mode direct_mimo
```

配置模板已设置 `sync_to_cz_review=true`，因此本机 MiMo 和 CZ 配置完整时，候选会进入
CZ“候选价值复核”；不会自动送审或发布知识。

复制接口配置样例，不要在配置文件中写真实密钥：

```powershell
Copy-Item .\config\second-part-pull.example.json `
  .\config\second-part-pull.local.json
```

根据第二部分接口文档修改本机 `second-part-pull.local.json` 中的 URL、
`items_path`、`next_cursor_path`、`has_more_path` 和 `field_map`。鉴权值使用
`${SECOND_PART_API_TOKEN}` 引用本机环境变量。

`.env` 示例：

```dotenv
SECOND_PART_PULL_PROFILE=config/second-part-pull.local.json
SECOND_PART_PULL_STATE=data/second-part-pull/state.json
SECOND_PART_PULL_MAX_PAGES=10
SECOND_PART_API_TOKEN=

ANSWER_HUB_AUTOMATION_USE_MIMO=true
ANSWER_HUB_AUTOMATION_CLUSTERING_MODE=direct_mimo
# 安全初始值：先完成本地模型和 CZ 联调，再改为 true。
ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW=false
AUTO_REVIEW_ENABLED=false
```

手动只拉取一次、不运行模型：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli second-part-pull `
  --profile .\config\second-part-pull.local.json `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs `
  --state-file data\second-part-pull\state.json
```

配置 `SECOND_PART_PULL_PROFILE` 后，现有 `run_automation_queue.ps1` 和
Windows 计划任务会在每轮队列处理前自动执行拉取。每个成功入队的接口页才会推进
cursor；重复批次通过稳定批次指纹复用，不会重复生成队列任务。

手动执行一次扫描：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli automation-queue `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs `
  --clustering-mode direct_mimo
```

当前正式组合链路由 Answer Hub Automation API/队列和 CZ“候选价值复核”承接：
接收第二部分数据 → 清洗 → 聚类 → 主题分类与价值判断 →
仅转写值得沉淀主题 → 内容质量初标 → 候选价值复核 →
人工点击批量送审至知识库管理。

自动化队列现在默认走 CZ 原生“候选价值复核”队列；旧的直接候选上传能力仅保留为
受控兼容接口，不是当前默认主链路。

安全初始配置如下，不同步 CZ：

```dotenv
ANSWER_HUB_AUTOMATION_USE_MIMO=true
ANSWER_HUB_AUTOMATION_CLUSTERING_MODE=direct_mimo
ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW=false
ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ=false
AUTO_REVIEW_ENABLED=false
AUTO_REVIEW_VALIDATED_MODEL=已验收的模型名称
AUTO_REVIEW_VALIDATED_PROMPT_VERSION=已验收的Prompt版本
KB_BASE_URL=驰卓知识库服务地址
KB_INTEGRATION_KEY=通过安全环境变量提供
```

完成 CZ 接口、幂等和候选价值复核联调后，将
`ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW=true`。该开关只同步到候选价值复核，
不会绕过人工复核、Qwen3 查重、CZ 终审或自动发布知识。

配置 `.env` 后，正式使用建议安装每 3 天执行一次的 Windows 计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_automation_task.ps1 `
  -Schedule Daily `
  -IntervalDays 3 `
  -StartTime 03:00
```

联调测试时可临时改成分钟级扫描，例如 `-IntervalMinutes 5`。测试结束后应恢复为
三天一跑，避免每天生成过多候选，压缩人工复核时间。

失败文件修复后可手动重试：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\run_automation_queue.ps1 `
  -RetryFailed
```

卸载计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_automation_task.ps1 `
  -Uninstall
```

模型审核未通过、低置信度、模型或 Prompt 版本未验收的候选会进入
`model_review_results.xlsx` 的人工复核例外表，并同步到 CZ 候选价值复核；
不会直接创建知识。CZ 人工点击“批量送审至知识库管理”后，才会进入 Qwen3 查重和
知识库`review`待审核状态。

## Review error types

- 分类错
- 标题不准
- 标准项映射错
- 场景理解错
- 话术不合适
- 证据不足
- 图片判断失误
- 标准过期或冲突
- 需要拆分/合并知识
