# Answer Hub Workflow

## 主题级工作流（当前）

当前按动态品类配置处理首批手机、平板、笔记本、相机机身、相机镜头、耳机、手表、游戏机、手写笔和学习机；单条高质量原子问题也可以形成待审核主题：

```text
第二部分服务器数据
→ CZ 接口幂等接收
→ 原子问题拆分
→ 纯大模型 1～N 聚类
→ 标注主题问题分类和是否值得沉淀
→ 仅对值得沉淀主题进行知识转写
→ 模型初标转写内容质量
→ 同步答疑中台候选价值复核
→ 人工复核后批量送审至知识库管理
→ Qwen3查重拦截、终审与发布
```

- 只有至少 2 条完整会话或可用图片证据记录的主题进入 `topic_review_queue.xlsx`。
- 无聊天且无可用图片的记录进入 `evidence_gap_rows`；1～N 个原子问题均可形成待审核主题。
- 聚类后先执行主题问题分类与沉淀价值标注；不值得沉淀主题保留审计和价值复核记录，但不进行正式知识转写。
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
- Aggregates one or more atomic questions into auditable topic candidates.
- Uses the MiMo OpenAI-compatible API (text + up to 4 images) when configured.
- Falls back to the deterministic rule candidate when MiMo is not configured or fails; these rows are forced into human review.
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

### Configure MiMo

Copy `.env.example` to `.env`, then fill the values copied from the MiMo console. Do not place the key in Excel, browser code, or source code.

```text
MIMO_API_KEY=你的Key
MIMO_BASE_URL=控制台提供的 OpenAI 兼容地址（例如以 /v1 结尾）
MIMO_MODEL=控制台提供的 V2.5 多模态模型 ID
MIMO_TIMEOUT_SECONDS=60
ANSWER_HUB_DB_PATH=data/phone_mvp.db
```

When any of the first three MiMo fields is absent, the pipeline remains usable but marks all rows as priority human review and writes a rule-based fallback candidate.

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
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the 10-field case-only contract.

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

启动后访问 `http://localhost:8501`。该平台不是真实上线入口，不直接向知识库送审。页面包含四个工作区：

1. `自动化看板`：上传脱敏会话，验证输入清洗、语义标注、主题聚类、价值分类、选择性知识转写和内容质量初标；当前默认不读取标准目录。
2. `聚类验证`：对边界样本执行聚类判断并收集人工反馈。
3. `生成主题候选`：手动运行原有主题候选流程，便于调参与单步验证。
4. `审核与反馈`：验证 `topic_review_queue.xlsx` 的模型结果，下载审核底稿、值得沉淀主题的10项候选和训练反馈样本。

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

每次运行会生成独立目录和 `automation_run.json`，持续记录六个阶段的状态、指标、错误和产物路径。自动化只生成待审核知识；人工确认后可从工作台提交 CZ，但不会自动发布。

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

1. Clean required fields and normalize duplicate image links. Rows missing core question, conclusion, basis, or category are excluded from candidate generation with a recorded reason.
2. Filter to `产品类型=手机` before candidate generation.
3. Use the complete conversation, historical actual reply and sanitized case image as primary evidence.
4. Download at most four public JPEG/PNG/WebP images, each at most 5MB.
5. Send case evidence to MiMo. The response must not cite or invent quality standards.
6. If image download fails, MiMo fails, or the evidence is uncertain, keep a process-style candidate and set `是否重点复核=是`.
7. Aggregate evidence-qualified records by topic before review. Only `通过` / `修改后通过` outcomes are exported as 10-field candidates.
8. CZ uses Qwen3 Embedding for final duplicate interception before creating a `review` item.

## Output files

- `review_queue.xlsx`: source rows plus model labels and the legacy per-record review fields.
- `topic_review_queue.xlsx`: topic-level review queue, source mapping and evidence gaps.
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the 10-field case-only contract.
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

启用生产上传前，需要在 `.env` 中配置：

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

配置 `.env` 后，可安装每 5 分钟执行一次的 Windows 计划任务：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_automation_task.ps1 `
  -IntervalMinutes 5
```

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
