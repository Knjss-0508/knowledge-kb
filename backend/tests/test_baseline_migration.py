import importlib.util
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch


class _Result:
    def first(self):
        return ("existing-admin",)


class _LegacyBind:
    def __init__(self):
        self.columns = {
            "category_id",
            "created_by",
        }
        self.categories = set()
        self.null_category_count = 4

    def exec_driver_sql(self, sql, parameters=None):
        normalized = " ".join(sql.split())

        if "ADD COLUMN IF NOT EXISTS deduplication_metadata" in normalized:
            self.columns.add("deduplication_metadata")
        elif "ADD COLUMN IF NOT EXISTS updated_by" in normalized:
            self.columns.add("updated_by")
        elif "VALUES ('cat-pending-classification'" in normalized:
            self.categories.add("cat-pending-classification")
        elif normalized.startswith(
            "UPDATE knowledge_items SET category_id = 'cat-pending-classification'"
        ):
            assert "cat-pending-classification" in self.categories
            self.null_category_count = 0
        elif normalized.startswith("UPDATE knowledge_items SET updated_by"):
            assert "updated_by" in self.columns
        elif normalized == (
            "ALTER TABLE knowledge_items ALTER COLUMN category_id SET NOT NULL"
        ):
            assert self.null_category_count == 0

        return _Result()


def _load_baseline_migration():
    migration_path = (
        Path(__file__).parents[1]
        / "migrations"
        / "versions"
        / "20260712_01_baseline.py"
    )
    spec = importlib.util.spec_from_file_location("baseline_migration", migration_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BaselineMigrationTest(TestCase):
    def test_baseline_upgrades_legacy_knowledge_items(self):
        migration = _load_baseline_migration()
        bind = _LegacyBind()

        with (
            patch.object(migration.op, "get_bind", return_value=bind),
            patch.object(migration.Base.metadata, "create_all"),
        ):
            migration.upgrade()

        self.assertIn("deduplication_metadata", bind.columns)
        self.assertIn("updated_by", bind.columns)
        self.assertEqual(bind.null_category_count, 0)
