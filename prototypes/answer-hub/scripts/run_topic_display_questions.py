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


def _question_input(theme: dict[str, Any]) -> dict[str, Any]:
    return {
        "theme_id": str(theme.get("theme_id", "")).strip(),
        "product_categories": theme.get("product_categories", []),
        "normalized_issues": theme.get("normalized_issues", []),
        "intents": theme.get("intents", []),
        "subjects": theme.get("subjects", []),
        "phenomena": theme.get("phenomena", []),
        "judgment_targets": theme.get("judgment_targets", []),
        "resolution_modes": theme.get("resolution_modes", []),
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
) -> bool:
    entry = cache.get(value["theme_id"])
    return bool(
        isinstance(entry, dict)
        and entry.get("status") == "ok"
        and entry.get("prompt_version") == TOPIC_DISPLAY_QUESTION_PROMPT_VERSION
        and entry.get("input_hash") == _input_hash(value)
        and str(entry.get("question", "")).endswith("？")
        and str(entry.get("question", "")).count("？") == 1
        and "?" not in str(entry.get("question", ""))
    )


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
                "question": questions[item["theme_id"]],
                "model": result.request_audit.get("model", client.config.model),
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
                "question": "",
                "model": client.config.model,
                "prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
                "input_hash": _input_hash(item),
            }
            for item in batch
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic-json", type=Path, required=True)
    parser.add_argument("--cache-json", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=12)
    parser.add_argument("--max-new", type=int, default=24)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args()

    topic_payload = _read_json(args.topic_json)
    themes = topic_payload.get("themes", [])
    question_inputs = [_question_input(theme) for theme in themes]
    if not question_inputs or any(not item["theme_id"] for item in question_inputs):
        raise ValueError("主题结果缺少有效 themes/theme_id")

    cache: dict[str, Any] = (
        _read_json(args.cache_json) if args.cache_json.exists() else {}
    )
    missing = [
        item
        for item in question_inputs
        if not _cache_entry_is_current(item, cache)
    ][: max(0, args.max_new)]

    if missing:
        client = MimoClient.from_env()
        if client is None:
            raise RuntimeError("MiMo 未配置，无法生成组员易懂主题问句")
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
            for future in as_completed(futures):
                cache.update(future.result())
                _write_json(args.cache_json, cache)

    rows = [
        {
            "theme_id": item["theme_id"],
            "question": cache.get(item["theme_id"], {}).get("question", ""),
            "status": cache.get(item["theme_id"], {}).get("status", "pending"),
            "error": cache.get(item["theme_id"], {}).get("error", ""),
            "model": cache.get(item["theme_id"], {}).get("model", ""),
            "prompt_version": cache.get(item["theme_id"], {}).get(
                "prompt_version",
                TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
            ),
        }
        for item in question_inputs
    ]
    output = {
        "metadata": {
            "theme_count": len(rows),
            "completed_count": sum(row["status"] == "ok" for row in rows),
            "pending_count": sum(row["status"] == "pending" for row in rows),
            "error_count": sum(row["status"] == "error" for row in rows),
            "prompt_version": TOPIC_DISPLAY_QUESTION_PROMPT_VERSION,
        },
        "questions": rows,
    }
    _write_json(args.output_json, output)
    print(json.dumps(output["metadata"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
