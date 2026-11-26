"""Evaluate manually reviewed final answers previously generated in isolation."""

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.orm import Session

from app.services.blind_label_learning import LearningReviewError, load_learning_review


_GRADED = {"correct", "incorrect"}


class AnswerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    generation: Literal["answered", "no_recommendation", "failed"]
    answer: str = Field("", max_length=4000)
    verdict: Literal["correct", "incorrect", "insufficient_evidence"] | None = None

    @model_validator(mode="after")
    def check_generation(self):
        if self.generation == "answered" and not self.answer:
            raise ValueError("成功生成时需提供脱敏答复")
        if self.generation != "answered" and (self.answer or self.verdict is not None):
            raise ValueError("没有推荐话术时不能附带答复或人工判定")
        return self


class AnswerReplayCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    work_order_id: str = Field(min_length=1, max_length=64)
    review_id: int = Field(gt=0)
    baseline: AnswerOutput | None = None
    candidate: AnswerOutput | None = None


class AnswerReplayManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    baseline_version: str = Field(max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    candidate_version: str = Field(max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
    cases: list[AnswerReplayCase] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_batch(self):
        if self.baseline_version == self.candidate_version:
            raise ValueError("基线和候选需使用不同的版本标识")
        if len({case.work_order_id for case in self.cases}) != len(self.cases):
            raise ValueError("同一工单不能重复计入答复验收")
        return self


def _review_reference(db: Session, case: AnswerReplayCase) -> dict:
    row = {"work_order_id": case.work_order_id, "review_id": case.review_id}
    invalid = {**row, "status": "review_changed_or_ineligible"}
    try:
        detail = load_learning_review(db, case.work_order_id)
    except LearningReviewError as exc:
        if exc.status_code != 409:
            raise
        return invalid

    review = detail.get("review") or {}
    if not detail["replay_eligible"] or review.get("id") != case.review_id:
        return invalid
    if review.get("issue_type") != "answer_issue":
        return {**row, "status": "not_answer_issue"}
    expected = (review.get("expected_answer") or "").strip()
    if not expected or not review.get("knowledge_id"):
        return {**row, "status": "missing_reference"}
    return {**row, "status": "eligible", "knowledge_id": review["knowledge_id"], "expected_answer": expected}


def _header(manifest: AnswerReplayManifest, scope: str) -> dict:
    return {
        "schema_version": 1,
        "scope": scope,
        "baseline_version": manifest.baseline_version,
        "candidate_version": manifest.candidate_version,
    }


def build_review_references(db: Session, manifest: AnswerReplayManifest) -> dict:
    """Explicit local reviewer export; contains private reference text."""
    return {
        **_header(manifest, "private_final_answer_references"),
        "cases": [_review_reference(db, case) for case in manifest.cases],
    }


def _answer_status(output: AnswerOutput | None) -> str:
    if output is None:
        return "missing_output"
    if output.generation == "failed":
        return "generation_failed"
    if output.generation == "no_recommendation":
        return "no_recommendation"
    return output.verdict or "unreviewed"


def _side_summary(rows: list[dict], side: str) -> dict:
    counts = Counter(row[side] for row in rows)
    evaluated = counts["correct"] + counts["incorrect"]
    return {
        "evaluated": evaluated,
        "not_evaluated": len(rows) - evaluated,
        "accuracy": counts["correct"] / evaluated if evaluated else None,
        "counts": dict(counts),
    }


def run_answer_replay(db: Session, manifest: AnswerReplayManifest) -> dict:
    """Summarize human verdicts without including the answers or reference text."""
    rows = []
    eligible = []
    for case in manifest.cases:
        row = _review_reference(db, case)
        row.pop("expected_answer", None)
        rows.append(row)
        if row["status"] == "eligible":
            row.update(baseline=_answer_status(case.baseline), candidate=_answer_status(case.candidate))
            eligible.append(row)

    pairs = [
        (row["baseline"], row["candidate"]) for row in eligible
        if row["baseline"] in _GRADED and row["candidate"] in _GRADED
    ]
    n = len(pairs)
    pair_counts = Counter(pairs)
    improved = pair_counts["incorrect", "correct"]
    regressed = pair_counts["correct", "incorrect"]
    both_correct = pair_counts["correct", "correct"]
    return {
        **_header(manifest, "isolated_final_answer_human_verdicts"),
        "summary": {
            "requested": len(rows),
            "eligible": len(eligible),
            "excluded": len(rows) - len(eligible),
            "baseline": _side_summary(eligible, "baseline"),
            "candidate": _side_summary(eligible, "candidate"),
            "paired": {
                "evaluated": n, "not_evaluated": len(eligible) - n,
                "improved": improved, "regressed": regressed, "unchanged": n - improved - regressed,
                "baseline_accuracy": (both_correct + regressed) / n if n else None,
                "candidate_accuracy": (both_correct + improved) / n if n else None,
                "accuracy_delta": (improved - regressed) / n if n else None,
            },
        },
        "cases": rows,
    }
