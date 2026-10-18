from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

try:
    from app.core.config import settings
except ModuleNotFoundError:  # Allows pure aggregation tests without backend extras.
    class _Settings:
        SECOND_PART_API_QUERY_LIMIT = 10000

    settings = _Settings()

from app.services.high_frequency_classifier import (
    _cache_key,
    classify_questions,
    get_cached_atomic_splits,
    get_cached_classifications,
    split_ambiguous_questions,
)
from app.services.high_frequency_taxonomy import normalize_product_category


FIELD_ALIASES = {
    "核心问题": "核心问题",
    "core_problem": "核心问题",
    "source_core_problem": "核心问题",
    "产品类型": "产品类型",
    "product_type": "产品类型",
    "product_category": "产品类型",
    "category": "产品类型",
    "一级分类": "一级分类",
    "category_l1": "一级分类",
    "二级分类": "二级分类",
    "category_l2": "二级分类",
    "判定结论": "判定结论",
    "judgment": "判定结论",
    "judgment_conclusion": "判定结论",
    "判定依据": "判定依据",
    "judgment_basis": "判定依据",
    "参考话术": "参考话术",
    "reference_reply": "参考话术",
    "recommended_reply": "参考话术",
}

_ENUMERATED_ISSUE_RE = re.compile(
    r"(?:^|[\n；;：:，,。])\s*(?:[①②③④⑤⑥⑦⑧⑨⑩]|\d{1,2}[\.、\)）])\s*"
)
_MULTI_ISSUE_HINT_RE = re.compile(
    r"(?:两个|两项|多个|多项).{0,12}(?:问题|事项)|"
    r"(?:两个|两项).{0,12}(?:待确认|关联疑问)|"
    r"(?:同时|另外|此外|随后|并且|以及).{0,30}"
    r"(?:追问|询问|咨询|反馈|确认|核实|判定|检查|测试|要求|是否|需不需要|要不要)|"
    r"(?:一是|首先).{0,60}(?:二是|其次)|"
    r"[？?].{0,80}[？?]"
)
_NORMALIZE_PUNCTUATION_RE = re.compile(r"[\s，,。．；;：:、！？!?（）()【】\[\]\"'“”‘’]+")
_AI_RESULT_SECTION_RE = re.compile(
    r"[【\[]\s*("
    + "|".join(
        re.escape(alias)
        for alias in sorted(FIELD_ALIASES, key=len, reverse=True)
    )
    + r")\s*[】\]]"
)
_CAMERA_CATEGORIES = {"数码相机", "单反机身", "单电/微单机身"}


def clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "").replace("\u3000", " ")).strip()


def clean_multiline(value: Any) -> str:
    text = str(value or "").replace("\u3000", " ").replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        re.sub(r"[ \t]+", " ", line).strip()
        for line in text.split("\n")
    ).strip()


def normalize_atomic_issue(value: str) -> str:
    """Normalize only whitespace and punctuation for phase-one literal matching."""

    return _NORMALIZE_PUNCTUATION_RE.sub("", clean(value)).casefold()


def normalize_standard_question(category: str, value: str) -> str:
    """Normalize only redundant product nouns, never business intent."""

    standard = clean(value)
    if category in _CAMERA_CATEGORIES:
        standard = standard.replace("机身序列号", "序列号")
    return standard


def expand_laptop_component_verification_issues(
    category: str,
    issue: str,
) -> list[str]:
    """Split the confirmed laptop memory/disk verification compound."""

    text = clean(issue)
    if (
        category not in {"笔记本", "笔记本电脑", "电脑"}
        or "内存" not in text
        or "硬盘" not in text
        or not any(marker in text for marker in ("品牌", "原装", "第三方", "非原装"))
    ):
        return [text] if text else []
    return [
        "内存是否品牌、原装或第三方",
        "硬盘是否品牌、原装或第三方",
    ]


def parse_ai_result(value: Any) -> dict[str, str]:
    if isinstance(value, dict):
        return {
            FIELD_ALIASES.get(clean(key), ""): (
                clean_multiline(item)
                if FIELD_ALIASES.get(clean(key)) == "核心问题"
                else clean(item)
            )
            for key, item in value.items()
            if FIELD_ALIASES.get(clean(key)) and clean(item)
        }
    text = str(value or "").replace("\u3000", " ").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            return parse_ai_result(payload)
    except json.JSONDecodeError:
        pass

    result: dict[str, str] = {}
    # AI reports often place the next field marker immediately after the
    # preceding prose, e.g. `……如何判定。 【产品类型】 手机`.  Requiring a
    # newline here causes the whole later report (classification, conclusion,
    # and reply) to be counted as part of the core question.
    matches = list(_AI_RESULT_SECTION_RE.finditer(text))
    for index, match in enumerate(matches):
        field = FIELD_ALIASES.get(clean(match.group(1)))
        if not field:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        result[field] = (
            clean_multiline(text[match.end() : end])
            if field == "核心问题"
            else clean(text[match.end() : end])
        )
    return result


def parse_source_record(raw: dict[str, Any]) -> dict[str, Any]:
    record = raw.get("record") if isinstance(raw.get("record"), dict) else raw
    ai = parse_ai_result(
        raw.get("ai_result") or record.get("ai_result") or raw.get("aiResult")
    )

    def pick(*names: str) -> str:
        for name in names:
            value = raw.get(name, record.get(name))
            if clean(value):
                return clean(value)
        return ""

    def pick_multiline(*names: str) -> str:
        for name in names:
            value = raw.get(name, record.get(name))
            if clean(value):
                return clean_multiline(value)
        return ""

    category = (
        pick("产品类型", "product_type", "category")
        or ai.get("产品类型")
        or "未分类"
    )
    return {
        "record_id": pick("工单ID", "work_order_id", "ticket_id", "id", "record_id"),
        "analyzed_at": pick("分析时间", "analyze_time", "analyzed_at", "created_at"),
        "category": normalize_product_category(category),
        "level1": pick("一级分类", "category_l1") or ai.get("一级分类", ""),
        "level2": pick("二级分类", "category_l2") or ai.get("二级分类", ""),
        "chat_content": pick("聊天内容", "chat_content", "conversation"),
        "core_problem": ai.get("核心问题")
        or pick_multiline("核心问题", "core_problem"),
        "judgment": ai.get("判定结论") or pick("判定结论", "judgment"),
        "basis": ai.get("判定依据") or pick("判定依据", "judgment_basis"),
        "reference_reply": ai.get("参考话术")
        or pick("参考话术", "reference_reply"),
    }


def split_atomic_issues(core_problem: str) -> tuple[list[str], str]:
    """Split only explicit numbered issue lists without calling a model.

    When the source clearly says it contains multiple issues but has no
    reliable numbered boundary, keep the raw question visible as ``待拆分``.
    """

    # Preserve newlines here. They are often the only trustworthy boundary
    # between `1.` and `2.` in second-part AI output.
    text = str(core_problem or "").replace("\u3000", " ").replace("\r\n", "\n").strip()
    if not text:
        return [], "缺少核心问题"

    matches = list(_ENUMERATED_ISSUE_RE.finditer(text))
    if len(matches) >= 2:
        issues: list[str] = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            issue = clean(text[match.end() : end]).strip("；;。 ")
            if issue:
                issues.append(issue)
        if len(issues) >= 2:
            return issues, "已拆分"

    if _MULTI_ISSUE_HINT_RE.search(text):
        return [text], "待拆分"
    return [text], "单问题"


def _date_value(value: str) -> date | None:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except (ValueError, TypeError):
        try:
            return date.fromisoformat(value[:10])
        except (ValueError, TypeError):
            return None


def _period_start(day: date, period: str) -> date:
    return day - timedelta(days=day.weekday()) if period == "week" else day.replace(day=1)


def _record_key(item: dict[str, Any], fallback_index: int) -> str:
    return item["record_id"] or (
        f"{item['analyzed_at']}|{item['category']}|{item['core_problem']}|{fallback_index}"
    )


def build_dashboard(
    records: list[dict[str, Any]],
    *,
    from_date: str = "",
    to_date: str = "",
    category: str = "",
    level1: str = "",
    level2: str = "",
    business_theme: str = "",
    high_frequency_only: bool = True,
    period: str = "week",
    cached_only: bool = False,
    source_possibly_truncated: bool = False,
    source_request_count: int = 1,
) -> dict[str, Any]:
    """Build phase-one exact statistics with category as a hard boundary."""

    source_records: dict[str, dict[str, Any]] = {}
    for index, raw in enumerate(records):
        item = parse_source_record(raw)
        source_records.setdefault(_record_key(item, index), item)

    start = _date_value(from_date)
    end = _date_value(to_date)
    filtered_records: list[dict[str, Any]] = []
    missing_core_problem_records = 0
    atomic_items: list[dict[str, Any]] = []
    split_candidates: list[dict[str, str]] = []

    for source_key, item in source_records.items():
        analyzed_day = _date_value(item["analyzed_at"])
        if start and (not analyzed_day or analyzed_day < start):
            continue
        if end and (not analyzed_day or analyzed_day > end):
            continue
        if category and item["category"] != category:
            continue
        if level1 and item["level1"] != level1:
            continue
        if level2 and item["level2"] != level2:
            continue

        filtered_records.append(item)
        issues, split_status = split_atomic_issues(item["core_problem"])
        if split_status == "缺少核心问题":
            missing_core_problem_records += 1
            continue
        if split_status == "待拆分":
            split_candidates.append(
                {
                    "id": source_key,
                    "category": item["category"],
                    "level1": item["level1"],
                    "level2": item["level2"],
                    "core_problem": item["core_problem"],
                }
            )
            continue

        expanded_issues = [
            expanded
            for issue in issues
            for expanded in expand_laptop_component_verification_issues(
                item["category"],
                issue,
            )
        ]
        for atomic_index, atomic_issue in enumerate(expanded_issues, 1):
            normalized = normalize_atomic_issue(atomic_issue)
            if normalized:
                atomic_items.append(
                    {
                        **item,
                        "source_key": source_key,
                        "atomic_issue": atomic_issue,
                        "atomic_issue_key": normalized,
                        "atomic_index": atomic_index,
                        "split_status": split_status,
                    }
                )

    if cached_only:
        split_results, uncached_split_count = get_cached_atomic_splits(
            split_candidates
        )
        split_error = None
    else:
        split_results, split_error = split_ambiguous_questions(split_candidates)
        uncached_split_count = 0

    pending_split_cases: list[dict[str, Any]] = []
    for candidate in split_candidates:
        item = source_records[candidate["id"]]
        result = split_results.get(candidate["id"])
        if result is None:
            split_status = (
                "拆分失败待核对" if split_error else "等待DeepSeek拆分"
            )
            pending_split_cases.append(
                {
                    "工单ID": item["record_id"] or "-",
                    "分析时间": item["analyzed_at"] or "-",
                    "品类": item["category"],
                    "原始问题": item["chat_content"] or "-",
                    "AI分析问题": item["core_problem"],
                    "原始AI核心问题": item["core_problem"],
                    "拆分状态": split_status,
                    "判定结论": item["judgment"] or "-",
                    "判定依据": item["basis"] or "-",
                    "参考话术": item["reference_reply"] or "",
                }
            )
            continue

        split_questions = result.get("questions", [])
        if result.get("needs_review") or not split_questions:
            pending_split_cases.append(
                {
                    "工单ID": item["record_id"] or "-",
                    "分析时间": item["analyzed_at"] or "-",
                    "品类": item["category"],
                    "原始问题": item["chat_content"] or "-",
                    "AI分析问题": item["core_problem"],
                    "原始AI核心问题": item["core_problem"],
                    "拆分状态": "AI标记需人工核对",
                    "判定结论": item["judgment"] or "-",
                    "判定依据": item["basis"] or "-",
                    "参考话术": item["reference_reply"] or "",
                }
            )
            continue

        expanded_questions = [
            expanded
            for question in split_questions
            for expanded in expand_laptop_component_verification_issues(
                item["category"],
                question,
            )
        ]
        for atomic_index, atomic_issue in enumerate(expanded_questions, 1):
            normalized = normalize_atomic_issue(atomic_issue)
            if normalized:
                atomic_items.append(
                    {
                        **item,
                        "source_key": candidate["id"],
                        "atomic_issue": atomic_issue,
                        "atomic_issue_key": normalized,
                        "atomic_index": atomic_index,
                        "split_status": (
                            "DeepSeek确认单问题"
                            if len(split_questions) == 1
                            else "DeepSeek已拆分"
                        ),
                    }
                )

    # A work order can contribute multiple distinct atomic issues, but an
    # identical atomic issue from the same work order counts only once.
    unique_atomic: dict[tuple[str, str], dict[str, Any]] = {}
    for item in atomic_items:
        unique_atomic.setdefault(
            (item["source_key"], item["atomic_issue_key"]),
            item,
        )
    atomic_items = list(unique_atomic.values())

    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for item in atomic_items:
        # Category is intentionally part of this key. Cross-category merge is forbidden.
        groups[(item["category"], item["atomic_issue_key"])].append(item)

    exact_groups: dict[tuple[str, str], list[dict[str, Any]]] = groups
    classification_inputs = [
        {
            "category": items[0]["category"],
            "issue": items[0]["atomic_issue"],
            "level1": items[0]["level1"],
            "level2": items[0]["level2"],
        }
        for items in exact_groups.values()
    ]
    if cached_only:
        classifications, uncached_classification_count = (
            get_cached_classifications(classification_inputs)
        )
        classification_error = None
    else:
        classifications, classification_error = classify_questions(
            classification_inputs
        )
        uncached_classification_count = 0

    # Model normalization is only used within an existing same-category
    # repeated-question candidate. A missing/failed model call safely falls
    # back to the exact phase-one key instead of inventing a merge.
    normalized_groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for exact_key, items in exact_groups.items():
        first = items[0]
        cache_key = _cache_key(
            first["category"],
            first["atomic_issue"],
            first["level1"],
            first["level2"],
        )
        classification = classifications.get(cache_key, {})
        if isinstance(classification, dict):
            standard = clean(
                classification.get("standard_question", "")
            ) or first["atomic_issue"]
            theme = clean(classification.get("business_theme", ""))
        else:
            # Compatibility with callers/tests that still provide title-only
            # model output. The production classifier returns both fields.
            standard = clean(classification) or first["atomic_issue"]
            theme = ""
        standard = normalize_standard_question(first["category"], standard)
        if not theme:
            theme = "待人工核对"
        standard_key = normalize_atomic_issue(standard)
        for item in items:
            normalized_groups[(item["category"], standard_key)].append(
                {
                    **item,
                    "business_theme": theme,
                    "standard_question": standard,
                }
            )

    for group_key in list(normalized_groups):
        items = normalized_groups[group_key]
        unique_by_source: dict[str, dict[str, Any]] = {}
        for item in items:
            source_item = unique_by_source.get(item["source_key"])
            if source_item is None:
                unique_by_source[item["source_key"]] = {
                    **item,
                    "source_atomic_issues": [item["atomic_issue"]],
                }
                continue
            if item["atomic_issue"] not in source_item["source_atomic_issues"]:
                source_item["source_atomic_issues"].append(item["atomic_issue"])
        normalized_groups[group_key] = list(unique_by_source.values())

    groups = normalized_groups
    all_rows: list[dict[str, Any]] = []
    total_atomic_issues = len(atomic_items)
    for (row_category, _), items in groups.items():
        count = len(items)
        representative = min(items, key=lambda item: item["atomic_issue"])
        standard_question = sorted(
            Counter(item["standard_question"] for item in items).items(),
            key=lambda item: (-item[1], len(item[0]), item[0]),
        )[0][0]
        row_business_theme = sorted(
            Counter(item["business_theme"] for item in items).items(),
            key=lambda item: (-item[1], len(item[0]), item[0]),
        )[0][0]
        buckets = sorted(
            {
                _period_start(
                    _date_value(item["analyzed_at"]) or date.today(),
                    period,
                ).isoformat()
                for item in items
            }
        )
        cases = [
            {
                "工单ID": item["record_id"] or "-",
                "分析时间": item["analyzed_at"] or "-",
                "品类": item["category"],
                "业务主题": item["business_theme"],
                "原始问题": item["chat_content"] or "-",
                "AI分析问题": "；".join(
                    item.get("source_atomic_issues", [item["atomic_issue"]])
                ),
                "原始AI核心问题": item["core_problem"],
                "原子问题": item["atomic_issue"],
                "拆分状态": item["split_status"],
                "判定结论": item["judgment"] or "-",
                "判定依据": item["basis"] or "-",
                "参考话术": item["reference_reply"] or "",
            }
            for item in sorted(items, key=lambda item: item["analyzed_at"], reverse=True)
        ]
        all_rows.append(
            {
                "品类": row_category,
                "业务主题": row_business_theme,
                "原子问题": standard_question,
                "出现次数": count,
                "出现占比": round(count / total_atomic_issues, 4)
                if total_atomic_issues
                else 0,
                "工单数": len({item["source_key"] for item in items}),
                "有话术": (
                    f"{sum(bool(item['参考话术']) for item in cases)}/{len(cases)}"
                ),
                "统计周期": "、".join(buckets),
                "最近出现时间": max(
                    (item["analyzed_at"] for item in items if item["analyzed_at"]),
                    default="-",
                ),
                "案例": cases,
            }
        )

    business_theme_options = sorted(
        {row["业务主题"] for row in all_rows if row["业务主题"]}
    )
    if business_theme:
        all_rows = [
            row for row in all_rows
            if row["业务主题"] == business_theme
        ]

    selected_atomic_issue_count = sum(
        row["出现次数"] for row in all_rows
    )
    if selected_atomic_issue_count:
        for row in all_rows:
            row["出现占比"] = round(
                row["出现次数"] / selected_atomic_issue_count,
                4,
            )
    selected_record_ids = {
        case["工单ID"]
        for row in all_rows
        for case in row["案例"]
        if case["工单ID"] != "-"
    }
    high_frequency_count = sum(
        row["出现次数"] >= 2 for row in all_rows
    )
    standard_question_count = len(all_rows)
    single_occurrence_count = sum(
        row["出现次数"] == 1 for row in all_rows
    )
    rows = [
        row for row in all_rows
        if not high_frequency_only or row["出现次数"] >= 2
    ]
    rows.sort(key=lambda item: (-item["出现次数"], item["品类"], item["原子问题"]))
    for index, row in enumerate(rows, 1):
        row["排名"] = index

    selected_standard_keys = {
        (row["品类"], normalize_atomic_issue(row["原子问题"]))
        for row in all_rows
    }
    trend: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"atomic_count": 0, "work_orders": set()}
    )
    for group_key, items in groups.items():
        if group_key not in selected_standard_keys:
            continue
        for item in items:
            analyzed_day = _date_value(item["analyzed_at"])
            if not analyzed_day:
                continue
            bucket = _period_start(analyzed_day, period).isoformat()
            trend[bucket]["atomic_count"] += 1
            trend[bucket]["work_orders"].add(item["source_key"])

    return {
        "summary": {
            "total_records": (
                len(selected_record_ids)
                if business_theme else len(filtered_records)
            ),
            "atomic_issue_count": selected_atomic_issue_count,
            "pending_split_records": len(pending_split_cases),
            "missing_core_problem_records": (
                0 if business_theme else missing_core_problem_records
            ),
            "standard_question_count": standard_question_count,
            "high_frequency_count": high_frequency_count,
            "single_occurrence_count": single_occurrence_count,
            "categories": len(
                {item["category"] for item in filtered_records if item["category"]}
            ),
        },
        "categories": sorted(
            {item["category"] for item in source_records.values() if item["category"]}
        ),
        "level1_options": sorted(
            {item["level1"] for item in filtered_records if item["level1"]}
        ),
        "level2_options": sorted(
            {item["level2"] for item in filtered_records if item["level2"]}
        ),
        "business_theme_options": business_theme_options,
        "rows": rows,
            "pending_cases": pending_split_cases,
        "trend": [
            {
                "period": bucket,
                "atomic_count": trend[bucket]["atomic_count"],
                "work_order_count": len(trend[bucket]["work_orders"]),
            }
            for bucket in sorted(trend)
        ],
        "meta": {
            "query_limit": settings.SECOND_PART_API_QUERY_LIMIT,
            "possibly_truncated": source_possibly_truncated,
            "source_request_count": source_request_count,
            "grouping_mode": "same_category_standard_question",
            "standard_question_mode": "deepseek_same_category_all_atomic_issues",
            "classification_error": classification_error or "",
            "split_error": split_error or "",
            "split_pending": bool(uncached_split_count),
            "uncached_split_count": uncached_split_count,
            "classification_pending": bool(
                uncached_classification_count or uncached_split_count
            ),
            "uncached_classification_count": uncached_classification_count,
            "needs_manual_review": bool(pending_split_cases),
            "quality_status": (
                "processing"
                if uncached_classification_count or uncached_split_count
                else "failed"
                if classification_error or split_error
                else "review"
                if pending_split_cases
                else "ready"
            ),
            "high_frequency_only": high_frequency_only,
        },
    }
