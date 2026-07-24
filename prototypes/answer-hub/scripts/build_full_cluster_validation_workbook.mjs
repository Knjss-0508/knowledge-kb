import fs from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";


function parseArgs(argv) {
  const args = {};
  for (let index = 2; index < argv.length; index += 2) {
    const key = argv[index];
    const value = argv[index + 1];
    if (!key?.startsWith("--") || value === undefined) {
      throw new Error(`参数格式错误：${key ?? ""}`);
    }
    args[key.slice(2)] = value;
  }
  for (const required of ["input-json", "output-xlsx", "preview-dir"]) {
    if (!args[required]) {
      throw new Error(`缺少参数 --${required}`);
    }
  }
  return args;
}


function text(value) {
  if (value === null || value === undefined) return "";
  if (Array.isArray(value)) return value.map(text).filter(Boolean).join("\n");
  if (typeof value === "object") return JSON.stringify(value, null, 2);
  return String(value).trim();
}


function unique(values) {
  return [...new Set(values.map(text).filter(Boolean))];
}


function columnLetter(columnCount) {
  let value = columnCount;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}


function applyHeaderStyle(range, fill = "#1F4E78") {
  range.format = {
    fill,
    font: { bold: true, color: "#FFFFFF" },
    wrapText: true,
    verticalAlignment: "center",
    horizontalAlignment: "center",
    borders: {
      bottom: { style: "medium", color: "#17365D" },
    },
  };
  range.format.rowHeight = 32;
}


function applyBodyStyle(range) {
  range.format = {
    wrapText: true,
    verticalAlignment: "top",
    borders: {
      insideHorizontal: { style: "thin", color: "#E2E8F0" },
    },
  };
}


function writeTable(sheet, headers, rows, tableName) {
  const matrix = [headers, ...rows];
  const endColumn = columnLetter(headers.length);
  const endRow = Math.max(1, matrix.length);
  sheet.getRange(`A1:${endColumn}${endRow}`).values = matrix;
  applyHeaderStyle(sheet.getRange(`A1:${endColumn}1`));
  if (rows.length) {
    applyBodyStyle(sheet.getRange(`A2:${endColumn}${endRow}`));
  }
  const table = sheet.tables.add(
    `A1:${endColumn}${endRow}`,
    true,
    tableName,
  );
  table.style = "TableStyleMedium2";
  table.showBandedRows = true;
  table.showFilterButton = true;
  sheet.freezePanes.freezeRows(1);
  sheet.showGridLines = false;
  return { endColumn, endRow };
}


function setColumnWidths(sheet, widths) {
  for (const [column, width] of Object.entries(widths)) {
    sheet.getRange(`${column}:${column}`).format.columnWidth = width;
  }
}


function clusterReviewRows(clusters, unitById) {
  return clusters.map((cluster) => {
    const units = (cluster.member_atomic_ids ?? [])
      .map((atomicId) => unitById.get(atomicId))
      .filter(Boolean);
    const sourceSampleIds = unique(units.map((unit) => unit.sample_id));
    const memberText = (field) => units
      .map((unit) => {
        const value = text(unit[field]);
        return value ? `【${text(unit.sample_id)}】${value}` : "";
      })
      .filter(Boolean)
      .join("\n\n");
    const mediaFacts = units
      .map((unit) => {
        const media = unit.media_analysis ?? {};
        const facts = [
          text(media.image_summary) && `图片：${text(media.image_summary)}`,
          text(media.video_summary) && `视频：${text(media.video_summary)}`,
        ].filter(Boolean).join("\n");
        return facts ? `【${text(unit.sample_id)}】${facts}` : "";
      })
      .filter(Boolean)
      .join("\n\n");
    const reviewReasons = [];
    if (units.some((unit) => Boolean(unit.requires_review))) {
      reviewReasons.push("成员含上游人工复核标记");
    }
    if (cluster.title_status === "error") {
      reviewReasons.push(`标题生成失败：${text(cluster.title_error)}`);
    }
    if (cluster.source === "program_rule_singleton") {
      reviewReasons.push("程序门禁拆分为单成员簇");
    }
    return [
      text(cluster.cluster_id),
      text(cluster.theme_title || cluster.theme_name),
      Number(cluster.member_count ?? units.length),
      text(cluster.product_category),
      text(cluster.scope_type),
      text(cluster.platform),
      text(cluster.brand),
      text(cluster.category_l1),
      text(cluster.intent),
      text(cluster.member_atomic_ids),
      sourceSampleIds.join("\n"),
      memberText("source_core_problem"),
      memberText("source_conversation"),
      memberText("image_links"),
      memberText("video_links"),
      mediaFacts,
      memberText("normalized_issue"),
      text(cluster.shared_knowledge_definition),
      text(cluster.merge_basis),
      reviewReasons.join("；") || "无",
      "",
      "",
      "",
      "",
      "",
      "",
      "",
      "",
    ];
  });
}


function atomicDetailRows(units, clusterByAtomicId) {
  return units.map((unit) => {
    const cluster = clusterByAtomicId.get(unit.unit_id) ?? {};
    const media = unit.media_analysis ?? {};
    return [
      text(unit.unit_id),
      text(unit.sample_id),
      text(unit.source_record_key),
      text(unit.work_order_id),
      text(cluster.cluster_id),
      text(cluster.theme_title || cluster.theme_name),
      text(unit.conversation_type),
      text(unit.product_category),
      text(unit.device_model),
      text(unit.scope_type),
      text(unit.platform),
      text(unit.brand),
      text(unit.model_scope),
      text(unit.category_l1),
      text(unit.category_l2),
      text(unit.intent),
      text(unit.subject),
      text(unit.phenomenon),
      text(unit.judgment_target),
      text(unit.resolution_mode),
      text(unit.standard_path),
      text(unit.threshold_or_exception),
      text(unit.normalized_issue),
      text(unit.evidence_summary),
      text(media.image_summary),
      text(media.video_summary),
      Boolean(unit.requires_review) ? "是" : "否",
      Number(unit.confidence || 0),
      text(unit.source_core_problem),
      text(unit.source_conversation),
      "",
      "",
      "",
      "",
      "",
      "",
    ];
  });
}


function reviewQueueRows(payload, unitById, clusterByAtomicId) {
  const rows = [];
  const seen = new Set();
  const push = (atomicId, reviewType, reason, suggested = "") => {
    const key = `${atomicId}|${reviewType}|${reason}`;
    if (seen.has(key)) return;
    seen.add(key);
    const unit = unitById.get(atomicId) ?? {};
    const cluster = clusterByAtomicId.get(atomicId) ?? {};
    rows.push([
      atomicId,
      text(unit.sample_id),
      text(cluster.cluster_id),
      text(cluster.theme_title || cluster.theme_name),
      reviewType,
      reason,
      suggested,
      text(unit.normalized_issue),
      text(unit.product_category),
      text(unit.device_model),
      text(unit.evidence_summary),
      "",
      "",
      "",
      "",
    ]);
  };
  for (const request of payload.review_requests ?? []) {
    push(
      text(request.atomic_id),
      `字段复核-${text(request.review_type) || "其他"}`,
      text(request.reason),
    );
  }
  for (const request of payload.split_requests ?? []) {
    push(
      text(request.atomic_id),
      "仍需拆分",
      text(request.reason),
      text(request.suggested_splits),
    );
  }
  for (const unit of payload.atomic_units ?? []) {
    if (unit.requires_review) {
      push(
        text(unit.unit_id),
        "上游建议复核",
        text(unit.fusion_reason) || "原子主题提取或媒体融合阶段标记需要人工复核",
      );
    }
  }
  return rows;
}


const args = parseArgs(process.argv);
const inputPath = path.resolve(args["input-json"]);
const outputPath = path.resolve(args["output-xlsx"]);
const previewDir = path.resolve(args["preview-dir"]);
const payload = JSON.parse(await fs.readFile(inputPath, "utf8"));
const clusters = payload.clusters ?? [];
const units = payload.atomic_units ?? [];
const metadata = payload.metadata ?? {};
const excludedRows = payload.excluded_rows ?? [];
const sourceMetadata = payload.source_metadata ?? {};

const unitById = new Map(
  units.map((unit) => [text(unit.unit_id), unit]),
);
const clusterByAtomicId = new Map();
for (const cluster of clusters) {
  for (const atomicId of cluster.member_atomic_ids ?? []) {
    clusterByAtomicId.set(text(atomicId), cluster);
  }
}

const workbook = Workbook.create();
workbook.comments.setSelf({ displayName: "User" });
const guide = workbook.worksheets.add("标注说明");
const summary = workbook.worksheets.add("指标统计");
const clusterSheet = workbook.worksheets.add("主题簇复核");
const atomicSheet = workbook.worksheets.add("原子问题明细");
const reviewSheet = workbook.worksheets.add("待人工复核");

const guideRows = [
  ["项目", "说明"],
  ["验证目标", "使用379条案例完成原子主题提取、完整1-N聚类和主题标题验证。"],
  ["归簇正确", "簇内所有成员可以共用同一条标准答疑知识；适用范围、对象、判定目标、处理路径和阈值例外一致。"],
  ["归簇错误", "簇内至少一个成员需要不同知识回答，或该成员应移动到其他主题簇。"],
  ["标题正确", "模型主题标题能够准确概括该簇共同问题，表达自然，不遗漏关键对象或判断目标。"],
  ["标题错误", "标题过宽、过窄、对象错误、包含案例叙述或未覆盖簇内共同问题。"],
  ["误合并优先级", "误合并会导致一条知识混入多个结论，风险高于误拆分；发现后优先拆簇。"],
  ["无效案例删除", "缺少有效答疑会话、无法识别具体问题且媒体不相关的案例直接排除，不进入聚类。"],
  ["品类隔离", "只有产品品类完全一致才能聚类；手机、平板、电脑等不同品类即使问题相似也必须拆分。"],
  ["人工主题ID", "需要调整时填写稳定人工主题ID，例如 GOLD-001；相同人工主题使用同一ID。"],
  ["人工判断填写", "主题簇复核表已展示每个成员的核心问题、完整聊天、图片/视频链接和媒体事实；填写“归簇判断”和“标题判断”，原子问题明细用于补充移动、合并和多主题拆分。"],
  ["第一版门槛", "指标统计表默认以80%作为第一版门槛；至少完成20个主题簇的归簇与标题复核。"],
  ["数据来源", "质检答疑案例库 (4).xlsx，共379条案例。"],
];
guide.getRange(`A1:B${guideRows.length}`).values = guideRows;
applyHeaderStyle(guide.getRange("A1:B1"));
applyBodyStyle(guide.getRange(`A2:B${guideRows.length}`));
guide.getRange("A2:A11").format.font = { bold: true, color: "#1F4E78" };
guide.getRange("A:A").format.columnWidth = 20;
guide.getRange("B:B").format.columnWidth = 90;
guide.getRange(`A1:B${guideRows.length}`).format.wrapText = true;
guide.freezePanes.freezeRows(1);
guide.showGridLines = false;

const clusterHeaders = [
  "模型簇ID",
  "模型主题标题",
  "成员数",
  "产品类型",
  "适用范围",
  "平台",
  "品牌",
  "一级分类",
  "意图",
  "成员原子ID",
  "来源样本ID",
  "成员核心问题",
  "成员完整聊天内容",
  "成员图片链接",
  "成员视频链接",
  "成员媒体识别事实",
  "成员标准化问题",
  "共享知识定义",
  "模型合并依据",
  "模型建议复核",
  "人工归簇判断",
  "人工标题判断",
  "人工主题ID",
  "人工主题标题",
  "应拆分/合并到",
  "人工备注",
  "审核人",
  "审核时间",
];
const clusterRows = clusterReviewRows(clusters, unitById);
const clusterTable = writeTable(
  clusterSheet,
  clusterHeaders,
  clusterRows,
  "ClusterReviewTable",
);
clusterSheet.freezePanes.freezeColumns(2);
setColumnWidths(clusterSheet, {
  A: 12, B: 34, C: 9, D: 12, E: 14, F: 12, G: 12, H: 15, I: 14,
  J: 24, K: 20, L: 52, M: 72, N: 48, O: 48, P: 58, Q: 52, R: 48,
  S: 48, T: 34, U: 14, V: 14, W: 16, X: 30, Y: 22, Z: 36, AA: 12,
  AB: 20,
});
if (clusterRows.length) {
  clusterSheet.getRange(`C2:C${clusterTable.endRow}`).format.numberFormat = "#,##0";
  clusterSheet.getRange(`U2:U${clusterTable.endRow}`).dataValidation = {
    rule: { type: "list", values: ["正确", "错误", "待定"] },
  };
  clusterSheet.getRange(`V2:V${clusterTable.endRow}`).dataValidation = {
    rule: { type: "list", values: ["正确", "错误", "待定"] },
  };
  clusterSheet.getRange(`U2:V${clusterTable.endRow}`).conditionalFormats.add(
    "containsText",
    { text: "正确", format: { fill: "#DCFCE7", font: { color: "#166534" } } },
  );
  clusterSheet.getRange(`U2:V${clusterTable.endRow}`).conditionalFormats.add(
    "containsText",
    { text: "错误", format: { fill: "#FEE2E2", font: { color: "#991B1B" } } },
  );
  clusterSheet.getRange(`T2:T${clusterTable.endRow}`).conditionalFormats.add(
    "notContainsText",
    { text: "无", format: { fill: "#FEF3C7", font: { color: "#92400E" } } },
  );
}

const atomicHeaders = [
  "原子ID",
  "来源样本ID",
  "源记录键",
  "工单ID",
  "模型簇ID",
  "模型主题标题",
  "会话主题类型",
  "产品类型",
  "机型",
  "适用范围",
  "平台",
  "品牌",
  "机型范围",
  "一级分类",
  "二级分类",
  "意图",
  "对象/部位",
  "异常现象",
  "判定目标",
  "处理方式",
  "标准路径",
  "阈值/例外",
  "标准化问题",
  "证据摘要",
  "图片事实",
  "视频事实",
  "模型建议复核",
  "置信度",
  "源核心问题",
  "源聊天内容",
  "人工主题ID",
  "人工主题标题",
  "人工归簇判断",
  "人工多主题判断",
  "应移至模型簇ID",
  "人工备注",
];
const atomicRows = atomicDetailRows(units, clusterByAtomicId);
const atomicTable = writeTable(
  atomicSheet,
  atomicHeaders,
  atomicRows,
  "AtomicDetailTable",
);
atomicSheet.freezePanes.freezeColumns(2);
setColumnWidths(atomicSheet, {
  A: 14, B: 13, C: 19, D: 19, E: 12, F: 34, G: 15, H: 12, I: 24,
  J: 14, K: 12, L: 12, M: 18, N: 15, O: 18, P: 14, Q: 16, R: 24,
  S: 30, T: 34, U: 34, V: 24, W: 48, X: 52, Y: 46, Z: 46, AA: 14,
  AB: 10, AC: 52, AD: 70, AE: 16, AF: 30, AG: 14, AH: 16, AI: 18,
  AJ: 36,
});
if (atomicRows.length) {
  atomicSheet.getRange(`AB2:AB${atomicTable.endRow}`).format.numberFormat = "0.0%";
  atomicSheet.getRange(`AG2:AG${atomicTable.endRow}`).dataValidation = {
    rule: { type: "list", values: ["正确", "错误", "待定"] },
  };
  atomicSheet.getRange(`AH2:AH${atomicTable.endRow}`).dataValidation = {
    rule: { type: "list", values: ["单主题", "多主题需拆分", "不确定"] },
  };
}

const reviewHeaders = [
  "原子ID",
  "来源样本ID",
  "模型簇ID",
  "模型主题标题",
  "复核类型",
  "复核原因",
  "建议拆分方向",
  "标准化问题",
  "产品类型",
  "机型",
  "证据摘要",
  "处理状态",
  "人工结论",
  "审核人",
  "审核时间",
];
const reviewRows = reviewQueueRows(payload, unitById, clusterByAtomicId);
const reviewTable = writeTable(
  reviewSheet,
  reviewHeaders,
  reviewRows,
  "ReviewQueueTable",
);
reviewSheet.freezePanes.freezeColumns(2);
setColumnWidths(reviewSheet, {
  A: 14, B: 13, C: 12, D: 34, E: 20, F: 48, G: 34, H: 48, I: 12,
  J: 24, K: 52, L: 14, M: 38, N: 12, O: 20,
});
if (reviewRows.length) {
  reviewSheet.getRange(`L2:L${reviewTable.endRow}`).dataValidation = {
    rule: { type: "list", values: ["待处理", "已确认", "已调整", "无需调整"] },
  };
  reviewSheet.getRange(`L2:L${reviewTable.endRow}`).conditionalFormats.add(
    "containsText",
    { text: "待处理", format: { fill: "#FEF3C7", font: { color: "#92400E" } } },
  );
  reviewSheet.getRange(`L2:L${reviewTable.endRow}`).conditionalFormats.add(
    "containsText",
    { text: "已", format: { fill: "#DCFCE7", font: { color: "#166534" } } },
  );
}

const sampleIds = unique(units.map((unit) => unit.sample_id));
const sourceCaseCount = Number(
  sourceMetadata.sample_size
  ?? sampleIds.length + excludedRows.length,
);
const sampleTypeById = new Map();
for (const unit of units) {
  sampleTypeById.set(text(unit.sample_id), text(unit.conversation_type));
}
const singleTopicRows = [...sampleTypeById.values()].filter(
  (value) => value === "single_topic",
).length;
const multiTopicRows = [...sampleTypeById.values()].filter(
  (value) => value === "multi_topic",
).length;
const uncertainRows = [...sampleTypeById.values()].filter(
  (value) => value === "uncertain",
).length;
const multiMemberClusters = clusters.filter(
  (cluster) => Number(cluster.member_count) > 1,
).length;
const titleCompleted = clusters.filter(
  (cluster) => cluster.title_status === "ok",
).length;
const titleErrors = clusters.filter(
  (cluster) => cluster.title_status === "error",
).length;
const maxClusterSize = clusters.reduce(
  (maximum, cluster) => Math.max(maximum, Number(cluster.member_count || 0)),
  0,
);
const threshold = 0.8;
const minimumReviewCount = 20;
const clusterEndRow = Math.max(2, clusterTable.endRow);

summary.getRange("A1:H1").merge();
summary.getRange("A1").values = [["379条案例完整聚类＋主题标题验证"]];
summary.getRange("A1:H1").format = {
  fill: "#17365D",
  font: { bold: true, color: "#FFFFFF", size: 18 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
};
summary.getRange("A1:H1").format.rowHeight = 38;
summary.getRange("A3:B3").values = [["验证参数", "数值"]];
applyHeaderStyle(summary.getRange("A3:B3"), "#4472C4");
summary.getRange("A4:B5").values = [
  ["第一版准确率门槛", threshold],
  ["最低完成复核主题数", minimumReviewCount],
];
summary.getRange("B4").format.numberFormat = "0.0%";
summary.getRange("B5").format.numberFormat = "#,##0";
summary.getRange("D3:E3").values = [["结构指标", "结果"]];
applyHeaderStyle(summary.getRange("D3:E3"), "#4472C4");
summary.getRange("D4:E17").values = [
  ["源案例总数", sourceCaseCount],
  ["纳入聚类案例数", sampleIds.length],
  ["直接排除无效案例数", excludedRows.length],
  ["原子问题数", units.length],
  ["单主题会话数", singleTopicRows],
  ["多主题会话数", multiTopicRows],
  ["不确定会话数", uncertainRows],
  ["主题簇数", clusters.length],
  ["多成员主题簇数", multiMemberClusters],
  ["单成员主题簇数", clusters.length - multiMemberClusters],
  ["最大簇成员数", maxClusterSize],
  ["主题标题生成成功数", titleCompleted],
  ["主题标题生成失败数", titleErrors],
  ["模型待人工复核原子数", reviewRows.length],
];
summary.getRange("E4:E17").format.numberFormat = "#,##0";
summary.getRange("G3:H3").values = [["人工验证指标", "结果"]];
applyHeaderStyle(summary.getRange("G3:H3"), "#70AD47");
summary.getRange("G4:G11").values = [
  ["已复核归簇主题数"],
  ["归簇正确主题数"],
  ["归簇准确率"],
  ["已复核标题主题数"],
  ["标题正确主题数"],
  ["标题准确率"],
  ["双项均正确主题数"],
  ["第一版状态"],
];
summary.getRange("H4:H11").formulas = [
  [`=COUNTIF('主题簇复核'!U2:U${clusterEndRow},"正确")+COUNTIF('主题簇复核'!U2:U${clusterEndRow},"错误")`],
  [`=COUNTIF('主题簇复核'!U2:U${clusterEndRow},"正确")`],
  ["=IFERROR(H5/H4,0)"],
  [`=COUNTIF('主题簇复核'!V2:V${clusterEndRow},"正确")+COUNTIF('主题簇复核'!V2:V${clusterEndRow},"错误")`],
  [`=COUNTIF('主题簇复核'!V2:V${clusterEndRow},"正确")`],
  ["=IFERROR(H8/H7,0)"],
  [`=COUNTIFS('主题簇复核'!U2:U${clusterEndRow},"正确",'主题簇复核'!V2:V${clusterEndRow},"正确")`],
  ['=IF(AND(H4>=$B$5,H7>=$B$5,H6>=$B$4,H9>=$B$4),"达到第一版门槛","待复核或未达到门槛")'],
];
summary.getRange("H6").format.numberFormat = "0.0%";
summary.getRange("H9").format.numberFormat = "0.0%";
summary.getRange("H11").conditionalFormats.add(
  "containsText",
  { text: "达到第一版门槛", format: { fill: "#DCFCE7", font: { bold: true, color: "#166534" } } },
);
summary.getRange("H11").conditionalFormats.add(
  "containsText",
  { text: "待复核", format: { fill: "#FEF3C7", font: { bold: true, color: "#92400E" } } },
);
summary.getRange("A19:H19").merge();
summary.getRange("A19").values = [[
  "说明：当前表先展示模型结构指标；人工在“主题簇复核”填写归簇与标题判断后，本页准确率和第一版状态会自动更新。",
]];
summary.getRange("A19:H19").format = {
  fill: "#EAF2F8",
  font: { color: "#1F4E78", italic: true },
  wrapText: true,
};
summary.getRange("A19:H19").format.rowHeight = 34;
summary.getRange("A:A").format.columnWidth = 24;
summary.getRange("B:B").format.columnWidth = 18;
summary.getRange("C:C").format.columnWidth = 4;
summary.getRange("D:D").format.columnWidth = 26;
summary.getRange("E:E").format.columnWidth = 16;
summary.getRange("F:F").format.columnWidth = 4;
summary.getRange("G:G").format.columnWidth = 28;
summary.getRange("H:H").format.columnWidth = 24;
applyBodyStyle(summary.getRange("A4:B5"));
applyBodyStyle(summary.getRange("D4:E17"));
applyBodyStyle(summary.getRange("G4:H11"));
summary.showGridLines = false;
summary.freezePanes.freezeRows(1);

await fs.mkdir(path.dirname(outputPath), { recursive: true });
await fs.mkdir(previewDir, { recursive: true });

const inspectSummary = await workbook.inspect({
  kind: "table",
  range: "指标统计!A1:H19",
  include: "values,formulas",
  tableMaxRows: 20,
  tableMaxCols: 10,
});
await fs.writeFile(
  path.join(previewDir, "inspect_summary.ndjson"),
  inspectSummary.ndjson,
  "utf8",
);
const formulaErrors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 300 },
  summary: "final formula error scan",
});
await fs.writeFile(
  path.join(previewDir, "formula_errors.ndjson"),
  formulaErrors.ndjson,
  "utf8",
);

const previews = [
  ["标注说明", `A1:B${guideRows.length}`, "guide.png"],
  ["指标统计", "A1:H19", "summary.png"],
  ["主题簇复核", `A1:AB${Math.min(clusterTable.endRow, 14)}`, "clusters.png"],
  ["原子问题明细", `A1:AJ${Math.min(atomicTable.endRow, 10)}`, "atomic.png"],
  ["待人工复核", `A1:O${Math.min(reviewTable.endRow, 12)}`, "review.png"],
];
for (const [sheetName, range, filename] of previews) {
  const preview = await workbook.render({
    sheetName,
    range,
    scale: 1,
    format: "png",
  });
  await fs.writeFile(
    path.join(previewDir, filename),
    new Uint8Array(await preview.arrayBuffer()),
  );
}

const output = await SpreadsheetFile.exportXlsx(workbook);
await output.save(outputPath);

console.log(JSON.stringify({
  output_xlsx: outputPath,
  source_cases: sourceCaseCount,
  included_cases: sampleIds.length,
  excluded_cases: excludedRows.length,
  atomic_units: units.length,
  clusters: clusters.length,
  multi_member_clusters: multiMemberClusters,
  title_completed: titleCompleted,
  review_rows: reviewRows.length,
  metadata_status: text(metadata.status),
}, null, 2));
