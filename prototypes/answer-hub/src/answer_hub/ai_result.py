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
_ORDERED_ITEM_RE = re.compile(
    r"(?<![\w.])(?P<index>\d{1,2})[.、）)](?!\d)[ \t]*"
)
_CIRCLED_INDICES = {
    "①": 1,
    "②": 2,
    "③": 3,
    "④": 4,
    "⑤": 5,
    "⑥": 6,
    "⑦": 7,
    "⑧": 8,
    "⑨": 9,
    "⑩": 10,
}


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


def _ordered_items(value: Any) -> list[tuple[int, str]]:
    text = _clean_text(value)
    if not text:
        return []
    for marker, index in _CIRCLED_INDICES.items():
        text = text.replace(marker, f"\n{index}. ")
    matches = list(_ORDERED_ITEM_RE.finditer(text))
    if not matches:
        return []
    indices = [int(match.group("index")) for match in matches]
    if indices != list(range(1, len(indices) + 1)):
        return []
    items: list[tuple[int, str]] = []
    for position, match in enumerate(matches):
        end = matches[position + 1].start() if position + 1 < len(matches) else len(text)
        item = text[match.end() : end].strip()
        if not item:
            return []
        items.append((indices[position], item))
    return items


def _ordered_item_marker_count(value: Any) -> int:
    text = _clean_text(value)
    for marker, index in _CIRCLED_INDICES.items():
        text = text.replace(marker, f"\n{index}. ")
    return len(list(_ORDERED_ITEM_RE.finditer(text)))


def parse_ai_result_question_reply_pairs(value: Any) -> dict[str, Any]:
    """Preserve the second-part question/reply order as an explicit contract."""

    parsed = parse_ai_result(value)
    core_problem = _clean_text(parsed.get("核心问题"))
    reference_reply = _clean_text(parsed.get("参考话术"))
    if not core_problem or not reference_reply:
        return {
            "status": "unavailable",
            "pairs": [],
            "question_count": 1 if core_problem else 0,
            "reply_count": 1 if reference_reply else 0,
            "reason": "核心问题或参考话术为空，无法建立有序对应关系",
        }

    question_items = _ordered_items(core_problem)
    reply_items = _ordered_items(reference_reply)
    question_marker_count = _ordered_item_marker_count(core_problem)
    reply_marker_count = _ordered_item_marker_count(reference_reply)
    if question_marker_count >= 1 and not question_items:
        return {
            "status": "invalid_numbering",
            "pairs": [],
            "question_count": question_marker_count,
            "reply_count": reply_marker_count,
            "reason": "核心问题的编号不连续、重复或未从1开始",
        }
    if len(question_items) <= 1:
        question_text = question_items[0][1] if question_items else core_problem
        return {
            "status": "single",
            "pairs": [
                {
                    "question_index": 1,
                    "core_problem": question_text,
                    "reference_reply": reference_reply,
                }
            ],
            "question_count": 1,
            "reply_count": len(reply_items) or 1,
            "reason": "",
        }

    if reply_marker_count >= 1 and not reply_items:
        return {
            "status": "invalid_numbering",
            "pairs": [],
            "question_count": len(question_items),
            "reply_count": reply_marker_count,
            "reason": "多个核心问题的参考话术编号不连续、重复或未从1开始",
        }

    question_indices = [index for index, _text in question_items]
    reply_indices = [index for index, _text in reply_items]
    if (
        len(question_items) != len(reply_items)
        or question_indices != reply_indices
    ):
        return {
            "status": "count_mismatch",
            "pairs": [],
            "question_count": len(question_items),
            "reply_count": len(reply_items) or 1,
            "reason": (
                "多个核心问题与参考话术的编号或数量不一致，"
                "禁止按位置强行配对"
            ),
        }

    return {
        "status": "paired",
        "pairs": [
            {
                "question_index": question_index,
                "core_problem": question_text,
                "reference_reply": reply_text,
            }
            for (
                question_index,
                question_text,
            ), (
                _reply_index,
                reply_text,
            ) in zip(question_items, reply_items)
        ],
        "question_count": len(question_items),
        "reply_count": len(reply_items),
        "reason": "",
    }
