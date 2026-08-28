import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Lock
from time import sleep
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.knowledge import Category, Knowledge, KnowledgeEmbedding, KnowledgeStatus
from app.services.knowledge_dedup import (
    _categories_overlap_for_deduplication,
    _combined_dedup_similarity,
    _has_content_containment,
    _has_enough_semantic_content,
    _split_search_chunks,
    build_dedup_documents,
    build_embedding_text,
    build_search_documents,
    check_duplicate,
    clear_query_embedding_cache,
    _query_embedding,
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

    def test_category_scope_only_skips_two_explicit_disjoint_categories(self):
        self.assertTrue(
            _categories_overlap_for_deduplication(
                [{"categoryId": "phone", "categoryName": "手机"}],
                ["phone"],
            )
        )
        self.assertTrue(
            _categories_overlap_for_deduplication(
                ["手机", "平板"],
                ["平板电脑"],
            )
        )
        self.assertTrue(_categories_overlap_for_deduplication([], ["手机"]))
        self.assertTrue(_categories_overlap_for_deduplication(["全部"], ["手机"]))
        self.assertFalse(
            _categories_overlap_for_deduplication(["手机"], ["笔记本"])
        )

    @staticmethod
    def _title_match_session(matches):
        query = MagicMock()
        query.filter.return_value = query
        query.order_by.return_value = query
        query.limit.return_value = query
        query.all.return_value = matches
        db = MagicMock()
        db.query.return_value = query
        return db

    def test_same_normalized_title_and_content_blocks_without_embedding(self):
        existing = SimpleNamespace(
            id="A-00001",
            title="  按键颜色不符是什么意思  ",
            content={"blocks": [{"type": "text", "value": "请按标准核验颜色。"}]},
            status=SimpleNamespace(value="published"),
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            category_id="cat-qc-standard",
        )
        db = self._title_match_session([existing])

        with patch("app.services.knowledge_dedup.embed_texts") as embed:
            decision = check_duplicate(
                db,
                title="按键颜色不符是什么意思",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "请 按标准核验颜色。"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
            )

        self.assertEqual(decision.action, "block_duplicate")
        self.assertEqual(decision.matches[0].match_type, "exact")
        embed.assert_not_called()

    def test_same_title_with_one_character_content_requires_human_review(self):
        existing = SimpleNamespace(
            id="A-00001",
            title="按键颜色不符是什么意思",
            content={"blocks": [{"type": "text", "value": "请按标准核验颜色。"}]},
            status=SimpleNamespace(value="published"),
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            category_id="cat-qc-standard",
        )
        db = self._title_match_session([existing])

        with patch(
            "app.services.knowledge_dedup.embed_texts",
            return_value=[[0.1], [0.2], [0.3]],
        ):
            decision = check_duplicate(
                db,
                title="按键颜色不符是什么意思",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "1"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
            )

        self.assertEqual(decision.action, "review_duplicate")
        self.assertEqual(decision.matches[0].match_type, "title_exact")
        self.assertEqual(decision.matches[0].similarity, 1.0)

    def test_exact_duplicate_is_blocked_only_inside_same_origin_and_business(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Category.__table__.create(engine)
        Knowledge.__table__.create(engine)
        KnowledgeEmbedding.__table__.create(engine)
        with Session(engine) as db:
            db.add(Category(id="cat-qc-standard", name="质检标准"))
            db.add(
                Knowledge(
                    id="A-00001",
                    knowledge_origin="business_accumulation",
                    business_type="self_operated",
                    title="按键颜色不符是什么意思",
                    content={
                        "blocks": [
                            {"type": "text", "value": "测试内容"}
                        ]
                    },
                    category_id="cat-qc-standard",
                    status=KnowledgeStatus.PUBLISHED,
                    created_by="tester",
                )
            )
            db.add(
                Knowledge(
                    id="A-00002",
                    knowledge_origin="headquarters_standard",
                    business_type="self_operated",
                    title="按键颜色不符是什么意思",
                    content={
                        "blocks": [
                            {"type": "text", "value": "测试内容"}
                        ]
                    },
                    category_id="cat-qc-standard",
                    status=KnowledgeStatus.PUBLISHED,
                    created_by="tester",
                )
            )
            db.commit()

            same_business = check_duplicate(
                db,
                title="按键颜色不符是什么意思",
                subtitles=[],
                content={
                    "blocks": [
                        {"type": "text", "value": "测试内容"}
                    ]
                },
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
            )
            with patch(
                "app.services.knowledge_dedup.embed_texts",
                return_value=[[0.1], [0.2], [0.3]],
            ):
                other_business = check_duplicate(
                    db,
                    title="按键颜色不符是什么意思",
                    subtitles=[],
                    content={
                        "blocks": [
                            {"type": "text", "value": "测试内容"}
                        ]
                    },
                    scene_tags=[],
                    knowledge_origin="business_accumulation",
                    business_type="aggregated",
                )
            same_business_other_origin = check_duplicate(
                db,
                title="按键颜色不符是什么意思",
                subtitles=[],
                content={
                    "blocks": [
                        {"type": "text", "value": "测试内容"}
                    ]
                },
                scene_tags=[],
                knowledge_origin="headquarters_standard",
                business_type="self_operated",
            )

        self.assertEqual(same_business.action, "block_duplicate")
        self.assertEqual(same_business.matches[0].business_type, "self_operated")
        self.assertEqual(other_business.action, "create")
        self.assertEqual(other_business.matches, [])
        self.assertEqual(same_business_other_origin.action, "block_duplicate")
        self.assertEqual(
            same_business_other_origin.matches[0].knowledge_origin,
            "headquarters_standard",
        )

    def test_exact_duplicate_is_skipped_for_disjoint_applicable_categories(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Category.__table__.create(engine)
        Knowledge.__table__.create(engine)
        KnowledgeEmbedding.__table__.create(engine)
        with Session(engine) as db:
            db.add(Category(id="cat-qc-standard", name="质检标准"))
            existing = Knowledge(
                id="A-00001",
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                title="储存容量如何判断",
                content={"blocks": [{"type": "text", "value": "按系统显示容量判断。"}]},
                category_id="cat-qc-standard",
                applicable_categories=["安卓手机"],
                status=KnowledgeStatus.PUBLISHED,
                created_by="tester",
            )
            db.add(existing)
            db.commit()

            same_category = check_duplicate(
                db,
                title="储存容量如何判断",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "按系统显示容量判断。"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                applicable_categories=[{"categoryName": "安卓手机"}],
            )
            with patch(
                "app.services.knowledge_dedup.embed_texts",
                return_value=[[0.1], [0.2], [0.3]],
            ):
                different_category = check_duplicate(
                    db,
                    title="储存容量如何判断",
                    subtitles=[],
                    content={"blocks": [{"type": "text", "value": "按系统显示容量判断。"}]},
                    scene_tags=[],
                    knowledge_origin="business_accumulation",
                    business_type="self_operated",
                    applicable_categories=["苹果手机"],
                )
            missing_incoming_category = check_duplicate(
                db,
                title="储存容量如何判断",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "按系统显示容量判断。"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                applicable_categories=[],
            )

            existing.applicable_categories = []
            db.commit()
            missing_existing_category = check_duplicate(
                db,
                title="储存容量如何判断",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "按系统显示容量判断。"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                applicable_categories=["苹果手机"],
            )

        self.assertEqual(same_category.action, "block_duplicate")
        self.assertEqual(different_category.action, "create")
        self.assertEqual(different_category.matches, [])
        self.assertEqual(missing_incoming_category.action, "block_duplicate")
        self.assertEqual(missing_existing_category.action, "block_duplicate")

    def test_disjoint_categories_skip_containment_and_semantic_candidates(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        Category.__table__.create(engine)
        Knowledge.__table__.create(engine)
        KnowledgeEmbedding.__table__.create(engine)
        with Session(engine) as db:
            db.add(Category(id="cat-qc-standard", name="质检标准"))
            db.add(
                Knowledge(
                    id="A-00001",
                    knowledge_origin="business_accumulation",
                    business_type="self_operated",
                    title="安卓设备容量规则",
                    content={
                        "blocks": [
                            {
                                "type": "text",
                                "value": "先打开系统设置，再查看设备当前显示的存储容量并记录结果。",
                            }
                        ]
                    },
                    category_id="cat-qc-standard",
                    applicable_categories=["安卓手机"],
                    status=KnowledgeStatus.PUBLISHED,
                    created_by="tester",
                )
            )
            db.commit()

            decision = check_duplicate(
                db,
                title="苹果设备容量规则",
                subtitles=[],
                content={"blocks": [{"type": "text", "value": "查看设备当前显示的存储容量并记录结果"}]},
                scene_tags=[],
                knowledge_origin="business_accumulation",
                business_type="self_operated",
                applicable_categories=["苹果手机"],
                embedding_vectors=([0.1], [0.2], [0.3]),
            )

        self.assertEqual(decision.action, "create")
        self.assertEqual(decision.matches, [])


class QueryEmbeddingCacheTests(unittest.TestCase):
    def setUp(self):
        self.original_provider = settings.EMBEDDING_PROVIDER
        self.original_base_url = settings.EMBEDDING_BASE_URL
        self.original_model = settings.EMBEDDING_MODEL
        self.original_dimensions = settings.EMBEDDING_DIMENSIONS
        self.original_batch_size = settings.QUERY_EMBEDDING_BATCH_SIZE
        self.original_batch_wait_ms = settings.QUERY_EMBEDDING_BATCH_WAIT_MS
        clear_query_embedding_cache()
        settings.EMBEDDING_DIMENSIONS = 2
        settings.QUERY_EMBEDDING_BATCH_SIZE = 8
        settings.QUERY_EMBEDDING_BATCH_WAIT_MS = 50

    def tearDown(self):
        clear_query_embedding_cache()
        settings.EMBEDDING_PROVIDER = self.original_provider
        settings.EMBEDDING_BASE_URL = self.original_base_url
        settings.EMBEDDING_MODEL = self.original_model
        settings.EMBEDDING_DIMENSIONS = self.original_dimensions
        settings.QUERY_EMBEDDING_BATCH_SIZE = self.original_batch_size
        settings.QUERY_EMBEDDING_BATCH_WAIT_MS = self.original_batch_wait_ms

    @patch("app.services.knowledge_dedup.embed_texts")
    def test_same_normalized_query_uses_one_embedding(self, embed):
        embed.return_value = [[0.1, 0.2]]

        first = _query_embedding("  同一个问题  ")
        second = _query_embedding("同一个问题")

        self.assertEqual(first, second)
        embed.assert_called_once_with(["同一个问题"])

    @patch("app.services.knowledge_dedup.embed_texts")
    def test_model_or_dimension_change_does_not_reuse_vector(self, embed):
        embed.side_effect = [[[0.1, 0.2]], [[0.3, 0.4]]]

        _query_embedding("问题")
        settings.EMBEDDING_MODEL = "Qwen/other"
        _query_embedding("问题")

        self.assertEqual(embed.call_count, 2)

    @patch("app.services.knowledge_dedup.embed_texts")
    def test_eight_concurrent_identical_queries_are_merged(self, embed):
        """The batcher/cache must prevent an embedding stampede under load."""
        start = Barrier(8)
        calls_lock = Lock()
        call_count = 0

        def slow_embedding(texts):
            nonlocal call_count
            with calls_lock:
                call_count += 1
            # Keep the first request in flight while the other seven contend
            # for the same key lock.
            sleep(0.05)
            self.assertEqual(texts, ["同一个问题"])
            return [[0.1, 0.2]]

        embed.side_effect = slow_embedding

        def run_query(index):
            start.wait(timeout=2)
            # Vary only surrounding whitespace; all calls must share a key.
            return _query_embedding(
                ("  同一个问题  " if index % 2 else "同一个问题")
            )

        with ThreadPoolExecutor(max_workers=8) as executor:
            vectors = list(executor.map(run_query, range(8)))

        self.assertEqual(call_count, 1)
        self.assertEqual(vectors, [[0.1, 0.2]] * 8)

    @patch("app.services.knowledge_dedup.embed_texts")
    def test_eight_concurrent_different_queries_share_one_batch(self, embed):
        start = Barrier(8)
        expected = {
            f"问题{index}": [float(index), float(index) + 0.5]
            for index in range(8)
        }

        def batch_embedding(texts):
            return [expected[text] for text in texts]

        embed.side_effect = batch_embedding

        def run_query(index):
            start.wait(timeout=2)
            return _query_embedding(f"问题{index}")

        with ThreadPoolExecutor(max_workers=8) as executor:
            vectors = list(executor.map(run_query, range(8)))

        embed.assert_called_once()
        self.assertEqual(set(embed.call_args.args[0]), set(expected))
        self.assertEqual(
            vectors,
            [expected[f"问题{index}"] for index in range(8)],
        )


if __name__ == "__main__":
    unittest.main()
