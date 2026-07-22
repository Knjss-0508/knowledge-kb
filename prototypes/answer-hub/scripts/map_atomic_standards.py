from __future__ import annotations

import argparse
import json
from pathlib import Path
import traceback
from typing import Any

from answer_hub.atomic_standard_mapper import (
    load_human_annotations,
    map_atomic_units_to_standards,
)
from answer_hub.catalog import load_standard_catalog
from answer_hub.embedding import EmbeddingClient, PersistentEmbeddingClient


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _load_atomic_units(path: Path) -> list[dict[str, Any]]:
    payload = _read_json(path)
    units = payload.get("atomic_units")
    if not isinstance(units, list):
        try:
            units = payload["schemes"]["new"]["units"]
        except (KeyError, TypeError) as exc:
            raise ValueError("输入 JSON 中未找到 atomic_units 或 schemes.new.units") from exc
    if not units:
        raise ValueError("输入 JSON 中没有原子知识点")
    return [dict(unit) for unit in units]


def main() -> None:
    parser = argparse.ArgumentParser(
        description="将原子知识点映射到正式质检标准Top候选，不自动覆盖人工结论。"
    )
    parser.add_argument("--atomic-json", type=Path, required=True)
    parser.add_argument("--standards-json", type=Path, required=True)
    parser.add_argument("--review-xlsx", type=Path)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--no-embedding", action="store_true")
    parser.add_argument("--embedding-cache", type=Path)
    args = parser.parse_args()

    units = _load_atomic_units(args.atomic_json)
    standards = load_standard_catalog(args.standards_json)
    annotations = (
        load_human_annotations(args.review_xlsx)
        if args.review_xlsx
        else {}
    )
    embedding_client = None if args.no_embedding else EmbeddingClient.from_env()
    if embedding_client is not None:
        cache_path = (
            args.embedding_cache
            or args.output_json.parent / "qwen_embedding_cache.sqlite3"
        )
        embedding_client = PersistentEmbeddingClient(
            embedding_client,
            cache_path,
        )

    def report_progress(stage: str, completed: int, total: int) -> None:
        print(
            json.dumps(
                {
                    "stage": stage,
                    "completed": completed,
                    "total": total,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )

    try:
        output = map_atomic_units_to_standards(
            units,
            standards,
            annotations,
            top_k=max(1, args.top_k),
            embedding_client=embedding_client,
            progress_callback=report_progress,
        )
    except Exception:
        error_path = args.output_json.with_suffix(".error.log")
        error_path.parent.mkdir(parents=True, exist_ok=True)
        error_path.write_text(traceback.format_exc(), encoding="utf-8")
        raise
    if args.no_embedding:
        output["metadata"]["embedding_status"] = "disabled"
    output["metadata"].update(
        {
            "atomic_json": str(args.atomic_json),
            "standards_json": str(args.standards_json),
            "review_xlsx": str(args.review_xlsx or ""),
        }
    )
    _write_json(args.output_json, output)
    print(
        json.dumps(
            {
                "atomic_units": output["metadata"]["atomic_unit_count"],
                "standards": output["metadata"]["standard_count"],
                "mapped": output["metadata"]["mapped_count"],
                "review_required": output["metadata"]["review_required_count"],
                "embedding_status": output["metadata"]["embedding_status"],
                "output": str(args.output_json),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
