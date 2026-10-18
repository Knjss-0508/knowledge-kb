from __future__ import annotations

import re
import unicodedata
from decimal import Decimal
from typing import Any, Mapping


QUESTION_STANDARDIZATION_VERSION = "structured-standard-question-v3-atomic-binding"

_UNCERTAIN_VALUES = {
    "",
    "unknown",
    "不确定",
    "其他待确认",
    "待人工确认",
    "待确认",
    "未知",
    "未识别",
}
_NO_THRESHOLD_VALUES = {
    "无",
    "无明确例外",
    "无明确阈值",
    "无明确阈值或例外",
    "无阈值",
    "通用",
}
_QUESTION_REPLACEMENTS = (
    ("应如何", "如何"),
    ("怎么", "如何"),
    ("怎样", "如何"),
)


def _text(value: Any) -> str:
    return re.sub(
        r"\s+",
        " ",
        unicodedata.normalize("NFKC", str(value or "")).replace("\u3000", " "),
    ).strip()


def _is_uncertain(value: Any) -> bool:
    return _text(value).casefold() in _UNCERTAIN_VALUES


def _normalized_key_part(value: Any) -> str:
    normalized = _text(value).casefold()
    for source, target in _QUESTION_REPLACEMENTS:
        normalized = normalized.replace(source, target)
    if normalized in _NO_THRESHOLD_VALUES:
        return "无明确阈值或例外"
    return re.sub(r"[\W_]+", "", normalized, flags=re.UNICODE)


def _display_part(value: Any) -> str:
    text = _text(value)
    if _is_uncertain(text):
        return ""
    for source, target in _QUESTION_REPLACEMENTS:
        text = text.replace(source, target)
    return re.sub(r"[，,。．；;：:、！？!?（）()【】\[\]\"'“”‘’]+", "", text)


def _threshold_key(value: Any) -> str:
    normalized = _text(value).casefold()
    if _is_uncertain(normalized):
        return ""
    if normalized in _NO_THRESHOLD_VALUES:
        return "无明确阈值或例外"
    # Decimal points and comparison operators carry decision semantics.
    for source, target in (
        ("大于等于", ">="), ("小于等于", "<="),
        ("不大于", "<="), ("不小于", ">="),
        ("至少", ">="), ("至多", "<="),
        ("不少于", ">="), ("不超过", "<="),
        ("大于", ">"), ("超过", ">"), ("小于", "<"),
        ("毫米", "mm"), ("≥", ">="), ("≤", "<="),
    ):
        normalized = normalized.replace(source, target)
    normalized = re.sub(r"\s+", "", normalized)
    return re.sub(
        r"\d+(?:\.\d+)?",
        lambda match: format(Decimal(match.group()), "f").rstrip("0").rstrip(".")
        if "." in match.group() else match.group(),
        normalized,
    )


def standard_question_sort_key(
    unit: Mapping[str, Any],
) -> tuple[int, str, str]:
    """Keep comparable questions adjacent without making the key a boundary."""

    status = _text(unit.get("standardization_status"))
    return (
        1 if status == "needs_review" else 0,
        _text(unit.get("standard_question_key")).casefold(),
        _text(unit.get("unit_id") or unit.get("atomic_id")).casefold(),
    )


def threshold_conflict_reason(units: list[Mapping[str, Any]]) -> str:
    constraints = set()
    for unit in units:
        value = _threshold_key(
            unit.get("_原子阈值例外") or unit.get("threshold_or_exception")
        )
        # Different sentences require evidence adjudication. Only explicit
        # numeric conditions can establish a deterministic threshold conflict.
        numeric = tuple(sorted(set(re.findall(
            r"(?:>=|<=|>|<|=)\d+(?:\.\d+)?(?:mm|cm|%|个|处|次)?", value,
        ))))
        if numeric:
            constraints.add(numeric)
    return (
        "标准问题结构化维度冲突：阈值或例外不同，不能自动聚类合并"
        if len(constraints) > 1 else ""
    )


def thresholds_need_evidence_review(units: list[Mapping[str, Any]]) -> bool:
    values = {
        value for unit in units
        if (value := _threshold_key(
            unit.get("_原子阈值例外") or unit.get("threshold_or_exception")
        )) not in {"", "无明确阈值或例外"}
    }
    return len(values) > 1


def standard_question_conflict_reason(
    units: list[Mapping[str, Any]],
) -> str:
    """Guard explicit rule boundaries; wording remains an evidence-model decision."""

    ready_units = [
        unit
        for unit in units
        if _text(unit.get("标准问题标准化状态") or unit.get("standardization_status"))
        == "ready"
    ]
    if len(ready_units) <= 1:
        return ""

    threshold_conflict = threshold_conflict_reason(ready_units)
    if threshold_conflict:
        return threshold_conflict
    guarded_dimensions = (
        ("聚类标准族", ("_聚类标准族", "clustering_standard_family")),
        ("聚类现象值", ("_聚类现象值", "clustering_phenomenon_value")),
    )
    for label, aliases in guarded_dimensions:
        if label == "聚类现象值" and all(
            _text(unit.get("_聚类合并策略") or unit.get("clustering_merge_policy"))
            == "same_standard_family"
            for unit in ready_units
        ):
            continue
        normalize = _normalized_key_part
        values = {
            normalize(
                next(
                    (
                        unit.get(alias)
                        for alias in aliases
                        if not _is_uncertain(unit.get(alias))
                    ),
                    "",
                )
            )
            for unit in ready_units
        } - {""}
        if len(values) > 1:
            return f"标准问题结构化维度冲突：{label}不同，不能自动聚类合并"
    return ""


def standardize_atomic_question(unit: Mapping[str, Any]) -> dict[str, str]:
    """Build an auditable comparison hint without changing the atomic issue."""

    business_line = _display_part(
        unit.get("business_line") or unit.get("回收业务层级")
    )
    product_category = _display_part(
        unit.get("product_category") or unit.get("产品类型")
    )
    subject = _display_part(unit.get("subject") or unit.get("对象/部位"))
    phenomenon = _display_part(
        unit.get("phenomenon") or unit.get("异常现象")
    )
    judgment_target = _display_part(
        unit.get("judgment_target") or unit.get("判定目标")
    )
    resolution_mode = _display_part(
        unit.get("resolution_mode") or unit.get("解题方式")
    )
    standard_path = _display_part(
        unit.get("standard_path") or unit.get("主标准路径")
    )
    threshold = _threshold_key(
        unit.get("threshold_or_exception") or unit.get("_原子阈值例外")
    )
    standard_family = _display_part(
        unit.get("_聚类标准族") or unit.get("clustering_standard_family")
    )
    phenomenon_value = _display_part(
        unit.get("_聚类现象值") or unit.get("clustering_phenomenon_value")
    )

    action = judgment_target or resolution_mode
    model_standard_question = _display_part(unit.get("standard_question"))
    standardization_source = "model" if model_standard_question else "fallback"
    if model_standard_question and subject:
        # A model hint may still describe the parent question after an atomic
        # split. Rebuild the hint; retain the original issue as source evidence.
        subject_key = _normalized_key_part(subject)
        question_key = _normalized_key_part(model_standard_question)
        notebook_components = ("内存", "硬盘", "bios锁", "序列号")
        extra_component = product_category == "笔记本" and any(
            component in question_key and component not in subject_key
            and component not in _normalized_key_part(judgment_target)
            for component in notebook_components
        )
        if subject_key not in question_key or extra_component:
            model_standard_question = ""
            standardization_source = "atomic_rebuild"
    display_parts = [
        value
        for value in (
            product_category,
            subject,
            phenomenon,
            judgment_target,
            resolution_mode,
        )
        if value
    ]
    standard_question = model_standard_question or "｜".join(display_parts)

    key_parts = (
        business_line,
        product_category,
        standard_question,
        standard_path,
        standard_family,
        phenomenon_value,
    )
    standard_question_key = "｜".join(
        _normalized_key_part(value) for value in key_parts
    ) + "｜" + threshold

    missing_dimensions = [
        label
        for label, value in (
            ("回收业务层级", business_line),
            ("产品品类", product_category),
            ("对象/部位", subject),
            ("异常现象或查询目标", phenomenon),
            ("判定诉求或处理动作", action),
        )
        if not value
    ]
    status = "needs_review" if missing_dimensions else "ready"
    review_reason = (
        "标准问题缺少明确维度：" + "、".join(missing_dimensions)
        if missing_dimensions
        else ""
    )
    return {
        "standard_question": standard_question,
        "standard_question_key": standard_question_key,
        "standardization_status": status,
        "standardization_review_reason": review_reason,
        "standardization_source": standardization_source,
        "standardization_version": QUESTION_STANDARDIZATION_VERSION,
    }
