from __future__ import annotations

from pathlib import Path

import answer_hub.automation_queue as automation_queue
from answer_hub.auto_review import AutoReviewPolicy
from answer_hub.automation import AutomationRunStore
from answer_hub.automation_queue import (
    AutomationQueue,
    read_queue_job_metadata,
    write_queue_job_metadata,
)
from answer_hub.run_history import list_automation_run_records


PROJECT_ROOT = Path(__file__).resolve().parents[1]


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
    assert summary["terminology"]["loaded"] is True
    assert summary["terminology"]["entry_count"] > 0
    assert summary["succeeded"] == 1
    assert not source.exists()
    assert (queue_root / "completed" / "source.xlsx").is_file()
    manifest = AutomationRunStore(tmp_path / "runs").load(
        summary["results"][0]["run_id"]
    )
    assert manifest["queue"]["disposition"] == "completed"
    assert Path(summary["log_path"]).is_file()


def test_queue_blocks_cz_sync_for_cluster_failure_guard(
    tmp_path: Path,
    monkeypatch,
) -> None:
    queue_root = tmp_path / "queue"
    pending = queue_root / "pending"
    pending.mkdir(parents=True)
    source = pending / "source.xlsx"
    source.write_bytes(b"source")

    def fake_run_automation_pipeline(**kwargs):
        manifest = _fake_manifest(
            kwargs["source_path"],
            kwargs["output_root"],
            status="review_pending",
            error="聚类失败保护：direct_mimo 聚类失败比例过高。",
        )
        manifest["summary"] = {
            "direct_cluster_calls": 2,
            "direct_cluster_failed": 2,
            "cluster_failure_guard_triggered": True,
            "cluster_failure_ratio": 1.0,
            "cz_candidate_sync_blocked": True,
        }
        return AutomationRunStore(kwargs["output_root"]).save(manifest)

    called = False

    class ExplodingCzAdapter:
        def sync_review_candidates(self, candidates):
            nonlocal called
            called = True
            raise AssertionError("聚类失败批次不应调用 CZ 候选同步")

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        queue_root,
        None,
        tmp_path / "runs",
        use_mimo=True,
        clustering_mode="direct_mimo",
        submit_to_cz=True,
        cz_adapter=ExplodingCzAdapter(),
    )

    assert summary["status"] == "completed_with_errors"
    assert summary["failed"] == 1
    assert (queue_root / "failed" / "source.xlsx").is_file()
    assert summary["results"][0]["status"] == "failed"
    assert called is False
    assert summary["results"][0].get("cz_candidate_sync", {}) == {}


def test_queue_can_process_while_caller_holds_queue_lock(
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
    queue = AutomationQueue(queue_root)
    with queue.lock():
        summary = automation_queue.process_automation_queue(
            queue_root,
            None,
            tmp_path / "runs",
            use_mimo=False,
            clustering_mode="rule",
            acquire_lock=False,
        )

    assert summary["status"] == "completed"
    assert (queue_root / "completed" / "source.xlsx").is_file()


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


def test_queue_treats_mimo_confirmation_runs_as_failed_for_human_retry(
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
            status="needs_confirmation",
            error="MiMo API 预检失败，需要人工确认是否继续规则兜底生成。",
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
        use_mimo=True,
        clustering_mode="direct_mimo",
    )

    assert summary["status"] == "completed_with_errors"
    assert summary["failed"] == 1
    assert (queue_root / "failed" / "source.xlsx").is_file()
    assert summary["results"][0]["status"] == "failed"
    assert "MiMo API 预检失败" in summary["results"][0]["error"]


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


def test_retrying_failed_cz_sync_restores_run_to_review_pending(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "runs"
    queue_root = tmp_path / "queue"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    failed_source = queue.failed / "job-001--source.xlsx"
    failed_source.write_bytes(b"source")
    manifest = _fake_manifest(
        failed_source,
        output_root,
        status="failed",
        error="未配置 KB_BASE_URL 或 KB_INTEGRATION_KEY。",
    )
    artifact_dir = Path(manifest["run_dir"]) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    topic_review = artifact_dir / "topic_review_queue.xlsx"
    topic_review.write_bytes(b"topic-review")
    manifest["artifacts"]["topic_review"] = str(topic_review)
    manifest["queue"] = {
        "source_path": str(failed_source),
        "claimed_path": str(failed_source),
        "final_path": str(failed_source),
        "disposition": "failed",
    }
    manifest["summary"]["cz_candidate_sync"] = {
        "queued": 0,
        "ready": 0,
        "rejected": 0,
        "reused": 0,
        "failed": 1,
    }
    write_queue_job_metadata(
        failed_source,
        {
            "job_id": "job-001",
            "status": "failed",
            "run_id": manifest["run_id"],
            "error": manifest["error"],
            "summary": {},
            "artifacts": {},
            "options": {"sync_to_cz_review": True},
        },
    )
    AutomationRunStore(output_root).save(manifest)

    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (
            ["topic_id"],
            [{"topic_id": "TOP-001"}],
        ),
    )

    class SuccessfulCzAdapter:
        def sync_review_candidates(self, candidates):
            return {
                "queued": len(candidates),
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "failed": 0,
                "results": [{"event_id": "TOP-001", "status": "queued"}],
            }

    result = automation_queue._run_model_review_and_cz_candidate_sync(
        manifest,
        output_root,
        policy=AutoReviewPolicy(),
        cz_adapter=SuccessfulCzAdapter(),
    )

    upload_stage = next(
        stage for stage in result["stages"] if stage["id"] == "cz_upload"
    )
    assert result["status"] == "review_pending"
    assert result["status_label"] == "待人工审核"
    assert result["error"] == ""
    assert result["attempt_count"] == 2
    assert result["retry_history"][-1]["error"] == (
        "未配置 KB_BASE_URL 或 KB_INTEGRATION_KEY。"
    )
    assert result["retry_history"][-1]["failed_stage"] == "cz_upload"
    assert result["retry_history"][-1]["cz_candidate_sync"]["failed"] == 1
    assert upload_stage["status"] == "completed"
    completed_source = queue.completed / failed_source.name
    assert completed_source.is_file()
    assert not failed_source.exists()
    assert result["queue"]["disposition"] == "completed"
    assert Path(result["queue"]["final_path"]) == completed_source
    metadata = read_queue_job_metadata(completed_source)
    assert metadata["status"] == "completed"
    assert metadata["error"] == ""
    assert metadata["run_id"] == result["run_id"]
    assert queue.candidates(retry_failed=True) == []
    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=10,
    )
    assert records[0]["effective_status"] == "review_pending"


def test_cz_retry_rolls_queue_file_back_when_metadata_update_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    output_root = tmp_path / "runs"
    queue = AutomationQueue(tmp_path / "queue")
    queue.ensure()
    failed_source = queue.failed / "job-rollback--source.xlsx"
    failed_source.write_bytes(b"source")
    manifest = _fake_manifest(
        failed_source,
        output_root,
        status="failed",
        error="CZ unavailable",
    )
    artifact_dir = Path(manifest["run_dir"]) / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    topic_review = artifact_dir / "topic_review_queue.xlsx"
    topic_review.write_bytes(b"topic-review")
    manifest["artifacts"]["topic_review"] = str(topic_review)
    manifest["queue"] = {
        "final_path": str(failed_source),
        "disposition": "failed",
    }
    write_queue_job_metadata(
        failed_source,
        {
            "job_id": "job-rollback",
            "status": "failed",
            "run_id": manifest["run_id"],
            "error": manifest["error"],
        },
    )
    AutomationRunStore(output_root).save(manifest)
    monkeypatch.setattr(
        automation_queue,
        "read_workbook_rows",
        lambda *args, **kwargs: (
            ["topic_id"],
            [{"topic_id": "TOP-ROLLBACK"}],
        ),
    )

    class SuccessfulCzAdapter:
        def sync_review_candidates(self, candidates):
            return {
                "queued": len(candidates),
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "failed": 0,
                "results": [
                    {"event_id": "TOP-ROLLBACK", "status": "queued"}
                ],
            }

    original_write_metadata = automation_queue.write_queue_job_metadata

    def fail_completed_metadata_write(source_path, payload):
        if Path(source_path).parent == queue.completed:
            raise OSError("simulated metadata write failure")
        return original_write_metadata(source_path, payload)

    monkeypatch.setattr(
        automation_queue,
        "write_queue_job_metadata",
        fail_completed_metadata_write,
    )

    result = automation_queue._run_model_review_and_cz_candidate_sync(
        manifest,
        output_root,
        policy=AutoReviewPolicy(),
        cz_adapter=SuccessfulCzAdapter(),
    )

    assert result["status"] == "failed"
    assert "simulated metadata write failure" in result["error"]
    assert failed_source.is_file()
    assert list(queue.completed.glob("*.xlsx")) == []
    assert result["queue"]["disposition"] == "failed"
    metadata = read_queue_job_metadata(failed_source)
    assert metadata["status"] == "failed"
    assert metadata["error"] == "CZ unavailable"
    assert queue.candidates(retry_failed=True) == [failed_source]


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


def test_queue_runner_builds_a_complete_powerzhuan_query_window() -> None:
    runner = (
        PROJECT_ROOT / "scripts" / "run_automation_queue.ps1"
    ).read_text(encoding="utf-8-sig")

    assert '$env:SECOND_PART_QUERY_FROM_DATE' in runner
    assert '$env:SECOND_PART_QUERY_TO_DATE' in runner
    assert '$env:SECOND_PART_QUERY_WINDOW_DAYS' in runner
    assert '(Get-Date).Date.AddDays(-1)' in runner
