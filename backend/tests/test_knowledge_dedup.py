import unittest
from types import SimpleNamespace

from app.services.knowledge_dedup import (
    _combined_dedup_similarity,
    _has_content_containment,
    _has_enough_semantic_content,
    _split_search_chunks,
    build_dedup_documents,
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

    def test_dedup_documents_keep_title_and_content_separate(self):
        result = build_dedup_documents(
            "iPhone question",
            {"blocks": [{"type": "text", "value": "iPhone cannot start"}]},
        )
        self.assertEqual(
            result,
            (
                "iPhone question\niPhone cannot start",
                "iPhone question",
                "iPhone cannot start",
            ),
        )

    def test_rich_text_links_are_reduced_to_visible_text(self):
        result = build_embedding_text(
            "Account help",
            [],
            {
                "blocks": [
                    {
                        "type": "text",
                        "value": 'Read the <a href="https://example.com/help">help center</a>.',
                    }
                ]
            },
        )
        self.assertEqual(result, "Account help\nRead the help center.")

    def test_external_media_urls_do_not_pollute_embedding_text(self):
        result = build_embedding_text(
            "Image help",
            [],
            {
                "blocks": [
                    {"type": "text", "value": "Read the visible instructions."},
                    {
                        "type": "image",
                        "external_url": "https://cdn.example.com/image.png",
                        "alt": "",
                        "caption": "",
                    },
                    {
                        "type": "video",
                        "external_url": "https://cdn.example.com/demo.mp4",
                        "alt": "",
                        "caption": "",
                    },
                ]
            },
        )
        self.assertEqual(result, "Image help\nRead the visible instructions.")

    def test_dedup_similarity_requires_both_title_and_content_to_match(self):
        self.assertEqual(_combined_dedup_similarity(0.99, 0.70), 0.70)
        self.assertEqual(_combined_dedup_similarity(0.88, 0.93), 0.88)

    def test_short_content_skips_semantic_deduplication(self):
        self.assertFalse(_has_enough_semantic_content("测试内容"))
        self.assertTrue(_has_enough_semantic_content("无法正常启动设备"))

    def test_content_containment_requires_a_meaningful_fragment(self):
        self.assertFalse(_has_content_containment("1234567890123456", "3456"))
        self.assertTrue(_has_content_containment("1234567890123456", "345678901234"))
        self.assertTrue(_has_content_containment(" 1234 5678 9012 3456 ", "345678901234"))
        self.assertFalse(_has_content_containment("1234567890123456", "999999999999"))


if __name__ == "__main__":
    unittest.main()
