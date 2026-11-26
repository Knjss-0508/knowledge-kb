"""Cross-check queued hints against retained retrieval events; never run a replay."""

import argparse
import json
import os
import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.exc import ArgumentError, SQLAlchemyError

from scripts._blind_replay_io import controlled_artifact_path, local_database, readonly_engine


_REPORT_FIELDS = (
    "work_order_id", "knowledge_id", "finding", "original_event_rank", "events_checked",
    "has_structured_scope_fields", "has_recorded_reply",
)
_NEXT_ACTIONS = {
    "stale_hint": "refresh_evidence",
    "already_in_frozen_candidates": "review_current_candidate_evidence",
    "present_in_original_event": "review_ranking_and_candidate_window",
    "present_in_same_request_event": "review_same_request_candidate_window",
    "original_snapshot_unusable": "recover_original_event_evidence",
    "not_seen_in_checked_events": "verify_applicability_and_prepare_sanitized_request",
}


AUDIT = text("""
WITH hints AS (
    SELECT * FROM jsonb_to_recordset(CAST(:hints AS jsonb)) AS h(
        work_order_id text, knowledge_id text, knowledge_updated_at timestamp
    )
)
SELECT h.work_order_id,h.knowledge_id,
       CASE
         WHEN w.id IS NULL OR w.pool_state <> 'active' OR k.id IS NULL
              OR k.updated_at IS DISTINCT FROM h.knowledge_updated_at THEN 'stale_hint'
         WHEN w.candidate_snapshot::jsonb @> jsonb_build_array(jsonb_build_object('knowledge_id',h.knowledge_id))
              THEN 'already_in_frozen_candidates'
         WHEN e.candidate_snapshot::jsonb @> jsonb_build_array(jsonb_build_object('knowledge_id',h.knowledge_id))
              THEN 'present_in_original_event'
         WHEN related.has_hit THEN 'present_in_same_request_event'
         WHEN coalesce(jsonb_typeof(e.candidate_snapshot::jsonb),'null') <> 'array'
              OR NOT jsonb_path_exists(e.candidate_snapshot::jsonb, '$[*].knowledge_id ? (@.type() == "string" && @ != "")')
              THEN 'original_snapshot_unusable'
         ELSE 'not_seen_in_checked_events'
       END AS finding,
       original.recorded_rank AS original_event_rank,
       related.events_checked,
       coalesce(related.has_structured_scope, false) AS has_structured_scope_fields,
       coalesce(related.has_recorded_reply, false) AS has_recorded_reply
FROM hints h
LEFT JOIN blind_label_work_orders w ON w.id=h.work_order_id
LEFT JOIN knowledge_items k ON k.id=h.knowledge_id AND k.status='PUBLISHED'
LEFT JOIN retrieval_quality_events e ON e.id=w.source_event_id
LEFT JOIN LATERAL (
    -- Report the recorded rank only; array offsets don't imply retrieval ranks.
    SELECT min(CASE WHEN candidate->>'rank' ~ '^[1-9][0-9]{0,5}$'
                    THEN (candidate->>'rank')::int END) AS recorded_rank
    FROM jsonb_array_elements(CASE WHEN jsonb_typeof(e.candidate_snapshot::jsonb)='array'
         THEN e.candidate_snapshot::jsonb ELSE '[]'::jsonb END) AS candidate
    WHERE candidate->>'knowledge_id'=h.knowledge_id
) original ON true
LEFT JOIN LATERAL (
    -- Only the exact original event or events with all three matching identities.
    -- Never join on question text, form alone, or conversation alone.
    SELECT count(*) AS events_checked,
           bool_or(r.candidate_snapshot::jsonb @> jsonb_build_array(
               jsonb_build_object('knowledge_id',h.knowledge_id))) AS has_hit,
           bool_or(r.event_metadata::jsonb ?| array[
               'businessType','business_type','categoryId','category_id','productType','product_type',
               'brand','brandId','brand_id','model','modelId','model_id','orderInfo','order_info'
           ]) AS has_structured_scope,
           bool_or(nullif(btrim(r.event_metadata->>'recommended_reply'),'') IS NOT NULL) AS has_recorded_reply
    FROM retrieval_quality_events r
    WHERE r.id=e.id OR (
        nullif(btrim(e.question_form_id),'') IS NOT NULL AND r.question_form_id=e.question_form_id
        AND nullif(btrim(e.request_id),'') IS NOT NULL AND r.request_id=e.request_id
        AND nullif(btrim(e.conversation_id),'') IS NOT NULL AND r.conversation_id=e.conversation_id
    )
) related ON true
ORDER BY h.work_order_id,h.knowledge_id
""")


def read_active_hints(inbox: Path, limit: int) -> tuple[list[dict], bool]:
    if not 1 <= limit <= 50:
        raise ValueError("invalid limit")
    with closing(sqlite3.connect(inbox.as_uri() + "?mode=ro", uri=True)) as db:
        tasks = db.execute("""
            SELECT work_order_id,hints_json FROM offline_recall_review_queue
            WHERE status='awaiting_evidence' ORDER BY work_order_id LIMIT ?
        """, (limit + 1,)).fetchall()
    hints = []
    for order, payload in tasks[:limit]:
        batch = json.loads(payload)
        if not isinstance(batch, list) or not 1 <= len(batch) <= 3:
            raise ValueError("invalid hint batch")
        for hint in batch:
            if not isinstance(hint, dict):
                raise ValueError("invalid hint")
            knowledge, version = hint.get("knowledge_id"), hint.get("knowledge_updated_at")
            if not all(isinstance(value, str) and 1 <= len(value) <= 64 for value in (order, knowledge, version)):
                raise ValueError("invalid hint reference")
            hints.append({"work_order_id": order, "knowledge_id": knowledge, "knowledge_updated_at": version})
    return hints, len(tasks) > limit


def describe(rows, has_more: bool) -> dict:
    rows = [{field: row[field] for field in _REPORT_FIELDS} for row in rows]
    for row in rows:
        row["next_action"] = _NEXT_ACTIONS[row["finding"]]
        row["replay_blockers"] = ["needs_independent_applicability_review", "needs_reviewed_sanitized_request"]
    recorded_ranks = [
        row["original_event_rank"] for row in rows if row["finding"] == "present_in_original_event"
    ]
    summary = {
        "work_orders": len({row["work_order_id"] for row in rows}),
        "hints": len(rows), "has_more": has_more,
        "findings": dict(Counter(row["finding"] for row in rows)),
        "original_rank_bands": {
            "top3": sum(rank is not None and rank <= 3 for rank in recorded_ranks),
            "after_top3": sum(rank is not None and rank > 3 for rank in recorded_ranks),
            "not_recorded": recorded_ranks.count(None),
        },
        "with_structured_scope_fields": sum(row["has_structured_scope_fields"] for row in rows),
        "with_recorded_reply": sum(row["has_recorded_reply"] for row in rows),
    }
    return {
        "schema_version": 2, "scope": "retained_event_candidate_presence_not_review",
        "summary": summary, "hints": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-isolated-replica", action="store_true")
    parser.add_argument("--review-inbox", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 51), default=20, metavar="1..50")
    args = parser.parse_args()
    if not args.confirm_isolated_replica:
        parser.error("必须确认断网隔离副本，不允许生产隧道。")
    try:
        inbox = controlled_artifact_path(args.review_inbox, ".sqlite3")
        output = controlled_artifact_path(args.out, ".json", new=True)
        hints, has_more = read_active_hints(inbox, args.limit)
        engine = readonly_engine(local_database())
        try:
            with engine.begin() as connection:
                connection.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
                rows = connection.execute(AUDIT, {"hints": json.dumps(hints)}).mappings().all() if hints else []
        finally:
            engine.dispose()
        report = describe(rows, has_more)
        with os.fdopen(os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=True, indent=2)
    except (ArgumentError, OSError, sqlite3.Error, SQLAlchemyError, ValueError):
        parser.error("无法核对隔离证据或写入产物；未输出原始数据。")
    print(json.dumps(report["summary"], ensure_ascii=True))


if __name__ == "__main__":
    main()
