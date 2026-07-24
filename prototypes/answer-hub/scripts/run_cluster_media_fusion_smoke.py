from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import copy
import hashlib
import json
from pathlib import Path
from typing import Any

from answer_hub.mimo import (
    CLUSTER_FUSION_PROMPT_VERSION,
    MimoClient,
    MimoError,
)


DEFAULT_SAMPLE_IDS = (
    "S005",
    "S012",
    "S022",
    "S054",
    "S011",
    "S048",
    "S018",
    "S001",
)


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


def _input_hash(
    row: dict[str, Any],
    text_entry: dict[str, Any],
    media_entry: dict[str, Any],
) -> str:
    payload = {
        "sample_id": row.get("样本ID"),
        "text_candidate": text_entry.get("candidate"),
        "media_candidate": media_entry.get("candidate"),
        "media": media_entry.get("media"),
    }
    raw = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _fallback_candidate(
    text_entry: dict[str, Any],
    media_entry: dict[str, Any],
    error: str,
) -> dict[str, Any]:
    candidate = copy.deepcopy(text_entry.get("candidate") or {})
    if not candidate.get("topics"):
        candidate = copy.deepcopy(media_entry.get("candidate") or {})
    media_candidate = media_entry.get("candidate") or {}
    candidate["media_analysis"] = copy.deepcopy(
        media_candidate.get("media_analysis")
        or {
            "image_summary": "融合失败，保留文字主题。",
            "video_summary": "融合失败，保留文字主题。",
            "media_relevance": "无法读取",
            "used_for_topic_split": False,
            "requires_review": True,
        }
    )
    candidate["media_analysis"]["requires_review"] = True
    candidate["reason"] = (
        f"媒体融合失败，已保守保留文字主题并转人工复核：{error[:160]}"
    )
    for topic in candidate.get("topics") or []:
        topic["requires_review"] = True
    return candidate


def _fuse_one(
    client: MimoClient,
    row: dict[str, Any],
    text_entry: dict[str, Any],
    media_entry: dict[str, Any],
) -> dict[str, Any]:
    sample_id = row["样本ID"]
    input_hash = _input_hash(row, text_entry, media_entry)
    try:
        result = client.fuse_cluster_units(
            row,
            text_entry["candidate"],
            media_entry["candidate"],
            media_entry.get("media") or {},
        )
        return {
            "sample_id": sample_id,
            "status": "ok",
            "text_type": text_entry["candidate"].get("conversation_type"),
            "media_type": media_entry["candidate"].get("conversation_type"),
            "fusion_type": result.candidate.get("conversation_type"),
            "candidate": result.candidate,
            "model": result.request_audit.get("model"),
            "prompt_version": CLUSTER_FUSION_PROMPT_VERSION,
            "input_hash": input_hash,
        }
    except MimoError as exc:
        return {
            "sample_id": sample_id,
            "status": "error",
            "error": str(exc),
            "text_type": text_entry["candidate"].get("conversation_type"),
            "media_type": media_entry["candidate"].get("conversation_type"),
            "fusion_type": (
                text_entry.get("candidate") or {}
            ).get("conversation_type", "uncertain"),
            "candidate": _fallback_candidate(
                text_entry,
                media_entry,
                str(exc),
            ),
            "model": client.config.model,
            "prompt_version": CLUSTER_FUSION_PROMPT_VERSION,
            "input_hash": input_hash,
        }


def _cache_entry_is_current(
    row: dict[str, Any],
    text_entry: dict[str, Any],
    media_entry: dict[str, Any],
    entry: Any,
    *,
    retry_errors: bool,
) -> bool:
    if not isinstance(entry, dict):
        return False
    if entry.get("prompt_version") != CLUSTER_FUSION_PROMPT_VERSION:
        return False
    if entry.get("input_hash") != _input_hash(row, text_entry, media_entry):
        return False
    if retry_errors and entry.get("status") == "error":
        return False
    return entry.get("status") in {"ok", "error"}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-json", type=Path, required=True)
    parser.add_argument("--text-cache-json", type=Path, required=True)
    parser.add_argument("--media-cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument(
        "--sample-ids",
        default=",".join(DEFAULT_SAMPLE_IDS),
        help="逗号分隔的样本ID",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="处理输入文件中的全部样本",
    )
    parser.add_argument("--cache-json", type=Path)
    parser.add_argument("--max-new", type=int, default=-1)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()

    rows = _read_json(args.sample_json)
    text_cache = _read_json(args.text_cache_json)
    media_cache = _read_json(args.media_cache_json)
    requested_ids = (
        {row["样本ID"] for row in rows}
        if args.all
        else {
            item.strip()
            for item in args.sample_ids.split(",")
            if item.strip()
        }
    )
    selected_rows = [row for row in rows if row["样本ID"] in requested_ids]
    missing = requested_ids - {row["样本ID"] for row in selected_rows}
    if missing:
        raise RuntimeError(f"样本不存在：{', '.join(sorted(missing))}")

    missing_cache_entries = [
        row
        for row in selected_rows
        if row["样本ID"] not in text_cache
        or row["样本ID"] not in media_cache
    ]
    if missing_cache_entries:
        raise RuntimeError(
            "文字或媒体缓存缺少样本："
            + ", ".join(row["样本ID"] for row in missing_cache_entries[:20])
        )

    cache_path = args.cache_json or args.output_json.with_name(
        f"{args.output_json.stem}_cache.json"
    )
    fused_results: dict[str, Any] = (
        _read_json(cache_path) if cache_path.exists() else {}
    )
    missing_rows = [
        row
        for row in selected_rows
        if not _cache_entry_is_current(
            row,
            text_cache[row["样本ID"]],
            media_cache[row["样本ID"]],
            fused_results.get(row["样本ID"]),
            retry_errors=args.retry_errors,
        )
    ]
    batch = (
        missing_rows
        if args.max_new < 0
        else missing_rows[: max(0, args.max_new)]
    )
    if batch:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo未配置")
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(
                    _fuse_one,
                    client,
                    row,
                    text_cache[row["样本ID"]],
                    media_cache[row["样本ID"]],
                ): row["样本ID"]
                for row in batch
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                sample_id = futures[future]
                result = future.result()
                fused_results[sample_id] = result
                _write_json(cache_path, fused_results)
                print(
                    json.dumps(
                        {
                            "progress": f"{completed}/{len(futures)}",
                            "sample_id": sample_id,
                            "status": result["status"],
                            "text_type": result["text_type"],
                            "media_type": result["media_type"],
                            "fusion_type": result["fusion_type"],
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    payload = {
        "sample_ids": sorted(requested_ids),
        "model": next(
            (
                item.get("model", "")
                for item in fused_results.values()
                if item.get("model")
            ),
            "",
        ),
        "prompt_version": CLUSTER_FUSION_PROMPT_VERSION,
        "results": {
            row["样本ID"]: fused_results[row["样本ID"]]
            for row in selected_rows
            if row["样本ID"] in fused_results
        },
    }
    _write_json(args.output_json, payload)
    remaining = [
        row["样本ID"]
        for row in selected_rows
        if not _cache_entry_is_current(
            row,
            text_cache[row["样本ID"]],
            media_cache[row["样本ID"]],
            fused_results.get(row["样本ID"]),
            retry_errors=False,
        )
    ]
    print(
        json.dumps(
            {
                "tested": len(payload["results"]),
                "ok": sum(
                    item["status"] == "ok"
                    for item in payload["results"].values()
                ),
                "errors": sum(
                    item["status"] == "error"
                    for item in payload["results"].values()
                ),
                "remaining": len(remaining),
                "cache_json": str(cache_path),
                "output_json": str(args.output_json),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
