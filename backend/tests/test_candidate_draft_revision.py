from datetime import datetime
from types import SimpleNamespace

import pytest

from app.routes import integration
from app.schemas.integration import (
    CandidateReviewBatchModelAnnotate,
    CandidateReviewBatchSubmitResponse,
    CandidateReviewSubmitResult,
)
from app.services.model_annotation import ModelAnnotationError


def _candidate_item():
    return SimpleNamespace(
        id="ing-revision-1",
        event_id="event-1",
        idempotency_key="key-1",
        source_system="answer-hub",
        source_conversation_id="conversation-1",
        source_conversation_url=None,
        source_message_ids=[],
        redaction_status="redacted",
        processing_metadata={},
        selection_metadata={},
        candidate_payload={
            "knowledge": {
                "title": "原草稿",
                "content": {"blocks": [{"type": "text", "value": "原正文"}]},
                "recommended_reply": "原回复",
                "evidence_excerpt": "来源证据",
                "knowledge_origin": "business_accumulation",
                "business_type": "self_operated",
                "category_id": "category-1",
            },
            "model_review": {
                "suggested_action": "return_for_revision",
                "draft_quality": "revision_required",
                "resolved_model_version": "model-v5",
                "prompt_version": "review-v8",
            },
            "human_review": {},
        },
        review_metadata={},
        review_status="revision_required",
        reviewed_by="reviewer",
        reviewed_at=datetime(2026, 10, 1),
        submitted_at=None,
        status="candidate_revision_required",
        knowledge_id=None,
        error_code=None,
        error_message=None,
        created_at=datetime(2026, 10, 1),
        updated_at=datetime(2026, 10, 1),
    )


def test_model_annotation_revision_is_generated_but_not_applied(monkeypatch):
    item = _candidate_item()
    item.candidate_payload["selection"] = {
        "answer_hub_replay_input": {"topic_id": "topic-1", "facts": ["完整主题事实"]},
    }
    knowledge = dict(item.candidate_payload["knowledge"])
    original_payload = dict(item.candidate_payload)

    def revise(candidate):
        assert candidate["evidence_excerpt"] == "来源证据"
        assert candidate["topic_evidence"]["facts"] == ["完整主题事实"]
        assert candidate["draft"]["content"]["blocks"][0]["value"] == "原正文"
        return {
            "revised_content": "基于证据修订后的正文",
            "revised_recommended_reply": "基于证据修订后的回复",
            "change_summary": ["补全证据边界"],
            "evidence_used": ["来源证据"],
            "limitations": [],
            "resolved_model_version": "model-v5",
            "prompt_version": "revision-prompt-v2",
        }

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", revise)

    revision = integration._generate_pending_model_draft_revision(
        item,
        knowledge,
        item.candidate_payload["model_review"],
        requested_by="reviewer",
    )

    assert revision["status"] == "pending_human_apply"
    assert revision["evidence_snapshot"] == "来源证据"
    assert revision["topic_evidence_snapshot"]["topic_id"] == "topic-1"
    assert revision["source_model_prompt_version"] == "review-v8"
    assert revision["revised_draft"]["content"]["blocks"][0]["value"] == "基于证据修订后的正文"
    assert item.candidate_payload == original_payload
    assert knowledge["content"]["blocks"][0]["value"] == "原正文"


def test_topic_evidence_falls_back_and_refreshes_old_revision(monkeypatch):
    item = _candidate_item()
    item.candidate_payload["selection"] = {"eligible": False}
    item.selection_metadata = {"answer_hub_replay_input": {"facts": ["新增事实"]}}
    knowledge = item.candidate_payload["knowledge"]
    review = item.candidate_payload["model_review"]
    item.review_metadata["model_draft_revision"] = {
        "status": "pending_human_apply",
        "original_draft": integration._candidate_draft_snapshot(knowledge),
        "source_model_review": review,
        "topic_evidence_snapshot": {"facts": ["旧事实"]},
    }
    captured = []
    def revise(candidate):
        captured.append(candidate)
        return {"revised_content": "新正文", "revised_recommended_reply": "新回复"}
    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", revise)
    revision = integration._generate_pending_model_draft_revision(
        item, knowledge, review, requested_by="reviewer",
    )
    assert len(captured) == 1
    assert captured[0]["topic_evidence"]["facts"] == ["新增事实"]
    assert revision["topic_evidence_snapshot"]["facts"] == ["新增事实"]


def test_pending_revision_reused_only_while_topic_evidence_is_unchanged(monkeypatch):
    item = _candidate_item()
    item.candidate_payload["selection"] = {
        "answer_hub_replay_input": {"facts": ["原始事实"]},
    }
    knowledge = item.candidate_payload["knowledge"]
    review = item.candidate_payload["model_review"]
    calls = []

    def revise(candidate):
        calls.append(candidate)
        return {"revised_content": "新正文", "revised_recommended_reply": "新回复"}

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", revise)
    first = integration._generate_pending_model_draft_revision(
        item, knowledge, review, requested_by="reviewer",
    )
    reused = integration._generate_pending_model_draft_revision(
        item, knowledge, review, requested_by="reviewer",
    )
    assert reused == first
    assert len(calls) == 1
    item.candidate_payload["selection"]["answer_hub_replay_input"]["facts"] = ["补全事实"]
    refreshed = integration._generate_pending_model_draft_revision(
        item, knowledge, review, requested_by="reviewer",
    )
    assert len(calls) == 2
    assert first["topic_evidence_snapshot"]["facts"] == ["原始事实"]
    assert refreshed["topic_evidence_snapshot"]["facts"] == ["补全事实"]
    assert refreshed["revision_id"] != first["revision_id"]


def test_single_model_annotation_executes_revision_when_model_requests_it(monkeypatch):
    item = _candidate_item()
    item.candidate_payload["model_review"] = {}
    item.review_metadata = {}
    item.review_status = "pending"
    item.status = "candidate_pending"
    revision_calls = []
    monkeypatch.setattr(
        integration,
        "_candidate_payload_with_taxonomy_defaults",
        lambda payload: (payload, payload["knowledge"]),
    )
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("审核 Prompt", "review-v9"),
    )
    monkeypatch.setattr(
        integration,
        "annotate_transcribed_candidate",
        lambda *_args, **_kwargs: {
            "suggested_action": "return_for_revision",
            "draft_quality": "revision_required",
            "resolved_model_version": "model-v5",
            "prompt_version": "review-v9",
        },
    )

    def revise(candidate):
        revision_calls.append(candidate)
        return {
            "revised_content": "自动生成的待审修订正文",
            "revised_recommended_reply": "自动生成的待审回复",
            "resolved_model_version": "model-v5",
            "prompt_version": "draft-revision-v2",
        }

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", revise)
    monkeypatch.setattr(integration, "_candidate_review_item", lambda current: current)

    result = integration.annotate_candidate_review_draft(
        item.id,
        _Db(item),
        SimpleNamespace(username="reviewer"),
    )

    assert result.review_metadata["model_draft_revision"]["status"] == "pending_human_apply"
    assert result.review_status == "revision_required"
    assert len(revision_calls) == 1
    assert item.candidate_payload["knowledge"]["content"]["blocks"][0]["value"] == "原正文"


def test_batch_model_annotation_executes_requested_draft_revisions(monkeypatch):
    item = _candidate_item()
    db = _Db(item)
    revision_calls = []
    monkeypatch.setattr(
        integration,
        "_candidate_payload_with_taxonomy_defaults",
        lambda payload: (payload, payload["knowledge"]),
    )
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("审核 Prompt", "review-v9"),
    )
    monkeypatch.setattr(
        integration,
        "annotate_transcribed_candidate",
        lambda *_args, **_kwargs: {
            "suggested_action": "return_for_revision",
            "draft_quality": "revision_required",
            "resolved_model_version": "model-v5",
            "prompt_version": "review-v9",
        },
    )

    def revise(candidate):
        revision_calls.append(candidate)
        return {
            "revised_content": "批量生成的待审修订正文",
            "revised_recommended_reply": "批量生成的待审回复",
            "resolved_model_version": "model-v5",
            "prompt_version": "draft-revision-v2",
        }

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", revise)

    result = integration.model_annotate_candidate_reviews(
        SimpleNamespace(ingestion_ids=[item.id]),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert result.updated == 1
    assert result.results[0].model_draft_revision_status == "pending_human_apply"
    assert result.results[0].review_status == "revision_required"
    assert item.review_metadata["model_draft_revision"]["status"] == "pending_human_apply"
    assert len(revision_calls) == 1
    assert item.candidate_payload["knowledge"]["content"]["blocks"][0]["value"] == "原正文"


def test_batch_model_annotation_can_apply_revision_reannotate_and_submit_ready(
    monkeypatch,
):
    item = _candidate_item()
    item.candidate_payload["selection"] = {"upstream_eligible": False}
    item.candidate_payload["human_review"] = {}
    item.review_metadata = {"draft_version": 1}
    item.review_status = "pending"
    item.status = "candidate_pending"
    db = _Db(item)
    annotation_calls = []
    monkeypatch.setattr(
        integration,
        "_candidate_payload_with_taxonomy_defaults",
        lambda payload: (payload, payload["knowledge"]),
    )
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("审核 Prompt", "review-v9"),
    )

    def annotate(*_args, **_kwargs):
        annotation_calls.append(True)
        if len(annotation_calls) == 1:
            return {
                "knowledge_value": "worthy",
                "suggested_action": "return_for_revision",
                "draft_quality": "revision_required",
                "confidence": 0.95,
                "resolved_model_version": "model-v5",
                "prompt_version": "review-v9",
            }
        return {
            "knowledge_value": "worthy",
            "suggested_action": "submit_for_human_review",
            "draft_quality": "approved",
            "confidence": 0.96,
            "resolved_model_version": "model-v5",
            "prompt_version": "review-v9",
        }

    monkeypatch.setattr(integration, "annotate_transcribed_candidate", annotate)
    monkeypatch.setattr(
        integration,
        "revise_transcribed_candidate_draft",
        lambda _candidate: {
            "revised_content": "批量采用后的正文",
            "revised_recommended_reply": "批量采用后的回复",
            "resolved_model_version": "model-v5",
            "prompt_version": "draft-revision-v2",
        },
    )
    submitted_ids = []

    def submit(body, _db, _user):
        submitted_ids.extend(body.ingestion_ids)
        item.review_status = "submitted"
        return CandidateReviewBatchSubmitResponse(
            submitted=1,
            failed=0,
            reused=0,
            results=[
                CandidateReviewSubmitResult(
                    ingestion_id=item.id,
                    status="submitted",
                    knowledge_id="K-1",
                )
            ],
        )

    monkeypatch.setattr(integration, "submit_candidate_reviews", submit)

    result = integration.model_annotate_candidate_reviews(
        CandidateReviewBatchModelAnnotate(
            ingestion_ids=[item.id],
            apply_model_revisions=True,
            submit_ready=True,
        ),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert len(annotation_calls) == 2
    assert item.candidate_payload["knowledge"]["content"]["blocks"][0]["value"] == "批量采用后的正文"
    assert item.review_metadata["model_draft_revision"]["status"] == "applied"
    assert item.review_metadata["batch_model_admission"]["draft_version"] == 2
    assert item.review_metadata["human_review_draft_version"] == 1
    assert submitted_ids == [item.id]
    assert result.revisions_applied == 1
    assert result.reannotated == 1
    assert result.submitted == 1
    assert result.results[0].revision_applied is True
    assert result.results[0].reannotated is True
    assert result.results[0].submission_status == "submitted"
    assert result.results[0].knowledge_id == "K-1"


def test_batch_model_workflow_continues_after_one_revision_crashes(monkeypatch):
    item = _candidate_item()
    item.id = "ing-2"
    db = _Db(item)
    monkeypatch.setattr(
        integration,
        "_model_annotate_candidate_review_once",
        lambda ingestion_id, *_args: integration.CandidateReviewAnnotateResult(
            ingestion_id=ingestion_id,
            status="updated",
            review_status="revision_required",
            model_draft_revision_status="pending_human_apply",
        ),
    )

    def apply_revision(ingestion_id, *_args):
        if ingestion_id == "ing-1":
            raise RuntimeError("broken historical metadata")

    monkeypatch.setattr(
        integration,
        "apply_candidate_review_model_revision",
        apply_revision,
    )
    monkeypatch.setattr(
        integration,
        "annotate_candidate_review_draft",
        lambda *_args: SimpleNamespace(
            review_status="pending",
            model_draft_revision={},
        ),
    )
    monkeypatch.setattr(
        integration,
        "_record_batch_model_admission",
        lambda *_args, **_kwargs: {"review_status": "ready"},
    )
    submitted_ids = []

    def submit(body, *_args):
        submitted_ids.extend(body.ingestion_ids)
        return CandidateReviewBatchSubmitResponse(
            submitted=1,
            failed=0,
            reused=0,
            results=[
                CandidateReviewSubmitResult(
                    ingestion_id=body.ingestion_ids[0],
                    status="submitted",
                    knowledge_id="K-2",
                )
            ],
        )

    monkeypatch.setattr(integration, "submit_candidate_reviews", submit)

    result = integration.model_annotate_candidate_reviews(
        CandidateReviewBatchModelAnnotate(
            ingestion_ids=["ing-1", "ing-2"],
            apply_model_revisions=True,
            submit_ready=True,
        ),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert result.failed == 1
    assert result.submitted == 1
    assert result.results[0].error_code == "MODEL_REVISION_WORKFLOW_FAILED"
    assert result.results[1].submission_status == "submitted"
    assert submitted_ids == ["ing-2"]
    assert db.rollback_count == 1


def test_batch_model_workflow_continues_when_one_submission_crashes(
    monkeypatch,
):
    item = _candidate_item()
    db = _Db(item)
    monkeypatch.setattr(
        integration,
        "_model_annotate_candidate_review_once",
        lambda ingestion_id, *_args: integration.CandidateReviewAnnotateResult(
            ingestion_id=ingestion_id,
            status="updated",
            review_status="pending",
        ),
    )
    monkeypatch.setattr(
        integration,
        "_record_batch_model_admission",
        lambda *_args, **_kwargs: {"review_status": "ready"},
    )
    submitted_ids = []

    def submit(body, *_args):
        ingestion_id = body.ingestion_ids[0]
        submitted_ids.append(ingestion_id)
        if ingestion_id == "ing-1":
            raise RuntimeError("submission failed")
        item.review_status = "submitted"
        return CandidateReviewBatchSubmitResponse(
            submitted=1,
            failed=0,
            reused=0,
            results=[
                CandidateReviewSubmitResult(
                    ingestion_id=ingestion_id,
                    status="submitted",
                    knowledge_id=f"K-{ingestion_id}",
                )
            ],
        )

    monkeypatch.setattr(integration, "submit_candidate_reviews", submit)

    result = integration.model_annotate_candidate_reviews(
        CandidateReviewBatchModelAnnotate(
            ingestion_ids=["ing-1", "ing-2"],
            apply_model_revisions=True,
            submit_ready=True,
        ),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert submitted_ids == ["ing-1", "ing-2"]
    assert result.submitted == 1
    assert result.submission_failed == 1
    assert result.results[0].error_code == "BATCH_SUBMISSION_FAILED"
    assert result.results[1].submission_status == "submitted"
    assert db.rollback_count == 1


def test_model_revision_failure_is_persisted_for_retry(monkeypatch):
    item = _candidate_item()

    def fail(_candidate):
        raise ModelAnnotationError("模型超时", "TIMEOUT", retryable=True)

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", fail)

    revision = integration._generate_pending_model_draft_revision(
        item,
        dict(item.candidate_payload["knowledge"]),
        item.candidate_payload["model_review"],
        requested_by="reviewer",
    )

    assert revision["status"] == "generation_failed"
    assert revision["error_code"] == "TIMEOUT"
    assert item.review_metadata["model_draft_revision"]["status"] == "generation_failed"


def test_manual_model_revision_uses_same_persisted_shape_as_automatic_revision(
    monkeypatch,
):
    item = _candidate_item()
    item.review_metadata = {
        "draft_version": 1,
        "model_annotation": {
            "status": "completed",
            "draft_version": 1,
        },
    }

    monkeypatch.setattr(
        integration,
        "revise_transcribed_candidate_draft",
        lambda _candidate: {
            "revised_content": "统一 helper 生成的正文",
            "revised_recommended_reply": "统一 helper 生成的回复",
            "change_summary": ["统一字段"],
            "evidence_used": ["来源证据"],
            "limitations": [],
            "requested_model": "deepseek-chat",
            "resolved_model_version": "deepseek-v3",
            "prompt_version": "revision-v3",
        },
    )
    monkeypatch.setattr(
        integration,
        "_candidate_review_item",
        lambda current: current,
    )

    result = integration.revise_candidate_review_draft(
        item.id,
        _Db(item),
        SimpleNamespace(username="reviewer"),
    )

    revision = result.review_metadata["model_draft_revision"]
    assert revision["status"] == "pending_human_apply"
    assert revision["revision_id"] == "draft-v1-r1"
    assert revision["base_draft_version"] == 1
    assert revision["source_model_prompt_version"] == "review-v8"
    assert revision["resolved_model_version"] == "deepseek-v3"
    assert revision["revision_model_version"] == "deepseek-v3"
    assert revision["prompt_version"] == "revision-v3"
    assert revision["revision_prompt_version"] == "revision-v3"


def test_manual_model_revision_persists_failure_before_returning_error(monkeypatch):
    item = _candidate_item()
    item.review_metadata = {
        "draft_version": 1,
        "model_annotation": {
            "status": "completed",
            "draft_version": 1,
        },
    }

    def fail(_candidate):
        raise ModelAnnotationError("模型超时", "TIMEOUT", retryable=True)

    monkeypatch.setattr(integration, "revise_transcribed_candidate_draft", fail)

    with pytest.raises(integration.HTTPException) as error:
        integration.revise_candidate_review_draft(
            item.id,
            _Db(item),
            SimpleNamespace(username="reviewer"),
        )

    assert error.value.status_code == 503
    assert item.review_metadata["model_draft_revision"]["status"] == "generation_failed"
    assert item.review_metadata["model_draft_revision"]["error_code"] == "TIMEOUT"


def test_new_model_annotation_supersedes_stale_pending_revision():
    item = _candidate_item()
    item.review_metadata = {
        "model_draft_revision": {
            "revision": 1,
            "revision_id": "draft-v1-r1",
            "base_draft_version": 1,
            "status": "pending_human_apply",
            "revised_draft": {"content": "过期建议"},
        },
        "draft_version": 1,
    }
    knowledge = item.candidate_payload["knowledge"]

    revision = integration._generate_pending_model_draft_revision(
        item,
        knowledge,
        {"suggested_action": "submit_for_human_review"},
        requested_by="reviewer",
    )

    assert revision == {}
    assert item.review_metadata["model_draft_revision"]["status"] == "superseded_by_model_annotation"


class _Query:
    def __init__(self, item):
        self.item = item

    def filter(self, *_args):
        return self

    def first(self):
        return self.item


class _Db:
    def __init__(self, item):
        self.item = item
        self.rollback_count = 0

    def query(self, *_args):
        return _Query(self.item)

    def add(self, _item):
        pass

    def commit(self):
        pass

    def rollback(self):
        self.rollback_count += 1

    def refresh(self, _item):
        pass


def test_human_apply_is_the_only_step_that_changes_draft_and_appends_history():
    item = _candidate_item()
    item.review_metadata = {
        "draft_version": 1,
        "model_draft_revision": {
            "revision": 1,
            "revision_id": "draft-v1-r1",
            "base_draft_version": 1,
            "status": "pending_human_apply",
            "original_draft": {
                "title": "原草稿",
                "content": {"blocks": [{"type": "text", "value": "原正文"}]},
                "recommended_reply": "原回复",
            },
            "evidence_snapshot": "来源证据",
            "revised_draft": {
                "title": "原草稿",
                "content": {"blocks": [{"type": "text", "value": "采用后正文"}]},
                "recommended_reply": "采用后回复",
            },
        }
    }
    db = _Db(item)
    user = SimpleNamespace(username="reviewer")

    result = integration.apply_candidate_review_model_revision("ing-revision-1", db, user)

    assert result.model_draft_revision["status"] == "applied"
    assert item.candidate_payload["knowledge"]["content"]["blocks"][0]["value"] == "采用后正文"
    history = item.review_metadata["draft_revision_history"]
    assert len(history) == 1
    assert history[0]["before"]["content"]["blocks"][0]["value"] == "原正文"
    assert history[0]["after"]["content"]["blocks"][0]["value"] == "采用后正文"
    assert item.review_metadata["model_annotation"]["status"] == "stale_after_draft_revision"


def test_model_revision_does_not_revive_stale_human_value():
    item = _candidate_item()
    item.candidate_payload["human_review"] = {
        "knowledge_value": "worthy",
        "usability": "usable",
        "decision": "approved",
    }
    item.review_metadata = {
        "draft_version": 2,
        "human_review": dict(item.candidate_payload["human_review"]),
        "human_review_draft_version": 1,
        "model_draft_revision": {
            "revision": 1,
            "revision_id": "draft-v2-r1",
            "base_draft_version": 2,
            "status": "pending_human_apply",
            "revised_draft": {
                "title": "原草稿",
                "content": {"blocks": [{"type": "text", "value": "采用后正文"}]},
                "recommended_reply": "采用后回复",
            },
        },
    }

    integration.apply_candidate_review_model_revision(
        item.id,
        _Db(item),
        SimpleNamespace(username="reviewer"),
    )

    assert item.review_metadata["draft_version"] == 3
    assert item.review_metadata["human_review_draft_version"] == 1
