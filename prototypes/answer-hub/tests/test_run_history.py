from datetime import datetime
from pathlib import Path

from answer_hub import run_history as run_history_module
from answer_hub.automation import AutomationRunStore
from answer_hub.automation_queue import (
    AutomationQueue,
    write_queue_job_metadata,
)
from answer_hub.run_history import list_automation_run_records


def test_run_monitor_lists_pending_online_job_before_run_exists(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.pending / "job-001--sample.xlsx"
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-001",
            "status": "pending",
            "created_at": "2026-08-07T10:00:00+08:00",
            "updated_at": "2026-08-07T10:00:00+08:00",
            "original_filename": "sample.xlsx",
            "run_id": "",
            "options": {"sync_to_cz_review": True},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "job-001"
    assert records[0]["source_type"] == "online_api"
    assert records[0]["source_label"] == "线上接口"
    assert records[0]["effective_status"] == "pending"
    assert records[0]["status_label"] == "排队中"
    assert records[0]["source_name"] == "sample.xlsx"
    assert records[0]["sync_to_cz_review"] is True


def test_run_monitor_marks_running_job_stalled_after_heartbeat_timeout(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.processing / "job-stalled--sample.xlsx"
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-stalled",
            "status": "processing",
            "created_at": "2026-08-09T08:00:00+08:00",
            "updated_at": "2026-08-09T08:30:00+08:00",
            "original_filename": "sample.xlsx",
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
        stale_after_seconds=7_200,
        now=datetime.fromisoformat("2026-08-09T12:00:00+08:00"),
    )

    assert len(records) == 1
    assert records[0]["effective_status"] == "running"
    assert records[0]["health_status"] == "stalled"
    assert records[0]["health_label"] == "疑似卡住"
    assert records[0]["is_stale"] is True
    assert records[0]["stale_seconds"] == 12_600


def test_run_monitor_uses_latest_topic_substage_as_current_activity(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "runs"
    queue_root = tmp_path / "queue"
    AutomationQueue(queue_root).ensure()
    store = AutomationRunStore(output_root)
    manifest = store.create("sample.xlsx", "", {})
    store.update_stage(
        manifest,
        "topic_build",
        "running",
        "主题处理中。",
    )
    store.update_stage(
        manifest,
        "knowledge_transcription",
        "running",
        "正在进行知识转写与内容初审。",
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert records[0]["current_stage"]["id"] == "knowledge_transcription"
    assert records[0]["current_stage"]["label"] == "知识转写与内容初审"
    assert records[0]["activity_history"][-1]["stage_id"] == (
        "knowledge_transcription"
    )


def test_run_monitor_lists_manually_dropped_queue_file_before_run_exists(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    (queue.pending / "manual-drop.xlsx").write_bytes(b"sample")

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "queue:manual-drop.xlsx"
    assert records[0]["source_type"] == "automation_queue"
    assert records[0]["source_label"] == "自动化队列"
    assert records[0]["effective_status"] == "pending"
    assert records[0]["source_name"] == "manual-drop.xlsx"

    queue.claim(queue.pending / "manual-drop.xlsx")
    processing_records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert processing_records[0]["record_id"] == "queue:manual-drop.xlsx"


def test_run_monitor_merges_online_job_with_running_manifest(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    store = AutomationRunStore(output_root)
    manifest = store.create(
        "sample.xlsx",
        "",
        {"sync_to_cz_review": True},
    )
    store.update_stage(
        manifest,
        "intake",
        "completed",
        "输入文件已保存。",
        {"source_bytes": 100},
    )
    store.update_stage(
        manifest,
        "load_input",
        "running",
        "正在读取输入。",
    )
    manifest["summary"] = {
        "eligible_rows": 70,
        "topic_rows": 5,
        "pending_cluster_rows": 12,
        "cz_candidate_sync": {
            "queued": 4,
            "ready": 0,
            "failed": 1,
        },
    }
    store.save(manifest)

    source = queue.processing / "job-002--sample.xlsx"
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-002",
            "status": "processing",
            "created_at": "2026-08-07T10:00:00+08:00",
            "updated_at": "2026-08-07T10:05:00+08:00",
            "original_filename": "sample.xlsx",
            "run_id": manifest["run_id"],
            "options": {"sync_to_cz_review": True},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    record = records[0]
    assert record["record_id"] == "job-002"
    assert record["run_id"] == manifest["run_id"]
    assert record["effective_status"] == "running"
    assert record["current_stage"]["id"] == "load_input"
    assert record["current_stage"]["detail"] == "正在读取输入。"
    assert record["summary"]["eligible_rows"] == 70
    assert record["cz_sync"]["queued"] == 4
    assert record["cz_sync"]["failed"] == 1


def test_run_monitor_matches_processing_job_before_run_id_is_written(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    queued_filename = "job-003--sample.xlsx"

    store = AutomationRunStore(output_root)
    manifest = store.create(queued_filename, "", {})
    store.update_stage(
        manifest,
        "semantic_label",
        "running",
        "正在进行语义标注。",
    )

    source = queue.processing / queued_filename
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-003",
            "status": "processing",
            "created_at": "2026-08-07T10:00:00+08:00",
            "updated_at": "2026-08-07T10:05:00+08:00",
            "original_filename": "sample.xlsx",
            "queued_filename": queued_filename,
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "job-003"
    assert records[0]["run_id"] == manifest["run_id"]
    assert records[0]["effective_status"] == "running"
    assert records[0]["current_stage"]["id"] == "semantic_label"


def test_run_monitor_groups_online_job_retry_attempts(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    queued_filename = "job-004--sample.xlsx"
    store = AutomationRunStore(output_root)

    failed = store.create(queued_filename, "", {})
    failed["status"] = "failed"
    failed["error"] = "first attempt failed"
    store.save(failed)

    running = store.create(queued_filename, "", {})
    store.update_stage(
        running,
        "topic_build",
        "running",
        "第二次尝试正在构建主题。",
    )

    source = queue.processing / queued_filename
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-004",
            "status": "processing",
            "created_at": "2026-08-07T10:00:00+08:00",
            "updated_at": "2026-08-07T10:10:00+08:00",
            "original_filename": "sample.xlsx",
            "queued_filename": queued_filename,
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    record = records[0]
    assert record["record_id"] == "job-004"
    assert record["run_id"] == running["run_id"]
    assert record["effective_status"] == "running"
    assert record["attempt_count"] == 2
    assert [attempt["run_id"] for attempt in record["attempt_runs"]] == [
        running["run_id"],
        failed["run_id"],
    ]
    assert record["attempt_runs"][1]["error"] == "first attempt failed"


def test_run_monitor_keeps_failed_attempt_visible_while_retry_is_pending(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    queued_filename = "job-005--sample.xlsx"
    store = AutomationRunStore(output_root)
    failed = store.create(queued_filename, "", {})
    failed["status"] = "failed"
    failed["error"] = "previous attempt failed"
    store.save(failed)

    source = queue.pending / queued_filename
    source.write_bytes(b"sample")
    write_queue_job_metadata(
        source,
        {
            "job_id": "job-005",
            "status": "pending",
            "created_at": "2026-08-07T10:00:00+08:00",
            "updated_at": "2026-08-07T10:20:00+08:00",
            "original_filename": "sample.xlsx",
            "queued_filename": queued_filename,
            "run_id": "",
            "options": {},
            "summary": {},
            "artifacts": {},
            "error": "",
        },
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    record = records[0]
    assert record["effective_status"] == "pending"
    assert record["run_id"] == ""
    assert record["attempt_count"] == 1
    assert record["attempt_runs"][0]["run_id"] == failed["run_id"]
    assert record["attempt_runs"][0]["error"] == "previous attempt failed"


def test_run_monitor_preserves_queue_failure_and_manual_run_origin(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    store = AutomationRunStore(output_root)

    failed = store.create("queue.xlsx", "", {})
    store.update_stage(
        failed,
        "topic_build",
        "failed",
        "cluster failure",
    )
    failed["status"] = "failed"
    failed["error"] = "cluster failure"
    failed["queue"] = {"disposition": "failed"}
    store.save(failed)

    manual = store.create("manual.xlsx", "", {})
    manual["status"] = "review_pending"
    store.save(manual)

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )
    by_run_id = {record["run_id"]: record for record in records}

    failed_record = by_run_id[failed["run_id"]]
    assert failed_record["source_type"] == "automation_queue"
    assert failed_record["source_label"] == "自动化队列"
    assert failed_record["effective_status"] == "failed"
    assert failed_record["current_stage"]["id"] == "topic_build"
    assert failed_record["error"] == "cluster failure"

    manual_record = by_run_id[manual["run_id"]]
    assert manual_record["source_type"] == "streamlit"
    assert manual_record["source_label"] == "Streamlit手动验证"
    assert manual_record["effective_status"] == "review_pending"


def test_run_monitor_deduplicates_manual_queue_file_and_manifest(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.failed / "manual-failed.xlsx"
    source.write_bytes(b"sample")

    store = AutomationRunStore(output_root)
    manifest = store.create("manual-failed.xlsx", "", {})
    manifest["status"] = "failed"
    manifest["error"] = "sync failure"
    manifest["queue"] = {
        "final_path": str(source),
        "disposition": "failed",
    }
    store.save(manifest)

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "queue:manual-failed.xlsx"
    assert records[0]["source_type"] == "automation_queue"
    assert records[0]["source_name"] == "manual-failed.xlsx"
    assert records[0]["error"] == "sync failure"


def test_run_monitor_groups_manually_dropped_queue_retry_attempts(
    tmp_path: Path,
) -> None:
    queue_root = tmp_path / "queue"
    output_root = tmp_path / "runs"
    queue = AutomationQueue(queue_root)
    queue.ensure()
    source = queue.processing / "manual-retry.xlsx"
    source.write_bytes(b"sample")
    store = AutomationRunStore(output_root)

    failed = store.create("manual-retry.xlsx", "", {})
    failed["status"] = "failed"
    failed["error"] = "first manual attempt failed"
    failed["queue"] = {"disposition": "failed"}
    store.save(failed)

    running = store.create("manual-retry.xlsx", "", {})
    store.update_stage(
        running,
        "semantic_label",
        "running",
        "manual retry running",
    )

    records = list_automation_run_records(
        output_root,
        queue_root,
        limit=20,
    )

    assert len(records) == 1
    assert records[0]["record_id"] == "queue:manual-retry.xlsx"
    assert records[0]["source_type"] == "automation_queue"
    assert records[0]["effective_status"] == "running"
    assert records[0]["attempt_count"] == 2
    assert [attempt["run_id"] for attempt in records[0]["attempt_runs"]] == [
        running["run_id"],
        failed["run_id"],
    ]


def test_run_monitor_redacts_secrets_from_visible_errors(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "runs"
    store = AutomationRunStore(output_root)
    manifest = store.create("secret-error.xlsx", "", {})
    manifest["status"] = "failed"
    manifest["error"] = (
        "request failed: https://example.test/run?token=url-secret "
        "Authorization=Bearer bearer-secret "
        "KB_INTEGRATION_KEY=integration-secret "
        '{"token":"json-secret"} '
        "{'X-Integration-Key': 'dict-secret'}"
    )
    manifest["alerts"] = ["api_key=alert-secret"]
    manifest["retry_history"] = [
        {
            "attempt": 1,
            "error": "previous api_key=retry-top-secret",
            "cz_candidate_sync": {
                "results": [
                    {
                        "status": "failed",
                        "error_message": (
                            "KB_INTEGRATION_KEY=retry-nested-secret"
                        ),
                    }
                ]
            },
        }
    ]
    store.save(manifest)

    records = list_automation_run_records(
        output_root,
        tmp_path / "queue",
        limit=20,
    )

    record = records[0]
    visible_text = " ".join(
        [
            record["error"],
            *record["alerts"],
            record["attempt_runs"][0]["error"],
            str(record["retry_history"]),
        ]
    )
    assert "url-secret" not in visible_text
    assert "bearer-secret" not in visible_text
    assert "integration-secret" not in visible_text
    assert "json-secret" not in visible_text
    assert "dict-secret" not in visible_text
    assert "alert-secret" not in visible_text
    assert "retry-top-secret" not in visible_text
    assert "retry-nested-secret" not in visible_text
    assert "<redacted>" in visible_text


def test_run_monitor_ignores_queue_file_moved_during_scan(
    tmp_path: Path,
    monkeypatch,
) -> None:
    missing_source = tmp_path / "queue" / "pending" / "moved.xlsx"
    monkeypatch.setattr(
        run_history_module,
        "_queue_sources_without_metadata",
        lambda _queue_root, _metadata_sources: [
            ("pending", missing_source)
        ],
    )

    records = list_automation_run_records(
        tmp_path / "runs",
        tmp_path / "queue",
        limit=20,
    )

    assert records == []
