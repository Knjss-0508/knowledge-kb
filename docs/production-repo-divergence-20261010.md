# 线上源码树 vs 仓库 master 差异盘点（2026-10-10，只读）

> 目的：看清 `/opt/knowledge-kb`（线上跑的那份代码）和 GitHub 仓库 `master` 到底差在哪，
> 避免「哪天从仓库重新部署，把线上独有功能冲掉」，也避免线上继续堆积没人知道来历的改动。
> **本次盘点全程只读：没有改动线上任何文件，没有提交、没有部署。**

## 一、结论（先看这个）

1. **线上运行的代码 = 仓库 master 的内容**，只差换行符（线上是 CRLF，仓库是 LF）。
   全部后端功能文件在忽略换行符差异后与 master **逐字节一致**：
   `backend/app/main.py`、`backend/app/services/model_annotation.py`、`backend/app/services/model_connection.py`、
   `backend/app/routes/confidence_training.py`、`backend/app/routes/blind_labeling.py`、
   `backend/app/schemas/confidence_training.py`、`backend/app/routes/integration.py`、`backend/app/services/blind_labeling.py`、
   `backend/app/models/*`、9 个 `backend/migrations/versions/2026*`、`frontend/index.html`。
2. 仓库 master 早已包含线上那些「看起来是线上独有」的功能——「模型连接配置」与「我的已标注按标注活动时间筛选」
   已由 #176～#179 合入（`8b05885`）。**这条风险基本已经不存在了。**
3. 真正「线上有、仓库没有」的文件只剩 **6 个**（其中 1 个是备份文件），本次已把其中 5 个搬回仓库。
4. 线上还堆着 **607 个部署暂存/备份文件**（`.codex-deploy-*`、`*.bak-*`、`releases/` 等），与代码无关，
   属于磁盘治理，另行排期。

## 二、盘点方法

| 步骤 | 做法 |
|---|---|
| 线上文件清单 | 线上执行 `git ls-files --cached --others --exclude-standard`，剔除 `.env*`、`venv`、`node_modules`、`__pycache__`、`outputs`、`data`、`media`、`uploads`、`logs` 与二进制后缀，再逐个 `git hash-object` 算 blob 哈希 → 1070 行清单（本地留档 `.dsh-remote/prod-manifest.txt`，120,293 B / md5 `8aec0d5f1b33af75db0246d20c3fedd9`） |
| 仓库侧清单 | 本地 `git ls-tree -r origin/master`（基线 `8b0588552a3817f3578fce0fda8523ed709df52d`，465 个文件） |
| 逐文件比对 | 线上打包 73 个「值得看」的文件（`/tmp/prod-snap.tgz`，485,853 B）取回本地，仓库侧用 `git archive` 导出，逐个 `git diff --no-index --numstat --ignore-cr-at-eol` |
| 关键文件内容 | 逐个打印统一 diff，人工确认改的是什么功能 |

脚本：`.dsh-remote/31-inventory-prod-vs-repo.sh`、`32-compare-prod-vs-repo.ps1`、`33-compare-source-only.ps1`、
`36-per-file-diff.ps1`、`37-show-key-diffs.ps1`、`38-removals-only.ps1`、`40-sync-prod-source.ps1`、`41-add-prod-only-files.ps1`；
留档：`.dsh-remote/prod-manifest.txt`、`snap-list.txt`、`prod-snapshot/`、`repo-snapshot/`、`compare/`。

## 三、总数（基线 `8b05885`）

| 分类 | 数量 | 说明 |
|---|---|---|
| 只在线上 | 678 | 其中 **607 个是部署暂存/备份**；71 个「源码」里 **65 个是垃圾/备份**（`backend/app.bak-user-20260909-120555/*` 51 个、`frontend/index.html.before-*` 4 个、`Dockerfile.bak`、`160ms`、`grep`、`integration.py.current-bak` 等），**真正有内容的只有 6 个** |
| 只在仓库 | 73 | 部署/运维脚本、部分测试、`local-migration/` 全套工具、若干文档；线上没跑它们，不影响生产 |
| 两边都有但内容不同 | 64 | 用 73 文件快照细看：**33 个只差 CRLF（内容完全相同）**、31 个真不同、9 个线上独有 |

31 个「真不同」的方向判读（`+` 加 / `−` 删，仓库=旧，线上=新）：

- **线上更新的只有 1 个**：`scripts/check-embedding-tunnel.sh`（只多一个空行）。
- **其余 30 个都是线上落后于仓库**，例如 `backend/tests/test_automation_monitor_frontend.py +0 −481`、
  `backend/tests/test_media_storage.py +0 −307`、`backend/tests/test_retrieval_quality_feedback.py +0 −272`、
  `docs/deploy.md +8 −101`、`scripts/deploy.sh +22 −35`（仓库多了 S3/远端媒体存储的校验）、
  `.gitignore +0 −16`、`docker-compose.yml +1 −19`、`prototypes/answer-hub/src/answer_hub/*` 等。
  ⇒ 这些是**线上没享受到仓库的修复**，不是线上独有的功能。

## 四、线上独有的 6 个文件

| 文件 | 规模 | 处理 |
|---|---|---|
| `scripts/retention/prune_retrieval_events.py` | 96 行 | **已搬回仓库**。检索事件保留策略：默认保留 90 天，只删纯检索事件（`feedback_type` 为空或 `none`），人工反馈记录永久保留；`--dry-run` / `--days` 可选 |
| `scripts/retention/run-prune.sh` | 19 行 | **已搬回仓库**（保留可执行位）。从 `.env` 读 `DATABASE_URL`，用 `knowledge-kb-backend:latest` 容器跑上面的脚本；线上 root crontab 已挂 `30 3 * * *` |
| `docker-compose.panel-port.yml` | 9 行 | **已搬回仓库**。给 postgres 开面板端口 |
| `prototypes/answer-hub/tests/test_draft_quality.py` | 35 行 | **已搬回仓库**（本地跑通） |
| `prototypes/answer-hub/tests/test_images.py` | 30 行 | **已搬回仓库**（本地跑通） |
| `backend/app/routes/integration.py.current-bak` | — | 线上的一份备份文件，**不搬**；建议随磁盘治理一起清掉 |

保留策略的实际运行情况（只读核对 `/var/log/kb-retention.log`）：每天 03:30 正常执行，2026-10-10 那次
「总行数 406,286，最早事件 2026-08-17，超过保留期的纯检索事件 0 行」⇒ 目前无需清理。

## 五、线上需要清理的垃圾（还没动）

- `.codex-deploy-*` 暂存/备份目录约 20 个，最大是 `.codex-deploy-20260929-pre-release`（351 个文件）、
  `.codex-deploy-20260910-backup`（56）、`.codex-deploy-20261006-pr173`（30）；
- `backend/app.bak-user-20260909-120555/`（51 个文件）、`frontend/index.html.before-*`（4）、`backend/Dockerfile.bak`、
  `backend/app/routes/integration.py.current-bak`、`releases/`（14）、无意义文件 `160ms`、`grep`、
  `outputs-supplement-20260913-20260914.log`、`.env.bak-url`。

属于磁盘治理，建议与隐患体检报告（`docs/production-risk-audit-20261008.md`）一起排期，**本次未执行**。

## 六、建议的下一步

1. ~~把线上独有的真代码搬回仓库~~ → **本次已完成 5 个**（见第四节）。
2. **`.gitattributes` 补 `*.html text eol=lf`（建议再加 `*.py text eol=lf`、`*.md text eol=lf`）**：
   仓库已有 `.gitattributes`（`*.sh`、`docker-compose*.yml`、`.env.example`），但没有覆盖前端 HTML——
   这正是当年 `frontend/index.html` 被 CRLF 弄坏、以及 33 个文件出现「假差异」的原因。
3. **线上清理暂存/备份/垃圾文件**：先打包备份再删，预计释放数 GB。
4. **把仓库领先的部分部署到线上**（测试、文档、`scripts/deploy.*` 的 S3 校验等），属于「线上落后」的补齐，
   与功能无关，可择期做。
5. 之后再补两个老分支 `fix/confidence-decision-gate`、`fix/confidence-shadow-inspection-panel` 的 PR。

## 七、教训（写下来避免重犯）

- **对比之前必须先 `git fetch` 并对齐 `origin/master`。** 第一次盘点用的是过期的 `origin/master`（`4b5e01f`），
  于是把 #176～#179 刚合入的功能误判成「线上独有、仓库没有」；当时若直接「以线上为准」覆盖仓库，
  就会用线上较旧的代码把仓库的新功能删掉。发现后已丢弃那次改动，重新以 `8b05885` 为基线核对。
- **判断方向要看 `+N/−N`**：`+0 −N` 是线上更旧（缺内容），`+N −0` 才是线上更新。
- **换行符差异会伪装成内容差异**：必须用 `git diff --numstat --ignore-cr-at-eol` 再看一遍，
  33 个「不同」的文件其实一模一样。
- **本地 `.ps1` 必须纯 ASCII 或带 BOM**：Windows PowerShell 5.1 会把无 BOM 脚本按 ANSI 读，中文路径直接乱码。
- `git diff --no-index <目录> <目录>` 会把 CRLF 警告混进 stdout 且输出全量路径 ⇒ 应一文件一对调用。

## 八、本步的红线

- 不改线上任何文件；不提交、不推送、不部署线上；
- 不打印、不留存任何密钥明文（本次只用长度 + sha8 指纹描述）；
- 线上 git 工作区那 142 条改动**原样保留**，不动它的 HEAD。
