import unittest
from datetime import datetime

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
        self.session_factory = sessionmaker(bind=self.engine)
        self.db = self.session_factory()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _payload(key: str, **overrides) -> RetrievalQualityEventPayload:
        data = {
            "schema_version": 2,
            "idempotency_key": key,
            "source_system": "qa-recommendation-plugin",
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

    def test_batch_persists_v2_fields_and_analytics_rates(self):
        items = [
            self._payload("accepted", total_latency_ms=10),
            self._payload(
                "alternative",
                selected=False,
                selected_knowledge_id="A-00002",
                selected_candidate_rank=2,
                total_latency_ms=20,
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
                top_rerank_score=0.5,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                total_latency_ms=30,
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

        alternative = (
            self.db.query(RetrievalQualityEvent)
            .filter(RetrievalQualityEvent.idempotency_key == "alternative")
            .one()
        )
        self.assertEqual(alternative.schema_version, 2)
        self.assertEqual(alternative.selection_status, "alternative_selected")
        self.assertEqual(alternative.selected_candidate_rank, 2)
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
        self.assertEqual(analytics["rates"]["candidate_coverage_rate"], 0.75)
        self.assertEqual(analytics["rates"]["threshold_pass_rate"], 0.6667)
        self.assertEqual(analytics["rates"]["top1_selection_rate"], 0.3333)
        self.assertEqual(analytics["rates"]["alternative_selection_rate"], 0.3333)
        self.assertEqual(analytics["rates"]["no_selection_rate"], 0.3333)
        self.assertEqual(analytics["latency"]["p50_ms"], 30.0)
        self.assertEqual(analytics["latency"]["p95_ms"], 50.0)

    def test_analytics_keeps_only_latest_event_per_work_order(self):
        items = [
            self._payload(
                "ticket-old",
                conversation_id="work-order-1",
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
            ),
            self._payload(
                "ticket-new",
                conversation_id="work-order-1",
            ),
            self._payload(
                "other-ticket",
                conversation_id="work-order-2",
                request_status="no_match",
                candidate_count=0,
                top_knowledge_id=None,
                top_rerank_score=None,
                selected=False,
                selected_knowledge_id=None,
                selected_candidate_rank=None,
                candidates=[],
            ),
            self._payload(
                "anonymous-low",
                conversation_id="  ",
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
            ),
            self._payload(
                "anonymous-accepted",
                conversation_id=None,
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

        self.assertEqual(analytics["summary"]["total"], 4)
        self.assertEqual(analytics["summary"]["accepted"], 2)
        self.assertEqual(analytics["summary"]["low_score"], 1)
        self.assertEqual(analytics["summary"]["no_candidates"], 1)
        self.assertEqual(analytics["summary"]["reviewed"], 0)
        self.assertEqual(analytics["summary"]["training_eligible"], 0)
        risk_ids = {risk["id"] for risk in analytics["risks"]}
        self.assertNotIn(events["ticket-old"].id, risk_ids)
        self.assertIn(events["ticket-new"].id, risk_ids)


if __name__ == "__main__":
    unittest.main()
