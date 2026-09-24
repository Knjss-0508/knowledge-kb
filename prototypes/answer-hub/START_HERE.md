# 答疑中台知识库交接包

更新日期：2026-08-06

> 交给另一台电脑或另一位 AI 继续工作时，先阅读
> `AI_HANDOFF_LATEST.md`。旧的`PROJECT_HANDOFF.md`和历史`handoff`包不再作为当前依据。

## 当前目标

本项目已经把第三部分与 CZ 原生“候选价值复核”和知识库管理接通。Streamlit 仅用于验证各阶段准确性，不是真实上线入口。系统不会自动发布知识。

## 本次关键变更

第二部分会由人工重点校正 `核心问题`、`产品类型` 和 `判定结论`。这三个字段现在与完整聊天、原子问题共同作为聚类证据：产品类型是硬边界，核心问题和判定结论用于补充本轮未读取图片时人工已确认的对象、现象和结论。若结构化字段与聊天冲突，必须进入人工复核。`判定依据`、`一级分类`、`二级分类`仍只作为弱参考和审计字段。

当前推荐链路：

```text
第二部分接口定时拉取 / 已脱敏 Excel 自动化上传
-> Answer Hub 持久化队列
-> 完整聊天内容 + 人工校正核心问题/产品类型/判定结论 + 历史实际回复
-> MiMo 会话语义标注
-> 单主题保留1个原子问题；仅多主题拆成2～3个；不确定最多保留1个暂定问题
-> MiMo 对原子问题进行 1～N 主题聚类
-> 聚类准入门禁：仅高置信、无风险、同业务层级且同品类的主题进入下一步
-> 增量主题归并：在同业务层级、同品类历史主题中复用稳定主题ID并追加原工单和证据
-> 低置信、降级、冲突或模型要求复核的主题进入人工聚类复核
-> 主题问题分类（质检标准、质检流程、案例解析、课外常识、不确定）+ 是否值得沉淀
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

完整自动化默认启用聚类准入门禁。默认阈值为`0.75`，清晰的单成员主题可以放行；
单成员不是拦截条件。多成员主题必须具备独立聚类置信度。任一成员存在原子复核、
聚类复核、人工优先复核、规则降级、失败、冲突拆分或品类/业务层级不一致时，不调用
知识分类、沉淀价值、知识转写和内容初审模型，统一写入`pending_cluster_rows`等待
人工确认。

正式自动化在聚类准入通过后默认启用增量主题库。新主题先与同一`回收业务层级 ->
产品品类`下的历史主题比较；高置信匹配复用原主题ID，幂等追加原始工单ID、原子问题
和来源事实证据。不同业务层级、不同品类、同一工单的不同原子问题、不同明确机型/
品牌/阈值边界，以及`separate_by_phenomenon`规则下的不同现象值不得自动合并。
模糊历史匹配进入`pending_historical_topic_review`，不会调用后续主题模型。未启用
聚类准入的验证调用不写入历史主题库。升级前候选只回填已发布或明确通过高置信聚类
准入的主题，普通`review_pending`候选不会被当作可信历史主题。

第二部分只提供 JSON 接口时，使用
`config\second-part-pull.example.json` 配置 URL、分页响应路径和字段映射，并在
`.env` 中设置 `SECOND_PART_PULL_PROFILE`。现有自动化计划任务会在每轮队列处理前
先执行增量拉取。

术语字典是全流程默认依赖，不需要额外参数。每次自动化、队列、直接工作流和上线后的
服务运行都会先加载代码内置字典，并在 `automation_run.json`、`summary.json` 中记录
`loaded`、条目数、适用品类和稳定版本；字典缺失或损坏时立即停止，不会静默使用无术语
提示词继续生成。

当前固定品类为手机、平板电脑、智能手表、耳机/耳麦、笔记本、游戏机、游戏卡带、单电/微单机身、单反机身、相机镜头、手写笔和学习机。品类由`src\answer_hub\product_categories.json`配置。`适用范围`只能从这12项中选择；品牌和机型分别写入独立字段。当前批量链路不读取或主动生成标准关联；新候选的`关联标准项`默认为空，已有值会保留并单独搁置。

聚类适用范围分为两级：`回收业务层级 -> 产品品类`。当前质检口径属于“自营回收”，
“聚合回收”已作为与自营回收并列的 CZ 适用类目上层预留，但其产品品类和质检口径
尚未接入。不同回收业务层级绝对不能聚类合并；聚合回收当前只做层级隔离并进入人工
优先审核，不会套用自营回收十品类质检规则。业务层级由
`src\answer_hub\business_lines.json`配置。

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
MIMO_API_KEYS=
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5
MIMO_MEDIA_MODEL=mimo-v2.5
MIMO_INPUT_COST_PER_MILLION_TOKENS=0.14
MIMO_OUTPUT_COST_PER_MILLION_TOKENS=0.28

EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_TIMEOUT_SECONDS=120
EMBEDDING_BATCH_SIZE=8
EMBEDDING_MAX_RETRIES=3
```

`MIMO_API_KEYS` 可按逗号或分号填写多个备用 Key。主 Key 余额、额度或鉴权不可用时，
服务会在当前进程内自动切到下一个 Key；不会把 Key 内容写入日志。

当前低成本默认模型为`mimo-v2.5`，文本、图片和视频使用同一个模型。与此前
`mimo-v2.5-pro`相比，请先用60对聚类标注和脱敏图文样本做回归；未完成模型与
Prompt验收前，`AUTO_REVIEW_ENABLED`必须保持关闭。`MIMO_*`缺失时，模型语义
标注可在手动验证流程中回退为规则特征并进入人工优先审核；无人值守自动化会先
停止并等待人工确认，只有显式允许`--continue-on-mimo-unavailable`后才会规则降级。

Qwen3 Embedding已经包含在CZ基础Compose中，不需要另行启动模型进程：

```powershell
.\scripts\start_local_cz.ps1
```

首次启动会下载模型。CPU Compose同时保留两条调用路径：

- CZ后端容器继续使用`http://embedding-qwen:80/v1`；
- 同一台服务器宿主机上的Answer Hub使用`http://127.0.0.1:8080/v1`。

宿主端口只绑定`127.0.0.1`，不允许改成`0.0.0.0`，也不通过公网Nginx代理。

## Streamlit 准确性验证顺序

Streamlit 是本地测试平台，用于检查聚类、价值分类、知识转写和内容质量初标的准确性，不承担正式候选同步和知识库送审。

1. 打开“自动化看板”。
2. 上传已脱敏的方向二会话Excel；当前为无标准引用模式。
3. 处理品类选择“全部”或单个品类；聚类方式默认选择“纯大模型 1～N 聚类”。
4. 查看处理阶段、主题问题分类、沉淀价值、选择性转写和内容质量初标指标；成功后会生成 `topic_review_queue.xlsx`。
5. 打开“审核与反馈”，验证主题来源记录、模型标签、完整聊天、现场图片和12项草稿。
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
6. 已训练第一个本地主题自动标注实验基线
   `topic-label-hash-nb-v1`，同时预测主题问题分类和是否值得沉淀。当前没有人工真值，
   只使用筛选后的 MiMo 伪标签，并为覆盖全部类别显式纳入了部分上游风险样本；
   因此模型强制人工复核、禁止生产自动放行。
7. 下一步完成人工复核工作簿中的主题分类、沉淀价值和训练集选择，再用人工真值重训；
   同时继续根据人工反馈调整 `mimo.py` 的标签体系和 Prompt。
8. 聚类稳定后，再优化“流程方法 / 具体判定”标注和主题级知识转写。

## 安全与包内容

交接包不包含 `.env`、任何 API Key、数据库文件、真实会话 Excel、Docker 缓存、`.venv`、运行日志和 `outputs` 结果。真实数据必须保持脱敏。
