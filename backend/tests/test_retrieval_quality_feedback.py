import unittest
from datetime import datetime
from unittest.mock import patch

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.integration import RetrievalQualityEvent
from app.routes.integration import (
    _retrieval_candidate_snapshot,
    _retrieval_feedback_dimensions,
    get_retrieval_analytics,
    submit_retrieval_quality_events,
)
from app.schemas.integration import (
    RetrievalQualityEventBatch,
    RetrievalQualityEventPayload,
)


class RetrievalQualityFeedbackTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        RetrievalQualityEvent.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine, autoflush=False)
        self.db = self.session_factory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _payload(key: str, **overrides) -> RetrievalQualityEventPayload:
        default_conversation_id = str(
            202608100000
            + sum(
                (index + 1) * ord(character)
                for index, character in enumerate(key)
            )
        )
        data = {
            "schema_version": 2,
            "idempotency_key": key,
            "source_system": "qa-recommendation-plugin",
            "conversation_id": default_conversation_id,
            "request_id": f"request-{key}",
            "query": f"问题 {key}",
            "request_status": "success",
            "candidate_count": 2,
            "top_knowledge_id": "A-00001",
            "top_rerank_score": 0.92,
            "score_threshold": 0.8,
            "selected": True,
            "selected_knowledge_id": "A-00001",
            "selected_candidate_rank": 1,
            "candidates": [
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "title": "第一条",
                    "embedding_score": 0.91,
                    "rerank_score": 0.92,
                    "final_score": 0.92,
                    "selected": True,
                },
                {
                    "knowledge_id": "A-00002",
                    "rank": 2,
                    "title": "第二条",
                    "embedding_score": 0.88,
                    "rerank_score": 0.89,
                    "final_score": 0.89,
                    "selected": False,
                },
            ],
            "embedding_model": "Qwen/Qwen3-Embedding-0.6B",
            "reranker_model": "Qwen/Qwen3-Reranker-0.6B",
            "prompt_version": "retrieval-v2",
            "total_latency_ms": 10,
        }
        data.update(overrides)
        metadata = {"source_kind": "reply"}
        metadata.update(data.get("metadata") or {})
        data["metadata"] = metadata
        return RetrievalQualityEventPayload.model_validate(data)

    def test_v2_dimensions_distinguish_top_alternative_threshold_and_failure(self):
        alternative = self._payload(
            "alternative",
            selected=False,
            selected_knowledge_id="A-00002",
            selected_candidate_rank=2,
            candidates=[
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "title": "第一条",
                    "final_score": 0.92,
                    "selected": False,
                },
                {
                    "knowledge_id": "A-00002",
                    "rank": 2,
                    "title": "第二条",
                    "final_score": 0.89,
                    "selected": True,
                },
            ],
        )
        dimensions = _retrieval_feedback_dimensions(alternative)
        snapshot = _retrieval_candidate_snapshot(alternative)

        self.assertEqual(dimensions["threshold_status"], "passed")
        self.assertEqual(dimensions["selection_status"], "alternative_selected")
        self.assertEqual(dimensions["outcome"], "accepted_alternative")
        self.assertFalse(snapshot[0]["selected"])
        self.assertTrue(snapshot[1]["selected"])

        low_score = self._payload(
            "low-score",
            top_rerank_score=0.5,
            selected=False,
            selected_knowledge_id=None,
            selected_candidate_rank=None,
            candidates=[
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "final_score": 0.5,
                    "selected": False,
                },
                {
                    "knowledge_id": "A-00002",
                    "rank": 2,
                    "final_score": 0.4,
                    "selected": False,
                },
            ],
        )
        self.assertEqual(
            _retrieval_feedback_dimensions(low_score),
            {
                "selected_knowledge_id": None,
                "selected_candidate_rank": None,
                "threshold_status": "below",
                "selection_status": "none_selected",
                "outcome": "low_score",
            },
        )

        technical = self._payload(
            "technical",
            request_status="timeout",
            candidate_count=0,
            top_knowledge_id=None,
            top_rerank_score=None,
            selected=False,
            selected_knowledge_id=None,
            selected_candidate_rank=None,
            candidates=[],
            failure_reason="technical_failure",
        )
        self.assertEqual(
            _retrieval_feedback_dimensions(technical)["outcome"],
            "technical_failure",
        )

    def test_selection_rank_is_calculated_within_each_knowledge_origin_pool(self):
        business_top = self._payload(
            "business-top",
            selected=False,
            selected_knowledge_id="A-00002",
            selected_candidate_rank=2,
            metadata={
                "candidate_origins": [
                    "headquarters_standard",
                    "business_accumulation",
                ]
            },
            candidates=[
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "title": "总部标准第一名",
                    "final_score": 0.92,
                    "selected": False,
                },
                {
                    "knowledge_id": "A-00002",
                    "rank": 2,
                    "title": "业务沉淀第一名",
                    "final_score": 0.89,
                    "selected": True,
                },
            ],
        )

        dimensions = _retrieval_feedback_dimensions(business_top)
        snapshot = _retrieval_candidate_snapshot(business_top)

        self.assertEqual(dimensions["selected_candidate_rank"], 2)
        self.assertEqual(dimensions["selection_status"], "top_selected")
        self.assertEqual(dimensions["outcome"], "accepted")
        self.assertEqual(
            [candidate["knowledge_origin"] for candidate in snapshot],
            ["headquarters_standard", "business_accumulation"],
        )

    def test_analytics_reclassifies_legacy_cross_origin_first_candidate(self):
        payload = self._payload(
            "legacy-business-top",
            selected=False,
            selected_knowledge_id="A-00002",
            selected_candidate_rank=2,
            metadata={
                "candidate_origins": [
                    "headquarters_standard",
                    "business_accumulation",
                ]
            },
            candidates=[
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "final_score": 0.92,
                    "selected": False,
                },
                {
                    "knowledge_id": "A-00002",
                    "rank": 2,
                    "final_score": 0.89,
                    "selected": True,
                },
            ],
        )
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=[payload]),
            self.db,
            None,
        )
        event = (
            self.db.query(RetrievalQualityEvent)
            .filter(
                RetrievalQualityEvent.idempotency_key
                == "legacy-business-top"
            )
            .one()
        )
        event.selection_status = "alternative_selected"
        event.outcome = "accepted_alternative"
        self.db.commit()

        analytics = get_retrieval_analytics(self.db, None)

        self.assertEqual(analytics["summary"]["accepted"], 1)
        self.assertEqual(analytics["summary"]["accepted_alternative"], 0)
        self.assertEqual(analytics["summary"]["top_selected"], 1)
        self.assertEqual(analytics["summary"]["alternative_selected"], 0)
        self.assertEqual(analytics["rates"]["top1_selection_rate"], 1.0)
        self.assertEqual(analytics["risks"][0]["selection_status"], "top_selected")
        self.assertEqual(analytics["risks"][0]["outcome"], "accepted")

    def test_batch_persists_v2_fields_and_analytics_rates(self):
        items = [
            self._payload("accepted", total_latency_ms=10),
            self._payload(
                "alternative",
                selected=False,
                selected_knowledge_id="A-00002",
                selected_candidate_rank=2,
                total_latency_ms=20,
                metadata={
                    "candidate_origins": [
                        "headquarters_standard",
                        "headquarters_standard",
                    ]
                },
                candidates=[
                    {
                        "knowledge_id": "A-00001",
                        "rank": 1,
                        "final_score": 0.92,
                        "selected": False,
                    },
                    {
                        "knowledge_id": "A-00002",
                        "rank": 2,
                        "final_score": 0.89,
                        "selected": True,
                    },
                ],
            ),
            self._payload(
                "low-score",
                top_rerank_score=0.4,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                total_latency_ms=30,
                candidates=[
                    {
                        "knowledge_id": "A-00001",
                        "rank": 1,
                        "final_score": 0.4,
                        "selected": False,
                    },
                    {
                        "knowledge_id": "A-00002",
                        "rank": 2,
                        "final_score": 0.3,
                        "selected": False,
                    },
                ],
            ),
            self._payload(
                "no-match",
                request_status="no_match",
                candidate_count=0,
                top_knowledge_id=None,
                top_rerank_score=None,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                candidates=[],
                total_latency_ms=40,
            ),
            self._payload(
                "technical",
                request_status="timeout",
                candidate_count=0,
                top_knowledge_id=None,
                top_rerank_score=None,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                candidates=[],
                failure_reason="technical_failure",
                total_latency_ms=50,
            ),
        ]

        response = submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=items),
            self.db,
            None,
        )
        self.assertEqual(response.recorded, 5)
        self.assertEqual(response.reused, 0)
        self.assertEqual(
            response.results[0].conversation_id,
            items[0].conversation_id,
        )
        self.assertEqual(response.results[0].request_id, "request-accepted")

        alternative = (
            self.db.query(RetrievalQualityEvent)
            .filter(RetrievalQualityEvent.idempotency_key == "alternative")
            .one()
        )
        self.assertEqual(alternative.schema_version, 2)
        self.assertEqual(alternative.selection_status, "alternative_selected")
        self.assertEqual(alternative.selected_candidate_rank, 2)
        self.assertEqual(alternative.conversation_id, items[1].conversation_id)
        self.assertEqual(alternative.request_id, "request-alternative")
        self.assertEqual(alternative.source_kind, "reply")
        self.assertEqual(alternative.candidate_snapshot[1]["knowledge_id"], "A-00002")
        self.assertTrue(alternative.candidate_snapshot[1]["selected"])
        self.assertEqual(alternative.embedding_model, "Qwen/Qwen3-Embedding-0.6B")

        accepted = (
            self.db.query(RetrievalQualityEvent)
            .filter(RetrievalQualityEvent.idempotency_key == "accepted")
            .one()
        )
        accepted.review_status = "confirmed"
        accepted.training_eligible = True
        self.db.commit()

        analytics = get_retrieval_analytics(self.db, None)
        self.assertEqual(analytics["summary"]["total"], 5)
        self.assertEqual(analytics["summary"]["accepted"], 1)
        self.assertEqual(analytics["summary"]["accepted_alternative"], 1)
        self.assertEqual(analytics["summary"]["low_score"], 1)
        self.assertEqual(analytics["summary"]["no_candidates"], 1)
        self.assertEqual(analytics["summary"]["technical_failure"], 1)
        self.assertEqual(analytics["summary"]["reviewed"], 1)
        self.assertEqual(analytics["summary"]["training_eligible"], 1)
        self.assertEqual(analytics["summary"]["candidate_requests"], 3)
        self.assertEqual(analytics["summary"]["candidate_queries"], 3)
        self.assertEqual(analytics["rates"]["candidate_coverage_rate"], 0.75)
        self.assertEqual(analytics["rates"]["threshold_pass_rate"], 0.6667)
        self.assertEqual(analytics["rates"]["top1_selection_rate"], 0.3333)
        self.assertEqual(analytics["rates"]["alternative_selection_rate"], 0.3333)
        self.assertEqual(analytics["rates"]["no_selection_rate"], 0.3333)
        self.assertEqual(analytics["latency"]["p50_ms"], 30.0)
        self.assertEqual(analytics["latency"]["p95_ms"], 50.0)
        alternative_risk = next(
            item
            for item in analytics["risks"]
            if item["id"] == alternative.id
        )
        self.assertEqual(
            [
                candidate["knowledge_origin"]
                for candidate in alternative_risk["candidates"]
            ],
            ["headquarters_standard", "headquarters_standard"],
        )

    @patch(
        "app.routes.integration.get_active_runtime_values",
        return_value={"retrieval_score_threshold": 0.75},
    )
    def test_batch_uses_server_retrieval_threshold_as_authoritative_value(
        self,
        _runtime_config,
    ):
        payload = self._payload(
            "server-threshold",
            candidate_count=1,
            top_rerank_score=0.70,
            score_threshold=0.10,
            selected=False,
            selected_knowledge_id=None,
            selected_candidate_rank=None,
            candidates=[
                {
                    "knowledge_id": "A-00001",
                    "rank": 1,
                    "final_score": 0.70,
                    "selected": False,
                }
            ],
        )

        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=[payload]),
            self.db,
            None,
        )
        event = (
            self.db.query(RetrievalQualityEvent)
            .filter(RetrievalQualityEvent.idempotency_key == "server-threshold")
            .one()
        )

        self.assertEqual(event.score_threshold, 0.75)
        self.assertEqual(event.threshold_status, "below")
        self.assertEqual(event.outcome, "low_score")

    def test_analytics_keeps_only_latest_event_per_work_order(self):
        items = [
            self._payload(
                "ticket-old",
                conversation_id="202608100001",
                top_rerank_score=0.4,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                candidates=[
                    {
                        "knowledge_id": "A-00001",
                        "rank": 1,
                        "final_score": 0.4,
                        "selected": False,
                    },
                    {
                        "knowledge_id": "A-00002",
                        "rank": 2,
                        "final_score": 0.3,
                        "selected": False,
                    },
                ],
            ),
            self._payload(
                "ticket-new",
                conversation_id="202608100001",
            ),
            self._payload(
                "other-ticket",
                conversation_id="202608100002",
                request_status="no_match",
                candidate_count=0,
                top_knowledge_id=None,
                top_rerank_score=None,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                candidates=[],
            ),
        ]
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=items),
            self.db,
            None,
        )
        events = {
            event.idempotency_key: event
            for event in self.db.query(RetrievalQualityEvent).all()
        }
        events["ticket-old"].created_at = datetime(2026, 8, 7, 10, 0, 0)
        events["ticket-new"].created_at = datetime(2026, 8, 7, 11, 0, 0)
        events["ticket-old"].review_status = "confirmed"
        events["ticket-old"].training_eligible = True
        self.db.commit()

        analytics = get_retrieval_analytics(self.db, None)

        self.assertEqual(analytics["summary"]["total"], 2)
        self.assertEqual(analytics["summary"]["accepted"], 1)
        self.assertEqual(analytics["summary"]["low_score"], 0)
        self.assertEqual(analytics["summary"]["no_candidates"], 1)
        self.assertEqual(analytics["summary"]["candidate_requests"], 1)
        self.assertEqual(analytics["summary"]["candidate_queries"], 1)
        self.assertEqual(analytics["summary"]["reviewed"], 0)
        self.assertEqual(analytics["summary"]["training_eligible"], 0)
        self.assertEqual(analytics["pagination"]["total"], 2)
        self.assertEqual(analytics["pagination"]["page"], 1)
        self.assertEqual(analytics["pagination"]["page_size"], 20)
        risk_ids = {risk["id"] for risk in analytics["risks"]}
        self.assertNotIn(events["ticket-old"].id, risk_ids)
        self.assertIn(events["ticket-new"].id, risk_ids)

    def test_analytics_keeps_latest_event_per_work_order_and_source_pool(self):
        items = [
            self._payload(
                "legacy-combined",
                conversation_id="202608100003",
                request_id="request-legacy",
                metadata={"source_kind": "reply"},
            ),
            self._payload(
                "reply-old",
                conversation_id="202608100003",
                request_id="request-old",
                metadata={"source_kind": "reply"},
            ),
            self._payload(
                "standard-current",
                conversation_id="202608100003",
                request_id="request-standard",
                metadata={"source_kind": "standard"},
            ),
            self._payload(
                "reply-current",
                conversation_id="202608100003",
                request_id="request-current",
                metadata={"source_kind": "reply"},
            ),
        ]
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=items),
            self.db,
            None,
        )
        events = {
            event.idempotency_key: event
            for event in self.db.query(RetrievalQualityEvent).all()
        }
        events["legacy-combined"].source_kind = "combined"
        events["legacy-combined"].created_at = datetime(2026, 8, 7, 9, 0, 0)
        events["reply-old"].created_at = datetime(2026, 8, 7, 10, 0, 0)
        events["standard-current"].created_at = datetime(
            2026,
            8,
            7,
            11,
            0,
            0,
        )
        events["reply-current"].created_at = datetime(
            2026,
            8,
            7,
            12,
            0,
            0,
        )
        self.db.commit()

        analytics = get_retrieval_analytics(self.db, None)

        self.assertEqual(analytics["summary"]["total"], 2)
        risk_ids = {risk["id"] for risk in analytics["risks"]}
        self.assertNotIn(events["legacy-combined"].id, risk_ids)
        self.assertNotIn(events["reply-old"].id, risk_ids)
        self.assertIn(events["standard-current"].id, risk_ids)
        self.assertIn(events["reply-current"].id, risk_ids)
        self.assertEqual(
            {
                risk["source_kind"]
                for risk in analytics["risks"]
                if risk["id"] in risk_ids
            },
            {"reply", "standard"},
        )

    def test_feedback_requires_plugin_supplied_identity(self):
        base = {
            "schema_version": 2,
            "idempotency_key": "missing-identity",
            "source_system": "qa-recommendation-plugin",
            "query": "屏幕漏光",
            "request_status": "no_match",
            "candidate_count": 0,
            "score_threshold": 0.8,
            "selected": False,
            "candidates": [],
        }
        for identity in (
            {},
            {
                "conversation_id": "work-order-1",
                "request_id": "request-1",
            },
            {
                "conversation_id": "２０２６０８１００００１",
                "request_id": "request-fullwidth",
            },
            {
                "conversation_id": "٢٠٢٦٠٨١٠٠٠٠١",
                "request_id": "request-arabic-indic",
            },
            {
                "conversation_id": "202608100001",
                "request_id": "包含空格",
            },
        ):
            with self.subTest(identity=identity):
                with self.assertRaises(ValidationError):
                    RetrievalQualityEventPayload.model_validate(
                        {**base, **identity}
                    )

    def test_feedback_requires_a_valid_candidate_source_pool(self):
        payload = self._payload("source-kind-required").model_dump()
        without_metadata = {
            key: value
            for key, value in payload.items()
            if key != "metadata"
        }
        with self.assertRaises(ValidationError):
            RetrievalQualityEventPayload.model_validate(without_metadata)

        for metadata in (
            {},
            {"source_kind": "combined"},
            {"source_kind": "unexpected"},
            {"source_kind": 1},
        ):
            with self.subTest(metadata=metadata):
                with self.assertRaises(ValidationError):
                    RetrievalQualityEventPayload.model_validate(
                        {
                            **payload,
                            "metadata": metadata,
                        }
                    )

        normalized = RetrievalQualityEventPayload.model_validate(
            {
                **payload,
                "metadata": {
                    **payload["metadata"],
                    "source_kind": " Reply ",
                },
            }
        )
        self.assertEqual(normalized.metadata["source_kind"], "reply")

    def test_plugin_user_action_failure_reasons_are_accepted(self):
        for feedback_type, failure_reason in (
            ("corrected", "user_correction"),
            ("unhelpful", "user_unhelpful"),
        ):
            with self.subTest(feedback_type=feedback_type):
                payload = self._payload(
                    f"user-action-{feedback_type}",
                    feedback_type=feedback_type,
                    failure_reason=failure_reason,
                )
                validated = RetrievalQualityEventPayload.model_validate(payload)
                self.assertEqual(validated.feedback_type, feedback_type)
                self.assertEqual(validated.failure_reason, failure_reason)

    def test_idempotency_key_cannot_be_reused_for_another_identity(self):
        original = self._payload(
            "identity-conflict",
            conversation_id="202608100001",
            request_id="request-original",
        )
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=[original]),
            self.db,
            None,
        )

        conflicting = self._payload(
            "identity-conflict",
            conversation_id="202608100002",
            request_id="request-conflicting",
        )
        with self.assertRaises(HTTPException) as raised:
            submit_retrieval_quality_events(
                RetrievalQualityEventBatch(items=[conflicting]),
                self.db,
                None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "IDEMPOTENCY_IDENTITY_CONFLICT",
        )

    def test_same_batch_reuses_duplicate_idempotency_key_with_autoflush_disabled(self):
        original = self._payload(
            "same-batch-retry",
            conversation_id="202608100001",
            request_id="request-same-batch",
        )
        duplicate = self._payload(
            "same-batch-retry",
            conversation_id="202608100001",
            request_id="request-same-batch",
        )

        response = submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=[original, duplicate]),
            self.db,
            None,
        )

        self.assertEqual(response.recorded, 1)
        self.assertEqual(response.reused, 1)
        self.assertEqual(
            [item.status for item in response.results],
            ["recorded", "reused"],
        )
        self.assertEqual(
            self.db.query(RetrievalQualityEvent)
            .filter(
                RetrievalQualityEvent.idempotency_key
                == "same-batch-retry"
            )
            .count(),
            1,
        )

    def test_same_batch_rejects_identity_rebinding_before_commit(self):
        original = self._payload(
            "same-batch-conflict",
            conversation_id="202608100001",
            request_id="request-original",
        )
        conflicting = self._payload(
            "same-batch-conflict",
            conversation_id="202608100002",
            request_id="request-conflicting",
        )

        with self.assertRaises(HTTPException) as raised:
            submit_retrieval_quality_events(
                RetrievalQualityEventBatch(items=[original, conflicting]),
                self.db,
                None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "IDEMPOTENCY_IDENTITY_CONFLICT",
        )

    def test_idempotency_key_cannot_be_reused_for_another_source_pool(self):
        original = self._payload(
            "source-kind-conflict",
            conversation_id="202608100004",
            request_id="request-shared",
            metadata={"source_kind": "reply"},
        )
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=[original]),
            self.db,
            None,
        )

        conflicting = self._payload(
            "source-kind-conflict",
            conversation_id="202608100004",
            request_id="request-shared",
            metadata={"source_kind": "standard"},
        )
        with self.assertRaises(HTTPException) as raised:
            submit_retrieval_quality_events(
                RetrievalQualityEventBatch(items=[conflicting]),
                self.db,
                None,
            )

        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(
            raised.exception.detail["code"],
            "IDEMPOTENCY_IDENTITY_CONFLICT",
        )

    def test_analytics_paginates_after_latest_work_order_and_source_pool_collapse(
        self,
    ):
        items = [
            self._payload(
                f"page-{index:02d}",
                conversation_id=f"900000000{index:03d}",
            )
            for index in range(22)
        ]
        items.extend(
            [
                self._payload(
                    "ticket-old-page",
                    conversation_id="999999999999",
                    request_id="request-page-old",
                    metadata={"source_kind": "reply"},
                ),
                self._payload(
                    "ticket-new-page",
                    conversation_id="999999999999",
                    request_id="request-page-new",
                    metadata={"source_kind": "reply"},
                ),
            ]
        )
        submit_retrieval_quality_events(
            RetrievalQualityEventBatch(items=items),
            self.db,
            None,
        )
        events = {
            event.idempotency_key: event
            for event in self.db.query(RetrievalQualityEvent).all()
        }
        events["ticket-old-page"].created_at = datetime(2026, 8, 10, 10, 0, 0)
        events["ticket-new-page"].created_at = datetime(2026, 8, 10, 11, 0, 0)
        self.db.commit()

        pages = [
            get_retrieval_analytics(
                self.db,
                None,
                page=page,
                page_size=10,
            )
            for page in (1, 2, 3)
        ]
        risk_ids = {
            item["id"]
            for response in pages
            for item in response["risks"]
        }

        self.assertEqual(pages[0]["summary"]["candidate_requests"], 23)
        self.assertEqual(pages[0]["summary"]["candidate_queries"], 23)
        self.assertEqual(pages[0]["pagination"]["total"], 23)
        self.assertEqual(pages[0]["pagination"]["total_pages"], 3)
        self.assertEqual(
            [len(response["risks"]) for response in pages],
            [10, 10, 3],
        )
        self.assertNotIn(events["ticket-old-page"].id, risk_ids)
        self.assertIn(events["ticket-new-page"].id, risk_ids)
        self.assertEqual(len(risk_ids), 23)


if __name__ == "__main__":
    unittest.main()
