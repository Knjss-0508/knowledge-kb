from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
from typing import Any

from answer_hub.mimo import (
    MimoClient,
    MimoError,
    TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
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


def _unique(values: list[Any], limit: int = 20) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def _title_input(
    cluster: dict[str, Any],
    unit_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    units = [
        unit_by_id[atomic_id]
        for atomic_id in cluster.get("member_atomic_ids", [])
        if atomic_id in unit_by_id
    ]
    return {
        "theme_id": cluster["cluster_id"],
        "product_categories": _unique(
            [unit.get("product_category") for unit in units]
        ),
        "normalized_issues": _unique(
            [unit.get("normalized_issue") for unit in units],
            limit=8,
        ),
        "intents": _unique([unit.get("intent") for unit in units]),
        "subjects": _unique([unit.get("subject") for unit in units]),
        "phenomena": _unique([unit.get("phenomenon") for unit in units]),
        "judgment_targets": _unique(
            [unit.get("judgment_target") for unit in units]
        ),
        "resolution_modes": _unique(
            [unit.get("resolution_mode") for unit in units]
        ),
    }


def _input_hash(value: dict[str, Any]) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_entry_is_current(
    value: dict[str, Any],
    cache: dict[str, Any],
    *,
    retry_errors: bool,
) -> bool:
    entry = cache.get(value["theme_id"])
    if not isinstance(entry, dict):
        return False
    if entry.get("prompt_version") != TOPIC_DISPLAY_QUESTION_PROMPT_VERSION:
        return False
    if entry.get("input_hash") != _input_hash(value):
        return False
    if retry_errors and entry.get("status") == "error":
        return False
    return entry.get("status") in {"ok", "error"}


def _rewrite_batch(
    client: MimoClient,
    batch: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    try:
        result = client.rewrite_topic_display_questions(batch)
        questions = {
            item["theme_id"]: item["question"]
            for item in result.candidate["questions"]
        }
        return {
            item["theme_id"]: {
                "status": "ok",
                "theme_title": questions[item["theme_id"]],
                "model": result.request_audit.get(
                    "model",
                    client.config.model,
                ),
                "prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
                "input_hash": _input_hash(item),
            }
            for item in batch
        }
    except MimoError as exc:
        return {
            item["theme_id"]: {
                "status": "error",
                "error": str(exc),
                "theme_title": "",
                "model": client.config.model,
                "prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
                "input_hash": _input_hash(item),
            }
            for item in batch
        }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="为完整1-N主题簇生成简短、自然的主题标题。"
    )
    parser.add_argument("--cluster-json", type=Path, required=True)
    parser.add_argument("--cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--max-new", type=int, default=-1)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retry-errors", action="store_true")
    args = parser.parse_args()

    payload = _read_json(args.cluster_json)
    clusters = payload.get("clusters") or []
    units = payload.get("atomic_units") or []
    unit_by_id = {
        str(unit.get("unit_id", "")).strip(): unit
        for unit in units
        if str(unit.get("unit_id", "")).strip()
    }
    title_inputs = [
        _title_input(cluster, unit_by_id)
        for cluster in clusters
    ]
    cache: dict[str, Any] = (
        _read_json(args.cache_json) if args.cache_json.exists() else {}
    )
    missing = [
        item
        for item in title_inputs
        if not _cache_entry_is_current(
            item,
            cache,
            retry_errors=args.retry_errors,
        )
    ]
    if args.max_new >= 0:
        missing = missing[: args.max_new]

    if missing:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo未配置，无法生成主题标题")
        batch_size = max(1, min(args.batch_size, 20))
        batches = [
            missing[index : index + batch_size]
            for index in range(0, len(missing), batch_size)
        ]
        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
            futures = {
                executor.submit(_rewrite_batch, client, batch): batch
                for batch in batches
            }
            for completed, future in enumerate(as_completed(futures), start=1):
                cache.update(future.result())
                _write_json(args.cache_json, cache)
                print(
                    json.dumps(
                        {
                            "progress": f"{completed}/{len(futures)}",
                            "completed_titles": sum(
                                entry.get("status") == "ok"
                                for entry in cache.values()
                            ),
                        },
                        ensure_ascii=False,
                    ),
                    flush=True,
                )

    enriched_clusters: list[dict[str, Any]] = []
    for cluster in clusters:
        entry = cache.get(cluster["cluster_id"], {})
        enriched_clusters.append(
            {
                **cluster,
                "theme_title": (
                    entry.get("theme_title")
                    or cluster.get("theme_name")
                    or ""
                ),
                "title_status": entry.get("status", "pending"),
                "title_error": entry.get("error", ""),
                "title_model": entry.get("model", ""),
                "title_prompt_version": entry.get(
                    "prompt_version",
                    TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
                ),
            }
        )

    output = {
        **payload,
        "metadata": {
            **(payload.get("metadata") or {}),
            "title_count": len(enriched_clusters),
            "title_completed_count": sum(
                cluster["title_status"] == "ok"
                for cluster in enriched_clusters
            ),
            "title_error_count": sum(
                cluster["title_status"] == "error"
                for cluster in enriched_clusters
            ),
            "title_pending_count": sum(
                cluster["title_status"] == "pending"
                for cluster in enriched_clusters
            ),
            "title_prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
        },
        "clusters": enriched_clusters,
    }
    _write_json(args.output_json, output)
    print(json.dumps(output["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
