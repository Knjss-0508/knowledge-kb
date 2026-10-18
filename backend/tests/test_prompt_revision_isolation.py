"""Prompt 修订只使用开发分区，测试集复核记录仅用于人工审计。"""

from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.routes import confidence_training as routes
from app.services import model_annotation
from app.services.model_annotation import ModelAnnotationError


def _row(sample_id, split):
    return {
        "id": sample_id,
        "split": split,
        "title": f"{sample_id}-title",
        "human_label": f"{sample_id}-truth",
        "human_draft_disposition": f"{sample_id}-draft-truth",
        "baseline": {"strict_correct": True, "reason": f"{sample_id}-baseline"},
        "candidate": {"strict_correct": False, "reason": f"{sample_id}-candidate"},
    }


def _job(rows, *, target_split="validation", phases=None):
    return SimpleNamespace(
        id="isolation-job",
        status="shadow_validation_review_pending",
        stage="",
        error_message="",
        resolved_model_version="offline-model",
        analysis_result={"evaluation_scope": "shadow_only"},
        candidate_prompt="candidate-v1",
        prompt_versions=[{
            "version": "v1-candidate",
            "prompt": "candidate-v1",
            "status": "validation_pending",
        }],
        dataset_payload=[
            {"id": row["id"], "split": row["split"], "content": f'{row["id"]}-body'}
            for row in rows
        ],
        regression_reviews={
            f'v1-candidate:{row["id"]}': {
                "classification": "candidate_prompt_error",
                "note": f'{row["id"]}-human-answer-attribution',
                "keep_as_regression_case": True,
                "reviewed_by": "human-reviewer",
                "reviewed_at": "2026-10-18T00:00:00",
            }
            for row in rows
        },
        shadow_evaluation={
            "status": "completed",
            "target_split": target_split,
            "candidate_prompt_version": "v1-candidate",
            "comparison": {"test_only_metric": "heldout-aggregate-secret"},
            "rows": rows,
            "phase_results": phases or {},
        },
    )


def _mixed_job():
    validation = _row("validation-example", "validation")
    heldout = _row("heldout-secret", "test")
    job = _job([validation, heldout], target_split="test")
    job.shadow_evaluation["rows"] = [heldout]
    job.shadow_evaluation["phase_results"] = {
        "v1-candidate:validation": {
            "status": "completed",
            "target_split": "validation",
            "candidate_prompt_version": "v1-candidate",
            "rows": [validation],
        },
    }
    return job


def test_revision_excludes_test_truth_outputs_notes_and_aggregates_but_keeps_audit():
    job = _mixed_job()
    before = deepcopy(vars(job))

    snapshot = routes._prompt_revision_snapshot(job)
    serialized = json.dumps(snapshot, ensure_ascii=False)

    assert [case["id"] for case in snapshot["regression_cases"]] == ["validation-example"]
    case = snapshot["regression_cases"][0]
    assert case["human_truth"]["knowledge_value"] == "validation-example-truth"
    assert case["human_note"] == "validation-example-human-answer-attribution"
    assert case["baseline_output"]["reason"] == "validation-example-baseline"
    assert case["candidate_output"]["reason"] == "validation-example-candidate"
    assert "heldout-secret" not in serialized
    assert "heldout-aggregate-secret" not in serialized
    assert snapshot["evaluation_scope"] == "shadow_only"
    assert vars(job) == before
    audit = routes._current_regression_reviews(job)
    assert audit["heldout-secret"]["note"] == "heldout-secret-human-answer-attribution"
    assert {row["id"] for row in routes._regressed_rows(job)} == {
        "validation-example", "heldout-secret",
    }


@pytest.mark.parametrize("split", ["train", "validation"])
def test_revision_retains_reviewed_development_partition_cases(split):
    job = _job([_row("development-example", split)], target_split=split)

    assert routes._revision_preconditions(job) == (True, "")
    snapshot = routes._prompt_revision_snapshot(job)
    assert [case["id"] for case in snapshot["regression_cases"]] == ["development-example"]
    assert snapshot["regression_cases"][0]["human_truth"] == {
        "knowledge_value": "development-example-truth",
        "draft_disposition": "development-example-draft-truth",
    }


@pytest.mark.parametrize("split", ["test", "", None, "unknown"])
def test_revision_fails_closed_without_development_cases(split):
    job = _job([_row("heldout-secret", split)], target_split=split)
    before = deepcopy(vars(job))

    ok, reason = routes._revision_preconditions(job)

    assert ok is False
    assert "train/validation" in reason
    with pytest.raises(ModelAnnotationError, match="train/validation"):
        routes._prompt_revision_snapshot(job)
    assert vars(job) == before


@pytest.mark.parametrize("frozen_split", ["test", "", None])
def test_frozen_partition_cannot_be_overridden_by_a_validation_result_row(frozen_split):
    job = _job([_row("heldout-secret", "validation")])
    job.dataset_payload[0]["split"] = frozen_split

    ok, _ = routes._revision_preconditions(job)

    assert ok is False
    with pytest.raises(ModelAnnotationError):
        routes._prompt_revision_snapshot(job)


def test_unknown_sample_or_conflicting_frozen_membership_is_not_revision_material():
    job = _job([_row("heldout-secret", "validation")])
    job.dataset_payload.append({"id": "heldout-secret", "split": "test"})
    with pytest.raises(ModelAnnotationError):
        routes._prompt_revision_snapshot(job)

    job.dataset_payload = [{"id": "another-sample", "split": "validation"}]
    with pytest.raises(ModelAnnotationError):
        routes._prompt_revision_snapshot(job)


def test_missing_row_partition_uses_explicit_phase_partition():
    job = _job([_row("validation-example", "validation")])
    job.shadow_evaluation["rows"][0].pop("split")

    snapshot = routes._prompt_revision_snapshot(job)

    assert snapshot["regression_cases"][0]["split"] == "validation"


def test_test_phase_rows_without_a_partition_are_still_excluded():
    job = _mixed_job()
    job.shadow_evaluation["rows"][0].pop("split")

    snapshot = routes._prompt_revision_snapshot(job)

    assert "heldout-secret" not in json.dumps(snapshot)


def test_unreviewed_or_unretained_validation_cases_do_not_allow_revision():
    job = _mixed_job()
    job.regression_reviews["v1-candidate:validation-example"]["keep_as_regression_case"] = False

    ok, reason = routes._revision_preconditions(job)

    assert ok is False
    assert "train/validation" in reason


def test_truth_correction_still_requires_real_human_review():
    job = _job([_row("validation-example", "validation")])
    job.regression_reviews["v1-candidate:validation-example"]["classification"] = (
        "human_truth_correction_required"
    )

    ok, reason = routes._revision_preconditions(job)

    assert ok is False
    assert "候选价值复核" in reason


def test_test_review_still_controls_human_gate_without_entering_model_input():
    job = _mixed_job()
    review = job.regression_reviews.pop("v1-candidate:heldout-secret")

    ok, reason = routes._revision_preconditions(job)
    assert ok is False
    assert "未完成人工归因" in reason

    job.regression_reviews["v1-candidate:heldout-secret"] = {
        **review, "classification": "human_truth_correction_required",
    }
    ok, reason = routes._revision_preconditions(job)
    assert ok is False
    assert "候选价值复核" in reason

    job.regression_reviews["v1-candidate:heldout-secret"] = review
    assert routes._revision_preconditions(job) == (True, "")
    assert "heldout-secret" not in json.dumps(routes._prompt_revision_snapshot(job))
    assert routes._current_regression_reviews(job)["heldout-secret"] == review


class _Session:
    def __init__(self, job):
        self.job = job
        self.commits = 0
        self.closed = False

    def query(self, *_):
        return self

    def filter(self, *_):
        return self

    def first(self):
        return self.job

    def add(self, job):
        assert job is self.job

    def commit(self):
        self.commits += 1

    def close(self):
        self.closed = True


def test_worker_sends_only_development_cases_and_keeps_candidate_shadow_only(monkeypatch):
    job = _mixed_job()
    audit_before = deepcopy(job.regression_reviews)
    truth_before = deepcopy(job.dataset_payload)
    evaluation_before = deepcopy(job.shadow_evaluation)
    session = _Session(job)
    received = []

    def revise(prompt, *, purpose):
        assert purpose == "退化样本提示词修订"
        received.append(prompt)
        return {"candidate_prompt": "candidate-v1.1", "resolved_model_version": "offline-stub"}

    def unexpected_active_access(*args, **kwargs):
        pytest.fail("Prompt 修订不得访问或切换 active Prompt")

    monkeypatch.setattr(routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", revise)
    monkeypatch.setattr(routes, "resolve_active_confidence_prompt", unexpected_active_access)

    routes._run_prompt_revision(job.id)

    assert len(received) == 1
    assert "shadow_only" in received[0]
    assert "validation-example-human-answer-attribution" in received[0]
    assert "heldout-secret" not in json.dumps(received)
    assert "heldout-aggregate-secret" not in json.dumps(received)
    assert job.candidate_prompt == "candidate-v1.1"
    assert job.status == "validation_pending"
    assert job.prompt_versions[-1]["status"] == "validation_pending"
    assert "shadow_only" in job.error_message
    assert job.regression_reviews == audit_before
    assert job.dataset_payload == truth_before
    assert job.shadow_evaluation == evaluation_before
    assert session.commits == 1
    assert session.closed


def test_worker_does_not_call_generator_for_test_only_regressions(monkeypatch):
    job = _job([_row("heldout-secret", "test")], target_split="test")
    before_prompt = job.candidate_prompt
    before_versions = deepcopy(job.prompt_versions)
    before_audit = deepcopy(job.regression_reviews)
    session = _Session(job)

    def unexpected_model_call(*args, **kwargs):
        pytest.fail("仅有 test 退化时不得调用 Prompt 生成模型")

    monkeypatch.setattr(routes, "SessionLocal", lambda: session)
    monkeypatch.setattr(routes, "revise_confidence_training_prompt", unexpected_model_call)

    routes._run_prompt_revision(job.id)

    assert job.status == "prompt_revision_failed"
    assert "train/validation" in job.error_message
    assert job.candidate_prompt == before_prompt
    assert job.prompt_versions == before_versions
    assert job.regression_reviews == before_audit
    assert session.closed
