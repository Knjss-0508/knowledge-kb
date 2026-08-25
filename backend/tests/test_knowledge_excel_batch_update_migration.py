import importlib.util
import unittest
from pathlib import Path
from unittest.mock import call, patch


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260820_01_add_knowledge_excel_batch_update.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260820_01",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class KnowledgeExcelBatchUpdateMigrationTests(unittest.TestCase):
    def test_revision_extends_model_configuration_excel_import(self):
        self.assertEqual(MIGRATION.revision, "20260820_01")
        self.assertEqual(MIGRATION.down_revision, "20260818_02")

    def test_upgrade_replaces_import_type_constraint(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.upgrade()

        self.assertEqual(
            operation.method_calls,
            [
                call.drop_constraint(
                    "ck_knowledge_import_task_import_type",
                    "knowledge_import_tasks",
                    type_="check",
                ),
                call.create_check_constraint(
                    "ck_knowledge_import_task_import_type",
                    "knowledge_import_tasks",
                    "import_type IN "
                    "('knowledge', 'knowledge_update', "
                    "'model_configuration')",
                ),
            ],
        )

    def test_downgrade_restores_previous_constraint(self):
        with patch.object(MIGRATION, "op") as operation:
            operation.get_bind.return_value.execute.return_value.scalar_one.return_value = 0
            MIGRATION.downgrade()

        operation.drop_constraint.assert_called_once_with(
            "ck_knowledge_import_task_import_type",
            "knowledge_import_tasks",
            type_="check",
        )
        operation.create_check_constraint.assert_called_once_with(
            "ck_knowledge_import_task_import_type",
            "knowledge_import_tasks",
            "import_type IN ('knowledge', 'model_configuration')",
        )

    def test_downgrade_is_blocked_while_update_task_history_exists(self):
        with patch.object(MIGRATION, "op") as operation:
            operation.get_bind.return_value.execute.return_value.scalar_one.return_value = 1
            with self.assertRaisesRegex(
                RuntimeError,
                "knowledge_update import tasks exist",
            ):
                MIGRATION.downgrade()

        operation.drop_constraint.assert_not_called()
        operation.create_check_constraint.assert_not_called()


if __name__ == "__main__":
    unittest.main()
