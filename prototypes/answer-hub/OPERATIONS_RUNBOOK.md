# 答疑知识中台运行治理手册

更新日期：2026-07-22

## 1. 运行指标与SLA

每次自动化运行的 `automation_run.json` 现在记录：

- 总耗时及各阶段耗时。
- 模型调用、失败、重试、Token和估算成本。
- 规则降级率。
- 每100条处理时长。
- SLA是否通过及超限原因。
- 运行尝试次数和失败恢复历史。

生成聚合运营报告：

```powershell
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m answer_hub.cli operations-report `
  --output-dir ".\outputs\automation-runs" `
  --output ".\outputs\operations\automation_metrics.json"
```

默认SLA可通过 `.env` 调整：

```dotenv
ANSWER_HUB_SLA_SECONDS_PER_100_ROWS=3600
ANSWER_HUB_SLA_MAX_FAILURE_RATE=0.05
ANSWER_HUB_SLA_MAX_FALLBACK_RATE=0.20
ANSWER_HUB_MAX_RUN_COST=50
```

## 2. 失败恢复

自动化流程在清洗、语义标注和主题构建阶段保存检查点。恢复失败运行：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli retry-run `
  --output-dir ".\outputs\automation-runs" `
  --run-id "20260722-120000-example"
```

工作台“自动化看板”也提供“从最近检查点继续”入口。恢复时复用已完成阶段，不重新上传输入。

## 3. 并发、限流与成本

```dotenv
ANSWER_HUB_MIMO_MAX_WORKERS=1
MIMO_MAX_REQUESTS_PER_SECOND=2
MIMO_MAX_RETRIES=2
MIMO_RETRY_BACKOFF_SECONDS=0.75
MIMO_INPUT_COST_PER_MILLION_TOKENS=0
MIMO_OUTPUT_COST_PER_MILLION_TOKENS=0
```

并发应从1逐步提升，并同时观察429、超时、失败率和单批成本。

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
