# 机型配置信息同步

`机型配置信息`是由飞书表格托管的精确知识来源。它可以通过专用 Excel 模板或
服务端 JSON 命令同步，但不使用普通知识导入、语义查重、Embedding、召回阈值
或 TOP 配置。

## 数据来源

脚本支持两种模式：

1. **单表兼容模式（默认）**：读取历史配置中的一个工作簿/Sheet，适合现有
   `w3Caff / 119 / 平板电脑` 来源，不改变原有 checkpoint。
2. **Wiki 多来源模式**：配置 `MODEL_CONFIG_SYNC_WIKI_ROOT` 后，从指定 Wiki
   节点递归遍历所有子节点，打开每本 `sheet` 工作簿，查找名为
   `个性化配置信息` 的 Sheet。每个来源按自己的 revision、内容摘要和错误状态
   单独处理，不会因某一本表读取或写库失败而阻塞其他来源。

品类通过 `deploy/systemd/model-configuration-sources.example.json` 中的映射确定，
支持按 spreadsheet token、Sheet ID、Wiki 节点 token 或工作簿标题匹配。生产映射
应填写 spreadsheet token；填写后必须精确命中该 token，不会因同名工作簿或复用的
Sheet ID 回退到错误类目。自定义映射会覆盖相同定位键的内置映射；不能
只写 `sheet_name`，避免把一个类目误绑到所有工作簿。匹配不到品类的来源会被列入
`skipped_sources`，不会猜测品类，也不会写入错误类目。
当前仓库附带了生产中已识别的十个来源映射示例；新增工作簿时只需在映射文件中
补一项，不需要改同步程序。Wiki 中发现但缺少目标 Sheet、目标 Sheet 改名、映射
冲突或无法读取的已配置工作簿都会进入 `skipped_sources`/`failed_sources`，因此整体结果
会是 `partial`，不会被误报为全量成功。

Wiki 下没有目标 Sheet 且没有命中任何来源映射的工作簿（例如同一 Wiki 中的
其他非机型配置表）会记录在 `ignored_non_target_sources`，仅作审计信息，不会导致
定时任务失败。只有命中已有来源映射却缺少目标 Sheet 的工作簿才会进入
`skipped_sources` 并触发 `partial`。

每次 Wiki 发现还会比对 `sources/` 下的历史来源 checkpoint：上次存在、这次未再
发现的来源会同时列入 `missing_previous_sources` 和 `skipped_sources`，只告警、
不删除中台已有知识。若历史 checkpoint 已损坏，脚本也只报告并保留原文件，
不会用空状态覆盖。

同步程序读取并校验以下字段：

- 来源知识ID（选填，兼容旧表头“知识ID”）、标题
- 品牌ID、品牌
- 型号ID、型号
- 综合内容（兼容部分来源使用的 `综合信息`）

旧表中的是否有卡槽、Home键、指纹识别、3D面容、内置手写笔、闪光灯、
蜂窝网络和光线传感器列会被忽略；`是否更新`仅保留在来源追溯中，
不作为发布或失效依据。

## 1. 使用 lark-cli 导出

在已完成飞书用户授权的 Windows 环境执行：

```powershell
pwsh -NoProfile -File scripts/export-model-configurations-from-lark.ps1 `
  -OutputPath D:\temp\model-configurations.json
```

脚本会：

1. 使用 `lark-cli --as user` 读取目标工作表。
2. 按工作表元数据的 `row_count` 和 `column_count` 动态读取完整区域（真实来源
   列数约为 20–26，部分来源需要到 Z 列，不能固定为 A:Q）。
3. 校验必填字段、正文列别名（`综合内容` 优先，`综合信息` 兼容）和重复的
   品类ID、品牌ID、型号ID组合。
4. 生成 UTF-8 JSON，同步文件不包含飞书 OAuth 令牌或应用密钥。

也可以在知识工作台下载“机型配置信息”专用 Excel 模板并批量上传。
模板和上传接口均使用 `import_type=model_configuration`；专用工作表名为
“机型配置信息”，同时兼容原始“个性化配置信息”工作表。Excel 文件会先完成
整本校验，再在单个数据库事务中调用同一套幂等同步服务；任一冲突都会整批回滚。

## 2. 在服务端执行幂等同步

生产 Compose 的 backend 容器没有挂载项目目录。将 JSON 文件放到服务器主机后，
通过标准输入交给容器内同步程序：

```bash
docker compose exec -T backend \
  python -m app.scripts.sync_model_configurations \
  - < /path/on/host/model-configurations.json
```

也可以先执行
`docker compose cp /path/on/host/model-configurations.json backend:/tmp/model-configurations.json`
再传容器内路径；完成后应删除 `/tmp` 临时文件。

同步规则：

- `knowledge_origin = model_configuration`
- `business_type = self_operated`，仅用于满足现有存储约束；精确查询不按请求业务类型排除
- `category_id = cat-extra-knowledge`
- `source_record_id = 其他知识库的可选追溯ID`（可为空，不参与机型配置唯一识别）
- `source_knowledge_key = model-configuration:品类ID:品牌ID:型号ID`
- 状态直接写为 `published`
- 不创建查重向量和检索向量

同一数据重复执行不会新增知识；字段变化会保留中台知识ID并原地更新，同时写入
变更日志。品类ID、品牌ID、型号ID组合发生冲突时整次同步失败并回滚。
源表行消失不会自动废弃旧知识，避免在没有明确禁用字段时误删。

## 3. 定时自动同步

生产环境可以使用仓库中的
`scripts/sync_model_configurations_scheduler.py` 配合 systemd timer。脚本单独部署在
`/opt/knowledge-kb-model-sync`，不覆盖正在运行的知识库应用工作树；运行在
安装了 `lark-cli` 的调度主机，不运行在 backend 容器内，因为 backend 镜像不包含
`lark-cli`。调度主机可以就是服务器，也可以是办公网 Windows 主机；后者通过
SSH 将 JSON 送入服务器的 `kb-backend` 容器，不需要开放新的公网端口。

默认每 15 分钟执行一次，单次执行最长约 45 分钟（包含网络重试），流程为：

1. 读取飞书文档 `revision`；与上次成功 checkpoint 相同则直接结束。
2. revision 变化后读取完整工作表并执行与手动导出相同的全表校验。
3. 读取结束后再次检查 revision；读取期间发生变化则丢弃本次结果并重试。
4. 校验通过后调用容器内 `sync_model_configurations`，成功后才写入 checkpoint。
5. 记录新增、修改、未变化结果；源表删行默认不自动废弃知识。

安装模板：

```bash
install -d -m 0750 /opt/knowledge-kb-model-sync \
  /opt/knowledge-kb-model-sync/state
# Node.js >= 16；固定为当前已验证的 lark-cli 版本，并安装在调度目录内。
npm install --prefix /opt/knowledge-kb-model-sync @larksuite/cli@1.0.87
/opt/knowledge-kb-model-sync/node_modules/.bin/lark-cli --version

install -m 0750 scripts/sync_model_configurations_scheduler.py \
  /opt/knowledge-kb-model-sync/sync_model_configurations_scheduler.py
install -m 0640 deploy/systemd/model-configuration-sync.env.example \
  /opt/knowledge-kb-model-sync/model-configuration-sync.env
install -m 0640 deploy/systemd/model-configuration-sources.example.json \
  /opt/knowledge-kb-model-sync/model-configuration-sources.json
install -m 0644 deploy/systemd/knowledge-kb-model-configuration-sync.service \
  /etc/systemd/system/knowledge-kb-model-configuration-sync.service
install -m 0644 deploy/systemd/knowledge-kb-model-configuration-sync.timer \
  /etc/systemd/system/knowledge-kb-model-configuration-sync.timer
systemctl daemon-reload
```

启用前必须满足：

- 服务器安装 `lark-cli`；
- 飞书应用申请 `sheets:spreadsheet:read`，并获得目标表访问权；
- 服务器上的运行用户（模板默认是 root）完成 `lark-cli` bot 身份配置；
- Wiki 多来源模式还需要给同一应用申请并发布 `wiki:wiki`、
  `wiki:wiki:readonly`、`wiki:node:read`、`wiki:node:retrieve`，并将目标 Wiki
  空间/根节点及其子节点工作簿共享给该 Bot；
- 先执行一次 `--check-only`，确认所有来源的读取、品类映射和行校验通过，再启动 timer。

完成 Bot 配置和环境文件填写后，首次上线按以下顺序执行。`--check-only`
只读取和校验，不会写入知识库；命令返回成功后才启用定时器：

```bash
set -a
. /opt/knowledge-kb-model-sync/model-configuration-sync.env
set +a
/usr/bin/python3 /opt/knowledge-kb-model-sync/sync_model_configurations_scheduler.py --check-only
systemctl enable --now knowledge-kb-model-configuration-sync.timer
```

服务器首次配置 Bot 时，不要复制 Windows 上的 `lark-cli` 配置文件，也不要把
App Secret 写入环境文件、命令参数、仓库或日志。以 root 交互登录服务器后，使用
CLI 的标准输入方式完成一次性配置：

```bash
export HOME=/root
umask 077
lark-cli config init \
  --app-id <同一飞书应用的 App ID> \
  --app-secret-stdin --brand feishu --lang zh_cn
```

CLI 会从终端标准输入读取 App Secret 并存入服务器本地安全存储。配置完成后先执行
`lark-cli auth status --verify --json`，确认 `identity=bot` 且状态为 ready，再做
`--check-only`；不需要也不应迁移用户 OAuth。

多来源模式的环境文件至少设置：

```dotenv
MODEL_CONFIG_SYNC_WIKI_ROOT=https://zhuanspirit.feishu.cn/wiki/HtX2wadYhiqsdwk7Q4yctvNDnlc
MODEL_CONFIG_SYNC_SOURCE_CONFIG=/opt/knowledge-kb-model-sync/model-configuration-sources.json
MODEL_CONFIG_SYNC_SHEET_NAME=个性化配置信息
```

配置 `MODEL_CONFIG_SYNC_WIKI_ROOT` 后，`MODEL_CONFIG_SYNC_SPREADSHEET_TOKEN`、
`MODEL_CONFIG_SYNC_SHEET_ID` 只作为兼容单表默认值，不会限制 Wiki 遍历范围。
每个来源的 checkpoint 位于：

```text
/opt/knowledge-kb-model-sync/state/sources/<来源指纹>/state.json
```

来源指纹由 spreadsheet token 和 Sheet ID 的 SHA-256 前缀组成，不在文件名或日志中
暴露原始 token。单表模式仍使用原来的 `state.json`。

如果调度主机是 Windows、服务器只提供 SSH，可以直接运行：

```powershell
python scripts/sync_model_configurations_scheduler.py `
  --identity bot `
  --target ssh `
  --ssh-host 81.71.6.245 `
  --ssh-user root `
  --ssh-key 'D:\下载\Lark\81.71.6.245_id_ed25519' `
  --check-only
```

确认检查成功后，将同一命令交给 Windows 任务计划程序，每 15 分钟运行一次；
建议任务使用专用运行账号，私钥文件只授予该账号读取权限。不要把飞书 OAuth
令牌、应用密钥或私钥写进仓库、JSON、任务参数或日志。

Windows 上检查 Wiki 多来源时：

```powershell
python scripts/sync_model_configurations_scheduler.py `
  --identity bot `
  --wiki-root 'https://zhuanspirit.feishu.cn/wiki/HtX2wadYhiqsdwk7Q4yctvNDnlc' `
  --source-config deploy/systemd/model-configuration-sources.example.json `
  --target ssh `
  --ssh-host 81.71.6.245 `
  --ssh-user root `
  --ssh-key 'D:\下载\Lark\81.71.6.245_id_ed25519' `
  --check-only
```

`--check-only` 会遍历并校验所有已映射来源，但不会调用服务端写库。只要有来源
读取不完整、表头缺失、重复机型组合或品类映射缺失，结果会标记为 `partial`，
不会把该来源的半成品发送到中台。

检查和查看日志：

```bash
systemctl start knowledge-kb-model-configuration-sync.service
systemctl status knowledge-kb-model-configuration-sync.service
journalctl -u knowledge-kb-model-configuration-sync.service -n 100 --no-pager
cat /opt/knowledge-kb-model-sync/state/state.json
find /opt/knowledge-kb-model-sync/state/sources -name state.json -print
```

脚本不会把 OAuth 令牌或应用密钥写入同步 JSON、checkpoint 或日志。定时任务只
同步新增和修改，不会因为表格读取不完整、权限失效或临时网络错误而删除旧知识。
多来源运行返回 `status=partial` 时，已成功来源的 checkpoint 会保留，失败来源会
记录 `last_error`，systemd 服务以非零状态结束，下一次定时运行会继续重试失败来源。

## 4. 精确查询

插件通过独立 HTTP 请求调用现有
`/api/v1/integration/standard-search`，并设置
`requestMode=model_configuration`，只提交品类、机型名称与可用 ID；品牌信息
为可选的额外精确约束。普通 `requestMode=semantic` 请求不再查询机型配置。
服务端：

1. 未提供品牌时，优先严格匹配品类 ID + 型号 ID。
2. 提供品牌时，优先严格匹配品类 ID + 品牌 ID + 型号 ID。
3. ID 组合未命中且相应名称完整时，按规范化后的名称组合精确匹配。
4. 完整 ID 组合命中时以 ID 为准，名称变化不会反向否决该命中。
5. 信息不足、可用的 ID/名称组合未命中或出现多条匹配时，返回未检索到。

结果位于响应的独立 `modelConfiguration` 字段，不进入两个语义候选池，也不参与
分数、阈值、TOP、候选上限或召回质量反馈。命中结果只返回综合内容，
不再返回卡槽、Home键等拆分属性。插件在工单品类和机型读取完整后请求一次，
并按工单 ID 缓存命中或未命中结果；后续会话变化不重复请求。
