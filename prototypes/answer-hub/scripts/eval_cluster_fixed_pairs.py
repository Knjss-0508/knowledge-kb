from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.run_cluster_ab_test import SchemeResult, _prediction, _source_state


VALID_GOLD_LABELS = {"同一主题", "不同主题", "多主题需拆分"}


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _text(value: object) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return "" if text.lower() == "nan" else text


def _load_gold_rows(path: Path, sheet_name: str) -> list[dict[str, Any]]:
    frame = pd.read_excel(path, sheet_name=sheet_name)
    return [dict(row) for _, row in frame.iterrows()]


def _states_from_result(
    result_payload: dict[str, Any],
    sample_rows: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    units = result_payload.get("schemes", {}).get("new", {}).get("units") or []
    scheme = SchemeResult(units=units, assignments={}, similarities={})
    return _source_state(scheme, sample_rows, new_scheme=True)


def evaluate_fixed_pairs(
    result_payload: dict[str, Any],
    sample_rows: list[dict[str, Any]],
    gold_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    states = _states_from_result(result_payload, sample_rows)
    by_label: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    wrong: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    correct = 0
    total = 0

    for row in gold_rows:
        gold = _text(row.get("人工判断"))
        pair_id = _text(row.get("配对ID"))
        left_id = _text(row.get("会话A_样本ID"))
        right_id = _text(row.get("会话B_样本ID"))
        if gold not in VALID_GOLD_LABELS:
            skipped.append(
                {
                    "pair_id": pair_id,
                    "left_id": left_id,
                    "right_id": right_id,
                    "reason": "人工判断为空、不确定或不在固定评测标签内",
                }
            )
            continue
        if left_id not in states or right_id not in states:
            skipped.append(
                {
                    "pair_id": pair_id,
                    "left_id": left_id,
                    "right_id": right_id,
                    "reason": "聚类结果缺少会话样本ID",
                }
            )
            continue

        predicted = _prediction(states[left_id], states[right_id], allow_multi_topic=True)
        is_correct = predicted == gold
        correct += int(is_correct)
        total += 1
        by_label[gold][0] += int(is_correct)
        by_label[gold][1] += 1
        if not is_correct:
            wrong.append(
                {
                    "pair_id": pair_id,
                    "left_id": left_id,
                    "right_id": right_id,
                    "gold": gold,
                    "prediction": predicted,
                }
            )

    return {
        "overall": {
            "correct": correct,
            "total": total,
            "accuracy": round(correct / total, 4) if total else None,
        },
        "by_label": {
            label: {
                "correct": values[0],
                "total": values[1],
                "accuracy": round(values[0] / values[1], 4) if values[1] else None,
            }
            for label, values in by_label.items()
        },
        "wrong_count": len(wrong),
        "wrong": wrong,
        "skipped_count": len(skipped),
        "skipped": skipped,
        "local_multi_topic_rescued_samples": sorted(
            sample_id
            for sample_id, state in states.items()
            if state.get("local_multi_topic_rescue")
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample-json", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    parser.add_argument("--gold-xlsx", type=Path, required=True)
    parser.add_argument("--sheet-name", default="配对盲标")
    parser.add_argument("--output-json", type=Path)
    args = parser.parse_args()

    result = evaluate_fixed_pairs(
        _read_json(args.result_json),
        _read_json(args.sample_json),
        _load_gold_rows(args.gold_xlsx, args.sheet_name),
    )
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(rendered + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
