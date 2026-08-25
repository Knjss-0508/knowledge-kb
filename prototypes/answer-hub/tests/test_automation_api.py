from __future__ import annotations

from io import BytesIO
import json
from pathlib import Path
import threading
import time

import answer_hub.automation_queue as automation_queue
from answer_hub.automation import AutomationRunStore
from answer_hub.automation_api import create_automation_api_app
from answer_hub.automation_queue import (
    AutomationQueue,
    read_queue_job_metadata,
    write_queue_job_metadata,
)
from answer_hub.excel_io import read_workbook_rows, write_rows_to_workbook


API_KEY = "test-answer-hub-key"
HEADERS = {"X-Answer-Hub-Key": API_KEY}


def _app(tmp_path: Path):
    app = create_automation_api_app(
        api_key=API_KEY,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
    )
    app.config["TESTING"] = True
    return app


def _create_job(client):
    return client.post(
        "/api/v1/automation/jobs",
        headers=HEADERS,
        data={
            "source_file": (BytesIO(b"source"), "second-part.xlsx"),
            "product_type": "手机",
            "use_mimo": "true",
            "clustering_mode": "direct_mimo",
            "sync_to_cz_review": "true",
        },
        content_type="multipart/form-data",
    )


def test_automation_api_requires_api_key(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    response = client.get("/api/v1/automation/jobs/missing")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"


def test_automation_api_creates_and_reads_job(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    created = _create_job(client)

    assert created.status_code == 202
    payload = created.get_json()
    assert payload["status"] == "pending"
    assert payload["options"]["sync_to_cz_review"] is True
    assert payload["options"]["submit_to_cz"] is True
    assert payload["options"]["clustering_mode"] == "direct_mimo"
    job_id = payload["job_id"]

    status = client.get(
        f"/api/v1/automation/jobs/{job_id}",
        headers=HEADERS,
    )
    assert status.status_code == 200
    assert status.get_json()["job_id"] == job_id
    assert status.get_json()["status"] == "pending"


def test_automation_api_lists_runs_and_persists_operator_feedback(
    tmp_path: Path,
) -> None:
    client = _app(tmp_path).test_client()
    created = _create_job(client).get_json()
    job_id = created["job_id"]
    queue = AutomationQueue(tmp_path / "queue")
    source = next(queue.pending.glob("*.xlsx"))
    metadata = read_queue_job_metadata(source)
    metadata["artifacts"] = {
        "summary": str(tmp_path / "private" / "summary.json")
    }
    metadata["summary"] = {
        "topic_rows": 1,
        "source_file": str(tmp_path / "private" / "source.xlsx"),
        "audit_db": str(tmp_path / "private" / "audit.db"),
    }
    metadata["options"]["direct_mimo_progress_path"] = str(
        tmp_path / "private" / "progress.json"
    )
    write_queue_job_metadata(source, metadata)

    listed = client.get(
        "/api/v1/automation/jobs",
        headers=HEADERS,
    )

    assert listed.status_code == 200
    payload = listed.get_json()
    assert payload["count"] == 1
    assert payload["items"][0]["record_id"] == job_id
    assert payload["items"][0]["health_status"] == "active"
    assert payload["items"][0]["feedback"]["status"] == "unhandled"
    assert payload["items"][0]["artifact_names"] == ["summary"]
    assert "artifacts" not in payload["items"][0]
    assert payload["items"][0]["summary"] == {"topic_rows": 1}
    assert "options" not in payload["items"][0]
    assert str(tmp_path) not in json.dumps(payload, ensure_ascii=False)

    updated = client.patch(
        f"/api/v1/automation/jobs/{job_id}/feedback",
        headers=HEADERS,
        json={
            "status": "in_progress",
            "owner": "小张",
            "cause_type": "clustering",
            "note": "正在检查聚类阶段。",
            "actor": "管理员",
        },
    )

    assert updated.status_code == 200
    assert updated.get_json()["feedback"]["status_label"] == "处理中"
    reloaded = client.get(
        "/api/v1/automation/jobs",
        headers=HEADERS,
    ).get_json()
    assert reloaded["items"][0]["feedback"]["owner"] == "小张"
    assert len(reloaded["items"][0]["feedback"]["history"]) == 1


def test_automation_api_defaults_candidate_sync_to_disabled(tmp_path: Path) -> None:
    client = _app(tmp_path).test_client()

    response = client.post(
        "/api/v1/automation/jobs",
        headers=HEADERS,
        data={
            "source_file": (BytesIO(b"source"), "second-part.xlsx"),
            "clustering_mode": "direct_mimo",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 202
    options = response.get_json()["options"]
    assert options["sync_to_cz_review"] is False
    assert options["submit_to_cz"] is False


def test_automation_api_accepts_second_part_json_batch_and_reuses_idempotency_key(
    tmp_path: Path,
) -> None:
    client = _app(tmp_path).test_client()
    request_body = {
        "source_system": "second-part-test",
        "idempotency_key": "batch-second-part-001",
        "options": {
            "use_mimo": False,
            "clustering_mode": "rule",
            "sync_to_cz_review": True,
        },
        "items": [
            {
                "event_id": "event-001",
                "redaction_status": "redacted",
                "record": {
                    "工单ID": "WO-001",
                    "聊天内容": "手机屏幕有亮线，应该怎么判断？",
                    "图片链接": [
                        "https://example.com/redacted-1.jpg",
                        "https://example.com/redacted-2.jpg",
                    ],
                    "产品类型": "手机",
                    "核心问题": "屏幕亮线如何判断",
                    "判定结论": "待人工确认",
                    "历史实际回复": "请补充白屏图片后核验。",
                },
            }
        ],
    }

    created = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json=request_body,
    )

    assert created.status_code == 202
    payload = created.get_json()
    assert payload["status"] == "pending"
    assert payload["reused"] is False
    assert payload["accepted_records"] == 1
    assert payload["options"]["source_system"] == "second-part-test"
    assert payload["options"]["source_batch_key"].startswith("sha256:")
    assert payload["options"]["source_idempotency_key"] == (
        "batch-second-part-001"
    )
    assert payload["options"]["sync_to_cz_review"] is True

    queue = AutomationQueue(tmp_path / "queue")
    workbook = next(queue.pending.glob("*.xlsx"))
    columns, rows = read_workbook_rows(workbook)
    assert "聊天内容" in columns
    assert rows == [
        {
            "序号": 1,
            "上传者": "second-part-test",
            "分析时间": None,
            "工单ID": "WO-001",
            "回收单号": None,
            "聊天内容": "手机屏幕有亮线，应该怎么判断？",
            "图片链接": (
                "https://example.com/redacted-1.jpg\n"
                "https://example.com/redacted-2.jpg"
            ),
            "视频链接": None,
            "核心问题": "屏幕亮线如何判断",
            "判定结论": "待人工确认",
            "判定依据": None,
            "回收业务层级": None,
            "回收业务层级编码": None,
            "产品类型": "手机",
            "产品类型编码": None,
            "一级分类": None,
            "二级分类": None,
            "参考话术": None,
            "历史实际回复": "请补充白屏图片后核验。",
            "ai_result": None,
        }
    ]

    replayed = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json=request_body,
    )

    assert replayed.status_code == 200
    assert replayed.get_json()["job_id"] == payload["job_id"]
    assert replayed.get_json()["reused"] is True
    assert len(list(queue.pending.glob("*.xlsx"))) == 1


def test_automation_api_rejects_non_redacted_second_part_record(
    tmp_path: Path,
) -> None:
    client = _app(tmp_path).test_client()

    response = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json={
            "idempotency_key": "batch-private-001",
            "items": [
                {
                    "event_id": "event-private-001",
                    "redaction_status": "pending",
                    "record": {
                        "工单ID": "WO-PRIVATE-001",
                        "聊天内容": "未完成脱敏的会话",
                        "产品类型": "手机",
                    },
                }
            ]
        },
    )

    assert response.status_code == 400
    assert "已脱敏" in response.get_json()["error"]


def test_automation_api_rejects_changed_batch_reusing_idempotency_key(
    tmp_path: Path,
) -> None:
    client = _app(tmp_path).test_client()
    request_body = {
        "source_system": "second-part-test",
        "idempotency_key": "batch-idempotency-conflict-001",
        "items": [
            {
                "redaction_status": "redacted",
                "record": {
                    "工单ID": "WO-CONFLICT-001",
                    "聊天内容": "手机无法开机怎么处理？",
                    "产品类型": "手机",
                },
            }
        ],
    }
    first = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json=request_body,
    )
    assert first.status_code == 202

    request_body["items"][0]["record"]["聊天内容"] = (
        "手机无法开机且充电无反应怎么处理？"
    )
    conflict = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json=request_body,
    )

    assert conflict.status_code == 409
    assert "幂等键" in conflict.get_json()["error"]
    assert len(list((tmp_path / "queue" / "pending").glob("*.xlsx"))) == 1


def test_second_part_json_batch_reaches_candidate_value_review(
    tmp_path: Path,
    monkeypatch,
) -> None:
    client = _app(tmp_path).test_client()
    received = client.post(
        "/api/v1/automation/second-part/records:batch",
        headers=HEADERS,
        json={
            "source_system": "second-part-test",
            "idempotency_key": "batch-e2e-001",
            "options": {
                "use_mimo": False,
                "clustering_mode": "rule",
                "sync_to_cz_review": True,
            },
            "items": [
                {
                    "event_id": "event-e2e-001",
                    "redaction_status": "redacted",
                    "record": {
                        "工单ID": "WO-E2E-001",
                        "聊天内容": "手机无法充电，应该如何排查？",
                        "产品类型": "手机",
                        "历史实际回复": "请先检查充电器、线材和接口。",
                    },
                }
            ],
        },
    )
    assert received.status_code == 202

    observed: dict[str, object] = {}

    def fake_pipeline(**kwargs):
        _columns, source_rows = read_workbook_rows(kwargs["source_path"])
        observed["source_rows"] = source_rows
        store = AutomationRunStore(kwargs["output_root"])
        manifest = store.create(Path(kwargs["source_path"]).name, "", {})
        manifest["status"] = "review_pending"
        artifact_dir = Path(manifest["run_dir"]) / "artifacts"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        topic_review = artifact_dir / "topic_review_queue.xlsx"
        write_rows_to_workbook(
            {
                "topic_review_queue": (
                    ["主题ID", "主标题", "知识内容", "产品类型"],
                    [
                        {
                            "主题ID": "TOP-E2E-001",
                            "主标题": "手机无法充电的排查流程",
                            "知识内容": "先检查充电器、线材和接口。",
                            "产品类型": "手机",
                        }
                    ],
                )
            },
            topic_review,
        )
        manifest["artifacts"]["topic_review"] = str(topic_review)
        return store.save(manifest)

    class FakeCzAdapter:
        def sync_review_candidates(self, candidates):
            observed["candidates"] = candidates
            return {
                "queued": len(candidates),
                "ready": 0,
                "rejected": 0,
                "reused": 0,
                "failed": 0,
                "results": [
                    {
                        "event_id": "TOP-E2E-001",
                        "status": "queued",
                    }
                ],
            }

    monkeypatch.setattr(
        automation_queue,
        "run_automation_pipeline",
        fake_pipeline,
    )
    summary = automation_queue.process_automation_queue(
        tmp_path / "queue",
        None,
        tmp_path / "runs",
        cz_adapter=FakeCzAdapter(),
    )

    assert observed["source_rows"] == [
        {
            "序号": 1,
            "上传者": "second-part-test",
            "分析时间": None,
            "工单ID": "WO-E2E-001",
            "回收单号": None,
            "聊天内容": "手机无法充电，应该如何排查？",
            "图片链接": None,
            "视频链接": None,
            "核心问题": None,
            "判定结论": None,
            "判定依据": None,
            "回收业务层级": None,
            "回收业务层级编码": None,
            "产品类型": "手机",
            "产品类型编码": None,
            "一级分类": None,
            "二级分类": None,
            "参考话术": None,
            "历史实际回复": "请先检查充电器、线材和接口。",
            "ai_result": None,
        }
    ]
    candidates = observed["candidates"]
    assert isinstance(candidates, list)
    assert len(candidates) == 1
    assert candidates[0]["主题ID"] == "TOP-E2E-001"
    assert candidates[0]["主标题"] == "手机无法充电的排查流程"
    assert candidates[0]["自动审核状态"] == "validation_manual_exception"
    assert summary["status"] == "completed"
    assert summary["results"][0]["cz_candidate_sync"]["queued"] == 1
    job_id = received.get_json()["job_id"]
    completed = client.get(
        f"/api/v1/automation/jobs/{job_id}",
        headers=HEADERS,
    )
    assert completed.status_code == 200
    assert completed.get_json()["status"] == "completed"


def test_automation_api_reports_corrupt_feedback_store(
    tmp_path: Path,
) -> None:
    feedback_path = tmp_path / "run_feedback.db"
    feedback_path.write_bytes(b"not-a-valid-sqlite-database")
    app = create_automation_api_app(
        api_key=API_KEY,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        feedback_path=feedback_path,
    )
    app.config["TESTING"] = True
    client = app.test_client()
    _create_job(client)

    response = client.get(
        "/api/v1/automation/jobs",
        headers=HEADERS,
    )

    assert response.status_code == 503
    assert "无法读取监管反馈存储" in response.get_json()["error"]


def test_automation_api_retries_failed_job(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    created = _create_job(client).get_json()
    queue = AutomationQueue(tmp_path / "queue")
    pending_source = next(queue.pending.glob("*.xlsx"))
    claimed = queue.claim(pending_source)
    failed = queue.finish(claimed, succeeded=False)
    metadata = read_queue_job_metadata(failed)
    metadata["status"] = "failed"
    metadata["error"] = "temporary failure"
    write_queue_job_metadata(failed, metadata)

    enabled = client.patch(
        "/api/v1/automation/control",
        headers=HEADERS,
        json={"enabled": True},
    )
    assert enabled.status_code == 200

    response = client.post(
        f"/api/v1/automation/jobs/{created['job_id']}/retry",
        headers=HEADERS,
    )

    assert response.status_code == 202
    retried = response.get_json()
    assert retried["status"] == "pending"
    assert retried["error"] == ""
    assert len(list(queue.pending.glob("*.xlsx"))) == 1


def test_automation_control_gates_manual_runs_and_prevents_duplicates(
    tmp_path: Path,
) -> None:
    started = threading.Event()
    release = threading.Event()

    def fake_run_worker(actor: str) -> dict[str, object]:
        assert actor == "管理员"
        started.set()
        assert release.wait(timeout=2)
        return {"status": "completed", "fetched_records": 2, "queued_jobs": 1}

    app = create_automation_api_app(
        api_key=API_KEY,
        queue_root=tmp_path / "queue",
        output_root=tmp_path / "runs",
        run_worker=fake_run_worker,
    )
    app.config["TESTING"] = True
    client = app.test_client()

    control = client.get("/api/v1/automation/control", headers=HEADERS)

    assert control.status_code == 200
    assert control.get_json()["enabled"] is False
    assert control.get_json()["timezone"] == "Asia/Shanghai"

    disabled_run = client.post(
        "/api/v1/automation/runs",
        headers=HEADERS,
        json={"actor": "管理员"},
    )

    assert disabled_run.status_code == 409
    assert "总开关" in disabled_run.get_json()["error"]

    updated = client.patch(
        "/api/v1/automation/control",
        headers=HEADERS,
        json={"enabled": True, "schedule_enabled": True, "schedule_time": "02:30"},
    )

    assert updated.status_code == 200
    assert updated.get_json()["enabled"] is True
    assert updated.get_json()["schedule_enabled"] is True
    assert updated.get_json()["schedule_time"] == "02:30"

    launched = client.post(
        "/api/v1/automation/runs",
        headers=HEADERS,
        json={"actor": "管理员"},
    )

    assert launched.status_code == 202
    assert started.wait(timeout=1)

    duplicate = client.post(
        "/api/v1/automation/runs",
        headers=HEADERS,
        json={"actor": "管理员"},
    )
    assert duplicate.status_code == 409
    assert "运行" in duplicate.get_json()["error"]

    release.set()
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = client.get("/api/v1/automation/control", headers=HEADERS).get_json()
        if not status["running"]:
            break
        time.sleep(0.02)

    assert status["running"] is False
    assert status["last_run"]["status"] == "completed"


def test_automation_manual_run_refuses_an_already_processing_queue_job(
    tmp_path: Path,
) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    created = _create_job(client).get_json()
    queue = AutomationQueue(tmp_path / "queue")
    pending_source = next(queue.pending.glob("*.xlsx"))
    processing_source = queue.claim(pending_source)
    metadata = read_queue_job_metadata(processing_source)
    metadata["job_id"] = created["job_id"]
    metadata["status"] = "processing"
    write_queue_job_metadata(processing_source, metadata)

    enabled = client.patch(
        "/api/v1/automation/control",
        headers=HEADERS,
        json={"enabled": True},
    )
    assert enabled.status_code == 200

    response = client.post(
        "/api/v1/automation/runs",
        headers=HEADERS,
        json={"actor": "管理员"},
    )

    assert response.status_code == 409
    assert "运行" in response.get_json()["error"]


def test_automation_api_downloads_run_artifact(tmp_path: Path) -> None:
    app = _app(tmp_path)
    client = app.test_client()
    created = _create_job(client).get_json()
    queue = AutomationQueue(tmp_path / "queue")
    source = next(queue.pending.glob("*.xlsx"))
    metadata = read_queue_job_metadata(source)

    store = AutomationRunStore(tmp_path / "runs")
    manifest = store.create(source.name, "", {})
    artifact_dir = Path(manifest["run_dir"]) / "artifacts"
    artifact_dir.mkdir(parents=True)
    artifact = artifact_dir / "cz_candidate_sync.json"
    artifact.write_bytes(b'{"ready":1}')
    manifest["artifacts"]["cz_candidate_sync"] = str(artifact)
    store.save(manifest)
    metadata["run_id"] = manifest["run_id"]
    metadata["status"] = "completed"
    metadata["artifacts"] = manifest["artifacts"]
    write_queue_job_metadata(source, metadata)

    response = client.get(
        (
            f"/api/v1/automation/jobs/{created['job_id']}"
            "/artifacts/cz_candidate_sync"
        ),
        headers=HEADERS,
    )

    assert response.status_code == 200
    assert response.data == b'{"ready":1}'
