from __future__ import annotations

import re
from typing import Any, Iterable, Mapping

from .contracts import TopicDraftAudit
from .evidence import build_topic_evidence
from .logic_gate import audit_logic_and_claims


def audit_topic_draft(
    *,
    content: str,
    recommended_reply: str,
    evidence_package: Mapping[str, Any] | None,
    topic: Mapping[str, Any] | None = None,
    existing_unsupported_claims: Iterable[str] = (),
    use_standard_references: bool = True,
    active_standard_refs: str = "",
    preserved_standard_refs: str = "",
) -> TopicDraftAudit:
    """Audit one topic draft behind a narrow, reusable interface."""

    evidence = build_topic_evidence(evidence_package)
    refs, missing, logic, scope = audit_logic_and_claims(
        content=content,
        recommended_reply=recommended_reply,
        evidence=evidence,
        existing_unsupported_claims=existing_unsupported_claims,
    )
    issues = [
        *(f"来源事实不支持：{claim}" for claim in missing),
        *logic,
        *scope,
    ]
    if not use_standard_references and active_standard_refs:
        issues.append("无标准引用模式检测到活动标准引用，必须清空并转人工复核。")
    historical_status = (
        "历史标准关联搁置"
        if preserved_standard_refs and not use_standard_references
        else "本轮活动标准引用"
        if active_standard_refs and use_standard_references
        else "无历史标准关联"
    )
    return TopicDraftAudit(
        status="manual_review" if issues else "passed",
        issues=tuple(dict.fromkeys(issues)),
        claim_fact_refs=refs,
        unsupported_claims=missing,
        logic_conflicts=logic,
        scope_expansion=scope,
        historical_standard_reference_status=historical_status,
    )
