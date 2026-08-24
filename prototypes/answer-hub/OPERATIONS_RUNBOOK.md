# 答疑知识中台运行治理手册

更新日期：2026-08-03

## 1. 运行指标与SLA

每次自动化运行的 `automation_run.json` 现在记录：

- 总耗时及各阶段耗时。
- 默认加载的共享术语字典版本、条目数、分类和适用品类。
- 模型调用、失败、重试、Token和估算成本。
- 规则降级率。
- 每100条处理时长。
- SLA是否通过及超限原因。
- 运行尝试次数和失败恢复历史。
- 最近真实活动、细分子阶段和监管处理反馈。

### 运行监管界面

Streamlit 工作台首页新增`运行监管`，默认每15秒刷新，可查看：

- 排队、运行、待人工审核、失败和CZ同步状态；
- 超过心跳阈值未更新的`疑似卡住`运行；
- 原子问题与主题聚类、聚类准入/历史归并/价值分类、知识转写/内容初审等细分阶段；
- 负责人、处理状态、原因分类、反馈备注和历史记录。

默认超过7200秒没有更新即标记为疑似卡住。可通过以下变量调整：

```dotenv
ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS=7200
ANSWER_HUB_RUN_FEEDBACK_PATH=data/automation-queue/run_feedback.db
```

监管反馈只记录运行处理信息，不修改候选知识，不触发送审或发布。

Automation API 提供受鉴权的生产监管接口：

```http
GET   /api/v1/automation/jobs
PATCH /api/v1/automation/jobs/{record_id}/feedback
```

列表接口不会返回服务器本地文件路径，只返回产物名称。单个任务产物仍通过原有鉴权下载接口获取。

生成聚合运营报告：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m answer_hub.cli operations-report `
  --output-dir ".\outputs\automation-runs" `
  --output ".\outputs\operations\automation_metrics.json"
```

默认SLA可通过 `.env` 调整：

```dotenv
ANSWER_HUB_SLA_SECONDS_PER_100_ROWS=900
ANSWER_HUB_SLA_MAX_FAILURE_RATE=0.05
ANSWER_HUB_SLA_MAX_FALLBACK_RATE=0.20
ANSWER_HUB_MAX_RUN_COST=50
```

术语字典不需要配置参数。自动化、队列和部署后的服务都会默认读取代码内置字典；
如果字典为空或条目损坏，运行会在生成前失败并进入人工处理，不会降级为没有术语
参考的模型提示词。

## 2. 失败恢复

自动化流程在清洗、语义标注和主题构建阶段保存检查点。恢复失败运行：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli retry-run `
  --output-dir ".\outputs\automation-runs" `
  --run-id "20260722-120000-example"
```

如果是人工 `Ctrl+C` 中断，运行清单可能仍显示 `running`。确认原 Python 进程已经
停止后，可显式允许从这种中断状态恢复：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli retry-run `
  --output-dir ".\outputs\automation-runs" `
  --run-id "20260722-120000-example" `
  --allow-interrupted-running
```

工作台“自动化看板”也提供“从最近检查点继续”入口。恢复时复用已完成阶段，不重新上传输入。

### 第二部分接口拉取恢复

第二部分接口拉取状态默认保存在：

```text
data/second-part-pull/state.json
```

只有接口页已经成功写入 `data/automation-queue/pending` 后才推进 cursor。拉取中断时，
修复接口或网络后重新执行同一命令即可：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli second-part-pull `
  --profile .\config\second-part-pull.local.json `
  --queue-dir data\automation-queue `
  --output-dir outputs\automation-runs `
  --state-file data\second-part-pull\state.json
```

如果进程在任务入队后、状态落盘前中断，重试会通过稳定批次指纹复用已有任务，不会
重复入队。不要手工修改 cursor；确需重置时先备份状态文件并确认上游接口支持安全重放。

## 3. 并发、限流与成本

```dotenv
ANSWER_HUB_MIMO_MAX_WORKERS=4
ANSWER_HUB_DIRECT_CLUSTER_MAX_WORKERS=4
ANSWER_HUB_DIRECT_ATOMIC_BATCH_SIZE=4
ANSWER_HUB_DIRECT_ATOMIC_BATCH_MAX_CHARS=16000
ANSWER_HUB_DIRECT_MIMO_BATCH_SIZE=6
ANSWER_HUB_DIRECT_PROGRESS_FLUSH_EVERY=5
ANSWER_HUB_DIRECT_RECONCILE_MODEL_FLOOR=0.68
ANSWER_HUB_DIRECT_RECONCILE_LIMIT=24
ANSWER_HUB_CLUSTER_ADMISSION_MIN_CONFIDENCE=0.75
MIMO_CLUSTER_MEDIA_POLICY=on_demand
MIMO_CLUSTER_MEDIA_MIN_TEXT_CHARS=220
MIMO_API_KEYS=
MIMO_MAX_REQUESTS_PER_SECOND=2
MIMO_RESPONSE_READ_TIMEOUT_SECONDS=90
MIMO_RESPONSE_TIMEOUT_CIRCUIT_BREAKER_FAILURES=2
MIMO_THINKING_TYPE=disabled
MIMO_MAX_COMPLETION_TOKENS=2048
MIMO_MAX_RETRIES=2
MIMO_RETRY_BACKOFF_SECONDS=0.75
MIMO_INPUT_COST_PER_MILLION_TOKENS=0.14
MIMO_OUTPUT_COST_PER_MILLION_TOKENS=0.28
```

`MIMO_API_KEYS` 使用逗号或分号分隔备用 Key。余额、额度或鉴权失败会自动切换，
普通429限流不会切换。可在运行汇总中查看 `model_key_switches`，但不得记录或上传
实际 Key。以上成本字段按`mimo-v2.5`官方未缓存输入和输出价格填写，单位为
美元/百万Token，只影响本地成本估算。

短 JSON 提取和聚类默认关闭 MiMo 深度思考（`MIMO_THINKING_TYPE=disabled`），并使用
`MIMO_MAX_COMPLETION_TOKENS=2048` 限制生成预算。若出现 JSON 截断，应先缩小批次或
适度提高上限，不要直接恢复默认的大输出预算。

默认使用4个原子提取线程和4个首轮聚类线程。无需本轮附媒体的同品类会话会按
最多4条、总聊天字符不超过16000字符合并为一次原子提取请求；批量输出漏条、重复
或格式错误时自动拆成更小批次，最终可降到单条处理。线程共享全局每秒请求限制，
输出仍按原顺序组装，断点文件由主线程统一写入。2026-08-11 的固定20条压测中，
4并发是无模型失败且最快的档位；若接口持续出现429或读取超时，先降到2，勿提高到6。

二次归并先执行本地质检口径；只有相似度达到0.68且本地规则无法决定时才调用MiMo，
每轮最多24次。成功裁决会写入`direct_mimo_progress.json`，恢复运行时不会重复付费。
进度会分别显示原子提取、首轮聚类和同品类二次归并，不应再出现聚类批次完成后长时间
没有状态变化的情况。

完整自动化在聚类完成后执行准入门禁。`single_topic`只保留1个原子问题，只有
`multi_topic`拆成2～3个，`uncertain`最多保留1个暂定问题。只有成功的
`direct_mimo`聚类、原子与聚类置信度达到0.75、没有复核/降级/失败/冲突标记，并且
回收业务层级和产品品类完全一致时，才允许自动合并并调用后续主题模型。业务层级或
产品品类冲突、未识别时写入`pending_cluster_rows`供人工聚类复核；低置信、复核、
降级、失败或冲突拆分时，按原子问题生成暂定单主题候选，继续转写并强制人工价值复核。
清晰单成员主题可以放行，不按成员数量直接拦截。

`on_demand`模式下，聊天证据达到220字时图片暂不附加给原子提取模型；短聊天和视频
仍保留媒体输入。长聊天中明确出现“请看图、图中、截图、照片中、圈出位置、视频中”
等视觉依赖表达时也会继续附加媒体。只有不需要本轮附媒体的记录会进入文本批量提取；
不同品类不会放入同一批次。该策略只减少聚类阶段的媒体Token，不影响后续知识转写
使用图片证据。

如果 MiMo 在主题聚类阶段长时间没有返回，可临时把
`MIMO_RESPONSE_READ_TIMEOUT_SECONDS` 调低到 45～60 秒，并把原子提取批次降到
2 条、主题聚类批次降到 4 条。一次响应总超时会先把当前
原子提取批次拆小重试；连续达到
`MIMO_RESPONSE_TIMEOUT_CIRCUIT_BREAKER_FAILURES` 次才熔断，后续候选保守进入人工复核。

只做聚类准确性验证时使用 `answer-hub automate --cluster-only`。该模式跳过主题价值、
知识转写、推荐回复和内容初审，只输出一个 `cluster_result.xlsx`，避免为验证聚类
额外消耗下游模型调用额度。`direct_mimo + --cluster-only` 默认将聚类媒体策略设为
`never`：不下载、不解析、不发送图片或视频，带媒体链接的同品类记录也可以进入文本
批量原子提取。需要做媒体影响对照实验时才显式增加
`--cluster-media-policy on_demand`；完整自动化流程仍按环境变量中的媒体策略运行。

吞吐试验只允许使用仅聚类模式，避免消耗下游转写额度。例如先在同一 PowerShell 中设定
待测并发和批次，再执行：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli automate `
  --source ".\data\脱敏样本.xlsx" `
  --output-dir ".\outputs\automation-runs" `
  --clustering-mode direct_mimo `
  --cluster-only `
  --max-source-rows 20 `
  --cluster-media-policy never
```

每轮只变更一组并发/批次参数。只有 `model_failed_calls=0`、
`atomic_extraction_failed=0`、`direct_cluster_failed=0` 且未熔断时，才比较
`seconds_per_100_rows` 并逐步提高吞吐；不要把试验参数直接写入 `.env`。

自动审核按品类灰度：

```dotenv
AUTO_REVIEW_ENABLED=false
AUTO_REVIEW_KILL_SWITCH=false
AUTO_REVIEW_PRODUCT_TYPES=手机
```

首次交付保持关闭；达到门槛后先只配置一个品类。出现错误放行时设置
`AUTO_REVIEW_KILL_SWITCH=true`，所有候选立即回到人工审核。

## 4. 脱敏和保留策略

输入会自动扫描手机号、邮箱、身份证号，并对银行卡号和地址特征产生提醒。发现高风险内容时默认拒绝处理：

```dotenv
ANSWER_HUB_REDACTION_ENFORCE=true
ANSWER_HUB_RETENTION_DAYS=30
```

预览过期运行目录：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli retention-cleanup `
  --output-dir ".\outputs\automation-runs" `
  --days 30
```

确认后执行：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli retention-cleanup `
  --output-dir ".\outputs\automation-runs" `
  --days 30 `
  --execute
```

## 5. CZ运行治理

CZ新增：

```http
GET  /api/v1/operations/metrics
POST /api/v1/operations/lifecycle/apply-expiry
GET  /api/v1/knowledge/lifecycle/overview
GET  /api/v1/integration/retrieval-analytics?days=30
POST /api/v1/topic-candidates/review:batch
```

知识支持发布时间、失效时间、最近复核时间、废弃原因和替代知识ID。已失效知识不会参与搜索、标准快照或正式知识查重。

## 6. 备份恢复

备份数据库和CZ媒体文件：

```powershell
.\scripts\backup_cz.ps1
```

恢复属于破坏性操作，必须显式确认：

```powershell
.\scripts\restore_cz.ps1 `
  -BackupDirectory ".\backups\cz-20260722-120000" `
  -ConfirmRestore
```

恢复后必须执行 `/health`、`/ready`、搜索和媒体抽查。

## 7. 端到端验收

只检查健康、就绪和分类字典：

```powershell
$env:INTEGRATION_API_KEY = "通过受控环境变量提供"
.\scripts\e2e_acceptance.ps1
```

增加第二部分批量处理与幂等验证：

```powershell
.\scripts\e2e_acceptance.ps1 -RunMutationTests
```

只有测试账号具备审核发布权限时，才使用 `-PublishTestKnowledge` 验证审核、发布、搜索和反馈，并在验收后废弃测试知识。

## 8. 安全扫描与CI

```powershell
.\.venv\Scripts\python.exe .\scripts\scan_sensitive_files.py `
  --root . `
  --ignore-local-env
```

GitHub Actions执行根项目测试、CZ测试、Python编译、敏感文件扫描、前端JavaScript语法和Compose配置检查。
