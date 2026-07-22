import fs from "node:fs/promises";
import {
  FileBlob,
  SpreadsheetFile,
  Workbook,
} from "@oai/artifact-tool";

const SOURCE_PATH = "C:\\Users\\admin\\Downloads\\质检答疑案例库.xlsx";
const OUTPUT_PATH =
  "C:\\Users\\admin\\Documents\\答疑中台知识库\\data\\聚类样本_手机_100条_脱敏_2026-07-16.xlsx";
const PREVIEW_PATH =
  "C:\\Users\\admin\\.codex\\visualizations\\2026\\07\\16\\019f6b62-f56c-7000-9372-0a840b32f893\\聚类样本_手机_100条_脱敏_预览.png";
const SAMPLE_SIZE = 100;
const SAMPLE_SEED = 20260716;

const sourceBlob = await FileBlob.load(SOURCE_PATH);
const sourceWorkbook = await SpreadsheetFile.importXlsx(sourceBlob);
const source715 = sourceWorkbook.worksheets.getItem("7.15");
const source716 = sourceWorkbook.worksheets.getItem("7.16");
const values715 = source715.getRange("A1:P380").values;
const values716 = source716.getRange("A1:P392").values;

const uploaderNames = new Set();
const sourceRows = [];

function hasValue(value) {
  return value !== null && value !== undefined && String(value).trim() !== "";
}

function addSourceRows(sheetName, values) {
  for (let index = 1; index < values.length; index += 1) {
    const row = values[index].slice(0, 16);
    if (!row.some(hasValue)) {
      continue;
    }
    if (hasValue(row[0])) {
      uploaderNames.add(String(row[0]).trim());
    }
    sourceRows.push({
      sheetName,
      sourceRow: index + 1,
      values: row,
    });
  }
}

addSourceRows("7.15", values715);
addSourceRows("7.16", values716);

function normalizeChat(value) {
  return String(value ?? "")
    .replace(/\s+/g, " ")
    .trim();
}

const phoneRows = sourceRows.filter((row) => {
  const productType = String(row.values[10] ?? row.values[4] ?? "").trim();
  return (
    productType === "手机" &&
    hasValue(row.values[6]) &&
    hasValue(row.values[7])
  );
});

const deduplicatedRows = [];
const seenChats = new Set();
for (const row of phoneRows) {
  const key = normalizeChat(row.values[6]);
  if (!key || seenChats.has(key)) {
    continue;
  }
  seenChats.add(key);
  deduplicatedRows.push(row);
}

function mulberry32(seed) {
  let state = seed >>> 0;
  return function random() {
    state += 0x6d2b79f5;
    let value = state;
    value = Math.imul(value ^ (value >>> 15), value | 1);
    value ^= value + Math.imul(value ^ (value >>> 7), value | 61);
    return ((value ^ (value >>> 14)) >>> 0) / 4294967296;
  };
}

function shuffle(items, random) {
  const result = [...items];
  for (let index = result.length - 1; index > 0; index -= 1) {
    const swapIndex = Math.floor(random() * (index + 1));
    [result[index], result[swapIndex]] = [result[swapIndex], result[index]];
  }
  return result;
}

function groupBy(items, keyFn) {
  const grouped = new Map();
  for (const item of items) {
    const key = keyFn(item) || "未分类";
    if (!grouped.has(key)) {
      grouped.set(key, []);
    }
    grouped.get(key).push(item);
  }
  return grouped;
}

function allocateQuotas(grouped, total) {
  const population = [...grouped.values()].reduce(
    (sum, rows) => sum + rows.length,
    0,
  );
  const entries = [...grouped.entries()].map(([name, rows]) => {
    const exact = (rows.length * total) / population;
    return {
      name,
      count: rows.length,
      quota: Math.floor(exact),
      remainder: exact - Math.floor(exact),
    };
  });

  let remaining =
    total - entries.reduce((sum, entry) => sum + entry.quota, 0);
  entries.sort(
    (left, right) =>
      right.remainder - left.remainder ||
      right.count - left.count ||
      left.name.localeCompare(right.name, "zh-CN"),
  );
  for (let index = 0; index < remaining; index += 1) {
    entries[index % entries.length].quota += 1;
  }
  return new Map(entries.map((entry) => [entry.name, entry.quota]));
}

function selectDiverseRows(rows, quota, seedOffset) {
  const random = mulberry32(SAMPLE_SEED + seedOffset);
  const byLevel2 = groupBy(rows, (row) => String(row.values[12] ?? "").trim());
  const buckets = shuffle(
    [...byLevel2.entries()].map(([name, bucket]) => ({
      name,
      rows: shuffle(bucket, random),
      cursor: 0,
    })),
    random,
  );

  const selected = [];
  while (selected.length < quota) {
    let added = false;
    for (const bucket of buckets) {
      if (selected.length >= quota) {
        break;
      }
      if (bucket.cursor < bucket.rows.length) {
        selected.push(bucket.rows[bucket.cursor]);
        bucket.cursor += 1;
        added = true;
      }
    }
    if (!added) {
      break;
    }
  }
  return selected;
}

const byLevel1 = groupBy(
  deduplicatedRows,
  (row) => String(row.values[11] ?? "").trim(),
);
const quotas = allocateQuotas(byLevel1, SAMPLE_SIZE);
let sampledRows = [];
let categoryIndex = 0;
for (const [level1, rows] of [...byLevel1.entries()].sort(([left], [right]) =>
  left.localeCompare(right, "zh-CN"),
)) {
  sampledRows.push(
    ...selectDiverseRows(rows, quotas.get(level1) ?? 0, categoryIndex * 997),
  );
  categoryIndex += 1;
}
sampledRows = shuffle(sampledRows, mulberry32(SAMPLE_SEED + 100000));

if (sampledRows.length !== SAMPLE_SIZE) {
  throw new Error(
    `Expected ${SAMPLE_SIZE} sampled rows, got ${sampledRows.length}.`,
  );
}

const sortedNames = [...uploaderNames].sort(
  (left, right) => right.length - left.length,
);

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function sanitizeText(value) {
  if (!hasValue(value)) {
    return "";
  }

  let text = String(value).replace(/\r\n?/g, "\n");
  text = text.replace(
    /^\s*\d{2,4}[/-]\d{1,2}[/-]\d{1,2}\s+\d{1,2}:\d{2}(?::\d{2})+(?::\d{2})?\s*/gm,
    "",
  );
  text = text.replace(
    /(?:IMEI|MEID|序列号|SN|串号|设备号)\s*[:：]?\s*[A-Z0-9-]{6,}/gi,
    (match) => `${match.split(/[:：]/)[0]}：[设备标识]`,
  );
  text = text.replace(
    /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi,
    "[邮箱]",
  );
  text = text.replace(/\b1[3-9]\d{9}\b/g, "[手机号]");
  text = text.replace(/\b\d{17}[\dXx]\b/g, "[证件号]");
  text = text.replace(/\b\d{12,}\b/g, "[编号]");
  text = text.replace(/https?:\/\/[^\s)\]）】]+/gi, "[链接已移除]");
  text = text.replace(
    /\b\d{1,2}:\d{2}(?::\d{2})+\b/g,
    "[时间]",
  );

  for (const name of sortedNames) {
    if (!name) {
      continue;
    }
    text = text.replace(new RegExp(escapeRegExp(name), "g"), "[人员]");
  }

  const cleanedLines = [];
  for (const rawLine of text.split("\n")) {
    let line = rawLine.trim();
    if (!line) {
      continue;
    }
    if (line === "预览" || line === "已加载全部") {
      line = "[图片]";
    }
    if (
      line === "[图片]" &&
      cleanedLines.length > 0 &&
      cleanedLines[cleanedLines.length - 1] === "[图片]"
    ) {
      continue;
    }
    cleanedLines.push(line);
  }
  return cleanedLines.join("\n").trim();
}

function parseDateOnly(value) {
  const match = String(value ?? "").match(
    /(\d{4})[/-](\d{1,2})[/-](\d{1,2})/,
  );
  if (!match) {
    return "";
  }
  return new Date(
    Number(match[1]),
    Number(match[2]) - 1,
    Number(match[3]),
  );
}

const outputColumns = [
  "序号",
  "上传者",
  "分析时间",
  "工单ID",
  "回收单号",
  "类目",
  "机型",
  "聊天内容",
  "核心问题",
  "判定结论",
  "判定依据",
  "产品类型",
  "一级分类",
  "二级分类",
  "参考话术",
  "图片链接",
  "视频链接",
  "脱敏状态",
];

const outputRows = sampledRows.map((row, index) => {
  const source = row.values;
  const sampleNumber = String(index + 1).padStart(4, "0");
  return [
    index + 1,
    "匿名用户",
    parseDateOnly(source[1]),
    `TICKET-${sampleNumber}`,
    `RECYCLE-${sampleNumber}`,
    sanitizeText(source[4]) || "手机",
    sanitizeText(source[5]),
    sanitizeText(source[6]),
    sanitizeText(source[7]),
    sanitizeText(source[8]),
    sanitizeText(source[9]),
    "手机",
    sanitizeText(source[11]),
    sanitizeText(source[12]),
    sanitizeText(source[13]),
    "",
    "",
    "已脱敏",
  ];
});

function countColumn(index) {
  const result = new Map();
  for (const row of outputRows) {
    const key = String(row[index] || "未分类");
    result.set(key, (result.get(key) || 0) + 1);
  }
  return [...result.entries()].sort(
    (left, right) =>
      right[1] - left[1] || left[0].localeCompare(right[0], "zh-CN"),
  );
}

const outputWorkbook = Workbook.create();
const sampleSheet = outputWorkbook.worksheets.add("聚类样本");
sampleSheet.showGridLines = false;
sampleSheet.getRange("A1:R101").values = [outputColumns, ...outputRows];
sampleSheet.freezePanes.freezeRows(1);
sampleSheet.getRange("A1:R1").format = {
  fill: "#155E75",
  font: { bold: true, color: "#FFFFFF", size: 10 },
  horizontalAlignment: "center",
  verticalAlignment: "center",
  wrapText: true,
};
sampleSheet.getRange("A2:R101").format = {
  font: { size: 10 },
  verticalAlignment: "top",
};
sampleSheet.getRange("H2:O101").format.wrapText = true;
sampleSheet.getRange("A2:G101").format.rowHeight = 72;
sampleSheet.getRange("H2:R101").format.rowHeight = 72;
sampleSheet.getRange("C2:C101").format.numberFormat = "yyyy-mm-dd";
sampleSheet.getRange("A1:A101").format.columnWidth = 8;
sampleSheet.getRange("B1:B101").format.columnWidth = 12;
sampleSheet.getRange("C1:C101").format.columnWidth = 13;
sampleSheet.getRange("D1:E101").format.columnWidth = 17;
sampleSheet.getRange("F1:F101").format.columnWidth = 10;
sampleSheet.getRange("G1:G101").format.columnWidth = 23;
sampleSheet.getRange("H1:H101").format.columnWidth = 55;
sampleSheet.getRange("I1:I101").format.columnWidth = 46;
sampleSheet.getRange("J1:J101").format.columnWidth = 38;
sampleSheet.getRange("K1:K101").format.columnWidth = 55;
sampleSheet.getRange("L1:M101").format.columnWidth = 16;
sampleSheet.getRange("N1:N101").format.columnWidth = 28;
sampleSheet.getRange("O1:O101").format.columnWidth = 50;
sampleSheet.getRange("P1:R101").format.columnWidth = 14;
const sampleTable = sampleSheet.tables.add(
  "A1:R101",
  true,
  "ClusterSampleTable",
);
sampleTable.style = "TableStyleMedium2";

const distributionSheet = outputWorkbook.worksheets.add("样本分布");
distributionSheet.showGridLines = false;
const level1Counts = countColumn(12);
const level2Counts = countColumn(13);
const dateCounts = new Map();
for (const row of outputRows) {
  const date = row[2] instanceof Date
    ? `${row[2].getFullYear()}-${String(row[2].getMonth() + 1).padStart(2, "0")}-${String(row[2].getDate()).padStart(2, "0")}`
    : String(row[2] || "未知日期");
  dateCounts.set(date, (dateCounts.get(date) || 0) + 1);
}

distributionSheet.getRange("A1:B1").values = [["一级分类", "样本数"]];
distributionSheet
  .getRange(`A2:B${level1Counts.length + 1}`)
  .values = level1Counts;
distributionSheet.getRange("D1:E1").values = [["二级分类", "样本数"]];
distributionSheet
  .getRange(`D2:E${level2Counts.length + 1}`)
  .values = level2Counts;
const dateRows = [...dateCounts.entries()].sort(([left], [right]) =>
  left.localeCompare(right),
);
distributionSheet.getRange("G1:H1").values = [["分析日期", "样本数"]];
distributionSheet
  .getRange(`G2:H${dateRows.length + 1}`)
  .values = dateRows;
distributionSheet.getRange("J1:K7").values = [
  ["抽样信息", "值"],
  ["原始有效记录", deduplicatedRows.length],
  ["抽样条数", SAMPLE_SIZE],
  ["抽样种子", SAMPLE_SEED],
  ["产品范围", "手机"],
  ["抽样方法", "一级分类按比例，二级分类轮询覆盖"],
  ["媒体处理", "图片与视频链接全部移除"],
];
for (const range of ["A1:B1", "D1:E1", "G1:H1", "J1:K1"]) {
  distributionSheet.getRange(range).format = {
    fill: "#155E75",
    font: { bold: true, color: "#FFFFFF" },
  };
}
distributionSheet.getRange("A:K");
distributionSheet.getRange("A1:A50").format.columnWidth = 24;
distributionSheet.getRange("B1:B50").format.columnWidth = 12;
distributionSheet.getRange("D1:D80").format.columnWidth = 42;
distributionSheet.getRange("E1:E80").format.columnWidth = 12;
distributionSheet.getRange("G1:G10").format.columnWidth = 16;
distributionSheet.getRange("H1:H10").format.columnWidth = 12;
distributionSheet.getRange("J1:J10").format.columnWidth = 20;
distributionSheet.getRange("K1:K10").format.columnWidth = 42;
distributionSheet.getRange("A1:K80").format.verticalAlignment = "top";
distributionSheet.freezePanes.freezeRows(1);

const notesSheet = outputWorkbook.worksheets.add("脱敏说明");
notesSheet.showGridLines = false;
const notes = [
  ["项目", "说明"],
  ["用途", "用于手机案例语义聚类与边界验证，不用于业务追溯。"],
  ["来源范围", "案例库 7.15 与 7.16 工作表中的手机案例。"],
  ["抽样方式", "固定随机种子 20260716；一级分类按比例分层，二级分类轮询覆盖。"],
  ["人员信息", "上传者统一替换为“匿名用户”；聊天中的已知人员名称替换为占位符。"],
  ["业务编号", "工单ID和回收单号重新生成，原始编号不保留。"],
  ["时间信息", "分析时间仅保留日期；聊天中的精确时间戳移除。"],
  ["联系方式", "手机号、邮箱和证件号替换为类型占位符。"],
  ["设备标识", "IMEI、MEID、序列号、SN、串号和长数字编号替换为占位符。"],
  ["媒体与链接", "图片链接、视频链接及正文URL全部移除；聊天中的图片预览保留为[图片]标记。"],
  ["保留字段", "保留机型、聊天语义、核心问题、判定结论、判定依据和分类，供聚类使用。"],
  ["映射关系", "输出文件不包含原始行号或脱敏前后映射表，不能由输出文件还原真实业务记录。"],
];
notesSheet.getRange(`A1:B${notes.length}`).values = notes;
notesSheet.getRange("A1:B1").format = {
  fill: "#155E75",
  font: { bold: true, color: "#FFFFFF" },
};
notesSheet.getRange(`A2:B${notes.length}`).format = {
  verticalAlignment: "top",
  wrapText: true,
};
notesSheet.getRange("A1:A20").format.columnWidth = 20;
notesSheet.getRange("B1:B20").format.columnWidth = 90;
notesSheet.getRange(`A2:B${notes.length}`).format.rowHeight = 34;
notesSheet.freezePanes.freezeRows(1);

await fs.mkdir(
  "C:\\Users\\admin\\Documents\\答疑中台知识库\\data",
  { recursive: true },
);
const outputBlob = await SpreadsheetFile.exportXlsx(outputWorkbook);
await outputBlob.save(OUTPUT_PATH);

const preview = await outputWorkbook.render({
  sheetName: "聚类样本",
  range: "A1:R10",
  scale: 1,
  format: "png",
});
await fs.writeFile(
  PREVIEW_PATH,
  new Uint8Array(await preview.arrayBuffer()),
);

console.log(
  JSON.stringify(
    {
      outputPath: OUTPUT_PATH,
      population: deduplicatedRows.length,
      sampleSize: outputRows.length,
      level1Counts: Object.fromEntries(level1Counts),
      level2Count: level2Counts.length,
      dates: Object.fromEntries(dateRows),
    },
    null,
    2,
  ),
);
