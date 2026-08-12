#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""聚类标注工具 V6 - Flask Web界面"""
import json
import glob
import pandas as pd
from flask import Flask, render_template_string, request, jsonify
from collections import defaultdict

app = Flask(__name__)

# 加载V6数据（优先）或V5（fallback）
v6_files = sorted(glob.glob('data/聚类结果_v6_*.xlsx'))
if not v6_files:
    v6_files = sorted(glob.glob('data/聚类结果_v5_*.xlsx'))
df = pd.read_excel(v6_files[-1])
print(f"加载数据: {v6_files[-1]}")

# 加载源数据以获取完整原始chat_log
src = pd.read_excel('data/质检答疑案例库 (4).xlsx')
src_cols = list(src.columns)
chat_col = 'chat_log' if 'chat_log' in src.columns else src_cols[6]

summary_files = sorted(glob.glob('data/主题摘要_v6_*.xlsx'))
if not summary_files:
    summary_files = sorted(glob.glob('data/主题摘要_v5_*.xlsx'))
summary_df = pd.read_excel(summary_files[-1])

# 按主题组织
topics = {}
for topic_id, group in df.groupby('主题编号'):
    row0 = group.iloc[0]
    cases = []
    for _, case in group.iterrows():
        orig_idx = int(case['原始行号'])
        # 从源数据读完整原始chat_log
        if orig_idx < len(src):
            full_chat = str(src.iloc[orig_idx][chat_col]) if not pd.isna(src.iloc[orig_idx][chat_col]) else ''
        else:
            full_chat = str(case.get('聊天记录', ''))
        # V6: 使用"实际对话内容"字段（已去除header的干净对话）
        clean_conv = str(case.get('实际对话内容', case.get('聊天记录', '')))
        cases.append({
            'row_idx': orig_idx,
            'model': str(case['型号']),
            'core_issue': str(case['核心问题'])[:300],
            'judgment': str(case['判定结果'])[:200],
            'chat_log': full_chat[:1200],
            'clean_conv': clean_conv[:600],
        })
    topics[int(topic_id)] = {
        'id': int(topic_id),
        'title': str(row0['知识标题']),
        'common_q': str(row0.get('常见问法', '')),
        'label': str(row0['主题标签']),
        'project': str(row0['品类']),
        'component': str(row0['具体部件']),
        'anomaly': str(row0['异常类型']),
        'domain': str(row0['判定对象域']),
        'std_type': str(row0['判定标准类型']),
        'size': len(cases),
        'cases': cases,
    }

# 加载已有标注
try:
    with open('data/annotations.json', 'r', encoding='utf-8') as f:
        annotations = json.load(f)
except:
    annotations = {}

TEMPLATE = r'''
<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>聚类标注工具 V6</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, "Microsoft YaHei", sans-serif; background: #f0f2f5; }
.header { background: #1a1a2e; color: white; padding: 12px 20px; display: flex; justify-content: space-between; align-items: center; position: sticky; top: 0; z-index: 100; }
.header h1 { font-size: 18px; }
.stats { font-size: 13px; color: #a0a0c0; }
.stats span { margin: 0 8px; }
.layout { display: flex; height: calc(100vh - 56px); }
.sidebar { width: 340px; background: white; overflow-y: auto; border-right: 1px solid #e0e0e0; }
.topic-card { padding: 10px 14px; border-bottom: 1px solid #f0f0f0; cursor: pointer; transition: background 0.15s; }
.topic-card:hover { background: #f5f7ff; }
.topic-card.active { background: #e8f0fe; border-left: 3px solid #1a73e8; padding-left: 11px; }
.topic-card .title { font-size: 14px; font-weight: 500; color: #1a1a2e; margin-bottom: 4px; }
.topic-card .meta { font-size: 12px; color: #888; }
.topic-card .badge { display: inline-block; background: #e8f0fe; color: #1a73e8; padding: 1px 6px; border-radius: 3px; font-size: 11px; margin-right: 4px; }
.topic-card .badge.warn { background: #fff3e0; color: #e65100; }
.topic-card .badge.ok { background: #e8f5e9; color: #2e7d32; }
.filters { padding: 10px 14px; background: #fafafa; border-bottom: 1px solid #e0e0e0; position: sticky; top: 0; }
.filters input { width: 100%; padding: 6px 10px; border: 1px solid #ddd; border-radius: 4px; font-size: 13px; }
.main { flex: 1; overflow-y: auto; padding: 20px; }
.topic-detail { background: white; border-radius: 8px; padding: 20px; margin-bottom: 16px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
.topic-detail h2 { font-size: 20px; margin-bottom: 8px; }
.topic-detail .info { font-size: 13px; color: #666; margin-bottom: 16px; display: flex; flex-wrap: wrap; gap: 8px; }
.topic-detail .info .tag { background: #f0f0f0; padding: 3px 8px; border-radius: 4px; }
.case-card { border: 1px solid #e8e8e8; border-radius: 6px; padding: 12px 16px; margin-bottom: 10px; display: flex; gap: 14px; align-items: flex-start; }
.case-card:hover { border-color: #1a73e8; }
.case-card.misplaced { background: #fff8e1; border-color: #f9a825; }
.case-card .case-num { font-weight: bold; color: #888; font-size: 12px; min-width: 40px; }
.case-card .case-body { flex: 1; }
.case-card .case-model { font-size: 12px; color: #1a73e8; margin-bottom: 4px; }
.case-card .case-issue { font-size: 13px; color: #333; line-height: 1.5; margin-bottom: 4px; }
.case-card .case-judgment { font-size: 12px; color: #666; background: #f9f9f9; padding: 4px 8px; border-radius: 3px; }
.case-card .case-chat { font-size: 12px; color: #555; background: #fafbfc; border: 1px solid #e8e8e8; border-radius: 4px; padding: 8px 10px; margin: 6px 0; line-height: 1.6; }
.case-card .case-chat summary { cursor: pointer; color: #1a73e8; font-weight: 500; margin-bottom: 4px; }
.case-actions { display: flex; gap: 6px; align-items: flex-start; flex-direction: column; }
.btn { padding: 5px 12px; border: 1px solid #ddd; border-radius: 4px; cursor: pointer; font-size: 12px; background: white; }
.btn:hover { background: #f5f5f5; }
.btn.danger { color: #c62828; border-color: #ef9a9a; }
.btn.danger:hover { background: #ffebee; }
.btn.danger.active { background: #c62828; color: white; border-color: #c62828; }
.btn.ok:hover { background: #e8f5e9; }
.btn.ok.active { background: #2e7d32; color: white; border-color: #2e7d32; }
.quick-nav { position: fixed; bottom: 20px; right: 20px; display: flex; gap: 6px; z-index: 200; }
.quick-nav button { padding: 8px 16px; border-radius: 20px; border: none; background: #1a73e8; color: white; cursor: pointer; font-size: 13px; box-shadow: 0 2px 8px rgba(26,115,232,0.3); }
.quick-nav button:hover { background: #1565c0; }
.empty-state { text-align: center; color: #aaa; padding: 60px 20px; }
.progress-bar { height: 4px; background: #e0e0e0; border-radius: 2px; margin-top: 6px; }
.progress-bar .fill { height: 100%; background: #2e7d32; border-radius: 2px; transition: width 0.3s; }
</style>
</head>
<body>
<div class="header">
  <h1>📋 聚类标注工具 V6</h1>
  <div class="stats">
    总主题: <span>{{ total_topics }}</span>
    已标注: <span id="annotated-count">{{ annotated_count }}</span>
    <button class="btn" style="background:#1a73e8;color:white;margin-left:12px" onclick="exportAnnotations()">导出标注</button>
  </div>
</div>

<div class="layout">
  <div class="sidebar">
    <div class="filters">
      <input type="text" id="filter" placeholder="搜索标题/部件/品类..." oninput="filterTopics()">
    </div>
    <div id="topic-list">
      {% for t in topic_list %}
      <div class="topic-card" data-id="{{ t.id }}" data-search="{{ t.title }} {{ t.project }} {{ t.component }} {{ t.anomaly }}" onclick="selectTopic({{ t.id }})">
        <div class="title">{{ t.title }}</div>
        <div class="meta">
          <span class="badge">{{ t.project }}</span>
          <span class="badge {{ 'ok' if t.annotated_ok else ('warn' if t.annotated_bad else '') }}">{{ t.size }}条</span>
          {{ t.component }} · {{ t.anomaly }}
        </div>
        {% if t.annotated_ok or t.annotated_bad %}
        <div class="progress-bar"><div class="fill" style="width:{{ t.annotated_pct }}%"></div></div>
        {% endif %}
      </div>
      {% endfor %}
    </div>
  </div>

  <div class="main" id="detail-area">
    <div class="empty-state">
      <p>← 点击左侧主题查看案例详情</p>
    </div>
  </div>
</div>

<div class="quick-nav">
  <button onclick="prevTopic()">◀ 上一个</button>
  <button onclick="nextTopic()">下一个 ▶</button>
</div>

<script>
const TOPICS = {{ topics_json | safe }};
const ALL_TOPIC_IDS = {{ topic_ids }};
let currentTopicId = null;
let annotations = {{ annotations_json | safe }};

function selectTopic(tid) {
  currentTopicId = tid;
  const t = TOPICS[tid];
  let ann = annotations[tid] || {};
  let okCases = ann.ok || [];
  let badCases = ann.bad || [];

  let html = `<div class="topic-detail">
    <h2>${t.title}</h2>
    <div class="info">
      <span class="tag">📱 ${t.project}</span>
      <span class="tag">🔧 ${t.component}</span>
      <span class="tag">⚠ ${t.anomaly}</span>
      <span class="tag">📂 ${t.domain}</span>
      <span class="tag">📏 ${t.std_type}</span>
      <span class="tag">📊 ${t.size}条案例</span>
    </div>`;
  if (t.common_q) {
    html += `<div style="font-size:12px;color:#888;margin-bottom:10px">💬 常见问法: ${escapeHtml(t.common_q)}</div>`;
  }
  html += `<div style="margin-bottom:10px">
    <button class="btn ok" onclick="markTopic(${tid}, 'ok')">✅ 全对</button>
    <button class="btn warn" onclick="markTopic(${tid}, 'review')">🔍 需复核</button>
  </div></div>`;

  for (let c of t.cases) {
    let isBad = badCases.includes(c.row_idx);
    let isOk = okCases.includes(c.row_idx);
    // V6: 优先展示干净的对话内容
    let cleanText = c.clean_conv || '';
    let rawChat = c.chat_log || '';
    let chatPreview = cleanText;
    if (!chatPreview && rawChat) {
      chatPreview = rawChat.length > 200 ? rawChat.substring(0, 200) + '...' : rawChat;
    }
    if (chatPreview && chatPreview.length > 250) {
      chatPreview = chatPreview.substring(0, 250) + '...';
    }
    html += `<div class="case-card ${isBad ? 'misplaced' : ''}" id="case-${c.row_idx}">
      <div class="case-num">#${c.row_idx}</div>
      <div class="case-body">
        <div class="case-model">📱 ${escapeHtml(c.model)}</div>
        <div class="case-chat">
          <div style="font-weight:500;color:#333;margin-bottom:3px">💬 实际对话内容 (已过滤header噪音):</div>
          <div style="white-space:pre-wrap;line-height:1.6;margin-bottom:6px">${escapeHtml(chatPreview) || '(无对话内容)'}</div>
          <details><summary>📋 原始记录 (${rawChat.length}字) - 含header行可忽略</summary>
          <div style="white-space:pre-wrap;line-height:1.5;margin-top:4px;color:#888;max-height:300px;overflow-y:auto">${escapeHtml(rawChat)}</div>
          </details>
        </div>
        <div class="case-issue" style="margin-top:6px"><b>核心问题:</b> ${escapeHtml(c.core_issue)}</div>
        <div class="case-judgment"><b>判定:</b> ${escapeHtml(c.judgment)}</div>
      </div>
      <div class="case-actions">
        <button class="btn danger ${isBad ? 'active' : ''}" onclick="toggleCase(${tid}, ${c.row_idx}, 'bad')">✗ 归类错误</button>
        <button class="btn ok ${isOk ? 'active' : ''}" onclick="toggleCase(${tid}, ${c.row_idx}, 'ok')" style="color:#2e7d32;border-color:#a5d6a7">✓ 归类正确</button>
      </div>
    </div>`;
  }

  document.getElementById('detail-area').innerHTML = html;
  document.querySelectorAll('.topic-card').forEach(el => el.classList.remove('active'));
  let card = document.querySelector(`.topic-card[data-id="${tid}"]`);
  if (card) card.classList.add('active');

  updateStats();
}

function toggleCase(tid, rowIdx, type) {
  if (!annotations[tid]) annotations[tid] = {ok: [], bad: []};
  let arr = annotations[tid][type];
  let otherArr = annotations[tid][type === 'ok' ? 'bad' : 'ok'];

  let idx = arr.indexOf(rowIdx);
  if (idx >= 0) { arr.splice(idx, 1); }
  else { arr.push(rowIdx); }

  let oIdx = otherArr.indexOf(rowIdx);
  if (oIdx >= 0) otherArr.splice(oIdx, 1);

  saveAnnotations();
  selectTopic(tid);
}

function markTopic(tid, type) {
  if (type === 'ok') {
    if (!annotations[tid]) annotations[tid] = {ok: [], bad: []};
    annotations[tid].ok = TOPICS[tid].cases.map(c => c.row_idx);
    annotations[tid].bad = [];
  }
  saveAnnotations();
  selectTopic(tid);
}

function saveAnnotations() {
  fetch('/save', { method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(annotations) });
  updateStats();
}

function escapeHtml(text) {
  return text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function updateStats() {
  let annotated = Object.keys(annotations).filter(k => (annotations[k].ok || []).length + (annotations[k].bad || []).length > 0).length;
  document.getElementById('annotated-count').textContent = annotated;
}

function filterTopics() {
  let q = document.getElementById('filter').value.toLowerCase();
  document.querySelectorAll('.topic-card').forEach(el => {
    el.style.display = q ? (el.dataset.search.toLowerCase().includes(q) ? '' : 'none') : '';
  });
}

function prevTopic() {
  let idx = ALL_TOPIC_IDS.indexOf(currentTopicId || ALL_TOPIC_IDS[0]);
  let prev = idx > 0 ? ALL_TOPIC_IDS[idx - 1] : ALL_TOPIC_IDS[ALL_TOPIC_IDS.length - 1];
  selectTopic(prev);
  document.querySelector(`.topic-card[data-id="${prev}"]`)?.scrollIntoView({block: 'center'});
}

function nextTopic() {
  let idx = ALL_TOPIC_IDS.indexOf(currentTopicId || ALL_TOPIC_IDS[0]);
  let next = idx < ALL_TOPIC_IDS.length - 1 ? ALL_TOPIC_IDS[idx + 1] : ALL_TOPIC_IDS[0];
  selectTopic(next);
  document.querySelector(`.topic-card[data-id="${next}"]`)?.scrollIntoView({block: 'center'});
}

function exportAnnotations() {
  fetch('/export').then(r => r.json()).then(data => {
    alert('标注已导出: ' + data.file);
  });
}

document.addEventListener('keydown', (e) => {
  if (e.key === 'ArrowDown' && e.shiftKey) { e.preventDefault(); nextTopic(); }
  if (e.key === 'ArrowUp' && e.shiftKey) { e.preventDefault(); prevTopic(); }
});

updateStats();
</script>
</body>
</html>
'''


@app.route('/')
def index():
    topic_list = []
    for tid in sorted(topics.keys()):
        t = topics[tid]
        ann = annotations.get(str(tid), {})
        ok_cases = len(ann.get('ok', []))
        bad_cases = len(ann.get('bad', []))
        annotated_ok = ok_cases == t['size']
        annotated_bad = bad_cases > 0
        annotated_pct = int((ok_cases + bad_cases) / t['size'] * 100) if t['size'] > 0 else 0
        topic_list.append({
            'id': tid,
            'title': t['title'],
            'project': t['project'],
            'component': t['component'],
            'anomaly': t['anomaly'],
            'size': t['size'],
            'annotated_ok': annotated_ok,
            'annotated_bad': annotated_bad,
            'annotated_pct': annotated_pct,
        })

    annotated_count = sum(1 for k in annotations if annotations[k].get('ok') or annotations[k].get('bad'))

    return render_template_string(
        TEMPLATE,
        topics=topics,
        topic_list=topic_list,
        total_topics=len(topics),
        annotated_count=annotated_count,
        topics_json=json.dumps(topics, ensure_ascii=False),
        topic_ids=json.dumps(sorted(topics.keys())),
        annotations_json=json.dumps(annotations, ensure_ascii=False),
    )


@app.route('/save', methods=['POST'])
def save():
    global annotations
    annotations = request.get_json()
    with open('data/annotations.json', 'w', encoding='utf-8') as f:
        json.dump(annotations, f, ensure_ascii=False, indent=2)
    return jsonify({'status': 'ok'})


@app.route('/export')
def export():
    export_data = []
    for topic_id, group in df.groupby('主题编号'):
        ann = annotations.get(str(int(topic_id)), {})
        ok_set = set(ann.get('ok', []))
        bad_set = set(ann.get('bad', []))
        for _, case in group.iterrows():
            ridx = int(case['原始行号'])
            status = 'correct' if ridx in ok_set else ('incorrect' if ridx in bad_set else 'unreviewed')
            export_data.append({
                '主题编号': int(topic_id),
                '主题标题': str(case['知识标题']),
                '原始行号': ridx,
                '型号': str(case['型号']),
                '核心问题': str(case['核心问题'])[:200],
                '标注状态': status,
            })

    edf = pd.DataFrame(export_data)
    path = 'data/标注结果.xlsx'
    edf.to_excel(path, index=False, engine='openpyxl')
    return jsonify({'file': path})


if __name__ == '__main__':
    print(f'加载了 {len(topics)} 个主题')
    print('启动: http://localhost:5000')
    app.run(debug=True, host='0.0.0.0', port=5000)
