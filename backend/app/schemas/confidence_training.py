from typing import Any, Literal

from pydantic import BaseModel, Field


ConfidenceBand = Literal["B0", "B1", "B2", "B3"]
TruthStatus = Literal["pending", "confirmed", "needs_adjudication", "not_evaluable"]
Correctness = Literal[
    "strict_correct",
    "partially_correct",
    "wrong",
    "not_evaluable",
]
TrainingCandidateStatus = Literal[
    "not_recommended",
    "recommended",
    "qualified",
    "excluded",
]


class ConfidenceTrainingUpdate(BaseModel):
    human_label: str | None = Field(None, max_length=64)
    truth_status: TruthStatus | None = None
    model_correctness: Correctness | None = None
    strict_correct: bool | None = None
    acceptable_correct: bool | None = None
    error_type: list[str] = Field(default_factory=list, max_length=20)
    error_severity: str | None = Field(None, max_length=32)
    review_note: str = Field("", max_length=4000)
    training_candidate_status: TrainingCandidateStatus = "not_recommended"
    dataset_split: str | None = Field(None, max_length=32)


class ConfidenceTrainingSettingsUpdate(BaseModel):
    training_candidate_collection_enabled: bool = True
    training_snapshot_enabled: bool = False
    auto_training_job_enabled: bool = False
    production_auto_review_enabled: bool = False


class ConfidenceTrainingSettings(BaseModel):
    training_candidate_collection_enabled: bool
    training_snapshot_enabled: bool
    auto_training_job_enabled: bool
    production_auto_review_enabled: bool
    training_task_available: bool
    training_task_reason: str
    model_name: str
    evaluation_scope: str


class ConfidenceTrainingJob(BaseModel):
    id: str
    status: str
    stage: str
    model_name: str
    evaluation_scope: str
    dataset_hash: str
    sample_count: int
    train_count: int
    validation_count: int
    test_count: int
    requested_by: str
    error_message: str = ""
    analysis_result: dict[str, Any] = Field(default_factory=dict)
    candidate_prompt: str = ""
    resolved_model_version: str = ""
    shadow_evaluation: dict[str, Any] = Field(default_factory=dict)
    regression_reviews: dict[str, Any] = Field(default_factory=dict)
    prompt_versions: list[dict[str, Any]] = Field(default_factory=list)
    created_at: str
    updated_at: str


RegressionClassification = Literal[
    "candidate_prompt_error",
    "human_truth_correction_required",
    "boundary_rule_missing",
    "technical_exception",
    "exclude_from_evaluation",
]


class ConfidenceRegressionReviewUpdate(BaseModel):
    classification: RegressionClassification
    note: str = Field(..., min_length=1, max_length=4000)
    keep_as_regression_case: bool = True


class ConfidenceTrainingItem(BaseModel):
    id: str
    event_id: str
    title: str
    model_label: str | None = None
    model_confidence: float | None = None
    confidence_band: str | None = None
    model_version: str | None = None
    prompt_version: str | None = None
    human_label: str | None = None
    human_draft_disposition: str | None = None
    model_draft_disposition: str | None = None
    truth_status: str
    model_correctness: str
    strict_correct: bool | None = None
    acceptable_correct: bool | None = None
    error_type: list[str] = Field(default_factory=list)
    error_severity: str | None = None
    review_note: str = ""
    correction_status: str = "not_started"
    correction_round: int = 0
    corrected_model_review: dict[str, Any] | None = None
    training_candidate_status: str
    dataset_split: str | None = None
    review_status: str


class ConfidenceTrainingOverview(BaseModel):
    total: int
    scored: int
    annotated: int
    valid_truth: int
    pending: int
    strict_accuracy: float | None = None
    acceptable_accuracy: float | None = None
    coverage: float
    not_evaluable_rate: float
    fatal_error_rate: float
    training_candidate_count: int
    model: str
    evaluation_scope: str
    production_auto_review_enabled: bool
    bands: list[dict[str, Any]]
    split_summary: list[dict[str, Any]] = []
    partitioned_bands: list[dict[str, Any]] = []
    settings: ConfidenceTrainingSettings
