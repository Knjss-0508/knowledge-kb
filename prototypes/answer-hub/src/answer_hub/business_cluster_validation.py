from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from .excel_io import write_rows_to_workbook
from .mimo import MimoClient
from .workflow import _clean_text, _direct_mimo_topic_groups, _topic_id


CATEGORY_SUMMARY_COLUMNS = [
    "产品品类",
    "运行状态",
    "源记录数",
    "原子问题数",
    "主题簇数",
    "多成员主题簇数",
    "原子提取调用数",
    "直接聚类调用数",
    "模型失败数",
    "备注",
]

CLUSTER_REVIEW_COLUMNS = [
    "主题簇ID",
    "产品品类",
    "主题标题",
    "主题簇成员数",
    "原子问题ID",
    "数据ID",
    "工单ID",
    "核心问题",
    "完整聊天",
    "对象/部位",
    "异常现象",
    "解题方式",
    "聚类决策",
    "聚类提供方",
    "聚类理由",
    "人工归簇判断",
    "人工动作",
    "合并目标主题簇ID",
    "人工主题ID",
    "人工主题标题",
    "人工备注",
]


def _group_rows_by_product_category(
    rows: list[dict[str, Any]],
) -> list[tuple[str, list[dict[str, Any]]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        category = _clean_text(row.get("产品类型")) or "未知品类"
        grouped.setdefault(category, []).append(dict(row))
    return sorted(grouped.items(), key=lambda item: item[0])


def _run_one_category(
    product_category: str,
    rows: list[dict[str, Any]],
    reviewer_factory: Callable[[], MimoClient],
    *,
    batch_size: int,
    atomic_max_workers: int | None,
) -> dict[str, Any]:
    try:
        reviewer = reviewer_factory()
        topic_groups, clustering_meta = _direct_mimo_topic_groups(
            rows,
            reviewer,
            batch_size=batch_size,
            atomic_max_workers=atomic_max_workers,
        )
        return {
            "product_category": product_category,
            "status": "completed",
            "source_rows": rows,
            "topic_groups": topic_groups,
            "clustering_meta": clustering_meta,
            "error": "",
        }
    except Exception as exc:
        return {
            "product_category": product_category,
            "status": "failed",
            "source_rows": rows,
            "topic_groups": [],
            "clustering_meta": {},
            "error": str(exc),
        }


def run_category_cluster_jobs(
    rows: list[dict[str, Any]],
    reviewer_factory: Callable[[], MimoClient],
    *,
    category_workers: int = 1,
    batch_size: int = 40,
    on_category_completed: Callable[[dict[str, Any]], None] | None = None,
) -> list[dict[str, Any]]:
    """Cluster each product category independently and notify after every category."""
    category_jobs = _group_rows_by_product_category(rows)
    workers = max(1, min(category_workers, len(category_jobs) or 1))
    atomic_max_workers = 1 if workers > 1 else None

    if workers == 1:
        results = []
        for product_category, category_rows in category_jobs:
            result = _run_one_category(
                product_category,
                category_rows,
                reviewer_factory,
                batch_size=batch_size,
                atomic_max_workers=atomic_max_workers,
            )
            results.append(result)
            if on_category_completed:
                on_category_completed(result)
        return results

    results: list[dict[str, Any]] = []
    with ThreadPoolExecutor(
        max_workers=workers,
        thread_name_prefix="answer-hub-category",
    ) as executor:
        futures = {
            executor.submit(
                _run_one_category,
                product_category,
                category_rows,
                reviewer_factory,
                batch_size=batch_size,
                atomic_max_workers=atomic_max_workers,
            ): product_category
            for product_category, category_rows in category_jobs
        }
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_category_completed:
                on_category_completed(result)
    return sorted(results, key=lambda item: str(item["product_category"]))


def _cluster_title(rows: list[dict[str, Any]]) -> str:
    titles = [
        _clean_text(row.get("_聚类主题标题"))
        for row in rows
        if _clean_text(row.get("_聚类主题标题"))
    ]
    if titles:
        return max(titles, key=len)
    problems = [
        _clean_text(row.get("核心问题"))
        for row in rows
        if _clean_text(row.get("核心问题"))
    ]
    return max(problems, key=len) if problems else "待人工命名主题"


def _review_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        product_category = str(result["product_category"])
        for group_key, member_rows in result["topic_groups"]:
            topic_id = _topic_id(group_key)
            title = _cluster_title(member_rows)
            for member in member_rows:
                rows.append(
                    {
                        "主题簇ID": topic_id,
                        "产品品类": product_category,
                        "主题标题": title,
                        "主题簇成员数": len(member_rows),
                        "原子问题ID": _clean_text(member.get("_原子知识ID")),
                        "数据ID": _clean_text(member.get("数据ID")),
                        "工单ID": _clean_text(member.get("工单ID")),
                        "核心问题": _clean_text(member.get("核心问题")),
                        "完整聊天": _clean_text(member.get("聊天内容")),
                        "对象/部位": _clean_text(member.get("对象/部位")),
                        "异常现象": _clean_text(member.get("异常现象")),
                        "解题方式": _clean_text(member.get("解题方式")),
                        "聚类决策": _clean_text(member.get("_聚类决策")),
                        "聚类提供方": _clean_text(member.get("_聚类裁决提供方")),
                        "聚类理由": _clean_text(member.get("_聚类裁决原因")),
                        "人工归簇判断": "",
                        "人工动作": "",
                        "合并目标主题簇ID": "",
                        "人工主题ID": "",
                        "人工主题标题": "",
                        "人工备注": "",
                    }
                )
    return sorted(
        rows,
        key=lambda item: (
            item["产品品类"],
            item["主题簇ID"],
            item["原子问题ID"],
        ),
    )


def _summary_rows(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in results:
        meta = dict(result.get("clustering_meta") or {})
        groups = list(result.get("topic_groups") or [])
        notes = [_clean_text(result.get("error"))]
        failed_calls = int(meta.get("direct_cluster_failed", 0) or 0)
        if failed_calls:
            notes.append(f"直接聚类失败{failed_calls}次")
        retry_splits = int(meta.get("direct_cluster_retry_splits", 0) or 0)
        retry_succeeded = int(meta.get("direct_cluster_retry_succeeded", 0) or 0)
        if retry_splits:
            notes.append(f"已拆批重试{retry_splits}次，成功{retry_succeeded}个子批")
        exhausted = int(meta.get("direct_cluster_retry_exhausted_batches", 0) or 0)
        if exhausted:
            notes.append(f"最小批仍失败{exhausted}个，已保守单例")
        last_error = _clean_text(meta.get("direct_cluster_last_error"))
        if last_error:
            notes.append(f"最近失败：{last_error}")
        rows.append(
            {
                "产品品类": result["product_category"],
                "运行状态": result["status"],
                "源记录数": len(result.get("source_rows") or []),
                "原子问题数": meta.get("atomic_unit_count", 0),
                "主题簇数": len(groups),
                "多成员主题簇数": sum(
                    len(member_rows) > 1 for _key, member_rows in groups
                ),
                "原子提取调用数": meta.get("atomic_extraction_calls", 0),
                "直接聚类调用数": meta.get("direct_cluster_calls", 0),
                "模型失败数": (
                    meta.get("atomic_extraction_failed", 0)
                    + meta.get("direct_cluster_failed", 0)
                ),
                "备注": "；".join(note for note in notes if note),
            }
        )
    return sorted(rows, key=lambda item: item["产品品类"])


def write_cluster_validation_workbook(
    workbook_path: str | Path,
    results: list[dict[str, Any]],
    *,
    source_row_count: int,
    excluded_rows: list[dict[str, Any]],
    expected_category_count: int | None = None,
) -> None:
    review_rows = _review_rows(results)
    summary_rows = _summary_rows(results)
    expected_count = expected_category_count or len(summary_rows)
    summary_rows.insert(
        0,
        {
            "产品品类": "全部",
            "运行状态": (
                "completed"
                if len(summary_rows) == expected_count
                and all(row["运行状态"] == "completed" for row in summary_rows)
                else "partial"
            ),
            "源记录数": source_row_count,
            "原子问题数": sum(
                int(row["原子问题数"]) for row in summary_rows
            ),
            "主题簇数": sum(int(row["主题簇数"]) for row in summary_rows),
            "多成员主题簇数": sum(
                int(row["多成员主题簇数"]) for row in summary_rows
            ),
            "原子提取调用数": sum(
                int(row["原子提取调用数"]) for row in summary_rows
            ),
            "直接聚类调用数": sum(
                int(row["直接聚类调用数"]) for row in summary_rows
            ),
            "模型失败数": sum(int(row["模型失败数"]) for row in summary_rows),
            "备注": "仅执行原子问题提取与主题聚类；未调用 CZ，未生成或发布知识。",
        },
    )
    write_rows_to_workbook(
        {
            "主题簇审核总表": (CLUSTER_REVIEW_COLUMNS, review_rows),
            "品类运行汇总": (CATEGORY_SUMMARY_COLUMNS, summary_rows),
            "未进入聚类记录": (
                ["数据ID", "工单ID", "产品类型", "核心问题", "排除原因"],
                excluded_rows,
            ),
        },
        workbook_path,
    )
