import importlib.util
import unittest
from pathlib import Path
from unittest.mock import call, patch

import sqlalchemy as sa


MIGRATION_PATH = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260828_01_add_knowledge_vector_tasks.py"
)
SPEC = importlib.util.spec_from_file_location(
    "migration_20260828_01",
    MIGRATION_PATH,
)
assert SPEC and SPEC.loader
MIGRATION = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MIGRATION)


class KnowledgeVectorTasksMigrationTests(unittest.TestCase):
    def test_revision_extends_current_head(self):
        self.assertEqual(MIGRATION.revision, "20260828_01")
        self.assertEqual(MIGRATION.down_revision, "20260825_01")

    def test_upgrade_creates_durable_queue_and_indexes(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.upgrade()

        create_table = operation.create_table.call_args
        self.assertEqual(create_table.args[0], "knowledge_vector_tasks")
        columns = {
            column.name: column
            for column in create_table.args[1:]
            if isinstance(column, sa.Column)
        }
        self.assertEqual(
            set(columns),
            {
                "id",
                "knowledge_id",
                "task_type",
                "content_hash",
                "status",
                "attempt_count",
                "next_attempt_at",
                "lease_expires_at",
                "started_at",
                "completed_at",
                "error_message",
                "created_at",
                "updated_at",
            },
        )
        constraint_names = {
            item.name
            for item in create_table.args[1:]
            if isinstance(item, sa.CheckConstraint)
        }
        self.assertEqual(
            constraint_names,
            {
                "ck_knowledge_vector_task_type",
                "ck_knowledge_vector_task_status",
            },
        )
        index_names = [item.args[0] for item in operation.create_index.call_args_list]
        self.assertEqual(
            index_names,
            [
                "ix_knowledge_vector_tasks_knowledge_id",
                "ix_knowledge_vector_tasks_content_hash",
                "ix_knowledge_vector_tasks_status",
                "ix_knowledge_vector_tasks_next_attempt_at",
                "ix_knowledge_vector_tasks_lease_expires_at",
                "ix_knowledge_vector_tasks_created_at",
            ],
        )

    def test_downgrade_drops_indexes_before_table(self):
        with patch.object(MIGRATION, "op") as operation:
            MIGRATION.downgrade()

        self.assertEqual(
            operation.method_calls,
            [
                call.drop_index(
                    "ix_knowledge_vector_tasks_created_at",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_index(
                    "ix_knowledge_vector_tasks_lease_expires_at",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_index(
                    "ix_knowledge_vector_tasks_next_attempt_at",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_index(
                    "ix_knowledge_vector_tasks_status",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_index(
                    "ix_knowledge_vector_tasks_content_hash",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_index(
                    "ix_knowledge_vector_tasks_knowledge_id",
                    table_name="knowledge_vector_tasks",
                ),
                call.drop_table("knowledge_vector_tasks"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
