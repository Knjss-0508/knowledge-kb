from __future__ import annotations

from collections.abc import Iterable
import re
from typing import Any


KNOWLEDGE_VALUE_EVIDENCE_GUARD_VERSION = (
    "knowledge-value-evidence-guard-v6-named-condition-branches"
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
_CONCRETE_DISPOSITION_PATTERN = re.compile(
    r"按(?:照)?(?!照)"
    r"(?!(?:(?:当前|相关|现有|上述|对应|具体|实际)的?)*"
    r"(?:实际|具体|标准|来源|情况|现场|事实|证据))"
    r"(?!(?:正常|异常)(?:类型)?(?:处理|判定))"
    r"[^，,。；;\n？?]{2,25}(?:处理|扣减|勾选|填写)"
)
_EXPLICIT_FUNCTION_FACT_PATTERN = re.compile(
    r"(?<!是否)(?<!只)(?<!仅)(?<!聊天)(?<!来源)(?<!证据)"
    r"(?:不支持|支持|不具备|具备|未配备|配备)"
    r"(?!哪些|什么|与否|情况|查询|确认|核对).{1,40}"
)
_CONDITIONAL_DISPOSITION_PATTERN = re.compile(
    r"(?P<condition>[^，,。；;\n：:！？!?|｜]{2,30}?)"
    r"(?P<modal>不需要|不用|无需|不必|不要|不需|需要|要|需)"
    r"(?P<action>[\u4e00-\u9fff]{0,8})"
)
_GENERIC_CONDITION_PATTERN = re.compile(
    r"(?:(?:正常|异常|实际|具体|现场|当前|一般|其他|其它)"
    r"(?:的)?(?:情况|情形|机器|设备|类型)?|"
    r"(?:这|那|该|本|此|其|某|另一|某一)(?:个|种|样|台|件|项|类)?"
    r"(?:的)?(?:情况|情形|机器|设备)?)"
)


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def concrete_judgment_label(value: Any) -> str:
    """Recognize a short, explicit option answer, without generic placeholders."""
    match = re.fullmatch(
        r"(?:改判|判定为|判为|判)([^\s，,。；;：:！？!?|｜]{2,20})[。！!]?",
        _clean_text(value),
    )
    if not match:
        return ""
    label = match.group(1)
    if label.startswith((
        "定", "断", "正常", "异常", "实际", "具体", "情况", "标准",
        "来源", "现场", "待", "未确认", "未核实", "需核实", "需补充",
        "不能确认", "不能判断", "无法确认", "无法判断",
        "未知", "不了", "不清", "不确定",
    )) or re.search(r"是否|如何|怎么|哪个|什么|[吗呢么]$", label):
        return ""
    return label


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


def _has_named_condition_branches(value: str) -> bool:
    clauses = value.split("，")
    if len(clauses) != 2:
        return False
    conditions: set[str] = set()
    polarities: set[bool] = set()
    actions: set[str] = set()
    for clause in clauses:
        match = _CONDITIONAL_DISPOSITION_PATTERN.fullmatch(clause)
        if not match:
            return False
        condition = re.sub(r"""[\s“”"'‘’]""", "", match.group("condition"))
        if len(condition) < 2 or _GENERIC_CONDITION_PATTERN.fullmatch(condition):
            return False
        conditions.add(condition)
        polarities.add(match.group("modal").startswith(("不", "无")))
        if action := match.group("action"):
            actions.add(action)
    # A missing verb is supported only by the other branch in this source.
    return len(conditions) == 2 and len(polarities) == 2 and len(actions) == 1


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
    if _CONCRETE_DISPOSITION_PATTERN.search(positive_rule_text):
        return True
    if any(concrete_judgment_label(value) for value in positive_source_values):
        return True
    if any(_has_named_condition_branches(value) for value in positive_source_values):
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
    if _CONCRETE_DISPOSITION_PATTERN.search(positive_rule_text):
        return True
    if any(concrete_judgment_label(value) for value in positive_source_values):
        return True
    if any(_has_named_condition_branches(value) for value in positive_source_values):
        return True
    return bool(_DRAFTABLE_SOURCE_RULE_PATTERN.search(positive_rule_text))
