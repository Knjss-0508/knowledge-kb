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
SAFE_RISK_VALUES = {
    "",
    "none",
    "no_error",
    "no_risk",
    "no_issue",
    "none_detected",
    "unknown",
    "not_applicable",
    "n_a",
    "normal",
    "safe",
    "无",
    "无风险",
    "无错误",
    "无明确错误",
    "未识别",
    "未发现",
    "不适用",
    "未知",
    "正常",
}
EVIDENCE_RISK_VALUES = {
    "insufficient",
    "inadequate",
    "missing",
    "weak",
    "low",
    "not_sufficient",
    "insufficient_evidence",
    "证据不足",
    "不足",
    "不充分",
    "不充足",
    "缺失",
    "待补充",
    "缺证据",
    "未提供",
}
EVIDENCE_OK_MARKERS = {
    "sufficient",
    "adequate",
    "verified",
    "complete",
    "充分",
    "充足",
    "已核实",
    "完整",
}
EVIDENCE_RISK_MARKERS = (
    "insufficient",
    "inadequate",
    "not_sufficient",
    "not_enough",
    "missing",
    "lack",
    "weak",
    "low",
    "不足",
    "不充分",
    "缺失",
    "缺少",
    "待补充",
)


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized(value: Any) -> str:
    return _text(value).lower().replace("-", "_").replace(" ", "_")


def _body_text(value: Any) -> str:
    """Extract prose from common candidate content shapes, excluding media-only blocks."""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, list):
        return " ".join(part for item in value if (part := _body_text(item)))
    if not isinstance(value, dict):
        return ""

    block_type = _normalized(value.get("type"))
    if block_type in {"image", "video", "audio", "media", "attachment"}:
        return ""
    if block_type in {"text", "paragraph", "heading", "list_item", "quote"}:
        for key in ("text", "value", "content", "children"):
            text = _body_text(value.get(key))
            if text:
                return text
        return ""
    if "blocks" in value:
        return _body_text(value["blocks"])
    if "content" in value:
        return _body_text(value["content"])
    if "text" in value:
        return _body_text(value["text"])
    if "value" in value:
        return _body_text(value["value"])
    return ""


def draft_blocking_reasons(knowledge: dict[str, Any] | None) -> list[str]:
    draft = dict(knowledge or {})
    reasons: list[str] = []
    if not _text(draft.get("title")):
        reasons.append("知识标题为空")
    if not _body_text(draft.get("content")):
        reasons.append("知识正文为空")
    if not _text(draft.get("recommended_reply")):
        reasons.append("推荐回复为空")
    return reasons


def _has_explicit_evidence_risk(model_review: dict[str, Any]) -> bool:
    evidence = _normalized(model_review.get("evidence_sufficiency"))
    if evidence in EVIDENCE_RISK_VALUES or any(
        marker in evidence for marker in EVIDENCE_RISK_MARKERS
    ):
        return True
    raw_flags = model_review.get("risk_flags") or []
    flags = [raw_flags] if isinstance(raw_flags, str) else raw_flags
    for flag in flags:
        value = _normalized(flag)
        mentions_evidence = "evidence" in value or "证据" in value
        has_risk_marker = any(marker in value for marker in EVIDENCE_RISK_MARKERS)
        has_ok_marker = any(marker in value for marker in EVIDENCE_OK_MARKERS)
        if mentions_evidence and (has_risk_marker or not has_ok_marker):
            return True
    return False


def current_draft_version(review_metadata: dict[str, Any] | None) -> int:
    metadata = dict(review_metadata or {})
    try:
        version = int(metadata.get("draft_version") or 0)
    except (TypeError, ValueError):
        version = 0
    if version > 0:
        return version

    history = metadata.get("draft_revision_history") or []
    inferred_version = 1
    if isinstance(history, list):
        for entry in history:
            if not isinstance(entry, dict):
                continue
            try:
                entry_version = entry.get("draft_version")
                if entry_version:
                    inferred_version = max(inferred_version, int(entry_version))
                else:
                    inferred_version = max(
                        inferred_version,
                        int(entry.get("revision") or 0) + 1,
                    )
            except (TypeError, ValueError):
                continue
    return inferred_version


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
    if action == "discard":
        return "discard"
    quality = _normalized(model.get("draft_quality"))
    if quality in DRAFT_APPROVED_VALUES:
        return "approved"
    if quality in DRAFT_REVISION_VALUES:
        return "revision_required"
    if quality in DRAFT_HOLD_VALUES:
        return "hold_for_evidence"
    return "pending"


def model_priority_reasons(
    model_review: dict[str, Any] | None,
    *,
    knowledge: dict[str, Any] | None = None,
) -> list[str]:
    """Count only candidates with a concrete model or evidence risk."""
    model = dict(model_review or {})
    reasons: list[str] = []

    raw_flags = model.get("risk_flags") or []
    flags = [raw_flags] if isinstance(raw_flags, str) else raw_flags
    for flag in flags:
        value = _text(flag)
        if _normalized(value) not in SAFE_RISK_VALUES and value not in reasons:
            reasons.append(f"模型风险：{value}")

    action = _text(model.get("suggested_action"))
    if action == "return_for_revision":
        reasons.append("模型要求修订草稿")
    elif action == "hold_for_evidence":
        reasons.append("模型要求补充来源证据")

    disposition = model_draft_disposition(model)
    if disposition == "revision_required" and action != "return_for_revision":
        reasons.append("模型判定草稿需要修订")
    elif disposition == "hold_for_evidence" and action != "hold_for_evidence":
        reasons.append("模型判定来源证据不足")

    if _has_explicit_evidence_risk(model):
        evidence = _text(model.get("evidence_sufficiency"))
        reasons.append(f"证据风险：{evidence or '模型风险标记指出来源证据不足'}")

    for field, label, risk_values in (
        ("content_consistency", "内容一致性风险", {"inconsistent", "mismatch", "不一致", "冲突"}),
        ("standard_consistency", "标准一致性风险", {"inconsistent", "mismatch", "不一致", "冲突"}),
    ):
        value = _normalized(model.get(field))
        if value in risk_values or any(
            marker in value for marker in ("inconsistent", "mismatch", "不一致", "冲突")
        ):
            reasons.append(f"{label}：{_text(model.get(field))}")

    error_type = _text(model.get("error_type"))
    if _normalized(error_type) not in SAFE_RISK_VALUES:
        reasons.append(f"模型识别风险：{error_type}")

    if knowledge is not None and not _text(dict(knowledge).get("evidence_excerpt")):
        reasons.append("证据风险：来源证据摘要缺失")

    return list(dict.fromkeys(reasons))


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


def build_quick_human_review(
    knowledge_value: Any,
    *,
    include_in_training: bool,
    notes: Any = "",
) -> dict[str, Any]:
    """Map one human value decision to the legacy review fields.

    The candidate review UI intentionally asks for one decision only. The older
    usability and decision fields remain populated for compatibility.
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


def upstream_candidate_eligible(selection: dict[str, Any] | None) -> bool:
    source = dict(selection or {})
    if "upstream_eligible" in source:
        return source["upstream_eligible"] is True
    return source.get("eligible") is True or (
        "自动审核：auto_approved" in (source.get("reasons") or [])
    )


def evaluate_candidate_state(
    selection: dict[str, Any] | None,
    human_review: dict[str, Any] | None,
    model_review: dict[str, Any] | None = None,
    *,
    knowledge: dict[str, Any] | None = None,
    model_annotation: dict[str, Any] | None = None,
    draft_version: int = 1,
    has_draft_revision_history: bool = False,
    submitted: bool = False,
    failed_reason: str = "",
    auto_review_min_confidence: float = 0.85,
) -> dict[str, Any]:
    selection_data = dict(selection or {})
    review = normalize_human_review(human_review)
    model = dict(model_review or {})
    annotation = dict(model_annotation or {})
    priority_reasons = model_priority_reasons(model, knowledge=knowledge)

    def result(
        status: str,
        eligible: bool,
        blockers: list[str],
        next_action: str,
    ) -> dict[str, Any]:
        return {
            "review_status": status,
            "eligible": eligible,
            "blocking_reasons": blockers,
            "review_reason": blockers[0] if blockers else {
                "ready": "候选价值与草稿门禁已通过，可提交发布审核",
                "submitted": "候选已进入发布审核，只读查看",
                "rejected": "候选已被人工判定为不沉淀",
            }.get(status, ""),
            "next_action": next_action,
            "draft_version": max(1, int(draft_version or 1)),
            "priority_review": bool(priority_reasons)
            and status not in {"submitted", "rejected"},
            "priority_reasons": priority_reasons,
        }

    if submitted:
        return result("submitted", False, [], "view_submitted")
    if failed_reason:
        return result("failed", False, [failed_reason], "fix_and_retry_submission")

    knowledge_value = review["knowledge_value"]
    usability = review["usability"]
    decision = review["decision"]
    if knowledge_value == "unworthy":
        return result("rejected", False, ["人工确认该知识点不值得沉淀"], "finish")
    if decision in {"rejected", "bad_case"}:
        return result("rejected", False, ["人工审核结论为驳回"], "finish")
    if usability == "unusable":
        return result("rejected", False, ["人工确认候选内容不可用"], "finish")

    completeness_blockers = draft_blocking_reasons(knowledge)
    if knowledge is not None and completeness_blockers:
        return result("draft_incomplete", False, completeness_blockers, "complete_draft")

    annotation_status = _text(annotation.get("status"))
    annotation_version = annotation.get("draft_version")
    if annotation_status == "stale_after_draft_revision":
        return result(
            "pending_model_annotation",
            False,
            [_text(annotation.get("reason")) or "当前草稿版本尚未完成模型初标"],
            "annotate_model",
        )
    if annotation_status == "failed":
        return result(
            "pending_model_annotation",
            False,
            [_text(annotation.get("reason")) or "模型初标失败，需要重试"],
            "annotate_model",
        )

    has_model_review = bool(model)
    if has_model_review:
        if annotation_version is None and not has_draft_revision_history:
            annotation_is_current = True
        else:
            try:
                annotation_is_current = int(annotation_version) == int(draft_version)
            except (TypeError, ValueError):
                annotation_is_current = False
        if not annotation_is_current:
            return result(
                "pending_model_annotation",
                False,
                ["模型初标对应的草稿版本已过期，需要重新标注"],
                "annotate_model",
            )
        action = _text(model.get("suggested_action"))
        disposition = model_draft_disposition(model)
        if _has_explicit_evidence_risk(model):
            return result(
                "hold_for_evidence",
                False,
                ["模型判断来源证据不足，补证后再继续复核"],
                "add_source_evidence",
            )
        if action == "return_for_revision" or (
            action not in {"submit_for_human_review", "hold_for_evidence", "discard"}
            and disposition == "revision_required"
        ):
            return result(
                "revision_required",
                False,
                ["模型判断知识草稿需退回转写修改"],
                "revise_draft",
            )
        if action == "hold_for_evidence" or (
            action not in {"submit_for_human_review", "return_for_revision", "discard"}
            and disposition == "hold_for_evidence"
        ):
            return result(
                "hold_for_evidence",
                False,
                ["模型要求补充来源证据或业务确认"],
                "add_source_evidence",
            )
        if (
            action not in {"", "submit_for_human_review", "discard"}
            and disposition == "pending"
        ) or (
            annotation_status == "completed"
            and not action
            and disposition == "pending"
        ):
            return result(
                "pending_model_annotation",
                False,
                ["模型初标未给出可执行的下一步动作"],
                "annotate_model",
            )
        if action == "discard":
            return result(
                "discard_review",
                False,
                ["模型建议停止该候选，等待人工确认"],
                "confirm_model_discard",
            )

    if knowledge_value == "worthy" and (
        usability == "usable" or decision in {"approved", "approved_with_changes"}
    ):
        return result("ready", True, [], "submit_for_publication_review")
    upstream_eligible = upstream_candidate_eligible(selection_data)
    raw_confidence = (model if has_model_review else selection_data).get("confidence")
    try:
        model_confidence = float(raw_confidence) if not isinstance(raw_confidence, bool) else -1
    except (TypeError, ValueError):
        model_confidence = -1
    if (
        upstream_eligible
        and not has_draft_revision_history
        and not priority_reasons
        and auto_review_min_confidence <= model_confidence <= 1
        and (
            not has_model_review
            or (
                model_draft_disposition(model) == "approved"
                and normalize_knowledge_value(model.get("knowledge_value")) == "worthy"
            )
        )
    ):
        return result("ready", True, [], "submit_for_publication_review")
    return result(
        "pending",
        False,
        ["等待人工确认沉淀价值"],
        "human_review",
    )


def evaluate_review_status(
    selection: dict[str, Any] | None,
    human_review: dict[str, Any] | None,
    model_review: dict[str, Any] | None = None,
) -> tuple[str, bool, str]:
    """Backward-compatible tuple view of the shared candidate state calculation."""
    state = evaluate_candidate_state(selection, human_review, model_review)
    return (
        state["review_status"],
        state["eligible"],
        state["review_reason"],
    )
