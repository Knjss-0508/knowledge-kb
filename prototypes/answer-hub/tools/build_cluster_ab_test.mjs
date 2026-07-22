import fs from "node:fs/promises";
import path from "node:path";
import {
  FileBlob,
  SpreadsheetFile,
  Workbook,
} from "@oai/artifact-tool";

const SAMPLE_SIZE = 60;
const SAMPLE_SEED = "2026-07-17-cluster-ab-60";

function text(value) {
  return String(value ?? "").trim();
}

function hash(value) {
  let state = 2166136261;
  for (const character of String(value)) {
    state ^= character.charCodeAt(0);
    state = Math.imul(state, 16777619);
  }
  return state >>> 0;
}

function normalizeChat(value) {
  return text(value).replace(/\s+/g, " ");
}

function sanitizeText(value, uploaderNames) {
  let result = text(value).replace(/\r\n?/g, "\n");
  result = result.replace(
    /(?:IMEI|MEID|序列号|SN|串号|设备号)\s*[:：]?\s*[A-Z0-9-]{6,}/gi,
    (matched) => `${matched.split(/[:：]/)[0]}：[设备标识]`,
  );
  result = result.replace(
    /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi,
    "[邮箱]",
  );
  result = result.replace(/\b1[3-9]\d{9}\b/g, "[手机号]");
  result = result.replace(/\b\d{17}[\dXx]\b/g, "[证件号]");
  for (const uploaderName of uploaderNames) {
    if (uploaderName) {
      result = result.split(uploaderName).join("[人员]");
    }
  }
  return result
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean)
    .join("\n");
}

function extractFactSummary(basis) {
  const value = text(basis);
  const matched = value.match(
    /事实核查结果\s*[:：]\s*([\s\S]*?)(?=\n?\s*采纳\s*\/\s*排除逻辑\s*[:：]|$)/,
  );
  if (matched) {
    return matched[1].trim();
  }
  return value
    .split(/\n+/)
    .filter((line) => /图片|视频|视屏|举证|视觉|实测|确认|描述/.test(line))
    .join("\n")
    .trim();
}

function isLikelyMultiTopic(row) {
  const value = [
    row["聊天内容"],
    row["核心问题"],
    row["判定结论"],
    row["上游媒体分析摘要"],
  ].join("\n");
  const objectTerms = [
    "屏幕",
    "摄像头",
    "电池",
    "充电",
    "按键",
    "键盘",
    "主板",
    "后盖",
    "中框",
    "扬声器",
    "声音",
    "蓝牙",
    "WiFi",
    "机型",
    "账号",
    "锁",
    "进水",
    "拆修",
  ];
  const objectCount = objectTerms.filter((term) => value.includes(term)).length;
  return (
    /另外|还有|同时|以及|并且|两个问题|一是|二是|除此之外/.test(value) ||
    (objectCount >= 2 && /和|与|及|、|又|还/.test(value))
  );
}

function selectRows(rows, predicate, count, selectedKeys) {
  const candidates = rows
    .filter((row) => !selectedKeys.has(row["源记录键"]) && predicate(row))
    .sort(
      (left, right) =>
        hash(`${SAMPLE_SEED}|${left["源记录键"]}`) -
        hash(`${SAMPLE_SEED}|${right["源记录键"]}`),
    );
  const selected = candidates.slice(0, count);
  for (const row of selected) {
    selectedKeys.add(row["源记录键"]);
  }
  return selected;
}

export async function extractClusterSample({
  sourcePath,
  sampleJsonPath,
}) {
  const sourceBlob = await FileBlob.load(sourcePath);
  const sourceWorkbook = await SpreadsheetFile.importXlsx(sourceBlob);
  const sourceRows = [];
  const uploaderNames = new Set();

  for (const sheetName of ["7.15", "7.16"]) {
    const sheet = sourceWorkbook.worksheets.getItem(sheetName);
    const values = sheet.getUsedRange(true).values;
    const headers = values[0].map(text);
    for (let rowIndex = 1; rowIndex < values.length; rowIndex += 1) {
      const valuesRow = values[rowIndex];
      const source = Object.fromEntries(
        headers.map((header, columnIndex) => [
          header,
          valuesRow[columnIndex] ?? "",
        ]),
      );
      if (!Object.values(source).some((value) => text(value))) {
        continue;
      }
      if (text(source["上传者"])) {
        uploaderNames.add(text(source["上传者"]));
      }
      if (text(source["产品类型"]) !== "手机" || !text(source["聊天内容"])) {
        continue;
      }
      const workOrderId = text(source["工单ID"]);
      sourceRows.push({
        源记录键: workOrderId || `${sheetName}-${rowIndex + 1}`,
        源工作表: sheetName,
        源行号: rowIndex + 1,
        工单ID: workOrderId,
        回收单号: text(source["回收单号"]),
        机型: text(source["机型"]),
        聊天内容: text(source["聊天内容"]),
        核心问题: text(source["核心问题"]),
        判定结论: text(source["判定结论"]),
        判定依据: text(source["判定依据"]),
        上游媒体分析摘要: extractFactSummary(source["判定依据"]),
        图片链接: text(source["图片链接"]),
        视频链接: text(source["视频链接"]),
        产品类型: text(source["产品类型"]),
        一级分类: text(source["一级分类"]),
        二级分类: text(source["二级分类"]),
      });
    }
  }

  const byWorkOrder = new Map();
  for (const row of sourceRows) {
    const key = row["源记录键"];
    const current = byWorkOrder.get(key);
    if (
      !current ||
      normalizeChat(row["聊天内容"]).length >
        normalizeChat(current["聊天内容"]).length
    ) {
      byWorkOrder.set(key, row);
    }
  }

  const uploaderList = [...uploaderNames].sort(
    (left, right) => right.length - left.length,
  );
  const deduplicated = [...byWorkOrder.values()].map((row) => {
    const sanitized = { ...row };
    for (const field of [
      "聊天内容",
      "核心问题",
      "判定结论",
      "判定依据",
      "上游媒体分析摘要",
    ]) {
      sanitized[field] = sanitizeText(row[field], uploaderList);
    }
    sanitized["疑似多主题抽样"] = isLikelyMultiTopic(sanitized);
    sanitized["含图片"] = Boolean(text(row["图片链接"]));
    sanitized["含视频"] = Boolean(text(row["视频链接"]));
    return sanitized;
  });

  const selectedKeys = new Set();
  const selected = [
    ...selectRows(
      deduplicated,
      (row) => row["疑似多主题抽样"],
      15,
      selectedKeys,
    ),
    ...selectRows(
      deduplicated,
      (row) => row["含视频"],
      15,
      selectedKeys,
    ),
    ...selectRows(
      deduplicated,
      (row) => row["含图片"] && !row["含视频"],
      15,
      selectedKeys,
    ),
    ...selectRows(deduplicated, () => true, 15, selectedKeys),
  ];

  if (selected.length < SAMPLE_SIZE) {
    selected.push(
      ...selectRows(
        deduplicated,
        () => true,
        SAMPLE_SIZE - selected.length,
        selectedKeys,
      ),
    );
  }
  if (selected.length !== SAMPLE_SIZE) {
    throw new Error(`Expected ${SAMPLE_SIZE} rows, got ${selected.length}.`);
  }

  selected.sort(
    (left, right) =>
      hash(`${SAMPLE_SEED}|final|${left["源记录键"]}`) -
      hash(`${SAMPLE_SEED}|final|${right["源记录键"]}`),
  );
  const outputRows = selected.map((row, index) => ({
    样本ID: `S${String(index + 1).padStart(3, "0")}`,
    ...row,
  }));

  await fs.mkdir(path.dirname(sampleJsonPath), { recursive: true });
  await fs.writeFile(
    sampleJsonPath,
    JSON.stringify(outputRows, null, 2),
    "utf8",
  );
  return {
    sampleJsonPath,
    rows: outputRows.length,
    likelyMultiTopic: outputRows.filter((row) => row["疑似多主题抽样"]).length,
    imageRows: outputRows.filter((row) => row["含图片"]).length,
    videoRows: outputRows.filter((row) => row["含视频"]).length,
  };
}

function columnLetter(index) {
  let value = index + 1;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}

function setColumnWidths(sheet, widths, rowCount) {
  widths.forEach((width, index) => {
    sheet
      .getRangeByIndexes(0, index, Math.max(1, rowCount), 1)
      .format.columnWidth = width;
  });
}

function writeSheetTable({
  workbook,
  name,
  headers,
  rows,
  widths,
  tableName,
  validationColumns = {},
  rowHeight = 72,
}) {
  const sheet = workbook.worksheets.add(name);
  sheet.showGridLines = false;
  const matrix = [headers, ...rows];
  const range = sheet.getRangeByIndexes(0, 0, matrix.length, headers.length);
  range.values = matrix;
  range.format = {
    font: { name: "Microsoft YaHei", size: 10, color: "#172033" },
    verticalAlignment: "top",
    wrapText: true,
  };
  const header = sheet.getRangeByIndexes(0, 0, 1, headers.length);
  header.format = {
    fill: "#176B87",
    font: { name: "Microsoft YaHei", size: 10, bold: true, color: "#FFFFFF" },
    verticalAlignment: "center",
    wrapText: true,
    borders: { preset: "outside", style: "thin", color: "#12566D" },
  };
  header.format.rowHeight = 30;
  if (rows.length) {
    sheet
      .getRangeByIndexes(1, 0, rows.length, headers.length)
      .format.rowHeight = rowHeight;
  }
  setColumnWidths(sheet, widths, matrix.length);
  sheet.freezePanes.freezeRows(1);
  sheet.freezePanes.freezeColumns(Math.min(2, headers.length));
  if (rows.length) {
    const lastColumn = columnLetter(headers.length - 1);
    const table = sheet.tables.add(
      `A1:${lastColumn}${rows.length + 1}`,
      true,
      tableName,
    );
    table.style = "TableStyleMedium2";
    for (const [columnName, values] of Object.entries(validationColumns)) {
      const columnIndex = headers.indexOf(columnName);
      if (columnIndex >= 0) {
        sheet
          .getRangeByIndexes(1, columnIndex, rows.length, 1)
          .dataValidation = {
            rule: { type: "list", values },
          };
      }
    }
  }
  return sheet;
}

function instructionRows(metadata) {
  return [
    ["测试目的", "比较旧聚类处理流程和新聚类处理流程，最终以人工标注准确率判断哪个更好。"],
    ["同一批数据", `两套方案使用相同的 ${metadata.sample_size} 条源会话和相同的 ${metadata.pair_count} 组验证对。`],
    ["待标注", "空白表示尚未处理，不填写“待确定”。"],
    ["单主题", "会话中的内容可以由同一篇知识完整回答；追问、澄清和补充证据不拆分。"],
    ["多主题需拆分", "会话清晰包含两个或以上独立问题，需要不同知识正文、对象或处理标准。此时直接标“多主题需拆分”，不是“不确定”。"],
    ["不确定", "聊天、图片/视频分析摘要仍不足，无法判断真实问题或是否应合并。"],
    ["配对标注", "先独立阅读 A/B，再填写：同一主题、不同主题、多主题需拆分或不确定。"],
    ["同一主题", "A/B 可以由同一篇知识准确回答，问题意图、对象、现象及处理方法基本一致。"],
    ["不同主题", "A/B 需要不同知识回答；仅共享“外观、功能、拆修”等宽泛词不算同一主题。"],
    ["媒体证据", "第二部分的核心问题、判定结论和事实核查摘要已包含图片/视频分析信息；原始链接仅供必要时复核。"],
    ["避免偏差", "标注时不要打开“内部预测”工作表；先完成人工判断，再由管理员查看结果统计。"],
    ["只标一份", "两份文件的“会话标注”和“配对盲标”内容相同。组员可以只标其中一份，回收后把人工列复制到另一份。"],
    ["准确率", "准确率排除空白和“不确定”；“多主题需拆分”是明确结论，会纳入整体处理准确率。"],
    ["向量基线", metadata.vectorizer],
    ["旧流程聚类阈值", metadata.old_threshold],
    ["新流程聚类阈值", metadata.new_threshold],
  ];
}

function workbookForScheme(results, schemeKey) {
  const workbook = Workbook.create();
  const metadata = results.metadata;
  const scheme = results.schemes[schemeKey];
  const sampleById = new Map(
    results.rows.map((row) => [row["样本ID"], row]),
  );

  const guideRows = instructionRows(metadata);
  writeSheetTable({
    workbook,
    name: "标注说明",
    headers: ["项目", "说明"],
    rows: guideRows,
    widths: [18, 88],
    tableName: `Guide_${schemeKey}`,
    rowHeight: 42,
  });

  const conversationHeaders = [
    "样本ID",
    "工单ID",
    "回收单号",
    "机型",
    "聊天内容",
    "核心问题（第二部分）",
    "判定结论（第二部分）",
    "事实核查/媒体分析摘要",
    "图片链接",
    "视频链接",
    "人工会话结构",
    "人工主题1",
    "人工主题2",
    "人工判断依据",
    "标注人",
    "标注时间",
  ];
  const conversationRows = results.rows.map((row) => [
    row["样本ID"],
    row["工单ID"],
    row["回收单号"],
    row["机型"],
    row["聊天内容"],
    row["核心问题"],
    row["判定结论"],
    row["上游媒体分析摘要"],
    row["图片链接"],
    row["视频链接"],
    "",
    "",
    "",
    "",
    "",
    "",
  ]);
  writeSheetTable({
    workbook,
    name: "会话标注",
    headers: conversationHeaders,
    rows: conversationRows,
    widths: [11, 22, 22, 18, 64, 42, 36, 50, 28, 28, 18, 28, 28, 36, 14, 20],
    tableName: `Conversation_${schemeKey}`,
    validationColumns: {
      人工会话结构: ["单主题", "多主题需拆分", "不确定"],
    },
    rowHeight: 92,
  });

  const clusterHeaders = [
    "预测簇ID",
    "问题单元ID",
    "样本ID",
    "模型会话结构",
    "规范化问题",
    "产品品类",
    "适用范围类型",
    "平台",
    "品牌",
    "机型范围",
    "知识一级分类",
    "知识二级分类",
    "问题意图",
    "对象/部位",
    "异常现象",
    "判定目标",
    "处理方式",
    "标准处理路径",
    "阈值/例外条件",
    "证据摘要",
    "模型拆分依据",
    "模型置信度",
    "是否需要复核",
  ];
  const clusterRows = [...scheme.units]
    .sort(
      (left, right) =>
        left.cluster_id.localeCompare(right.cluster_id) ||
        left.unit_id.localeCompare(right.unit_id),
    )
    .map((unit) => [
      unit.cluster_id,
      unit.unit_id,
      unit.sample_id,
      unit.conversation_type,
      unit.normalized_issue,
      unit.product_category ?? "",
      unit.scope_type ?? "",
      unit.platform ?? "",
      unit.brand ?? "",
      unit.model_scope ?? "",
      unit.category_l1 ?? "",
      unit.category_l2 ?? "",
      unit.intent ?? "",
      unit.subject ?? "",
      unit.phenomenon ?? "",
      unit.judgment_target ?? "",
      unit.resolution_mode ?? "",
      unit.standard_path ?? "",
      unit.threshold_or_exception ?? "",
      unit.evidence_summary ?? "",
      unit.reason ?? "",
      unit.confidence ?? "",
      unit.requires_review ? "是" : "否",
    ]);
  writeSheetTable({
    workbook,
    name: "聚类结果",
    headers: clusterHeaders,
    rows: clusterRows,
    widths: [
      13, 16, 11, 18, 38, 14, 16, 14, 14, 18, 18, 18, 16, 20, 24, 24, 28,
      28, 28, 48, 42, 14, 16,
    ],
    tableName: `Cluster_${schemeKey}`,
    rowHeight: 72,
  });

  const pairHeaders = [
    "样本对ID",
    "会话A_样本ID",
    "会话A_聊天内容",
    "会话A_核心问题",
    "会话A_媒体分析摘要",
    "会话B_样本ID",
    "会话B_聊天内容",
    "会话B_核心问题",
    "会话B_媒体分析摘要",
    "人工判断",
    "人工关键差异/依据",
    "标注人",
    "标注时间",
  ];
  const pairRows = results.pairs.map((pair) => {
    const left = sampleById.get(pair.left_id);
    const right = sampleById.get(pair.right_id);
    return [
      pair.pair_id,
      left["样本ID"],
      left["聊天内容"],
      left["核心问题"],
      left["上游媒体分析摘要"],
      right["样本ID"],
      right["聊天内容"],
      right["核心问题"],
      right["上游媒体分析摘要"],
      "",
      "",
      "",
      "",
    ];
  });
  writeSheetTable({
    workbook,
    name: "配对盲标",
    headers: pairHeaders,
    rows: pairRows,
    widths: [12, 13, 58, 38, 46, 13, 58, 38, 46, 20, 40, 14, 20],
    tableName: `Pairs_${schemeKey}`,
    validationColumns: {
      人工判断: ["同一主题", "不同主题", "多主题需拆分", "不确定"],
    },
    rowHeight: 96,
  });

  const predictionHeaders = [
    "样本对ID",
    "流程预测",
    "流程相似度",
    "人工判断",
    "是否正确",
  ];
  const predictionRows = results.pairs.map((pair) => [
    pair.pair_id,
    pair[`${schemeKey}_prediction`],
    pair[`${schemeKey}_similarity`],
    "",
    "",
  ]);
  const predictionSheet = writeSheetTable({
    workbook,
    name: "内部预测",
    headers: predictionHeaders,
    rows: predictionRows,
    widths: [14, 22, 16, 22, 14],
    tableName: `Prediction_${schemeKey}`,
    rowHeight: 30,
  });
  if (predictionRows.length) {
    predictionSheet.getRange("D2").formulas = [
      ["=IF('配对盲标'!J2=\"\",\"\",'配对盲标'!J2)"],
    ];
    predictionSheet
      .getRange(`D2:D${predictionRows.length + 1}`)
      .fillDown();
    predictionSheet.getRange("E2").formulas = [
      [
        '=IF(OR(D2="",D2="不确定"),"",IF(B2=D2,1,0))',
      ],
    ];
    predictionSheet
      .getRange(`E2:E${predictionRows.length + 1}`)
      .fillDown();
    predictionSheet
      .getRange(`C2:C${predictionRows.length + 1}`)
      .format.numberFormat = "0.000";
  }

  const stats = workbook.worksheets.add("结果统计");
  stats.showGridLines = false;
  stats.getRange("A1:B10").values = [
    ["指标", "结果"],
    ["方案", scheme.name],
    ["源会话数", metadata.sample_size],
    ["预测问题单元数", scheme.units.length],
    ["预测簇数", scheme.cluster_count],
    ["模型识别多主题数", scheme.multi_topic_rows ?? 0],
    ["模型不确定数", scheme.uncertain_rows ?? 0],
    ["已形成明确人工结论数", ""],
    ["预测正确数", ""],
    ["整体处理准确率", ""],
  ];
  stats.getRange("B8").formulas = [
    [`=COUNT('内部预测'!E2:E${predictionRows.length + 1})`],
  ];
  stats.getRange("B9").formulas = [
    [`=SUM('内部预测'!E2:E${predictionRows.length + 1})`],
  ];
  stats.getRange("B10").formulas = [["=IFERROR(B9/B8,\"\")"]];
  stats.getRange("A1:B1").format = {
    fill: "#176B87",
    font: { name: "Microsoft YaHei", bold: true, color: "#FFFFFF" },
  };
  stats.getRange("A1:B10").format = {
    font: { name: "Microsoft YaHei", size: 11 },
    verticalAlignment: "center",
  };
  stats.getRange("B10").format.numberFormat = "0.0%";
  stats.getRange("A1:A10").format.columnWidth = 26;
  stats.getRange("B1:B10").format.columnWidth = 34;
  stats.getRange("A1:B10").format.rowHeight = 30;
  stats.freezePanes.freezeRows(1);

  return workbook;
}

async function saveWorkbookWithPreviews({
  workbook,
  outputPath,
  previewDir,
  previewPrefix,
}) {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(previewDir, { recursive: true });
  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(outputPath);
  for (const sheetName of [
    "标注说明",
    "会话标注",
    "聚类结果",
    "配对盲标",
    "内部预测",
    "结果统计",
  ]) {
    const preview = await workbook.render({
      sheetName,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    await fs.writeFile(
      path.join(previewDir, `${previewPrefix}_${sheetName}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }
}

export async function buildClusterWorkbooks({
  resultsJsonPath,
  outputDir,
}) {
  const results = JSON.parse(await fs.readFile(resultsJsonPath, "utf8"));
  const oldWorkbook = workbookForScheme(results, "old");
  const newWorkbook = workbookForScheme(results, "new");
  const oldPath = path.join(
    outputDir,
    "方案A_旧流程_60条聚类盲标.xlsx",
  );
  const newPath = path.join(
    outputDir,
    "方案B_新流程_60条聚类盲标.xlsx",
  );
  const previewDir = path.join(outputDir, "previews");
  await saveWorkbookWithPreviews({
    workbook: oldWorkbook,
    outputPath: oldPath,
    previewDir,
    previewPrefix: "方案A",
  });
  await saveWorkbookWithPreviews({
    workbook: newWorkbook,
    outputPath: newPath,
    previewDir,
    previewPrefix: "方案B",
  });
  return { oldPath, newPath, previewDir };
}
