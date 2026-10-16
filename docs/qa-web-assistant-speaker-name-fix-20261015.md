# 答疑工作台聊天记录发送者姓名：线上修复记录与交接（2026-10-15）

承接 `docs/qa-web-assistant-speaker-name-resolution-20261014.md`（命名办法与「角色词 / 真名跳变」根因）。
那篇的结论停在「根因在上游渲染期，建议上游修」。

**最终结论（2026-10-16 上午复核，前后端均已逐字节比对）**：这个修复**上游源码里有，线上也已经生效，
而且线上前后端跑的就是该源码的提交版本**。
套壳前端仓库 `https://gitcode.com/gcw_dIwpN56T/project1`（本机克隆 `E:\dsh-scratch\project1`）
的 `main` 分支提交 `710866e`（v0.9.6，入库时间 **2026-10-15 19:36:14 +0800**）里，
`frontend/src/pages/WorkOrderDetail.tsx:157-159` 已写成
`detail?.qaTrainerName || trainerMap[trainerZzUserId || qaTrainerZzUserId] || '答疑'`；
把该提交的 `frontend/` 在本机重新构建出来的产物，与线上正在提供的 bundle
**逐字节完全相同**（同为 1,526,947 B，`sha256:288f8ebc…4d38436`）；后端 `server.js` 也与本机
**sha256 完全相同**（203,207 B，`sha256:5db8e024…3771c9`，pm2 已在 19:36 重启过）。
**线上没有任何待补的部署动作，显示修复属于源码自带，也不需要再打产物补丁。**
本文前半记录 2026-10-15 白天那次线上抢修的经过与验证（当时以为源码丢了，故先补产物），
后半是源码定位、逐字节取证与收尾建议。
（中台侧的配套拦截 PR #199 已于 2026-10-14 上线，见第五节。）

## 一、根因回顾（一句话）

聊天消息本身不带发送者姓名，姓名是渲染期用「订单详情 + 答疑名单」两份异步数据现查的；
`yx()` 既算名字又算归属，第三分支的兜底是 `{name:"答疑", isMe:true}`——
**同一个兜底同时决定了「叫什么」和「算谁发的」**，所以详情缺失时整屏都会变成「答疑」且右对齐。

关键证据（线上 bundle 静态阅读，未执行）：第三分支原本走 `Nk(n)`：

```js
function Nk(e){const t=e!=null?String(e):"";if(t)return Cp.get(t)}
```

`Cp` 是「appUid → 真实姓名」的 Map，但线上 bundle 里**只有读、没有写**：
`Cp` 的填充函数（原先只由 `/qa-roster` 名单里同时带 `appUid + name` 的行驱动）在这份套壳前端里
从未被调用/从未拿到行数据，于是 `Nk()` 恒返回 `undefined`，
第三分支实际恒等于兜底分支——这就是「明明名单里有杨培鹏，界面还显示答疑」的直接原因。

## 二、实现的修复（最小侵入：只补数据源，不动归属判定）

把「答疑名单」的填充口径从「只有 appUid」扩到「训练页名单的 `zzUserId`」，即
`appUid → 姓名` 的 Map 同时接受 `trainerZzUserId` / `qaTrainerZzUserId` 作为 key。

改后的取值表达式（线上实际运行形态，变量名随构建变化，语义不变）：

```js
// 头部文案（原 const w = r?.qaTrainerName || "答疑"）
E = (r?.qaTrainerName) || v[String(r?.trainerZzUserId || r?.qaTrainerZzUserId || "")] || "答疑"

// 气泡发送者（与头部共用同一份映射 v）
p = (i) => { const t = i ? String(i) : "";
  if ($ && t === $) return { name: I_engineer, isMe: false };  // 回收师侧
  if (w && t === w) return { name: "我", isMe: true };
  if (t && v[t])    return { name: v[t], isMe: true };          // ← 命中名单：真名
  return { name: "答疑", isMe: true }; }                        // ← 仍未命中：保持原样
```

**刻意不改的东西**：`isMe` 的归属判定逻辑保持原样。
也就是说这次只解决「显示什么名字」，不解决「详情为空时回收师消息被画到右侧」——
后者属于 `docs/qa-web-assistant-speaker-name-resolution-20261014.md` 第七节建议 1，
需要上游按会话方向/消息序号重做归属，风险更高，不在本次范围内。

## 三、线上落地与验证

- 目标文件：`/www/wwwroot/zzdy.powerzhuan.cn/frontend/dist/assets/index-*.js`（无 sourcemap）。
- 落地方式：**对构建产物打补丁**（备份留在
  `/www/wwwroot/zzdy.powerzhuan.cn/_backup-answerer-name-20261015-1829/` 与 dist 内 `_backup-*` 目录）。
- 验证（真实浏览器 + 真实登录态，Playwright，页面错误数 0）：
  - 工单 `2110638084679799377`，头部 `chat-title` 行：
    `工单ID：2110638084679799377 | 问题类型：质检问题 | 回收师：3刘雨枫 | 答疑员：杨培鹏 | 状态：已完成`
  - 修复前同位置为 `答疑员：答疑`；修复后 `答疑员：杨培鹏`（名单里的真名，不再是角色词）。
- **2026-10-15 晚间复核（决定性，推翻下面两条旧判断）**：
  - 把仓内 `710866e` 的 `frontend/` 在本机 `npm ci && npm run build` 重出产物后，
    得到的入口文件**与线上正在提供的那一份逐字节完全相同**：
    两者都是 1,526,947 B、`index.html` 里引用的都是 `assets/index-DFKASPWw.js`，
    SHA256 同为 `288f8ebcb08501951e87959a6228c6dd06bc3e8b2ea6aaeca80f952fa4d38436`；
    `index.html` 也同为 `sha256:05d42baaebd335f08c1d521999e1491f12367df73a75aba149fc57759c3a096e`。
  - 既然线上产物 = 该提交的源码构建，**线上这一版就是「源码构建」而不是「打过补丁的产物」**，
    而源码 `WorkOrderDetail.tsx` 里本就带着姓名兜底 → **线上已经包含修复**，无需重建、也无需再打补丁。
  - 因此先前「线上是 19:32 旧构建、缺 19:36 入库的修复」的判断不成立：19:32 那次就是按
    `710866e` 构建部署的（提交时间 19:36:14 是**入库时间**，源码改动早于构建）。
- **18:29 那次产物补丁已经不在线上，但不需要再补**：补丁版是 `index-60714913.js`
  （1,525,299 B，仍完整留档在 `/www/wwwroot/zzdy.powerzhuan.cn/_backup-closeway-20261015-1845/frontend-dist/`，
  `sha256:607149130a445a0ccfe9a10805ff7a00d0548fd83781e6cccf11c9bb1bf8ead2`），
  19:32 起被 `index-DFKASPWw.js` 取代。两者都带同一表达式（`qaTrainerZzUserId` 1 次），
  且新版来自源码，属于**殊途同归的收敛**，不需要回滚动作。
- 仍留档的旧判断（供追溯，勿再据此行动）：
  - 「补丁在某次并发重建后依然存活」——当时只按 bundle 关键字计数（`qaTrainerZzUserId` 1 次、
    `qaTrainerName` 2 次）推断，而 `trainerMap` 在压缩产物里被改名为 `v`，
    该计数**无法区分「打过补丁」与「源码构建」**；逐字节比对才是否证它的证据。
  - 「重建是手工补丁而非流水线」——人手改痕迹确实存在（见下），但 19:32 那次前端是**真源码构建**。
- 人手改痕迹（真实存在，属后端/部署侧）：`/root/server.js.bak-20261015-175727 / 180841 / 184744 / 185119 /
  185354 / 190159 / 190827 / 191816`（还有 `index.html.bak-20261015-180841`、
  `package.json.bak-20261015-193610`）；PM2 日志里 `19:18:19`、`19:36:11` 各有一次 `qa-shell` 冷启动；
  `/www/wwwroot/zzdy.powerzhuan.cn/` 根下有 `_backup-closeway-*`、`_backup-im-frames-*`、
  `_backup-media-upload-*`、`_backup-uploadfix-*` 等 `_backup-*` 目录，且**没有 `src/`、没有 vite/tsconfig/*.tsx**
  ——这台机器上确实有人长期直接改产物/后端文件，但**不是**前端 19:32 那一次。

## 四、源码定位结论与收尾建议

1. **修复已在源码里，不需要再提源码 PR**。套壳前端仓库是
   `https://gitcode.com/gcw_dIwpN56T/project1`（本机克隆 `E:\dsh-scratch\project1`，`main` = `710866e` v0.9.6，
   提交时间 `2026-10-15 19:36:14 +0800`）。它的 `frontend/src/pages/WorkOrderDetail.tsx` 已经是修好的版本：

   ```ts
   // frontend/src/pages/WorkOrderDetail.tsx:147-152
   const trainerMap = useMemo(
     () => Object.fromEntries(
       trainers.filter((t) => t.zzUserId && t.trainerName).map((t) => [String(t.zzUserId), t.trainerName]),
     ) as Record<string, string>,
     [trainers],
   );
   // :153-161
   const custUid = detail ? String(detail.creatorAppUserId || detail.creatorZzUserId || '') : '';
   const meUid = user?.zzAppUid ? String(user.zzAppUid) : '';
   const qaName =
     detail?.qaTrainerName ||
     trainerMap[String((detail as { trainerZzUserId?: string } | null)?.trainerZzUserId || detail?.qaTrainerZzUserId || '')] ||
     '答疑';
   const custName = detail?.engineerName || detail?.adminName || '回收师';
   // :163-171
   function who(uid) {
     if (custUid && u && u === custUid) return { name: custName, isMe: false };
     if (meUid && u && u === meUid) return { name: '我', isMe: true };
     if (u && trainerMap[u]) return { name: trainerMap[u], isMe: true };
     return { name: qaName, isMe: true };
   }
   ```

   也就是说：线上 bundle 里那两个「答疑」兜底（头部 `qaTrainerName` 与气泡 `who()` 第三分支）
   在这份源码里本来就是**同一段逻辑的相邻分支**，我们 18:29 的产物补丁相当于把源码写法原样搬了回去。
2. **前端已闭环：线上产物 = `710866e` 源码构建，且自带修复**（决定性证据）：
   - 本机在 `E:\dsh-scratch\project1\frontend` 执行 `npm ci && npm run build`（vite 5.4.21，4079 modules），
     产出入口 `dist/assets/index-DFKASPWw.js` 1,526,947 B，与线上
     `/www/wwwroot/zzdy.powerzhuan.cn/frontend/dist/assets/index-DFKASPWw.js` **逐字节相同**
     （`sha256:288f8ebcb08501951e87959a6228c6dd06bc3e8b2ea6aaeca80f952fa4d38436`）；
     `index.html` 亦相同（`sha256:05d42baaebd335f08c1d521999e1491f12367df73a75aba149fc57759c3a096e`）。
   - 所以线上前端**不需要重新构建**，浏览器加载的就是带姓名兜底的源码产物。
   - 该产物里 `qaTrainerName` 出现 2 次（头部与 `who()` 兜底）、`qaTrainerZzUserId` 1 次，
     而 `trainerMap` 为 0 次是因为压缩后改名（源码里的 `v`）——**不能再把 `trainerMap` 计数当作判据**。
3. **后端也已闭环：线上 `server.js` 就是 `710866e` 的工作区文件（2026-10-16 上午补证）**：
   - 两端 `sha256` 逐字节相同 —— 线上 `/www/wwwroot/zzdy.powerzhuan.cn/server.js` 与本机
     `E:\dsh-scratch\project1\server.js` 同为 **203,207 B**、
     `sha256:5db8e0247978bde14aa808c05eb72b1bb706c205c6beca39aea209c8363771c9`。
     该文件在工作区是 CRLF、在 git 对象里是 LF（`core.autocrlf=true`），所以
     `git cat-file -s HEAD:server.js` 只有 199,737 B 而 `git status` 依然干净，**这不是差异**。
   - 关键字两端一致：`已读水位` 7 次、`水位` 19 次、`readState` 16 次、`readWatermark` 0 次
     （`readWatermark` 是别人先前探针里的口误，源码里根本没有这个标识符）。
   - pm2：`zzdy-powerzhuan` v0.9.6，pid 3572709，uptime 13h，启动时刻 `2026-10-15T11:36:11Z`(= 北京 19:36)，
     `script path` / `cwd` 都指向该站点目录 —— **19:36 已随 v0.9.6 一起重启过**。
   - ⚠️ 先前一度判定「线上后端落后一版、缺已读水位、需要覆盖」，那条结论**已被本轮复核推翻**：
     误判来自把一个 CRLF 副本的字节数与 git 对象的 LF 字节数直接相减，并把搜索词写成 `readWatermark`。
     **不要再按那条结论去覆盖线上 `server.js`。**
4. **建议的收尾方式（只剩一件非技术小事）**：
   1. 前端与后端都已与 `710866e` 一致，**不需要重新部署任何东西**；以后重建/重启前记得先
      `git status` 确认工作区（该仓的工作区改动就是线上正在跑的那一份）。
   2. 打补丁式的线上改动（`/root/server.js.bak-20261015-*`、站点内 `_backup-*` 那一串）此后应走该仓自己的分支流程。
      ⚠️ 该仓 `main` 的 `710866e` 在本机是**浅克隆**（`.git/shallow` 含该 commit，`git log` 显示 `(grafted)`），
      所以「它是不是没有父提交的导入式初始提交」**无法在本机证实**（要证实需 `git fetch --unshallow`）。
      也就是说：既不能说「这个仓库没有历史」，也不能从本机断言它有几条历史。
5. **已确认的相邻事实（避免重复找）**：
   - `/opt/voc-workbench/current/frontend` 是 `voc-review-workbench`（Vue 3 + Element Plus），
     源码里没有 `qaTrainerName` / `qaTrainerZzUserId` / `qa-roster`，**是另一个产品，不要改错仓库**。
   - 桌面端「答疑工作台」的源码仓库是私有的 `Cyj-090900/dayigongzuotai`
     （`package.json`：`answer-workbench` v2.7.8，appId `com.answer.workbench`，React 19 + antd 6 + Vite 7，
     更新源 `https://qa.powerzhuan.cn/updates/`）——它是** Electron 壳**，
     内部用 `WebContentsView` 打开 `https://zzdy.powerzhuan.cn`，`src/` 里**没有** `qaTrainerName`，
     所以它不是待改的仓库（但它请求 `queryQuestionFormDetail?sceneType=1`，若有同款头部需另行确认）。
   - 同团队另一个网页版的仓库范式：`https://gitcode.com/gcw_dIwpN56T/zntj`
     （本机克隆在开发者 Mac 的 `~/答疑智能推荐助手 web 版`，凭据在 `~/.git-credentials`，
     线上部署在 `/www/wwwroot/zntj.powerzhuan.cn/web/`）——正是它把线索指向了 gitcode，
     与本次定位到的 `project1` 同一命名空间。
   - gitcode 上的 `project1` **需要凭据才能读**（不是公开仓库）：带全局 Git Credential Manager 凭据时
     `git ls-remote https://gitcode.com/gcw_dIwpN56T/project1` 成功，加 `-c credential.helper=` 后立刻
     `fatal: could not read Username for 'https://gitcode.com'`；网页侧仓库主页 **403**、
     `raw.gitcode.com/.../README.md` **403**、`api.gitcode.com/api/v5/repos/...` **401**、
     commits 页面 **200 但只返回 SPA 空壳**（不含提交数据）。
   - 远端 refs（2026-10-16 核查）：`HEAD` 与 `refs/heads/main` 都是
     `710866efa8f103585a3115fd0c8f2e73dd1c93ac`，默认分支 `main` 且是唯一 head，**远端没有任何 tag**
     —— `v0.9.6` 只是提交信息里的文字，**不存在 v0.9.6 标签**；`git fetch --dry-run` 无输出，本地不落后也不领先。
   - 本机克隆在 `E:\dsh-scratch\project1`；中台仓库 knowledge-kb 全历史里没有套壳前端源码
     （`git log --all -S 'qa-shell-server'` 与 `--diff-filter=A -- '*.tsx'` 均 0 命中）。

> **附：上线记录欠账的合规化进度（2026-10-15 晚）**
> 本次线上抢修的两条记录已走 PR 合入 `master`：**PR #201**（本文档首版）与
> **PR #202**（`docs/production-deploy-20261014-blind-pool-state` 分支的盲标池退池上线记录）。
> 两个 PR 均按用户指示以 **Squash and merge** 合并（#201 → `01f9ec9`，#202 → `7b52c74`），
> 相关分支已删除。`master` 与 `origin/master` 现已一致。

## 五、答疑中台侧（与本修复正交，已上线）

- 会话号工单不再入池、存量自动退池：PR #199 已合并（`e3b361f` 之前的 `99fe345`），
  并已按 `docs/production-deploy-20261014-blind-pool-state.md` 上线——
  线上后端镜像 `knowledge-kb-backend:pool-state-e3b361f`（构建于 2026-10-14 22:43 +0800），
  切换后 worker 在第一个轮询周期内完成存量退池：`pool_state='retired'` 共 **290** 张工单，
  全部 `retire_reason='session_number'`（切换前为 0）。
  本次核对线上容器：`kb-backend  knowledge-kb-backend:pool-state-e3b361f  Up 21 hours`。
- 注意：那份上线记录**仍停留在分支 `docs/production-deploy-20261014-blind-pool-state`，没有合并进 master**，
  属于同一类「结论没进主干」的欠账，建议一并补 PR。
- 本次修复解决的是「名单里有名字却显示答疑」；#199 解决的是「详情为空导致整屏兜底」。
  两者互不替代，目前都已生效。

## 六、本次取证方式（可复现）与踩过的坑

- 线上 bundle 用 `curl` 下载后静态阅读（`grep -o -E '.{160}qaTrainerZzUserId.{200}'`），未执行任何上游代码。
- 浏览器复核（2026-10-15 18:30 那次成功）：服务器上 Playwright + Chromium，注入登录态 cookie（`dsh_sid`）后打开
  `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<工单号>&sceneType=2`，
  读 `chat-title` 行文本并统计 `pageerror` 数量；当时得到
  `工单ID：2110638084679799377 | 问题类型：质检问题 | 回收师：3刘雨枫 | 答疑员：杨培鹏 | 状态：已完成`、页面错误 0。
- 2026-10-16 上午再跑同款浏览器复核时**登录态已失效**：`sessions.json` 里那个 `dsh_sid` 已过期，
  页面回落到「选择登录方式」登录页（`.chat-title` 为空、`queryQuestionFormDetail` 直接返回
  `{"respCode":401,"errorMsg":"session expired","needLogin":true}`）。
  **再要跑浏览器端到端复核，必须先让本人重新登录拿一个新鲜的 `dsh_sid`**；
  在此之前，逐字节比对（源码 ↔ 构建产物 ↔ 线上文件）就是可用的最强证据。
- 只读探针脚本一律放在本地 `.dsh-local/probe-answerer-*.sh`（不提交），
  以 `Get-Content -Raw ... | ssh root@<host> "bash -s"` 方式执行；**必须确保 LF 行尾**，
  否则 `\r` 会污染变量赋值导致 `grep: /index-xxx.js: No such file or directory` 这类假故障。
- 两个实测坑，别再踩：
  1. 从 PowerShell 往 `ssh ... bash -s` 管道投喂**含中文的脚本**会被控制台编码打乱
     （实测把脚本里的中文写成乱码，甚至让 heredoc 里的 node 脚本语法错误）。
     **探针脚本一律用纯 ASCII 写**，中文匹配改用 `'\u6768\u57f9\u9e3f'` 这类转义；
     一定要写中文时用 `[System.IO.File]::WriteAllText(path, text, UTF8Encoding(false))`，
     不要用 `Set-Content`。
  2. 比较「线上文件 ↔ git 对象」大小时必须区分行尾：该仓 `core.autocrlf=true`，
     工作区 CRLF、git 对象 LF，同一个文件 `wc -c` 与 `git cat-file -s` 会差几千字节；
     只有 **sha256 / 字节级比对** 才能下「是否同一份」的结论。
