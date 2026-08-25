import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260818_02_add_model_configuration_excel_import.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260818_02",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class ModelConfigurationExcelImportMigrationTests(unittest.TestCase):
    def test_revision_extends_model_configuration_origin_migration(self):
        self.assertEqual(MIGRATION.revision, "20260818_02")
        self.assertEqual(MIGRATION.down_revision, "20260818_01")

    def test_upgrade_adds_import_type_counts_constraint_and_index(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.upgrade()

        added_columns = [
            call.args[1].name
            for call in operation.add_column.call_args_list
        ]
        self.assertEqual(
            added_columns,
            ["import_type", "created", "updated", "unchanged"],
        )
        operation.create_check_constraint.assert_called_once_with(
            "ck_knowledge_import_task_import_type",
            "knowledge_import_tasks",
            "import_type IN ('knowledge', 'model_configuration')",
        )
        operation.create_index.assert_called_once_with(
            "ix_knowledge_import_tasks_import_type",
            "knowledge_import_tasks",
            ["import_type"],
            unique=False,
        )

    def test_downgrade_removes_new_index_constraint_and_columns(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.downgrade()

        operation.drop_index.assert_called_once_with(
            "ix_knowledge_import_tasks_import_type",
            table_name="knowledge_import_tasks",
        )
        operation.drop_constraint.assert_called_once_with(
            "ck_knowledge_import_task_import_type",
            "knowledge_import_tasks",
            type_="check",
        )
        self.assertEqual(
            [
                call.args[1]
                for call in operation.drop_column.call_args_list
            ],
            ["unchanged", "updated", "created", "import_type"],
        )


if __name__ == "__main__":
    unittest.main()
