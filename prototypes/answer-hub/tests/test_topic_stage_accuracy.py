from __future__ import annotations

from scripts.run_topic_stage_accuracy import (
    _apply_single_case_knowledge_guard,
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

    guarded = _apply_single_case_knowledge_guard(theme, _prediction())

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

    guarded = _apply_single_case_knowledge_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False


def test_multi_case_topic_is_not_forced_by_single_case_guard() -> None:
    theme = {
        "member_count": 2,
        "normalized_issues": ["同一主题的两个案例"],
        "thresholds_or_exceptions": ["无明确阈值"],
    }

    guarded = _apply_single_case_knowledge_guard(theme, _prediction())

    assert guarded["knowledge_value"] == "值得沉淀"
    assert guarded["knowledge_value_guard_applied"] is False
