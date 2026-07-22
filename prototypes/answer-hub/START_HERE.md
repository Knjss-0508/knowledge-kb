# 答疑中台知识库交接包

更新日期：2026-07-22

## 当前目标

本项目已经把第三部分与本地CZ合并交付，负责“第二部分批量数据 -> 无标准案例知识 -> 人工审核 -> Qwen3查重拦截 -> CZ终审”。系统不会自动发布知识。

## 本次关键变更

旧的 `核心问题`、`判定结论`、`判定依据`、`一级分类`、`二级分类` 来自百晓生上游，可能不代表用户真实意图。它们现在只保留为弱参考和审计字段。

当前推荐链路：

```text
完整聊天内容 + 历史实际回复 + 脱敏案例图
-> MiMo 会话语义标注
-> MiMo 拆分 1～3 个原子问题
-> MiMo 对原子问题进行 1～N 主题聚类
-> 主题级转写
-> 推荐回复
-> 模型初标：是否值得沉淀 + 草稿质量
-> 组员标注：是否值得沉淀 / 是否可用 / 如何修改 / 问题反馈
-> 批量提交CZ
-> Qwen3重复拦截
-> cz终审与发布
```

`direct_mimo` 是推荐模式，直接使用 API + 提示词完成原子问题拆分和 1～N 聚类，不依赖 Embedding。`semantic_mimo`、`semantic` 和 `rule` 继续作为备用模式。

首批品类为手机、平板、笔记本、相机机身、相机镜头、耳机、手表、游戏机、手写笔和学习机。品类由`src\answer_hub\product_categories.json`配置。当前批量链路不读取或主动生成标准关联；新候选的`关联标准项`默认为空，已有值会保留并单独搁置。

## 新电脑运行

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pytest -q
```

交付前要求测试全部通过，实际数量以当前版本测试输出为准。

启动工作台：

```powershell
.\start_streamlit.ps1
```

浏览器地址：`http://localhost:8501`

也可以直接双击项目根目录中的 `启动自动化看板.cmd`。启动窗口必须保持打开；关闭窗口即停止前端服务。

启动本地CZ、PostgreSQL、Redis和Qwen3查重服务：

```powershell
Copy-Item .\cz-knowledge-kb\knowledge-kb-master\.env.example `
  .\cz-knowledge-kb\knowledge-kb-master\.env
.\scripts\start_local_cz.ps1
```

也可以双击`启动本地CZ.cmd`。

## 配置

`.env` 不在交接包内。请从 `.env.example` 新建，并按需填写：

```dotenv
MIMO_API_KEY=
MIMO_BASE_URL=
MIMO_MODEL=

EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_BATCH_SIZE=8
EMBEDDING_MAX_RETRIES=3
```

`MIMO_*` 缺失时，模型语义标注会回退为规则特征；可查看页面“模型语义标注”指标确认。推荐实际验证时配置 MiMo。

Qwen3 Embedding已经包含在CZ基础Compose中，不需要单独启动CPU覆盖文件：

```powershell
.\scripts\start_local_cz.ps1
```

首次启动会下载模型。Qwen3只在Docker内部提供给CZ查重，不对宿主机公开端口。

## Streamlit 使用顺序

1. 打开“自动化看板”。
2. 上传已脱敏的方向二会话Excel；当前为无标准引用模式。
3. 处理品类选择“全部”或单个品类；聚类方式默认选择“纯大模型 1～N 聚类”。
4. 查看六个处理阶段、运行指标和异常信息；成功后会生成 `topic_review_queue.xlsx`。
5. 打开“审核与反馈”，查看主题来源记录的模型标签、模型依据、完整聊天和现场图片。
6. 发给组员的候选表标注“是否值得沉淀、是否可用、如何修改、问题反馈”；标注为不值得沉淀的知识不会进入批量送审。
7. 审核通过后批量提交10项候选；Qwen3结合标题和正文文本重合证据拦截真正重复项，同模板但问题对象不同的知识正常进入待审核。

“生成主题候选”保留为手动调试入口；“聚类验证”用于单独验证边界样本。自动化流程只到待审核，不自动发布知识。

## 发布前检查

正式交付前执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_release.ps1 `
  -BuildPackage -Version 20260722-qwen3-batch-adapter-v8
```

只有脚本返回`status=passed`时才能交付。该脚本同时验证逐条失败隔离、Qwen3不可用拦截、无标准字段门禁、图例兼容、前端语法、Compose和交付包敏感文件。
也可以双击`发布前验收并打包.cmd`执行同一流程。

## 本次待办

1. 将 `cluster_validation_review (1).xlsx` 和 `质检答疑案例库(4).xlsx` 复制到新电脑项目的 `data\` 目录，或直接在 Streamlit 上传。
2. 用真实数据统计“模型语义分类”和旧一级/二级分类的偏差，确认标签枚举是否需要收敛。
3. 在“聚类验证”页由人工标注边界样本，观察错误合并和错误拆分。
4. 基于人工反馈调 `mimo.py` 的会话语义标注 Prompt；先调提示词和标签体系，暂不训练小模型。
5. 聚类稳定后，再优化“流程方法 / 具体判定”标注和主题级知识转写。

## 安全与包内容

交接包不包含 `.env`、任何 API Key、数据库文件、真实会话 Excel、Docker 缓存、`.venv`、运行日志和 `outputs` 结果。真实数据必须保持脱敏。
