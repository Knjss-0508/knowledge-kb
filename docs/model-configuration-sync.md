# 机型配置信息同步

`机型配置信息`是由飞书表格托管的精确知识来源。它可以通过专用 Excel 模板或
服务端 JSON 命令同步，但不使用普通知识导入、语义查重、Embedding、召回阈值
或 TOP 配置。

## 数据来源

- 工作簿：`大模型【平板电脑】知识库`
- 工作表：`个性化配置信息`
- Sheet ID：`w3Caff`
- 类目：`119 / 平板电脑`

同步程序读取并校验以下字段：

- 来源知识ID（选填，兼容旧表头“知识ID”）、标题
- 品牌ID、品牌
- 型号ID、型号
- 综合内容

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
2. 按工作表当前物理行数读取完整 A:Q 区域。
3. 校验必填字段和重复的品类ID、品牌ID、型号ID组合。
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
`scripts/sync_model_configurations_scheduler.py` 配合 systemd timer。脚本运行在
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
install -d -m 0750 /etc/knowledge-kb /var/lib/knowledge-kb/model-configuration-sync
install -m 0640 deploy/systemd/model-configuration-sync.env.example \
  /etc/knowledge-kb/model-configuration-sync.env
install -m 0644 deploy/systemd/knowledge-kb-model-configuration-sync.service \
  /etc/systemd/system/knowledge-kb-model-configuration-sync.service
install -m 0644 deploy/systemd/knowledge-kb-model-configuration-sync.timer \
  /etc/systemd/system/knowledge-kb-model-configuration-sync.timer
systemctl daemon-reload
systemctl enable --now knowledge-kb-model-configuration-sync.timer
```

启用前必须满足：

- 服务器安装 `lark-cli`；
- 飞书应用申请 `sheets:spreadsheet:read`，并获得目标表访问权；
- 服务器上的运行用户（模板默认是 root）完成 `lark-cli` bot 身份配置；
- 先执行一次 `--check-only`，确认表格能完整读取，再启动 timer。

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

检查和查看日志：

```bash
systemctl start knowledge-kb-model-configuration-sync.service
systemctl status knowledge-kb-model-configuration-sync.service
journalctl -u knowledge-kb-model-configuration-sync.service -n 100 --no-pager
cat /var/lib/knowledge-kb/model-configuration-sync/state.json
```

脚本不会把 OAuth 令牌或应用密钥写入同步 JSON、checkpoint 或日志。定时任务只
同步新增和修改，不会因为表格读取不完整、权限失效或临时网络错误而删除旧知识。

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
