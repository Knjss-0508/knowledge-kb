# 答疑工作台聊天记录发送者姓名：线上修复记录与交接（2026-10-15）

承接 `docs/qa-web-assistant-speaker-name-resolution-20261014.md`（命名办法与「角色词 / 真名跳变」根因）。
那篇的结论停在「根因在上游渲染期，建议上游修」；**本文记录这个建议已经被实际实现并上线，
以及它目前只存在于构建产物里、还未进入任何源码仓库这件事**——这是本任务唯一的未完成项。

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
  重建后的 bundle 里新表达式仍在（`qaTrainerZzUserId` 命中 1 次，旧的无兜底表达式 0 次），
  浏览器复核头部仍为「答疑员：杨培鹏」。可见重建是基于**打补丁后的产物**做的增量改造，
  不是从干净源码重出。

## 四、未完成项（交接给能拿到源码的人）

1. **修复没有进入源码**。已在服务器全盘确认：zzdy.powerzhuan.cn 提供的是「答疑工作台 2.0」React 套壳前沿，
   该前端的源码仓库**不在这台服务器上**（`/opt/voc-workbench/current/frontend` 是
   `voc-review-workbench`，Vue 3 + Element Plus，源码里没有 `qaTrainerName` / `qaTrainerZzUserId` / `qa-roster`，
   是另一个产品，不要改错仓库）。
   因此本补丁目前**只存在于构建产物**，下次有人从干净源码重建就会被覆盖。
2. **建议的上游源码改法**（两处，参考 `docs/qa-web-assistant-speaker-name-resolution-20261014.md` 第三节）：
   - 训练/详情页那份：`const w=(r?.qaTrainerName)||v[String(r?.trainerZzUserId||r?.qaTrainerZzUserId||"")]||"答疑"`，
     并确保映射 `v`（原 `I`：`u.filter(M=>M.zzUserId&&M.trainerName)` → `I[String(M.zzUserId)]=M.trainerName`）
     在头部渲染前已填充。
   - 聊天列表那份：`yx()` 的第三分支保持 `Nk(n)` 不变，但要让 `Cp` 真正被填充
     （补上调用方，或让 `/qa-roster` 兼容只带 `zzUserId` 的行）。
   - 归属（`isMe`）与姓名解析**解耦**是后续独立改动，别和这次混在一个 PR 里。
3. **需要确认运维事实**：谁在 10-15 19:18 / 19:32 重建了 dist、用的是什么流程。
   如果存在「本地构建 → 上传产物」的流水线，补丁必然丢失；若确认是「在线上产物上继续手工改」，
   则需要把这次改动沉淀到源码才有意义。

## 五、答疑中台侧（与本修复正交，仍未部署）

- 会话号工单不再入池、存量自动退池：PR #199（分支 `fix/blind-label-session-number-gate`，**未部署**）。
  部署后池里不会再出现「详情接口返回空 → 打开就是一片答疑」的那一份。
- 本次修复解决的是「名单有名字但显示答疑」；PR #199 解决的是「详情为空导致整屏兜底」。
  两者互不替代，都值得上线。

## 六、本次取证方式（可复现）

- 线上 bundle 用 `curl` 下载后静态阅读（`grep -o -E '.{160}qaTrainerZzUserId.{200}'`），未执行任何上游代码。
- 浏览器复核：服务器上 Playwright + Chromium，注入登录态 cookie（`dsh_sid`）后打开
  `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<工单号>&sceneType=2`，
  读 `chat-title` 行文本并统计 `pageerror` 数量。
- 只读探针脚本一律放在本地 `.dsh-local/probe-answerer-*.sh`（不提交），
  以 `Get-Content -Raw ... | ssh root@<host> "bash -s"` 方式执行；**必须确保 LF 行尾**，
  否则 `\r` 会污染变量赋值导致 `grep: /index-xxx.js: No such file or directory` 这类假故障。
