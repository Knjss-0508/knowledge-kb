# 百晓生转人工会话分析

## 能力范围

- 全量导入或分页采集曼哈顿转人工列表。
- 每周按日期、原转人工原因和类目分层抽样，默认 350 条。
- 按工单 ID、工程师和时间关联百晓生会话。
- 结合百晓生召回、知识主表和能力注册表进行模型或规则初标。
- 人工复核低置信度记录，生成八个工作表的周报。
- 诊断标签不单独占列，统一写入 `备注`：

```text
【诊断】标签1、标签2；【事实】会话和召回证据；【建议】优化动作
```

## 安装

基础分析和 Streamlit：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui,dev]"
```

需要后台登录及接口采集时额外安装 Playwright。采集器默认调用电脑中已经安装的 Chrome，不下载或绕过公司登录组件：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[collector]"
```

## 先使用文件完成联调

```powershell
$env:PYTHONPATH = "src"

.\.venv\Scripts\python.exe -m answer_hub.cli transfer-collect `
  --system manhattan `
  --source-file ".\data\曼哈顿转人工.xlsx"

.\.venv\Scripts\python.exe -m answer_hub.cli transfer-collect `
  --system baixiaosheng `
  --source-file ".\data\百晓生会话.xlsx"

.\.venv\Scripts\python.exe -m answer_hub.cli transfer-analyze `
  --week-start "2026-07-13" `
  --standards ".\data\当前有效知识主表.xlsx" `
  --output-dir ".\outputs\transfer-analysis\2026-07-13" `
  --sample-size 350
```

使用 `--rule-only` 可以在不调用 MiMo 的情况下验证完整链路。

## 接口勘探

首次执行时打开浏览器，由用户自行完成登录。然后在两个后台分别执行日期查询、翻页、打开会话、查看召回及工具调用，完成后关闭浏览器：

```powershell
.\.venv\Scripts\python.exe -m answer_hub.cli transfer-discover `
  --system manhattan `
  --login-url "实际登录地址" `
  --output ".\outputs\transfer-analysis\discovery\manhattan-network.ndjson"
```

勘探文件只保存请求参数和响应 JSON 的字段结构，不保存响应正文、Cookie 或 Authorization。根据勘探结果填写：

- `examples/manhattan_endpoint_profile.example.json`
- `examples/baixiaosheng_endpoint_profile.example.json`

不要把 Cookie、Token 或密码写入接口模板。

## 周报

输出工作表：

1. 转人工分析明细
2. 人工复核队列
3. badcase清单
4. 知识补充候选
5. 召回质量分析
6. 工具调用问题
7. 周度统计
8. 责任方优化清单

百晓生不支持通用多模态。当前能力注册表仅确认内存硬盘品牌识别工具；笔记本识别工具在没有成功调用记录或正式能力说明时，统一标记为能力范围不确定并进入人工复核。

## 自动运行

`scripts/run_transfer_daily.ps1` 默认采集前一自然日的曼哈顿列表。

`scripts/run_transfer_weekly.ps1` 默认分析上一个完整自然周，并在抽样后抓取双方详情。

两个脚本从 `.env` 读取：

- `TRANSFER_MANHATTAN_PROFILE`
- `TRANSFER_BAIXIAOSHENG_PROFILE`
- `TRANSFER_KB_CATALOG_PATH`
- `TRANSFER_ANALYSIS_DB_PATH`

可以在 Windows 任务计划程序中分别设置为每天 02:30 和每周一 03:30。
