// 运行：node --test scripts/test-chat-window.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '../frontend/index.html'), 'utf8');
const script = html.match(/<script>\s*var API=[\s\S]*?<\/script>/)[0].replace(/^<script>/, '').replace(/<\/script>$/, '');
function setup(overrides = {}, rectOverrides = {}) {
  const calls = [];
  const rects = {dialog:{left:90,top:50}, '.dlg-h':{bottom:123}, '.retrieval-review-detail':{left:726.75}, '.retrieval-review-footer':{top:877}, ...rectOverrides};
  const win = {KB_RUNTIME:{}, screen:{availLeft:0,availTop:0,availWidth:1920,availHeight:1080}, screenX:0, screenY:0, outerWidth:1600, innerWidth:1600, outerHeight:1080, innerHeight:1000, focus(){throw Error('不要把主窗口抢回前台');}, ...overrides};
  let options;
  const context = vm.createContext({window:win, Vue:{createApp(o){options=o; return {mount(){}};}}});
  vm.runInContext(script, context);
  const app = {workOrderChatBlockedUrl:'', $refs:{retrievalReviewDialog:{getBoundingClientRect:()=>rects.dialog, querySelector:s=>({getBoundingClientRect:()=>rects[s]})}}, ...options.methods};
  win.open = (url, target, features) => {
    calls.push(['open',url,target,features]);
    const popup={closed:false, opener:{}, resizeTo(w,h){calls.push(['resize',w,h]);}, moveTo(x,y){calls.push(['move',x,y]);}, location:{replace(url){calls.push(['navigate',url,popup.opener]);}}, close(){popup.closed=true; calls.push(['close']);}, focus(){calls.push(['focus']);}};
    return popup;
  };
  return {app,win,calls,context};
}
test('实际 DOM 边界：外框不遮挡标题、右侧详情和页脚', () => {
  const {app}=setup(); const b=app.workOrderChatGeometry();
  assert.equal(b.left,90); assert.equal(b.top,203); assert.equal(b.width,625); assert.equal(b.height,754);
  assert.ok(b.left+b.width < 726.75); assert.equal(b.top+b.height,957);
});
test('125% 缩放与负坐标副屏：鼠标屏幕坐标校准', () => {
  const {app}=setup({screen:{availLeft:-1920,availTop:0,availWidth:1920,availHeight:1400},outerWidth:1600,innerWidth:1280});
  const b=app.workOrderChatGeometry({detail:1,screenX:-900,screenY:400,clientX:400,clientY:200});
  assert.equal(b.left,-1287); assert.equal(b.top,304); assert.equal(b.width,781); assert.equal(b.height,943);
});
test('窄屏独立窗口限制在可用屏幕内', () => {
  const {app}=setup({screen:{availLeft:0,availTop:0,availWidth:375,availHeight:667}}, {'.retrieval-review-detail':{left:100}});
  const b=app.workOrderChatGeometry(); assert.equal(b.width,375); assert.equal(b.height,667); assert.equal(b.left,0); assert.equal(b.top,0);
});
test('先定位同源空白窗口，再断开 opener 并导航，保留完整工单 ID', () => {
  const {app,calls}=setup(); app.openWorkOrderChat('2104481532893725752');
  assert.deepEqual(calls.map(c=>c[0]),['open','resize','move','navigate','focus']);
  assert.equal(calls[0][1],'about:blank'); assert.equal(calls[0][2],'_blank');
  assert.equal(calls[3][2],null);
  assert.equal(calls[3][1],'https://zzdy.powerzhuan.cn/#/workorderDetail?questionFormId=2104481532893725752&sceneType=2');
});
test('定位 API 被浏览器拒绝也能打开聊天', () => {
  const {app,win,calls}=setup(); const open=win.open;
  win.open=(...args)=>{const p=open(...args); p.resizeTo=()=>{throw Error('blocked');}; return p;};
  app.openWorkOrderChat('123'); assert.ok(calls.some(c=>c[0]==='navigate')); assert.equal(app.workOrderChatBlockedUrl,'');
});
test('重新对齐成功后替换旧窗口；弹窗拦截不关闭旧窗口', () => {
  const {app,win,calls}=setup(); app.openWorkOrderChat('first'); app.openWorkOrderChat('second');
  assert.equal(calls.filter(c=>c[0]==='close').length,1);
  assert.equal(calls.filter(c=>c[0]==='navigate').at(-1)[1].includes('questionFormId=second'),true);
  win.open=()=>null; app.openWorkOrderChat('third');
  assert.equal(calls.filter(c=>c[0]==='close').length,1); assert.ok(app.workOrderChatBlockedUrl.includes('questionFormId=third'));
});
test('没有工单 ID 时不打开窗口', () => {
  const {app,calls}=setup(); app.openWorkOrderChat(' '); app.openWorkOrderChat('-'); app.openWorkOrderChat(null); assert.equal(calls.length,0);
});
