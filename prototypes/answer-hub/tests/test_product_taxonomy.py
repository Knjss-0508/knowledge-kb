from __future__ import annotations

import json
from pathlib import Path

import pytest

from answer_hub.mimo import _validate_cluster_units
from answer_hub.product_taxonomy import (
    DEFAULT_PRODUCT_TAXONOMY_PATH,
    UNKNOWN_PRODUCT_NAME,
    canonical_product_code,
    canonical_product_name,
    configured_product_names,
    normalize_product_scope,
)
from answer_hub.workflow import preprocess_source_rows


def test_default_taxonomy_contains_configured_categories() -> None:
    assert configured_product_names() == (
        "手机",
        "平板电脑",
        "智能手表",
        "耳机/耳麦",
        "笔记本",
        "游戏机",
        "游戏卡带",
        "单电/微单机身",
        "单反机身",
        "相机镜头",
        "手写笔",
        "学习机",
    )


def test_alias_and_code_resolve_to_stable_category() -> None:
    assert canonical_product_name("平板") == "平板电脑"
    assert canonical_product_name("手表") == "智能手表"
    assert canonical_product_name("耳机") == "耳机/耳麦"
    assert canonical_product_name("耳麦") == "耳机/耳麦"
    assert canonical_product_name("笔记本电脑") == "笔记本"
    assert canonical_product_name("电脑") == UNKNOWN_PRODUCT_NAME
    assert canonical_product_name("微单") == "单电/微单机身"
    assert canonical_product_name("单电机身") == "单电/微单机身"
    assert canonical_product_name("单反") == "单反机身"
    assert canonical_product_name("镜头") == "相机镜头"
    assert canonical_product_name("camera_lens") == "相机镜头"
    assert canonical_product_name("Switch卡带") == "游戏卡带"
    assert canonical_product_code("触控笔") == "stylus"


def test_ambiguous_legacy_camera_body_never_guesses_a_new_category() -> None:
    assert canonical_product_name("相机") == UNKNOWN_PRODUCT_NAME
    assert canonical_product_name("相机机身") == UNKNOWN_PRODUCT_NAME
    assert canonical_product_code("camera_body") == ""


def test_source_preprocessing_maps_business_aliases_to_configured_products() -> None:
    rows = preprocess_source_rows(
        [
            {
                "工单ID": "LAPTOP-1",
                "聊天内容": "笔记本电脑配置怎么查",
                "产品类型": "笔记本电脑",
            },
            {"工单ID": "MIRRORLESS-1", "聊天内容": "微单机身编号怎么核对", "产品类型": "微单"},
            {"工单ID": "DSLR-1", "聊天内容": "单反机身编号怎么核对", "产品类型": "单反"},
            {"工单ID": "LENS-1", "聊天内容": "镜头霉斑怎么判断", "产品类型": "镜头"},
        ]
    )

    assert [(row["产品类型"], row["产品类型编码"]) for row in rows] == [
        ("笔记本", "laptop"),
        ("单电/微单机身", "mirrorless_camera_body"),
        ("单反机身", "dslr_camera_body"),
        ("相机镜头", "camera_lens"),
    ]
    assert {row["回收业务层级"] for row in rows} == {"自营回收"}
    assert {row["回收业务层级编码"] for row in rows} == {"self_operated"}


def test_source_preprocessing_prefers_category_over_legacy_product_type() -> None:
    rows = preprocess_source_rows(
        [
            {
                "工单ID": "LAPTOP-CATEGORY-1",
                "聊天内容": "笔记本底部外壳破损怎么判定",
                "类目": "笔记本",
                "产品类型": "电脑",
            },
            {
                "工单ID": "TABLET-CATEGORY-1",
                "聊天内容": "平板屏幕怎么判定",
                "类目": "平板电脑",
                "产品类型": "平板",
            },
        ]
    )

    assert [(row["产品类型"], row["产品类型编码"]) for row in rows] == [
        ("笔记本", "laptop"),
        ("平板电脑", "tablet"),
    ]
    assert {row["回收业务层级"] for row in rows} == {"自营回收"}
    assert all(row["产品类型原值"] in {"电脑", "平板"} for row in rows)
    assert all("类目优先" in row["预处理备注"] for row in rows)


def test_custom_taxonomy_cannot_change_fixed_categories_or_add_ambiguous_camera_alias(
    tmp_path: Path,
) -> None:
    payload = json.loads(DEFAULT_PRODUCT_TAXONOMY_PATH.read_text(encoding="utf-8"))
    changed_names = json.loads(json.dumps(payload, ensure_ascii=False))
    changed_names["categories"][0]["name"] = "手机设备"
    changed_path = tmp_path / "changed-names.json"
    changed_path.write_text(
        json.dumps(changed_names, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="固定12项"):
        configured_product_names(changed_path)

    ambiguous_alias = json.loads(json.dumps(payload, ensure_ascii=False))
    mirrorless = next(
        row
        for row in ambiguous_alias["categories"]
        if row["name"] == "单电/微单机身"
    )
    mirrorless["aliases"].append("相机机身")
    ambiguous_path = tmp_path / "ambiguous-alias.json"
    ambiguous_path.write_text(
        json.dumps(ambiguous_alias, ensure_ascii=False),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="模糊相机别名"):
        configured_product_names(ambiguous_path)


def test_taxonomy_cache_refreshes_when_same_file_content_changes(
    tmp_path: Path,
) -> None:
    payload = json.loads(DEFAULT_PRODUCT_TAXONOMY_PATH.read_text(encoding="utf-8"))
    custom_path = tmp_path / "taxonomy.json"
    payload["categories"][0]["aliases"].append("测试手机别名甲")
    custom_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    assert canonical_product_name("测试手机别名甲", custom_path) == "手机"

    payload["categories"][0]["aliases"][-1] = "测试手机别名乙乙"
    custom_path.write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )
    assert canonical_product_name("测试手机别名甲", custom_path) == UNKNOWN_PRODUCT_NAME
    assert canonical_product_name("测试手机别名乙乙", custom_path) == "手机"


def test_source_preprocessing_sends_ambiguous_camera_body_to_manual_confirmation() -> None:
    row = preprocess_source_rows(
        [
            {
                "工单ID": "CAMERA-AMBIGUOUS-1",
                "聊天内容": "相机机身编号怎么核对",
                "产品类型": "相机机身",
            }
        ]
    )[0]

    assert row["产品类型"] == UNKNOWN_PRODUCT_NAME
    assert row["产品类型编码"] == ""
    assert "进入人工确认" in row["预处理备注"]


def test_source_preprocessing_reserves_aggregate_business_hierarchy() -> None:
    explicit = preprocess_source_rows(
        [
            {
                "工单ID": "AGG-PHONE-1",
                "聊天内容": "聚合回收手机屏幕怎么判",
                "回收业务层级": "聚合回收",
                "产品类型": "手机",
            }
        ]
    )[0]
    legacy = preprocess_source_rows(
        [
            {
                "工单ID": "AGG-LEGACY-1",
                "聊天内容": "聚合回收节点怎么处理",
                "产品类型": "聚合回收",
            }
        ]
    )[0]

    assert explicit["回收业务层级"] == "聚合回收"
    assert explicit["回收业务层级编码"] == "aggregate"
    assert explicit["产品类型"] == "手机"
    assert "聚合回收产品品类口径尚未配置" in explicit["预处理备注"]
    assert legacy["回收业务层级"] == "聚合回收"
    assert legacy["产品类型"] == UNKNOWN_PRODUCT_NAME


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


def test_scope_normalization_only_returns_one_of_the_configured_categories() -> None:
    assert normalize_product_scope("笔记本", "Windows") == "笔记本"
    assert normalize_product_scope("相机镜头", "相机镜头-尼康F卡口") == "相机镜头"
    assert normalize_product_scope("耳机", "AirPods 一代") == "耳机/耳麦"
    assert normalize_product_scope("相机机身", "通用") == UNKNOWN_PRODUCT_NAME


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
