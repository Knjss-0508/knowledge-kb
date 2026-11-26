import json

import pytest

from scripts._blind_knowledge_inbox import open_inbox
from scripts.audit_blind_recall_hints import describe, read_active_hints


def test_read_active_recall_audit_only_projects_bounded_hint_refs(tmp_path):
    path = tmp_path / "recall queue#example.sqlite3"
    hint = {"knowledge_id": "k1", "knowledge_updated_at": "2026-11-25T09:00:00", "private_note": "do not export"}
    with open_inbox(path) as db:
        db.execute("CREATE TABLE offline_recall_review_queue (work_order_id TEXT,hints_json TEXT,status TEXT)")
        db.executemany("INSERT INTO offline_recall_review_queue VALUES (?,?,?)", (
            ("w3", json.dumps([hint]), "awaiting_evidence"),
            ("w1", "not-json", "superseded"),
            ("w2", json.dumps([hint]), "awaiting_evidence"),
        ))
    assert read_active_hints(path, 1) == ([
        {"work_order_id": "w2", "knowledge_id": "k1", "knowledge_updated_at": "2026-11-25T09:00:00"},
    ], True)
    hints, has_more = read_active_hints(path, 2)
    assert not has_more
    assert [hint["work_order_id"] for hint in hints] == ["w2", "w3"]
    with open_inbox(path) as db:
        db.execute("UPDATE offline_recall_review_queue SET hints_json='[]' WHERE work_order_id='w2'")
    with pytest.raises(ValueError):
        read_active_hints(path, 1)


def test_recall_audit_keeps_candidate_presence_separate_from_truth_and_exports_no_raw_fields():
    first = {
        "work_order_id": "w1", "knowledge_id": "k1", "finding": "present_in_original_event",
        "original_event_rank": 4,
        "events_checked": 2, "has_structured_scope_fields": False, "has_recorded_reply": False,
        "private_question": "do not export",
    }
    second = {
        **first, "knowledge_id": "k2", "finding": "not_seen_in_checked_events",
        "original_event_rank": None, "events_checked": 2, "has_structured_scope_fields": True,
    }
    report = describe([first, second], False)
    assert report["scope"] == "retained_event_candidate_presence_not_review"
    assert report["summary"] == {
        "work_orders": 1, "hints": 2, "has_more": False,
        "findings": {"present_in_original_event": 1, "not_seen_in_checked_events": 1},
        "original_rank_bands": {"top3": 0, "after_top3": 1, "not_recorded": 0},
        "with_structured_scope_fields": 1, "with_recorded_reply": 0,
    }
    assert "do not export" not in json.dumps(report)
    assert [row["next_action"] for row in report["hints"]] == [
        "review_ranking_and_candidate_window", "verify_applicability_and_prepare_sanitized_request",
    ]
    assert all("needs_reviewed_sanitized_request" in row["replay_blockers"] for row in report["hints"])
