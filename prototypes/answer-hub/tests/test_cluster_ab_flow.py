from __future__ import annotations

from types import SimpleNamespace

import pytest

from answer_hub.mimo import (
    CLUSTER_UNIT_PROMPT_VERSION,
    MimoError,
    MimoLabelResult,
    _validate_cluster_units,
)
from scripts.run_cluster_ab_test import (
    TEXT_ONLY_PROMPT_VERSION,
    SchemeResult,
    _analyze_row,
    _cache_entry_is_current,
    _cache_entry_needs_refresh,
    _cluster_units,
    _fallback_units,
    _invalid_source_reason,
    _local_multi_topic_rescue,
    _new_semantic_text,
    _prediction,
    _run_new_scheme_direct_mimo,
    _select_pairs,
    _source_state,
)
from answer_hub.workflow import (
    _direct_reconcile_bucket_compatible,
    _direct_reconcile_has_hard_conflict,
)


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


def test_multi_topic_prediction_uses_matching_atomic_cluster_before_override() -> None:
    left = {
        "conversation_type": "multi_topic",
        "cluster_ids": ["C001", "C009"],
    }
    right = {
        "conversation_type": "single_topic",
        "cluster_ids": ["C009"],
    }

    assert _prediction(left, right, allow_multi_topic=True) == "同一主题"


def test_multi_topic_prediction_splits_when_no_atomic_topic_matches() -> None:
    left = {
        "conversation_type": "multi_topic",
        "cluster_ids": ["C001", "C009"],
    }
    right = {
        "conversation_type": "single_topic",
        "cluster_ids": ["C010"],
    }

    assert _prediction(left, right, allow_multi_topic=True) == "多主题需拆分"


def test_prediction_never_merges_different_product_categories() -> None:
    left = {
        "conversation_type": "multi_topic",
        "product_category": "电脑",
        "cluster_ids": ["C001", "LOCAL-MULTI-S001"],
        "local_multi_topic_rescue": True,
    }
    right = {
        "conversation_type": "single_topic",
        "product_category": "耳机",
        "cluster_ids": ["C001"],
    }

    assert _prediction(left, right, allow_multi_topic=True) == "不同主题"


def test_prediction_prioritizes_local_multi_topic_rescue() -> None:
    left = {
        "conversation_type": "multi_topic",
        "product_category": "电脑",
        "cluster_ids": ["C001", "LOCAL-MULTI-S001"],
        "local_multi_topic_rescue": True,
    }
    right = {
        "conversation_type": "single_topic",
        "product_category": "电脑",
        "cluster_ids": ["C001"],
    }

    assert _prediction(left, right, allow_multi_topic=True) == "多主题需拆分"


def test_source_state_rescues_model_label_followup_as_multi_topic() -> None:
    row = {
        "样本ID": "S038",
        "核心问题": "底部标签被撕，需要核对具体型号",
        "聊天内容": (
            "底部的标签被他撕了\n"
            "怎么看第几款\n"
            "怎么核对\n"
            "有没有什么问题\n"
            "查官网是这一款\n"
            "后壳标签人为去除的话要勾选后壳无序列号\n"
            "显卡硬盘这些有问题吗\n"
            "内存硬盘都是品牌的"
        ),
    }
    scheme = SchemeResult(
        units=[
            {
                "unit_id": "S038-01",
                "sample_id": "S038",
                "conversation_type": "single_topic",
                "cluster_id": "C041",
            }
        ],
        assignments={},
        similarities={},
    )

    assert _local_multi_topic_rescue(row)
    state = _source_state(scheme, [row], new_scheme=True)["S038"]

    assert state["conversation_type"] == "multi_topic"
    assert state["local_multi_topic_rescue"] is True
    assert state["cluster_ids"] == ["C041", "LOCAL-MULTI-S038"]


def test_local_rescue_detects_repair_components_with_separate_standards() -> None:
    assert _local_multi_topic_rescue(
        {
            "核心问题": "主板有标签、屏幕有贴纸，不清楚怎么判定",
            "聊天内容": (
                "老师，看一下拆机图，主板有标签，屏幕有贴纸。这个怎么判定？\n"
                "这个是屏幕的其他非原厂特征\n"
                "贴纸的话我这边再确认下"
            ),
        }
    )


def test_local_rescue_detects_multiple_information_query_targets() -> None:
    assert _local_multi_topic_rescue(
        {
            "核心问题": "需要确认BIOS锁状态、型号、硬盘内存品牌和指纹支持",
            "聊天内容": (
                "有没有bios锁\n"
                "无锁吧\n"
                "没锁的\n"
                "型号是这个吗\n"
                "RedmiBook 14 2025\n"
                "硬盘内存品牌吗\n"
                "内存和硬盘都是品牌认证的\n"
                "这个机支持指纹吗\n"
                "不支持"
            ),
        }
    )


def test_select_pairs_skips_cross_product_pairs() -> None:
    rows = [
        {"样本ID": "S001", "产品类型": "手机"},
        {"样本ID": "S002", "产品类型": "电脑"},
        {"样本ID": "S003", "产品类型": "手机"},
    ]
    old_scheme = SchemeResult(
        units=[
            {
                "unit_id": "S001-01",
                "sample_id": "S001",
                "cluster_id": "O001",
                "conversation_type": "single_topic",
            },
            {
                "unit_id": "S002-01",
                "sample_id": "S002",
                "cluster_id": "O002",
                "conversation_type": "single_topic",
            },
            {
                "unit_id": "S003-01",
                "sample_id": "S003",
                "cluster_id": "O003",
                "conversation_type": "single_topic",
            },
        ],
        assignments={},
        similarities={},
    )
    new_scheme = SchemeResult(
        units=[
            {
                "unit_id": "S001-01",
                "sample_id": "S001",
                "cluster_id": "N001",
                "conversation_type": "single_topic",
            },
            {
                "unit_id": "S002-01",
                "sample_id": "S002",
                "cluster_id": "N001",
                "conversation_type": "single_topic",
            },
            {
                "unit_id": "S003-01",
                "sample_id": "S003",
                "cluster_id": "N003",
                "conversation_type": "single_topic",
            },
        ],
        assignments={},
        similarities={},
    )

    pairs = _select_pairs(rows, old_scheme, new_scheme, limit=10)

    assert pairs == [
        {
            "left_id": "S001",
            "right_id": "S003",
            "old_prediction": "不同主题",
            "new_prediction": "不同主题",
            "old_similarity": 0.0,
            "new_similarity": 0.0,
            "stratum": "共同拆分",
            "pair_id": "P001",
        }
    ]


def test_failed_model_fallback_does_not_trust_problem_description() -> None:
    result = _fallback_units(
        {
            "核心问题": "IMEI号全是0怎么判",
            "聊天内容": (
                "26/07/15 18:05:00:00 问题类型：质检问题 "
                "问题描述：IMEI号全是0怎么判 "
                "转人工原因：问题复杂不确定怎么问\n"
                "26/07/15 18:05:51:51 屏幕边缘发红是不是算老化\n"
                "26/07/15 18:06:20:20 后壳有保护壳留下的印怎么判"
            ),
            "产品类型": "平板",
        },
        "模拟模型失败",
    )

    topic = result["topics"][0]
    assert topic["normalized_issue"] == "问题待人工确认"
    assert "IMEI号全是0" not in topic["evidence_summary"]
    assert "屏幕边缘发红" in topic["evidence_summary"]
    assert topic["requires_review"] is True


def test_direct_reconcile_allows_scope_difference_for_same_topic_family() -> None:
    left = [
        "手机",
        "品类专用",
        "通用",
        "通用",
        "通用",
        "成色与回收标准",
        "标准判定",
    ]
    right = [
        "手机",
        "品牌专用",
        "iOS",
        "Apple",
        "通用",
        "成色与回收标准",
        "标准判定",
    ]

    assert _direct_reconcile_bucket_compatible(left, right)


def test_direct_reconcile_rejects_different_product_or_topic_family() -> None:
    base = [
        "手机",
        "品类专用",
        "通用",
        "通用",
        "通用",
        "成色与回收标准",
        "标准判定",
    ]
    different_product = [*base]
    different_product[0] = "笔记本"
    different_category = [*base]
    different_category[5] = "显示问题"

    assert not _direct_reconcile_bucket_compatible(base, different_product)
    assert not _direct_reconcile_bucket_compatible(base, different_category)


def test_direct_reconcile_scope_level_is_not_a_hard_conflict() -> None:
    generic = {
        "产品类型": "手机",
        "_原子适用范围类型": "品类专用",
        "_原子平台": "通用",
        "_原子品牌": "通用",
        "_原子机型范围": "通用",
        "_原子阈值例外": "无明确阈值",
    }
    apple = {
        "产品类型": "手机",
        "_原子适用范围类型": "品牌专用",
        "_原子平台": "iOS",
        "_原子品牌": "Apple",
        "_原子机型范围": "通用",
        "_原子阈值例外": "无明确阈值",
    }

    assert not _direct_reconcile_has_hard_conflict(generic, [apple])


def test_direct_reconcile_rejects_two_explicit_platforms() -> None:
    ios = {
        "产品类型": "手机",
        "_原子适用范围类型": "平台专用",
        "_原子平台": "iOS",
        "_原子品牌": "通用",
        "_原子机型范围": "通用",
        "_原子阈值例外": "无明确阈值",
    }
    android = {
        "产品类型": "手机",
        "_原子适用范围类型": "平台专用",
        "_原子平台": "Android",
        "_原子品牌": "通用",
        "_原子机型范围": "通用",
        "_原子阈值例外": "无明确阈值",
    }

    assert _direct_reconcile_has_hard_conflict(ios, [android])


def test_business_rules_keep_different_product_categories_separate() -> None:
    phone = _unit("PHONE", product_category="手机")
    camera = _unit("CAMERA", product_category="相机")

    result = _cluster_units(
        [phone, camera],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert result.assignments["PHONE"] != result.assignments["CAMERA"]


def test_universal_scope_does_not_allow_cross_product_clustering() -> None:
    phone = _unit(
        "PHONE",
        product_category="手机",
        scope_type="通用",
    )
    tablet = _unit(
        "TABLET",
        product_category="平板",
        scope_type="通用",
    )

    result = _cluster_units(
        [phone, tablet],
        threshold=0.01,
        enforce_business_rules=True,
    )

    assert result.assignments["PHONE"] != result.assignments["TABLET"]


def test_invalid_metadata_only_source_is_excluded() -> None:
    reason = _invalid_source_reason(
        {
            "核心问题": (
                "输入中未提供一线回收师与后台答疑人员的聊天记录原文，"
                "无法提取真实具体问题。"
            ),
            "判定结论": "无法基于现有信息作出判定。",
            "判定依据": (
                "输入信息中仅包含工单元数据，缺失历史咨询会话记录。"
            ),
        }
    )

    assert reason == "缺少有效咨询会话和具体问题，只有工单元数据，无法形成主题"


def test_valid_case_is_not_excluded_when_conclusion_requires_review() -> None:
    reason = _invalid_source_reason(
        {
            "核心问题": "屏幕边缘发红怎么判？",
            "判定结论": "当前图片不清晰，暂时无法作出判定。",
            "判定依据": "存在有效咨询会话，但需要补拍图片。",
        }
    )

    assert reason == ""


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


def test_media_cluster_validation_requires_media_analysis_when_requested() -> None:
    with pytest.raises(MimoError, match="media_analysis"):
        _validate_cluster_units(
            _cluster_unit_payload(),
            require_media_analysis=True,
        )


def test_cluster_ab_cache_refreshes_after_media_prompt_upgrade() -> None:
    row = {"样本ID": "S001"}

    assert not _cache_entry_is_current(
        row,
        {"S001": {"prompt_version": "old-text-only-version"}},
    )


def test_cluster_ab_can_retry_only_current_error_entries() -> None:
    row = {"样本ID": "S001"}
    cache = {
        "S001": {
            "prompt_version": CLUSTER_UNIT_PROMPT_VERSION,
            "status": "error",
        }
    }

    assert not _cache_entry_needs_refresh(row, cache)
    assert _cache_entry_needs_refresh(row, cache, retry_errors=True)


def test_text_only_cache_uses_distinct_prompt_version() -> None:
    row = {"样本ID": "S001"}
    cache = {
        "S001": {
            "prompt_version": TEXT_ONLY_PROMPT_VERSION,
            "status": "ok",
        }
    }

    assert _cache_entry_is_current(row, cache, text_only=True)
    assert not _cache_entry_is_current(row, cache)


def test_text_only_analysis_removes_media_fields() -> None:
    captured: dict[str, object] = {}

    class FakeConfig:
        model = "mimo-text"
        media_model = "mimo-media"

    class FakeResult:
        candidate = {
            "conversation_type": "single_topic",
            "topics": [{"normalized_issue": "测试主题"}],
        }
        request_audit = {"model": "mimo-text", "media": {"mode": "none"}}

    class FakeClient:
        config = FakeConfig()

        def analyze_cluster_units(self, row):
            captured.update(row)
            return FakeResult()

    result = _analyze_row(
        FakeClient(),
        {
            "样本ID": "S001",
            "图片链接": "https://example.com/a.jpg",
            "视频链接": "https://example.com/a.mp4",
            "上游媒体分析摘要": "媒体摘要",
        },
        text_only=True,
    )

    assert captured["图片链接"] == ""
    assert captured["视频链接"] == ""
    assert captured["上游媒体分析摘要"] == ""
    assert result["prompt_version"] == TEXT_ONLY_PROMPT_VERSION


def test_ab_direct_mimo_mode_reuses_cached_atomic_analysis() -> None:
    class FakeDirectMimo:
        config = SimpleNamespace(model="mimo-direct-test")

        def __init__(self) -> None:
            self.cluster_calls = 0

        def cluster_atomic_units(self, units):
            self.cluster_calls += 1
            return MimoLabelResult(
                candidate={
                    "clusters": [
                        {
                            "cluster_id": "C001",
                            "member_atomic_ids": [
                                unit["unit_id"]
                                for unit in units
                            ],
                            "merge_basis": "两个问题可以共用同一条知识。",
                        }
                    ],
                    "split_requests": [],
                    "review_requests": [],
                },
                request_audit={},
                response_audit={},
            )

    rows = [
        {
            "样本ID": sample_id,
            "工单ID": sample_id,
            "聊天内容": question,
            "产品类型": "手机",
        }
        for sample_id, question in (
            ("S001", "全新未拆封怎么判"),
            ("S002", "塑封完整是否算全新机"),
        )
    ]
    cache = {
        row["样本ID"]: {
            "candidate": {
                "conversation_type": "single_topic",
                "topics": [
                    {
                        "normalized_issue": "手机全新未拆封状态判定",
                        "product_category": "手机",
                        "scope_type": "品类专用",
                        "platform": "通用",
                        "brand": "通用",
                        "model_scope": "通用",
                        "category_l1": "成色与回收标准",
                        "category_l2": "成色判定",
                        "intent": "标准判定",
                        "subject": "手机整体包装",
                        "phenomenon": "全新未拆封",
                        "judgment_target": "判定是否属于全新机",
                        "resolution_mode": "依据包装状态判定",
                        "standard_path": "待确认",
                        "threshold_or_exception": "无明确阈值",
                        "evidence_summary": row["聊天内容"],
                        "confidence": 0.9,
                        "requires_review": False,
                    }
                ],
            }
        }
        for row in rows
    }
    reviewer = FakeDirectMimo()

    scheme, meta = _run_new_scheme_direct_mimo(rows, cache, reviewer)

    assert reviewer.cluster_calls == 1
    assert meta["direct_cluster_calls"] == 1
    assert len({unit["cluster_id"] for unit in scheme.units}) == 1
