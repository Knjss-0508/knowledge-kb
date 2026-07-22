const form = document.querySelector('#validationForm');
const statusNode = document.querySelector('#status');
const previewButton = document.querySelector('#previewButton');
const downloadButton = document.querySelector('#downloadButton');
const summary = document.querySelector('#summary');
const results = document.querySelector('#results');
const tableHead = document.querySelector('#tableHead');
const tableBody = document.querySelector('#tableBody');
const tableTitle = document.querySelector('#tableTitle');
const tableMeta = document.querySelector('#tableMeta');
const detailTitle = document.querySelector('#detailTitle');
const detailBody = document.querySelector('#detailBody');
const reviewFile = document.querySelector('#reviewFile');
const loadReviewButton = document.querySelector('#loadReviewButton');
const exportReviewButton = document.querySelector('#exportReviewButton');
const reviewStatus = document.querySelector('#reviewStatus');
const reviewWorkspace = document.querySelector('#reviewWorkspace');
const reviewMeta = document.querySelector('#reviewMeta');
const reviewKeyword = document.querySelector('#reviewKeyword');
const reviewDecisionFilter = document.querySelector('#reviewDecisionFilter');
const focusOnly = document.querySelector('#focusOnly');
const reviewTableHead = document.querySelector('#reviewTableHead');
const reviewTableBody = document.querySelector('#reviewTableBody');
const reviewTitle = document.querySelector('#reviewTitle');
const reviewEvidence = document.querySelector('#reviewEvidence');
const reviewForm = document.querySelector('#reviewForm');

let resultData = null;
let activeView = 'candidates';
let reviewRows = [];
let selectedReviewRowIndex = null;
const reviewChanges = new Map();

const views = {
  candidates: {
    title: '候选知识',
    columns: [
      ['数据ID', '数据 ID'],
      ['核心问题', '核心问题'],
      ['模型主标题', '模型主标题'],
      ['模型一级分类', '一级分类'],
      ['模型二级分类', '二级分类'],
      ['模型知识形态', '知识形态'],
      ['模型置信度', '置信度'],
      ['模型阶段状态', '模型状态'],
      ['是否重点复核', '重点复核'],
    ],
    details: ['模型知识内容', '模型关联标准', '标准检索状态', '检索标准Top5', '标准版本', '模型适用范围', '模型初标依据', '图片处理状态', '图片证据摘要', '模型错误', '模型提供方', '模型名称', 'Prompt版本', '预处理备注', '判定结论', '判定依据', '参考话术'],
  },
  preprocessed: {
    title: '预处理',
    columns: [
      ['数据ID', '数据 ID'],
      ['核心问题', '核心问题'],
      ['预处理状态', '状态'],
      ['缺失字段', '缺失字段'],
      ['可进入模型初标', '可初标'],
      ['预处理备注', '处理备注'],
    ],
    details: ['聊天内容', '图片链接', '原始问题清洗', '原始聊天清洗', '原始依据清洗', '原始话术清洗'],
  },
  excluded: {
    title: '排除记录',
    columns: [
      ['序号', '序号'],
      ['产品类型', '产品类型'],
      ['核心问题', '核心问题'],
      ['一级分类', '一级分类'],
      ['二级分类', '二级分类'],
      ['排除原因', '排除原因'],
    ],
    details: ['聊天内容', '判定结论', '判定依据', '图片链接'],
  },
};

function setStatus(message, isError = false) {
  statusNode.textContent = message;
  statusNode.classList.toggle('error', isError);
}

function setReviewStatus(message, isError = false) {
  reviewStatus.textContent = message;
  reviewStatus.classList.toggle('error', isError);
}

function setText(selector, value) {
  const node = document.querySelector(selector);
  if (node) node.textContent = value;
}

function valueText(value) {
  if (Array.isArray(value)) return value.join('；');
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function currentRows() {
  if (!resultData) return [];
  if (activeView === 'candidates') return resultData.candidates || [];
  if (activeView === 'preprocessed') return resultData.preprocessed || [];
  return resultData.excluded || [];
}

function showDetail(row) {
  const view = views[activeView];
  detailTitle.textContent = valueText(row['模型主标题'] || row['核心问题'] || row['数据ID'] || row['序号']);
  detailBody.replaceChildren();
  const fields = [...view.columns.map(([key]) => key), ...view.details];
  for (const key of [...new Set(fields)]) {
    const value = valueText(row[key]);
    if (value === '—') continue;
    const dt = document.createElement('dt');
    dt.textContent = key;
    const dd = document.createElement('dd');
    dd.textContent = value;
    detailBody.append(dt, dd);
  }
}

function renderTable() {
  const view = views[activeView];
  const rows = currentRows();
  tableTitle.textContent = view.title;
  tableMeta.textContent = `${rows.length} 条`;
  tableHead.replaceChildren();
  tableBody.replaceChildren();

  const headerRow = document.createElement('tr');
  for (const [, label] of view.columns) {
    const th = document.createElement('th');
    th.textContent = label;
    headerRow.append(th);
  }
  tableHead.append(headerRow);

  rows.forEach((row, index) => {
    const tr = document.createElement('tr');
    if (index === 0) tr.classList.add('selected');
    for (const [key] of view.columns) {
      const td = document.createElement('td');
      const value = valueText(row[key]);
      td.textContent = value;
      if (key === '是否重点复核') {
        td.classList.add(value === '是' ? 'flag-yes' : 'flag-no');
      }
      tr.append(td);
    }
    tr.addEventListener('click', () => {
      tableBody.querySelectorAll('tr').forEach((item) => item.classList.remove('selected'));
      tr.classList.add('selected');
      showDetail(row);
    });
    tableBody.append(tr);
  });
  if (rows.length) showDetail(rows[0]);
  else {
    detailTitle.textContent = activeView === 'excluded' ? '没有被排除的记录' : '没有可展示的数据';
    detailBody.replaceChildren();
  }
}

function renderResult(data) {
  resultData = data;
  setText('#sourceTotal', data.source_total_rows);
  setText('#selectedTotal', data.selected_rows);
  setText('#standardTotal', data.standard_count);
  setText('#reviewTotal', data.focus_review_rows);
  setText('#excludedTotal', data.excluded_rows);
  setText('#modelFailedTotal', data.model_failed_rows || 0);
  setText('#imageUnavailableTotal', data.image_unavailable_rows || 0);
  summary.hidden = false;
  results.hidden = false;
  renderTable();
}

async function requestPreview(event) {
  event.preventDefault();
  if (!form.source.files.length) {
    setStatus('请先选择第二部分数据表。', true);
    return;
  }
  previewButton.disabled = true;
  setStatus('正在处理配置品类数据并匹配质检标准…');
  try {
    const response = await fetch('/api/preview', { method: 'POST', body: new FormData(form) });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '验证失败');
    renderResult(payload);
    const modelText = payload.mimo_enabled
      ? (payload.mimo_configured ? '已调用 MiMo 或已记录模型结果' : '未配置 MiMo，已使用规则式候选兜底')
      : '已按规则式候选运行';
    setStatus(`验证完成：候选 ${payload.selected_rows} 条，重点复核 ${payload.focus_review_rows} 条；${modelText}。`);
  } catch (error) {
    setStatus(error.message || '验证失败', true);
  } finally {
    previewButton.disabled = false;
  }
}

async function downloadWorkbook() {
  if (!form.source.files.length) {
    setStatus('请先选择第二部分数据表。', true);
    return;
  }
  downloadButton.disabled = true;
  setStatus('正在生成 cz 复核工作簿…');
  try {
    const response = await fetch('/api/review-workbook', { method: 'POST', body: new FormData(form) });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || '生成失败');
    }
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = '多品类-候选知识复核表.xlsx';
    link.click();
    URL.revokeObjectURL(link.href);
    setStatus('复核工作簿已生成。');
  } catch (error) {
    setStatus(error.message || '生成失败', true);
  } finally {
    downloadButton.disabled = false;
  }
}

form.addEventListener('submit', requestPreview);
downloadButton.addEventListener('click', downloadWorkbook);
document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    activeView = tab.dataset.view;
    document.querySelectorAll('.tab').forEach((item) => item.classList.toggle('active', item === tab));
    renderTable();
  });
});

const reviewColumns = [
  ['数据ID', '数据 ID'],
  ['核心问题', '核心问题'],
  ['模型主标题', '模型主标题'],
  ['模型一级分类', '模型分类'],
  ['模型关联标准', '模型标准'],
  ['是否重点复核', '重点复核'],
  ['CZ复核结论', 'CZ结论'],
];

function rawValue(value) {
  if (Array.isArray(value)) return value.join('\n');
  return value === null || value === undefined ? '' : String(value);
}

function filteredReviewRows() {
  const keyword = reviewKeyword.value.trim().toLowerCase();
  const decision = reviewDecisionFilter.value;
  return reviewRows.filter((row) => {
    const rowDecision = rawValue(row['CZ复核结论']);
    if (decision === '__pending__' && rowDecision) return false;
    if (decision && decision !== '__pending__' && rowDecision !== decision) return false;
    if (focusOnly.checked && rawValue(row['是否重点复核']) !== '是') return false;
    if (!keyword) return true;
    return ['数据ID', '工单ID', '核心问题', '模型主标题']
      .some((key) => rawValue(row[key]).toLowerCase().includes(keyword));
  });
}

function selectedReviewRow() {
  return reviewRows.find((row) => row._review_row_index === selectedReviewRowIndex) || null;
}

function showReviewEditor(row) {
  if (!row) {
    reviewTitle.textContent = '请选择一条候选';
    reviewEvidence.textContent = '';
    reviewForm.querySelectorAll('[data-field]').forEach((field) => { field.value = ''; });
    return;
  }
  selectedReviewRowIndex = row._review_row_index;
  reviewTitle.textContent = rawValue(row['模型主标题'] || row['核心问题'] || row['数据ID']);
  reviewEvidence.textContent = [
    `数据 ID：${rawValue(row['数据ID'])}`,
    `工单 ID：${rawValue(row['工单ID'])}`,
    `模型知识内容：${rawValue(row['模型知识内容'])}`,
    `模型关联标准：${rawValue(row['模型关联标准'])}`,
    `标准检索状态：${rawValue(row['标准检索状态'])}`,
    `模型初标依据：${rawValue(row['模型初标依据'])}`,
    `图片证据摘要：${rawValue(row['图片证据摘要'])}`,
    `模型错误：${rawValue(row['模型错误'])}`,
  ].join('\n\n');
  reviewForm.querySelectorAll('[data-field]').forEach((field) => {
    field.value = rawValue(row[field.dataset.field]);
  });
}

function renderReviewTable() {
  const rows = filteredReviewRows();
  reviewTableHead.replaceChildren();
  reviewTableBody.replaceChildren();
  const header = document.createElement('tr');
  reviewColumns.forEach(([, label]) => {
    const th = document.createElement('th');
    th.textContent = label;
    header.append(th);
  });
  reviewTableHead.append(header);

  rows.forEach((row) => {
    const tr = document.createElement('tr');
    if (row._review_row_index === selectedReviewRowIndex) tr.classList.add('selected');
    reviewColumns.forEach(([key]) => {
      const td = document.createElement('td');
      const value = valueText(row[key]);
      td.textContent = value;
      if (key === '是否重点复核') td.classList.add(value === '是' ? 'flag-yes' : 'flag-no');
      if (key === 'CZ复核结论') td.classList.add(value === '—' ? 'reviewed-no' : 'reviewed-yes');
      tr.append(td);
    });
    tr.addEventListener('click', () => {
      showReviewEditor(row);
      renderReviewTable();
    });
    reviewTableBody.append(tr);
  });
  if (!rows.length) {
    reviewTitle.textContent = '没有符合筛选条件的候选';
    reviewEvidence.textContent = '';
  } else if (!selectedReviewRow() || !rows.some((row) => row._review_row_index === selectedReviewRowIndex)) {
    showReviewEditor(rows[0]);
    renderReviewTable();
    return;
  }
  reviewMeta.textContent = `显示 ${rows.length} / ${reviewRows.length} 条；已保存本地修改 ${reviewChanges.size} 条`;
}

async function loadReviewWorkspace() {
  if (!reviewFile.files.length) {
    setReviewStatus('请先选择 review_queue.xlsx。', true);
    return;
  }
  loadReviewButton.disabled = true;
  setReviewStatus('正在读取复核工作簿…');
  try {
    const data = new FormData();
    data.append('review_file', reviewFile.files[0]);
    const response = await fetch('/api/review-queue', { method: 'POST', body: data });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '读取复核工作簿失败');
    reviewRows = payload.rows || [];
    selectedReviewRowIndex = null;
    reviewChanges.clear();
    reviewWorkspace.hidden = false;
    exportReviewButton.disabled = false;
    renderReviewTable();
    const suffix = payload.truncated ? '，当前仅展示前 500 条' : '';
    setReviewStatus(`已加载 ${payload.total_rows} 条候选：待复核 ${payload.pending_rows} 条，重点复核 ${payload.focus_review_rows} 条${suffix}。`);
  } catch (error) {
    setReviewStatus(error.message || '读取复核工作簿失败', true);
  } finally {
    loadReviewButton.disabled = false;
  }
}

function saveReview(event) {
  event.preventDefault();
  const row = selectedReviewRow();
  if (!row) {
    setReviewStatus('请先选择一条候选。', true);
    return;
  }
  const updates = {};
  reviewForm.querySelectorAll('[data-field]').forEach((field) => {
    updates[field.dataset.field] = field.value.trim();
  });
  Object.assign(row, updates);
  reviewChanges.set(row._review_row_index, updates);
  renderReviewTable();
  setReviewStatus(`已暂存数据 ID ${rawValue(row['数据ID'])} 的复标；请点击“下载已复标工作簿”保存。`);
}

async function exportReviewWorkbook() {
  if (!reviewFile.files.length) {
    setReviewStatus('请先选择 review_queue.xlsx。', true);
    return;
  }
  exportReviewButton.disabled = true;
  setReviewStatus('正在生成已复标工作簿…');
  try {
    const data = new FormData();
    data.append('review_file', reviewFile.files[0]);
    data.append('changes', JSON.stringify(
      [...reviewChanges.entries()].map(([rowIndex, updates]) => ({ row_index: rowIndex, updates }))
    ));
    const response = await fetch('/api/review-export', { method: 'POST', body: data });
    if (!response.ok) {
      const payload = await response.json();
      throw new Error(payload.error || '导出失败');
    }
    const blob = await response.blob();
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = '多品类-已复标候选知识表.xlsx';
    link.click();
    URL.revokeObjectURL(link.href);
    setReviewStatus(`已导出 ${reviewChanges.size} 条本地复标修改。`);
  } catch (error) {
    setReviewStatus(error.message || '导出失败', true);
  } finally {
    exportReviewButton.disabled = false;
  }
}

loadReviewButton.addEventListener('click', loadReviewWorkspace);
exportReviewButton.addEventListener('click', exportReviewWorkbook);
reviewForm.addEventListener('submit', saveReview);
reviewKeyword.addEventListener('input', renderReviewTable);
reviewDecisionFilter.addEventListener('change', renderReviewTable);
focusOnly.addEventListener('change', renderReviewTable);
