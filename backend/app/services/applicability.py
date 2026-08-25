from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
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
    grouped_options = cache.get("options_by_business_type")
    if isinstance(grouped_options, dict) and business_type in grouped_options:
        stored = grouped_options.get(business_type)
        stored = stored if isinstance(stored, dict) else {}
        return {
            "applicable_categories": list(
                stored.get("applicable_categories") or []
            ),
            "brands_by_category": dict(
                stored.get("brands_by_category") or {}
            ),
            "models": list(stored.get("models") or []),
        }

    raw_categories = [
        category
        for category in cache.get("applicable_categories") or []
        if isinstance(category, dict)
        and _business_type_for_category(category) in {None, business_type}
    ]
    categories = list(raw_categories)
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
    brands_by_category = {
        category_id: brands
        for category_id, brands in raw_brands_by_category.items()
        if not category_ids or normalize_scope_key(category_id) in category_ids
    }
    models = list(cache.get("models") or [])
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


def _option_identifier(option: dict[str, Any], kind: str) -> str:
    prefix = {
        "category": "category",
        "brand": "brand",
        "model": "model",
    }[kind]
    for key in (f"{prefix}Id", f"{prefix}_id", "id", "code", "value"):
        value = option.get(key)
        if value not in (None, ""):
            return str(value).strip()
    return ""


def _scope_value_text(value: Any, kind: str) -> str:
    if isinstance(value, dict):
        for key in _FIELD_KEYS[kind]:
            nested = value.get(key)
            if nested not in (None, ""):
                return str(nested).strip()
        return ""
    return "" if value is None else str(value).strip()


@dataclass(frozen=True)
class _CanonicalOption:
    identifier: str
    category_ids: frozenset[str] = frozenset()
    brand_ids: frozenset[str] = frozenset()


def _parent_identifiers(option: dict[str, Any], keys: tuple[str, ...]) -> set[str]:
    return {
        str(option.get(key)).strip()
        for key in keys
        if option.get(key) not in (None, "")
    }


def _index_option(
    index: dict[str, list[_CanonicalOption]],
    option: dict[str, Any],
    kind: str,
    *,
    category_ids: set[str] | None = None,
    brand_ids: set[str] | None = None,
) -> None:
    identifier = _option_identifier(option, kind)
    if not identifier:
        return
    candidate = _CanonicalOption(
        identifier=identifier,
        category_ids=frozenset(category_ids or ()),
        brand_ids=frozenset(brand_ids or ()),
    )
    for key in scope_keys(option, kind):
        index.setdefault(key, []).append(candidate)


def _canonicalize_indexed_values(
    values: Any,
    index: dict[str, list[_CanonicalOption]],
    kind: str,
    *,
    category_ids: set[str] | None = None,
    brand_ids: set[str] | None = None,
) -> tuple[list[str], set[str]]:
    canonical: list[str] = []
    seen: set[str] = set()
    matched_identifiers: set[str] = set()
    for value in _iter_values(values):
        raw_value = _scope_value_text(value, kind)
        if not raw_value:
            continue
        requested_key = normalize_scope_key(raw_value)
        identifiers = {
            candidate.identifier
            for candidate in index.get(requested_key, ())
            if (
                not category_ids
                or not candidate.category_ids
                or not candidate.category_ids.isdisjoint(category_ids)
            )
            if (
                not brand_ids
                or not candidate.brand_ids
                or not candidate.brand_ids.isdisjoint(brand_ids)
            )
        }
        if len(identifiers) == 1:
            resolved_value = next(iter(identifiers))
            matched_identifiers.add(resolved_value)
        else:
            resolved_value = raw_value
        if resolved_value in seen:
            continue
        seen.add(resolved_value)
        canonical.append(resolved_value)
    return canonical, matched_identifiers


class ApplicabilityCanonicalizer:
    """按业务缓存预建索引，供批量导入逐行复用。"""

    def __init__(self, cache: dict[str, Any], business_type: str):
        group = _cache_group(cache, business_type)
        self._category_index: dict[str, list[_CanonicalOption]] = {}
        self._brand_index: dict[str, list[_CanonicalOption]] = {}
        self._model_index: dict[str, list[_CanonicalOption]] = {}

        for category in group["applicable_categories"]:
            if isinstance(category, dict):
                _index_option(
                    self._category_index,
                    category,
                    "category",
                )

        for category_id, brands in (group["brands_by_category"] or {}).items():
            group_category_ids = {str(category_id).strip()}
            for brand in brands or []:
                if not isinstance(brand, dict):
                    continue
                category_ids = group_category_ids | _parent_identifiers(
                    brand,
                    ("categoryId", "category_id"),
                )
                _index_option(
                    self._brand_index,
                    brand,
                    "brand",
                    category_ids=category_ids,
                )

        for model in group["models"]:
            if not isinstance(model, dict):
                continue
            _index_option(
                self._model_index,
                model,
                "model",
                category_ids=_parent_identifiers(
                    model,
                    ("categoryId", "category_id"),
                ),
                brand_ids=_parent_identifiers(
                    model,
                    ("brandId", "brand_id"),
                ),
            )

    def canonicalize(
        self,
        *,
        category_values: Any = None,
        brand_values: Any = None,
        model_values: Any = None,
    ) -> dict[str, list[str]]:
        categories, category_ids = _canonicalize_indexed_values(
            category_values,
            self._category_index,
            "category",
        )
        requested_category_keys = scope_keys(category_values, "category")
        requested_brand_keys = scope_keys(brand_values, "brand")

        can_resolve_brands = not requested_category_keys or bool(category_ids)
        brands, brand_ids = _canonicalize_indexed_values(
            brand_values,
            self._brand_index if can_resolve_brands else {},
            "brand",
            category_ids=category_ids,
        )

        can_resolve_models = (
            can_resolve_brands
            and (not requested_brand_keys or bool(brand_ids))
        )
        models, _ = _canonicalize_indexed_values(
            model_values,
            self._model_index if can_resolve_models else {},
            "model",
            category_ids=category_ids,
            brand_ids=brand_ids,
        )

        return {
            "categories": categories,
            "brands": brands,
            "models": models,
        }


def build_applicability_canonicalizer(
    cache: dict[str, Any],
    business_type: str,
) -> ApplicabilityCanonicalizer:
    return ApplicabilityCanonicalizer(cache, business_type)


def canonicalize_applicability_values(
    cache: dict[str, Any],
    business_type: str,
    *,
    category_values: Any = None,
    brand_values: Any = None,
    model_values: Any = None,
) -> dict[str, list[str]]:
    """将唯一可识别的名称规范成 Manhattan ID，未知或歧义值原样保留。"""
    return build_applicability_canonicalizer(
        cache,
        business_type,
    ).canonicalize(
        category_values=category_values,
        brand_values=brand_values,
        model_values=model_values,
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
