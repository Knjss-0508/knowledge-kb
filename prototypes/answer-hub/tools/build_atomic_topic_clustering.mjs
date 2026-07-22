import fs from "node:fs/promises";
import path from "node:path";
import {
  SpreadsheetFile,
  Workbook,
} from "@oai/artifact-tool";


function text(value) {
  return String(value ?? "").trim();
}


function columnName(columnNumber) {
  let value = columnNumber;
  let result = "";
  while (value > 0) {
    const remainder = (value - 1) % 26;
    result = String.fromCharCode(65 + remainder) + result;
    value = Math.floor((value - 1) / 26);
  }
  return result;
}


function applyHeaderStyle(range) {
  range.format = {
    fill: "#176B87",
    font: {
      name: "Microsoft YaHei",
      size: 10,
      bold: true,
      color: "#FFFFFF",
    },
    verticalAlignment: "center",
    horizontalAlignment: "center",
    wrapText: true,
    borders: {
      preset: "inside",
      style: "thin",
      color: "#D7E3E8",
    },
  };
  range.format.rowHeight = 34;
}


function applyBodyStyle(range, rowHeight = 56) {
  range.format = {
    font: {
      name: "Microsoft YaHei",
      size: 10,
      color: "#1F2937",
    },
    verticalAlignment: "top",
    horizontalAlignment: "left",
    wrapText: true,
    borders: {
      insideHorizontal: {
        style: "thin",
        color: "#E5E7EB",
      },
    },
  };
  range.format.rowHeight = rowHeight;
}


function writeTableSheet({
  workbook,
  name,
  headers,
  rows,
  widths,
  tableName,
  rowHeight = 56,
}) {
  const sheet = workbook.worksheets.getItem(name);
  sheet.showGridLines = false;
  const lastColumn = columnName(headers.length);
  sheet.getRange(`A1:${lastColumn}1`).values = [headers];
  applyHeaderStyle(sheet.getRange(`A1:${lastColumn}1`));
  if (rows.length) {
    sheet.getRange(`A2:${lastColumn}${rows.length + 1}`).values = rows;
    applyBodyStyle(
      sheet.getRange(`A2:${lastColumn}${rows.length + 1}`),
      rowHeight,
    );
    const table = sheet.tables.add(
      `A1:${lastColumn}${rows.length + 1}`,
      true,
      tableName,
    );
    table.style = "TableStyleMedium2";
    table.showBandedColumns = false;
    table.showFilterButton = true;
  }
  widths.forEach((width, index) => {
    sheet
      .getRange(`${columnName(index + 1)}:${columnName(index + 1)}`)
      .format.columnWidth = width;
  });
  sheet.freezePanes.freezeRows(1);
  return sheet;
}


function addDecisionFormatting(range) {
  range.conditionalFormats.add("containsText", {
    text: "共用一条知识",
    format: {
      fill: "#DCFCE7",
      font: { color: "#166534", bold: true },
    },
  });
  range.conditionalFormats.add("containsText", {
    text: "需要拆分",
    format: {
      fill: "#FEE2E2",
      font: { color: "#991B1B", bold: true },
    },
  });
  range.conditionalFormats.add("containsText", {
    text: "不确定",
    format: {
      fill: "#FEF3C7",
      font: { color: "#92400E", bold: true },
    },
  });
}


function buildWorkbook(results) {
  if (results.metadata?.status !== "complete") {
    throw new Error(
      `聚类结果尚未完成，剩余 ${results.metadata?.pending_bucket_count ?? "未知"} 个模型分桶。`,
    );
  }

  const workbook = Workbook.create();
  for (const sheetName of [
    "主题簇总览",
    "主题簇成员",
    "多主题拆分请求",
    "待人工确认",
    "人工簇审核",
  ]) {
    workbook.worksheets.add(sheetName);
  }
  const units = results.atomic_units ?? [];
  const clusters = results.clusters ?? [];
  const splitRequests = results.split_requests ?? [];
  const reviewRequests = results.review_requests ?? [];
  const unitById = new Map(units.map((unit) => [text(unit.unit_id), unit]));
  const clusterByAtomicId = new Map();
  for (const cluster of clusters) {
    for (const atomicId of cluster.member_atomic_ids ?? []) {
      clusterByAtomicId.set(text(atomicId), cluster);
    }
  }

  const overview = workbook.worksheets.getItem("主题簇总览");
  overview.showGridLines = false;
  overview.mergeCells("A1:N1");
  overview.getRange("A1").values = [[
    "新版原子知识主题聚类结果（唯一标准：簇内成员可共用同一条标准答疑知识）",
  ]];
  overview.getRange("A1:N1").format = {
    fill: "#0F4C5C",
    font: {
      name: "Microsoft YaHei",
      size: 15,
      bold: true,
      color: "#FFFFFF",
    },
    horizontalAlignment: "left",
    verticalAlignment: "center",
  };
  overview.getRange("A1:N1").format.rowHeight = 34;
  overview.getRange("A2:N3").format = {
    font: {
      name: "Microsoft YaHei",
      size: 10,
    },
    verticalAlignment: "center",
  };
  overview.getRange("A2:M2").values = [[
    "已覆盖原子知识点",
    "",
    "主题簇数",
    "",
    "多成员簇数",
    "",
    "待人工确认",
    "",
    "多主题拆分",
    "",
    "完整性检查",
    "",
    "输入原子知识点",
  ]];
  overview.getRange("A2:M2").format = {
    fill: "#E8F3F5",
    font: {
      name: "Microsoft YaHei",
      size: 10,
      bold: true,
      color: "#0F4C5C",
    },
    verticalAlignment: "center",
    horizontalAlignment: "center",
  };
  const memberLastRow = Math.max(2, clusters.reduce(
    (count, cluster) => count + Number(cluster.member_count ?? 0),
    0,
  ) + 1);
  const reviewLastRow = Math.max(2, reviewRequests.length + 1);
  const splitLastRow = Math.max(2, splitRequests.length + 1);
  const overviewLastRow = Math.max(6, clusters.length + 5);
  overview.getRange("B3").formulas = [[
    `=COUNTA('主题簇成员'!C2:C${memberLastRow})+COUNTA('待人工确认'!A2:A${reviewLastRow})+COUNTA('多主题拆分请求'!A2:A${splitLastRow})`,
  ]];
  overview.getRange("D3").formulas = [[`=COUNTA(A6:A${overviewLastRow})`]];
  overview.getRange("F3").formulas = [[`=COUNTIF(C6:C${overviewLastRow},">1")`]];
  overview.getRange("H3").formulas = [[`=COUNTA('待人工确认'!A2:A${reviewLastRow})`]];
  overview.getRange("J3").formulas = [[`=COUNTA('多主题拆分请求'!A2:A${splitLastRow})`]];
  overview.getRange("L3").formulas = [[
    `=IF(B3=M3,"通过","异常")`,
  ]];
  overview.getRange("M3").values = [[Number(results.metadata.atomic_unit_count ?? units.length)]];
  overview.getRange("A3:M3").format = {
    fill: "#F8FAFC",
    font: {
      name: "Microsoft YaHei",
      size: 12,
      bold: true,
      color: "#111827",
    },
    verticalAlignment: "center",
    horizontalAlignment: "center",
    borders: {
      bottom: {
        style: "thin",
        color: "#CBD5E1",
      },
    },
  };
  overview.getRange("A3:M3").format.rowHeight = 30;
  overview.getRange("L3").conditionalFormats.add("containsText", {
    text: "通过",
    format: {
      fill: "#DCFCE7",
      font: { color: "#166534", bold: true },
    },
  });
  overview.getRange("L3").conditionalFormats.add("containsText", {
    text: "异常",
    format: {
      fill: "#FEE2E2",
      font: { color: "#991B1B", bold: true },
    },
  });

  const overviewHeaders = [
    "主题簇ID",
    "主题名称",
    "成员数",
    "成员原子ID",
    "产品品类",
    "适用范围类型",
    "平台",
    "品牌",
    "机型范围",
    "知识一级分类",
    "问题意图",
    "共享知识定义",
    "系统合并依据",
    "五项一致性检查",
  ];
  const overviewRows = clusters.map((cluster) => [
    cluster.cluster_id,
    cluster.theme_name,
    Number(cluster.member_count ?? cluster.member_atomic_ids?.length ?? 0),
    (cluster.member_atomic_ids ?? []).join("\n"),
    cluster.product_category,
    cluster.scope_type,
    cluster.platform,
    cluster.brand,
    cluster.model_scope,
    cluster.category_l1,
    cluster.intent,
    cluster.shared_knowledge_definition,
    cluster.merge_basis,
    [
      cluster.scope_consistent,
      cluster.object_consistent,
      cluster.judgment_target_consistent,
      cluster.standard_path_consistent,
      cluster.threshold_exception_consistent,
    ].every(Boolean)
      ? "全部通过"
      : "异常",
  ]);
  overview.getRange("A5:N5").values = [overviewHeaders];
  applyHeaderStyle(overview.getRange("A5:N5"));
  if (overviewRows.length) {
    overview.getRange(`A6:N${overviewRows.length + 5}`).values = overviewRows;
    applyBodyStyle(overview.getRange(`A6:N${overviewRows.length + 5}`), 64);
    const overviewTable = overview.tables.add(
      `A5:N${overviewRows.length + 5}`,
      true,
      "AtomicTopicOverview",
    );
    overviewTable.style = "TableStyleMedium2";
  }
  [
    12, 28, 9, 18, 12, 15, 12, 12, 16, 16, 13, 38, 42, 16,
  ].forEach((width, index) => {
    overview
      .getRange(`${columnName(index + 1)}:${columnName(index + 1)}`)
      .format.columnWidth = width;
  });
  overview.freezePanes.freezeRows(5);
  overview.freezePanes.freezeColumns(2);

  const memberHeaders = [
    "主题簇ID",
    "主题名称",
    "原子知识ID",
    "样本ID",
    "规范化问题",
    "产品品类",
    "适用范围类型",
    "平台",
    "品牌",
    "机型范围",
    "知识一级分类",
    "知识二级分类",
    "问题意图",
    "核心对象",
    "异常现象/查询目标",
    "判定目标",
    "处理方式",
    "标准处理路径",
    "阈值/例外条件",
    "证据摘要",
  ];
  const memberRows = [];
  for (const cluster of clusters) {
    for (const atomicId of cluster.member_atomic_ids ?? []) {
      const unit = unitById.get(text(atomicId)) ?? {};
      memberRows.push([
        cluster.cluster_id,
        cluster.theme_name,
        atomicId,
        unit.sample_id,
        unit.normalized_issue,
        unit.product_category,
        unit.scope_type,
        unit.platform,
        unit.brand,
        unit.model_scope,
        unit.category_l1,
        unit.category_l2,
        unit.intent,
        unit.subject,
        unit.phenomenon,
        unit.judgment_target,
        unit.resolution_mode,
        unit.standard_path,
        unit.threshold_or_exception,
        unit.evidence_summary,
      ]);
    }
  }
  writeTableSheet({
    workbook,
    name: "主题簇成员",
    headers: memberHeaders,
    rows: memberRows,
    widths: [
      12, 28, 16, 11, 38, 12, 15, 12, 12, 16, 16, 18, 13, 20, 24, 28,
      28, 38, 30, 46,
    ],
    tableName: "AtomicTopicMembers",
    rowHeight: 72,
  });

  const splitRows = splitRequests.map((request) => {
    const unit = unitById.get(text(request.atomic_id)) ?? {};
    return [
      request.atomic_id,
      unit.sample_id,
      unit.normalized_issue,
      unit.product_category,
      unit.category_l1,
      unit.subject,
      unit.judgment_target,
      unit.standard_path,
      request.reason,
      (request.suggested_splits ?? []).join("\n"),
      "",
      "",
      "",
    ];
  });
  const splitSheet = writeTableSheet({
    workbook,
    name: "多主题拆分请求",
    headers: [
      "原子知识ID",
      "样本ID",
      "规范化问题",
      "产品品类",
      "知识一级分类",
      "核心对象",
      "判定目标",
      "标准处理路径",
      "系统拆分原因",
      "建议拆分方向",
      "人工处理结论",
      "人工说明",
      "标注人",
    ],
    rows: splitRows,
    widths: [16, 11, 38, 12, 16, 20, 28, 38, 38, 34, 18, 36, 14],
    tableName: "AtomicSplitRequests",
    rowHeight: 72,
  });
  if (splitRows.length) {
    const decisionRange = splitSheet.getRange(`K2:K${splitRows.length + 1}`);
    decisionRange.dataValidation = {
      rule: {
        type: "list",
        values: ["确认需拆分", "原子知识无需拆分", "不确定"],
      },
    };
    addDecisionFormatting(decisionRange);
  }

  const reviewRows = reviewRequests.map((request) => {
    const unit = unitById.get(text(request.atomic_id)) ?? {};
    return [
      request.atomic_id,
      unit.sample_id,
      unit.normalized_issue,
      unit.product_category,
      unit.scope_type,
      unit.platform,
      unit.brand,
      unit.model_scope,
      unit.category_l1,
      unit.subject,
      unit.judgment_target,
      unit.standard_path,
      unit.threshold_or_exception,
      request.review_type,
      request.reason,
      "",
      "",
      "",
    ];
  });
  const reviewSheet = writeTableSheet({
    workbook,
    name: "待人工确认",
    headers: [
      "原子知识ID",
      "样本ID",
      "规范化问题",
      "产品品类",
      "适用范围类型",
      "平台",
      "品牌",
      "机型范围",
      "知识一级分类",
      "核心对象",
      "判定目标",
      "标准处理路径",
      "阈值/例外条件",
      "待确认类型",
      "系统原因",
      "人工处理结论",
      "人工说明",
      "标注人",
    ],
    rows: reviewRows,
    widths: [
      16, 11, 38, 12, 15, 12, 12, 16, 16, 20, 28, 38, 30, 18, 38, 20,
      36, 14,
    ],
    tableName: "AtomicReviewRequests",
    rowHeight: 72,
  });
  if (reviewRows.length) {
    const decisionRange = reviewSheet.getRange(`P2:P${reviewRows.length + 1}`);
    decisionRange.dataValidation = {
      rule: {
        type: "list",
        values: ["补充字段后参与聚类", "保持单知识点簇", "需要重新拆分", "不确定"],
      },
    };
    addDecisionFormatting(decisionRange);
  }

  const auditRows = clusters.map((cluster) => {
    const memberIssues = (cluster.member_atomic_ids ?? [])
      .map((atomicId) => {
        const unit = unitById.get(text(atomicId)) ?? {};
        return `${atomicId}：${text(unit.normalized_issue)}`;
      })
      .join("\n");
    return [
      cluster.cluster_id,
      cluster.theme_name,
      Number(cluster.member_count ?? 0),
      (cluster.member_atomic_ids ?? []).join("\n"),
      memberIssues,
      cluster.shared_knowledge_definition,
      cluster.merge_basis,
      "",
      "",
      "",
      "",
      "",
    ];
  });
  const auditSheet = writeTableSheet({
    workbook,
    name: "人工簇审核",
    headers: [
      "主题簇ID",
      "主题名称",
      "成员数",
      "成员原子ID",
      "成员问题摘要",
      "共享知识定义",
      "系统合并依据",
      "人工审核结论",
      "拆分/合并说明",
      "目标主题簇ID",
      "标注人",
      "标注时间",
    ],
    rows: auditRows,
    widths: [12, 28, 9, 18, 52, 42, 42, 24, 42, 16, 14, 20],
    tableName: "AtomicClusterAudit",
    rowHeight: 84,
  });
  if (auditRows.length) {
    const decisionRange = auditSheet.getRange(`H2:H${auditRows.length + 1}`);
    decisionRange.dataValidation = {
      rule: {
        type: "list",
        values: [
          "整个簇可共用一条知识",
          "需要拆分",
          "应与其他簇合并",
          "不确定",
        ],
      },
    };
    addDecisionFormatting(decisionRange);
  }

  return workbook;
}


async function saveWorkbook({ workbook, outputPath, previewDir }) {
  await fs.mkdir(path.dirname(outputPath), { recursive: true });
  await fs.mkdir(previewDir, { recursive: true });

  const overviewInspection = await workbook.inspect({
    kind: "table",
    range: "主题簇总览!A1:N15",
    include: "values,formulas",
    tableMaxRows: 15,
    tableMaxCols: 14,
    maxChars: 5000,
  });
  console.log(overviewInspection.ndjson);
  const errors = await workbook.inspect({
    kind: "match",
    searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
    options: { useRegex: true, maxResults: 100 },
    summary: "final formula error scan",
  });
  console.log(errors.ndjson);

  for (const sheetName of [
    "主题簇总览",
    "主题簇成员",
    "多主题拆分请求",
    "待人工确认",
    "人工簇审核",
  ]) {
    const preview = await workbook.render({
      sheetName,
      autoCrop: "all",
      scale: 1,
      format: "png",
    });
    await fs.writeFile(
      path.join(previewDir, `${sheetName}.png`),
      new Uint8Array(await preview.arrayBuffer()),
    );
  }

  const exported = await SpreadsheetFile.exportXlsx(workbook);
  await exported.save(outputPath);
}


async function main() {
  const resultsJsonPath = process.argv[2];
  const outputPath = process.argv[3];
  const previewDir = process.argv[4]
    || path.join(path.dirname(outputPath || "."), "atomic_topic_previews");
  if (!resultsJsonPath || !outputPath) {
    throw new Error(
      "用法：node tools/build_atomic_topic_clustering.mjs <聚类结果JSON> <输出XLSX> [预览目录]",
    );
  }
  const results = JSON.parse(await fs.readFile(resultsJsonPath, "utf8"));
  const workbook = buildWorkbook(results);
  await saveWorkbook({ workbook, outputPath, previewDir });
  console.log(JSON.stringify({ outputPath, previewDir }, null, 2));
}


await main();
