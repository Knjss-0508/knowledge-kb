from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.applicability import (
    build_applicability_canonicalizer,
    canonicalize_applicability_values,
    filter_applicable_rows,
    resolve_applicability_scope,
    scope_keys,
)
from app.services.knowledge_dedup import search_embeddings


def _row(
    knowledge_id: str,
    *,
    categories=None,
    brands=None,
    models=None,
):
    return SimpleNamespace(
        knowledge_id=knowledge_id,
        applicable_categories=categories or [],
        applicable_brands=brands or [],
        applicable_models=models or [],
    )


def test_category_brand_model_funnel_excludes_cross_category_knowledge() -> None:
    cache = {
        "applicable_categories": [
            {
                "categoryId": 119,
                "categoryName": "平板电脑",
                "bizType": 0,
            },
            {
                "categoryId": 1100000016,
                "categoryName": "笔记本",
                "bizType": 0,
            },
        ],
        "brands_by_category": {
            "1100000016": [
                {"brandId": 10530, "brandName": "苹果"},
                {"brandId": 10544, "brandName": "联想"},
            ]
        },
        "models": [
            {
                "modelId": 7000,
                "modelName": "拯救者 R7000P 2022",
                "categoryId": "1100000016",
                "brandId": "10544",
            },
            {
                "modelId": 7001,
                "modelName": "拯救者 Y7000P 2022",
                "categoryId": "1100000016",
                "brandId": "10544",
            },
        ],
    }
    scope = resolve_applicability_scope(
        cache,
        "self_operated",
        category_values=["笔记本电脑"],
        brand_values=["联想"],
        model_values=["联想 拯救者 R7000P 2022"],
    )

    assert "1100000016" in scope["categories"]
    assert "10544" in scope["brands"]
    assert "7000" in scope["models"]

    rows = [
        _row("generic"),
        _row("laptop-generic", categories=["1100000016"]),
        _row("tablet", categories=["119"]),
        _row(
            "wrong-brand",
            categories=["笔记本"],
            brands=[{"brandId": 10530, "brandName": "苹果"}],
        ),
        _row(
            "wrong-model",
            categories=[{"categoryId": 1100000016}],
            brands=["10544"],
            models=["7001"],
        ),
        _row(
            "matched",
            categories=[{"categoryId": 1100000016, "categoryName": "笔记本"}],
            brands=[{"brandId": 10544, "brandName": "联想"}],
            models=[{"modelId": 7000, "modelName": "拯救者 R7000P 2022"}],
        ),
    ]

    filtered = filter_applicable_rows(
        rows,
        category_keys=scope["categories"],
        brand_keys=scope["brands"],
        model_keys=scope["models"],
    )

    assert [row.knowledge_id for row in filtered] == [
        "generic",
        "laptop-generic",
        "matched",
    ]


def test_missing_brand_and_model_context_skips_lower_funnel_layers() -> None:
    rows = [
        _row("generic"),
        _row("lenovo", categories=["笔记本"], brands=["联想"]),
        _row("apple", categories=["笔记本电脑"], brands=["苹果"]),
        _row("tablet", categories=["平板电脑"]),
    ]

    filtered = filter_applicable_rows(
        rows,
        category_keys={"笔记本", "笔记本电脑"},
    )

    assert [row.knowledge_id for row in filtered] == [
        "generic",
        "lenovo",
        "apple",
    ]


def test_canonicalize_applicability_names_to_unique_manhattan_ids() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 119, "categoryName": "平板电脑", "bizType": 0},
        ],
        "brands_by_category": {
            "119": [
                {"brandId": 10530, "brandName": "苹果"},
                {"brandId": 10531, "brandName": "小米"},
                {"brandId": 18472, "brandName": "红米"},
                {"brandId": 10534, "brandName": "vivo"},
            ],
        },
        "models": [
            {
                "modelId": 5316,
                "modelName": "iPad Pro 12.9 2021",
                "categoryId": 119,
                "brandId": 10530,
            },
        ],
    }

    canonical = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["平板电脑", "119"],
        brand_values=["苹果", "10530", "VIVO", "小米", "红米"],
        model_values=["iPad Pro 12.9 2021"],
    )

    assert canonical == {
        "categories": ["119"],
        "brands": ["10530", "10534", "10531", "18472"],
        "models": ["5316"],
    }


def test_canonicalize_applicability_preserves_unknown_or_ambiguous_values() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 119, "categoryName": "平板电脑", "bizType": 0},
        ],
        "brands_by_category": {
            "119": [
                {"brandId": 1, "brandName": "同名品牌"},
                {"brandId": 2, "brandName": "同名品牌"},
            ],
        },
        "models": [],
    }

    canonical = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["平板电脑"],
        brand_values=["同名品牌", "未收录品牌"],
    )

    assert canonical["categories"] == ["119"]
    assert canonical["brands"] == ["同名品牌", "未收录品牌"]


def test_explicit_empty_business_group_does_not_fall_back_to_other_business() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 119, "categoryName": "平板电脑", "bizType": 0},
        ],
        "brands_by_category": {
            "119": [{"brandId": 10530, "brandName": "苹果"}],
        },
        "models": [],
        "options_by_business_type": {
            "self_operated": {
                "applicable_categories": [
                    {"categoryId": 119, "categoryName": "平板电脑"},
                ],
                "brands_by_category": {
                    "119": [{"brandId": 10530, "brandName": "苹果"}],
                },
                "models": [],
            },
            "aggregated": {
                "applicable_categories": [],
                "brands_by_category": {},
                "models": [],
            },
        },
    }

    canonical = canonicalize_applicability_values(
        cache,
        "aggregated",
        category_values=["平板电脑"],
        brand_values=["苹果"],
    )

    assert canonical["categories"] == ["平板电脑"]
    assert canonical["brands"] == ["苹果"]


def test_unknown_parent_scope_does_not_resolve_children_across_groups() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 101, "categoryName": "手机", "bizType": 0},
        ],
        "brands_by_category": {
            "101": [{"brandId": 1, "brandName": "苹果"}],
        },
        "models": [
            {
                "modelId": 11,
                "modelName": "iPhone 11",
                "categoryId": 101,
                "brandId": 1,
            },
        ],
    }

    unknown_category = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["未收录类目"],
        brand_values=["苹果"],
        model_values=["iPhone 11"],
    )
    unknown_brand = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["手机"],
        brand_values=["未收录品牌"],
        model_values=["iPhone 11"],
    )

    assert unknown_category == {
        "categories": ["未收录类目"],
        "brands": ["苹果"],
        "models": ["iPhone 11"],
    }
    assert unknown_brand == {
        "categories": ["101"],
        "brands": ["未收录品牌"],
        "models": ["iPhone 11"],
    }


def test_ambiguous_parent_scope_does_not_resolve_children() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 101, "categoryName": "同名类目", "bizType": 0},
            {"categoryId": 102, "categoryName": "同名类目", "bizType": 0},
        ],
        "brands_by_category": {
            "101": [{"brandId": 1, "brandName": "苹果"}],
            "102": [{"brandId": 2, "brandName": "华为"}],
        },
        "models": [
            {
                "modelId": 11,
                "modelName": "iPhone 11",
                "categoryId": 101,
                "brandId": 1,
            },
        ],
    }

    canonical = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["同名类目"],
        brand_values=["苹果"],
        model_values=["iPhone 11"],
    )

    assert canonical == {
        "categories": ["同名类目"],
        "brands": ["苹果"],
        "models": ["iPhone 11"],
    }


def test_ambiguous_brand_scope_does_not_resolve_models() -> None:
    cache = {
        "applicable_categories": [
            {"categoryId": 101, "categoryName": "手机", "bizType": 0},
        ],
        "brands_by_category": {
            "101": [
                {"brandId": 1, "brandName": "同名品牌"},
                {"brandId": 2, "brandName": "同名品牌"},
            ],
        },
        "models": [
            {
                "modelId": 11,
                "modelName": "测试机型",
                "categoryId": 101,
                "brandId": 1,
            },
        ],
    }

    canonical = canonicalize_applicability_values(
        cache,
        "self_operated",
        category_values=["手机"],
        brand_values=["同名品牌"],
        model_values=["测试机型"],
    )

    assert canonical == {
        "categories": ["101"],
        "brands": ["同名品牌"],
        "models": ["测试机型"],
    }


def test_high_cardinality_model_index_is_reused_per_row() -> None:
    cache = {
        "options_by_business_type": {
            "self_operated": {
                "applicable_categories": [
                    {"categoryId": 119, "categoryName": "平板电脑"},
                ],
                "brands_by_category": {
                    "119": [{"brandId": 10530, "brandName": "苹果"}],
                },
                "models": [
                    {
                        "modelId": index,
                        "modelName": f"机型-{index}",
                        "categoryId": 119,
                        "brandId": 10530,
                    }
                    for index in range(5000)
                ],
            },
        },
    }
    canonicalizer = build_applicability_canonicalizer(
        cache,
        "self_operated",
    )

    with patch(
        "app.services.applicability.scope_keys",
        wraps=scope_keys,
    ) as tracked_scope_keys:
        for _ in range(200):
            result = canonicalizer.canonicalize(
                category_values=["平板电脑"],
                brand_values=["苹果"],
                model_values=["机型-4999"],
            )

    assert result["models"] == ["4999"]
    assert tracked_scope_keys.call_count == 400


@patch("app.services.knowledge_dedup._query_embedding")
def test_scope_funnel_runs_before_query_vector_ranking(query_embedding) -> None:
    db = MagicMock()
    scope_query = MagicMock()
    db.query.return_value = scope_query
    scope_query.filter.return_value = scope_query
    scope_query.all.return_value = [
        _row("tablet", categories=["平板电脑"]),
    ]

    result = search_embeddings(
        db,
        query="笔记本屏幕问题",
        knowledge_origin="headquarters_standard",
        business_type="self_operated",
        applicable_category_keys={"笔记本", "笔记本电脑"},
    )

    assert result == []
    query_embedding.assert_not_called()
