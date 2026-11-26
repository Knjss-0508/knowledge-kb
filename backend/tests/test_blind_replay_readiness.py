import json
from contextlib import nullcontext
from unittest.mock import Mock

import pytest

from app.services.blind_label_learning import LearningReviewError
from app.services.blind_label_replay import ReplayManifest
from scripts import check_blind_replay_readiness as readiness
from scripts import run_blind_learning_cycle as cycle


def _packet(*refs):
    return readiness.ReviewHandoff(
        schema_version=1, scope="replay_review_handoff_not_manifest", runnable=False,
        cases=[{"work_order_id": order, "knowledge_id": target, "lane": lane} for order, target, lane in refs],
    )


def _manifest(*refs):
    return ReplayManifest.model_validate({"cases": [
        {"work_order_id": order, "review_id": version,
         "request": {"normalizedQuestion": "do not export", "businessType": "self_operated"}}
        for order, version in refs
    ]})


def _review(target, issue="ranking", version=7):
    return {"replay_eligible": True, "review": {"id": version, "knowledge_id": target, "issue_type": issue}}


def test_readiness_preflights_only_materialized_matching_reviews(monkeypatch):
    ranking = "ranking_candidate_review"
    recall = "recall_candidate_review"
    packet = _packet(("ready", "alt", ranking), ("ready", "target", ranking),
                     ("pending", "target", ranking), ("wrong-target", "target", ranking),
                     ("wrong-type", "target", recall), ("wrong-version", "target", ranking),
                     ("no-request", "target", ranking), ("retired", "target", recall))
    reviews = {
        "ready": _review("target"), "pending": {"replay_eligible": False, "review": None},
        "wrong-target": _review("another"), "wrong-type": _review("target"),
        "wrong-version": _review("target"), "no-request": _review("target"),
    }

    def load_review(db, order):
        if order == "retired":
            raise LearningReviewError(409, "private evidence")
        return reviews[order]

    monkeypatch.setattr(readiness, "load_learning_review", load_review)
    report = readiness.check_readiness(None, packet, _manifest(
        ("ready", 7), ("pending", 7), ("wrong-version", 6), ("outside", 7),
    ))
    assert report["handoff_orders"] == 7 and report["ready"] == 1
    assert not report["manifest_ready"]
    assert report["manifest_orders_outside_handoff"] == 1
    assert report["statuses"] == {
        "ready": 1, "no_current_eligible_review": 1, "review_not_matched_to_handoff": 2,
        "manifest_review_version_mismatch": 1, "sanitized_request_missing": 1, "order_unavailable": 1,
    }
    assert "do not export" not in json.dumps(report) and "private evidence" not in json.dumps(report)


def test_reviewed_subset_can_pass_but_no_manifest_never_passes(monkeypatch):
    packet = _packet(("ready", "target", "recall_candidate_review"),
                     ("pending", "target", "ranking_candidate_review"))
    monkeypatch.setattr(readiness, "load_learning_review", lambda _, order: (
        _review("target", "retrieval_missed") if order == "ready" else {"replay_eligible": False, "review": None}
    ))
    assert readiness.check_readiness(None, packet, _manifest(("ready", 7)))["manifest_ready"]
    report = readiness.check_readiness(None, packet, None)
    assert report["ready"] == 0 and not report["manifest_ready"]
    assert report["statuses"]["sanitized_request_missing"] == 1


def test_cycle_stops_before_models_or_output_on_gate_failure(tmp_path, monkeypatch, capsys):
    handoff = tmp_path / "handoff.json"
    handoff.write_text(_packet(("w", "target", "ranking_candidate_review")).model_dump_json(), encoding="utf-8")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(_manifest(("w", 6)).model_dump_json(by_alias=True), encoding="utf-8")
    output = tmp_path / "cycle.json"
    engine = Mock()
    engine.begin.return_value = nullcontext(Mock())
    monkeypatch.setattr(cycle, "readonly_engine", lambda _: engine)
    monkeypatch.setattr(cycle, "Session", lambda **_: nullcontext(None))
    monkeypatch.setattr(cycle, "local_database", lambda: None)
    monkeypatch.setattr(cycle, "_local_endpoints", lambda: (None, "http://127.0.0.1:8082"))
    monkeypatch.setattr(cycle.settings, "EMBEDDING_BASE_URL", "")
    monkeypatch.setattr(cycle.settings, "EMBEDDING_API_KEY", "")
    monkeypatch.setenv("NO_PROXY", "")
    monkeypatch.setenv("no_proxy", "")
    monkeypatch.setattr(readiness, "load_learning_review", lambda *_: _review("target"))
    execute = Mock(side_effect=AssertionError("cycle must not start"))
    monkeypatch.setattr(cycle, "run_learning_cycle", execute)
    monkeypatch.setattr("sys.argv", [
        "cycle", "--review-handoff", str(handoff), "--retrieval-manifest", str(manifest),
        "--ai-limit", "0", "--confirm-local-sanitized", "--out", str(output),
    ])
    with pytest.raises(SystemExit) as exc:
        cycle.main()
    assert exc.value.code == 1
    assert json.loads(capsys.readouterr().out)["statuses"] == {"manifest_review_version_mismatch": 1}
    execute.assert_not_called()
    assert not output.exists()
