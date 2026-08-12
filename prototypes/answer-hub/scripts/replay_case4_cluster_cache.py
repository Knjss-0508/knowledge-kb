from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from answer_hub import workflow
from answer_hub.product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_name,
)
try:
    from scripts import eval_case4_cluster_accuracy
except ImportError:
    import eval_case4_cluster_accuracy


class OfflineCacheReviewer:
    def __init__(self, model: str, media_model: str) -> None:
        self.config = SimpleNamespace(
            model=model,
            media_model=media_model or model,
            cluster_media_policy="never",
            cluster_media_min_text_chars=220,
        )

    def analyze_cluster_units(self, _row: dict[str, Any]) -> None:
        raise RuntimeError("离线回放缺少原子提取缓存，已阻止真实模型调用")

    def analyze_cluster_units_batch(
        self,
        _rows: list[dict[str, Any]],
    ) -> None:
        raise RuntimeError("离线回放缺少批量原子提取缓存，已阻止真实模型调用")

    def can_batch_cluster_units(self, _row: dict[str, Any]) -> bool:
        return True

    def cluster_atomic_units(
        self,
        _units: list[dict[str, Any]],
    ) -> None:
        raise RuntimeError("离线回放缺少首轮聚类缓存，已阻止真实模型调用")


def _read_cache(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("direct_mimo 缓存必须是 JSON 对象")
    if not isinstance(payload.get("atomic_results"), dict):
        raise ValueError("direct_mimo 缓存缺少 atomic_results")
    if not isinstance(payload.get("cluster_results"), dict):
        raise ValueError("direct_mimo 缓存缺少 cluster_results")
    return payload


def _legacy_progress_key_without_business_line(
    source_index: int,
    source_row: dict[str, Any],
) -> str:
    signature = (
        workflow._clean_text(source_row.get("工单ID"))
        or workflow._clean_text(source_row.get("数据ID"))
        or workflow._clean_text(source_row.get("来源记录ID")),
        workflow._clean_text(source_row.get("回收单号")),
        workflow._clean_text(source_row.get("产品类型")),
        workflow._normalize_lines(source_row.get("聊天内容")),
        workflow._normalize_lines(source_row.get("图片链接")),
        workflow._normalize_lines(source_row.get("视频链接")),
    )
    payload = json.dumps(
        {
            "source_index": source_index,
            "signature": signature,
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _resolve_atomic_cache_entry(
    source_index: int,
    source_row: dict[str, Any],
    atomic_results: dict[str, Any],
) -> tuple[dict[str, Any] | None, str, str]:
    current_key = workflow._direct_mimo_progress_key(
        source_index,
        source_row,
    )
    current = atomic_results.get(current_key)
    if isinstance(current, dict):
        return current, current_key, "current"

    legacy_key = _legacy_progress_key_without_business_line(
        source_index,
        source_row,
    )
    legacy = atomic_results.get(legacy_key)
    if isinstance(legacy, dict):
        return legacy, legacy_key, "legacy_without_business_line"
    return None, "", "missing"


def replay(
    source_path: Path,
    cache_path: Path,
    model_workbook: Path,
    output_dir: Path,
    *,
    allow_atomic_fallback: bool = False,
) -> dict[str, Any]:
    output_dir.mkdir(parents=True, exist_ok=True)
    source_rows = workflow._read_source_rows(source_path)
    preprocessed_rows = workflow.preprocess_source_rows(source_rows)
    eligible_rows, excluded_rows = workflow.filter_preprocessed_rows_for_model(
        preprocessed_rows
    )
    deduplicated_rows, duplicate_rows = workflow._direct_mimo_deduplicate_rows(
        eligible_rows
    )
    payload = _read_cache(cache_path)
    baseline_predictions = eval_case4_cluster_accuracy._read_model_workbook(
        model_workbook
    )
    source_signature = payload.get("atomic_signature") or {}
    reviewer = OfflineCacheReviewer(
        model=str(source_signature.get("model") or "offline-cache-replay"),
        media_model=str(source_signature.get("media_model") or ""),
    )
    atomic_results = payload["atomic_results"]
    grouped_rows: dict[tuple[str, ...], list[dict[str, Any]]] = defaultdict(list)
    used_atomic_ids: set[str] = set()
    cached_atomic_rows = 0
    current_atomic_cache_hits = 0
    legacy_atomic_cache_hits = 0
    fallback_atomic_rows = 0
    used_atomic_cache_keys: set[str] = set()

    for source_index, source_row in enumerate(deduplicated_rows, start=1):
        cached, cache_key, cache_mode = _resolve_atomic_cache_entry(
            source_index,
            source_row,
            atomic_results,
        )
        if cached is not None:
            if cache_key in used_atomic_cache_keys:
                raise ValueError(
                    "离线回放缓存键被多个源记录重复使用："
                    f"source_index={source_index}"
                )
            used_atomic_cache_keys.add(cache_key)
            cached_atomic_rows += 1
            if cache_mode == "current":
                current_atomic_cache_hits += 1
            else:
                legacy_atomic_cache_hits += 1
        topics = cached.get("topics") if isinstance(cached, dict) else None
        if not isinstance(topics, list) or not topics:
            if not allow_atomic_fallback:
                raise ValueError(
                    "离线回放缺少可精确匹配的原子问题缓存："
                    f"source_index={source_index}"
                )
            topics = [workflow._direct_atomic_fallback(source_row)]
            cached = {
                "topics": topics,
                "failed": True,
                "rescue_reason": "",
            }
            fallback_atomic_rows += 1
        topics, rescue_reason = workflow._direct_local_multi_topic_rescue_topics(
            source_row,
            list(topics),
        )
        if rescue_reason:
            cached = {
                **cached,
                "topics": topics,
                "rescue_reason": rescue_reason,
            }
        if not topics:
            raise ValueError(
                "离线回放无法构造原子问题："
                f"source_index={source_index}"
            )
        source_id = str(
            source_row.get("数据ID")
            or source_row.get("工单ID")
            or f"ROW-{source_index:05d}"
        )
        source_product = canonical_product_name(
            source_row.get("产品类型"),
            unknown=UNKNOWN_PRODUCT_NAME,
        )
        source_business_line = workflow._business_line_for_row(source_row)
        predicted_topics = eval_case4_cluster_accuracy._prediction_values(
            baseline_predictions,
            source_id,
            source_product,
        )

        for topic_index, raw_topic in enumerate(topics, start=1):
            topic = dict(raw_topic)
            atomic_id = f"{source_id}-U{topic_index:02d}"
            if atomic_id in used_atomic_ids:
                atomic_id = (
                    f"{source_id}-R{source_index:05d}-U{topic_index:02d}"
                )
            used_atomic_ids.add(atomic_id)
            unit = {
                "unit_id": atomic_id,
                "sample_id": source_id,
                "source_conversation": str(
                    source_row.get("聊天内容") or ""
                ),
                "source_core_problem": str(
                    source_row.get("核心问题")
                    or source_row.get("原始核心问题")
                    or ""
                ),
                "source_judgment_conclusion": str(
                    source_row.get("判定结论")
                    or source_row.get("原始判定结论")
                    or ""
                ),
                "business_line": source_business_line,
                **topic,
                "product_category": source_product,
            }
            rule_match = workflow._direct_clustering_rule_match(unit)
            atomic_row = dict(source_row)
            atomic_row.update(
                {
                    "_原子知识ID": atomic_id,
                    "_原子需要复核": bool(cached.get("failed")),
                    "_原子适用范围类型": str(
                        topic.get("scope_type") or ""
                    ),
                    "_原子平台": str(topic.get("platform") or ""),
                    "_原子品牌": str(topic.get("brand") or ""),
                    "_原子机型范围": str(
                        topic.get("model_scope") or ""
                    ),
                    "_原子阈值例外": str(
                        topic.get("threshold_or_exception") or ""
                    ),
                    "_聚类判定规则ID": (
                        rule_match.rule_id if rule_match else ""
                    ),
                    "_聚类标准族": (
                        rule_match.standard_family if rule_match else ""
                    ),
                    "_聚类现象值": (
                        rule_match.phenomenon_value if rule_match else ""
                    ),
                    "_聚类合并策略": (
                        rule_match.merge_policy if rule_match else ""
                    ),
                    "原始核心问题": unit["source_core_problem"],
                    "原始判定结论": unit[
                        "source_judgment_conclusion"
                    ],
                    "_聚类主题标题": str(
                        topic.get("normalized_issue") or ""
                    ),
                    "_聚类决策": "复用首轮真实模型簇",
                    "_聚类裁决提供方": "mimo-direct",
                    "_聚类裁决原因": "离线复用已生成的首轮聚类结果",
                    "_聚类裁决置信度": topic.get("confidence", ""),
                    "_聚类需要复核": bool(cached.get("failed")),
                    "核心问题": str(
                        topic.get("normalized_issue")
                        or source_row.get("核心问题")
                        or ""
                    ),
                    "产品类型": source_product,
                    "模型主题一级分类": str(
                        topic.get("category_l1") or ""
                    ),
                    "模型主题二级分类": str(
                        topic.get("category_l2") or ""
                    ),
                    "问题意图": str(topic.get("intent") or ""),
                    "对象/部位": str(topic.get("subject") or ""),
                    "异常现象": str(topic.get("phenomenon") or ""),
                    "判定目标": str(
                        topic.get("judgment_target") or ""
                    ),
                    "解题方式": str(
                        topic.get("resolution_mode") or ""
                    ),
                    "主标准路径": str(
                        topic.get("standard_path") or ""
                    ),
                    "语义标注依据": str(
                        topic.get("evidence_summary") or ""
                    ),
                    "语义标注置信度": topic.get("confidence", ""),
                }
            )
            baseline_cluster = (
                predicted_topics[topic_index - 1]
                if topic_index <= len(predicted_topics)
                else f"UNMAPPED-{source_index:05d}-{topic_index:02d}"
            )
            grouped_rows[
                (
                    "direct_mimo",
                    source_business_line,
                    source_product,
                    "baseline",
                    baseline_cluster,
                )
            ].append(atomic_row)

    guarded_groups: list[
        tuple[tuple[str, ...], list[dict[str, Any]]]
    ] = []
    post_guard_split_clusters = 0
    post_guard_singletons = 0
    for key, rows in grouped_rows.items():
        conflict_reason = workflow._direct_cluster_hard_conflict_reason(rows)
        if not conflict_reason:
            guarded_groups.append((key, rows))
            continue
        post_guard_split_clusters += 1
        for member_index, row in enumerate(rows, start=1):
            post_guard_singletons += 1
            row.update(
                {
                    "_聚类决策": "离线按当前程序门禁拆分冲突聚类",
                    "_聚类裁决提供方": "mimo-direct-post-guard",
                    "_聚类裁决原因": conflict_reason,
                    "_聚类需要复核": True,
                }
            )
            guarded_groups.append(
                (
                    (
                        *key,
                        "offline-post-guard",
                        str(member_index),
                        str(row.get("_原子知识ID") or ""),
                    ),
                    [row],
                )
            )

    meta: dict[str, Any] = {
        "provider": "offline-real-cluster-replay",
        "model": reviewer.config.model,
        "offline_post_guard_split_clusters": post_guard_split_clusters,
        "offline_post_guard_singletons": post_guard_singletons,
    }
    reconciled_groups = workflow._reconcile_direct_topic_groups(
        guarded_groups,
        reviewer,  # type: ignore[arg-type]
        meta,
        review_limit=workflow.DEFAULT_DIRECT_RECONCILE_LIMIT,
    )
    cluster_rows = [
        workflow._cluster_only_topic_row(
            workflow._topic_id(key),
            key,
            rows,
        )
        for key, rows in reconciled_groups
    ]
    workbook_path = output_dir / "cluster_result.xlsx"
    workflow.write_rows_to_workbook(
        {"聚类结果": (workflow.CLUSTER_ONLY_COLUMNS, cluster_rows)},
        workbook_path,
    )
    replay_summary = {
        **meta,
        "source_rows": len(source_rows),
        "eligible_rows": len(eligible_rows),
        "excluded_rows": len(excluded_rows),
        "deduplicated_rows": len(deduplicated_rows),
        "duplicate_rows": len(duplicate_rows),
        "cached_atomic_rows": cached_atomic_rows,
        "current_atomic_cache_hits": current_atomic_cache_hits,
        "legacy_atomic_cache_hits": legacy_atomic_cache_hits,
        "fallback_atomic_rows": fallback_atomic_rows,
        "baseline_cluster_rows": len(grouped_rows),
        "cluster_rows": len(cluster_rows),
        "offline_replay": True,
        "real_model_calls": 0,
        "source_cache": str(cache_path),
        "model_workbook": str(model_workbook),
        "output_file": str(workbook_path),
    }
    (output_dir / "replay_summary.json").write_text(
        json.dumps(replay_summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return replay_summary


def main() -> None:
    parser = argparse.ArgumentParser(
        description="使用已有 direct_mimo 缓存离线回放 Case4 聚类后处理。",
    )
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--model-workbook", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument(
        "--allow-atomic-fallback",
        action="store_true",
        help="显式允许缺失原子缓存时使用规则降级；默认严格停止。",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            replay(
                args.source,
                args.cache,
                args.model_workbook,
                args.output_dir,
                allow_atomic_fallback=args.allow_atomic_fallback,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
