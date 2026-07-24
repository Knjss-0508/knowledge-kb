from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

try:
    from scripts.run_cluster_ab_test import (
        _new_semantic_text,
        _normalize_unit_scope,
    )
except ModuleNotFoundError:
    from run_cluster_ab_test import _new_semantic_text, _normalize_unit_scope


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


UNCERTAIN_VALUES = {
    "",
    "待确认",
    "未知",
    "不确定",
    "其他待确认",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _is_uncertain(value: Any) -> bool:
    text = _text(value)
    return text in UNCERTAIN_VALUES or "待确认" in text


def _candidate_exclusion_reason(
    row: dict[str, Any],
    candidate: dict[str, Any],
) -> str:
    if _text(candidate.get("conversation_type")) != "uncertain":
        return ""
    topics = candidate.get("topics") or []
    if not topics:
        return "模型未能提取任何有效问题主题"

    critical_fields = (
        "scope_type",
        "category_l1",
        "intent",
        "subject",
        "phenomenon",
        "judgment_target",
        "resolution_mode",
        "standard_path",
    )
    all_topics_unresolved = all(
        sum(not _is_uncertain(topic.get(field)) for field in critical_fields) <= 1
        and float(topic.get("confidence") or 0) <= 0.1
        for topic in topics
    )
    media_analysis = candidate.get("media_analysis") or {}
    media_relevance = _text(media_analysis.get("media_relevance"))
    media_is_unusable = media_relevance in {
        "",
        "不相关",
        "无法读取",
        "无媒体",
    }
    evidence_text = "\n".join(
        [
            _text(candidate.get("reason")),
            *[_text(topic.get("evidence_summary")) for topic in topics],
            _text(row.get("核心问题")),
            _text(row.get("判定结论")),
            _text(row.get("判定依据")),
        ]
    )
    missing_evidence_markers = (
        "缺失会话",
        "缺少会话",
        "缺失了进行问题分析",
        "仅包含工单元数据",
        "无法提取具体问题",
        "无法基于现有信息作出判定",
        "信息缺失",
    )
    explicitly_missing_evidence = any(
        marker in evidence_text
        for marker in missing_evidence_markers
    )
    if all_topics_unresolved and (
        media_is_unusable or explicitly_missing_evidence
    ):
        return "缺少有效答疑会话，且媒体与质检问题无关或无法提供有效问题事实"
    return ""


def _build_units_and_exclusions(
    rows: list[dict[str, Any]],
    fusion_results: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    units: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []
    for row in rows:
        sample_id = row["样本ID"]
        entry = fusion_results.get(sample_id)
        if not isinstance(entry, dict) or not isinstance(entry.get("candidate"), dict):
            raise ValueError(f"融合结果缺少样本：{sample_id}")
        candidate = entry["candidate"]
        exclusion_reason = _candidate_exclusion_reason(row, candidate)
        if exclusion_reason:
            excluded_rows.append(
                {
                    "sample_id": sample_id,
                    "source_record_key": row.get("源记录键", ""),
                    "work_order_id": row.get("工单ID", ""),
                    "product_category": row.get("产品类型", ""),
                    "device_model": row.get("机型", ""),
                    "source_core_problem": row.get("核心问题", ""),
                    "conversation_type": candidate.get(
                        "conversation_type",
                        "uncertain",
                    ),
                    "exclusion_reason": exclusion_reason,
                }
            )
            continue
        topics = candidate.get("topics") or []
        if not topics:
            raise ValueError(f"融合结果没有主题：{sample_id}")
        for topic_index, topic in enumerate(topics, start=1):
            unit = {
                "unit_id": f"{sample_id}-{topic_index:02d}",
                "sample_id": sample_id,
                "source_record_key": row.get("源记录键", ""),
                "work_order_id": row.get("工单ID", ""),
                "device_model": row.get("机型", ""),
                "source_core_problem": row.get("核心问题", ""),
                "source_conversation": row.get("聊天内容", ""),
                "image_links": row.get("图片链接", ""),
                "video_links": row.get("视频链接", ""),
                "conversation_type": candidate.get(
                    "conversation_type",
                    "uncertain",
                ),
                "fusion_status": entry.get("status", ""),
                "fusion_reason": candidate.get("reason", ""),
                "media_analysis": candidate.get("media_analysis", {}),
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
                "confidence": topic.get("confidence", ""),
                "requires_review": topic.get("requires_review", False),
            }
            _normalize_unit_scope(unit)
            unit["semantic_text"] = _new_semantic_text(unit)
            units.append(unit)
    return units, excluded_rows


def _build_units(
    rows: list[dict[str, Any]],
    fusion_results: dict[str, Any],
) -> list[dict[str, Any]]:
    units, _excluded_rows = _build_units_and_exclusions(
        rows,
        fusion_results,
    )
    return units


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将全量媒体融合结果转换为1-N原子主题聚类输入。"
    )
    parser.add_argument("--source-json", type=Path, required=True)
    parser.add_argument("--fusion-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    args = parser.parse_args()

    rows = _read_json(args.source_json)
    fusion_payload = _read_json(args.fusion_json)
    fusion_results = fusion_payload.get("results") or {}
    units, excluded_rows = _build_units_and_exclusions(
        rows,
        fusion_results,
    )
    included_sample_ids = {
        unit["sample_id"]
        for unit in units
    }
    payload = {
        "metadata": {
            "sample_size": len(rows),
            "included_sample_size": len(included_sample_ids),
            "excluded_sample_size": len(excluded_rows),
            "atomic_unit_count": len(units),
            "single_topic_rows": sum(
                fusion_results[row["样本ID"]]["candidate"].get(
                    "conversation_type"
                )
                == "single_topic"
                for row in rows
            ),
            "multi_topic_rows": sum(
                fusion_results[row["样本ID"]]["candidate"].get(
                    "conversation_type"
                )
                == "multi_topic"
                for row in rows
            ),
            "uncertain_rows": sum(
                fusion_results[row["样本ID"]]["candidate"].get(
                    "conversation_type"
                )
                == "uncertain"
                for row in rows
            ),
            "fusion_error_rows": sum(
                fusion_results[row["样本ID"]].get("status") == "error"
                for row in rows
            ),
        },
        "rows": rows,
        "excluded_rows": excluded_rows,
        "schemes": {
            "new": {
                "name": "文字Pro＋媒体事实＋融合裁决",
                "units": units,
            }
        },
    }
    _write_json(args.output_json, payload)
    print(json.dumps(payload["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
