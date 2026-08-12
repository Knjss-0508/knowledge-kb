from __future__ import annotations

from scripts.run_topic_stage_accuracy import (
    _apply_knowledge_value_evidence_guard,
    _theme_payload,
)


def _prediction() -> dict[str, object]:
    return {
        "topic_stage": "质检标准",
        "knowledge_value": "值得沉淀",
        "stage_reason": "询问判定口径。",
        "value_reason": "模型认为有复用价值。",
        "reusable_knowledge": "某现象判为正常。",
        "confidence": 0.9,
        "needs_human_review": False,
    }


def test_single_case_direct_conclusion_is_forced_unworthy() -> None:
    theme = {
        "member_count": 1,
        "normalized_issues": ["摄像头里有一根毛是否正常"],
        "judgment_targets": ["判断是否影响质检"],
        "resolution_modes": ["按正常处理"],
        "standard_paths": ["待确认"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "evidence_summaries": ["后台查看当前图片后回复正常。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "不值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is True
    assert guarded["needs_human_review"] is True


def test_single_case_with_explicit_threshold_can_remain_worthy() -> None:
    theme = {
        "member_count": 1,
        "normalized_issues": ["屏幕点状瑕疵怎么判"],
        "judgment_targets": ["区分坏点和漏液"],
        "resolution_modes": ["测量点状瑕疵直径"],
        "standard_paths": ["显示问题"],
        "thresholds_or_exceptions": ["直径大于1mm判漏液"],
        "evidence_summaries": ["标准明确给出1mm阈值。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False


def test_multi_case_without_reusable_evidence_is_also_forced_unworthy() -> None:
    theme = {
        "member_count": 2,
        "normalized_issues": ["同一主题的两个案例"],
        "thresholds_or_exceptions": ["无明确阈值"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "不值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is True


def test_negated_rule_terms_are_not_reusable_evidence() -> None:
    theme = {
        "member_count": 2,
        "normalized_issues": ["同一主题的两个案例"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "evidence_summaries": ["当前案例没有通用边界或核验步骤，只记录了机器正常。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "不值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is True


def test_explicit_steps_after_negated_threshold_remain_reusable() -> None:
    theme = {
        "member_count": 1,
        "normalized_issues": ["缺少数值阈值时如何补充核验信息"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "resolution_modes": ["没有明确阈值，但必须先拍摄全景图片，再核对设备型号。"],
        "evidence_summaries": ["来源明确要求先补充全景图片，再核对设备型号。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False


def test_question_only_threshold_is_not_reusable_evidence() -> None:
    theme = {
        "member_count": 2,
        "normalized_issues": ["屏幕点状瑕疵超过1mm是否异常"],
        "judgment_targets": ["判断是否超过1mm"],
        "resolution_modes": ["询问是否超过1mm"],
        "standard_paths": ["屏幕点状瑕疵判定步骤"],
        "thresholds_or_exceptions": ["是否超过1mm"],
        "evidence_summaries": ["用户只是在询问是否超过1mm，没有来源给出结论。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "不值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is True


def test_instruction_can_use_whether_as_its_verification_object() -> None:
    theme = {
        "member_count": 1,
        "resolution_modes": ["必须核对设备是否支持该功能"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "evidence_summaries": ["来源明确要求核对设备是否支持该功能。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False


def test_question_mark_keeps_assertive_looking_text_as_a_question() -> None:
    theme = {
        "member_count": 1,
        "resolution_modes": ["超过1mm按异常处理？"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "evidence_summaries": ["用户询问超过1mm是否按异常处理。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "不值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is True


def test_assertive_rule_can_start_with_whether() -> None:
    theme = {
        "member_count": 1,
        "resolution_modes": ["是否支持应以设备型号为准"],
        "thresholds_or_exceptions": ["无明确阈值"],
        "evidence_summaries": ["来源已确认该判断规则。"],
    }

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False


def test_theme_payload_preserves_available_source_judgment_evidence() -> None:
    theme = _theme_payload(
        "C001",
        [
            {
                "unit_id": "U001",
                "sample_id": "S001",
                "source_judgment_conclusion": "闭合瞬间单次异响属于转轴异响",
                "historical_actual_reply": "该情形属于转轴异响。",
                "source_conversation": "客服明确回复该情形属于转轴异响。",
            }
        ],
    )

    guarded = _apply_knowledge_value_evidence_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False
