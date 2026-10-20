'use strict';
// 从知识库「导出 Excel」生成术语语料与标准项清单（离线、确定性、无需凭据）。
//
// 用法：
//   node knowledge/dictionaries/corpus/build-from-export.cjs <导出文件.xlsx>
//
// 为什么改用导出而不是检索接口爬取（refresh-corpus.cjs）：
//   1. 检索接口是「按问法反查」，覆盖度取决于探针写得好不好：576 次请求只爬到 527 条，
//      靠旧语料合并才凑到 760 条，而知识库全量业务知识有 1200 条。
//   2. 列表接口 /api/v1/knowledge 有分页缺陷（按 created_at 排序，同秒记录顺序不稳定），
//      翻页会静默跳过约 31% 的记录，不能用来取全量。
//   3. 导出接口 /api/v1/knowledge/export/excel 一次给全，且自带「关联标准项」——
//      那是知识库自己的规范勾选项路径，是比标题更干净的术语源。
//
// 输出（都提交进仓库，供测试与词库映射复用）：
//   knowledge-titles.json  业务知识（总部标准 + 业务沉淀）的标题与自带同义问法
//   standard-items.json    知识库「关联标准项」路径清单（含出现次数），映射用术语源
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

// ---- 极简 xlsx 读取（zip + sharedStrings），不引入第三方依赖 ----

function readZipEntries(buf) {
  const entries = {};
  let eocd = -1;
  for (let i = buf.length - 22; i >= 0 && i > buf.length - 66000; i -= 1) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd === -1) throw new Error('不是有效的 xlsx（未找到 zip 中央目录）');
  const count = buf.readUInt16LE(eocd + 10);
  let off = buf.readUInt32LE(eocd + 16);
  for (let n = 0; n < count; n += 1) {
    if (buf.readUInt32LE(off) !== 0x02014b50) break;
    const method = buf.readUInt16LE(off + 10);
    const compSize = buf.readUInt32LE(off + 20);
    const nameLen = buf.readUInt16LE(off + 28);
    const extraLen = buf.readUInt16LE(off + 30);
    const commentLen = buf.readUInt16LE(off + 32);
    const localOff = buf.readUInt32LE(off + 42);
    const name = buf.slice(off + 46, off + 46 + nameLen).toString('utf8');
    const lNameLen = buf.readUInt16LE(localOff + 26);
    const lExtraLen = buf.readUInt16LE(localOff + 28);
    const dataStart = localOff + 30 + lNameLen + lExtraLen;
    const data = buf.slice(dataStart, dataStart + compSize);
    entries[name] = method === 8 ? zlib.inflateRawSync(data) : data;
    off += 46 + nameLen + extraLen + commentLen;
  }
  return entries;
}

function unescapeXml(value) {
  return String(value)
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .replace(/&quot;/g, '"')
    .replace(/&apos;/g, "'")
    .replace(/&#(\d+);/g, function (_match, digits) { return String.fromCharCode(Number(digits)); })
    .replace(/&amp;/g, '&');
}

function readSharedStrings(entries) {
  const shared = [];
  const raw = entries['xl/sharedStrings.xml'];
  if (!raw) return shared;
  const xml = raw.toString('utf8');
  const entryRe = /<si>([\s\S]*?)<\/si>/g;
  let entry;
  while ((entry = entryRe.exec(xml)) !== null) {
    const texts = [];
    const textRe = /<t[^>]*>([\s\S]*?)<\/t>/g;
    let text;
    while ((text = textRe.exec(entry[1])) !== null) texts.push(unescapeXml(text[1]));
    shared.push(texts.join(''));
  }
  return shared;
}

function parseSheet(sheetXml, shared) {
  const rows = [];
  const rowRe = /<row[^>]*>([\s\S]*?)<\/row>/g;
  let rowMatch;
  while ((rowMatch = rowRe.exec(sheetXml)) !== null) {
    const cells = {};
    const cellRe = /<c\s+r="([A-Z]+)\d+"([^>]*)>([\s\S]*?)<\/c>/g;
    let cellMatch;
    while ((cellMatch = cellRe.exec(rowMatch[1])) !== null) {
      const column = cellMatch[1];
      const attributes = cellMatch[2] || '';
      const inner = cellMatch[3];
      let value = '';
      const inline = /<is>([\s\S]*?)<\/is>/.exec(inner);
      if (inline) {
        const texts = [];
        const textRe = /<t[^>]*>([\s\S]*?)<\/t>/g;
        let text;
        while ((text = textRe.exec(inline[1])) !== null) texts.push(text[1]);
        value = texts.join('');
      } else {
        const vMatch = /<v>([\s\S]*?)<\/v>/.exec(inner);
        if (vMatch) {
          if (/t="s"/.test(attributes)) {
            const index = Number(vMatch[1]);
            value = shared[index] !== undefined ? shared[index] : vMatch[1];
          } else {
            value = vMatch[1];
          }
        }
      }
      cells[column] = unescapeXml(value);
    }
    rows.push(cells);
  }
  return rows;
}

// ---- 列定义（导出表头，勿随意改）----
// A 知识ID B 主题键 C 记录ID D 知识键 E 主标题 F 副标题 G 知识内容 H 知识来源
// I 业务类型 J 知识分类 K 录入方式 L 关联标准项 M 适用范围 N 生效状态

const BUSINESS_SOURCES = ['总部标准', '业务沉淀'];

function splitKeywords(rawSubtitle, scope) {
  const list = String(rawSubtitle || '')
    .split('\n')
    .map(function (line) {
      return line.trim().replace(/^[·•・\-*]\s*/, '').trim();
    })
    .filter(Boolean);
  if (scope) list.push('适用范围：' + scope);
  return list;
}

// 业务沉淀行的「主标题」为空，标题实际存在「主题键」里（210/210 条已验证）。
function titleOf(row) {
  return String(row.E || '').trim() || String(row.B || '').trim();
}

function pathLines(rawStandardItem) {
  return String(rawStandardItem || '')
    .split('\n')
    .map(function (line) { return line.trim(); })
    .filter(function (line) { return line && !/^适用(类目|品牌|机型)[:：]/.test(line); });
}

function main() {
  const input = process.argv[2];
  if (!input) {
    console.error('用法: node build-from-export.cjs <导出文件.xlsx>');
    process.exit(1);
  }
  const file = path.resolve(process.cwd(), input);
  if (!fs.existsSync(file)) {
    console.error('找不到导出文件: ' + file);
    process.exit(1);
  }

  const entries = readZipEntries(fs.readFileSync(file));
  const shared = readSharedStrings(entries);
  const sheets = Object.keys(entries)
    .filter(function (name) { return /^xl\/worksheets\/sheet\d+\.xml$/.test(name); })
    .sort();
  if (!sheets.length) throw new Error('导出文件里没有工作表');
  const sheetName = sheets.indexOf('xl/worksheets/sheet1.xml') !== -1
    ? 'xl/worksheets/sheet1.xml'
    : sheets[0];
  const rows = parseSheet(entries[sheetName].toString('utf8'), shared);
  const data = rows.slice(1);

  const business = data.filter(function (row) {
    return BUSINESS_SOURCES.indexOf(String(row.H || '').trim()) !== -1;
  });
  if (!business.length) {
    throw new Error('导出里没有业务知识（知识来源须为 ' + BUSINESS_SOURCES.join(' / ') + '），导出参数是否漏了 status=published？');
  }

  const untitled = business.filter(function (row) { return !titleOf(row); });
  if (untitled.length) {
    throw new Error('有 ' + untitled.length + ' 条业务知识既没有主标题也没有主题键，无法作为语料');
  }

  const titles = business.map(function (row) {
    const scope = String(row.M || '').trim();
    const source = String(row.H || '').trim();
    return {
      id: String(row.A || '').trim(),
      title: titleOf(row),
      level1: String(row.J || '').trim(),
      origin: source === '业务沉淀' ? 'business_accumulation' : 'headquarters_standard',
      status: String(row.N || '').trim(),
      keywords: splitKeywords(row.F, scope)
    };
  }).sort(function (left, right) {
    return left.id.localeCompare(right.id);
  });

  // 「关联标准项」两种形态：
  //   总部标准：多行【模块】-【子项】-【叶子】路径
  //   业务沉淀：单行「场景：X；部件；现象；适用类目：Y」
  const standardPaths = {};
  const scenarioItems = {};
  business.forEach(function (row) {
    const raw = String(row.L || '').trim();
    if (!raw) return;
    if (/^场景[:：]/.test(raw)) {
      scenarioItems[raw] = (scenarioItems[raw] || 0) + 1;
      return;
    }
    pathLines(raw).forEach(function (line) {
      standardPaths[line] = (standardPaths[line] || 0) + 1;
    });
  });

  const byCountThenKey = function (left, right) {
    if (right[1] !== left[1]) return right[1] - left[1];
    return left[0].localeCompare(right[0]);
  };
  const standardItems = {
    _note: '知识库「关联标准项」清单：知识库自己的规范勾选项路径，是词库术语映射的权威来源。',
    _source: '知识库导出 Excel 的「关联标准项」列（总部分支为多行路径，业务沉淀为「场景：…；部件；现象；适用类目：…」单行）。',
    _generatedAt: new Date().toISOString(),
    _count: Object.keys(standardPaths).length + Object.keys(scenarioItems).length,
    standardPaths: Object.keys(standardPaths)
      .map(function (key) { return [key, standardPaths[key]]; })
      .sort(byCountThenKey)
      .map(function (pair) { return { path: pair[0], count: pair[1] }; }),
    scenarios: Object.keys(scenarioItems)
      .map(function (key) { return [key, scenarioItems[key]]; })
      .sort(byCountThenKey)
      .map(function (pair) { return { path: pair[0], count: pair[1] }; })
  };

  const corpus = {
    _note: '知识库业务知识（总部标准 + 业务沉淀）标题快照，用于词典术语对齐与持续回归。不含知识正文。',
    _source: '知识库导出 Excel；由 build-from-export.cjs 生成。',
    _generatedAt: new Date().toISOString(),
    _count: titles.length,
    _howToRefresh: '见 knowledge/dictionaries/corpus/README.md',
    titles: titles
  };

  const dir = __dirname;
  fs.writeFileSync(path.join(dir, 'knowledge-titles.json'), JSON.stringify(corpus, null, 2), 'utf8');
  fs.writeFileSync(path.join(dir, 'standard-items.json'), JSON.stringify(standardItems, null, 2), 'utf8');

  const published = titles.filter(function (item) { return item.status === '生效中'; });
  console.log('导出数据行 = ' + data.length);
  console.log('业务知识（' + BUSINESS_SOURCES.join(' + ') + '）= ' + business.length +
    '（生效中 ' + published.length + ' / 其他 ' + (business.length - published.length) + '）');
  console.log('来源分布：总部标准 ' + titles.filter(function (t) { return t.origin === 'headquarters_standard'; }).length +
    '，业务沉淀 ' + titles.filter(function (t) { return t.origin === 'business_accumulation'; }).length);
  console.log('标题来源：主标题 ' + business.filter(function (r) { return String(r.E || '').trim(); }).length +
    '，主题键兜底 ' + business.filter(function (r) { return !String(r.E || '').trim(); }).length);
  console.log('标准项路径 = ' + standardItems.standardPaths.length + '，业务沉淀场景项 = ' + standardItems.scenarios.length);
  console.log('已写入 knowledge-titles.json 与 standard-items.json');
}

main();
