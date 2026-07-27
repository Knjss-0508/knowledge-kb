import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException

from app.routes.integration import submit_candidate_reviews, submit_knowledge_candidates
from app.models.knowledge import KnowledgeStatus
from app.routes.knowledge import (
    _check_manual_deduplication,
    _deduplication_metadata,
    _queue_excel_deduplication_review,
    batch_approve_knowledge,
    list_review_selection,
)
from app.schemas.integration import (
    CandidateReviewBatchSubmit,
    IntegrationCandidateBatch,
)
from app.schemas.knowledge import KnowledgeBatchApprove, KnowledgeCreate
from app.services.knowledge_dedup import DedupDecision, DedupMatch


def _review_decision() -> DedupDecision:
    return DedupDecision(
        action="review_duplicate",
        content_hash="dedup-hash",
        embedding=None,
        title_embedding=None,
        content_embedding=None,
        matches=[
            DedupMatch(
                knowledge_id="A-00001",
                title="按键颜色不符是什么意思",
                status="published",
                category_id="cat-qc-standard",
                match_type="title_exact",
                similarity=1.0,
                title_similarity=1.0,
            )
        ],
    )


def _candidate_batch() -> IntegrationCandidateBatch:
    return IntegrationCandidateBatch.model_validate(
        {
            "items": [
                {
                    "event_id": "event-1",
                    "idempotency_key": "dedup-workflow-test-1",
                    "source": {
                        "system": "answer-hub",
                        "conversation_id": "conversation-1",
                    },
                    "processing": {
                        "summary_version": "v1",
                        "label_model": "qwen",
                        "plugin_name": "answer-hub",
                        "plugin_version": "v1",
                    },
                    "selection": {
                        "eligible": True,
                        "confidence": 0.9,
                    },
                    "knowledge": {
                        "title": "按键颜色不符是什么意思",
                        "content": {"blocks": [{"type": "text", "value": "1"}]},
                        "category_id": "cat-qc-standard",
                    },
                }
            ]
        }
    )


class DeduplicationWorkflowTests(unittest.TestCase):
    def test_manual_review_requires_explicit_confirmation(self):
        decision = _review_decision()
        with patch(
            "app.routes.knowledge.check_duplicate",
            return_value=decision,
        ):
            with self.assertRaises(HTTPException) as raised:
                _check_manual_deduplication(
                    MagicMock(),
                    title="按键颜色不符是什么意思",
                    subtitles=[],
                    content={"blocks": [{"type": "text", "value": "1"}]},
                    scene_tags=[],
                )
            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(
                raised.exception.detail["code"],
                "DUPLICATE_REVIEW_REQUIRED",
            )

            confirmed = _check_manual_deduplication(
                MagicMock(),
                title="按键颜色不符是什么意思",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "1"}]},
                scene_tags=[],
                confirm_dedup_review=True,
            )
        self.assertIs(confirmed, decision)

    def test_manual_confirmation_is_recorded_for_review_traceability(self):
        metadata = _deduplication_metadata(
            _review_decision(),
            confirmed_by="reviewer",
        )

        self.assertEqual(metadata["review_confirmation"]["confirmed_by"], "reviewer")
        self.assertIn("confirmed_at", metadata["review_confirmation"])

    def test_automation_duplicate_is_queued_without_creating_knowledge(self):
        ingestion_query = MagicMock()
        ingestion_query.filter.return_value.first.return_value = None
        category_query = MagicMock()
        category_query.filter.return_value.first.return_value = SimpleNamespace(
            id="cat-qc-standard"
        )
        db = MagicMock()
        db.query.side_effect = [ingestion_query, category_query]

        with patch(
            "app.routes.integration.check_duplicate",
            return_value=_review_decision(),
        ):
            response = submit_knowledge_candidates(_candidate_batch(), db, None)

        self.assertEqual(response.accepted, 0)
        self.assertEqual(response.review_required, 1)
        self.assertEqual(response.results[0].status, "review_required")
        self.assertEqual(response.results[0].error_code, "DUPLICATE_REVIEW_REQUIRED")
        queued = db.add.call_args.args[0]
        self.assertIsNone(queued.knowledge_id)
        self.assertEqual(queued.error_code, "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(
            queued.review_metadata["deduplication"]["matches"][0]["match_type"],
            "title_exact",
        )

    def test_candidate_review_submission_requires_matching_confirmation(self):
        candidate = _candidate_batch().items[0]
        item = SimpleNamespace(
            id="ing-1",
            knowledge_id=None,
            review_status="ready",
            status="candidate_ready",
            candidate_payload=candidate.model_dump(mode="json"),
            review_metadata={},
            error_code=None,
            error_message=None,
        )
        ingestion_query = MagicMock()
        ingestion_query.filter.return_value.first.return_value = item
        category_query = MagicMock()
        category_query.filter.return_value.first.return_value = SimpleNamespace(
            id="cat-qc-standard"
        )
        db = MagicMock()
        db.query.side_effect = [ingestion_query, category_query]
        current_user = SimpleNamespace(username="reviewer")

        with patch(
            "app.routes.integration.check_duplicate",
            return_value=_review_decision(),
        ):
            response = submit_candidate_reviews(
                CandidateReviewBatchSubmit(ingestion_ids=["ing-1"]),
                db,
                current_user,
            )

        self.assertEqual(response.failed, 1)
        self.assertEqual(response.results[0].error_code, "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(item.review_status, "ready")
        self.assertEqual(
            item.review_metadata["deduplication"]["content_hash"],
            "dedup-hash",
        )

    def test_excel_duplicate_is_queued_for_deduplication_confirmation(self):
        query = MagicMock()
        query.filter.return_value.first.return_value = None
        db = MagicMock()
        db.query.return_value = query
        user = SimpleNamespace(username="excel-user")
        body = KnowledgeCreate(
            title="按键颜色不符是什么意思",
            content={"blocks": [{"type": "text", "value": "不同处理步骤"}]},
            category_id="cat-qc-standard",
        )
        metadata = _deduplication_metadata(_review_decision())

        task = _queue_excel_deduplication_review(
            db,
            body=body,
            filename="知识导入.xlsx",
            row_number=2,
            current_user=user,
            deduplication=metadata,
        )

        self.assertEqual(task.source_system, "excel")
        self.assertEqual(task.review_status, "ready")
        self.assertTrue(task.review_metadata["deduplication_only"])
        self.assertEqual(task.error_code, "DUPLICATE_REVIEW_REQUIRED")
        self.assertEqual(
            task.candidate_payload["knowledge"]["title"],
            body.title,
        )

    def test_batch_approve_publishes_only_review_items(self):
        item = SimpleNamespace(
            id="A-00020",
            status=KnowledgeStatus.REVIEW,
            updated_by=None,
            updated_at=None,
        )
        query = MagicMock()
        query.filter.return_value.first.return_value = item
        db = MagicMock()
        db.query.return_value = query
        current_user = SimpleNamespace(username="approver")

        with patch("app.routes.knowledge.ensure_embedding"), patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ):
            response = batch_approve_knowledge(
                KnowledgeBatchApprove(knowledge_ids=[item.id]),
                db,
                current_user,
            )

        self.assertEqual(response.approved, 1)
        self.assertEqual(response.results[0].status, "approved")
        self.assertEqual(item.status, KnowledgeStatus.PUBLISHED)
        self.assertEqual(item.updated_by, "approver")

    def test_review_selection_returns_all_pending_ids(self):
        query = MagicMock()
        query.filter.return_value.order_by.return_value.all.return_value = [
            ("A-00020",),
            ("A-00021",),
        ]
        db = MagicMock()
        db.query.return_value = query

        response = list_review_selection(db, None)

        self.assertEqual(response.total, 2)
        self.assertEqual(response.knowledge_ids, ["A-00020", "A-00021"])


if __name__ == "__main__":
    unittest.main()
