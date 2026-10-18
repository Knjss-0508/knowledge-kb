import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app import main
from app.routes import high_frequency
from app.services.second_part_client import SecondPartFetchResult


class HighFrequencyRouteTests(unittest.TestCase):
    def test_dashboard_reads_persisted_payload_without_fetching_upstream(self):
        payload = {"summary": {"total_records": 2}, "meta": {}}
        with patch.object(
            high_frequency,
            "build_persisted_dashboard",
            return_value=payload,
        ) as build, patch.object(high_frequency, "fetch_records") as fetch:
            result = high_frequency.dashboard(
                from_date="2026-10-01",
                to_date="2026-10-02",
                category="手机",
                level1="",
                level2="",
                business_theme="",
                high_frequency_only=True,
                period="week",
                db=object(),
                _=None,
            )

        self.assertIs(result, payload)
        build.assert_called_once()
        fetch.assert_not_called()

    def test_sync_fetches_upstream_and_queues_only_persisted_delta(self):
        source = SecondPartFetchResult(
            records=[{"工单ID": "1"}],
            possibly_truncated=False,
            request_count=1,
        )
        with patch.object(high_frequency, "fetch_records", return_value=source), patch.object(
            high_frequency,
            "ingest_records",
            return_value={
                "fetched": 1,
                "inserted": 1,
                "changed": 0,
                "unchanged": 0,
                "queued": 1,
            },
        ) as ingest:
            result = high_frequency.sync(
                from_date="2026-10-01",
                to_date="2026-10-01",
                dry_run=False,
                db=object(),
                _=None,
            )

        self.assertEqual(result["queued"], 1)
        self.assertEqual(result["source_request_count"], 1)
        ingest.assert_called_once()

    def test_sync_persists_source_completeness_metadata(self):
        source = SecondPartFetchResult(
            records=[{"工单ID": "1"}],
            possibly_truncated=True,
            request_count=4,
        )
        with patch.object(high_frequency, "fetch_records", return_value=source), patch.object(
            high_frequency,
            "ingest_records",
            return_value={"fetched": 1, "queued": 1},
        ) as ingest:
            high_frequency.sync(
                from_date="2026-10-01",
                to_date="2026-10-02",
                dry_run=False,
                db=object(),
                _=None,
            )

        self.assertTrue(ingest.call_args.kwargs["source_possibly_truncated"])
        self.assertEqual(ingest.call_args.kwargs["source_request_count"], 4)

    def test_dry_run_checks_source_without_writing_or_queuing(self):
        source = SecondPartFetchResult(
            records=[
                {
                    "工单ID": "1",
                    "ai_result": {"核心问题": "屏幕异常如何判定"},
                    "产品类型": "手机",
                }
            ],
            possibly_truncated=False,
            request_count=1,
        )
        with patch.object(high_frequency, "fetch_records", return_value=source), patch.object(
            high_frequency,
            "ingest_records",
        ) as ingest:
            result = high_frequency.sync(
                from_date="2026-10-01",
                to_date="2026-10-01",
                dry_run=True,
                db=object(),
                _=None,
            )

        self.assertTrue(result["dry_run"])
        self.assertEqual(result["fetched"], 1)
        self.assertEqual(result["ai_core_problem_count"], 1)
        ingest.assert_not_called()

    def test_export_guard_rejects_pending_and_failed_results(self):
        for meta in (
            {"classification_pending": True},
            {"worker_state": "failed"},
            {"possibly_truncated": True},
        ):
            with self.subTest(meta=meta):
                with self.assertRaises(HTTPException) as raised:
                    high_frequency._assert_export_ready({"meta": meta})
                self.assertEqual(raised.exception.status_code, 409)

        high_frequency._assert_export_ready(
            {"meta": {"classification_pending": False, "worker_state": "completed"}}
        )

    def test_shadow_comparison_returns_local_snapshot_read_only(self):
        payload = {
            "schema_version": 1,
            "summary": {"source_record_count": 161},
            "comparison_rows": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "high_frequency_shadow_comparison.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with patch.object(high_frequency, "_shadow_comparison_path", return_value=path):
                self.assertEqual(high_frequency.shadow_comparison(_=None), payload)

    def test_sync_endpoint_requires_submit_permission(self):
        route = next(
            route
            for route in high_frequency.router.routes
            if route.path.endswith("/sync")
        )
        closure_values = [
            cell.cell_contents
            for dependency in route.dependant.dependencies
            for cell in (dependency.call.__closure__ or ())
        ]
        self.assertIn("knowledge:submit", closure_values)

    def test_high_frequency_worker_count_is_bounded(self):
        with patch.object(main.settings, "HIGH_FREQUENCY_WORKER_CONCURRENCY", 0):
            self.assertEqual(main._high_frequency_worker_count(), 1)
        with patch.object(main.settings, "HIGH_FREQUENCY_WORKER_CONCURRENCY", 4):
            self.assertEqual(main._high_frequency_worker_count(), 4)
        with patch.object(main.settings, "HIGH_FREQUENCY_WORKER_CONCURRENCY", 99):
            self.assertEqual(main._high_frequency_worker_count(), 8)


if __name__ == "__main__":
    unittest.main()
