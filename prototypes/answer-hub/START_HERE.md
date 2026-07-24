# 答疑中台知识库交接包

更新日期：2026-07-24

> 交给另一台电脑或另一位 AI 继续工作时，先阅读
> `AI_HANDOFF_LATEST.md`。旧的`PROJECT_HANDOFF.md`和历史`handoff`包不再作为当前依据。

## 当前目标

本项目已经把第三部分与 CZ 原生“候选价值复核”和知识库管理接通。Streamlit 仅用于验证各阶段准确性，不是真实上线入口。系统不会自动发布知识。

## 本次关键变更

旧的 `核心问题`、`判定结论`、`判定依据`、`一级分类`、`二级分类` 来自百晓生上游，可能不代表用户真实意图。它们现在只保留为弱参考和审计字段。

当前推荐链路：

```text
完整聊天内容 + 历史实际回复 + 脱敏案例图
-> MiMo 会话语义标注
-> MiMo 拆分 1～3 个原子问题
-> MiMo 对原子问题进行 1～N 主题聚类
-> 主题问题分类 + 是否值得沉淀
-> 仅对值得沉淀的主题进行知识转写与推荐回复生成
-> 模型初标转写内容质量
-> 同步到答疑中台“候选价值复核”
-> 人工复核沉淀价值、草稿和分类
-> 点击“批量送审至知识库管理”
-> Qwen3重复拦截
-> 知识库待审核
-> CZ人工终审与发布
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

## Streamlit 准确性验证顺序

Streamlit 是本地测试平台，用于检查聚类、价值分类、知识转写和内容质量初标的准确性，不承担正式候选同步和知识库送审。

1. 打开“自动化看板”。
2. 上传已脱敏的方向二会话Excel；当前为无标准引用模式。
3. 处理品类选择“全部”或单个品类；聚类方式默认选择“纯大模型 1～N 聚类”。
4. 查看处理阶段、主题问题分类、沉淀价值、选择性转写和内容质量初标指标；成功后会生成 `topic_review_queue.xlsx`。
5. 打开“审核与反馈”，验证主题来源记录、模型标签、完整聊天、现场图片和10项草稿。
6. Streamlit 中的组员标注只用于准确率验证和训练反馈，不执行正式送审。

正式流程在答疑中台“候选价值复核”完成复核，再点击“批量送审至知识库管理”。Qwen3 查重后只创建知识库待审核知识，不自动发布。

## 发布前检查

正式交付前执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_release.ps1 `
  -BuildPackage -Version 20260722-ai-handoff-v16
```

只有脚本返回`status=passed`时才能交付。该脚本同时验证逐条失败隔离、Qwen3不可用拦截、无标准字段门禁、图例兼容、前端语法、Compose和交付包敏感文件。
也可以双击`发布前验收并打包.cmd`执行同一流程。

运行治理常用命令：

```powershell
# 聚合运行成功率、耗时、降级率、模型成本和SLA
.\.venv\Scripts\python.exe -m answer_hub.cli operations-report

# 从最近检查点恢复失败运行
.\.venv\Scripts\python.exe -m answer_hub.cli retry-run --run-id "<运行ID>"

# 预览30天前的运行目录，增加 --execute 后才会删除
.\.venv\Scripts\python.exe -m answer_hub.cli retention-cleanup --days 30
```

完整说明见 `OPERATIONS_RUNBOOK.md`。

## 本次待办

1. 当前正式聚类数据集为 `data\质检答疑案例库 (4).xlsx`，共379条、覆盖10个产品类型；旧的100条手机脱敏集仅保留作回归测试。
2. 已重新生成当前数据集的60条聚类A/B样本和人工标注工作簿：

```text
outputs\cluster-ab-current-379\sample_60.json
outputs\cluster-ab-current-379\当前379条数据_60对聚类人工标注.xlsx
```

3. 下一步是在人工标注工作簿或“聚类验证”页完成边界样本标注，观察错误合并和错误拆分。
4. `cluster-ab-current-379` 必须使用新的 MiMo 缓存和结果文件，不得复用旧 `cluster-ab-test-60` 缓存。
5. 用真实数据统计“模型语义分类”和旧一级/二级分类的偏差，确认标签枚举是否需要收敛。
6. 基于人工反馈调 `mimo.py` 的会话语义标注 Prompt；先调提示词和标签体系，暂不训练小模型。
7. 聚类稳定后，再优化“流程方法 / 具体判定”标注和主题级知识转写。

## 安全与包内容

交接包不包含 `.env`、任何 API Key、数据库文件、真实会话 Excel、Docker 缓存、`.venv`、运行日志和 `outputs` 结果。真实数据必须保持脱敏。
