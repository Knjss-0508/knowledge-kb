from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


KNOWLEDGE_VALUE_EVIDENCE_GUARD_VERSION = (
    "knowledge-value-evidence-guard-v3-question-aware"
)

_GENERIC_RULE_VALUES = {
    "",
    "待确认",
    "未知",
    "无",
    "无明确阈值",
    "无明确标准",
    "不适用",
}
_NEGATED_REUSABLE_RULE_PATTERN = re.compile(
    r"(?:没有|缺少|无明确|未提供|未说明|不包含)"
    r".{0,60}(?:阈值|边界|规则|步骤|方法|流程|标准|"
    r"核对|检查|检测|拍摄|补充|读取|查看|操作|确认|核验|判定)"
)
_REUSABLE_RULE_CLAUSE_SPLIT_PATTERN = re.compile(
    r"(?:[。！!\n；;，,：:]+|但(?:是)?|不过|然而|可是)"
)
_QUESTION_FRAMING_PATTERN = re.compile(
    r"^(?:(?:用户.{0,8})?(?:只是在)?(?:询问|咨询|想问|想知道)|"
    r"请问|是否|能否|是不是|算不算|怎么|如何|多少|多大|哪个|哪些)"
)
_QUESTION_TERM_PATTERN = re.compile(
    r"(?:是否|能否|是不是|算不算|怎么|如何|请问|多少|多大|哪个|哪些)"
)
_ASSERTIVE_SOURCE_PATTERN = re.compile(
    r"(?:必须|需要|应当|应|需|先|再|核对|检查|检测|确认|判定|"
    r"说明|答复|回复|结论|要求|为准)"
)
_EXPLICIT_REUSABLE_JUDGMENT_PATTERN = re.compile(
    r"(?:(?:应|需|需要|可|可以|直接)?判定为|不属于|属于|"
    r"(?:当|若|如果|出现|存在|超过|低于|大于|小于).{0,50}"
    r"按.{1,30}(?:处理|判定|回收|质检)|"
    r"(?:需|需要|必须).{0,40}(?:结合|核对|确认|核验|检查|拍摄|补充|读取|查看))"
)
_DRAFTABLE_JUDGMENT_PATTERN = re.compile(
    r"(?:(?:应|需|需要|可|可以|直接)?判定为|不属于|属于|"
    r"(?:当|若|如果|出现|存在|超过|低于|大于|小于).{0,50}"
    r"按.{1,30}(?:处理|判定|回收|质检))"
)
_EXPLICIT_REUSABLE_STEP_PATTERN = re.compile(
    r"(以.+为准|优先采用|优先按|先.+再|依次|步骤|进入.+页面|"
    r"读取.+信息|核对.+信息|检查.+后|检测.+后|大于|小于|"
    r"不超过|不少于|至少|必须|不得|复测|"
    r"(?:打开|进入).{0,30}(?:查看|读取)|查看.{0,20}(?:型号|信息)|"
    r"再.{0,30}核对|补充.{0,20}(?:图片|截图|全景|近景)|"
    r"拍摄.{0,20}(?:图片|截图|全景|近景))"
)
_DRAFTABLE_SOURCE_RULE_PATTERN = re.compile(
    r"(以.{1,40}为准|选择[“\"']?.{1,20}[”\"']?|"
    r"不要求.{0,20}(?:检查|检测|核验)|不需要.{0,20}(?:检查|检测|核验)|"
    r"可以.{0,20}(?:回收|质检|直接|继续|正常)|"
    r"先.+再|依次|大于|小于|不超过|不少于|至少|必须|不得|复测)"
)
_NUMERIC_RULE_PATTERN = re.compile(
    r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|%|次|个|GB|TB|分钟|小时|天)",
    flags=re.IGNORECASE,
)
_EXPLICIT_FUNCTION_FACT_PATTERN = re.compile(
    r"(?<!是否)(?<!只)(?<!仅)(?<!聊天)(?<!来源)(?<!证据)"
    r"(?:不支持|支持|不具备|具备|未配备|配备)"
    r"(?!哪些|什么|与否|情况|查询|确认|核对).{1,40}"
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _is_question_clause(value: str) -> bool:
    if value.endswith(("？", "?", "吗", "呢")):
        return True
    if (
        _QUESTION_FRAMING_PATTERN.search(value)
        and not _ASSERTIVE_SOURCE_PATTERN.search(value)
    ):
        return True
    return bool(
        _QUESTION_TERM_PATTERN.search(value)
        and not _ASSERTIVE_SOURCE_PATTERN.search(value)
    )


def _positive_source_values(values: Iterable[Any]) -> list[str]:
    positive_values: list[str] = []
    for value in values:
        positive_clauses: list[str] = []
        prepared_value = re.sub(r"([？?])", r"\1\n", _clean_text(value))
        for clause in _REUSABLE_RULE_CLAUSE_SPLIT_PATTERN.split(
            prepared_value
        ):
            cleaned_clause = clause.strip()
            if (
                cleaned_clause
                and not _NEGATED_REUSABLE_RULE_PATTERN.search(cleaned_clause)
                and not _is_question_clause(cleaned_clause)
            ):
                positive_clauses.append(cleaned_clause)
        if positive_clauses:
            positive_values.append("，".join(positive_clauses))
    return positive_values


def has_explicit_reusable_knowledge(
    *,
    source_values: Iterable[Any],
    threshold_values: Iterable[Any],
) -> bool:
    positive_source_values = _positive_source_values(source_values)
    positive_rule_text = "\n".join(positive_source_values)
    positive_threshold_values = {
        text
        for value in threshold_values
        for text in _positive_source_values([value])
    }
    if any(
        value not in _GENERIC_RULE_VALUES
        for value in positive_threshold_values
    ):
        return True
    if _EXPLICIT_REUSABLE_JUDGMENT_PATTERN.search(positive_rule_text):
        return True
    if _EXPLICIT_FUNCTION_FACT_PATTERN.search(positive_rule_text):
        return True
    if _NUMERIC_RULE_PATTERN.search(positive_rule_text):
        return True
    return bool(_EXPLICIT_REUSABLE_STEP_PATTERN.search(positive_rule_text))


def has_draftable_source_rule(
    *,
    source_values: Iterable[Any],
    threshold_values: Iterable[Any],
) -> bool:
    positive_source_values = _positive_source_values(source_values)
    positive_rule_text = "\n".join(positive_source_values)
    positive_threshold_values = {
        text
        for value in threshold_values
        for text in _positive_source_values([value])
    }
    if any(
        value not in _GENERIC_RULE_VALUES
        for value in positive_threshold_values
    ):
        return True
    if _DRAFTABLE_JUDGMENT_PATTERN.search(positive_rule_text):
        return True
    if _EXPLICIT_FUNCTION_FACT_PATTERN.search(positive_rule_text):
        return True
    if _NUMERIC_RULE_PATTERN.search(positive_rule_text):
        return True
    return bool(_DRAFTABLE_SOURCE_RULE_PATTERN.search(positive_rule_text))
