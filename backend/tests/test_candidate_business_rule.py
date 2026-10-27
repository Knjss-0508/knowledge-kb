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


def test_opaque_category_id_does_not_reclassify_without_product_evidence():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "self_operated", "applicable_categories": ["1234"]},
    })
    assert knowledge["business_type"] == "self_operated"


def test_explicit_aggregate_business_for_doorstep_product_is_preserved():
    _, knowledge = _candidate_payload_with_taxonomy_defaults({
        "knowledge": {"business_type": "aggregated", "applicable_categories": ["手机"]},
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
