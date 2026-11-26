"""从隔离副本准备漏召回与最终答复的复核清单，不输出正文或训练标签。"""

import argparse
import json
import os
from collections import Counter, defaultdict
from contextlib import nullcontext
from pathlib import Path
from sqlite3 import Error as SQLiteError

from sqlalchemy import bindparam, text
from sqlalchemy.exc import ArgumentError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.services.blind_label_learning import preview_learning_samples
from scripts._blind_knowledge_inbox import open_inbox
from scripts._blind_replay_io import controlled_artifact_path, local_database, readonly_engine
from scripts._blind_review_inbox import sync_recall_review_inbox
from scripts.run_blind_vote_shadow import EVIDENCE


_QUESTION_KEYS = text("""
    SELECT id, category_id,
           md5(lower(regexp_replace(trim(query_text), '[[:space:]]+', ' ', 'g'))) AS question_group
    FROM blind_label_work_orders WHERE id IN :ids
""").bindparams(bindparam("ids", expanding=True))


def attach_cross_order_hints(recall: list[dict], keys, vote_rows) -> int:
    """Same-question, same-category hints only; exact applicability is still unverified."""
    index = defaultdict(dict)
    for vote in vote_rows:
        total = vote["assignment_limit"]
        if (not vote["category_id"] or not vote["snapshot_matches"] or not vote["knowledge_updated_at"]
                or total not in (3, 5) or not total // 2 < vote["yes"] <= total):
            continue
        key = (vote["category_id"], vote["question_group"])
        candidate = vote["knowledge_id"]
        # Keep the strongest source for each article.
        previous = index[key].get(candidate)
        if previous is None or _source_rank(vote) < _source_rank(previous):
            index[key][candidate] = vote
    by_id = {row["id"]: row for row in keys}
    for row in recall:
        group = by_id.get(row["work_order_id"])
        hints = []
        if group is not None and group["category_id"]:
            key = group["category_id"], group["question_group"]
            for knowledge, source in sorted(
                index.get(key, {}).items(), key=lambda x: (-x[1]["yes"], x[0])
            ):
                if knowledge not in row["knowledge_ids_to_exclude"] and source["work_order_id"] != row["work_order_id"]:
                    hints.append({"knowledge_id": knowledge, "source_work_order_id": source["work_order_id"],
                                  "referable_votes": source["yes"], "total_votes": source["assignment_limit"],
                                  "knowledge_updated_at": source["knowledge_updated_at"].isoformat()})
        row["outside_candidate_hints"] = hints[:3]
    return sum(bool(row["outside_candidate_hints"]) for row in recall)


def _source_rank(vote):
    return -vote["yes"], vote["assignment_limit"], vote["work_order_id"]


def intake(items: list[dict], per_lane: int) -> dict:
    lanes = {"recall_check": [], "answer_check": []}
    eligible = Counter()
    missing = Counter()
    for row in items:
        state = row["review_status"]
        if state not in {"pending_review", "stale"} or not row["has_source_query"]:
            missing[state if state not in {"pending_review", "stale"} else "no_source_query"] += 1
            continue
        completed = row["completed_annotators"]
        if completed != row["required_annotators"] or completed not in (3, 5):
            missing["not_fully_voted"] += 1
            continue
        if row["consensus_status"] == "arbitrated":
            missing["historical_arbitration"] += 1
            continue
        candidates = row["candidates"]
        if not 1 <= len(candidates) <= 3:
            missing["unexpected_candidate_count"] += 1
            continue
        agreed = row["task_reason_counts"].get("knowledge_exists_not_recalled", 0)
        positives = [c for c in candidates if c["referable_count"] * 2 > completed]
        common = {
            "work_order_id": row["work_order_id"], "evidence_version": row["evidence_version"],
            "category_id": row["category_id"], "status": "awaiting_evidence",
            "completed_annotators": completed, "review_status": state,
        }
        if not positives and all(c["not_referable_count"] * 2 > completed for c in candidates):
            eligible["recall_check"] += 1
            lanes["recall_check"].append({
                **common, "signal": "explicit_recall_reason" if agreed * 2 > completed else "all_frozen_candidates_rejected",
                "recall_reason_votes": agreed,
                "required_evidence": ["published_knowledge_absent_from_frozen_candidates", "reviewed_sanitized_search_input"],
                "knowledge_ids_to_exclude": sorted({c["knowledge_id"] for c in candidates if c["knowledge_id"]}),
            })
        elif len(positives) == 1:
            candidate = positives[0]
            if not candidate["knowledge_id"] or candidate["knowledge_state"] != "snapshot_matches":
                missing["answer_reference_changed_or_missing"] += 1
                continue
            eligible["answer_check"] += 1
            lanes["answer_check"].append({
                **common, "signal": "one_referable_article_not_an_answer_verdict",
                "reference_candidate_knowledge_id": candidate["knowledge_id"],
                "reference_candidate_votes": candidate["referable_count"],
                "required_evidence": ["reviewed_sanitized_input", "reviewed_expected_answer", "both_actual_recommendations",
                                      "independent_outcome_judgment"],
            })
        else:
            missing["ambiguous_candidate_evidence"] += 1
    # Do not use any outcome to choose answer controls. Balance categories so a
    # large one doesn't consume the whole intake batch.
    for lane, rows in lanes.items():
        rows.sort(key=lambda r: (-r.get("recall_reason_votes", 0), r["work_order_id"]))
        by_category = Counter()
        decorated = []
        for row in rows:
            key = row["category_id"]
            weight = by_category[key]
            by_category[key] += 1
            decorated.append((-row.get("recall_reason_votes", 0), weight, row["work_order_id"], row))
        lanes[lane] = [row for _, _, _, row in sorted(decorated)[:per_lane]]
    return {"eligible_by_lane": dict(eligible), "excluded": dict(missing), "lanes": lanes}


def _scan(engine, args):
    cursor = None
    scanned = 0
    # Retain only bounded structured preview results; never export source rows.
    items = []
    with engine.begin() as connection:
        connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        with Session(bind=connection, autoflush=False) as db:
            for _ in range(args.pages):
                page = preview_learning_samples(db, scan_limit=100, cursor=cursor)
                scanned += page["scanned"]
                items.extend(page["items"])
                cursor = page["next_cursor"]
                if not cursor:
                    break
            result = intake(items, 5000 if args.cross_order_hints else args.per_lane)
            recall = result["lanes"]["recall_check"]
            hint_count = 0
            if args.cross_order_hints and recall:
                keys = list(connection.execute(
                    _QUESTION_KEYS, {"ids": [row["work_order_id"] for row in recall]}
                ).mappings())
                hint_count = attach_cross_order_hints(recall, keys, connection.execute(EVIDENCE).mappings())
                recall.sort(key=lambda row: (not row["outside_candidate_hints"], -row["recall_reason_votes"]))
    for lane, rows in result["lanes"].items():
        result["lanes"][lane] = rows[:args.per_lane]
    return result, scanned, bool(cursor), items, recall, hint_count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-replica", action="store_true")
    parser.add_argument("--pages", type=int, choices=range(1, 51), default=10, metavar="1..50")
    parser.add_argument("--per-lane", type=int, choices=range(1, 51), default=20, metavar="1..50")
    parser.add_argument("--cross-order-hints", action="store_true", help="在同品类同问题的其他工单多数票中找池外参考线索")
    parser.add_argument("--review-inbox", type=Path, help="仓库外该副本专用的待核验 SQLite 队列；仅池外知识线索入队")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    if not args.confirm_isolated_replica:
        parser.error("必须确认断网隔离副本，不允许生产隧道。")
    if args.review_inbox and not args.cross_order_hints:
        parser.error("登记待核验任务需要 --cross-order-hints。")
    try:
        output = controlled_artifact_path(args.out, ".json", new=True)
        inbox = controlled_artifact_path(args.review_inbox, ".sqlite3") if args.review_inbox else None
    except (OSError, ValueError):
        parser.error("产物须保存到仓库外受控目录，JSON 不能覆盖。")
    try:
        engine = readonly_engine(local_database())
        try:
            # Acquire the queue lock before the DB snapshot to serialize refreshes.
            with (open_inbox(inbox) if inbox else nullcontext()) as queue:
                result, scanned, has_more, items, recall, hint_count = _scan(engine, args)
                queue_counts = (sync_recall_review_inbox(
                    queue, {row["work_order_id"] for row in items}, recall, result["lanes"]["recall_check"]
                ) if queue is not None else None)
        finally:
            engine.dispose()
    except (ArgumentError, SQLAlchemyError, ValueError):
        parser.error("无法读取隔离副本。未输出原始数据。")
    except (OSError, SQLiteError):
        parser.error("无法更新受控复核队列。")
    count = {
        "scanned": scanned, "previewed": len(items), "has_more": has_more,
        "eligible_by_lane": result["eligible_by_lane"],
        "selected_by_lane": {lane: len(rows) for lane, rows in result["lanes"].items()},
        "explicit_recall_signals_selected": sum(r["signal"] == "explicit_recall_reason" for r in result["lanes"]["recall_check"]),
        "cross_order_hints_available": hint_count,
        "cross_order_hints_selected": sum(bool(r.get("outside_candidate_hints")) for r in result["lanes"]["recall_check"]),
    }
    if queue_counts is not None:
        count["review_inbox"] = queue_counts
    try:
        with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as handle:
            json.dump({"schema_version": 1, "scope": "review_intake_not_truth", "summary": count, **result},
                      handle, ensure_ascii=True, indent=2)
    except OSError:
        parser.error("无法写入受控产物。")
    print(json.dumps(count, ensure_ascii=True))


if __name__ == "__main__":
    main()
