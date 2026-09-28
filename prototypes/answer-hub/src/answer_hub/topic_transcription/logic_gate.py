from __future__ import annotations

import re
from difflib import SequenceMatcher
from typing import Any, Iterable, Mapping

from .contracts import TopicEvidence


_POSITIVE_MARKERS = ("正常", "通过", "合格", "符合", "一致", "相符")
_NEGATIVE_MARKERS = (
    "异常",
    "不通过",
    "不合格",
    "不符合",
    "不一致",
    "不相符",
    "失败",
    "不正常",
)
_ANY_MARKERS = (
    "任一",
    "任意一个",
    "任何一个",
    "只要一个",
    "有一个",
    "至少一个",
    "其中一个",
    "或者",
    "或",
)
_ALL_MARKERS = (
    "同时",
    "全部",
    "每个",
    "每一",
    "各个",
    "均",
    "都",
    "两个",
    "两处",
    "分别",
)
_ABSOLUTE_SCOPE_MARKERS = ("所有", "任何", "一律", "全部")
_STRUCTURAL_LINE_MARKERS = (
    "适用情形",
    "核验要点",
    "处理结论",
    "处理方式",
    "适用边界",
    "例外",
    "补充证据",
    "人工复核",
)
_SUBSTANTIVE_MARKERS = (
    *_POSITIVE_MARKERS,
    *_NEGATIVE_MARKERS,
    "判定",
    "选择",
    "适用",
    "检测",
    "核验",
    "孔位",
    "阈值",
    "机型",
)
_CONCLUSION_MARKERS = (
    "判定",
    "选择",
    "应选",
    "属于",
    "不属于",
    "处理为",
    "按",
)


def _normalize(value: Any) -> str:
    return re.sub(r"[\s，,。；;：:！？!?（）()\[\]【】“”\"'、]+", "", str(value or "")).lower()


def _has_marker(text: str, markers: Iterable[str]) -> bool:
    return any(marker in text for marker in markers)


def _polarity_flags(value: str) -> tuple[bool, bool]:
    """Return positive/negative flags without counting negated positives twice."""

    text = value
    negative = _has_marker(text, _NEGATIVE_MARKERS)
    positive_text = text
    for marker in _NEGATIVE_MARKERS:
        positive_text = positive_text.replace(marker, "")
    positive = _has_marker(positive_text, _POSITIVE_MARKERS)
    return positive, negative


def _last_polarity(value: str) -> tuple[bool, bool]:
    """Use the last substantive clause for the resulting state polarity."""

    for segment in reversed(
        [
            item.strip()
            for item in re.split(r"[。；;！？!?\n]+", value)
            if item.strip()
        ]
    ):
        positive, negative = _polarity_flags(segment)
        if positive or negative:
            return positive, negative
    return _polarity_flags(value)


def _explicit_polarity(value: str) -> tuple[bool, bool]:
    """Infer state only from a clause that explicitly expresses a result."""

    segments = [
        item.strip()
        for item in re.split(r"[。；;！？!?\n]+", value)
        if item.strip()
    ]
    for segment in reversed(segments):
        positions = [
            (segment.rfind(marker), marker)
            for marker in _CONCLUSION_MARKERS
            if segment.rfind(marker) >= 0
        ]
        if not positions:
            continue
        position, _marker = max(positions)
        tail = segment[position:]
        positive, negative = _polarity_flags(tail)
        if positive or negative:
            return positive, negative
    return False, False


def _claims(content: str, recommended_reply: str) -> list[str]:
    values = [content, recommended_reply]
    result: list[str] = []
    for value in values:
        for raw in re.split(r"[\n。；;！？!?]+", str(value or "")):
            line = re.sub(r"^\s*\d+[.、)）]\s*", "", raw).strip()
            if line and line not in result:
                result.append(line)
    return result


def _fact_refs(claim: str, evidence: TopicEvidence) -> tuple[str, ...]:
    normalized_claim = _normalize(claim)
    if len(normalized_claim) < 4:
        return ()
    scored: list[tuple[float, str]] = []
    for fact in evidence.facts:
        normalized_source = _normalize(fact.text)
        if not normalized_source:
            continue
        overlap = 0
        for width in range(6, min(12, len(normalized_source)) + 1):
            if any(
                normalized_source[start : start + width] in normalized_claim
                for start in range(0, len(normalized_source) - width + 1)
            ):
                overlap = width
                break
        ratio = SequenceMatcher(None, normalized_claim, normalized_source).ratio()
        if overlap >= 6 or ratio >= 0.45:
            scored.append((max(overlap / 12, ratio), fact.fact_id))
    scored.sort(reverse=True)
    return tuple(fact_id for _score, fact_id in scored[:5])


def _strong_claim_without_ref(claim: str, refs: tuple[str, ...]) -> bool:
    normalized = _normalize(claim)
    if refs or len(normalized) < 8:
        return False
    return _has_marker(normalized, _SUBSTANTIVE_MARKERS) and not _has_marker(
        normalized,
        _STRUCTURAL_LINE_MARKERS,
    )


def _source_outcome_text(evidence: TopicEvidence) -> str:
    return "\n".join(
        fact.fields.get(field, "")
        for fact in evidence.facts
        for field in (
            "human_judgment_conclusion",
            "historical_actual_reply",
            "judgment_basis",
        )
        if fact.fields.get(field)
    )


def _logic_conflicts(content: str, recommended_reply: str, evidence: TopicEvidence) -> list[str]:
    source_raw = evidence.source_text
    outcome_raw = _source_outcome_text(evidence)
    draft_raw = f"{content}\n{recommended_reply}"
    source = _normalize(source_raw)
    outcome = _normalize(outcome_raw)
    draft = _normalize(draft_raw)
    if not source or not draft:
        return []

    source_has_positive, source_has_negative = _polarity_flags(source)
    source_has_mixed = source_has_positive and source_has_negative
    source_has_joint = _has_marker(source, _ALL_MARKERS) or bool(
        re.search(r"(?:一个|1号|一号).{0,24}(?:符合|一致|相符).{0,24}(?:另一个|另一|5号|五号).{0,24}(?:不符合|不一致|不相符)", source)
    )
    source_has_any = _has_marker(source, _ANY_MARKERS)
    draft_has_any = _has_marker(draft, _ANY_MARKERS)
    draft_has_all = _has_marker(draft, _ALL_MARKERS)
    draft_positive, draft_negative = _explicit_polarity(draft_raw)
    outcome_positive, outcome_negative = _explicit_polarity(outcome_raw)

    source_has_only_if = bool(
        re.search(r"(?:只有|仅当).{0,36}(?:才|才能|方可)", source_raw)
    )
    draft_has_sufficient_condition = bool(
        re.search(r"(?:只要|有一个|满足).{0,36}(?:即可|就可以|便可|可以)", draft_raw)
    )

    issues: list[str] = []
    if source_has_joint and draft_has_any and not source_has_any:
        issues.append(
            "量词关系错误：来源要求同时/分别核验多个条件，正文却使用“任一/只要一个”替代联合条件。"
        )
    if source_has_only_if and draft_has_sufficient_condition:
        issues.append(
            "条件方向错误：来源表达“只有满足条件才可判定”，正文却改写成满足单一条件即可。"
        )
    if source_has_mixed and draft_has_any and draft_positive and (
        outcome_negative or "异常" in outcome
    ):
        issues.append(
            "正常/异常极性冲突：来源同时包含符合与不符合证据，人工结论指向异常，正文却把任一符合改写为正常。"
        )
    if (
        outcome_negative
        and not outcome_positive
        and draft_positive
        and not draft_negative
    ):
        issues.append(
            "处理结论极性冲突：来源结论为异常/不通过，正文或推荐回复却单独输出正常/通过。"
        )
    if (
        outcome_positive
        and not outcome_negative
        and draft_negative
        and not draft_positive
    ):
        issues.append(
            "处理结论极性冲突：来源结论为正常/通过，正文或推荐回复却单独输出异常/不通过。"
        )
    if draft_has_any and draft_has_all:
        # A sentence can legitimately explain both branches.  Only report the
        # conflict when the same draft also contains a direct positive result.
        if draft_positive and source_has_mixed:
            issues.append(
                "条件关系不清：正文同时出现任一和全部条件，但未明确分支对应的处理结论。"
            )
    return list(dict.fromkeys(issues))


def _scope_expansions(
    content: str,
    recommended_reply: str,
    evidence: TopicEvidence,
) -> tuple[str, ...]:
    draft = _normalize(f"{content}\n{recommended_reply}")
    source = _normalize(evidence.source_text)
    if _has_marker(draft, _ABSOLUTE_SCOPE_MARKERS) and not _has_marker(
        source,
        _ABSOLUTE_SCOPE_MARKERS,
    ):
        return (
            "适用范围扩大：正文使用所有/任何/一律等绝对范围词，来源未提供同等范围。",
        )
    return ()


def audit_logic_and_claims(
    *,
    content: str,
    recommended_reply: str,
    evidence: TopicEvidence,
    existing_unsupported_claims: Iterable[str] = (),
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    refs_by_claim: dict[str, tuple[str, ...]] = {}
    missing: list[str] = []
    for claim in _claims(content, recommended_reply):
        refs = _fact_refs(claim, evidence)
        refs_by_claim[claim] = refs
        if _strong_claim_without_ref(claim, refs):
            missing.append(claim)
    missing.extend(str(item) for item in existing_unsupported_claims if str(item).strip())
    logic = _logic_conflicts(content, recommended_reply, evidence)
    scope = _scope_expansions(content, recommended_reply, evidence)
    return (
        refs_by_claim,
        tuple(dict.fromkeys(missing)),
        tuple(logic),
        tuple(scope),
    )
