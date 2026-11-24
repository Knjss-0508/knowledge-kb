import json
from types import SimpleNamespace

import httpx
import pytest

from app.services import blind_label_cycle as cycle
from app.services.blind_label_cycle import Diagnosis, EvaluatedCase, RankedCandidate
from app.services.blind_label_replay import ReplayManifest
from app.services.embedding import EmbeddingServiceUnavailable
from scripts import run_blind_learning_cycle as cli


def _preview(order="w1", state="pending_review", issue=None):
    return {
        "work_order_id": order, "evidence_version": "a" * 64, "review_status": state,
        "suggested_track": "ranking_review", "completed_annotators": 5, "required_annotators": 5,
        "referable_count": 4, "not_referable_count": 1,
        "task_reason_counts": {"knowledge_missing": 1, "PRIVATE_FREE_TEXT": 5},
        "query_text": "PRIVATE_QUESTION", "review": {"id": 1, "issue_type": issue} if issue else None,
        "candidates": [{
            "rank": 1, "referable_count": 4, "not_referable_count": 1, "reason_counts": {"needs_conditions": 2},
            "knowledge_id": "PRIVATE_ARTICLE", "content": "PRIVATE_CONTENT",
        }],
    }


def _patch_preview(monkeypatch, items):
    monkeypatch.setattr(cycle, "preview_learning_samples", lambda *a, **kw: {
        "scanned": len(items) + 2, "next_cursor": "next", "items": items,
    })


def test_ai_is_advisory_and_only_gets_enum_and_counts(monkeypatch):
    items = [
        _preview(), _preview("gap", "confirmed", "knowledge_gap"),
        _preview("old", "stale", "knowledge_issue"), _preview("w4"),
    ]
    _patch_preview(monkeypatch, items)
    sent = []

    def diagnose(evidence):
        sent.append(evidence)
        return Diagnosis(issue="knowledge_gap")

    result = cycle.run_learning_cycle(None, diagnose=diagnose, ai_limit=1)
    assert len(sent) == 1
    assert "PRIVATE" not in json.dumps(sent)
    assert "w1" not in json.dumps(sent)
    assert result["discovery"]["next_cursor"] == "next"
    assert result["discovery"]["ai_suggested"] == 1
    assert result["discovery"]["cases"][0]["ai_issue"] == "knowledge_gap"
    assert result["training"]["status"] == "no_reviewed_manifest"
    assert result["candidate"] is None and result["published"] is False
    assert [draft["work_order_id"] for draft in result["knowledge_drafts"]] == ["gap"]
    draft = result["knowledge_drafts"][0]
    assert draft["knowledge_id"] is None and draft["proposed_content"] is None
    assert draft["draft_id"] == cycle.run_learning_cycle(None)["knowledge_drafts"][0]["draft_id"]


def _manifest(n):
    return ReplayManifest.model_validate({"cases": [{
        "work_order_id": f"w{i}", "review_id": i + 1,
        "request": {"normalizedQuestion": f"参考型号{i:04}", "businessType": "self_operated"},
    } for i in range(n)]})


def _review(order, *, issue="ranking"):
    i = int(order[1:])
    return {
        "replay_eligible": True,
        "review": {"id": i + 1, "knowledge_id": f"k{i}", "issue_type": issue},
        "sample": {"candidates": [
            {"knowledge_id": "n1", "verdict": "not_referable", "knowledge_state": "snapshot_matches"},
            # Unselected and changed snapshot are not training negatives.
            {"knowledge_id": "n2", "verdict": None, "knowledge_state": "snapshot_matches"},
            {"knowledge_id": "n3", "verdict": "not_referable", "knowledge_state": "snapshot_differs"},
        ]},
    }


def _ranked(*args, **kwargs):
    request = args[1]
    i = int(request.normalized_question[-4:])
    data = {
        "internal": [
            (SimpleNamespace(id="n1", title="测试设备"), 0.82),
            (SimpleNamespace(id="n2", title="其他商家"), 0.81),
            (SimpleNamespace(id="n3", title="请勿使用"), 0.80),
            (SimpleNamespace(id=f"k{i}", title=request.normalized_question), 0.79),
        ],
        "other_origin": [(SimpleNamespace(id="noise", title="来自不同来源"), 0.99)],
    }
    return data, data, 0.78


def test_real_fit_logic_only_uses_reviewed_pairs_and_checks_holdout(monkeypatch):
    _patch_preview(monkeypatch, [])
    monkeypatch.setattr(cycle, "load_learning_review", lambda db, order: _review(order))
    monkeypatch.setattr(cycle.integration, "get_active_runtime_values", lambda _: {})
    monkeypatch.setattr(cycle.integration, "rank_standard_semantic_request", _ranked)
    required = _manifest(30)
    retrieval, cases = cycle._collect_retrieval(None, required)
    assert all(c.explicit_negatives == {"n1"} for c in cases)
    assert all(c.baseline_position == 4 for c in cases)
    assert retrieval["summary"]["top3_hits"] == 0
    result = cycle.run_learning_cycle(None, retrieval_manifest=required)
    assert result["training"]["status"] == "offline_candidate"
    assert result["training"]["candidate_holdout_top3"] == 10
    assert result["training"]["baseline_holdout_top3"] == 0
    assert result["training"]["holdout_regressed"] == 0
    assert result["candidate"]["title_bigram_overlap_weight"] > 0
    assert result["candidate"]["production_approved"] is False
    assert "参考型号" not in json.dumps(result, ensure_ascii=False)


def test_model_unavailable_and_review_changed_never_fits(monkeypatch):
    _patch_preview(monkeypatch, [])
    requested = _manifest(3)
    requested.cases[0].review_id = 999
    monkeypatch.setattr(cycle, "load_learning_review",
                        lambda db, order: _review(order, issue="answer_issue" if order == "w1" else "ranking"))
    monkeypatch.setattr(cycle.integration, "get_active_runtime_values", lambda _: {})

    def unavailable(*args, **kwargs):
        raise EmbeddingServiceUnavailable("don't expose this")

    monkeypatch.setattr(cycle.integration, "rank_standard_semantic_request", unavailable)
    result = cycle.run_learning_cycle(None, retrieval_manifest=requested)
    assert result["validation"]["retrieval"]["summary"]["outcomes"] == {
        "review_changed_or_ineligible": 1, "not_retrieval_training_issue": 1, "embedding_unavailable": 1,
    }
    assert result["training"]["status"] == "validation_incomplete" and result["candidate"] is None


def test_train_holdout_groups_no_duplicate_queries_or_targets_and_no_holdout_regression():
    cases = [EvaluatedCase(f"w{i}", f"q{i}", f"k{i}", (), frozenset()) for i in range(40)]
    cases.extend([
        EvaluatedCase("duplicate-query", "q1", "else", (), frozenset()),
        EvaluatedCase("duplicate-target", "new", "else", (), frozenset()),
    ])
    train, holdout = cycle._partition(cases)
    assert {c.query_key for c in train}.isdisjoint(c.query_key for c in holdout)
    assert {c.target for c in train}.isdisjoint(c.target for c in holdout)

    examples = []
    for i in range(30):
        target = f"k{i}"
        examples.append(EvaluatedCase(f"w{i}", f"q{i}", target, (
            RankedCandidate("n", 0.83, 0.0), RankedCandidate(target, 0.82, 1.0),
        ), frozenset({"n"})))
    retrieval = {"configured": True, "summary": {"requested": len(examples)}}
    result, candidate = cycle._train(retrieval, examples)
    assert result["status"] == "holdout_gate_failed"  # already at top-3 saturation
    assert candidate is None


def test_local_ollama_ignores_free_text_and_rejects_remote_endpoints(monkeypatch):
    monkeypatch.setenv("BLIND_CYCLE_OLLAMA_MODEL", "qwen2.5:3b-instruct")
    for url in ("http://192.0.2.5:18114", "http://127.0.0.1:18114/proxy",
                "http://test@127.0.0.1:18114", "http://127.0.0.1:18114?forward=1"):
        monkeypatch.setenv("BLIND_CYCLE_OLLAMA_URL", url)
        with pytest.raises(ValueError):
            cli._local_ollama()
    monkeypatch.setenv("BLIND_CYCLE_OLLAMA_URL", "http://127.0.0.1:18114")
    endpoint = cli._local_ollama()

    def answer(request):
        assert request.url.path == "/api/chat"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"message": {"content": '{"issue":"ranking","extra":"unsafe"}'}})

    with httpx.Client(transport=httpx.MockTransport(answer)) as client:
        assert cli._diagnose(client, endpoint, {"completed": 5}) is None
