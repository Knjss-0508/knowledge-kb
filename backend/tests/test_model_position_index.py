"""车型位置索引的回归测试。

原实现对每个请求遍历全部车型（生产环境实测 31,321 个），每个车型调用
scope_keys 三次，一次检索在应用层花掉约 122 ms。索引把「归一化车型键 ->
车型在列表中的位置」预先算好，只对命中的车型做类目/品牌校验。

这里用一份小规模的合成缓存，断言索引实现与「暴力遍历」的参考实现
在所有输入组合下结果一致，并断言缓存对象换新后索引会重建。
"""
from app.services import applicability
from app.services.applicability import resolve_applicability_scope

CATEGORIES = [
    {"categoryId": 119, "categoryName": "平板电脑", "bizType": 0},
    {"categoryId": 1100000016, "categoryName": "笔记本", "bizType": 0},
]

BRANDS_BY_CATEGORY = {
    "119": [
        {"brandId": 10530, "brandName": "苹果", "categoryId": 119},
        {"brandId": 20001, "brandName": "华为", "categoryId": 119},
    ],
    "1100000016": [
        {"brandId": 10530, "brandName": "苹果", "categoryId": 1100000016},
        {"brandId": 30001, "brandName": "联想", "categoryId": 1100000016},
    ],
}

MODELS = [
    {"modelId": 9001, "modelName": "iPad Pro 11", "brandId": 10530, "brandName": "苹果", "categoryId": 119},
    {"modelId": 9002, "modelName": "iPad Air", "brandId": 10530, "brandName": "苹果", "categoryId": 119},
    {"modelId": 9003, "modelName": "MatePad 11", "brandId": 20001, "brandName": "华为", "categoryId": 119},
    {"modelId": 9101, "modelName": "MacBook Pro 14", "brandId": 10530, "brandName": "苹果", "categoryId": 1100000016},
    {"modelId": 9102, "modelName": "ThinkPad X1", "brandId": 30001, "brandName": "联想", "categoryId": 1100000016},
    # 非 dict 元素：索引与参考实现都必须跳过
    "not-a-dict",
    None,
    {"modelId": 9001, "modelName": "iPad Pro 11", "brandId": 10530, "brandName": "苹果", "categoryId": 119},
]


def _cache() -> dict:
    return {
        "updated_at": "2026-09-23T00:00:00Z",
        "applicable_categories": CATEGORIES,
        "brands_by_category": BRANDS_BY_CATEGORY,
        "models": MODELS,
        "options_by_business_type": {
            "self_operated": {
                "applicable_categories": CATEGORIES,
                "brands_by_category": BRANDS_BY_CATEGORY,
                "models": MODELS,
            }
        },
    }


def _reference_scope(cache, business_type, *, category_values=None, brand_values=None, model_values=None):
    """暴力遍历版本，语义等同改动前的实现（只用于对拍）。"""
    group = applicability._cache_group(cache, business_type)

    requested_categories = applicability.scope_keys(category_values, "category")
    matching_categories = applicability._matching_options(
        group["applicable_categories"], requested_categories, "category"
    )
    category_keys = set(requested_categories)
    category_ids: set[str] = set()
    for category in matching_categories:
        category_keys.update(applicability.scope_keys(category, "category"))
        category_ids.update(applicability._identifier_keys(category, "category"))

    brand_options = []
    for category_id, brands in (group["brands_by_category"] or {}).items():
        if category_ids and applicability.normalize_scope_key(category_id) not in category_ids:
            continue
        brand_options.extend(b for b in brands or [] if isinstance(b, dict))
    requested_brands = applicability.scope_keys(brand_values, "brand")
    matching_brands = applicability._matching_options(brand_options, requested_brands, "brand")
    brand_keys = set(requested_brands)
    brand_ids: set[str] = set()
    for brand in matching_brands:
        brand_keys.update(applicability.scope_keys(brand, "brand"))
        brand_ids.update(applicability._identifier_keys(brand, "brand"))

    requested_models = applicability.scope_keys(model_values, "model")
    for brand_key in requested_brands:
        if brand_key.isdigit():
            continue
        for model_key in tuple(requested_models):
            if model_key.startswith(brand_key) and len(model_key) > len(brand_key):
                requested_models.add(model_key[len(brand_key):])

    matching_models = []
    for model in group["models"]:
        if not isinstance(model, dict):
            continue
        model_category_ids = applicability.scope_keys(
            [model.get("categoryId"), model.get("category_id")], "category"
        )
        if category_ids and model_category_ids and model_category_ids.isdisjoint(category_ids):
            continue
        model_brand_ids = applicability.scope_keys(
            [model.get("brandId"), model.get("brand_id")], "brand"
        )
        if brand_ids and model_brand_ids and model_brand_ids.isdisjoint(brand_ids):
            continue
        if not applicability.scope_keys(model, "model").isdisjoint(requested_models):
            matching_models.append(model)

    model_keys = set(requested_models)
    for model in matching_models:
        model_keys.update(applicability.scope_keys(model, "model"))

    return {
        "categories": category_keys,
        "brands": brand_keys,
        "models": model_keys,
    }


# 覆盖：空值、类目 ID/名称、品牌 ID/名称、车型 ID/名称、跨类目组合、
# 品牌前缀、通用键、无意义值、多值、字典、嵌套列表
CASES = [
    (None, None, None),
    ((), (), ()),
    ([], [], []),
    (("",), ("",), ("",)),
    ((119,), None, None),
    (("平板电脑",), None, None),
    ((1100000016,), None, None),
    (("笔记本",), None, None),
    (None, (10530,), None),
    (None, ("苹果",), None),
    (None, ("联想",), None),
    (None, None, (9001,)),
    (None, None, ("iPad Pro 11",)),
    (None, None, ("MatePad 11",)),
    (None, None, (9102,)),
    ((119,), (10530,), (9001,)),
    ((1100000016,), (10530,), (9001,)),          # 车型与类目冲突
    ((119,), (30001,), None),                    # 品牌与类目冲突
    (None, (10530,), (9001, 9101)),              # 跨类目的同一品牌
    (None, None, (9001, 9002, 9003, 9101, 9102)),
    (None, ("苹果",), ("iPad",)),                 # 品牌前缀逻辑
    ((119,), None, ("iPad",)),
    (("全部",), ("全部",), ("全部",)),             # 通用键
    (("未知",), ("无",), ("*",)),
    (("不存在的类目",), ("不存在的品牌",), ("不存在的车型",)),
    (None, None, (9001, 999999)),
    (None, None, (MODELS[0],)),                   # 字典
    ((CATEGORIES[0],), None, None),               # 字典
    (([119],), ([10530],), ([9001],)),            # 嵌套列表
]


def test_index_matches_brute_force_for_every_case() -> None:
    cache = _cache()
    for category_values, brand_values, model_values in CASES:
        expected = _reference_scope(
            cache,
            "self_operated",
            category_values=category_values,
            brand_values=brand_values,
            model_values=model_values,
        )
        actual = resolve_applicability_scope(
            cache,
            "self_operated",
            category_values=category_values,
            brand_values=brand_values,
            model_values=model_values,
        )
        assert actual == expected, (category_values, brand_values, model_values)


def test_index_is_rebuilt_when_cache_object_changes() -> None:
    cache_a = _cache()
    resolve_applicability_scope(cache_a, "self_operated", model_values=(9001,))
    index_a = applicability._MODEL_POSITION_INDEX_MEMO["index"]
    assert index_a is not None
    assert applicability._MODEL_POSITION_INDEX_MEMO["cache"] is cache_a

    # 同一对象重复调用必须命中同一个索引
    resolve_applicability_scope(cache_a, "self_operated", model_values=(9001,))
    assert applicability._MODEL_POSITION_INDEX_MEMO["index"] is index_a

    # 换一个缓存对象（等价于缓存文件被更新）必须重建
    cache_b = _cache()
    cache_b["options_by_business_type"]["self_operated"]["models"] = MODELS[:3]
    resolve_applicability_scope(cache_b, "self_operated", model_values=(9001,))
    index_b = applicability._MODEL_POSITION_INDEX_MEMO["index"]
    assert index_b is not index_a
    assert applicability._MODEL_POSITION_INDEX_MEMO["cache"] is cache_b
    # 新索引不应包含被移除车型的键
    assert "9102" not in index_b


def test_index_handles_non_dict_and_empty_models() -> None:
    cache = _cache()
    # 车型列表里的非 dict 元素不能出现在结果中
    scope = resolve_applicability_scope(cache, "self_operated", model_values=(9001,))
    assert "9001" in scope["models"]

    empty = _cache()
    empty["options_by_business_type"]["self_operated"]["models"] = []
    scope = resolve_applicability_scope(empty, "self_operated", model_values=(9001,))
    assert scope["models"] == {"9001"}


def test_no_model_values_skips_index_entirely() -> None:
    cache = _cache()
    applicability._MODEL_POSITION_INDEX_MEMO["index"] = None
    applicability._MODEL_POSITION_INDEX_MEMO["cache"] = None
    scope = resolve_applicability_scope(cache, "self_operated")
    assert scope["models"] == set()
    # 没有指定车型时不应构建索引
    assert applicability._MODEL_POSITION_INDEX_MEMO["index"] is None
