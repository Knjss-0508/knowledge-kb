import hashlib
import unittest
from datetime import datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException
from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.models.knowledge import Category, KnowledgeImportTask
from app.routes.knowledge import (
    _import_embedding_batches,
    _import_retry_delay_seconds,
    _lock_import_task_attempt,
    _precompute_import_embeddings,
    cancel_knowledge_import_task,
    process_knowledge_import_task,
    process_next_knowledge_import_task,
    retry_failed_knowledge_import_task,
)
from app.schemas.knowledge import ExcelImportRowResult
from app.services.embedding import EmbeddingServiceUnavailable
from app.services.knowledge_excel import ExcelKnowledgeRow


class KnowledgeImportTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        KnowledgeImportTask.__table__.create(self.engine)
        Category.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _row(row_number, *, source_status="生效中", valid=True):
        return ExcelKnowledgeRow(
            row_number=row_number,
            title=f"标题{row_number}",
            knowledge_origin="business_accumulation",
            business_type="self_operated",
            category_id="cat-qc",
            content=f"正文{row_number}",
            subtitles=[],
            applicable_scenes=[],
            applicable_categories=["平板电脑"],
            applicable_brands=[],
            applicable_models=[],
            source_status=source_status,
            error_code=None if valid else "INVALID_ROW",
            error_message=None if valid else "无效行",
        )

    @patch("app.routes.knowledge.embed_texts")
    def test_import_embedding_precompute_batches_rows_and_keeps_review_light(self, embed):
        embed.side_effect = lambda texts: [[float(index)] for index, _ in enumerate(texts)]
        rows = [
            self._row(2, source_status="生效中"),
            self._row(3, source_status="待审核"),
        ]

        bundles = _precompute_import_embeddings(rows)

        self.assertEqual(embed.call_count, 1)
        # Published-source row has dedup (3) + search (1) documents; review
        # row only needs the three deduplication documents.
        self.assertEqual(len(embed.call_args.args[0]), 7)
        self.assertEqual(len(bundles[2].dedup_vectors), 3)
        self.assertEqual(bundles[3].search_vectors, {})

    def test_import_embedding_batches_keep_invalid_and_deprecated_rows(self):
        rows = [
            self._row(2, valid=False),
            self._row(3, source_status="已禁用"),
            self._row(4),
        ]

        batches = _import_embedding_batches(rows)

        self.assertEqual(
            [row.row_number for batch in batches for row, _ in batch],
            [2, 3, 4],
        )

    @staticmethod
    def _workbook_bytes(rows):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["标题", "知识来源", "业务类型", "知识分类", "正文"])
        for row in rows:
            sheet.append(row)
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    @staticmethod
    def _task(task_id, content, **overrides):
        values = {
            "id": task_id,
            "created_by": "tester",
            "original_filename": "knowledge.xlsx",
            "file_size": len(content),
            "file_sha256": hashlib.sha256(content).hexdigest(),
            "file_content": content,
            "status": "queued",
            "next_attempt_at": datetime.utcnow() - timedelta(seconds=1),
        }
        values.update(overrides)
        return KnowledgeImportTask(**values)

    def test_invalid_workbook_is_persisted_as_a_failed_task(self):
        with self.session_factory() as db:
            db.add(self._task("import-invalid", b"not an xlsx file"))
            db.commit()

        self.assertTrue(
            process_next_knowledge_import_task(
                session_factory=self.session_factory,
            )
        )

        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.processed_rows, 0)
            self.assertTrue(task.completed_at)
            self.assertTrue(task.error_message)

    def test_expired_task_resumes_at_the_first_uncommitted_row(self):
        workbook = self._workbook_bytes(
            [
                ["第一条", "业务沉淀", "自营回收", "cat-qc", "正文一"],
                ["第二条", "业务沉淀", "自营回收", "cat-qc", "正文二"],
            ]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-resume",
                    workbook,
                    status="running",
                    total_rows=2,
                    processed_rows=1,
                    imported=1,
                    results=[
                        {
                            "row": 2,
                            "title": "第一条",
                            "status": "imported",
                            "knowledge_id": "A-00001",
                        }
                    ],
                    lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
                )
            )
            db.commit()

        with patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=lambda _db, row, _user, **_kwargs: ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id="A-00002",
            ),
        ) as process_row:
            self.assertTrue(
                process_next_knowledge_import_task(
                    session_factory=self.session_factory,
                )
            )

        self.assertEqual(process_row.call_count, 1)
        self.assertEqual(process_row.call_args.args[1].row_number, 3)
        # A failed precomputation must fall back to the legacy three-argument
        # row path so its original error handling remains authoritative.
        self.assertEqual(process_row.call_args.kwargs, {})
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.processed_rows, 2)
            self.assertEqual(task.imported, 2)
            self.assertEqual([item["row"] for item in task.results], [2, 3])

    def test_reclaimed_attempt_stops_the_old_worker_before_row_write(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-claim",
                    workbook,
                    status="running",
                    total_rows=1,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        def steal_claim(rows, *, on_batch_complete):
            with self.session_factory() as other_db:
                task = other_db.query(KnowledgeImportTask).filter(
                    KnowledgeImportTask.id == "import-claim"
                ).one()
                task.attempt_count = 2
                other_db.commit()
            on_batch_complete(1, 1)
            return {}

        with patch(
            "app.routes.knowledge._precompute_import_embeddings",
            side_effect=steal_claim,
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
        ) as process_row, patch(
            "app.routes.knowledge.logger.exception",
        ):
            process_knowledge_import_task(
                "import-claim",
                session_factory=self.session_factory,
            )

        process_row.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.attempt_count, 2)
            self.assertEqual(task.status, "running")
            self.assertEqual(task.processed_rows, 0)

    def test_owner_can_cancel_queued_task(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                self._task(
                    "import-cancel",
                    workbook,
                    error_message="后台处理暂时失败，将自动重试",
                )
            )
            db.commit()

            response = cancel_knowledge_import_task(
                "import-cancel",
                db=db,
                current_user=SimpleNamespace(
                    username="tester",
                    role="user",
                ),
            )

            self.assertEqual(response.status, "cancelled")
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "cancelled")
            self.assertEqual(task.attempt_count, 1)
            self.assertIsNone(task.lease_expires_at)
            self.assertIsNotNone(task.completed_at)
            self.assertEqual(task.error_message, "")
            repeated = cancel_knowledge_import_task(
                "import-cancel",
                db=db,
                current_user=SimpleNamespace(
                    username="tester",
                    role="user",
                ),
            )
            self.assertEqual(repeated.status, "cancelled")
            self.assertEqual(repeated.attempt_count, 1)

    def test_only_owner_or_super_admin_can_cancel_task(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(self._task("import-cancel-auth", workbook))
            db.commit()

            with self.assertRaises(HTTPException) as caught:
                cancel_knowledge_import_task(
                    "import-cancel-auth",
                    db=db,
                    current_user=SimpleNamespace(
                        username="other",
                        role="user",
                    ),
                )
            self.assertEqual(caught.exception.status_code, 403)
            db.rollback()

            response = cancel_knowledge_import_task(
                "import-cancel-auth",
                db=db,
                current_user=SimpleNamespace(
                    username="admin",
                    role="super_admin",
                ),
            )
            self.assertEqual(response.status, "cancelled")

    def test_running_task_cancel_stops_before_the_next_row(self):
        workbook = self._workbook_bytes(
            [
                ["第一条", "业务沉淀", "自营回收", "cat-qc", "正文一"],
                ["第二条", "业务沉淀", "自营回收", "cat-qc", "正文二"],
            ]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-cancel-running",
                    workbook,
                    status="running",
                    total_rows=2,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        lock_count = 0

        def cancel_before_second_row(db, task_id, claimed_attempt):
            nonlocal lock_count
            lock_count += 1
            task = _lock_import_task_attempt(db, task_id, claimed_attempt)
            if lock_count == 5:
                now = datetime.utcnow()
                task.status = "cancelled"
                task.lease_expires_at = None
                task.completed_at = now
                task.updated_at = now
                db.commit()
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
            return task

        with patch(
            "app.routes.knowledge._lock_import_task_attempt",
            side_effect=cancel_before_second_row,
        ), patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=lambda _db, row, _user, **_kwargs: ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id=f"A-{row.row_number:05d}",
            ),
        ) as process_row:
            process_knowledge_import_task(
                "import-cancel-running",
                session_factory=self.session_factory,
            )

        self.assertEqual(process_row.call_count, 1)
        self.assertEqual(process_row.call_args.args[1].row_number, 2)
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "cancelled")
            self.assertEqual(task.processed_rows, 1)
            self.assertEqual([item["row"] for item in task.results], [2])

    def test_cancel_at_final_transition_is_not_overwritten_by_completed(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-cancel-final",
                    workbook,
                    status="running",
                    total_rows=1,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        lock_count = 0

        def cancel_at_final_lock(db, task_id, claimed_attempt):
            nonlocal lock_count
            lock_count += 1
            task = _lock_import_task_attempt(db, task_id, claimed_attempt)
            if lock_count == 5:
                now = datetime.utcnow()
                task.status = "cancelled"
                task.lease_expires_at = None
                task.completed_at = now
                task.updated_at = now
                db.commit()
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
            return task

        with patch(
            "app.routes.knowledge._lock_import_task_attempt",
            side_effect=cancel_at_final_lock,
        ), patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=lambda _db, row, _user, **_kwargs: ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id="A-00001",
            ),
        ):
            process_knowledge_import_task(
                "import-cancel-final",
                session_factory=self.session_factory,
            )

        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "cancelled")
            self.assertEqual(task.processed_rows, 1)

    def test_cancel_during_transient_failure_is_not_requeued(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-cancel-retry",
                    workbook,
                    status="running",
                    total_rows=1,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        lock_count = 0

        def cancel_before_retry_state(db, task_id, claimed_attempt):
            nonlocal lock_count
            lock_count += 1
            task = _lock_import_task_attempt(db, task_id, claimed_attempt)
            if lock_count == 5:
                now = datetime.utcnow()
                task.status = "cancelled"
                task.lease_expires_at = None
                task.completed_at = now
                task.updated_at = now
                db.commit()
                task = _lock_import_task_attempt(
                    db,
                    task_id,
                    claimed_attempt,
                )
            return task

        with patch(
            "app.routes.knowledge._lock_import_task_attempt",
            side_effect=cancel_before_retry_state,
        ), patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=EmbeddingServiceUnavailable("model unavailable"),
        ), patch(
            "app.routes.knowledge.logger.error",
        ):
            process_knowledge_import_task(
                "import-cancel-retry",
                session_factory=self.session_factory,
            )

        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "cancelled")
            self.assertEqual(task.processed_rows, 0)
            self.assertEqual(task.results, [])

    def test_retry_failed_selects_only_non_contiguous_retry_rows(self):
        workbook = self._workbook_bytes(
            [
                [
                    f"第{row_number}条",
                    "业务沉淀",
                    "自营回收",
                    "cat-qc",
                    f"正文{row_number}",
                ]
                for row_number in range(2, 45)
            ]
        )
        successful_results = [
            {
                "row": row_number,
                "title": f"第{row_number}条",
                "status": "imported",
                "knowledge_id": f"A-{row_number:05d}",
            }
            for row_number in range(2, 45)
            if row_number not in {14, 44}
        ]
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-retry-rows",
                    workbook,
                    status="running",
                    total_rows=43,
                    processed_rows=41,
                    imported=41,
                    retry_rows=[14, 44],
                    results=successful_results,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        with patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=lambda _db, row, _user, **_kwargs: ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id=f"A-{row.row_number:05d}",
            ),
        ) as process_row:
            process_knowledge_import_task(
                "import-retry-rows",
                session_factory=self.session_factory,
            )

        self.assertEqual(
            [call.args[1].row_number for call in process_row.call_args_list],
            [14, 44],
        )
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.processed_rows, 43)
            self.assertEqual(task.imported, 43)
            self.assertEqual(task.retry_rows, [])
            self.assertEqual(len(task.results), 43)

    def test_transient_failure_after_one_retry_row_resumes_only_failed_row(self):
        workbook = self._workbook_bytes(
            [
                [
                    f"第{row_number}条",
                    "业务沉淀",
                    "自营回收",
                    "cat-qc",
                    f"正文{row_number}",
                ]
                for row_number in range(2, 45)
            ]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-retry-resume",
                    workbook,
                    status="running",
                    total_rows=43,
                    processed_rows=41,
                    imported=41,
                    retry_rows=[14, 44],
                    results=[
                        {
                            "row": row_number,
                            "title": f"第{row_number}条",
                            "status": "imported",
                            "knowledge_id": f"A-{row_number:05d}",
                        }
                        for row_number in range(2, 45)
                        if row_number not in {14, 44}
                    ],
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        def fail_second_retry_row(_db, row, _user, **_kwargs):
            if row.row_number == 44:
                raise HTTPException(
                    status_code=503,
                    detail="Embedding 服务不可用",
                )
            return ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id=f"A-{row.row_number:05d}",
            )

        with patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=fail_second_retry_row,
        ), patch(
            "app.routes.knowledge.logger.warning",
        ):
            process_knowledge_import_task(
                "import-retry-resume",
                session_factory=self.session_factory,
            )

        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "queued")
            self.assertEqual(task.processed_rows, 42)
            self.assertEqual(task.imported, 42)
            self.assertEqual(task.failed, 0)
            self.assertEqual(task.retry_rows, [44])
            self.assertEqual(len(task.results), 42)
            task.next_attempt_at = datetime.utcnow() - timedelta(seconds=1)
            db.commit()

        with patch(
            "app.routes.knowledge._precompute_import_embeddings",
            return_value={},
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=lambda _db, row, _user, **_kwargs: ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id=f"A-{row.row_number:05d}",
            ),
        ) as process_row:
            self.assertTrue(
                process_next_knowledge_import_task(
                    session_factory=self.session_factory,
                )
            )

        self.assertEqual(process_row.call_count, 1)
        self.assertEqual(process_row.call_args.args[1].row_number, 44)
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.processed_rows, 43)
            self.assertEqual(task.imported, 43)
            self.assertEqual(task.retry_rows, [])

    def test_embedding_precompute_failure_requeues_without_advancing_progress(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-precompute-retry",
                    workbook,
                    status="running",
                    total_rows=1,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        started_at = datetime.utcnow()
        with patch(
            "app.routes.knowledge.embed_texts",
            side_effect=EmbeddingServiceUnavailable(
                "model unavailable",
                retryable=True,
            ),
        ), patch(
            "app.routes.knowledge._process_excel_import_row",
        ) as process_row, patch(
            "app.routes.knowledge.logger.warning",
        ):
            process_knowledge_import_task(
                "import-precompute-retry",
                session_factory=self.session_factory,
            )

        process_row.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "queued")
            self.assertEqual(task.processed_rows, 0)
            self.assertEqual(task.failed, 0)
            self.assertEqual(task.results, [])
            self.assertGreater(task.next_attempt_at, started_at)

    def test_permanent_embedding_rejection_only_fails_that_row(self):
        workbook = self._workbook_bytes(
            [
                ["永久拒绝", "业务沉淀", "自营回收", "cat-qc", "超长正文"],
                ["正常知识", "业务沉淀", "自营回收", "cat-qc", "正常正文"],
            ]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-permanent-rejection",
                    workbook,
                    status="running",
                    total_rows=2,
                    attempt_count=1,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        embedding_call_count = 0

        def isolate_permanent_rejection(texts, **_kwargs):
            nonlocal embedding_call_count
            embedding_call_count += 1
            if embedding_call_count <= 2:
                raise EmbeddingServiceUnavailable(
                    "input is too long",
                    retryable=False,
                )
            return [
                [float(index)]
                for index, _ in enumerate(texts)
            ]

        def process_precomputed_row(_db, row, _user, **kwargs):
            bundle = kwargs.get("embedding_bundle")
            if bundle is not None and bundle.error is not None:
                raise bundle.error
            return ExcelImportRowResult(
                row=row.row_number,
                title=row.title,
                status="imported",
                knowledge_id=f"A-{row.row_number:05d}",
            )

        with patch(
            "app.routes.knowledge.embed_texts",
            side_effect=isolate_permanent_rejection,
        ) as embed, patch(
            "app.routes.knowledge._process_excel_import_row",
            side_effect=process_precomputed_row,
        ) as process_row:
            process_knowledge_import_task(
                "import-permanent-rejection",
                session_factory=self.session_factory,
            )

        self.assertEqual(embed.call_count, 3)
        self.assertEqual(process_row.call_count, 2)
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed_with_errors")
            self.assertEqual(task.processed_rows, 2)
            self.assertEqual(task.imported, 1)
            self.assertEqual(task.failed, 1)
            self.assertEqual(
                [result["status"] for result in task.results],
                ["failed", "imported"],
            )
            self.assertEqual(
                task.results[0]["error_code"],
                "EMBEDDING_REJECTED",
            )
            self.assertIn(
                "input is too long",
                task.results[0]["error_message"],
            )

    def test_retry_backoff_is_exponential_and_capped(self):
        with patch.object(
            settings,
            "KNOWLEDGE_IMPORT_RETRY_BASE_SECONDS",
            5,
        ), patch.object(
            settings,
            "KNOWLEDGE_IMPORT_RETRY_MAX_SECONDS",
            30,
        ):
            self.assertEqual(_import_retry_delay_seconds(1), 5)
            self.assertEqual(_import_retry_delay_seconds(2), 10)
            self.assertEqual(_import_retry_delay_seconds(3), 20)
            self.assertEqual(_import_retry_delay_seconds(4), 30)
            self.assertEqual(_import_retry_delay_seconds(10), 30)

    def test_max_attempts_marks_transient_failure_as_failed(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-max-attempts",
                    workbook,
                    status="running",
                    total_rows=1,
                    attempt_count=2,
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=1),
                )
            )
            db.commit()

        with patch.object(
            settings,
            "KNOWLEDGE_IMPORT_MAX_ATTEMPTS",
            2,
        ), patch(
            "app.routes.knowledge._precompute_import_embeddings",
            side_effect=EmbeddingServiceUnavailable("model unavailable"),
        ), patch(
            "app.routes.knowledge.logger.error",
        ) as log_error, patch(
            "app.routes.knowledge.logger.warning",
        ) as log_warning:
            process_knowledge_import_task(
                "import-max-attempts",
                session_factory=self.session_factory,
            )

        log_warning.assert_not_called()
        log_error.assert_called_once()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "failed")
            self.assertIsNotNone(task.completed_at)
            self.assertIn("最大尝试次数", task.error_message)

    def test_expired_task_at_max_attempts_is_not_reclaimed(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                self._task(
                    "import-expired-max",
                    workbook,
                    status="running",
                    attempt_count=2,
                    lease_expires_at=datetime.utcnow() - timedelta(seconds=1),
                )
            )
            db.commit()

        with patch.object(
            settings,
            "KNOWLEDGE_IMPORT_MAX_ATTEMPTS",
            2,
        ), patch(
            "app.routes.knowledge.process_knowledge_import_task",
        ) as process_task:
            self.assertTrue(
                process_next_knowledge_import_task(
                    session_factory=self.session_factory,
                )
            )

        process_task.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.attempt_count, 2)
            self.assertIsNotNone(task.completed_at)
            self.assertIn("最大尝试次数", task.error_message)

    def test_retry_failed_endpoint_queues_only_retryable_failures(self):
        workbook = self._workbook_bytes(
            [
                [
                    f"第{row_number}条",
                    "业务沉淀",
                    "自营回收",
                    "cat-qc",
                    f"正文{row_number}",
                ]
                for row_number in range(2, 45)
            ]
        )
        successful_results = [
            {
                "row": row_number,
                "title": f"第{row_number}条",
                "status": "imported",
                "knowledge_id": f"A-{row_number:05d}",
            }
            for row_number in range(2, 45)
            if row_number not in {14, 44}
        ]
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-retry-endpoint",
                    workbook,
                    status="completed_with_errors",
                    total_rows=43,
                    processed_rows=43,
                    imported=41,
                    failed=2,
                    results=[
                        *successful_results,
                        {
                            "row": 14,
                            "title": "失败一",
                            "status": "failed",
                            "error_code": "EMBEDDING_UNAVAILABLE",
                            "error_message": "Embedding 服务不可用",
                        },
                        {
                            "row": 44,
                            "title": "失败二",
                            "status": "failed",
                            "error_code": "DEDUP_UNAVAILABLE",
                            "error_message": "查重服务不可用",
                        },
                    ],
                )
            )
            db.commit()

            response = retry_failed_knowledge_import_task(
                "import-retry-endpoint",
                db=db,
                current_user=SimpleNamespace(
                    username="tester",
                    role="user",
                ),
            )

            self.assertEqual(response.status, "queued")
            self.assertEqual(response.retry_rows, [14, 44])
            self.assertEqual(response.processed_rows, 41)
            self.assertEqual(response.imported, 41)
            self.assertEqual(response.failed, 0)
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(
                {item["row"] for item in task.results},
                set(range(2, 45)) - {14, 44},
            )

    def test_retry_failed_endpoint_rejects_business_failures(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-retry-rejected",
                    workbook,
                    status="completed_with_errors",
                    total_rows=1,
                    processed_rows=1,
                    failed=1,
                    results=[
                        {
                            "row": 2,
                            "title": "无效数据",
                            "status": "failed",
                            "error_code": "INVALID_ROW",
                            "error_message": "适用类目必填",
                        }
                    ],
                )
            )
            db.commit()

            with self.assertRaises(HTTPException) as caught:
                retry_failed_knowledge_import_task(
                    "import-retry-rejected",
                    db=db,
                    current_user=SimpleNamespace(
                        username="tester",
                        role="user",
                    ),
                )
            self.assertEqual(caught.exception.status_code, 409)
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed_with_errors")
            self.assertEqual(task.failed, 1)

    def test_retry_failed_endpoint_rejects_inconsistent_task_state(self):
        workbook = self._workbook_bytes(
            [["唯一条", "业务沉淀", "自营回收", "cat-qc", "正文"]]
        )
        with self.session_factory() as db:
            db.add(
                Category(
                    id="cat-qc",
                    name="质检标准",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.add(
                self._task(
                    "import-retry-inconsistent",
                    workbook,
                    status="completed_with_errors",
                    total_rows=1,
                    processed_rows=2,
                    failed=1,
                    results=[
                        {
                            "row": 2,
                            "title": "基础设施失败",
                            "status": "failed",
                            "error_code": "EMBEDDING_UNAVAILABLE",
                            "error_message": "Embedding 服务不可用",
                        }
                    ],
                )
            )
            db.commit()

            with self.assertRaises(HTTPException) as caught:
                retry_failed_knowledge_import_task(
                    "import-retry-inconsistent",
                    db=db,
                    current_user=SimpleNamespace(
                        username="tester",
                        role="user",
                    ),
                )
            self.assertEqual(caught.exception.status_code, 409)
            self.assertIn("进度", caught.exception.detail)


if __name__ == "__main__":
    unittest.main()
