import json
from datetime import datetime

import pytest

from scripts._blind_knowledge_inbox import open_inbox
from scripts._blind_replay_io import controlled_artifact_path
from scripts._blind_review_inbox import sync_recall_review_inbox
from scripts.prepare_blind_replay_intake import attach_cross_order_hints, intake


def _candidate(knowledge_id="in-pool", *, yes=0, no=4, state="snapshot_matches"):
    return {"knowledge_id": knowledge_id, "referable_count": yes, "not_referable_count": no,
            "knowledge_state": state, "private_content": "do not export"}


def _order(order_id, category="a", **overrides):
    return {
        "work_order_id": order_id, "evidence_version": "v1", "category_id": category,
        "review_status": "pending_review", "has_source_query": True,
        "completed_annotators": 5, "required_annotators": 5, "consensus_status": "majority",
        "task_reason_counts": {}, "candidates": [_candidate()], "private_question": "do not export",
        **overrides,
    }


def _vote(knowledge_id, source="neighbor", **overrides):
    return {"category_id": "a", "question_group": "q", "knowledge_id": knowledge_id,
            "work_order_id": source, "yes": 4, "assignment_limit": 5, "snapshot_matches": True,
            "knowledge_updated_at": datetime(2026, 11, 3), "private_content": "do not export", **overrides}


def test_intake_only_routes_complete_unarbitrated_and_explicitly_decided_candidates():
    rows = [
        _order("recall", task_reason_counts={"knowledge_exists_not_recalled": 3}),
        _order("answer", candidates=[_candidate(yes=4, no=1)]),
        _order("stale", review_status="stale"),
        _order("arbitration", consensus_status="arbitrated"),
        _order("incomplete", completed_annotators=4),
        _order("confirmed", review_status="confirmed"),
        _order("no-query", has_source_query=False),
        _order("changed", candidates=[_candidate(yes=4, no=1, state="snapshot_differs")]),
        _order("unselected", candidates=[_candidate(no=0)]),
        _order("undecided-candidate", candidates=[_candidate(), _candidate("another", no=2)]),
        _order("two-positives", candidates=[_candidate(yes=4, no=1), _candidate("another", yes=3, no=2)]),
    ]
    result = intake(rows, 20)
    assert result["eligible_by_lane"] == {"recall_check": 2, "answer_check": 1}
    assert result["excluded"] == {
        "historical_arbitration": 1, "not_fully_voted": 1, "confirmed": 1, "no_source_query": 1,
        "answer_reference_changed_or_missing": 1, "ambiguous_candidate_evidence": 3,
    }
    recall, stale = result["lanes"]["recall_check"]
    assert recall["signal"] == "explicit_recall_reason" and recall["recall_reason_votes"] == 3
    assert stale["review_status"] == "stale"
    answer = result["lanes"]["answer_check"][0]
    assert answer["reference_candidate_votes"] == 4 and answer["status"] == "awaiting_evidence"
    assert "independent_outcome_judgment" in answer["required_evidence"]
    assert "do not export" not in json.dumps(result)


def test_intake_prioritizes_explicit_reasons_balances_categories_and_limits_rows():
    result = intake([
        _order("a0"), _order("a1"), _order("a2", task_reason_counts={"knowledge_exists_not_recalled": 4}),
        _order("b0", category="b"), _order("b1", category="b"),
    ], 3)
    assert result["eligible_by_lane"]["recall_check"] == 5
    assert [r["work_order_id"] for r in result["lanes"]["recall_check"]] == ["a2", "b0", "a0"]


def test_hints_only_other_orders_same_question_category_current_published_snapshot():
    recall = intake([_order("target"), _order("unknown", category=None)], 20)["lanes"]["recall_check"]
    keys = [{"id": "target", "category_id": "a", "question_group": "q"},
            {"id": "unknown", "category_id": None, "question_group": "q"}]
    votes = [
        _vote("in-pool"), _vote("same-order", "target"),
        _vote("changed", snapshot_matches=False), _vote("missing-version", knowledge_updated_at=None),
        _vote("different-category", category_id="b"), _vote("different-query", question_group="another"),
        _vote("not-majority", yes=2), _vote("impossible-vote-count", yes=6),
        _vote("a-pick", "z-source", yes=5), _vote("a-pick", "a-source", yes=5),
        _vote("a-pick", "weaker-source"),
        _vote("c-pick"), _vote("b-pick"), _vote("overflow"),
    ]
    assert attach_cross_order_hints(recall, keys, votes) == 1
    target, unknown = recall
    hints = target["outside_candidate_hints"]
    assert [r["knowledge_id"] for r in hints] == ["a-pick", "b-pick", "c-pick"]
    assert hints[0]["source_work_order_id"] == "a-source"
    assert unknown["outside_candidate_hints"] == []
    assert "do not export" not in json.dumps(recall)
    assert "question_group" not in json.dumps(recall)


def test_recall_queue_deduplicates_limits_and_refreshes_only_observed_evidence(tmp_path):
    target, other = intake([_order("target"), _order("z-other")], 20)["lanes"]["recall_check"]
    keys = [{"id": row["work_order_id"], "category_id": "a", "question_group": "q"} for row in (target, other)]
    assert attach_cross_order_hints([target, other], keys, [_vote("outside")]) == 2
    target["outside_candidate_hints"][0]["private_content"] = "do not export"
    observed = {"target", "z-other"}
    path = tmp_path / "review.sqlite3"
    with open_inbox(path) as db:
        assert sync_recall_review_inbox(db, observed, [target, other], [target]) == {
            "inserted": 1, "superseded": 0, "reactivated": 0,
        }
    with open_inbox(path) as db:
        assert sync_recall_review_inbox(db, observed, [target, other], [target])["inserted"] == 0
        assert sync_recall_review_inbox(db, observed, [target, other], [])["superseded"] == 0
        assert sync_recall_review_inbox(db, {"z-other"}, [other], [])["superseded"] == 0
        payload, = db.execute("SELECT hints_json FROM offline_recall_review_queue").fetchone()
        assert "do not export" not in payload
        assert sync_recall_review_inbox(db, observed, [other], [])["superseded"] == 1
        assert sync_recall_review_inbox(db, observed, [target, other], [target])["reactivated"] == 1
        newer = {**target, "outside_candidate_hints": [
            {**target["outside_candidate_hints"][0], "knowledge_updated_at": "2026-11-04T00:00:00"}
        ]}
        assert sync_recall_review_inbox(db, observed, [newer, other], [newer]) == {
            "inserted": 1, "superseded": 1, "reactivated": 0,
        }
        assert db.execute("""
            SELECT status, count(*) FROM offline_recall_review_queue GROUP BY status ORDER BY status
        """).fetchall() == [("awaiting_evidence", 1), ("superseded", 1)]


def test_intake_artifacts_must_be_outside_git_and_report_cannot_be_overwritten(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").touch()
    with pytest.raises(ValueError):
        controlled_artifact_path(repo / "queue.sqlite3", ".sqlite3")
    report = tmp_path / "report.json"
    report.touch()
    with pytest.raises(ValueError):
        controlled_artifact_path(report, ".json", new=True)
    assert controlled_artifact_path(tmp_path / "queue.sqlite3", ".sqlite3").parent == tmp_path.resolve()
