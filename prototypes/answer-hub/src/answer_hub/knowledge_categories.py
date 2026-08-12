from __future__ import annotations

from typing import Any


QC_STANDARD_CATEGORY = "质检标准"
QC_PROCESS_CATEGORY = "质检流程"
CASE_ANALYSIS_CATEGORY = "案例解析"
EXTRA_KNOWLEDGE_CATEGORY = "课外常识"
UNCERTAIN_CATEGORY = "不确定"

SUPPORTED_KNOWLEDGE_CATEGORIES = (
    QC_STANDARD_CATEGORY,
    QC_PROCESS_CATEGORY,
    CASE_ANALYSIS_CATEGORY,
    EXTRA_KNOWLEDGE_CATEGORY,
    UNCERTAIN_CATEGORY,
)

LEGACY_KNOWLEDGE_CATEGORY_ALIASES = {
    "检测方法": QC_PROCESS_CATEGORY,
    "流程方法": QC_PROCESS_CATEGORY,
    "操作流程": QC_PROCESS_CATEGORY,
    "场景判定": QC_STANDARD_CATEGORY,
    "标准定义": QC_STANDARD_CATEGORY,
    "已有知识": QC_STANDARD_CATEGORY,
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def normalize_knowledge_category(
    value: Any,
    *,
    default: str = UNCERTAIN_CATEGORY,
) -> str:
    text = _text(value)
    if not text:
        return default
    if text in SUPPORTED_KNOWLEDGE_CATEGORIES:
        return text
    return LEGACY_KNOWLEDGE_CATEGORY_ALIASES.get(text, default)


def knowledge_category_from_topic_stage(
    topic_stage: Any,
    knowledge_form: Any = "",
) -> str:
    normalized = normalize_knowledge_category(topic_stage, default="")
    if normalized:
        return normalized
    form = _text(knowledge_form)
    if form == "流程方法":
        return QC_PROCESS_CATEGORY
    if form == "具体判定":
        return QC_STANDARD_CATEGORY
    return UNCERTAIN_CATEGORY


def category_lookup_names(value: Any) -> tuple[str, ...]:
    text = _text(value)
    category = normalize_knowledge_category(text, default="")
    if category == QC_PROCESS_CATEGORY:
        return (QC_PROCESS_CATEGORY, "操作流程", "检测方法")
    if category == QC_STANDARD_CATEGORY:
        return (QC_STANDARD_CATEGORY, "场景判定", "标准定义")
    if category == UNCERTAIN_CATEGORY:
        return (UNCERTAIN_CATEGORY, CASE_ANALYSIS_CATEGORY)
    if category:
        return (category,)
    return (text,) if text else ()
