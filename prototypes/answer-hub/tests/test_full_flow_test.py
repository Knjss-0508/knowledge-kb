from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from answer_hub.automation_queue import (
    AutomationQueue,
    read_queue_job_metadata,
)
from answer_hub.full_flow_test import (
    FullFlowTestError,
    run_full_flow_test,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _configured_environment(tmp_path: Path) -> dict[str, str]:
    return {
        "MIMO_API_KEY": "test-mimo-key",
        "MIMO_BASE_URL": "https://mimo.example/v1",
        "MIMO_MODEL": "mimo-test",
        "KB_BASE_URL": "http://127.0.0.1:8801",
        "KB_INTEGRATION_KEY": "test-cz-key",
        "ANSWER_HUB_AUTOMATION_QUEUE": str(tmp_path / "queue"),
        "ANSWER_HUB_AUTOMATION_OUTPUT": str(tmp_path / "runs"),
    }


def test_full_flow_test_requires_real_model_and_cz_configuration(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")

    with pytest.raises(FullFlowTestError, match="缺少必要配置"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ={},
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_requires_primary_mimo_key_used_by_runtime(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    environment = _configured_environment(tmp_path)
    environment.pop("MIMO_API_KEY")
    environment["MIMO_API_KEYS"] = "backup-key"

    with pytest.raises(FullFlowTestError, match="MIMO_API_KEY"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=environment,
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_refuses_remote_cz_target(tmp_path: Path) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    environment = _configured_environment(tmp_path)
    environment["KB_BASE_URL"] = "http://111.230.109.227:8801"

    with pytest.raises(FullFlowTestError, match="仅允许连接本机 CZ"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=environment,
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_refuses_to_mix_with_existing_pending_jobs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    queue = AutomationQueue(tmp_path / "queue")
    queue.ensure()
    (queue.pending / "existing.xlsx").write_bytes(b"existing")

    with pytest.raises(FullFlowTestError, match="已有 1 个待处理任务"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=_configured_environment(tmp_path),
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_refuses_an_active_queue_runner(tmp_path: Path) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    queue = AutomationQueue(tmp_path / "queue")
    queue.ensure()
    queue.lock_path.write_text("active", encoding="utf-8")

    with pytest.raises(FullFlowTestError, match="自动化队列正在运行"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=_configured_environment(tmp_path),
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_refuses_existing_processing_work(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    queue = AutomationQueue(tmp_path / "queue")
    queue.ensure()
    (queue.processing / "existing.xlsx").write_bytes(b"processing")

    with pytest.raises(FullFlowTestError, match="正在处理 1 个任务"):
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=_configured_environment(tmp_path),
            ready_checker=lambda _base_url: None,
            processor=lambda *args, **kwargs: {},
        )


def test_full_flow_test_removes_new_pending_job_when_runner_crashes(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    queue = AutomationQueue(tmp_path / "queue")

    def failing_processor(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise RuntimeError("private candidate content")

    with pytest.raises(
        FullFlowTestError,
        match="自动化队列执行失败",
    ) as captured:
        run_full_flow_test(
            source,
            project_root=tmp_path,
            environ=_configured_environment(tmp_path),
            ready_checker=lambda _base_url: None,
            processor=failing_processor,
        )

    assert "private candidate content" not in str(captured.value)
    assert queue.candidates() == []
    assert list(queue.pending.glob("*.job.json")) == []


def test_full_flow_test_enqueues_and_processes_one_cz_review_job(
    tmp_path: Path,
) -> None:
    source = tmp_path / "sample.xlsx"
    source.write_bytes(b"xlsx")
    environment = _configured_environment(tmp_path)
    observed: dict[str, Any] = {}

    def ready_checker(base_url: str) -> None:
        observed["base_url"] = base_url

    def processor(
        queue_root: str | Path,
        standards_path: str | Path | None,
        output_root: str | Path,
        **kwargs: Any,
    ) -> dict[str, Any]:
        queue = AutomationQueue(queue_root)
        queued_source = queue.candidates()[0]
        metadata = read_queue_job_metadata(queued_source)
        observed["metadata"] = metadata
        observed["processor_kwargs"] = kwargs
        observed["standards_path"] = standards_path
        observed["output_root"] = Path(output_root)
        return {
            "attempted": 1,
            "succeeded": 1,
            "failed": 0,
            "results": [
                {
                    "job_id": metadata["job_id"],
                    "run_id": "run-full-flow-001",
                    "status": "completed",
                    "cz_candidate_sync": {
                        "queued": 2,
                        "ready": 0,
                        "rejected": 1,
                        "reused": 0,
                        "failed": 0,
                    },
                }
            ],
        }

    result = run_full_flow_test(
        source,
        project_root=tmp_path,
        environ=environment,
        ready_checker=ready_checker,
        processor=processor,
    )

    assert observed["base_url"] == "http://127.0.0.1:8801"
    assert observed["standards_path"] is None
    assert observed["output_root"] == tmp_path / "runs"
    assert observed["processor_kwargs"]["max_files"] == 1
    assert observed["processor_kwargs"]["submit_to_cz"] is True
    assert observed["processor_kwargs"]["acquire_lock"] is False
    assert observed["processor_kwargs"]["use_mimo"] is True
    assert observed["processor_kwargs"]["clustering_mode"] == "direct_mimo"
    metadata = observed["metadata"]
    assert metadata["options"]["sync_to_cz_review"] is True
    assert metadata["options"]["submit_to_cz"] is True
    assert metadata["options"]["continue_on_mimo_unavailable"] is False
    assert result == {
        "job_id": metadata["job_id"],
        "run_id": "run-full-flow-001",
        "status": "completed",
        "cz_candidate_sync": {
            "queued": 2,
            "ready": 0,
            "rejected": 1,
            "reused": 0,
            "failed": 0,
        },
    }


def test_windows_cmd_launcher_uses_ascii_and_crlf() -> None:
    launcher = (PROJECT_ROOT / "全流程测试上传.cmd").read_bytes()

    assert all(byte < 128 for byte in launcher)
    assert b"\r\n" in launcher
    assert b"\n" not in launcher.replace(b"\r\n", b"")
    assert b"powershell.exe -NoProfile -ExecutionPolicy Bypass" in launcher
    assert b"-SourcePath \"%~1\"" in launcher


def test_powershell_launcher_accepts_an_optional_source_path() -> None:
    launcher = (
        PROJECT_ROOT / "scripts" / "run_full_flow_test.ps1"
    ).read_text(encoding="utf-8-sig")

    assert '[string]$SourcePath = ""' in launcher
    assert "if ($SourcePath)" in launcher
    assert "Test-Path -LiteralPath $selectedSourcePath" in launcher
