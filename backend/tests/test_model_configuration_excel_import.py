import hashlib
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models.knowledge import Category, KnowledgeImportTask
from app.routes.knowledge import (
    _to_import_task_response,
    process_knowledge_import_task,
)
from app.services.knowledge_excel import (
    KnowledgeExcelError,
    ParsedModelConfigurationWorkbook,
)
from app.services.model_configuration import (
    ModelConfigurationSyncError,
    ModelConfigurationSyncItemResult,
    ModelConfigurationSyncResult,
    parse_model_configuration_payload,
)


class ModelConfigurationExcelImportTaskTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite+pysqlite:///:memory:")
        KnowledgeImportTask.__table__.create(self.engine)
        Category.__table__.create(self.engine)
        self.session_factory = sessionmaker(bind=self.engine)

    def tearDown(self):
        self.engine.dispose()

    @staticmethod
    def _record(index: int = 1):
        return parse_model_configuration_payload(
            {
                "records": [
                    {
                        "source_record_id": str(56000 + index),
                        "title": f"测试机型 {index}",
                        "brand_id": "10530",
                        "brand_name": "苹果",
                        "model_id": str(97000 + index),
                        "model_name": f"测试机型 {index}",
                        "content": f"配置正文 {index}",
                    }
                ]
            }
        )[0]

    def _add_task(self, task_id: str = "import-model") -> None:
        content = b"model-configuration-xlsx"
        with self.session_factory() as db:
            db.add(
                KnowledgeImportTask(
                    id=task_id,
                    import_type="model_configuration",
                    created_by="tester",
                    original_filename="model-configuration.xlsx",
                    file_size=len(content),
                    file_sha256=hashlib.sha256(content).hexdigest(),
                    file_content=content,
                    status="running",
                    attempt_count=1,
                    next_attempt_at=datetime.utcnow() - timedelta(seconds=1),
                    lease_expires_at=datetime.utcnow() + timedelta(minutes=5),
                )
            )
            db.commit()

    def test_legacy_unflushed_task_response_defaults_new_fields(self):
        now = datetime.utcnow()
        task = KnowledgeImportTask(
            id="legacy-task",
            created_by="tester",
            original_filename="legacy.xlsx",
            file_size=1,
            file_sha256="0" * 64,
            file_content=b"x",
            status="queued",
            total_rows=0,
            processed_rows=0,
            imported=0,
            review_required=0,
            pending_review=0,
            deprecated=0,
            failed=0,
            retry_rows=[],
            attempt_count=0,
            next_attempt_at=now,
            error_message="",
            created_at=now,
            results=[],
        )

        response = _to_import_task_response(task)

        self.assertEqual(response.import_type, "knowledge")
        self.assertEqual(response.created, 0)
        self.assertEqual(response.updated, 0)
        self.assertEqual(response.unchanged, 0)

    def test_task_response_can_return_more_than_500_model_results(self):
        now = datetime.utcnow()
        results = [
            {
                "row": row_number,
                "title": f"机型 {row_number}",
                "status": "imported",
                "knowledge_id": f"A-{row_number:05d}",
                "operation": "unchanged",
            }
            for row_number in range(2, 602)
        ]
        task = KnowledgeImportTask(
            id="large-model-task",
            import_type="model_configuration",
            created_by="tester",
            original_filename="large.xlsx",
            file_size=1,
            file_sha256="0" * 64,
            file_content=b"x",
            status="completed",
            total_rows=600,
            processed_rows=600,
            imported=600,
            review_required=0,
            pending_review=0,
            deprecated=0,
            failed=0,
            created=0,
            updated=0,
            unchanged=600,
            retry_rows=[],
            attempt_count=1,
            next_attempt_at=now,
            error_message="",
            created_at=now,
            completed_at=now,
            results=results,
        )

        response = _to_import_task_response(
            task,
            include_results=True,
            result_limit=5000,
        )

        self.assertEqual(len(response.results), 600)
        self.assertEqual(response.results[-1].row, 601)

    def test_model_configuration_task_commits_one_atomic_sync_with_operations(self):
        self._add_task()
        records = [self._record(1), self._record(2), self._record(3)]
        parsed = ParsedModelConfigurationWorkbook(
            records=records,
            row_numbers=[2, 3, 4],
        )
        sync_result = ModelConfigurationSyncResult(
            total=3,
            created=1,
            updated=1,
            unchanged=1,
            items=(
                ModelConfigurationSyncItemResult(
                    source_record_id=records[0].source_record_id,
                    knowledge_id="A-00001",
                    operation="created",
                ),
                ModelConfigurationSyncItemResult(
                    source_record_id=records[1].source_record_id,
                    knowledge_id="A-00002",
                    operation="updated",
                ),
                ModelConfigurationSyncItemResult(
                    source_record_id=records[2].source_record_id,
                    knowledge_id="A-00003",
                    operation="unchanged",
                ),
            ),
        )

        with (
            patch(
                "app.routes.knowledge.parse_model_configuration_workbook",
                return_value=parsed,
            ) as parse_workbook,
            patch(
                "app.routes.knowledge.sync_model_configurations",
                return_value=sync_result,
            ) as sync,
            patch(
                "app.routes.knowledge._canonicalize_excel_rows_applicability",
            ) as canonicalize,
            patch(
                "app.routes.knowledge._precompute_import_embeddings",
            ) as precompute,
            patch(
                "app.routes.knowledge._create_knowledge_item",
            ) as create_knowledge,
            patch(
                "app.routes.knowledge.ensure_embedding",
            ) as ensure_embedding,
            patch(
                "app.routes.knowledge.ensure_search_embeddings",
            ) as ensure_search_embeddings,
            patch(
                "app.routes.knowledge.save_embedding",
            ) as save_embedding,
        ):
            process_knowledge_import_task(
                "import-model",
                session_factory=self.session_factory,
            )

        parse_workbook.assert_called_once_with(b"model-configuration-xlsx")
        sync.assert_called_once()
        self.assertEqual(sync.call_args.kwargs["actor"], "tester")
        self.assertEqual(sync.call_args.args[1], records)
        canonicalize.assert_not_called()
        precompute.assert_not_called()
        create_knowledge.assert_not_called()
        ensure_embedding.assert_not_called()
        ensure_search_embeddings.assert_not_called()
        save_embedding.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "completed")
            self.assertEqual(task.import_type, "model_configuration")
            self.assertEqual(task.total_rows, 3)
            self.assertEqual(task.processed_rows, 3)
            self.assertEqual(task.imported, 3)
            self.assertEqual(task.created, 1)
            self.assertEqual(task.updated, 1)
            self.assertEqual(task.unchanged, 1)
            self.assertEqual(task.review_required, 0)
            self.assertEqual(task.pending_review, 0)
            self.assertEqual(task.failed, 0)
            self.assertEqual(
                [result["operation"] for result in task.results],
                ["created", "updated", "unchanged"],
            )
            self.assertEqual(
                [result["knowledge_id"] for result in task.results],
                ["A-00001", "A-00002", "A-00003"],
            )

    def test_model_configuration_sync_failure_rolls_back_the_entire_batch(self):
        self._add_task("import-model-rollback")
        records = [self._record(1)]
        parsed = ParsedModelConfigurationWorkbook(
            records=records,
            row_numbers=[2],
        )

        def fail_after_database_write(db, _records, *, actor):
            self.assertEqual(actor, "tester")
            db.add(
                Category(
                    id="must-rollback",
                    name="必须回滚",
                    parent_id=None,
                    level=1,
                    sort_order=1,
                )
            )
            db.flush()
            raise ModelConfigurationSyncError(
                "SOURCE_IDENTIFIER_CONFLICT",
                "来源知识ID与机型键冲突。",
                source_record_id=records[0].source_record_id,
            )

        with (
            patch(
                "app.routes.knowledge.parse_model_configuration_workbook",
                return_value=parsed,
            ),
            patch(
                "app.routes.knowledge.sync_model_configurations",
                side_effect=fail_after_database_write,
            ),
            patch(
                "app.routes.knowledge._precompute_import_embeddings",
            ) as precompute,
        ):
            process_knowledge_import_task(
                "import-model-rollback",
                session_factory=self.session_factory,
            )

        precompute.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.total_rows, 1)
            self.assertEqual(task.processed_rows, 1)
            self.assertEqual(task.imported, 0)
            self.assertEqual(task.created, 0)
            self.assertEqual(task.updated, 0)
            self.assertEqual(task.unchanged, 0)
            self.assertEqual(task.failed, 1)
            self.assertEqual(task.results, [])
            self.assertIn("全部数据已回滚", task.error_message)
            self.assertIn("Excel 第 2 行", task.error_message)
            self.assertIn("SOURCE_IDENTIFIER_CONFLICT", task.error_message)
            self.assertIsNone(
                db.query(Category).filter(
                    Category.id == "must-rollback"
                ).first()
            )

    def test_invalid_model_configuration_workbook_fails_before_sync(self):
        self._add_task("import-model-invalid")

        with (
            patch(
                "app.routes.knowledge.parse_model_configuration_workbook",
                side_effect=KnowledgeExcelError(
                    "机型配置信息第 2 行：品牌ID不能为空。"
                ),
            ),
            patch(
                "app.routes.knowledge.sync_model_configurations",
            ) as sync,
        ):
            process_knowledge_import_task(
                "import-model-invalid",
                session_factory=self.session_factory,
            )

        sync.assert_not_called()
        with self.session_factory() as db:
            task = db.query(KnowledgeImportTask).one()
            self.assertEqual(task.status, "failed")
            self.assertEqual(task.total_rows, 0)
            self.assertEqual(task.processed_rows, 0)
            self.assertEqual(task.created, 0)
            self.assertIn("第 2 行", task.error_message)


if __name__ == "__main__":
    unittest.main()
