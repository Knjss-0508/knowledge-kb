import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from sqlalchemy.exc import IntegrityError

from app.routes.integration import queue_knowledge_review_candidates
from app.schemas.integration import IntegrationCandidateBatch


def _queue_candidate_payload() -> dict:
    return {
        "event_id": "event-queue-1",
        "idempotency_key": "queue-idempotency-1",
        "source": {
            "system": "answer-hub",
            "conversation_id": "conversation-queue-1",
        },
        "processing": {
            "summary_version": "summary-v1",
            "label_model": "rules-v1",
            "plugin_name": "answer-hub-topic-transcription",
            "plugin_version": "1.0.0",
        },
        "selection": {
            "eligible": True,
            "confidence": 0.95,
        },
        "knowledge": {
            "title": "Screen inspection steps",
            "content": "Check whether the screen display is normal.",
            "knowledge_origin": "business_accumulation",
            "business_type": "aggregated",
            "category_id": "cat-qc-standard",
        },
    }


class CandidateReviewQueueIdempotencyTests(unittest.TestCase):
    def test_reuses_same_idempotency_key_within_one_batch(self):
        payload = _queue_candidate_payload()
        body = IntegrationCandidateBatch.model_validate(
            {"items": [payload, payload]}
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.return_value = None

        def commit():
            if db.add.call_count > 1:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))

        db.commit.side_effect = commit

        response = queue_knowledge_review_candidates(body, db, None)

        self.assertEqual(response.queued, 1)
        self.assertEqual(response.reused, 1)
        self.assertEqual(
            [result.status for result in response.results],
            ["queued", "reused"],
        )
        self.assertEqual(db.add.call_count, 1)
        db.commit.assert_called_once()

    def test_recovers_when_another_request_inserts_the_same_key_first(self):
        payload = _queue_candidate_payload()
        body = IntegrationCandidateBatch.model_validate({"items": [payload]})
        existing = SimpleNamespace(
            id="ing-existing",
            knowledge_id=None,
            review_status="pending",
            reviewed_at=None,
            created_at=None,
            event_id=payload["event_id"],
            source_system=payload["source"]["system"],
            source_conversation_id=payload["source"]["conversation_id"],
            source_conversation_url=None,
            source_message_ids=[],
            redaction_status=None,
            processing_metadata={},
            selection_metadata={},
            candidate_payload={},
            review_metadata={},
            status="candidate_pending",
            error_code=None,
            error_message=None,
        )
        db = MagicMock()
        db.query.return_value.filter.return_value.first.side_effect = [None, existing]
        db.flush.side_effect = IntegrityError("INSERT", {}, Exception("duplicate key"))

        def commit():
            if not db.flush.called:
                raise IntegrityError("INSERT", {}, Exception("duplicate key"))

        db.commit.side_effect = commit

        response = queue_knowledge_review_candidates(body, db, None)

        self.assertEqual(response.queued, 0)
        self.assertEqual(response.reused, 1)
        self.assertEqual(response.results[0].status, "reused")
        self.assertEqual(response.results[0].ingestion_id, "ing-existing")
        db.begin_nested.assert_called_once()
        db.flush.assert_called_once()
        db.commit.assert_called_once()


if __name__ == "__main__":
    unittest.main()
