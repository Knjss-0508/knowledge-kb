from __future__ import annotations

import re
from typing import Any, Mapping

from .contracts import TopicEvidence, TopicFact


_FACT_TEXT_FIELDS = (
    "human_core_problem",
    "atomic_question",
    "human_judgment_conclusion",
    "judgment_basis",
    "semantic_basis",
    "historical_actual_reply",
    "conversation_excerpt",
    "conversation_full_excerpt",
    "intent_evidence_excerpt",
    "threshold_or_exception",
    "source_supported_threshold_or_exception",
    "image_processing_status",
    "image_evidence_summary",
)

_STRUCTURED_FIELD_ALIASES: dict[str, tuple[str, ...]] = {
    "object": ("object", "object_entity", "对象", "对象/部位"),
    "condition": (
        "condition",
        "检测条件",
        "判定条件",
        "适用条件",
        "applicable_scope",
    ),
    "observation": (
        "observation",
        "观察结果",
        "异常现象",
        "现象",
    ),
    "action": ("action", "处理方式", "解题方式", "处理动作"),
    "result": (
        "result",
        "处理结论",
        "判定结论",
        "human_judgment_conclusion",
    ),
    "boundary": (
        "boundary",
        "适用边界",
        "threshold_or_exception",
        "source_supported_threshold_or_exception",
    ),
}


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _first_text(raw: Mapping[str, Any], aliases: tuple[str, ...]) -> str:
    for alias in aliases:
        value = _clean(raw.get(alias))
        if value:
            return value
    return ""


def _confidence(raw: Mapping[str, Any]) -> float | None:
    value = raw.get("confidence")
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build_topic_evidence(
    evidence_package: Mapping[str, Any] | None,
) -> TopicEvidence:
    """Normalize the existing workflow evidence package without changing it."""

    package = evidence_package or {}
    raw_facts = [
        *(package.get("facts") or []),
        *(package.get("representative_facts") or []),
    ]
    # A representative fact is often the same source fact with richer
    # excerpts. Merge by fact_id so the richer fields are not lost merely
    # because the ordinary facts list appeared first.
    merged: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for index, raw in enumerate(raw_facts, start=1):
        if not isinstance(raw, Mapping):
            continue
        fact_id = _clean(raw.get("fact_id")) or f"F{index:02d}"
        if fact_id not in merged:
            merged[fact_id] = {}
            order.append(fact_id)
        target = merged[fact_id]
        for key, value in raw.items():
            if key not in target or not _clean(target.get(key)):
                target[key] = value
            elif key in {"image_urls", "video_urls", "unavailable_image_urls"}:
                existing = target.get(key)
                if isinstance(existing, list) and isinstance(value, list):
                    target[key] = list(dict.fromkeys([*existing, *value]))
    facts: list[TopicFact] = []
    for fact_id in order:
        raw = merged[fact_id]
        fields = {
            field: _clean(raw.get(field))
            for field in _FACT_TEXT_FIELDS
            if _clean(raw.get(field))
        }
        structured = {
            name: _first_text(raw, aliases)
            for name, aliases in _STRUCTURED_FIELD_ALIASES.items()
        }
        image_state = _first_text(
            raw,
            ("image_state", "image_processing_status", "image_evidence_summary"),
        )
        if not image_state and "image_usable" in raw:
            image_state = "可用" if raw.get("image_usable") else "不可用"
        text = "\n".join(
            dict.fromkeys(
                value
                for value in (
                    *fields.values(),
                    structured["object"],
                    structured["condition"],
                    structured["observation"],
                    structured["action"],
                    structured["result"],
                    structured["boundary"],
                )
                if value
            )
        )
        if not text:
            continue
        facts.append(
            TopicFact(
                fact_id=fact_id,
                source_record_id=_clean(raw.get("source_record_id")),
                text=text,
                fields=fields,
                object=structured["object"],
                condition=structured["condition"],
                observation=structured["observation"],
                action=structured["action"],
                result=structured["result"],
                boundary=structured["boundary"],
                confidence=_confidence(raw),
                fact_type=_first_text(
                    raw,
                    ("fact_type", "evidence_type", "type"),
                ),
                image_state=image_state,
            )
        )
    representative_ids = tuple(
        _clean(item)
        for item in (package.get("representative_fact_ids") or [])
        if _clean(item)
    )
    return TopicEvidence(
        facts=tuple(facts),
        representative_fact_ids=representative_ids,
    )
