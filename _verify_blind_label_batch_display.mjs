/**
 * 盲标工作台批次显示口径的静态复核（无需 pytest / 无需前端依赖）。
 *
 * 做法：从 frontend/index.html 抽出真实的 blindLabelBatchSize / blindLabelBatchPending /
 * blindLabelBatchPercent / blindLabelBatchEmptyHint 方法体，在 Node 里用真实 Vue 实例的 this
 * （blindLabeling.batch + blindLabeling.assignments）执行，验证界面数字与实际装载条数一致。
 *
 * 用法：node _verify_blind_label_batch_display.mjs
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(fileURLToPath(import.meta.url));
const FRONTEND = readFileSync(join(root, 'frontend', 'index.html'), 'utf8');

/**
 * 从 `    name: function(` 起用大括号深度扫描到函数体结束。
 * 跳过字符串、模板字符串、行注释与块注释，避免注释里的括号干扰。
 */
function extractMethod(name) {
  const start = FRONTEND.indexOf(`    ${name}: function(`);
  if (start === -1) throw new Error(`method not found: ${name}`);
  let depth = 0;
  let started = false;
  let inLine = false;
  let inBlock = false;
  let quote = null;
  for (let i = start; i < FRONTEND.length; i += 1) {
    const ch = FRONTEND[i];
    const next = FRONTEND[i + 1];
    if (inLine) {
      if (ch === '\n') inLine = false;
      continue;
    }
    if (inBlock) {
      if (ch === '*' && next === '/') {
        inBlock = false;
        i += 1;
      }
      continue;
    }
    if (quote) {
      if (ch === '\\') {
        i += 1;
        continue;
      }
      if (ch === quote) quote = null;
      continue;
    }
    if (ch === '/' && next === '/') {
      inLine = true;
      i += 1;
      continue;
    }
    if (ch === '/' && next === '*') {
      inBlock = true;
      i += 1;
      continue;
    }
    if (ch === "'" || ch === '"' || ch === '`') {
      quote = ch;
      continue;
    }
    if (ch === '{') {
      depth += 1;
      started = true;
      continue;
    }
    if (ch === '}') {
      depth -= 1;
      if (started && depth === 0) return FRONTEND.slice(start, i + 1);
    }
  }
  throw new Error(`unterminated method: ${name}`);
}

const METHODS = ['blindLabelBatchSize', 'blindLabelBatchPending', 'blindLabelBatchPercent', 'blindLabelBatchEmptyHint'];
const listingMethods = eval(`({${METHODS.map(extractMethod).join(',\n')}})`); // eslint-disable-line no-eval

const results = [];
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  results.push({ ok, label });
  console.log(`  [${ok ? 'ok' : 'FAIL'}] ${label}: ${JSON.stringify(actual)}${ok ? '' : ` (期望 ${JSON.stringify(expected)})`}`);
}

/** 模拟一次渲染：batch + 当前列表条数。 */
function render(batch, assignmentCount) {
  return {
    blindLabeling: { batch, assignments: Array.from({ length: assignmentCount }, (_, i) => ({ id: `a${i}` })) },
    ...listingMethods,
  };
}

function display(batch, assignmentCount) {
  const vm = render(batch, assignmentCount);
  return {
    size: vm.blindLabelBatchSize(),
    pending: vm.blindLabelBatchPending(),
    percent: vm.blindLabelBatchPercent(),
    hint: vm.blindLabelBatchEmptyHint(),
  };
}

console.log('场景 1：截图中的批次（目标 50，本批 1 条已完成 + 11 条进行中，列表显示 11 行）');
let view = display({ id: 'b1', status: 'active', total: 50, completed: 1, in_progress: 11, released: 38, assigned: 12, pending: 38 }, 11);
check('头部「N 条批次」与进度分母', view.size, 12);
check('进度百分比（1/12）', view.percent, 8);
check('剩余名额', view.pending, 1);
check('明细「已完成 + 进行中」不超过分母', 1 + 11 <= view.size, true);
check('空态文案（本批已分发完）', view.hint, '本批 12 条已分发完毕，其中 38 条被系统回收，等待新样本入库后点击“补充/刷新任务”。');

console.log('场景 2：刚领取、尚无提交（列表 11 行全部进行中）');
view = display({ id: 'b2', status: 'active', total: 50, completed: 0, in_progress: 11, released: 0, assigned: 11, pending: 39 }, 11);
check('头部条数', view.size, 11);
check('剩余名额', view.pending, 0);
check('进度百分比（0/11）', view.percent, 0);

console.log('场景 3：尚未领取的空批次（轮询占位，列表为空）');
view = display({ id: '', status: '', total: 50, completed: 0, in_progress: 0, released: 0, assigned: 0 }, 0);
check('回落到目标口径', view.size, 50);
check('空态文案（无样本）', view.hint, '本批没有可标注的工单，等待新样本入库后点击“领取下一批”。');

console.log('场景 4：整批 50 条全部完成（列表为空，历史在已标注页签）');
view = display({ id: 'b4', status: 'completed', total: 50, completed: 50, in_progress: 0, released: 0, assigned: 50 }, 0);
check('头部条数', view.size, 50);
check('进度百分比', view.percent, 100);
check('已完成批次剩余名额', view.pending, 0);
check('空态文案（可领下一批）', view.hint, '本批 50 条已完成，可点击“领取下一批”。');

console.log('场景 5：任务处理完但未填满目标（列表 9 行，3 条被系统回收）');
view = display({ id: 'b5', status: 'active', total: 50, completed: 9, in_progress: 0, released: 3, assigned: 9 }, 9);
check('条数不把已回收算成本批条数', view.size, 9);
check('剩余名额', view.pending, 0);
check('空态文案（含回收提示）', view.hint, '本批 9 条已分发完毕，其中 3 条被系统回收，等待新样本入库后点击“补充/刷新任务”。');

console.log('场景 5b：任务处理完且无回收（列表 9 行）');
view = display({ id: 'b5b', status: 'active', total: 50, completed: 9, in_progress: 0, released: 0, assigned: 9 }, 9);
check('空态文案（全部处理完）', view.hint, "本批 9 条任务已全部处理完毕，等待新样本入库后点击“补充/刷新任务”。");

console.log('场景 6：池子枯竭后仍有 12 条在途（列表 12 行，其中 1 条已完成）');
view = display({ id: 'b6', status: 'active', total: 50, completed: 1, in_progress: 11, released: 6, assigned: 12, pending: 38 }, 12);
check('头部条数', view.size, 12);
check('剩余名额', view.pending, 0);

console.log('UI 契约：');
const contracts = [
  ['头部不再写死「当前 50 条批次」', !FRONTEND.includes('>当前 50 条批次<')],
  ['不再回落到 batch.total || 50', !FRONTEND.includes('blindLabeling.batch.total || 50')],
  ['进度明细不再出现后端推算的「待处理」', !FRONTEND.includes('待处理 <b>')],
  ['进度明细用「剩余名额」而非会过期的「待领取」', FRONTEND.includes('剩余名额 <b>{{blindLabelBatchPending()}}')],
  ['空态文案复用 helper', FRONTEND.includes('blindLabelBatchEmptyHint()')],
  ['「我的已标注」不渲染批次进度卡', FRONTEND.includes("<template v-if=\"blindLabeling.tab==='mine'\">")],
  ['切页签清空在途批次', FRONTEND.includes("if (tab === 'completed') this.blindLabeling.batch = this.blindLabelEmptyBatch();")],
  ['已标注响应回来仍清空批次', FRONTEND.includes("if (self.blindLabeling.tab === 'completed') self.blindLabeling.batch = self.blindLabelEmptyBatch();")],
  ['空批次占位 helper 存在', FRONTEND.includes('blindLabelEmptyBatch: function()')],
  ['领取动作仍按固定 50 条', FRONTEND.includes("/blind-labeling/my-batch:claim?target_count=50")],
];
for (const [label, ok] of contracts) check(label, ok, true);

console.log('模板嵌套：');
const start = FRONTEND.indexOf('<div class="mn blind-label-shell"');
const end = FRONTEND.indexOf('<template v-if="isBlindLabelOverviewTab()">', start);
let depth = 0;
for (const line of FRONTEND.slice(start, end).split('\n')) {
  depth += (line.match(/<div\b/g) || []).length - (line.match(/<\/div>/g) || []).length;
  depth += (line.match(/<template\b/g) || []).length - (line.match(/<\/template>/g) || []).length;
}
check('盲标区块闭合深度', depth, 1);

const failed = results.filter((item) => !item.ok);
console.log(`RESULT: ${failed.length ? `${failed.length} FAILED` : 'ALL OK'}`);
process.exit(failed.length ? 1 : 0);
