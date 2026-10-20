'use strict';
// 知识库「关联标准项」→ 检索词库 的映射体检。
//
// 用法：
//   node knowledge/dictionaries/corpus/map-standard-items.cjs             # 汇总
//   node knowledge/dictionaries/corpus/map-standard-items.cjs --list      # 列出全部未映射项
//   node knowledge/dictionaries/corpus/map-standard-items.cjs --json      # 输出机器可读结果
//
// 为什么需要它：
//   知识库的「关联标准项」是它自己的规范勾选项路径（如【屏幕外观情况】-【屏幕脱胶…支架缺损】）。
//   同事提问用的是口语，助手检索用的是 standard-retrieval-lexicon.js 里的别名表。
//   两者一旦脱节，就会出现「同事问的是 A、助手按 B 检索」，召回的知识驴唇不对马嘴。
//   本脚本把「知识库有哪些规范术语」和「词库已经认识哪些」做差集，
//   让补词这件事变成可复现的比对，而不是靠翻标题猜。
//
// 输入：standard-items.json（由 build-from-export.cjs 从导出 Excel 生成）
// 输出：未映射的标准项清单（按出现次数排序）+ 覆盖度统计
const fs = require('node:fs');
const path = require('node:path');

const lexicon = require('../standard-retrieval-lexicon.js');
const itemsPath = path.join(__dirname, 'standard-items.json');

if (!fs.existsSync(itemsPath)) {
  console.error('缺少 ' + itemsPath + '，请先跑：node knowledge/dictionaries/corpus/build-from-export.cjs <导出.xlsx>');
  process.exit(1);
}

const items = JSON.parse(fs.readFileSync(itemsPath, 'utf8'));

// 「关联标准项」里的非术语片段：模块名与固定修饰语，不是可检索的领域词。
const NOISE = /^(适用类目|适用品牌|适用机型|场景|空|通用$|无$|有$)/;
// 纯阈值描述（如「有斑数量≤15且直径≤20mm」）无法映射成单个术语，单独归类。
const THRESHOLD_LIKE = /[<>≤≥]|\d+\s*mm|\d+%/;

function recognized(term) {
  return lexicon.extractParts(term).length +
    lexicon.extractPhenomena(term).length +
    lexicon.extractCategories(term).length > 0;
}

// 把标准项拆成待映射片段：
//   总部标准路径：【模块】-【子项】-【叶子】→ 取每个【】内容
//   业务沉淀场景项：场景：X；部件；现象；适用类目：Y → 取分号段（跳过「场景/stem适用」开头）
function segmentsOf(entry) {
  const raw = String(entry.path || '');
  if (/^场景[:：]/.test(raw)) {
    return raw
      .split('；')
      .map(function (part) { return part.trim(); })
      .filter(function (part) {
        return part && !NOISE.test(part) && !/^（/.test(part);
      });
  }
  const brackets = [];
  const re = /【([^】]*)】/g;
  let match;
  while ((match = re.exec(raw)) !== null) brackets.push(match[1].trim());
  return brackets.filter(Boolean);
}

const total = {};      // 片段 -> 出现次数
const evidence = {};   // 片段 -> 来源路径样例（最多 3 条）
(items.standardPaths || []).concat(items.scenarios || []).forEach(function (entry) {
  segmentsOf(entry).forEach(function (segment) {
    total[segment] = (total[segment] || 0) + (entry.count || 1);
    if (!evidence[segment]) evidence[segment] = [];
    if (evidence[segment].length < 3) evidence[segment].push(entry.path);
  });
});

const all = Object.keys(total);
const mapped = all.filter(recognized);
const unmapped = all.filter(function (term) { return !recognized(term); });
const unmappedReal = unmapped.filter(function (term) { return !THRESHOLD_LIKE.test(term); });
const unmappedThreshold = unmapped.filter(function (term) { return THRESHOLD_LIKE.test(term); });

const ordered = unmappedReal.slice().sort(function (left, right) {
  if (total[right] !== total[left]) return total[right] - total[left];
  return left.localeCompare(right);
});

if (process.argv.indexOf('--json') !== -1) {
  process.stdout.write(JSON.stringify({
    totalSegments: all.length,
    mappedCount: mapped.length,
    unmappedCount: unmapped.length,
    unmapped: ordered.map(function (term) {
      return { term: term, count: total[term], examples: evidence[term] };
    }),
    thresholdOnly: unmappedThreshold.map(function (term) { return { term: term, count: total[term] }; })
  }, null, 2) + '\n');
  process.exit(0);
}

console.log('标准项片段总数 = ' + all.length +
  '（已映射 ' + mapped.length + '，未映射 ' + unmapped.length +
  '；其中阈值描述类 ' + unmappedThreshold.length + '，可补词的 ' + unmappedReal.length + '）');
console.log('映射率 = ' + (100 * mapped.length / all.length).toFixed(1) + '%');
console.log('');

if (process.argv.indexOf('--list') === -1) {
  console.log('未映射片段 top 40（--list 看全部）：');
} else {
  console.log('未映射片段（全部，按出现次数排序）：');
}
const limit = process.argv.indexOf('--list') === -1 ? 40 : ordered.length;
ordered.slice(0, limit).forEach(function (term) {
  console.log('  ' + String(total[term]).padStart(4) + '  ' + term);
});
if (process.argv.indexOf('--list') === -1) {
  console.log('');
  console.log('阈值描述类（如「有斑数量≤15且直径≤20mm」，不当作单个术语映射）：');
  unmappedThreshold.slice(0, 10).forEach(function (term) {
    console.log('  ' + String(total[term]).padStart(4) + '  ' + term);
  });
}
