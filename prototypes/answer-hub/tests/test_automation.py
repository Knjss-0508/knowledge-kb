from __future__ import annotations

from pathlib import Path

import answer_hub.automation as automation


def _write_input_files(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source.xlsx"
    standards = tmp_path / "standards.json"
    source.write_bytes(b"source")
    standards.write_text("[]", encoding="utf-8")
    return source, standards


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
    assert all(stage["status"] == "completed" for stage in manifest["stages"])
    assert Path(manifest["artifacts"]["topic_review"]).is_file()
    persisted = automation.AutomationRunStore(output_root).load(manifest["run_id"])
    assert persisted["summary"]["topic_rows"] == 2
    assert automation.list_automation_runs(output_root)[0]["run_id"] == manifest["run_id"]


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
    assert not (Path(manifest["run_dir"]) / "inputs" / "standards.xlsx").exists()
