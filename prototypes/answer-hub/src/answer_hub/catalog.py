from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import json
import re

from openpyxl import load_workbook


ACTIVE_STANDARD_STATUSES = {
    "active",
    "published",
    "生效",
    "生效中",
    "已发布",
}


def _clean(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _split_keywords(value: Any) -> list[str]:
    text = _clean(value)
    if not text:
        return []
    parts = re.split(r"[,\n，;；/|、\s]+", text)
    return [part.strip() for part in parts if part.strip()]


def _pick(data: dict[str, Any], aliases: list[str]) -> str:
    for key in aliases:
        value = data.get(key)
        if value not in (None, ""):
            return _clean(value)
    return ""


def _path_categories(value: str) -> tuple[str, str]:
    parts = [
        re.sub(r"[*（(][^）)]*[）)]", "", part).strip()
        for part in re.findall(r"【([^】]+)】", value)
    ]
    parts = [part for part in parts if part]
    return (parts[0] if parts else "", parts[1] if len(parts) > 1 else "")


def is_active_standard(status: str) -> bool:
    return _clean(status).lower() in ACTIVE_STANDARD_STATUSES


@dataclass(frozen=True)
class StandardCatalogItem:
    standard_id: str
    title: str
    category_l1: str
    category_l2: str
    knowledge_type: str
    standard_path: str
    keywords: list[str]
    scope: str
    response_snippet: str
    status: str
    version: str

    def searchable_text(self) -> str:
        parts = [
            self.title,
            self.category_l1,
            self.category_l2,
            self.knowledge_type,
            self.standard_path,
            self.scope,
            self.response_snippet,
            " ".join(self.keywords),
        ]
        return " ".join(part for part in parts if part).lower()


def _normalize_row(data: dict[str, Any]) -> StandardCatalogItem:
    explicit_keywords = _split_keywords(_pick(data, ["keywords", "检索关键词", "关键词", "标签"]))
    standard_path = _pick(data, ["standard_path", "关联标准项", "关联标准路径"])
    path_keywords = [part.strip() for part in re.split(r"[【】\[\]()/\\*\-]+", standard_path) if part.strip()]
    path_l1, path_l2 = _path_categories(standard_path)
    return StandardCatalogItem(
        standard_id=_pick(data, ["standard_id", "标准ID", "ID", "id"]),
        title=_pick(data, ["title", "主标题", "标准标题", "知识标题"]),
        category_l1=_pick(data, ["category_l1", "一级分类", "分类一级", "一级类目"]) or path_l1,
        category_l2=_pick(data, ["category_l2", "二级分类", "分类二级", "二级类目"]) or path_l2,
        knowledge_type=_pick(data, ["knowledge_type", "知识分类"]),
        standard_path=standard_path,
        keywords=list(dict.fromkeys(explicit_keywords + path_keywords)),
        scope=_pick(data, ["scope", "适用范围", "适用场景"]),
        response_snippet=_pick(data, ["response_snippet", "参考话术", "话术", "回复话术", "知识内容"]),
        status=_pick(data, ["status", "状态", "生效状态"]) or "published",
        version=_pick(data, ["version", "版本", "source_version", "来源版本"]) or "v1",
    )


def _rows_from_xlsx(path: Path) -> list[dict[str, Any]]:
    workbook = load_workbook(path, data_only=True)
    sheet = workbook[workbook.sheetnames[0]]
    rows = list(sheet.iter_rows(values_only=True))
    if not rows:
        return []
    headers = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    results: list[dict[str, Any]] = []
    for row in rows[1:]:
        record: dict[str, Any] = {}
        for index, header in enumerate(headers):
            if not header:
                continue
            value = row[index] if index < len(row) else None
            record[header] = value
        if any(value not in (None, "") for value in record.values()):
            results.append(record)
    return results


def load_standard_catalog(path: str | Path | None) -> list[StandardCatalogItem]:
    if not path:
        return []
    file_path = Path(path)
    if not file_path.exists():
        raise FileNotFoundError(f"Standard catalog not found: {file_path}")

    if file_path.suffix.lower() == ".json":
        payload = json.loads(file_path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            rows = payload.get("items") or payload.get("rows") or []
        else:
            rows = payload
    else:
        rows = _rows_from_xlsx(file_path)
    return [
        item
        for item in (_normalize_row(dict(row)) for row in rows)
        if is_active_standard(item.status)
    ]
