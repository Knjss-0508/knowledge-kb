from types import SimpleNamespace

from app.services.confidence_training import aggregate, confidence_band, item_payload, settings_snapshot, update_settings
from app.routes.confidence_training import _manual_training_dataset, _prompt_optimization_snapshot, _shadow_comparison, _revision_preconditions, _prompt_revision_snapshot, _shadow_sample_completeness


def test_confidence_band_boundaries_are_frozen():
    assert confidence_band(0.0) == "B0"
    assert confidence_band(0.4999) == "B0"
    assert confidence_band(0.5) == "B1"
    assert confidence_band(0.9) == "B4"
    assert confidence_band(0.98) == "B6"
    assert confidence_band(1.0) == "B6"
    assert confidence_band(1.01) is None


def test_unevaluable_truth_is_not_counted_as_correct():
    items = [
        {"confidence_band": "B4", "truth_status": "confirmed", "strict_correct": True, "acceptable_correct": True, "error_severity": None},
        {"confidence_band": "B4", "truth_status": "not_evaluable", "strict_correct": None, "acceptable_correct": None, "error_severity": None},
    ]
    overview, bands = aggregate(items)
    assert overview["valid_truth"] == 1
    assert overview["strict_accuracy"] == 1.0
    assert overview["coverage"] == 0.5
    assert bands[4]["n_scored"] == 2
    assert bands[4]["n_valid_truth"] == 1


def test_item_reads_model_and_human_reviews_without_overwriting_model_output():
    item = SimpleNamespace(
        id="i-1",
        event_id="batch-1",
        review_status="pending",
        candidate_payload={
            "knowledge": {"title": "测试知识"},
            "model_review": {"knowledge_value": "worthy", "confidence": 0.92, "model_name": "deepseek-flash"},
            "human_review": {"knowledge_value": "unworthy"},
        },
        review_metadata={},
    )
    payload = item_payload(item)
    assert payload["model_label"] == "worthy"
    assert payload["human_label"] == "unworthy"
    assert payload["model_confidence"] == 0.92


def test_human_truth_always_comes_from_candidate_review_not_evaluation_form():
    item = SimpleNamespace(
        id="i-2",
        event_id="batch-2",
        review_status="ready",
        candidate_payload={
            "knowledge": {"title": "候选"},
            "model_review": {"knowledge_value": "worthy", "confidence": 0.91},
            "human_review": {"knowledge_value": "unworthy"},
        },
        review_metadata={
            "confidence_training": {
                "human_label": "worthy",
                "truth_status": "confirmed",
            }
        },
    )
    payload = item_payload(item)
    assert payload["human_label"] == "unworthy"
    assert payload["truth_status"] == "confirmed"


def test_confidence_evaluation_compares_value_and_draft_disposition_automatically():
    item = SimpleNamespace(
        id="i-3",
        event_id="batch-3",
        review_status="revision_required",
        candidate_payload={
            "knowledge": {"title": "可复用主题但草稿跑题"},
            "model_review": {
                "knowledge_value": "worthy",
                "confidence": 0.96,
                "suggested_action": "submit_for_human_review",
            },
            "human_review": {
                "knowledge_value": "worthy",
                "draft_disposition": "revision_required",
                "training_eligible": "是",
            },
        },
        review_metadata={},
    )

    payload = item_payload(item)
    assert payload["truth_status"] == "confirmed"
    assert payload["model_draft_disposition"] == "approved"
    assert payload["human_draft_disposition"] == "revision_required"
    assert payload["model_correctness"] == "wrong"
    assert payload["training_candidate_status"] == "recommended"


def test_training_settings_persist_but_never_enable_unavailable_training_task():
    class Query:
        def __init__(self, db): self.db = db
        def filter(self, *_args): return self
        def first(self): return self.db.record

    class Db:
        record = None
        def query(self, *_args): return Query(self)
        def add(self, record): self.record = record
        def commit(self): return None
        def refresh(self, _record): return None
        def rollback(self): return None

    db = Db()
    saved = update_settings(db, {"training_candidate_collection_enabled": False, "training_snapshot_enabled": True, "auto_training_job_enabled": True, "production_auto_review_enabled": True}, updated_by="tester")
    restored = settings_snapshot(db)
    assert saved["training_snapshot_enabled"] is True
    assert restored["training_candidate_collection_enabled"] is False
    assert restored["auto_training_job_enabled"] is False
    assert restored["production_auto_review_enabled"] is False
    assert restored["training_task_available"] is False


def test_manual_training_dataset_uses_all_confirmed_truth_and_deterministic_splits():
    candidate = SimpleNamespace(
        id="ing-1",
        event_id="batch-1",
        review_status="rejected",
        candidate_payload={
            "knowledge": {"title": "人工确认不沉淀的候选", "content": {"blocks": []}},
            "model_review": {"knowledge_value": "worthy", "confidence": 0.91},
            "human_review": {"knowledge_value": "unworthy", "training_eligible": "否"},
        },
        review_metadata={},
    )
    pending = SimpleNamespace(
        id="ing-2",
        event_id="batch-1",
        review_status="pending",
        candidate_payload={"knowledge": {"title": "待确认"}, "human_review": {}},
        review_metadata={},
    )
    dataset, counts = _manual_training_dataset([candidate, pending])
    assert len(dataset) == 1
    assert dataset[0]["id"] == "ing-1"
    assert dataset[0]["evaluation_scope"] == "shadow_only"
    assert dataset[0]["not_for_weight_training"] is True
    assert dataset[0]["evaluation_eligible"] is False
    assert dataset[0]["input_completeness"] == "not_evaluable"
    assert sum(counts.values()) == 1


def test_prompt_optimization_snapshot_is_shadow_only_and_error_first():
    snapshot = _prompt_optimization_snapshot([
        {"id": "wrong", "title": "错例", "content": "正文", "recommended_reply": "回复", "model_label": "worthy", "human_label": "unworthy", "model_draft_disposition": "approved", "human_draft_disposition": "not_applicable"},
        {"id": "same", "title": "对例", "content": "正文", "recommended_reply": "回复", "model_label": "worthy", "human_label": "worthy", "model_draft_disposition": "approved", "human_draft_disposition": "approved"},
    ])
    assert snapshot["evaluation_scope"] == "shadow_only"
    assert snapshot["representative_cases"][0]["id"] == "wrong"


def test_shadow_sample_completeness_keeps_partial_data_but_skips_empty_shells():
    partial = _shadow_sample_completeness({
        "content": "有效正文",
        "recommended_reply": "",
        "evidence_excerpt": None,
    })
    assert partial["evaluation_eligible"] is True
    assert partial["input_completeness"] == "partial"
    assert partial["missing_fields"] == ["recommended_reply", "evidence_excerpt"]

    empty = _shadow_sample_completeness({
        "content": {"blocks": []},
        "recommended_reply": "  ",
        "evidence_excerpt": [],
    })
    assert empty["evaluation_eligible"] is False
    assert empty["input_completeness"] == "not_evaluable"
    assert "无法进行新旧 Prompt 对比" in empty["not_evaluable_reason"]


def test_shadow_comparison_requires_improvement_and_no_regression():
    rows = [
        {"baseline": {"value_correct": False, "draft_correct": False, "strict_correct": False}, "candidate": {"value_correct": True, "draft_correct": True, "strict_correct": True}},
        {"baseline": {"value_correct": True, "draft_correct": True, "strict_correct": True}, "candidate": {"value_correct": True, "draft_correct": True, "strict_correct": True}},
    ]
    comparison = _shadow_comparison(rows)
    assert comparison["strict_accuracy_delta"] == 0.5
    assert comparison["improved_count"] == 1
    assert comparison["regressed_count"] == 0


def test_regression_revision_requires_review_and_preserves_truth_boundary():
    job = SimpleNamespace(
        status="shadow_rerun_review_pending",
        candidate_prompt="v1",
        prompt_versions=[{"version": "v1-candidate", "prompt": "v1"}],
        regression_reviews={},
        shadow_evaluation={"status": "completed", "rows": [{
            "id": "ing-1", "title": "退化", "human_label": "worthy",
            "human_draft_disposition": "approved",
            "baseline": {"strict_correct": True},
            "candidate": {"strict_correct": False},
        }]},
    )
    ok, reason = _revision_preconditions(job)
    assert not ok and "未完成人工归因" in reason
    job.regression_reviews = {"ing-1": {
        "classification": "candidate_prompt_error",
        "note": "门槛过严",
        "keep_as_regression_case": True,
    }}
    ok, reason = _revision_preconditions(job)
    assert ok and not reason
    snapshot = _prompt_revision_snapshot(job)
    assert snapshot["base_prompt_version"] == "v1-candidate"
    assert snapshot["regression_cases"][0]["human_truth"]["knowledge_value"] == "worthy"


def test_truth_correction_blocks_prompt_revision_until_candidate_review():
    job = SimpleNamespace(
        status="shadow_rerun_review_pending", candidate_prompt="v1", prompt_versions=[],
        regression_reviews={"ing-1": {"classification": "human_truth_correction_required", "note": "需回候选复核", "keep_as_regression_case": True}},
        shadow_evaluation={"status": "completed", "rows": [{"id": "ing-1", "baseline": {"strict_correct": True}, "candidate": {"strict_correct": False}}]},
    )
    ok, reason = _revision_preconditions(job)
    assert not ok and "候选价值复核" in reason
