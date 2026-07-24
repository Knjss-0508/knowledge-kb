from pathlib import Path

from scripts.scan_sensitive_files import scan


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_dify_runtime_volume_is_ignored_only_for_local_runtime(
    tmp_path: Path,
) -> None:
    runtime_db = (
        tmp_path
        / "tools"
        / "dify"
        / "dify-1.15.0"
        / "docker"
        / "volumes"
        / "weaviate"
        / "schema.db"
    )
    runtime_db.parent.mkdir(parents=True)
    runtime_db.write_bytes(b"runtime")

    assert scan(tmp_path, ignore_local_runtime=True) == []
    assert scan(tmp_path, ignore_local_runtime=False) == [
        f"forbidden artifact: {runtime_db.relative_to(tmp_path)}"
    ]


def test_non_runtime_database_under_tools_is_still_rejected(
    tmp_path: Path,
) -> None:
    database = tmp_path / "tools" / "dify" / "export.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"not-runtime")

    assert scan(tmp_path, ignore_local_runtime=True) == [
        f"forbidden artifact: {database.relative_to(tmp_path)}"
    ]


def test_codex_stage_artifacts_are_always_ignored(tmp_path: Path) -> None:
    database = tmp_path / ".codex_stage" / "test-run" / "audit.db"
    database.parent.mkdir(parents=True)
    database.write_bytes(b"temporary")

    assert scan(tmp_path, ignore_local_runtime=False) == []
    assert scan(tmp_path, ignore_local_runtime=True) == []


def test_streamlit_runtime_logs_are_ignored_only_for_local_runtime(
    tmp_path: Path,
) -> None:
    runtime_log = tmp_path / "streamlit-d-stderr.log"
    runtime_log.write_text("local runtime output", encoding="utf-8")

    assert scan(tmp_path, ignore_local_runtime=True) == []
    assert scan(tmp_path, ignore_local_runtime=False) == [
        f"forbidden artifact: {runtime_log.relative_to(tmp_path)}"
    ]


def test_unrelated_log_is_still_rejected_for_local_runtime(
    tmp_path: Path,
) -> None:
    application_log = tmp_path / "application.log"
    application_log.write_text("must not be uploaded", encoding="utf-8")

    assert scan(tmp_path, ignore_local_runtime=True) == [
        f"forbidden artifact: {application_log.relative_to(tmp_path)}"
    ]


def test_github_publish_script_excludes_local_dify_runtime() -> None:
    script = (
        PROJECT_ROOT / "scripts" / "publish_answer_hub_to_github.ps1"
    ).read_text(encoding="utf-8")

    assert 'Join-Path $SourcePath "tools\\dify"' in script
    assert '"dify"' in script
    assert '"*.egg-info"' in script
    assert '"codex_test_*.patch"' in script
    assert '"update_pr12_with_cz_interfaces.ps1"' in script
    assert '"更新PR12合并CZ接口.cmd"' in script
    for forbidden_pattern in (
        '"*.db"',
        '"*.log"',
        '"*.sqlite"',
        '"*.sqlite3"',
    ):
        assert forbidden_pattern in script
