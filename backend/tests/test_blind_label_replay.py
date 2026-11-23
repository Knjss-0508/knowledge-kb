from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

from app.models.knowledge import KnowledgeStatus
from app.routes import integration
from app.services.embedding import EmbeddingServiceUnavailable


def _manifest():
    from app.services.blind_label_replay import ReplayManifest

    return ReplayManifest.model_validate({"cases": [
        {"work_order_id": key, "review_id": 1, "request": {
            "normalizedQuestion": "脱敏问题", "businessType": "self_operated", "categoryId": "phone",
        }}
        for key in ("hit", "below", "missing", "stale", "error")
    ]})


def test_replay_scores_per_pool_separates_failures_and_never_calls_telemetry(monkeypatch):
    from app.services import blind_label_replay as replay

    valid = {"review_status": "confirmed", "replay_eligible": True}
    cases = {
        key: {**valid, "review": {"id": 1, "issue_type": "ranking", "knowledge_id": "target"}}
        for key in ("hit", "below", "missing", "stale", "error")
    }
    cases["stale"]["review"]["id"] = 2
    monkeypatch.setattr(replay, "load_learning_review", lambda db, key: cases[key])
    monkeypatch.setattr(integration, "get_active_runtime_values", lambda db: {"retrieval_score_threshold": 0.60})
    monkeypatch.setattr(integration, "_read_manhattan_cache", lambda: {})
    record = Mock(side_effect=AssertionError("离线回放不得写日志"))
    monkeypatch.setattr(integration, "_record_standard_search_events", record)
    target = SimpleNamespace(id="target", status=KnowledgeStatus.PUBLISHED)
    a = SimpleNamespace(id="other", status=KnowledgeStatus.PUBLISHED)
    results = iter([
        [(a, 0.99)], [(a, 0.9), (target, 0.8)],
        [], [(target, 0.59)],
        [], [],
    ])
    counter = []

    def search(db, **kwargs):
        counter.append(kwargs)
        try:
            return next(results)
        except StopIteration:
            raise EmbeddingServiceUnavailable("private service message") from None

    monkeypatch.setattr(integration, "search_embeddings", search)
    report = replay.run_retrieval_replay(object(), _manifest())
    assert [(row["status"], row.get("returned_rank")) for row in report["cases"]] == [
        ("returned", 2), ("filtered_by_threshold", None), ("not_in_search_window", None),
        ("review_changed_or_ineligible", None), ("embedding_unavailable", None),
    ]
    assert report["summary"]["evaluated"] == 3
    assert report["summary"]["top3_hits"] == 1
    assert report["summary"]["not_evaluated"] == 2
    assert [call["knowledge_origin"] for call in counter[:2]] == [
        "headquarters_standard", "business_accumulation",
    ]
    assert "脱敏问题" not in str(report)
    assert "private service message" not in str(report)
    record.assert_not_called()


def test_replay_manifest_rejects_raw_identity_fields_and_duplicate_cases():
    from app.services.blind_label_replay import ReplayManifest

    case = {"work_order_id": "wo-1", "review_id": 1, "request": {
        "normalizedQuestion": "脱敏问题", "businessType": "aggregated", "categoryId": "phone",
    }}
    with pytest.raises(ValidationError):
        ReplayManifest.model_validate({"cases": [case, case]})
    with pytest.raises(ValidationError):
        ReplayManifest.model_validate({"cases": [{**case, "request": {**case["request"], "conversationId": "123"}}]})
    with pytest.raises(ValidationError):
        ReplayManifest.model_validate({"cases": [{**case, "request": {
            **case["request"], "orderInfo": {"phoneNumber": "private"},
        }}]})


def test_replay_cli_only_accepts_explicit_local_endpoints(monkeypatch):
    import importlib.util
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "scripts" / "replay_blind_label_retrieval.py"
    spec = importlib.util.spec_from_file_location("replay_cli_for_test", path)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    monkeypatch.setenv("BLIND_REPLAY_DATABASE_URL", "postgresql://127.0.0.1:5439/replay_fixture")
    monkeypatch.setenv("BLIND_REPLAY_EMBEDDING_URL", "http://127.0.0.1:18080/v1")
    database, embedding = cli._local_endpoints()
    assert database.database == "replay_fixture"
    assert embedding == "http://127.0.0.1:18080/v1"
    monkeypatch.setenv("BLIND_REPLAY_EMBEDDING_URL", "https://example.org/v1")
    with pytest.raises(ValueError):
        cli._local_endpoints()
    monkeypatch.setenv("BLIND_REPLAY_EMBEDDING_URL", "http://127.0.0.1:18080/v1")
    monkeypatch.setenv("BLIND_REPLAY_DATABASE_URL", "postgresql://127.0.0.1/replay_fixture?host=example.org")
    with pytest.raises(ValueError):
        cli._local_endpoints()
