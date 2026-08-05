import unittest
from unittest.mock import Mock, patch

from app.core.config import settings
from app.services.embedding import embed_texts


class EmbeddingProviderTests(unittest.TestCase):
    def setUp(self):
        self.original_provider = settings.EMBEDDING_PROVIDER
        self.original_dimensions = settings.EMBEDDING_DIMENSIONS
        self.original_max_batch_texts = settings.EMBEDDING_MAX_BATCH_TEXTS
        self.original_max_batch_chars = settings.EMBEDDING_MAX_BATCH_CHARS
        settings.EMBEDDING_DIMENSIONS = 2

    def tearDown(self):
        settings.EMBEDDING_PROVIDER = self.original_provider
        settings.EMBEDDING_DIMENSIONS = self.original_dimensions
        settings.EMBEDDING_MAX_BATCH_TEXTS = self.original_max_batch_texts
        settings.EMBEDDING_MAX_BATCH_CHARS = self.original_max_batch_chars

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

    @patch("app.services.embedding.httpx.Client")
    def test_embedding_requests_are_batched_by_count_and_total_characters(self, client_class):
        settings.EMBEDDING_PROVIDER = "openai_compatible"
        settings.EMBEDDING_MAX_BATCH_TEXTS = 3
        settings.EMBEDDING_MAX_BATCH_CHARS = 5
        client = client_class.return_value.__enter__.return_value

        def response_for_request(*_args, **kwargs):
            response = Mock()
            response.json.return_value = {
                "data": [
                    {"embedding": [float(index), 0.2]}
                    for index, _ in enumerate(kwargs["json"]["input"])
                ]
            }
            return response

        client.post.side_effect = response_for_request

        texts = ["甲" * 3, "乙" * 2, "丙" * 3, "丁"]
        vectors = embed_texts(texts)

        self.assertEqual(len(vectors), len(texts))
        self.assertEqual(
            [call.kwargs["json"]["input"] for call in client.post.call_args_list],
            [["甲" * 3, "乙" * 2], ["丙" * 3, "丁"]],
        )

    @patch("app.services.embedding.httpx.Client")
    def test_batch_progress_hook_runs_after_each_embedding_batch(self, client_class):
        settings.EMBEDDING_PROVIDER = "openai_compatible"
        settings.EMBEDDING_MAX_BATCH_TEXTS = 2
        settings.EMBEDDING_MAX_BATCH_CHARS = 100
        client = client_class.return_value.__enter__.return_value

        def response_for_request(*_args, **kwargs):
            response = Mock()
            response.json.return_value = {
                "data": [
                    {"embedding": [0.1, 0.2]}
                    for _ in kwargs["json"]["input"]
                ]
            }
            return response

        client.post.side_effect = response_for_request
        progress: list[tuple[int, int]] = []

        vectors = embed_texts(
            ["甲", "乙", "丙", "丁", "戊"],
            on_batch_complete=lambda processed, total: progress.append(
                (processed, total)
            ),
        )

        self.assertEqual(len(vectors), 5)
        self.assertEqual(progress, [(2, 5), (4, 5), (5, 5)])


if __name__ == "__main__":
    unittest.main()
