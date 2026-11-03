import pytest

from app.routes.integration import _candidate_payload_with_taxonomy_defaults
from app.services.applicability import SELF_OPERATED_CATEGORY_NAMES
from app.services.candidate_business import DOORSTEP_PRODUCTS


def test_candidate_and_manhattan_doorstep_category_contracts_match():
    assert {group[1] for group in DOORSTEP_PRODUCTS} == SELF_OPERATED_CATEGORY_NAMES


@pytest.mark.parametrize("product", ["无人机", "扫地机器人", "数码相机"])
def test_non_doorstep_product_overrides_legacy_self_operated_default(product):
    original = {"knowledge": {
        "applicable_categories": [product], "business_type": "self_operated",
    }}
    _, knowledge = _candidate_payload_with_taxonomy_defaults(original)
    assert knowledge["business_type"] == "aggregated"
    assert original["knowledge"]["business_type"] == "self_operated"


@pytest.mark.parametrize("product", [
    "手机", "平板电脑", "智能手表", "耳机/耳麦", "笔记本", "游戏机",
    "游戏卡带", "单电/微单机身", "单反机身", "相机镜头", "手写笔", "学习机",
    "平板", "笔记本电脑", "蓝牙耳机",
])
def test_doorstep_products_keep_self_operated(product):
    _, knowledge = _candidate_payload_with_taxonomy_defaults(
        {"knowledge": {"applicable_categories": [product]}}
    )
    assert knowledge["business_type"] == "self_operated"


def test_structured_source_product_is_used_when_applicability_is_empty():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated"},
        "selection": {"answer_hub_replay_input": {"product_type": "无人机"}},
    })
    assert knowledge["business_type"] == "aggregated"


def test_no_product_evidence_keeps_legacy_self_operated_default():
    """没有产品证据的历史候选不能被静默改判为聚合回收。"""

    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"title": "历史候选", "category_id": "cat-process"},
    })
    assert knowledge["business_type"] == "self_operated"


def test_opaque_category_id_does_not_reclassify_without_product_evidence():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": ["1234"]},
    })
    assert knowledge["business_type"] == "self_operated"


def test_explicit_aggregate_business_keeps_doorstep_product():
    """聚合回收品类库包含手机等上门品类，显式聚合不能被品类改回自营。"""

    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "aggregated", "applicable_categories": ["手机"]},
    })
    assert knowledge["business_type"] == "aggregated"


def test_explicit_self_operated_with_non_doorstep_product_is_corrected():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {
            "business_type": "self_operated",
            "applicable_categories": ["智能手环"],
        },
    })
    assert knowledge["business_type"] == "aggregated"


def test_replay_source_line_takes_priority_over_product_inference():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"applicable_categories": ["手机"]},
        "selection": {"answer_hub_replay_input": {"回收业务层级": "聚合回收"}},
    })
    assert knowledge["business_type"] == "aggregated"


def test_replay_self_operated_line_with_non_doorstep_product_is_corrected():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"applicable_categories": ["智能手环"]},
        "selection": {"answer_hub_replay_input": {"回收业务层级": "自营回收"}},
    })
    assert knowledge["business_type"] == "aggregated"


def test_human_selected_business_type_is_not_overridden_by_replay_source():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {
            "business_type": "aggregated",
            "business_type_source": "human",
            "applicable_categories": ["手机"],
        },
        "selection": {"answer_hub_replay_input": {"回收业务层级": "自营回收"}},
    })
    assert knowledge["business_type"] == "aggregated"


@pytest.mark.parametrize("categories", [
    ["自营回收/手机"],
    [{"id": "a63d6e8b-ff10", "name": "手机"}],
    ["a63d6e8b-ff10"],
])
def test_category_paths_names_and_opaque_ids_are_not_unconfigured_products(categories):
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": categories},
    })
    assert knowledge["business_type"] == "self_operated"


def test_placeholder_source_category_does_not_hide_known_drone_product():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": ["无人机"]},
        "selection": {"answer_hub_replay_input": {"类目": "待确认", "产品类型": "无人机"}},
    })
    assert knowledge["business_type"] == "aggregated"


def test_manhattan_category_id_is_resolved_before_classification(monkeypatch):
    from app.routes import integration

    monkeypatch.setattr(
        integration,
        "_read_manhattan_cache",
        lambda: {
            "options_by_business_type": {
                "aggregated": {
                    "applicable_categories": [
                        {"categoryId": "901", "categoryName": "黄金"}
                    ]
                }
            }
        },
    )
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": ["901"]},
    })
    assert knowledge["business_type"] == "aggregated"


def test_unknown_opaque_category_id_keeps_legacy_business_type(monkeypatch):
    from app.routes import integration

    monkeypatch.setattr(integration, "_read_manhattan_cache", lambda: {})
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": ["901"]},
    })
    assert knowledge["business_type"] == "self_operated"


def test_mixed_category_name_and_id_are_resolved_together(monkeypatch):
    from app.routes import integration

    monkeypatch.setattr(
        integration,
        "_read_manhattan_cache",
        lambda: {
            "options_by_business_type": {
                "self_operated": {
                    "applicable_categories": [
                        {"categoryId": "119", "categoryName": "手机"}
                    ]
                }
            }
        },
    )
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"applicable_categories": ["119", "手机"]},
    })
    assert knowledge["business_type"] == "self_operated"
