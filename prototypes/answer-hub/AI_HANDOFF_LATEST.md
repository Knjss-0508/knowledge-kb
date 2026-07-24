# 答疑中台知识库：最新 AI 交接说明

更新时间：2026-07-24  
当前发布版本：`0.2.0`  
本文是另一台电脑或另一位 AI 接手时的**唯一优先入口**。

## 1. 当前结论

项目当前主链路已经统一为：

```text
第二部分脱敏会话 Excel / 自动化上传
-> 会话语义标注
-> 拆分 1～3 个原子问题
-> 1～N 主题聚类
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

默认聚类模式是`direct_mimo`，直接使用 MiMo 完成原子问题拆分和 1～N 聚类。`semantic_mimo`、`semantic`、`rule`只作为降级或对照模式，不是当前默认生产链路。

## 2. 当前有效架构

```text
┌─────────────────────────────────────────────────────────────┐
│ 输入：已脱敏 Excel / Dify 文件上传                         │
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
│ CZ 知识库 :8000                                            │
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

旧的`核心问题`、`判定结论`、`判定依据`、`一级分类`、`二级分类`仅保留为弱参考和审计字段，不再作为当前主链路的硬门禁。

### 阶段 3：原子问题与主题聚类

1. MiMo 对每条会话生成语义标签。
2. 每条会话拆分 1～3 个原子问题。
3. `direct_mimo`按产品和问题语义执行 1～N 聚类。
4. 模型异常时按配置降级，并在运行摘要中记录实际生效模式。

当前首批产品类型：

```text
手机、平板、笔记本、相机机身、相机镜头、
耳机、手表、游戏机、手写笔、学习机
```

配置文件：`src\answer_hub\product_categories.json`

### 阶段 4：主题价值与问题分类

聚类完成后，先标注：

- 问题分类：质检标准、质检流程、案例解析或课外常识。
- 是否值得沉淀：值得沉淀或不值得沉淀。

只有值得沉淀的主题进入知识转写。不值得沉淀主题保留审计和人工价值复核记录，但不生成正式知识草稿。

### 阶段 5：主题级知识转写

当前候选固定为 10 项：

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
关键词
```

知识必须基于主题的多条证据生成，不能把单个案例结论外推为通用结论。图片依赖无法验证时必须阻止提交。

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

地址：`http://127.0.0.1:8000`

基础`docker-compose.yml`已经包含 PostgreSQL、Redis、CZ 和 CPU 版 Qwen3 Embedding。GPU 环境才叠加`docker-compose.embedding-gpu.yml`。

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
MIMO_BASE_URL=
MIMO_MODEL=

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
```

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
4. 根据人工反馈优先调整`src\answer_hub\mimo.py`中的标签体系和 Prompt，暂不训练小模型。
5. 聚类稳定后，再优化“流程方法 / 具体判定”标注和主题级知识转写。
6. Dify、Docker、真实 MiMo 和真实 CZ 密钥相关的线上联调必须在新电脑本机完成。

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
