from __future__ import annotations

from pathlib import Path

import answer_hub.automation_queue as automation_queue
from answer_hub.auto_review import AutoReviewPolicy
from answer_hub.automation import AutomationRunStore
from answer_hub.automation_queue import write_queue_job_metadata


def _fake_manifest(
    source_path: str | Path,
    output_root: str | Path,
    *,
    status: str,
    error: str = "",
) -> dict:
    store = AutomationRunStore(output_root)
    manifest = store.create(Path(source_path).name, "", {})
    manifest["status"] = status
    manifest["error"] = error
    return store.save(manifest)


def _fake_pipeline_with_topic_workbook(**kwargs) -> dict:
    manifest = _fake_manifest(
        kwargs["source_path"],
        kwargs["output_root"],
        status="review_pending",
    )
    artifact_dir = Path(manifest["run_dir"]) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    topic_review = artifact_dir / "topic_review_queue.xlsx"
    topic_review.write_bytes(b"topic-review")
    manifest["artifacts"]["topic_review"] = str(topic_review)
    return AutomationRunStore(kwargs["output_root"]).save(manifest)


def _production_policy() -> AutoReviewPolicy:
    return AutoReviewPolicy(
        enabled=True,
        validated_model="mimo-v2.5-pro",
        validated_prompt_version="topic-review-v1",
    )


def test_queue_moves_successful_workbook_to_completed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    source = pending / "source.xlsx"
    source.write_bytes(b"source")

    def fake_run_automation_pipeline(**kwargs):
        return _fake_manifest(
            kwargs["source_path"],
            kwargs["output_root"],
            status="review_pending",
        )

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    assert summary["status"] == "completed"
    assert summary["succeeded"] == 1
    assert not source.exists()
    assert (queue_root / "completed" / "source.xlsx").is_file()
    manifest = AutomationRunStore(tmp_path / "runs").load(
        summary["results"][0]["run_id"]
    )
    assert manifest["queue"]["disposition"] == "completed"
    assert Path(summary["log_path"]).is_file()


def test_queue_isolates_failed_workbook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    source = pending / "bad.xlsx"
    source.write_bytes(b"bad")

    def fake_run_automation_pipeline(**kwargs):
        return _fake_manifest(
            kwargs["source_path"],
            kwargs["output_root"],
            status="failed",
            error="invalid workbook",
        )

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    assert summary["status"] == "completed_with_errors"
    assert summary["failed"] == 1
    assert (queue_root / "failed" / "bad.xlsx").is_file()
    assert summary["results"][0]["error"] == "invalid workbook"


def test_queue_can_retry_failed_workbook(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    failed = queue_root / "failed"
    failed.mkdir(parents=True)
    source = failed / "retry.xlsx"
    source.write_bytes(b"retry")

    def fake_run_automation_pipeline(**kwargs):
        return _fake_manifest(
            kwargs["source_path"],
            kwargs["output_root"],
            status="review_pending",
        )

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
        retry_failed=True,
    )

    assert summary["succeeded"] == 1
    assert (queue_root / "completed" / "retry.xlsx").is_file()


def test_queue_lock_prevents_duplicate_worker(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    source = pending / "source.xlsx"
    source.write_bytes(b"source")
    (queue_root / ".runner.lock").write_text("active", encoding="utf-8")

    def unexpected_pipeline(**kwargs):
        raise AssertionError("pipeline must not run while the queue is locked")

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        unexpected_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    assert summary["status"] == "locked"
    assert source.is_file()


def test_queue_model_reviews_and_syncs_all_rows_to_candidate_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    (pending / "source.xlsx").write_bytes(b"source")
    synced: list[dict] = []

    class FakeCzAdapter:
        def sync_review_candidates(self, candidates):
            synced.extend(candidates)
            return {
                "queued": 1,
                "ready": 1,
                "rejected": 0,
                "reused": 0,
                "results": [
                    {"event_id": "TOP-001", "status": "ready"},
                    {"event_id": "TOP-002", "status": "queued"},
                ],
            }

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        _fake_pipeline_with_topic_workbook,
    )
    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (
            ["topic_id"],
            [{"topic_id": "TOP-001"}, {"topic_id": "TOP-002"}],
        ),
    )
    monkeypatch.setattr(
        automation_queue,
        "select_candidates_for_submission",
        lambda rows, policy: [{**rows[0], "decision": "approved"}],
    )
    monkeypatch.setattr(
        automation_queue,
        "partition_auto_review_candidates",
        lambda rows, policy: (
            [{**rows[0], "自动审核状态": "auto_approved"}],
            [{**rows[1], "自动审核状态": "manual_exception"}],
        ),
    )

    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=True,
        submit_to_cz=True,
        auto_review_policy=_production_policy(),
        cz_adapter=FakeCzAdapter(),
    )

    assert summary["status"] == "completed"
    assert synced == [
        {"topic_id": "TOP-001", "decision": "approved"},
        {"topic_id": "TOP-002", "自动审核状态": "manual_exception"},
    ]
    run_id = summary["results"][0]["run_id"]
    manifest = AutomationRunStore(tmp_path / "runs").load(run_id)
    model_stage = next(
        stage for stage in manifest["stages"] if stage["id"] == "model_review"
    )
    upload_stage = next(
        stage for stage in manifest["stages"] if stage["id"] == "cz_upload"
    )
    assert model_stage["status"] == "completed"
    assert upload_stage["status"] == "completed"
    assert manifest["summary"]["cz_candidate_sync"]["ready"] == 1
    assert manifest["summary"]["cz_candidate_sync"]["queued"] == 1
    assert Path(manifest["artifacts"]["model_review"]).is_file()
    assert Path(manifest["artifacts"]["cz_candidate_sync"]).is_file()


def test_queue_syncs_model_review_exceptions_for_human_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    (pending / "source.xlsx").write_bytes(b"source")
    synced: list[dict] = []

    class FakeCzAdapter:
        def sync_review_candidates(self, candidates):
            synced.extend(candidates)
            return {
                "queued": len(candidates),
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "results": [{"status": "queued"}],
            }

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        _fake_pipeline_with_topic_workbook,
    )
    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (["topic_id"], [{"topic_id": "TOP-001"}]),
    )
    monkeypatch.setattr(
        automation_queue,
        "select_candidates_for_submission",
        lambda rows, policy: [],
    )
    monkeypatch.setattr(
        automation_queue,
        "partition_auto_review_candidates",
        lambda rows, policy: ([], [rows[0]]),
    )

    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=True,
        submit_to_cz=True,
        auto_review_policy=_production_policy(),
        cz_adapter=FakeCzAdapter(),
    )

    assert summary["status"] == "completed"
    assert synced == [{"topic_id": "TOP-001"}]
    candidate_sync = summary["results"][0]["cz_candidate_sync"]
    assert candidate_sync["skipped"] is False
    assert candidate_sync["queued"] == 1


def test_queue_moves_source_to_failed_when_candidate_sync_has_partial_failures(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    (pending / "source.xlsx").write_bytes(b"source")

    class PartiallyFailingCzAdapter:
        def sync_review_candidates(self, candidates):
            return {
                "queued": len(candidates) - 1,
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "failed": 1,
                "results": [
                    {"event_id": "TOP-001", "status": "queued"},
                    {
                        "event_id": "TOP-002",
                        "status": "failed",
                        "error_code": "LOCAL_VALIDATION_ERROR",
                    },
                ],
            }

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        _fake_pipeline_with_topic_workbook,
    )
    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (
            ["topic_id"],
            [{"topic_id": "TOP-001"}, {"topic_id": "TOP-002"}],
        ),
    )
    monkeypatch.setattr(
        automation_queue,
        "select_candidates_for_submission",
        lambda rows, policy: [],
    )
    monkeypatch.setattr(
        automation_queue,
        "partition_auto_review_candidates",
        lambda rows, policy: ([], rows),
    )

    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=True,
        submit_to_cz=True,
        auto_review_policy=_production_policy(),
        cz_adapter=PartiallyFailingCzAdapter(),
    )

    assert summary["status"] == "completed_with_errors"
    assert (queue_root / "failed" / "source.xlsx").is_file()
    run_id = summary["results"][0]["run_id"]
    manifest = AutomationRunStore(tmp_path / "runs").load(run_id)
    assert manifest["status"] == "failed"
    assert manifest["summary"]["cz_candidate_sync"]["queued"] == 1
    assert manifest["summary"]["cz_candidate_sync"]["failed"] == 1
    assert Path(manifest["artifacts"]["cz_candidate_sync"]).is_file()


def test_queue_moves_source_to_failed_when_cz_candidate_sync_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    (pending / "source.xlsx").write_bytes(b"source")

    class FailingCzAdapter:
        def sync_review_candidates(self, candidates):
            raise RuntimeError("CZ unavailable")

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        _fake_pipeline_with_topic_workbook,
    )
    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (["topic_id"], [{"topic_id": "TOP-001"}]),
    )
    monkeypatch.setattr(
        automation_queue,
        "select_candidates_for_submission",
        lambda rows, policy: [rows[0]],
    )
    monkeypatch.setattr(
        automation_queue,
        "partition_auto_review_candidates",
        lambda rows, policy: ([rows[0]], []),
    )

    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=True,
        submit_to_cz=True,
        auto_review_policy=_production_policy(),
        cz_adapter=FailingCzAdapter(),
    )

    assert summary["status"] == "completed_with_errors"
    assert (queue_root / "failed" / "source.xlsx").is_file()
    run_id = summary["results"][0]["run_id"]
    manifest = AutomationRunStore(tmp_path / "runs").load(run_id)
    upload_stage = next(
        stage for stage in manifest["stages"] if stage["id"] == "cz_upload"
    )
    assert manifest["status"] == "failed"
    assert upload_stage["status"] == "failed"
    assert manifest["error"] == "CZ unavailable"


def test_queue_uses_per_job_options_and_updates_job_metadata(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    source = pending / "job-001--source.xlsx"
    source.write_bytes(b"source")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-001",
            "status": "pending",
            "options": {
                "product_type": "平板",
                "use_mimo": False,
                "clustering_mode": "rule",
                "submit_to_cz": False,
            },
        },
    )
    captured: dict = {}

    def fake_run_automation_pipeline(**kwargs):
        captured.update(kwargs)
        return _fake_manifest(
            kwargs["source_path"],
            kwargs["output_root"],
            status="review_pending",
        )

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        product_type="手机",
        use_mimo=True,
        clustering_mode="direct_mimo",
        submit_to_cz=True,
    )

    assert summary["status"] == "completed"
    assert captured["product_type"] == "平板"
    assert captured["use_mimo"] is False
    assert captured["clustering_mode"] == "rule"
    completed_source = next((queue_root / "completed").glob("*.xlsx"))
    metadata = automation_queue.read_queue_job_metadata(completed_source)
    assert metadata["job_id"] == "job-001"
    assert metadata["status"] == "completed"
    assert metadata["run_id"]
