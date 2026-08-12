from __future__ import annotations

import json
import re
from typing import Any


AI_RESULT_FIELDS = (
    "核心问题",
    "产品类型",
    "一级分类",
    "二级分类",
    "判定结论",
    "判定依据",
    "参考话术",
)

_FIELD_ALIASES = {
    "核心问题": "核心问题",
    "core_problem": "核心问题",
    "source_core_problem": "核心问题",
    "产品类型": "产品类型",
    "product_type": "产品类型",
    "product_category": "产品类型",
    "一级分类": "一级分类",
    "category_l1": "一级分类",
    "二级分类": "二级分类",
    "category_l2": "二级分类",
    "判定结论": "判定结论",
    "judgment": "判定结论",
    "judgment_conclusion": "判定结论",
    "判定依据": "判定依据",
    "judgment_basis": "判定依据",
    "参考话术": "参考话术",
    "reference_reply": "参考话术",
    "recommended_reply": "参考话术",
}

_HEADER_RE = re.compile(
    r"(?m)^[ \t]*(?:【(?P<cn>[^】\r\n]+)】|\[(?P<bracket>[^\]\r\n]+)\])[ \t]*"
)


def _clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"[ \t]+", " ", str(value).replace("\u3000", " ")).strip()


def _canonical_field(value: Any) -> str:
    text = _clean_text(value).strip(" ：:;；")
    return _FIELD_ALIASES.get(text, _FIELD_ALIASES.get(text.casefold(), ""))


def _append_value(target: dict[str, str], field: str, value: Any) -> None:
    cleaned = _clean_text(value).lstrip(" ：:;；")
    if not field or not cleaned:
        return
    existing = target.get(field, "")
    if not existing:
        target[field] = cleaned
        return
    if cleaned not in existing.split("\n\n"):
        target[field] = f"{existing}\n\n{cleaned}"


def _parse_mapping(value: dict[str, Any]) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for key, item in value.items():
        field = _canonical_field(key)
        if field:
            if isinstance(item, list):
                item = "\n".join(_clean_text(part) for part in item if _clean_text(part))
            elif isinstance(item, dict):
                item = json.dumps(item, ensure_ascii=False, sort_keys=True)
            _append_value(parsed, field, item)
    return parsed


def parse_ai_result(value: Any) -> dict[str, str]:
    """Extract known structured evidence from the second-part ai_result field."""
    if isinstance(value, dict):
        return _parse_mapping(value)
    text = _clean_text(value)
    if not text:
        return {}
    if text.startswith("{") and text.endswith("}"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            return _parse_mapping(payload)

    matches = list(_HEADER_RE.finditer(text))
    if not matches:
        return {}
    parsed: dict[str, str] = {}
    for index, match in enumerate(matches):
        field = _canonical_field(match.group("cn") or match.group("bracket"))
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        _append_value(parsed, field, text[match.end() : end])
    return parsed
