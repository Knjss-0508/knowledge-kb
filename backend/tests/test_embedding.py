import unittest
from unittest.mock import Mock, patch

from app.core.config import settings
from app.services.embedding import embed_texts


class EmbeddingProviderTests(unittest.TestCase):
    def setUp(self):
        self.original_provider = settings.EMBEDDING_PROVIDER
        self.original_dimensions = settings.EMBEDDING_DIMENSIONS
        settings.EMBEDDING_DIMENSIONS = 2

    def tearDown(self):
        settings.EMBEDDING_PROVIDER = self.original_provider
        settings.EMBEDDING_DIMENSIONS = self.original_dimensions

    @patch("app.services.embedding.httpx.Client")
    def test_tei_provider_does_not_try_openai_endpoint(self, client_class):
        settings.EMBEDDING_PROVIDER = "tei"
        client = client_class.return_value.__enter__.return_value
        response = Mock()
        response.json.return_value = [[0.1, 0.2]]
        client.post.return_value = response

        self.assertEqual(embed_texts(["测试"]), [[0.1, 0.2]])
        self.assertTrue(client.post.call_args.args[0].endswith("/embed"))

    @patch("app.services.embedding.httpx.Client")
    def test_openai_provider_uses_openai_payload(self, client_class):
        settings.EMBEDDING_PROVIDER = "openai_compatible"
        client = client_class.return_value.__enter__.return_value
        response = Mock()
        response.json.return_value = {"data": [{"embedding": [0.1, 0.2]}]}
        client.post.return_value = response

        self.assertEqual(embed_texts(["测试"]), [[0.1, 0.2]])
        self.assertTrue(client.post.call_args.args[0].endswith("/embeddings"))
        self.assertEqual(client.post.call_args.kwargs["json"]["input"], ["测试"])


if __name__ == "__main__":
    unittest.main()
