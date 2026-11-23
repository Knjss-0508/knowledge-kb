import json
import sys

import pytest
from pydantic import ValidationError

from app.services.blind_label_learning import LearningReviewError


def _case(key, baseline=None, candidate=None):
    return {"work_order_id": key, "review_id": 1, "baseline": baseline, "candidate": candidate}


def _answer(verdict=None):
    return {"generation": "answered", "answer": "脱敏答复", "verdict": verdict}


def _manifest(cases):
    from app.services.blind_label_answer_replay import AnswerReplayManifest

    return AnswerReplayManifest.model_validate({
        "baseline_version": "baseline-1", "candidate_version": "candidate-2", "cases": cases,
    })


def test_answer_replay_counts_only_paired_verdicts_in_accuracy_delta(monkeypatch):
    from app.services import blind_label_answer_replay as replay

    cases = [
        _case("improved", _answer("incorrect"), _answer("correct")),
        _case("regressed", _answer("correct"), _answer("incorrect")),
        _case("unchanged", _answer("correct"), _answer("correct")),
        _case("candidate-only", {"generation": "failed"}, _answer("correct")),
        _case("missing-and-pending", None, _answer()),
        _case("insufficient", _answer("insufficient_evidence"), _answer("correct")),
        _case("changed", _answer("correct"), _answer("correct")),
        _case("missing-reference", _answer("incorrect"), _answer("correct")),
        _case("ranking", _answer("incorrect"), _answer("correct")),
        _case("retired", _answer("incorrect"), _answer("correct")),
    ]

    def load(db, key):
        if key == "retired":
            raise LearningReviewError(409, "原工单不可用")
        return {"replay_eligible": True, "review": {
            "id": 2 if key == "changed" else 1,
            "issue_type": "ranking" if key == "ranking" else "answer_issue",
            "knowledge_id": "target",
            "expected_answer": " " if key == "missing-reference" else "脱敏预期要点",
        }}

    monkeypatch.setattr(replay, "load_learning_review", load)
    report = replay.run_answer_replay(object(), _manifest(cases))
    assert [(row["status"], row.get("baseline"), row.get("candidate")) for row in report["cases"]] == [
        ("eligible", "incorrect", "correct"), ("eligible", "correct", "incorrect"),
        ("eligible", "correct", "correct"), ("eligible", "generation_failed", "correct"),
        ("eligible", "missing_output", "unreviewed"), ("eligible", "insufficient_evidence", "correct"),
        ("review_changed_or_ineligible", None, None), ("missing_reference", None, None),
        ("not_answer_issue", None, None), ("review_changed_or_ineligible", None, None),
    ]
    summary = report["summary"]
    assert (summary["requested"], summary["eligible"], summary["excluded"]) == (10, 6, 4)
    assert summary["baseline"]["evaluated"] == 3
    assert summary["baseline"]["accuracy"] == pytest.approx(2 / 3)
    assert summary["candidate"]["evaluated"] == 5
    assert summary["candidate"]["accuracy"] == pytest.approx(4 / 5)
    assert summary["paired"] == {
        "evaluated": 3, "not_evaluated": 3, "improved": 1, "regressed": 1, "unchanged": 1,
        "baseline_accuracy": pytest.approx(2 / 3),
        "candidate_accuracy": pytest.approx(2 / 3), "accuracy_delta": 0,
    }
    assert "脱敏答复" not in json.dumps(report, ensure_ascii=False)
    assert "脱敏预期要点" not in json.dumps(report, ensure_ascii=False)
    assert "原工单不可用" not in json.dumps(report, ensure_ascii=False)


def test_no_paired_labels_does_not_imply_zero_accuracy(monkeypatch):
    from app.services import blind_label_answer_replay as replay

    monkeypatch.setattr(replay, "load_learning_review", lambda *_: {
        "replay_eligible": True,
        "review": {"id": 1, "issue_type": "answer_issue", "knowledge_id": "target", "expected_answer": "要点"},
    })
    report = replay.run_answer_replay(
        object(), _manifest([_case("wo", _answer(), _answer("incorrect"))]),
    )
    assert report["summary"]["baseline"]["accuracy"] is None
    assert report["summary"]["candidate"]["accuracy"] == 0
    assert report["summary"]["paired"]["accuracy_delta"] is None

    refs = replay.build_review_references(object(), _manifest([_case("wo", _answer())]))
    assert refs["cases"] == [{
        "work_order_id": "wo", "review_id": 1, "status": "eligible",
        "knowledge_id": "target", "expected_answer": "要点",
    }]


@pytest.mark.parametrize("cases", [
    [_case("wo"), _case("wo")],
    [_case("wo", {"generation": "failed", "verdict": "incorrect"})],
    [_case("wo", {"generation": "answered", "answer": " "})],
    [_case("wo", {**_answer(), "transcript": "原对话"})],
])
def test_manifest_rejects_ambiguous_judgments_and_extra_payloads(cases):
    with pytest.raises(ValidationError):
        _manifest(cases)


def test_cli_fails_closed_without_confirmation_or_local_database(monkeypatch, capsys):
    from scripts import replay_blind_label_answers as cli

    monkeypatch.setenv("BLIND_REPLAY_DATABASE_URL", "postgresql://user:private-key@example.org/replay")
    monkeypatch.setattr(sys, "argv", ["replay", "unread.json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    assert "--confirm-local-sanitized" in capsys.readouterr().err

    monkeypatch.setattr(sys, "argv", ["replay", "unread.json", "--confirm-local-sanitized"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "BLIND_REPLAY_DATABASE_URL" in captured.err
    assert "private-key" not in captured.err
    assert not captured.out
