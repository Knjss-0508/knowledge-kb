# Dify + Answer Hub 私有化部署

## 架构

```text
Dify :8080
  -> Answer Hub API :8780
  -> data/automation-queue/pending
  -> Windows 计划任务
  -> 清洗、聚类、主题价值分类、选择性知识转写、内容质量初标
  -> 同步 CZ 候选价值复核
```

Dify/Answer Hub 队列当前不直接作为正式知识库送审入口。正式候选需要进入答疑中台“候选价值复核”，由人工点击“批量送审至知识库管理”。

## 1. 配置 Answer Hub

在项目 `.env` 中配置以下变量。真实密钥只放本机环境变量，不提交到仓库。

```dotenv
# 可留空；首次启动 API 时脚本会生成并写入本机 .env
ANSWER_HUB_API_KEY=
ANSWER_HUB_API_HOST=0.0.0.0
ANSWER_HUB_API_PORT=8780

ANSWER_HUB_AUTOMATION_USE_MIMO=true
ANSWER_HUB_AUTOMATION_CLUSTERING_MODE=direct_mimo
ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW=false
ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ=false

AUTO_REVIEW_ENABLED=false
AUTO_REVIEW_VALIDATED_MODEL=已完成验收的模型名称
AUTO_REVIEW_VALIDATED_PROMPT_VERSION=已完成验收的Prompt版本

KB_BASE_URL=驰卓知识库服务地址
KB_INTEGRATION_KEY=驰卓集成密钥
```

## 一键启动

完成模型和驰卓配置后，可以直接双击：

```text
启动Dify平台.cmd
```

或执行：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\start_dify_answer_hub.ps1
```

脚本会依次启动 Dify、Answer Hub API，并安装每分钟扫描一次的自动化队列任务。
队列任务通过 `wscript.exe` 静默运行，不会再周期性弹出终端窗口。

## 2. 启动 Answer Hub API

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\start_automation_api.ps1
```

检查：

```text
http://127.0.0.1:8780/health
```

## 3. 安装并启动 Dify

脚本使用 Dify 官方 `1.15.0` 标签和 Docker Compose：

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_dify_local.ps1
```

然后打开：

```text
http://localhost:8080/install
```

## 4. 启动队列计划任务

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\install_automation_task.ps1 `
  -IntervalMinutes 1
```

## 5. 在 Dify 导入工具

1. 打开“工具”或“自定义工具”。
2. 选择通过 OpenAPI 导入。
3. 导入 `config/dify-answer-hub-openapi.json`。
4. 鉴权方式选择 API Key。
5. Header 名称填写 `X-Answer-Hub-Key`。
6. 值填写本机 `.env` 中的 `ANSWER_HUB_API_KEY`，不要复制到工作流提示词。

## 6. 创建 Dify 工作流

按以下节点连接：

```text
开始（Excel文件、产品类型）
  -> createAnswerHubJob
  -> 循环
       -> getAnswerHubJob
       -> 条件：status 是否为 completed/failed
  -> completed：输出 summary 和 artifact_urls
  -> failed：输出 error，并提供 retryAnswerHubJob
```

建议开始节点变量：

| 变量 | 类型 | 默认值 |
|---|---|---|
| `source_file` | File | 必填 |
| `product_type` | Text | 空 |
| `use_mimo` | Boolean | true |
| `clustering_mode` | Select | direct_mimo |
| `sync_to_cz_review` | Boolean | false |

## 7. 安全边界

- Dify 不保存 MiMo 或驰卓密钥。
- Dify 仅持有 Answer Hub 内部 API Key。
- 未通过模型审核的候选会进入 CZ 人工候选复核，不会直接创建知识。
- 只有人工复核后点击批量送审，才创建待审核知识；不会直接发布。
- 同一知识点通过稳定幂等键安全重试。
