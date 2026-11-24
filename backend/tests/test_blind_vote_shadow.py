from datetime import datetime

from scripts._blind_knowledge_inbox import open_inbox
from scripts.run_blind_vote_shadow import _samples, _split, _write_editor_queue, train


def _row(**patch):
    return {
        "snapshot_matches": True, "knowledge_id": "article", "knowledge_updated_at": datetime(2026, 11, 1),
        "work_order_id": "w1", "candidate_ref": "r1", "question_group": "question-hash",
        "rank": "1", "score": "0.81", "assignment_limit": 5, "yes": 4, "no": 1,
        "needs_conditions": 3, "needs_merge": 0, "content_incorrect": 1, "content_outdated": 0,
        **patch,
    }


def test_majority_only_and_no_duplicate_question_training_examples(tmp_path):
    groups, tasks = _samples([
        _row(),
        _row(candidate_ref="r2", yes=0, no=1, needs_conditions=0),
        _row(work_order_id="w2", candidate_ref="r3", needs_conditions=0),
        _row(question_group="different", work_order_id="w3", snapshot_matches=False),
    ])
    assert groups == [("question-hash", [(1, 0.81, 1, "article")])]
    assert [task["reason"] for task in tasks] == ["needs_conditions"]
    path = tmp_path / "editor.sqlite3"
    with open_inbox(path) as queue:
        assert _write_editor_queue(queue, tasks) == 1
    with open_inbox(path) as queue:
        assert _write_editor_queue(queue, tasks) == 0
        assert queue.execute("select status,count(*) from offline_vote_editor_queue group by status").fetchall() == [
            ("needs_editor", 1),
        ]


def test_independent_knowledge_slice_can_reject_otherwise_good_candidate():
    rows = []
    inversions = 0
    for i in range(600):
        key = f"q-{i}"
        negative_first = _split(key) == "test" and inversions < 20
        if negative_first:
            inversions += 1
        knowledge = f"unseen-{i}" if negative_first else "common"
        rows.append((key, [(1, 0.87, int(not negative_first), knowledge), (1, 0.62, int(negative_first), knowledge)]))
    report, candidate = train(rows)
    assert report["test_candidate_brier"] < report["test_rank_only_brier"]
    assert report["unseen_candidate_brier"] > report["unseen_rank_only_brier"]
    assert report["status"] == "quality_gate_failed" and candidate is None


def test_good_candidate_is_offline_only():
    groups = [(f"q-{i}", [(1, 0.87, 1, f"a{i}"), (1, 0.62, 0, f"b{i}")]) for i in range(300)]
    report, candidate = train(groups)
    assert report["status"] == "offline_candidate"
    assert candidate["production_approved"] is False
