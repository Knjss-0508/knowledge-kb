# 项目协作规则

## 语言

- 一切对话、提交说明、PR 描述和项目文档优先使用中文。

## Git 分支规则

- 禁止直接在 `master` 分支开发、提交或推送。
- 每项改动必须从最新 `master` 创建独立分支。
- 首次克隆项目后必须执行 `powershell -ExecutionPolicy Bypass -File scripts/setup-git-hooks.ps1`，启用主分支推送防护。
- 分支命名使用以下前缀：
  - `feature/`：新增功能
  - `fix/`：缺陷修复
  - `docs/`：文档改动
  - `chore/`：配置、构建和维护
  - `codex/`：由 Codex 执行的改动
- 开始开发前执行：

```bash
git switch master
git pull --ff-only origin master
git switch -c <分支名>
```

## 提交与同步

- 提交前只暂存本次任务相关文件，不混入无关改动。
- 推送前先同步远程主分支：

```bash
git fetch origin
git rebase origin/master
```

- 如果出现冲突，必须逐项确认两边改动，禁止直接整文件覆盖。
- 禁止对 `master` 使用强制推送。
- 如果钩子提示禁止推送 `master`，必须切换到独立分支并创建 PR，禁止使用 `--no-verify` 绕过。

## Pull Request

- 所有代码必须通过 Pull Request 合并到 `master`。
- PR 必须写明改动内容、验证方式和已知风险。
- PR 合并前必须确认：
  - 没有未解决的合并冲突。
  - 相关测试或检查已经通过。
  - 没有提交 `.env`、密码、Cookie、令牌或本地运行文件。
- 默认使用 Squash and merge，保持 `master` 历史简洁。

## 发布版本

- 版本标签只能基于已合并到 `master` 的提交创建。
- 发布前必须先拉取最新 `master` 并完成必要验证。
- 禁止在功能分支上直接创建正式版本标签。

