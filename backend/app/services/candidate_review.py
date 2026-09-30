from __future__ import annotations

from typing import Any


WORTHY_VALUES = {"worthy", "是", "值得沉淀", "值得", "yes", "true", "1"}
UNWORTHY_VALUES = {"unworthy", "否", "不值得沉淀", "不值得", "no", "false", "0"}
USABLE_VALUES = {"usable", "是", "可用", "通过", "yes", "true", "1"}
UNUSABLE_VALUES = {"unusable", "否", "不可用", "驳回", "no", "false", "0"}
PASS_DECISIONS = {"approved", "approved_with_changes", "通过", "修改后通过"}
REJECT_DECISIONS = {"rejected", "bad_case", "驳回", "标记Bad Case"}
DRAFT_APPROVED_VALUES = {"approved", "合格", "通过", "可送审"}
DRAFT_REVISION_VALUES = {
    "revision_required", "revise", "需修改", "退回转写", "退回修改"
}
DRAFT_HOLD_VALUES = {"hold_for_evidence", "hold", "待补证据", "待确认"}


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


def model_draft_disposition(model_review: dict[str, Any] | None) -> str:
    """Map model-produced draft quality to the candidate queue state."""
    model = dict(model_review or {})
    action = _text(model.get("suggested_action"))
    if action == "submit_for_human_review":
        return "approved"
    if action == "return_for_revision":
        return "revision_required"
    if action == "hold_for_evidence":
        return "hold_for_evidence"
    quality = _text(model.get("draft_quality")).lower()
    if quality in DRAFT_APPROVED_VALUES:
        return "approved"
    if quality in DRAFT_REVISION_VALUES:
        return "revision_required"
    if quality in DRAFT_HOLD_VALUES:
        return "hold_for_evidence"
    return "pending"


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


def build_quick_human_review(
    knowledge_value: Any,
    *,
    include_in_training: bool,
    notes: Any = "",
) -> dict[str, Any]:
    """Map one human value decision to the legacy review fields.

    The candidate review UI intentionally asks for one decision only.  The
    older usability and decision fields remain populated for compatibility
    with the existing review gate and historical exports.
    """
    normalized = normalize_knowledge_value(knowledge_value)
    if normalized == "worthy":
        usability = "usable"
        decision = "approved"
    elif normalized == "unworthy":
        usability = "unusable"
        decision = "rejected"
    else:
        usability = "pending"
        decision = ""
    return {
        "knowledge_value": normalized,
        "usability": usability,
        "decision": decision,
        "training_eligible": "是" if include_in_training else "否",
        "notes": _text(notes),
    }


def evaluate_review_status(
    selection: dict[str, Any] | None,
    human_review: dict[str, Any] | None,
    model_review: dict[str, Any] | None = None,
) -> tuple[str, bool, str]:
    selection = dict(selection or {})
    review = normalize_human_review(human_review)
    knowledge_value = review["knowledge_value"]
    usability = review["usability"]
    decision = review["decision"]
    draft_disposition = model_draft_disposition(model_review)

    if knowledge_value == "unworthy":
        return "rejected", False, "人工确认该知识点不值得沉淀"
    if decision in {"rejected", "bad_case"}:
        return "rejected", False, "人工审核结论为驳回"
    if usability == "unusable":
        return "rejected", False, "人工确认候选内容不可用"
    if draft_disposition == "revision_required":
        return "revision_required", False, "模型判断知识草稿需退回转写修改"
    if draft_disposition == "hold_for_evidence":
        return "pending", False, "模型判断需要补充证据或业务确认后再复核知识草稿"
    if knowledge_value == "worthy" and (
        usability == "usable" or decision in {"approved", "approved_with_changes"}
    ):
        return "ready", True, "人工确认值得沉淀，可提交发布审核"
    if bool(selection.get("eligible")):
        return "ready", True, "上游模型或人工门禁已通过"
    return "pending", False, "等待人工确认沉淀价值和可用性"
