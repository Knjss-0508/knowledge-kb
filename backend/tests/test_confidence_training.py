from datetime import datetime
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.services.confidence_training import aggregate, confidence_band, item_payload, settings_snapshot, update_item, update_settings
from app.schemas.confidence_training import ConfidenceTrainingJob as ConfidenceTrainingJobSchema
from app.schemas.confidence_training import ConfidenceTrainingOverview
from app.routes.confidence_training import (
    CONFIDENCE_PROMPT_SAMPLE_LIMIT,
    _adoption_preconditions,
    _draft_prompt_training_dataset,
    _draft_replay_comparison,
    _manual_training_dataset,
    _prompt_optimization_snapshot,
    _prompt_revision_snapshot,
    _revision_preconditions,
    _run_shadow_rerun,
    _shadow_comparison,
    _training_job_payload,
    _shadow_rerun_samples,
    _shadow_sample_completeness,
    decide_confidence_prompt,
    start_confidence_training_shadow_rerun,
)


def test_confidence_band_boundaries_are_frozen():
    assert confidence_band(0.0) == "B0"
    assert confidence_band(0.6999) == "B0"
    assert confidence_band(0.7) == "B1"
    assert confidence_band(0.85) == "B2"
    assert confidence_band(0.95) == "B3"
    assert confidence_band(1.0) == "B3"
    assert confidence_band(1.01) is None


def test_unevaluable_truth_is_not_counted_as_correct():
    items = [
        {"confidence_band": "B3", "truth_status": "confirmed", "strict_correct": True, "acceptable_correct": True, "error_severity": None},
        {"confidence_band": "B3", "truth_status": "not_evaluable", "strict_correct": None, "acceptable_correct": None, "error_severity": None},
    ]
    overview, bands = aggregate(items)
    assert overview["valid_truth"] == 1
    assert overview["strict_accuracy"] == 1.0
    assert overview["coverage"] == 0.5
    assert bands[3]["n_scored"] == 2
    assert bands[3]["n_valid_truth"] == 1


def test_every_partition_metric_exposes_sample_model_and_prompt_provenance():
    sample = {
        "id": "candidate-1",
        "candidate_id": "candidate-1",
        "evaluation_sample_id": "eval-sample-1",
        "model_version": "model-v7",
        "prompt_version": "prompt-v12",
        "confidence_band": "B2",
        "dataset_split": "validation",
        "truth_status": "confirmed",
        "strict_correct": True,
        "acceptable_correct": True,
        "error_severity": None,
    }

    overview, _ = aggregate([sample])
    validation = next(
        row for row in overview["split_summary"]
        if row["dataset_split"] == "validation"
    )
    band = next(
        row for row in overview["partitioned_bands"]
        if row["dataset_split"] == "validation" and row["band"] == "B2"
    )

    expected = {
        "sample_id": "eval-sample-1",
        "candidate_id": "candidate-1",
        "model_version": "model-v7",
        "prompt_version": "prompt-v12",
    }
    assert validation["sample_provenance"] == [expected]
    assert band["sample_provenance"] == [expected]


def test_frozen_truth_counts_in_partition_accuracy_without_a_confidence_score():
    sample = {
        "id": "candidate-1",
        "candidate_id": "candidate-1",
        "evaluation_sample_id": "eval-sample-1",
        "model_version": "model-v7",
        "prompt_version": "prompt-v12",
        "confidence_band": None,
        "dataset_split": "test",
        "truth_status": "confirmed",
        "strict_correct": True,
        "acceptable_correct": True,
        "error_severity": None,
    }

    overview, _ = aggregate([sample])
    test_split = next(
        row for row in overview["split_summary"]
        if row["dataset_split"] == "test"
    )

    assert overview["total"] == 1
    assert overview["scored"] == 0
    assert overview["valid_truth"] == 1
    assert overview["strict_accuracy"] == 1
    assert test_split["sample_count"] == 1
    assert test_split["n_valid_truth"] == 1
    assert test_split["strict_accuracy"] == 1
    assert test_split["sample_provenance"][0]["sample_id"] == "eval-sample-1"


def test_overview_response_schema_preserves_sample_provenance():
    sample = {
        "id": "candidate-1",
        "candidate_id": "candidate-1",
        "evaluation_sample_id": "eval-sample-1",
        "model_version": "model-v7",
        "prompt_version": "prompt-v12",
        "confidence_band": "B2",
        "dataset_split": "validation",
        "truth_status": "confirmed",
        "strict_correct": True,
        "acceptable_correct": True,
        "error_severity": None,
    }
    overview, bands = aggregate([sample])

    response = ConfidenceTrainingOverview.model_validate({
        **overview,
        "bands": bands,
        "settings": settings_snapshot(),
    })

    assert response.sample_provenance[0]["sample_id"] == "eval-sample-1"
    assert response.split_summary[1]["sample_provenance"][0]["prompt_version"] == "prompt-v12"


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


@pytest.mark.parametrize("human_fields", [
    {"usability": "usable"},
    {"decision": "approved"},
    {"decision": "approved_with_changes"},
])
def test_website_review_confirms_current_draft_without_human_disposition(human_fields):
    item = SimpleNamespace(
        id="website-review",
        event_id="batch-website",
        review_status="revision_required",
        candidate_payload={
            "knowledge": {"title": "已完成人工价值复核的当前草稿"},
            "model_review": {
                "knowledge_value": "worthy",
                "confidence": 0.94,
                "suggested_action": "return_for_revision",
            },
            "human_review": {"knowledge_value": "worthy", **human_fields},
        },
        review_metadata={
            "draft_version": 2,
            "human_review_draft_version": 2,
            "draft_revision_history": [{"draft_version": 2}],
            "model_annotation": {"status": "completed", "draft_version": 2},
        },
    )

    payload = item_payload(item)

    assert payload["truth_status"] == "confirmed"
    assert payload["human_draft_disposition"] == "approved"
    assert payload["model_draft_disposition"] == "revision_required"
    assert payload["strict_correct"] is False
    assert payload["acceptable_correct"] is True
    assert "draft_disposition" not in item.candidate_payload["human_review"]


@pytest.mark.parametrize("human_value", ["worthy", "unworthy"])
@pytest.mark.parametrize("invalid_binding", [
    "old_human_review",
    "unbound_human_review",
    "old_model_review",
    "unbound_model_review",
    "stale_model_annotation",
    "failed_model_annotation",
    "missing_model_review",
])
def test_invalid_review_binding_cannot_reuse_cached_evaluation(human_value, invalid_binding):
    metadata = {
        "draft_version": 2,
        "human_review_draft_version": 2,
        "draft_revision_history": [{"draft_version": 2}],
        "model_annotation": {"status": "completed", "draft_version": 2},
        "confidence_training": {
            "model_correctness": "strict_correct",
            "strict_correct": True,
            "acceptable_correct": True,
            "training_candidate_status": "qualified",
        },
    }
    model = {"knowledge_value": "worthy", "suggested_action": "submit_for_human_review"}
    if invalid_binding == "old_human_review":
        metadata["human_review_draft_version"] = 1
    elif invalid_binding == "unbound_human_review":
        metadata.pop("human_review_draft_version")
    elif invalid_binding == "old_model_review":
        metadata["model_annotation"]["draft_version"] = 1
    elif invalid_binding == "unbound_model_review":
        metadata["model_annotation"].pop("draft_version")
    elif invalid_binding == "stale_model_annotation":
        metadata["model_annotation"]["status"] = "stale_after_draft_revision"
    elif invalid_binding == "failed_model_annotation":
        metadata["model_annotation"]["status"] = "failed"
    else:
        model = {}
    item = SimpleNamespace(
        id="invalid-review",
        event_id="batch-invalid",
        review_status="pending",
        candidate_payload={
            "model_review": model,
            "human_review": {
                "knowledge_value": human_value,
                "usability": "usable",
                "draft_disposition": "approved",
            },
        },
        review_metadata=metadata,
    )

    payload = update_item(item, {"review_note": "仅更新评测备注"})

    assert payload["truth_status"] == "pending"
    assert payload["model_correctness"] == "not_evaluable"
    assert payload["strict_correct"] is None
    assert payload["acceptable_correct"] is None
    assert payload["training_candidate_status"] == "not_recommended"
    assert item.review_metadata["confidence_training"]["truth_status"] == "pending"
    assert item.candidate_payload["human_review"]["knowledge_value"] == human_value


@pytest.mark.parametrize("metadata", [
    {"draft_version": 2},
    {"draft_revision_history": [{"draft_version": 2}]},
    {"human_review_draft_version": None},
    {"human_review_draft_version": True},
])
def test_only_unchanged_legacy_v1_may_omit_review_versions(metadata):
    item = SimpleNamespace(
        id="legacy-binding",
        event_id="legacy-batch",
        review_status="ready",
        candidate_payload={
            "model_review": {"knowledge_value": "worthy", "draft_quality": "approved"},
            "human_review": {"knowledge_value": "worthy", "draft_disposition": "approved"},
        },
        review_metadata=metadata,
    )

    assert item_payload(item)["truth_status"] == "pending"


def test_legacy_v1_keeps_valid_disposition_with_empty_normalized_review_fields():
    item = SimpleNamespace(
        id="legacy-normalized",
        event_id="legacy-batch",
        review_status="revision_required",
        candidate_payload={
            "model_review": {
                "knowledge_value": "worthy",
                "suggested_action": "submit_for_human_review",
            },
            "human_review": {
                "knowledge_value": "worthy",
                "usability": "pending",
                "decision": "",
                "draft_disposition": "revision_required",
            },
        },
        review_metadata={},
    )

    payload = item_payload(item)

    assert payload["truth_status"] == "confirmed"
    assert payload["human_draft_disposition"] == "revision_required"
    assert payload["strict_correct"] is False


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


def test_manual_training_dataset_uses_confirmed_truth_and_deterministic_splits():
    candidate = {
        "id": "sample-1",
        "candidate_id": "ing-1",
        "snapshot_id": "snapshot-1",
        "source_batch": "batch-1",
        "topic_group": "topic-1",
        "draft_version": "draft-1",
        "dataset_split": "test",
        "draft_snapshot": {
            "title": "人工确认不沉淀的候选",
            "content": {"blocks": []},
            "recommended_reply": "",
            "evidence_excerpt": "",
        },
        "published_knowledge_snapshot": {},
        "model_output": {
            "knowledge_value": "worthy",
            "confidence": 0.91,
            "suggested_action": "submit_for_human_review",
        },
        "model_version": "model-v2",
        "prompt_version": "prompt-v2",
        "human_truth": {
            "knowledge_value": "unworthy",
            "draft_disposition": "not_applicable",
        },
    }
    dataset, counts = _manual_training_dataset([candidate])
    assert len(dataset) == 1
    assert dataset[0]["id"] == "sample-1"
    assert dataset[0]["candidate_id"] == "ing-1"
    assert dataset[0]["split"] == "test"
    assert dataset[0]["evaluation_scope"] == "shadow_only"
    assert dataset[0]["not_for_weight_training"] is True
    assert dataset[0]["evaluation_eligible"] is False
    assert dataset[0]["input_completeness"] == "not_evaluable"
    assert sum(counts.values()) == 1


def test_prompt_dataset_uses_published_worthy_samples_and_unworthy_samples_directly():
    def sample(sample_id, *, label, split, published):
        return {
            "id": sample_id,
            "candidate_id": "candidate-" + sample_id,
            "snapshot_id": "snapshot-1",
            "source_batch": "batch-1",
            "topic_group": "topic-" + sample_id,
            "draft_version": "draft-1",
            "dataset_split": split,
            "draft_snapshot": {
                "title": sample_id,
                "content": "候选草稿正文",
                "recommended_reply": "候选回复",
                "evidence_excerpt": "来源证据",
            },
            "published_knowledge_snapshot": (
                {
                    "knowledge_id": "knowledge-" + sample_id,
                    "title": sample_id,
                    "content": "已发布正文",
                    "recommended_reply": "已发布回复",
                }
                if published
                else {}
            ),
            "model_output": {
                "knowledge_value": "worthy",
                "suggested_action": "submit_for_human_review",
            },
            "model_version": "model-v3",
            "prompt_version": "prompt-v4",
            "human_truth": {
                "knowledge_value": label,
                "draft_disposition": "approved" if label == "worthy" else "not_applicable",
            },
        }

    unpublished_worthy = sample(
        "unpublished-worthy", label="worthy", split="train", published=False
    )
    published_worthy = sample(
        "published-worthy", label="worthy", split="validation", published=True
    )
    direct_unworthy = sample(
        "direct-unworthy", label="unworthy", split="test", published=False
    )

    dataset, counts = _manual_training_dataset(
        [unpublished_worthy, published_worthy, direct_unworthy]
    )

    assert {item["id"] for item in dataset} == {"published-worthy", "direct-unworthy"}
    published = next(item for item in dataset if item["id"] == "published-worthy")
    unworthy = next(item for item in dataset if item["id"] == "direct-unworthy")
    assert published["content"] == "已发布正文"
    assert published["recommended_reply"] == "已发布回复"
    assert unworthy["content"] == "候选草稿正文"
    assert unworthy["human_label"] == "unworthy"
    assert published["model_version"] == "model-v3"
    assert published["prompt_version"] == "prompt-v4"
    assert counts == {"train": 0, "validation": 1, "test": 1}


def test_manual_training_dataset_caps_at_200_without_splitting_topics():
    rows = []
    for index in range(260):
        human_value = "worthy" if index % 2 else "unworthy"
        rows.append({
            "id": f"sample-{index:03d}",
            "candidate_id": f"ing-{index:03d}",
            "snapshot_id": "snapshot-1",
            "source_batch": f"batch-{index // 20}",
            "topic_group": f"topic-{index:03d}",
            "draft_version": f"draft-{index}",
            "dataset_split": ("train", "validation", "test")[index % 3],
            "draft_snapshot": {
                "title": f"样本 {index}",
                "content": f"草稿正文 {index}",
                "recommended_reply": f"草稿回复 {index}",
                "evidence_excerpt": f"来源证据 {index}",
            },
            "published_knowledge_snapshot": (
                {
                    "knowledge_id": f"knowledge-{index}",
                    "title": f"样本 {index}",
                    "content": f"已发布正文 {index}",
                    "recommended_reply": f"已发布回复 {index}",
                }
                if human_value == "worthy"
                else {}
            ),
            "model_output": {
                "knowledge_value": "worthy",
                "confidence": 0.5 + (index % 50) / 100,
                "suggested_action": "submit_for_human_review",
            },
            "model_version": "model-v3",
            "prompt_version": "prompt-v4",
            "human_truth": {
                "knowledge_value": human_value,
                "draft_disposition": "approved" if human_value == "worthy" else "not_applicable",
            },
        })

    first, first_counts = _manual_training_dataset(rows)
    second, second_counts = _manual_training_dataset(list(reversed(rows)))

    assert CONFIDENCE_PROMPT_SAMPLE_LIMIT == 200
    assert len(first) == 200
    assert sum(first_counts.values()) == 200
    assert all(first_counts[split] for split in ("train", "validation", "test"))
    assert second_counts == first_counts
    assert [(row["id"], row["split"]) for row in second] == [
        (row["id"], row["split"]) for row in first
    ]
    assert len({row["id"] for row in first}) == 200
    topics_by_split = {}
    for row in first:
        topics_by_split.setdefault(row["topic_group"], set()).add(row["split"])
    assert all(len(splits) == 1 for splits in topics_by_split.values())


def test_shadow_rerun_samples_only_selects_requested_validation_or_test_split():
    dataset = [
        {"id": "discover", "split": "train"},
        {"id": "validate", "split": "validation"},
        {"id": "final", "split": "test"},
    ]

    assert [row["id"] for row in _shadow_rerun_samples(dataset, "validation")] == ["validate"]
    assert [row["id"] for row in _shadow_rerun_samples(dataset, "test")] == ["final"]


def test_draft_prompt_dataset_uses_real_before_after_revision_history():
    rows = [{
        "id": f"sample-{index}",
        "candidate_id": f"ing-{index}",
        "source_batch": "batch-1",
        "topic_group": f"topic-{index}",
        "draft_version": f"draft-{index}",
        "dataset_split": ("train", "validation", "test")[index % 3],
        "draft_snapshot": {
            "title": f"草稿 {index}",
            "content": f"候选正文 {index}",
            "recommended_reply": "候选回复",
            "evidence_excerpt": "来源证据",
            "answer_hub_replay_input": {
                "topic_id": f"TOP-{index}",
                "产品类型": "笔记本",
                "主题事实证据包": {
                    "facts": [{"fact_id": f"F-{index}"}]
                },
            },
        },
        "published_knowledge_snapshot": {
            "knowledge_id": f"knowledge-{index}",
            "title": f"已发布知识 {index}",
            "content": f"已发布正文 {index}",
            "recommended_reply": "已发布回复",
        },
        "draft_revision_history": [{
            "revision": 1,
            "source": "human_edit",
            "before": {"content": f"原正文 {index}", "recommended_reply": "原回复"},
            "after": {"content": f"人工正文 {index}", "recommended_reply": "人工回复"},
        }],
        "human_truth": {"knowledge_value": "worthy"},
        "model_version": "draft-model-v1",
        "prompt_version": "draft-prompt-v1",
    } for index in range(210)]

    dataset, counts = _draft_prompt_training_dataset(rows)

    assert len(dataset) == 200
    assert sum(counts.values()) == 200
    assert all(counts[split] for split in ("train", "validation", "test"))
    assert dataset[0]["before"] != dataset[0]["after"]
    assert dataset[0]["after"]["content"].startswith("已发布正文")
    assert dataset[0]["source"] == "published_knowledge_with_human_revision"
    assert dataset[0]["evidence_excerpt"] == "来源证据"
    assert dataset[0]["answer_hub_replay_input"]["topic_id"].startswith("TOP-")
    assert dataset[0]["prompt_version"] == "draft-prompt-v1"


def test_draft_prompt_dataset_excludes_unpublished_and_not_worthy_samples():
    sample = {
        "id": "sample-1",
        "candidate_id": "candidate-1",
        "dataset_split": "train",
        "draft_snapshot": {"evidence_excerpt": "证据"},
        "draft_revision_history": [{
            "before": {"content": "旧"},
        }],
        "human_truth": {"knowledge_value": "worthy"},
    }
    unpublished, counts = _draft_prompt_training_dataset([sample])
    assert unpublished == []
    assert counts == {"train": 0, "validation": 0, "test": 0}


def test_prompt_optimization_snapshot_is_shadow_only_and_error_first():
    snapshot = _prompt_optimization_snapshot([
        {"id": "wrong", "split": "train", "title": "错例", "content": "正文", "recommended_reply": "回复", "model_label": "worthy", "human_label": "unworthy", "model_draft_disposition": "approved", "human_draft_disposition": "not_applicable"},
        {"id": "same", "split": "validation", "title": "对例", "content": "正文", "recommended_reply": "回复", "model_label": "worthy", "human_label": "worthy", "model_draft_disposition": "approved", "human_draft_disposition": "approved"},
        {"id": "test", "split": "test", "title": "测试集", "content": "正文", "recommended_reply": "回复", "model_label": "unworthy", "human_label": "unworthy", "model_draft_disposition": "not_applicable", "human_draft_disposition": "not_applicable"},
    ])
    assert snapshot["evaluation_scope"] == "shadow_only"
    assert snapshot["representative_cases"][0]["id"] == "wrong"
    assert all(item["split"] == "train" for item in snapshot["representative_cases"])


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


def test_shadow_comparison_keeps_improved_candidate_reviewable_with_regressions():
    rows = [
        {"baseline": {"value_correct": False, "draft_correct": False, "strict_correct": False}, "candidate": {"value_correct": True, "draft_correct": True, "strict_correct": True}},
        {"baseline": {"value_correct": False, "draft_correct": False, "strict_correct": False}, "candidate": {"value_correct": True, "draft_correct": True, "strict_correct": True}},
        {"baseline": {"value_correct": True, "draft_correct": True, "strict_correct": True}, "candidate": {"value_correct": False, "draft_correct": False, "strict_correct": False}},
        {"baseline": {"value_correct": True, "draft_correct": True, "strict_correct": True}, "candidate": {"value_correct": True, "draft_correct": True, "strict_correct": True}},
    ]
    comparison = _shadow_comparison(rows)
    assert comparison["strict_accuracy_delta"] == 0.25
    assert comparison["improved_count"] == 2
    assert comparison["regressed_count"] == 1
    assert comparison["gate_passed"] is True
    assert comparison["regression_review_required"] is True
    assert "人工分析" in comparison["recommended_action"]


def test_shadow_comparison_keeps_sample_and_paired_prompt_provenance():
    row = {
        "id": "eval-sample-1",
        "evaluation_sample_id": "eval-sample-1",
        "candidate_id": "ing-1",
        "source_model_version": "source-model-v1",
        "source_prompt_version": "source-prompt-v1",
        "baseline": {
            "value_correct": True,
            "draft_correct": True,
            "strict_correct": True,
            "resolved_model_version": "shadow-model-v2",
            "prompt_version": "production-v3",
        },
        "candidate": {
            "value_correct": True,
            "draft_correct": True,
            "strict_correct": True,
            "resolved_model_version": "shadow-model-v2",
            "prompt_version": "candidate-v1",
        },
    }

    comparison = _shadow_comparison([row])

    assert comparison["baseline"]["sample_provenance"] == [{
        "sample_id": "eval-sample-1",
        "candidate_id": "ing-1",
        "source_model_version": "source-model-v1",
        "source_prompt_version": "source-prompt-v1",
        "model_version": "shadow-model-v2",
        "prompt_version": "production-v3",
    }]
    assert comparison["candidate"]["sample_provenance"][0]["prompt_version"] == "candidate-v1"


def test_regression_revision_requires_review_and_preserves_truth_boundary():
    job = SimpleNamespace(
        status="shadow_rerun_review_pending",
        candidate_prompt="v1",
        prompt_versions=[{"version": "v1-candidate", "prompt": "v1"}],
        regression_reviews={},
        shadow_evaluation={"status": "completed", "target_split": "validation", "rows": [{
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


def _adoption_job(rows, **overrides):
    job = SimpleNamespace(
        status="shadow_test_review_pending",
        candidate_prompt="候选 Prompt",
        prompt_versions=[{"version": "v1-candidate", "prompt": "候选 Prompt", "status": "diagnostic_pending"}],
        regression_reviews={},
        shadow_evaluation={
            "status": "completed",
            "target_split": "test",
            "candidate_prompt_version": "v1-candidate",
            "rows": rows,
            "comparison": _shadow_comparison(rows),
        },
    )
    for key, value in overrides.items():
        setattr(job, key, value)
    return job


def test_prompt_approval_requires_test_split_comparison():
    job = _adoption_job([
        _shadow_row("eval-1", False, True),
        _shadow_row("eval-2", False, True),
    ])
    job.status = "shadow_validation_review_pending"
    job.shadow_evaluation["target_split"] = "validation"

    ok, reason = _adoption_preconditions(job)

    assert not ok
    assert "测试集" in reason



def test_validation_regressions_must_be_reviewed_before_test_rerun():
    rows = [
        _shadow_row("validation-improved-1", False, True),
        _shadow_row("validation-improved-2", False, True),
        _shadow_row("validation-regressed", True, False),
    ]
    job = SimpleNamespace(
        id="job-validation-gate",
        status="shadow_validation_review_pending",
        candidate_prompt="候选 Prompt",
        prompt_versions=[{"version": "v1-candidate", "prompt": "候选 Prompt"}],
        regression_reviews={},
        analysis_result={"optimization_target": "review"},
        dataset_payload=[{"id": "test-sample", "split": "test"}],
        shadow_evaluation={
            "status": "completed",
            "target_split": "validation",
            "candidate_prompt_version": "v1-candidate",
            "rows": rows,
            "comparison": _shadow_comparison(rows),
            "phase_results": {},
        },
    )
    db = SimpleNamespace(
        query=lambda *_args: SimpleNamespace(
            filter=lambda *_filters: SimpleNamespace(first=lambda: job)
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        start_confidence_training_shadow_rerun(
            job.id,
            split="test",
            db=db,
            _=None,
        )

    assert exc_info.value.status_code == 409
    assert "请先复核验证集退化样本" in str(exc_info.value.detail)


def test_final_prompt_approval_requires_review_of_regressions_in_all_completed_splits():
    job = _adoption_job([
        _shadow_row("test-improved-1", False, True),
        _shadow_row("test-improved-2", False, True),
    ])
    validation_regression = _shadow_row("validation-regressed", True, False)
    validation_regression.update({
        "split": "validation",
        "candidate_prompt_version": "v1-candidate",
    })
    job.shadow_evaluation["phase_results"] = {
        "v1-candidate:validation": {
            "status": "completed",
            "target_split": "validation",
            "candidate_prompt_version": "v1-candidate",
            "rows": [validation_regression],
        },
    }

    ok, reason = _adoption_preconditions(job)

    assert not ok
    assert "未完成人工归因" in reason

    job.regression_reviews = {
        "v1-candidate:validation-regressed": {
            "classification": "candidate_prompt_error",
            "note": "验证集回归已复核",
            "keep_as_regression_case": True,
        },
    }
    ok, reason = _adoption_preconditions(job)

    assert ok and not reason


def test_previous_prompt_regression_review_cannot_satisfy_new_prompt_gate():
    job = _adoption_job([
        _shadow_row("test-improved-1", False, True),
        _shadow_row("test-improved-2", False, True),
    ])
    job.candidate_prompt = "v2 Prompt"
    job.prompt_versions = [
        {"version": "v1.1-candidate", "prompt": "v1.1 Prompt", "source": "regression_review"},
        {"version": "v2-candidate", "prompt": "v2 Prompt", "source": "regression_review"},
    ]
    validation_regression = _shadow_row("validation-regressed", True, False)
    validation_regression.update({
        "split": "validation",
        "candidate_prompt_version": "v2-candidate",
    })
    job.shadow_evaluation["phase_results"] = {
        "v2-candidate:validation": {
            "status": "completed",
            "target_split": "validation",
            "candidate_prompt_version": "v2-candidate",
            "rows": [validation_regression],
        },
    }
    job.regression_reviews = {
        "v1.1-candidate:validation-regressed": {
            "classification": "candidate_prompt_error",
            "note": "旧 Prompt 的复核",
            "keep_as_regression_case": True,
        },
    }

    ok, reason = _adoption_preconditions(job)

    assert not ok
    assert "未完成人工归因" in reason


def test_training_job_payload_reports_current_prompt_partition_review_progress():
    rows = [
        _shadow_row("validation-regressed", True, False),
        _shadow_row("test-improved", False, True),
    ]
    rows[0].update({"split": "validation", "candidate_prompt_version": "v1-candidate"})
    rows[1].update({"split": "test", "candidate_prompt_version": "v1-candidate"})
    job = SimpleNamespace(
        id="job-partition-summary",
        status="shadow_test_review_pending",
        stage="测试集完成",
        sample_count=2,
        train_count=0,
        validation_count=1,
        test_count=1,
        model_name="test-model",
        created_at=datetime(2026, 10, 12),
        updated_at=datetime(2026, 10, 12),
        requested_by="reviewer",
        error_message="",
        evaluation_scope="frozen",
        dataset_hash="hash",
        analysis_result={},
        candidate_prompt="候选 Prompt",
        resolved_model_version="model-v1",
        prompt_versions=[{"version": "v1-candidate", "prompt": "候选 Prompt"}],
        regression_reviews={
            "v1-candidate:validation-regressed": {
                "classification": "candidate_prompt_error",
                "note": "已复核",
                "keep_as_regression_case": True,
            },
        },
        shadow_evaluation={
            "status": "completed",
            "target_split": "test",
            "candidate_prompt_version": "v1-candidate",
            "rows": [rows[1]],
            "phase_results": {
                "v1-candidate:validation": {
                    "status": "completed",
                    "target_split": "validation",
                    "candidate_prompt_version": "v1-candidate",
                    "rows": [rows[0]],
                },
            },
        },
    )

    payload = _training_job_payload(job)
    response = ConfidenceTrainingJobSchema.model_validate(payload)

    assert payload["regression_rows"] == [rows[0]]
    assert response.regression_partitions == [
        {
            "prompt_version": "v1-candidate",
            "split": "validation",
            "status": "completed",
            "regression_count": 1,
            "reviewed_count": 1,
            "pending_review_count": 0,
        },
        {
            "prompt_version": "v1-candidate",
            "split": "test",
            "status": "completed",
            "regression_count": 0,
            "reviewed_count": 0,
            "pending_review_count": 0,
        },
    ]

def test_draft_replay_comparison_uses_human_final_draft_as_truth():
    comparison = _draft_replay_comparison([
        {
            "baseline": {"title": "错", "content": "错", "recommended_reply": "错"},
            "candidate": {"title": "人工标题", "content": "人工正文", "recommended_reply": "人工回复"},
            "human_final_draft": {"title": "人工标题", "content": "人工正文", "recommended_reply": "人工回复"},
        },
        {
            "baseline": {"title": "人工标题2", "content": "人工正文2", "recommended_reply": "人工回复2"},
            "candidate": {"title": "人工标题2", "content": "人工正文2", "recommended_reply": "人工回复2"},
            "human_final_draft": {"title": "人工标题2", "content": "人工正文2", "recommended_reply": "人工回复2"},
        },
    ])

    assert comparison["baseline"]["strict_accuracy"] == 0.5
    assert comparison["candidate"]["strict_accuracy"] == 1.0
    assert comparison["improved_count"] == 1
    assert comparison["regressed_count"] == 0
    assert comparison["gate_passed"] is True


def test_draft_generation_prompt_can_start_answer_hub_validation_replay():
    job = SimpleNamespace(
        id="draft-job",
        status="draft_prompt_replay_required",
        stage="",
        error_message="",
        analysis_result={"optimization_target": "draft_generation"},
        candidate_prompt="候选草稿 Prompt",
        model_name="deepseek-flash",
        evaluation_scope="shadow_only",
        dataset_hash="hash",
        sample_count=1,
        train_count=0,
        validation_count=1,
        test_count=0,
        requested_by="reviewer",
        resolved_model_version="deepseek-flash",
        regression_reviews={},
        created_at=__import__("datetime").datetime(2026, 10, 16, 10, 0),
        updated_at=__import__("datetime").datetime(2026, 10, 16, 10, 0),
        prompt_versions=[{
            "version": "v1-candidate",
            "prompt": "候选草稿 Prompt",
            "status": "diagnostic_pending",
        }],
        dataset_payload=[{
            "id": "validation-1",
            "split": "validation",
            "answer_hub_replay_input": {
                "topic_id": "TOP-1",
                "产品类型": "笔记本",
                "主题事实证据包": {"facts": [{"fact_id": "F01"}]},
            },
            "after": {
                "title": "人工标题",
                "content": "人工正文",
                "recommended_reply": "人工回复",
            },
        }],
        shadow_evaluation={},
    )
    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return job

    class Db:
        def query(self, *_args):
            return Query()

        def add(self, _item):
            pass

        def commit(self):
            pass

        def refresh(self, _item):
            pass

    db = Db()

    result = start_confidence_training_shadow_rerun(
        "draft-job",
        split="validation",
        db=db,
        _=None,
    )

    assert result["status"] == "shadow_rerun_queued"
    assert job.shadow_evaluation["replay_type"] == "answer_hub_draft_transcription"
    assert job.shadow_evaluation["total_samples"] == 1


def test_draft_prompt_replay_stays_human_review_only_after_answer_hub_connection():
    job = SimpleNamespace(
        analysis_result={"optimization_target": "draft_generation"},
        candidate_prompt="候选草稿 Prompt",
        status="shadow_test_review_pending",
        shadow_evaluation={
            "status": "completed",
            "target_split": "test",
            "comparison": {"gate_passed": True},
        },
        regression_reviews={},
    )

    revision_ok, revision_reason = _revision_preconditions(job)
    adoption_ok, adoption_reason = _adoption_preconditions(job)

    assert not revision_ok
    assert "已接入 Answer Hub 离线回放" in revision_reason
    assert not adoption_ok
    assert "不能批准或应用到生产转写" in adoption_reason


def test_shadow_worker_sends_draft_prompt_to_answer_hub_replay(monkeypatch):
    job = SimpleNamespace(
        id="draft-job",
        status="shadow_rerun_queued",
        stage="",
        error_message="",
        analysis_result={"optimization_target": "draft_generation"},
        candidate_prompt="草稿候选 Prompt",
        prompt_versions=[{"version": "v1-candidate", "prompt": "草稿候选 Prompt"}],
        dataset_payload=[{
            "id": "validation-1",
            "split": "validation",
            "answer_hub_replay_input": {
                "topic_id": "TOP-1",
                "产品类型": "笔记本",
                "主题事实证据包": {"facts": [{"fact_id": "F01"}]},
            },
            "after": {
                "title": "人工标题",
                "content": "人工正文",
                "recommended_reply": "人工回复",
            },
        }],
        shadow_evaluation={
            "target_split": "validation",
            "candidate_prompt_version": "v1-candidate",
            "phase_results": {},
            "rows": [],
        },
        regression_reviews={},
    )

    class Query:
        def filter(self, *_args):
            return self

        def first(self):
            return job

    class Db:
        def query(self, *_args):
            return Query()

        def add(self, _item):
            pass

        def commit(self):
            pass

        def close(self):
            pass

    db = Db()
    monkeypatch.setattr("app.routes.confidence_training.SessionLocal", lambda: db)
    monkeypatch.setattr(
        "app.routes.confidence_training.shadow_rerun_transcribed_candidate",
        lambda **_kwargs: pytest.fail("草稿 Prompt 不得调用审核评测器"),
    )
    monkeypatch.setattr(
        "app.routes.confidence_training.replay_answer_hub_draft_prompts",
        lambda **kwargs: {
            "status": "completed",
            "model": "deepseek-flash",
            "completed_count": 1,
            "skipped_count": 0,
            "failed_count": 0,
            "rows": [{
                "id": "validation-1",
                "baseline": {
                    "title": "旧标题",
                    "content": "旧正文",
                    "recommended_reply": "旧回复",
                },
                "candidate": {
                    "title": "人工标题",
                    "content": "人工正文",
                    "recommended_reply": "人工回复",
                },
                "human_final_draft": {
                    "title": "人工标题",
                    "content": "人工正文",
                    "recommended_reply": "人工回复",
                },
            }],
            "skipped_samples": [],
            "failed_samples": [],
        },
    )

    _run_shadow_rerun(job.id)

    assert job.status == "shadow_validation_review_pending"
    assert job.shadow_evaluation["replay_type"] == "answer_hub_draft_transcription"
    assert job.shadow_evaluation["comparison"]["gate_passed"] is True


def test_approved_prompt_decision_cannot_be_overwritten_by_reject():
    job = _adoption_job([])
    job.status = "prompt_approved_for_future_candidates"
    db = SimpleNamespace(
        query=lambda *_args: SimpleNamespace(
            filter=lambda *_filters: SimpleNamespace(first=lambda: job)
        )
    )

    with pytest.raises(HTTPException) as exc_info:
        decide_confidence_prompt(
            "job-1",
            SimpleNamespace(decision="reject"),
            db=db,
            current_user=SimpleNamespace(username="reviewer"),
        )

    assert exc_info.value.status_code == 409
    assert "撤销批准" in str(exc_info.value.detail)


def _shadow_row(row_id, baseline_correct, candidate_correct):
    return {
        "id": row_id,
        "baseline": {"value_correct": baseline_correct, "draft_correct": baseline_correct, "strict_correct": baseline_correct},
        "candidate": {"value_correct": candidate_correct, "draft_correct": candidate_correct, "strict_correct": candidate_correct},
    }


def test_adoption_gate_passes_when_metrics_improve_without_regressions():
    # 零退化（regressed_count = 0）时，采用门禁不得再要求「可用于修订的退化样本」。
    rows = [_shadow_row("ing-1", False, True), _shadow_row("ing-2", False, True)]
    job = _adoption_job(rows)
    assert job.shadow_evaluation["comparison"]["gate_passed"] is True
    assert job.shadow_evaluation["comparison"]["regressed_count"] == 0
    ok, reason = _adoption_preconditions(job)
    assert ok and not reason
    # 修订候选 Prompt 的门禁保持不变：没有退化样本时仍然拒绝。
    ok, reason = _revision_preconditions(job)
    assert not ok and "退化样本" in reason


def test_adoption_gate_requires_metric_gate_and_candidate_prompt():
    regressed_rows = [_shadow_row("ing-1", True, False), _shadow_row("ing-2", True, False)]
    job = _adoption_job(regressed_rows)
    assert job.shadow_evaluation["comparison"]["gate_passed"] is False
    ok, reason = _adoption_preconditions(job)
    assert not ok and "验收指标未改善" in reason

    ok, reason = _adoption_preconditions(_adoption_job([], candidate_prompt=""))
    assert not ok and "没有可采纳的候选 Prompt" in reason


def test_adoption_gate_still_requires_regression_attribution_when_regressions_exist():
    rows = [
        _shadow_row("ing-1", False, True),
        _shadow_row("ing-2", False, True),
        _shadow_row("ing-3", True, False),
    ]
    job = _adoption_job(rows)
    comparison = job.shadow_evaluation["comparison"]
    assert comparison["gate_passed"] is True and comparison["regressed_count"] == 1
    ok, reason = _adoption_preconditions(job)
    assert not ok and "未完成人工归因" in reason
    job.regression_reviews = {"ing-3": {"classification": "candidate_prompt_error", "note": "门槛过严", "keep_as_regression_case": True}}
    ok, reason = _adoption_preconditions(job)
    assert ok and not reason
