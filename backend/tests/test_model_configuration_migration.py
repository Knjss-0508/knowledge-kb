import importlib.util
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260818_01_add_model_configuration_origin.py"
)
SCHEMA_PATHS = (
    Path(__file__).resolve().parents[2]
    / "database"
    / "knowledge-kb-schema.sql",
    Path(__file__).resolve().parents[2]
    / "database"
    / "knowledge-kb-schema-console.sql",
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260818_01",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class ModelConfigurationMigrationTests(unittest.TestCase):
    def test_revision_extends_current_head(self):
        self.assertEqual(MIGRATION.revision, "20260818_01")
        self.assertEqual(MIGRATION.down_revision, "20260810_01")

    def test_upgrade_rebuilds_constraint_and_adds_partial_unique_indexes(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.upgrade()

        operation.drop_constraint.assert_called_once_with(
            "ck_knowledge_items_knowledge_origin",
            "knowledge_items",
            type_="check",
        )
        constraint_expression = (
            operation.create_check_constraint.call_args.args[2]
        )
        self.assertIn("model_configuration", constraint_expression)
        index_names = [
            call.args[0]
            for call in operation.create_index.call_args_list
        ]
        self.assertEqual(
            index_names,
            [
                "uq_knowledge_items_model_configuration_source_record_id",
                "uq_knowledge_items_model_configuration_source_knowledge_key",
            ],
        )

    def test_downgrade_refuses_to_orphan_managed_rows(self):
        bind = MagicMock()
        bind.execute.return_value.scalar_one.return_value = 1
        with patch.object(MIGRATION.op, "get_bind", return_value=bind):
            with self.assertRaisesRegex(
                RuntimeError,
                "model_configuration knowledge exists",
            ):
                MIGRATION.downgrade()

    def test_schema_snapshots_include_managed_origin_and_current_unique_key(self):
        for schema_path in SCHEMA_PATHS:
            with self.subTest(schema=schema_path.name):
                schema = schema_path.read_text(encoding="utf-8")
                self.assertIn(
                    "'model_configuration'::character varying",
                    schema,
                )
                self.assertNotIn(
                    "CREATE UNIQUE INDEX "
                    "uq_knowledge_items_model_configuration_source_record_id",
                    schema,
                )
                self.assertIn(
                    "CREATE UNIQUE INDEX "
                    "uq_knowledge_items_model_configuration_"
                    "source_knowledge_key",
                    schema,
                )


if __name__ == "__main__":
    unittest.main()
