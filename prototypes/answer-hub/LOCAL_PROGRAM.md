# 本地程序验证说明

当前项目已经具备本地工作台、自动化 API 和队列处理器。切换到组内模型时，只需要在本机配置模型地址和模型名；API Key 不写入配置文件，而是放在 `api_key_env` 指向的 Windows 环境变量中。

## 1. 准备本地模型配置

在项目根目录执行：

```powershell
Copy-Item .\config\local-model.example.json .\config\local-model.json
```

编辑 `config\local-model.json`，至少修改：

- `base_url`：组内 OpenAI 兼容接口地址，例如 `http://<组内模型主机>:8000/v1`；
- `model`：组内实际模型名；
- `media_model`：需要看图/视频时使用的模型名，没有单独模型就与 `model` 相同；
- `api_key_env`：保存密钥的环境变量名称，不要把密钥原文写进 JSON。

在当前 PowerShell 会话设置密钥（示例值仅用于说明，不要照抄）：

```powershell
$env:GROUP_LLM_API_KEY = "<从组内安全渠道取得的密钥>"
```

如果希望每次打开电脑都可用，请把该变量写入当前 Windows 用户环境变量，不要提交到 Git。

## 2. 启动本地 API 和处理器

先安装环境并复制 `.env`：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
Copy-Item .env.example .env
```

启动 API（一个 PowerShell 窗口保持运行）：

```powershell
.\scripts\start_automation_api.ps1
```

另开一个 PowerShell 窗口运行队列处理（会读取本地模型配置并在本机执行推理）：

```powershell
.\scripts\run_automation_queue.ps1
```

如果 API 启动脚本自动生成了 `ANSWER_HUB_API_KEY`，在第二个窗口先载入它（只写入当前会话，不会打印密钥）：

```powershell
$env:ANSWER_HUB_API_KEY = ((Get-Content .env -Encoding UTF8 | Where-Object { $_ -match '^ANSWER_HUB_API_KEY=' } | Select-Object -First 1) -split '=', 2)[1]
```

API 默认地址是 `http://127.0.0.1:8780`；如需让同一内网其他电脑访问，可在 `.env` 设置 `ANSWER_HUB_API_HOST` 为受控内网地址，并配合防火墙限制来源。

## 3. POST 提交、GET 查询

提交已脱敏的第二部分 JSON：

```powershell
$headers = @{ "X-Answer-Hub-Key" = $env:ANSWER_HUB_API_KEY }
$body = @{
  source_system = "second-part"
  idempotency_key = "demo-20260911-001"
  items = @(
    @{
      redaction_status = "redacted"
      record = @{
        工单ID = "DEMO-001"
        聊天内容 = "手机屏幕有亮线，应该怎么判断？"
        产品类型 = "手机"
      }
    }
  )
} | ConvertTo-Json -Depth 8

$created = Invoke-RestMethod `
  -Method Post `
  -Uri "http://127.0.0.1:8780/api/v1/automation/second-part/records:batch" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $body
$created.job_id
```

查询分析状态和结果：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri ("http://127.0.0.1:8780/api/v1/automation/jobs/{0}" -f $created.job_id) `
  -Headers $headers
```

## 4. 不改容器即可切换模型

读取当前本地模型配置：

```powershell
Invoke-RestMethod `
  -Method Get `
  -Uri "http://127.0.0.1:8780/api/v1/local/model-config" `
  -Headers $headers
```

更新模型地址和名称（密钥仍由 `api_key_env` 环境变量提供）：

```powershell
$newModel = @{
  provider = "group-internal"
  base_url = "http://<新的组内模型主机>:8000/v1"
  model = "<新的模型名>"
  media_model = "<新的模型名>"
  api_key_env = "GROUP_LLM_API_KEY"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Put `
  -Uri "http://127.0.0.1:8780/api/v1/local/model-config" `
  -Headers $headers `
  -ContentType "application/json" `
  -Body $newModel
```

配置写入 `config\local-model.json`，从下一次任务开始生效；不会修改 Docker 容器内部文件，也不会把 API Key 返回给调用方。

## 5. 验证边界

- `/health` 只证明 API 进程存活；
- 查询到 `status=completed` 且有输出工件，才证明一次本地分析完成；
- 仍需使用脱敏样本验证模型质量，低置信或失败记录继续进入人工审核；
- 本地化不改变 Qwen3 查重、CZ 人工终审和“不自动发布”的现有红线。
