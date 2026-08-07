from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from typing import Any


_FIELD_KEYS = {
    "category": (
        "categoryId",
        "category_id",
        "id",
        "code",
        "value",
        "categoryName",
        "category_name",
        "name",
        "label",
        "title",
        "text",
    ),
    "brand": (
        "brandId",
        "brand_id",
        "id",
        "code",
        "value",
        "brandName",
        "brand_name",
        "name",
        "label",
        "title",
        "text",
    ),
    "model": (
        "modelId",
        "model_id",
        "id",
        "code",
        "value",
        "modelName",
        "model_name",
        "name",
        "label",
        "title",
        "text",
    ),
}
_EMPTY_SCOPE_KEYS = {
    "",
    "无",
    "未知",
    "暂无",
    "none",
    "null",
    "na",
}
_UNIVERSAL_SCOPE_KEYS = {
    "*",
    "all",
    "any",
    "全部",
    "所有",
    "通用",
    "全品类",
    "全品牌",
    "全机型",
}
_CATEGORY_ALIAS_GROUPS = (
    ("平板", "平板电脑"),
    ("笔记本", "笔记本电脑"),
)
_BUSINESS_TYPE_VALUES = {
    "self_operated": {"0", "selfoperated"},
    "aggregated": {"2", "aggregated"},
}


def normalize_scope_key(value: Any) -> str:
    raw_value = "" if value is None else str(value)
    normalized = unicodedata.normalize("NFKC", raw_value).casefold().strip()
    compact = "".join(character for character in normalized if character.isalnum())
    return "" if compact in _EMPTY_SCOPE_KEYS else compact


def _iter_values(value: Any) -> Iterable[Any]:
    if value is None:
        return ()
    if isinstance(value, (list, tuple, set)):
        return value
    return (value,)


def scope_keys(values: Any, kind: str) -> set[str]:
    keys: set[str] = set()
    field_keys = _FIELD_KEYS[kind]
    for value in _iter_values(values):
        if isinstance(value, dict):
            nested_values = [value.get(key) for key in field_keys if key in value]
            keys.update(scope_keys(nested_values, kind))
            continue
        normalized = normalize_scope_key(value)
        if normalized:
            keys.add(normalized)
    if kind == "category" and keys:
        for aliases in _CATEGORY_ALIAS_GROUPS:
            normalized_aliases = {normalize_scope_key(alias) for alias in aliases}
            if keys.intersection(normalized_aliases):
                keys.update(normalized_aliases)
    return keys


def _business_type_for_category(category: dict[str, Any]) -> str | None:
    raw_value = category.get("bizType")
    if raw_value is None:
        raw_value = category.get("biz_type")
    normalized = normalize_scope_key(raw_value)
    for business_type, values in _BUSINESS_TYPE_VALUES.items():
        if normalized in values:
            return business_type
    return None


def _cache_group(cache: dict[str, Any], business_type: str) -> dict[str, Any]:
    stored = (cache.get("options_by_business_type") or {}).get(business_type) or {}
    raw_categories = [
        category
        for category in cache.get("applicable_categories") or []
        if isinstance(category, dict)
        and _business_type_for_category(category) in {None, business_type}
    ]
    categories = list(stored.get("applicable_categories") or raw_categories)
    category_ids = {
        value
        for category in categories
        for value in scope_keys(
            [
                category.get("categoryId"),
                category.get("category_id"),
                category.get("id"),
            ],
            "category",
        )
    }

    raw_brands_by_category = cache.get("brands_by_category") or {}
    brands_by_category = stored.get("brands_by_category") or {
        category_id: brands
        for category_id, brands in raw_brands_by_category.items()
        if not category_ids or normalize_scope_key(category_id) in category_ids
    }
    models = list(stored.get("models") or cache.get("models") or [])
    return {
        "applicable_categories": categories,
        "brands_by_category": brands_by_category,
        "models": models,
    }


def _matching_options(
    options: Iterable[Any],
    requested_keys: set[str],
    kind: str,
) -> list[dict[str, Any]]:
    if not requested_keys:
        return []
    return [
        option
        for option in options
        if isinstance(option, dict)
        and not scope_keys(option, kind).isdisjoint(requested_keys)
    ]


def _identifier_keys(option: dict[str, Any], kind: str) -> set[str]:
    prefix = {
        "category": "category",
        "brand": "brand",
        "model": "model",
    }[kind]
    return scope_keys(
        [
            option.get(f"{prefix}Id"),
            option.get(f"{prefix}_id"),
            option.get("id"),
        ],
        kind,
    )


def resolve_applicability_scope(
    cache: dict[str, Any],
    business_type: str,
    *,
    category_values: Any = None,
    brand_values: Any = None,
    model_values: Any = None,
) -> dict[str, set[str]]:
    """Resolve request names and Manhattan IDs into comparable applicability keys."""
    group = _cache_group(cache, business_type)

    requested_categories = scope_keys(category_values, "category")
    matching_categories = _matching_options(
        group["applicable_categories"],
        requested_categories,
        "category",
    )
    category_keys = set(requested_categories)
    category_ids: set[str] = set()
    for category in matching_categories:
        category_keys.update(scope_keys(category, "category"))
        category_ids.update(_identifier_keys(category, "category"))

    brand_options: list[dict[str, Any]] = []
    for category_id, brands in (group["brands_by_category"] or {}).items():
        normalized_category_id = normalize_scope_key(category_id)
        if category_ids and normalized_category_id not in category_ids:
            continue
        brand_options.extend(
            brand for brand in brands or [] if isinstance(brand, dict)
        )
    requested_brands = scope_keys(brand_values, "brand")
    matching_brands = _matching_options(
        brand_options,
        requested_brands,
        "brand",
    )
    brand_keys = set(requested_brands)
    brand_ids: set[str] = set()
    for brand in matching_brands:
        brand_keys.update(scope_keys(brand, "brand"))
        brand_ids.update(_identifier_keys(brand, "brand"))

    requested_models = scope_keys(model_values, "model")
    for brand_key in requested_brands:
        if brand_key.isdigit():
            continue
        for model_key in tuple(requested_models):
            if model_key.startswith(brand_key) and len(model_key) > len(brand_key):
                requested_models.add(model_key[len(brand_key) :])

    matching_models: list[dict[str, Any]] = []
    for model in group["models"]:
        if not isinstance(model, dict):
            continue
        model_category_ids = scope_keys(
            [model.get("categoryId"), model.get("category_id")],
            "category",
        )
        if category_ids and model_category_ids and model_category_ids.isdisjoint(category_ids):
            continue
        model_brand_ids = scope_keys(
            [model.get("brandId"), model.get("brand_id")],
            "brand",
        )
        if brand_ids and model_brand_ids and model_brand_ids.isdisjoint(brand_ids):
            continue
        if not scope_keys(model, "model").isdisjoint(requested_models):
            matching_models.append(model)

    model_keys = set(requested_models)
    for model in matching_models:
        model_keys.update(scope_keys(model, "model"))

    return {
        "categories": category_keys,
        "brands": brand_keys,
        "models": model_keys,
    }


def applicability_layer_matches(
    stored_values: Any,
    requested_keys: set[str] | None,
    kind: str,
) -> bool:
    if not requested_keys:
        return True
    stored_keys = scope_keys(stored_values, kind)
    if not stored_keys or stored_keys.intersection(_UNIVERSAL_SCOPE_KEYS):
        return True
    return not stored_keys.isdisjoint(requested_keys)


def filter_applicable_rows(
    rows: Iterable[Any],
    *,
    category_keys: set[str] | None = None,
    brand_keys: set[str] | None = None,
    model_keys: set[str] | None = None,
) -> list[Any]:
    """Apply category -> brand -> model as an ordered applicability funnel."""
    filtered = list(rows)
    for attribute, requested_keys, kind in (
        ("applicable_categories", category_keys, "category"),
        ("applicable_brands", brand_keys, "brand"),
        ("applicable_models", model_keys, "model"),
    ):
        if not requested_keys:
            continue
        filtered = [
            row
            for row in filtered
            if applicability_layer_matches(
                getattr(row, attribute, None),
                requested_keys,
                kind,
            )
        ]
        if not filtered:
            break
    return filtered
