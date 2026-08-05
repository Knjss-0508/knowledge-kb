import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260805_01_add_knowledge_origin.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260805_01",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


def _bind_with_counts(knowledge_count: int, candidate_count: int):
    bind = MagicMock()
    knowledge_result = MagicMock()
    knowledge_result.scalar_one.return_value = knowledge_count
    candidate_result = MagicMock()
    candidate_result.scalar_one.return_value = candidate_count
    bind.execute.side_effect = [knowledge_result, candidate_result]
    return bind


class KnowledgeOriginMigrationTests(unittest.TestCase):
    def test_non_empty_legacy_database_requires_explicit_origin(self):
        with patch.dict(
            os.environ,
            {"KNOWLEDGE_ORIGIN_BACKFILL": ""},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "requires an explicit KNOWLEDGE_ORIGIN_BACKFILL",
            ):
                MIGRATION._resolve_legacy_knowledge_origin(
                    _bind_with_counts(159, 0)
                )

    def test_legacy_candidate_also_requires_explicit_origin(self):
        with patch.dict(
            os.environ,
            {"KNOWLEDGE_ORIGIN_BACKFILL": ""},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "requires an explicit KNOWLEDGE_ORIGIN_BACKFILL",
            ):
                MIGRATION._resolve_legacy_knowledge_origin(
                    _bind_with_counts(0, 1)
                )

    def test_configured_headquarters_origin_is_used_for_legacy_rows(self):
        with patch.dict(
            os.environ,
            {
                "KNOWLEDGE_ORIGIN_BACKFILL": (
                    "headquarters_standard"
                )
            },
        ):
            origin = MIGRATION._resolve_legacy_knowledge_origin(
                _bind_with_counts(159, 0)
            )

        self.assertEqual(origin, "headquarters_standard")

    def test_empty_database_needs_no_backfill_configuration(self):
        with patch.dict(
            os.environ,
            {"KNOWLEDGE_ORIGIN_BACKFILL": ""},
        ):
            origin = MIGRATION._resolve_legacy_knowledge_origin(
                _bind_with_counts(0, 0)
            )

        self.assertEqual(origin, "business_accumulation")

    def test_invalid_backfill_origin_is_rejected(self):
        with patch.dict(
            os.environ,
            {"KNOWLEDGE_ORIGIN_BACKFILL": "unknown"},
        ):
            with self.assertRaisesRegex(
                RuntimeError,
                "must be 'headquarters_standard'",
            ):
                MIGRATION._resolve_legacy_knowledge_origin(
                    _bind_with_counts(1, 0)
                )


if __name__ == "__main__":
    unittest.main()
