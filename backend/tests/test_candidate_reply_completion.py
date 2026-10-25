from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.routes import integration
from app.schemas.integration import CandidateReviewBatchModelAnnotate
from app.services import model_annotation
from backend.tests.test_candidate_review_state import _candidate_item, _db_for


EVIDENCE = "答疑确认：先核对两处设备信息；不一致时暂缓修改，并提交复核。"
REPLY = "请先核对两处设备信息；若不一致，请暂缓修改并提交复核。"


def test_reply_completion_uses_summary_as_the_fact_source(monkeypatch):
    captured = {}

    def call(prompt, *, purpose):
        captured.update(prompt=prompt, purpose=purpose)
        return {
            "recommended_reply": REPLY,
            "evidence_sufficiency": "sufficient",
            "evidence_used": ["不一致时暂缓修改，并提交复核"],
        }

    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", call)
    result = model_annotation.complete_candidate_recommended_reply({
        "title": "设备信息核对",
        "evidence_excerpt": EVIDENCE,
        "content": "无来源支持的旧正文结论",
    })
    assert result["recommended_reply"] == REPLY
    assert EVIDENCE in captured["prompt"]
    assert "无来源支持的旧正文结论" not in captured["prompt"]
    assert "来源证据摘要" in captured["prompt"]
    assert "不得补造" in captured["prompt"]


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_initial_annotation_fills_missing_reply_then_reviews_new_version(monkeypatch, mode):
    human = {"knowledge_value": "worthy", "usability": "usable", "decision": "approved"}
    item = _candidate_item(recommended_reply="", evidence_excerpt=EVIDENCE, human_review=human)
    before = deepcopy(item.candidate_payload["knowledge"])
    calls = []

    def call(prompt, *, purpose):
        calls.append(purpose)
        if purpose == "根据来源证据摘要补充推荐回复":
            assert EVIDENCE in prompt
            return {
                "recommended_reply": REPLY, "evidence_sufficiency": "sufficient",
                "evidence_used": ["不一致时暂缓修改，并提交复核"],
            }
        assert purpose == "草稿标注"
        assert REPLY in prompt
        return {
            "knowledge_value": "worthy", "draft_quality": "approved",
            "confidence": 0.95, "reason": "回复与摘要一致",
        }

    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", call)
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *args, **kwargs: ("审核规则", "active-v1"))
    db = _db_for(item)
    user = SimpleNamespace(username="reviewer")
    if mode == "single":
        result = integration.annotate_candidate_review_draft(item.id, db, user)
        assert result.recommended_reply == REPLY
        assert result.review_status == "pending"
    else:
        result = integration.model_annotate_candidate_reviews(
            CandidateReviewBatchModelAnnotate(ingestion_ids=[item.id]), db, user,
        )
        assert result.updated == 1
        assert result.failed == 0
    assert calls == ["根据来源证据摘要补充推荐回复", "草稿标注"]
    assert item.candidate_payload["knowledge"] == {**before, "recommended_reply": REPLY}
    assert item.candidate_payload["human_review"] == human
    assert item.review_metadata["draft_version"] == 2
    assert item.review_metadata["human_review_draft_version"] == 1
    assert item.review_metadata["model_annotation"]["draft_version"] == 2
    assert item.review_metadata["reply_completion"]["evidence_snapshot"] == EVIDENCE
    assert item.review_metadata["draft_revision_history"][0]["source"] == "model_reply_completion"
    assert item.review_status == "pending"
    assert item.knowledge_id is None


@pytest.mark.parametrize("result,error_code", [
    ({"evidence_sufficiency": "insufficient", "recommended_reply": ""}, "REPLY_EVIDENCE_INSUFFICIENT"),
    ({"evidence_sufficiency": "sufficient", "recommended_reply": "", "evidence_used": [EVIDENCE]}, "INVALID_REPLY_COMPLETION"),
    ({"evidence_sufficiency": "sufficient", "recommended_reply": REPLY, "evidence_used": ["摘要没有的依据"]}, "INVALID_REPLY_COMPLETION"),
    ({"evidence_sufficiency": "sufficient", "recommended_reply": REPLY, "evidence_used": []}, "INVALID_REPLY_COMPLETION"),
    ({"evidence_sufficiency": "sufficient", "recommended_reply": ["错误类型"], "evidence_used": [EVIDENCE]}, "INVALID_REPLY_COMPLETION"),
])
def test_completion_rejects_unsupported_or_incomplete_answers(monkeypatch, result, error_code):
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", lambda *args, **kwargs: result)
    with pytest.raises(model_annotation.ModelAnnotationError) as error:
        model_annotation.complete_candidate_recommended_reply({"title": "设备信息核对", "evidence_excerpt": EVIDENCE})
    assert error.value.error_code == error_code


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("failure", ["insufficient", "timeout"])
def test_completion_failure_keeps_reply_empty_and_previous_truth(monkeypatch, mode, failure):
    old_review = {"suggested_action": "hold_for_evidence", "knowledge_value": "pending"}
    human = {"knowledge_value": "worthy", "usability": "usable"}
    item = _candidate_item(recommended_reply="", evidence_excerpt=EVIDENCE, model_review=old_review, human_review=human)
    before = deepcopy(item.candidate_payload)

    def call(prompt, *, purpose):
        assert purpose == "根据来源证据摘要补充推荐回复"
        if failure == "timeout":
            raise model_annotation.ModelAnnotationError("模型超时", "TIMEOUT", retryable=True)
        return {"evidence_sufficiency": "insufficient", "recommended_reply": ""}

    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", call)
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *args, **kwargs: ("审核规则", "active-v1"))
    db = _db_for(item)
    user = SimpleNamespace(username="reviewer")
    if mode == "single":
        with pytest.raises(integration.HTTPException) as error:
            integration.annotate_candidate_review_draft(item.id, db, user)
        assert error.value.status_code == 503
    else:
        result = integration.model_annotate_candidate_reviews(
            CandidateReviewBatchModelAnnotate(ingestion_ids=[item.id]), db, user,
        )
        assert result.failed == 1
        assert result.updated == 0
        assert result.results[0].error_code == ("TIMEOUT" if failure == "timeout" else "REPLY_EVIDENCE_INSUFFICIENT")
    assert item.candidate_payload["knowledge"] == before["knowledge"]
    assert item.candidate_payload["human_review"] == human
    assert item.candidate_payload["model_review"] == old_review
    assert item.review_status == "draft_incomplete"
    assert item.review_metadata["reply_completion"]["status"] == "failed"
    assert item.review_metadata["draft_version"] == 1
    db.commit.assert_called_once()


@pytest.mark.parametrize("mode", ["single", "batch"])
@pytest.mark.parametrize("changes", [
    {"evidence_excerpt": ""},
    {"title": ""},
    {"content": {"blocks": [{"type": "image", "external_url": "https://example.com/image.png"}]}},
    {"knowledge_id": "knowledge-1"},
])
def test_missing_summary_body_or_submission_lock_blocks_completion(monkeypatch, mode, changes):
    item = _candidate_item(recommended_reply="", evidence_excerpt=EVIDENCE)
    changes = dict(changes)
    item.knowledge_id = changes.pop("knowledge_id", None)
    item.candidate_payload["knowledge"].update(changes)
    def unexpected(*args, **kwargs):
        pytest.fail("Blocked draft must not call the model")
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", unexpected)
    db = _db_for(item)
    user = SimpleNamespace(username="reviewer")
    if mode == "single":
        with pytest.raises(integration.HTTPException) as error:
            integration.annotate_candidate_review_draft(item.id, db, user)
        assert error.value.status_code == 409
    else:
        result = integration.model_annotate_candidate_reviews(
            CandidateReviewBatchModelAnnotate(ingestion_ids=[item.id]), db, user,
        )
        assert result.failed == 1
        assert result.updated == 0
    assert item.candidate_payload["knowledge"]["recommended_reply"] == ""
    assert "reply_completion" not in item.review_metadata


@pytest.mark.parametrize("mode", ["single", "batch"])
def test_existing_reply_is_preserved_without_completion_call(monkeypatch, mode):
    item = _candidate_item(recommended_reply="人工已有回复", evidence_excerpt="")
    def call(prompt, *, purpose):
        assert purpose == "草稿标注"
        return {
            "knowledge_value": "worthy", "draft_quality": "approved",
            "confidence": 0.95, "reason": "已有回复可用",
        }
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", call)
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *args, **kwargs: ("规则", "v1"))
    db, user = _db_for(item), SimpleNamespace(username="reviewer")
    if mode == "single":
        result = integration.annotate_candidate_review_draft(item.id, db, user)
        assert result.recommended_reply == "人工已有回复"
    else:
        result = integration.model_annotate_candidate_reviews(
            CandidateReviewBatchModelAnnotate(ingestion_ids=[item.id]), db, user,
        )
        assert result.updated == 1
    assert item.candidate_payload["knowledge"]["recommended_reply"] == "人工已有回复"
    assert item.review_metadata["draft_version"] == 1
    assert "reply_completion" not in item.review_metadata


def test_annotation_retry_reuses_completed_reply_and_keeps_version(monkeypatch):
    item = _candidate_item(recommended_reply="", evidence_excerpt=EVIDENCE)
    calls = []
    def call(prompt, *, purpose):
        calls.append(purpose)
        if purpose == "根据来源证据摘要补充推荐回复":
            return {"recommended_reply": REPLY, "evidence_sufficiency": "sufficient", "evidence_used": [EVIDENCE]}
        if calls.count("草稿标注") == 1:
            raise model_annotation.ModelAnnotationError("初标超时", "TIMEOUT", retryable=True)
        return {"knowledge_value": "worthy", "draft_quality": "approved", "confidence": 0.95, "reason": "与摘要一致"}
    monkeypatch.setattr(model_annotation, "_call_deepseek_flash", call)
    monkeypatch.setattr(integration, "get_confidence_prompt_for_candidate", lambda *args, **kwargs: ("规则", "v1"))
    db, user = _db_for(item), SimpleNamespace(username="reviewer")
    with pytest.raises(integration.HTTPException):
        integration.annotate_candidate_review_draft(item.id, db, user)
    assert item.candidate_payload["knowledge"]["recommended_reply"] == REPLY
    assert item.review_status == "pending_model_annotation"
    assert item.review_metadata["model_annotation"]["draft_version"] == 2
    result = integration.annotate_candidate_review_draft(item.id, db, user)
    assert result.draft_version == 2
    assert result.model_annotation["status"] == "completed"
    assert len(item.review_metadata["draft_revision_history"]) == 1
    assert calls.count("根据来源证据摘要补充推荐回复") == 1
