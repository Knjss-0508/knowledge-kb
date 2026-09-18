import unittest
from unittest.mock import MagicMock, patch

from app.scripts import check_media_cutover as script


class MediaCutoverPreflightTests(unittest.TestCase):
    def _session(self, deletion_count: int, staging_count: int):
        db = MagicMock()
        deletion_query = MagicMock()
        deletion_query.filter.return_value.count.return_value = deletion_count
        staging_query = MagicMock()
        staging_query.filter.return_value.count.return_value = staging_count
        db.query.side_effect = [deletion_query, staging_query]
        return db

    def test_empty_local_queues_pass(self):
        with patch.object(script, "get_local_queue_counts", return_value=(0, 0)):
            self.assertEqual(script.main(), 0)

    def test_non_empty_local_queue_blocks_cutover(self):
        with patch.object(script, "get_local_queue_counts", return_value=(1, 0)):
            self.assertEqual(script.main(), 1)

    def test_database_error_returns_preflight_error(self):
        with patch.object(
            script,
            "get_local_queue_counts",
            side_effect=RuntimeError("database unavailable"),
        ):
            self.assertEqual(script.main(), 2)

    def test_count_query_is_read_only_and_closes_session(self):
        db = self._session(0, 0)
        with patch.object(script, "SessionLocal", return_value=db):
            self.assertEqual(script.get_local_queue_counts(), (0, 0))

        self.assertEqual(db.query.call_count, 2)
        db.commit.assert_not_called()
        db.flush.assert_not_called()
        db.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
