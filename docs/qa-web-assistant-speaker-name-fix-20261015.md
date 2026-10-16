# 答疑工作台聊天记录发送者姓名：线上修复记录与交接（2026-10-15）

承接 `docs/qa-web-assistant-speaker-name-resolution-20261014.md`（命名办法与「角色词 / 真名跳变」根因）。
那篇的结论停在「根因在上游渲染期，建议上游修」。

**最终结论（2026-10-15 复核）**：这个修复**上游源码里已经有了**——
套壳前端仓库 `https://gitcode.com/gcw_dIwpN56T/project1`（本机克隆 `E:\dsh-scratch\project1`）
的 `main` 分支在 **2026-10-15 19:36:14 +0800** 的提交 `710866e`（v0.9.6）里，
`frontend/src/pages/WorkOrderDetail.tsx:156-159` 已写成
`detail?.qaTrainerName || trainerMap[trainerZzUserId || qaTrainerZzUserId] || '答疑'`。
**真正的问题是线上 `frontend/dist` 是 19:32 的旧构建，没有包含 19:36 才入库的这行修复**——
源码已修、产物未重建。本文前半记录这次线上抢修的经过与验证（它让线上在重建前先有了正确显示），
后半是定位结论与收尾建议。
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
- **补丁在某次并发重建后依然存活**：2026-10-15 19:18 与 19:32 该 dist 被他人重建两次
  （入口 hash 从 `index-60714913.js` 变为 `index-DFKASPWw.js`，1,526,947 B），
  重建后的 bundle 里新表达式仍在（`qaTrainerName` 2 次、`qaTrainerZzUserId` 1 次、
  `trainerMap` 0 次 —— 前两个数字说明补丁在，`trainerMap` 为 0 说明它不是从源码构建的），
  浏览器复核头部仍为「答疑员：杨培鹏」。可见重建是基于**打补丁后的产物**做的增量改造，
  不是从干净源码重出。
- **「重建」的性质是手工补丁而非流水线重建**（2026-10-15 复核）：
  - 同一个人的手改痕迹在 `/root` 里成串：`server.js.bak-20261015-175727 / 180841 / 184744 / 185119 /
    185354 / 190159 / 190827 / 191816`（还有 `index.html.bak-20261015-180841`、
    `package.json.bak-20261015-193610`）；PM2 日志里 `19:18:19`、`19:36:11` 各有一次 `qa-shell` 冷启动。
  - 同期 `/www/wwwroot/zzdy.powerzhuan.cn/` 根下也在备份：`_backup-closeway-20261015-1845`、
    `_backup-im-frames-20261015-0046`、`_backup-media-upload-20261015-1257`、`_backup-uploadfix-20261015`
    —— **全是 `_backup-*` 目录，没有 `src/`、没有 vite/tsconfig/*.tsx**。
  - 结论：这台服务器上的前端历来就是**直接补产物**（连 `index.html` 都单独备份），
    所以我们的补丁能存活；**只要没人从干净源码重新构建并覆盖，它就不会丢**。

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
2. **真正待办：线上前后端都停在 `710866e^`，缺 19:36 才入库的这行修复**（证据链）：
   - 线上后端目录 `/www/wwwroot/zzdy.powerzhuan.cn/`：`server.js`（203,207 B，`10-15 19:18`）、
     `package.json`（669 B，`10-15 19:36`，`"version": "0.9.6"`）；nginx `root` 指向该目录，
     接口走 `proxy_pass http://127.0.0.1:3210`。
   - `frontend/dist/index.html` 与最新入口 `frontend/dist/assets/index-DFKASPWw.js`（1,526,947 B）都是 `10-15 19:32`；
     而仓内 `710866e` 的提交时间是 `19:36:14`。
   - 线上 bundle 关键字计数：`qaTrainerName` **2** 次、`qaTrainerZzUserId` **1** 次、`trainerMap` **0** 次
     （`qaTrainerName` 的 2 次正好是头部与 `who()` 两处；`trainerMap` 为 0 说明产物不是这份源码构建的）。
   - 时间线：**19:18 后端 / 19:32 前端构建 → 19:36 源码入库（v0.9.6，含本修复） → 前后端都未再部署**。
3. **后端可以安全地用仓库版本覆盖（已逐项核对，不必再担心打回功能）**：
   - 线上 `server.js` 与仓内 `710866e:server.js` 都是 3,470 行、路由数同为 36、全库唯一行各 583 行
     ——典型的「同一条线上一版 vs 下一版」，仓内版本是**前进方向**而非分叉。
   - 仓内独有的是 `readWatermark` / 「已读水位」相关（`已读水位` 在 repo 出现 7 次、线上 0 次），
     正对应该提交说明里的「已读水位线持久化 + 永久已读日志 + 探针 BigInt 修复」。
   - 曾担心会被打回的 `/im/frames`、`closeWay`、`mediaUpload` 只是 `/www/wwwroot/zzdy.powerzhuan.cn/` 下
     `_backup-*` 目录名里的说法：`server.js` 里**线上与仓库都是 0 命中**（`/im/` 各 8 次，
     `closeWay` / `closeReason` / `media-upload` 均 0），说明它们不在这个后端，不会被此次覆盖影响。
4. **建议的收尾方式（按优先级）**：
   1. 由该仓 owner 在本地 `npm ci && npm run build` 后，把 `frontend/dist` 同步到
      `/www/wwwroot/zzdy.powerzhuan.cn/frontend/dist/`；新产物自带修复，可同时撤销我们 18:29 那次手改
      （两套并存反而难回溯）。
   2. 顺带把 `server.js` 也同步到 `710866e` 版本并重启（`pm2 restart qa-shell`），
      让线上拿到「已读水位线持久化」等同一提交里的后端改进。
   3. 打补丁式的线上改动应尽快回植源码，并走该仓自己的分支流程（不要再直接改产物）。
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
   - gitcode 上的 `project1` **匿名即可克隆**（本机无该站凭据也成功了）；本机旧克隆在
     `E:\dsh-scratch\project1`。knowledge-kb 仓全历史里没有套壳前端源码
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

## 六、本次取证方式（可复现）

- 线上 bundle 用 `curl` 下载后静态阅读（`grep -o -E '.{160}qaTrainerZzUserId.{200}'`），未执行任何上游代码。
- 浏览器复核：服务器上 Playwright + Chromium，注入登录态 cookie（`dsh_sid`）后打开
  `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<工单号>&sceneType=2`，
  读 `chat-title` 行文本并统计 `pageerror` 数量。
- 只读探针脚本一律放在本地 `.dsh-local/probe-answerer-*.sh`（不提交），
  以 `Get-Content -Raw ... | ssh root@<host> "bash -s"` 方式执行；**必须确保 LF 行尾**，
  否则 `\r` 会污染变量赋值导致 `grep: /index-xxx.js: No such file or directory` 这类假故障。
