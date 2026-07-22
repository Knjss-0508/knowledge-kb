# 答疑知识中台交付指南

更新日期：2026-07-22

## 1. 交付目标

交付对象不是一个本地项目目录，也不是一批 Excel，而是一套可部署、可接入、可运营、可验收和可追溯的知识生产服务。

正式交付分为三类：

1. **业务使用交付**：面向审核员、知识运营和负责人。
2. **系统部署交付**：面向运维、管理员和后续维护人员。
3. **接口接入交付**：面向第二部分、知识消费端和其他系统开发人员。

## 2. 业务用户收到什么

业务用户通常不需要源代码，应收到：

- 工作台和 CZ 知识库访问地址。
- 独立用户账号与权限。
- 《业务操作手册》。
- 品类、分类和审核口径。
- 脱敏数据导入模板。
- 聚类验证模板。
- 审核与问题反馈模板。
- 验收结果和已知限制。

交付包中的业务操作手册为 `USER_OPERATIONS_GUIDE.md`。

业务操作主链：

```text
查看自动化运行
→ 处理异常记录
→ 审核主题候选
→ 填写是否值得沉淀、是否可用、如何修改、问题反馈
→ 批量提交 CZ 终审
→ 查看Qwen3疑似重复拦截和明确重复阻断
→ 查看发布和驳回结果
```

业务用户不应接触：

- API Key。
- 服务间集成密钥。
- 数据库账号。
- 未脱敏原始会话。
- 模型服务内部配置。

## 3. 部署人员收到什么

部署交付物为：

```text
answer-hub-delivery-<版本>.zip
```

压缩包包含：

- 第三部分 `answer_hub` 源码。
- Streamlit 工作台。
- CZ FastAPI 后端和静态前端。
- 数据库迁移。
- Docker Compose。
- 本地Qwen3 Embedding查重拦截服务。
- 十品类初始配置。
- API 请求示例。
- 测试。
- 部署、运行、验收和故障处理文档。
- `manifest.json`。
- `checksums.sha256`。

压缩包明确不包含：

- `.env`。
- 任何密钥、Token、Cookie和密码。
- 真实会话和真实工单。
- 数据库文件。
- 运行日志。
- `outputs`。
- 虚拟环境。
- Docker 数据卷。
- 旧流程使用的业务标准文件。

部署人员从 `.env.example` 新建 `.env`，密钥通过企业密钥系统或受控环境变量注入。

## 4. 接口开发人员收到什么

接口接入交付包含：

- `automation-api-reference.md`。
- OpenAPI 页面地址。
- `examples/second_part_batch.example.json`。
- 分类字典接口说明。
- Qwen3查重拦截与批量提交说明。
- 幂等重试规则。
- 错误码说明。
- 联调环境地址。
- 单独安全传递的集成密钥。

生产主入口：

```http
POST /api/v1/integration/second-part/records:batch
```

分类字典：

```http
GET /api/v1/integration/taxonomy
```

知识消费和反馈：

```http
POST /api/v1/knowledge/search
POST /api/v1/integration/retrieval-events:batch
```

## 5. 品类扩展方式

初始配置包含：

- 手机
- 平板
- 笔记本
- 相机机身
- 相机镜头
- 耳机
- 手表
- 游戏机
- 手写笔
- 学习机

默认配置文件：

```text
src/answer_hub/product_categories.json
```

生产环境可通过下列环境变量覆盖：

```dotenv
ANSWER_HUB_PRODUCT_TAXONOMY_PATH=/absolute/path/product_categories.json
```

增加新品类时：

1. 在 CZ 建立叶子分类。
2. 在品类配置中增加稳定编码、名称和别名。
3. 用至少两条脱敏记录执行技术冒烟。
4. 完成业务验收后启用品类。

未知品类进入人工确认，不得默认归入手机。

## 6. 当前无标准引用边界

当前批量链路只使用完整会话、历史实际回复和脱敏案例图。旧标准流程保留为历史兼容代码，但不作为本期部署入口，也不需要随交付包提供标准文件。

候选字段固定为10项，`关联标准项`字段保留。当前流程不主动生成标准关联；新候选默认为空，已有标准关联和来源版本保留并单独搁置。Qwen3只负责语义查重，不负责生成或补写质检标准。

## 7. 正式交付步骤

### 7.1 冻结版本

- 确认品类配置。
- 确认数据库迁移。
- 确认接口契约。
- 确认模型和 Prompt 版本。
- 自动审核保持关闭，除非已有正式验收报告。

### 7.2 构建交付包

推荐直接执行发布前全量验收并构建：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\verify_release.ps1 `
  -BuildPackage -Version 20260722-qwen3-batch-adapter-v8
```

如只需要单独构建：

```powershell
.\scripts\build_delivery_package.ps1
```

`verify_release.ps1`会在构建前执行根项目和CZ测试、Python编译、前端JavaScript语法检查、Compose检查；构建后扫描禁止文件、核对`checksums.sha256`，并在交付包内再次执行两套测试。任何一步失败都不得交付。
Windows用户也可以双击根目录的`发布前验收并打包.cmd`。

脚本会生成：

```text
handoff\answer-hub-delivery-<时间>\
handoff\answer-hub-delivery-<时间>.zip
```

### 7.3 交付前验证

```powershell
.\.venv\Scripts\python.exe -m pytest -q
& 'C:\path\to\python.exe' -m pytest -q
docker compose config --quiet
```

同时按照 `ACCEPTANCE_CHECKLIST.md` 完成业务验收。

### 7.4 安全传输

- 代码交付包通过公司受控网盘、制品库或内网文件系统交付。
- 密钥不放入压缩包，通过密钥管理系统单独配置。
- 标准和真实脱敏样本根据数据权限单独审批。
- 接收方核对 `checksums.sha256`。

### 7.5 部署与签收

接收方完成：

1. 解压并核对校验和。
2. 配置 `.env`。
3. 执行数据库迁移。
4. 启动 CZ、Embedding 和工作台。
5. 执行十品类冒烟。
6. 验证重复提交幂等复用。
7. 验证审核、查重、发布和检索反馈。
8. 由业务负责人和技术负责人共同签收。

## 8. 版本和回滚

每次交付应记录：

- 交付版本。
- 构建时间。
- Git 提交号；当前项目正式交付前必须初始化 Git。
- 数据库迁移版本。
- 品类配置版本。
- 标准快照版本。
- 模型名称。
- Prompt 版本。
- 验收报告版本。

生产部署至少保留上一个可运行交付包和对应数据库备份，禁止只覆盖服务器目录。
