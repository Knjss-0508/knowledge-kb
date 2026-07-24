from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from answer_hub.mimo import CLUSTER_UNIT_PROMPT_VERSION


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


def _row_fingerprint(row: dict[str, Any]) -> str:
    payload = "\n".join(
        str(row.get(field, "") or "").strip()
        for field in (
            "源记录键",
            "工单ID",
            "核心问题",
            "聊天内容",
            "产品类型",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target-sample", type=Path, required=True)
    parser.add_argument("--target-cache", type=Path, required=True)
    parser.add_argument("--source-sample", type=Path, action="append", default=[])
    parser.add_argument("--source-cache", type=Path, action="append", default=[])
    args = parser.parse_args()

    if len(args.source_sample) != len(args.source_cache):
        raise ValueError("source-sample 与 source-cache 数量必须一致")

    target_rows = _read_json(args.target_sample)
    target_cache: dict[str, Any] = (
        _read_json(args.target_cache) if args.target_cache.exists() else {}
    )
    source_entries: dict[str, dict[str, Any]] = {}
    stale_entries = 0
    for sample_path, cache_path in zip(
        args.source_sample,
        args.source_cache,
        strict=True,
    ):
        source_rows = _read_json(sample_path)
        source_cache = _read_json(cache_path)
        for row in source_rows:
            entry = source_cache.get(row["样本ID"])
            if not isinstance(entry, dict):
                continue
            if (
                entry.get("status") != "ok"
                or entry.get("prompt_version") != CLUSTER_UNIT_PROMPT_VERSION
            ):
                stale_entries += 1
                continue
            source_entries[_row_fingerprint(row)] = entry

    seeded = 0
    for row in target_rows:
        fingerprint = _row_fingerprint(row)
        entry = source_entries.get(fingerprint)
        if not entry:
            continue
        target_cache[row["样本ID"]] = entry
        seeded += 1

    _write_json(args.target_cache, target_cache)
    print(
        json.dumps(
            {
                "target_rows": len(target_rows),
                "current_source_entries": len(source_entries),
                "stale_source_entries": stale_entries,
                "seeded_rows": seeded,
                "remaining_rows": len(target_rows) - seeded,
                "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
