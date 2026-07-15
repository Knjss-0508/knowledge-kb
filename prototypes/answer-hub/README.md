# Answer Hub Workflow

## 主题级工作流（当前）

手机第一期按以下链路处理，单条记录不会直接生成知识正文：

```text
单条记录清洗与主题特征提取
→ 混合聚类
→ 主题级标准检索
→ 主题级 MiMo/规则初标
→ 人工完整编辑与反馈
→ 13 列候选导出
```

- 只有至少 2 条完整会话或可用图片证据记录的主题进入 `topic_review_queue.xlsx`。
- 无聊天且无可用图片的记录进入 `evidence_gap_rows`；只有 1 条有效证据的主题进入 `pending_cluster_rows`。
- 审核人在工作台直接编辑最终 13 列候选，审核字段仅保留结论、错误说明、训练标记和审核信息。
- `.env` 中的 `KB_BASE_URL`、`KB_INTEGRATION_KEY` 只用于显示 cz API 联调就绪状态；当前版本不会发送任何 cz API 请求。

This repository implements the third-part knowledge ingestion workflow for the answer hub:

`第二部分数据 -> 数据预处理 -> 标准检索 -> MiMo 多模态初标 -> 人工复核 -> 发布 -> 反馈回流`

## What it does

- Reads the Excel output from the second part.
- Loads a qc standard catalog exported from the cz skill转写 result.
- Converts every input row into one auditable knowledge candidate.
- Retrieves the Top 3–5 relevant phone QC standards before labeling.
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

### Standard catalog
The standard catalog can be a `.json` or `.xlsx` file.

Expected fields:

- 标准ID / standard_id
- 主标题 / title
- 一级分类 / category_l1
- 二级分类 / category_l2
- 检索关键词 / keywords
- 适用范围 / scope
- 参考话术 / response_snippet
- 状态 / status
- 版本 / version

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
  --standards "D:\\飞书\\cz标准目录.xlsx" \
  --output-dir ".\\outputs"
```

Phone MVP example. The standards file should contain only the phone standard master table exported by cz's skill:

```bash
answer-hub ingest \
  --source "D:\\飞书\\共享数据汇总_2026-07-10~2026-07-10.xlsx" \
  --standards "D:\\飞书\\手机-RAG知识库主表.xlsx" \
  --product-type "手机" \
  --output-dir ".\\outputs\\phone"
```

Use `--rule-only` to validate preprocessing and standard retrieval without calling MiMo. Use `--audit-db .\\data\\phone_mvp.db` to override the local audit database path.

The command now writes:

- `review_queue.xlsx`: per-record audit and model trace, not the formal review entry.
- `topic_review_queue.xlsx`: topic-level candidate workbook for local review.
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the shared 13-column contract.

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

## Streamlit 主题知识工作台

安装一次前端依赖：

```cmd
python -m pip install streamlit openpyxl
```

启动本地工作台：

```cmd
cd /d "C:\Users\admin\Desktop\答疑中台知识库"
python -m streamlit run streamlit_app.py
```

页面会自动打开本地地址。它包含两个工作区：

1. `生成主题候选`：上传方向二 Excel 与 cz 手机标准主表，执行证据分流、标准检索、模型/规则初标和主题聚类。
2. `审核与反馈`：审核 `topic_review_queue.xlsx`，下载审核底稿、13 列候选知识和训练反馈样本。

没有聊天内容且没有可用现场图片的记录只进入 `evidence_gap_rows`，不会独立生成主题候选。若电脑无法访问 PyPI，请使用公司镜像或由管理员提供 `streamlit` 的离线 wheel 安装包。

Run the local validation page:

```bash
set PYTHONPATH=src
python -m answer_hub.web
```

Open `http://127.0.0.1:8765`. The page accepts the second-part workbook and the phone standard master table, calls MiMo only on the local server, previews the candidate queue and downloads the review workbook. It also supports uploading `review_queue.xlsx`, filtering candidates, filling the `CZ*` review fields, and downloading an updated workbook. The API key is never exposed to the browser.

If PyPI is unavailable and Flask cannot be installed, run the bundled Codex Python instead. The web entrypoint automatically falls back to a standard-library local server:

```cmd
set PYTHONPATH=src
"C:\Users\admin\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe" -m answer_hub.web
```

## Phone MVP processing rules

1. Clean required fields and normalize duplicate image links. Rows missing core question, conclusion, basis, or category are excluded from candidate generation with a recorded reason.
2. Filter to `产品类型=手机` before candidate generation.
3. Retrieve up to five active standards using category path and keywords; broad terms alone cannot establish a trusted match.
4. Download at most four public JPEG/PNG/WebP images, each at most 5MB.
5. Send text, available image data and retrieved standards to MiMo. The response must be valid JSON and can cite only the retrieved standards. Invalid JSON/fields are retried once.
6. If image download fails, no standard is retrieved, MiMo fails, or the evidence is uncertain, keep a process-style candidate and set `是否重点复核=是`. A record without chat content and usable image evidence only contributes to coverage analysis and topic clues.
7. Aggregate evidence-qualified records by topic before review. Only `通过` / `修改后通过` topic outcomes are exported as 13-column candidates for the cz website.

## Output files

- `review_queue.xlsx`: source rows plus model labels and the legacy per-record review fields.
- `topic_review_queue.xlsx`: topic-level review queue, source mapping and evidence gaps.
- `candidate_knowledge.xlsx`: unreviewed topic candidates in the 13-column contract.
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
