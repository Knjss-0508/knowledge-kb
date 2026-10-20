'use strict';
// 刷新知识库术语语料快照（只读）。
//
// 用法：
//   node knowledge/dictionaries/corpus/refresh-corpus.cjs <integrationKey>
//
// 原理：知识库没有匿名的全量列表接口（/api/v1/knowledge 需登录态），
// 因此用只读检索接口按「品类×主题」爬取。
//
// 两个必须遵守的点（都踩过坑）：
//   1. **品类决定候选集**：productType 不同，知识库返回的候选集不同。
//      固定用 '手机' 会永远拿不到笔记本/平板/游戏机等专属知识
//      （实测固定手机时 576 次请求只能爬到 414 条，且缺 346 条）。
//   2. **requestId 必须只生成一次**：知识库要求 Header X-Request-Id 与正文
//      requestId 完全一致，分别调用 Date.now() 会导致 REQUEST_IDENTITY_MISMATCH(400)。
//
// 安全约定：
//   - 只用只读检索接口 POST /api/v1/integration/standard-search
//   - 低并发（默认 4），控制轮数，避免影响生产
//   - 集成密钥由命令行传入，不写入任何文件
//
// 注意：候选的 text 字段是截断的（实测 414 条里 364 条长度为 0），
// 因此语料只采集完整的 title 与 keywords。
const fs = require('node:fs');
const path = require('node:path');

const BASE = process.env.KB_BASE_URL || 'https://knowledgekb.powerzhuan.cn';
const WORK_ORDER_ID = process.env.KB_WORK_ORDER_ID || '2092919668271485492';
const KEY = process.argv[2] || process.env.CODEX_RETRIEVAL_INTEGRATION_KEY || '';
const CONCURRENCY = Number(process.env.CONCURRENCY || 4);
const MAX_ROUNDS = Number(process.env.MAX_ROUNDS || 40);
const PER_ROUND = Number(process.env.PER_ROUND || 60);
const EMPTY_STREAK_LIMIT = 3;

if (!KEY) {
  console.error('缺少集成密钥：node refresh-corpus.cjs <integrationKey>');
  process.exit(1);
}

// 品类：知识库按品类过滤候选集，必须逐个品类爬。
const CATEGORIES = ['手机', '笔记本', '平板', '智能手表', '耳机', '相机', '游戏机', '台式机'];
// 主题：覆盖部件 / 现象 / 信息核验 三类。
const TOPICS = ['屏幕', '序列号', '电池', '外观', '进水', '拆修', '功能', '摄像头',
  '键盘', '转轴', '充电', '声音', '指纹', '面容', '系统', '型号', '保修', '颜色',
  '主板', '尾插', '散热', '外壳', '接口', '触摸'];

// 种子：品类 × 主题 × 问法
const SEEDS = [];
CATEGORIES.forEach(function(category) {
  TOPICS.forEach(function(topic) {
    ['如何判定', '异常怎么处理', '是什么现象'].forEach(function(suffix) {
      SEEDS.push({ category: category, question: category + topic + suffix });
    });
  });
});

async function query(text, category) {
  // requestId 只生成一次：知识库要求 Header 与正文的 requestId 完全一致
  const requestId = 'corpus-' + Date.now() + '-' + Math.floor(Math.random() * 1e6);
  const response = await fetch(BASE + '/api/v1/integration/standard-search', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'X-Integration-Key': KEY,
      'X-Conversation-Id': WORK_ORDER_ID,
      'X-Request-Id': requestId
    },
    body: JSON.stringify({
      conversationId: WORK_ORDER_ID,
      workOrderId: WORK_ORDER_ID,
      conversationIdKind: 'workorder',
      requestId: requestId,
      requestMode: 'semantic',
      normalizedQuestion: text,
      contextSummary: text,
      productType: category || '手机',
      orderInfo: { workOrderId: WORK_ORDER_ID, category: category || '手机' },
      knowledgeTypes: ['reply', 'standard'],
      limit: 20,
      limitPerType: 20
    })
  });
  if (!response.ok) return [];
  const body = await response.json().catch(function() { return null; });
  // 必须同时收集「顶层 candidates」与 reply/standard 两个池——
  // 知识库对某些 knowledgeOrigin 只放在顶层（此前只读两个池会大量漏抓）。
  const rows = [];
  if (body && Array.isArray(body.candidates)) rows.push.apply(rows, body.candidates);
  ['reply', 'standard'].forEach(function(name) {
    const bucket = body && body[name];
    if (bucket && Array.isArray(bucket.candidates)) rows.push.apply(rows, bucket.candidates);
  });
  return rows;
}

async function batch(probes) {
  const out = [];
  for (let index = 0; index < probes.length; index += CONCURRENCY) {
    const slice = probes.slice(index, index + CONCURRENCY);
    const rows = await Promise.all(slice.map(function(probe) {
      return query(probe.question, probe.category).catch(function() { return []; });
    }));
    rows.forEach(function(candidates) { out.push(candidates); });
    process.stdout.write('.');
  }
  return out;
}

(async () => {
  const knowledge = {};
  const seenProbes = {};
  let queue = SEEDS.slice();
  let emptyStreak = 0;
  let requestCount = 0;

  for (let round = 1; round <= MAX_ROUNDS; round += 1) {
    const probes = queue.filter(function(p) {
      return p && p.question && !seenProbes[p.category + '|' + p.question];
    }).slice(0, PER_ROUND);
    if (!probes.length) { console.log('探针队列已空'); break; }
    probes.forEach(function(p) { seenProbes[p.category + '|' + p.question] = true; });

    process.stdout.write('第 ' + round + ' 轮：' + probes.length + ' 探针 ');
    // 本轮主体的品类（种子阶段每轮只含同一品类的探针；keywords 扩充阶段沿用该品类）
    const categoryCount = {};
    probes.forEach(function(p) {
      categoryCount[p.category] = (categoryCount[p.category] || 0) + 1;
    });
    const batchCategory = Object.keys(categoryCount)
      .sort(function(a, b) { return categoryCount[b] - categoryCount[a]; })[0] || '手机';
    const batches = await batch(probes);
    requestCount += probes.length;

    const discovered = {};
    let added = 0;
    batches.forEach(function(candidates) {
      candidates.forEach(function(item) {
        if (!item || !item.id) return;
        if (!knowledge[item.id]) {
          knowledge[item.id] = {
            id: item.id,
            title: item.title || '',
            level1: item.level1Label || '',
            origin: item.knowledgeOrigin || '',
            keywords: Array.isArray(item.keywords) ? item.keywords : []
          };
          added += 1;
        }
        // 用知识库自带的 keywords（同义问法）作为下一轮探针，沿用同品类
        (item.keywords || []).forEach(function(keyword) {
          const key = (batchCategory || '手机') + '|' + keyword;
          if (!keyword || seenProbes[key] || /^适用范围|^标准判定/.test(keyword)) return;
          discovered[keyword] = true;
        });
      });
    });
    // 下一轮探针：沿用本轮主体的品类
    queue = Object.keys(discovered).map(function(question) {
      return { category: batchCategory || '手机', question: question };
    });
    console.log('新增 ' + added + '，累计 ' + Object.keys(knowledge).length + '，新探针 ' + queue.length);
    emptyStreak = added ? 0 : emptyStreak + 1;
    if (emptyStreak >= EMPTY_STREAK_LIMIT) { console.log('已收敛'); break; }
  }

  const rows = Object.keys(knowledge).map(function(id) { return knowledge[id]; })
    .sort(function(left, right) { return left.id.localeCompare(right.id); });
  const payload = {
    _note: '知识库真实知识标题快照，用于词典术语对齐与持续回归。仅含标题与关键词，不含知识正文。',
    _source: '通过只读检索接口反查知识库得到（候选的 keywords 字段滚雪球）。',
    _generatedAt: new Date().toISOString(),
    _count: rows.length,
    _howToRefresh: '本目录 refresh-corpus.cjs',
    titles: rows
  };
  const out = path.join(__dirname, 'knowledge-titles.json');
  fs.writeFileSync(out, JSON.stringify(payload, null, 2), 'utf8');
  console.log('');
  console.log('总请求 = ' + requestCount + '，写入 ' + rows.length + ' 条 -> ' + out);
})().catch(function(error) { console.error('FAILED: ' + error.message); process.exit(1); });
