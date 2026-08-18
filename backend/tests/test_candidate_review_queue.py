from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from app.routes.integration import queue_knowledge_review_candidates
from app.schemas.integration import IntegrationCandidateBatch


def _candidate(idempotency_key: str) -> dict:
    return {
        "event_id": "event-candidate-1",
        "idempotency_key": idempotency_key,
        "source": {
            "system": "answer-hub-test",
            "conversation_id": "conversation-1",
        },
        "processing": {
            "summary_version": "summary-v1",
            "label_model": "rules-v1",
            "plugin_name": "answer-hub-topic-transcription",
            "plugin_version": "1.0.0",
        },
        "selection": {"eligible": False, "confidence": 0.95},
        "knowledge": {
            "title": "手机无法充电的排查方法",
            "content": "先检查充电线和充电口。",
            "knowledge_origin": "headquarters_standard",
            "business_type": "self_operated",
            "category_id": "cat-qc-standard",
        },
    }


def _batch(*items: dict) -> IntegrationCandidateBatch:
    return IntegrationCandidateBatch.model_validate({"items": list(items)})


def _query_db(*existing_items):
    db = MagicMock()
    query = MagicMock()
    query.filter.return_value.first.side_effect = existing_items
    db.query.return_value = query
    return db


def test_queue_reuses_duplicate_idempotency_key_within_one_batch() -> None:
    db = _query_db(None)
    body = _batch(_candidate("same-key"), _candidate("same-key"))

    response = queue_knowledge_review_candidates(body, db, None)

    assert response.queued == 1
    assert response.reused == 1
    assert [item.status for item in response.results] == ["queued", "reused"]
    assert db.add.call_count == 1
    db.flush.assert_called_once()
    db.commit.assert_called_once()


def test_queue_reuses_candidate_when_concurrent_insert_wins() -> None:
    existing = SimpleNamespace(
        id="ing-existing",
        knowledge_id=None,
        reviewed_at=None,
        review_status="pending",
        status="candidate_pending",
    )
    db = _query_db(None, existing)
    db.flush.side_effect = IntegrityError("duplicate idempotency key", None, Exception())
    body = _batch(_candidate("race-key"))

    response = queue_knowledge_review_candidates(body, db, None)

    assert response.queued == 0
    assert response.reused == 1
    assert response.results[0].status == "reused"
    assert response.results[0].ingestion_id == "ing-existing"
    assert db.add.call_count == 1
    db.flush.assert_called_once()
    db.commit.assert_called_once()
