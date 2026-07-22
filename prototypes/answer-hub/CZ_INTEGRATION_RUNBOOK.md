# CZ、本地工作台与第二部分批量链路运行手册

更新日期：2026-07-22

## 1. 当前生产链路

```text
第二部分已脱敏答疑记录
→ POST /api/v1/integration/second-part/records:batch
→ 完整会话、历史实际回复和案例图进入无标准引用工作流
→ 原子问题拆分与 1～N 主题聚类
→ 生成10项候选知识
→ 组员验证或人工例外审核
→ 批量提交 CZ 待审核队列
→ Qwen3 Embedding 查重拦截
→ CZ 人工终审与发布
```

本链路不读取、不检索、不引用质检标准。旧标准关联代码仍保留，但不作为当前批量入口。

10项候选字段为：

```text
知识ID、主标题、副标题、知识内容、图例、推荐回复、
知识分类、关联标准项、适用范围、关键词
```

`关联标准项`字段始终保留。当前无标准流程不主动生成标准关联；新候选默认为空，已有标准关联和来源版本保留，并进入“标准关联搁置”队列。

## 2. Qwen3批量导入拦截规则

CZ 使用 `Qwen/Qwen3-Embedding-0.6B` 对“主标题 + 知识正文”查重：

| 结果 | 默认阈值 | 处理方式 |
|---|---:|---|
| `create` | 小于0.88，或仅有同品类/同模板语义相似但缺少有效文本重合 | 正常进入CZ待审核 |
| `review_duplicate` | 0.88～0.96，且标题和正文具备有效文本重合；或正文存在包含关系 | 进入CZ待审核，但标记为疑似重复拦截 |
| `block_duplicate` | 大于等于0.96且具备有效文本重合，或标题与正文完全相同 | 阻断入库 |

批量接口会分别返回：

- `submitted`：正常进入待审核。
- `intercepted`：Qwen3疑似重复拦截。
- `blocked`：明确重复阻断。
- `reused`：幂等复用。
- `failed`：其他失败。

不得为了提高批量成功率绕过Qwen3查重。

批量适配不会降低完全重复和正文包含关系的拦截能力。它只过滤Qwen3在同品类、同写作模板下产生的高基线相似度，防止“第一条通过、后续全部疑似重复”的链式误拦截。

## 3. 必要配置

根目录工作台`.env`：

```dotenv
KB_BASE_URL=http://127.0.0.1:8000
KB_INTEGRATION_KEY=
KB_TIMEOUT_SECONDS=30
KB_MAX_RETRIES=3
KB_RETRY_BACKOFF_SECONDS=0.5

MIMO_API_KEY=
MIMO_BASE_URL=
MIMO_MODEL=
MIMO_TIMEOUT_SECONDS=60
```

CZ目录`.env`：

```dotenv
INTEGRATION_API_KEY=

MIMO_API_KEY=
MIMO_BASE_URL=
MIMO_MODEL=
MIMO_TIMEOUT_SECONDS=60

THIRD_PART_SOURCE_DIR=
THIRD_PART_STANDARDS_PATH=
THIRD_PART_CLUSTERING_MODE=direct_mimo
THIRD_PART_PRODUCT_TYPE=
ANSWER_HUB_PRODUCT_TAXONOMY_PATH=

EMBEDDING_BASE_URL=http://embedding-qwen:80/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_DIMENSIONS=1024
DEDUP_REVIEW_THRESHOLD=0.88
DEDUP_BLOCK_THRESHOLD=0.96
DEDUP_MIN_TITLE_LEXICAL_SIMILARITY=0.10
DEDUP_MIN_CONTENT_LEXICAL_SIMILARITY=0.08
DEDUP_STRONG_CONTENT_LEXICAL_SIMILARITY=0.30
```

两个系统的集成密钥必须一致，且只能通过环境变量配置。

## 4. 本地部署

### 4.1 首次配置

```powershell
Copy-Item .\cz-knowledge-kb\knowledge-kb-master\.env.example `
  .\cz-knowledge-kb\knowledge-kb-master\.env
```

编辑`.env`，至少填写`INTEGRATION_API_KEY`。真实密钥不得提交到Git。

### 4.2 CPU启动

双击：

```text
启动本地CZ.cmd
```

或执行：

```powershell
.\scripts\start_local_cz.ps1
```

基础`docker-compose.yml`已经包含PostgreSQL、Redis、CZ后端前端和Qwen3 Embedding。

### 4.3 GPU启动

```powershell
.\scripts\start_local_cz.ps1 -Embedding gpu
```

### 4.4 健康检查

```powershell
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

`/ready`必须同时确认数据库和Qwen3 Embedding可用。Embedding不可用时批量导入应返回`DEDUP_UNAVAILABLE`，不能跳过查重。

## 5. 第二部分批量接入

接口：

```http
POST /api/v1/integration/second-part/records:batch
X-Integration-Key: <INTEGRATION_API_KEY>
```

约束：

- 单批最多100条，客户端自动分批。
- 只接收`redaction_status=redacted`。
- 同一业务记录重试必须保持相同`idempotency_key`。
- 首次处理状态为`topic_candidates_generated`。
- 相同幂等键重试返回`reused`，不会重复调用模型。
- 响应中的`knowledge_mode`为`case_only`。
- `standard_references_enabled`固定为`false`。

请求示例见`examples/second_part_batch.example.json`。

## 6. 审核后批量导入

CZ页面“主题审核”提供：

1. 接收第二部分Excel并批量生成候选。
2. 导入已有`topic_review_queue.xlsx`。
3. 人工复标候选。
4. 点击“批量提交待 cz 终审”。

批量接口：

```http
POST /api/v1/topic-candidates/submit-to-cz-review:batch
```

请求：

```json
{
  "candidate_ids": ["tpc-001", "tpc-002"]
}
```

单次最多100个候选ID。接口逐条隔离失败，不会因一条重复导致整批回滚。

## 7. 上线前验证

```powershell
# 第三部分
.\.venv\Scripts\python.exe -m pytest -q

# CZ后端
Set-Location .\cz-knowledge-kb\knowledge-kb-master\backend
..\..\..\.cz_test_venv\Scripts\python.exe -m pytest -q

# Compose语法
Set-Location ..
docker compose config --quiet
```

至少验证：

1. 同一批第二部分数据重试全部返回`reused`。
2. 普通知识返回`submitted`。
3. 疑似重复知识返回`intercepted`。
4. 完全重复知识返回`blocked`。
5. 所有成功项状态均为`review`，不会自动发布。

## 8. 常见问题

- `401/403`：检查两侧集成密钥是否一致。
- `MiMo未配置`：会回退规则草稿并进入重点复核。
- `DEDUP_UNAVAILABLE`：Qwen3 Embedding未就绪，保持原幂等键稍后重试。
- `DUPLICATE_BLOCKED`：明确重复，停止送审并查看命中知识。
- `CATEGORY_NOT_FOUND`：重新选择CZ当前分类。
- 图片无法保存：补充可访问的脱敏案例图，或确认该知识是否确实依赖图片。
