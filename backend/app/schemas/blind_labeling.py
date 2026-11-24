"""Pydantic contracts for the multi-annotator blind-labeling workflow."""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


REPLAY_ISSUE_TYPES = frozenset({"retrieval_missed", "ranking", "answer_issue"})


class BlindLabelLearningReviewInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    evidence_version: str = Field(pattern=r"^[a-f0-9]{64}$")
    previous_review_id: int | None = Field(ge=1)
    issue_type: Literal["retrieval_missed", "ranking", "answer_issue", "knowledge_issue", "knowledge_gap", "exclude"]
    knowledge_id: str | None = Field(None, min_length=1, max_length=64)
    expected_answer: str = Field("", max_length=1000)

    @model_validator(mode="after")
    def require_review_evidence(self):
        if self.issue_type in REPLAY_ISSUE_TYPES and not self.knowledge_id:
            raise ValueError("回放样本必须关联正确且已发布的知识")
        if self.issue_type not in REPLAY_ISSUE_TYPES and self.knowledge_id:
            raise ValueError("仅可回放类型需要指定正确知识")
        if self.issue_type == "answer_issue" and not self.expected_answer:
            raise ValueError("答复问题必须填写脱敏后的预期答复要点")
        if self.issue_type != "answer_issue" and self.expected_answer:
            raise ValueError("只有答复问题可填写预期答复要点")
        return self


class BlindLabelAnnotationInput(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(..., alias="candidateRef", min_length=1, max_length=64)
    verdict: Literal["referable", "not_referable", "可参考", "不可参考"]
    reason_code: str = Field("", alias="reasonCode", max_length=64)
    reason: str = Field("", max_length=2000)

    @field_validator("verdict", mode="before")
    @classmethod
    def normalize_verdict(cls, value: Any) -> str:
        mapping = {
            "可参考": "referable",
            "不可参考": "not_referable",
            "helpful": "referable",
            "unhelpful": "not_referable",
        }
        return mapping.get(str(value).strip(), str(value).strip())


class BlindLabelSubmitRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    assignment_id: str | None = Field(default=None, alias="assignmentId", max_length=64)
    annotations: list[BlindLabelAnnotationInput] = Field(
        default_factory=list,
        min_length=1,
        max_length=3,
        alias="annotations",
    )
    note: str = Field("", max_length=2000)
    task_reason_code: str = Field("", alias="taskReasonCode", max_length=64)


class BlindLabelReleaseRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    assignment_id: str | None = Field(default=None, alias="assignmentId", max_length=64)
    reason: str = Field("", max_length=512)


class BlindLabelClaimRequest(BaseModel):
    """Optional JSON body for the POST claim compatibility endpoint."""

    target_count: Literal[50] = Field(50, alias="targetCount")

    model_config = ConfigDict(populate_by_name=True)


class BlindLabelPublicCandidate(BaseModel):
    candidate_ref: str = Field(alias="candidateRef")
    rank: int
    title: str = ""
    content: Any = None
    category: str = ""

    model_config = ConfigDict(populate_by_name=True)


class BlindLabelPrivateCandidate(BlindLabelPublicCandidate):
    knowledge_id: str | None = Field(default=None, alias="knowledgeId")
    category_id: str | None = Field(default=None, alias="categoryId")
    knowledge_origin: str | None = Field(default=None, alias="knowledgeOrigin")
    embedding_score: float | None = Field(default=None, alias="embeddingScore")
    rerank_score: float | None = Field(default=None, alias="rerankScore")
    final_score: float | None = Field(default=None, alias="finalScore")
    selected: bool = False


class BlindLabelAssignmentSummary(BaseModel):
    id: str
    work_order_id: str = Field(alias="workOrderId")
    batch_id: str = Field(alias="batchId")
    conversation_id: str = Field(alias="conversationId")
    query: str = ""
    category: str = ""
    candidate_count: int = Field(alias="candidateCount")
    status: str
    assigned_at: datetime | None = Field(default=None, alias="assignedAt")
    started_at: datetime | None = Field(default=None, alias="startedAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")
    annotation_count: int = Field(default=0, alias="annotationCount")
    note: str = ""
    task_reason_code: str = Field(default="", alias="taskReasonCode")

    model_config = ConfigDict(populate_by_name=True)


class BlindLabelBatchSummary(BaseModel):
    id: str
    status: str
    target_count: int = Field(alias="targetCount")
    assigned_count: int = Field(alias="assignedCount")
    completed_count: int = Field(alias="completedCount")
    in_progress_count: int = Field(alias="inProgressCount")
    released_count: int = Field(alias="releasedCount")
    created_at: datetime | None = Field(default=None, alias="createdAt")
    completed_at: datetime | None = Field(default=None, alias="completedAt")

    model_config = ConfigDict(populate_by_name=True)


class BlindLabelMyBatchResponse(BaseModel):
    batch: BlindLabelBatchSummary | None = None
    items: list[BlindLabelAssignmentSummary]
    total: int


class BlindLabelAssignmentDetail(BaseModel):
    assignment: BlindLabelAssignmentSummary
    candidates: list[BlindLabelPublicCandidate]
    annotations: list[BlindLabelAnnotationInput] = Field(default_factory=list)


class BlindLabelSubmitResponse(BaseModel):
    status: Literal["recorded"]
    assignment_id: str = Field(alias="assignmentId")
    batch: BlindLabelBatchSummary | None = None
    annotation_count: int = Field(alias="annotationCount")

    model_config = ConfigDict(populate_by_name=True)


class BlindLabelConsensus(BaseModel):
    status: Literal["pending", "majority", "no_majority", "consensus", "unanimous", "arbitrated"]
    completed_annotators: int = Field(alias="completedAnnotators")
    verdict: Literal["referable", "not_referable"] | None = None
    referable_count: int = Field(default=0, alias="referableCount")
    not_referable_count: int = Field(default=0, alias="notReferableCount")
    needs_arbitration: bool = Field(alias="needsArbitration")
    candidate_results: list[dict[str, Any]] = Field(
        default_factory=list,
        alias="candidateResults",
    )

    model_config = ConfigDict(populate_by_name=True)
