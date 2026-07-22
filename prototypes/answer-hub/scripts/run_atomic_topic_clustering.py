from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
from typing import Any

from answer_hub.mimo import (
    ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
    MimoClient,
    MimoError,
)


SCOPE_LEVELS = {
    "通用": 0,
    "品类专用": 1,
    "平台专用": 2,
    "品牌专用": 3,
    "机型专用": 4,
}
UNCERTAIN_MARKERS = {"", "待确认", "未知", "不确定"}


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


def _text(value: Any) -> str:
    return str(value or "").strip()


def _normalized_key_value(value: Any) -> str:
    return re.sub(
        r"[\s/／|｜、，,;；:：()\[\]【】]+",
        "",
        _text(value).lower(),
    )


def _scope_signature(unit: dict[str, Any]) -> tuple[str, str, str, str]:
    scope_type = _text(unit.get("scope_type"))
    scope_level = SCOPE_LEVELS.get(scope_type, -1)
    return (
        scope_type,
        _normalized_key_value(unit.get("platform")) if scope_level >= 2 else "通用",
        _normalized_key_value(unit.get("brand")) if scope_level >= 3 else "通用",
        _normalized_key_value(unit.get("model_scope")) if scope_level >= 4 else "通用",
    )


def _hard_bucket_key(unit: dict[str, Any]) -> tuple[str, ...]:
    scope_type, platform, brand, model_scope = _scope_signature(unit)
    return (
        _normalized_key_value(unit.get("product_category")),
        _normalized_key_value(scope_type),
        platform,
        brand,
        model_scope,
        _normalized_key_value(unit.get("category_l1")),
        _normalized_key_value(unit.get("intent")),
    )


def _unit_review_reason(unit: dict[str, Any]) -> str:
    required_fields = (
        "product_category",
        "scope_type",
        "category_l1",
        "intent",
        "subject",
        "judgment_target",
        "resolution_mode",
        "standard_path",
        "threshold_or_exception",
    )
    uncertain_fields = [
        field
        for field in required_fields
        if _text(unit.get(field)) in UNCERTAIN_MARKERS
        or "待确认" in _text(unit.get(field))
    ]
    if uncertain_fields:
        return f"关键聚类字段待确认：{', '.join(uncertain_fields)}"
    if bool(unit.get("requires_review")):
        return "原子知识提取阶段已标记需要人工复核，不能自动合并"
    if _text(unit.get("scope_type")) not in SCOPE_LEVELS:
        return "适用范围类型不合法或无法识别"
    return ""


def _bucket_id(bucket_key: tuple[str, ...], atomic_ids: list[str]) -> str:
    raw = json.dumps(
        {"bucket_key": bucket_key, "atomic_ids": sorted(atomic_ids)},
        ensure_ascii=False,
        sort_keys=True,
    )
    return f"B-{hashlib.sha1(raw.encode('utf-8')).hexdigest()[:12]}"


def _bucket_units(
    units: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    review_units: list[dict[str, Any]] = []
    grouped: dict[tuple[str, ...], list[dict[str, Any]]] = {}
    for source_unit in units:
        unit = dict(source_unit)
        atomic_id = _text(unit.get("unit_id") or unit.get("atomic_id"))
        if not atomic_id:
            raise ValueError("输入原子知识点缺少 unit_id")
        unit["unit_id"] = atomic_id
        review_reason = _unit_review_reason(unit)
        if review_reason:
            unit["_review_reason"] = review_reason
            review_units.append(unit)
            continue
        grouped.setdefault(_hard_bucket_key(unit), []).append(unit)

    buckets: list[dict[str, Any]] = []
    for key, members in grouped.items():
        sorted_members = sorted(members, key=lambda item: item["unit_id"])
        atomic_ids = [item["unit_id"] for item in sorted_members]
        buckets.append(
            {
                "bucket_id": _bucket_id(key, atomic_ids),
                "bucket_key": list(key),
                "units": sorted_members,
                "atomic_ids": atomic_ids,
            }
        )
    buckets.sort(key=lambda item: (-len(item["units"]), item["bucket_id"]))
    review_units.sort(key=lambda item: item["unit_id"])
    return buckets, review_units


def _singleton_candidate(unit: dict[str, Any]) -> dict[str, Any]:
    atomic_id = unit["unit_id"]
    theme_name = _text(unit.get("normalized_issue")) or atomic_id
    return {
        "clusters": [
            {
                "cluster_id": "C001",
                "theme_name": theme_name[:160],
                "member_atomic_ids": [atomic_id],
                "scope_consistent": True,
                "object_consistent": True,
                "judgment_target_consistent": True,
                "standard_path_consistent": True,
                "threshold_exception_consistent": True,
                "shared_knowledge_definition": theme_name[:500],
                "merge_basis": "该硬规则分桶仅包含一个原子知识点，保留为单知识点主题簇。",
            }
        ],
        "split_requests": [],
        "review_requests": [],
    }


def _analyze_bucket(client: MimoClient, bucket: dict[str, Any]) -> dict[str, Any]:
    try:
        result = client.cluster_atomic_units(bucket["units"])
        return {
            "status": "ok",
            "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
            "model": client.config.model,
            "bucket_id": bucket["bucket_id"],
            "bucket_key": bucket["bucket_key"],
            "atomic_ids": bucket["atomic_ids"],
            "candidate": result.candidate,
            "request_audit": result.request_audit,
            "response_audit": result.response_audit,
        }
    except MimoError as exc:
        return {
            "status": "error",
            "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
            "model": client.config.model,
            "bucket_id": bucket["bucket_id"],
            "bucket_key": bucket["bucket_key"],
            "atomic_ids": bucket["atomic_ids"],
            "error": str(exc),
        }


def _cache_entry_valid(
    entry: Any,
    bucket: dict[str, Any],
    *,
    retry_errors: bool,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("prompt_version") != ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION:
        return False
    if entry.get("atomic_ids") != bucket["atomic_ids"]:
        return False
    if retry_errors and entry.get("status") == "error":
        return False
    return entry.get("status") in {"ok", "error"}


def _review_request(
    unit: dict[str, Any],
    review_type: str,
    reason: str,
) -> dict[str, str]:
    return {
        "atomic_id": unit["unit_id"],
        "review_type": review_type,
        "reason": reason[:300],
    }


def _model_review_singleton(
    unit: dict[str, Any],
    request: dict[str, Any],
) -> dict[str, Any]:
    theme_name = _text(unit.get("normalized_issue")) or unit["unit_id"]
    return {
        "cluster_id": "MODEL-SINGLETON",
        "theme_name": theme_name[:160],
        "member_atomic_ids": [unit["unit_id"]],
        "scope_consistent": True,
        "object_consistent": True,
        "judgment_target_consistent": True,
        "standard_path_consistent": True,
        "threshold_exception_consistent": True,
        "shared_knowledge_definition": theme_name[:500],
        "merge_basis": (
            "该原子知识点自身主题清楚，但与同桶其他成员不能共用一条知识，"
            f"因此保留为单成员簇。模型区分依据：{_text(request.get('reason'))}"
        )[:300],
    }


def _normalized_threshold(value: Any) -> str:
    normalized = _normalized_key_value(value)
    if normalized in {
        "",
        "无",
        "无明确阈值",
        "无阈值",
        "不涉及",
        "不适用",
    }:
        return "无明确阈值"
    return normalized


def _cluster_program_conflicts(
    cluster: dict[str, Any],
    unit_by_id: dict[str, dict[str, Any]],
) -> list[str]:
    if len(cluster["member_atomic_ids"]) <= 1:
        return []
    members = [
        unit_by_id[atomic_id]
        for atomic_id in cluster["member_atomic_ids"]
    ]
    reasons: list[str] = []
    category_l2_values = {
        _normalized_key_value(member.get("category_l2"))
        for member in members
    }
    if len(category_l2_values) > 1:
        reasons.append("知识二级分类不同")
    threshold_values = {
        _normalized_threshold(member.get("threshold_or_exception"))
        for member in members
    }
    if len(threshold_values) > 1:
        reasons.append("阈值或例外条件不同")
    return reasons


def _post_validation_singleton(
    unit: dict[str, Any],
    original_cluster: dict[str, Any],
    conflict_reasons: list[str],
) -> dict[str, Any]:
    theme_name = _text(unit.get("normalized_issue")) or unit["unit_id"]
    return {
        "cluster_id": f"POST-SINGLETON-{unit['unit_id']}",
        "theme_name": theme_name[:160],
        "member_atomic_ids": [unit["unit_id"]],
        "scope_consistent": True,
        "object_consistent": True,
        "judgment_target_consistent": True,
        "standard_path_consistent": True,
        "threshold_exception_consistent": True,
        "shared_knowledge_definition": theme_name[:500],
        "merge_basis": (
            f"程序二次门禁拆分原候选簇“{_text(original_cluster.get('theme_name'))}”："
            f"{'、'.join(conflict_reasons)}。硬规则不允许自动合并，保留为单成员簇。"
        )[:300],
    }


def _build_output(
    *,
    input_path: Path,
    units: list[dict[str, Any]],
    buckets: list[dict[str, Any]],
    review_units: list[dict[str, Any]],
    cache: dict[str, Any],
) -> dict[str, Any]:
    clusters: list[dict[str, Any]] = []
    split_requests: list[dict[str, Any]] = []
    review_requests: list[dict[str, Any]] = [
        _review_request(
            unit,
            "原子字段待确认",
            unit["_review_reason"],
        )
        for unit in review_units
    ]
    pending_buckets: list[dict[str, Any]] = []

    for bucket in buckets:
        if len(bucket["units"]) == 1:
            candidate = _singleton_candidate(bucket["units"][0])
            source = "single_member_bucket"
        else:
            entry = cache.get(bucket["bucket_id"])
            if not _cache_entry_valid(entry, bucket, retry_errors=False):
                pending_buckets.append(
                    {
                        "bucket_id": bucket["bucket_id"],
                        "atomic_ids": bucket["atomic_ids"],
                        "member_count": len(bucket["units"]),
                    }
                )
                continue
            if entry["status"] == "error":
                for unit in bucket["units"]:
                    review_requests.append(
                        _review_request(
                            unit,
                            "模型调用失败",
                            f"整桶聚类失败，未自动合并：{_text(entry.get('error'))}",
                        )
                    )
                continue
            candidate = entry["candidate"]
            source = "mimo_bucket_clustering"

        bucket_unit_by_id = {
            unit["unit_id"]: unit
            for unit in bucket["units"]
        }
        candidate_clusters: list[dict[str, Any]] = []
        for cluster in candidate["clusters"]:
            conflict_reasons = _cluster_program_conflicts(
                cluster,
                bucket_unit_by_id,
            )
            if conflict_reasons:
                candidate_clusters.extend(
                    _post_validation_singleton(
                        bucket_unit_by_id[atomic_id],
                        cluster,
                        conflict_reasons,
                    )
                    for atomic_id in cluster["member_atomic_ids"]
                )
            else:
                candidate_clusters.append(cluster)

        candidate_reviews: list[dict[str, Any]] = []
        for request in candidate["review_requests"]:
            unit = bucket_unit_by_id[request["atomic_id"]]
            if _unit_review_reason(unit):
                candidate_reviews.append(request)
            else:
                candidate_clusters.append(
                    _model_review_singleton(unit, request)
                )

        for cluster in candidate_clusters:
            clusters.append(
                {
                    **cluster,
                    "bucket_id": bucket["bucket_id"],
                    "source": (
                        "program_rule_singleton"
                        if cluster["cluster_id"].startswith("POST-SINGLETON-")
                        else
                        "mimo_distinct_singleton"
                        if cluster["cluster_id"] == "MODEL-SINGLETON"
                        else source
                    ),
                    "local_cluster_id": cluster["cluster_id"],
                }
            )
        split_requests.extend(
            {
                **request,
                "bucket_id": bucket["bucket_id"],
            }
            for request in candidate["split_requests"]
        )
        review_requests.extend(
            {
                **request,
                "bucket_id": bucket["bucket_id"],
            }
            for request in candidate_reviews
        )

    clusters.sort(
        key=lambda item: (
            item["member_atomic_ids"][0],
            item["bucket_id"],
            item["local_cluster_id"],
        )
    )
    for index, cluster in enumerate(clusters, start=1):
        cluster["cluster_id"] = f"T{index:03d}"
        cluster["member_count"] = len(cluster["member_atomic_ids"])

    split_requests.sort(key=lambda item: item["atomic_id"])
    review_requests.sort(key=lambda item: item["atomic_id"])
    assigned_ids = {
        atomic_id
        for cluster in clusters
        for atomic_id in cluster["member_atomic_ids"]
    }
    assigned_ids.update(request["atomic_id"] for request in split_requests)
    assigned_ids.update(request["atomic_id"] for request in review_requests)
    all_ids = {_text(unit.get("unit_id")) for unit in units}
    unassigned_ids = sorted(all_ids - assigned_ids)
    duplicate_count = (
        sum(cluster["member_count"] for cluster in clusters)
        + len(split_requests)
        + len(review_requests)
        - len(assigned_ids)
    )
    status = "complete" if not pending_buckets and not unassigned_ids else "partial"
    if status == "complete" and (assigned_ids != all_ids or duplicate_count):
        raise RuntimeError("最终主题聚类未满足 atomic_id 完整、唯一覆盖要求")

    unit_by_id = {_text(unit.get("unit_id")): unit for unit in units}
    cluster_summaries: list[dict[str, Any]] = []
    for cluster in clusters:
        member_units = [
            unit_by_id[atomic_id]
            for atomic_id in cluster["member_atomic_ids"]
        ]
        representative = member_units[0]
        cluster_summaries.append(
            {
                **cluster,
                "product_category": _text(representative.get("product_category")),
                "scope_type": _text(representative.get("scope_type")),
                "platform": _text(representative.get("platform")),
                "brand": _text(representative.get("brand")),
                "model_scope": _text(representative.get("model_scope")),
                "category_l1": _text(representative.get("category_l1")),
                "intent": _text(representative.get("intent")),
            }
        )

    return {
        "metadata": {
            "status": status,
            "input_path": str(input_path),
            "prompt_version": ATOMIC_TOPIC_CLUSTER_PROMPT_VERSION,
            "atomic_unit_count": len(units),
            "hard_bucket_count": len(buckets),
            "mimo_bucket_count": sum(len(bucket["units"]) > 1 for bucket in buckets),
            "single_member_bucket_count": sum(
                len(bucket["units"]) == 1 for bucket in buckets
            ),
            "cluster_count": len(cluster_summaries),
            "multi_member_cluster_count": sum(
                cluster["member_count"] > 1 for cluster in cluster_summaries
            ),
            "split_request_count": len(split_requests),
            "review_request_count": len(review_requests),
            "pending_bucket_count": len(pending_buckets),
            "unassigned_atomic_count": len(unassigned_ids),
            "duplicate_assignment_count": duplicate_count,
            "clustering_standard": "簇内所有知识点必须能够共用同一条标准答疑知识",
        },
        "atomic_units": units,
        "clusters": cluster_summaries,
        "split_requests": split_requests,
        "review_requests": review_requests,
        "pending_buckets": pending_buckets,
        "unassigned_atomic_ids": unassigned_ids,
    }


def _load_units(input_path: Path) -> list[dict[str, Any]]:
    payload = _read_json(input_path)
    try:
        units = payload["schemes"]["new"]["units"]
    except (KeyError, TypeError) as exc:
        raise ValueError(
            "输入 JSON 缺少 schemes.new.units，无法读取新版原子知识点"
        ) from exc
    if not isinstance(units, list) or not units:
        raise ValueError("输入 JSON 中没有可聚类的新版原子知识点")
    atomic_ids = [_text(unit.get("unit_id")) for unit in units]
    if any(not atomic_id for atomic_id in atomic_ids):
        raise ValueError("输入原子知识点存在空 unit_id")
    if len(atomic_ids) != len(set(atomic_ids)):
        raise ValueError("输入原子知识点 unit_id 重复")
    return [dict(unit) for unit in units]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将新版原子知识点直接归并为一到多个可共用标准答疑知识的主题簇。"
    )
    parser.add_argument("--input-json", type=Path, required=True)
    parser.add_argument("--cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--max-new-buckets", type=int, default=1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()

    units = _load_units(args.input_json)
    buckets, review_units = _bucket_units(units)
    cache: dict[str, Any] = (
        _read_json(args.cache_json) if args.cache_json.exists() else {}
    )
    api_buckets = [bucket for bucket in buckets if len(bucket["units"]) > 1]
    missing_buckets = [
        bucket
        for bucket in api_buckets
        if not _cache_entry_valid(
            cache.get(bucket["bucket_id"]),
            bucket,
            retry_errors=args.retry_errors,
        )
    ]
    if args.max_new_buckets < 0:
        batch = missing_buckets
    else:
        batch = missing_buckets[: args.max_new_buckets]

    if batch:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo 未配置，无法运行整批原子知识主题聚类")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_analyze_bucket, client, bucket): bucket
                for bucket in batch
            }
            for future in as_completed(futures):
                bucket = futures[future]
                cache[bucket["bucket_id"]] = future.result()
                _write_json(args.cache_json, cache)

    output = _build_output(
        input_path=args.input_json,
        units=units,
        buckets=buckets,
        review_units=review_units,
        cache=cache,
    )
    _write_json(args.output_json, output)
    metadata = output["metadata"]
    print(
        json.dumps(
            {
                "status": metadata["status"],
                "atomic_units": metadata["atomic_unit_count"],
                "hard_buckets": metadata["hard_bucket_count"],
                "mimo_buckets": metadata["mimo_bucket_count"],
                "cached_mimo_buckets": sum(
                    _cache_entry_valid(
                        cache.get(bucket["bucket_id"]),
                        bucket,
                        retry_errors=False,
                    )
                    for bucket in api_buckets
                ),
                "remaining_mimo_buckets": metadata["pending_bucket_count"],
                "clusters": metadata["cluster_count"],
                "review_requests": metadata["review_request_count"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
