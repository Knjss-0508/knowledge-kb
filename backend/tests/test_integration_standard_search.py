import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import settings
from app.core.database import get_db
from app.core.integration_auth import require_integration_key, require_retrieval_key
from app.main import app
from app.models.knowledge import KnowledgeStatus
from app.routes.integration import search_standard_provider_knowledge
from app.schemas.integration import IntegrationStandardSearchRequest
from app.services.embedding import EmbeddingServiceUnavailable


TEST_CONVERSATION_ID = "202608100001"
TEST_REQUEST_ID = "server-contract-test-1"


def _identity_payload(**overrides):
    return {
        "conversationId": TEST_CONVERSATION_ID,
        "requestId": TEST_REQUEST_ID,
        **overrides,
    }


def _call_search(body, db=None, **identity_overrides):
    return search_standard_provider_knowledge(
        body,
        identity_overrides.get("conversation_id", TEST_CONVERSATION_ID),
        identity_overrides.get("request_id", TEST_REQUEST_ID),
        db or MagicMock(),
        None,
    )


def _knowledge(
    index: int,
    *,
    status: KnowledgeStatus = KnowledgeStatus.PUBLISHED,
    knowledge_origin: str = "business_accumulation",
    business_type: str = "self_operated",
):
    return SimpleNamespace(
        id=f"A-{index:05d}",
        knowledge_origin=knowledge_origin,
        business_type=business_type,
        title=f"知识 {index}",
        subtitles=[f"问法 {index}", f"问法 {index}"],
        content={
            "blocks": [
                {"type": "text", "value": f"<p>正文 {index}</p>"},
                {
                    "type": "image",
                    "external_url": "https://example.com/private.png",
                    "alt": f"图片说明 {index}",
                },
            ]
        },
        status=status,
        category_id="cat-qc-standard",
        category=SimpleNamespace(name="质检标准"),
        applicable_scenes=["验机"],
        applicable_categories=["phone"],
        applicable_models=["iphone-17e"],
    )


class IntegrationStandardSearchTests(unittest.TestCase):
    def test_plugin_request_aliases_and_context_limits(self):
        request = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="  屏幕漏光怎么判断  ",
                knowledgeOrigin="business_accumulation",
                businessType="aggregated",
                productType="手机",
                categoryId="101",
                brand="苹果",
                brandId="1",
                model="iPhone 17e",
                modelId="17",
                orderInfo={
                    "category": "手机",
                    "categoryId": "101",
                    "brand": "苹果",
                    "brandId": "1",
                    "model": "iPhone 17e",
                    "modelId": "17",
                },
                partTerms=[" 屏幕 ", ""],
                phenomenonTerms=["漏光"],
                categoryIntent=["外观问题"],
                limit=8,
            )
        )

        self.assertEqual(request.conversation_id, TEST_CONVERSATION_ID)
        self.assertEqual(request.request_id, TEST_REQUEST_ID)
        self.assertEqual(request.normalized_question, "屏幕漏光怎么判断")
        self.assertEqual(request.knowledge_origin, "business_accumulation")
        self.assertEqual(request.business_type, "aggregated")
        self.assertEqual(request.part_terms, ["屏幕"])
        self.assertEqual(request.limit, 8)
        self.assertEqual(request.category_id, "101")
        self.assertEqual(request.brand, "苹果")
        self.assertEqual(request.brand_id, "1")
        self.assertEqual(request.model_id, "17")
        self.assertEqual(request.order_info.brand, "苹果")
        request_without_origin = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(normalizedQuestion="屏幕漏光怎么判断")
        )
        self.assertIsNone(request_without_origin.knowledge_origin)

        with self.assertRaises(ValidationError):
            IntegrationStandardSearchRequest.model_validate(
                _identity_payload(
                    normalizedQuestion="   ",
                    knowledgeOrigin="business_accumulation",
                )
            )
        with self.assertRaises(ValidationError):
            IntegrationStandardSearchRequest.model_validate(
                _identity_payload(
                    normalizedQuestion="屏幕",
                    knowledgeOrigin="business_accumulation",
                    partTerms=["项目"] * 101,
                )
            )
        for invalid_identity in (
            _identity_payload(
                conversationId="work-order-1",
                normalizedQuestion="屏幕",
            ),
            _identity_payload(
                requestId="包含空格的请求 ID",
                normalizedQuestion="屏幕",
            ),
        ):
            with self.assertRaises(ValidationError):
                IntegrationStandardSearchRequest.model_validate(invalid_identity)

    @patch("app.routes.integration.search_embeddings")
    def test_returns_top_five_per_origin_in_plugin_compatible_shape(self, search):
        def ranked_for_origin(*_args, **kwargs):
            origin = kwargs["knowledge_origin"]
            start = 1 if origin == "headquarters_standard" else 101
            return [
                (
                    _knowledge(index, knowledge_origin=origin),
                    0.99 - offset * 0.01,
                )
                for offset, index in enumerate(range(start, start + 7))
            ]

        search.side_effect = ranked_for_origin
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="屏幕漏光",
                knowledgeOrigin="business_accumulation",
                limit=8,
            )
        )

        response = _call_search(body)
        payload = response.model_dump(mode="json", by_alias=True)

        self.assertEqual(payload["conversationId"], TEST_CONVERSATION_ID)
        self.assertEqual(payload["requestId"], TEST_REQUEST_ID)
        self.assertEqual(search.call_count, 2)
        self.assertEqual(
            [
                call.kwargs["knowledge_origin"]
                for call in search.call_args_list
            ],
            ["headquarters_standard", "business_accumulation"],
        )
        for call in search.call_args_list:
            self.assertEqual(call.kwargs["query"], "屏幕漏光")
            self.assertEqual(call.kwargs["business_type"], "self_operated")
            self.assertEqual(call.kwargs["top_k"], 5)
        self.assertEqual(len(payload["candidates"]), 10)
        self.assertEqual(
            [item["id"] for item in payload["candidates"]],
            [
                *[f"A-{index:05d}" for index in range(1, 6)],
                *[f"A-{index:05d}" for index in range(101, 106)],
            ],
        )
        first = payload["candidates"][0]
        self.assertEqual(first["finalScore"], first["score"])
        self.assertEqual(first["knowledgeOrigin"], "headquarters_standard")
        self.assertEqual(
            [item["knowledgeOrigin"] for item in payload["candidates"]],
            ["headquarters_standard"] * 5
            + ["business_accumulation"] * 5,
        )
        self.assertEqual(first["businessType"], "self_operated")
        self.assertEqual(first["level1Label"], "质检标准")
        self.assertEqual(first["productType"], "phone")
        self.assertEqual(first["sourceRef"], "knowledge-kb://knowledge/A-00001")
        self.assertIn("正文 1", first["text"])
        self.assertIn("图片说明 1", first["text"])
        self.assertNotIn("private.png", first["text"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["retrievalMode"], "semantic_pgvector")
        self.assertEqual(payload["scoreThreshold"], 0.42)

    @patch(
        "app.routes.integration.get_active_runtime_values",
        return_value={"retrieval_score_threshold": 0.90},
    )
    @patch("app.routes.integration.search_embeddings")
    def test_active_retrieval_threshold_filters_each_origin(
        self,
        search,
        _runtime_config,
    ):
        def ranked_for_origin(*_args, **kwargs):
            origin = kwargs["knowledge_origin"]
            start = 1 if origin == "headquarters_standard" else 101
            return [
                (_knowledge(start, knowledge_origin=origin), 0.95),
                (_knowledge(start + 1, knowledge_origin=origin), 0.89),
            ]

        search.side_effect = ranked_for_origin
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(normalizedQuestion="屏幕漏光")
        )

        response = _call_search(body)
        payload = response.model_dump(mode="json", by_alias=True)

        self.assertEqual(payload["scoreThreshold"], 0.90)
        self.assertEqual(
            [item["id"] for item in payload["candidates"]],
            ["A-00001", "A-00101"],
        )

    @patch("app.routes.integration.search_embeddings", return_value=[])
    def test_explicit_business_type_overrides_legacy_hints(self, search):
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="聚合回收屏幕标准",
                knowledgeOrigin="business_accumulation",
                businessType="self_operated",
                productType="聚合回收",
            )
        )

        _call_search(body)

        self.assertEqual(search.call_count, 2)
        for call in search.call_args_list:
            self.assertEqual(call.kwargs["business_type"], "self_operated")

    @patch("app.routes.integration.search_embeddings", return_value=[])
    def test_legacy_aggregated_hint_selects_aggregated_business(self, search):
        requests = [
            {
                "normalizedQuestion": "屏幕标准",
                "knowledgeOrigin": "business_accumulation",
                "productType": "聚合回收",
            },
            {
                "normalizedQuestion": "屏幕标准",
                "knowledgeOrigin": "business_accumulation",
                "orderInfo": {"category": "聚合回收"},
            },
            {
                "normalizedQuestion": "屏幕标准",
                "knowledgeOrigin": "business_accumulation",
                "productType": "手机",
                "orderInfo": {"category": "聚合回收"},
            },
        ]

        for payload in requests:
            with self.subTest(payload=payload):
                search.reset_mock()
                body = IntegrationStandardSearchRequest.model_validate(
                    _identity_payload(**payload)
                )
                _call_search(body)
                self.assertEqual(search.call_count, 2)
                for call in search.call_args_list:
                    self.assertEqual(call.kwargs["business_type"], "aggregated")

    @patch("app.routes.integration.search_embeddings")
    def test_defensively_excludes_non_published_items(self, search):
        search.side_effect = [
            [
                (
                    _knowledge(
                        1,
                        status=KnowledgeStatus.REVIEW,
                        knowledge_origin="headquarters_standard",
                    ),
                    0.99,
                ),
                (
                    _knowledge(2, knowledge_origin="headquarters_standard"),
                    0.90,
                ),
            ],
            [
                (
                    _knowledge(
                        3,
                        status=KnowledgeStatus.DEPRECATED,
                        knowledge_origin="business_accumulation",
                    ),
                    0.89,
                ),
                (
                    _knowledge(4, knowledge_origin="business_accumulation"),
                    0.88,
                ),
            ],
        ]
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="屏幕漏光",
                knowledgeOrigin="business_accumulation",
            )
        )

        response = _call_search(body)

        self.assertEqual(
            [item.id for item in response.candidates],
            ["A-00002", "A-00004"],
        )

    @patch(
        "app.routes.integration.search_embeddings",
        side_effect=[[], []],
    )
    def test_no_match_returns_successful_empty_envelope(self, _search):
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="不存在的知识",
                knowledgeOrigin="business_accumulation",
            )
        )

        response = _call_search(body)

        self.assertEqual(response.status, "no_match")
        self.assertEqual(response.candidates, [])

    @patch("app.routes.integration.search_embeddings")
    def test_per_origin_limit_and_partial_match_are_preserved(self, search):
        search.side_effect = [
            [
                (
                    _knowledge(
                        index,
                        knowledge_origin="headquarters_standard",
                    ),
                    0.99 - index * 0.01,
                )
                for index in range(1, 5)
            ],
            [],
        ]
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="屏幕漏光",
                limit=2,
            )
        )

        response = _call_search(body)

        self.assertEqual(response.status, "success")
        self.assertEqual(
            [item.id for item in response.candidates],
            ["A-00001", "A-00002"],
        )
        self.assertEqual(
            [call.kwargs["top_k"] for call in search.call_args_list],
            [2, 2],
        )

    @patch(
        "app.routes.integration.search_embeddings",
        side_effect=EmbeddingServiceUnavailable("model unavailable"),
    )
    def test_embedding_unavailable_returns_503(self, _search):
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(
                normalizedQuestion="屏幕漏光",
                knowledgeOrigin="business_accumulation",
            )
        )

        response = _call_search(body)
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(payload["conversationId"], TEST_CONVERSATION_ID)
        self.assertEqual(payload["requestId"], TEST_REQUEST_ID)
        self.assertEqual(payload["code"], "EMBEDDING_SERVICE_UNAVAILABLE")
        self.assertNotIn("model unavailable", payload["message"])

    @patch("app.routes.integration.search_embeddings", return_value=[])
    def test_rejects_header_and_body_identity_mismatch(self, search):
        body = IntegrationStandardSearchRequest.model_validate(
            _identity_payload(normalizedQuestion="屏幕漏光")
        )

        response = _call_search(body, conversation_id="202608100002")
        payload = json.loads(response.body)

        self.assertEqual(response.status_code, 400)
        self.assertEqual(payload["conversationId"], TEST_CONVERSATION_ID)
        self.assertEqual(payload["requestId"], TEST_REQUEST_ID)
        self.assertEqual(payload["code"], "REQUEST_IDENTITY_MISMATCH")
        search.assert_not_called()

    def test_retrieval_key_is_required_and_not_interchangeable(self):
        original_integration = settings.INTEGRATION_API_KEY
        original_retrieval = settings.RETRIEVAL_API_KEY
        settings.INTEGRATION_API_KEY = "test-integration-key"
        settings.RETRIEVAL_API_KEY = "test-retrieval-key"
        try:
            with self.assertRaises(HTTPException) as missing:
                require_retrieval_key(None)
            self.assertEqual(missing.exception.status_code, 401)

            with self.assertRaises(HTTPException) as invalid:
                require_retrieval_key("wrong-key")
            self.assertEqual(invalid.exception.status_code, 401)

            with self.assertRaises(HTTPException) as old_key:
                require_retrieval_key("test-integration-key")
            self.assertEqual(old_key.exception.status_code, 401)

            with self.assertRaises(HTTPException) as retrieval_on_upstream:
                require_integration_key("test-retrieval-key")
            self.assertEqual(retrieval_on_upstream.exception.status_code, 401)

            self.assertIsNone(require_retrieval_key("test-retrieval-key"))
            self.assertIsNone(require_integration_key("test-integration-key"))

            settings.RETRIEVAL_API_KEY = ""
            with self.assertRaises(HTTPException) as unconfigured:
                require_retrieval_key("test-retrieval-key")
            self.assertEqual(unconfigured.exception.status_code, 503)
        finally:
            settings.INTEGRATION_API_KEY = original_integration
            settings.RETRIEVAL_API_KEY = original_retrieval

    def test_only_standard_search_and_feedback_use_retrieval_key(self):
        def dependency_calls(path: str, method: str):
            route = next(
                item
                for item in app.routes
                if getattr(item, "path", "") == path
                and method in getattr(item, "methods", set())
            )
            return {dependency.call for dependency in route.dependant.dependencies}

        for path in (
            "/api/v1/integration/standard-search",
            "/api/v1/integration/retrieval-events:batch",
        ):
            dependencies = dependency_calls(path, "POST")
            self.assertIn(require_retrieval_key, dependencies)
            self.assertNotIn(require_integration_key, dependencies)

        upstream_dependencies = dependency_calls(
            "/api/v1/integration/taxonomy",
            "GET",
        )
        self.assertIn(require_integration_key, upstream_dependencies)
        self.assertNotIn(require_retrieval_key, upstream_dependencies)

    @patch("app.routes.integration.search_embeddings")
    def test_http_contract_uses_retrieval_key_and_camel_case(self, search):
        def ranked_for_origin(*_args, **kwargs):
            origin = kwargs["knowledge_origin"]
            start = 1 if origin == "headquarters_standard" else 101
            return [
                (
                    _knowledge(
                        index,
                        knowledge_origin=origin,
                        business_type="aggregated",
                    ),
                    0.99 - offset * 0.01,
                )
                for offset, index in enumerate(range(start, start + 7))
            ]

        search.side_effect = ranked_for_origin
        original_integration = settings.INTEGRATION_API_KEY
        original_retrieval = settings.RETRIEVAL_API_KEY
        settings.INTEGRATION_API_KEY = "test-integration-key"
        settings.RETRIEVAL_API_KEY = "test-retrieval-key"
        client = TestClient(app)
        try:
            unauthorized = client.post(
                "/api/v1/integration/standard-search",
                json=_identity_payload(
                    normalizedQuestion="屏幕漏光",
                    limit=8,
                ),
            )
            self.assertEqual(unauthorized.status_code, 401)

            old_key_response = client.post(
                "/api/v1/integration/standard-search",
                headers={
                    "X-Integration-Key": "test-integration-key",
                    "X-Conversation-Id": TEST_CONVERSATION_ID,
                    "X-Request-Id": TEST_REQUEST_ID,
                },
                json=_identity_payload(
                    normalizedQuestion="屏幕漏光",
                    limit=8,
                ),
            )
            self.assertEqual(old_key_response.status_code, 401)

            response = client.post(
                "/api/v1/integration/standard-search",
                headers={
                    "X-Integration-Key": "test-retrieval-key",
                    "X-Conversation-Id": TEST_CONVERSATION_ID,
                    "X-Request-Id": TEST_REQUEST_ID,
                },
                json=_identity_payload(
                    normalizedQuestion="屏幕漏光",
                    knowledgeOrigin="business_accumulation",
                    businessType="aggregated",
                    productType="手机",
                    model="iPhone 17e",
                    limit=8,
                ),
            )
        finally:
            client.close()
            settings.INTEGRATION_API_KEY = original_integration
            settings.RETRIEVAL_API_KEY = original_retrieval

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["conversationId"], TEST_CONVERSATION_ID)
        self.assertEqual(payload["requestId"], TEST_REQUEST_ID)
        self.assertEqual(len(payload["candidates"]), 10)
        self.assertIn("retrievalMode", payload)
        self.assertIn("knowledgeVersion", payload)
        self.assertIn("finalScore", payload["candidates"][0])
        self.assertEqual(
            payload["candidates"][0]["knowledgeOrigin"],
            "headquarters_standard",
        )
        self.assertEqual(payload["candidates"][0]["businessType"], "aggregated")
        self.assertEqual(
            [item["knowledgeOrigin"] for item in payload["candidates"]],
            ["headquarters_standard"] * 5
            + ["business_accumulation"] * 5,
        )
        self.assertNotIn("retrieval_mode", payload)
        self.assertNotIn("final_score", payload["candidates"][0])
        self.assertEqual(search.call_count, 2)
        for call in search.call_args_list:
            self.assertEqual(call.kwargs["business_type"], "aggregated")

    def test_http_retrieval_key_scope_includes_feedback_but_not_taxonomy(self):
        original_integration = settings.INTEGRATION_API_KEY
        original_retrieval = settings.RETRIEVAL_API_KEY
        settings.INTEGRATION_API_KEY = "test-integration-key"
        settings.RETRIEVAL_API_KEY = "test-retrieval-key"

        db = MagicMock()

        def query_model(_model):
            query = MagicMock()
            query.filter.return_value.first.return_value = None
            query.order_by.return_value.all.return_value = []
            query.all.return_value = []
            return query

        db.query.side_effect = query_model

        def override_get_db():
            yield db

        app.dependency_overrides[get_db] = override_get_db
        client = TestClient(app)
        feedback_body = {
            "items": [
                {
                    "idempotency_key": "qa-plugin:trace-1",
                    "source_system": "qa-recommendation-plugin",
                    "query": "屏幕漏光怎么判定",
                    "conversation_id": TEST_CONVERSATION_ID,
                    "request_id": TEST_REQUEST_ID,
                    "candidate_count": 0,
                    "score_threshold": 0.42,
                    "selected": False,
                    "metadata": {
                        "trace_id": "trace-1",
                        "source_kind": "reply",
                    },
                }
            ]
        }
        try:
            old_key_feedback = client.post(
                "/api/v1/integration/retrieval-events:batch",
                headers={"X-Integration-Key": "test-integration-key"},
                json=feedback_body,
            )
            self.assertEqual(old_key_feedback.status_code, 401)

            feedback = client.post(
                "/api/v1/integration/retrieval-events:batch",
                headers={"X-Integration-Key": "test-retrieval-key"},
                json=feedback_body,
            )
            self.assertEqual(feedback.status_code, 202)
            self.assertEqual(feedback.json()["recorded"], 1)
            self.assertEqual(feedback.json()["results"][0]["outcome"], "no_candidates")
            self.assertEqual(
                feedback.json()["results"][0]["conversation_id"],
                TEST_CONVERSATION_ID,
            )
            self.assertEqual(
                feedback.json()["results"][0]["request_id"],
                TEST_REQUEST_ID,
            )
            db.add.assert_called_once()
            db.commit.assert_called_once()

            retrieval_key_taxonomy = client.get(
                "/api/v1/integration/taxonomy",
                headers={"X-Integration-Key": "test-retrieval-key"},
            )
            self.assertEqual(retrieval_key_taxonomy.status_code, 401)

            upstream_key_taxonomy = client.get(
                "/api/v1/integration/taxonomy",
                headers={"X-Integration-Key": "test-integration-key"},
            )
            self.assertEqual(upstream_key_taxonomy.status_code, 200)
            taxonomy = upstream_key_taxonomy.json()
            self.assertEqual(taxonomy["version"], "automation-v5")
            self.assertEqual(
                taxonomy["knowledge_origins"],
                [
                    {
                        "value": "headquarters_standard",
                        "label": "总部标准",
                    },
                    {
                        "value": "business_accumulation",
                        "label": "业务沉淀",
                    },
                ],
            )
            self.assertEqual(
                taxonomy["business_types"],
                [
                    {"value": "self_operated", "label": "自营回收"},
                    {"value": "aggregated", "label": "聚合回收"},
                ],
            )
        finally:
            client.close()
            app.dependency_overrides.pop(get_db, None)
            settings.INTEGRATION_API_KEY = original_integration
            settings.RETRIEVAL_API_KEY = original_retrieval


if __name__ == "__main__":
    unittest.main()
