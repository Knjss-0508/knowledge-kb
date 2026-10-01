# 组内电脑本地置信度训练

本包把置信度训练作为 Answer Hub 的本地子流程，使用 SQLite 保存候选、人工真值、DeepSeek 标注和任务状态。它不连接 CZ PostgreSQL，也不包含服务器数据库或密钥。

## 最小闭环

```text
POST /api/v1/confidence-training/items
-> PATCH /api/v1/confidence-training/items/<id>（人工真值）
-> POST /api/v1/confidence-training/jobs（DeepSeek 标注/影子结果）
-> GET /api/v1/confidence-training/overview
-> GET /api/v1/confidence-training/export
```

## 模型边界

`config/local-model.json` 必须指向公司内网 DeepSeek，密钥只放在 `GROUP_LLM_API_KEY` 环境变量。组内模型不可用时任务失败并保留候选，不会回退到个人 MiMo。

## 故障恢复

SQLite 数据库和导出目录必须纳入备份；任务按候选 ID 保存，重启后可继续查看已完成标注。服务器备用端需要部署同样的本地模块和 DeepSeek 访问能力，才可在组内电脑故障时接管置信度训练。
