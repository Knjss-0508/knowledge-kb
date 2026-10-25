from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from unittest.mock import MagicMock

from app.routes import integration
from app.schemas.integration import (
    CandidateReviewBatchModelAnnotate,
    CandidateReviewBatchAnnotate,
    CandidateReviewBatchSubmit,
    CandidateReviewUpdate,
    IntegrationCandidateBatch,
)
from app.services.knowledge_dedup import DedupDecision
from app.models.integration import IntegrationIngestion


def _new_knowledge_decision() -> DedupDecision:
    return DedupDecision(
        action="create",
        content_hash="candidate-content-hash",
        embedding=None,
        title_embedding=None,
        content_embedding=None,
        matches=[],
    )


def _candidate_item(
    *,
    review_status="pending",
    title="候选标题",
    content="候选正文",
    recommended_reply="推荐回复",
    evidence_excerpt="来源证据摘要",
    model_review=None,
    human_review=None,
    review_metadata=None,
    knowledge_id=None,
):
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id="ing-1",
        event_id="event-1",
        idempotency_key="key-1",
        source_system="answer-hub",
        source_conversation_id="conversation-1",
        source_conversation_url=None,
        source_message_ids=["message-1"],
        review_status=review_status,
        status=f"candidate_{review_status}",
        candidate_payload={
            "event_id": "event-1",
            "idempotency_key": "key-1",
            "source": {
                "system": "answer-hub",
                "conversation_id": "conversation-1",
                "message_ids": ["message-1"],
                "redaction_status": "redacted",
            },
            "processing": {
                "summary_version": "summary-v1",
                "label_model": "label-v1",
                "plugin_name": "answer-hub-topic-transcription",
                "plugin_version": "1.0.0",
            },
            "knowledge": {
                "title": title,
                "content": content,
                "recommended_reply": recommended_reply,
                "evidence_excerpt": evidence_excerpt,
                "knowledge_origin": "business_accumulation",
                "business_type": "self_operated",
                "category_id": "category-1",
            },
            "selection": {"eligible": False, "confidence": 0.9},
            "model_review": model_review or {},
            "human_review": human_review or {},
        },
        selection_metadata={"eligible": False},
        review_metadata=review_metadata or {},
        knowledge_id=knowledge_id,
        submitted_at=None,
        reviewed_by=None,
        reviewed_at=None,
        error_code=None,
        error_message=None,
        created_at=now,
        updated_at=now,
    )


def _db_for(item):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.return_value = item
    return db


@pytest.mark.parametrize("changes", [
    {"applicable_brands": ["品牌甲"]},
    {"applicable_models": ["机型甲"]},
    {"applicable_categories": ["手机"]},
    {"related_standard_items": ["STD-TEST"]},
    {"applicable_scenes": ["验机"]},
    {"subtitles": ["补充检索标题"]},
])
def test_scope_changes_invalidate_old_model_review(changes):
    item = _candidate_item(
        model_review={"suggested_action": "submit_for_human_review"},
        human_review={"knowledge_value": "worthy", "usability": "usable"},
        review_metadata={
            "draft_version": 1,
            "model_annotation": {"status": "completed", "draft_version": 1},
        },
    )
    result = integration.update_candidate_review(
        "ing-1", CandidateReviewUpdate(**changes),
        _db_for(item), SimpleNamespace(username="reviewer"),
    )
    assert result.draft_version == 2
    assert result.review_status == "pending_model_annotation"
    assert result.eligible is False
    assert item.review_metadata["human_review_draft_version"] == 1


def test_human_value_save_binds_current_draft_without_manual_draft_disposition():
    item = _candidate_item(
        model_review={"suggested_action": "submit_for_human_review"},
        review_metadata={
            "draft_version": 2,
            "model_annotation": {"status": "completed", "draft_version": 2},
        },
    )
    result = integration.update_candidate_review(
        "ing-1",
        CandidateReviewUpdate(knowledge_value="worthy", usability="usable", decision="approved"),
        _db_for(item), SimpleNamespace(username="reviewer"),
    )
    assert item.review_metadata["human_review_draft_version"] == 2
    assert result.review_status == "ready"


def test_human_value_only_save_completes_legacy_fields_and_survives_reload():
    item = _candidate_item(
        model_review={"suggested_action": "submit_for_human_review"},
        human_review={"knowledge_value": "pending", "usability": "pending"},
    )
    result = integration.update_candidate_review(
        "ing-1", CandidateReviewUpdate(knowledge_value="worthy"),
        _db_for(item), SimpleNamespace(username="reviewer"),
    )
    assert result.review_status == "ready"
    assert result.human_review["usability"] == "usable"
    assert result.human_review["decision"] == "approved"
    reloaded = integration._candidate_review_item(item)
    assert reloaded.review_status == item.review_status == "ready"
    assert reloaded.eligible is True


def test_upstream_approved_candidate_with_model_review_stays_ready():
    item = _candidate_item(
        model_review={
            "suggested_action": "submit_for_human_review",
            "knowledge_value": "worthy",
            "confidence": 0.95,
        },
    )
    item.candidate_payload["selection"]["eligible"] = True
    state = integration._persist_candidate_state(
        item, item.candidate_payload, item.review_metadata,
    )
    assert state["review_status"] == "ready"
    assert integration._candidate_review_item(item).review_status == "ready"


@pytest.mark.parametrize("confidence", [0.1, 0.84, None, "invalid", float("nan"), True])
def test_upstream_admission_with_low_or_invalid_model_confidence_waits_for_human(confidence):
    item = _candidate_item(model_review={
        "suggested_action": "submit_for_human_review",
        "knowledge_value": "worthy",
        "confidence": confidence,
    })
    item.candidate_payload["selection"]["eligible"] = True
    state = integration._candidate_state(item, item.candidate_payload, {})
    assert state["review_status"] == "pending"
    assert state["eligible"] is False


def test_model_admission_honors_configured_confidence_threshold(monkeypatch):
    monkeypatch.setattr(integration.settings, "AUTO_REVIEW_MIN_CONFIDENCE", 0.97)
    item = _candidate_item(model_review={
        "suggested_action": "submit_for_human_review",
        "knowledge_value": "worthy",
        "confidence": 0.95,
    })
    item.candidate_payload["selection"]["eligible"] = True
    assert integration._candidate_state(
        item, item.candidate_payload, {},
    )["review_status"] == "pending"


@pytest.mark.parametrize("selection,expected", [
    ({"eligible": False, "reasons": ["自动审核：auto_approved"]}, "ready"),
    ({"eligible": False, "reasons": ["模型建议通过"]}, "pending"),
    ({"eligible": False, "reasons": ["自动审核：auto_approved"],
      "upstream_eligible": False}, "pending"),
])
def test_legacy_admission_recovers_only_explicit_auto_approval(selection, expected):
    item = _candidate_item(model_review={
        "suggested_action": "submit_for_human_review",
        "knowledge_value": "worthy",
        "confidence": 0.95,
    })
    item.candidate_payload["selection"] = selection
    state = integration._persist_candidate_state(
        item, item.candidate_payload, item.review_metadata,
    )
    assert state["review_status"] == expected
    assert integration._candidate_review_item(item).review_status == expected
    assert item.candidate_payload["human_review"] == {}


@pytest.mark.parametrize("changes,metadata", [
    ({"confidence": 0.1}, {}),
    ({"risk_flags": ["source_conflict"]}, {}),
    ({}, {"draft_revision_history": [{"draft_version": 2}],
          "draft_version": 2,
          "model_annotation": {"status": "completed", "draft_version": 2}}),
])
def test_legacy_auto_approval_does_not_override_current_blockers(changes, metadata):
    item = _candidate_item(model_review={
        "suggested_action": "submit_for_human_review",
        "knowledge_value": "worthy", "confidence": 0.95, **changes,
    }, review_metadata=metadata)
    item.candidate_payload["selection"] = {
        "eligible": False, "reasons": ["自动审核：auto_approved"],
    }
    assert integration._candidate_state(
        item, item.candidate_payload, metadata,
    )["eligible"] is False


@pytest.mark.parametrize("confidence,expected", [
    (0.1, "pending"), (0.84, "pending"), (None, "pending"),
    ("invalid", "pending"), (float("nan"), "pending"), (True, "pending"),
    (0.95, "ready"),
])
@pytest.mark.parametrize("already_eligible", [False, True])
def test_unannotated_admission_requires_valid_upstream_confidence(confidence, expected, already_eligible):
    item = _candidate_item()
    item.candidate_payload["selection"] = {
        "eligible": already_eligible, "reasons": ["自动审核：auto_approved"],
        "confidence": confidence,
    }
    state = integration._persist_candidate_state(item, item.candidate_payload, {})
    assert state["review_status"] == expected
    assert item.review_status == expected


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_explicit_pending_human_review_revokes_previous_admission(mode):
    item = _candidate_item(
        model_review={
            "suggested_action": "submit_for_human_review",
            "draft_quality": "approved",
            "knowledge_value": "worthy",
            "confidence": 0.95,
        },
        review_metadata={
            "draft_version": 1,
            "model_annotation": {"status": "completed", "draft_version": 1},
            "batch_model_admission": {
                "status": "approved",
                "source": "batch_model_workflow",
                "draft_version": 1,
            },
        },
    )
    item.candidate_payload["selection"].update(
        {"eligible": True, "upstream_eligible": True},
    )
    db = _db_for(item)
    user = SimpleNamespace(username="reviewer")
    if mode == "single":
        integration.update_candidate_review(
            "ing-1", CandidateReviewUpdate(knowledge_value="pending"), db, user,
        )
    else:
        integration.annotate_candidate_reviews(
            CandidateReviewBatchAnnotate(
                ingestion_ids=["ing-1"], knowledge_value="pending",
            ), db, user,
        )
    assert integration._candidate_review_item(item).review_status == "pending"
    assert item.selection_metadata["upstream_eligible"] is False
    assert "batch_model_admission" not in item.review_metadata


def test_explicit_legacy_rejection_is_not_overwritten_by_value_default():
    item = _candidate_item(model_review={"suggested_action": "submit_for_human_review"})
    result = integration.update_candidate_review(
        "ing-1",
        CandidateReviewUpdate(knowledge_value="worthy", decision="rejected"),
        _db_for(item), SimpleNamespace(username="reviewer"),
    )
    assert result.review_status == "rejected"


@pytest.mark.parametrize("model,metadata,knowledge_change,expected", [
    ({"suggested_action": "return_for_revision"}, {}, {}, "revision_required"),
    ({"suggested_action": "hold_for_evidence"}, {}, {}, "hold_for_evidence"),
    ({"suggested_action": "discard"}, {}, {}, "discard_review"),
    ({"suggested_action": "submit_for_human_review", "risk_flags": ["source_conflict"]},
     {}, {}, "pending"),
    ({"suggested_action": "submit_for_human_review"},
     {"model_annotation": {"status": "failed"}}, {}, "pending_model_annotation"),
    ({"suggested_action": "submit_for_human_review"}, {},
     {"recommended_reply": ""}, "draft_incomplete"),
    ({"suggested_action": "submit_for_human_review"},
     {"draft_version": 2, "human_review_draft_version": 1,
      "model_annotation": {"status": "completed", "draft_version": 2}},
     {}, "pending"),
])
def test_upstream_admission_never_bypasses_current_candidate_blockers(
    model, metadata, knowledge_change, expected,
):
    item = _candidate_item(model_review=model, review_metadata=metadata)
    item.candidate_payload["selection"].update(
        {"eligible": True, "upstream_eligible": True},
    )
    item.candidate_payload["knowledge"].update(knowledge_change)
    state = integration._persist_candidate_state(
        item, item.candidate_payload, item.review_metadata,
    )
    assert state["review_status"] == expected
    assert state["eligible"] is False


@pytest.mark.parametrize(
    "model_review,expected",
    [
        (
            {
                "suggested_action": "submit_for_human_review",
                "draft_quality": "approved",
                "knowledge_value": "worthy",
                "confidence": 0.84,
            },
            "pending",
        ),
        (
            {
                "suggested_action": "submit_for_human_review",
                "draft_quality": "approved",
                "knowledge_value": "worthy",
                "confidence": 0.95,
                "risk_flags": ["source_conflict"],
            },
            "pending",
        ),
        (
            {
                "suggested_action": "hold_for_evidence",
                "draft_quality": "hold_for_evidence",
                "knowledge_value": "worthy",
                "confidence": 0.95,
            },
            "hold_for_evidence",
        ),
    ],
)
def test_batch_model_admission_keeps_confidence_risk_and_evidence_gates(
    model_review,
    expected,
):
    item = _candidate_item(
        model_review=model_review,
        review_metadata={
            "draft_version": 2,
            "draft_revision_history": [{"draft_version": 2}],
            "model_review": model_review,
            "model_annotation": {"status": "completed", "draft_version": 2},
            "batch_model_admission": {
                "status": "approved",
                "source": "batch_model_workflow",
                "draft_version": 2,
            },
        },
    )

    state = integration._persist_candidate_state(
        item,
        item.candidate_payload,
        item.review_metadata,
    )

    assert state["review_status"] == expected
    assert state["eligible"] is False


def test_new_model_annotation_does_not_reconfirm_old_human_value():
    item = _candidate_item(
        model_review={"suggested_action": "submit_for_human_review"},
        human_review={"knowledge_value": "worthy", "usability": "usable"},
        review_metadata={
            "draft_version": 2,
            "human_review_draft_version": 1,
            "model_annotation": {"status": "completed", "draft_version": 2},
        },
    )
    state = integration._candidate_state(item, item.candidate_payload, item.review_metadata)
    assert state["review_status"] == "pending"
    assert state["eligible"] is False
    assert state["next_action"] == "human_review"


@pytest.mark.parametrize("model_review,expected_priority", [
    ({"priority_review": True, "suggested_action": "submit_for_human_review"}, False),
    ({"priority_review": False, "risk_flags": ["source_conflict"]}, True),
])
def test_light_candidate_priority_matches_detail_risk_policy(model_review, expected_priority):
    item = _candidate_item(model_review=model_review)
    row = SimpleNamespace(
        id=item.id, event_id=item.event_id,
        source_conversation_id=item.source_conversation_id,
        review_status=item.review_status, updated_at=item.updated_at,
        candidate_model_review=model_review, metadata_model_review={},
        candidate_human_review={}, metadata_human_review={},
        deduplication=None, deduplication_confirmation={},
        applicable_categories=[], evidence_length=10,
    )
    light = integration._candidate_review_light_item(row)
    detail = integration._candidate_review_item(item)
    assert light.priority_review == detail.priority_review == expected_priority


def test_single_model_annotation_persists_recalculated_candidate_state(monkeypatch):
    item = _candidate_item(
        review_metadata={"draft_version": 3},
    )
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("prompt", "p2"),
    )
    monkeypatch.setattr(
        integration,
        "annotate_transcribed_candidate",
        lambda *_args, **_kwargs: {
            "suggested_action": "hold_for_evidence",
            "priority_review": True,
            "evidence_sufficiency": "insufficient",
        },
    )

    result = integration.annotate_candidate_review_draft(
        "ing-1", db, SimpleNamespace(username="reviewer")
    )

    assert result.review_status == "hold_for_evidence"
    assert result.eligible is False
    assert result.next_action == "add_source_evidence"
    assert result.draft_version == 3
    assert result.priority_review is True
    assert item.candidate_payload["selection"]["eligible"] is False
    assert item.review_metadata["model_annotation"]["draft_version"] == 3
    assert item.review_metadata["candidate_state"]["review_status"] == "hold_for_evidence"
    assert item.review_metadata["candidate_state"]["next_action"] == "add_source_evidence"
    db.commit.assert_called_once()


def test_pending_model_annotation_status_fits_persistent_column():
    item = _candidate_item(review_metadata={"model_annotation": {"status": "failed"}})
    integration._persist_candidate_state(item, item.candidate_payload, item.review_metadata)
    assert item.review_status == "pending_model_annotation"
    assert len(item.status) <= IntegrationIngestion.__table__.c.status.type.length


@pytest.mark.parametrize(
    "suggested_action",
    [
        "return_for_revision",
        "hold_for_evidence",
        "submit_for_human_review",
        "discard",
    ],
)
def test_current_model_action_cannot_be_bypassed_by_reannotation(
    monkeypatch,
    suggested_action,
):
    model_review = {"suggested_action": suggested_action}
    item = _candidate_item(
        model_review=model_review,
        review_metadata={
            "draft_version": 2,
            "model_review": model_review,
            "model_annotation": {"status": "completed", "draft_version": 2},
        },
    )
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "annotate_transcribed_candidate",
        lambda *_args, **_kwargs: pytest.fail("当前动作应先完成，不能直接重复模型标注"),
    )

    with pytest.raises(HTTPException) as error:
        integration.annotate_candidate_review_draft(
            "ing-1", db, SimpleNamespace(username="reviewer")
        )

    assert error.value.status_code == 409
    assert "已给出下一步动作" in error.value.detail
    db.commit.assert_not_called()


def test_model_annotation_refuses_missing_reply_but_not_missing_evidence_excerpt(monkeypatch):
    item = _candidate_item(recommended_reply="", evidence_excerpt="")
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("prompt", "p2"),
    )
    monkeypatch.setattr(
        integration,
        "annotate_transcribed_candidate",
        lambda *_args, **_kwargs: pytest.fail("模型不应被调用"),
    )

    with pytest.raises(HTTPException) as error:
        integration.annotate_candidate_review_draft(
            "ing-1", db, SimpleNamespace(username="reviewer")
        )

    assert error.value.status_code == 409
    assert "推荐回复为空" in error.value.detail
    assert item.review_status == "draft_incomplete"
    assert item.selection_metadata["eligible"] is False
    assert item.review_metadata["candidate_state"]["next_action"] == "complete_draft"
    db.commit.assert_called_once()


def test_batch_model_annotation_uses_the_same_completeness_gate(monkeypatch):
    item = _candidate_item(title=" ", content={"blocks": []})
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "get_confidence_prompt_for_candidate",
        lambda *_args, **_kwargs: ("prompt", "p2"),
    )

    result = integration.model_annotate_candidate_reviews(
        CandidateReviewBatchModelAnnotate(ingestion_ids=["ing-1"]),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert result.updated == 0
    assert result.failed == 1
    assert result.results[0].error_code == "DRAFT_INCOMPLETE"
    assert result.results[0].review_status == "draft_incomplete"
    assert item.review_metadata["candidate_state"]["eligible"] is False


def test_batch_invalid_model_result_preserves_initial_review_and_human_truth(monkeypatch):
    from app.services import model_annotation

    original = {"knowledge_value": "worthy", "decision": "通过", "confidence": 0.76}
    human = {"knowledge_value": "unworthy", "reviewer": "human"}
    item = _candidate_item(model_review=original, human_review=human)
    db = _db_for(item)
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *_args, **_kwargs: ("审核规则", "active"))
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", lambda *_args, **_kwargs: {"type": "json_object"})

    result = integration.model_annotate_candidate_reviews(
        CandidateReviewBatchModelAnnotate(ingestion_ids=["ing-1"]), db,
        SimpleNamespace(username="reviewer"),
    )
    assert result.updated == 0
    assert result.failed == 1
    assert result.results[0].error_code == "INVALID_ANNOTATION_RESULT"
    assert item.candidate_payload["model_review"] == original
    assert item.candidate_payload["human_review"] == human
    assert item.review_metadata["model_annotation"]["status"] == "failed"


def test_human_draft_edit_increments_version_and_invalidates_model_annotation():
    item = _candidate_item(
        review_status="ready",
        model_review={"suggested_action": "submit_for_human_review"},
        human_review={"knowledge_value": "worthy", "usability": "usable"},
        review_metadata={
            "draft_version": 1,
            "model_review": {"suggested_action": "submit_for_human_review"},
            "model_annotation": {"status": "completed", "draft_version": 1},
            "model_draft_revision": {
                "revision": 1,
                "revision_id": "draft-v1-r1",
                "base_draft_version": 1,
                "status": "pending_human_apply",
            },
            "model_draft_revisions": [
                {
                    "revision": 1,
                    "revision_id": "draft-v1-r1",
                    "base_draft_version": 1,
                    "status": "pending_human_apply",
                }
            ],
        },
    )
    db = _db_for(item)

    result = integration.update_candidate_review(
        "ing-1",
        CandidateReviewUpdate(content="修订后的正文"),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert result.draft_version == 2
    assert result.review_status == "pending_model_annotation"
    assert result.eligible is False
    assert result.model_review == {}
    assert item.review_metadata["model_annotation"]["status"] == "stale_after_draft_revision"
    assert item.review_metadata["model_annotation"]["draft_version"] == 2
    assert item.review_metadata["model_draft_revision"]["status"] == "stale"
    assert item.review_metadata["model_draft_revisions"][0]["status"] == "stale"
    db.commit.assert_called_once()


def test_submitted_candidate_cannot_be_model_annotated():
    item = _candidate_item(review_status="submitted", knowledge_id="K-1")
    db = _db_for(item)

    with pytest.raises(HTTPException) as error:
        integration.annotate_candidate_review_draft(
            "ing-1", db, SimpleNamespace(username="reviewer")
        )

    assert error.value.status_code == 409


def test_submitted_candidate_cannot_be_manually_rewritten():
    item = _candidate_item(review_status="submitted", knowledge_id="K-1")
    db = _db_for(item)

    with pytest.raises(HTTPException) as error:
        integration.update_candidate_review(
            "ing-1",
            CandidateReviewUpdate(content="不应写入的新正文"),
            db,
            SimpleNamespace(username="reviewer"),
        )

    assert error.value.status_code == 409
    assert item.candidate_payload["knowledge"]["content"] == "候选正文"


def test_batch_submit_allows_empty_evidence_excerpt_when_required_draft_fields_exist(
    monkeypatch,
):
    item = _candidate_item(
        review_status="ready",
        evidence_excerpt="",
        human_review={"knowledge_value": "worthy", "usability": "usable"},
        model_review={"suggested_action": "submit_for_human_review"},
        review_metadata={
            "draft_version": 1,
            "model_review": {"suggested_action": "submit_for_human_review"},
            "model_annotation": {"status": "completed", "draft_version": 1},
            "human_review": {"knowledge_value": "worthy", "usability": "usable"},
        },
    )
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "check_duplicate",
        lambda *_args, **_kwargs: _new_knowledge_decision(),
    )
    monkeypatch.setattr(
        integration,
        "_generate_knowledge_id",
        lambda _db: "K-1",
    )
    monkeypatch.setattr(
        integration,
        "ensure_search_embeddings",
        lambda *_args, **_kwargs: None,
    )

    result = integration.submit_candidate_reviews(
        CandidateReviewBatchSubmit(ingestion_ids=["ing-1"]),
        db,
        SimpleNamespace(username="reviewer"),
    )

    assert result.failed == 0
    assert result.submitted == 1
    assert item.review_status == "submitted"


def test_candidate_summary_counts_human_pending_separately_from_explicit_model_risk(
    monkeypatch,
):
    ordinary = _candidate_item(
        model_review={
            "suggested_action": "submit_for_human_review",
            "priority_review": True,
        },
        review_metadata={
            "draft_version": 1,
            "model_review": {
                "suggested_action": "submit_for_human_review",
                "priority_review": True,
            },
            "model_annotation": {"status": "completed", "draft_version": 1},
        },
    )
    risky = _candidate_item(
        model_review={
            "suggested_action": "submit_for_human_review",
            "risk_flags": ["source_conflict"],
        },
        review_metadata={
            "draft_version": 1,
            "model_review": {
                "suggested_action": "submit_for_human_review",
                "risk_flags": ["source_conflict"],
            },
            "model_annotation": {"status": "completed", "draft_version": 1},
        },
    )
    items = [ordinary, risky]
    monkeypatch.setattr(
        integration,
        "_candidate_review_light_rows",
        lambda _db, *, keyword: items,
    )
    monkeypatch.setattr(
        integration,
        "_candidate_review_light_item",
        integration._candidate_review_item,
    )
    monkeypatch.setattr(
        integration,
        "_candidate_review_full_items",
        lambda _db, ids: [
            integration._candidate_review_item(item)
            for item in items
            if item.id in ids
        ],
    )
    db = MagicMock()

    response = integration.list_candidate_reviews(
        keyword="",
        review_status="",
        priority_only=False,
        deduplication_required=False,
        product_category="",
        annotation_status="",
        model_knowledge_value="",
        updated_from=None,
        updated_to=None,
        limit=100,
        offset=0,
        db=db,
        _=None,
    )

    assert response.summary["human_pending"] == 2
    assert response.summary["model_priority"] == 1
    assert response.items[0].priority_review is False
    assert response.items[1].priority_review is True


def test_direct_candidate_submission_accepts_empty_source_excerpt_when_draft_is_complete(
    monkeypatch,
):
    db = MagicMock()
    db.query.return_value.filter.return_value.first.side_effect = [
        None,
        SimpleNamespace(id="category-1"),
    ]
    monkeypatch.setattr(
        integration,
        "check_duplicate",
        lambda *_args, **_kwargs: _new_knowledge_decision(),
    )
    monkeypatch.setattr(
        integration,
        "_generate_knowledge_id",
        lambda _db: "K-1",
    )
    monkeypatch.setattr(
        integration,
        "ensure_search_embeddings",
        lambda *_args, **_kwargs: None,
    )
    body = IntegrationCandidateBatch(
        items=[
            {
                "event_id": "event-1",
                "idempotency_key": "key-1",
                "source": {
                    "system": "answer-hub",
                    "conversation_id": "conversation-1",
                    "message_ids": ["message-1"],
                },
                "processing": {
                    "summary_version": "summary-v1",
                    "label_model": "label-v1",
                    "plugin_name": "answer-hub-topic-transcription",
                    "plugin_version": "1.0.0",
                },
                "selection": {"eligible": True, "confidence": 0.9},
                "knowledge": {
                    "title": "候选标题",
                    "content": "候选正文",
                    "recommended_reply": "推荐回复",
                    "knowledge_origin": "business_accumulation",
                    "business_type": "self_operated",
                    "category_id": "category-1",
                },
            }
        ]
    )

    result = integration.submit_knowledge_candidates(body, db, None)

    assert result.accepted == 1
    assert result.rejected == 0
    assert db.add.call_count == 2


def test_model_revision_is_versioned_and_adoption_invalidates_old_annotation(monkeypatch):
    item = _candidate_item(
        review_status="revision_required",
        model_review={"suggested_action": "return_for_revision"},
        human_review={"knowledge_value": "worthy"},
        review_metadata={
            "draft_version": 1,
            "model_review": {"suggested_action": "return_for_revision"},
            "model_annotation": {"status": "completed", "draft_version": 1},
        },
    )
    db = _db_for(item)
    monkeypatch.setattr(
        integration,
        "revise_transcribed_candidate_draft",
        lambda *_args, **_kwargs: {
            "revised_content": "修订后的正文",
            "revised_recommended_reply": "修订后的回复",
            "change_summary": ["补齐步骤"],
        },
    )

    revised = integration.revise_candidate_review_draft(
        "ing-1", db, SimpleNamespace(username="reviewer")
    )
    assert revised.model_draft_revision["revision"] == 1
    assert revised.model_draft_revision["base_draft_version"] == 1

    applied = integration.apply_candidate_review_model_revision(
        "ing-1", db, SimpleNamespace(username="reviewer")
    )
    assert applied.draft_version == 2
    assert applied.review_status == "pending_model_annotation"
    assert applied.model_review == {}
    assert item.review_metadata["model_draft_revision"]["status"] == "applied"
    assert item.review_metadata["model_annotation"]["status"] == "stale_after_draft_revision"
