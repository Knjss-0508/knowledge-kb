from __future__ import annotations

import json
from pathlib import Path

import answer_hub.automation as automation
import answer_hub.cli as cli


def _write_input_files(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.xlsx"
    standards = tmp_path / "standards.json"
    source.write_bytes(b"source")
    standards.write_text("[]", encoding="utf-8")
    return source, standards


def test_automation_run_store_retries_transient_windows_replace_error(
    tmp_path: Path,
    monkeypatch,
) -> None:
    original_replace = Path.replace
    replace_attempts = 0

    def transiently_locked_replace(source: Path, target: Path) -> Path:
        nonlocal replace_attempts
        replace_attempts += 1
        if replace_attempts < 3:
            raise PermissionError(
                5,
                "拒绝访问。",
                str(source),
                str(target),
            )
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", transiently_locked_replace)

    store = automation.AutomationRunStore(tmp_path / "runs")
    manifest = store.create("source.xlsx", "", {"clustering_mode": "direct_mimo"})

    assert replace_attempts == 3
    assert store.load(manifest["run_id"])["run_id"] == manifest["run_id"]
    assert not list((tmp_path / "runs").rglob("*.tmp"))


def test_automation_pipeline_persists_successful_run(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)

    def fake_initial_label_from_workbook(**kwargs):
        callback = kwargs["progress_callback"]
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for stage_id in (
            "load_input",
            "preprocess",
            "semantic_label",
            "topic_build",
            "export_review",
        ):
            callback(stage_id, "running", f"{stage_id} running", {})
            callback(stage_id, "completed", f"{stage_id} completed", {"topic_rows": 2})
        review = artifact_dir / "review_queue.xlsx"
        topic_review = artifact_dir / "topic_review_queue.xlsx"
        candidate = artifact_dir / "candidate_knowledge.xlsx"
        summary_path = artifact_dir / "summary.json"
        for path in (review, topic_review, candidate, summary_path):
            path.write_bytes(b"artifact")
        return {
            "output_file": str(review),
            "topic_review_file": str(topic_review),
            "candidate_output_file": str(candidate),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 4,
            "eligible_rows": 3,
            "topic_rows": 2,
            "evidence_gap_rows": 1,
            "excluded_rows": 0,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )
    output_root = tmp_path / "runs"
    manifest = automation.run_automation_pipeline(
        source,
        standards,
        output_root,
        use_mimo=False,
        clustering_mode="rule",
    )

    assert manifest["status"] == "review_pending"
    assert manifest["terminology"]["loaded"] is True
    assert manifest["terminology"]["entry_count"] > 0
    assert manifest["terminology"]["version"].startswith("terminology-")
    assert all(stage["status"] == "completed" for stage in manifest["stages"])
    assert Path(manifest["artifacts"]["topic_review"]).is_file()
    persisted = automation.AutomationRunStore(output_root).load(manifest["run_id"])
    assert persisted["summary"]["topic_rows"] == 2
    assert automation.list_automation_runs(output_root)[0]["run_id"] == manifest["run_id"]


def test_automation_pipeline_blocks_cz_delivery_when_clustering_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)
    monkeypatch.setenv("ANSWER_HUB_CLUSTER_FAILURE_ABORT_RATIO", "0.5")

    def fake_initial_label_from_workbook(**kwargs):
        callback = kwargs["progress_callback"]
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for stage_id in (
            "load_input",
            "preprocess",
            "semantic_label",
            "topic_build",
            "export_review",
        ):
            callback(stage_id, "running", "running", {})
            callback(stage_id, "completed", "completed", {})
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 4,
            "eligible_rows": 4,
            "topic_rows": 4,
            "direct_cluster_calls": 2,
            "direct_cluster_failed": 2,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        standards,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="direct_mimo",
    )

    assert manifest["status"] == "failed"
    assert manifest["summary"]["cluster_failure_guard_triggered"] is True
    assert manifest["summary"]["cluster_failure_ratio"] == 1.0
    assert manifest["summary"]["cz_candidate_sync_blocked"] is True
    assert "聚类失败保护" in manifest["error"]
    summary_path = Path(manifest["run_dir"]) / "artifacts" / "summary.json"
    persisted_summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert persisted_summary["cz_candidate_sync_blocked"] is True
    stages = {stage["id"]: stage for stage in manifest["stages"]}
    assert stages["topic_cluster"]["status"] == "failed"


def test_cluster_failure_retry_reexecutes_clustering_from_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)
    output_root = tmp_path / "runs"
    calls: list[bool] = []

    def fake_initial_label_from_workbook(**kwargs):
        calls.append(bool(kwargs.get("resume")))
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        if kwargs.get("resume"):
            checkpoint = json.loads(
                (artifact_dir / "workflow_checkpoint.json").read_text(
                    encoding="utf-8"
                )
            )
            assert checkpoint["stage"] == "semantic_label"
            return {
                "output_file": str(artifact_dir / "review_queue.xlsx"),
                "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
                "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
                "audit_db": str(tmp_path / "audit.db"),
                "source_total_rows": 1,
                "eligible_rows": 1,
                "topic_rows": 1,
                "direct_cluster_calls": 1,
                "direct_cluster_failed": 0,
            }
        (artifact_dir / "workflow_checkpoint.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "stage": "export_review",
                    "run_id": "cluster-retry",
                    "feature_rows": [{"id": "row-1"}],
                    "topic_summary": {
                        "cluster_admission_enforced": True,
                        "cluster_admission_policy_version": "cluster-admission-v1",
                        "cluster_admission_min_confidence": 0.75,
                        "clustering_requested_mode": "direct_mimo",
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
            "direct_cluster_calls": 1,
            "direct_cluster_failed": 1,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )
    failed = automation.run_automation_pipeline(
        source,
        standards,
        output_root,
        use_mimo=False,
        clustering_mode="direct_mimo",
    )
    assert failed["status"] == "failed"

    resumed = automation.resume_automation_pipeline(output_root, failed["run_id"])

    assert calls == [False, True]
    assert resumed["status"] == "review_pending"


def test_automation_pipeline_tracks_topic_substages_and_activity_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)

    def fake_initial_label_from_workbook(**kwargs):
        callback = kwargs["progress_callback"]
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        callback(
            "topic_build",
            "running",
            "正在拆分原子问题。",
            {"pipeline_phase": "topic_cluster"},
        )
        callback(
            "topic_build",
            "running",
            "正在执行聚类准入和价值分类。",
            {"pipeline_phase": "topic_enrichment"},
        )
        callback(
            "topic_build",
            "running",
            "正在进行知识转写与内容初审。",
            {"pipeline_phase": "knowledge_transcription"},
        )
        callback(
            "topic_build",
            "completed",
            "主题处理完成。",
            {"topic_rows": 1},
        )
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
            "evidence_gap_rows": 0,
            "excluded_rows": 0,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        standards,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    stages = {stage["id"]: stage for stage in manifest["stages"]}
    assert stages["topic_cluster"]["status"] == "completed"
    assert stages["topic_enrichment"]["status"] == "completed"
    assert stages["knowledge_transcription"]["status"] == "completed"
    activity_stage_ids = [
        item["stage_id"] for item in manifest["activity_history"]
    ]
    assert "topic_cluster" in activity_stage_ids
    assert "topic_enrichment" in activity_stage_ids
    assert "knowledge_transcription" in activity_stage_ids


def test_automation_failure_marks_only_current_topic_substage_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)

    def fail_during_transcription(**kwargs):
        callback = kwargs["progress_callback"]
        callback(
            "topic_build",
            "running",
            "正在拆分原子问题。",
            {"pipeline_phase": "topic_cluster"},
        )
        callback(
            "topic_build",
            "running",
            "正在执行聚类准入和价值分类。",
            {"pipeline_phase": "topic_enrichment"},
        )
        callback(
            "topic_build",
            "running",
            "正在进行知识转写与内容初审。",
            {"pipeline_phase": "knowledge_transcription"},
        )
        raise RuntimeError("transcription failed")

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fail_during_transcription,
    )

    manifest = automation.run_automation_pipeline(
        source,
        standards,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    stages = {stage["id"]: stage for stage in manifest["stages"]}
    assert manifest["status"] == "failed"
    assert stages["topic_cluster"]["status"] == "completed"
    assert stages["topic_enrichment"]["status"] == "interrupted"
    assert stages["knowledge_transcription"]["status"] == "failed"
    assert manifest["current_activity"]["stage_id"] == (
        "knowledge_transcription"
    )
    assert manifest["current_activity"]["status"] == "failed"


def test_automation_pipeline_marks_active_stage_failed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)

    def fake_initial_label_from_workbook(**kwargs):
        kwargs["progress_callback"](
            "load_input",
            "running",
            "reading source",
            {},
        )
        raise RuntimeError("invalid workbook")

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )
    manifest = automation.run_automation_pipeline(
        source,
        standards,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    assert manifest["status"] == "failed"
    assert manifest["terminology"]["loaded"] is True
    assert manifest["error"] == "invalid workbook"
    load_stage = next(
        stage for stage in manifest["stages"] if stage["id"] == "load_input"
    )
    assert load_stage["status"] == "failed"
    assert load_stage["detail"] == "invalid workbook"


def test_automation_pipeline_runs_without_standard_file(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"source")
    captured: dict[str, object] = {}

    def fake_initial_label_from_workbook(**kwargs):
        captured.update(kwargs)
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
            "evidence_gap_rows": 0,
            "excluded_rows": 0,
            "standard_references_enabled": False,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="rule",
    )

    assert manifest["status"] == "review_pending"
    assert manifest["standards_name"] == ""
    assert manifest["options"]["use_standard_references"] is False
    assert captured["standards_path"] is None
    assert captured["use_standard_references"] is False
    assert captured["enforce_cluster_admission"] is True
    assert not (Path(manifest["run_dir"]) / "inputs" / "standards.xlsx").exists()


def test_automation_pipeline_passes_cluster_only_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"source")
    captured: dict[str, object] = {}

    def fake_initial_label_from_workbook(**kwargs):
        captured.update(kwargs)
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        result = artifact_dir / "cluster_result.xlsx"
        result.write_bytes(b"cluster-only")
        return {
            "output_file": str(result),
            "topic_review_file": str(result),
            "candidate_output_file": "",
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 2,
            "eligible_rows": 2,
            "cluster_only": True,
            "cluster_rows": 1,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        None,
        tmp_path / "runs",
        use_mimo=False,
        clustering_mode="direct_mimo",
        cluster_only=True,
    )

    assert manifest["status"] == "review_pending"
    assert manifest["options"]["cluster_only"] is True
    assert manifest["options"]["cluster_media_policy"] == "never"
    assert captured["cluster_only"] is True
    assert captured["cluster_media_policy"] == "never"
    assert captured["enforce_cluster_admission"] is False
    assert manifest["artifacts"]["cluster_result"].endswith("cluster_result.xlsx")
    assert "candidate_knowledge" not in manifest["artifacts"]


def test_automation_cli_passes_cluster_only_mode(
    tmp_path: Path,
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_automation_pipeline(**kwargs):
        captured.update(kwargs)
        return {"status": "review_pending"}

    monkeypatch.setattr(
        cli,
        "run_automation_pipeline",
        fake_run_automation_pipeline,
    )

    exit_code = cli.main(
        [
            "automate",
            "--source",
            str(tmp_path / "source.xlsx"),
            "--cluster-only",
        ]
    )

    assert exit_code == 0
    assert captured["cluster_only"] is True


def test_automation_pipeline_waits_for_confirmation_when_mimo_preflight_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"source")

    def fail_preflight() -> dict[str, object]:
        raise automation.MimoPreflightError("MiMo HTTP 401: unauthorized")

    def unexpected_initial_label_from_workbook(**_kwargs):
        raise AssertionError("generation must wait for human confirmation")

    monkeypatch.setattr(automation, "run_mimo_preflight", fail_preflight)
    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        unexpected_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        None,
        tmp_path / "runs",
        use_mimo=True,
        clustering_mode="direct_mimo",
    )

    assert manifest["status"] == "needs_confirmation"
    assert "MiMo API 预检失败" in manifest["error"]
    assert manifest["summary"]["mimo_preflight"]["passed"] is False
    assert "MiMo HTTP 401" in manifest["summary"]["mimo_preflight"]["error"]
    assert manifest["alerts"]


def test_automation_pipeline_continues_with_rule_fallback_after_confirmation(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source = tmp_path / "source.xlsx"
    source.write_bytes(b"source")
    captured: dict[str, object] = {}

    def fail_preflight() -> dict[str, object]:
        raise automation.MimoPreflightError("all configured MiMo keys failed")

    def fake_initial_label_from_workbook(**kwargs):
        captured.update(kwargs)
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
            "topic_signal_fallback_rows": 1,
            "standard_references_enabled": False,
        }

    monkeypatch.setattr(automation, "run_mimo_preflight", fail_preflight)
    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fake_initial_label_from_workbook,
    )

    manifest = automation.run_automation_pipeline(
        source,
        None,
        tmp_path / "runs",
        use_mimo=True,
        clustering_mode="direct_mimo",
        continue_on_mimo_unavailable=True,
    )

    assert manifest["status"] == "review_pending"
    assert captured["use_mimo"] is False
    assert captured["clustering_mode"] == "rule"
    assert captured["enforce_cluster_admission"] is True
    assert manifest["summary"]["mimo_preflight"] == {
        "passed": False,
        "error": "all configured MiMo keys failed",
        "continued_with_rule_fallback": True,
    }
    assert any("人工确认" in alert for alert in manifest["alerts"])


def test_failed_automation_run_can_resume_from_checkpoint(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)
    output_root = tmp_path / "runs"

    def fail_initial_label(**kwargs):
        artifact_dir = Path(kwargs["output_dir"])
        artifact_dir.mkdir(parents=True, exist_ok=True)
        (artifact_dir / "workflow_checkpoint.json").write_text(
            '{"version":1,"stage":"semantic_label","run_id":"resume-run",'
            '"selected_rows":[],"preprocessed_rows":[],"eligible_rows":[],'
            '"eligible_raw_rows":[],"excluded_rows":[],"feature_rows":[]}',
            encoding="utf-8",
        )
        kwargs["progress_callback"](
            "topic_build",
            "running",
            "topic build",
            {},
        )
        raise RuntimeError("temporary model failure")

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        fail_initial_label,
    )
    failed = automation.run_automation_pipeline(
        source,
        standards,
        output_root,
        use_mimo=False,
        clustering_mode="rule",
    )
    failed["stages"] = [
        stage
        for stage in failed["stages"]
        if stage["id"]
        not in {
            "topic_cluster",
            "topic_enrichment",
            "knowledge_transcription",
        }
    ]
    failed.pop("current_activity", None)
    failed.pop("activity_history", None)
    automation.AutomationRunStore(output_root).save(failed)

    captured: dict[str, object] = {}

    def resume_initial_label(**kwargs):
        captured.update(kwargs)
        artifact_dir = Path(kwargs["output_dir"])
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        kwargs["progress_callback"](
            "topic_build",
            "completed",
            "restored",
            {"topic_rows": 1},
        )
        kwargs["progress_callback"](
            "export_review",
            "completed",
            "exported",
            {"topic_rows": 1},
        )
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
            "topic_signal_fallback_rows": 0,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        resume_initial_label,
    )
    resumed = automation.resume_automation_pipeline(
        output_root,
        failed["run_id"],
    )

    assert resumed["status"] == "review_pending"
    assert resumed["terminology"]["loaded"] is True
    assert resumed["terminology"]["entry_count"] > 0
    assert resumed["attempt_count"] == 2
    assert resumed["retry_history"][0]["error"] == "temporary model failure"
    assert captured["resume"] is True
    resumed_stages = {stage["id"]: stage for stage in resumed["stages"]}
    assert resumed_stages["topic_cluster"]["status"] == "completed"
    assert resumed_stages["topic_enrichment"]["status"] == "completed"
    assert resumed_stages["knowledge_transcription"]["status"] == "completed"


def test_interrupted_running_run_can_resume_when_explicitly_allowed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    source, standards = _write_input_files(tmp_path)
    output_root = tmp_path / "runs"
    store = automation.AutomationRunStore(output_root)
    manifest = store.create(source.name, standards.name, {"clustering_mode": "direct_mimo"})
    run_dir = Path(manifest["run_dir"])
    input_dir = run_dir / "inputs"
    artifact_dir = run_dir / "artifacts"
    input_dir.mkdir(parents=True, exist_ok=True)
    artifact_dir.mkdir(parents=True, exist_ok=True)
    (input_dir / source.name).write_bytes(b"source")
    (input_dir / standards.name).write_text("[]", encoding="utf-8")
    manifest["stages"][3]["status"] = "completed"
    manifest["stages"][4]["status"] = "running"
    store.save(manifest)

    captured: dict[str, object] = {}

    def resume_initial_label(**kwargs):
        captured.update(kwargs)
        artifact_dir = Path(kwargs["output_dir"])
        for filename in (
            "review_queue.xlsx",
            "topic_review_queue.xlsx",
            "candidate_knowledge.xlsx",
        ):
            (artifact_dir / filename).write_bytes(b"artifact")
        return {
            "output_file": str(artifact_dir / "review_queue.xlsx"),
            "topic_review_file": str(artifact_dir / "topic_review_queue.xlsx"),
            "candidate_output_file": str(artifact_dir / "candidate_knowledge.xlsx"),
            "audit_db": str(tmp_path / "audit.db"),
            "source_total_rows": 1,
            "eligible_rows": 1,
            "topic_rows": 1,
        }

    monkeypatch.setattr(
        automation,
        "initial_label_from_workbook",
        resume_initial_label,
    )

    resumed = automation.resume_automation_pipeline(
        output_root,
        manifest["run_id"],
        allow_interrupted_running=True,
    )

    assert resumed["status"] == "review_pending"
    assert resumed["attempt_count"] == 2
    assert resumed["retry_history"][0]["failed_stage"] == "topic_build"
    assert captured["resume"] is True
