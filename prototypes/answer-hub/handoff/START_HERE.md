# 回家继续开发：答疑中台知识库

更新时间：2026-07-15

## 1. 当前目标

当前只优先调通以下链路：

```text
方向二脱敏会话
-> 证据筛选
-> Embedding 语义聚类
-> 主题级知识转写
-> 模型/规则初标
-> Streamlit 人工复核
```

暂时不要继续训练小模型，也不要继续改正式知识库发布流程。先确保语义聚类结果可以人工检查和调节。

## 2. 已完成

- Streamlit 工作台界面已经美化。
- 审核页可以查看主题成员的原始聊天内容。
- 聚类支持 `semantic` 和 `rule` 两种模式。
- 语义聚类支持相似度阈值调节，默认值为 `0.84`。
- 语义聚类使用 OpenAI-compatible `/embeddings` 接口。
- 推荐模型为 `Qwen/Qwen3-Embedding-0.6B`。
- Embedding 不可用时会明确提示并回退规则聚类。
- 当前完整测试结果：`30 passed`。

## 3. 新电脑首次安装

在 PowerShell 中进入本目录：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install --upgrade pip
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
Copy-Item .env.example .env
```

不要把真实 API Key 写入代码、Excel 或聊天记录。只在本机 `.env` 中填写。

## 4. 启动 Embedding

CPU 电脑：

```powershell
docker compose -f handoff\docker-compose.embedding-cpu.yml up -d
```

有可用 NVIDIA GPU：

```powershell
docker compose -f handoff\docker-compose.embedding-gpu.yml up -d
```

首次启动需要下载模型镜像和模型文件，时间取决于网络。

在 `.env` 中设置：

```dotenv
EMBEDDING_BASE_URL=http://127.0.0.1:8080/v1
EMBEDDING_MODEL=Qwen/Qwen3-Embedding-0.6B
EMBEDDING_API_KEY=
EMBEDDING_TIMEOUT_SECONDS=60
```

MiMo 不是语义聚类的必要条件。只测试聚类时，可以不配置 `MIMO_*`。

## 5. 验证测试

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

预期：

```text
30 passed
```

## 6. 启动 Streamlit

```powershell
.\.venv\Scripts\python.exe -m streamlit run streamlit_app.py --server.port 8501
```

浏览器访问：

```text
http://localhost:8501
```

## 7. 使用脱敏样例测试

生成主题候选时上传：

```text
examples\semantic_clustering_demo.xlsx
examples\semantic_clustering_standards.json
```

页面设置：

```text
聚类方式：语义聚类
语义相似度阈值：0.84
调用 MiMo：关闭
```

生成后应看到页面提示“语义聚类已生效”。若提示已回退规则聚类，优先检查：

1. Docker 容器是否运行。
2. `http://127.0.0.1:8080` 是否可访问。
3. `.env` 中模型名和地址是否正确。
4. 容器是否仍在下载模型。

## 8. 当前语义聚类实现

语义文本由以下字段组成：

```text
核心问题
聊天内容
判定结论
判定依据
问题意图
对象/部位
异常现象
解题方式
一级分类
二级分类
主标准路径
```

处理逻辑：

```text
过滤无有效证据记录
-> 批量生成 Embedding
-> 产品类型作为硬边界
-> 计算记录与主题中心的余弦相似度
-> 超过阈值则合并并更新主题中心
-> 单条主题进入待聚合队列
```

主要代码：

```text
src\answer_hub\embedding.py
src\answer_hub\workflow.py
streamlit_app.py
tests\test_mimo_workflow.py
```

## 9. 下一步只做这些

1. 使用真实脱敏数据比较 `0.72 / 0.76 / 0.80 / 0.84 / 0.88`。
2. 记录错误合并和错误拆分案例。
3. 在 Streamlit 中确认每个主题的成员聊天内容是否合理。
4. 根据人工结果选择阈值。
5. 聚类稳定后再继续小模型初标训练。

## 10. 安全说明

本交接包不包含：

- `.env`
- API Key 或密码
- `data\phone_mvp.db`
- 真实会话 Excel
- `outputs` 运行结果
- `.venv`
- 缓存文件

