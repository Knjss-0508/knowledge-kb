# 答疑工作台聊天记录：发送者姓名的解析口径，与「角色词 / 真名跳变」现象（2026-10-14）

本文记录一次线上现象排查：盲标工单页里内嵌的聊天记录，发送者一会儿显示角色词（「回收师」「答疑」），
一会儿显示真实姓名（如「赦琳结」）。结论是**姓名不在聊天消息里**，而是渲染期用「订单详情 + 答疑名单」
两份异步数据现查出来的，两份数据缺失时落到硬编码角色词，且未命中时还会翻转气泡归属。
同时说明：答疑中台前端已经优先用上游工单号打开聊天，本现象**不能靠前端改字段解决**，
真正的拦截点在盲标池入池门禁（见 PR #199）。

## 一、现象

- 盲标工单页（工单 `2110212696858170447`）聊天记录中：回收师侧显示「赦琳结」，答疑侧显示「答疑」。
- 同一段对话在另一个池内条目（会话号 `2110212723299062272`）下打开时，**所有气泡都变成「答疑」**，
  且回收师发的消息被画到右侧（当成答疑方）。
- 用户看到的就是这种「一会儿角色词、一会儿真名」的跳变，并指出答疑工作台桌面端（Electron）里是同一条工单、
  显示正常。

## 二、结论

1. **姓名不在消息里**。历史消息接口只返回 `fromUid / type / text / msgId / time` 等字段，
   发送者显示名是渲染期解析的。
2. **显示名来自两份异步数据**：订单详情（回收师的 `adminName`）与答疑名单（`appUid → 姓名`）。
   两份都没命中时，分别落到硬编码字面量 `"回收师"` 与 `"答疑"`。
3. **未命中的 uid 会被当成答疑方**：`yx()` 的兜底分支返回 `isMe: true`，于是回收师的消息被右对齐、
   挂在「答疑」名下——这解释了「同一段对话换个入口就变了」的观感。

## 三、上游实现（线上 bundle）

线上文件：`/www/wwwroot/zzdy.powerzhuan.cn/frontend/dist/assets/index-3VYlC2Tq.js`（1,519,866 B，
构建产物，无 sourcemap；排查时下载到服务器 `/tmp/zzdy_probe/main.js` 静态阅读，未执行）。

```js
function Pk(e){var r,o;const t=new Set,n=[(r=e.detail)==null?void 0:r.creatorAppUserId,
  (o=e.selected)==null?void 0:o.engineerAppZzUserId];for(const s of n)s&&t.add(String(s));return t}

function yx(e,t){var s,a,l;const n=e!=null?String(e):"";
  if(n&&Pk(t).has(n))return{name:((s=t.selected)==null?void 0:s.adminName)
    ||((a=t.detail)==null?void 0:a.adminName)||"回收师",isMe:!1};
  const r=((l=t.user)==null?void 0:l.zzAppUid)!=null?String(t.user.zzAppUid):"";
  if(r&&n===r)return{name:"我",isMe:!0};
  const o=Nk(n);return o?{name:o,isMe:!0}:{name:"答疑",isMe:!0}}

function Nk(e){const t=e!=null?String(e):"";if(t)return Cp.get(t)}

function tle(e){if(!e||e.length===0)return;let t=!1;
  for(const n of e){if(!n||!n.appUid||!n.name)continue;const r=String(n.appUid);
    Cp.get(r)!==n.name&&(Cp.set(r,n.name),t=!0)}t&&rae(e).catch(()=>{})}
```

调用点（同一 bundle）：

- 列表渲染：`le.map(ne=>{const ye=yx(ne.fromUid,Ne); ...})`，`Ne` 是当前会话上下文。
- 展示名：`const Ve=i.useCallback(ne=>yx(ne.fromUid,Ne).name,[Ne])`。
- 训练样本导出：`Kle(e,t,n,r,o)` 里 `const m=yx(d.fromUid,{detail:t,selected:r,user:o});
  return{... role:m.isMe?"support":"recycler", text:d.text, sequence:f+1}` ——
  **归属（role）和名字用的是同一个兜底判定**。

| fromUid 命中什么 | 显示的发送者 | 气泡位置 |
| --- | --- | --- |
| `detail.creatorAppUserId` 或 `selected.engineerAppZzUserId` | `selected.adminName` → `detail.adminName` → 兜底「回收师」 | 左侧（回收师） |
| 当前登录人的 `zzAppUid` | 我 | 右侧 |
| 全局名单 Map `Cp`（由 `/qa-roster` 名单里带 `appUid + name` 的行填充） | 名单里的真实姓名 | 右侧 |
| 都不命中 | 兜底「答疑」 | **右侧（被当成答疑方）** |

同一逻辑还有第二份实现（训练页），字段名不同：
`const w=(r?.qaTrainerName)||"答疑"`、`const E=(r?.engineerName)||(r?.adminName)||"回收师"`，
名单 `u.filter(M=>M.zzUserId&&M.trainerName)` → `I[String(M.zzUserId)] = M.trainerName`，
`P(fromUid)` 的四分支与 `yx()` 一致。修一处需同步另一处。

相关接口：`/qa-roster`（`wae=()=>tt("/qa-roster")`，另有 `/qa-roster/hide|show|clear`）、
`/assistant-access/me`、`/im/history`（`la=(e,t)=>tt("/im/history",{query:{conversationId:e},timeoutMs:t})`）。
无登录态访问 `/im/history` 返回 `{"respCode":401,"errorMsg":"need login","needLogin":true}`。

## 四、答疑中台侧的表现与证据

- 盲标页聊天记录是 iframe：`frontend/index.html:2225`
  （`:src="blindLabelChatUrl()"`、`:key="blindLabeling.current.chatFrameKey"`）；
  URL 由 `frontend/index.html:5846 blindLabelChatUrl()` → `frontend/index.html:5830 workOrderChatUrl()`
  拼成 `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<号>&sceneType=2`；
  工单详情链接 `frontend/index.html:5840 blindLabelWorkOrderDetailUrl()` 也只用工单号；
  「重新加载」按钮 `frontend/index.html:2222` → `frontend/index.html:5852 reloadBlindLabelChat()`
  会 +1 `chatFrameKey` 重建 iframe（因此会先回到首帧兜底态，再随数据到达升级）。
- **前端已经优先用上游工单号**，不需要再改：
  `blindLabelChatUrl()` = `blindLabelQuestionFormId(record) || blindLabelConversationId(record)`
  （`frontend/index.html:5846-5851`），该优先逻辑来自 `2e1d1f0`（盲标页内嵌答疑聊天记录）
  与 `4b5e01f`（盲标工单只认上游工单号 question_form_id，#175）两支。
- **线上页面与仓库 HEAD 完全一致**，可直接证实优先逻辑已上线：
  线上 `GET /app` 750,041 B，`git hash-object` = `2f47965ef26e217e635ca384d00e66a26c259840`
  = `git rev-parse HEAD:frontend/index.html`。
- **本现象不是「前端选错字段」**：泄漏进池的会话号工单里，`question_form_id` 本身就等于会话号。
  - `wo-ae09717054ee4ed38fcc1e2a`（2110212723299062272）：`conversation_id = question_form_id = 2110212723299062272`。
  - `wo-62d72fef78f846949445196d`（2110212696858170447，真工单号）：两者同样相同。
  对会话号工单，前端无论选哪个字段，打开的都是那个取不到详情的号。
- 上游详情接口响应体大小（zzdy 访问日志最近 300 MB，只读）：
  - `queryQuestionFormDetail?questionFormId=2110212696858170447&sceneType=2` → `200` / 658·659·660 B（有数据）。
  - `queryQuestionFormDetail?questionFormId=2110212723299062272&sceneType=2` → **`200` / 124 B（空）**。
- 截图像素级复核（放大裁剪脚本 `outputs/cmp/crop_labels.py`，产物 `outputs/cmp/*.png`，不提交）：
  - 会话号那一份：11:34:53 / 11:35:09 / 11:35:25 / 11:35:31 四条**全部显示「答疑」且全部右对齐**
    —— 即详情为空时名字全兜底 + 归属翻转的形态。
  - 工单号那一份：11:40:08 起正常显示「赦琳结」（左）/「答疑」（右），与 Electron 端同一套渲染规则一致。
- 同一段对话在池内有两份（真工单号 + 会话号），各自 5 条派发 / 6 条标注：
  对比两个池内条目就会看到「一会儿角色词、一会儿真名」。

## 五、为什么答疑工作台桌面端看起来正常

桌面端永远带着真实工单上下文（头部：工单ID / 问题类型 / 回收师：赦琳结 / 答疑员：答疑 / 状态），
首屏就有订单详情与名单；盲标页里的聊天**就是同一个 App 页面**（iframe），差别只在我们喂进去的号码。
换言之：上游渲染规则没变，变的是我们给了它一个取不到详情的号。

## 六、可复现步骤（建议交给上游）

1. 用一个只有会话号的号码打开
   `https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=<会话号>&sceneType=2`
   （或让详情接口返回 124 B 空体）→ 所有气泡显示「答疑」，回收师消息右对齐。
2. 换成正常工单号打开 → 回收师显示真实姓名。
3. 慢网/断网（或让 `/qa-roster`、详情延迟返回）→ 首帧角色词，数据到达后跳成真名。

## 七、建议

上游（答疑工作台）：

1. `yx()` 未命中时不要返回 `isMe: true`；归属应由会话方向、消息序号或已知回收师 uid 判定，
   不能靠「查不到就当归答疑」。
2. 兜底文案不要使用角色词（「回收师」「答疑」），改为「未知账号」或延迟渲染骨架，
   避免与真实姓名混淆。
3. 详情为空时改用名单/会话接口补名字，并把 `isMe` 判定与名字解析解耦。
4. 训练页那份 `P()` 是同一逻辑的第二实现，修复需同步。

答疑中台侧：

1. 会话号不再进池、存量自动退池：PR #199（分支 `fix/blind-label-session-number-gate`，**未部署**）。
   部署后池里不会再出现「打开就是一片答疑」的那一份。
2. 存量「真工单号 + 会话号」两份，退池后自然只剩真工单号那一份。

## 八、附：本次取证方式

- 生产库只读探针（容器内 `PYTHONPATH=/app` 运行）。注意 `blind_label_work_orders` **没有**
  `work_order_verified` / `conversation_id_kind` 列，这两列在 `retrieval_quality_events`。
- zzdy 访问日志（容器内只读挂载 `/app/nlogs`）统计详情接口响应体大小分布。
- 线上 bundle 下载后静态阅读（未执行任何上游代码）。
- 截图像素复核：bundled python + Pillow。
