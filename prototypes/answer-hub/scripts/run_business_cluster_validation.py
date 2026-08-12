from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import re
from threading import Lock
from typing import Any

from answer_hub.business_cluster_validation import (
    run_category_cluster_jobs,
    write_cluster_validation_workbook,
)
from answer_hub.excel_io import read_workbook_rows
from answer_hub.mimo import MimoClient
from answer_hub.workflow import (
    filter_preprocessed_rows_for_model,
    preprocess_source_rows,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _safe_name(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*]+', "_", value).strip(" .")
    return cleaned or "未知品类"


def _jsonable_result(result: dict[str, Any]) -> dict[str, Any]:
    return {
        **result,
        "topic_groups": [
            {
                "key": list(key),
                "rows": member_rows,
            }
            for key, member_rows in result.get("topic_groups", [])
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="按产品品类分批执行 direct_mimo 聚类，并增量汇总人工审核表。",
    )
    parser.add_argument(
        "--input-xlsx",
        type=Path,
        default=Path("data") / "质检答疑案例库 (4).xlsx",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("outputs") / "business-cluster-validation-by-category",
    )
    parser.add_argument(
        "--category-workers",
        type=int,
        default=1,
        help="同时运行的产品品类数；真实 MiMo 建议从 1 或 2 开始。",
    )
    parser.add_argument("--batch-size", type=int, default=40)
    args = parser.parse_args()

    if args.category_workers < 1:
        raise ValueError("--category-workers 必须大于等于 1")
    if args.batch_size < 1:
        raise ValueError("--batch-size 必须大于等于 1")
    if not args.input_xlsx.is_file():
        raise FileNotFoundError(f"输入文件不存在：{args.input_xlsx}")
    if MimoClient.from_env() is None:
        raise RuntimeError("MiMo 未配置；本脚本只执行本地 Excel 的 direct_mimo 聚类。")

    _headers, source_rows = read_workbook_rows(args.input_xlsx)
    preprocessed_rows = preprocess_source_rows(source_rows)
    eligible_rows, excluded_rows = filter_preprocessed_rows_for_model(
        preprocessed_rows
    )
    run_dir = args.output_dir / datetime.now().strftime("%Y%m%d-%H%M%S")
    categories_dir = run_dir / "categories"
    total_workbook = run_dir / "主题簇审核总表.xlsx"
    checkpoint_path = run_dir / "category_checkpoint.json"
    results: list[dict[str, Any]] = []
    results_lock = Lock()
    category_count = len(
        {str(row.get("产品类型") or "未知品类") for row in eligible_rows}
    )

    def on_category_completed(result: dict[str, Any]) -> None:
        category = str(result["product_category"])
        category_dir = categories_dir / _safe_name(category)
        category_dir.mkdir(parents=True, exist_ok=True)
        _write_json(category_dir / "cluster_result.json", _jsonable_result(result))
        write_cluster_validation_workbook(
            category_dir / "主题簇审核表.xlsx",
            [result],
            source_row_count=len(result.get("source_rows") or []),
            excluded_rows=[],
            expected_category_count=1,
        )
        with results_lock:
            results[:] = [
                item
                for item in results
                if item["product_category"] != category
            ]
            results.append(result)
            results.sort(key=lambda item: str(item["product_category"]))
            completed_count = len(results)
            _write_json(
                checkpoint_path,
                {
                    "input_xlsx": str(args.input_xlsx),
                    "source_row_count": len(source_rows),
                    "eligible_row_count": len(eligible_rows),
                    "excluded_row_count": len(excluded_rows),
                    "expected_category_count": category_count,
                    "completed_category_count": completed_count,
                    "status": (
                        "completed"
                        if completed_count == category_count
                        else "partial"
                    ),
                    "results": [
                        _jsonable_result(item) for item in results
                    ],
                },
            )
            write_cluster_validation_workbook(
                total_workbook,
                results,
                source_row_count=len(source_rows),
                excluded_rows=excluded_rows,
                expected_category_count=category_count,
            )
        print(
            json.dumps(
                {
                    "category": category,
                    "status": result["status"],
                    "progress": f"{completed_count}/{category_count}",
                    "clusters": len(result.get("topic_groups") or []),
                    "output": str(category_dir),
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    results = run_category_cluster_jobs(
        eligible_rows,
        reviewer_factory=lambda: MimoClient.from_env()
        or (_ for _ in ()).throw(RuntimeError("MiMo 配置已失效")),
        category_workers=args.category_workers,
        batch_size=args.batch_size,
        on_category_completed=on_category_completed,
    )
    if not total_workbook.is_file():
        write_cluster_validation_workbook(
            total_workbook,
            results,
            source_row_count=len(source_rows),
            excluded_rows=excluded_rows,
            expected_category_count=category_count,
        )
    print(
        json.dumps(
            {
                "status": "completed",
                "source_rows": len(source_rows),
                "eligible_rows": len(eligible_rows),
                "excluded_rows": len(excluded_rows),
                "categories": len(results),
                "clusters": sum(
                    len(result.get("topic_groups") or []) for result in results
                ),
                "workbook": str(total_workbook),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
