import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.core.config import settings
from app.core.integration_auth import require_integration_key
from app.main import app
from app.models.knowledge import KnowledgeStatus
from app.routes.integration import search_standard_provider_knowledge
from app.schemas.integration import IntegrationStandardSearchRequest
from app.services.embedding import EmbeddingServiceUnavailable


def _knowledge(
    index: int,
    *,
    status: KnowledgeStatus = KnowledgeStatus.PUBLISHED,
):
    return SimpleNamespace(
        id=f"A-{index:05d}",
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
            {
                "normalizedQuestion": "  屏幕漏光怎么判断  ",
                "productType": "手机",
                "model": "iPhone 17e",
                "orderInfo": {"category": "手机", "model": "iPhone 17e"},
                "partTerms": [" 屏幕 ", ""],
                "phenomenonTerms": ["漏光"],
                "categoryIntent": ["外观问题"],
                "limit": 8,
            }
        )

        self.assertEqual(request.normalized_question, "屏幕漏光怎么判断")
        self.assertEqual(request.part_terms, ["屏幕"])
        self.assertEqual(request.limit, 8)

        with self.assertRaises(ValidationError):
            IntegrationStandardSearchRequest.model_validate(
                {"normalizedQuestion": "   "}
            )
        with self.assertRaises(ValidationError):
            IntegrationStandardSearchRequest.model_validate(
                {
                    "normalizedQuestion": "屏幕",
                    "partTerms": ["项目"] * 101,
                }
            )

    @patch("app.routes.integration.search_embeddings")
    def test_returns_highest_five_in_plugin_compatible_shape(self, search):
        search.return_value = [
            (_knowledge(index), 0.99 - index * 0.01)
            for index in range(1, 8)
        ]
        body = IntegrationStandardSearchRequest.model_validate(
            {"normalizedQuestion": "屏幕漏光", "limit": 8}
        )

        response = search_standard_provider_knowledge(body, MagicMock(), None)
        payload = response.model_dump(mode="json", by_alias=True)

        search.assert_called_once()
        self.assertEqual(search.call_args.kwargs["query"], "屏幕漏光")
        self.assertEqual(search.call_args.kwargs["top_k"], 5)
        self.assertEqual(len(payload["candidates"]), 5)
        self.assertEqual(
            [item["id"] for item in payload["candidates"]],
            [f"A-{index:05d}" for index in range(1, 6)],
        )
        first = payload["candidates"][0]
        self.assertEqual(first["finalScore"], first["score"])
        self.assertEqual(first["level1Label"], "质检标准")
        self.assertEqual(first["productType"], "phone")
        self.assertEqual(first["sourceRef"], "knowledge-kb://knowledge/A-00001")
        self.assertIn("正文 1", first["text"])
        self.assertIn("图片说明 1", first["text"])
        self.assertNotIn("private.png", first["text"])
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["retrievalMode"], "semantic_pgvector")

    @patch("app.routes.integration.search_embeddings")
    def test_defensively_excludes_non_published_items(self, search):
        search.return_value = [
            (_knowledge(1, status=KnowledgeStatus.REVIEW), 0.99),
            (_knowledge(2), 0.90),
            (_knowledge(3, status=KnowledgeStatus.DEPRECATED), 0.89),
        ]
        body = IntegrationStandardSearchRequest.model_validate(
            {"normalizedQuestion": "屏幕漏光"}
        )

        response = search_standard_provider_knowledge(body, MagicMock(), None)

        self.assertEqual([item.id for item in response.candidates], ["A-00002"])

    @patch("app.routes.integration.search_embeddings", return_value=[])
    def test_no_match_returns_successful_empty_envelope(self, _search):
        body = IntegrationStandardSearchRequest.model_validate(
            {"normalizedQuestion": "不存在的知识"}
        )

        response = search_standard_provider_knowledge(body, MagicMock(), None)

        self.assertEqual(response.status, "no_match")
        self.assertEqual(response.candidates, [])

    @patch(
        "app.routes.integration.search_embeddings",
        side_effect=EmbeddingServiceUnavailable("model unavailable"),
    )
    def test_embedding_unavailable_returns_503(self, _search):
        body = IntegrationStandardSearchRequest.model_validate(
            {"normalizedQuestion": "屏幕漏光"}
        )

        with self.assertRaises(HTTPException) as raised:
            search_standard_provider_knowledge(body, MagicMock(), None)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertNotIn("model unavailable", str(raised.exception.detail))

    def test_integration_key_is_required(self):
        original = settings.INTEGRATION_API_KEY
        settings.INTEGRATION_API_KEY = "test-integration-key"
        try:
            with self.assertRaises(HTTPException) as missing:
                require_integration_key(None)
            self.assertEqual(missing.exception.status_code, 401)

            with self.assertRaises(HTTPException) as invalid:
                require_integration_key("wrong-key")
            self.assertEqual(invalid.exception.status_code, 401)

            self.assertIsNone(require_integration_key("test-integration-key"))
        finally:
            settings.INTEGRATION_API_KEY = original

    @patch("app.routes.integration.search_embeddings")
    def test_http_contract_uses_integration_key_and_camel_case(self, search):
        search.return_value = [
            (_knowledge(index), 0.99 - index * 0.01)
            for index in range(1, 8)
        ]
        original = settings.INTEGRATION_API_KEY
        settings.INTEGRATION_API_KEY = "test-integration-key"
        client = TestClient(app)
        try:
            unauthorized = client.post(
                "/api/v1/integration/standard-search",
                json={"normalizedQuestion": "屏幕漏光", "limit": 8},
            )
            self.assertEqual(unauthorized.status_code, 401)

            response = client.post(
                "/api/v1/integration/standard-search",
                headers={"X-Integration-Key": "test-integration-key"},
                json={
                    "normalizedQuestion": "屏幕漏光",
                    "productType": "手机",
                    "model": "iPhone 17e",
                    "limit": 8,
                },
            )
        finally:
            client.close()
            settings.INTEGRATION_API_KEY = original

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(len(payload["candidates"]), 5)
        self.assertIn("retrievalMode", payload)
        self.assertIn("knowledgeVersion", payload)
        self.assertIn("finalScore", payload["candidates"][0])
        self.assertNotIn("retrieval_mode", payload)
        self.assertNotIn("final_score", payload["candidates"][0])


if __name__ == "__main__":
    unittest.main()
