# 答疑中台知识库：模型成本与适配性复核

查询日期：2026-08-10
研究范围：中文结构化抽取、主题分类、聚类裁决、知识转写和带图证据分析；仅使用厂商官方文档及本仓库代码。未读取 `.env` 实际值，未调用真实模型 API，也未修改生产配置。

## 先给结论

当前最值得做的是：**在同一批脱敏样本上，验证通义 `qwen3.7-flash` 是否能替代 `mimo-v2.5`，同时给两者关闭不必要的思考和限制输出。**

北京地域的官方按量价格中，`mimo-v2.5` 的未命中缓存输入/输出为 `¥1` / `¥2`（每百万 Tokens）；`qwen3.7-flash` 在单次输入不超过 32K Tokens 时为 `¥0.2` / `¥0.8`。以每次 8,000 输入、1,000 输出 Tokens 的相同文本负载计算，前者约 `¥0.010`、后者约 `¥0.0024`，后者的 Token 费用约低 76%。本项目当前的 24,000 字符原子问题批次通常有机会落入该档位，但必须以实际 `usage` 为准；超过 32K 后百炼会进入更高价格档。[M2][Q1][Q4][P2]

同时，客户端没有显式关闭 MiMo 默认开启的思考模式，也没有限制最大完成 Tokens；MiMo V2.5 的默认最大输出是 32,768 Tokens。对于只需要受字段约束 JSON 的任务，长推理和长输出很可能也是单次花费高的主要原因。[M1][M3][P1]

推荐按下面顺序验证：

1. **先做单模型 A/B（推荐）**：比较 `mimo-v2.5` 与 `qwen3.7-flash`，两组均显式关闭思考模式并设置同样的任务级输出上限；保持 JSON 校验、重试、人工审核、Qwen3 查重和 CZ 终审不变。
2. **若 Qwen3.7 Flash 质量达标，将它作为默认模型**：它可用一个模型覆盖文本、图片和视频，支持结构化输出和 OpenAI 兼容接口，并且在输入不超过 32K Tokens 时的官方价格显著低于 MiMo。[Q1][Q2][Q3][Q4]
3. **仅在完成多供应商路由后考虑 `deepseek-v4-flash`**：文本 Token 单价与当前 MiMo 标准版相同，但官方说明不支持图片/视频，不能直接替换本项目的完整图文链路。[D1][D2][D3]
4. **不把 OpenAI GPT-5.4 nano 作为降本替代**：官方价格为 `$0.20` 输入、`$1.25` 输出（每百万 Tokens；Batch API 可享 50% 折扣）。它功能完整，但输出成本仍显著高于 MiMo 的国际价，适合质量基准或复杂人工复核，而非默认省钱模型。[O1][O2][O3]

## 本项目的实际兼容边界

当前客户端在 `src/answer_hub/mimo.py` 中已经实现的是一套 OpenAI Chat Completions 风格的单供应商客户端：

- `POST {base_url}/chat/completions` 和 Bearer Token；
- 读取 `choices[0].message.content`，并使用 `usage.prompt_tokens`、`usage.completion_tokens` 统计本地估算费用；
- `response_format={"type":"json_object"}`；
- 图片使用 `image_url`，视频使用 `video_url`；
- 只配置一个主 Base URL/密钥池，文本和媒体模型也默认属于同一供应商。

因此，**同一厂商的单一多模态模型**迁移成本最低；“一个文本模型 + 一个视觉模型”的组合需要新增供应商路由、密钥池、错误处理、计费归因和媒体请求分流。当前不能只改模型名就安全使用 DeepSeek 文本模型。[P1]

此外，项目的 `.env.example` 中已经限制了并发、批量字符数、主题模型调用上限和每次运行的成本上限；但 API payload 没有传递 `thinking` 或 `max_completion_tokens`。这意味着本地估算只能按总输入/输出 Tokens 统计，不能单独解释缓存命中、推理 Tokens 或某个任务的异常长输出。[P1][P2]

## 官方模型价格与适配性

价格单位均保留厂商原币种，**不使用临时汇率强行比较人民币与美元**。除特别标注外，均为每百万 Tokens；多媒体、缓存、批量接口和不同上下文档位以官方页面为准。

| 模型 | 官方 Token 价格 | 本项目所需能力 | 迁移判断 |
|---|---:|---|---|
| 小米 MiMo `mimo-v2.5` | 北京：缓存命中输入 `¥0.02`；未命中输入 `¥1`；输出 `¥2`。国际价格页为 `$0.014` / `$0.14` / `$0.28` | 1M 上下文；文本、图片、视频、音频；Chat Completions、JSON Object、思考开关 | **当前基线**。当前端点与消息格式原生适配；应先限制输出并关闭不需要的思考。 |
| 通义千问 `qwen3.7-flash` | 北京：输入≤32K 为 `¥0.2` / 缓存 `¥0.04` / 输出 `¥0.8`；32K–256K 为 `¥0.6` / `¥0.12` / `¥2.4`；256K–1M 为 `¥1.2` / `¥0.24` / `¥4.8` | 1M 上下文；文本、图片、视频；结构化输出；OpenAI 兼容 | **首选替代候选**。媒体能力完整，低输入档 Token 成本显著更低；需实测图片/视频消息、Usage、思考开关和限流错误。 |
| 通义千问 `qwen-flash` | 北京：未命中输入 `¥0.15`；缓存命中 `¥0.03`；输出 `¥1.5` | 1M 上下文；文本；结构化输出；OpenAI 兼容 | **仅文本子任务候选**。不能单独覆盖当前带图/视频流程。 |
| DeepSeek `deepseek-v4-flash` | 缓存命中输入 `$0.01`；未命中输入 `$0.14`；输出 `$0.28` | 1M 上下文；JSON Output；OpenAI 兼容；不支持图像/视频 | **不能直接替换**。纯文本子任务可作为未来路由候选，带图/视频仍须保留另一个模型。 |
| OpenAI `gpt-5.4-nano` | 标准：输入 `$0.20`，输出 `$1.25`；Batch API 为上述 50% | 图片输入；结构化输出；不支持视频 | **不推荐为省钱默认**。可用作质量基线或少量困难样本的人工复核辅助。 |

### 参考成本（只用于理解 Token 价格）

假设 1,000 次纯文本调用，每次 8,000 输入 Tokens、1,000 输出 Tokens；不计算缓存命中、图片/视频额外计费或重试：

```text
总费用 = 调用次数 × (输入 Tokens × 输入单价 + 输出 Tokens × 输出单价) / 1,000,000
```

| 模型 | 1,000 次参考费用 |
|---|---:|
| `mimo-v2.5`（北京、全部未命中缓存） | `¥10.00` |
| `qwen3.7-flash`（北京、输入≤32K） | `¥2.40` |
| `qwen-flash`（北京、全部未命中缓存） | `¥2.70` |
| `deepseek-v4-flash`（全部未命中缓存） | `$1.40` |
| `gpt-5.4-nano`（标准 API） | `$2.85` |

这不是项目一次真实批处理的账单预测：当前批处理会产生不止一次调用，存在 JSON 失败重试、原子问题批量、主题后处理和可能的多媒体输入。若一次运行花费十几元，优先检查总调用次数和输出 Tokens，而不是只看一条原始会话长度。

## 为什么先控制思考与输出

MiMo 官方说明：V2.5 / V2.5 Pro 的思考模式默认启用；通过 `thinking: {"type": "disabled"}` 可关闭。该系列默认 `max_completion_tokens` 为 32,768。Qwen3.7 Flash 的思考模式也需要在结构化抽取/分类场景显式关闭，并按百炼实际接口参数验证。官方还建议在结构化输出时使用 JSON Object 模式并在提示词要求只返回 JSON。[M3][Q1]

本项目的请求已经具备 JSON Object 模式和本地字段校验，且多个字段本身有长度限制。因此建议在**脱敏测试样本**中验证下面的策略，而不是不加上限地让每一种任务使用同一输出预算：

| 任务 | 建议试验策略 | 目的 |
|---|---|---|
| 连通性预检、主题环节/价值分类、聚类裁决 | 关闭思考；较小输出上限 | 返回值是短 JSON，避免为推理过程和冗长解释付费。 |
| 原子问题提取和 1-N 聚类 | 关闭思考；按单次批量条数设中等输出上限 | 保留多条结果空间，但防止异常长输出。 |
| 知识转写、推荐回复、内容初标 | 分别设上限；先验证是否确有必要保留思考 | 这类文本更长，不能直接套用分类任务的限制。 |
| 图片/视频证据不足或模型降级 | 不因降本放行；继续进入人工优先审核 | 保持项目既有业务红线。 |

具体上限需要在现有 60 对聚类标注和脱敏图文样本上测出，不能凭空写入生产配置。验收至少包含：JSON 首次成功率、重试后成功率、原子问题拆分准确率、错误合并率、错误拆分率、人工修改率、每 100 条的实际输入/输出 Tokens 与费用。

## 各路径的迁移注意事项

### 路径 A：Qwen3.7 Flash 单模型替换（首选试点）

- 百炼提供 OpenAI 兼容 Chat Completions；北京兼容模式 Base URL 为 `https://dashscope.aliyuncs.com/compatible-mode/v1`。结构化输出需按其 API 文档验证请求参数和响应格式；固定 JSON 抽取/分类要显式使用 `enable_thinking=false`。[Q1][Q2][Q3]
- 必须验证当前 `image_url`、`video_url`、`response_format`、Usage 字段、429/余额/鉴权错误与现有客户端处理是否一致。
- 先对每次请求记录输入 Tokens，确认绝大多数生产样本保持在 32K 以下；超过 32K 时应按官方阶梯重新估算成本。
- 代码中的 `MIMO_*` 名称只是历史命名；若切换供应商，应避免把日志、审核记录和成本字段误标成 MiMo。
- 可延迟最多 24 小时的离线任务可另测百炼 Batch File：它在 Qwen3.7 Flash 各档 Token 价格上再减半；但 Batch Chat 不打折，不能当作简单同步接口替换。[Q1][Q5]

### 路径 B：继续用 MiMo V2.5，并控费

- 不变更供应商、Base URL、鉴权、图文/视频消息和现有成本统计口径。
- 代码改动应只新增可配置的 `thinking` 与任务级输出上限，并为每个 payload 传入；不应改变“低置信度、降级或风险候选进入人工审核”的逻辑。
- 需要为默认值、关闭思考、输出上限、请求 payload 和回归结果补充单元测试。
- 先在测试样本上 A/B；通过后再调整本机 `.env`，不要将任何真实密钥写入仓库或测试文件。

### 路径 C：DeepSeek 文本 + MiMo/千问多模态

- DeepSeek 官方明确当前不支持图片和视频，因此仅可承接纯文本任务。[D2]
- 这是多供应商架构改造：需要显式文本/媒体路由、两套密钥池和 Base URL、各自的重试/限流/成本记录、以及不能跨供应商泄露密钥的日志脱敏。
- 只在路径 A 后仍需要进一步降低“纯文本高频任务”的成本时评估；不能为了省钱把带图任务静默降为纯文本。

### 路径 D：OpenAI GPT-5 mini

- 原生支持结构化输出和图片输入，适合用于质量基线或抽样复核。[O1][O2]
- 以官方 Batch API 单价看，不是当前 MiMo 的低成本替代；若使用实时 API，必须在实际 OpenAI 定价页再次核对后再估算。
- 数据出境、合规与网络连通性需要由业务方另行确认，本研究不建议擅自开启新的外部服务。

## 建议的最小验证顺序

1. 从 `operations-report` 或单次运行的聚合指标取得 `model_calls`、`model_retries`、`model_input_tokens`、`model_output_tokens` 和 `model_estimated_cost`；只看聚合数，不复制真实会话内容。
2. 在 60 对聚类标注与脱敏图文样本中，比较 `mimo-v2.5` 与 `qwen3.7-flash`；两组都使用“关闭思考 + 合理输出上限”。
3. 若 Qwen3.7 Flash 的准确率和 JSON 稳定性不低于当前基线，即可将它作为默认模型；MiMo V2.5 保留给失败重试或人工复核的高难度样本。
4. 若 Qwen3.7 Flash 质量不达标，则保留 MiMo V2.5，并先落地输出与思考控费。
5. 若只想压缩文本高频步骤，再单独立项评估 DeepSeek 的多供应商路由；不得绕过媒体证据、Qwen3 重复检测、CZ 待审核和人工终审。

## 官方来源

所有外部资料均于 **2026-08-10** 核对。

- [M1] 小米 MiMo V2.5 模型概览（能力、上下文、媒体输入）：<https://mimo.mi.com/docs/quick-start/summary/model>
- [M2] 小米 MiMo 中国大陆按量付费（缓存命中/未命中输入和输出价格）：<https://mimo.mi.com/docs/zh-CN/price/pay-as-you-go>
- [M3] 小米 MiMo Chat Completions API（思考模式、`max_completion_tokens`、JSON Object）：<https://mimo.mi.com/docs/api-reference/chat-completions>
- [M4] 小米 MiMo 国际定价页（美元价格）：<https://mimo.mi.com/docs/en-US/price/pay-as-you-go>
- [Q1] 阿里云百炼 Qwen3.7 Flash 模型说明（1M 上下文、文本/图片/视频）：<https://help.aliyun.com/zh/model-studio/qwen3-7-flash>
- [Q2] 阿里云百炼结构化输出：<https://help.aliyun.com/zh/model-studio/structured-output>
- [Q3] 阿里云百炼 OpenAI 兼容 Chat Completions：<https://help.aliyun.com/zh/model-studio/developer-reference/use-qwen-by-calling-api>
- [Q4] 阿里云百炼模型价格：<https://help.aliyun.com/zh/model-studio/model-pricing>
- [D1] DeepSeek 模型与价格：<https://api-docs.deepseek.com/quick_start/pricing>
- [D2] DeepSeek API 快速开始（OpenAI 兼容及多模态限制）：<https://api-docs.deepseek.com/>
- [D3] DeepSeek JSON Output：<https://api-docs.deepseek.com/guides/json_mode>
- [O1] OpenAI GPT-5.4 nano 模型页（图片输入、结构化输出与价格）：<https://developers.openai.com/api/docs/models/gpt-5.4-nano>
- [O2] OpenAI 结构化输出指南：<https://platform.openai.com/docs/guides/structured-outputs>
- [O3] OpenAI Batch API：<https://developers.openai.com/api/docs/guides/batch>
- [Q5] 阿里云百炼 OpenAI 兼容 Batch 接口：<https://help.aliyun.com/zh/model-studio/batch-interfaces-compatible-with-openai>
- [P1] 本项目 `src/answer_hub/mimo.py`（兼容客户端、调用 payload、重试和成本统计）。
- [P2] 本项目 `.env.example`（当前默认模型、批量/并发/成本上限配置示例）。

## 本次变更

本次仅新增本研究文档：

```text
LOW_COST_MODEL_RESEARCH_2026-08-10.md
```

未改动模型配置、业务流程、真实数据、运行输出或密钥。
