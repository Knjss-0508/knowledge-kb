from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from answer_hub.mimo import (
    MimoClient,
    MimoError,
    TOPIC_STAGE_PROMPT_VERSION,
)

SINGLE_CASE_KNOWLEDGE_GUARD_VERSION = "single-case-knowledge-guard-v1"
GENERIC_RULE_VALUES = {
    "",
    "待确认",
    "未知",
    "无",
    "无明确阈值",
    "无明确标准",
    "不适用",
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


def _text(value: Any, limit: int = 1200) -> str:
    return str(value or "").strip()[:limit]


def _unique(values: list[Any], limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        text = _text(value)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _theme_payload(cluster_id: str, units: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "theme_id": cluster_id,
        "member_count": len(units),
        "source_sample_ids": _unique([unit.get("sample_id") for unit in units]),
        "source_unit_ids": _unique([unit.get("unit_id") for unit in units]),
        "conversation_types": _unique(
            [unit.get("conversation_type") for unit in units]
        ),
        "product_categories": _unique(
            [unit.get("product_category") for unit in units]
        ),
        "scope_types": _unique([unit.get("scope_type") for unit in units]),
        "normalized_issues": _unique(
            [unit.get("normalized_issue") for unit in units],
            limit=8,
        ),
        "category_l1": _unique([unit.get("category_l1") for unit in units]),
        "category_l2": _unique([unit.get("category_l2") for unit in units]),
        "intents": _unique([unit.get("intent") for unit in units]),
        "subjects": _unique([unit.get("subject") for unit in units]),
        "phenomena": _unique([unit.get("phenomenon") for unit in units]),
        "judgment_targets": _unique(
            [unit.get("judgment_target") for unit in units]
        ),
        "resolution_modes": _unique(
            [unit.get("resolution_mode") for unit in units]
        ),
        "standard_paths": _unique(
            [unit.get("standard_path") for unit in units]
        ),
        "thresholds_or_exceptions": _unique(
            [unit.get("threshold_or_exception") for unit in units]
        ),
        "evidence_summaries": _unique(
            [_text(unit.get("evidence_summary"), 800) for unit in units],
            limit=6,
        ),
        "upstream_requires_review": any(
            bool(unit.get("requires_review"))
            for unit in units
        ),
    }


def _load_themes(cluster_payload: dict[str, Any]) -> list[dict[str, Any]]:
    units = cluster_payload.get("schemes", {}).get("new", {}).get("units", [])
    if not isinstance(units, list) or not units:
        raise ValueError("聚类结果缺少 schemes.new.units")
    grouped: dict[str, list[dict[str, Any]]] = {}
    for unit in units:
        cluster_id = _text(unit.get("cluster_id"), 80)
        if not cluster_id:
            raise ValueError("聚类单元缺少 cluster_id")
        grouped.setdefault(cluster_id, []).append(unit)
    return [
        _theme_payload(cluster_id, grouped[cluster_id])
        for cluster_id in sorted(grouped)
    ]


def _theme_hash(theme: dict[str, Any]) -> str:
    raw = json.dumps(
        theme,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _has_explicit_reusable_rule(theme: dict[str, Any]) -> bool:
    fields = [
        *theme.get("normalized_issues", []),
        *theme.get("judgment_targets", []),
        *theme.get("resolution_modes", []),
        *theme.get("standard_paths", []),
        *theme.get("thresholds_or_exceptions", []),
        *theme.get("evidence_summaries", []),
    ]
    text = "\n".join(str(value or "").strip() for value in fields)
    threshold_values = {
        str(value or "").strip()
        for value in theme.get("thresholds_or_exceptions", [])
    }
    if any(value not in GENERIC_RULE_VALUES for value in threshold_values):
        return True
    if re.search(
        r"\d+(?:\.\d+)?\s*(?:mm|毫米|cm|厘米|%|次|个|GB|TB|分钟|小时|天)",
        text,
        flags=re.IGNORECASE,
    ):
        return True
    if re.search(
        r"(以.+为准|优先采用|优先按|先.+再|依次|步骤|进入.+页面|"
        r"读取.+信息|核对.+信息|检查.+后|检测.+后|大于|小于|"
        r"不超过|不少于|至少|必须|不得)",
        text,
    ):
        return True
    return False


def _apply_single_case_knowledge_guard(
    theme: dict[str, Any],
    prediction: dict[str, Any],
) -> dict[str, Any]:
    guarded = dict(prediction)
    if (
        int(theme.get("member_count") or 0) != 1
        or guarded.get("knowledge_value") != "值得沉淀"
        or _has_explicit_reusable_rule(theme)
    ):
        guarded["knowledge_value_guard_applied"] = False
        return guarded
    guard_reason = (
        "单案例主题未提供明确数值阈值、通用优先级规则或可执行操作步骤；"
        "当前仅能形成个案结论，按规则强制标记为不值得沉淀。"
    )
    guarded.update(
        {
            "knowledge_value": "不值得沉淀",
            "value_reason": "；".join(
                part
                for part in (
                    str(guarded.get("value_reason", "")).strip(),
                    guard_reason,
                )
                if part
            ),
            "reusable_knowledge": (
                "当前只有单个案例结论，缺少可复用的阈值、边界、"
                "通用处理规则或操作步骤。"
            ),
            "needs_human_review": True,
            "knowledge_value_guard_applied": True,
        }
    )
    return guarded


def _cache_entry_is_current(
    theme: dict[str, Any],
    cache: dict[str, Any],
) -> bool:
    entry = cache.get(theme["theme_id"])
    return bool(
        isinstance(entry, dict)
        and entry.get("prompt_version") == TOPIC_STAGE_PROMPT_VERSION
        and entry.get("input_hash") == _theme_hash(theme)
        and entry.get("status") == "ok"
    )


def _classify_theme(
    client: MimoClient,
    theme: dict[str, Any],
) -> dict[str, Any]:
    try:
        result = client.classify_topic_stage(theme)
        return {
            "status": "ok",
            "prediction": result.candidate,
            "model": result.request_audit.get("model", client.config.model),
            "prompt_version": TOPIC_STAGE_PROMPT_VERSION,
            "input_hash": _theme_hash(theme),
        }
    except MimoError as exc:
        return {
            "status": "error",
            "error": str(exc),
            "prediction": {},
            "model": client.config.model,
            "prompt_version": TOPIC_STAGE_PROMPT_VERSION,
            "input_hash": _theme_hash(theme),
        }


def _build_output(
    cluster_payload: dict[str, Any],
    themes: list[dict[str, Any]],
    cache: dict[str, Any],
) -> dict[str, Any]:
    output_themes: list[dict[str, Any]] = []
    for theme in themes:
        entry = cache.get(theme["theme_id"], {})
        raw_prediction = entry.get("prediction", {})
        prediction = (
            _apply_single_case_knowledge_guard(theme, raw_prediction)
            if entry.get("status") == "ok"
            else raw_prediction
        )
        output_themes.append(
            {
                **theme,
                "classification_status": entry.get("status", "pending"),
                "classification_error": entry.get("error", ""),
                "model": entry.get("model", ""),
                "prompt_version": entry.get(
                    "prompt_version",
                    TOPIC_STAGE_PROMPT_VERSION,
                ),
                "prediction": prediction,
            }
        )
    predictions = [
        theme["prediction"]
        for theme in output_themes
        if theme["classification_status"] == "ok"
    ]
    stage_counts: dict[str, int] = {}
    value_counts: dict[str, int] = {}
    for prediction in predictions:
        stage = _text(prediction.get("topic_stage"), 32)
        value = _text(prediction.get("knowledge_value"), 32)
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        value_counts[value] = value_counts.get(value, 0) + 1
    cluster_metadata = cluster_payload.get("metadata", {})
    return {
        "metadata": {
            "source_sample_size": cluster_metadata.get("sample_size", ""),
            "source_pair_count": cluster_metadata.get("pair_count", ""),
            "theme_count": len(themes),
            "classified_count": len(predictions),
            "pending_count": sum(
                theme["classification_status"] == "pending"
                for theme in output_themes
            ),
            "error_count": sum(
                theme["classification_status"] == "error"
                for theme in output_themes
            ),
            "model": next(
                (
                    theme["model"]
                    for theme in output_themes
                    if theme["model"]
                ),
                "",
            ),
            "prompt_version": TOPIC_STAGE_PROMPT_VERSION,
            "knowledge_value_guard_version": (
                SINGLE_CASE_KNOWLEDGE_GUARD_VERSION
            ),
            "single_case_guard_applied_count": sum(
                bool(
                    theme.get("prediction", {}).get(
                        "knowledge_value_guard_applied"
                    )
                )
                for theme in output_themes
            ),
            "stage_counts": stage_counts,
            "knowledge_value_counts": value_counts,
            "accuracy_status": "待人工标注",
        },
        "themes": output_themes,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cluster-json", type=Path, required=True)
    parser.add_argument("--cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--max-new", type=int, default=12)
    parser.add_argument("--workers", type=int, default=4)
    args = parser.parse_args()

    cluster_payload = _read_json(args.cluster_json)
    themes = _load_themes(cluster_payload)
    cache: dict[str, Any] = (
        _read_json(args.cache_json) if args.cache_json.exists() else {}
    )
    missing_themes = [
        theme
        for theme in themes
        if not _cache_entry_is_current(theme, cache)
    ]
    batch = missing_themes[: max(0, args.max_new)]
    if batch:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo 未配置，无法执行主题环节与沉淀价值判断")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_classify_theme, client, theme): theme
                for theme in batch
            }
            for future in as_completed(futures):
                theme = futures[future]
                cache[theme["theme_id"]] = future.result()
                _write_json(args.cache_json, cache)

    output = _build_output(cluster_payload, themes, cache)
    _write_json(args.output_json, output)
    print(
        json.dumps(
            {
                **output["metadata"],
                "remaining_count": sum(
                    not _cache_entry_is_current(theme, cache)
                    for theme in themes
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
