import unittest
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.models.integration import IntegrationIngestion
from app.models.knowledge import Category, Knowledge
from app.routes.integration import (
    _candidate_review_item,
    check_knowledge_deduplication,
    submit_candidate_reviews,
    submit_knowledge_candidates,
)
from app.schemas.integration import (
    CandidateReviewBatchSubmit,
    IntegrationCandidateBatch,
    IntegrationDedupCheckRequest,
)
from app.services.knowledge_dedup import DedupDecision, DedupMatch


def _candidate_payload(
    *,
    knowledge_origin: str = "business_accumulation",
    business_type: str = "aggregated",
    applicable_categories: list[str] | None = None,
) -> dict:
    return {
        "event_id": "event-business-1",
        "idempotency_key": "business-1",
        "source": {
            "system": "upstream-test",
            "conversation_id": "conversation-1",
        },
        "processing": {
            "summary_version": "summary-v1",
            "label_model": "rules-v1",
            "plugin_name": "knowledge-transform",
            "plugin_version": "1.0.0",
        },
        "selection": {
            "eligible": True,
            "confidence": 0.95,
        },
        "knowledge": {
            "title": "聚合回收屏幕质检",
            "content": "检查屏幕显示是否正常。",
            "knowledge_origin": knowledge_origin,
            "business_type": business_type,
            "category_id": "cat-qc-standard",
            "applicable_categories": applicable_categories or ["手机"],
        },
    }


def _create_decision(*, with_match: bool = False) -> DedupDecision:
    matches = []
    if with_match:
        matches.append(
            DedupMatch(
                knowledge_id="A-00001",
                title="聚合回收屏幕质检",
                status="published",
                knowledge_origin="business_accumulation",
                business_type="aggregated",
                category_id="cat-qc-standard",
                match_type="semantic",
                similarity=0.9,
            )
        )
    return DedupDecision(
        action="review_duplicate" if with_match else "create",
        content_hash="hash-1",
        embedding=None,
        title_embedding=None,
        content_embedding=None,
        matches=matches,
    )


class IntegrationBusinessTypeTests(unittest.TestCase):
    @patch("app.routes.integration.check_duplicate")
    def test_dedup_check_scopes_by_business_type_and_returns_match_type(
        self,
        check_duplicate,
    ):
        check_duplicate.return_value = _create_decision(with_match=True)
        request = IntegrationDedupCheckRequest.model_validate(
            {"knowledge": _candidate_payload()["knowledge"]}
        )
        db = MagicMock()

        response = check_knowledge_deduplication(request, db, None)

        self.assertEqual(
            check_duplicate.call_args.kwargs["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["business_type"],
            "aggregated",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["applicable_categories"],
            ["手机"],
        )
        self.assertEqual(
            response.matches[0].knowledge_origin,
            "business_accumulation",
        )
        self.assertEqual(response.matches[0].business_type, "aggregated")
        db.commit.assert_called_once()

    @patch("app.routes.integration.ensure_search_embeddings")
    @patch("app.routes.integration._generate_knowledge_id", return_value="A-00002")
    @patch("app.routes.integration.check_duplicate")
    def test_direct_candidate_submission_preserves_business_type(
        self,
        check_duplicate,
        _generate_knowledge_id,
        ensure_search_embeddings,
    ):
        check_duplicate.return_value = _create_decision()
        body = IntegrationCandidateBatch.model_validate(
            {"items": [_candidate_payload()]}
        )
        db = MagicMock()

        def query_model(model):
            query = MagicMock()
            if model is IntegrationIngestion:
                query.filter.return_value.first.return_value = None
            elif model is Category:
                query.filter.return_value.first.return_value = SimpleNamespace(
                    id="cat-qc-standard"
                )
            return query

        db.query.side_effect = query_model

        response = submit_knowledge_candidates(body, db, None)

        self.assertEqual(response.accepted, 1)
        self.assertEqual(
            check_duplicate.call_args.kwargs["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["business_type"],
            "aggregated",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["applicable_categories"],
            ["手机"],
        )
        created = [
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], Knowledge)
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].knowledge_origin, "business_accumulation")
        self.assertEqual(created[0].business_type, "aggregated")
        self.assertEqual(created[0].applicable_categories, ["手机"])
        ensure_search_embeddings.assert_called_once_with(db, created[0])

    def test_candidate_review_item_returns_origin_and_business_type(self):
        now = datetime.now(UTC)
        payload = _candidate_payload()
        item = SimpleNamespace(
            id="ing-business-1",
            event_id=payload["event_id"],
            source_system=payload["source"]["system"],
            source_conversation_id=payload["source"]["conversation_id"],
            source_conversation_url=None,
            review_status="pending",
            status="candidate_pending",
            candidate_payload=payload,
            selection_metadata=payload["selection"],
            review_metadata={},
            knowledge_id=None,
            error_code=None,
            error_message=None,
            reviewed_by=None,
            reviewed_at=None,
            submitted_at=None,
            created_at=now,
            updated_at=now,
        )

        response = _candidate_review_item(item)

        self.assertEqual(response.knowledge_origin, "business_accumulation")
        self.assertEqual(response.business_type, "aggregated")

    @patch("app.routes.integration.ensure_search_embeddings")
    @patch("app.routes.integration._generate_knowledge_id", return_value="A-00003")
    @patch("app.routes.integration.check_duplicate")
    def test_review_submission_rechecks_dedup_in_same_business(
        self,
        check_duplicate,
        _generate_knowledge_id,
        ensure_search_embeddings,
    ):
        check_duplicate.return_value = _create_decision()
        payload = _candidate_payload()
        item = SimpleNamespace(
            id="ing-business-2",
            candidate_payload=payload,
            review_metadata={},
            review_status="ready",
            status="candidate_ready",
            knowledge_id=None,
            submitted_at=None,
            error_code=None,
            error_message=None,
        )
        db = MagicMock()

        def query_model(model):
            query = MagicMock()
            if model is IntegrationIngestion:
                query.filter.return_value.first.return_value = item
            elif model is Category:
                query.filter.return_value.first.return_value = SimpleNamespace(
                    id="cat-qc-standard"
                )
            return query

        db.query.side_effect = query_model

        response = submit_candidate_reviews(
            CandidateReviewBatchSubmit(ingestion_ids=[item.id]),
            db,
            SimpleNamespace(username="reviewer"),
        )

        self.assertEqual(response.submitted, 1)
        self.assertEqual(
            check_duplicate.call_args.kwargs["knowledge_origin"],
            "business_accumulation",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["business_type"],
            "aggregated",
        )
        self.assertEqual(
            check_duplicate.call_args.kwargs["applicable_categories"],
            ["手机"],
        )
        created = [
            call.args[0]
            for call in db.add.call_args_list
            if isinstance(call.args[0], Knowledge)
        ]
        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].knowledge_origin, "business_accumulation")
        self.assertEqual(created[0].business_type, "aggregated")
        self.assertEqual(created[0].applicable_categories, ["手机"])
        self.assertEqual(item.review_status, "submitted")
        ensure_search_embeddings.assert_called_once_with(db, created[0])


if __name__ == "__main__":
    unittest.main()
