from __future__ import annotations

from io import BytesIO
from pathlib import Path

from answer_hub.automation import AutomationRunStore
from answer_hub.automation_api import create_automation_api_app
from answer_hub.automation_queue import (
    AutomationQueue,
    read_queue_job_metadata,
    write_queue_job_metadata,
)


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

    response = client.post(
        f"/api/v1/automation/jobs/{created['job_id']}/retry",
        headers=HEADERS,
    )

    assert response.status_code == 202
    retried = response.get_json()
    assert retried["status"] == "pending"
    assert retried["error"] == ""
    assert len(list(queue.pending.glob("*.xlsx"))) == 1


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
