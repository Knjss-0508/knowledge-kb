from __future__ import annotations

from answer_hub.auto_review import (
    AutoReviewPolicy,
    apply_auto_review_annotation,
    assess_auto_review_candidate,
    evaluate_auto_review_validation,
    partition_auto_review_candidates,
    select_candidates_for_submission,
)


def _model_pass(**overrides):
    row = {
        "主题ID": "TOP-001",
        "产品类型": "手机",
        "主标题": "屏幕异常核验",
        "知识内容": "按标准完成屏幕清洁、背景切换和测量后再判定。",
        "关联标准项": "STD-001",
        "模型初标状态": "topic_initial_reviewed_model",
        "模型初标提供方": "mimo",
        "模型初标模型名称": "mimo-v2.5-pro",
        "模型初标Prompt版本": "multi-category-topic-initial-review-v3",
        "模型初标结论": "通过",
        "模型初标是否值得沉淀": "值得沉淀",
        "模型初标置信度": 0.93,
        "模型初标重点复核": "否",
        "是否重点复核": "否",
        "主题分类状态": "topic_stage_classified_model",
        "主题分类提供方": "mimo",
        "主题分类置信度": 0.93,
        "主题分类重点复核": "否",
        "模型初标标准一致性": "一致",
        "模型初标证据充分性": "充分",
        "模型初标内容一致性": "一致",
        "模型初标标题质量": "清晰",
        "模型初标图片必要性": "不需要",
    }
    row.update(overrides)
    return row


def _policy(enabled: bool = False) -> AutoReviewPolicy:
    return AutoReviewPolicy(
        enabled=enabled,
        min_confidence=0.85,
        min_validation_samples=2,
        min_validation_accuracy=0.90,
        min_pass_precision=0.90,
        validated_model="mimo-v2.5-pro",
        validated_prompt_version="multi-category-topic-initial-review-v3",
    )


def test_auto_review_only_releases_strict_model_passes() -> None:
    approved, exceptions = partition_auto_review_candidates(
        [
            _model_pass(),
            _model_pass(主题ID="TOP-002", 模型初标置信度=0.7),
            _model_pass(主题ID="TOP-003", 模型初标重点复核="是"),
        ],
        _policy(enabled=True),
    )

    assert [row["主题ID"] for row in approved] == ["TOP-001"]
    assert {row["主题ID"] for row in exceptions} == {"TOP-002", "TOP-003"}


def test_auto_review_does_not_require_standard_reference_in_case_only_mode() -> None:
    approved, exceptions = partition_auto_review_candidates(
        [
            _model_pass(
                知识内容="先清洁屏幕，再切换背景复测并记录现象。",
                关联标准项="",
                模型初标标准一致性="无可信标准",
            )
        ],
        _policy(enabled=True),
    )

    assert [row["主题ID"] for row in approved] == ["TOP-001"]
    assert exceptions == []


def test_auto_review_does_not_release_knowledge_without_deposition_value() -> None:
    approved, exceptions = partition_auto_review_candidates(
        [_model_pass(模型初标是否值得沉淀="不值得沉淀")],
        _policy(enabled=True),
    )

    assert approved == []
    assert exceptions[0]["主题ID"] == "TOP-001"
    assert "值得沉淀" in exceptions[0]["自动审核原因"]


def test_auto_review_requires_successful_high_confidence_topic_classification() -> None:
    approved, exceptions = partition_auto_review_candidates(
        [
            _model_pass(主题ID="TOP-LOW", 主题分类置信度=0.7),
            _model_pass(主题ID="TOP-FOCUS", 主题分类重点复核="是"),
            _model_pass(
                主题ID="TOP-FALLBACK",
                主题分类状态="topic_stage_classified_rule",
                主题分类提供方="stage-rule",
            ),
        ],
        _policy(enabled=True),
    )

    assert approved == []
    assert {row["主题ID"] for row in exceptions} == {
        "TOP-LOW",
        "TOP-FOCUS",
        "TOP-FALLBACK",
    }


def test_validation_treats_teammate_modification_as_not_ready_for_auto_release() -> None:
    report = evaluate_auto_review_validation(
        [
            _model_pass(是否值得沉淀="是", 是否可用="是", 如何修改=""),
            _model_pass(
                主题ID="TOP-002",
                是否值得沉淀="是",
                是否可用="是",
                如何修改="需要精简",
            ),
        ],
        _policy(),
    )

    assert report["validated_rows"] == 2
    assert report["true_pass"] == 1
    assert report["false_pass"] == 1
    assert report["accuracy"] == 0.5
    assert not report["gate_ready"]


def test_production_mode_uses_model_while_validation_mode_uses_teammate_label() -> None:
    candidate = _model_pass(是否可用="", 如何修改="")
    validation_selected = select_candidates_for_submission([candidate], _policy(False))
    production_selected = select_candidates_for_submission([candidate], _policy(True))

    assert validation_selected == []
    assert production_selected[0]["审核结论"] == "通过"
    assert "模型自动标注" in production_selected[0]["审核备注"]


def test_validation_mode_requires_human_deposition_annotation() -> None:
    candidate = _model_pass(是否可用="是", 是否值得沉淀="")

    assert select_candidates_for_submission([candidate], _policy(False)) == []


def test_annotation_marks_validation_and_production_modes_differently() -> None:
    validation_row = apply_auto_review_annotation(_model_pass(), _policy(False))
    production_row = apply_auto_review_annotation(_model_pass(), _policy(True))

    assert validation_row["自动审核状态"] == "validation_auto_approve"
    assert production_row["自动审核状态"] == "auto_approved"


def test_auto_review_canary_is_limited_by_product_type() -> None:
    policy = AutoReviewPolicy(
        enabled=True,
        enabled_product_types=("手机",),
        validated_model="mimo-v2.5-pro",
        validated_prompt_version="multi-category-topic-initial-review-v3",
    )

    approved, reasons = assess_auto_review_candidate(
        _model_pass(产品类型="平板"),
        policy,
        enforce_deployment_version=True,
    )

    assert approved is False
    assert reasons == ["当前品类未进入自动审核灰度范围"]


def test_auto_review_kill_switch_overrides_enabled_flag(monkeypatch) -> None:
    monkeypatch.setenv("AUTO_REVIEW_ENABLED", "true")
    monkeypatch.setenv("AUTO_REVIEW_KILL_SWITCH", "true")

    policy = AutoReviewPolicy.from_env()

    assert policy.enabled is False
    assert policy.kill_switch is True
