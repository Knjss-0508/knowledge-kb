import unittest
from types import SimpleNamespace

from app.services.knowledge_dedup import (
    _split_search_chunks,
    build_embedding_text,
    build_search_documents,
)


class KnowledgeDedupTextTests(unittest.TestCase):
    def test_dedup_text_excludes_subtitles_and_tags(self):
        result = build_embedding_text(
            "主标题",
            ["副标题问法一", "副标题问法二"],
            {"blocks": [{"type": "text", "value": "知识正文"}]},
            ["场景标签"],
        )
        self.assertEqual(result, "主标题\n知识正文")

    def test_search_documents_keeps_each_subtitle_independent(self):
        item = SimpleNamespace(
            title="主标题",
            subtitles=["问法一", "问法二"],
            content={"blocks": [{"type": "text", "value": "知识正文"}]},
        )
        documents = build_search_documents(item)
        self.assertEqual(
            documents,
            [
                ("subtitle", 0, "主标题\n问法一"),
                ("subtitle", 1, "主标题\n问法二"),
                ("content", 0, "主标题\n知识正文"),
            ],
        )

    def test_long_content_is_split_with_bounded_chunks(self):
        chunks = _split_search_chunks("甲" * 1900)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(chunk) <= 800 for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
