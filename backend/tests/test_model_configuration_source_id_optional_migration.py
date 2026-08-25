import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260825_01_make_model_configuration_source_id_optional.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260825_01",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class ModelConfigurationSourceIdOptionalMigrationTests(unittest.TestCase):
    def test_revision_extends_current_head(self):
        self.assertEqual(MIGRATION.revision, "20260825_01")
        self.assertEqual(MIGRATION.down_revision, "20260820_01")

    def test_upgrade_removes_source_record_id_unique_index(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.upgrade()

        operation.drop_index.assert_called_once_with(
            "uq_knowledge_items_model_configuration_source_record_id",
            table_name="knowledge_items",
        )

    def test_downgrade_restores_index_when_trace_ids_are_unique(self):
        bind = MagicMock()
        bind.execute.return_value.scalar_one.return_value = 0
        with patch.object(MIGRATION.op, "get_bind", return_value=bind), patch.object(
            MIGRATION.op,
            "create_index",
        ) as create_index:
            MIGRATION.downgrade()

        create_index.assert_called_once()
        self.assertEqual(
            create_index.call_args.args[:3],
            (
                "uq_knowledge_items_model_configuration_source_record_id",
                "knowledge_items",
                ["source_record_id"],
            ),
        )
        self.assertTrue(create_index.call_args.kwargs["unique"])
        self.assertIn(
            "source_record_id IS NOT NULL",
            str(create_index.call_args.kwargs["postgresql_where"]),
        )

    def test_downgrade_blocks_duplicate_trace_ids(self):
        bind = MagicMock()
        bind.execute.return_value.scalar_one.return_value = 1
        with patch.object(MIGRATION.op, "get_bind", return_value=bind), patch.object(
            MIGRATION.op,
            "create_index",
        ) as create_index:
            with self.assertRaisesRegex(RuntimeError, "duplicated trace IDs"):
                MIGRATION.downgrade()

        create_index.assert_not_called()


if __name__ == "__main__":
    unittest.main()
