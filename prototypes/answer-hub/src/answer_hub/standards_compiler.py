from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable
import hashlib
import json
import re
import zipfile
import xml.etree.ElementTree as ET

from .catalog import StandardCatalogItem
from .excel_io import read_workbook_rows
from .product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    infer_product_category,
    normalize_product_scope,
    resolve_product_category,
)


MAIN_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PACKAGE_REL_NS = "http://schemas.openxmlformats.org/package/2006/relationships"

DEFAULT_ACTIVE_SHEETS = {
    "手机": (
        "SJ-HSYJBZ-2026009【5.13以后】",
        "“标准补丁”答疑",
    ),
    "手表": ("ZNSB-HSYJBZ-2026005【5.13以后】",),
    "平板": (
        "PB-HSYJBZ-2026006【5.13以后】",
        "“标准补丁”答疑",
    ),
    "耳机": ("RJ-HSYJBZ-2026004【5.13以后】",),
}

DEFAULT_SOURCE_FILENAMES = {
    "手机": "【手机】-质检标准(新回收报告) -陈朝伟专用.xlsx",
    "手表": "【手表】-质检标准（新回收报告）陈朝伟副本.xlsx",
    "平板": "【平板】.xlsx",
    "耳机": "【耳机】-质检标准（新回收报告） 陈朝伟副本.xlsx",
}

HEADER_WORDS = {
    "序号",
    "一级分类",
    "二级分类",
    "三级分类",
    "四级分类",
    "问题",
    "问题类型",
    "检测项",
    "检测标准",
    "判定标准",
    "标准",
    "备注",
    "说明",
    "答案",
    "答疑",
    "分类",
}

SKIP_ROW_WORDS = {
    "首页",
    "目录",
    "返回首页",
    "版本变更记录",
    "标准更新记录",
}

KEYWORD_STOPWORDS = {
    "这个",
    "那个",
    "需要",
    "可以",
    "是否",
    "怎么",
    "如何",
    "进行",
    "情况",
    "问题",
    "标准",
    "判定",
    "检测",
    "说明",
    "备注",
}

QC_HEADER_ALIASES = {
    "scope": {"适用类型"},
    "category_l1": {"一级类", "一级项"},
    "category_l2": {"二级项"},
    "degree": {"程度值"},
    "category_l3": {"三级值", "三级项"},
    "definition": {"标准定义"},
    "changed_definition": {"本期需改动标准定义"},
    "method": {"检测方法"},
    "remark": {"备注"},
}


def discover_default_standard_sources(
    downloads_dir: str | Path | None = None,
) -> dict[str, Path]:
    root = Path(downloads_dir) if downloads_dir else Path.home() / "Downloads"
    return {
        product_type: root / filename
        for product_type, filename in DEFAULT_SOURCE_FILENAMES.items()
    }


def load_standard_source_manifest(
    path: str | Path,
) -> tuple[dict[str, Path], dict[str, tuple[str, ...]]]:
    manifest_path = Path(path)
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    products = payload.get("products") if isinstance(payload, dict) else None
    if not isinstance(products, list) or not products:
        raise ValueError("标准源清单必须包含非空 products 数组")
    sources: dict[str, Path] = {}
    active_sheets: dict[str, tuple[str, ...]] = {}
    for item in products:
        if not isinstance(item, dict):
            raise ValueError("标准源清单中的产品项必须是 JSON 对象")
        product_type = _clean(item.get("product_type"), 80)
        source = _clean(item.get("source"), 1000)
        sheets = tuple(
            value
            for value in (_clean(raw, 200) for raw in item.get("active_sheets") or [])
            if value
        )
        if not product_type or not source or not sheets:
            raise ValueError("每个标准源必须配置 product_type、source、active_sheets")
        sources[product_type] = Path(source)
        active_sheets[product_type] = sheets
    return sources, active_sheets


def _column_index(reference: str) -> int:
    letters = re.match(r"[A-Z]+", reference.upper())
    if not letters:
        return 0
    result = 0
    for character in letters.group(0):
        result = result * 26 + ord(character) - ord("A") + 1
    return result


def _cell_coordinates(reference: str) -> tuple[int, int]:
    column = _column_index(reference)
    row_match = re.search(r"\d+", reference)
    return (int(row_match.group(0)) if row_match else 0, column)


def _shared_strings(archive: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in archive.namelist():
        return []
    root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
    namespace = {"m": MAIN_NS}
    return [
        "".join(node.text or "" for node in item.findall(".//m:t", namespace)).strip()
        for item in root.findall("m:si", namespace)
    ]


def _sheet_targets(archive: zipfile.ZipFile) -> dict[str, str]:
    namespace = {"m": MAIN_NS, "r": REL_NS}
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {
        relationship.attrib["Id"]: relationship.attrib["Target"]
        for relationship in relationships.findall(f"{{{PACKAGE_REL_NS}}}Relationship")
    }
    result: dict[str, str] = {}
    for sheet in workbook.findall("m:sheets/m:sheet", namespace):
        relationship_id = sheet.attrib.get(f"{{{REL_NS}}}id", "")
        target = targets.get(relationship_id, "")
        if not target:
            continue
        normalized = target.replace("\\", "/").lstrip("/")
        if not normalized.startswith("xl/"):
            normalized = f"xl/{normalized}"
        result[sheet.attrib.get("name", "")] = normalized
    return result


def _cell_value(
    cell: ET.Element,
    shared_strings: list[str],
    namespace: dict[str, str],
) -> str:
    cell_type = cell.attrib.get("t", "")
    if cell_type == "inlineStr":
        return "".join(
            node.text or ""
            for node in cell.findall(".//m:is/m:t", namespace)
        ).strip()
    value_node = cell.find("m:v", namespace)
    if value_node is None or value_node.text is None:
        return ""
    raw_value = value_node.text.strip()
    if cell_type == "s":
        try:
            return shared_strings[int(raw_value)].strip()
        except (ValueError, IndexError):
            return ""
    if cell_type == "b":
        return "是" if raw_value == "1" else "否"
    return raw_value


def read_raw_standard_sheet(
    path: str | Path,
    sheet_name: str,
) -> list[dict[str, Any]]:
    """Read one worksheet without loading embedded media or the whole workbook."""
    source = Path(path)
    namespace = {"m": MAIN_NS}
    with zipfile.ZipFile(source) as archive:
        targets = _sheet_targets(archive)
        if sheet_name not in targets:
            raise ValueError(f"{source.name} 中不存在工作表：{sheet_name}")
        shared_strings = _shared_strings(archive)
        root = ET.fromstring(archive.read(targets[sheet_name]))

    row_cells: dict[int, dict[int, str]] = {}
    for row in root.findall("m:sheetData/m:row", namespace):
        row_number = int(row.attrib.get("r", "0") or 0)
        values: dict[int, str] = {}
        for cell in row.findall("m:c", namespace):
            reference = cell.attrib.get("r", "")
            column_number = _column_index(reference)
            value = _cell_value(cell, shared_strings, namespace)
            if column_number and value:
                values[column_number] = value
        if values:
            row_cells[row_number] = values

    # Raw standards use vertically merged classification cells. Propagate only
    # the first column of a merged range so category context is retained without
    # duplicating large horizontal headings across every column.
    for merged in root.findall("m:mergeCells/m:mergeCell", namespace):
        reference = merged.attrib.get("ref", "")
        if ":" not in reference:
            continue
        start, end = reference.split(":", 1)
        start_row, start_col = _cell_coordinates(start)
        end_row, _end_col = _cell_coordinates(end)
        value = row_cells.get(start_row, {}).get(start_col, "")
        if not value or start_row == end_row:
            continue
        for row_number in range(start_row + 1, end_row + 1):
            row_cells.setdefault(row_number, {}).setdefault(start_col, value)

    return [
        {
            "row_number": row_number,
            "cells": dict(sorted(values.items())),
        }
        for row_number, values in sorted(row_cells.items())
    ]


def _clean(value: Any, limit: int = 4000) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _meaningful_values(cells: dict[int, str]) -> list[str]:
    values: list[str] = []
    for value in cells.values():
        cleaned = _clean(value)
        if not cleaned or cleaned in values:
            continue
        values.append(cleaned)
    return values


def _is_header_or_navigation(values: list[str]) -> bool:
    if not values:
        return True
    if len(values) == 1 and values[0] in SKIP_ROW_WORDS:
        return True
    return all(value in HEADER_WORDS for value in values)


def _keywords(values: Iterable[str], product_type: str) -> list[str]:
    candidates: list[str] = [product_type]
    for value in values:
        candidates.extend(
            token
            for token in re.findall(r"[A-Za-z][A-Za-z0-9._-]{1,30}|[\u4e00-\u9fff]{2,12}", value)
            if token not in KEYWORD_STOPWORDS
        )
    return list(dict.fromkeys(candidates))[:40]


def _category_values(values: list[str], product_type: str) -> tuple[str, str]:
    short_values = [
        value
        for value in values
        if 1 < len(value) <= 24
        and value not in HEADER_WORDS
        and not re.fullmatch(r"\d+(?:\.\d+)?", value)
    ]
    def normalize(value: str) -> str:
        return re.sub(r"[*＊]?[（(][A-Za-z0-9]+[）)]$", "", value).strip("*＊ /")

    category_l1 = normalize(short_values[0]) if short_values else product_type
    category_l2 = normalize(short_values[1]) if len(short_values) > 1 else ""
    return category_l1, category_l2


def _row_title(
    values: list[str],
    product_type: str,
    category_l1: str,
    category_l2: str,
    row_number: int,
) -> str:
    question_like = next(
        (
            value
            for value in values
            if 4 <= len(value) <= 100
            and any(marker in value for marker in ("如何", "怎么", "是否", "什么", "判定", "检测", "区分"))
        ),
        "",
    )
    if question_like:
        return question_like
    descriptive = next(
        (
            value
            for value in reversed(values)
            if 4 <= len(value) <= 100 and value not in {category_l1, category_l2}
        ),
        "",
    )
    if descriptive:
        return descriptive
    category = " / ".join(value for value in (category_l1, category_l2) if value)
    return f"{product_type}{category or '质检'}标准第{row_number}行"


def _normalize_qc_category_l1(value: str, product_type: str) -> str:
    text = _clean(value, 120)
    text = re.sub(r"^[（(]商品类型[）)]\s*", "", text)
    text = re.sub(r"[*＊]+", "", text)
    text = re.sub(r"[（(][ABＡＢ][）)]$", "", text)
    return text.strip(" /-") or product_type


def _meaningful_path_part(value: str) -> str:
    text = _clean(value, 200)
    return "" if text in {"", "/", "-", "—", "无", "不适用"} else text


def _find_qc_header(
    raw_rows: list[dict[str, Any]],
) -> tuple[int, dict[str, int]] | None:
    for raw_row in raw_rows[:30]:
        detected: dict[str, int] = {}
        for column, value in raw_row["cells"].items():
            cleaned = _clean(value, 80)
            for field, aliases in QC_HEADER_ALIASES.items():
                if cleaned in aliases:
                    detected[field] = column
        if {
            "category_l1",
            "category_l2",
            "definition",
        }.issubset(detected):
            return int(raw_row["row_number"]), detected
    return None


def _structured_qc_items(
    raw_rows: list[dict[str, Any]],
    source_path: Path,
    product_type: str,
    sheet_name: str,
) -> list[StandardCatalogItem] | None:
    header = _find_qc_header(raw_rows)
    if header is None:
        return None
    header_row, columns = header
    carried = {
        "scope": "",
        "category_l1": "",
        "category_l2": "",
        "degree": "",
        "category_l3": "",
    }
    grouped: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for raw_row in raw_rows:
        if int(raw_row["row_number"]) <= header_row:
            continue
        cells = raw_row["cells"]
        current = {
            field: _clean(cells.get(column), 4000)
            for field, column in columns.items()
        }
        for field in carried:
            if current.get(field):
                carried[field] = current[field]
            else:
                current[field] = carried[field]

        raw_l1 = current.get("category_l1", "")
        raw_l2 = current.get("category_l2", "")
        if not raw_l1 or not raw_l2:
            continue
        category_l1 = _normalize_qc_category_l1(raw_l1, product_type)
        category_l2 = _clean(raw_l2, 160)
        degree = _meaningful_path_part(current.get("degree", ""))
        category_l3 = _meaningful_path_part(current.get("category_l3", ""))
        applicable_type = _clean(current.get("scope"), 240)
        scope = (
            f"{product_type}-{applicable_type}"
            if applicable_type
            else f"{product_type}-通用"
        )
        key = (scope, category_l1, category_l2, degree, category_l3)
        group = grouped.setdefault(
            key,
            {
                "row_numbers": [],
                "definitions": [],
                "changed_definitions": [],
                "methods": [],
                "remarks": [],
            },
        )
        group["row_numbers"].append(int(raw_row["row_number"]))
        for field, target in (
            ("definition", "definitions"),
            ("changed_definition", "changed_definitions"),
            ("method", "methods"),
            ("remark", "remarks"),
        ):
            value = _clean(current.get(field), 4000)
            if value and value not in group[target]:
                group[target].append(value)

    items: list[StandardCatalogItem] = []
    for key, group in grouped.items():
        scope, category_l1, category_l2, degree, category_l3 = key
        path_parts = [
            product_type,
            category_l1,
            category_l2,
            degree,
            category_l3,
        ]
        standard_path = "-".join(
            f"【{part}】"
            for part in path_parts
            if _meaningful_path_part(part)
        )
        title = category_l3 or degree or category_l2
        sections: list[str] = []
        for label, values in (
            ("标准定义", group["definitions"]),
            ("本期改动", group["changed_definitions"]),
            ("检测方法", group["methods"]),
            ("备注", group["remarks"]),
        ):
            if values:
                sections.append(f"{label}：" + "\n".join(values))
        response_snippet = "\n".join(sections)[:4000]
        digest = hashlib.sha1(
            f"{product_type}|{sheet_name}|{scope}|{standard_path}".encode("utf-8")
        ).hexdigest()[:12].upper()
        keyword_values = [
            category_l1,
            category_l2,
            degree,
            category_l3,
            applicable_type,
            response_snippet,
        ]
        items.append(
            StandardCatalogItem(
                standard_id=f"QC-{digest}",
                title=title,
                category_l1=category_l1,
                category_l2=category_l2,
                knowledge_type="质检标准",
                standard_path=standard_path,
                keywords=_keywords(keyword_values, product_type),
                scope=scope,
                response_snippet=response_snippet,
                status="published",
                version=sheet_name,
            )
        )
    return items


def _compile_sheet_items(
    source_path: Path,
    product_type: str,
    sheet_name: str,
) -> list[StandardCatalogItem]:
    raw_rows = read_raw_standard_sheet(source_path, sheet_name)
    structured_items = _structured_qc_items(
        raw_rows,
        source_path,
        product_type,
        sheet_name,
    )
    if structured_items is not None:
        return structured_items

    items: list[StandardCatalogItem] = []
    for raw_row in raw_rows:
        row_number = int(raw_row["row_number"])
        values = _meaningful_values(raw_row["cells"])
        if _is_header_or_navigation(values):
            continue
        combined = "；".join(values)
        if len(combined) < 4:
            continue
        category_l1, category_l2 = _category_values(values, product_type)
        title = _row_title(values, product_type, category_l1, category_l2, row_number)
        digest = hashlib.sha1(
            f"{product_type}|{sheet_name}|{row_number}|{combined}".encode("utf-8")
        ).hexdigest()[:12].upper()
        items.append(
            StandardCatalogItem(
                standard_id=f"RAW-{digest}",
                title=title,
                category_l1=category_l1,
                category_l2=category_l2,
                knowledge_type="质检标准",
                standard_path=f"【{product_type}】-【{sheet_name}】-【第{row_number}行】",
                keywords=_keywords(values, product_type),
                scope=f"{product_type}-通用",
                response_snippet=combined[:4000],
                status="published",
                version=sheet_name,
            )
        )
    return items


def _normalize_existing_scope(product_type: str, scope: str) -> str:
    text = _clean(scope, 200)
    normalized = normalize_product_scope(product_type, text)
    if text and normalized != f"{product_type}-通用":
        return normalized
    lowered = text.lower()
    if any(marker in lowered for marker in ("苹果", "iphone", "ipad", "ios", "watchos")):
        return normalize_product_scope(product_type, "苹果")
    if any(marker in lowered for marker in ("安卓", "android", "鸿蒙", "harmony")):
        return normalize_product_scope(product_type, "安卓")
    return normalize_product_scope(product_type)


def _existing_knowledge_items(path: Path) -> list[StandardCatalogItem]:
    _columns, rows = read_workbook_rows(path)
    items: list[StandardCatalogItem] = []
    for index, row in enumerate(rows, start=1):
        title = _clean(row.get("主标题"), 160)
        content = _clean(row.get("知识内容"), 4000)
        if not title or not content:
            continue
        scope = _clean(row.get("适用范围"), 200)
        product_category = infer_product_category(
            (
                scope,
                title,
                _clean(row.get("知识分类"), 200),
            )
        )
        product_type = product_category.name if product_category else UNKNOWN_PRODUCT_NAME
        digest = hashlib.sha1(
            f"{title}|{content}|{scope}".encode("utf-8")
        ).hexdigest()[:12].upper()
        items.append(
            StandardCatalogItem(
                standard_id=f"KNOWLEDGE-{digest}",
                title=title,
                category_l1=_clean(row.get("知识分类"), 80),
                category_l2="",
                knowledge_type="已有知识",
                standard_path=_clean(row.get("关联标准项"), 500) or f"【已有知识】-【{index}】",
                keywords=_keywords(
                    [
                        title,
                        _clean(row.get("副标题"), 300),
                        _clean(row.get("检索关键词"), 1000),
                        content,
                    ],
                    product_type,
                ),
                scope=_normalize_existing_scope(product_type, scope),
                response_snippet=content,
                status="published",
                version=_clean(row.get("来源版本"), 120) or "existing-knowledge",
            )
        )
    return items


def compile_standard_catalog(
    sources: dict[str, str | Path],
    output_path: str | Path,
    *,
    active_sheets: dict[str, tuple[str, ...]] | None = None,
    existing_knowledge_path: str | Path | None = None,
) -> dict[str, Any]:
    selected_sheets = DEFAULT_ACTIVE_SHEETS if active_sheets is None else active_sheets
    all_items: list[StandardCatalogItem] = []
    source_summary: list[dict[str, Any]] = []
    normalized_sources: dict[str, Path] = {}
    for source_product_type, source_path in sources.items():
        category = resolve_product_category(source_product_type)
        if category is None:
            raise ValueError(f"产品类型未在品类配置中定义：{source_product_type}")
        if category.name in normalized_sources:
            raise ValueError(f"产品类型重复配置标准源：{category.name}")
        normalized_sources[category.name] = Path(source_path)

    for product_type, source in normalized_sources.items():
        if not source.is_file():
            raise FileNotFoundError(f"{product_type}质检标准不存在：{source}")
        sheet_names = tuple(selected_sheets.get(product_type, ()))
        if not sheet_names:
            raise ValueError(
                f"{product_type}未配置生效工作表；请在标准源清单中提供 active_sheets"
            )
        product_items: list[StandardCatalogItem] = []
        for sheet_name in sheet_names:
            product_items.extend(_compile_sheet_items(source, product_type, sheet_name))
        all_items.extend(product_items)
        source_summary.append(
            {
                "product_type": product_type,
                "source_file": str(source),
                "sheets": list(sheet_names),
                "items": len(product_items),
            }
        )

    existing_items: list[StandardCatalogItem] = []
    if existing_knowledge_path:
        existing_path = Path(existing_knowledge_path)
        if existing_path.is_file():
            existing_items = _existing_knowledge_items(existing_path)
            all_items = [*existing_items, *all_items]

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": datetime.now().astimezone().strftime("%Y%m%d-%H%M%S"),
        "compiled_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "existing_knowledge_items": len(existing_items),
        "standard_items": len(all_items) - len(existing_items),
        "total_items": len(all_items),
        "sources": source_summary,
        "items": [asdict(item) for item in all_items],
    }
    temporary = output.with_suffix(f"{output.suffix}.tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(output)
    return {
        "output_file": str(output),
        "existing_knowledge_items": len(existing_items),
        "standard_items": len(all_items) - len(existing_items),
        "total_items": len(all_items),
        "sources": source_summary,
    }
