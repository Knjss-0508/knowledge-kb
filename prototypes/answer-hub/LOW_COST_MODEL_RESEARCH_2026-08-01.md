# 答疑中台知识库低成本大模型选型研究

查询日期：2026-08-01
研究范围：仅使用模型厂商一手官方文档；未调用任何真实模型 API；未读取 `.env`。
价格口径：均为官方页面公布的每百万 Tokens 单价，保留厂商原始币种；不同币种不使用临时汇率强行换算。

## 1. 结论

### 默认推荐

- 默认文本模型：`mimo-v2.5`
- 默认图片模型：`mimo-v2.5`

建议先把当前 `mimo-v2.5-pro` 降为 `mimo-v2.5`，文本和图文任务都使用同一个模型。

主要原因：

1. `mimo-v2.5` 官方价格为输入缓存未命中 `0.14 美元`、输出 `0.28 美元`；`mimo-v2.5-pro` 为输入缓存未命中 `0.435 美元`、输出 `0.87 美元`。按相同 Token 用量计算，标准版价格约为 Pro 的 32%，理论费用下降约 68%。[M1][M2]
2. 两者均提供 1M 上下文；`mimo-v2.5` 还原生支持文本、图片、视频和音频输入，更符合本项目“完整聊天 + 案例图，未来可能扩展视频”的证据形态。[M1]
3. 当前项目已经使用 MiMo 的 OpenAI 兼容 `/v1/chat/completions`、`image_url`、`video_url` 和 `response_format={"type":"json_object"}` 请求形态。切换到同一厂商的 `mimo-v2.5` 不需要生产代码改造，只需以后修改模型配置并做回归验证。[M3][P1]
4. 当前代码只配置一套 `base_url` 和 API Key 池。若采用“DeepSeek 文本 + 千问图片”，需要新增多供应商路由、两套密钥和两套端点，迁移成本明显高于同一模型承接全部任务。[P1]

这不是说 `mimo-v2.5` 的项目准确率已经得到证明。官方资料不能替代本项目真实样本评测；正式替换前仍应在现有 60 对聚类样本和脱敏图文样本上比较 JSON 成功率、拆题准确率、错误合并率、错误拆分率和人工修改率。

### 第二选择

若 `mimo-v2.5` 的离线 A/B 准确率不达标，建议第二顺位测试：

- 文本和图片统一使用 `qwen3.6-flash`

它提供 1M 上下文、文本/图片/视频输入、OpenAI 兼容接口和结构化输出，单一模型即可覆盖现有请求，迁移复杂度低于跨厂商双模型组合。[Q1][Q2][Q3]

### 暂不作为首选

- `deepseek-v4-flash`：文本价格低、1M 上下文、支持 JSON，但官方明确暂不支持图片输入；当前客户端还不能把所有图片请求可靠地分流到另一厂商。[D1][D2][D3]
- `qwen-flash`：纯文本价格极低，但不接受图片。作为未来独立文本路由很有吸引力，当前直接替换会让带图调用存在兼容风险。[Q2][Q4]
- `glm-4.7-flash` / `glm-4.6v-flash`：官方模型总览标为免费，适合 PoC 或故障备用；但免费模型的生产并发、稳定性和项目准确率需要单独验证，不建议仅因“免费”直接成为正式默认模型。[Z1]

## 2. 项目实际要求

根据当前仓库实现，模型不是普通聊天机器人，而是连续参与以下任务：

1. 中文会话语义标注。
2. 每条会话拆分 1～3 个原子问题。
3. 1～N 主题聚类和聚类裁决。
4. 主题价值分类。
5. 长文本知识转写和推荐回复生成。
6. 内容质量初标。
7. 对最多 4 张案例图及部分视频链接进行证据分析。
8. 返回可被程序直接解析和业务校验的 JSON。

因此候选模型至少需要满足：

- 中文指令遵循稳定；
- 支持 OpenAI 兼容 Chat Completions；
- 支持 `response_format={"type":"json_object"}` 或等效 JSON 模式；
- 能承载较长系统提示词、完整聊天和聚类候选；
- 图片模型支持标准 `image_url` 图文消息；
- 最好支持视频输入，或至少对不支持的视频明确降级；
- 单次失败可以被当前重试和人工审核机制接住。

### 当前客户端的关键迁移边界

当前 [src/answer_hub/mimo.py](src/answer_hub/mimo.py) 虽然类名和环境变量仍以 MiMo 命名，但 HTTP 客户端本身较通用：

- Bearer Token 鉴权；
- `POST {base_url}/chat/completions`；
- 读取 `choices[0].message.content`；
- 读取 `usage.prompt_tokens` / `completion_tokens`；
- 发送 `temperature` 和 JSON Object 模式；
- 图片使用 `image_url`；
- 视频使用 `video_url`。

不过它只支持一套：

- `MIMO_BASE_URL`
- `MIMO_API_KEY` / `MIMO_API_KEYS`
- 文本模型 ID 和同一端点下的媒体模型 ID

而且部分带图方法仍直接使用主文本模型，并非全部使用 `MIMO_MEDIA_MODEL`。因此：

- 同一厂商、同一端点、单个多模态模型：迁移最低；
- 同一厂商、文本模型和视觉模型分开：需要逐方法确认路由；
- DeepSeek 文本 + 千问/智谱图片：需要生产代码支持多供应商，迁移成本最高。

## 3. 官方模型与价格对比

### 3.1 核心候选

| 厂商与模型 | 官方单价/百万 Tokens | 上下文与输出 | 图文/视频 | JSON 与 OpenAI 兼容 | 迁移判断 |
|---|---:|---|---|---|---|
| MiMo `mimo-v2.5-pro`（切换前文本） | 缓存命中输入 `$0.0435`；未命中输入 `$0.435`；输出 `$0.87` | 1M 上下文 | 官方模型表标为文本输入 | 官方 Chat Completions 和 JSON 输出 | 已接入，但价格高于标准版 |
| MiMo `mimo-v2.5` | 缓存命中输入 `$0.014`；未命中输入 `$0.14`；输出 `$0.28` | 1M 上下文 | 文本、图片、视频、音频输入 | 与当前端点、消息和 JSON 请求形态一致 | **最低：配置切换 + 回归测试** |
| 通义 `qwen3.6-flash` | 输入长度不超过 128K：输入 `¥1.2`、输出 `¥7.2`；超过 128K：输入 `¥2.4`、输出 `¥14.4` | 1M 上下文 | 文本、图片、视频输入，文本输出 | 官方支持 OpenAI 兼容和结构化输出 | 低到中：单模型可覆盖，但需验证媒体格式 |
| 通义 `qwen-flash` | 输入长度不超过 128K：输入 `¥0.15`、输出 `¥1.5`；超过 128K：输入 `¥0.6`、输出 `¥6` | 1M 上下文，最大输出 32K | 文本输入 | OpenAI 兼容；官方结构化输出页列为支持 | 中到高：只适合独立文本路由 |
| DeepSeek `deepseek-v4-flash` | 缓存命中输入 `$0.01`；未命中输入 `$0.14`；输出 `$0.28` | 1M 上下文 | 官方说明暂不支持多模态 | OpenAI 兼容；支持 JSON Output | 高：需另配图片供应商和路由 |
| DeepSeek `deepseek-v4` | 缓存命中输入 `$0.02`；未命中输入 `$0.28`；输出 `$0.42` | 1M 上下文 | 官方说明暂不支持多模态 | OpenAI 兼容；支持 JSON Output | 高：同上，且价格高于 Flash |
| 智谱 `glm-4.7-flash` | 官方模型总览标为免费 | 200K 上下文，最大输出 128K | 文本输入 | 智谱提供 OpenAI SDK/LangChain 兼容用法；GLM 支持结构化输出 | 中：可做免费文本 PoC，不宜未经压测直接生产 |
| 智谱 `glm-4.6v-flash` | 官方模型总览标为免费 | 128K 上下文，最大输出 32K | 视觉推理，支持长上下文 | 同一智谱 OpenAI 兼容端点 | 中：可做免费图片 PoC，需验证 `image_url`/视频细节 |

来源：[M1][M2][M3][Q1][Q2][Q3][Q4][D1][D2][D3][Z1][Z2][Z3]

### 3.2 最新旗舰参考

这些模型代表各厂商截至查询日的较新能力上限，但不一定符合“低成本默认模型”目标：

| 厂商 | 最新或较新旗舰 | 官方能力摘要 | 本项目判断 |
|---|---|---|---|
| MiMo | `mimo-v2.5-pro` | 1M 文本上下文 | 切换前默认；可作为高难度人工重试模型，不必作为每次默认模型 |
| 通义 | `qwen3.7-plus` | 1M 上下文，可输入文本、图片和视频，最大输出 128K | 能力完整；官方不同区域/页面对结构化输出支持描述存在差异，迁移前必须按实际区域验证 |
| DeepSeek | `deepseek-v4` | 1M 上下文；官方定价页提供标准版和 Flash 版 | 适合复杂文本任务，但不支持图片 |
| 智谱 | `glm-5.2` | 1M 上下文，最大输出 128K，支持 JSON 结构化输出 | 文本旗舰；官方价格页为动态页面，本研究未采用无法稳定复核的单价 |
| 智谱 | `glm-5v-turbo` | 200K 上下文，最大输出 128K，视觉理解和推理 | 视觉旗舰；不是本次低成本默认候选 |

来源：[M1][Q5][D1][Z1][Z4][Z5]

## 4. 参考工作量成本

为便于理解，假设 1,000 次文本调用，每次：

- 输入 8,000 Tokens；
- 输出 1,000 Tokens；
- 不计算缓存命中；
- 不计算图片/视频额外计费；
- 输入长度位于各厂商最低价格档。

计算公式：

```text
总成本 = 1000 × (8000 × 输入单价 + 1000 × 输出单价) / 1,000,000
```

| 模型 | 1,000 次参考调用成本 |
|---|---:|
| `mimo-v2.5-pro` | `$4.35` |
| `mimo-v2.5` | `$1.40` |
| `deepseek-v4-flash` | `$1.40` |
| `deepseek-v4` | `$2.66` |
| `qwen3.6-flash` | `¥16.80` |
| `qwen-flash` | `¥2.70` |
| `glm-4.7-flash` | 官方标为免费 |

同币种直接比较可见：

- `mimo-v2.5` 相比 `mimo-v2.5-pro` 节省约 67.8%；
- `deepseek-v4-flash` 的公开 Token 价格与 `mimo-v2.5` 相同，但没有图片能力；
- `qwen-flash` 的文本价格非常低，但不能直接承接完整图文链路。

## 5. 分项判断

### 中文效果

各候选均由国内厂商提供中文文档和中文调用示例，可以进入中文业务候选集。

但“适合本项目的中文效果好”不能仅凭厂商介绍确认。本项目存在大量质检术语、转人工元数据、长聊天、相似问题拆分和业务红线，最终必须使用已有人工标注集评测。由于本研究按要求不调用 API，不对模型准确率做无依据排名。

### OpenAI 兼容

- MiMo：官方提供 OpenAI 兼容 Chat Completions；与当前实现完全一致。[M3]
- 通义：百炼提供 OpenAI Chat Completion 兼容接口。[Q3]
- DeepSeek：官方接口与 OpenAI 格式兼容，可通过修改 `base_url` 使用。[D2]
- 智谱：官方文档展示使用 `ChatOpenAI` 和 `openai_api_base` 访问智谱端点。[Z2]

### 图文输入

- 最完整且迁移最低：`mimo-v2.5`。
- 千问低成本完整候选：`qwen3.6-flash`。
- 智谱免费 PoC：`glm-4.6v-flash`。
- DeepSeek：官方当前不支持多模态，不可单独覆盖项目。

### 结构化 JSON

- 当前代码使用 JSON Object 模式，并在本地再次执行 `json.loads` 和字段校验。
- MiMo、通义、DeepSeek、智谱均有官方 JSON/结构化输出说明。[M3][Q2][D3][Z3]
- “支持 JSON”不等于业务字段永远完整；当前项目保留二次校验和重试是必要的。

### 长文本

- 1M：MiMo V2.5/Pro、Qwen3.6 Flash、Qwen Flash、DeepSeek V4/Flash、GLM-5.2。
- 200K：GLM-4.7 Flash。
- 128K：GLM-4.6V Flash。

项目单条提示词目前远低于这些上限。长上下文更重要的价值是未来把更多主题成员、完整聊天或审核历史放在同一次请求中，而不是当前必须为 1M 支付旗舰溢价。

## 6. 迁移成本排序

### A. 极低：MiMo Pro 改 MiMo 标准版

预期只需要以后调整本机配置：

```dotenv
MIMO_MODEL=mimo-v2.5
MIMO_MEDIA_MODEL=mimo-v2.5
```

后续落地任务已按该结论更新`.env.example`和当前交接文档，并把本工作区真实
`.env`中的`MIMO_MODEL`改为`mimo-v2.5`；未读取或输出任何密钥。若
`MIMO_MEDIA_MODEL`未配置，当前客户端也会自动使用`mimo-v2.5`。

### B. 低到中：所有任务统一改用 Qwen3.6 Flash

预计仍可复用当前 Chat Completions 客户端，但需要验证：

- 百炼 OpenAI 兼容 Base URL；
- `response_format=json_object`；
- Base64 `image_url` 和远程 URL；
- 当前 `video_url` 消息形态；
- Token usage 字段；
- 429、鉴权和余额错误格式；
- 现有 JSON 校验重试能否正常工作。

### C. 中：所有任务统一改用智谱视觉 Flash

需要完成与千问类似的兼容测试。虽然官方标为免费，但必须补充：

- 并发和限流压测；
- 长输出截断率；
- JSON 字段完整率；
- 质检图片细节识别准确率；
- 免费模型的持续可用性观察。

### D. 高：DeepSeek 文本 + 其他厂商图片

需要把当前单供应商配置改为至少两套 Provider：

- 独立 Base URL；
- 独立 API Key 池；
- 独立文本/图片请求路由；
- 每个 Provider 的错误码、重试和成本统计；
- 带图方法统一走图片 Provider；
- 审计字段从固定 `mimo` 改为实际 Provider。

在没有这层改造前，不建议直接把主文本模型改为 DeepSeek。

## 7. 推荐落地顺序

1. 首先只验证 `mimo-v2.5`，保持端点、鉴权、请求格式和媒体能力不变。
2. 使用已有 60 对聚类标注和脱敏图文样本做 A/B，不接触正式未脱敏数据。
3. 至少比较：
   - JSON 首次解析成功率；
   - 重试后成功率；
   - 原子问题拆分准确率；
   - 错误合并率；
   - 错误拆分率；
   - 主题分类准确率；
   - 人工修改率；
   - 每 100 条实际 Token 与费用。
4. 若标准版质量达标，正式默认改为 `mimo-v2.5`，Pro 仅保留给失败重试或高风险人工复核。
5. 若标准版质量不达标，再测试 `qwen3.6-flash` 单模型方案。
6. 只有确认需要进一步压低纯文本成本时，再开发多供应商路由，并测试 `qwen-flash` 或 `deepseek-v4-flash` 文本 + 多模态模型的组合。

无论选择哪一项，都必须保持现有业务红线：低置信度、模型降级、图片不足或高风险候选进入人工审核；不得绕过 Qwen3 查重和 CZ 终审。

## 8. 官方来源

所有来源查询日期均为 **2026-08-01**。

### MiMo

- [M1] 小米 MiMo 官方模型概览：<https://mimo.mi.com/docs/quick-start/summary/model>
- [M2] 小米 MiMo 官方定价：<https://mimo.mi.com/docs/pricing>
- [M3] 小米 MiMo Chat Completions API：<https://mimo.mi.com/docs/api-reference/chat-completions>

### 通义千问 / 阿里云百炼

- [Q1] 阿里云百炼 Qwen3.6 Flash 模型说明：<https://help.aliyun.com/zh/model-studio/qwen3-6-flash>
- [Q2] 阿里云百炼结构化输出：<https://help.aliyun.com/zh/model-studio/structured-output>
- [Q3] 阿里云百炼 OpenAI Chat Completion 兼容接口：<https://help.aliyun.com/zh/model-studio/developer-reference/use-qwen-by-calling-api>
- [Q4] 阿里云百炼模型价格：<https://help.aliyun.com/zh/model-studio/model-pricing>
- [Q5] 阿里云百炼 Qwen3.7 Plus 模型说明：<https://help.aliyun.com/zh/model-studio/qwen3-7-plus>

### DeepSeek

- [D1] DeepSeek 官方模型与定价：<https://api-docs.deepseek.com/quick_start/pricing>
- [D2] DeepSeek 官方 API 快速开始：<https://api-docs.deepseek.com/>
- [D3] DeepSeek 官方 JSON Output 指南：<https://api-docs.deepseek.com/guides/json_mode>

### 智谱

- [Z1] 智谱官方模型概览：<https://docs.bigmodel.cn/cn/guide/start/model-overview>
- [Z2] 智谱官方 LangChain/OpenAI 兼容示例：<https://docs.bigmodel.cn/cn/guide/develop/langchain/introduction>
- [Z3] 智谱官方结构化输出：<https://docs.bigmodel.cn/cn/guide/capabilities/struct-output>
- [Z4] 智谱 GLM-5.2 官方说明：<https://docs.bigmodel.cn/cn/guide/models/text/glm-5.2>
- [Z5] 智谱官方价格入口：<https://docs.bigmodel.cn/cn/guide/start/pricing>

### 本地项目依据

- [P1] `src/answer_hub/mimo.py`：当前 OpenAI 兼容请求、JSON 校验、图片/视频消息、单端点配置和成本统计实现。
- [P2] `.env.example`：落地前的基线配置使用
  `MIMO_MODEL=mimo-v2.5-pro`、`MIMO_MEDIA_MODEL=mimo-v2.5`；主任务随后已把示例默认
  调整为文本和媒体均使用`mimo-v2.5`。

## 9. 本研究修改范围

研究子任务仅新增：

```text
LOW_COST_MODEL_RESEARCH_2026-08-01.md
```

主任务随后更新了本工作区`.env`中的非密钥模型名、`.env.example`、测试和当前
说明文档；未修改生产业务逻辑、真实数据或运行输出。
