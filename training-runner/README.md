# 本机 GPU 训练 Runner

本机仅作为 GPU 算力节点，不运行知识库前端、后端、PostgreSQL、Redis、
Embedding 服务或本项目 Docker 容器。知识库完整服务运行在服务器，
Runner 只通过出站请求领取指定训练任务、回传进度和结果。任务 URL 必须是新部署
对外入口提供的 HTTP(S) 完整任务地址。

## 目录边界

- 代码：当前仓库的 `training-runner`
- 独立训练环境：默认 `%LOCALAPPDATA%\KnowledgeKB\embedding-training`
- 推荐本机配置：`D:\knowledge-kb-training-runtime`
- 模型缓存与训练产物：均位于独立训练环境中，不进入 Git 仓库

## 首次安装

使用 PowerShell 7：

```powershell
pwsh -File .\training-runner\host-runner.ps1 `
  -Action install `
  -RuntimeRoot D:\knowledge-kb-training-runtime
```

安装脚本会创建独立 Python 虚拟环境，并安装固定版本的 PyTorch CUDA、
ms-swift 与 bitsandbytes。它不会执行 Docker Compose，也不会启动项目服务。

## 配置

在工作台“训练与版本”中创建 LoRA 任务后，会显示一次性可复制的：

- `TRAINING_JOB_URL`：精确绑定该任务的完整访问地址
- `TRAINING_JOB_TOKEN`：只允许领取和更新该任务的短期密钥

将两项粘贴到桌面“知识库模型训练控制台”，点击“保存并开始此任务”。
控制台会自动保存到 `training-runner\.env`，不需要从服务器查找或复制
全局密钥。

其余本机配置包括：

- `TRAINING_RUNNER_ID`：本机稳定标识
- `TRAINING_RUNTIME_ROOT`：项目目录外的运行目录

Runner 只发起出站连接，本机无需开放端口。每次启动只执行 URL 绑定的
一条任务，完成、取消或失败后自动退出；不会领取其他排队任务。

## 校验与运行

```powershell
pwsh -File .\training-runner\host-runner.ps1 -Action check
pwsh -File .\training-runner\host-runner.ps1 -Action smoke
pwsh -File .\training-runner\host-runner.ps1 -Action probe
pwsh -File .\training-runner\host-runner.ps1 -Action run
```

`smoke` 会下载基础模型并执行 1 step 的真实 4-bit NF4 QLoRA，
用于确认当前 Windows、CUDA、显卡和训练依赖能够共同完成训练。

## 可视化控制台

日常操作无需手工输入命令，可打开：

```powershell
pwsh -File .\training-runner\runner-console.ps1
```

控制台提供：

- 任务 URL、任务密钥、Runner 标识和名称的本机配置；
- GPU、独立训练环境、Runner 进程和服务器连接状态；
- 安装环境、环境检查、1 Step 实训、连接检测；
- 保存并开始指定任务、停止当前训练、查看日志和打开训练产物目录；
- 上传模型、替换生产模型和全量向量重建的锁定提示。

创建桌面快捷方式：

```powershell
pwsh -File .\training-runner\install-console-shortcut.ps1
```

启动前脚本会检查本机是否仍有知识库项目容器运行；发现
`kb-backend`、`kb-postgres`、`kb-redis`、`kb-embedding-qwen`
或容器版训练 Runner 时将拒绝启动。

## 模型发布授权边界

训练完成后的候选模型只保留在本机独立训练产物目录。Runner 仅向服务器
回传本机产物引用、校验值和评估指标，不上传模型文件。

- 候选审批只记录人工评审结论，不等于允许上传。
- 模型上传必须取得用户针对本次上传的明确授权。
- 替换服务器生产模型必须再次取得用户针对本次替换的明确授权。
- 全量向量重建属于独立高风险操作，也必须单独确认。

任何训练完成、候选批准或质量门禁通过事件都不能自动触发上述操作。
