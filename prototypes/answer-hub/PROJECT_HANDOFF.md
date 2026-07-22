# 答疑中台知识库项目交接说明

更新日期：2026-07-14

本文面向没有本次聊天上下文的接手人员，说明项目目标、正确代码位置、当前进度、已知问题和后续工作顺序。

## 1. 项目目标与分工

整体链路分为三段：

```text
方向二数据
-> 第三部分主题沉淀、模型转写、模型初标、人工复标
-> cz 知识库终审与发布
```

本项目当前负责的是第三部分，目标不是把单条工单复述成知识，而是：

```text
方向二记录清洗和特征提取
-> 主题聚类
-> 主题级知识转写草稿
-> 模型初标审核
-> 人工复标
-> 提交 cz 终审
-> cz 发布正式知识
```

角色边界必须保持清楚：

- **主题转写**：模型根据多个同类记录，生成一条可复用的主题级 13 列知识草稿。
- **模型初标**：模型审核知识点是否值得沉淀及转写草稿是否合格，不改写草稿。
- **人工复标**：第三部分审核人员编辑最终 13 列草稿，记录审核结论和训练反馈。
- **cz 终审**：cz 在正式知识库中审核并发布，第三部分不能自动发布。

外观、显示、拆修、功能异常等依赖现场图片的问题，通常沉淀为核验流程，不得把单个案例判定外推为通用结论。只有“坏点/漏液”“磕点/划痕”等有明确标准边界且证据充分的主题，才可沉淀为具体判定。

## 2. 正确项目位置

### 第三部分 Python 工作流

```text
C:\Users\admin\Desktop\答疑中台知识库
```

关键文件：

```text
src\answer_hub\workflow.py        主题聚类、规则兜底、审核队列输出
src\answer_hub\mimo.py            MiMo 主题转写与模型初标 Prompt、JSON 校验
src\answer_hub\cz_integration.py 预留的 cz API 适配层
streamlit_app.py                  本地 Streamlit 原型工作台
```

### cz 正确知识库项目

来源压缩包：

```text
C:\Users\admin\Downloads\knowledge-kb-master (2).zip
```

已解压到：

```text
C:\Users\admin\Desktop\答疑中台知识库\cz-knowledge-kb\knowledge-kb-master
```

技术栈：

```text
FastAPI + SQLAlchemy + PostgreSQL/pgvector + Redis + Vue 3 静态单页
```

该项目提供：

```text
/api/v1/integration/taxonomy
/api/v1/integration/knowledge-dedup:check
/api/v1/integration/knowledge-candidates:batch
```

这些接口是第三部分后续直连送审的正确目标。

### 误用的网站

```text
C:\laragon\www\kb-system
```

这不是 cz 的知识库项目，不应继续在其中开发或部署第三部分。

此前曾误在该目录新增过主题审核相关页面、PHP API 和 SQLite 表；这些内容不属于当前目标，未用于正式流程。后续应在确认目录与备份后单独清理，避免影响那个独立网站。

## 3. 第三部分 Python 工作流状态

已实现的设计：

```text
单条记录清洗
-> 结构化主题特征
-> 混合聚类
-> 主题级标准检索
-> 主题级 MiMo/规则转写
-> 模型初标
-> topic_review_queue.xlsx
```

关键门禁：

- 只处理手机记录。
- 缺核心问题、判定结论、判定依据、一级/二级分类的记录进入排除队列。
- 无聊天内容且无有效图片证据的记录进入 `evidence_gap_rows`。
- 同主题有效证据少于 2 条的记录进入 `pending_cluster_rows`，不进入人工审核。
- 只检索生效标准；没有可信标准时不伪造标准引用。
- 无可信标准、证据不足、疑似/无法判断等情况，强制生成“流程方法”候选并标记重点复核。
- 转写和模型初标是两次独立调用；模型初标不会覆盖 13 列草稿。

最终 13 列字段：

```text
主标题
副标题
知识内容
知识分类
知识来源
关联标准项
适用范围
生效状态
来源版本
变更类型
失效原因
检索关键词
校验备注
```

最近一份主题工作簿：

```text
outputs\topic-workbench\20260714-164022\topic_review_queue.xlsx
```

该工作簿包含：

```text
topic_review_queue      主题候选、模型初标和最终 13 列草稿
topic_model_drafts      原始主题转写草稿
topic_source_mapping    主题与来源记录的追溯映射
evidence_gap_rows       证据缺口记录
pending_cluster_rows    样本不足、暂不审核的主题
excluded_rows           字段缺失或不符合范围的记录
```

注意：上传到 cz 主题审核模块的是 `topic_review_queue.xlsx`，不是旧版按单条工单生成的 `review_queue.xlsx`。

## 4. 已接入 cz 源码的第三部分模块

以下改动已经写入正确的 cz 项目源码，但尚未实际部署或执行数据库迁移。

新增文件：

```text
backend\app\models\topic_review.py
backend\app\schemas\topic_review.py
backend\app\routes\topic_review.py
backend\migrations\versions\20260714_06_topic_review_workbench.py
docs\third-part-topic-review.md
```

已修改文件：

```text
backend\requirements.txt
backend\app\main.py
backend\app\routes\auth.py
frontend\index.html
```

功能说明：

1. 新增独立表：

```text
topic_candidates
topic_review_events
```

它们与正式 `knowledge_items` 分离，保存主题元数据、转写原稿、模型初标、人工最终草稿和操作审计。

2. 新增 API：

```text
GET  /api/v1/topic-candidates/stats
GET  /api/v1/topic-candidates
GET  /api/v1/topic-candidates/{id}
POST /api/v1/topic-candidates/import
POST /api/v1/topic-candidates/{id}/review
POST /api/v1/topic-candidates/{id}/submit-to-cz-review
```

3. 新增页面入口：

```text
cz 网站左侧导航 -> 主题审核
```

页面包含：

```text
左侧：主题候选队列、状态和重点复核标记
中间：主题转写草稿、模型初标、证据摘要和标准 Top5
右侧：人工完整编辑 13 列、审核反馈、cz 分类和层级映射
```

4. 人工审核通过后：

```text
提交 cz 终审
-> 创建正式 knowledge_items 中的一条 review 状态知识
-> 返回原有“知识库管理”页面，由 cz 审核发布
```

不会自动发布。

5. 权限：

```text
junior_support：查看和人工复标
senior_support / super_support：导入、人工复标、提交 cz 终审
super_admin：全部权限
```

## 5. 部署现状与阻塞

当前电脑没有可用的 `docker` 命令，正确 cz 项目尚未启动。

因此当前不能完成：

- PostgreSQL 数据库迁移。
- 导入真实 `topic_review_queue.xlsx`。
- 浏览器端页面验收。
- cz 正式审核和发布流程验证。

Docker 安装后，建议使用 CPU embedding 组合启动：

```powershell
Set-Location "C:\Users\admin\Desktop\答疑中台知识库\cz-knowledge-kb\knowledge-kb-master"
Copy-Item .env.example .env
docker compose -f docker-compose.yml -f docker-compose.embedding-cpu.yml -p knowledge-kb up -d --build
```

启动后检查：

```text
http://127.0.0.1:8000/login
http://127.0.0.1:8000/docs
http://127.0.0.1:8000/ready
```

初始管理员账号信息由 cz 项目自己的 `docs\deploy.md` 提供。首次登录后必须立即修改密码；不要把密码写入工作簿、代码、日志或聊天内容。

首次运行时，Docker 还需要下载 PostgreSQL、Redis、Embedding 服务镜像和模型，网络不可用会导致启动失败。

## 6. 验证状态

已完成：

- 新增 cz 后端 Python 文件通过 `py_compile` 语法检查。
- 新增前端调用名称与后端路由名称已核对。
- 第三部分主题审核说明文档已写入 cz 项目。

未完成：

- Docker/Compose 运行验证。
- Alembic 数据库迁移验证。
- `openpyxl` 真实 Excel 导入验证。
- 浏览器端同屏审核、权限和终审提交验证。
- 主题候选提交后的正式知识查重和发布验证。

当前 Python 工作流测试命令失败的原因是本地 `.venv` 缺少项目依赖：`answer_hub` 未按可编辑模式安装，且 `openpyxl` 未安装。这是环境依赖问题，不是已实现工作流的业务逻辑结论。

## 7. 后续建议执行顺序

1. 安装 Docker Desktop。
   - Windows 11 专业版可使用 WSL 2 或 Hyper-V 后端。
   - 推荐 WSL 2；当前 WSL 下载曾遇到网络连接失败，可在网络正常时继续处理。

2. 启动正确 cz 项目。
   - 先复制 `.env.example` 为 `.env`。
   - 设置安全的数据库密码和 `INTEGRATION_API_KEY`。
   - 用 CPU embedding Compose 覆盖文件启动。

3. 验收主题审核模块。
   - 登录。
   - 打开“主题审核”。
   - 导入最近的 `topic_review_queue.xlsx`。
   - 检查转写草稿、模型初标和人工复标是否同屏。
   - 保存“修改后通过”，选择 cz 分类及 L2，点击“提交 cz 终审”。
   - 进入“知识库管理”，检查生成的知识为 `review` 状态。
   - 使用 cz 原有审核按钮发布。

4. 建立多人账号。
   - 在 cz 的“账号管理”中创建第三部分审核人员账号。
   - 根据实际职责分配 `junior_support`、`senior_support`、`super_support`。

5. 再做 Python 到 cz API 的直连。
   - 当前阶段先通过页面导入工作簿完成审核验证。
   - 稳定后再在 `src\answer_hub\cz_integration.py` 中接入 taxonomy、预查重和批量送审接口。
   - API 直连必须使用 `.env` 中的 `KB_BASE_URL` 和 `KB_INTEGRATION_KEY`，不得写入 Excel、日志或前端。

6. 建立正式评估基线。
   - 先积累 20 至 50 条人工复标主题。
   - 按标准引用一致率、分类一致率、标题/正文修改率、驳回率、重点复核率比较 Prompt 或模型版本。
   - 在此之前不建议进行模型微调；优先优化标准检索、主题聚类、转写 Prompt 和模型初标 Prompt。

## 8. Prompt 与模型维护位置

第三部分 Prompt 位置：

```text
src\answer_hub\mimo.py
_build_topic_prompt()          主题转写 Prompt
_build_topic_review_prompt()   模型初标 Prompt
PROMPT_VERSION
TOPIC_REVIEW_PROMPT_VERSION
```

修改原则：

```text
一次只改一个阶段
-> 增加 Prompt 版本号
-> 用同一批人工金标主题回归
-> 比较安全门禁和人工修改率
-> 再决定是否上线
```

当前 MiMo 客户端使用 OpenAI 兼容接口。更换模型时可先替换本机 `.env` 的模型地址、模型名和 Key；若转写和初标需要使用不同模型，再拆分为两组配置和两个客户端。

## 9. 安全原则

- MiMo Key、Integration Key、Cookie、数据库密码仅保存在 `.env`。
- 不将原始聊天全文、手机号、订单号、地址等隐私数据写入 cz 知识库。
- 工作簿只保留脱敏证据摘要、来源记录 ID、工单 ID、标准版本和模型运行信息。
- 第三部分只生成候选和提交终审，不自动发布知识。
