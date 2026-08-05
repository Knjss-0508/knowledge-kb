import unittest
from datetime import datetime, timedelta
from io import BytesIO
from unittest.mock import patch

from openpyxl import Workbook
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.knowledge import Category, KnowledgeImportTask
from app.routes.knowledge import (
    _import_embedding_batches,
    _precompute_import_embeddings,
    process_next_knowledge_import_task,
)
from app.schemas.knowledge import ExcelImportRowResult
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
        sheet.append(["标题", "业务类型", "知识分类", "正文"])
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
            "file_sha256": "a" * 64,
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
                ["第一条", "自营回收", "cat-qc", "正文一"],
                ["第二条", "自营回收", "cat-qc", "正文二"],
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
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.processed_rows, 2)
            self.assertEqual(task.imported, 2)
            self.assertEqual([item["row"] for item in task.results], [2, 3])


if __name__ == "__main__":
    unittest.main()
