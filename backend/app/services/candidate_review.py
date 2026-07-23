from __future__ import annotations

from typing import Any


WORTHY_VALUES = {"worthy", "是", "值得沉淀", "值得", "yes", "true", "1"}
UNWORTHY_VALUES = {"unworthy", "否", "不值得沉淀", "不值得", "no", "false", "0"}
USABLE_VALUES = {"usable", "是", "可用", "通过", "yes", "true", "1"}
UNUSABLE_VALUES = {"unusable", "否", "不可用", "驳回", "no", "false", "0"}
PASS_DECISIONS = {"approved", "approved_with_changes", "通过", "修改后通过"}
REJECT_DECISIONS = {"rejected", "bad_case", "驳回", "标记Bad Case"}


def _text(value: Any) -> str:
    return str(value or "").strip()


def normalize_knowledge_value(value: Any) -> str:
    normalized = _text(value).lower()
    if normalized in WORTHY_VALUES:
        return "worthy"
    if normalized in UNWORTHY_VALUES:
        return "unworthy"
    return "pending"


def normalize_usability(value: Any) -> str:
    normalized = _text(value).lower()
    if normalized in USABLE_VALUES:
        return "usable"
    if normalized in UNUSABLE_VALUES:
        return "unusable"
    return "pending"


def normalize_decision(value: Any) -> str:
    normalized = _text(value)
    if normalized in PASS_DECISIONS:
        return "approved_with_changes" if normalized in {"approved_with_changes", "修改后通过"} else "approved"
    if normalized in REJECT_DECISIONS:
        return "bad_case" if normalized in {"bad_case", "标记Bad Case"} else "rejected"
    return ""


def normalize_human_review(review: dict[str, Any] | None) -> dict[str, Any]:
    source = dict(review or {})
    return {
        **source,
        "knowledge_value": normalize_knowledge_value(source.get("knowledge_value")),
        "usability": normalize_usability(source.get("usability")),
        "decision": normalize_decision(source.get("decision")),
        "modification_notes": _text(source.get("modification_notes")),
        "feedback": _text(source.get("feedback")),
        "error_type": _text(source.get("error_type")),
        "training_eligible": _text(source.get("training_eligible")),
        "notes": _text(source.get("notes")),
    }


def evaluate_review_status(
    selection: dict[str, Any] | None,
    human_review: dict[str, Any] | None,
) -> tuple[str, bool, str]:
    selection = dict(selection or {})
    review = normalize_human_review(human_review)
    knowledge_value = review["knowledge_value"]
    usability = review["usability"]
    decision = review["decision"]

    if knowledge_value == "unworthy":
        return "rejected", False, "人工确认该知识点不值得沉淀"
    if decision in {"rejected", "bad_case"}:
        return "rejected", False, "人工审核结论为驳回"
    if usability == "unusable":
        return "rejected", False, "人工确认候选内容不可用"
    if knowledge_value == "worthy" and (
        usability == "usable" or decision in {"approved", "approved_with_changes"}
    ):
        return "ready", True, "已完成人工验证，可提交发布审核"
    if bool(selection.get("eligible")):
        return "ready", True, "上游模型或人工门禁已通过"
    return "pending", False, "等待人工确认沉淀价值和可用性"
