import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.knowledge import (
    Category,
    Knowledge,
    KnowledgeEmbedding,
    KnowledgeSearchEmbedding,
    KnowledgeChangeLog,
    KnowledgeStatus,
    KnowledgeVectorTask,
)
from app.routes import knowledge as knowledge_routes
from app.schemas.knowledge import KnowledgeCreate, KnowledgeUpdate
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.knowledge_dedup import DedupDecision


class KnowledgeVectorTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        for model in (
            Category,
            Knowledge,
            KnowledgeChangeLog,
            KnowledgeEmbedding,
            KnowledgeSearchEmbedding,
            KnowledgeVectorTask,
        ):
            model.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            db.add(Category(id="cat-case-analysis", name="案例分析"))
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def _item(self, *, status=KnowledgeStatus.REVIEW, source="manual"):
        return Knowledge(
            id="A-00001",
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            title="原始标题",
            subtitles=["原始问法"],
            content={"blocks": [{"type": "text", "value": "原始正文"}]},
            category_id="cat-case-analysis",
            status=status,
            source=source,
            created_by="tester",
            updated_by="tester",
        )

    @staticmethod
    def _vector(value: float) -> list[float]:
        return [value] * settings.EMBEDDING_DIMENSIONS

    def test_manual_create_commits_without_embedding_and_queues_task(self):
        body = KnowledgeCreate(
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            title="新知识",
            content={"blocks": [{"type": "text", "value": "正文内容"}]},
            category_id="cat-case-analysis",
        )
        user = SimpleNamespace(username="tester")
        with self.session_factory() as db, patch(
            "app.routes.knowledge._generate_knowledge_id",
            return_value="A-00002",
        ), patch(
            "app.routes.knowledge._check_manual_deduplication"
        ) as check_duplicate, patch(
            "app.routes.knowledge.save_embedding"
        ) as save_embedding, patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ) as ensure_search:
            item = knowledge_routes._create_knowledge_item(body, db, user)
            db.commit()

            self.assertEqual(item.status, KnowledgeStatus.REVIEW)
            task = db.query(KnowledgeVectorTask).one()
            self.assertEqual(task.status, "queued")
            self.assertEqual(item.deduplication_metadata["action"], "pending")
            self.assertEqual(
                item.deduplication_metadata["vector_task_id"],
                task.id,
            )
            check_duplicate.assert_not_called()
            save_embedding.assert_not_called()
            ensure_search.assert_not_called()
            self.assertEqual(db.query(KnowledgeEmbedding).count(), 0)

    def test_manual_update_invalidates_old_vectors_and_supersedes_old_task(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            db.add(
                KnowledgeEmbedding(
                    id="emb-old",
                    knowledge_id=item.id,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_dimension=settings.EMBEDDING_DIMENSIONS,
                    content_hash="old",
                    embedding=self._vector(0.1),
                    embedding_vector=self._vector(0.1),
                    title_embedding_vector=self._vector(0.1),
                    content_embedding_vector=self._vector(0.1),
                )
            )
            db.add(
                KnowledgeSearchEmbedding(
                    id="se-old",
                    knowledge_id=item.id,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_kind="content",
                    chunk_index=0,
                    content_hash="old",
                    source_text="原始标题\n原始正文",
                    embedding_dimension=settings.EMBEDDING_DIMENSIONS,
                    embedding=self._vector(0.1),
                    embedding_vector=self._vector(0.1),
                )
            )
            old_task = KnowledgeVectorTask(
                id="kvt-old",
                knowledge_id=item.id,
                content_hash="old-task",
                status="completed",
                next_attempt_at=datetime.utcnow(),
            )
            db.add(old_task)
            db.commit()

            with patch(
                "app.routes.knowledge.ensure_embedding"
            ) as ensure_embedding, patch(
                "app.routes.knowledge.ensure_search_embeddings"
            ) as ensure_search, patch(
                "app.routes.knowledge._to_response",
                return_value={"id": item.id},
            ):
                response = knowledge_routes.update_knowledge(
                    item.id,
                    KnowledgeUpdate(title="新标题"),
                    db,
                    SimpleNamespace(
                        username="tester",
                        role="super_admin",
                        permissions=[],
                    ),
                )

            self.assertEqual(response["id"], item.id)
            db.expire_all()
            refreshed = db.get(Knowledge, item.id)
            tasks = db.query(KnowledgeVectorTask).order_by(
                KnowledgeVectorTask.created_at
            ).all()
            self.assertEqual(refreshed.title, "新标题")
            self.assertEqual(tasks[0].status, "superseded")
            self.assertEqual(tasks[1].status, "queued")
            self.assertEqual(db.query(KnowledgeEmbedding).count(), 0)
            self.assertEqual(db.query(KnowledgeSearchEmbedding).count(), 0)
            ensure_embedding.assert_not_called()
            ensure_search.assert_not_called()

    def test_manual_noop_update_keeps_existing_vectors_and_task(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            db.add(
                KnowledgeEmbedding(
                    id="emb-existing",
                    knowledge_id=item.id,
                    embedding_model=settings.EMBEDDING_MODEL,
                    embedding_dimension=settings.EMBEDDING_DIMENSIONS,
                    content_hash="existing",
                    embedding=self._vector(0.1),
                    embedding_vector=self._vector(0.1),
                    title_embedding_vector=self._vector(0.1),
                    content_embedding_vector=self._vector(0.1),
                )
            )
            db.commit()

            with patch(
                "app.routes.knowledge._invalidate_knowledge_vectors"
            ) as invalidate, patch(
                "app.routes.knowledge._enqueue_manual_vectorization"
            ) as enqueue, patch(
                "app.routes.knowledge._to_response",
                return_value={"id": item.id},
            ):
                response = knowledge_routes.update_knowledge(
                    item.id,
                    KnowledgeUpdate(title=item.title),
                    db,
                    SimpleNamespace(
                        username="tester",
                        role="super_admin",
                        permissions=[],
                    ),
                )

            self.assertEqual(response["id"], item.id)
            invalidate.assert_not_called()
            enqueue.assert_not_called()
            self.assertEqual(db.query(KnowledgeEmbedding).count(), 1)

    def test_worker_success_writes_vectors_and_marks_completed(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            task = KnowledgeVectorTask(
                id="kvt-success",
                knowledge_id=item.id,
                content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                status="queued",
                next_attempt_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()

        decision = DedupDecision(
            action="create",
            content_hash="dedup-content",
            embedding=self._vector(0.1),
            title_embedding=self._vector(0.2),
            content_embedding=self._vector(0.3),
            matches=[],
        )
        with patch(
            "app.routes.knowledge.check_duplicate",
            return_value=decision,
        ), patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ) as ensure_search:
            self.assertTrue(
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=self.session_factory
                )
            )

        with self.session_factory() as db:
            task = db.get(KnowledgeVectorTask, "kvt-success")
            item = db.get(Knowledge, "A-00001")
            self.assertEqual(task.status, "completed")
            self.assertEqual(item.deduplication_metadata["action"], "create")
            self.assertEqual(
                item.deduplication_metadata["vector_status"],
                "completed",
            )
            embedding = db.query(KnowledgeEmbedding).one()
            self.assertEqual(embedding.content_hash, "dedup-content")
            ensure_search.assert_called_once()

    def test_worker_builds_search_vectors_for_exact_duplicate_decision(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            db.add(
                KnowledgeVectorTask(
                    id="kvt-exact",
                    knowledge_id=item.id,
                    content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                    status="queued",
                    next_attempt_at=datetime.utcnow(),
                )
            )
            db.commit()

        decision = DedupDecision(
            action="block_duplicate",
            content_hash="dedup-exact",
            embedding=None,
            title_embedding=None,
            content_embedding=None,
            matches=[],
        )
        with patch(
            "app.routes.knowledge.check_duplicate",
            return_value=decision,
        ), patch(
            "app.routes.knowledge.ensure_embedding"
        ) as ensure_embedding, patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ) as ensure_search:
            self.assertTrue(
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=self.session_factory
                )
            )

        ensure_embedding.assert_called_once()
        ensure_search.assert_called_once()

    def test_worker_is_quiet_before_vector_task_migration(self):
        engine = create_engine("sqlite+pysqlite:///:memory:")
        session_factory = sessionmaker(bind=engine)
        try:
            self.assertFalse(
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=session_factory
                )
            )
        finally:
            engine.dispose()

    def test_worker_reclaims_running_task_without_a_lease(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            db.add(
                KnowledgeVectorTask(
                    id="kvt-no-lease",
                    knowledge_id=item.id,
                    content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                    status="running",
                    lease_expires_at=None,
                    next_attempt_at=datetime.utcnow(),
                )
            )
            db.commit()

        decision = DedupDecision(
            action="create",
            content_hash="dedup-no-lease",
            embedding=self._vector(0.1),
            title_embedding=self._vector(0.2),
            content_embedding=self._vector(0.3),
            matches=[],
        )
        with patch(
            "app.routes.knowledge.check_duplicate",
            return_value=decision,
        ), patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ):
            self.assertTrue(
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=self.session_factory
                )
            )

        with self.session_factory() as db:
            self.assertEqual(
                db.get(KnowledgeVectorTask, "kvt-no-lease").status,
                "completed",
            )

    def test_worker_supersedes_result_when_content_changes_during_embedding(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            task = KnowledgeVectorTask(
                id="kvt-stale",
                knowledge_id=item.id,
                content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                status="queued",
                next_attempt_at=datetime.utcnow(),
            )
            db.add(task)
            db.commit()

        def change_content(db, **_):
            current = db.get(Knowledge, "A-00001")
            current.content = {"blocks": [{"type": "text", "value": "更新后的正文"}]}
            db.commit()
            return DedupDecision(
                action="create",
                content_hash="stale",
                embedding=self._vector(0.1),
                title_embedding=self._vector(0.2),
                content_embedding=self._vector(0.3),
                matches=[],
            )

        with patch(
            "app.routes.knowledge.check_duplicate",
            side_effect=change_content,
        ), patch(
            "app.routes.knowledge.ensure_search_embeddings"
        ), patch(
            "app.routes.knowledge.save_embedding"
        ) as save_embedding:
            knowledge_routes.process_next_knowledge_vector_task(
                session_factory=self.session_factory
            )

        with self.session_factory() as db:
            task = db.get(KnowledgeVectorTask, "kvt-stale")
            self.assertEqual(task.status, "superseded")
            save_embedding.assert_not_called()

    def test_worker_retries_transient_embedding_failure_then_marks_failed(self):
        original_max = settings.KNOWLEDGE_VECTOR_MAX_ATTEMPTS
        original_base = settings.KNOWLEDGE_VECTOR_RETRY_BASE_SECONDS
        settings.KNOWLEDGE_VECTOR_MAX_ATTEMPTS = 2
        settings.KNOWLEDGE_VECTOR_RETRY_BASE_SECONDS = 1
        try:
            with self.session_factory() as db:
                item = self._item()
                db.add(item)
                db.flush()
                db.add(
                    KnowledgeVectorTask(
                        id="kvt-retry",
                        knowledge_id=item.id,
                        content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                        status="queued",
                        next_attempt_at=datetime.utcnow(),
                    )
                )
                db.commit()

            failure = EmbeddingServiceUnavailable("暂时不可用", retryable=True)
            with patch(
                "app.routes.knowledge.check_duplicate",
                side_effect=failure,
            ):
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=self.session_factory
                )
            with self.session_factory() as db:
                task = db.get(KnowledgeVectorTask, "kvt-retry")
                self.assertEqual(task.status, "queued")
                task.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
                db.commit()

            with patch(
                "app.routes.knowledge.check_duplicate",
                side_effect=failure,
            ):
                knowledge_routes.process_next_knowledge_vector_task(
                    session_factory=self.session_factory
                )
            with self.session_factory() as db:
                task = db.get(KnowledgeVectorTask, "kvt-retry")
                self.assertEqual(task.status, "failed")
                self.assertIn("最大尝试次数", task.error_message)
        finally:
            settings.KNOWLEDGE_VECTOR_MAX_ATTEMPTS = original_max
            settings.KNOWLEDGE_VECTOR_RETRY_BASE_SECONDS = original_base

    def test_approve_is_blocked_while_vector_task_is_pending(self):
        with self.session_factory() as db:
            item = self._item()
            db.add(item)
            db.flush()
            task = KnowledgeVectorTask(
                id="kvt-pending",
                knowledge_id=item.id,
                content_hash=knowledge_routes._knowledge_vector_content_hash(item),
                status="queued",
                next_attempt_at=datetime.utcnow(),
            )
            db.add(task)
            item.deduplication_metadata = {
                "action": "pending",
                "vector_status": "queued",
                "vector_task_id": task.id,
            }
            db.commit()

            with self.assertRaises(HTTPException) as raised:
                knowledge_routes.approve_knowledge(
                    item.id,
                    db,
                    SimpleNamespace(username="approver"),
                )

            self.assertEqual(raised.exception.status_code, 409)
            self.assertEqual(
                raised.exception.detail["code"],
                "VECTOR_PROCESSING",
            )
            self.assertEqual(item.status, KnowledgeStatus.REVIEW)


if __name__ == "__main__":
    unittest.main()
