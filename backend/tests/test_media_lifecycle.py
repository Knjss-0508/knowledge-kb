import unittest
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.knowledge import (
    Category,
    Knowledge,
    KnowledgeMedia,
    MediaDeletionTask,
    MediaUploadStaging,
)
from app.routes import knowledge as knowledge_routes
from app.services.media_deletion import (
    enqueue_media_deletion,
    expire_media_upload_staging,
    process_media_deletion_tasks,
)
from app.services.media_storage import MediaStorageError


class FakeStorage:
    backend = "s3"

    def __init__(self, *, fail_put_at=None, fail_delete=False):
        self.fail_put_at = fail_put_at
        self.fail_delete = fail_delete
        self.put_calls = []
        self.delete_calls = []

    def put(self, filename, content, mime_type):
        self.put_calls.append((filename, content, mime_type))
        if self.fail_put_at == len(self.put_calls):
            raise MediaStorageError("put failed")
        return f"knowledge-kb/media/{filename}"

    def delete(self, storage_key, filename):
        self.delete_calls.append((storage_key, filename))
        if self.fail_delete:
            raise MediaStorageError("delete failed")


def staged_upload(
    temp_id="temp-one",
    *,
    username="tester",
    expires_at=None,
    status="ready",
    storage_backend=None,
    storage_key=None,
):
    filename = f"{temp_id.removeprefix('temp-')}.png"
    return MediaUploadStaging(
        id=temp_id,
        username=username,
        storage_backend=storage_backend or knowledge_routes.media_storage.backend,
        storage_key=(
            f"knowledge-kb/media/{filename}"
            if storage_key is None
            else storage_key
        ),
        filename=filename,
        status=status,
        media_type="image",
        original_name="image.png",
        file_size=5,
        mime_type="image/png",
        alt="暂存标题",
        caption="暂存说明",
        expires_at=expires_at or datetime.utcnow() + timedelta(minutes=15),
    )


class TempMediaLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        Category.__table__.create(self.engine)
        Knowledge.__table__.create(self.engine)
        KnowledgeMedia.__table__.create(self.engine)
        MediaUploadStaging.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)
        with self.session_factory() as db:
            db.add(Category(id="cat-qc-standard", name="质检标准"))
            db.add(
                Knowledge(
                    id="A-00001",
                    title="测试知识",
                    content={"blocks": [{"type": "text", "value": "旧正文"}]},
                    category_id="cat-qc-standard",
                    status=knowledge_routes.KnowledgeStatus.REVIEW,
                    source="manual",
                    created_by="tester",
                    updated_by="tester",
                )
            )
            db.commit()

    def tearDown(self):
        self.engine.dispose()

    def test_same_temp_id_is_consumed_once_without_reuploading_object(self):
        storage = FakeStorage()
        blocks = [
            {"type": "image", "media_id": "temp-one", "alt": "正面"},
            {"type": "image", "media_id": "temp-one", "alt": "重复引用"},
        ]
        content = {"blocks": blocks}
        with self.session_factory() as db:
            db.add(staged_upload(storage_backend=storage.backend))
            db.commit()

            item = db.get(Knowledge, "A-00001")
            item.content = content
            with patch.object(knowledge_routes, "media_storage", storage):
                knowledge_routes._persist_temp_media(
                    db,
                    item,
                    content,
                    "tester",
                )
            db.commit()

        self.assertEqual(storage.put_calls, [])
        self.assertEqual(storage.delete_calls, [])
        self.assertEqual(blocks[0]["media_id"], blocks[1]["media_id"])
        self.assertEqual(blocks[0]["media_id"], "one.png")
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 0)
            media = db.query(KnowledgeMedia).one()
            self.assertEqual(media.filename, "one.png")
            self.assertEqual(media.alt, "正面")
            self.assertEqual(media.caption, "暂存说明")

    def test_transaction_rollback_restores_staging_and_does_not_delete_object(self):
        storage = FakeStorage()
        with self.session_factory() as db:
            db.add(staged_upload(storage_backend=storage.backend))
            db.commit()

            item = db.get(Knowledge, "A-00001")
            content = {
                "blocks": [{"type": "image", "media_id": "temp-one"}]
            }
            item.content = content
            with patch.object(knowledge_routes, "media_storage", storage):
                knowledge_routes._persist_temp_media(
                    db,
                    item,
                    content,
                    "tester",
                )
            db.flush()
            db.rollback()

        self.assertEqual(storage.put_calls, [])
        self.assertEqual(storage.delete_calls, [])
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 1)
            self.assertEqual(db.query(KnowledgeMedia).count(), 0)
            item = db.get(Knowledge, "A-00001")
            self.assertEqual(
                item.content,
                {"blocks": [{"type": "text", "value": "旧正文"}]},
            )

    def test_unavailable_temp_ids_return_structured_422(self):
        with self.session_factory() as db:
            db.add(staged_upload("temp-valid"))
            db.add(
                staged_upload(
                    "temp-expired",
                    expires_at=datetime.utcnow() - timedelta(seconds=1),
                )
            )
            db.add(staged_upload("temp-other", username="other-user"))
            db.commit()

            item = db.get(Knowledge, "A-00001")
            content = {
                "blocks": [
                    {"type": "image", "media_id": "temp-valid"},
                    {"type": "image", "media_id": "temp-expired"},
                    {"type": "image", "media_id": "temp-other"},
                    {"type": "image", "media_id": "temp-missing"},
                ]
            }
            item.content = content
            with self.assertRaises(HTTPException) as raised:
                knowledge_routes._persist_temp_media(
                    db,
                    item,
                    content,
                    "tester",
                )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "TEMP_UPLOAD_UNAVAILABLE",
                "message": "临时媒体已过期或不可用",
                "temp_ids": [
                    "temp-expired",
                    "temp-other",
                    "temp-missing",
                ],
            },
        )
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 3)
            self.assertEqual(db.query(KnowledgeMedia).count(), 0)

    def test_temp_media_type_mismatch_returns_structured_422(self):
        with self.session_factory() as db:
            db.add(staged_upload())
            db.commit()
            item = db.get(Knowledge, "A-00001")
            content = {
                "blocks": [{"type": "video", "media_id": "temp-one"}]
            }
            item.content = content
            with self.assertRaises(HTTPException) as raised:
                knowledge_routes._persist_temp_media(
                    db,
                    item,
                    content,
                    "tester",
                )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "TEMP_UPLOAD_TYPE_MISMATCH",
                "message": "临时媒体类型与内容块不一致",
                "temp_ids": ["temp-one"],
            },
        )
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 1)
            self.assertEqual(db.query(KnowledgeMedia).count(), 0)

    def test_uploading_staging_cannot_be_consumed(self):
        with self.session_factory() as db:
            db.add(staged_upload(status="uploading"))
            db.commit()
            item = db.get(Knowledge, "A-00001")
            content = {
                "blocks": [{"type": "image", "media_id": "temp-one"}]
            }
            item.content = content
            with self.assertRaises(HTTPException) as raised:
                knowledge_routes._persist_temp_media(
                    db,
                    item,
                    content,
                    "tester",
                )

        self.assertEqual(raised.exception.status_code, 422)
        self.assertEqual(
            raised.exception.detail,
            {
                "code": "TEMP_UPLOAD_UNAVAILABLE",
                "message": "临时媒体已过期或不可用",
                "temp_ids": ["temp-one"],
            },
        )
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 1)
            self.assertEqual(db.query(KnowledgeMedia).count(), 0)

    def test_dedup_failure_does_not_consume_staging(self):
        body = SimpleNamespace(
            title="标题",
            subtitles=[],
            content={"blocks": [{"type": "image", "media_id": "temp-one"}]},
            category_id="cat-qc-standard",
            applicable_scenes=[],
            applicable_categories=[],
            applicable_brands=[],
            applicable_models=[],
            confirm_dedup_review=False,
        )
        user = SimpleNamespace(username="tester")
        with self.session_factory() as db:
            db.add(staged_upload())
            db.commit()
            with patch.object(
                knowledge_routes,
                "_check_manual_deduplication",
                side_effect=HTTPException(
                    status_code=409,
                    detail={
                        "code": "DUPLICATE_REVIEW_REQUIRED",
                        "message": "请确认",
                    },
                ),
            ):
                with self.assertRaises(HTTPException):
                    knowledge_routes._create_knowledge_item(body, db, user)

        with self.session_factory() as db:
            self.assertEqual(db.query(MediaUploadStaging).count(), 1)

    def test_create_commit_failure_rolls_back_database_transaction(self):
        db = Mock()
        db.commit.side_effect = RuntimeError("commit failed")
        item = SimpleNamespace(id="A-00001")

        with patch.object(
            knowledge_routes,
            "_create_knowledge_item",
            return_value=item,
        ):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                knowledge_routes.create_knowledge(
                    SimpleNamespace(),
                    db,
                    SimpleNamespace(username="tester"),
                )

        db.rollback.assert_called_once()

    def test_update_commit_failure_rolls_back_database_transaction(self):
        item = SimpleNamespace(
            id="A-00001",
            status=knowledge_routes.KnowledgeStatus.REVIEW,
            created_by="tester",
            content={"blocks": []},
            media=[],
            updated_at=None,
        )
        user = SimpleNamespace(username="tester", role="super_admin")
        body = SimpleNamespace(
            model_dump=lambda **_: {
                "content": {
                    "blocks": [{"type": "image", "media_id": "temp-one"}]
                }
            }
        )
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = item
        db.commit.side_effect = RuntimeError("commit failed")

        with (
            patch.object(
                knowledge_routes,
                "_persist_temp_media",
            ),
            patch.object(knowledge_routes, "_sync_media_meta"),
            patch.object(
                knowledge_routes,
                "_knowledge_snapshot",
                return_value={},
            ),
            patch.object(
                knowledge_routes,
                "_referenced_media_filenames",
                return_value={"image.png"},
            ),
            patch.object(knowledge_routes, "ensure_embedding"),
            patch.object(knowledge_routes, "ensure_search_embeddings"),
        ):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                knowledge_routes.update_knowledge(
                    "A-00001",
                    body,
                    db,
                    user,
                )

        db.rollback.assert_called_once()

    def test_update_embedding_failure_rolls_back_database_transaction(self):
        item = SimpleNamespace(
            id="A-00001",
            status=knowledge_routes.KnowledgeStatus.REVIEW,
            created_by="tester",
            content={"blocks": []},
            media=[],
            updated_at=None,
        )
        user = SimpleNamespace(username="tester", role="super_admin")
        body = SimpleNamespace(
            model_dump=lambda **_: {
                "content": {
                    "blocks": [{"type": "image", "media_id": "temp-one"}]
                }
            }
        )
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = item

        with (
            patch.object(
                knowledge_routes,
                "_persist_temp_media",
            ),
            patch.object(knowledge_routes, "_sync_media_meta"),
            patch.object(
                knowledge_routes,
                "_knowledge_snapshot",
                return_value={},
            ),
            patch.object(
                knowledge_routes,
                "_referenced_media_filenames",
                return_value={"image.png"},
            ),
            patch.object(
                knowledge_routes,
                "ensure_embedding",
                side_effect=RuntimeError("embedding failed"),
            ),
            patch.object(knowledge_routes, "ensure_search_embeddings"),
        ):
            with self.assertRaisesRegex(RuntimeError, "embedding failed"):
                knowledge_routes.update_knowledge(
                    "A-00001",
                    body,
                    db,
                    user,
                )

        db.rollback.assert_called_once()
        db.commit.assert_not_called()


class UploadTempTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_temp_persists_staging_and_returns_temp_contract(self):
        user = SimpleNamespace(username="tester")
        file = SimpleNamespace(filename="image.png", content_type="image/png")
        db = Mock()
        storage = FakeStorage()
        events = []
        commit_snapshots = []

        def record_commit():
            events.append("commit")
            staging = db.add.call_args.args[0]
            commit_snapshots.append(
                (staging.status, staging.expires_at)
            )

        def record_refresh(*args, **kwargs):
            events.append("refresh")

        def record_put(filename, content, mime_type):
            events.append("put")
            storage.put_calls.append((filename, content, mime_type))
            return f"knowledge-kb/media/{filename}"

        db.commit.side_effect = record_commit
        db.refresh.side_effect = record_refresh
        storage.put = Mock(side_effect=record_put)

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(knowledge_routes, "media_storage", storage),
            patch.object(
                knowledge_routes,
                "_read_validated_upload",
                new=AsyncMock(return_value=(b"image", ".png")),
            ),
            patch.object(
                knowledge_routes,
                "run_in_threadpool",
                side_effect=run_inline,
            ) as threadpool,
            patch.object(
                knowledge_routes.settings,
                "MEDIA_UPLOAD_ACTIVE_TTL_SECONDS",
                3600,
            ),
            patch.object(
                knowledge_routes.settings,
                "MEDIA_UPLOAD_STAGING_TTL_SECONDS",
                900,
            ),
        ):
            response = await knowledge_routes.upload_temp(
                file,
                "image",
                "图片标题",
                "图片说明",
                db,
                user,
            )

        staging = db.add.call_args.args[0]
        self.assertIsInstance(staging, MediaUploadStaging)
        self.assertEqual(response["id"], staging.id)
        self.assertEqual(response["filename"], staging.id)
        self.assertTrue(staging.id.startswith("temp-"))
        self.assertTrue(staging.filename.endswith(".png"))
        self.assertNotEqual(staging.filename, staging.id)
        self.assertEqual(
            staging.storage_key,
            f"knowledge-kb/media/{staging.filename}",
        )
        self.assertEqual(staging.status, "ready")
        self.assertEqual(staging.file_size, 5)
        self.assertGreater(staging.expires_at, datetime.utcnow())
        self.assertEqual(threadpool.await_count, 1)
        self.assertEqual(db.commit.call_count, 2)
        db.refresh.assert_called_once_with(
            staging,
            with_for_update=True,
        )
        self.assertEqual(
            events,
            ["commit", "refresh", "put", "commit"],
        )
        self.assertEqual(
            [snapshot[0] for snapshot in commit_snapshots],
            ["uploading", "ready"],
        )
        self.assertGreater(
            (
                commit_snapshots[0][1]
                - commit_snapshots[1][1]
            ).total_seconds(),
            2600,
        )

    async def test_upload_temp_commit_failure_cleans_uploaded_object(self):
        user = SimpleNamespace(username="tester")
        file = SimpleNamespace(filename="image.png", content_type="image/png")
        db = Mock()
        db.commit.side_effect = [
            None,
            RuntimeError("commit failed"),
            None,
        ]
        storage = FakeStorage()

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(knowledge_routes, "media_storage", storage),
            patch.object(
                knowledge_routes,
                "_read_validated_upload",
                new=AsyncMock(return_value=(b"image", ".png")),
            ),
            patch.object(
                knowledge_routes,
                "run_in_threadpool",
                side_effect=run_inline,
            ),
            patch.object(
                knowledge_routes,
                "delete_media_immediately_or_enqueue",
                return_value=True,
            ) as cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                await knowledge_routes.upload_temp(
                    file,
                    "image",
                    "",
                    "",
                    db,
                    user,
                )

        staging = db.add.call_args.args[0]
        db.rollback.assert_called_once()
        self.assertEqual(db.commit.call_count, 3)
        db.refresh.assert_called_once_with(
            staging,
            with_for_update=True,
        )
        cleanup.assert_called_once_with(
            staging.storage_key,
            staging.filename,
            storage=storage,
        )

    async def test_upload_temp_storage_failure_expires_uploading_row(self):
        user = SimpleNamespace(username="tester")
        file = SimpleNamespace(filename="image.png", content_type="image/png")
        db = Mock()
        storage = FakeStorage(fail_put_at=1)

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(knowledge_routes, "media_storage", storage),
            patch.object(
                knowledge_routes,
                "_read_validated_upload",
                new=AsyncMock(return_value=(b"image", ".png")),
            ),
            patch.object(
                knowledge_routes,
                "run_in_threadpool",
                side_effect=run_inline,
            ),
        ):
            with self.assertRaises(HTTPException) as raised:
                await knowledge_routes.upload_temp(
                    file,
                    "image",
                    "",
                    "",
                    db,
                    user,
                )

        staging = db.add.call_args.args[0]
        self.assertEqual(raised.exception.status_code, 502)
        self.assertEqual(staging.status, "uploading")
        self.assertEqual(staging.storage_key, "")
        self.assertLessEqual(staging.expires_at, datetime.utcnow())
        self.assertEqual(db.commit.call_count, 2)
        db.refresh.assert_called_once_with(
            staging,
            with_for_update=True,
        )


class UploadMediaTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_runs_storage_in_threadpool_and_does_not_refresh_after_commit(self):
        item = SimpleNamespace(id="A-00001")
        user = SimpleNamespace(username="tester")
        file = SimpleNamespace(filename="image.png", content_type="image/png")
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = item
        storage = FakeStorage()

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(knowledge_routes, "media_storage", storage),
            patch.object(
                knowledge_routes,
                "_read_validated_upload",
                new=AsyncMock(return_value=(b"image", ".png")),
            ),
            patch.object(
                knowledge_routes,
                "_can_edit_knowledge",
                return_value=True,
            ),
            patch.object(
                knowledge_routes,
                "run_in_threadpool",
                side_effect=run_inline,
            ) as threadpool,
        ):
            response = await knowledge_routes.upload_media(
                "A-00001",
                file,
                "image",
                "",
                "",
                db,
                user,
            )

        self.assertTrue(response["filename"].endswith(".png"))
        self.assertEqual(threadpool.await_count, 1)
        db.commit.assert_called_once()
        db.refresh.assert_not_called()

    async def test_upload_commit_failure_uses_failure_safe_cleanup(self):
        item = SimpleNamespace(id="A-00001")
        user = SimpleNamespace(username="tester")
        file = SimpleNamespace(filename="image.png", content_type="image/png")
        db = Mock()
        db.query.return_value.filter.return_value.first.return_value = item
        db.commit.side_effect = RuntimeError("commit failed")
        storage = FakeStorage()

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch.object(knowledge_routes, "media_storage", storage),
            patch.object(
                knowledge_routes,
                "_read_validated_upload",
                new=AsyncMock(return_value=(b"image", ".png")),
            ),
            patch.object(
                knowledge_routes,
                "_can_edit_knowledge",
                return_value=True,
            ),
            patch.object(
                knowledge_routes,
                "run_in_threadpool",
                side_effect=run_inline,
            ),
            patch.object(
                knowledge_routes,
                "delete_media_immediately_or_enqueue",
                return_value=True,
            ) as cleanup,
        ):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                await knowledge_routes.upload_media(
                    "A-00001",
                    file,
                    "image",
                    "",
                    "",
                    db,
                    user,
                )

        db.rollback.assert_called_once()
        cleanup.assert_called_once()


class DeleteMediaTests(unittest.TestCase):
    def test_unreferenced_media_cleanup_does_not_require_embedding(self):
        item = SimpleNamespace(
            id="A-00001",
            content={"blocks": [{"type": "text", "value": "正文"}]},
            updated_at=None,
        )
        media = SimpleNamespace(
            filename="orphan.png",
            file_path="knowledge-kb/media/orphan.png",
        )
        user = SimpleNamespace(username="tester")
        knowledge_query = Mock()
        knowledge_query.filter.return_value.first.return_value = item
        media_query = Mock()
        media_query.filter.return_value.first.return_value = media
        db = Mock()
        db.query.side_effect = [knowledge_query, media_query]

        with (
            patch.object(
                knowledge_routes,
                "_can_edit_knowledge",
                return_value=True,
            ),
            patch.object(
                knowledge_routes,
                "enqueue_media_deletion",
            ) as enqueue,
            patch.object(
                knowledge_routes,
                "ensure_embedding",
            ) as ensure_embedding,
            patch.object(
                knowledge_routes,
                "ensure_search_embeddings",
            ) as ensure_search_embeddings,
        ):
            knowledge_routes.delete_media(
                "A-00001",
                "orphan.png",
                db,
                user,
            )

        enqueue.assert_called_once_with(
            db,
            "knowledge-kb/media/orphan.png",
            "orphan.png",
            storage_backend=knowledge_routes.media_storage.backend,
        )
        db.delete.assert_called_once_with(media)
        db.commit.assert_called_once()
        ensure_embedding.assert_not_called()
        ensure_search_embeddings.assert_not_called()
        self.assertEqual(
            item.content,
            {"blocks": [{"type": "text", "value": "正文"}]},
        )


class MediaDeletionOutboxTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        MediaDeletionTask.__table__.create(self.engine)
        MediaUploadStaging.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    def test_successful_task_is_removed(self):
        storage = FakeStorage()
        with self.session_factory() as db:
            enqueue_media_deletion(
                db,
                "knowledge-kb/media/image.png",
                "image.png",
                storage_backend="s3",
            )
            db.commit()

        result = process_media_deletion_tasks(
            session_factory=self.session_factory,
            storage=storage,
            now=datetime.utcnow() + timedelta(seconds=1),
        )

        self.assertEqual(result, {"selected": 1, "deleted": 1, "failed": 0})
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaDeletionTask).count(), 0)
        self.assertEqual(len(storage.delete_calls), 1)

    def test_failed_task_is_retried_idempotently(self):
        failing_storage = FakeStorage(fail_delete=True)
        with self.session_factory() as db:
            enqueue_media_deletion(
                db,
                "knowledge-kb/media/image.png",
                "image.png",
                storage_backend="s3",
            )
            db.commit()

        first_now = datetime.utcnow() + timedelta(seconds=1)
        first_result = process_media_deletion_tasks(
            session_factory=self.session_factory,
            storage=failing_storage,
            now=first_now,
        )
        self.assertEqual(
            first_result,
            {"selected": 1, "deleted": 0, "failed": 1},
        )

        with self.session_factory() as db:
            task = db.query(MediaDeletionTask).one()
            self.assertEqual(task.attempt_count, 1)
            self.assertGreater(task.next_attempt_at, first_now)
            task.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()

        healthy_storage = FakeStorage()
        second_result = process_media_deletion_tasks(
            session_factory=self.session_factory,
            storage=healthy_storage,
            now=datetime.utcnow(),
        )
        self.assertEqual(
            second_result,
            {"selected": 1, "deleted": 1, "failed": 0},
        )
        with self.session_factory() as db:
            self.assertEqual(db.query(MediaDeletionTask).count(), 0)

    def test_expired_staging_is_moved_to_outbox_before_object_deletion(self):
        now = datetime.utcnow()
        with self.session_factory() as db:
            db.add(
                staged_upload(
                    "temp-expired",
                    expires_at=now - timedelta(seconds=1),
                    status="uploading",
                    storage_backend="s3",
                    storage_key="",
                )
            )
            db.add(
                staged_upload(
                    "temp-fresh",
                    expires_at=now + timedelta(minutes=5),
                    storage_backend="s3",
                )
            )
            db.commit()

        expired = expire_media_upload_staging(
            session_factory=self.session_factory,
            now=now,
        )

        self.assertEqual(expired, 1)
        with self.session_factory() as db:
            staged_ids = {
                row.id for row in db.query(MediaUploadStaging).all()
            }
            self.assertEqual(staged_ids, {"temp-fresh"})
            task = db.query(MediaDeletionTask).one()
            self.assertEqual(task.storage_backend, "s3")
            self.assertEqual(task.storage_key, "")
            self.assertEqual(task.filename, "expired.png")

        storage = FakeStorage()
        result = process_media_deletion_tasks(
            session_factory=self.session_factory,
            storage=storage,
            now=now + timedelta(seconds=1),
        )
        self.assertEqual(
            result,
            {"selected": 1, "deleted": 1, "failed": 0},
        )
        self.assertEqual(
            storage.delete_calls,
            [("", "expired.png")],
        )

    def test_media_filename_has_unique_constraint(self):
        constraint_names = {
            constraint.name
            for constraint in KnowledgeMedia.__table__.constraints
        }
        self.assertIn("uq_knowledge_media_filename", constraint_names)

        staging_constraint_names = {
            constraint.name
            for constraint in MediaUploadStaging.__table__.constraints
        }
        self.assertIn(
            "uq_media_upload_staging_filename",
            staging_constraint_names,
        )
        self.assertIn(
            "ck_media_upload_staging_status",
            staging_constraint_names,
        )
        staging_index_names = {
            index.name for index in MediaUploadStaging.__table__.indexes
        }
        self.assertEqual(
            staging_index_names,
            {
                "ix_media_upload_staging_expires_at",
                "ix_media_upload_staging_username",
            },
        )


if __name__ == "__main__":
    unittest.main()
