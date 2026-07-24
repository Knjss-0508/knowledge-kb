from __future__ import annotations

import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
from typing import Any

from answer_hub.mimo import (
    CLUSTER_UNIT_PROMPT_VERSION,
    MimoClient,
    MimoError,
    MimoLabelResult,
    _primary_conversation_evidence,
)
from answer_hub.workflow import _direct_mimo_topic_groups


DEFAULT_OLD_THRESHOLD = 0.24
DEFAULT_NEW_THRESHOLD = 0.04
TEXT_ONLY_PROMPT_VERSION = f"{CLUSTER_UNIT_PROMPT_VERSION}-text-only-pro-v1"
GENERIC_SCOPE_VALUES = {"", "通用", "不限", "全部", "待确认", "未知", "无"}
INVALID_PRODUCT_CATEGORY_VALUES = {"", "待确认", "未知", "无"}
SCOPE_LEVELS = {
    "通用": 0,
    "品类专用": 1,
    "平台专用": 2,
    "品牌专用": 3,
    "机型专用": 4,
}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _tokens(text: str) -> list[str]:
    normalized = re.sub(r"\s+", "", str(text or "").lower())
    characters = [
        character
        for character in normalized
        if "\u4e00" <= character <= "\u9fff" or character.isalnum()
    ]
    tokens: list[str] = []
    for size in (2, 3, 4):
        tokens.extend(
            "".join(characters[index : index + size])
            for index in range(max(0, len(characters) - size + 1))
        )
    tokens.extend(re.findall(r"[a-z0-9]{2,}", normalized))
    return tokens or [normalized or "空"]


def _tfidf_vectors(texts: list[str]) -> list[dict[str, float]]:
    tokenized = [_tokens(text) for text in texts]
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    document_count = len(tokenized)
    vectors: list[dict[str, float]] = []
    for tokens in tokenized:
        counts = Counter(tokens)
        token_count = max(1, len(tokens))
        vector = {
            token: (count / token_count)
            * (math.log((1 + document_count) / (1 + document_frequency[token])) + 1)
            for token, count in counts.items()
        }
        norm = math.sqrt(sum(value * value for value in vector.values()))
        vectors.append(
            {token: value / norm for token, value in vector.items()}
            if norm
            else vector
        )
    return vectors


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if len(left) > len(right):
        left, right = right, left
    return sum(value * right.get(token, 0.0) for token, value in left.items())


def _similarity_matrix(vectors: list[dict[str, float]]) -> list[list[float]]:
    count = len(vectors)
    matrix = [[0.0] * count for _ in range(count)]
    for index in range(count):
        matrix[index][index] = 1.0
        for other in range(index + 1, count):
            value = _cosine(vectors[index], vectors[other])
            matrix[index][other] = value
            matrix[other][index] = value
    return matrix


def _cluster_average_linkage(
    similarities: list[list[float]],
    threshold: float,
    compatibility: list[list[bool]] | None = None,
) -> list[int]:
    clusters: list[list[int]] = [[index] for index in range(len(similarities))]
    while True:
        best_pair: tuple[int, int] | None = None
        best_score = threshold
        for left_index in range(len(clusters)):
            for right_index in range(left_index + 1, len(clusters)):
                cross_pairs = [
                    (left, right)
                    for left in clusters[left_index]
                    for right in clusters[right_index]
                ]
                if compatibility is not None and any(
                    not compatibility[left][right]
                    for left, right in cross_pairs
                ):
                    continue
                scores = [
                    similarities[left][right]
                    for left, right in cross_pairs
                ]
                score = sum(scores) / len(scores)
                if score > best_score:
                    best_score = score
                    best_pair = (left_index, right_index)
        if best_pair is None:
            break
        left_index, right_index = best_pair
        clusters[left_index].extend(clusters[right_index])
        del clusters[right_index]

    assignments = [-1] * len(similarities)
    ordered_clusters = sorted(clusters, key=lambda cluster: min(cluster))
    for cluster_index, cluster in enumerate(ordered_clusters, start=1):
        for item_index in cluster:
            assignments[item_index] = cluster_index
    return assignments


def _old_semantic_text(row: dict[str, Any]) -> str:
    return str(row.get("聊天内容", "")).strip()


def _new_semantic_text(topic: dict[str, Any]) -> str:
    fields = [
        "normalized_issue",
        "product_category",
        "scope_type",
        "category_l1",
        "category_l2",
        "intent",
        "subject",
        "phenomenon",
        "judgment_target",
        "resolution_mode",
        "standard_path",
        "threshold_or_exception",
    ]
    scope_level = SCOPE_LEVELS.get(str(topic.get("scope_type", "")).strip(), -1)
    if scope_level >= 2:
        fields.append("platform")
    if scope_level >= 3:
        fields.append("brand")
    if scope_level >= 4:
        fields.append("model_scope")
    return "\n".join(
        str(topic.get(field, "")).strip()
        for field in fields
        if str(topic.get(field, "")).strip()
    )


def _infer_platform(model: Any) -> str:
    text = str(model or "").strip().lower()
    if any(marker in text for marker in ("iphone", "ipad", "apple", "苹果")):
        return "iOS"
    if any(
        marker in text
        for marker in (
            "android",
            "小米",
            "红米",
            "华为",
            "荣耀",
            "oppo",
            "vivo",
            "三星",
            "一加",
            "realme",
            "努比亚",
            "魅族",
        )
    ):
        return "Android"
    return "待确认"


def _infer_brand(model: Any) -> str:
    text = str(model or "").strip()
    lowered = text.lower()
    brand_markers = (
        ("Apple", ("iphone", "ipad", "mac", "苹果")),
        ("小米", ("小米", "红米", "redmi")),
        ("华为", ("华为", "huawei")),
        ("荣耀", ("荣耀", "honor")),
        ("OPPO", ("oppo",)),
        ("vivo", ("vivo", "iqoo")),
        ("三星", ("三星", "samsung")),
        ("一加", ("一加", "oneplus")),
        ("realme", ("realme", "真我")),
        ("努比亚", ("努比亚", "红魔", "nubia")),
    )
    for brand, markers in brand_markers:
        if any(marker in lowered for marker in markers):
            return brand
    return "待确认"


def _fallback_scope_fields(row: dict[str, Any]) -> dict[str, Any]:
    product_type = str(row.get("产品类型", "")).strip() or "待确认"
    model = str(row.get("机型", "")).strip()
    platform = _infer_platform(model)
    brand = _infer_brand(model)
    scope_type = "品类专用"
    if model:
        scope_type = "机型专用"
    elif platform != "待确认":
        scope_type = "平台专用"
    return {
        "product_category": product_type,
        "scope_type": scope_type,
        "platform": platform,
        "brand": brand,
        "model_scope": model or "待确认",
        "category_l1": str(row.get("一级分类", "")).strip() or "待确认",
        "category_l2": str(row.get("二级分类", "")).strip() or "待确认",
        "judgment_target": str(row.get("核心问题", "")).strip()[:160] or "待确认",
        "standard_path": "待确认",
        "threshold_or_exception": "待确认",
        "requires_review": True,
    }


def _normalized_scope_value(value: Any) -> str:
    return re.sub(r"[\s/／|｜、，,;；:：()\[\]【】]+", "", str(value or "").strip()).lower()


def _is_generic_scope_value(value: Any) -> bool:
    return str(value or "").strip().lower() in {
        item.lower()
        for item in GENERIC_SCOPE_VALUES
    }


def _product_categories_match(
    left: dict[str, Any],
    right: dict[str, Any],
) -> bool:
    left_value = str(left.get("product_category", "")).strip()
    right_value = str(right.get("product_category", "")).strip()
    if (
        left_value in INVALID_PRODUCT_CATEGORY_VALUES
        or right_value in INVALID_PRODUCT_CATEGORY_VALUES
    ):
        return False
    return _normalized_scope_value(left_value) == _normalized_scope_value(
        right_value
    )


def _field_conflicts(left: dict[str, Any], right: dict[str, Any], field: str) -> bool:
    left_value = left.get(field)
    right_value = right.get(field)
    if _is_generic_scope_value(left_value) or _is_generic_scope_value(right_value):
        return False
    return _normalized_scope_value(left_value) != _normalized_scope_value(right_value)


def _normalize_unit_scope(unit: dict[str, Any]) -> None:
    scope_type = str(unit.get("scope_type", "")).strip()
    scope_level = SCOPE_LEVELS.get(scope_type, -1)
    if scope_level < 2:
        unit["platform"] = "通用"
    if scope_level < 3:
        unit["brand"] = "通用"
    if scope_level < 4:
        unit["model_scope"] = "通用"

    semantic_text = " ".join(
        str(unit.get(field, "") or "")
        for field in (
            "normalized_issue",
            "phenomenon",
            "judgment_target",
            "resolution_mode",
        )
    )
    if (
        any(marker in semantic_text for marker in ("全新机", "二手", "成色定级"))
        and unit.get("category_l1") == "基本情况"
    ):
        unit["category_l1"] = "成色与回收标准"


def _units_hard_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    if not _product_categories_match(left, right):
        return False
    left_scope = str(left.get("scope_type", "")).strip()
    right_scope = str(right.get("scope_type", "")).strip()
    left_level = SCOPE_LEVELS.get(left_scope, -1)
    right_level = SCOPE_LEVELS.get(right_scope, -1)
    if left_level >= 0 and right_level >= 0 and left_scope != right_scope:
        return False
    if min(left_level, right_level) >= 2 and _field_conflicts(left, right, "platform"):
        return False
    if (
        min(left_level, right_level) >= 3
        and _field_conflicts(left, right, "brand")
    ):
        return False
    if (
        min(left_level, right_level) >= 4
        and _field_conflicts(left, right, "model_scope")
    ):
        return False
    for field in ("category_l1", "category_l2", "intent", "standard_path"):
        if _field_conflicts(left, right, field):
            return False
    left_exception = str(left.get("threshold_or_exception", "")).strip()
    right_exception = str(right.get("threshold_or_exception", "")).strip()
    if (
        not _is_generic_scope_value(left_exception)
        and not _is_generic_scope_value(right_exception)
        and _normalized_scope_value(left_exception)
        != _normalized_scope_value(right_exception)
    ):
        return False
    return True


def _invalid_source_reason(row: dict[str, Any]) -> str:
    core_problem = str(row.get("核心问题", "")).strip()
    conclusion = str(row.get("判定结论", "")).strip()
    basis = str(row.get("判定依据", "")).strip()
    evidence = "\n".join(
        (
            core_problem,
            conclusion,
            basis,
            str(row.get("上游媒体分析摘要", "")).strip(),
        )
    )
    missing_dialogue = (
        "仅包含工单元数据" in evidence
        and "历史咨询会话记录" in evidence
        and "缺失" in evidence
    ) or (
        "未提供一线回收师与后台答疑人员的聊天记录原文" in evidence
        and "无法提取" in evidence
    )
    no_judgment = any(
        marker in conclusion
        for marker in (
            "无法基于现有信息作出判定",
            "无法根据现有信息作出判定",
            "无法作出判定",
        )
    )
    if missing_dialogue and no_judgment:
        return "缺少有效咨询会话和具体问题，只有工单元数据，无法形成主题"
    return ""


def _fallback_units(row: dict[str, Any], error: str) -> dict[str, Any]:
    conversation_evidence = _primary_conversation_evidence(
        row.get("聊天内容"),
        300,
    )
    media_summary = str(row.get("上游媒体分析摘要", "")).strip()
    scope_fields = _fallback_scope_fields(row)
    return {
        "conversation_type": "uncertain",
        "reason": f"MiMo 问题单元提取失败，需人工确认：{error[:180]}",
        "topics": [
            {
                "normalized_issue": "问题待人工确认",
                **scope_fields,
                "intent": "其他待确认",
                "subject": "待确认",
                "phenomenon": "待确认",
                "resolution_mode": "转人工确认",
                "evidence_summary": (
                    conversation_evidence
                    or media_summary
                    or "缺少可靠聊天与媒体证据"
                )[:300],
                "confidence": 0.0,
                "requires_review": True,
            }
        ],
    }


def _analyze_row(
    client: MimoClient,
    row: dict[str, Any],
    *,
    text_only: bool = False,
) -> dict[str, Any]:
    source_row = dict(row)
    prompt_version = CLUSTER_UNIT_PROMPT_VERSION
    if text_only:
        source_row["图片链接"] = ""
        source_row["视频链接"] = ""
        source_row["上游媒体分析摘要"] = ""
        prompt_version = TEXT_ONLY_PROMPT_VERSION
    try:
        result = client.analyze_cluster_units(source_row)
        return {
            "status": "ok",
            "candidate": result.candidate,
            "model": result.request_audit.get("model", client.config.model),
            "configured_text_model": client.config.model,
            "configured_media_model": client.config.media_model,
            "media": result.request_audit.get("media", {}),
            "prompt_version": prompt_version,
            "analysis_mode": "text_only" if text_only else "multimodal",
        }
    except MimoError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "candidate": _fallback_units(row, str(exc)),
            "model": client.config.model,
            "configured_text_model": client.config.model,
            "configured_media_model": client.config.media_model,
            "media": {"mode": "failed", "images": [], "videos": []},
            "prompt_version": prompt_version,
            "analysis_mode": "text_only" if text_only else "multimodal",
        }


def _cache_entry_is_current(
    row: dict[str, Any],
    cache: dict[str, Any],
    *,
    text_only: bool = False,
) -> bool:
    entry = cache.get(row["样本ID"])
    expected_prompt_version = (
        TEXT_ONLY_PROMPT_VERSION
        if text_only
        else CLUSTER_UNIT_PROMPT_VERSION
    )
    return bool(
        isinstance(entry, dict)
        and entry.get("prompt_version") == expected_prompt_version
    )


def _cache_entry_needs_refresh(
    row: dict[str, Any],
    cache: dict[str, Any],
    *,
    retry_errors: bool = False,
    text_only: bool = False,
) -> bool:
    if not _cache_entry_is_current(row, cache, text_only=text_only):
        return True
    return bool(
        retry_errors
        and cache[row["样本ID"]].get("status") == "error"
    )


@dataclass
class SchemeResult:
    units: list[dict[str, Any]]
    assignments: dict[str, int]
    similarities: dict[tuple[str, str], float]


def _run_old_scheme(rows: list[dict[str, Any]], threshold: float) -> SchemeResult:
    units = [
        {
            "unit_id": f"{row['样本ID']}-01",
            "sample_id": row["样本ID"],
            "conversation_type": "single_topic",
            "normalized_issue": str(row.get("核心问题", "")).strip(),
            "semantic_text": _old_semantic_text(row),
            "reason": "旧流程仅使用清洗后的聊天记录直接聚类。",
            "confidence": "",
        }
        for row in rows
    ]
    return _cluster_units(units, threshold)


def _run_new_scheme(
    rows: list[dict[str, Any]],
    cache: dict[str, Any],
    threshold: float,
) -> SchemeResult:
    units: list[dict[str, Any]] = []
    for row in rows:
        sample_id = row["样本ID"]
        candidate = cache[sample_id]["candidate"]
        topics = candidate.get("topics") or []
        if not topics:
            topics = _fallback_units(row, "模型未返回问题单元")["topics"]
        for topic_index, topic in enumerate(topics, start=1):
            unit = {
                "unit_id": f"{sample_id}-{topic_index:02d}",
                "sample_id": sample_id,
                "conversation_type": candidate.get("conversation_type", "uncertain"),
                "normalized_issue": topic.get("normalized_issue", ""),
                "product_category": topic.get(
                    "product_category",
                    row.get("产品类型", "待确认"),
                ),
                "scope_type": topic.get("scope_type", "待确认"),
                "platform": topic.get("platform", "待确认"),
                "brand": topic.get("brand", "待确认"),
                "model_scope": topic.get(
                    "model_scope",
                    row.get("机型", "待确认"),
                ),
                "category_l1": topic.get("category_l1", "待确认"),
                "category_l2": topic.get("category_l2", "待确认"),
                "intent": topic.get("intent", ""),
                "subject": topic.get("subject", ""),
                "phenomenon": topic.get("phenomenon", ""),
                "judgment_target": topic.get(
                    "judgment_target",
                    topic.get("normalized_issue", ""),
                ),
                "resolution_mode": topic.get("resolution_mode", ""),
                "standard_path": topic.get("standard_path", "待确认"),
                "threshold_or_exception": topic.get(
                    "threshold_or_exception",
                    "待确认",
                ),
                "evidence_summary": topic.get("evidence_summary", ""),
                "reason": candidate.get("reason", ""),
                "confidence": topic.get("confidence", ""),
                "requires_review": topic.get("requires_review", False),
            }
            _normalize_unit_scope(unit)
            unit["semantic_text"] = _new_semantic_text(unit)
            units.append(unit)
    return _cluster_units(units, threshold, enforce_business_rules=True)


class _CachedAtomicAnalysisReviewer:
    def __init__(
        self,
        reviewer: MimoClient,
        cache: dict[str, Any],
    ) -> None:
        self._reviewer = reviewer
        self._cache = cache
        self.config = reviewer.config

    def analyze_cluster_units(
        self,
        row: dict[str, Any],
    ) -> MimoLabelResult:
        sample_id = str(
            row.get("数据ID")
            or row.get("样本ID")
            or row.get("工单ID")
            or ""
        ).strip()
        entry = self._cache.get(sample_id)
        if not isinstance(entry, dict) or not isinstance(
            entry.get("candidate"),
            dict,
        ):
            raise MimoError(f"缺少样本 {sample_id} 的原子问题缓存")
        return MimoLabelResult(
            candidate=entry["candidate"],
            request_audit={"source": "cached_cluster_ab_analysis"},
            response_audit={},
        )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._reviewer, name)


def _run_new_scheme_direct_mimo(
    rows: list[dict[str, Any]],
    cache: dict[str, Any],
    reviewer: MimoClient,
) -> tuple[SchemeResult, dict[str, Any]]:
    input_rows = [
        {
            **row,
            "数据ID": row["样本ID"],
        }
        for row in rows
    ]
    topic_groups, meta = _direct_mimo_topic_groups(
        input_rows,
        _CachedAtomicAnalysisReviewer(reviewer, cache),
    )

    units: list[dict[str, Any]] = []
    assignments: dict[str, int] = {}
    for cluster_index, (_key, member_rows) in enumerate(
        topic_groups,
        start=1,
    ):
        cluster_id = f"C{cluster_index:03d}"
        for member in member_rows:
            sample_id = str(member.get("数据ID") or "").strip()
            atomic_id = str(member.get("_原子知识ID") or "").strip()
            conversation_type = str(
                cache.get(sample_id, {})
                .get("candidate", {})
                .get("conversation_type", "uncertain")
            )
            unit = {
                "unit_id": atomic_id,
                "sample_id": sample_id,
                "conversation_type": conversation_type,
                "normalized_issue": str(member.get("核心问题") or ""),
                "product_category": str(member.get("产品类型") or ""),
                "scope_type": str(member.get("_原子适用范围类型") or ""),
                "platform": str(member.get("_原子平台") or ""),
                "brand": str(member.get("_原子品牌") or ""),
                "model_scope": str(member.get("_原子机型范围") or ""),
                "category_l1": str(member.get("模型主题一级分类") or ""),
                "category_l2": str(member.get("模型主题二级分类") or ""),
                "intent": str(member.get("问题意图") or ""),
                "subject": str(member.get("对象/部位") or ""),
                "phenomenon": str(member.get("异常现象") or ""),
                "resolution_mode": str(member.get("解题方式") or ""),
                "standard_path": str(member.get("主标准路径") or ""),
                "threshold_or_exception": str(
                    member.get("_原子阈值例外") or ""
                ),
                "evidence_summary": str(member.get("语义标注依据") or ""),
                "source_conversation": str(member.get("聊天内容") or ""),
                "cluster_id": cluster_id,
            }
            units.append(unit)
            assignments[atomic_id] = cluster_index

    similarities: dict[tuple[str, str], float] = {}
    for left_index, left in enumerate(units):
        for right in units[left_index + 1 :]:
            similarities[(left["unit_id"], right["unit_id"])] = (
                1.0
                if left["cluster_id"] == right["cluster_id"]
                else 0.0
            )
    return (
        SchemeResult(
            units=units,
            assignments=assignments,
            similarities=similarities,
        ),
        meta,
    )


def _cluster_units(
    units: list[dict[str, Any]],
    threshold: float,
    enforce_business_rules: bool = False,
) -> SchemeResult:
    if enforce_business_rules:
        for unit in units:
            _normalize_unit_scope(unit)
            unit["semantic_text"] = _new_semantic_text(unit)
    vectors = _tfidf_vectors([unit["semantic_text"] for unit in units])
    matrix = _similarity_matrix(vectors)
    compatibility: list[list[bool]] | None = None
    if enforce_business_rules:
        compatibility = [
            [
                left_index == right_index
                or _units_hard_compatible(units[left_index], units[right_index])
                for right_index in range(len(units))
            ]
            for left_index in range(len(units))
        ]
        for left_index in range(len(units)):
            for right_index in range(left_index + 1, len(units)):
                if not compatibility[left_index][right_index]:
                    matrix[left_index][right_index] = 0.0
                    matrix[right_index][left_index] = 0.0
    cluster_indexes = _cluster_average_linkage(
        matrix,
        threshold,
        compatibility=compatibility,
    )
    assignments: dict[str, int] = {}
    for unit, cluster_index in zip(units, cluster_indexes):
        unit["cluster_id"] = f"C{cluster_index:03d}"
        assignments[unit["unit_id"]] = cluster_index
    similarities: dict[tuple[str, str], float] = {}
    for left_index, left in enumerate(units):
        for right_index in range(left_index + 1, len(units)):
            right = units[right_index]
            similarities[(left["unit_id"], right["unit_id"])] = matrix[left_index][right_index]
    return SchemeResult(units=units, assignments=assignments, similarities=similarities)


def _source_state(
    scheme: SchemeResult,
    rows: list[dict[str, Any]],
    new_scheme: bool,
) -> dict[str, dict[str, Any]]:
    grouped_units: dict[str, list[dict[str, Any]]] = {}
    for unit in scheme.units:
        grouped_units.setdefault(unit["sample_id"], []).append(unit)
    states: dict[str, dict[str, Any]] = {}
    for row in rows:
        sample_id = row["样本ID"]
        units = grouped_units[sample_id]
        conversation_type = units[0].get("conversation_type", "single_topic")
        states[sample_id] = {
            "conversation_type": conversation_type if new_scheme else "single_topic",
            "cluster_ids": [unit["cluster_id"] for unit in units],
            "unit_ids": [unit["unit_id"] for unit in units],
        }
    return states


def _pair_similarity(
    scheme: SchemeResult,
    left_units: list[str],
    right_units: list[str],
) -> float:
    values = [
        scheme.similarities.get(
            (left, right) if (left, right) in scheme.similarities else (right, left),
            1.0 if left == right else 0.0,
        )
        for left in left_units
        for right in right_units
    ]
    return max(values) if values else 0.0


def _prediction(
    left: dict[str, Any],
    right: dict[str, Any],
    allow_multi_topic: bool,
) -> str:
    if allow_multi_topic:
        if "multi_topic" in {left["conversation_type"], right["conversation_type"]}:
            return "多主题需拆分"
        if "uncertain" in {left["conversation_type"], right["conversation_type"]}:
            return "不确定"
    return (
        "同一主题"
        if set(left["cluster_ids"]) & set(right["cluster_ids"])
        else "不同主题"
    )


def _stable_rank(value: str) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()


def _select_pairs(
    rows: list[dict[str, Any]],
    old_scheme: SchemeResult,
    new_scheme: SchemeResult,
    limit: int = 60,
) -> list[dict[str, Any]]:
    old_states = _source_state(old_scheme, rows, new_scheme=False)
    new_states = _source_state(new_scheme, rows, new_scheme=True)
    candidates: list[dict[str, Any]] = []
    for left_index, left_row in enumerate(rows):
        for right_row in rows[left_index + 1 :]:
            left_id = left_row["样本ID"]
            right_id = right_row["样本ID"]
            old_similarity = _pair_similarity(
                old_scheme,
                old_states[left_id]["unit_ids"],
                old_states[right_id]["unit_ids"],
            )
            new_similarity = _pair_similarity(
                new_scheme,
                new_states[left_id]["unit_ids"],
                new_states[right_id]["unit_ids"],
            )
            old_prediction = _prediction(
                old_states[left_id],
                old_states[right_id],
                allow_multi_topic=False,
            )
            new_prediction = _prediction(
                new_states[left_id],
                new_states[right_id],
                allow_multi_topic=True,
            )
            if old_prediction != new_prediction:
                stratum = "方案分歧"
            elif old_prediction == "同一主题":
                stratum = "共同合并"
            else:
                stratum = "共同拆分"
            candidates.append(
                {
                    "left_id": left_id,
                    "right_id": right_id,
                    "old_prediction": old_prediction,
                    "new_prediction": new_prediction,
                    "old_similarity": round(old_similarity, 4),
                    "new_similarity": round(new_similarity, 4),
                    "stratum": stratum,
                    "rank": _stable_rank(f"{left_id}|{right_id}"),
                }
            )

    selected: list[dict[str, Any]] = []
    selected_keys: set[tuple[str, str]] = set()
    source_counts: Counter[str] = Counter()

    def add_from(pool: list[dict[str, Any]], target: int) -> None:
        for candidate in pool:
            if len(selected) >= target:
                break
            key = (candidate["left_id"], candidate["right_id"])
            if key in selected_keys:
                continue
            if source_counts[candidate["left_id"]] >= 5 or source_counts[candidate["right_id"]] >= 5:
                continue
            selected.append(candidate)
            selected_keys.add(key)
            source_counts.update(key)

    disagreements = sorted(
        (item for item in candidates if item["stratum"] == "方案分歧"),
        key=lambda item: (-max(item["old_similarity"], item["new_similarity"]), item["rank"]),
    )
    common_same = sorted(
        (item for item in candidates if item["stratum"] == "共同合并"),
        key=lambda item: (-max(item["old_similarity"], item["new_similarity"]), item["rank"]),
    )
    common_different = sorted(
        (item for item in candidates if item["stratum"] == "共同拆分"),
        key=lambda item: (-max(item["old_similarity"], item["new_similarity"]), item["rank"]),
    )
    add_from(disagreements, min(limit, 30))
    add_from(common_same, min(limit, len(selected) + 15))
    add_from(common_different, limit)
    if len(selected) < limit:
        add_from(
            sorted(candidates, key=lambda item: item["rank"]),
            limit,
        )

    selected = selected[:limit]
    for index, pair in enumerate(selected, start=1):
        pair["pair_id"] = f"P{index:03d}"
        pair.pop("rank", None)
    return selected


def _finalize(
    rows: list[dict[str, Any]],
    cache: dict[str, Any],
    output_path: Path,
    old_threshold: float,
    new_threshold: float,
    excluded_rows: list[dict[str, str]] | None = None,
    new_cluster_mode: str = "tfidf",
    reviewer: MimoClient | None = None,
) -> None:
    excluded_rows = excluded_rows or []
    old_scheme = _run_old_scheme(rows, old_threshold)
    direct_meta: dict[str, Any] = {}
    if new_cluster_mode == "direct_mimo":
        if reviewer is None:
            raise RuntimeError("生产版MiMo聚类需要已配置MiMo")
        new_scheme, direct_meta = _run_new_scheme_direct_mimo(
            rows,
            cache,
            reviewer,
        )
    else:
        new_scheme = _run_new_scheme(rows, cache, new_threshold)
    pairs = _select_pairs(rows, old_scheme, new_scheme)
    payload = {
        "metadata": {
            "source_sample_size": len(rows) + len(excluded_rows),
            "sample_size": len(rows),
            "excluded_sample_count": len(excluded_rows),
            "excluded_samples": excluded_rows,
            "pair_count": len(pairs),
            "old_threshold": old_threshold,
            "new_threshold": new_threshold,
            "new_cluster_mode": new_cluster_mode,
            "vectorizer": (
                "mimo-direct-1-to-n"
                if new_cluster_mode == "direct_mimo"
                else "local-chinese-char-tfidf-2-4gram"
            ),
            "old_flow": "清洗聊天内容 → TF-IDF → 余弦平均链接聚类",
            "new_flow": (
                "聊天 + 第二部分媒体分析 → MiMo原子知识/多主题识别 "
                "→ MiMo直接1-N聚类 → 单例跨桶二次复核"
                if new_cluster_mode == "direct_mimo"
                else (
                    "聊天 + 第二部分媒体分析 → MiMo原子知识/多主题识别 "
                    "→ 品类与适用范围硬门槛 → 标准化问题TF-IDF → 余弦平均链接聚类"
                )
            ),
            "direct_mimo": direct_meta,
            "mimo_model": next(iter(cache.values())).get("model", "") if cache else "",
            "mimo_prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
            "accuracy_status": "待人工标注",
        },
        "rows": rows,
        "pairs": pairs,
        "schemes": {
            "old": {
                "name": "旧流程",
                "units": old_scheme.units,
                "cluster_count": len({unit["cluster_id"] for unit in old_scheme.units}),
            },
            "new": {
                "name": "新流程",
                "units": new_scheme.units,
                "cluster_count": len({unit["cluster_id"] for unit in new_scheme.units}),
                "multi_topic_rows": sum(
                    cache[row["样本ID"]]["candidate"]["conversation_type"] == "multi_topic"
                    for row in rows
                ),
                "uncertain_rows": sum(
                    cache[row["样本ID"]]["candidate"]["conversation_type"] == "uncertain"
                    for row in rows
                ),
            },
        },
    }
    _write_json(output_path, payload)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-json", type=Path, required=True)
    parser.add_argument("--cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--max-new", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument(
        "--retry-errors",
        action="store_true",
        help="只重新调用当前缓存中 status=error 的样本",
    )
    parser.add_argument(
        "--text-only",
        action="store_true",
        help="移除图片、视频和上游媒体摘要，仅使用文字与结构化字段提取主题",
    )
    parser.add_argument(
        "--old-threshold",
        type=float,
        default=DEFAULT_OLD_THRESHOLD,
    )
    parser.add_argument(
        "--new-threshold",
        type=float,
        default=DEFAULT_NEW_THRESHOLD,
    )
    parser.add_argument(
        "--new-cluster-mode",
        choices=["tfidf", "direct_mimo"],
        default="tfidf",
        help="新版聚类方式；direct_mimo与生产默认链路一致",
    )
    args = parser.parse_args()

    source_rows = _read_json(args.sample_json)
    excluded_rows = [
        {
            "sample_id": row.get("样本ID", ""),
            "reason": reason,
        }
        for row in source_rows
        if (reason := _invalid_source_reason(row))
    ]
    excluded_ids = {item["sample_id"] for item in excluded_rows}
    rows = [
        row
        for row in source_rows
        if row.get("样本ID", "") not in excluded_ids
    ]
    cache: dict[str, Any] = (
        _read_json(args.cache_json) if args.cache_json.exists() else {}
    )
    missing_rows = [
        row
        for row in rows
        if _cache_entry_needs_refresh(
            row,
            cache,
            retry_errors=args.retry_errors,
            text_only=args.text_only,
        )
    ]
    batch = missing_rows[: max(0, args.max_new)]
    client: MimoClient | None = None
    if batch:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo 未配置，无法运行新版聚类流程")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    _analyze_row,
                    client,
                    row,
                    text_only=args.text_only,
                ): row
                for row in batch
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                row = futures[future]
                result = future.result()
                cache[row["样本ID"]] = result
                _write_json(args.cache_json, cache)
                print(
                    json.dumps(
                        {
                            "progress": f"{completed}/{len(batch)}",
                            "sample_id": row["样本ID"],
                            "status": result.get("status"),
                            "model": result.get("model"),
                            "media_mode": result.get("media", {}).get("mode"),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    remaining = [
        row["样本ID"]
        for row in rows
        if not _cache_entry_is_current(
            row,
            cache,
            text_only=args.text_only,
        )
    ]
    current_entries = [
        cache[row["样本ID"]]
        for row in rows
        if _cache_entry_is_current(
            row,
            cache,
            text_only=args.text_only,
        )
    ]
    print(
        json.dumps(
            {
                "sample_rows": len(rows),
                "source_rows": len(source_rows),
                "excluded_rows": len(excluded_rows),
                "excluded_samples": excluded_rows,
                "cached_rows": len(current_entries),
                "remaining_rows": len(remaining),
                "errors": sum(
                    item.get("status") == "error"
                    for item in current_entries
                ),
                "analysis_mode": (
                    "text_only"
                    if args.text_only
                    else "multimodal"
                ),
                "media_rows": sum(
                    str(item.get("media", {}).get("mode", "")).startswith(
                        "mimo-direct-multimodal"
                    )
                    for item in current_entries
                ),
                "image_attachments": sum(
                    len(item.get("media", {}).get("images", []))
                    for item in current_entries
                ),
                "video_attachments": sum(
                    len(item.get("media", {}).get("videos", []))
                    for item in current_entries
                ),
                "video_unavailable": sum(
                    media_item.get("status") == "unavailable"
                    for item in current_entries
                    for media_item in item.get("media", {}).get("videos", [])
                ),
            },
            ensure_ascii=False,
        )
    )
    if not remaining:
        if args.new_cluster_mode == "direct_mimo" and client is None:
            client = MimoClient.from_env()
            if client is None:
                raise RuntimeError("MiMo 未配置，无法运行生产版1-N聚类")
        _finalize(
            rows,
            cache,
            args.output_json,
            args.old_threshold,
            args.new_threshold,
            excluded_rows=excluded_rows,
            new_cluster_mode=args.new_cluster_mode,
            reviewer=client,
        )


if __name__ == "__main__":
    main()
