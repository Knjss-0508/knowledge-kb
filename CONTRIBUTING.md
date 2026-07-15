# 协作开发说明

## 标准流程

每项功能或修复都使用独立分支和 Pull Request，不直接修改 `master`。

```bash
git switch master
git pull --ff-only origin master
git switch -c feature/功能名称
```

完成开发并验证后：

```bash
git add <本次修改的文件>
git commit -m "feat: 简要说明"
git fetch origin
git rebase origin/master
git push -u origin feature/功能名称
```

然后在 GitHub 创建 Pull Request，目标分支选择 `master`。

## 提交类型

- `feat:` 新功能
- `fix:` 缺陷修复
- `docs:` 文档
- `refactor:` 重构
- `test:` 测试
- `chore:` 构建、配置或维护

## 同时修改同一文件

推送前先执行：

```bash
git fetch origin
git rebase origin/master
```

发生冲突时，打开冲突文件，确认双方改动都被正确保留，再执行：

```bash
git add <冲突文件>
git rebase --continue
```

不要使用整文件覆盖的方式解决冲突，也不要对 `master` 强制推送。

## Pull Request 要求

PR 描述至少包含：

- 改了什么。
- 为什么修改。
- 如何验证。
- 是否涉及数据库、环境变量或部署配置。

合并前确认测试通过、没有敏感信息、没有无关文件。

## 发布版本

PR 合并后切回主分支：

```bash
git switch master
git pull --ff-only origin master
```

确认主分支稳定后再创建版本标签和 GitHub Release。

