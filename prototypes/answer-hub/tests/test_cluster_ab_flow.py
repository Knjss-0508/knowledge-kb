from __future__ import annotations

import pytest

from answer_hub.mimo import MimoError, _validate_cluster_units
from scripts.run_cluster_ab_test import _cluster_units, _new_semantic_text


def _unit(
    unit_id: str,
    *,
    product_category: str = "手机",
    scope_type: str = "品类专用",
    platform: str = "通用",
    brand: str = "通用",
    model_scope: str = "通用",
    category_l1: str = "显示问题",
    category_l2: str = "屏幕色斑",
    intent: str = "标准判定",
    standard_path: str = "屏幕显示异常判定",
    threshold_or_exception: str = "无",
) -> dict[str, object]:
    topic = {
        "normalized_issue": "屏幕出现局部颜色异常时如何判定",
        "product_category": product_category,
        "scope_type": scope_type,
        "platform": platform,
        "brand": brand,
        "model_scope": model_scope,
        "category_l1": category_l1,
        "category_l2": category_l2,
        "intent": intent,
        "subject": "屏幕",
        "phenomenon": "局部颜色异常",
        "judgment_target": "判断是否属于色斑",
        "resolution_mode": "按照显示异常标准判定",
        "standard_path": standard_path,
        "threshold_or_exception": threshold_or_exception,
        "evidence_summary": "这段证据文字不应进入向量文本。",
    }
    return {
        "unit_id": unit_id,
        "sample_id": unit_id,
        "semantic_text": _new_semantic_text(topic),
        **topic,
    }


def test_new_semantic_text_uses_standardized_fields_not_evidence_narrative() -> None:
    text = _new_semantic_text(_unit("A"))

    assert "产品" not in text
    assert "这段证据文字不应进入向量文本" not in text
    assert "屏幕显示异常判定" in text
    assert "判断是否属于色斑" in text


def test_business_rules_keep_different_product_categories_separate() -> None:
    phone = _unit("PHONE", product_category="手机")
    camera = _unit("CAMERA", product_category="相机")

    result = _cluster_units(
        [phone, camera],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert result.assignments["PHONE"] != result.assignments["CAMERA"]


def test_business_rules_keep_platform_specific_standards_separate() -> None:
    ios = _unit(
        "IOS",
        scope_type="平台专用",
        platform="iOS",
        brand="Apple",
    )
    android = _unit(
        "ANDROID",
        scope_type="平台专用",
        platform="Android",
        brand="小米",
    )

    result = _cluster_units(
        [ios, android],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert result.assignments["IOS"] != result.assignments["ANDROID"]


def test_category_scope_ignores_case_brand_and_model() -> None:
    ios_case = _unit(
        "IOS",
        scope_type="品类专用",
        platform="iOS",
        brand="Apple",
        model_scope="iPhone 17 Pro",
    )
    android_case = _unit(
        "ANDROID",
        scope_type="品类专用",
        platform="Android",
        brand="小米",
        model_scope="小米 14 Ultra",
    )

    result = _cluster_units(
        [ios_case, android_case],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert len(set(result.assignments.values())) == 1
    assert ios_case["platform"] == "通用"
    assert android_case["brand"] == "通用"


def test_business_rules_allow_one_to_many_cluster_when_all_members_match() -> None:
    units = [_unit("A"), _unit("B"), _unit("C")]

    result = _cluster_units(
        units,
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert len(set(result.assignments.values())) == 1


def test_business_rules_keep_different_standard_paths_separate() -> None:
    display_rule = _unit("DISPLAY", standard_path="屏幕显示异常判定")
    repair_rule = _unit("REPAIR", standard_path="屏幕拆修痕迹核验")

    result = _cluster_units(
        [display_rule, repair_rule],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert result.assignments["DISPLAY"] != result.assignments["REPAIR"]


def _cluster_unit_payload(**overrides: object) -> dict[str, object]:
    topic: dict[str, object] = {
        "normalized_issue": "手机｜屏幕｜颜色异常｜判断是否属于色斑",
        "product_category": "手机",
        "scope_type": "品类专用",
        "platform": "通用",
        "brand": "通用",
        "model_scope": "通用",
        "category_l1": "显示问题",
        "category_l2": "屏幕色斑",
        "intent": "标准判定",
        "subject": "屏幕",
        "phenomenon": "边缘偏绿、中间偏蓝",
        "judgment_target": "判断是否属于色斑",
        "resolution_mode": "按照显示异常标准判定",
        "standard_path": "屏幕显示异常判定",
        "threshold_or_exception": "无明确阈值",
        "evidence_summary": "聊天明确描述屏幕颜色异常。",
        "confidence": 0.9,
        "requires_review": False,
    }
    topic.update(overrides)
    return {
        "conversation_type": "single_topic",
        "reason": "会话只包含一个屏幕显示异常判定问题。",
        "topics": [topic],
    }


def test_cluster_unit_validation_forces_review_when_scope_is_uncertain() -> None:
    result = _validate_cluster_units(
        _cluster_unit_payload(standard_path="待确认", requires_review=False)
    )

    assert result["topics"][0]["requires_review"] is True


def test_cluster_unit_validation_rejects_display_issue_as_repair_category() -> None:
    with pytest.raises(MimoError, match="显示问题"):
        _validate_cluster_units(
            _cluster_unit_payload(category_l1="拆修问题")
        )
