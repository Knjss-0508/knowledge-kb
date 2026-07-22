from __future__ import annotations

from answer_hub.mimo import _validate_cluster_units
from answer_hub.product_taxonomy import (
    UNKNOWN_PRODUCT_NAME,
    canonical_product_code,
    canonical_product_name,
    configured_product_names,
    normalize_product_scope,
)
from answer_hub.workflow import preprocess_source_rows


def test_default_taxonomy_contains_initial_ten_categories() -> None:
    assert configured_product_names() == (
        "手机",
        "平板",
        "笔记本",
        "相机机身",
        "相机镜头",
        "耳机",
        "手表",
        "游戏机",
        "手写笔",
        "学习机",
    )


def test_alias_and_code_resolve_to_stable_category() -> None:
    assert canonical_product_name("笔记本电脑") == "笔记本"
    assert canonical_product_name("camera_lens") == "相机镜头"
    assert canonical_product_code("触控笔") == "stylus"


def test_unknown_category_never_defaults_to_phone() -> None:
    row = preprocess_source_rows(
        [
            {
                "工单ID": "UNKNOWN-1",
                "聊天内容": "需要确认这个设备如何检测",
                "产品类型": "未知新品类",
            }
        ]
    )[0]

    assert row["产品类型"] == UNKNOWN_PRODUCT_NAME
    assert row["产品类型编码"] == ""
    assert "进入人工确认" in row["预处理备注"]


def test_scope_normalization_supports_non_mobile_platforms() -> None:
    assert normalize_product_scope("笔记本", "Windows") == "笔记本-Windows"
    assert normalize_product_scope("相机镜头", "相机镜头-尼康F卡口") == "相机镜头-尼康F卡口"


def test_mimo_cluster_validation_accepts_configured_new_category() -> None:
    result = _validate_cluster_units(
        {
            "conversation_type": "single_topic",
            "reason": "会话只涉及一个镜头检测问题",
            "topics": [
                {
                    "normalized_issue": "相机镜头｜镜片｜霉斑｜确认检测方法",
                    "product_category": "相机镜头",
                    "scope_type": "品类专用",
                    "platform": "通用",
                    "brand": "通用",
                    "model_scope": "通用",
                    "category_l1": "外观问题",
                    "category_l2": "镜片外观",
                    "intent": "检测核验",
                    "subject": "镜片",
                    "phenomenon": "疑似霉斑",
                    "judgment_target": "确认是否存在霉斑",
                    "resolution_mode": "按镜片外观标准核验",
                    "standard_path": "镜片外观检测",
                    "threshold_or_exception": "无明确阈值",
                    "evidence_summary": "会话要求确认镜片上的斑点是否为霉斑",
                    "confidence": 0.88,
                    "requires_review": False,
                }
            ],
        }
    )

    assert result["topics"][0]["product_category"] == "相机镜头"
