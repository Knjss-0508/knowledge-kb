# 答疑中台知识库：最新 AI 交接说明

更新时间：2026-08-06
当前发布版本：`0.2.0`  
本文是另一台电脑或另一位 AI 接手时的**唯一优先入口**。

## 1. 当前结论

项目当前主链路已经统一为：

```text
第二部分脱敏会话 Excel / 自动化上传
-> 会话语义标注
-> 单主题保留1个原子问题；仅多主题拆成2～3个；不确定最多保留1个暂定问题
-> 1～N 主题聚类
-> 高置信聚类准入；低置信和风险聚类进入人工聚类复核
-> 高置信主题与同业务层级、同品类历史主题执行增量归并
-> 主题问题分类与是否值得沉淀标注
-> 仅对值得沉淀主题进行知识转写
-> 模型初标转写内容质量
-> 同步答疑中台候选价值复核
-> 人工复核
-> 批量送审至知识库管理
-> Qwen3 查重拦截
-> CZ 人工终审
-> 人工发布
```

当前是**无标准案例知识模式**：

- 主证据是完整脱敏会话、历史实际回复和脱敏案例图。
- 不主动读取、检索、生成或引用质检标准。
- 新候选的`关联标准项`默认为空。
- 已有标准关联、来源版本或正文标准引用不删除，进入“标准关联搁置”。
- 自动化只能创建 CZ 的`review`待审核知识，不能自动发布。
- 术语字典是全流程默认依赖。自动化、队列、直接工作流和部署后的服务都会先加载
  代码内置字典，并在运行记录中保存字典版本、条目数、分类和适用品类；字典缺失或
  损坏时停止生成，不会静默使用没有术语参考的提示词。

默认聚类模式是`direct_mimo`，直接使用 MiMo 完成原子问题拆分和 1～N 聚类。`semantic_mimo`、`semantic`、`rule`只作为降级或对照模式，不是当前默认生产链路。

当前低成本默认模型已从`mimo-v2.5-pro`调整为`mimo-v2.5`，文本和图文任务统一
使用该模型。内部模式名`direct_mimo`保持不变。正式启用自动审核前，仍需在现有
60对聚类标注和脱敏图文样本上重新验证模型与Prompt版本。

## 2. 当前有效架构

```text
┌─────────────────────────────────────────────────────────────┐
│ 输入：第二部分接口定时拉取 / 已脱敏 Excel / Dify 上传      │
└──────────────────────────┬──────────────────────────────────┘
                           v
┌─────────────────────────────────────────────────────────────┐
│ Answer Hub                                                 │
│ 清洗 -> MiMo 标注 -> 原子问题 -> 聚类 -> 转写 -> 初审      │
│ 验证入口：Streamlit :8501（只验证准确性，不用于正式送审）  │
│ 自动入口：Automation API :8780 + Windows 计划任务          │
└──────────────────────────┬──────────────────────────────────┘
                           v
┌─────────────────────────────────────────────────────────────┐
│ CZ 知识库宿主机 :8801（容器内 :8000）                       │
│ 候选价值复核 -> 人工批量送审 -> Qwen3查重 -> review待审核  │
│ 人工终审后才允许发布                                        │
└─────────────────────────────────────────────────────────────┘

可选编排入口：
Dify :8080 -> Answer Hub API :8780 -> 自动化队列
```

### 2.1 Streamlit 准确性验证模式

适合首次验证、边界样本调试和组员标注，不是真实上线入口：

```text
Streamlit 上传 Excel
-> 生成 topic_review_queue.xlsx
-> 审核与反馈
-> 标注“是否值得沉淀 / 是否可用 / 如何修改 / 问题反馈”
-> 计算准确率并导出验证反馈
```

正式候选由服务端调用
`POST /api/v1/integration/knowledge-review-candidates:batch`
同步到答疑中台“候选价值复核”，不得通过 Streamlit 直接送审。

### 2.2 无人值守模式

适合 Dify 或其他系统提交文件：

```text
POST Answer Hub 自动化任务
-> data/automation-queue/pending
-> Windows 计划任务扫描
-> 自动运行全流程
-> 严格模型审核策略
-> 可选同步 CZ 候选价值复核
```

只有同时满足以下条件，模型自动审核才可以把候选直接标为 CZ `ready`：

- `AUTO_REVIEW_ENABLED=true`
- 已配置并验证`AUTO_REVIEW_VALIDATED_MODEL`
- 已配置并验证`AUTO_REVIEW_VALIDATED_PROMPT_VERSION`
- 候选通过代码中的生产门禁

条件不满足时仍可同步到 CZ，但必须保持`pending`并进入人工复核；
不得直接创建知识或绕过审核。

第二部分仅提供 JSON 接口时，使用 profile 驱动的拉取器：

```text
SECOND_PART_PULL_PROFILE
-> answer-hub second-part-pull
-> data/automation-queue/pending
-> automation-queue
```

profile 样例为 `config/second-part-pull.example.json`。cursor 默认保存在
`data/second-part-pull/state.json`；成功入队后才推进，重复批次通过批次指纹复用。

## 3. 当前完整处理流程

### 阶段 1：输入与安全检查

1. 输入必须是已脱敏会话。
2. 正式当前数据集是`data\质检答疑案例库 (4).xlsx`，共 379 条，覆盖 10 个产品类型。
3. 旧`data\聚类样本_手机_100条_脱敏_2026-07-16.xlsx`仅用于回归测试。
4. `.env`、真实业务数据、数据库、运行输出和模型缓存不得进入交付包。

### 阶段 2：会话理解

输入主证据：

- 完整聊天内容
- 历史实际回复
- 脱敏案例图

第二部分人工校正后的`核心问题`、`产品类型`和`判定结论`需要与完整聊天、原子问题共同用于聚类。产品类型是硬门禁；核心问题和判定结论可补充本轮未读取图片时人工已确认的对象、现象和结论。字段与聊天冲突时进入人工复核。`判定依据`、`一级分类`、`二级分类`仍只作为弱参考和审计字段。

### 阶段 3：原子问题与主题聚类

1. MiMo 对每条会话生成语义标签。
2. `single_topic`只保留1个原子问题，不重复拆写。
3. 只有`multi_topic`才拆成2～3个原子问题；`uncertain`最多保留1个暂定问题。
4. `direct_mimo`按产品和问题语义执行 1～N 聚类，并返回主题聚类置信度和是否需要复核。
5. 完整自动化只放行置信度不低于`0.75`且无风险标记的聚类。不同回收业务层级、
   不同产品品类、原子低置信、聚类低置信、规则降级、调用失败、冲突拆分或模型要求
   复核的主题进入`pending_cluster_rows`，不调用知识分类、沉淀价值、转写和内容初审模型。
6. 清晰的单成员主题允许放行；单成员本身不是聚类错误或拦截条件。
7. 聚类准入通过后，系统读取持久化主题库。高置信历史匹配复用原主题ID并追加原始
   工单ID和事实证据；模糊匹配进入`pending_historical_topic_review`。不同业务层级、
   不同产品品类、同工单不同原子问题、明确范围/阈值冲突和必须按现象值拆分的主题
   绝对不能自动归并。未启用聚类准入的验证调用不会污染历史主题库。

增量主题库保存在Answer Hub审计数据库的`topic_registry`、`topic_members`和
`topic_merge_events`表。重复导入同一原子证据不会增加成员或证据版本。首次升级时，
只自动回填已发布或带“已自动放行”及有效准入置信度的旧候选；普通待审核候选仍需
人工确认。

当前固定产品类型：

```text
手机、平板电脑、智能手表、耳机/耳麦、笔记本、游戏机、
游戏卡带、单电/微单机身、单反机身、相机镜头、手写笔、学习机
```

配置文件：`src\answer_hub\product_categories.json`

### 阶段 4：主题价值与问题分类

通过聚类准入后，才标注：

- 问题分类：质检标准、质检流程、案例解析、课外常识或不确定。
- 是否值得沉淀：值得沉淀或不值得沉淀。

只有值得沉淀的主题进入知识转写。不值得沉淀主题保留审计和人工价值复核记录，但不生成正式知识草稿。

2026-08-05 已增加第一个本地实验模型`topic-label-hash-nb-v1`，同时预测上述两个标签。
当前人工真值为0条，首版只使用筛选后的 MiMo 伪标签，并为覆盖全部类别显式纳入部分
上游风险样本。该模型仅用于验证训练、预测和反馈闭环，所有结果强制人工复核，
`production_eligible=false`，不得接入生产自动审核、送审或发布。

### 阶段 5：主题级知识转写

当前候选固定为 12 项：

```text
知识ID
主标题
副标题
知识内容
图例
推荐回复
知识分类
关联标准项
适用范围
适用品牌
适用机型
关键词
```

`适用范围`只能使用上述12个产品品类本身，例如`手机`、`笔记本`、`平板电脑`，不得出现任何
后缀。来源事实明确限制品牌或具体机型时，分别填写`适用品牌`、`适用机型`；找不到
来源时必须留空，不得推测。

旧名称`平板`、`手表`、`耳机`可以归一为对应正式名称；旧`相机机身`无法证明属于
微单还是单反，必须进入人工确认，不得静默选择任一品类。

知识必须基于主题的多条证据生成，不能把单个案例结论外推为通用结论。图片依赖无法验证时必须阻止提交。

2026-08-06 起，转写前先建立统一来源事实证据包。每条事实保留事实ID、来源记录ID、
人工核心问题、人工判定结论、聊天、历史实际回复和案例图；转写输入使用代表性事实
选择替代固定前5条。规则降级正文也必须保留人工判定结论。知识图例只能从当前主题
代表性事实对应的案例中选取，并以CZ富文本图片块同步；事实引用和图片来源映射保留在
主题审核字段及CZ的`evidence_excerpt`中。正文和推荐回复的每条实质陈述都必须由同一
来源事实或已命中真实标准支持；无来源的阈值、对象、操作、原因、范围或判定写入
`主题无来源内容`，由确定性审核门禁强制改为“需修改”，模型初审不得覆盖该结果。
CZ送审还会核验案例图追踪中的事实ID和来源记录ID确实属于当前候选。

### 阶段 6：内容质量初标与候选价值复核

知识转写后的模型初标只判断内容质量，不重新判断沉淀价值，至少检查：

- 标题质量
- 内容与证据一致性
- 证据充分性
- 图片必要性
- 风险和修改建议

候选随后同步到答疑中台“候选价值复核”，人工补充：

- 是否值得沉淀
- 是否可用
- 如何修改
- 问题反馈

明确“不值得沉淀”或未达到生产自动审核条件的候选不得直接创建知识；
它们可以进入 CZ 候选价值复核队列，等待人工处理。

### 阶段 7：批量送审与 Qwen3 查重

人工复核后点击“批量送审至知识库管理”。批量接入逐条隔离事务，单条失败不会回滚整批。

查重动作：

| 动作 | 结果 |
|---|---|
| `create` | 创建 CZ 待审核知识 |
| `review_duplicate` | 创建待审核知识，并标记疑似重复 |
| `block_duplicate` | 阻止入库 |

Qwen3 使用标题和正文做语义查重，并增加有效文本重合门禁，避免“相同流程模板、不同问题对象”被整批误拦截。完全重复、正文包含和内容哈希一致仍会被拦截。

### 阶段 8：CZ 终审与发布

所有自动化结果只进入`review`状态。最终发布必须由 CZ 人工完成。

## 4. 三种启动方式

### 4.1 安装环境

要求 Python 3.11 或更高版本。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -m pytest -q
```

真实密钥只能写入本机`.env`或环境变量。

### 4.2 启动 Streamlit

```powershell
.\start_streamlit.ps1
```

地址：`http://localhost:8501`

Windows 也可双击`启动自动化看板.cmd`。

### 4.3 启动本地 CZ

```powershell
Copy-Item .\cz-knowledge-kb\knowledge-kb-master\.env.example `
  .\cz-knowledge-kb\knowledge-kb-master\.env
.\scripts\start_local_cz.ps1
```

地址：`http://127.0.0.1:8801`

启动脚本会组合基础`docker-compose.yml`、本地 PostgreSQL
`docker-compose.local.yml`和对应CPU/GPU的Qwen3 Embedding覆盖文件。

### 4.4 启动 Dify + 自动化 API

```powershell
.\scripts\start_dify_answer_hub.ps1
```

或双击`启动Dify平台.cmd`。

地址：

```text
Dify：http://localhost:8080
Answer Hub API：http://127.0.0.1:8780/health
```

Dify 导入：`config\dify-answer-hub-openapi.json`

## 5. 关键配置

只记录变量名，不在文档中记录真实值：

```dotenv
MIMO_API_KEY=
MIMO_API_KEYS=
MIMO_BASE_URL=https://api.xiaomimimo.com/v1
MIMO_MODEL=mimo-v2.5
MIMO_MEDIA_MODEL=mimo-v2.5
MIMO_INPUT_COST_PER_MILLION_TOKENS=0.14
MIMO_OUTPUT_COST_PER_MILLION_TOKENS=0.28

ANSWER_HUB_API_KEY=
ANSWER_HUB_API_HOST=0.0.0.0
ANSWER_HUB_API_PORT=8780
ANSWER_HUB_AUTOMATION_USE_MIMO=true
ANSWER_HUB_AUTOMATION_CLUSTERING_MODE=direct_mimo
# 同步全部候选到 CZ 候选价值复核，不直接建知识
ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW=false
# 旧变量，仅作兼容；新变量优先
ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ=false

AUTO_REVIEW_ENABLED=
AUTO_REVIEW_VALIDATED_MODEL=
AUTO_REVIEW_VALIDATED_PROMPT_VERSION=

KB_BASE_URL=
KB_INTEGRATION_KEY=

SECOND_PART_PULL_PROFILE=
SECOND_PART_PULL_STATE=data/second-part-pull/state.json
SECOND_PART_PULL_MAX_PAGES=10
SECOND_PART_API_TOKEN=
```

`MIMO_API_KEYS` 为逗号或分号分隔的备用 Key。余额不足、额度耗尽或鉴权失败时自动
切换；普通限流仍使用当前 Key 重试。运行指标只记录 `model_key_switches` 次数，
不得记录实际 Key。成本配置使用美元/百万Token，仅用于本地估算；当前填写的是
`mimo-v2.5`未缓存输入和输出的官方价格。

完整变量说明以`.env.example`为准。

## 6. 接手时优先查看的文件

### 主流程

```text
src\answer_hub\workflow.py
src\answer_hub\mimo.py
src\answer_hub\automation.py
src\answer_hub\automation_queue.py
src\answer_hub\automation_api.py
src\answer_hub\auto_review.py
src\answer_hub\cz_integration.py
src\answer_hub\operations.py
streamlit_app.py
```

### CZ

```text
cz-knowledge-kb\knowledge-kb-master\backend\app\routes\integration.py
cz-knowledge-kb\knowledge-kb-master\backend\app\routes\topic_review.py
cz-knowledge-kb\knowledge-kb-master\backend\app\services\knowledge_dedup.py
cz-knowledge-kb\knowledge-kb-master\backend\app\services\qc_standards.py
cz-knowledge-kb\knowledge-kb-master\frontend\index.html
cz-knowledge-kb\knowledge-kb-master\backend\migrations\versions\20260722_07_operational_governance.py
```

### 说明与验证

```text
START_HERE.md
CHANGE_SUMMARY.md
DIFY_SETUP.md
OPERATIONS_RUNBOOK.md
automation-api-reference.md
ACCEPTANCE_CHECKLIST.md
scripts\verify_release.ps1
scripts\build_delivery_package.ps1
```

## 7. 当前未完成工作

1. 已为 379 条正式数据重新生成 60 对聚类人工标注工作簿：
   `outputs\cluster-ab-current-379\当前379条数据_60对聚类人工标注.xlsx`。
2. 该工作簿仍需完成人工边界标注，重点观察错误合并和错误拆分。
3. 需要用真实结果统计“模型语义分类”与旧一级/二级分类的偏差。
4. 已完成首个主题分类与沉淀价值本地实验模型，但没有人工真值，不能作为生产准确率结论。
5. 完成`最新版聚类结果_主题分类与沉淀价值_人工复核.xlsx`中的人工标签和训练集选择，
   再用人工真值重训并按品类验收；同时继续根据人工反馈调整`mimo.py`的标签体系和 Prompt。
6. 聚类稳定后，再优化“流程方法 / 具体判定”标注和主题级知识转写。
7. Dify、Docker、真实 MiMo 和真实 CZ 密钥相关的线上联调必须在新电脑本机完成。

## 8. 已废弃或不要继续使用的内容

以下内容不得作为当前实现依据，也不进入最新交付包：

- 2026-07-14 的`PROJECT_HANDOFF.md`。
- 旧`PACKAGE_CONTENTS.md`。
- `handoff`目录中的历史 v1～v15 交接包。
- `C:\laragon\www\kb-system`误用网站。
- 旧的“标准检索 + 13 列知识主表”作为默认主链路。
- 旧`review_queue.xlsx`单工单流程；当前使用`topic_review_queue.xlsx`。
- 把旧一级/二级分类、核心问题、判定结论、判定依据作为硬门禁。
- 把 100 条手机样本当作正式聚类数据。
- 单独启动宿主机 8080 端口 CPU Embedding 作为 CZ 默认方案。
- 复用`outputs\cluster-ab-test-60`的旧缓存或结果。

注意：`semantic_mimo`、`semantic`、`rule`仍是代码支持的降级/对照模式，不属于删除项，但不要替代`direct_mimo`成为默认生产模式。

## 9. 新电脑验收顺序

1. 解压最新交付包。
2. 先读本文，再读`manifest.json`和`CHANGE_SUMMARY.md`。
3. 根据`.env.example`新建本机`.env`，不要复制旧电脑密钥。
4. 安装 Python 环境并运行测试。
5. 执行发布验收：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File .\scripts\verify_release.ps1 `
  -PackagePath "<最新交付包 zip 的绝对路径>"
```

6. 先启动 Streamlit 做无密钥界面检查。
7. 配置 Docker 后启动 CZ，验证`/ready`和 Qwen3。
8. 最后配置 MiMo、CZ 和 Dify 密钥，执行真实脱敏样本端到端验证。

## 10. 安全边界

最新交付包必须排除：

```text
.env
API Key / 密码
data
outputs
数据库文件
真实 Excel / CSV
日志
模型缓存
.venv / node_modules
历史交接包
```

交付包通过`manifest.json`记录版本，通过`checksums.sha256`校验文件完整性。当前工作目录的`.git`不可用，因此`git_commit`可能为空，应以压缩包文件名、构建时间、版本号和校验和为准。
