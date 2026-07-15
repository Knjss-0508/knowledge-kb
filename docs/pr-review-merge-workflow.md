# 分支、Pull Request 与代码审核流程

## 目标

多人可以同时开发，但所有改动必须经过独立分支和 Pull Request（PR）进入 `master`。

这套流程不能取消代码同步，但可以做到：

- 不让半成品直接进入 `master`。
- 在合并前发现冲突和代码覆盖风险。
- 清楚看到每个人修改了什么。
- 对单个功能进行审核、测试、回退。
- 同时保留多个开发者互不冲突的改动。

## 基本规则

1. 禁止直接在 `master` 开发或推送。
2. 每项功能或修复使用独立分支。
3. 所有改动通过 PR 合并。
4. 推送 PR 前同步一次最新 `master`。
5. 有冲突时人工确认业务逻辑，禁止整文件覆盖。
6. 默认使用 Squash and merge。

## 开始开发

```bash
git switch master
git pull --ff-only origin master
git switch -c feature/功能名称
```

常用分支前缀：

```text
feature/  新功能
fix/      缺陷修复
docs/     文档
chore/    配置和维护
```

## 完成开发

只提交本次任务相关文件：

```bash
git add <修改的文件>
git commit -m "feat: 简要说明"
```

提交 PR 前同步最新主分支：

```bash
git fetch origin
git rebase origin/master
```

确认无误后推送：

```bash
git push -u origin feature/功能名称
```

然后在 GitHub 创建 PR，目标分支选择 `master`。

## 两个人同时开发

假设开发者 A 和开发者 B 从同一个 `master` 创建了两个分支：

```text
master
├── feature/a
└── feature/b
```

如果两个人修改不同文件或不同代码区域，Git 通常可以自动保留两边改动。

如果 A 的 PR 先合并，B 在合并前执行：

```bash
git fetch origin
git rebase origin/master
```

Git 会把 B 的提交重新应用到包含 A 改动的最新版代码上。

## 行号变化

Git 主要根据代码内容和上下文合并，不是只看行号。

例如原来的第 5 行：

```text
price = 100
```

开发者 A 改为：

```text
price = 200
```

开发者 B 在文件前面新增了 10 行，使原代码移动到第 15 行，但没有修改该内容。Git 通常能识别它还是同一段代码，并自动得到：

```text
第 15 行：price = 200
```

## 同一行冲突

原始内容：

```text
12345678
```

开发者 A 修改为：

```text
22335678
```

开发者 B 修改为：

```text
12347788
```

因为双方修改了同一行，Git 通常会产生冲突：

```text
<<<<<<< HEAD
22335678
=======
12347788
>>>>>>> origin/master
```

需要人工理解双方需求，再决定最终内容是否应该是：

```text
22337788
```

Git 不会自动理解数字每一位的业务意义。

解决冲突后执行：

```bash
git add <冲突文件>
git rebase --continue
```

## PR 审核流程

开发者提交 PR 后，把 PR 地址交给审核负责人。

审核负责人依次执行：

1. 查看两个 PR 的文件差异。
2. 检查是否修改了相同代码区域。
3. 检查是否误删、覆盖或重复实现。
4. 检查密码、Cookie、令牌、`.env` 和本地运行文件。
5. 拉取 PR 分支运行相关测试。
6. 验证 Docker 构建或本地功能。
7. 无问题后合并 PR。

## 两个 PR 有冲突

如果两个 PR 修改范围较大，不适合直接依次合并，可以创建集成分支：

```bash
git switch master
git pull --ff-only origin master
git switch -c integration/功能组合
```

将两个分支的改动合入集成分支，在这里统一处理冲突并测试。

确认最终结果后，为集成分支创建新的 PR，再合并到 `master`。

## 没人审核怎么办

当前仓库要求必须通过 PR，但审批人数是 `0`。

开发者可以：

1. 创建自己的功能分支。
2. 推送并创建 PR。
3. 在 GitHub 查看最终差异和冲突提示。
4. 完成本地测试。
5. 自行使用 Squash and merge 合并。

这样不需要其他人点击 Approve，但仍然不能直接推送 `master`。

## Codex 代为审核

可以把一个或多个 PR 地址发给 Codex，由 Codex：

1. 拉取 PR 分支。
2. 审查代码差异。
3. 运行测试和构建。
4. 处理或说明冲突。
5. 必要时创建集成分支。
6. 验证后通过 PR 合并到 `master`。

如果 PR 使用仓库所有者自己的 GitHub 账号创建，Codex 无法用同一个账号给该 PR 点击 Approve，但可以完成代码审查，并在审批人数为 `0` 时执行合并。

## 推荐日常流程

```text
拉取最新 master
        ↓
创建独立分支
        ↓
开发并本地测试
        ↓
rebase 最新 master
        ↓
推送分支并创建 PR
        ↓
人工或 Codex 审核
        ↓
Squash and merge
        ↓
删除已合并分支
```

